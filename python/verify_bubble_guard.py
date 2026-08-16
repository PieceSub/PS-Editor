"""Balon kenar korumasi dogrulama (adim 3, istek d) — GERCEK PIPELINE YOLU.

GUI'nin birebir kullandigi kodla calisir (pipeline.py'nin inpaint adimi):
tespit -> build_bubble_outline_mask (gercek cizgi pikselleri) ->
build_bubble_junk_mask (banddaki cizgi-olmayan kirintilar) ->
apply_bubble_guard(outline=..., junk_mask=...) -> inpaint_lama ->
refine_remnants.

Metrikler (balon basina):
  - outline_delta: GERCEK cizgi piksellerindeki koyu sayisinin oncesi/sonrasi
    farki. Kenar korumasi %100 ise delta=0 (zaten bu adim 3'un hedefi).
  - junk_cleaned: bandda cizgi-disi kalan koyu kirintilarin temizlenen
    sayisi (0'den buyuk OLABILIR; kalinti temizliginin isareti).
  - ring_bad: guard maskesinin outline pikseline tasmasi (0 olmali).

Ayrica orijinal/onarilmis zumlu (crop) karsilastirma gorselleri uretilir.
"""
import sys
sys.path.insert(0, "/home/yusufia/Projeler/PS-Editor/python")
from pathlib import Path
import numpy as np
from PIL import Image
import cv2

from ocr_prototype import ComicTextDetector, nms
from inpaint_prototype import (
    apply_bubble_guard, inpaint_lama, refine_remnants,
    adaptive_ring_width, build_bubble_outline_mask,
    build_bubble_junk_mask,
)

BASE = Path("/home/yusufia/Projeler/PS-Editor/python/test_data/regression")
MAIN_T = Path("/home/yusufia/Projeler/PS-Editor/python/test_data/manga_test.png")
OUT = Path("/home/yusufia/Projeler/PS-Editor/python/test_data/regression/guard_check")
OUT.mkdir(parents=True, exist_ok=True)
LAMA_MODEL = "/home/yusufia/.cache/torch/hub/checkpoints/big-lama.pt"
DARK = 120

JOBS = [
    (BASE / "manga_cover_700x1080.png", "kapak 700x1080 (dikey)"),
    (BASE / "manga_cover_square_1024.png", "kapak kare 1024x1024"),
    (BASE / "manga_square_1024.png", "icerik kare 1024x1024"),
    (BASE / "manga_landscape_1600x900.png", "icerik yatay 1600x900"),
    (MAIN_T, "icerik dikey 800x1130"),
]

def band_for(bbox, rw, hw, ww):
    x1, y1, x2, y2 = bbox
    band = np.zeros((hw, ww), dtype=np.uint8)
    cv2.rectangle(band, (x1, y1), (x2, y2), 255, -1)
    cv2.rectangle(band, (x1 + rw, y1 + rw), (x2 - rw, y2 - rw), 0, -1)
    return band

def dark_px(img: np.ndarray) -> np.ndarray:
    return img.mean(axis=2) < DARK

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
        orig_np = np.asarray(orig)
        # pipeline.py ile BIREBİR ayni adimlar
        b_line = build_bubble_outline_mask(orig_np, bubbles)
        b_junk = build_bubble_junk_mask(orig_np, bubbles)
        mask = apply_bubble_guard(w, h, bubbles, 0.08, texts, dilate=4,
                                  ring_width=0, outline=b_line,
                                  junk_mask=b_junk)
        res = inpaint_lama(orig_np, mask, "cpu", 2048, LAMA_MODEL)
        res = refine_remnants(res, mask)

        outline_px = (b_line > 0) if b_line is not None else np.zeros((h, w), bool)
        junk_px = (b_junk > 0) if b_junk is not None else np.zeros((h, w), bool)
        ring_bad = int((mask > 0)[outline_px].sum())
        dark_b = dark_px(orig_np)
        dark_r = dark_px(res)

        print(f"\n== {desc}  {img_path.name} ({w}x{h})  "
              f"{len(texts)} metin, {len(bubbles)} balon-kutu ==")
        print(f"   maskede gercek cizgi pikseli: {ring_bad}")
        page_ok = ring_bad == 0
        per_bubble = []
        for i, b in enumerate(bubbles):
            x1, y1, x2, y2 = b["bbox"]
            rw = adaptive_ring_width(x2 - x1, y2 - y1)
            band = band_for((x1, y1, x2, y2), rw, h, w) > 0
            o_on = dark_b & band & outline_px
            r_on = dark_r & band & outline_px
            d = int(o_on.sum()) - int(r_on.sum())
            junk_clean = int((dark_b & band & junk_px & ~outline_px).sum()) - \
                         int((dark_r & band & junk_px & ~outline_px).sum())
            ok = d == 0
            page_ok &= ok
            all_ok &= ok
            per_bubble.append((img_path.name, x1, y1, x2, y2, rw,
                               int(o_on.sum()), int(r_on.sum()), d, junk_clean, ok))
            print(f"   balon[{i}] ({x1},{y1},{x2},{y2}) ring~{rw}px "
                  f"cizgi_before={int(o_on.sum())} cizgi_after={int(r_on.sum())} "
                  f"delta={d} junk_temiz={junk_clean} {'TAMAM' if ok else 'HATA'}")
        if ring_bad:
            page_ok = False
            all_ok = False
            print("   HATA: maske balon cizgisine tasiyor")
        table.extend(per_bubble)

        # zumlu oncesi/sonrasi karsilastirma
        panel = Image.new("RGB", (w * 2 + 8, h), (40, 40, 40))
        panel.paste(orig, (0, 0))
        panel.paste(Image.fromarray(res), (w + 8, 0))
        panel.save(OUT / f"{img_path.stem}_before_after.png")
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
    print("\nOZET TABLOSU (gorsel, balon, ring_px, cizgi_before, "
          "cizgi_after, junk_temiz):")
    for name, x1, y1, x2, y2, rw, db, da, d, junk, ok in table:
        print(f"  {name:28s} ({x1:4d},{y1:4d},{x2:4d},{y2:4d}) ring={rw:2d}px "
              f"before={db:5d} after={da:5d} delta={d:4d} "
              f"junk_temiz={junk:4d} {'TAMAM' if ok else 'HATA'}")
    return 0 if all_ok else 1

if __name__ == "__main__":
    sys.exit(main())