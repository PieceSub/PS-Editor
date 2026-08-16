"""PS Editor - OCR prototipi (adım 2).

Komut satırından bir manga sayfası görseli alır ve sırayla:
  1) Balon / metin bölgesi tespiti yapar
     (ogkalu/comic-text-and-bubble-detector — Apache-2.0, RT-DETR-v2,
      ONNX Runtime ile CPU üzerinde; model ilk çalıştırmada Hugging Face'ten iner)
  2) Her bölgeyi manga-ocr (Apache-2.0) ile okur
     (PyTorch; CUDA varsa GPU'da, yoksa CPU'da — otomatik)
  3) Sonuçları bounding box koordinatlarıyla terminale yazdırır
  4) --visualize ile bölgeleri çizilmiş bir görüntü dosyası kaydeder

Kullanım:
  python ocr_prototype.py <görsel> [--visualize [çıktı_yolu]] [--conf 0.3]
                            [--force-cpu] [--json] [--detector-model YOL]
                            [--debug]

Örnekler:
  python ocr_prototype.py test_data\\manga_test.png --visualize
  python ocr_prototype.py test_data\\manga_test.png --json --conf 0.4

Not: Windows terminalinde Japonca çıktı bozuk görünürse şu komutlarla
UTF-8 çıktıyı açın:
  chcp 65001
  (PowerShell 5.1: [Console]::OutputEncoding = [System.Text.Encoding]::UTF8)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

MODEL_REPO = "ogkalu/comic-text-and-bubble-detector"
MODEL_FILE = "detector-v4-s_int8.onnx"
MODEL_URL = f"https://huggingface.co/{MODEL_REPO}/resolve/main/{MODEL_FILE}"

CLASS_NAMES = {0: "bubble", 1: "text_bubble", 2: "text_free"}
CLASS_COLORS = {0: (66, 133, 244), 1: (15, 157, 88), 2: (245, 124, 0)}  # BGR değil, RGB

# Dekoratif (çevrilmemesi gereken başlık/logo) sezgiseli.
# Model sınıflandırması yalnızca "balon içi metin / serbest metin" ayrımı
# yapar; kapak sayfalarında başlık bandı, logo, yazar mührü gibi öğeleri de
# text_bubble/text_free olarak işaretler. Bu eşikler yalnızca BÜYÜK ölçekli
# adayları işaretler:
#   - alanı sayfanın %15'inden büyük bölge (kapak başlığı / tam sayfa yanlış
#     pozitifi): tipik diyalog balonları sayfa alanının ~%3-8'idir.
#   - sayfa genişliğinin %55'inden geniş VE yüksekliği %15'inden kısa bant
#     (tam genişlik başlık şeridi).
# Editörde "devre dışı" olarak gelirler; kullanıcı isterse elle açabilir
# (yanlış pozitif güvenlik ağı).
DECORATIVE_MIN_AREA_FRAC = 0.15
DECORATIVE_BAND_MIN_W = 0.55
DECORATIVE_BAND_MAX_H = 0.18

# text_free (serbest metin / SFX / başlık) için ek güven eşiği.
# Kapaklardaki dekoratif öğeler (logo, yayıncı yazısı, imza mührü) düşük
# skorla tespit edilir; gerçek konuşma balonları text_bubble sınıfındadır.
# Bu eşik YALNIZCA text_free sınıfına uygulanır: altında kalan adaylar
# dekoratif ("varsayılan kapalı") işaretlenir ve otomatik typeset'e girmez.
# Kalibrasyon (test_data/regression + gerçek kapak ölçümleri):
#   dekoratif text_free öğeler   -> 0.41-0.79 (imza 0.41, başlık 0.48-0.55,
#                                    gerçek kapak logoları 0.51-0.79)
#   sentetik diyalog text_bubble -> 0.67-0.85 (eşiğe tabi değil)
#   0.80; gözlenen en yüksek dekoratif skoru (0.79) kaplar, gerçek balonları
#   etkilemez. Ortam değişkeni / ayar ile değiştirilebilir.
TEXT_FREE_MIN_CONF = 0.8

JP_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\YuGothM.ttc",
    r"C:\Windows\Fonts\YuGothB.ttc",
    r"C:\Windows\Fonts\msgothic.ttc",
    r"C:\Windows\Fonts\YuGothR.ttc",
    r"C:\Windows\Fonts\meiryo.ttc",
]


def info(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- yardımcılar

def ensure_utf8_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def find_jp_font(size: int = 20) -> ImageFont.FreeTypeFont | None:
    for path in JP_FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return None


def gpu_info() -> dict:
    """torch + nvidia-smi üzerinden GPU durumunu döndürür (sidecar ile aynı felsefe)."""
    info = {"torch_cuda": False, "device_name": None, "nvidia_smi": None}
    try:
        import torch
    except ImportError:
        return info
    info["torch_cuda"] = bool(torch.cuda.is_available())
    if info["torch_cuda"]:
        info["device_name"] = torch.cuda.get_device_name(0)
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10, encoding="utf-8",
                errors="replace",
            )
            if out.returncode == 0 and out.stdout.strip():
                info["nvidia_smi"] = out.stdout.strip().splitlines()[0].strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return info


def iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / union if union > 0 else 0.0


def nms(boxes: list[tuple], scores: list[float], iou_thr: float = 0.5) -> list[int]:
    """Basit greedy NMS; kabul edilen indeksleri döndürür."""
    order = sorted(range(len(boxes)), key=lambda i: scores[i], reverse=True)
    keep: list[int] = []
    while order:
        i = order.pop(0)
        keep.append(i)
        order = [j for j in order if iou(boxes[i], boxes[j]) <= iou_thr]
    return keep


def crop_region(image: Image.Image, bbox: tuple[int, int, int, int],
                pad: int = 6) -> Image.Image:
    """Bounding box'ı küçük bir kenarlık payıyla kırpar, sınırlara kilitler."""
    x1, y1, x2, y2 = bbox
    x1 = max(0, x1 - pad)
    y1 = max(0, y1 - pad)
    x2 = min(image.width, x2 + pad)
    y2 = min(image.height, y2 + pad)
    return image.crop((x1, y1, x2, y2))


