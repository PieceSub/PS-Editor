"""PS Editor Python sidecar'ı.

Tauri tarafından stdin/stdout üzerinden JSON Lines protokolüyle sürülür.

Protokol:
  İstek (Tauri -> Python, stdin):   {"id": 1, "cmd": "hello", "payload": {...}}
  Yanıt (Python -> Tauri, stdout):  {"id": 1, "ok": true, "result": {...}}
  Hata:                             {"id": 1, "ok": false, "error": "..."}
  Olay (push):                      {"event": "ready", "payload": {...}}

Adım 5'ten itibaren komutlar:
  - translate_page: uçtan uca OCR + inpainting + çeviri + typeset pipeline'ı.
    İlerleme, stdout'tan {"event": "translate_page_progress", "payload": {...}}
    olaylarıyla akar (payload.name = aşama; oran payload.progress, 0..1).
    Rust tarafı bunları "python-event" olarak ön yüze iletir (1. adımda kurulan
    mekanizmanın aynısı — yeni protokol yok). Sonuç, normal istek/yanıt
    kanalından döner.
  - set_api_key / delete_api_key: OS güvenli anahtar deposuna yazma/silme
    (Windows Credential Manager / macOS Keychain / Linux Secret Service;
    keyring kütüphanesi, PyInstaller için backend açıkça set edilir).
  - list_providers: desteklenen sağlayıcılar + anahtar durumu (anahtar yok!).
  - vram_report: GPU/VRAM durumu + çeviri modu kararı önizlemesi.

Hata politikası: kullanıcıya gösterilebilir mesajlar için SidecarError;
main() bunu yalnızca mesaj olarak (tip öneki olmadan) yanıtlar. pipeline.py
bu hatanın alt sınıfı olan PipelineError'ı kullanır ve build_user_error ile
API anahtarı/VRAM/model indirme hatalarını okunabilir Türkçe mesaja çevirir.

Not: Bu dosya modül yükleme zamanında ağır bağımlılık (torch/cv2/PIL)
içermez; pipeline.py yalnızca translate_page çağrıldığında (geç import)
yüklenir, böylece ping/hello/check_cuda anında çalışır.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone

# Protokol çıktısı her zaman ORİJİNAL stdout'a yazılır: pipeline sırasında
# üçüncü taraf print()'leri bastırmak için sys.stdout geçici olarak
# değiştirilebilir (quiet_stdout), ama JSON satırları bu referanstan akar.
_STDOUT = sys.stdout


def write_message(obj: dict) -> None:
    """stdout'a tek satır JSON yazar ve flush eder (pipe tamponu için kritik)."""
    _STDOUT.write(json.dumps(obj, ensure_ascii=False) + "\n")
    _STDOUT.flush()


class SidecarError(Exception):
    """Kullanıcıya gösterilebilir, stack-trace'siz hata. __str__ yalnızca mesajdır."""


def cmd_hello(payload: dict) -> dict:
    name = payload.get("name", "dünya")
    return {
        "greeting": f"Merhaba {name}! Python sidecar'ı çalışıyor.",
        "python_version": sys.version.split()[0],
        "time": datetime.now(timezone.utc).isoformat(),
    }


def cmd_ping(payload: dict) -> dict:
    return {"pong": True, "time": datetime.now(timezone.utc).isoformat()}


def _nvidia_smi() -> dict | None:
    """nvidia-smi bulunursa GPU listesini döndürür, yoksa None.

    nvidia-smi, NVIDIA sürücüsüyle birlikte Windows System32'ye kurulur;
    ağır bir bağımlılık (torch vb.) gerektirmez.
    """
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    gpus = []
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            gpus.append(
                {
                    "name": parts[0],
                    "driver": parts[1],
                    "vram_mib": int(float(parts[2])),
                }
            )
    return {"source": "nvidia-smi", "gpus": gpus}


def _torch_check() -> dict | None:
    """torch kuruluysa torch.cuda bilgisini döndürür, kurulu değilse None."""
    try:
        import torch  # opsiyonel; requirements.txt'e bak
    except ImportError:
        return None
    return {
        "source": "torch",
        "available": bool(torch.cuda.is_available()),
        "device_count": torch.cuda.device_count(),
        "device_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "version": torch.__version__,
    }


def cmd_check_cuda(payload: dict) -> dict:
    smi = _nvidia_smi()
    torch_info = _torch_check()
    if smi:
        cuda = True
        detail = smi
    elif torch_info and torch_info["available"]:
        cuda = True
        detail = torch_info
    else:
        cuda = False
        detail = {"gpus": [], "note": "NVIDIA GPU veya CUDA çalışma zamanı bulunamadı."}
    return {
        "cuda_available": cuda,
        "detail": detail,
        "torch_installed": torch_info is not None,
        "torch": torch_info,
    }


