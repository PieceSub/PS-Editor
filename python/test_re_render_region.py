"""Adim 7 dogrulama: bolge bazli yeniden render (re_render_region).

torch YUKLEMEZ; yalnizca PIL (+ cv2, istege bagli) - hizli calisir.

Kullanim (python/ dizininden):
    .venv\\Scripts\\python.exe test_re_render_region.py

Dogruladiklari:
  1) typeset_region geriye donuk uyumlu (style yoksa eski davranis), style
     varsayilanlari dogru.
  2) re_render_region yalnizca hedef bolgeyi degistirir; diger bolgeler
     piksel piksel ayni kalir.
  3) style parametreleri (renk / boyut / hizalama / agirlik) ete kana gecer:
     bolge icinde degisim var ve kullanilan stil dondurulur.
  4) bos ceviri = devre disi birakma: bolge temizlenmis gorsele doner.
  5) erase=inpaint yolu (kaynak temiz gorselin yoklugu) calisir.
  6) sidecar protokolu uzerinden re_render_region komutu uctan uca calisir.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
WORK = HERE / "test_data" / "_re_render_test"
CLEANED = HERE / "test_data" / "manga_test_cleaned.png"
OCR_JSON = HERE / "test_data" / "manga_test_ocr.json"

from inpaint_prototype import load_regions  # noqa: E402
from translate_typeset_prototype import (  # noqa: E402
    DEFAULT_FONT,
    REGION_STYLE_DEFAULTS,
    normalize_region_style,
    re_render_region,
    typeset_region,
)

MOCK_TEXTS = [
    "Hi there!",
    "That is quite a surprise!",
    "What should we do now?",
    "Let me think about it.",
    "Oh no!",
    "It is hard to believe what happened, but here we are.",
]

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "TAMAM" if ok else "HATA"
    print(f"  [{status}] {name}{(' - ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


def diff_box(a: Image.Image, b: Image.Image, box: tuple) -> int:
    """Iki gorsel arasinda kutudaki farkli piksel sayisi."""
    ca = a.crop(box).convert("RGB")
    cb = b.crop(box).convert("RGB")
    ba = ca.tobytes()
    bb = cb.tobytes()
    return sum(1 for x, y in zip(ba, bb) if x != y) // 3


def build_initial_canvas() -> Image.Image:
    """Temizlenmis gorseli alir, 6 bolgeye mock cevirileri typeset eder
    (otomatik akisin typeset adimini taklit eder, style'siz)."""
    regions, _ = load_regions(OCR_JSON)
    canvas = Image.open(CLEANED).convert("RGB")
    infos = {}
    for i, r in enumerate(regions):
        infos[i] = typeset_region(
            canvas, r["bbox"], MOCK_TEXTS[i], DEFAULT_FONT,
            min_size=9, max_size=36)
    return canvas, regions, infos


def main() -> int:
    if not CLEANED.is_file() or not OCR_JSON.is_file():
        print("HATA: test data eksik (manga_test_cleaned.png / manga_test_ocr.json).")
        print("Onceden: python ocr_prototype.py ... --json ve inpaint calistirin.")
        return 2
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)

    # ------------------------------------------------ 1) geriye donuk uyum
    print("1) typeset_region geriye donuk uyum + stil varsayilanlari")
    canvas, regions, infos = build_initial_canvas()
    r0 = regions[0]
    check("style'siz cagri basarili (eski imza)", True)
    st = normalize_region_style(None)
    check("varsayilan stil == REGION_STYLE_DEFAULTS", st == REGION_STYLE_DEFAULTS)
    st2 = normalize_region_style({
        "font_weight": "normal", "color": "#12ab34", "font_size_override": "abc",
        "align": "right"})
    check("stil dogrulama: gecerli alanlar alinir", st2["font_weight"] == "normal"
          and st2["color"] == "#12ab34" and st2["align"] == "right")
    check("stil dogrulama: gecersiz boyut varsayilana doner",
          st2["font_size_override"] is None)
    check("stil dogrulama: gecersiz renk varsayilana doner",
          normalize_region_style({"color": "kirmizi"})["color"] is None)

    out_path = WORK / "page_translated.png"
    canvas.save(out_path)
    before = Image.open(out_path)
    before.load()  # dosya sonra uzerine yazilir; pikselleri simdi oku

    # ------------------------------------------------ 2) diger bolgeler sabit
    print("2) re_render_region: yalnizca hedef bolge degisir")
    style = {"font_weight": "normal", "color": "#e05a8a",
             "font_size_override": 20, "align": "left"}
    res = re_render_region(
        cleaned_path=CLEANED, output_path=out_path, bbox=r0["bbox"],
        translation="New manual heading!", style=style, erase="paste")
    after = Image.open(out_path)

    box1 = tuple(r0["bbox"])
    box3 = tuple(regions[2]["bbox"])
    box5 = tuple(regions[5]["bbox"])
    check("hedef bolgede piksel degisimi var", diff_box(before, after, box1) > 0,
          f"{diff_box(before, after, box1)} px")
    check("uzak bolgede (3) piksel ayni", diff_box(before, after, box3) == 0)
    check("uzak bolgede (6) piksel ayni", diff_box(before, after, box5) == 0)
    check("donen font_size override ile ayni", res["font_size"] == 20)
    check("donen stil kullanilan stili bildirir",
          res["style_used"]["color"] == "#e05a8a"
          and res["style_used"]["font_weight"] == "normal"
          and res["style_used"]["align"] == "left")
    check("devre disi degil", res["disabled"] is False)
    check("translation geri donuyor", res["translation"] == "New manual heading!")

    # ------------------------------------------------ 3) otomatik boyut + renk
    print("3) renkli + otomatik boyut (font_size_override yok)")
    res2 = re_render_region(
        cleaned_path=CLEANED, output_path=out_path, bbox=box3,
        translation="Colored!", style={"color": "#2244ff"}, erase="paste")
    check("otomatik boyut > 0", (res2["font_size"] or 0) > 0)
    check("renk istendiginde kullanilan stil renk tutar",
          res2["style_used"]["color"] == "#2244ff")

    # ------------------------------------------------ 4) devre disi birakma
    print("4) bos ceviri = devre disi (yalnizca silme)")
    res3 = re_render_region(
        cleaned_path=CLEANED, output_path=out_path, bbox=box5,
        translation="   ", erase="paste")
    after2 = Image.open(out_path)
    cleaned = Image.open(CLEANED)
    box5_grown = (box5[0] - 12, box5[1] - 12, box5[2] + 12, box5[3] + 12)
    # Tekst bayagi silindiyse bolge ~ temizlenmis gorsel olmalidir
    # (kontur payi icin buyutulmus kutu; kenarlardaki hat marji nedeniyle %98).
    diff_clean = diff_box(after2, cleaned, box5_grown)
    total = (box5_grown[2] - box5_grown[0]) * (box5_grown[3] - box5_grown[1])
    check("devre disi bolge temizlenmis gorsele yaklasir",
          diff_clean < total * 0.02, f"{diff_clean}/{total} px fark")
    check("devre disi sonucu disabled=True", res3["disabled"] is True)

    # ------------------------------------------------ 5) erase=inpaint yolu
    print("5) erase=inpaint (temiz kaynak yokken yerel cv2)")
    inpaint_img = Image.open(CLEANED).convert("RGB")
    ip = WORK / "page_inpaint.png"
    inpaint_img.save(ip)  # kaynak gorsel hic yokmuş gibi: erase=inpaint
    res4 = re_render_region(
        cleaned_path=WORK / "yok.png", output_path=ip, bbox=box1,
        translation="Inpainted!", style={"color": "#ff2222"}, erase="inpaint")
    check("inpaint yolu basarili", res4["translation"] == "Inpainted!")

    # ------------------------------------------------ 6) sidecar protokolu
    print("6) sidecar uzerinden re_render_region (uçtan uca)")
    payload = {
        "output": str(out_path),
        "cleaned": str(CLEANED),
        "region": {
            "bbox": list(box3),
            "translation": "Via sidecar!",
            "erase": "paste",
            "style": {"font_weight": "bold", "color": "#00aa44",
                      "font_size_override": 26, "align": "center"},
        },
    }
    line = json.dumps({"id": 1, "cmd": "re_render_region", "payload": payload},
                       ensure_ascii=False)
    proc = subprocess.run(
        [sys.executable, str(HERE / "sidecar.py")],
        input=line + "\n" + json.dumps({"id": 2, "cmd": "shutdown", "payload": {}}) + "\n",
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, cwd=str(HERE),
    )
    responses = [json.loads(t) for t in proc.stdout.splitlines() if t.strip()]
    resp = next((r for r in responses if r.get("id") == 1), None)
    check("sidecar yanit ok=True", bool(resp and resp.get("ok")),
          proc.stderr.strip()[:200] if not (resp and resp.get("ok")) else "")
    check("sidecar bolge sonucu dondurdu",
          bool(resp and resp["ok"] and resp["result"]["translation"] == "Via sidecar!"))
    check("sidecar stili yansitti",
          bool(resp and resp["ok"]
               and resp["result"]["style_used"]["color"] == "#00aa44"))

    # ------------------------------------------------ ozet
    print("")
    if failures:
        print(f"SONUC: {len(failures)} dogrulama HATA:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("SONUC: tum dogrulamalar basarili.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())