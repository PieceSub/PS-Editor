//! Proje depolama (adım 8): her proje kendi klasöründe yaşar.
//!
//! Dizin düzeni (Tauri'nin app_data_dir'ı altında, platforma göre otomatik):
//!
//! ```text
//! {app_data}/Projects/{project_id}/
//!   project.json     - manifest: meta + pages[] (Region dahil, yalnızca göreli yollar)
//!   thumb.png        - liste önizlemesi (ilk sayfanın çevrilmiş görseli)
//!   pages/
//!     p0/            - original.*, translated.png, cleaned.png, ...
//!     p1/
//! ```
//!
//! Mimari kararlar (kaynaklar bu dosyanın başlığında belirtilmiştir):
//!
//!  1. **Binary'ler manifest'ten ayrı, göreli yolla referans**: CapCut'in
//!     proje deseni ("tek canonical JSON + assets/ klasörü", capcut-cli
//!     draft-schema belgeleri) ve Premiere'in "project refers to — but
//!     doesn't contain — source files" kuralı (O'Reilly Visual QuickStart)
//!     birebir aynı şeyi söyler: büyük görseller JSON'a gömülmez, ayrı
//!     dosyalar klasörde durur, manifest yalnızca yol tutar. Böylece JSON
//!     küçük kalır, proje klasörü taşınabilir olur ve `re_render_region`
//!     görseli yerinde (translated.png üzerine) güncelleyebilir.
//!
//!  2. **Atomik yazma**: önce `project.json.tmp`, sonra rename. CapCut'in
//!     `.bak` + atomik geri yazma deseninin dengi; kesintiye uğrarsa eski
//!     manifest sağlam kalır.
//!
//!  3. **Autosave noktaları**: ön yüz, düzenleyicide net eylem anlarında
//!     (Uygula / Devre Dışı Bırak / Sil / yeni bölge) `save_project`
//!     çağırır; ayrıca her `project_add_page` işlem sonrası manifesti
//!     günceller. Ayrı "Kaydet" butonu yoktur.

use serde_json::{json, Value};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use tauri::{AppHandle, Manager};

const PROJECTS_DIR: &str = "Projects";
const MANIFEST_FILE: &str = "project.json";

/// Klasör adı çakışmasını önleyen tekil üreteç.
static SEQ: AtomicU64 = AtomicU64::new(0);

fn now_iso() -> String {
    use chrono::{SecondsFormat, Utc};
    Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true)
}

fn new_project_id() -> String {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    let n = SEQ.fetch_add(1, Ordering::SeqCst);
    format!("p_{millis:x}_{n}")
}

/// Projeler kök dizinini döndürür (yoksa oluşturur).
fn projects_root(app: &AppHandle) -> Result<PathBuf, String> {
    let root = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("Uygulama veri dizini alınamadı: {e}"))?
        .join(PROJECTS_DIR);
    fs::create_dir_all(&root).map_err(|e| format!("Proje dizini oluşturulamadı: {e}"))?;
    Ok(root)
}

/// Kimlik doğrulaması + proje klasörü. Yol gezinmesini (path traversal)
/// önlemek için kimlik yalnızca `[a-zA-Z0-9_]` kabul eder.
fn project_dir(app: &AppHandle, id: &str) -> Result<PathBuf, String> {
    if id.is_empty() || !id.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') {
        return Err(format!("Geçersiz proje kimliği: {id:?}"));
    }
    let dir = projects_root(app)?.join(id);
    if !dir.is_dir() {
        return Err(format!("Proje bulunamadı: {id}"));
    }
    Ok(dir)
}

fn read_manifest(project: &Path) -> Result<Value, String> {
    let text = fs::read_to_string(project.join(MANIFEST_FILE))
        .map_err(|e| format!("project.json okunamadı: {e}"))?;
    serde_json::from_str(&text).map_err(|e| format!("project.json bozuk: {e}"))
}

/// `updated_at` tazeler ve manifesti atomik yazar; yeni zamanı döndürür.
fn save_manifest(project: &Path, mut manifest: Value) -> Result<String, String> {
    let now = now_iso();
    manifest["updated_at"] = Value::String(now.clone());
    let target = project.join(MANIFEST_FILE);
    let tmp = project.join("project.json.tmp");
    let text = serde_json::to_string_pretty(&manifest)
        .map_err(|e| format!("project.json serileştirilemedi: {e}"))?;
    fs::write(&tmp, text).map_err(|e| format!("project.json yazılamadı: {e}"))?;
    fs::rename(&tmp, target).map_err(|e| format!("project.json güncellenemedi: {e}"))?;
    Ok(now)
}

