"""PS Editor Python sidecar'ı.

Tauri tarafından stdin/stdout üzerinden JSON Lines protokolüyle sürülür.

Protokol:
  İstek (Tauri -> Python, stdin):   {"id": 1, "cmd": "hello", "payload": {...}}
  Yanıt (Python -> Tauri, stdout):  {"id": 1, "ok": true, "result": {...}}
  Hata:                             {"id": 1, "ok": false, "error": "..."}
  Olay (push):                      {"event": "ready", "payload": {...}}

Bu aşamada yalnızca Python standart kütüphanesi kullanılır; bağımlılık yoktur.
(İlerleyen adımlarda FastAPI / OCR / çeviri / inpainting katmanları eklenecek.)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone


def write_message(obj: dict) -> None:
    """stdout'a tek satır JSON yazar ve flush eder (pipe tamponu için kritik)."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


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


COMMANDS = {
    "hello": cmd_hello,
    "ping": cmd_ping,
    "check_cuda": cmd_check_cuda,
}


def main() -> int:
    # Pipe durumunda Python stdout'u blok tamponludur; satır bazlı tamponlama aç.
    sys.stdout.reconfigure(line_buffering=True)
    write_message({"event": "ready", "payload": {"version": "0.1.0"}})

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
        except Exception as exc:  # noqa: BLE001 - süreç canlı kalmalı
            write_message({"id": msg_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
