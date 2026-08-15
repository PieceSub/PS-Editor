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
Yeni saglayici eklemek: sınıf yazip create_backend'e baglamak yeterli.
Su an kayitli:
  - mock          : API anahtari gerektirmez; pipeline'i uctan uca test etmek
                    icin deterministik sahte ceviri uretir.
  - api / openai_compat : openai kutuphanesiyle OpenAI ya da herhangi bir
                    OpenAI-uyumlu ucnokta (Groq, Together, DeepSeek, Ollama,
                    LM Studio, vLLM vb. - BASE_URL + API_KEY ile).
  - openai        : "openai_compat"in OpenAI resmi ucnoktasina kilitli hali
                    (BASE_URL bos birakilir); ayri sinif gerektirmez.
  - anthropic     : Anthropic Claude Messages API (OpenAI-uyumlu DEGILDIR:
                    system top-level parametredir, max_tokens zorunludur,
                    yanit content blok dizisidir) -- ayri AnthropicBackend
                    sinifidir; OpenAICompatBackend'e yazilmaz.
  - local         : yerel Ollama (BASE_URL http://localhost:11434/v1).

Ceviri kaynagi secimi ("auto"/"local"/"api") ve VRAM esigi mantigi
pipeline.py'deki resolve_translation_mode icindedir (adim 5). Burada
yalnizca backend uretimi + kimlik cozumu vardir.

API anahtari / base_url / model, "resolve_credentials" adli TEK fonksiyondan
okunur. Kaynak onceligi (adim 5 itibariyle):
  1) CLI/payload argumanlari
  2) isletim sistemi guvenli anahtar deposu (keyring: Windows Credential
     Manager / macOS Keychain / Linux Secret Service) -- PyInstaller'daki
     bilinen backend kesfi sorunu icin backend acikca set edilir
  3) .env ortam degiskenleri (geliştirme fallback'i)
  4) kod varsayilanlari

Guvenli depoya yazma: store_credential() (sidecar "set_api_key" komutu bunu
cagirir). Anahtarlar hicbir zaman stdout/log'a yazilmaz.

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
import re
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


class AnthropicBackend:
    """Anthropic Claude Messages API backend'i (OpenAI-uyumlu DEGIL).

    OpenAI'dan farkli noktalar (adim 5 arastirma):
      - Kimlik: Authorization: Bearer yerine x-api-key + anthropic-version.
      - Sisteme komut uyeleri icinde "system" ROLE yoktur; system TOP-LEVEL
        parametredir.
      - max_tokens ZORUNLUDUR (yoksa gecersiz 400).
      - Yanit: choices[0].message.content yerine content (blok dizisi);
        metin bloklarinda .text/.type vardir. Text'i toplariz.
      - response_format / seed gibi OpenAI parametreleri desteklenmez;
        JSON, prompt ile istenir ve parse_llm_json ile ayiklanir
        (OpenAI meslektaisiyla ayni yapidal prompt kullanilir).
    """

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str = "claude-sonnet-4-6",
        temperature: float = 0.2,
        timeout: float = 120.0,
        max_tokens: int = 4096,
    ):
        from anthropic import Anthropic

        kwargs: dict = {"api_key": api_key, "timeout": timeout}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = Anthropic(**kwargs)
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    def translate_page(
        self,
        entries: list[dict],
        target_lang: str,
        context: str = "",
    ) -> dict[int, str]:
        system, user = OpenAICompatBackend.build_prompt(
            entries, target_lang, context)
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            temperature=self.temperature,
            messages=[{"role": "user", "content": user}],
        )
        parts = [
            block.text
            for block in resp.content
            if getattr(block, "type", None) == "text"
        ]
        raw = "\n".join(parts)
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


# ---------------------------------------------------------------- guvenli anahtar deposu

KEYRING_SERVICE = "PS-Editor"
KEYRING_FIELD = "api_key"


def _init_keyring_backend() -> None:
    """PyInstaller paketinde keyring backend'leri entry-point kesfiyle
    gorunmez (bilinen sorun: jaraco/keyring #324, #439, #591). Platform
    backend'ini acikca import edip set_keyring ile sabitleriz; hata olursa
    sessizce gec (cagiran .env fallback'ine duser).
    """
    try:
        import keyring
    except ImportError:
        return
    try:
        if sys.platform == "win32":
            from keyring.backends import Windows
            backend = Windows.WinVaultKeyring()
        elif sys.platform == "darwin":
            from keyring.backends import macOS
            backend = macOS.Keyring()
        else:
            from keyring.backends import SecretService
            backend = SecretService.Keyring()
        keyring.set_keyring(backend)
    except Exception:  # noqa: BLE001
        pass


