# PS Editor

**PieceSub** markası altında manga ve anime çevirisi için masaüstü uygulaması.

Mimari: **Tauri 2** (Rust + WebView) kabuk + **Python sidecar** (ileride FastAPI;
OCR / çeviri / inpainting gibi ağır ML işlerini bu servis yapacak).

Öncelik sırası: Windows > Linux > macOS > Android (son).

---

## Kurallar / Bağımlılıklar

| Bağımlılık | Gerekçe | Windows kurulumu |
|---|---|---|
| Rust (stable ≥ 1.88) | Tauri çekirdeği | `winget install Rustlang.Rustup` (MSVC target ile) |
| MSVC Build Tools | C++ linker + Windows SDK | `winget install Microsoft.VisualStudio.2022.BuildTools --override "--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"` |
| Node.js LTS ≥ 20 | Frontend (Vite + TS) | `winget install OpenJS.NodeJS.LTS` |
| Python 3.11+ | Sidecar | `winget install Python.Python.3.11` |
| WebView2 Runtime | WebView (Win11'de hazır) | [Microsoft indirme](https://developer.microsoft.com/en-us/microsoft-edge/webview2/) |

Not: MSI paketi üretmek istiyorsanız Windows "VBSCRIPT" isteğe bağlı özelliğinin açık olması gerekir (`failed to run light.exe` hatası görürseniz kontrol edin).

PowerShell Notu: npm betikleri `npm.ps1` yerine `npm.cmd` ile çağrılır; `npm` PowerShell'de
çalışmazsa `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` uygulayın ya da `npm.cmd` kullanın.

---

## Hızlı Başlangıç (yeni ortam)

```powershell
# 1) Python sanal ortamı (python/.venv)
npm run setup:python

# 2) Frontend bağımlılıkları
npm install

# 3) Python sidecar'ı PyInstaller ile derle -> src-tauri/binaries/
npm run sidecar:build

# 4) Geliştirme modu (pencere açılır)
npm run tauri dev
```

> **2. adımdan sonraki `npm run` komutları `npm.cmd run` gerektirebilir.**

### Python tarafında hızlı iterasyon (PyInstaller'sız)

Sidecar üretim binary'si ile çalışıyor olsa bile Python kodunu `python/sidecar.py` içinde
değiştirip derlemeden denemek isterseniz:

```powershell
$env:PS_EDITOR_PY_SOURCE = "venv"   # Rust tarafı venv Python'u kullanır
npm.cmd run tauri dev
```

Bunun çalışabilmesi için `src-tauri/binaries/` içinde sidecar bulunmalıdır (tauri-build derleme
aşamasında `resources` altındaki `binaries/python-sidecar/` dizininin varlığını zorunlu tutar).

---

## Python ↔ Tauri İletişim Mimarisi

**Seçim: stdin/stdout üzerinden JSON Lines (NDJSON).** Neden HTTP port değil:

1. Doğrudan pipe yönetimi (`std::process::Command` + okuma iş parçacığı) ekstra bağımlılık
   gerektirmez; sidecar **onedir** paketlendiği için çalıştırma yolu da tamamen bizim
   kontrolümüzdedir (resource dizini).
2. Dev (venv python) ve prod (PyInstaller onedir) modlarında **aynı kod yolu** kullanılır.
3. Hayat döngüsü (başlat/kapat) Tauri'ye aittir; süreç kapanınca Tauri bunu fark eder.

İleride FastAPI eklendiğinde: stdin/stdout **kontrol kanalı** (start/stop/health/ping) olarak
kalır, büyük veri (OCR/çeviri) HTTP üzerinden akar — topluluğun yerleşik deseni budur.

### Protokol

```
İstek (Tauri -> Python, stdin):   {"id": 1, "cmd": "hello", "payload": {...}}
Yanıt (Python -> Tauri, stdout):  {"id": 1, "ok": true,  "result": {...}}
Hata:                             {"id": 1, "ok": false, "error": "..."}
Olay (push):                      {"event": "ready", "payload": {...}}
```

- Her satır tek bir JSON nesnesidir (UTF-8).
- `id` istekleri eşleştirir; Rust tarafı yanıtı bekleyen `invoke` çağrısına yönlendirir.
- Olaylar ön yüze `python-event` adıyla yayınlanır.
- Mevcut komutlar: `ping`, `hello`, `check_cuda` (ve kapanışta `shutdown`).

### Sidecar yapılandırması

- **PyInstaller modu: onedir** (onefile değil). onefile her başlatılışta ~4.7 GB'ı geçici
  dizine (/tmp, %TEMP%) ayıklar; force-kill/çökme sonrası bu `_MEI*` dizinleri temizlenmeden
  birikip alanı doldurur. onedir hiç ayıklama yapmaz; sidecar `src-tauri/binaries/python-sidecar/`
  klasöründe derlenir (exe + `_internal/`).
- `src-tauri/tauri.conf.json` → `bundle.resources: {"binaries/python-sidecar/": "sidecar/"}`
  (derleme öncesi `npm run sidecar:build` gerekir; tauri-build varlığını zorunlu tutar).
- Rust: `$RESOURCE/sidecar/python-sidecar` yolundan `std::process::Command` ile başlatılır;
  fallback olarak `PS_EDITOR_PY_SOURCE=venv` ile `python/.venv/Scripts/python.exe python/sidecar.py`.
- Savunma: `sidecar.py` main() başında geçici dizindeki öksüz `_MEI*` kalıntılarını temizler
  (kendi dizinini, canlı süreçlerin dizinlerini, genç dizinleri ve symlink'leri asla silmez;
  `PS_EDITOR_MEI_MAX_AGE_SECONDS` eşiği geçersiz kılar). Böylece eski onefile kalıntıları
  sonraki başlatılışta otomatik temizlenir.

---

## Proje Yapısı

```
PS-Editor/
├── package.json              # vite, tauri CLI, komut kestirmeleri
├── vite.config.ts            # 1420 portu, src-tauri/python izleme dışı
├── tsconfig.json
├── index.html
├── app-icon.png              # ikon kaynağı (tauri icon ile yeniden üretilir)
├── src/                      # Ön yüz (vanilla TypeScript)
│   ├── main.ts               # invoke köprüleri + python-event dinleyicisi
│   └── styles.css
├── python/
│   ├── sidecar.py            # JSON Lines sidecar (sadece stdlib)
│   ├── ocr_prototype.py      # adım 2: tespit + OCR (balon/metin bbox'ları, --json)
│   ├── inpaint_prototype.py  # adım 3: OpenCV vs LaMa metin temizleme
│   ├── make_test_manga.py    # test sayfası üretici
│   ├── test_data/            # test sayfası + çıktılar (mask, karşılaştırma, JSON)
│   └── requirements.txt      # OCR + inpainting bağımlılıkları
├── scripts/
│   ├── setup-python.ps1      # venv oluştur
│   └── build-sidecar.ps1     # PyInstaller onedir -> src-tauri/binaries/python-sidecar/
└── src-tauri/
    ├── tauri.conf.json       # resources (sidecar klasörü), pencere, kimlik
    ├── Cargo.toml
    ├── capabilities/default.json
    ├── icons/                # tauri icon ile üretildi
    ├── binaries/             # sidecar klasörü (gitignore'da; her platformda ayrı derlenir)
    └── src/
        ├── main.rs
        └── lib.rs            # sidecar süpervizörü: spawn, stdin/stdout, istek eşleştirme
```

---

## OCR ve Inpainting Prototipleri (adım 2-3)

Şu an tamamlanan iki prototip `python/` altında; ikisi de sidecar'a taşınmadan önce
algoritma/mimari kararlarını netleştirmek için yazıldı.

### Kurulum (mevcut venv'e ek)

```powershell
cd python
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

İlk çalıştırmada modeller otomatik iner:
- Tespit: `detector-v4-s_int8.onnx` (~11 MB, Hugging Face)
- OCR: `manga-ocr` (~100 MB, Hugging Face)
- LaMa inpainting: `big-lama.pt` (~206 MB, GitHub release) — yerel kopya için
  `LAMA_MODEL` ortam değişkeni veya `--lama-model` ile yol verin.

### Adım 2 — OCR (`ocr_prototype.py`)

Balon/metin tespiti (RT-DETR-v2, ONNX/CPU) + manga-ocr (GPU). Döndürülmüş SFX gibi
kaçan bölgeler `--add-bbox` ile elle eklenir.

```powershell
.\.venv\Scripts\python.exe ocr_prototype.py test_data\manga_test.png --json
```

### Adım 3 — Inpainting (`inpaint_prototype.py`)

OCR JSON'undaki bbox'lardan metin maskesi üretir, balon kenarı koruması (bubble
guard) uygular ve iki yöntemle temizler:

```powershell
.\.venv\Scripts\python.exe inpaint_prototype.py test_data\manga_test.png `
  --regions test_data\manga_ocr.json --method both --add-bbox 100,903,540,1127 --json
```

Çıktılar: `manga_test_mask.png`, `manga_test_inpaint_{opencv,lama}.png`,
`manga_test_compare.png` (yan yana), `manga_test_regions_compare.png` (bölge zoom'ları).

**Doğrulama senaryosu (test sayfası, 800x1130):**

| Metrik | Beklenen | Gözlenen |
|---|---|---|
| Balon kenarı koyu piksel (6 balon) | değişmemeli | %100 korundu (618/595/514/551/1467/1514 → aynı) |
| Metin bölgesi mürekkep oranı (LaMa) | → ~0 | 0.000-0.001 |
| Metin bölgesi mürekkep oranı (OpenCV) | → ~0 | 0.000-0.006 |
| SFX glifleri | silinmeli | 6998 → 1611 (kalan: koruma bandı + kuyruk, bilinçli) |
| Süre (GPU, RTX 4060) | — | OpenCV 0.26s; LaMa 0.7s yükleme + 1.1s inference |

**Varsayılan yöntem: LaMa.** Doku (screen tone) ve çizgiye dokunmadan metni
söker; OpenCV (Telea) büyük deliklerde gri leke bırakıp maske şekline duyarlıdır —
yalnızca hızlı önizleme için uygun. Maske stratejisi: bbox birleşimi + 4px dilate,
balon-içi bölgeler iç kutuya kırpılır, balon üstüne binen SFX'te balonun 10px'lik
çizgi bandı korunur, son bir geçiş maskede kalan koyu kalıntıları (bağlı bileşen,
≥20px) maskenin dışına taşmadan temizler.

**Bilinen sınırlamalar:** yoğun screen tone üzerinde OpenCV leke bırakabilir;
balon sınırına bitişik taşan metin (maske kenarı glife değerse) birkaç px kalıntı
bırakabilir; döndürülmüş SFX'te balon kuyruğu çizgisi bilinçli olarak korunur;
balon tespiti kaçarsa manuel bbox gerekir.

---

## Sık Karşılaşılan Sorunlar

| Belirti | Çözüm |
|---|---|
| `resource path binaries\python-sidecar doesn't exist` | `npm.cmd run sidecar:build` (tauri-build derlemede sidecar klasörünü ister) |
| Eski Python kodu çalışıyor (prod) | `npm.cmd run sidecar:build` ile yeniden derleyin + `src-tauri/target/release/sidecar` klasörünü silip yeniden derleyin (tauri-build resource önbelleğini güncellemez — [tauri#15134](https://github.com/tauri-apps/tauri/issues/15134)) |
| `npm` PowerShell'de "scripts disabled" | `npm.cmd` kullanın veya `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| MSI hatası `failed to run light.exe` | Windows "VBSCRIPT" özelliğini açın |
| Pencere açılıp anında kapanıyor | WebView2 Runtime eksik — kurun |
| Orphan python-sidecar süreci | Uygulamayı force-kill etmeyin; pencereyi normal kapatın (Exit handler'ı öldürür) |
| /tmp (veya %TEMP%) `_MEI*` kalıntılarıyla doldu | Sidecar'ı son kez başlatın: `sidecar.py` başlangıçta öksüz kalıntıları otomatik temizler (onedir moduna geçildiği için artık yeni kalıntı oluşmaz) |

---

## Yol Haritası (sonraki adımlar)

1. FastAPI katmanı: `sidecar.py` içine uvicorn + FastAPI; stdin/stdout kontrol kanalı + HTTP veri kanalı.
2. Prototip modüllerini (OCR + inpainting) sidecar komutlarına taşıma; çıktı formatı prototip JSON'larına dayanır.
3. Çeviri modülü ve Tauri komutları.
4. Linux/macOS build kurulumları; `sidecar:build` scriptine platform üçlüleri.
5. Android (en son).