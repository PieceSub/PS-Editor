"""PS Editor - Sahte manga test sayfası üretici (adım 2).

Telif sorunu olmayan, programatik olarak üretilmiş bir manga sayfası
oluşturur: paneller, konuşma balonları, bir anlatım (caption) kutusu ve
bir efekt yazısı (SFX). İçindeki tüm Japonca metinler bu script'in ürettiği
özgün örnek cümlelerdir; hiçbir eserden alıntı yoktur.

Kullanım:
    python make_test_manga.py [çıktı_yolu]

Varsayılan çıktı:  test_data/manga_test.png  (script'in bulunduğu dizine göre)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PAGE_W, PAGE_H = 800, 1130
OUT_DEFAULT = Path(__file__).resolve().parent / "test_data" / "manga_test.png"

# Panel düzeni: 2 sütun x 3 sıra
PANEL_COLS = [(60, 380), (420, 740)]
PANEL_ROWS = [(60, 385), (425, 750), (790, 1070)]


def find_jp_font() -> str | None:
    """Windows'ta Japonca glif içeren bir yazı tipi bulur (None olabilir)."""
    candidates = [
        r"C:\Windows\Fonts\YuGothM.ttc",   # Yu Gothic Medium
        r"C:\Windows\Fonts\YuGothB.ttc",   # Yu Gothic Bold (SFX için)
        r"C:\Windows\Fonts\msgothic.ttc",  # MS Gothic
        r"C:\Windows\Fonts\YuGothR.ttc",
        r"C:\Windows\Fonts\meiryo.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def draw_hatch(draw: ImageDraw.ImageDraw, x1: int, y1: int, x2: int, y2: int,
               step: int = 18, color: str = "#ECECEC", width: int = 1) -> None:
    """Panel içine 45° tarama (screen tone) çizer; taşmalar PIL tarafından kırpılır."""
    h = y2 - y1
    for off in range(-h, x2 - x1 + h, step):
        draw.line((x1 + off, y1, x1 + off + h, y2), fill=color, width=width)


def draw_bubble(draw: ImageDraw.ImageDraw, cx: int, cy: int, w: int, h: int,
                tail: tuple | None = None) -> None:
    """Beyaz dolgulu, siyah konturlu konuşma balonu (opsiyonel kuyruk)."""
    x1, y1, x2, y2 = cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2
    draw.ellipse((x1, y1, x2, y2), fill="white", outline="black", width=3)
    if tail:
        draw.polygon(tail, fill="white", outline="black")


def vertical_columns(page: Image.Image, cx: int, cy: int, lines: list[str],
                     font: ImageFont.FreeTypeFont, gap: int = 26) -> None:
    """Japonca dikey (tategaki) metni, soldan sağa birden çok sütun olarak çizer.

    Gerçek manga tipografisinde dikey sütundaki glifler dik durur (yalnızca
    bazı noktalama işaretleri 90° döner); bu nedenle glifleri dik istifler.
    """
    draw = ImageDraw.Draw(page)
    advance = font.size + 2
    col_widths = [max(font.getbbox(ch)[2] - font.getbbox(ch)[0] for ch in text)
                  for text in lines]
    total_w = sum(col_widths) + gap * (len(lines) - 1)
    x = cx - total_w / 2
    for text, cw in zip(lines, col_widths):
        top = cy - len(text) * advance / 2
        y = top
        for ch in text:
            gw = font.getbbox(ch)[2] - font.getbbox(ch)[0]
            draw.text((x + (cw - gw) / 2, y), ch, font=font, fill="black")
            y += advance
        x += cw + gap


def fit_dialogue_font(path: str, text: str, max_h: int,
                      min_size: int = 18, max_size: int = 30) -> ImageFont.FreeTypeFont:
    """Metin sütunu verilen yüksekliğe sığacak en büyük font boyutunu döner."""
    longest = max(len(t) for t in text)
    size = min(max_size, int((max_h - 30) / longest))
    size = max(min_size, size)
    return ImageFont.truetype(path, size)


def main() -> int:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT_DEFAULT
    font_path = find_jp_font()
    if font_path is None:
        print("Uyarı: Japonca yazı tipi bulunamadı; metinler çizilemeyecek.")
        return 1
    font_dlg = ImageFont.truetype(font_path, 26)
    font_sfx = ImageFont.truetype(font_path, 46)

    page = Image.new("RGB", (PAGE_W, PAGE_H), "#FFFFFF")
    draw = ImageDraw.Draw(page)

    # ---- Paneller ----
    for c, (x1, x2) in enumerate(PANEL_COLS):
        for r, (y1, y2) in enumerate(PANEL_ROWS):
            draw.rectangle((x1, y1, x2, y2), fill="#FFFFFF", outline="black", width=3)
            if c == 0 and r == 1:  # orta sol panelde tarama (B3'ün zemini)
                draw_hatch(draw, x1 + 6, y1 + 6, x2 - 6, y2 - 6)

    # ---- Konuşma balonları (bubble) ----
    bubbles = [
        # (cx, cy, w, h, kuyruk, [dikey sütunlar])
        (220, 225, 300, 225, (90, 320, 50, 385, 135, 340), ["こんにちは！", "元気ですか？"]),
        (580, 215, 290, 210, (650, 300, 720, 345, 660, 330), ["はい、", "元気ですよ"]),
        (220, 585, 300, 230, (100, 690, 60, 745, 145, 705), ["すごい！", "大発見だ！"]),
        (580, 580, 300, 235, (650, 695, 715, 740, 660, 660), ["まさか…", "そんなことが"]),
        (220, 930, 310, 235, (95, 1030, 60, 1060, 150, 1040), ["それでは、", "行こう！"]),
    ]
    for cx, cy, w, h, tail, lines in bubbles:
        draw_bubble(draw, cx, cy, w, h, tail)
        font = fit_dialogue_font(font_path, lines, h)
        vertical_columns(page, cx, cy, lines, font)

    # ---- Anlatım kutusu (text_free): dikdörtgen + dikey metin ----
    cap_x1, cap_y1, cap_x2, cap_y2 = 470, 830, 620, 1020
    draw.rectangle((cap_x1, cap_y1, cap_x2, cap_y2), fill="#FFFFFF",
                   outline="black", width=3)
    cap_lines = ["翌朝、", "物語が", "始まる"]
    cap_font = ImageFont.truetype(font_path, 24)
    vertical_columns(page, (cap_x1 + cap_x2) // 2, (cap_y1 + cap_y2) // 2,
                     cap_lines, cap_font, gap=20)

    # ---- Efekt yazısı (SFX, text_free): yatay + hafif döndürülmüş ----
    sfx = "ドキドキ"
    tmp = Image.new("RGBA", (420, 140), (0, 0, 0, 0))
    tdraw = ImageDraw.Draw(tmp)
    bb = tdraw.textbbox((0, 0), sfx, font=font_sfx)
    tdraw.text(((420 - (bb[2] - bb[0])) // 2 - bb[0], (140 - (bb[3] - bb[1])) // 2 - bb[1]),
               sfx, font=font_sfx, fill="black",
               stroke_width=2, stroke_fill="black")
    tmp = tmp.rotate(-12, expand=True, resample=Image.BICUBIC)
    sx, sy = 320, 1015
    page.paste(tmp, (int(sx - tmp.width / 2), int(sy - tmp.height / 2)), tmp)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    page.save(out_path)
    print(f"Test görseli oluşturuldu: {out_path} ({PAGE_W}x{PAGE_H})")
    print("Beklenen bölgeler: 5 balon (text_bubble) + 1 anlatım kutusu + 1 SFX (text_free)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