def secure_get(provider: str, field: str = KEYRING_FIELD) -> str | None:
    """OS guvenli deposundan deger okur. Asla istisna firlatmaz (None doner):
    kilitli keyring (macOS), backend yoklugu (Linux headless), PyInstaller
    backend sorunu -- hepsinde sessizce .env fallback'ine gecilir.
    """
    try:
        _init_keyring_backend()
        import keyring
        return keyring.get_password(KEYRING_SERVICE, f"{provider}:{field}")
    except Exception:  # noqa: BLE001
        return None


def secure_set(provider: str, api_key: str, field: str = KEYRING_FIELD) -> bool:
    """OS guvenli deposuna API anahtari yazar. Basariyi bool olarak doner;
    anahtar hicbir yerde loglanmaz."""
    if not api_key:
        return False
    try:
        _init_keyring_backend()
        import keyring
        keyring.set_password(KEYRING_SERVICE, f"{provider}:{field}", api_key)
        return True
    except Exception:  # noqa: BLE001
        return False


def secure_delete(provider: str, field: str = KEYRING_FIELD) -> bool:
    try:
        _init_keyring_backend()
        import keyring
        keyring.delete_password(KEYRING_SERVICE, f"{provider}:{field}")
        return True
    except Exception:  # noqa: BLE001
        return False


def store_credential(provider: str, api_key: str) -> dict:
    """sidecar "set_api_key" komutunun kullandigi resmi yazma noktasi."""
    ok = secure_set(provider, api_key)
    return {"stored": ok, "provider": provider,
            "note": None if ok else
            "Guvenli depo kullanilamadi; anahtar kaydedilmedi. "
            "Geliştirme ortaminda .env kullanabilirsiniz."}


# ---------------------------------------------------------------- backend fabrikasi

DEFAULT_OLLAMA_URL = "http://localhost:11434/v1"
DEFAULT_OLLAMA_MODEL = "llama3.1"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"


