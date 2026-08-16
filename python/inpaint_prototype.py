"""PS Editor - Inpainting prototipi (adım 3).

OCR prototipinin (adim 2) --json ciktisini girdi olarak alir; her metin
bolgesindeki orijinal metni siler ve arka plani (balon dolgusu, screen tone,
cizgiler, golgeler) bozulmadan tamamlar.

Iki yontem desteklenir:
  a) opencv : cv2.inpaint (Telea / Navier-Stokes) - hizli, model gerektirmez,
              ancak dokulu zeminlerde bulanik leke birakabilir.
  b) lama   : LaMa big-lama modeli (simple-lama-inpainting paketi,
              Apache-2.0; checkpoint TorchScript olarak ilk calistirmada
              GitHub release'den iner ~206 MB). Kalite olarak ustundur.

Maske stratejisi:
  1. Metin bbox'lari birlesimi -> ikili maske
  2. Ellipse kernel ile --dilate px genisletme (glif kenarlarindaki
     anti-aliasing kirintilarini yakalamak icin)
  3. Balon korumasi (bubble guard): maske, detektorun "bubble" sinifindan
     (label 0) buldugu balon kutusunun ic bolgesiyle kirpilir (kenardan
     --bubble-margin oraninda iceride). Boylece maske balonun kendi
     cizgisine/dolgusuna tasmaz. Balon bilgisi JSON'da "bubbles" anahtarinda
     varsa oradan, yoksa ayni detektorle (hizli, ~200 ms CPU) yerinde
     tespit edilir; --no-bubble-guard ile tamamen kapatilabilir.
  4. Feathering varsayilan olarak KAPALI: LaMa sert maskelerle egitildi;
     yumusatilmis maske gri hale birakir. --feather deneysel bayraktir.

Kullanim:
  python inpaint_prototype.py <gorsel> --regions <ocr.json> [secenekler]
  python inpaint_prototype.py <gorsel> --detect [secenekler]

Ornekler:
  python ocr_prototype.py test_data\\manga_test.png --json > ocr.json
  python inpaint_prototype.py test_data\\manga_test.png --regions ocr.json \\
      --method both
  python inpaint_prototype.py test_data\\manga_test.png --regions ocr.json \\
      --method opencv --cv2-method ns
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

CLASS_NAMES = {0: "bubble", 1: "text_bubble", 2: "text_free"}

LAMA_PACKAGE = "simple-lama-inpainting (Apache-2.0, PyPI)"
LAMA_CHECKPOINT_URL = (
    "https://github.com/enesmsahin/simple-lama-inpainting/"
    "releases/download/v0.1.0/big-lama.pt"
)


def info(msg: str) -> None:
    print(msg, flush=True)


def ensure_utf8_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ---------------------------------------------------------------- girdi

def load_regions(json_path: Path) -> tuple[list[dict], list[dict]]:
    """OCR --json ciktisini okur; (metin bolgeleri, balonlar) dondurur.

    JSON ya ocr_prototype'in tam payload'i olabilir ({"regions": [...],
    "bubbles": [...]}) ya da dogrudan bolge listesi. bbox list olarak
    normalize edilir; "label" yoksa metin varsayilir.

    Not: PowerShell '>' redirekti OCR loglarini da dosyaya yazar; bu yuzden
    dosyanin tamami gecerli JSON degilse ilk '{' ile baslayan satirdan
    sonunu ayiklariz.
    """
    text = json_path.read_text(encoding="utf-8-sig")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = None
        for i, line in enumerate(text.splitlines()):
            if line.lstrip().startswith("{"):
                start = i
                break
        if start is None:
            raise
        data = json.loads("\n".join(text.splitlines()[start:]))
    if isinstance(data, dict):
        raw_regions = data.get("regions", [])
        raw_bubbles = data.get("bubbles", [])
    else:
        raw_regions, raw_bubbles = data, []

    def normalize(items: list) -> list[dict]:
        out = []
        for it in items:
            if not isinstance(it, dict) or "bbox" not in it:
                continue
            x1, y1, x2, y2 = (int(v) for v in it["bbox"])
            out.append({
                "bbox": (x1, y1, x2, y2),
                "label": int(it.get("label", 1)),
                "label_name": it.get(
                    "label_name", CLASS_NAMES.get(int(it.get("label", 1)), "?"),
                ),
                "score": float(it.get("score", 0.0)),
                "text": it.get("text", ""),
            })
        return out

    return normalize(raw_regions), normalize(raw_bubbles)


def detect_inline(image: Image.Image, conf: float) -> tuple[list[dict], list[dict]]:
    """Balon korumasi icin ayni detektoru CPU'da calistirir (~200 ms)."""
    from ocr_prototype import ComicTextDetector

    det = ComicTextDetector(conf=conf)
    t0 = time.perf_counter()
    detections = det.detect(image)
    info(f"Balon tespiti (yerinde): {len(detections)} bolge "
         f"({(time.perf_counter() - t0) * 1000:.0f} ms)")
    texts = [d for d in detections if d["label"] in (1, 2)]
    bubbles = [d for d in detections if d["label"] == 0]
    return texts, bubbles


