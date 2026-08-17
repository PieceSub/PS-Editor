# PS Editor

**PieceSub** markası altında manga ve anime çevirisi için masaüstü uygulaması. Şu an
**PRE-ALPHA** aşamasında; manga sayfası çevirisi uçtan uca çalışır (tespit → OCR →
inpainting → LLM çeviri → typesetting), anime tarafı için yalnızca yer tutucu sekme vardır.

Mimari: **Tauri 2** (Rust + WebView) kabuk + **Python sidecar** (OCR / inpainting /
çeviri gibi ağır işler bu serviste çalışır).

Öncelik sırası: Windows > Linux > macOS > Android (son).

---

## Özellikler

- Tek sayfa veya klasör halinde toplu manga sayfası çevirisi
- Otomatik balon/metin tespiti (RT-DETR-v2, ONNX/CPU) + manga-ocr
- LaMa / OpenCV ile metin temizleme (balon kenarı korumalı)
- Çeviri sağlayıcıları: yerel Ollama, OpenAI, OpenAI uyumlu (Groq / Together /
  DeepSeek vb.), Anthropic Claude ve API anahtarı gerektirmeyen mock (test)
- **Otomatik mod** (auto): VRAM'e göre yerel model ile API arasında otomatik seçim
- Manga okuma sırasına göre sayfa diyaloğunu tek LLM isteğiyle çevirme
- Scanlation stili typesetting: otomatik font boyutu, satır kaydırma, beyaz kontur
- Görsel düzenleyici: bölge taşıma/yeniden boyutlandırma, metin düzenleme, devre dışı
  bırakma, silme, elle yeni bölge ekleme — tümü **autosave** ile projeye yazılır
- Kalıcı proje sistemi: proje kartları, küçük resim önizleme, yeniden açma, silme
- F11 tam ekran, proje kartı zoom (Ctrl + tekerlek)
- API anahtarları işletim sistemi güvenli deposunda (keyring) saklanır

---

## Gereksinimler