def load_image(path: str | Path) -> Image.Image:
    """Görseli RGB olarak açar; EXIF yönlendirmesini uygular.

    Tarayıcı <img> etiketi EXIF Orientation'ı onurlar; ham piksel koordinatları
    üzerinde çalışan detektör de aynı dönüşümü yapmalıdır — aksi halde
    telefon/akıllı tarayıcıdan gelen gerçek görsellerde bbox'lar ekrandaki
    görsele göre kayık görünür (döndürülmüş koordinat uzayı).
    """
    from PIL import ImageOps

    image = Image.open(path).convert("RGB")
    return ImageOps.exif_transpose(image)


def decorative_flags(w: int, h: int, bbox: tuple[int, int, int, int]) -> tuple[bool, str]:
    """Bölgenin 'muhtemelen dekoratif başlık/logo' olduğunu sezgisel ile bulur.

    Model çıktısındaki yalnızca geometrik sinyalleri kullanır (sınıf ayrımı
    zaten yok): sayfaya göre büyüklük ve aşırı geniş kısa bant. Döndürür:
    (dekoratif mi?, gerekçe). Detaylar modül başındaki sabitlerde.
    """
    x1, y1, x2, y2 = bbox
    area_frac = ((x2 - x1) * (y2 - y1)) / max(1, w * h)
    wf = (x2 - x1) / max(1, w)
    hf = (y2 - y1) / max(1, h)
    if area_frac >= DECORATIVE_MIN_AREA_FRAC:
        return True, f"bölge sayfanın %{100 * area_frac:.0f}'ini kaplıyor"
    if wf >= DECORATIVE_BAND_MIN_W and hf <= DECORATIVE_BAND_MAX_H:
        return True, "tam genişlikte kısa bant (başlık/logo)"
    return False, ""


# ---------------------------------------------------------------- tespit

