"""Geliştirme düzeneği (test-editor.html) için sentetik manga sayfaları üretir.

Üç görsel üretir, public/test/ altına yazar:
  page.png        — "orijinal": panelli sayfa, balonlarda sahte Japonca satırları
                    andıran gri çizgi blokları (JP glyph gerektirmez)
  page_cleaned.png— metin blokları silinmiş (inpaint sonucu taklidi)
  page_translated.png — ComicNeue-Bold ile İngilizce çeviri basılı

Kullanım: python scripts/make_harness_pages.py [--size WxH]
Varsayılan boyut 2480x3500 (A4 @300dpi — yüksek çözünürlük performans senaryosu).
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "public" / "test"
FONT = ROOT / "python" / "fonts" / "ComicNeue-Bold.ttf"

# Balonlar: (cx, cy, w, h, çeviri metni)
BUBBLES = [
    (620, 620, 900, 560, "The storm is coming!"),
    (1720, 590, 860, 520, "We should head back before dark."),
    (600, 1780, 920, 580, "Look at the horizon..."),
    (1750, 1750, 880, 600, "It is already too late."),
    (1150, 2950, 1100, 480, "Then let them come."),
]


def rounded_bubble(d: ImageDraw.ImageDraw, box: tuple, r: int = 60) -> None:
    x0, y0, x1, y1 = box
    d.ellipse((x0, y0, x0 + 2 * r, y1), fill="white", outline="black", width=6)
    d.ellipse((x1 - 2 * r, y0, x1, y1), fill="white", outline="black", width=6)
    d.rectangle((x0 + r, y0, x1 - r, y1), fill="white")
    d.rectangle((x0 + r, y0, x1 - r, y1), outline="black", width=6)
    d.rectangle((x0 + r + 3, y0 + 6, x1 - r - 3, y1 - 6), fill="white")


def fake_text_block(d: ImageDraw.ImageDraw, cx: int, cy: int, w: int, lines: int) -> None:
    """Japonca dikey sütunları andıran gri dikdörtgen bloklar."""
    col_w, gap = 34, 26
    n_cols = max(1, w // (col_w + gap))
    start_x = cx - (n_cols * col_w + (n_cols - 1) * gap) // 2
    for i in range(n_cols):
        x = start_x + i * (col_w + gap)
        d.rounded_rectangle(
            (x, cy - lines * 30 // 2, x + col_w, cy + lines * 30 // 2),
            radius=8,
            fill=(70, 70, 75),
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", default="2480x3500")
    args = ap.parse_args()
    W, H = (int(v) for v in args.size.lower().split("x"))

    OUT.mkdir(parents=True, exist_ok=True)

    def new_page() -> tuple[Image.Image, ImageDraw.ImageDraw]:
        img = Image.new("RGB", (W, H), (235, 235, 235))
        return img, ImageDraw.Draw(img)

    orig, d = new_page()
    # Paneller
    d.rectangle((40, 40, W - 40, H // 2 - 30), outline="black", width=10)
    d.rectangle((40, H // 2 + 10, W - 40, H - 40), outline="black", width=10)
    # Arka plan dokusu: koyu gri degrade şeritler
    for i in range(0, H, 90):
        shade = 200 - (i % 180) // 4
        d.line((60, i, W - 60, i + 30), fill=(shade, shade, shade), width=26)
    # Balonlar + sahte metin
    for cx, cy, bw, bh, _ in BUBBLES:
        box = (cx - bw // 2, cy - bh // 2, cx + bw // 2, cy + bh // 2)
        rounded_bubble(d, box)
        fake_text_block(d, cx, cy, bw - 160, lines=bh // 90)
    orig.save(OUT / "page.png")

    # Cleaned: balon içleri boş (metin inpaint edilmiş), geri kalan aynı
    cln = orig.copy()
    dc = ImageDraw.Draw(cln)
    for cx, cy, bw, bh, _ in BUBBLES:
        dc.ellipse(
            (cx - bw // 2 + 20, cy - bh // 2 + 20, cx + bw // 2 - 20, cy + bh // 2 - 20),
            fill="white",
        )
    cln.save(OUT / "page_cleaned.png")

    # Translated: temizlenmiş üzerine ComicNeue-Bold çeviri
    tr = cln.copy()
    dt = ImageDraw.Draw(tr)
    size = max(28, H // 90)
    font = ImageFont.truetype(str(FONT), size)
    for cx, cy, bw, bh, txt in BUBBLES:
        tw = dt.textbbox((0, 0), txt, font=font)[2]
        x = cx - tw // 2
        y = cy - size // 2
        stroke = max(2, int(size * 0.10))
        dt.text((x, y), txt, font=font, fill="black", stroke_width=stroke, stroke_fill="white")
    tr.save(OUT / "page_translated.png")

    print(f"yazıldı: {OUT}/page.png ({W}x{H}), page_cleaned.png, page_translated.png")


if __name__ == "__main__":
    main()
