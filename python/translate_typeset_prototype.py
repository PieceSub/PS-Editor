"""PS Editor - Ceviri + typesetting prototipi (adim 4).

OCR prototipinin (adim 2) --json ciktisini ve inpainting prototipinin (adim 3)
urettigi temizlenmis gorseli girdi olarak alir:

  1) Metin bolgelerini manga okuma sirasina dizer
     (dikey Japonca duzeni: sutunlar sagdan sola, sutun ici ustten alta)
  2) Tum sayfa diyalogunu TEK bir LLM istegine verir (sayfa baglami korunur:
     ton, diyalog akisi, karakter tutarliligi - adimli/balon balon degil)
  3) Cevrilen metni temizlenmis gorselin bbox alanina geri yerlestirir:
     otomatik font boyutu (ikili arama), kelime bazli satir kaydirma,
     balon icinde ortala, beyaz kontur + siyah dolgu (scanlation stili)

LLM saglayicisi soyutlanmistir (TranslationBackend + create_backend fabrikasi).
Yeni saglayici eklemek: sınıf yazip BACKEND_REGISTRY'ye kaydetmek yeterli.
Su an kayitli:
  - mock          : API anahtari gerektirmez; pipeline'i uctan uca test etmek
                    icin deterministik sahte ceviri uretir.
  - api           : openai kutuphanesiyle OpenAI ya da herhangi bir
                    OpenAI-uyumlu ucnokta (Anthropic dahil degil; ancak
                    OpenAI-uyumlu arayuz sunan Groq, Together, DeepSeek,
                    Ollama, LM Studio, vLLM vb. - BASE_URL + API_KEY ile).
  - local         : yerel Ollama (BASE_URL http://localhost:11434/v1).

Kullanim modlari (hedef 5-6. adimdaki ayar ekraniyla birebir ayni felsefe):
  - auto  : .env'de LLM_API_KEY varsa "api" moduna, yoksa "mock"a duser.
  - local : LLM_BASE_URL yoksa Ollama adresine varsayar; anahtar gerekmez.
  - api   : LLM_API_KEY + LLM_BASE_URL + LLM_MODEL (.env veya CLI argumanlari).

API anahtari / base_url / model, "resolve_credentials" adli TEK fonksiyondan
okunur (CLI argumani > .env > varsayilan). Ileride (FastAPI + Tauri
entegrasyonunda) anahtarlar Windows Credential Manager gibi guvenli bir
depoya tasinacak; o zaman yalnizca bu fonksiyonun giris kaynagi degisir,
donen imza ayni kalir.

API anahtari/model python/.env dosyasindan okunur (python-dotenv):
  LLM_PROVIDER=auto|local|api|mock
  LLM_API_KEY=sk-...
  LLM_BASE_URL=https://api.openai.com/v1
  LLM_MODEL=gpt-4.1-mini
  LLM_TEMPERATURE=0.2
  LLM_TIMEOUT=120

Kullanim:
  python ocr_prototype.py test_data\\manga_test.png --json > ocr.json
  python inpaint_prototype.py test_data\\manga_test.png --regions ocr.json --method lama
  python translate_typeset_prototype.py test_data\\manga_test.png \\
      --regions ocr.json --cleaned test_data\\manga_test_inpaint_lama.png

Ornekler:
  # API anahtari olmadan pipeline testi (deterministik mock ceviri):
  python translate_typeset_prototype.py test_data\\manga_test.png \\
      --regions ocr.json --cleaned test_data\\manga_test_inpaint_lama.png \\
      --provider mock

  # Gercek LLM (python/.env icinde LLM_API_KEY vb. dolu olmali):
  python translate_typeset_prototype.py test_data\\manga_test.png \\
      --regions ocr.json --cleaned test_data\\manga_test_inpaint_lama.png \\
      --provider openai_compat --context "Iki arkadas bir maceraya cikiyor"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

from PIL import Image, ImageDraw, ImageFont

# inpaint_prototype.load_regions, adim 2/3 ile AYNI JSON protokolunu okur
# (tek kaynak); bbox'lari tuple'a normalize eder ve "bubbles"i da ayiklar.
from inpaint_prototype import load_regions

DEFAULT_FONT = Path(__file__).resolve().parent / "fonts" / "ComicNeue-Bold.ttf"
DEFAULT_FONT_REGULAR = (
    Path(__file__).resolve().parent / "fonts" / "ComicNeue-Regular.ttf"
)

# Mock backend cümle havuzu (uzunluk araligina gore; indeksle deterministik)
MOCK_POOL = {
    "short": ["Hi there!", "Oh no!", "Wait...", "Really?!", "Wow.", "Hey!"],
    "medium": [
        "That is quite a surprise!",
        "I never expected this.",
        "What should we do now?",
        "Let me think about it.",
    ],
    "long": [
        "It is hard to believe what happened, but here we are.",
        "I cannot believe we managed to pull that off.",
        "Something incredible just happened, right in front of us.",
    ],
}

FALLBACK_TRANSLATION = "[untranslated]"


def info(msg: str) -> None:
    print(msg, flush=True)


def ensure_utf8_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ---------------------------------------------------------------- ceviri backendleri

@runtime_checkable
class TranslationBackend(Protocol):
    """Sayfa cevirisi soyutlamasi. Yeni saglayicilar bu protokolu saglar."""

    name: str
    model: str

    def translate_page(
        self,
        entries: list[dict],
        target_lang: str,
        context: str = "",
    ) -> dict[int, str]:
        """entries: [{"id": int, "text": str, "label_name": str}...] okuma sirasinda.

        Donduren: {id: ceviri_metni}. Eksik id -> cagiran taraf fallback koyar.
        """
        ...


class MockBackend:
    """API gerektirmeyen, deterministik test backendi.

    Girdinin uzunluguna gore uc kisa cümle havuzundan indeks tabanli (bölge
    sirasina bagli, dolayisiyla her calistirmada AYNI) bir ceviri uretir.
    Gercek ceviri degildir; pipeline dogrulamasi icindir.
    """

    name = "mock"
    model = "mock-v1"

    def translate_page(
        self,
        entries: list[dict],
        target_lang: str,
        context: str = "",
    ) -> dict[int, str]:
        out: dict[int, str] = {}
        for i, e in enumerate(entries):
            n = len(e.get("text", "").strip())
            if n <= 8:
                pool = MOCK_POOL["short"]
            elif n <= 20:
                pool = MOCK_POOL["medium"]
            else:
                pool = MOCK_POOL["long"]
            out[e["id"]] = pool[i % len(pool)]
        return out


class OpenAICompatBackend:
    """openai kutuphanesi uzerinden OpenAI-uyumlu ucnokta backend'i.

    LLM_API_KEY / LLM_BASE_URL / LLM_MODEL .env'den gelir. Yanit JSON
    yapilandirilmis istenir: {"translations": [{"id": .., "translation": ..}]}.
    Bazi ucnoktalar response_format desteklemez; o zaman yapisal prompt ile
    yine JSON istenir ve metinden sifirlanir (fenced code bloklari soyulur).
    """

    name = "openai_compat"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str = "gpt-4.1-mini",
        temperature: float = 0.2,
        timeout: float = 120.0,
    ):
        from openai import OpenAI

        kwargs: dict = {"api_key": api_key, "timeout": timeout}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = OpenAI(**kwargs)
        self.model = model
        self.temperature = temperature

    # -- prompt (komik-translate / COLING 2025 PBP yaklasimi: tum sayfa tek istek)
    @staticmethod
    def build_prompt(
        entries: list[dict], target_lang: str, context: str
    ) -> tuple[str, str]:
        lines = "\n".join(
            f'  {{"id": {e["id"]}, "text": {json.dumps(e["text"], ensure_ascii=False)}}}'
            for e in entries
        )
        ctx = context.strip() or "NONE (standalone test page)"
        user = (
            "Translate the following manga page dialogue from Japanese into "
            f"{target_lang}.\n\n"
            f"Series/context: {ctx}\n\n"
            "The bubbles below are listed in page reading order "
            "(right-to-left columns, top-to-bottom). Keep that dialogue flow, "
            "tone and register; translate naturally and idiomatically as a "
            "professional manga localizer would. Do not add explanations.\n\n"
            "Bubbles:\n"
            "[\n" + lines + "\n]\n\n"
            'Respond with ONLY valid JSON, no markdown, in exactly this shape: '
            '{"translations": [{"id": <same id>, '
            '"translation": "<translation text>"}]}. '
            "Include every id exactly once."
        )
        system = (
            "You are a professional manga localizer with 20 years of experience. "
            "You keep dialogue flow, tone and character voice consistent across "
            "the whole page. You prefer concise, natural lines that fit speech "
            "balloons. You keep onomatopoeia conventions (e.g. '…', '!'). "
            "You never add content that is not in the source, and you never "
            "add commentary outside the requested JSON."
        )
        return system, user

    def translate_page(
        self,
        entries: list[dict],
        target_lang: str,
        context: str = "",
    ) -> dict[int, str]:
        system, user = self.build_prompt(entries, target_lang, context)
        kwargs: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
        }
        # Bazi ucnoktalar response_format bilmez -> dene, duserse sifirla.
        try:
            kwargs["response_format"] = {"type": "json_object"}
            resp = self.client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            info(f"response_format desteklenmiyor, yapisal metin deneniyor "
                 f"({type(exc).__name__}).")
            kwargs.pop("response_format", None)
            resp = self.client.chat.completions.create(**kwargs)

        raw = resp.choices[0].message.content or ""
        data = parse_llm_json(raw)
        return map_translations(data, entries)


# ---------------------------------------------------------------- LLM yanit ayristirma

def strip_code_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:]
    return t.strip()


def parse_llm_json(raw: str) -> object:
    """LLM yanitini olabildigince saglam sekilde JSON'a cevirir."""
    t = strip_code_fence(raw)
    # ilk '{' ile son '}' arasini al (log kirliligi icin)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(t[start : end + 1])
            except json.JSONDecodeError:
                pass
    raise ValueError(f"LLM yaniti gecerli JSON degil: {raw[:200]!r}")


