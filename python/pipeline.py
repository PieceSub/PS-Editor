"""PS Editor - Tek sayfa icin uctan uca pipeline (adim 5).

Adim 2/3/4 prototiplerini tek orkestratorda birlestirir:

  tespit(OCR) -> manga-ocr -> inpainting(LaMa) -> ceviri(LLM) -> typeset

sidecar.py'nin "translate_page" komutu bu modulu surer. Ilerleme, callback
temelli event iletimiyle raporlanir (emit(payload)), boylece sidecar bunlari
mevcut python-event JSON Lines mekanizmasiyla Tauri'ye iletebilir.

Onemli mimari notlar:
  - Bu modul import edildiginde agir bagimliliklar (torch/cv2/PIL) yuklenir.
    sidecar.py bu modulu YALNIZCA komut isleme sirasinda (gec import)
    cagirir; boylece sidecar'ın ping/hello/check_cuda gibi hafif komutlari
    aninda calisir. Prototipler de ayni felsefeyi uygular.
  - Ceviri modu karari ("auto"/"local"/"api") suradadr: resolve_translation_mode.
    VRAM olcumu torch.cuda.mem_get_info (oncelikli) / nvidia-smi (yedek).
    Esikler ve rezervler Settings.dataclass'ta; .env veya komut payload'inda
    settings_override ile degistirilebilir -- hicbir yerde hardcode degil.
  - Kullanici hatasi: PipelineError, "API anahtariniz gecersiz gorunuyor"
    gibi stack-trace'siz, kullaniciya gosterilebilir mesajlar tasir;
    build_user_error herhangi bir exception'i oraya cevirir.

Kullanim (dogrudan / CLI benzeri):
    python -c "import json,sys; from pipeline import translate_page_pipeline, \
        resolve_translation_mode, get_vram_info; \
        r=translate_page_pipeline('test_data/manga_test.png', provider='mock', \
        emit=lambda p: print(json.dumps({'event':'translate_page_progress','payload':p}, ensure_ascii=False))); \
        print(json.dumps(r, ensure_ascii=False, indent=2))"
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Callable

from ocr_prototype import ComicTextDetector, crop_region, nms
from inpaint_prototype import (
    apply_bubble_guard,
    build_text_mask,
    dilate_mask,
    inpaint_lama,
    inpaint_opencv,
    refine_remnants,
)
from translate_typeset_prototype import (
    DEFAULT_FONT,
    create_backend,
    manga_reading_order,
    resolve_credentials,
    typeset_region,
)

from PIL import Image

EVENT_NAME = "translate_page_progress"

# ---------------------------------------------------------------- hata taksonomisi

class PipelineError(Exception):
    """Stack-trace degil, kullaniciya gosterilebilir mesaj tasiyan hata."""

    def __init__(self, message: str, code: str = "pipeline_error"):
        super().__init__(message)
        self.code = code
        self.user_message = message


class _QuietOutput:
    """stdout suppression icin: tüm print'leri yutar ama fut (pipe) bozulmaz."""

    def write(self, s: str) -> int:
        return len(s)

    def flush(self) -> None:
        pass

    @property
    def encoding(self) -> str:
        return "utf-8"

    @property
    def errors(self) -> str:
        return "replace"


@contextlib.contextmanager
def quiet_stdout():
    """Prototip/modul icindeki print()'lerin JSONL pipe'a karismasini onler.

    sidecar, kendi mesajlarini modul-yuklenme oncesi yakalanan orijinal
    stdout referansiyla yazar (bkz. sidecar.write_message), bu yuzden bu
    suppresion protokol mesajlarini etkilemez.
    """
    old = sys.stdout
    sys.stdout = _QuietOutput()
    try:
        yield
    finally:
        sys.stdout = old


