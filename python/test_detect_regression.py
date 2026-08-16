"""Tespit koordinat dönüşümü regresyon testi (kare + kare olmayan sayfalar).

Amaç: sayfa boyutu / en-boy oranı değiştiğinde detektör bbox'larının görselle
hizası bozulmasın. Ayrıca "dekoratif aday" sezgiselini doğrular (başlık bandı
decorative, diyalog balonları değil).

Ölçülen üç bağımsız iddia:
  1) Tüm kutular görsel sınırları içinde.
  2) Her gerçek (ground truth) diyalog metninin merkezi bir tespit kutusunun
     içinde (ya da çok yakınında) — koordinat dönüşümü hatasız.
  3) Ölçek değişmezliği: sayfanın yarı boyutlu kopyasında, normalleştirilmiş
     kutu merkezleri birebir eşleşir (stretch-resize + orig_target_sizes
     protokolünün kendisini sınar; en-boy oranı ne olursa olsun).
  4) Dekoratif sezgiseli: başlık bandı tespit edildiyse decorative=True,
     diyalog balonları asla decorative değil.

Çalıştırma (python/ dizininden):
    .venv/bin/python test_detect_regression.py
    .venv/bin/python test_detect_regression.py --conf 0.2

torch / manga-ocr YÜKLEMEZ; yalnızca onnxruntime + PIL (hızlı, ~1-2 s).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image

from make_test_manga import find_jp_font, render_page
from ocr_prototype import CLASS_NAMES, ComicTextDetector, nms

HERE = Path(__file__).resolve().parent

# Test edilen sayfalar: (ad, boyut, kapak mı). Kare, dikey kapak ve yatay
# sayfa oranlarının üçü de kapsanır.
PAGES = [
    ("dikey sayfa 800x1130 (mevcut test verisi)", (800, 1130), False),
    ("kare sayfa 1024x1024", (1024, 1024), False),
    ("yatay sayfa 1600x900", (1600, 900), False),
    ("kapak 700x1080 (dikey)", (700, 1080), True),
    ("kapak kare 1024x1024", (1024, 1024), True),
]

# Eşikler. %3: detektör kutusu glif bloğunun merkezini kapsayabilmesi için
# rahat tolerans; asıl hata modu (ölçekleme bozuksa) onlarca % olur.
CENTER_TOL_FRAC = 0.03
DECORATIVE_IOU = 0.25
SCALE_INVARIANT_TOL_FRAC = 0.03

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "TAMAM" if ok else "HATA"
    print(f"  [{status}] {name}{(' - ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


def iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / union if union > 0 else 0.0


def best_match(detections: list[dict], gt_box: tuple[int, int, int, int]) -> dict | None:
    """GT kutusuyla en yüksek IoU'lu tespiti döndürür (eşik yok, boş olabilir)."""
    best, best_iou = None, 0.0
    for d in detections:
        if d["label"] not in (1, 2):
            continue
        v = iou(d["bbox"], gt_box)
        if v > best_iou:
            best, best_iou = d, v
    return best


def center(box: tuple[int, int, int, int]) -> tuple[float, float]:
    x1, y1, x2, y2 = box
    return (x1 + x2) / 2, (y1 + y2) / 2