def map_translations(data: object, entries: list[dict]) -> dict[int, str]:
    """LLM JSON'unu {id: ceviri} sozlugune cevirir; 3 kabul edilen bicim:
    {"translations": [{"id","translation"}]}, [{...}], {"id": "ceviri"}."""
    out: dict[int, str] = {}

    def take(item: object) -> None:
        if not isinstance(item, dict):
            return
        if "translation" in item and "id" in item:
            out[int(item["id"])] = str(item["translation"]).strip()
            return
        for v in item.values():
            if isinstance(v, str):
                continue
            if isinstance(v, dict) and "translation" in v:
                out[int(item["id"])] = str(v["translation"]).strip()
                return

    if isinstance(data, dict) and "translations" in data and isinstance(
        data["translations"], list
    ):
        for it in data["translations"]:
            take(it)
    elif isinstance(data, list):
        for it in data:
            take(it)
    elif isinstance(data, dict):
        if all(isinstance(k, (int, str)) and isinstance(v, str) for k, v in data.items()):
            out = {int(k): v.strip() for k, v in data.items()}
        else:
            for k, v in data.items():
                if isinstance(v, dict) and "translation" in v:
                    out[int(k)] = str(v["translation"]).strip()
    return out


# ---------------------------------------------------------------- backend fabrikasi