# ---------------------------------------------------------------- maske

def build_text_mask(w: int, h: int, regions: list[dict]) -> np.ndarray:
    """Metin bbox'lari birlesiminden ikili maske (uint8, 0/255)."""
    mask = np.zeros((h, w), dtype=np.uint8)
    for r in regions:
        x1, y1, x2, y2 = r["bbox"]
        cv2.rectangle(mask, (x1, y1), (x2, y2), 255, -1)
    return mask


def dilate_mask(mask: np.ndarray, px: int) -> np.ndarray:
    if px <= 0:
        return mask
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * px + 1, 2 * px + 1))
    return cv2.dilate(mask, k)


def _line_run_stats(bw: int, bh: int, rw: int,
                    cw: int, ch: int, area: int,
                    min_run: float = 0.35,
                    max_spec_area: int = 50,
                    max_spec_dim: int = 12) -> str:
    """Band bileseni icin siniflandirma: 'line' / 'spec' / 'other'.

    Cizgi (line): balon cevresi boyunca uzanan kosu. Esik, kutu boyutuna
    degil BANDIN GORULEN UZUNLUGUNA (exposure = max(bw,bh) - 2*rw) gore
    kurulur: dikdortgen balonun SOL/SAG kenar cizgisi, band duzeyinde en
    fazla bh-2rw kadar gorunur; tam balon genisligine esitlenen bir esik
    (eski max(bw,bh)*min_run) bu parcalari 'cizgi degil' sayip junk'a
    atiyordu ve cizgiyi yiyordu (or. 14x85 yan segment).

    Spek (spec): line olmayan ve cok kucuk (alan < 50, en buyuk dim <= 12)
    izole koyu nokta/kirinti — damga kosesindeki karartilar gibi. Bunlar
    gercek cizginin disinda kalir ve maske edilmelidir (junk).

    Other: ne cizgi ne spek; maske edilmez, korunur.
    """
    exposure = max(bw, bh) - 2 * rw
    if max(cw, ch) >= max(exposure, 1) * min_run and area >= 30:
        return "line"
    if area < max_spec_area and max(cw, ch) <= max_spec_dim:
        return "spec"
    return "other"


def build_bubble_junk_mask(img_rgb: np.ndarray,
                           bubbles: list[dict],
                           ring_frac: float = 0.10,
                           ink_thr: int = 170,
                           min_run: float = 0.35) -> np.ndarray | None:
    """Ring bandindaki cizgi-olmayan KOYU SPEKLER (maske turu).

    build_bubble_outline_mask'in tamamlayicisi: cizgi bandinda kalan ve
    gercek cizginin hicbir parcasina ait olmayan kucuk koyu kirintilar
    (kose karartisi, anti-aliasing artigi, yuk tasan glif parcalari).
    Bunlar hicbir metin bolgesi tarafindan maskelenmedigi icin goruntude
    koyu kalinti olarak kalir (or. dekoratif damga kutusunun kosesindeki
    karartilar). Bu fonksiyon yalnizca 'spec' sinifini maske adayi yapar;
    cizgi parcalari ve belirsiz icerik (other) KORUNUR.
    """
    if not bubbles:
        return None
    gray = img_rgb.mean(axis=2)
    h, w = gray.shape
    out = np.zeros((h, w), dtype=np.uint8)
    for b in bubbles:
        x1, y1, x2, y2 = b["bbox"]
        bw, bh = x2 - x1, y2 - y1
        if bw <= 0 or bh <= 0:
            continue
        rw = adaptive_ring_width(bw, bh, ring_frac)
        band = np.zeros((h, w), dtype=np.uint8)
        cv2.rectangle(band, (x1, y1), (x2, y2), 255, -1)
        cv2.rectangle(band, (x1 + rw, y1 + rw), (x2 - rw, y2 - rw), 0, -1)
        ink = ((gray < ink_thr) & (band > 0)).astype(np.uint8)
        n, labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
        for i in range(1, n):
            bx, by, cw, ch, area = stats[i]
            if _line_run_stats(bw, bh, rw, cw, ch, area,
                               min_run=min_run) == "spec":
                out[labels == i] = 255
    return out


