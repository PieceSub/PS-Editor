"""manga_cover_700x1080 icin piksel-seviyesi kalinti analizi.

Pipeline'in gercek yolunu (LaMa + refine_remnants) icerik testindeki
Telea yoluyla karsilastirir ve kalintinin kaynagini ayirt eder:

  A) maskenin glifi ORTMEMESI (bbox disina tasan murekkep) -> mask kalinligi
  B) maske icinde kalan koyu piksel (inpaint yontemi kalintiyi silmemis)
  C) ring bandi / edge hasari (dark_before != dark_after)

Cikti: her metin bolgesi icin murekkep orani (orijinal/telea/lama) +
      maske kapsama + balon ring metrikleri + zumlu paneller.
"""
import sys
sys.path.insert(0, "/home/yusufia/Projeler/PS-Editor/python")
from pathlib import Path
import numpy as np
from PIL import Image

from ocr_prototype import ComicTextDetector, nms
from inpaint_prototype import (
    apply_bubble_guard, inpaint_opencv, inpaint_lama, refine_remnants,
    adaptive_ring_width, ink_coverage, bubble_border_ink,
)

BASE = Path("/home/yusufia/Projeler/PS-Editor/python/test_data/regression")
OUT = Path("/home/yusufia/Projeler/PS-Editor/python/test_data/regression/guard_check")
OUT.mkdir(parents=True, exist_ok=True)
LAMA_MODEL = "/home/yusufia/.cache/torch/hub/checkpoints/big-lama.pt"

img_path = BASE / "manga_cover_700x1080.png"
orig = Image.open(img_path).convert("RGB")
w, h = orig.size
det = ComicTextDetector(conf=0.3)
dets = det.detect(orig)
texts = [d for d in dets if d["label"] in (1, 2)]
bubbles = [d for d in dets if d["label"] == 0]
if texts:
    keep = nms([d["bbox"] for d in texts], [d["score"] for d in texts], 0.5)
    texts = [texts[i] for i in keep]

mask = apply_bubble_guard(w, h, bubbles, 0.08, texts, dilate=4, ring_width=0)
orig_np = np.asarray(orig)
masked_px = int((mask > 0).sum())
print(f"gorsel: {img_path.name} {w}x{h} | {len(texts)} metin, "
      f"{len(bubbles)} balon | maske {masked_px} px "
      f"({100 * masked_px / (w * h):.2f}% alan)")

res_telea = inpaint_opencv(orig_np[:, :, ::-1], mask, "telea", 3)[:, :, ::-1]
res_telea = refine_remnants(res_telea, mask)
res_lama = inpaint_lama(orig_np, mask, "cpu", 2048, LAMA_MODEL)
res_lama = refine_remnants(res_lama, mask)

print("\n== metin bolgeleri: murekkep orani (gray<120) ==")
print(f"{'idx':>3} {'label':<11} {'bbox':<24} {'ink_orj':>8} "
      f"{'ink_telea':>9} {'ink_lama':>9} {'maske_kapsama':>12}")
for i, r in enumerate(texts):
    bbox = r["bbox"]
    x1, y1, x2, y2 = bbox
    ink_orig = ink_coverage(orig_np, bbox)
    ink_t = ink_coverage(res_telea, bbox)
    ink_l = ink_coverage(res_lama, bbox)
    gray = orig_np[y1:y2, x1:x2].mean(axis=2)
    ink_px_total = int((gray < 120).sum())
    inner_mask = mask[y1:y2, x1:x2] > 0
    covered = int((inner_mask & (gray < 120)).sum())
    miss = ink_px_total - covered
    print(f"{i:3d} {r['label_name']:<11} {str(bbox):<24} "
          f"{ink_orig:8.4f} {ink_t:9.4f} {ink_l:9.4f} "
          f"{covered}/{ink_px_total} (kacak {miss})")

print("\n== balon ring metrikleri (dark gray<120) ==")
print(f"{'idx':>3} {'bbox':<24} {'rw':>3} {'before':>7} "
      f"{'telea_son':>9} {'lama_son':>9}")
for i, b in enumerate(bubbles):
    x1, y1, x2, y2 = b["bbox"]
    rw = adaptive_ring_width(x2 - x1, y2 - y1)
    print(f"{i:3d} {str(b['bbox']):<24} {rw:3d} "
          f"{bubble_border_ink(orig_np, b):7d} "
          f"{bubble_border_ink(res_telea, b):9d} "
          f"{bubble_border_ink(res_lama, b):9d}")

def panel(res, suffix):
    p = Image.new("RGB", (w * 2 + 8, h), (40, 40, 40))
    p.paste(orig, (0, 0))
    p.paste(Image.fromarray(res), (w + 8, 0))
    p.save(OUT / f"{img_path.stem}_{suffix}.png")
    # balon zumlu crop'lar
    crops = []
    for b in bubbles:
        x1, y1, x2, y2 = b["bbox"]
        cx1, cy1 = max(0, x1 - 30), max(0, y1 - 30)
        cx2, cy2 = min(w, x2 + 30), min(h, y2 + 30)
        o = orig.crop((cx1, cy1, cx2, cy2))
        r = Image.fromarray(res).crop((cx1, cy1, cx2, cy2))
        cell = Image.new("RGB", (o.width * 2 + 6, o.height), (40, 40, 40))
        cell.paste(o, (0, 0))
        cell.paste(r, (o.width + 6, 0))
        crops.append(cell)
    ch = max(c.height for c in crops)
    grid = Image.new("RGB", (sum(c.width for c in crops) + 8,
                             ch * len(crops) + 8), (40, 40, 40))
    yy, xx = 4, 4
    for c in crops:
        grid.paste(c, (xx, yy))
        xx += c.width + 4
        yy += ch + 4
    grid.save(OUT / f"{img_path.stem}_{suffix}_zoom_bubbles.png")
    print(f"  paneller: {img_path.stem}_{suffix}.png (+zoom)")

print("\n== cikti panelleri ==")
panel(res_telea, "telea")
panel(res_lama, "lama")