| Bağımlılık | Gerekçe | Windows kurulumu |
|---|---|---|
| Rust (stable ≥ 1.88) | Tauri çekirdeği | `winget install Rustlang.Rustup` (MSVC target ile) |
| MSVC Build Tools | C++ linker + Windows SDK | `winget install Microsoft.VisualStudio.2022.BuildTools --override "--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"` |
| Node.js LTS ≥ 20 | Frontend (Vite + TS) | `winget install OpenJS.NodeJS.LTS` |
| Python 3.11+ | Sidecar | `winget install Python.Python.3.11` |
| WebView2 Runtime | WebView (Win11'de hazır) | [Microsoft indirme](https://developer.microsoft.com/en-us/microsoft-edge/webview2/) |

Notlar:

- MSI paketi üretmek istiyorsanız Windows "VBSCRIPT" isteğe bağlı özelliği açık olmalı
  (`failed to run light.exe` hatası görürseniz kontrol edin).
- PowerShell'de npm betikleri `npm.ps1` yerine `npm.cmd` ile çağrılır; `npm`
  çalışmazsa `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` uygulayın ya da
  `npm.cmd` kullanın. `npm run setup:python` / `npm run sidecar:build` komutları
  platforma göre `.ps1` / `.sh` betiklerini `run-platform.mjs` üzerinden otomatik seçer.

---

## Hızlı Başlangıç

```powershell
# 1) Python sanal ortamı + bağımlılıklar (python/.venv)
npm run setup:python

# 2) Frontend bağımlılıkları
npm install

# 3) Python sidecar'ı PyInstaller ile derle -> src-tauri/binaries/
npm run sidecar:build

# 4) Geliştirme modu (pencere açılır)
npm run tauri dev
```

> **2. adımdan sonraki `npm run` komutları Windows'ta `npm.cmd run` gerektirebilir.**

### Python tarafında hızlı iterasyon (PyInstaller'sız)

Sidecar üretim binary'si ile çalışıyor olsa bile Python kodunu derlemeden denemek
isterseniz:

```powershell
$env:PS_EDITOR_PY_SOURCE = "venv"   # Rust tarafı venv Python'u kullanır
npm.cmd run tauri dev
```

Bunun çalışabilmesi için `src-tauri/binaries/` içinde sidecar bulunmalıdır
(tauri-build derleme aşamasında varlığını zorunlu tutar).

---

## Python ↔ Tauri İletişim Mimarisi

**Seçim: stdin/stdout üzerinden JSON Lines (NDJSON).** Neden HTTP port değil:

1. Doğrudan pipe yönetimi ekstra bağımlılık gerektirmez; sidecar **onedir**
   paketlendiği için çalıştırma yolu tamamen bizim kontrolümüzdedir.
2. Dev (venv python) ve prod (PyInstaller onedir) modlarında **aynı kod yolu** kullanılır.
3. Yaşam döngüsü (başlat/kapat) Tauri'ye aittir; süreç kapanınca Tauri bunu fark eder.

İleride FastAPI eklendiğinde: stdin/stdout **kontrol kanalı** (start/stop/health/ping)
olarak kalır, büyük veri (OCR/çeviri) HTTP üzerinden akar.

### Protokol

```
İstek (Tauri -> Python, stdin):   {"id": 1, "cmd": "hello", "payload": {...}}
Yanıt (Python -> Tauri, stdout):  {"id": 1, "ok": true,  "result": {...}}
Hata:                             {"id": 1, "ok": false, "error": "..."}
Olay (push):                      {"event": "ready", "payload": {...}}
```

- Her satır tek bir JSON nesnesidir (UTF-8; üç akış da UTF-8'e sabitlenmiştir).
- `id` istekleri eşleştirir; Rust tarafı yanıtı bekleyen `invoke` çağrısına yönlendirir.
- Olaylar ön yüze `python-event` adıyla yayınlanır (`translate_page_progress` vb.).

Mevcut komutlar:

| Komut | Açıklama |
|---|---|
| `hello`, `ping` | Sağlık kontrolü |
| `check_cuda`, `vram_report` | GPU durumu / VRAM raporu |
| `translate_page` | Tek sayfa uçtan uca pipeline |
| `re_render_region` | Tek bölgeyi yeniden typeset etme (düzenleyici) |
| `set_api_key` / `delete_api_key` | Güvenli depoya anahtar yaz / sil |
| `list_providers` | Kullanılabilir çeviri sağlayıcıları |
| `shutdown` | Kapanış |

### Pipeline akışı (`translate_page`)

```
tespit (RT-DETR-v2, ONNX/CPU) -> manga-ocr (GPU)
  -> inpainting (LaMa; OpenCV alternatifi)  -> çeviri (LLM) -> typesetting
```

- Ağır bağımlılıklar (torch/cv2/PIL) **yalnızca komut işleme sırasında** (geç import)
  yüklenir; `ping` gibi hafif komutlar anında yanıt verir.
- Çeviri modu kararı (`auto`/`local`/`api`) `pipeline.py` → `resolve_translation_mode`
  içindedir: VRAM ölçümü `torch.cuda.mem_get_info` (öncelikli) / `nvidia-smi` (yedek).
  Eşikler ve rezervler `.env` veya komut payload'ındaki `settings_override` ile
  değiştirilebilir.
- Kullanıcı hatası mesajları stack-trace'siz taşınır (`PipelineError`).

### Çeviri sağlayıcıları

`translate_typeset_prototype.py` içinde soyutlanmıştır (`TranslationBackend` +
`create_backend` fabrikası). Yeni sağlayıcı: sınıf yazıp `create_backend`'e bağlamak
yeterli.

| Sağlayıcı | Açıklama |
|---|---|
| `mock` | API anahtarı gerektirmez; deterministik sahte çeviri (test) |
| `local` | Yerel Ollama (`http://localhost:11434/v1`) |
| `openai` | OpenAI resmi uç noktası |
| `openai_compat` | OpenAI uyumlu herhangi bir uç nokta (Groq, Together, DeepSeek, LM Studio, vLLM…) |
| `anthropic` | Claude Messages API (OpenAI uyumlu değildir; ayrı sınıf) |

Anahtar / base_url / model `resolve_credentials` ile çözülür; kaynak önceliği:
CLI/payload argümanları → işletim sistemi güvenli anahtar deposu (keyring:
Windows Credential Manager / macOS Keychain / Linux Secret Service) → `.env`
(geliştirme fallback'i) → kod varsayılanları. Anahtarlar asla stdout/log'a yazılmaz.

### Sidecar paketleme

- **PyInstaller modu: onedir** (onefile değil). onefile her başlatılışta ~4.7 GB'ı
  geçici dizine (/tmp, %TEMP%) ayıklar; force-kill/çökme sonrası `_MEI*` dizinleri
  birikip alanı doldurur. onedir hiç ayıklama yapmaz.
- `src-tauri/tauri.conf.json` → `bundle.resources: {"binaries/python-sidecar/": "sidecar/"}`
- Rust: `$RESOURCE/sidecar/python-sidecar` yolundan başlatılır; fallback olarak
  `PS_EDITOR_PY_SOURCE=venv` ile `python/.venv` Python'u kullanılır.
- Savunma: `sidecar.py` başlangıçta geçici dizindeki öksüz `_MEI*` kalıntılarını
  temizler (kendi dizinini, canlı süreçlerin dizinlerini asla silmez;
  `PS_EDITOR_MEI_MAX_AGE_SECONDS` eşiği geçersiz kılar).

---

## Proje Sistemi

Her proje kendi klasöründe yaşar (`app_data_dir/Projects/{project_id}/`):

```
{project_id}/
├── project.json     # manifest: meta + pages[] (Region dahil, yalnızca göreli yollar)
├── thumb.png        # liste önizlemesi (ilk sayfanın çevrilmiş görseli)
└── pages/
    ├── p0/          # original.*, translated.png, cleaned.png, ...
    └── p1/
```

Mimari kararlar:

1. **Binary'ler manifest'ten ayrı, göreli yolla referanslanır** (CapCut/Premiere
   proje deseni): büyük görseller JSON'a gömülmez; manifest küçük kalır, proje
   klasörü taşınabilir olur, `re_render_region` görseli yerinde güncelleyebilir.
2. **Atomik yazma**: önce `project.json.tmp`, sonra rename — kesintiye uğrarsa eski
   manifest sağlam kalır.
3. **Autosave**: ayrı "Kaydet" butonu yoktur; net eylem anlarında (Uygula / Devre Dışı
   Bırak / Sil / yeni bölge) ve her sayfa işlenince manifest diske yazılır.

---

## Proje Yapısı

```
PS-Editor/
├── package.json              # vite, tauri CLI, komut kestirmeleri
├── vite.config.ts            # 1420 portu, src-tauri/python izleme dışı
├── tsconfig.json
├── index.html
├── app-icon.png              # ikon kaynağı
├── LICENSE                   # MIT
├── src/                      # Ön yüz (vanilla TypeScript)
│   ├── main.ts               # invoke köprüleri, python-event dinleyicisi, proje akışı
│   ├── editor.ts             # bölge düzenleyici (taşı, boyutlandır, metin)
│   ├── viewer.ts             # sonuç görüntüleyici (karşılaştır/ön/arka modları)
│   ├── labels.ts             # aşama etiketleri
│   └── styles.css
├── python/
│   ├── sidecar.py            # JSON Lines sidecar (komutlar + _MEI* temizliği)
│   ├── pipeline.py           # uçtan uca pipeline orkestrasyonu (adım 5)
│   ├── ocr_prototype.py      # adım 2: tespit + OCR
│   ├── inpaint_prototype.py  # adım 3: OpenCV vs LaMa metin temizleme
│   ├── translate_typeset_prototype.py  # adım 4: çeviri backends + typesetting
│   ├── make_test_manga.py    # test sayfası üretici
│   ├── analyze_ink_remnant.py, verify_bubble_guard.py, test_detect_regression.py, ...
│   ├── fonts/                # typesetting yazı tipleri (Comic Neue)
│   ├── test_data/            # test sayfası + çıktılar (regression/ dahil)
│   └── requirements.txt
├── scripts/
│   ├── run-platform.mjs      # platforma göre .ps1/.sh seçici
│   ├── setup-python.{ps1,sh} # venv oluştur + bağımlılıklar
│   └── build-sidecar.{ps1,sh}# PyInstaller onedir -> src-tauri/binaries/python-sidecar/
└── src-tauri/
    ├── tauri.conf.json       # resources (sidecar klasörü), pencere, kimlik
    ├── Cargo.toml
    ├── capabilities/default.json
    ├── icons/                # tauri icon ile üretildi
    ├── binaries/             # sidecar klasörü (gitignore'da; her platformda ayrı derlenir)
    └── src/
        ├── main.rs
        ├── lib.rs            # sidecar süpervizörü + Tauri komutları
        └── projects.rs       # proje depolama (manifest, atomik yazma, autosave)
```

---

## Prototipler (OCR / Inpainting / Typesetting)

Prototipler `python/` altındadır; sidecar'a taşınmadan önce algoritma/mimari
kararlarını netleştirmek için yazıldı. İlk çalıştırmada modeller otomatik iner:

- Tespit: `detector-v4-s_int8.onnx` (~11 MB, Hugging Face)
- OCR: `manga-ocr` (~100 MB, Hugging Face)
- LaMa inpainting: `big-lama.pt` (~206 MB, GitHub release) — yerel kopya için
  `LAMA_MODEL` ortam değişkeni veya `--lama-model` ile yol verin.

```powershell
# OCR (test sayfası, python/ altında)
.\.venv\Scripts\python.exe ocr_prototype.py test_data\manga_test.png --json

# Inpainting
.\.venv\Scripts\python.exe inpaint_prototype.py test_data\manga_test.png `
  --regions test_data\manga_ocr.json --method both --add-bbox 100,903,540,1127 --json

# Çeviri + typesetting
.\.venv\Scripts\python.exe translate_typeset_prototype.py test_data\manga_test.png `
  --regions ocr.json --cleaned test_data\manga_test_inpaint_lama.png
```

### Doğrulama senaryosu (test sayfası, 800x1130)

| Metrik | Beklenen | Gözlenen |
|---|---|---|
| Balon kenarı koyu piksel (6 balon) | değişmemeli | %100 korundu |
| Metin bölgesi mürekkep oranı (LaMa) | → ~0 | 0.000-0.001 |
| Metin bölgesi mürekkep oranı (OpenCV) | → ~0 | 0.000-0.006 |
| SFX glifleri | silinmeli | 6998 → 1611 (kalan: koruma bandı + kuyruk, bilinçli) |
| Süre (GPU, RTX 4060) | — | OpenCV 0.26s; LaMa 0.7s yükleme + 1.1s inference |

**Varsayılan yöntem: LaMa.** Doku (screen tone) ve çizgiye dokunmadan metni söker;
OpenCV (Telea) yalnızca hızlı önizleme için uygundur. Balon kenarı koruması (bubble
guard), bbox birleşimi + 4px dilate maske stratejisi ve kalıntı temizleme pası
uygulanır; davranış regresyon testleriyle doğrulanır.

**Bilinen sınırlamalar:** yoğun screen tone üzerinde OpenCV leke bırakabilir; balon
sınırına bitişik taşan metin birkaç px kalıntı bırakabilir; döndürülmüş SFX'te balon
kuyruğu çizgisi bilinçli olarak korunur; balon tespiti kaçarsa manuel bbox gerekir.

---

## Sık Karşılaşılan Sorunlar

| Belirti | Çözüm |
|---|---|
| `resource path binaries\python-sidecar doesn't exist` | `npm run sidecar:build` (tauri-build derlemede sidecar klasörünü ister) |
| Eski Python kodu çalışıyor (prod) | `npm run sidecar:build` ile yeniden derleyin + `src-tauri/target/release/sidecar` klasörünü silip yeniden derleyin (tauri-build resource önbelleğini güncellemez — [tauri#15134](https://github.com/tauri-apps/tauri/issues/15134)) |
| `npm` PowerShell'de "scripts disabled" | `npm.cmd` kullanın veya `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| MSI hatası `failed to run light.exe` | Windows "VBSCRIPT" özelliğini açın |
| Pencere açılıp anında kapanıyor | WebView2 Runtime eksik — kurun |
| Orphan python-sidecar süreci | Uygulamayı force-kill etmeyin; pencereyi normal kapatın |
| /tmp (veya %TEMP%) `_MEI*` kalıntılarıyla dolu | Sidecar'ı son kez başlatın — başlangıçta öksüz kalıntıları otomatik temizler (onedir moduna geçildiği için artık yeni kalıntı oluşmaz) |

---

## Yol Haritası

1. FastAPI katmanı: `sidecar.py` içine uvicorn + FastAPI; stdin/stdout kontrol kanalı + HTTP veri kanalı.
2. Prototip modüllerinin sidecar komutlarına taşınması (büyük ölçüde tamam; `translate_page` / `re_render_region` aktif).
3. Çeviri kalitesi: context bellek, per-sayfa stilleri, şablon desteği.
4. Anime tarafı: alt yazı + sahne metni tespiti/çevirisi (yer tutucu sekme hazır).
5. Linux/macOS build kurulumları ve platform üçlüleri (`sidecar:build`).
6. Android (en son).

---

## Lisans

MIT — telif hakkı (c) 2026 PieceSub. Ayrıntılar için [LICENSE](LICENSE).