def build_bubble_outline_mask(img_rgb: np.ndarray,
                              bubbles: list[dict],
                              ring_frac: float = 0.10,
                              ink_thr: int = 170,
                              min_run: float = 0.35,
                              dilate_px: int = 3) -> np.ndarray | None:
    """Balon cizgisinin GERCEK piksel konumu (tahmini band degil).

    Sorunun kok nedeni: 'cizgi bandini' korusun diye tum ringi (0..rw)
    maske disinda birakmak, glyf piksellerini de (cizgi 4-14px'teyken
    14-21px'te kalan) koruyup temizlikten kaciriyordu. Bu fonksiyon:
      - Her balonun ring bandindaki (kenardan 0..rw) koyu pikselleri bulur,
      - 8-bagiantili bilesenlerden balon cevresinin onemli bir bolumunu
        kaplayan UZUN kosular (cizgi) kalir; kisa glif kirintilari elenir,
      - Sonuc 3px kadar genisletilir (maske ayiklamasi payi).
    Cikti: cizgi pikselleri maske (uint8). Bubbles yoksa None.
    """
    if not bubbles:
        return None
    gray = img_rgb.mean(axis=2)
    h, w = gray.shape
    out = np.zeros((h, w), dtype=np.uint8)
    for b in bubbles:
        x1, y1, x2, y2 = b["bbox"]
        bw, bh = x2 - x1, y2 - y1
        if bw <= 0 or bh <= 0:
            continue
        rw = adaptive_ring_width(bw, bh, ring_frac)
        band = np.zeros((h, w), dtype=np.uint8)
        cv2.rectangle(band, (x1, y1), (x2, y2), 255, -1)
        cv2.rectangle(band, (x1 + rw, y1 + rw), (x2 - rw, y2 - rw), 0, -1)
        ink = ((gray < ink_thr) & (band > 0)).astype(np.uint8)
        n, labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
        for i in range(1, n):
            bx, by, cw, ch, area = stats[i]
            # balon cevresi boyunca uzanan kosu: gercek cizgi
            if _line_run_stats(bw, bh, rw, cw, ch, area,
                               min_run=min_run) == "line":
                out[labels == i] = 255
    if dilate_px > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                      (2 * dilate_px + 1,) * 2)
        out = cv2.dilate(out, k)
    return out


def _rect_inter(a: tuple[int, int, int, int],
                b: tuple[int, int, int, int]) -> int:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    w = max(0, min(ax2, bx2) - max(ax1, bx1))
    h = max(0, min(ay2, by2) - max(ay1, by1))
    return w * h


def adaptive_ring_width(bw: int, bh: int, ring_frac: float = 0.10) -> int:
    """Balon kutusuna gore olceklenir cizgi koruma bandi (px).

    Balon cizgisi, detektor kutusunun icinde kutu boyutuyla degisen bir
    mesafede durur (olcum: 700x1080 kapakta cizgi kutu kenarindan 4-14 px
    iceride; kutunun kendisi cizginin disina da pay birakir). Sabit piksel
    ring (or. 10 px) farkli olceklerde cizginin bir kismini koruyamaz ve
    maske cizgiyi yiyerek 'kesik kesik' gorunum yaratir. Bu yuzden ring,
    balonun kisa kenarina oranla hesaplanir:
        ring = clamp(ceil(min_side * 0.10), 8, 32)
    700x1080 kapak (min_side ~200)  -> 20 px  (olculen ihtiyac ~14+3)
    1024 kare (min_side ~250)       -> 25 px
    cok buyuk balonlar              -> 32 px ust sinir
    kucuk balonlar                  -> 8 px alt sinir
    """
    if bw <= 0 or bh <= 0:
        return 8
    px = int(np.ceil(min(bw, bh) * ring_frac))
    return max(8, min(32, px))