/// Görsel yolunu proje klasörüne göre GÖRELİ yapar. Manifestte her zaman
/// göreli yollar saklanır; böylece proje klasörü taşınabilir kalır.
fn to_rel(project: &Path, path: &str) -> String {
    let p = Path::new(path);
    if p.is_absolute() {
        if let Ok(rel) = p.strip_prefix(project) {
            return rel.to_string_lossy().replace('\\', "/");
        }
    }
    path.to_string()
}

/// Göreli yolu mutlak yapar (açma sırasında görüntüleyiciye verilir).
fn to_abs(project: &Path, path: &str) -> String {
    let p = Path::new(path);
    if p.is_absolute() {
        return path.to_string();
    }
    project.join(path).to_string_lossy().to_string()
}

/// PageResult içindeki görsel yollarını dönüştürücüden geçirir.
fn map_result_paths(result: &mut Value, f: &dyn Fn(&str) -> String) {
    if let Some(img) = result.get("image").and_then(Value::as_str) {
        result["image"] = Value::String(f(img));
    }
    if let Some(outputs) = result.get_mut("outputs").and_then(|v| v.as_object_mut()) {
        for key in ["translated", "cleaned", "ocr_regions", "before_after"] {
            if let Some(v) = outputs.get_mut(key) {
                if let Some(s) = v.as_str() {
                    *v = Value::String(f(s));
                }
            }
        }
    }
}

fn copy_if_exists(src: &str, dst: &Path) -> Result<bool, String> {
    if src.is_empty() {
        return Ok(false);
    }
    let s = Path::new(src);
    if !s.is_file() {
        return Ok(false);
    }
    fs::copy(s, dst).map_err(|e| format!("{} kopyalanamadı: {e}", s.display()))?;
    Ok(true)
}

/* ------------------------------------------------------------ çekirdek (test edilebilir) */

/// `root` içindeki projeleri özetler (kartlar için). En yeni `updated_at` önce.
fn list_projects_in(root: &Path) -> Result<Vec<Value>, String> {
    let mut out = Vec::new();
    for entry in fs::read_dir(root)
        .map_err(|e| format!("Proje listesi okunamadı: {e}"))?
        .flatten()
    {
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        let Ok(text) = fs::read_to_string(path.join(MANIFEST_FILE)) else {
            continue;
        };
        let Ok(Value::Object(mut m)) = serde_json::from_str::<Value>(&text) else {
            continue;
        };
        let id = m.get("id").and_then(Value::as_str).unwrap_or_default();
        if id.is_empty() || id != path.file_name().and_then(|s| s.to_str()).unwrap_or_default() {
            continue;
        }
        let page_count = m.get("pages").and_then(Value::as_array).map(|a| a.len()).unwrap_or(0);
        let thumb = path.join("thumb.png");
        m.insert("page_count".into(), json!(page_count));
        m.insert(
            "thumb".into(),
            if thumb.is_file() {
                json!(thumb.to_string_lossy().to_string())
            } else {
                Value::Null
            },
        );
        out.push(Value::Object(m));
    }
    out.sort_by(|a, b| {
        let at = a.get("updated_at").and_then(Value::as_str).unwrap_or_default();
        let bt = b.get("updated_at").and_then(Value::as_str).unwrap_or_default();
        bt.cmp(at)
    });
    Ok(out)
}

/// Boş bir proje klasörü + manifest oluşturur. "Yeni çeviri ekle" akışının
/// başlangıcı: her işlem artık her zaman bir projeye yazar (adım 6/7'deki
/// tek seferlik pipeline çağrıları değişmez, yalnızca sonucu kalıcılaşır).
fn create_project_in(
    root: &Path,
    name: String,
    source_type: String,
    mode: String,
    provider: String,
    target_lang: String,
) -> Result<Value, String> {
    let id = new_project_id();
    let dir = root.join(&id);
    fs::create_dir_all(dir.join("pages"))
        .map_err(|e| format!("Proje klasörü oluşturulamadı: {e}"))?;

    let now = now_iso();
    let manifest = json!({
        "schema_version": 1,
        "id": id,
        "name": name,
        "created_at": now,
        "updated_at": now,
        "source_type": source_type,
        "provider_settings": {
            "mode": mode,
            "provider": provider,
            "target_lang": target_lang
        },
        "pages": [],
    });
    save_manifest(&dir, manifest)?;
    Ok(json!({ "id": id, "name": name, "created_at": now, "updated_at": now }))
}