DEFAULT_OLLAMA_URL = "http://localhost:11434/v1"
DEFAULT_OLLAMA_MODEL = "llama3.1"


def resolve_credentials(
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> tuple[str, str | None, str, float, float]:
    """API anahtari / base_url / model kaynagini TEK noktada toplar.

    Kaynak onceligi: CLI argumanlari > ortam degiskenleri > varsayilan.
    Ileride guvenli anahtar deposuna (Windows Credential Manager vb.)
    gecis yapilirsa yalnizca bu fonksiyon degisir.
    """
    return (
        api_key or os.environ.get("LLM_API_KEY", "").strip(),
        base_url or os.environ.get("LLM_BASE_URL") or None,
        model or os.environ.get("LLM_MODEL", "gpt-4.1-mini"),
        float(os.environ.get("LLM_TEMPERATURE", "0.2")),
        float(os.environ.get("LLM_TIMEOUT", "120")),
    )


def create_backend(
    provider: str,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    timeout: float | None = None,
) -> TranslationBackend:
    """Provider adindan backend ornegi uretir (kayit defteri tabanli).

    Yeni saglayici: bu dosyaya sınıf ekle, asagidaki sozluge kaydet.
    Anthropic CLI prototipinde test edilemedigi icin su an kayitli degil;
    eklenince "api" modunun yanina "anthropic" modu da CLI'a baglanir.
    """
    if provider == "mock":
        return MockBackend()

    if provider in ("api", "openai_compat", "local"):
        key, url, mdl, temp, to = resolve_credentials(api_key, base_url, model)
        if provider == "local":
            url = url or DEFAULT_OLLAMA_URL
            mdl = model or DEFAULT_OLLAMA_MODEL
            key = api_key or ""  # Ollama anahtar istemez
        return OpenAICompatBackend(
            api_key=key,
            base_url=url,
            model=mdl,
            temperature=temperature if temperature is not None else temp,
            timeout=timeout if timeout is not None else to,
        )

    raise ValueError(f"bilinmeyen provider: {provider}")


# ---------------------------------------------------------------- manga okuma sirasi

def _x_overlap_frac(a: tuple, b: tuple) -> float:
    """Iki bbox x-ekseninde ne oranda ortusuyor (0..1, kucuk olana gore)."""
    x1 = max(a[0], b[0])
    x2 = min(a[2], b[2])
    if x2 <= x1:
        return 0.0
    ov = x2 - x1
    return ov / max(1, min(a[2] - a[0], b[2] - b[0]))


def manga_reading_order(regions: list[dict]) -> list[dict]:
    """Bolgeleri manga okuma sirasina dizer: sutunlar SAGDAN SOLA, sutun ici
    USTTEN ALTA.

    Sezgisel: bir bolgenin x-araligi baska bir bolgeninkiyle buyuk olcude
    ortusuyorsa ayni sutundadir (dikey Japonca metin bolgeleri uzun ve dardir,
    sutunun tum yuksekligine yayilir). Sutunlar x-merkezine gore azalan
    (sagdan sola) siralanir. Bu, "bbox x-koordinatlarina gore sagdan sola"
    basit kuralinin sutun-karisik sayfalar icin daha saglam hali.

    Not: OCR icerisindeki (bolge ici) dikey sutun sirasi manga-ocr tarafindan
    manga konvansiyonuna uygun (sagdan sola) uretilir; bu fonksiyon yalnizca
    BOLGELER arasi sayfa sirasini duzeltir.
    """
    if len(regions) <= 1:
        return list(regions)

    cols: list[list[dict]] = []
    for r in sorted(regions, key=lambda d: d["bbox"][1]):
        placed = False
        for col in cols:
            anchor = col[0]["bbox"]
            if _x_overlap_frac(anchor, r["bbox"]) >= 0.5:
                col.append(r)
                placed = True
                break
        if not placed:
            cols.append([r])

    for col in cols:
        col.sort(key=lambda d: (d["bbox"][1], d["bbox"][0]))

    cols.sort(
        key=lambda col: sum(d["bbox"][0] + d["bbox"][2] for d in col) / (2 * len(col)),
        reverse=True,
    )
    return [r for col in cols for r in col]


# ---------------------------------------------------------------- typesetting

def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Kelime bazli acgozlu satir kaydirma (CSS word-break benzeri).

    Tek kelime sıgmazsa karakter bazli boler (cunku LLM bazen bosluksuz
    metin de dondurabilir). max_width 0'dan kucukse tek satir dondur.
    """
    if max_width <= 0:
        return [text]
    lines: list[str] = []
    for raw_line in text.split("\n"):
        words = raw_line.split()
        if not words:
            lines.append("")
            continue
        cur = words[0]
        for w in words[1:]:
            if font.getlength(cur + " " + w) <= max_width:
                cur += " " + w
            else:
                # kur satıra sıgmayan kelimeyi karakter bazinda bol
                if font.getlength(cur) > max_width:
                    chunks = _char_break(cur, font, max_width)
                    lines.extend(chunks[:-1])
                    cur = chunks[-1] if chunks else ""
                lines.append(cur)
                cur = w
        if font.getlength(cur) > max_width:
            chunks = _char_break(cur, font, max_width)
            lines.extend(chunks[:-1])
            cur = chunks[-1] if chunks else ""
        lines.append(cur)
    return lines


def _char_break(word: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    chunks: list[str] = []
    cur = ""
    for ch in word:
        if font.getlength(cur + ch) > max_width and cur:
            chunks.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        chunks.append(cur)
    return chunks


def line_height(font: ImageFont.FreeTypeFont, spacing: float = 1.08) -> int:
    ascent, descent = font.getmetrics()
    return int((ascent + descent) * spacing)


def fit_font_size(
    text: str,
    font_path: str | Path,
    inner_w: int,
    inner_h: int,
    min_size: int,
    max_size: int,
) -> tuple[int, list[str], int]:
    """Icine sigan EN BUYUK font boyutunu ikili aramayla bulur.

    Dondurur: (font_size, wrapped_lines, line_h). Sıgmazsa min_size'de doner;
    cagiran overflow'u kendisi raporlar (typeset yine de yapilir).
    """
    if max_size < min_size:
        max_size = min_size

    def fits(size: int) -> bool:
        font = ImageFont.truetype(str(font_path), size)
        lines = wrap_text(text, font, inner_w)
        return len(lines) * line_height(font) <= inner_h

    lo, hi = min_size, max_size
    if fits(hi):
        size = hi
    elif not fits(lo):
        size = lo
    else:
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if fits(mid):
                lo = mid
            else:
                hi = mid
        size = lo

    font = ImageFont.truetype(str(font_path), size)
    lines = wrap_text(text, font, inner_w)
    return size, lines, line_height(font)


def typeset_region(
    img: Image.Image,
    bbox: tuple[int, int, int, int],
    text: str,
    font_path: str | Path,
    min_size: int,
    max_size: int,
    margin_x_frac: float = 0.06,
    margin_y_frac: float = 0.08,
    stroke_frac: float = 0.10,
) -> dict:
    """Bir bolgeye ceviriyi yazar. Olcu bilgilerini (font, satirlar, tasma)
    sozluk olarak dondurur."""
    x1, y1, x2, y2 = bbox
    bw, bh = x2 - x1, y2 - y1
    ix = max(1, int(bw * margin_x_frac))
    iy = max(1, int(bh * margin_y_frac))
    inner_w, inner_h = bw - 2 * ix, bh - 2 * iy

    cap = min(max_size, max(min_size, int(inner_h * 0.45)))
    size, lines, lh = fit_font_size(text, font_path, inner_w, inner_h, min_size, cap)
    font = ImageFont.truetype(str(font_path), size)
    block_h = len(lines) * lh
    overflow = block_h > inner_h
    stroke = max(2, int(size * stroke_frac))

    top = y1 + iy + max(0, (inner_h - block_h) // 2)
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        lw = int(font.getlength(line))
        x = x1 + ix + max(0, (inner_w - lw) // 2)
        y = top + i * lh
        if stroke > 0:
            draw.text((x, y), line, font=font, fill="white", stroke_width=stroke,
                      stroke_fill="white")
        draw.text((x, y), line, font=font, fill="black")

    return {
        "font_size": size,
        "lines": lines,
        "line_height": lh,
        "block_height": block_h,
        "inner": (x1 + ix, y1 + iy, x2 - ix, y2 - iy),
        "overflow": overflow,
    }


# ---------------------------------------------------------------- cikti

def make_before_after(
    original: Image.Image,
    cleaned: Image.Image,
    translated: Image.Image,
    out_path: Path,
    labels: tuple[str, str, str] = ("Orijinal (JP)", "Temizlenmis", "Ceviri + Typeset"),
) -> None:
    w, h = original.size
    pad, hdr = 6, 30
    total_w = (w + pad) * 3 + pad
    total_h = h + hdr + pad * 2
    canvas = Image.new("RGB", (total_w, total_h), (28, 28, 28))
    draw = ImageDraw.Draw(canvas)
    for i, (name, im) in enumerate(zip(labels, (original, cleaned, translated))):
        x = pad + i * (w + pad)
        draw.rectangle((x, 0, x + w, hdr), fill=(10, 10, 10))
        draw.text((x + 8, 7), name, fill=(255, 255, 255))
        canvas.paste(im, (x, hdr + pad))
    canvas.save(out_path)
    info(f"Karsilastirma kaydedildi: {out_path}")


# ---------------------------------------------------------------- .env

def load_env() -> None:
    """python-dotenv varsa .env'i yukler (python/ ve calisma dizini arar)."""
    try:
        from dotenv import load_dotenv

        here = Path(__file__).resolve().parent / ".env"
        cwd = Path.cwd() / ".env"
        if here.is_file():
            load_dotenv(here)
        elif cwd.is_file():
            load_dotenv(cwd)
        else:
            info("Uyari: .env bulunamadi. python/.env.example dosyasindan "
                 "kopyalayin (LLM_API_KEY vb.) - ya da --provider mock kullanin.")
    except ImportError:
        info("Uyari: python-dotenv kurulu degil; ortam degiskenleri "
             "dogrudan kullanilacak.")


# ---------------------------------------------------------------- CLI

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Ceviri + typesetting prototipi: OCR json + temizlenmis "
                    "gorsel -> cevrilmis typeset sayfa",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("image", help="Orijinal manga sayfasi gorseli (JPG/PNG)")
    p.add_argument("--regions", required=True, metavar="JSON",
                   help="ocr_prototype.py --json ciktisi (bbox + Japonca metin)")
    p.add_argument("--cleaned", metavar="GORSEL", default=None,
                   help="inpaint_prototype.py'nin temizlenmis gorseli. "
                        "Verilmezse orijinal uzerine yazar (onerilmez).")
    p.add_argument("--target-lang", default="en",
                   help="Hedef dil (kod veya ad; 'Turkish' de olur)")
    p.add_argument("--provider", choices=["auto", "mock", "local", "api",
                                          "openai_compat"],
                   default="auto",
                   help="auto: LLM_API_KEY varsa api, yoksa mock | "
                        "local: yerel Ollama | api: .env/CLI'dan anahtar+adres")
    p.add_argument("--api-key", default=None,
                   help="LLM_API_KEY yerine dogrudan anahtar (CLI kullanimi "
                        "ortak shell'lerde onerilmez)")
    p.add_argument("--base-url", default=None,
                   help="LLM_BASE_URL yerine dogrudan ucnokta adresi "
                        "(ornek: https://api.groq.com/openai/v1)")
    p.add_argument("--model", default=None,
                   help="LLM_MODEL yerine dogrudan model adi")
    p.add_argument("--temperature", type=float, default=None,
                   help="LLM istegi sicakligi (varsayilan: .env 0.2)")
    p.add_argument("--timeout", type=float, default=None,
                   help="LLM istegi zaman asimi saniye (varsayilan: .env 120)")
    p.add_argument("--context", default="",
                   help="LLM'e verilecek seri/olay baglami notu (tutarlilik icin)")
    p.add_argument("--font", metavar="TTF", default=str(DEFAULT_FONT),
                   help="Typeset fontu (varsayilan: Comic Neue Bold, OFL-1.1)")
    p.add_argument("--min-font-size", type=int, default=9,
                   help="Alt font boyutu siniri (px)")
    p.add_argument("--max-font-size", type=int, default=36,
                   help="Ust font boyutu siniri (px)")
    p.add_argument("--out-dir", metavar="DIR", default=None,
                   help="Cikti dizini (varsayilan: gorselin yanindaki test_data)")
    p.add_argument("--json", action="store_true",
                   help="Sonuclari makine-okunur JSON olarak da bas")
    p.add_argument("--save-json", metavar="YOL", default=None,
                   help="JSON sonucu dosyaya UTF-8 olarak yaz")
    p.add_argument("--debug", action="store_true", help="Detayli log")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()
    args = parse_args(argv)
    load_env()

    image_path = Path(args.image)
    if not image_path.is_file():
        info(f"Hata: gorsel bulunamadi: {image_path}")
        return 2
    regions_path = Path(args.regions)
    if not regions_path.is_file():
        info(f"Hata: bolge JSON'u bulunamadi: {regions_path}")
        return 2

    out_dir = Path(args.out_dir) if args.out_dir else image_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    original = Image.open(image_path).convert("RGB")
    info(f"Gorsel: {image_path} ({original.width}x{original.height})")

    # ---- 1) Bolgeler ----
    regions, bubbles = load_regions(regions_path)
    info(f"Bolge girdisi: {len(regions)} metin, {len(bubbles)} balon")

    # Cevrilecek bolgeler: bos / "(manual)" (SFX sonradan elle eklenen) haric
    candidates = []
    for i, r in enumerate(regions):
        text = (r.get("text") or "").strip()
        if not text or text == "(manual)":
            info(f"  bolge[{i}] atlandi (cevrilecek metin yok: {text!r})")
            continue
        r = dict(r)
        r["index"] = i
        candidates.append(r)
    if not candidates:
        info("Hata: cevrilecek metin bolgesi yok.")
        return 2

    # ---- 2) Okuma sirasi (dikey Japonca duzeni icin) ----
    ordered = manga_reading_order(candidates)
    if args.debug:
        info("Okuma sirasi (sagdan sola sutunlar, ustten alta):")
        for n, r in enumerate(ordered, start=1):
            info(f"  {n}. bolge[{r['index']}] {r['label_name']} "
                 f"bbox={r['bbox']} text={r.get('text', '')[:16]!r}")

    entries = [{"id": n - 1, "text": r["text"], "label_name": r["label_name"]}
               for n, r in enumerate(ordered, start=1)]

    # ---- 3) Ceviri ----
    provider_name = args.provider
    if provider_name == "auto":
        provider_name = "api" if resolve_credentials(args.api_key)[0] else "mock"
        if provider_name == "mock":
            info("auto: LLM_API_KEY yok -> mock backend (deterministik test "
                 "cevirisi). Gercek ceviri icin .env'i doldurun.")

    try:
        backend: TranslationBackend = create_backend(
            provider_name,
            api_key=args.api_key,
            base_url=args.base_url,
            model=args.model,
            temperature=args.temperature,
            timeout=args.timeout,
        )
    except ValueError as exc:
        info(f"Hata: backend kurulamadi: {exc}")
        return 2

    if backend.name == "openai_compat":
        _key, url, _mdl, _t, _to = resolve_credentials(args.api_key,
                                                       args.base_url, args.model)
        if not _key and not url:
            info("Hata: LLM_API_KEY bos. python/.env dosyasina yazin ya da "
                 "--provider mock kullanin.")
            return 2

    info(f"Provider: {backend.name} (model: {backend.model}) | "
         f"Hedef dil: {args.target_lang} | Bolge: {len(entries)} "
         f"-> tek istek")
    t0 = time.perf_counter()
    try:
        translations = backend.translate_page(
            entries, args.target_lang, context=args.context
        )
    except Exception as exc:  # noqa: BLE001
        info(f"Hata: LLM istegi basarisiz: {exc}")
        return 1
    t_translate = (time.perf_counter() - t0) * 1000

    # id -> bolge eslemesi; eksik cevirilerde fallback
    for e in entries:
        if e["id"] not in translations or not translations[e["id"]].strip():
            translations[e["id"]] = FALLBACK_TRANSLATION
            info(f"  uyari: bolge id={e['id']} cevirisi yok/yok -> "
                 f"{FALLBACK_TRANSLATION}")
    for n, e in enumerate(entries):
        info(f"  {n + 1}. [{ordered[n]['label_name']}] "
             f"{e['text'][:18]!r} -> {translations[e['id']][:40]!r}")

    # ---- 4) Typeset ----
    cleaned_path = Path(args.cleaned) if args.cleaned else None
    if cleaned_path and cleaned_path.is_file():
        canvas = Image.open(cleaned_path).convert("RGB")
        info(f"Temizlenmis gorsel: {cleaned_path}")
    else:
        canvas = original.copy()
        info("Uyari: --cleaned verilmedi/bulunamadi; ceviri orijinal "
             "(temizlenmemis) gorsel uzerine yazilacak.")

    t0 = time.perf_counter()
    typeset_info: dict[int, dict] = {}
    for e in entries:
        r = ordered[e["id"]]
        typeset_info[e["id"]] = typeset_region(
            canvas,
            r["bbox"],
            translations[e["id"]],
            args.font,
            min_size=args.min_font_size,
            max_size=args.max_font_size,
        )
    t_typeset = (time.perf_counter() - t0) * 1000

    for e in entries:
        ti = typeset_info[e["id"]]
        flag = "  (TASMA!)" if ti["overflow"] else ""
        info(f"  id={e['id']} font={ti['font_size']}px "
             f"satir={len(ti['lines'])} {ti['lines']!r}{flag}")

    stem = image_path.stem
    translated_path = out_dir / f"{stem}_translated.png"
    canvas.save(translated_path)
    info(f"Cevrilmis sayfa kaydedildi: {translated_path}")

    before_after_path = out_dir / f"{stem}_before_after.png"
    make_before_after(original, canvas if cleaned_path is None else
                      Image.open(cleaned_path).convert("RGB"), canvas,
                      before_after_path)

    # ---- 5) JSON ----
    payload = {
        "image": str(image_path),
        "cleaned": str(cleaned_path) if cleaned_path else None,
        "provider": {"name": backend.name, "model": backend.model},
        "target_language": args.target_lang,
        "context": args.context,
        "reading_order": [
            {"id": e["id"], "index": r["index"], "label_name": r["label_name"],
             "bbox": list(r["bbox"]), "text": r["text"]}
            for e, r in zip(entries, ordered)
        ],
        "translations": [
            {
                "id": e["id"],
                "index": r["index"],
                "label_name": r["label_name"],
                "bbox": list(r["bbox"]),
                "original": r["text"],
                "translation": translations[e["id"]],
                "font_size": typeset_info[e["id"]]["font_size"],
                "lines": typeset_info[e["id"]]["lines"],
                "overflow": typeset_info[e["id"]]["overflow"],
            }
            for e, r in zip(entries, ordered)
        ],
        "timings_ms": {
            "translate": round(t_translate, 1),
            "typeset": round(t_typeset, 1),
        },
        "outputs": {
            "translated": str(translated_path),
            "before_after": str(before_after_path),
        },
    }

    if args.save_json:
        Path(args.save_json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        info(f"JSON kaydedildi: {args.save_json}")
    if args.json:
        print("")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