def apply_bubble_guard(w: int, h: int, bubbles: list[dict],
                       margin_frac: float,
                       regions: list[dict],
                       dilate: int = 0,
                       ring_width: int = 0,
                       inside_frac: float = 0.5,
                       outline: np.ndarray | None = None,
                       junk_mask: np.ndarray | None = None) -> np.ndarray:
    """Balon cizgisini korurken metni tamamen maskeleyen bolge-bazli guard.

    Her balon kutusu kenarlarindan min(min_side * margin_frac, min_px)
    kadar iceride 'ic bolge' hesaplanir; ic bolgenin disinda kalan
    'cizgi bandi' balonun gorunen kenar cizgisini barindirir. Band
    genisligi sabit degildir: ring_width > 0 verilirse o deger, verilmezse
    her balon icin kutu boyutuna orantili olarak hesaplanir
    (adaptive_ring_width). Band, ic bolgenin disinda KALIR (ic bolge
    disindan degil iceriden sinirlandigi icin cakisma yok).

    Bolge bazli karar:
      - Metin bolgesi AGIRLIKLI olarak bir balonun icindeyse
        (kesisim / alan >= inside_frac): maskesi ic bolgeyle sinirlanir,
        ardindan orijinal bbox'i geri eklenir (glif kaybi olmaz) — ANCAK
        geri eklenen kisim cizgiden arindirilir. 'outline' verilirse
        yalnizca GERCEK cizgi pikselleri (build_bubble_outline_mask)
        korunur; verilmezse tum ring bandi (0..rw) korunur (eski davranis).
      - Balon sinirlarini asan serbest metin (SFX vb.): maskesi yalnizca
        cizgi piksellerinden arindirilir; balon icinde ve disinda
        tamamen maskelenebilir -> balon ustune binen SFX bile silinir.

    Onemli: islem global degil, bolge bazli birlesimdir.
    """
    if not bubbles or margin_frac <= 0:
        return build_text_mask(w, h, regions)

    inner_boxes, rings = [], []
    for b in bubbles:
        x1, y1, x2, y2 = b["bbox"]
        bw, bh = x2 - x1, y2 - y1
        rw = adaptive_ring_width(bw, bh) if ring_width <= 0 else ring_width
        # Ic bolge, cizgi bandinin IC kenarindan baslar: band cizgiyi
        # barindirir, ic bolge asla bandin icine giremez.
        m = max(rw, int(min(bw, bh) * margin_frac))
        gx1, gy1 = min(w - 1, max(0, x1 + m)), min(h - 1, max(0, y1 + m))
        gx2, gy2 = max(gx1, min(w - 1, x2 - m)), max(gy1, min(h - 1, y2 - m))
        inner_boxes.append((gx1, gy1, gx2, gy2))
        ring = np.zeros((h, w), dtype=np.uint8)
        cv2.rectangle(ring, (x1, y1), (x2, y2), 255, -1)
        cv2.rectangle(ring, (x1 + rw, y1 + rw),
                      (x2 - rw, y2 - rw), 0, -1)
        rings.append(ring)

    out = np.zeros((h, w), dtype=np.uint8)
    for r in regions:
        rb = r["bbox"]
        rb_area = (rb[2] - rb[0]) * (rb[3] - rb[1])
        if rb_area <= 0:
            continue

        best_i, best_inter = -1, 0
        for i, b in enumerate(bubbles):
            inter = _rect_inter(rb, b["bbox"])
            if inter > best_inter:
                best_i, best_inter = i, inter

        # Bolgenin kendi maskesi: bbox + dilate
        rmask = np.zeros((h, w), dtype=np.uint8)
        cv2.rectangle(rmask, (rb[0], rb[1]), (rb[2], rb[3]), 255, -1)
        if dilate > 0:
            rmask = dilate_mask(rmask, dilate)

        if best_i >= 0 and best_inter / rb_area >= inside_frac:
            # Balon icindeki metin: ic bolge + orijinal bbox. Geri eklenen
            # bbox, cizgiden arindirilir: uzun metin kutularinin balon
            # cizgisine tasmasi durumunda maske cizgiyi yemez
            # (kesik/gorunmez cizgi hatasi).
            ib = inner_boxes[best_i]
            inner = np.zeros((h, w), dtype=np.uint8)
            cv2.rectangle(inner, (ib[0], ib[1]), (ib[2], ib[3]), 255, -1)
            rmask = cv2.bitwise_and(rmask, inner)
            cv2.rectangle(rmask, (rb[0], rb[1]), (rb[2], rb[3]), 255, -1)
            if outline is not None:
                rmask = cv2.bitwise_and(rmask, cv2.bitwise_not(outline))
            else:
                rmask = cv2.bitwise_and(rmask, cv2.bitwise_not(rings[best_i]))
        else:
            # Serbest metin: yalnizca cizgi pikselleri korunur
            if outline is not None:
                rmask = cv2.bitwise_and(rmask, cv2.bitwise_not(outline))
            else:
                for ring in rings:
                    rmask = cv2.bitwise_and(rmask, cv2.bitwise_not(ring))

        out = cv2.bitwise_or(out, rmask)

    # Hiyerarsik balonlar (kucuk balon buyuk balonun cizgi bandi icinde):
    # HICBIR balonun cizgisine maske tasamaz. Bagdastirici: outline yoksa
    # bandlarin birlesimi (eski davranis), varsa gercek cizgi pikselleri.
    if outline is not None:
        out = cv2.bitwise_and(out, cv2.bitwise_not(outline))
    else:
        union_rings = np.zeros((h, w), dtype=np.uint8)
        for ring in rings:
            union_rings = cv2.bitwise_or(union_rings, ring)
        out = cv2.bitwise_and(out, cv2.bitwise_not(union_rings))

    # Banddaki cizgi-olmayan kirintilar maske adayidir: hicbir metin
    # bolgesi kapsamadigi icin bunlar bastan beri goruntude kalirdi.
    if junk_mask is not None:
        out = cv2.bitwise_or(out, junk_mask)
    # Son adim her zaman cizgi korumasidir: junk bile cizgiden (dilate
    # payi dahil) arindirilir — cizgi asla maskelenmez.
    if outline is not None:
        out = cv2.bitwise_and(out, cv2.bitwise_not(outline))
    elif junk_mask is None:
        union_rings = np.zeros((h, w), dtype=np.uint8)
        for ring in rings:
            union_rings = cv2.bitwise_or(union_rings, ring)
        out = cv2.bitwise_and(out, cv2.bitwise_not(union_rings))
    return out


def feather_mask(mask: np.ndarray, sigma: int) -> np.ndarray:
    if sigma <= 0:
        return mask
    return cv2.GaussianBlur(mask, (0, 0), sigma)


