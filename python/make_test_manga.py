"""PS Editor - Sahte manga test sayfası üretici (adım 2).

Telif sorunu olmayan, programatik olarak üretilmiş bir manga sayfası
oluşturur: paneller, konuşma balonları, bir anlatım (caption) kutusu ve
bir efekt yazısı (SFX). İçindeki tüm Japonca metinler bu script'in ürettiği
özgün örnek cümlelerdir; hiçbir eserden alıntı yoktur.

En-boy oranı ve kapak benzeri düzen üretilebilir (tespit koordinat
dönüşümünün farklı oranlarda doğrulanması için):

  --size WxH : sayfa boyutunu değiştirir (varsayılan 800x1130, dikey).
               Düzen 800x1130 sanal tuvaline göre orantısal ölçeklenir,
               hedef boyuta ortalanır. Kare (1024x1024), yatay (1600x900)
               vb. üretilebilir.
  --cover    : kapak benzeri düzen: tam genişlikte başlık bandı (dekoratif),
               yazar mührü (dekoratif), bir kılıç silueti (çizim öğesi) ve
               2 konuşma balonu (diyalog). Gerçek kapak sayfalarında
               detektörün davranışını ölçmek içindir.

  --gt-json  : her görsel öğenin (balon metni, başlık, mühür, SFX) gerçek
               piksel bbox'unu yazar (test_detect_regression.py bunu
               doğrulama referansı olarak kullanır; ayrıca burada
               "decorative" etiketi vardır).

Kullanım:
    python make_test_manga.py [çıktı_yolu] [--size WxH] [--cover] [--gt-json YOL]

Varsayılan çıktı:  test_data/manga_test.png  (script'in bulunduğu dizine göre)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 800x1130 sanal tuval: tüm düzen bunun üzerinde tanımlanır, hedef boyuta
# orantısal ölçeklenir.
PAGE_W, PAGE_H = 800, 1130
OUT_DEFAULT = Path(__file__).resolve().parent / "test_data" / "manga_test.png"

# Panel düzeni: 2 sütun x 3 sıra (yalnızca sayfa düzeni modunda)
PANEL_COLS = [(60, 380), (420, 740)]
PANEL_ROWS = [(60, 385), (425, 750), (790, 1070)]

# Konuşma balonları: (cx, cy, w, h, kuyruk, [dikey sütunlar])
BUBBLES = [
    (220, 225, 300, 225, (90, 320, 50, 385, 135, 340), ["こんにちは！", "元気ですか？"]),
    (580, 215, 290, 210, (650, 300, 720, 345, 660, 330), ["はい、", "元気ですよ"]),
    (220, 585, 300, 230, (100, 690, 60, 745, 145, 705), ["すごい！", "大発見だ！"]),
    (580, 580, 300, 235, (650, 695, 715, 740, 660, 660), ["まさか…", "そんなことが"]),
    (220, 930, 310, 235, (95, 1030, 60, 1060, 150, 1040), ["それでは、", "行こう！"]),
]

# Kapak düzeni (--cover): başlık bandı + mühür + 2 diyalog balonu
COVER_TITLE = "ベルセルク"
COVER_BUBBLES = [
    (230, 520, 310, 230, (110, 620, 60, 680, 160, 640), ["こんにちは！", "元気ですか？"]),
    (590, 780, 300, 230, (660, 890, 725, 935, 675, 900), ["まさか…", "そんなことが"]),
]

# Yazı tipi adayları: Windows / macOS / Linux
JP_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\YuGothM.ttc",
    r"C:\Windows\Fonts\YuGothB.ttc",
    r"C:\Windows\Fonts\msgothic.ttc",
    r"C:\Windows\Fonts\YuGothR.ttc",
    r"C:\Windows\Fonts\meiryo.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
]


def find_jp_font() -> str | None:
    """Japonca glif içeren bir yazı tipi bulur (None olabilir)."""
    for path in JP_FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def parse_size(text: str) -> tuple[int, int]:
    parts = text.lower().replace("x", " ").split()
    if len(parts) != 2:
        raise SystemExit(f"Geçersiz boyut: {text!r} (WxH, ör. 1024x1024)")
    w, h = int(parts[0]), int(parts[1])
    if w < 200 or h < 200:
        raise SystemExit(f"Geçersiz boyut: {text!r} (en az 200x200)")
    return w, h


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
                     font: ImageFont.FreeTypeFont, gap: int = 26) -> tuple[int, int, int, int]:
    """Japonca dikey (tategaki) metni, soldan sağa birden çok sütun olarak çizer.

    Gerçek manga tipografisinde dikey sütundaki glifler dik durur (yalnızca
    bazı noktalama işaretleri 90° döner); bu nedenle glifleri dik istifler.
    Çizilen metnin piksel bbox'unu döndürür (test için gerçek konum).
    """
    draw = ImageDraw.Draw(page)
    advance = font.size + 2
    col_widths = [max(font.getbbox(ch)[2] - font.getbbox(ch)[0] for ch in text)
                  for text in lines]
    total_w = sum(col_widths) + gap * (len(lines) - 1)
    x = cx - total_w / 2
    xs, ys, xe, ye = cx, cy, cx, cy
    for text, cw in zip(lines, col_widths):
        top = cy - len(text) * advance / 2
        y = top
        for ch in text:
            gw = font.getbbox(ch)[2] - font.getbbox(ch)[0]
            draw.text((x + (cw - gw) / 2, y), ch, font=font, fill="black")
            y += advance
        xs = min(xs, x)
        xe = max(xe, x + cw)
        ys = min(ys, top)
        ye = max(ye, y - advance)
        x += cw + gap
    return int(xs), int(ys), int(xe), int(ye)


def fit_dialogue_font(path: str, text: list[str], max_h: int,
                      min_size: int = 18, max_size: int = 30) -> ImageFont.FreeTypeFont:
    """Metin sütunu verilen yüksekliğe sığacak en büyük font boyutunu döner."""
    longest = max(len(t) for t in text)
    size = min(max_size, int((max_h - 30) / longest))
    size = max(min_size, size)
    return ImageFont.truetype(path, size)


def draw_sword(page: Image.Image, cx: int, cy: int, scale: float) -> None:
    """Kapak modunda: metin içermeyen büyük bir çizim öğesi (kılıç silueti).

    Gerçek kapaklarda detektörün çizim öğelerini metin sanması durumunu
    taklit eder; bbox'ı GT'ye 'dekoratif çizim' olarak yazılır (gereksinim
    yok — yalnızca gözlem verisi).
    """
    draw = ImageDraw.Draw(page)
    # Kabza + balçak (artıkça sağa giden yatay yay)
    gw = int(90 * scale)
    draw.line((cx - gw, cy, cx + gw, cy), fill=(40, 40, 40), width=int(18 * scale))
    draw.line((cx - gw, cy, cx - gw - int(30 * scale), cy - int(40 * scale)),
              fill=(90, 70, 40), width=int(10 * scale))
    # Namlu (dikey)
    draw.polygon([
        (cx - int(16 * scale), cy - int(30 * scale)),
        (cx + int(16 * scale), cy - int(30 * scale)),
        (cx + int(9 * scale), cy - int(420 * scale)),
        (cx - int(9 * scale), cy - int(420 * scale)),
    ], fill=(60, 60, 70))


def render_page(size: tuple[int, int], cover: bool,
                font_path: str) -> tuple[Image.Image, list[dict]]:
    """Belirtilen boyutta test sayfası üretir; (görsel, GT bbox listesi) döndürür.

    Düzen 800x1130 sanal tuvalinde tanımlanır, s = min(w/800, h/1130) ile
    orantılı ölçeklenir ve hedef tuvalin ortasına yerleştirilir. GT listesi:
    {name, label_name, bbox, decorative, required} sözlüklerinden oluşur;
    "required" = detektörün bulması GEREKEN diyalog metni (test iddiası).
    """
    w, h = size
    s = min(w / PAGE_W, h / PAGE_H)
    ox, oy = int((w - PAGE_W * s) / 2), int((h - PAGE_H * s) / 2)

    def S(v: float) -> int:
        return int(round(v * s))

    page = Image.new("RGB", (w, h), "#FFFFFF")
    gt: list[dict] = []

    if cover:
        # ---- Başlık bandı (dekoratif): tam genişlik, üstte ----
        band_h = S(240)
        title_font = ImageFont.truetype(font_path, S(150))
        draw = ImageDraw.Draw(page)
        draw.rectangle((0, 0, w, band_h), fill="#202020")
        bb = draw.textbbox((0, 0), COVER_TITLE, font=title_font)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        draw.text(((w - tw) // 2 - bb[0], (band_h - th) // 2 - bb[1]),
                  COVER_TITLE, font=title_font, fill="#F2E9D0")
        gt.append({
            "name": "baslik", "label_name": "text_free",
            "bbox": [0, 0, w, band_h], "decorative": True, "required": False,
        })

        # ---- Yazar mührü (dekoratif) ----
        seal_cx, seal_cy = S(690), S(930)
        seal_sz = S(110)
        seal_font = ImageFont.truetype(font_path, S(46))
        draw.rectangle((seal_cx - seal_sz // 2, seal_cy - seal_sz // 2,
                        seal_cx + seal_sz // 2, seal_cy + seal_sz // 2),
                       fill="#B31E30")
        bb = draw.textbbox((0, 0), "三浦", font=seal_font)
        draw.text((seal_cx - (bb[2] - bb[0]) // 2 - bb[0],
                   seal_cy - (bb[3] - bb[1]) // 2 - bb[1]),
                  "三浦", font=seal_font, fill="#FFFFFF")
        gt.append({
            "name": "muhur", "label_name": "text_free",
            "bbox": [seal_cx - seal_sz // 2, seal_cy - seal_sz // 2,
                     seal_cx + seal_sz // 2, seal_cy + seal_sz // 2],
            "decorative": True, "required": False,
        })

        # ---- Kılıç silueti (çizim öğesi, metin yok) ----
        draw_sword(page, S(180), S(760), s)

        # ---- Diyalog balonları ----
        for i, (cx, cy, bw, bh, tail, lines) in enumerate(COVER_BUBBLES):
            x1, y1, x2, y2 = S(cx - bw // 2), S(cy - bh // 2), S(cx + bw // 2), S(cy + bh // 2)
            draw_bubble(draw, S(cx), S(cy), S(bw), S(bh), tuple(S(v) for v in tail))
            font = fit_dialogue_font(font_path, lines, S(bh))
            tx1, ty1, tx2, ty2 = vertical_columns(page, S(cx), S(cy), lines, font,
                                                  gap=S(26))
            gt.append({
                "name": f"balon_{i + 1}", "label_name": "text_bubble",
                "bbox": [tx1, ty1, tx2, ty2], "decorative": False,
                "required": True,
            })
    else:
        draw = ImageDraw.Draw(page)
        for c, (x1, x2) in enumerate(PANEL_COLS):
            for r, (y1, y2) in enumerate(PANEL_ROWS):
                draw.rectangle((S(x1) + ox, S(y1) + oy, S(x2) + ox, S(y2) + oy),
                               fill="#FFFFFF", outline="black", width=3)
                if c == 0 and r == 1:  # orta sol panelde tarama (B3'ün zemini)
                    draw_hatch(draw, S(x1 + 6) + ox, S(y1 + 6) + oy,
                               S(x2 - 6) + ox, S(y2 - 6) + oy)

        # ---- Konuşma balonları ----
        for i, (cx, cy, bw, bh, tail, lines) in enumerate(BUBBLES):
            tail_scaled = tuple(S(v) + (ox if i % 2 == 0 else oy)
                                for i, v in enumerate(tail))
            draw_bubble(draw, S(cx) + ox, S(cy) + oy, S(bw), S(bh), tail_scaled)
            font = fit_dialogue_font(font_path, lines, S(bh))
            tx1, ty1, tx2, ty2 = vertical_columns(
                page, S(cx) + ox, S(cy) + oy, lines, font, gap=S(26))
            gt.append({
                "name": f"balon_{i + 1}", "label_name": "text_bubble",
                "bbox": [tx1, ty1, tx2, ty2], "decorative": False,
                "required": True,
            })

        # ---- Anlatım kutusu (text_free): dikdörtgen + dikey metin ----
        cap_x1, cap_y1, cap_x2, cap_y2 = S(470) + ox, S(830) + oy, S(620) + ox, S(1020) + oy
        draw.rectangle((cap_x1, cap_y1, cap_x2, cap_y2), fill="#FFFFFF",
                       outline="black", width=3)
        cap_lines = ["翌朝、", "物語が", "始まる"]
        cap_font = ImageFont.truetype(font_path, S(24))
        tx1, ty1, tx2, ty2 = vertical_columns(
            page, (cap_x1 + cap_x2) // 2, (cap_y1 + cap_y2) // 2,
            cap_lines, cap_font, gap=S(20))
        gt.append({
            "name": "anlatim_kutusu", "label_name": "text_free",
            "bbox": [tx1, ty1, tx2, ty2], "decorative": False, "required": False,
        })

        # ---- Efekt yazısı (SFX, text_free): yatay + hafif döndürülmüş ----
        sfx = "ドキドキ"
        tmp = Image.new("RGBA", (S(420), S(140)), (0, 0, 0, 0))
        tdraw = ImageDraw.Draw(tmp)
        sfx_font = ImageFont.truetype(font_path, S(46))
        bb = tdraw.textbbox((0, 0), sfx, font=sfx_font)
        tdraw.text(((S(420) - (bb[2] - bb[0])) // 2 - bb[0],
                    (S(140) - (bb[3] - bb[1])) // 2 - bb[1]),
                   sfx, font=sfx_font, fill="black",
                   stroke_width=2, stroke_fill="black")
        tmp = tmp.rotate(-12, expand=True, resample=Image.BICUBIC)
        sx, sy = S(320) + ox, S(1015) + oy
        page.paste(tmp, (int(sx - tmp.width / 2), int(sy - tmp.height / 2)), tmp)
        gt.append({
            "name": "sfx", "label_name": "text_free",
            "bbox": [int(sx - tmp.width / 2), int(sy - tmp.height / 2),
                     int(sx + tmp.width / 2), int(sy + tmp.height / 2)],
            "decorative": False, "required": False,
        })

    return page, gt


def main() -> int:
    p = argparse.ArgumentParser(
        description="Sahte manga test sayfası üretici",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("out", nargs="?", default=None, help="Çıktı görsel yolu")
    p.add_argument("--size", default="800x1130", metavar="WxH",
                   help="Sayfa boyutu (dikey 800x1130, kare 1024x1024, yatay 1600x900 vb.)")
    p.add_argument("--cover", action="store_true",
                   help="Kapak benzeri düzen (başlık bandı + mühür + diyalog)")
    p.add_argument("--gt-json", metavar="YOL", default=None,
                   help="Gerçek konum (ground truth) bbox'larını JSON olarak yaz")
    args = p.parse_args()

    out_path = Path(args.out) if args.out else OUT_DEFAULT
    size = parse_size(args.size)
    font_path = find_jp_font()
    if font_path is None:
        print("Uyarı: Japonca yazı tipi bulunamadı; metinler çizilemeyecek.")
        return 1

    page, gt = render_page(size, args.cover, font_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    page.save(out_path)
    print(f"Test görseli oluşturuldu: {out_path} ({size[0]}x{size[1]}, "
          f"{'kapak' if args.cover else 'sayfa'} düzeni)")

    if args.gt_json:
        gt_path = Path(args.gt_json)
        gt_path.parent.mkdir(parents=True, exist_ok=True)
        gt_path.write_text(
            json.dumps({"size": list(size), "items": gt}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"GT bbox'ları yazıldı: {gt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