def build_user_error(exc: BaseException) -> dict:
    """Herhangi bir exception'i {code, message} kullanici hatasina cevirir.

    Onceligi: bilinen sinif adlari / durum kodlari; hicbiri tutmazsa kisa,
    stack-trace'siz generic mesaj. SDK siniflarini iceride GEC import ettiğimiz
    icin hizli yolda (ör. mock) import bosa harcanmaz (yine de cache'lenir).
    """
    if isinstance(exc, PipelineError):
        return {"code": exc.code, "message": exc.user_message}

    name = type(exc).__name__
    mod = type(exc).__module__ or ""
    status = getattr(exc, "status_code", None)
    low = str(exc).lower()

    # Model indirme / kurulum hatalari
    if isinstance(exc, SystemExit) or "download" in low or name in (
        "HTTPError", "OSError",
    ) and ("model" in low or "hub" in mod or "onnx" in low):
        return {
            "code": "model_download_failed",
            "message": (
                "Model indirilemedi veya yuklenemedi. Internet baglantinizi"
                " kontrol edin ve tekrar deneyin."
            ),
        }

    # LLM API kimlik / limit / baglanti hatalari
    if name == "AuthenticationError" or (
        status in (401, 403)
        and (mod.startswith("openai") or mod.startswith("anthropic"))
    ):
        return {
            "code": "invalid_api_key",
            "message": (
                "API anahtariniz gecersiz gorunuyor. Ayarlar bolumunden"
                " saglayici anahtarini kontrol edin."
            ),
        }
    if name == "RateLimitError" or status in (429, 529):
        return {
            "code": "rate_limited",
            "message": (
                "API istek limitine ulasildi (veya saglayici yogun)."
                " Biraz sonra tekrar deneyin."
            ),
        }
    if name in ("APIConnectionError", "APITimeoutError") or (
        (mod.startswith("openai") or mod.startswith("anthropic"))
        and name.startswith("APIConnection")
    ):
        return {
            "code": "connection_error",
            "message": (
                "Saglayiciya baglanilamadi. Internet baglantisini ve saglayici"
                " adresini (BASE_URL) kontrol edin."
            ),
        }

    # GPU bellek hatalari
    if "OutOfMemoryError" in name or "out of memory" in low:
        return {
            "code": "vram_oom",
            "message": (
                "GPU bellegi yetersiz. Ceviri modunu 'api' yapin veya"
                " gorselin cozunurlugunu dusurun (lama_max_side)."
            ),
        }

    # Gorsel acma hatalari
    if name in ("UnidentifiedImageError", "FileNotFoundError") or (
        name == "OSError" and "image" in low
    ):
        return {
            "code": "invalid_image",
            "message": "Gorsel acilamadi veya bulunamadi. Gecerli bir PNG/JPG secin.",
        }

    return {
        "code": "unexpected",
        "message": f"Islem basarisiz: {name}. Detay icin konsol gecmisini inceleyin.",
    }


# ---------------------------------------------------------------- ayarlar

@dataclass
class Settings:
    """Tum pipeline esikleri + prototip secenekleri. .env veya payload'dan okunur.

    VRAM karsilastirmasi (auto modu):
      local_model_icin_yer = free_vram - media_reserve - headroom
      local_model_icin_yer >= translation_min  ->  yerel model kullanilir
    Varsayilanlar ile esik TOPLAM ~7 GiB civarina tekabul eder
    (2 GiB OCR+LaMa + 1 GiB headroom + 4 GiB ceviri modeli) -- istendigi
    gibi 6-8 GiB bandinda.
    """

    vram_media_reserve_mb: int = 2048
    vram_headroom_mb: int = 1024
    vram_translation_min_mb: int = 4096

    ocr_conf: float = 0.3
    ocr_force_cpu: bool = False

    inpaint_method: str = "lama"         # lama | opencv
    inpaint_device: str = "auto"         # auto | cuda | cpu
    inpaint_dilate: int = 4
    inpaint_bubble_margin: float = 0.08
    inpaint_ring_width: int = 10
    lama_max_side: int = 2048
    no_refine_remnants: bool = False

    font: str = str(DEFAULT_FONT)
    min_font_size: int = 9
    max_font_size: int = 36

    out_dir: str | None = None            # yoksa gorselin yani (test_data)
    save_intermediate: bool = True        # cleaned/ocr/regions ara dosyalari
    debug: bool = False

    ENV: dict = None  # sinif sonrasi doldurulur

    @classmethod
    def from_mapping(cls, overrides: dict | None = None) -> "Settings":
        """Ortam degiskenlerini + istege bagli overrides dict'ini uygular."""
        s = cls()
        for f in fields(cls):
            if f.name == "ENV":
                continue
            env_val = os.environ.get(cls.ENV.get(f.name, ""))
            if env_val is not None and env_val.strip():
                setattr(s, f.name, _cast(f.type, env_val))
        if overrides:
            for k, v in overrides.items():
                if hasattr(s, k):
                    setattr(s, k, _cast(_type_hint(s, k), v))
        return s

    def to_dict(self) -> dict:
        out = {}
        for f in fields(self):
            if f.name == "ENV":
                continue
            out[f.name] = getattr(self, f.name)
        return out