def refine_remnants(img_rgb: np.ndarray, mask: np.ndarray,
                    ink_threshold: int = 180,
                    min_component: int = 20) -> np.ndarray:
    """Maske icinde kalan koyu kalintilari temizler (dokuya dokunmaz).

    LaMa/cv2 maske kenarlarinda glif artigi birakabilir. Bu gecis, maske
    icindeki koyu piksellerin bagli bilesenlerini bulur ve MINIMUM
    BUYUKLUKTEN buyuk bilesenleri (gercek leke/kalinti) cv2.inpaint ile
    kucuk delik olarak doldurur. Screen tone noktalari (2-8 px) esigin
    altinda kaldigi icin dokunulmaz; kalinti lekeleri (yuzlerce px) silinir.
    """
    gray = img_rgb.mean(axis=2)
    ink = ((gray < ink_threshold) & (mask > 0)).astype(np.uint8) * 255
    if not ink.any():
        return img_rgb

    n, labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    keep = np.zeros_like(ink)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= min_component:
            keep[labels == i] = 255
    if not keep.any():
        return img_rgb

    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    rem_mask = cv2.dilate(keep, k)
    # Maske disina tasma: maske disinda kalan pikseller (balon cizgisi,
    # kuyruk cizgisi vb.) asla degistirilmez.
    rem_mask = cv2.bitwise_and(rem_mask, mask)
    if not rem_mask.any():
        return img_rgb
    bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
    filled = cv2.inpaint(bgr, rem_mask, 2, cv2.INPAINT_TELEA)
    filled = cv2.cvtColor(filled, cv2.COLOR_BGR2RGB)
    out = img_rgb.copy()
    out[rem_mask > 0] = filled[rem_mask > 0]
    return out


# ---------------------------------------------------------------- yontemler

def inpaint_opencv(img_bgr: np.ndarray, mask: np.ndarray, method: str,
                   radius: int) -> np.ndarray:
    """cv2.inpaint (Telea / Navier-Stokes). BGR giris/cikis."""
    flags = cv2.INPAINT_NS if method == "ns" else cv2.INPAINT_TELEA
    return cv2.inpaint(img_bgr, mask, radius, flags)


def inpaint_lama(img_rgb: np.ndarray, mask: np.ndarray,
                 device: str, max_side: int,
                 model_override: str | None = None) -> np.ndarray:
    """LaMa big-lama (TorchScript) ile inpainting. RGB np dizisi dondurur.

    Model indirme: LAMA_MODEL ortam degiskeni yerel bir .pt yolu belirtirse
    o kullanilir (uretim/paketleme icin), yoksa GitHub release'den iner ve
    torch hub cache'inde (C:\\Users\\<kullanici>\\.cache\\torch\\hub) tutulur.
    """
    if device == "auto":
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"

    if model_override:
        os.environ["LAMA_MODEL"] = model_override
    else:
        os.environ.pop("LAMA_MODEL", None)

    from simple_lama_inpainting import SimpleLama  # yavas import, gec yukle

    h, w = img_rgb.shape[:2]
    factor = 1.0
    if max_side > 0:
        factor = min(1.0, max_side / max(h, w))
    if factor < 1.0:
        img_in = cv2.resize(img_rgb, (int(w * factor), int(h * factor)),
                            interpolation=cv2.INTER_AREA)
        mask_in = cv2.resize(mask, (int(w * factor), int(h * factor)),
                             interpolation=cv2.INTER_NEAREST)
        info(f"LaMa: giris {w}x{h} -> {img_in.shape[1]}x{img_in.shape[0]} "
             f"(--lama-max-side {max_side})")
    else:
        img_in, mask_in = img_rgb, mask

    t0 = time.perf_counter()
    model = SimpleLama(device=torch_device(device))
    info(f"LaMa modeli yuklendi ({(time.perf_counter() - t0):.1f} s, "
         f"cihaz: {model.device})")

    t0 = time.perf_counter()
    out_pil = model(Image.fromarray(img_in), Image.fromarray(mask_in))
    pred = np.asarray(out_pil.convert("RGB"))[:img_in.shape[0], :img_in.shape[1]]
    info(f"LaMa inference: {(time.perf_counter() - t0) * 1000:.0f} ms")

    if factor < 1.0:
        pred = cv2.resize(pred, (w, h), interpolation=cv2.INTER_LINEAR)

    # Sadece maske bolgesini model ciktisiyla degistir; gerisi orijinal.
    mask3 = (mask > 0)[..., None]
    return np.where(mask3, pred, img_rgb)


def torch_device(device: str):
    import torch
    if device == "cpu":
        return torch.device("cpu")
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


# ---------------------------------------------------------------- metrikler

def ink_coverage(img: np.ndarray, bbox: tuple[int, int, int, int]) -> float:
    """Bolge icinde 'murekkep' (koyu) piksel orani. Metin silinince dusmeli."""
    x1, y1, x2, y2 = bbox
    patch = img[y1:y2, x1:x2]
    if patch.size == 0:
        return 0.0
    gray = patch.mean(axis=2) if patch.ndim == 3 else patch
    return float(np.mean(gray < 120))


