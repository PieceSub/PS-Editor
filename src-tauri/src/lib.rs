use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::path::Path;
use std::process::Stdio;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, Sender};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

mod projects;

/// Sidecar iletişim süpervizörü.
///
/// Mimari karar: Tauri ile Python arasındaki iletişim, stdin/stdout üzerinden
/// JSON Lines protokolüyle yapılır. Bunun yerine yerel HTTP portu seçmedik çünkü:
///  1. Tauri'nin sidecar mekanizması (tauri-plugin-shell) stdin/stdout pipe'larını
///     doğal olarak yönetir (CommandEvent::Stdout/Stderr) — ekstra ayar gerektirmez.
///  2. Port çakışması, CORS, firewall izni, localhost güvenlik endişesi yoktur.
///  3. Dev (venv python) ve prod (PyInstaller exe) modlarında aynı kod yolu kullanılır.
/// İleride FastAPI eklendiğinde: stdin/stdout kontrol kanalı (start/stop/health)
/// olarak kalır, büyük veri (OCR/çeviri) HTTP üzerinden akar — topluluktaki
/// yerleşik desen budur (ör. dieharders/example-tauri-v2-python-server-sidecar).
enum PyProcess {
    /// Üretim modu: paketlenmiş sidecar binary'si (tauri-plugin-shell).
    Shell { child: CommandChild },
    /// Geliştirme modu: venv'deki python + sidecar.py kaynak dosyası.
    Std { child: std::process::Child },
}

impl PyProcess {
    fn write_line(&mut self, line: &str) -> Result<(), String> {
        match self {
            Self::Shell { child } => child.write(line.as_bytes()).map_err(|e| e.to_string()),
            Self::Std { child } => child
                .stdin
                .as_mut()
                .ok_or_else(|| "çocuk sürecin stdin'i yok".to_string())?
                .write_all(line.as_bytes())
                .map_err(|e| e.to_string()),
        }
    }

    fn kill(self) {
        match self {
            Self::Shell { child } => {
                let _ = child.kill();
            }
            Self::Std { mut child } => {
                let _ = child.kill();
            }
        }
    }
}

struct PyState {
    /// Sidecar sürecine yazma ucu (stdin).
    stdin: Mutex<Option<PyProcess>>,
    /// İstek numarası üreteci (yanıt eşleştirme).
    next_id: AtomicU64,
    /// Bekleyen istekler: id -> yanıt kanalı.
    pending: Mutex<HashMap<u64, Sender<Value>>>,
}

impl PyState {
    fn new() -> Self {
        Self {
            stdin: Mutex::new(None),
            next_id: AtomicU64::new(1),
            pending: Mutex::new(HashMap::new()),
        }
    }

    fn attach(&self, process: PyProcess) {
        *self.stdin.lock().unwrap() = Some(process);
    }

    fn kill(&self) {
        if let Some(process) = self.stdin.lock().unwrap().take() {
            process.kill();
        }
    }
}

/// Sidecar stdout'undan gelen bir satırı işler:
///  - `{"id": N, ...}`   → bekleyen isteğe yanıt olarak iletir.
///  - `{"event": ...}`   → ön yüze `python-event` olarak yayınlar.
fn handle_line(app: &AppHandle, state: &PyState, bytes: &[u8]) {
    let text = String::from_utf8_lossy(bytes);
    let trimmed = text.trim();
    if trimmed.is_empty() {
        return;
    }
    match serde_json::from_str::<Value>(trimmed) {
        Ok(value) => {
            if let Some(id) = value.get("id").and_then(Value::as_u64) {
                if let Some(tx) = state.pending.lock().unwrap().remove(&id) {
                    let _ = tx.send(value);
                } else {
                    println!("[sidecar] eşleşmeyen yanıt id={id}: {trimmed}");
                }
            } else if value.get("event").is_some() {
                println!("[sidecar] olay: {trimmed}");
                let _ = app.emit("python-event", value);
            } else {
                println!("[sidecar] tanımsız mesaj: {trimmed}");
            }
        }
        Err(e) => eprintln!("[sidecar] geçersiz JSON satırı ({e}): {trimmed}"),
    }
}