/// İşlenmiş bir sayfayı projeye ekler: görselleri proje klasörüne kopyalar,
/// manifeste sayfa girişi ekler, ilk sayfadan `thumb.png` üretir ve sonucu
/// MUTLAK yollarla (ön yüzün doğrudan kullanması için) döndürür.
fn add_page_in(root: &Path, id: &str, page: &Value) -> Result<Value, String> {
    let dir = root.join(id);
    let mut manifest = read_manifest(&dir)?;

    let result = page.get("result").cloned().ok_or("page.result eksik")?;
    let name = page.get("name").and_then(Value::as_str).unwrap_or_default().to_string();

    let index = manifest
        .get("pages")
        .and_then(Value::as_array)
        .map(|a| a.len())
        .unwrap_or(0);
    let page_dir = dir.join("pages").join(format!("p{index}"));
    fs::create_dir_all(&page_dir)
        .map_err(|e| format!("Sayfa klasörü oluşturulamadı: {e}"))?;

    let src = result.get("image").and_then(Value::as_str).unwrap_or_default().to_string();
    if src.is_empty() {
        return Err("Sonuçta kaynak görsel yolu yok (result.image)".into());
    }
    // Kaynağın uzantısını koru (png/jpg/webp/bmp/gif...), bilinmeyen -> png.
    let ext = Path::new(&src)
        .extension()
        .and_then(|e| e.to_str())
        .map(|e| e.to_lowercase())
        .filter(|e| !e.is_empty() && e.len() <= 5 && e.chars().all(|c| c.is_ascii_alphanumeric()))
        .unwrap_or_else(|| "png".into());
    let original_rel = format!("pages/p{index}/original.{ext}");
    if !copy_if_exists(&src, &dir.join(&original_rel))? {
        return Err(format!("Kaynak görsel kopyalanamadı (bulunamadı): {src}"));
    }

    // Çıktı görsellerini de projeye kopyala: translated/cleaned/ocr_regions/
    // before_after -> pages/p{n}/<anahtar>.png. Böylece manifestteki TÜM
    // yollar proje içi olur (taşınabilirlik) ve düzenleme (re_render_region)
    // proje içindeki translated.png'nin üzerine yazarak yerinde çalışır.
    const OUTPUT_KEYS: [&str; 4] = ["translated", "cleaned", "ocr_regions", "before_after"];
    let mut copied_outputs: Vec<(&str, String)> = Vec::new();
    for key in OUTPUT_KEYS {
        let path = result.pointer(&format!("/outputs/{key}")).and_then(Value::as_str);
        let Some(path) = path else { continue };
        if path.is_empty() {
            continue;
        }
        let dst_rel = format!("pages/p{index}/{key}.png");
        if copy_if_exists(path, &dir.join(&dst_rel))? {
            copied_outputs.push((key, dst_rel));
        }
    }

    // Liste önizlemesi: ilk sayfanın çevrilmiş görseli (yoksa orijinali).
    if index == 0 {
        let thumb_src = copied_outputs
            .iter()
            .find(|(k, _)| *k == "translated")
            .map(|(_, dst)| dir.join(dst))
            .unwrap_or_else(|| dir.join(&original_rel));
        let _ = fs::copy(&thumb_src, &dir.join("thumb.png"));
    }

    // Manifest: göreli yollar (kopyalananlar kendi proje içi yollarına işaret eder).
    let mut rel_result = result.clone();
    rel_result["image"] = json!(original_rel);
    for (key, dst) in &copied_outputs {
        rel_result["outputs"][key] = json!(dst);
    }
    map_result_paths(&mut rel_result, &|p| to_rel(&dir, p));
    manifest
        .get_mut("pages")
        .and_then(Value::as_array_mut)
        .ok_or("project.json'da pages dizisi yok")?
        .push(json!({
            "index": index,
            "name": name,
            "source": original_rel,
            "result": rel_result,
        }));
    save_manifest(&dir, manifest)?;

    // Yanıt: mutlak yollar (ön yüz convertFileSrc ile doğrudan gösterir).
    let mut abs_result = result;
    abs_result["image"] = json!(to_abs(&dir, &original_rel));
    map_result_paths(&mut abs_result, &|p| to_abs(&dir, p));
    Ok(json!({
        "index": index,
        "name": name,
        "source": to_abs(&dir, &original_rel),
        "result": abs_result,
    }))
}