def bubble_border_ink(img: np.ndarray, bubble: dict) -> int:
    """Balon kutusu kenarina bitisik 2-8 px halkadaki koyu piksel sayisi."""
    x1, y1, x2, y2 = bubble["bbox"]
    h, w = img.shape[:2]
    in_b = np.zeros((h, w), dtype=np.uint8)
    cv2.rectangle(in_b, (x1, y1), (x2, y2), 255, -1)
    cv2.rectangle(in_b, (x1 + 8, y1 + 8), (x2 - 8, y2 - 8), 0, -1)
    ring = in_b > 0
    if not ring.any():
        return 0
    gray = img.mean(axis=2) if img.ndim == 3 else img
    return int(np.sum(gray[ring] < 120))


def region_clean_stats(img: np.ndarray, bbox: tuple) -> tuple[float, float]:
    """Metin bolgesindeki ortalama parlaklik ve std (temizlik olcutu)."""
    x1, y1, x2, y2 = bbox
    patch = img[y1:y2, x1:x2]
    if patch.size == 0:
        return 0.0, 0.0
    gray = patch.mean(axis=2) if patch.ndim == 3 else patch
    return float(gray.mean()), float(gray.std())


def report_metrics(orig: np.ndarray, result: np.ndarray,
                   regions: list[dict], bubbles: list[dict]) -> list[dict]:
    out = []
    for i, r in enumerate(regions):
        m = {
            "index": i,
            "bbox": list(r["bbox"]),
            "label_name": r["label_name"],
            "text": r.get("text", ""),
            "ink_before": round(ink_coverage(orig, r["bbox"]), 4),
            "ink_after": round(ink_coverage(result, r["bbox"]), 4),
            "clean_mean": round(region_clean_stats(result, r["bbox"])[0], 1),
            "clean_std": round(region_clean_stats(result, r["bbox"])[1], 2),
        }
        out.append(m)
    border_stats = []
    for i, b in enumerate(bubbles):
        border_stats.append({
            "index": i,
            "bbox": list(b["bbox"]),
            "dark_before": bubble_border_ink(orig, b),
            "dark_after": bubble_border_ink(result, b),
        })
    return out, border_stats


# ---------------------------------------------------------------- cikti

def make_compare(orig: Image.Image, mask: Image.Image, panels: dict[str, Image.Image],
                 out_path: Path, scale: float = 1.0) -> None:
    """Orijinal / maske / yontem sonuclari yan yana; ustlerinde etiket."""
    labels = [("Orijinal", orig)] + [("Maske", mask)] + [
        (name, img) for name, img in panels.items()
    ]
    w, h = orig.size
    pad, hdr = 4, 28
    total_w = sum(p[1].size[0] + pad for p in labels) + pad
    total_h = h + hdr + pad * 2
    canvas = Image.new("RGB", (total_w, total_h), (30, 30, 30))
    draw = ImageDraw.Draw(canvas)
    x = pad
    for name, img in labels:
        draw.rectangle((x, 0, x + img.size[0], hdr), fill=(10, 10, 10))
        draw.text((x + 6, 6), name, fill=(255, 255, 255))
        canvas.paste(img, (x, hdr + pad))
        x += img.size[0] + pad
    canvas.save(out_path)
    info(f"Karsilastirma kaydedildi: {out_path}")


def make_regions_compare(orig: Image.Image, panels: dict[str, Image.Image],
                         regions: list[dict], out_path: Path, pad: int = 24) -> None:
    """Her metin bolgesi icin bir satir: orijinal / her yontem (zoom)."""
    names = ["Orijinal"] + list(panels.keys())
    imgs = [orig] + list(panels.values())
    cell_w = max(im.width for im in imgs)
    row_h = max(im.height for im in imgs) + 34
    canvas = Image.new(
        "RGB", (cell_w * len(imgs) + 8, row_h * len(regions) + 8), (40, 40, 40))
    draw = ImageDraw.Draw(canvas)
    for ri, r in enumerate(regions):
        x1, y1, x2, y2 = r["bbox"]
        c = (max(0, x1 - pad), max(0, y1 - pad), min(orig.width, x2 + pad),
             min(orig.height, y2 + pad))
        for ci, im in enumerate(imgs):
            crop = im.crop(c)
            cx, cy = 4 + ci * cell_w, 4 + ri * row_h
            canvas.paste(crop.resize((cell_w, crop.height), Image.LANCZOS), (cx, cy + 30))
        label = f"#{ri + 1} [{r.get('label_name', '?')}] {r.get('text', '')[:18]}"
        draw.text((8, 4 + ri * row_h), label, fill=(255, 255, 255))
    canvas.save(out_path)
    info(f"Bolge detaylari kaydedildi: {out_path}")


def mask_preview(mask: np.ndarray, out_path: Path) -> None:
    Image.fromarray(mask).save(out_path)
    info(f"Maske kaydedildi: {out_path}")