/// Python sidecar'ını başlatır. Önce paketlenmiş binary'yi (prod), bulamazsa
/// venv python'u (dev) dener.
fn spawn_python(app: &AppHandle, state: &Arc<PyState>) -> Result<(), String> {
    // Geliştirme kısayolu: PS_EDITOR_PY_SOURCE=venv ortam değişkeni, PyInstaller
    // derlemesi beklemeden doğrudan sanal ortamdaki Python'u kullanmayı sağlar
    // (Python tarafında hızlı iterasyon için).
    if std::env::var("PS_EDITOR_PY_SOURCE").as_deref() == Ok("venv") {
        return spawn_python_dev(app, state);
    }

    // 1) Üretim: bundle.externalBin -> src-tauri/binaries/python-sidecar-<triple>.exe
    if let Ok(command) = app
        .shell()
        // Konsol kod sayfasına (chcp) bağımlı kalmadan UTF-8 zorla:
        // PYTHONIOENCODING stdin/stdout/stderr'ı geleceğe dönük olarak
        // UTF-8'e sabitler, PYTHONUTF8 ise PEP 540 UTF-8 modunu açar.
        .sidecar("python-sidecar")
        .map(|cmd| cmd.env("PYTHONIOENCODING", "utf-8").env("PYTHONUTF8", "1"))
    {
        match command.spawn() {
            Ok((mut rx, child)) => {
                state.attach(PyProcess::Shell { child });
                let handle = app.clone();
                let task_state = state.clone();
                tauri::async_runtime::spawn(async move {
                    while let Some(event) = rx.recv().await {
                        match event {
                            CommandEvent::Stdout(line) => {
                                handle_line(&handle, &task_state, &line)
                            }
                            CommandEvent::Stderr(line) => {
                                eprintln!("[sidecar stderr] {}", String::from_utf8_lossy(&line))
                            }
                            CommandEvent::Error(e) => eprintln!("[sidecar error] {e}"),
                            CommandEvent::Terminated(p) => {
                                println!("[sidecar] süreç sonlandı: {:?}", p.code);
                                let _ = handle.emit(
                                    "python-event",
                                    json!({ "name": "exit", "payload": format!("kod {:?}", p.code) }),
                                );
                                break;
                            }
                            _ => {}
                        }
                    }
                });
                println!("[sidecar] paketlenmiş binary ile başlatıldı");
                return Ok(());
            }
            Err(e) => {
                eprintln!("[sidecar] paketli binary başlatılamadı ({e}); dev moduna geçiliyor…");
            }
        }
    }

    spawn_python_dev(app, state)
}

/// Geliştirme modu: python/.venv + sidecar.py kaynak dosyası.
fn spawn_python_dev(app: &AppHandle, state: &Arc<PyState>) -> Result<(), String> {
    let manifest = env!("CARGO_MANIFEST_DIR");
    let python = Path::new(manifest)
        .join("..")
        .join("python")
        .join(".venv")
        .join("Scripts")
        .join("python.exe");
    if !python.exists() {
        return Err(format!(
            "Python sanal ortamı bulunamadı: {}. Önce 'npm run setup:python' çalıştırın.",
            python.display()
        ));
    }
    let script = Path::new(manifest).join("..").join("python").join("sidecar.py");

    let mut child = std::process::Command::new(&python)
        .arg(&script)
        .env("PYTHONUNBUFFERED", "1")
        // Unicode yollar için stdin/stdout UTF-8'e sabitlenmeli; aksi halde
        // Windows ANSI kod sayfası (cp125x) devreye girip UTF-8 baytlarını
        // bozar (ör. "Masaüstü" -> "MasaÃ¼stÃ¼").
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUTF8", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .map_err(|e| format!("Python başlatılamadı: {e}"))?;

    let stdout = child.stdout.take().ok_or("çocuk sürecin stdout'u alınamadı")?;
    state.attach(PyProcess::Std { child });
    let handle = app.clone();
    let task_state = state.clone();
    std::thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for line in reader.lines() {
            match line {
                Ok(line) => handle_line(&handle, &task_state, line.as_bytes()),
                Err(e) => {
                    eprintln!("[sidecar] stdout okuma hatası: {e}");
                    break;
                }
            }
        }
        let _ = handle.emit("python-event", json!({ "name": "exit", "payload": "stdout kapandı" }));
    });

    println!("[sidecar] geliştirme modu (venv python) ile başlatıldı: {}", python.display());
    Ok(())
}

/// İsteği sidecar'a gönderir ve yanıtı bekler. (Bloklama — async çağıranlar
/// spawn_blocking kullanmalı.)
fn send_request(state: &PyState, cmd: &str, payload: Value) -> Result<Value, String> {
    let id = state.next_id.fetch_add(1, Ordering::SeqCst);
    let (tx, rx) = mpsc::channel::<Value>();
    {
        let mut pending = state.pending.lock().unwrap();
        pending.insert(id, tx);
    }

    let line = json!({ "id": id, "cmd": cmd, "payload": payload }).to_string() + "\n";
    let write_result = match state.stdin.lock().unwrap().as_mut() {
        Some(process) => process.write_line(&line),
        None => Err("Python servisi çalışmıyor".to_string()),
    };
    if let Err(e) = write_result {
        state.pending.lock().unwrap().remove(&id);
        return Err(format!("Sidecar'a yazılamadı: {e}"));
    }

    match rx.recv_timeout(Duration::from_secs(60)) {
        Ok(resp) => {
            if resp.get("ok").and_then(Value::as_bool).unwrap_or(false) {
                Ok(resp.get("result").cloned().unwrap_or(Value::Null))
            } else {
                Err(resp
                    .get("error")
                    .and_then(Value::as_str)
                    .unwrap_or("bilinmeyen Python hatası")
                    .to_string())
            }
        }
        Err(_) => {
            state.pending.lock().unwrap().remove(&id);
            Err("Python yanıt vermedi (zaman aşımı)".to_string())
        }
    }
}