def run_page(name: str, size: tuple[int, int], cover: bool,
             detector: ComicTextDetector, conf: float) -> None:
    print(f"== {name} ==")
    font_path = find_jp_font()
    if font_path is None:
        check(f"{name}: Japonca yazı tipi yok, sayfa üretilemedi", False)
        return
    image, gt = render_page(size, cover, font_path)
    w, h = image.size
    detections = detector.detect(image)

    # NMS (pipeline ile aynı: yalnızca metin sınıfları)
    texts = [d for d in detections if d["label"] in (1, 2)]
    if texts:
        keep = nms([d["bbox"] for d in texts], [d["score"] for d in texts], 0.5)
        texts = [texts[i] for i in keep]

    # ---- 1) sınırlar ----
    bad = [d for d in detections if not (0 <= d["bbox"][0] < d["bbox"][2] <= w
                                          and 0 <= d["bbox"][1] < d["bbox"][3] <= h)]
    check(f"{name}: {len(detections)} tespit kutusu görsel sınırları içinde",
          not bad, f"taşan: {len(bad)}" if bad else "")

    # ---- 2) diyalog GT merkezleri tespit kutusu içinde ----
    gt_required = [g for g in gt if g.get("required")]
    missing = []
    for g in gt_required:
        gx1, gy1, gx2, gy2 = g["bbox"]
        cx, cy = center(g["bbox"])
        tol_x, tol_y = w * CENTER_TOL_FRAC, h * CENTER_TOL_FRAC
        hit = any(
            (d["bbox"][0] <= cx + tol_x and d["bbox"][2] >= cx - tol_x
             and d["bbox"][1] <= cy + tol_y and d["bbox"][3] >= cy - tol_y)
            for d in texts)
        if not hit:
            missing.append(f"{g['name']} merkez({cx:.0f},{cy:.0f}) bulunamadı")
    check(f"{name}: {len(gt_required)} diyalog metni hizalı tespit edildi",
          not missing, "; ".join(missing) if missing else "")

    # ---- 2b) GT diyalog bölgesine düşen tespit decorative DEĞİL ----
    for g in gt_required:
        m = best_match(texts, tuple(g["bbox"]))
        if m is not None:
            check(f"{name}: '{g['name']}' decorative işaretlenmemiş",
                  not m.get("decorative"),
                  f"gerekçe: {m.get('decorative_reason', '?')}" if m.get("decorative") else "")

    # ---- 2c) dekoratif GT (başlık bandı) tespit edildiyse decorative=TRUE ----
    for g in [x for x in gt if x.get("decorative") and "baslik" in x["name"]]:
        m = best_match(texts, tuple(g["bbox"]))
        if m is not None:
            check(f"{name}: başlık bandı tespiti decorative işaretli",
                  bool(m.get("decorative")),
                  f"IoU={iou(m['bbox'], tuple(g['bbox'])):.2f}, "
                  f"alan={((m['bbox'][2]-m['bbox'][0])*(m['bbox'][3]-m['bbox'][1])/(w*h))*100:.0f}%")

    # ---- 3) ölçek değişmezliği (yarı boyutta aynı normalleştirilmiş hiza) ----
    half = image.resize((w // 2, h // 2), Image.LANCZOS)
    det_half = detector.detect(half)
    texts_half = [d for d in det_half if d["label"] in (1, 2)]
    if texts_half:
        keep = nms([d["bbox"] for d in texts_half],
                   [d["score"] for d in texts_half], 0.5)
        texts_half = [texts_half[i] for i in keep]
    drift = []
    for d in texts:
        cx, cy = center(d["bbox"])
        nx, ny = cx / w, cy / h
        best_n, best_dist = None, 1e9
        for d2 in texts_half:
            cx2, cy2 = center(d2["bbox"])
            dist = max(abs(cx2 / (w / 2) - nx), abs(cy2 / (h / 2) - ny))
            if dist < best_dist:
                best_n, best_dist = d2, dist
        if best_n is None or best_dist > SCALE_INVARIANT_TOL_FRAC:
            drift.append(f"#{len(drift)} sapma={best_dist:.3f}")
    check(f"{name}: yarı boyutta ölçek değişmezliği "
          f"({len(texts)} kutu, tol={SCALE_INVARIANT_TOL_FRAC:.2f})",
          not drift, "; ".join(drift[:5]) if drift else "")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Tespit koordinat dönüşümü regresyon testi")
    parser.add_argument("--conf", type=float, default=0.3,
                        help="Tespit güven eşiği")
    args = parser.parse_args()

    detector = ComicTextDetector(conf=args.conf)
    print(f"Model: {Path(detector.onnx_path).name}  conf={args.conf}\n")
    for name, size, cover in PAGES:
        run_page(name, size, cover, detector, args.conf)
        print("")

    if failures:
        print(f"\nSONUÇ: HATA — {len(failures)} başarısız kontrol")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\nSONUÇ: TAMAM — tüm kontroller geçti ({len(PAGES)} sayfa)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