# ---------------------------------------------------------------- CLI

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Manga metni temizleme prototipi: OpenCV vs LaMa",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("image", help="Manga sayfasi gorseli (JPG/PNG)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--regions", metavar="JSON",
                   help="ocr_prototype.py --json ciktisi (metin bbox'lari)")
    g.add_argument("--detect", action="store_true",
                   help="Bolge tespitini de yerinde yap (OCR'suz)")
    p.add_argument("--method", choices=["opencv", "lama", "both"],
                   default="lama", help="Inpainting yontemi")
    p.add_argument("--cv2-method", choices=["telea", "ns"], default="telea",
                   help="cv2.inpaint algoritmasi")
    p.add_argument("--cv2-radius", type=int, default=3,
                   help="cv2.inpaint tarama yaricapi")
    p.add_argument("--dilate", type=int, default=4,
                   help="Maske genisletme (px); glif kenar artiklarini yakalar")
    p.add_argument("--bubble-margin", type=float, default=0.08,
                   help="Balon kutusunun icerden kucultme orani (0-0.5); "
                        "balon cizgisini korur (Telea icin stabil maske saglar)")
    p.add_argument("--ring-width", type=int, default=0,
                   help="Korunacak balon cizgi bandi (px); 0 = balon kutu "
                        "boyutuna gore adaptif (varsayilan, min_side*%%10, "
                        "8-32 px). Cizgi kalinligi olcekle degisir, sabit "
                        "piksel degerler farkli olceklerde kesik cizgiye "
                        "yol acar")
    p.add_argument("--no-bubble-guard", action="store_true",
                   help="Balon kenar korumasini kapat (saf bbox+dilate)")
    p.add_argument("--feather", type=int, default=0,
                   help="Deneysel: maske kenarlarina Gaussian bulaniklik (px sigma)")
    p.add_argument("--no-refine-remnants", action="store_true",
                   help="Kalinti aritma gecisini kapat "
                        "(varsayilan: maske icindeki koyu kalintilar "
                        "doku korumali olarak temizlenir)")
    p.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto",
                   help="LaMa icin cihaz")
    p.add_argument("--lama-max-side", type=int, default=2048,
                   help="LaMa girisinin en uzun kenari (CPU hizi icin dusurun)")
    p.add_argument("--lama-model", metavar="YOL",
                   help="Yerel TorchScript big-lama.pt (indirme yerine)")
    p.add_argument("--conf", type=float, default=0.3,
                   help="Yerinde tespit guven esigi (--detect / balon icin)")
    p.add_argument("--add-bbox", action="append", metavar="x1,y1,x2,y2",
                   help="Detektorun kacirdigi bolgeyi elle ekle (tekrarlanabilir; "
                        "or. SFX)")
    p.add_argument("--out-dir", metavar="DIR", default=None,
                   help="Cikti dizini (varsayilan: gorselin yanindaki test_data)")
    p.add_argument("--json", action="store_true",
                   help="Sonuclari makine-okunur JSON olarak da bas")
    p.add_argument("--debug", action="store_true", help="Detayli log")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()
    args = parse_args(argv)

    image_path = Path(args.image)
    if not image_path.is_file():
        info(f"Hata: gorsel bulunamadi: {image_path}")
        return 2
    out_dir = Path(args.out_dir) if args.out_dir else image_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    orig_pil = Image.open(image_path).convert("RGB")
    w, h = orig_pil.size
    info(f"Gorsel: {image_path} ({w}x{h})")

    # ---- Bolgeler ----
    if args.regions:
        regions, bubbles_json = load_regions(Path(args.regions))
        info(f"Bolge girdisi: {len(regions)} metin, {len(bubbles_json)} balon")
    else:
        regions, bubbles_json = [], []
        info("--detect: bolge tespiti yerinde yapiliyor (OCR'suz)")

    manual = []
    for spec in args.add_bbox or []:
        try:
            manual.append({
                "bbox": tuple(int(v) for v in spec.split(",")),
                "label": 2, "label_name": "text_free", "score": 0.0,
                "text": "(manual)",
            })
        except ValueError:
            info(f"Hata: gecersiz --add-bbox: {spec}")
            return 2
    regions = regions + manual
    if manual:
        info(f"Manuel bolge eklendi: {len(manual)}")

    if not regions:
        info("Uyari: temizlenecek metin bolgesi yok. "
             "--regions / --detect / --add-bbox kontrol edin.")
        return 2

    # ---- Balon korumasi icin balon kutusu bul ----
    if args.no_bubble_guard:
        bubbles: list[dict] = []
        info("--no-bubble-guard: balon kenar korumasi kapali")
    else:
        bubbles = bubbles_json
        if not bubbles:
            _texts, bubbles = detect_inline(orig_pil, args.conf)
        else:
            info(f"Balon kutulari JSON'dan alindi ({len(bubbles)})")

    # ---- Maske ----
    t0 = time.perf_counter()
    if not args.no_bubble_guard and bubbles:
        mask = apply_bubble_guard(w, h, bubbles, args.bubble_margin, regions,
                                  dilate=args.dilate, ring_width=args.ring_width)
        ring_desc = (f"adaptif (balon boyutuna gore)"
                     if args.ring_width <= 0 else f"{args.ring_width}px")
        info(f"Balon korumasi uygulandi (margin %{args.bubble_margin * 100:.0f}, "
             f"ring {ring_desc})")
    else:
        mask = build_text_mask(w, h, regions)
        if args.dilate > 0:
            mask = dilate_mask(mask, args.dilate)
            info(f"Maske genisletildi: +{args.dilate} px")
    if args.feather > 0:
        mask = feather_mask(mask, args.feather)
        info(f"Maske yumusatildi (sigma={args.feather})")
    mask_time = (time.perf_counter() - t0) * 1000
    info(f"Maske: {int((mask > 0).sum())} px "
         f"({100 * (mask > 0).mean():.2f}% sayfa, {mask_time:.0f} ms)")

    if args.debug:
        for i, b in enumerate(bubbles):
            info(f"  balon[{i}] bbox={b['bbox']}")
        for i, r in enumerate(regions):
            info(f"  bolge[{i}] {r['label_name']} bbox={r['bbox']} "
                 f"text={r.get('text', '')[:20]!r}")

    mask_preview(mask, out_dir / f"{image_path.stem}_mask.png")

    # ---- Inpainting ----
    orig_np = np.asarray(orig_pil)
    orig_bgr = cv2.cvtColor(orig_np, cv2.COLOR_RGB2BGR)
    results: dict[str, Image.Image] = {}
    metrics: dict[str, list] = {}
    borders: dict[str, list] = {}
    timings: dict[str, float] = {}

    methods = ["opencv", "lama"] if args.method == "both" else [args.method]
    for method in methods:
        info(f"--- Yontem: {method} ---")
        t0 = time.perf_counter()
        if method == "opencv":
            res_bgr = inpaint_opencv(orig_bgr, mask, args.cv2_method,
                                     args.cv2_radius)
            res = cv2.cvtColor(res_bgr, cv2.COLOR_BGR2RGB)
        else:
            res = inpaint_lama(orig_np, mask, args.device, args.lama_max_side,
                               args.lama_model)
        if not args.no_refine_remnants:
            before = int((res.mean(axis=2) < 120)[mask > 0].sum())
            res = refine_remnants(res, mask)
            after = int((res.mean(axis=2) < 120)[mask > 0].sum())
            info(f"Kalinti aritma: maske icinde koyu piksel "
                 f"{before} -> {after}")
        timings[method] = (time.perf_counter() - t0) * 1000
        results[method] = Image.fromarray(res)
        out_png = out_dir / f"{image_path.stem}_inpaint_{method}.png"
        results[method].save(out_png)
        info(f"Kaydedildi: {out_png}  "
             f"({timings[method] / 1000:.1f} s dahil model yukleme)")

        rm, bs = report_metrics(orig_np, res, regions, bubbles)
        metrics[method], borders[method] = rm, bs
        for m in rm:
            info(f"  [{m['label_name']}] ink {m['ink_before']:.3f} -> "
                 f"{m['ink_after']:.3f}  (balon icinde temizlik "
                 f"mean={m['clean_mean']:.0f} std={m['clean_std']:.1f})")

    # ---- Karsilastirma gorselleri ----
    if len(methods) == 2:
        make_compare(orig_pil, Image.fromarray(mask), results,
                     out_dir / f"{image_path.stem}_compare.png")
        make_regions_compare(orig_pil, results, regions,
                             out_dir / f"{image_path.stem}_regions_compare.png")

    # ---- JSON ----
    if args.json:
        payload = {
            "image": str(image_path),
            "method": args.method,
            "mask": {
                "dilate": args.dilate,
                "bubble_guard": not args.no_bubble_guard and bool(bubbles),
                "bubble_margin": args.bubble_margin,
                "feather": args.feather,
                "covered_px": int((mask > 0).sum()),
                "covered_frac": round(float((mask > 0).mean()), 4),
            },
            "timings_ms": {k: round(v, 1) for k, v in timings.items()},
            "model": {
                "opencv": f"cv2.inpaint({args.cv2_method})",
                "lama": {"package": LAMA_PACKAGE,
                         "checkpoint": args.lama_model or LAMA_CHECKPOINT_URL,
                         "device": args.device},
            },
            "regions": [
                {
                    "bbox": list(r["bbox"]),
                    "label_name": r["label_name"],
                    "text": r.get("text", ""),
                    **({"opencv": metrics["opencv"][i],
                        "lama": metrics["lama"][i]}
                       if len(methods) == 2 else {methods[0]: metrics[methods[0]][i]}),
                }
                for i, r in enumerate(regions)
            ],
        }
        if borders:
            payload["bubble_borders"] = {
                m: bs for m, bs in borders.items()
            }
        print("")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