# ---------------------------------------------------------------- adim 5: pipeline

PROVIDER_NAMES = ["mock", "local", "openai", "openai_compat", "anthropic"]


def cmd_translate_page(payload: dict) -> dict:
    """Uçtan uca sayfa pipeline'ı.

    Girdi: image yolu + hedef dil + mod (auto/local/api) + sağlayıcı ayarları.
    Akış: translate_page_progress olayları (payload.name = aşama) -> sonuç.
    Uzun işlem (bir sayfa birkaç saniye); bu süre boyunca stdin döngüsü
    meşguldür — tek sayfa, tek komut kullanımı için kabul edilebilir.
    """
    from pipeline import (  # agir bagimliliklar yalnizca burada yuklenir
        EVENT_NAME,
        PipelineError,
        build_user_error,
        quiet_stdout,
        translate_page_pipeline,
    )
    from translate_typeset_prototype import load_env

    def emit(p: dict) -> None:
        write_message({"event": EVENT_NAME, "payload": p})

    try:
        with quiet_stdout():
            load_env()  # .env dev fallback'i (guvenli depo onceliklidir)
            return translate_page_pipeline(
                image_path=payload.get("image", ""),
                target_lang=payload.get("target_lang", "en"),
                mode=payload.get("mode", "auto"),
                provider=payload.get("provider", "openai_compat"),
                context=payload.get("context", ""),
                api_key=payload.get("api_key"),
                base_url=payload.get("base_url"),
                model=payload.get("model"),
                temperature=payload.get("temperature"),
                timeout=payload.get("timeout"),
                settings_override=payload.get("settings") or None,
                emit=emit,
                job_id=payload.get("job_id"),
            )
    except PipelineError as exc:
        emit({"name": "error", "progress": 1.0, "message": exc.user_message,
              "data": {"code": exc.code}})
        raise SidecarError(exc.user_message) from exc
    except Exception as exc:  # noqa: BLE001 - bilinmeyenler de kullanici dostu olsun
        err = build_user_error(exc)
        emit({"name": "error", "progress": 1.0, "message": err["message"],
              "data": {"code": err["code"], "debug": f"{type(exc).__name__}: {exc}"}})
        raise SidecarError(err["message"]) from exc


def cmd_set_api_key(payload: dict) -> dict:
    """API anahtarini OS guvenli deposuna yazar (asla yanit icinde gostermez)."""
    from translate_typeset_prototype import store_credential

    provider = payload.get("provider", "openai_compat")
    api_key = payload.get("api_key", "")
    if provider not in PROVIDER_NAMES or provider in ("mock", "local"):
        raise SidecarError(
            f"Bu saglayici icin anahtar saklanamaz: {provider!r} "
            "(yalnizca openai / openai_compat / anthropic).")
    if not api_key:
        raise SidecarError("API anahtari bos olamaz.")
    return store_credential(provider, api_key)


def cmd_delete_api_key(payload: dict) -> dict:
    from translate_typeset_prototype import secure_delete

    provider = payload.get("provider", "openai_compat")
    ok = secure_delete(provider)
    return {"deleted": ok, "provider": provider}


def cmd_list_providers(payload: dict) -> dict:
    from translate_typeset_prototype import secure_get

    providers = []
    for name in PROVIDER_NAMES:
        if name in ("mock", "local"):
            providers.append({"name": name, "needs_key": False,
                              "has_key": False})
        else:
            has = bool(secure_get(name))
            providers.append({"name": name, "needs_key": True, "has_key": has})
    return {"providers": providers}


def cmd_vram_report(payload: dict) -> dict:
    """GPU/VRAM + 'auto' modu karar onizlemesi (pipeline calistirmadan)."""
    from pipeline import Settings, get_vram_info, resolve_translation_mode

    vram = get_vram_info()
    s = Settings.from_mapping(payload.get("settings") or None)
    decision = resolve_translation_mode(
        payload.get("mode", "auto"), s, vram)
    return {
        "vram": {k: v for k, v in vram.items() if k != "reason_unavailable"},
        "mode_decision": {
            "requested": decision["details"]["requested_mode"],
            "decision": decision["decision"],
            "reason": decision["reason"],
            "settings": {
                "media_reserve_mb": s.vram_media_reserve_mb,
                "headroom_mb": s.vram_headroom_mb,
                "translation_min_mb": s.vram_translation_min_mb,
            },
        },
    }