def _type_hint(inst, name) -> str:
    for f in fields(inst):
        if f.name == name:
            return f.type
    return "str"


def _cast(type_hint: Any, value: Any) -> Any:
    if isinstance(value, str):
        v = value.strip().lower()
        if "bool" in str(type_hint):
            return v in ("1", "true", "yes", "on")
        if "float" in str(type_hint):
            return float(value)
        if "int" in str(type_hint):
            return int(float(value))
        return value
    return value


Settings.ENV = {
    "vram_media_reserve_mb": "PS_VRAM_MEDIA_RESERVE_MB",
    "vram_headroom_mb": "PS_VRAM_HEADROOM_MB",
    "vram_translation_min_mb": "PS_VRAM_TRANSLATION_MIN_MB",
    "ocr_conf": "PS_OCR_CONF",
    "ocr_force_cpu": "PS_OCR_FORCE_CPU",
    "inpaint_method": "PS_INPAINT_METHOD",
    "inpaint_device": "PS_INPAINT_DEVICE",
    "inpaint_dilate": "PS_INPAINT_DILATE",
    "inpaint_bubble_margin": "PS_INPAINT_BUBBLE_MARGIN",
    "inpaint_ring_width": "PS_INPAINT_RING_WIDTH",
    "lama_max_side": "PS_LAMA_MAX_SIDE",
    "no_refine_remnants": "PS_INPAINT_NO_REFINE",
    "font": "PS_TYPEFACE_FONT",
    "min_font_size": "PS_TYPEFACE_MIN_SIZE",
    "max_font_size": "PS_TYPEFACE_MAX_SIZE",
    "out_dir": "PS_OUT_DIR",
    "save_intermediate": "PS_SAVE_INTERMEDIATE",
    "debug": "PS_DEBUG",
}


# ---------------------------------------------------------------- VRAM olcumu + mod karari