def resolve_credentials(
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    provider: str = "openai_compat",
) -> tuple[str, str | None, str, float, float]:
    """API anahtari / base_url / model kaynagini TEK noktada toplar.

    Kaynak onceligi (adim 5): CLI/payload argumanlari > OS guvenli deposu
    (keyring: Windows Credential Manager / macOS Keychain / Linux Secret
    Service) > .env ortam degiskenleri > kod varsayilanlari.

    Donen imza DEGISMEDI (api_key, base_url, model, temperature, timeout);
    yalnizca giris kaynaklari guvenli depo ile genisletildi. Anthropic,
    provider="anthropic" ile cagrildiginda kendine ait env adlarini okur.
    """
    key = (api_key or "").strip()
    if not key:
        key = (secure_get(provider) or "").strip()
    if not key:
        key = os.environ.get("LLM_API_KEY", "").strip()

    if provider == "anthropic":
        if not key:
            key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        url = base_url or os.environ.get("ANTHROPIC_BASE_URL") or None
        mdl = model or os.environ.get("ANTHROPIC_MODEL",
                                      DEFAULT_ANTHROPIC_MODEL)
    else:
        url = base_url or os.environ.get("LLM_BASE_URL") or None
        mdl = model or os.environ.get("LLM_MODEL", DEFAULT_OPENAI_MODEL)

    return (
        key,
        url,
        mdl,
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
    """Provider adindan backend ornegi uretir.

    - mock          -> MockBackend (anahtar gerekmez)
    - openai        -> OpenAICompatBackend, resmi OpenAI ucnoktasi
    - openai_compat / api -> OpenAICompatBackend, BASE_URL+MODEL ile
      (Groq, Together, DeepSeek, LM Studio, vLLM vb.)
    - local         -> OpenAICompatBackend, Ollama adresi (anahtar gerekmez)
    - anthropic     -> AnthropicBackend (ayri API formati)
    """
    if provider == "mock":
        return MockBackend()

    if provider in ("openai", "openai_compat", "api", "local"):
        key, url, mdl, temp, to = resolve_credentials(
            api_key, base_url, model, provider=provider)
        if provider == "local":
            url = url or DEFAULT_OLLAMA_URL
            mdl = model or DEFAULT_OLLAMA_MODEL
            # Ollama anahtar istemez ancak openai>=3 bos string'i reddeder;
            # dolgu anahtar geciliyor.
            key = api_key or "ollama"
        elif provider == "openai":
            url = None  # resmi ucnokta; LLM_BASE_URL kasitla yok sayilir
        return OpenAICompatBackend(
            api_key=key,
            base_url=url,
            model=mdl,
            temperature=temperature if temperature is not None else temp,
            timeout=timeout if timeout is not None else to,
        )

    if provider == "anthropic":
        key, url, mdl, temp, to = resolve_credentials(
            api_key, base_url, model, provider="anthropic")
        return AnthropicBackend(
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


# ------------------------------------------------------- bolge-bazli stil (adim 7)

REGION_STYLE_DEFAULTS: dict = {
    "font_weight": "bold",       # "bold" | "normal"
    "color": None,               # None -> klasik siyah dolgu + beyaz kontur
    "font_size_override": None,  # int -> otomatik uyumu iptal eder
    "align": "center",           # "left" | "center" | "right"
}


def normalize_region_style(style: dict | None) -> dict:
    """Bölge stilini dogrular, eksik alanlari varsayilanla doldurur.

    Bilinmeyen/yanlis tipli degerler sessizce varsayilana duser; firlatma
    yoktur - frontend'in gonderebilecegi kirli degerlere karsi dayanikli.
    """
    out = dict(REGION_STYLE_DEFAULTS)
    if not style or not isinstance(style, dict):
        return out
    for key in ("font_weight", "color", "font_size_override", "align"):
        if key not in style or style[key] is None:
            continue
        val = style[key]
        if key == "font_weight":
            if val in ("bold", "normal"):
                out[key] = val
        elif key == "align":
            if val in ("left", "center", "right"):
                out[key] = val
        elif key == "font_size_override":
            try:
                out[key] = max(1, int(val))
            except (TypeError, ValueError):
                pass
        elif key == "color":
            s = str(val).strip()
            if re.fullmatch(r"#[0-9a-fA-F]{6}", s):
                out[key] = s
    return out


def resolve_font_for_weight(font_path: str | Path, weight: str) -> Path:
    """Kayitli bold/regular font ciftini duyarlı sekilde secer.

    font_path default (ComicNeue-Bold) ise ve weight='normal' istendiyse
    yanindaki Regular dosyasina gecilir; ozel fontta (kullanici yolu) bolunme
    yoksa oldugu gibi kullanilir - ozel TTF zaten kendi agirligini tasir.
    """
    p = Path(font_path)
    if weight == "normal" and p.name.lower() in ("comicneue-bold.ttf", "comicneue_bold.ttf"):
        sibling = p.parent / "ComicNeue-Regular.ttf"
        if sibling.is_file():
            return sibling
    return p


def _relative_luminance(hex_color: str) -> float:
    """W3C yakinligi (BT.709) - kontur rengi secimi icin 0..1."""
    r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


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
    style: dict | None = None,
) -> dict:
    """Bir bolgeye ceviriyi yazar. Olcu bilgilerini (font, satirlar, tasma)
    sozluk olarak dondurur.

    Adim 7: istege bagli `style` sozlugu bolge bazli gorunum secenekleri:
      - font_weight        : "bold" (varsayilan) | "normal" - font dosyasi secimi
      - color              : "#rrggbb" | None (varsayilan) - None ise eski
                             davranis: siyah dolgu + beyaz kontur (scanlation).
                             Renk verilirse dolgu renk olur; kontur parlakliga
                             gore otomatik (koyu renkte beyaz, acikta siyah).
      - font_size_override : int | None - verilirse sabit boyut (otomatik
                             uyum atlanir); tasma yine raporlanir.
      - align              : "left" | "center" (varsayilan) | "right" - yatay
                             hizalama; dikey ortalamaya dokunmaz.
    `style=None` veya bos dict -> onceki davranisin aynisi (geriye donuk).
    """
    st = normalize_region_style(style)
    font_path = resolve_font_for_weight(font_path, st["font_weight"])

    x1, y1, x2, y2 = bbox
    bw, bh = x2 - x1, y2 - y1
    ix = max(1, int(bw * margin_x_frac))
    iy = max(1, int(bh * margin_y_frac))
    inner_w, inner_h = bw - 2 * ix, bh - 2 * iy

    if st["font_size_override"]:
        size = max(min_size, min(int(st["font_size_override"]), max(200, max_size)))
        font = ImageFont.truetype(str(font_path), size)
        lines = wrap_text(text, font, inner_w)
        lh = line_height(font)
    else:
        cap = min(max_size, max(min_size, int(inner_h * 0.45)))
        size, lines, lh = fit_font_size(
            text, font_path, inner_w, inner_h, min_size, cap)
        font = ImageFont.truetype(str(font_path), size)
    block_h = len(lines) * lh
    overflow = block_h > inner_h
    stroke = max(2, int(size * stroke_frac))

    # Renk cozumu: None -> klasik; deger verildiyse kontur parlakliga gore.
    if st["color"] is None:
        fill = "black"
        stroke_fill = "white"
    else:
        fill = st["color"]
        stroke_fill = "black" if _relative_luminance(st["color"]) >= 140 else "white"

    top = y1 + iy + max(0, (inner_h - block_h) // 2)
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        lw = int(font.getlength(line))
        if st["align"] == "left":
            x = x1 + ix
        elif st["align"] == "right":
            x = x2 - ix - lw
        else:
            x = x1 + ix + max(0, (inner_w - lw) // 2)
        y = top + i * lh
        if stroke > 0:
            draw.text((x, y), line, font=font, fill=stroke_fill,
                      stroke_width=stroke, stroke_fill=stroke_fill)
        draw.text((x, y), line, font=font, fill=fill)

    return {
        "font_size": size,
        "lines": lines,
        "line_height": lh,
        "block_height": block_h,
        "inner": (x1 + ix, y1 + iy, x2 - ix, y2 - iy),
        "overflow": overflow,
        "style_used": st,
    }


# ------------------------------------------------------- bolge bazli yeniden render (adim 7)

ERASE_PAD_FRAC = 0.04       # silme kutusunu genisletme orani (kontur kalintisi icin)
INPAINT_RADIUS = 3


def _erase_box(canvas: Image.Image, source: Image.Image | None, box: tuple[int, int, int, int],
               method: str) -> None:
    """Bir kutuyu okunmaz hale getirir (eski typeset / kacirilmis kaynak metin).

    - "paste"   : temizlenmis gorselin ayni bolgesini yapistirir (canvas'a
                  sadik, anlik). Kaynak metin inpainting ile silinmis bir
                  bolge icin dogrudur.
    - "inpaint" : kaynak gorsel yoksa ya da bolge hic inpaintenmemisse
                  (elle eklenen bolgeler) yerel cv2 Telea yontemiyle doldurur
                  (torch yok, ~1 sn).
    - "none"    : dokunma (yalnizca typeset).
    """
    x1, y1, x2, y2 = box
    if method == "none" or x2 <= x1 or y2 <= y1:
        return
    if method == "paste" and source is not None:
        w, h = canvas.size
        cx1, cy1 = max(0, x1), max(0, y1)
        cx2, cy2 = min(w, x2), min(h, y2)
        if cx2 > cx1 and cy2 > cy1:
            canvas.paste(source.crop((cx1, cy1, cx2, cy2)), (cx1, cy1, cx2, cy2))
        return
    if method == "inpaint":
        import cv2
        import numpy as np
        w, h = canvas.size
        cx1, cy1 = max(0, x1), max(0, y1)
        cx2, cy2 = min(w, x2), min(h, y2)
        if cx2 <= cx1 or cy2 <= cy1:
            return
        big = canvas.crop((cx1, cy1, cx2, cy2)).convert("RGB")
        mask = np.zeros((cy2 - cy1, cx2 - cx1), dtype=np.uint8)
        mask[:] = 255
        kernel = np.ones((5, 5), np.uint8)
        mask = cv2.dilate(mask, kernel, iterations=1)
        res = cv2.inpaint(np.asarray(big), mask, INPAINT_RADIUS, cv2.INPAINT_TELEA)
        canvas.paste(Image.fromarray(res), (cx1, cy1, cx2, cy2))
        return
    raise ValueError(f"bilinmeyen silme yontemi: {method!r}")


def re_render_region(
    cleaned_path: str | Path | None,
    output_path: str | Path,
    bbox: tuple[int, int, int, int],
    translation: str,
    font_path: str | Path = DEFAULT_FONT,
    min_size: int = 9,
    max_size: int = 36,
    style: dict | None = None,
    erase: str = "paste",
    erase_boxes: list[tuple[int, int, int, int]] | None = None,
) -> dict:
    """TEK bolgeyi yeniden typeset eder; sayfanin geri kalani degismez.

    Strateji (arastirma notu: PIL crop+paste, bolge bazli yeniden renderin
    kanonik yoludur -- Pillow docs Image.crop / Image.paste):
      1) canvas = cikti gorseli (mevcut translated sayfa) kopyasi
      2) hedef kutu onceden temizlenmis gorselden (kaynak metinsiz) kırpılıp
         geri yapistirilir -> eski typeset metni gider ("paste")
         - kaynak yoksa / bolge inpaintenmemisse yerel cv2 inpaint ("inpaint")
      3) yeni ceviri ayarli stille yazilir (typeset_region)
      4) dosya kaydedilir; yeni olcumler ve kullanilan stil doner.

    Böylece otomatik pipeline'i (OCR/inpainting/ceviri) hic calistirmadan
    yalnizca bu bolge guncellenir; islem yalnizca PIL (+istege bagli cv2)
    icerir, torch yuklemez.
    """
    if os.path.isfile(output_path):
        canvas = Image.open(output_path).convert("RGB")
    else:
        # Cikti henuz yok (ornek: cok yeni elle bolge) -> temiz gorselden basla.
        canvas = (
            Image.open(cleaned_path).convert("RGB")
            if cleaned_path and Path(cleaned_path).is_file()
            else None
        )
        if canvas is None:
            raise ValueError(
                "Yeniden render icin cikti gorseli yok ve temizlenmis kaynak "
                "bulunamadi; sayfayi once isleyin."
            )
    source = (
        Image.open(cleaned_path).convert("RGB")
        if cleaned_path and Path(cleaned_path).is_file()
        else None
    )
    if source is None and erase == "paste":
        erase = "inpaint"
    # Varsayilan silme: typeset edilecek kutunun kendisi (+ guvenli pay);
    # tasinma durumunda eski konum da erase_boxes ile birlikte silinir.
    pad_x = max(4, int((bbox[2] - bbox[0]) * ERASE_PAD_FRAC))
    pad_y = max(4, int((bbox[3] - bbox[1]) * ERASE_PAD_FRAC))
    boxes = [bbox, *(erase_boxes or [])]
    for bx in boxes:
        grown = (
            bx[0] - pad_x, bx[1] - pad_y, bx[2] + pad_x, bx[3] + pad_y
        )
        _erase_box(canvas, source, grown, erase)

    if not (translation or "").strip():
        # Bos metin = bolgeyi devre disi birak (yalnizca sil, yazma).
        canvas.save(output_path)
        return {
            "bbox": list(bbox),
            "translation": "",
            "font_size": None,
            "lines": 0,
            "overflow": False,
            "disabled": True,
            "style_used": normalize_region_style(style),
            "erase": erase,
        }

    info = typeset_region(
        canvas, bbox, translation, font_path,
        min_size=min_size, max_size=max_size, style=style)

    canvas.save(output_path)
    return {
        "bbox": list(bbox),
        "translation": translation,
        "font_size": info["font_size"],
        "lines": len(info["lines"]),
        "overflow": info["overflow"],
        "disabled": False,
        "style_used": info["style_used"],
        "erase": erase,
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
                                          "openai", "openai_compat",
                                          "anthropic"],
                   default="auto",
                   help="auto: LLM_API_KEY varsa api, yoksa mock | "
                        "local: yerel Ollama | openai: OpenAI | "
                        "openai_compat: BASE_URL+MODEL ile uyumlu ucnokta | "
                        "anthropic: Claude Messages API | "
                        "api: .env/guvenli depodan anahtar+adres")
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
        provider_name = "api" if resolve_credentials(
            args.api_key, provider="openai_compat")[0] else "mock"
        if provider_name == "mock":
            info("auto: API anahtari yok (guvenli depo + .env) -> mock "
                 "backend (deterministik test cevirisi).")

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

    if backend.name in ("openai_compat", "anthropic"):
        _key, url, _mdl, _t, _to = resolve_credentials(
            args.api_key, args.base_url, args.model, provider=backend.name)
        if provider_name == "anthropic" and not _key:
            info("Hata: Anthropic API anahtari bos. Guvenli depoya "
                 "kaydedin (set_api_key) ya da .env'e ANTHROPIC_API_KEY "
                 "yazin; --provider mock ile test edebilirsiniz.")
            return 2
        if not _key and not url:
            info("Hata: API anahtari bos. Guvenli depoya kaydedin ya da "
                 "python/.env dosyasina yazin; --provider mock ile test "
                 "edebilirsiniz.")
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