/// Ön yüzdden gelen genel komut köprüsü. `cmd` Python tarafındaki
/// komut adıyla birebir eşleşmelidir (örn. "hello", "check_cuda").
#[tauri::command]
async fn python_request(
    state: tauri::State<'_, Arc<PyState>>,
    cmd: String,
    payload: Option<Value>,
) -> Result<Value, String> {
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || send_request(&state, &cmd, payload.unwrap_or(Value::Null)))
        .await
        .map_err(|e| format!("İstek işleme hatası: {e}"))?
}

/// Sidecar süreci çalışıyor mu?
#[tauri::command]
fn python_status(state: tauri::State<'_, Arc<PyState>>) -> bool {
    state.stdin.lock().unwrap().is_some()
}

const IMAGE_EXTS: [&str; 6] = ["png", "jpg", "jpeg", "webp", "bmp", "gif"];

/// Seçilen klasördeki görsel dosyaları sıralı döndürür (batch işleme için).
#[tauri::command]
fn list_images(dir: String) -> Result<Vec<String>, String> {
    let entries = std::fs::read_dir(&dir).map_err(|e| format!("Klasör okunamadı: {e}"))?;
    let mut out: Vec<String> = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_file() {
            continue;
        }
        let ext = path
            .extension()
            .and_then(|e| e.to_str())
            .map(|s| s.to_lowercase())
            .unwrap_or_default();
        if IMAGE_EXTS.contains(&ext.as_str()) {
            out.push(path.to_string_lossy().to_string());
        }
    }
    out.sort();
    Ok(out)
}

/// Dosyayı hedef klasöre kopyalar (dışa aktarma). Aynı ad varsa üzerine yazar.
#[tauri::command]
fn copy_file(src: String, dst_dir: String) -> Result<String, String> {
    let src_path = Path::new(&src);
    let name = src_path
        .file_name()
        .ok_or_else(|| "Kaynak dosya adı alınamadı".to_string())?;
    let dst = Path::new(&dst_dir).join(name);
    std::fs::copy(src_path, &dst).map_err(|e| format!("Dosya kopyalanamadı: {e}"))?;
    Ok(dst.to_string_lossy().to_string())
}

/// Metin dosyası yazar (ör. dışa aktarmada sonuç JSON'u).
#[tauri::command]
fn write_text_file(path: String, contents: String) -> Result<(), String> {
    std::fs::write(&path, contents).map_err(|e| format!("Dosya yazılamadı: {e}"))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            python_request,
            python_status,
            list_images,
            copy_file,
            write_text_file,
            projects::list_projects,
            projects::create_project,
            projects::project_add_page,
            projects::project_prepare_page,
            projects::save_project,
            projects::open_project,
            projects::delete_project
        ])
        .setup(|app| {
            let state = Arc::new(PyState::new());
            app.manage(state.clone());

            if let Err(e) = spawn_python(app.handle(), &state) {
                eprintln!("[sidecar] BAŞLATILAMADI: {e}");
                let _ = app.emit("python-event", json!({ "name": "error", "payload": e }));
                return Ok(());
            }

            // Başlangıç sağlık kontrolü: 400ms sonra ping at, sonucu terminale logla.
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                std::thread::sleep(Duration::from_millis(400));
                match send_request(&state, "ping", json!({})) {
                    Ok(v) => {
                        println!("[sidecar] başlangıç pingi OK: {v}");
                        let _ = handle.emit("python-event", json!({ "name": "ready", "payload": v }));
                    }
                    Err(e) => {
                        eprintln!("[sidecar] başlangıç pingi HATA: {e}");
                        let _ = handle.emit("python-event", json!({ "name": "error", "payload": e }));
                    }
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Tauri uygulaması kurulurken hata oluştu");

    app.run(|app_handle, event| {
        if let tauri::RunEvent::Exit = event {
            if let Some(state) = app_handle.try_state::<Arc<PyState>>() {
                state.kill();
            }
        }
    });
}