/// Autosave: ön yüzün bellekteki manifestini (bölge düzenlemeleri dahil)
/// diske yazar. Yollar proje klasörüne görelileştirilir, `updated_at` tazelenir.
fn save_project_in(root: &Path, id: &str, manifest: &Value) -> Result<Value, String> {
    let dir = root.join(id);
    let mut m = manifest.clone();
    // Güvenlik + tutarlılık: manifest id'si her zaman klasör adıyla eşleşir.
    m["id"] = json!(id);
    if let Some(pages) = m.get_mut("pages").and_then(Value::as_array_mut) {
        for page in pages {
            if let Some(src) = page.get("source").and_then(Value::as_str) {
                page["source"] = json!(to_rel(&dir, src));
            }
            if let Some(result) = page.get_mut("result") {
                map_result_paths(result, &|p| to_rel(&dir, p));
            }
        }
    }
    let updated_at = save_manifest(&dir, m)?;
    Ok(json!({ "updated_at": updated_at }))
}

/// Projeyi açar: manifesti MUTLAK yollarla döndürür (görsel yolları
/// ön yüzde doğrudan kullanılabilir).
fn open_project_in(root: &Path, id: &str) -> Result<Value, String> {
    let dir = root.join(id);
    let mut manifest = read_manifest(&dir)?;
    if let Some(pages) = manifest.get_mut("pages").and_then(Value::as_array_mut) {
        for page in pages {
            if let Some(src) = page.get("source").and_then(Value::as_str) {
                page["source"] = json!(to_abs(&dir, src));
            }
            if let Some(result) = page.get_mut("result") {
                map_result_paths(result, &|p| to_abs(&dir, p));
            }
        }
    }
    Ok(manifest)
}

fn delete_project_in(root: &Path, id: &str) -> Result<(), String> {
    let dir = root.join(id);
    if !dir.is_dir() {
        return Err(format!("Proje bulunamadı: {id}"));
    }
    fs::remove_dir_all(&dir).map_err(|e| format!("Proje silinemedi: {e}"))
}

/* --------------------------------------------------------------- komutlar */

/// Proje listesi (kartlar için özet). En yeni `updated_at` önce gelir.
#[tauri::command]
pub fn list_projects(app: AppHandle) -> Result<Vec<Value>, String> {
    list_projects_in(&projects_root(&app)?)
}

/// Boş bir proje klasörü + manifest oluşturur.
#[tauri::command]
pub fn create_project(
    app: AppHandle,
    name: String,
    source_type: String,
    mode: String,
    provider: String,
    target_lang: String,
) -> Result<Value, String> {
    create_project_in(&projects_root(&app)?, name, source_type, mode, provider, target_lang)
}

/// İşlenmiş bir sayfayı projeye ekler.
#[tauri::command]
pub fn project_add_page(
    app: AppHandle,
    project_id: String,
    page: Value,
) -> Result<Value, String> {
    let root = projects_root(&app)?;
    let _ = project_dir(&app, &project_id)?; // kimlik doğrulama
    add_page_in(&root, &project_id, &page)
}

/// Autosave: manifesti diske yazar.
#[tauri::command]
pub fn save_project(app: AppHandle, project_id: String, manifest: Value) -> Result<Value, String> {
    let root = projects_root(&app)?;
    let _ = project_dir(&app, &project_id)?; // kimlik doğrulama
    save_project_in(&root, &project_id, &manifest)
}

/// Projeyi açar (mutlak yollarla).
#[tauri::command]
pub fn open_project(app: AppHandle, project_id: String) -> Result<Value, String> {
    let root = projects_root(&app)?;
    let _ = project_dir(&app, &project_id)?; // kimlik doğrulama
    open_project_in(&root, &project_id)
}

/// Projeyi ve tüm içeriğini kalıcı olarak siler (ön yüz onay ister).
#[tauri::command]
pub fn delete_project(app: AppHandle, project_id: String) -> Result<(), String> {
    delete_project_in(&projects_root(&app)?, &project_id)
}