class ComicTextDetector:
    """RT-DETR-v2 (comic-text-and-bubble-detector) ONNX backend'i.

    comic-translate (Apache-2.0) projesinin kullandığı protokolün birebir
    aynısı: giriş (1,3,640,640) float32 [0,1] + orig_target_sizes [[w,h]];
    çıktılar labels/boxes/scores, kutular orijinal görsel koordinatında.
    Sınıflar: 0=bubble, 1=text_bubble, 2=text_free.
    """

    def __init__(self, onnx_path: str | None = None, conf: float = 0.3,
                 text_free_min_conf: float = TEXT_FREE_MIN_CONF):
        import onnxruntime as ort

        self.conf = conf
        self.text_free_min_conf = text_free_min_conf
        self.onnx_path = onnx_path or self._resolve_model()
        so = ort.SessionOptions()
        so.log_severity_level = 3
        so.intra_op_num_threads = 4
        so.inter_op_num_threads = 1
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(
            self.onnx_path, sess_options=so, providers=["CPUExecutionProvider"]
        )
        info(f"Tespit modeli yüklendi: {Path(self.onnx_path).name} "
             f"({Path(self.onnx_path).stat().st_size / 1e6:.1f} MB, CPU)")

    @staticmethod
    def _resolve_model() -> str:
        """Modeli Hugging Face'ten indirir (HF cache'e), varsa yolu döndürür."""
        try:
            from huggingface_hub import hf_hub_download

            info(f"Tespit modeli indiriliyor ({MODEL_URL}) ...")
            path = hf_hub_download(repo_id=MODEL_REPO, filename=MODEL_FILE)
            return path
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(
                f"Tespit modeli indirilemedi: {exc}\n"
                f"Manuel indirme: {MODEL_URL}\n"
                f"İndirilen dosyayı --detector-model <yol> ile verin."
            ) from exc

    def detect(self, image: Image.Image) -> list[dict]:
        w, h = image.size
        resized = image.resize((640, 640))
        arr = np.asarray(resized, dtype=np.float32) / 255.0
        arr = np.transpose(arr, (2, 0, 1))[np.newaxis, ...]  # (1,3,640,640)
        orig_size = np.array([[w, h]], dtype=np.int64)

        labels, boxes, scores = self.session.run(
            None, {"images": arr, "orig_target_sizes": orig_size}
        )

        if labels.ndim == 2 and labels.shape[0] == 1:
            labels = labels[0]
        if scores.ndim == 2 and scores.shape[0] == 1:
            scores = scores[0]
        if boxes.ndim == 3 and boxes.shape[0] == 1:
            boxes = boxes[0]

        results: list[dict] = []
        for lab, box, scr in zip(labels, boxes, scores):
            score = float(scr)
            if score < self.conf:
                continue
            x1, y1, x2, y2 = (max(0, int(v)) for v in box)
            x2 = min(w, x2)
            y2 = min(h, y2)
            if x2 <= x1 or y2 <= y1:
                continue
            decorative, reason = decorative_flags(w, h, (x1, y1, x2, y2))
            if (not decorative and int(lab) == 2
                    and score < self.text_free_min_conf):
                # text_free ve güven eşiğinin altında: dekoratif varsay —
                # kapak logoları/yayıncı yazısı/imza gibi öğeler düşük
                # skorla tespit edilir. Verinin sahibi kullanıcıdır:
                # bölge editöre "varsayılan kapalı" gelir, kullanıcı
                # isterse etkinleştirebilir.
                decorative = True
                reason = (f"güven skoru {score:.2f} < "
                          f"{self.text_free_min_conf:.2f} (text_free eşiği)")
            results.append({
                "label": int(lab),
                "label_name": CLASS_NAMES.get(int(lab), f"unknown_{int(lab)}"),
                "score": score,
                "bbox": (x1, y1, x2, y2),
                "decorative": decorative,
                "decorative_reason": reason,
            })
        return results


# ---------------------------------------------------------------- OCR