def get_vram_info() -> dict:
    """GPU VRAM bilgisi. Torch (kesin) oncelikli, nvidia-smi yedek.

    Mem-get beltirli: karar zamaninda CUDA context henuz tahsisli degil,
    dolayisiyla free ~= kullanilabilir toplam. Bu istenen davranis: karar,
    OCR+inpainting icine yuklenmeden once verilir.
    """
    info: dict = {
        "available": False,
        "source": None,
        "total_mib": None,
        "free_mib": None,
        "used_mib": None,
        "device_name": None,
    }
    try:
        import torch
        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            total_mib = int(props.total_memory // (1024 * 1024))
            free_mib, _ = torch.cuda.mem_get_info(0)
            info.update(
                available=True, source="torch", total_mib=total_mib,
                free_mib=int(free_mib // (1024 * 1024)),
                used_mib=total_mib - int(free_mib // (1024 * 1024)),
                device_name=props.name,
            )
            return info
    except Exception:  # noqa: BLE001 - olcum hataliysa asagi in
        pass

    import shutil
    import subprocess
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.free,memory.used",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
                encoding="utf-8", errors="replace",
            )
            if out.returncode == 0 and out.stdout.strip():
                parts = [p.strip() for p in out.stdout.strip().splitlines()[0].split(",")]
                if len(parts) >= 4:
                    info.update(
                        available=True, source="nvidia-smi",
                        device_name=parts[0],
                        total_mib=int(float(parts[1])),
                        free_mib=int(float(parts[2])),
                        used_mib=int(float(parts[3])),
                    )
        except Exception:  # noqa: BLE001
            pass
    if not info["available"]:
        info["reason_unavailable"] = (
            "CUDA ve nvidia-smi bulunamadi; VRAM olculemiyor."
        )
    return info


def resolve_translation_mode(requested: str, s: Settings, vram: dict) -> dict:
    """Ceviri modeli secimi: 'auto'/'local'/'api'.

    - local : her zaman yerel Ollama
    - api   : her zaman kullanicinin sectigi API saglayicisi
    - auto  : local, VRAM esigini gecerse -> local; gecmezse -> api.
      esik: (free - media_reserve - headroom) >= translation_min.

    Burasi dokunulabilir bir ayar katmanidir: esikler Settings / .env'dedir;
    bu fonksiyon yalnizca karar mantigini tasir. Donen sozluk hem karari hem
    de gerekceyi raporlar (frontend bunu 'neden bu mod?' aciklamasi olarak
    gosterebilir).
    """
    reason = ""
    details = {
        "requested_mode": requested,
        "vram": vram,
        "media_reserve_mb": s.vram_media_reserve_mb,
        "headroom_mb": s.vram_headroom_mb,
        "translation_min_mb": s.vram_translation_min_mb,
    }

    if requested == "local":
        reason = "Mod 'local' seciendi; her zaman yerel Ollama kullanilir."
        return {"decision": "local", "reason": reason, "details": details}
    if requested == "api":
        reason = "Mod 'api' seciendi; her zaman API saglayicisi kullanilir."
        return {"decision": "api", "reason": reason, "details": details}

    # auto
    if not vram.get("available"):
        reason = (
            "GPU/yeterli VRAM bilgisi bulunamadi; yerel model guvenli "
            "varsayilamaz, API saglayicisi secilir."
        )
        return {"decision": "api", "reason": reason, "details": details}

    free_mib = int(vram.get("free_mib") or 0)
    free_after = max(0, free_mib - s.vram_media_reserve_mb - s.vram_headroom_mb)
    details["free_after_reserve_mb"] = free_after
    if free_after >= s.vram_translation_min_mb:
        reason = (
            f"VRAM yeterli: {free_mib} MiB bos - {s.vram_media_reserve_mb} MiB "
            f"(OCR+LaMa) - {s.vram_headroom_mb} MiB headroom = {free_after} MiB "
            f">= {s.vram_translation_min_mb} MiB. Yerel Ollama modeli kullanilir."
        )
        return {"decision": "local", "reason": reason, "details": details}

    reason = (
        f"VRAM yerel model icin yetersiz: {free_after} MiB < "
        f"{s.vram_translation_min_mb} MiB esigi. API saglayicisi kullanilir."
    )
    return {"decision": "api", "reason": reason, "details": details}


def _api_has_credentials(provider: str, api_key: str | None,
                         base_url: str | None, model: str | None) -> bool:
    """API modunda saglayicinin calismasi icin yeterli kimlik/uycnokta var mi?"""
    key, url, _m, _t, _to = resolve_credentials(
        api_key, base_url, model, provider=provider)
    if provider == "anthropic":
        return bool((api_key or "").strip() or key)
    return bool((api_key or "").strip() or key or url)


# ---------------------------------------------------------------- pipeline

def _emit_func(emit, job_id: str, name: str, progress: float,
               message: str = "", data: dict | None = None) -> None:
    if emit is None:
        return
    emit({
        "job_id": job_id,
        "name": name,
        "progress": round(float(progress), 3),
        "message": message,
        "data": data or {},
    })


def translate_page_pipeline(
    image_path: str,
    target_lang: str = "en",
    mode: str = "auto",
    provider: str = "openai_compat",
    context: str = "",
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    timeout: float | None = None,
    settings_override: dict | None = None,
    emit: Callable[[dict], None] | None = None,
    job_id: str | None = None,
) -> dict:
    """Uctan uca sayfa cevirisi. Ilerleme event'leri emit(payload) ile cikar.

    Sonuc: Tauri'ye gonderilebilen (JSON-safe) sozluk.
    Hata durumunda PipelineError (kullanici dostu) firlatilir; build_user_error
    ile dinamik olarak da siniflandirilabilir.
    """
    job_id = job_id or uuid.uuid4().hex[:12]
    s = Settings.from_mapping(settings_override)
    timings: dict[str, float] = {}
    t_wall = time.perf_counter()

    def emit_(name, progress, message="", data=None):
        _emit_func(emit, job_id, name, progress, message, data)

    emit_("started", 0.0, "Sayfa islem basladi",
          {"image": str(image_path), "mode": mode, "provider": provider,
           "target_lang": target_lang})

    # ---- 0) gorsel ----
    img_path = Path(image_path)
    if not img_path.is_file():
        raise PipelineError(
            f"Gorsel dosyasi bulunamadi: {img_path}", "file_not_found")
    try:
        image = Image.open(img_path).convert("RGB")
    except Exception as exc:
        raise PipelineError(
            f"Gorsel acilamadi: {img_path}", "invalid_image") from exc
    w, h = image.size
    emit_("image_loaded", 0.03, f"{w}x{h} px")

    out_dir = Path(s.out_dir) if s.out_dir else img_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    vram = get_vram_info()
    decision = resolve_translation_mode(mode, s, vram)
    emit_("mode_decided", 0.05, decision["reason"], {
        "decision": decision["decision"], "requested": mode,
        "details": {
            k: v for k, v in decision["details"].items() if k != "vram"
        },
        "vram": {k: v for k, v in vram.items() if k != "reason_unavailable"},
    })
    chosen = decision["decision"]

    # ---- 1) tespit + OCR ----
    emit_("ocr_started", 0.1, "Balon / metin bolgeleri tespit ediliyor")
    try:
        detector = ComicTextDetector(conf=s.ocr_conf)
    except SystemExit as exc:
        raise PipelineError(
            "Bolge tespiti modeli kurulamadi veya indirilemedi. "
            "Internet baglantisini kontrol edip tekrar deneyin.",
            "model_download_failed") from exc
    t0 = time.perf_counter()
    detections = detector.detect(image)
    timings["detect"] = (time.perf_counter() - t0) * 1000

    text_regions = [d for d in detections if d["label"] in (1, 2)]
    if text_regions:
        keep = nms([d["bbox"] for d in text_regions],
                   [d["score"] for d in text_regions], iou_thr=0.5)
        text_regions = [text_regions[i] for i in keep]
        text_regions.sort(key=lambda d: (d["bbox"][1], d["bbox"][0]))
    bubbles = [d for d in detections if d["label"] == 0]

    emit_("ocr_detect_done", 0.16,
          f"{len(text_regions)} metin bolgesi, {len(bubbles)} balon",
          {"regions": len(text_regions), "bubbles": len(bubbles)})

    if not text_regions:
        raise PipelineError(
            "Sayfada cevrilecek metin bolgesi tespit edilemedi. "
            "Erisimi dusurmeyi deneyin (ocr_conf).", "no_text_found")

    emit_("ocr_models", 0.2, "manga-ocr modeli yukleniyor")
    from manga_ocr import MangaOcr
    try:
        ocr = MangaOcr(force_cpu=s.ocr_force_cpu)
    except Exception as exc:
        raise PipelineError(
            "manga-ocr modeli yuklenemedi. Internet baglantisini kontrol edin.",
            "model_download_failed") from exc

    t0 = time.perf_counter()
    for i, region in enumerate(text_regions):
        crop = crop_region(image, region["bbox"])
        region["text"] = ocr(crop)
        p = 0.2 + 0.22 * ((i + 1) / len(text_regions))
        emit_("ocr_progress", p,
              f"Bolge {i + 1}/{len(text_regions)} okundu",
              {"region_index": i, "region_count": len(text_regions),
               "text": (region.get("text") or "")[:20]})
    timings["ocr"] = (time.perf_counter() - t0) * 1000
    emit_("ocr_done", 0.44, f"{len(text_regions)} bolge OCR ile okundu")

    regions_json = out_dir / f"{img_path.stem}_ocr.json"
    if s.save_intermediate:
        regions_json.write_text(
            json.dumps({
                "image": str(img_path), "regions": [
                    {k: (list(v) if k == "bbox" else v)
                     for k, v in r.items()}
                    for r in text_regions
                ],
                "bubbles": [
                    {k: (list(v) if k == "bbox" else v)
                     for k, v in b.items()}
                    for b in bubbles
                ],
            }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 2) inpainting ----
    emit_("inpaint_started", 0.46, "Metin yuk simgeleri temizleniyor")
    orig_np = __import__("numpy").asarray(image)
    h_img, w_img = orig_np.shape[:2]
    if bubbles and s.inpaint_bubble_margin > 0:
        mask = apply_bubble_guard(
            w_img, h_img, bubbles, s.inpaint_bubble_margin, text_regions,
            dilate=s.inpaint_dilate, ring_width=s.inpaint_ring_width)
    else:
        mask = build_text_mask(w_img, h_img, text_regions)
        if s.inpaint_dilate > 0:
            mask = dilate_mask(mask, s.inpaint_dilate)
    t0 = time.perf_counter()
    if s.inpaint_method == "opencv":
        import cv2
        method_cv = "telea"
        res_bgr = inpaint_opencv(
            cv2.cvtColor(orig_np, cv2.COLOR_RGB2BGR), mask, method_cv, 3)
        res = cv2.cvtColor(res_bgr, cv2.COLOR_BGR2RGB)
    else:
        res = inpaint_lama(orig_np, mask, s.inpaint_device, s.lama_max_side,
                           None)
    if not s.no_refine_remnants:
        res = refine_remnants(res, mask)
    timings["inpaint"] = (time.perf_counter() - t0) * 1000

    cleaned = Image.fromarray(res)
    cleaned_path = out_dir / f"{img_path.stem}_cleaned.png"
    if s.save_intermediate:
        cleaned.save(cleaned_path)
    emit_("inpaint_done", 0.78, "Inpainting tamamlandi",
          {"cleaned": str(cleaned_path)})

    # ---- 3) ceviri backendi secimi + ceviri ----
    backend_name = "mock"
    warning: str | None = None
    if provider == "mock":
        backend_name = "mock"
    elif chosen == "local":
        backend_name = "local"
    else:
        backend_name = provider
        if provider != "mock" and not _api_has_credentials(
                provider, api_key, base_url, model):
            warning = (
                "API saglayicisi icin kimlik bilgisi bulunamadi "
                "(guvenli depo + .env bos); test mock cevirisi kullanilacak."
            )
            backend_name = "mock"

    if warning:
        emit_("warning", 0.8, warning,
              {"requested_provider": provider, "fallback": "mock"})

    emit_("translate_started", 0.8,
          f"Bolge metinleri cevriliyor "
          f"({backend_name})", {"backend": backend_name})

    try:
        backend = create_backend(
            backend_name,
            api_key=api_key,
            base_url=base_url,
            model=model,
            temperature=temperature,
            timeout=timeout,
        )
    except ValueError as exc:
        raise PipelineError(str(exc), "unknown_provider") from exc
    except Exception as exc:  # noqa: BLE001 - openai>=3 bos anahtar/yanlis url de
        if backend_name == "local":
            raise PipelineError(
                "Yerel Ollama sunucusuna ulasilamadi. Ollama'nin calistigini"
                " ve http://localhost:11434 adresini kontrol edin veya ceviri"
                " modunu 'api' yapin.", "local_unavailable") from exc
        raise

    # Mangada okuma sirasi: sutunlar sagdan sola, sutun ici ustten alta
    candidates = []
    for i, r in enumerate(text_regions):
        text = (r.get("text") or "").strip()
        if not text or text == "(manual)":
            continue
        r2 = dict(r)
        r2["index"] = i
        candidates.append(r2)
    if not candidates:
        raise PipelineError(
            "OCR metni bulunamadi; cevirilecek icerik yok.",
            "no_translatable_text")
    ordered = manga_reading_order(candidates)
    entries = [{"id": n - 1, "text": r["text"], "label_name": r["label_name"]}
               for n, r in enumerate(ordered, start=1)]

    t0 = time.perf_counter()
    try:
        translations = backend.translate_page(entries, target_lang, context=context)
    except Exception as exc:
        if backend_name == "local":
            raise PipelineError(
                "Yerel Ollama sunucusuna ulasilamadi. Ollama'nin calistigini"
                " ve http://localhost:11434 adresini kontrol edin veya ceviri"
                " modunu 'api' yapin.", "local_unavailable") from exc
        raise
    timings["translate"] = (time.perf_counter() - t0) * 1000
    emit_("translate_done", 0.9, f"Backend: {backend.name} ({backend.model})",
          {"backend": backend.name, "model": backend.model})

    for e in entries:
        if e["id"] not in translations or not translations[e["id"]].strip():
            translations[e["id"]] = "[untranslated]"

    # ---- 4) typeset ----
    emit_("typeset_started", 0.92, "Ceviri sayfaya yerlestiriliyor")
    canvas = cleaned.copy()
    t0 = time.perf_counter()
    typeset_info: dict[int, dict] = {}
    for e in entries:
        r = ordered[e["id"]]
        typeset_info[e["id"]] = typeset_region(
            canvas, r["bbox"], translations[e["id"]], s.font,
            min_size=s.min_font_size, max_size=s.max_font_size)
    timings["typeset"] = (time.perf_counter() - t0) * 1000

    translated_path = out_dir / f"{img_path.stem}_translated.png"
    canvas.save(translated_path)
    before_after_path = out_dir / f"{img_path.stem}_before_after.png"
    if s.save_intermediate:
        from translate_typeset_prototype import make_before_after
        make_before_after(image, cleaned, canvas, before_after_path)
    timings["total"] = (time.perf_counter() - t_wall) * 1000

    provider_used = backend.name
    payload = {
        "job_id": job_id,
        "image": str(img_path),
        "mode_decision": {
            "requested": mode,
            "decision": chosen,
            "chosen_backend": provider_used,
            "reason": decision["reason"],
            "details": {k: v for k, v in decision["details"].items() if k != "vram"},
            "vram": {k: v for k, v in vram.items() if k != "reason_unavailable"},
        },
        "provider": {"name": provider_used, "model": backend.model},
        "target_language": target_lang,
        "context": context,
        "reading_order": [
            {"id": e["id"], "index": r["index"], "label_name": r["label_name"],
             "bbox": list(r["bbox"]), "text": r["text"]}
            for e, r in zip(entries, ordered)
        ],
        "regions": [
            {
                "id": e["id"],
                "index": r["index"],
                "label_name": r["label_name"],
                "bbox": list(r["bbox"]),
                "original": r["text"],
                "translation": translations[e["id"]],
                "font_size": typeset_info[e["id"]]["font_size"],
                "lines": typeset_info[e["id"]]["lines"],
                "overflow": typeset_info[e["id"]]["overflow"],
                "style": typeset_info[e["id"]].get("style_used") or {},
            }
            for e, r in zip(entries, ordered)
        ],
        "timings_ms": {k: round(v, 1) for k, v in timings.items()},
        "warnings": [warning] if warning else [],
        "outputs": {
            "translated": str(translated_path),
            "cleaned": str(cleaned_path) if s.save_intermediate else None,
            "ocr_regions": str(regions_json) if s.save_intermediate else None,
            "before_after": str(before_after_path) if s.save_intermediate else None,
        },
    }
    emit_("done", 1.0,
          f"Sayfa tamamlandi: {len(entries)} bolge cevrildi",
          {"backend": provider_used, "model": backend.model,
           "outputs": payload["outputs"]})
    return payload


# ---------------------------------------------------------------- CLI / hizli test

def _cli() -> int:
    import argparse
    import json as _json
    p = argparse.ArgumentParser(description="Uctan uca sayfa pipeline (test)")
    p.add_argument("image", help="Manga sayfasi gorseli")
    p.add_argument("--target-lang", default="en")
    p.add_argument("--mode", default="auto", choices=["auto", "local", "api"])
    p.add_argument("--provider", default="mock",
                   choices=["mock", "local", "openai", "openai_compat", "anthropic"])
    p.add_argument("--settings", default=None,
                   help="JSON: {\"vram_translation_min_mb\": 12000}")
    args = p.parse_args()
    overrides = json.loads(args.settings) if args.settings else None
    lines: list[str] = []

    def emit(payload):
        lines.append(json.dumps(
            {"event": EVENT_NAME, "payload": payload}, ensure_ascii=False))

    result = translate_page_pipeline(
        args.image, target_lang=args.target_lang, mode=args.mode,
        provider=args.provider, settings_override=overrides, emit=emit)
    for ln in lines:
        print(ln)
    if result:
        print("")
        print(_json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_cli())
    except PipelineError as exc:
        print(f"Hata ({exc.code}): {exc.user_message}", file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:  # noqa: BLE001
        print(f"Hata: {build_user_error(exc)['message']}", file=sys.stderr)
        raise SystemExit(2)
