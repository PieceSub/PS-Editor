"""Balon kenar korumasi dogrulama (adim 3, istek d).

Her boyuttaki test gorseli icin: tespit -> maske (yeni adaptif ring) ->
inpaint (opencv, maske mekanigi icin yeterli) -> refine_remnants.
Balon sinir halkasindaki koyu piksel sayisi (dark_before/dark_after)
karsilastirilir: kenar korumasi %100 ise delta = 0.

Ayrica orijinal/onarilmis zumlu (crop) karsilastirma gorselleri uretilir.
"""
import sys
sys.path.insert(0, "/home/yusufia/Projeler/PS-Editor/python")
import json
from pathlib import Path
import numpy as np
from PIL import Image

from ocr_prototype import ComicTextDetector, nms
from inpaint_prototype import (
    apply_bubble_guard, inpaint_opencv, refine_remnants,
    adaptive_ring_width, report_metrics,
)

BASE = Path("/home/yusufia/Projeler/PS-Editor/python/test_data/regression")
MAIN_T = Path("/home/yusufia/Projeler/PS-Editor/python/test_data/manga_test.png")
OUT = Path("/home/yusufia/Projeler/PS-Editor/python/test_data/regression/guard_check")
OUT.mkdir(parents=True, exist_ok=True)

JOBS = [
    (BASE / "manga_cover_700x1080.png", "kapak 700x1080 (dikey)"),
    (BASE / "manga_cover_square_1024.png", "kapak kare 1024x1024"),
    (BASE / "manga_square_1024.png", "icerik kare 1024x1024"),
    (BASE / "manga_landscape_1600x900.png", "icerik yatay 1600x900"),
    (MAIN_T, "icerik dikey 800x1130"),
]

def main():
    det = ComicTextDetector(conf=0.3)
    all_ok = True
    table = []
    for img_path, desc in JOBS:
        orig = Image.open(img_path).convert("RGB")
        w, h = orig.size
        dets = det.detect(orig)
        texts = [d for d in dets if d["label"] in (1, 2)]
        bubbles = [d for d in dets if d["label"] == 0]
        if texts:
            keep = nms([d["bbox"] for d in texts], [d["score"] for d in texts], 0.5)
            texts = [texts[i] for i in keep]
        mask = apply_bubble_guard(w, h, bubbles, 0.08, texts, dilate=4,
                                  ring_width=0)
        hw, ww = mask.shape[:2]
        orig_np = np.asarray(orig)
        res = inpaint_opencv(
            np.asarray(orig.convert("RGB"))[:, :, ::-1], mask, "telea", 3)[:, :, ::-1]
        res = refine_remnants(res, mask)

        # kenar halkasinda maske var mi? (ayni adaptif ring geometrisi ile)
        ring_bad = 0
        for b in bubbles:
            x1, y1, x2, y2 = b["bbox"]
            rw = adaptive_ring_width(x2 - x1, y2 - y1)
            band = np.zeros((hw, ww), dtype=bool)
            band[y1:y2 + 1, x1:x2 + 1] = True
            band[y1 + rw:y2 - rw + 1, x1 + rw:x2 - rw + 1] = False
            ring_bad += int((mask > 0)[band].sum())

        rm, bs = report_metrics(orig_np, res, texts, bubbles)
        print(f"\n== {desc}  {img_path.name} ({w}x{h})  "
              f"{len(texts)} metin, {len(bubbles)} balon-kutu ==")
        print(f"   maskede ring bandindaki piksel: {ring_bad}")
        page_ok = True
        for i, b in enumerate(bubbles):
            x1, y1, x2, y2 = b["bbox"]
            # olceklendirilmis ring genisligini raporla
            rw = max(8, min(32, int(np.ceil(min(x2 - x1, y2 - y1) * 0.10))))
            d = bs[i]["dark_before"] - bs[i]["dark_after"]
            ok = d == 0
            page_ok &= ok
            all_ok &= ok
            print(f"   balon[{i}] ({x1},{y1},{x2},{y2}) ring~{rw}px "
                  f"dark_before={bs[i]['dark_before']} "
                  f"dark_after={bs[i]['dark_after']} "
                  f"delta={d} {'TAMAM' if ok else 'HATA'}")
        if ring_bad:
            page_ok = False
            all_ok = False
            print("   HATA: maske balon cizgi bandina tasiyor")
        for i, b in enumerate(bubbles):
            x1, y1, x2, y2 = b["bbox"]
            rw = adaptive_ring_width(x2 - x1, y2 - y1)
            table.append((img_path.name, x1, y1, x2, y2, rw,
                          bs[i]["dark_before"], bs[i]["dark_after"]))

        # zumlu oncesi/sonrasi karsilastirma
        panel = Image.new("RGB", (w * 2 + 8, h), (40, 40, 40))
        panel.paste(orig, (0, 0))
        panel.paste(Image.fromarray(res), (w + 8, 0))
        panel.save(OUT / f"{img_path.stem}_before_after.png")
        # her balonun zumlu (crop&buyut) hali: orijinal / onarilmis
        crops = []
        for i, b in enumerate(bubbles):
            x1, y1, x2, y2 = b["bbox"]
            cx1, cy1 = max(0, x1 - 26), max(0, y1 - 26)
            cx2, cy2 = min(w, x2 + 26), min(h, y2 + 26)
            o = orig.crop((cx1, cy1, cx2, cy2))
            r = Image.fromarray(res).crop((cx1, cy1, cx2, cy2))
            cell = Image.new("RGB", (o.width * 2 + 6, o.height), (40, 40, 40))
            cell.paste(o, (0, 0))
            cell.paste(r, (o.width + 6, 0))
            crops.append(cell)
        if crops:
            ch = max(c.height for c in crops)
            grid = Image.new("RGB", (sum(c.width for c in crops) + 8,
                                     ch * len(crops) + 8), (40, 40, 40))
            yy = 4
            xx = 4
            for c in crops:
                grid.paste(c, (xx, yy))
                xx += c.width + 4
                yy += ch + 4
            grid.save(OUT / f"{img_path.stem}_zoom_bubbles.png")
        print(f"   sonuc: {'TAMAM' if page_ok else 'HATA'}")
    print(f"\nGENEL: {'TUMU TAMAM' if all_ok else 'HATA VAR'}")
    print("\nOZET TABLOSU (gorsel, balon, ring_px, dark_before, dark_after):")
    for name, x1, y1, x2, y2, rw, db, da in table:
        print(f"  {name:28s} ({x1:4d},{y1:4d},{x2:4d},{y2:4d}) ring={rw:2d}px "
              f"before={db:5d} after={da:5d} delta={db - da:4d} "
              f"{'TAMAM' if db == da else 'HATA'}")
    return 0 if all_ok else 1

if __name__ == "__main__":
    sys.exit(main())