def run_ocr(image: Image.Image, regions: list[dict], force_cpu: bool) -> None:
    """Her metin bölgesini manga-ocr ile okur; sonuçları regions'a yazar."""
    from manga_ocr import MangaOcr

    info("manga-ocr modeli yükleniyor (ilk çalıştırmada indirir, ~400 MB)...")
    t0 = time.perf_counter()
    ocr = MangaOcr(force_cpu=force_cpu)
    info(f"manga-ocr hazır ({(time.perf_counter() - t0):.1f} s, "
         f"cihaz: {'GPU' if not force_cpu else 'CPU'})")

    for i, region in enumerate(regions):
        crop = crop_region(image, region["bbox"])
        t1 = time.perf_counter()
        text = ocr(crop)
        region["text"] = text
        region["ocr_ms"] = int((time.perf_counter() - t1) * 1000)
        print("")
        print(f"  Bölge {i + 1}/{len(regions)}  [{region['label_name']}]  "
              f"conf={region['score']:.2f}  bbox={region['bbox']}  "
              f"OCR={region['ocr_ms']} ms")
        print(f"    Metin: {text or '(boş)'}")


# ---------------------------------------------------------------- görselleştirme

def visualize(image: Image.Image, regions: list[dict], out_path: Path,
              debug: bool = False) -> None:
    """Bölgeleri orijinal görsel üzerine çizer ve kaydeder.

    Dekoratif olarak işaretlenen bölgeler (başlık/logo adayı) kesikli gri
    çerçeveyle çizilir; normal bölgeler sınıf renginde düz çerçeve alır.
    """
    vis = image.convert("RGB")
    draw = ImageDraw.Draw(vis)
    font = find_jp_font(20)

    for i, region in enumerate(regions, start=1):
        x1, y1, x2, y2 = region["bbox"]
        color = CLASS_COLORS.get(region["label"], (128, 128, 128))
        if region.get("decorative"):
            draw_dashed_rect(draw, (x1, y1, x2, y2), color=(120, 120, 120), width=3)
        else:
            draw.rectangle((x1, y1, x2, y2), outline=color, width=3)

        badge = f"#{i}"
        if font:
            bb = draw.textbbox((0, 0), badge, font=font)
            bw, bh = bb[2] - bb[0], bb[3] - bb[1]
            bx, by = x1, max(0, y1 - bh - 6)
            draw.rectangle((bx, by, bx + bw + 6, by + bh + 6),
                           fill=(120, 120, 120) if region.get("decorative") else color)
            draw.text((bx + 3, by + 3), badge, font=font, fill="white")

        text = region.get("text")
        if text and font:
            label = text if len(text) <= 24 else text[:24] + "…"
            draw.text((x1, min(vis.height - 6, y2 + 4)), label, font=font,
                      fill=(20, 20, 20), stroke_width=2, stroke_fill="white")

    vis.save(out_path)
    info(f"Görselleştirme kaydedildi: {out_path}")


def draw_dashed_rect(draw: ImageDraw.ImageDraw,
                     box: tuple[int, int, int, int],
                     color, width: int = 3, dash: int = 10) -> None:
    """PIL'de hazır desteği olmayan kesikli dikdörtgen çizer."""
    x1, y1, x2, y2 = box
    edges = [
        ((x1, y1), (x2, y1)),
        ((x2, y1), (x2, y2)),
        ((x2, y2), (x1, y2)),
        ((x1, y2), (x1, y1)),
    ]
    for (ax, ay), (bx, by) in edges:
        horizontal = ay == by
        length = abs(bx - ax) if horizontal else abs(by - ay)
        step = max(1, 2 * dash)
        t = 0
        while t < length:
            end = min(t + dash, length)
            if horizontal:
                draw.line((ax + t, ay, ax + end, ay), fill=color, width=width)
            else:
                draw.line((ax, ay + t, ax, ay + end), fill=color, width=width)
            t += step