/* ------------------------------------------------------------------ testler */

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_project_dir() -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "ps-editor-projects-test-{}-{}",
            std::process::id(),
            SEQ.fetch_add(1, Ordering::SeqCst)
        ));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn rel_abs_round_trip() {
        let project = temp_project_dir();
        let rel = "pages/p0/original.png";
        let abs = project.join(rel).to_string_lossy().to_string();

        assert_eq!(to_rel(&project, &abs), rel);
        assert_eq!(to_abs(&project, rel), abs);
        // Proje dışındaki mutlak yollar korunur (dışa aktarma vb.).
        let outside = std::env::temp_dir().join("dis-bir-sey.png").to_string_lossy().to_string();
        assert_eq!(to_rel(&project, &outside), outside);
        assert_eq!(to_abs(&project, &outside), outside);
        fs::remove_dir_all(&project).unwrap();
    }

    #[test]
    fn manifest_round_trip_atomic() {
        let project = temp_project_dir();
        let manifest = json!({
            "schema_version": 1,
            "id": "p_test",
            "pages": [{
                "result": {
                    "image": "/x/pages/p0/original.png",
                    "outputs": {
                        "translated": "/x/pages/p0/translated.png",
                        "cleaned": null,
                        "ocr_regions": "/x/pages/p0/ocr.png"
                    }
                }
            }]
        });
        let now = save_manifest(&project, manifest).unwrap();
        assert!(!now.is_empty());

        let read = read_manifest(&project).unwrap();
        assert_eq!(read["pages"][0]["result"]["outputs"]["cleaned"], Value::Null);
        assert_eq!(read["pages"][0]["result"]["image"], "/x/pages/p0/original.png");
        assert!(read.get("updated_at").and_then(Value::as_str).is_some());
        // .tmp yan ürünü kalmamalı.
        assert!(!project.join("project.json.tmp").exists());
        fs::remove_dir_all(&project).unwrap();
    }

    #[test]
    fn add_page_rewrites_paths() {
        let project = temp_project_dir();
        fs::create_dir_all(project.join("pages")).unwrap();

        // Sahte kaynak görseller (içerik önemsiz).
        let src = project.join("girdi.png");
        fs::write(&src, b"ornek-png-baytlari").unwrap();
        let translated = project.join("girdi_translated.png");
        fs::write(&translated, b"cikti-baytlari").unwrap();

        let mut result = json!({
            "image": src.to_string_lossy().to_string(),
            "outputs": {
                "translated": translated.to_string_lossy().to_string(),
                "cleaned": null,
            },
            "regions": [{
                "id": 1, "index": 0, "label_name": "manual",
                "bbox": [1, 2, 3, 4], "original": "", "translation": "Merhaba",
                "font_size": null, "lines": 0, "overflow": false,
                "manual": true, "disabled": false, "committed": false
            }]
        });

        // Görelileştirme: proje içindeki yollar rel olur, değerler korunur.
        map_result_paths(&mut result, &|p| to_rel(&project, p));
        assert_eq!(result["image"], "girdi.png");
        assert_eq!(result["outputs"]["translated"], "girdi_translated.png");
        assert_eq!(result["outputs"]["cleaned"], Value::Null);
        assert_eq!(result["regions"][0]["translation"], "Merhaba");

        // Mutlaklaştırma: yuvarlak dönüş.
        map_result_paths(&mut result, &|p| to_abs(&project, p));
        assert_eq!(result["image"], json!(src.to_string_lossy().to_string()));
        assert_eq!(result["outputs"]["translated"], json!(translated.to_string_lossy().to_string()));
        fs::remove_dir_all(&project).unwrap();
    }

    /// Uçtan uca akış: oluştur -> sayfa ekle -> düzenle/kaydet -> aç -> listele
    /// -> sil. GUI olmadan proje katmanının tam davranışını doğrular
    /// (ön yüzün invoke ettiği komutların aynı mantığı).
    #[test]
    fn full_project_lifecycle() {
        let root = temp_project_dir();

        // 1) Proje oluştur.
        let created = create_project_in(
            &root,
            "Test Manga 1".into(),
            "folder".into(),
            "auto".into(),
            "mock".into(),
            "tr".into(),
        )
        .unwrap();
        let id = created["id"].as_str().unwrap().to_string();

        // 2) İki sayfa işle (sahte görseller).
        let fake = |name: &str| {
            let p = root.join(name);
            fs::write(&p, format!("baytlar-{name}")).unwrap();
            p
        };
        let src1 = fake("sayfa1.png");
        let tr1 = fake("sayfa1_translated.png");
        let src2 = fake("sayfa2.jpg");
        let tr2 = fake("sayfa2_translated.png");

        let page1 = json!({
            "name": "sayfa1.png",
            "result": {
                "job_id": "j1",
                "image": src1.to_string_lossy().to_string(),
                "outputs": {
                    "translated": tr1.to_string_lossy().to_string(),
                    "cleaned": null,
                    "ocr_regions": null,
                    "before_after": null
                },
                "regions": [{
                    "id": 1, "index": 0, "label_name": "bubble",
                    "bbox": [10, 20, 110, 60], "original": "こんにちは",
                    "translation": "Merhaba", "font_size": 18, "lines": 1,
                    "overflow": false, "style": {"font_weight": "bold"}
                }],
                "warnings": [], "timings_ms": {"total": 1500}
            }
        });
        let added1 = add_page_in(&root, &id, &page1).unwrap();
        assert_eq!(added1["index"], 0);
        // Dönüş mutlak olmalı ve kopyalanan dosyalar var olmalı.
        let abs_tr1 = added1["result"]["outputs"]["translated"].as_str().unwrap();
        assert!(Path::new(abs_tr1).is_file());
        assert!(root.join(&id).join("thumb.png").is_file(), "ilk sayfadan thumb üretilmeli");

        let page2 = json!({
            "name": "sayfa2.jpg",
            "result": {
                "job_id": "j2",
                "image": src2.to_string_lossy().to_string(),
                "outputs": {
                    "translated": tr2.to_string_lossy().to_string(),
                    "cleaned": null, "ocr_regions": null, "before_after": null
                },
                "regions": [], "warnings": [], "timings_ms": {"total": 900}
            }
        });
        add_page_in(&root, &id, &page2).unwrap();

        // 3) Liste: 1 proje, 2 sayfa.
        let list = list_projects_in(&root).unwrap();
        assert_eq!(list.len(), 1);
        assert_eq!(list[0]["page_count"], 2);
        assert_eq!(list[0]["name"], "Test Manga 1");

        // 4) Düzenleme sonrası autosave: ön yüz mutlak yollar gönderir.
        let opened = open_project_in(&root, &id).unwrap();
        let mut manifest = opened.clone();
        manifest["pages"][0]["result"]["regions"][0]["translation"] = json!("Merhaba dünya!");
        manifest["pages"][0]["result"]["regions"][0]["style"] =
            json!({"font_weight": "bold", "color": "#ff0000", "align": "center"});
        manifest["pages"][0]["result"]["regions"][0]["committed"] = json!(true);
        save_project_in(&root, &id, &manifest).unwrap();

        // Diskteki ham dosya göreli yollar içermeli.
        let raw: Value =
            serde_json::from_str(&fs::read_to_string(root.join(&id).join("project.json")).unwrap())
                .unwrap();
        let img = raw["pages"][0]["result"]["image"].as_str().unwrap();
        assert_eq!(img, "pages/p0/original.png");
        assert!(
            raw["pages"][0]["result"]["outputs"]["translated"].as_str().unwrap().starts_with("pages/p")
        );

        // 5) Yeniden aç (kapat-aç simülasyonu): düzenlemeler korunmalı.
        let reopened = open_project_in(&root, &id).unwrap();
        assert_eq!(reopened["pages"][0]["result"]["regions"][0]["translation"], "Merhaba dünya!");
        assert_eq!(reopened["pages"][0]["result"]["regions"][0]["style"]["color"], "#ff0000");
        assert_eq!(reopened["pages"][0]["result"]["regions"][0]["committed"], true);
        let abs_img = reopened["pages"][0]["result"]["image"].as_str().unwrap();
        assert!(Path::new(abs_img).is_file());
        assert_eq!(reopened["pages"].as_array().unwrap().len(), 2);
        assert_eq!(reopened["name"], "Test Manga 1");
        assert_eq!(reopened["provider_settings"]["target_lang"], "tr");

        // 6) Sil.
        delete_project_in(&root, &id).unwrap();
        assert_eq!(list_projects_in(&root).unwrap().len(), 0);
        assert!(!root.join(&id).exists());

        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn rejects_invalid_ids() {
        let root = temp_project_dir();
        fs::create_dir_all(&root).unwrap();
        assert!(delete_project_in(&root, "../geçersiz").is_err());
        assert!(delete_project_in(&root, "a/b").is_err());
        fs::remove_dir_all(&root).unwrap();
    }
}