def cmd_re_render_region(payload: dict) -> dict:
    """Bölge bazlı yeniden typeset (adim 7).

    Otomatik pipeline'i (OCR/inpaint/ceviri) yeniden calistirmadan yalnizca
    istenen bolgeyi gunceller: eski metni temizlenmis gorselden kırpıp
    yapistirir (veya elle eklenen bolgelerde yerel cv2 inpaint), yeni metni
    bolge stiliyle yazar. Hafif bagimliliklar (PIL + istege bagli cv2);
    torch YUKLENMEZ, yanit ~1 sn icinde doner.

    Girdi:
      output        : duzenlenecek translated gorsel yolu (ustune yazilir)
      cleaned       : temizlenmis gorsel yolu (istege bagli; "paste" icin)
      region        : {bbox, translation, style, erase, erase_boxes}
      settings      : Settings override (font / min_font_size / max_font_size)
    """
    from pipeline import Settings  # agir degil: Settings yalnizca dataclass
    from translate_typeset_prototype import DEFAULT_FONT, re_render_region

    s = Settings.from_mapping(payload.get("settings") or None)
    region = payload.get("region") or {}
    bbox = region.get("bbox") or []
    if len(bbox) != 4:
        raise SidecarError(
            "Bolge bbox'i gecersiz: [x1, y1, x2, y2] bekleniyor.")
    output = payload.get("output", "")
    if not output:
        raise SidecarError("Cikti gorsel yolu verilmedi (output).")

    try:
        return re_render_region(
            cleaned_path=payload.get("cleaned"),
            output_path=output,
            bbox=tuple(int(v) for v in bbox),
            translation=str(region.get("translation") or ""),
            font_path=str(s.font or DEFAULT_FONT),
            min_size=int(s.min_font_size),
            max_size=int(s.max_font_size),
            style=region.get("style") or None,
            erase=region.get("erase") or "paste",
            erase_boxes=[
                tuple(int(v) for v in box)
                for box in (region.get("erase_boxes") or [])
                if len(box) == 4
            ] or None,
        )
    except FileNotFoundError as exc:
        raise SidecarError(
            f"Gorsel bulunamadi: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - kullanici dostu mesaj
        raise SidecarError(
            f"Bolge yeniden render edilemedi: {type(exc).__name__}: {exc}"
        ) from exc


COMMANDS = {
    "hello": cmd_hello,
    "ping": cmd_ping,
    "check_cuda": cmd_check_cuda,
    "translate_page": cmd_translate_page,
    "re_render_region": cmd_re_render_region,
    "set_api_key": cmd_set_api_key,
    "delete_api_key": cmd_delete_api_key,
    "list_providers": cmd_list_providers,
    "vram_report": cmd_vram_report,
}


def main() -> int:
    # Pipe durumunda Python stdout'u blok tamponludur; satır bazlı tamponlama
    # aç. prototiplerdeki ensure_utf8_stdout ile aynı felsefe: stderr de
    # (manga-ocr/logging Japangıa yazabilir) UTF-8 + errors=replace olmadan
    # cp1254 gibi kodlamalarda UnicodeEncodeError fırlatılabilir.
    # stdin DE dahil üç akış da UTF-8'e sabitlenir. Kritik: Windows'ta pipe
    # olmayan bir akış için varsayılan kodlama ANSI kod sayfasıdır (cp125x);
    # Rust tarafı stdin'e her zaman ham UTF-8 baytları yazar (serde_json
    # ASCII kaçışı yapmaz), bu baytlar cp1252/cp1254 ile çözülürse "ü" ->
    # "Ã¼" mojibake olur ve Türkçe karakterli dosya yolları bulunamaz.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
        except Exception:  # noqa: BLE001 - sözlük/consol gibi akışlar değişemez
            pass
    write_message({"event": "ready", "payload": {"version": "0.2.0"}})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            write_message({"ok": False, "error": f"geçersiz JSON: {exc}"})
            continue

        msg_id = msg.get("id")
        cmd = msg.get("cmd")
        payload = msg.get("payload") or {}

        if cmd == "shutdown":
            write_message({"id": msg_id, "ok": True, "result": "kapatılıyor"})
            break

        handler = COMMANDS.get(cmd)
        try:
            if handler is None:
                raise ValueError(f"bilinmeyen komut: {cmd!r}")
            result = handler(payload)
            write_message({"id": msg_id, "ok": True, "result": result})
        except SidecarError as exc:
            # Kullanici dostu hata: tip adi OLMADAN, yalnizca mesaj yazilir.
            write_message({"id": msg_id, "ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - süreç canlı kalmalı
            write_message({"id": msg_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