# ---------------------------------------------------------------- CLI

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Manga sayfası OCR prototipi: balon tespiti + manga-ocr",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("image", help="Manga sayfası görseli (JPG/PNG)")
    p.add_argument("--visualize", nargs="?", const="", metavar="ÇIKTI",
                   help="Bölgeleri çizip ayrı dosyaya kaydet (varsayılan: <ad>_annotated.png)")
    p.add_argument("--conf", type=float, default=0.3,
                   help="Tespit güven eşiği (0-1)")
    p.add_argument("--text-free-min-conf", type=float, default=TEXT_FREE_MIN_CONF,
                   help="text_free sınıfı için ayrı eşik: altında kalan "
                        "bölgeler dekoratif/varsayılan-kapalı işaretlenir "
                        "(text_bubble'a uygulanmaz)")
    p.add_argument("--force-cpu", action="store_true",
                   help="GPU olsa bile OCR'ı CPU'da çalıştır")
    p.add_argument("--json", action="store_true",
                   help="Sonuçları makine-okunur JSON olarak da bas")
    p.add_argument("--detector-model", metavar="YOL",
                   help="Yerel ONNX model dosyası (indirme yerine)")
    p.add_argument("--debug", action="store_true",
                   help="ONNX session giriş/çıkış bilgilerini göster")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()
    args = parse_args(argv)

    image_path = Path(args.image)
    if not image_path.is_file():
        print(f"Hata: görsel bulunamadı: {image_path}")
        return 2

    gpu = gpu_info()
    if gpu["torch_cuda"]:
        info(f"GPU: {gpu['device_name']}  (OCR bu cihazda çalışacak)")
    elif gpu["nvidia_smi"]:
        info(f"GPU algılandı (nvidia-smi: {gpu['nvidia_smi']}) ancak "
             f"torch CUDA erişemiyor; OCR CPU'da çalışacak.")
    else:
        info("GPU bulunamadı; OCR CPU'da çalışacak.")
    if args.force_cpu:
        info("--force-cpu verildi; OCR CPU'da zorlanıyor.")

    image = load_image(image_path)
    info(f"Görsel: {image_path} ({image.width}x{image.height})")

    # ---- 1) Tespit ----
    detector = ComicTextDetector(onnx_path=args.detector_model, conf=args.conf,
                                 text_free_min_conf=args.text_free_min_conf)
    t0 = time.perf_counter()
    detections = detector.detect(image)
    t_detect = time.perf_counter() - t0
    info(f"Tespit: {len(detections)} aday bölge ({t_detect * 1000:.0f} ms)")

    if args.debug:
        for i, det in enumerate(detections):
            extra = ""
            if det.get("decorative"):
                extra = f"  [DEKORATIF: {det['decorative_reason']}]"
            info(f"  det[{i}] {det['label_name']} score={det['score']:.3f} "
                 f"bbox={det['bbox']}{extra}")

    # ---- 2) Bölgeleri ayır: OCR yalnızca metin sınıflarında (1, 2) ----
    text_regions = [d for d in detections if d["label"] in (1, 2)]
    if text_regions:
        keep = nms([d["bbox"] for d in text_regions],
                   [d["score"] for d in text_regions], iou_thr=0.5)
        text_regions = [text_regions[i] for i in keep]
        text_regions.sort(key=lambda d: (d["bbox"][1], d["bbox"][0]))

    bubbles = [d for d in detections if d["label"] == 0]
    decor_count = sum(1 for d in text_regions if d.get("decorative"))
    info(f"Metin bölgesi (OCR adayı): {len(text_regions)}  "
         f"(balon çerçevesi: {len(bubbles)})"
         + (f"  — {decor_count} dekoratif aday (başlık/logo)" if decor_count else ""))

    if not text_regions:
        print("Uyarı: metin bölgesi tespit edilemedi. "
              "Eşiği düşürmeyi deneyin: --conf 0.2")

    # ---- 3) OCR ----
    run_ocr(image, text_regions, args.force_cpu)

    # ---- 4) Görselleştirme ----
    if args.visualize is not None:
        out = Path(args.visualize) if args.visualize else image_path.with_name(
            f"{image_path.stem}_annotated.png")
        visualize(image, text_regions + bubbles, out)
    # ---- JSON çıktı ----
    if args.json:
        payload = {
            "image": str(image_path),
            "gpu": gpu,
            "detector": {"model": MODEL_REPO + "/" + MODEL_FILE,
                         "backend": "onnxruntime-cpu", "conf": args.conf},
            "ocr": {"model": "kha-white/manga-ocr-base",
                    "device": "cpu" if args.force_cpu else ("gpu" if gpu["torch_cuda"] else "cpu")},
            "regions": [
                {**{k: v for k, v in r.items() if k != "bbox"},
                 "bbox": list(r["bbox"])}
                for r in text_regions
            ],
        }
        print("")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
