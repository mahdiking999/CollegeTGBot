# ============================================================================
# GENERATED FILE - do not edit by hand.
# Source: engine/pdf_engine.py + engine/app.py
# Rebuild: python engine/build_hf_app.py
# ============================================================================

"""
Layout-preserving bilingual (English -> Arabic) PDF engine.

No web-framework imports here, so it can be unit-tested directly.

Output page order:
    source page 1, Arabic page 1, source page 2, Arabic page 2, ...

Per page:
  1. copy the original page untouched (English)
  2. copy it again and delete only the text layer (images / vector art survive)
  3. put every translated block back into its original box with an Arabic font,
     shrinking the type as little as possible so the block still fits

Translation providers are swappable (Google Cloud key, DeepL key, or the free
Google web endpoint). The free endpoint is rate-limited from datacenter IPs, so
a key-based provider is strongly preferred for anything beyond a demo.
"""

from __future__ import annotations

import html as html_mod
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

import pymupdf as fitz

VERSION = "3.0.0"

DEFAULT_MAX_PAGES = int(os.environ.get("MAX_PAGES", "40"))
MAX_PAGES_HARD_LIMIT = 200
MAX_SEGMENT_CHARS = 4000
MIN_FONT_SIZE = 6.5
FONT_REGULAR = "NotoNaskhArabic-Regular.ttf"
FONT_BOLD = "NotoNaskhArabic-Bold.ttf"

_FONT_URLS = {
    FONT_REGULAR: (
        "https://raw.githubusercontent.com/googlefonts/noto-fonts/main/"
        "hinted/ttf/NotoNaskhArabic/NotoNaskhArabic-Regular.ttf"
    ),
    FONT_BOLD: (
        "https://raw.githubusercontent.com/googlefonts/noto-fonts/main/"
        "hinted/ttf/NotoNaskhArabic/NotoNaskhArabic-Bold.ttf"
    ),
}


class EngineError(RuntimeError):
    """Base class for expected, user-facing engine failures."""


class InputError(EngineError):
    """The uploaded document cannot be processed as-is (400)."""


class TranslationError(EngineError):
    """The translation provider failed (502)."""

    def __init__(self, message: str, *, provider: str = "", status: Optional[int] = None):
        super().__init__(message)
        self.provider = provider
        self.status = status


class LayoutError(EngineError):
    """The PDF could not be rebuilt (500)."""


# ---------------------------------------------------------------------------
# Arabic font handling
# ---------------------------------------------------------------------------


def default_font_dir() -> str:
    return os.environ.get("ARABIC_FONT_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "fonts"
    )


def ensure_fonts(font_dir: Optional[str] = None, allow_download: bool = True) -> dict:
    """Make sure an Arabic-capable TTF is available. Returns font paths."""
    directory = font_dir or default_font_dir()
    os.makedirs(directory, exist_ok=True)
    found = {}
    for name in (FONT_REGULAR, FONT_BOLD):
        path = os.path.join(directory, name)
        if not os.path.isfile(path) or os.path.getsize(path) < 50_000:
            if allow_download:
                _download_font(_FONT_URLS[name], path)
        if os.path.isfile(path) and os.path.getsize(path) >= 50_000:
            found[name] = path
    if FONT_REGULAR not in found:
        raise LayoutError(
            "Arabic font is missing. Put NotoNaskhArabic-Regular.ttf in "
            f"'{directory}' (or set ARABIC_FONT_DIR) and retry."
        )
    return found


def _download_font(url: str, path: str) -> None:
    tmp = path + ".part"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "bilingual-pdf-engine"})
        with urllib.request.urlopen(request, timeout=25) as response:
            data = response.read()
        if len(data) < 50_000:
            return
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _css(fonts: dict) -> str:
    rules = [
        f"@font-face {{ font-family: arabic; src: url({FONT_REGULAR}); }}",
    ]
    if FONT_BOLD in fonts:
        rules.append(
            f"@font-face {{ font-family: arabic; font-weight: bold; "
            f"src: url({FONT_BOLD}); }}"
        )
    rules.append("* { font-family: arabic; }")
    # MuPDF's default body margin offsets the content and breaks height fitting.
    rules.append("body { margin: 0; padding: 0; }")
    rules.append(
        "div { direction: rtl; margin: 0; padding: 0; "
        "line-height: 1.18; overflow-wrap: break-word; }"
    )
    return "\n".join(rules)


# ---------------------------------------------------------------------------
# Translation providers
# ---------------------------------------------------------------------------


def _post_json(url: str, payload: dict, headers: Optional[dict] = None,
               timeout: int = 40) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        snippet = ""
        try:
            snippet = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise TranslationError(
            f"HTTP {exc.code} from translation provider. {snippet}".strip(),
            status=exc.code,
        ) from exc
    except Exception as exc:  # network, DNS, timeout, bad JSON
        raise TranslationError(
            f"{type(exc).__name__}: {exc}", status=None
        ) from exc


def _with_retries(call: Callable[[], dict], provider: str, attempts: int = 3) -> dict:
    delay = 1.0
    last: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except TranslationError as exc:
            last = exc
            retryable = exc.status in (None, 429, 500, 502, 503, 504) or (
                exc.status is not None and 500 <= exc.status < 600
            )
            if not retryable or attempt == attempts:
                raise
            time.sleep(delay)
            delay *= 2
    raise TranslationError(f"{provider} failed: {last}", provider=provider)


def _chunks(items: Sequence[str], max_items: int, max_chars: int) -> Iterable[Sequence[str]]:
    batch: list = []
    chars = 0
    for item in items:
        if batch and (len(batch) >= max_items or chars + len(item) > max_chars):
            yield batch
            batch, chars = [], 0
        batch.append(item)
        chars += len(item)
    if batch:
        yield batch


class Translator:
    name = "base"

    def translate_many(self, texts: Sequence[str]) -> list:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"provider": self.name}


class GoogleCloudTranslator(Translator):
    """Google Cloud Translation v2. 500k characters/month free tier."""

    name = "google_cloud"
    BATCH_ITEMS = 100
    BATCH_CHARS = 25_000

    def __init__(self, api_key: str, target: str = "ar", source: str = "en"):
        self.api_key = api_key.strip()
        self.target = target
        self.source = source
        self.endpoint = (
            os.environ.get("GOOGLE_TRANSLATE_ENDPOINT")
            or "https://translation.googleapis.com/language/translate/v2"
        )

    def translate_many(self, texts: Sequence[str]) -> list:
        results: list = []
        for chunk in _chunks(list(texts), self.BATCH_ITEMS, self.BATCH_CHARS):
            payload = {
                "q": list(chunk),
                "source": self.source,
                "target": self.target,
                "format": "text",
            }
            url = f"{self.endpoint}?key={urllib.parse.quote(self.api_key)}"
            data = _with_retries(
                lambda: _post_json(url, payload, timeout=45), self.name
            )
            translations = (data.get("data") or {}).get("translations") or []
            if len(translations) != len(chunk):
                raise TranslationError(
                    f"Provider returned {len(translations)} translations for "
                    f"{len(chunk)} segments.",
                    provider=self.name,
                )
            results.extend(
                html_mod.unescape(str(item.get("translatedText", "")))
                for item in translations
            )
        return results


class DeepLTranslator(Translator):
    """DeepL API (free or pro endpoint), Arabic supported."""

    name = "deepl"
    BATCH_ITEMS = 50
    BATCH_CHARS = 25_000

    def __init__(self, api_key: str, target: str = "AR", source: str = "EN"):
        self.api_key = api_key.strip()
        self.target = target
        self.source = source
        self.endpoint = (
            os.environ.get("DEEPL_API_URL")
            or "https://api-free.deepl.com/v2/translate"
        )

    def translate_many(self, texts: Sequence[str]) -> list:
        results: list = []
        for chunk in _chunks(list(texts), self.BATCH_ITEMS, self.BATCH_CHARS):
            payload = {
                "text": list(chunk),
                "target_lang": self.target,
                "source_lang": self.source,
            }
            data = _with_retries(
                lambda: _post_json(
                    self.endpoint,
                    payload,
                    headers={"Authorization": f"DeepL-Auth-Key {self.api_key}"},
                    timeout=45,
                ),
                self.name,
            )
            translations = data.get("translations") or []
            if len(translations) != len(chunk):
                raise TranslationError(
                    f"Provider returned {len(translations)} translations for "
                    f"{len(chunk)} segments.",
                    provider=self.name,
                )
            results.extend(str(item.get("text", "")) for item in translations)
        return results


class GoogleFreeTranslator(Translator):
    """Free web endpoint via deep-translator. No key, heavily rate-limited."""

    name = "google_free"

    def __init__(self, target: str = "ar", source: str = "en"):
        self.target = target
        self.source = source
        self._client = None
        self._cache: dict = {}

    def _get_client(self):
        if self._client is None:
            from deep_translator import GoogleTranslator

            self._client = GoogleTranslator(source=self.source, target=self.target)
        return self._client

    def translate_many(self, texts: Sequence[str]) -> list:
        client = self._get_client()
        out = []
        for text in texts:
            if text in self._cache:
                out.append(self._cache[text])
                continue
            translated = self._translate_one(client, text)
            self._cache[text] = translated
            out.append(translated)
        return out

    def _translate_one(self, client, text: str) -> str:
        last = None
        for attempt in range(3):
            try:
                result = client.translate(text[:MAX_SEGMENT_CHARS])
                if result and result.strip():
                    return result.strip()
                last = "empty result"
            except Exception as exc:  # deep-translator raises a wide range
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(1.0 + attempt * 1.5)
        raise TranslationError(
            f"Free Google endpoint rejected the request ({last}). "
            "This normally means rate limiting from a datacenter IP; "
            "configure GOOGLE_TRANSLATE_API_KEY or DEEPL_API_KEY.",
            provider=self.name,
        )


def build_translator(env: Optional[dict] = None) -> Translator:
    env = env if env is not None else os.environ
    provider = (env.get("TRANSLATOR_PROVIDER") or "auto").strip().lower()
    target = (env.get("TRANSLATOR_TARGET") or "ar").strip() or "ar"

    def has(key: str) -> Optional[str]:
        value = env.get(key)
        return value.strip() if value and value.strip() else None

    if provider in ("auto", ""):
        if has("GOOGLE_TRANSLATE_API_KEY"):
            provider = "google_cloud"
        elif has("DEEPL_API_KEY"):
            provider = "deepl"
        else:
            provider = "google_free"

    if provider == "google_cloud":
        key = has("GOOGLE_TRANSLATE_API_KEY")
        if not key:
            raise TranslationError(
                "TRANSLATOR_PROVIDER=google_cloud but GOOGLE_TRANSLATE_API_KEY is not set.",
                provider="google_cloud",
            )
        return GoogleCloudTranslator(key, target=target)
    if provider == "deepl":
        key = has("DEEPL_API_KEY")
        if not key:
            raise TranslationError(
                "TRANSLATOR_PROVIDER=deepl but DEEPL_API_KEY is not set.",
                provider="deepl",
            )
        return DeepLTranslator(key)
    if provider == "google_free":
        return GoogleFreeTranslator(target=target)
    raise TranslationError(f"Unknown TRANSLATOR_PROVIDER '{provider}'.", provider=provider)


def translator_status(env: Optional[dict] = None) -> dict:
    env = env if env is not None else os.environ
    try:
        translator = build_translator(env)
        return translator.describe()
    except TranslationError as exc:
        return {"provider": "unconfigured", "error": str(exc)}


# ---------------------------------------------------------------------------
# PDF analysis
# ---------------------------------------------------------------------------

_BULLETS = "•◦▪▫‣·-–—*"
_WS_RE = re.compile(r"\s+")


@dataclass
class TextBlock:
    rect: "fitz.Rect"
    text: str
    size: float
    color: tuple
    bold: bool
    align: str = "right"
    bullet: str = ""
    min_size: float = MIN_FONT_SIZE
    # Rect used to delete the English text; always the original block box so an
    # expanded drawing box can never wipe a neighbouring cell.
    redact_rect: object = None


@dataclass
class PagePlan:
    index: int
    blocks: list = field(default_factory=list)
    other_text_blocks: int = 0

    @property
    def translatable(self) -> int:
        return len(self.blocks)


def _int_to_rgb(value) -> tuple:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return (0, 0, 0)
    return ((value >> 16) & 255, (value >> 8) & 255, value & 255)


def _is_translatable(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    latin = sum(1 for ch in letters if ch.isascii())
    return latin / len(letters) >= 0.6


def _span_texts(block) -> list:
    lines = []
    for line in block.get("lines", []):
        parts = []
        for span in line.get("spans", []):
            value = span.get("text", "")
            if value:
                parts.append(value)
        if parts:
            lines.append(_WS_RE.sub(" ", "".join(parts)).strip())
    return [line for line in lines if line]


def _block_properties(block) -> tuple:
    sizes, colors, bold = [], [], False
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            text = span.get("text", "")
            if not text.strip():
                continue
            try:
                sizes.append(float(span.get("size", 0)) or 0.0)
            except (TypeError, ValueError):
                pass
            colors.append(_int_to_rgb(span.get("color", 0)))
            try:
                if int(span.get("flags", 0)) & 16:  # PyMuPDF bold flag
                    bold = True
            except (TypeError, ValueError):
                pass
    size = max(sizes) if sizes else 11.0
    color = max(set(colors), key=colors.count) if colors else (0, 0, 0)
    return size, color, bold


def _line_text(line) -> str:
    parts = [
        span.get("text", "")
        for span in line.get("spans", [])
        if span.get("text", "")
    ]
    return _WS_RE.sub(" ", "".join(parts)).strip()


def _line_properties(line) -> tuple:
    sizes, colors, bold = [], [], False
    for span in line.get("spans", []):
        if not span.get("text", "").strip():
            continue
        try:
            sizes.append(float(span.get("size", 0)) or 0.0)
        except (TypeError, ValueError):
            pass
        colors.append(_int_to_rgb(span.get("color", 0)))
        try:
            if int(span.get("flags", 0)) & 16:
                bold = True
        except (TypeError, ValueError):
            pass
    size = max(sizes) if sizes else 11.0
    color = max(set(colors), key=colors.count) if colors else (0, 0, 0)
    return size, color, bold


def _block_segments(block) -> list:
    """Split a block into pieces: one per bullet line, else the whole block."""
    lines = [line for line in block.get("lines", []) if line.get("bbox")]
    pairs = [(line, _line_text(line)) for line in lines]
    pairs = [(line, text) for line, text in pairs if text]
    if not pairs:
        return []

    bullets = [text for _, text in pairs if text[0] in _BULLETS]
    if len(pairs) >= 2 and len(bullets) == len(pairs):
        segments = []
        for line, text in pairs:
            box = fitz.Rect(line["bbox"])
            box = fitz.Rect(box.x0, box.y0 - 1.5, box.x1, box.y1 + 1.5)
            size, color, bold = _line_properties(line)
            segments.append(
                (box, text, size, color, bold, [(box.x0 + box.x1) / 2])
            )
        return segments

    lines_text = [text for _, text in pairs]
    text = ""
    for line in lines_text:
        if not text:
            text = line
        elif text.endswith("-"):
            text = text[:-1] + line
        else:
            text += " " + line
    text = _WS_RE.sub(" ", text).strip()
    size, color, bold = _block_properties(block)
    line_centers = [
        (fitz.Rect(line["bbox"]).x0 + fitz.Rect(line["bbox"]).x1) / 2
        for line in lines
    ]
    return [(fitz.Rect(block["bbox"]), text, size, color, bold, line_centers)]


def _target_rect(rect, above, below, page) -> "fitz.Rect":
    # Room for taller Arabic text, capped so neighbouring boxes never collide.
    top_room = min(4.0, max(0.0, (rect.y0 - above.y1) / 2)) if above else 4.0
    bottom_room = min(4.0, max(0.0, (below.y0 - rect.y1) / 2)) if below else 4.0
    target = fitz.Rect(
        rect.x0,
        max(page.rect.y0, rect.y0 - top_room),
        min(page.rect.x1, rect.x1),
        min(page.rect.y1, rect.y1 + bottom_room),
    )
    # Narrow header/footer boxes get some sideways room so a short Arabic label
    # is not crushed. Restricted to the header/footer bands: expanding a table
    # cell could overlap the neighbouring column.
    in_margin_band = (
        rect.y1 < page.rect.height * 0.12 or rect.y0 > page.rect.height * 0.88
    )
    if rect.width < 120 and in_margin_band:
        extra = min(120 - rect.width, 70)
        page_center = (page.rect.x0 + page.rect.x1) / 2
        if (rect.x0 + rect.x1) / 2 >= page_center:
            target.x0 = max(page.rect.x0 + 18, target.x0 - extra)
        else:
            target.x1 = min(page.rect.x1 - 18, target.x1 + extra)
    return target


def analyze_page(page, page_index: int) -> PagePlan:
    plan = PagePlan(index=page_index)
    data = page.get_text("dict")
    raw = []
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for segment in _block_segments(block):
            rect, text, size, color, bold, line_centers = segment
            if len(text) < 3 or not _is_translatable(text):
                plan.other_text_blocks += 1
                continue
            raw.append((rect, text, size, color, bold, line_centers))
    if not raw:
        return plan

    raw.sort(key=lambda item: (round(item[0].y0, 1), item[0].x0))
    page_width = page.rect.width
    for index, (rect, text, size, color, bold, line_centers) in enumerate(raw):
        above = raw[index - 1][0] if index > 0 else None
        below = raw[index + 1][0] if index + 1 < len(raw) else None
        target = _target_rect(rect, above, below, page)

        align = "right"
        if rect.width < page_width * 0.8 and line_centers:
            block_center = (rect.x0 + rect.x1) / 2
            if all(abs(center - block_center) < 6 for center in line_centers):
                align = "center"

        bullet = ""
        stripped = text
        if text and text[0] in _BULLETS:
            bullet = "•"
            stripped = text[1:].strip()
            if not stripped:
                continue

        size = max(5.2, min(size, 26.0))
        plan.blocks.append(
            TextBlock(
                rect=target,
                redact_rect=rect,
                text=stripped,
                size=size,
                color=color,
                bold=bold,
                align=align,
                bullet=bullet,
                min_size=MIN_FONT_SIZE if size >= 9 else 5.2,
            )
        )
    return plan


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _block_html(block: TextBlock, size: float) -> str:
    r, g, b = block.color
    text = html_mod.escape((block.bullet + " " if block.bullet else "") + block.text)
    weight = "bold" if block.bold else "normal"
    return (
        f'<div dir="rtl" style="font-size:{size:.2f}pt;'
        f"color:rgb({r},{g},{b});text-align:{block.align};"
        f'font-weight:{weight};">{text}</div>'
    )


def _measure(html: str, css: str, archive, rect) -> Optional[float]:
    """Return the used height when the content fits, otherwise None."""
    try:
        story = fitz.Story(html=html, user_css=css, archive=archive)
        more, filled = story.place(rect)
    except Exception:
        return None
    if more != 0:
        return None
    filled = fitz.Rect(filled)
    height = filled.y1 - filled.y0
    if height > rect.height + 0.75:
        return None
    return height


def _fit_and_draw(page, block: TextBlock, css: str, archive) -> tuple:
    """Pick the largest font size that still fits. Returns (size, fitted)."""
    sizes = [block.size]
    size = block.size
    while size > block.min_size:
        size = max(block.min_size, size - 0.5)
        sizes.append(size)
    for candidate in sizes:
        html = _block_html(block, candidate)
        if _measure(html, css, archive, block.rect) is not None:
            page.insert_htmlbox(
                block.rect, html, css=css, archive=archive,
                scale_low=0.9, overlay=True,
            )
            return candidate, True
    # Nothing fit cleanly: draw at the minimum size and let MuPDF compress it.
    page.insert_htmlbox(
        block.rect,
        _block_html(block, block.min_size),
        css=css,
        archive=archive,
        scale_low=0.55,
        overlay=True,
    )
    return block.min_size, False


def _delete_file(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@dataclass
class ProcessResult:
    path: str
    source_pages: int
    output_pages: int
    translated_blocks: int
    skipped_blocks: int
    unfitted_blocks: int
    provider: str
    seconds: float
    warnings: list = field(default_factory=list)


def process_pdf(
    input_path: str,
    title: Optional[str] = None,
    translator: Optional[Translator] = None,
    max_pages: Optional[int] = None,
    font_dir: Optional[str] = None,
) -> ProcessResult:
    started = time.time()
    limit = max_pages or DEFAULT_MAX_PAGES
    limit = max(1, min(int(limit), MAX_PAGES_HARD_LIMIT))

    if not os.path.isfile(input_path):
        raise InputError("The uploaded PDF could not be found.")
    with open(input_path, "rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise InputError("The uploaded file is not a valid PDF.")

    translator = translator or build_translator()
    fonts = ensure_fonts(font_dir)
    css = _css(fonts)
    archive = fitz.Archive(os.path.dirname(fonts[FONT_REGULAR]))

    src_doc = fitz.open(input_path)
    out_doc = fitz.open()
    output_path = os.path.join(
        os.environ.get("TMPDIR") or os.path.dirname(input_path) or ".",
        f"bilingual_{uuid.uuid4().hex}.pdf",
    )

    try:
        if src_doc.is_encrypted:
            raise InputError("Password-protected PDFs are not supported.")
        if src_doc.page_count == 0:
            raise InputError("The uploaded PDF has no pages.")
        if src_doc.page_count > limit:
            raise InputError(
                f"PDF has {src_doc.page_count} pages; the limit is {limit} "
                f"pages per document."
            )

        plans = [analyze_page(src_doc[i], i) for i in range(src_doc.page_count)]
        texts = [block.text for plan in plans for block in plan.blocks]
        if not texts:
            raise InputError(
                "No selectable text was found. Scanned/image-only PDFs need OCR "
                "before they can be translated."
            )

        translated = translator.translate_many(texts)
        if len(translated) != len(texts):
            raise TranslationError(
                f"Provider returned {len(translated)} segments for {len(texts)} blocks.",
                provider=translator.name,
            )

        warnings: list = []
        cursor = 0
        unfitted = 0
        skipped = sum(plan.other_text_blocks for plan in plans)

        for page_index, plan in enumerate(plans):
            out_doc.insert_pdf(src_doc, from_page=page_index, to_page=page_index)
            out_doc.insert_pdf(src_doc, from_page=page_index, to_page=page_index)
            arabic_page = out_doc[-1]

            page_translations = []
            for block in plan.blocks:
                page_translations.append((block, translated[cursor]))
                cursor += 1
            if not page_translations:
                warnings.append(f"page {page_index + 1}: no translatable text")
                continue

            for block, _ in page_translations:
                arabic_page.add_redact_annot(
                    block.redact_rect or block.rect, fill=False
                )
            arabic_page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0),
            )

            for block, arabic_text in page_translations:
                if not arabic_text.strip():
                    arabic_text = block.text
                block.text = arabic_text.strip()
                _, fitted = _fit_and_draw(arabic_page, block, css, archive)
                if not fitted:
                    unfitted += 1

        expected = src_doc.page_count * 2
        if out_doc.page_count != expected:
            raise LayoutError(
                f"Output page count {out_doc.page_count} != expected {expected}."
            )

        metadata = dict(src_doc.metadata or {})
        metadata["title"] = (
            f"{title or metadata.get('title') or 'Bilingual PDF'} \u2014 English/Arabic"
        )
        metadata["producer"] = f"Bilingual PDF Engine {VERSION}"
        out_doc.set_metadata(metadata)
        out_doc.save(output_path, garbage=4, deflate=True)
        if unfitted:
            warnings.append(
                f"{unfitted} block(s) needed more space than the original layout "
                "offered and were compressed; review those pages."
            )
        return ProcessResult(
            path=output_path,
            source_pages=src_doc.page_count,
            output_pages=out_doc.page_count,
            translated_blocks=len(texts),
            skipped_blocks=skipped,
            unfitted_blocks=unfitted,
            provider=translator.name,
            seconds=round(time.time() - started, 1),
            warnings=warnings[:8],
        )
    except Exception:
        _delete_file(output_path)
        raise
    finally:
        out_doc.close()
        src_doc.close()

# ============================================================================
# HTTP service (from engine/app.py)
# ============================================================================

import asyncio
import hmac
import json
import logging
import os
import tempfile
import time
import uuid
from typing import Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask


MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "20")) * 1024 * 1024
STARTED_AT = time.time()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("bilingual")

app = FastAPI(title="Bilingual PDF Translation Service", version=VERSION)
_SEMAPHORE: Optional[asyncio.Semaphore] = None


def _semaphore() -> asyncio.Semaphore:
    global _SEMAPHORE
    if _SEMAPHORE is None:
        _SEMAPHORE = asyncio.Semaphore(max(1, int(os.environ.get("MAX_CONCURRENCY", "1"))))
    return _SEMAPHORE


def _json_error(status: int, code: str, detail: str, request_id: str, **extra):
    payload = {
        "ok": False,
        "error": code,
        "detail": detail,
        "request_id": request_id,
        "version": VERSION,
    }
    payload.update(extra)
    return JSONResponse(payload, status_code=status)


def _authorised(request: Request) -> bool:
    secret = (os.environ.get("ENGINE_SECRET") or "").strip()
    if not secret:
        return True
    provided = (request.headers.get("x-engine-secret") or "").strip()
    if not provided:
        bearer = (request.headers.get("authorization") or "").strip()
        if bearer.lower().startswith("bearer "):
            provided = bearer[7:].strip()
    return bool(provided) and hmac.compare_digest(provided, secret)


def _delete_file(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


@app.get("/api")
async def root():
    return {
        "ok": True,
        "service": "bilingual-pdf-translation",
        "version": VERSION,
        "endpoints": {"health": "/health", "translate": "/translate", "ui": "/"},
    }


@app.get("/health")
async def health(test: int = 0):
    info = {
        "ok": True,
        "status": "ok",
        "version": VERSION,
        "uptime_seconds": int(time.time() - STARTED_AT),
        "translator": translator_status(),
        "max_pages": int(os.environ.get("MAX_PAGES", str(DEFAULT_MAX_PAGES))),
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "auth_required": bool((os.environ.get("ENGINE_SECRET") or "").strip()),
        "fonts": _font_report(),
    }
    if test:
        try:
            translator = build_translator()
            started = time.time()
            result = translator.translate_many(["Hello"])
            info["translation_test"] = {
                "ok": bool(result and result[0].strip()),
                "sample": result[0] if result else "",
                "seconds": round(time.time() - started, 2),
            }
        except Exception as exc:  # report, never crash the health check
            info["translation_test"] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    return info


def _font_report() -> dict:
    try:
        fonts = ensure_fonts(allow_download=False)
        return {"regular": True, "bold": bool(fonts.get(FONT_BOLD))}
    except Exception as exc:
        return {"regular": False, "error": str(exc)}


@app.post("/translate")
async def translate_endpoint(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(""),
):
    request_id = uuid.uuid4().hex[:10]
    started = time.time()

    if not _authorised(request):
        return _json_error(401, "unauthorised", "Missing or invalid engine secret.", request_id)

    name = (file.filename or "").lower()
    if not name.endswith(".pdf"):
        return _json_error(400, "not_a_pdf", "Please upload a .pdf file.", request_id)

    upload_path = os.path.join(tempfile.gettempdir(), f"upload_{uuid.uuid4().hex}.pdf")
    total = 0
    try:
        with open(upload_path, "wb") as destination:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    return _json_error(
                        413,
                        "too_large",
                        f"PDF exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.",
                        request_id,
                    )
                destination.write(chunk)
    finally:
        await file.close()

    if total == 0:
        _delete_file(upload_path)
        return _json_error(400, "empty", "The uploaded file is empty.", request_id)

    try:
        semaphore = _semaphore()
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=90)
        except asyncio.TimeoutError:
            return _json_error(
                503,
                "busy",
                "The service is busy. Try again in a minute.",
                request_id,
            )
        try:
            result = await asyncio.to_thread(
                process_pdf,
                upload_path,
                (title or "").strip() or None,
            )
        finally:
            semaphore.release()
    except InputError as exc:
        log.info("input rejected rid=%s: %s", request_id, exc)
        return _json_error(400, "invalid_document", str(exc), request_id)
    except TranslationError as exc:
        log.warning("translation failed rid=%s: %s", request_id, exc)
        return _json_error(
            502,
            "translation_failed",
            str(exc),
            request_id,
            provider=exc.provider or None,
            provider_status=exc.status,
        )
    except EngineError as exc:
        log.error("engine error rid=%s: %s", request_id, exc)
        return _json_error(500, "engine_error", str(exc), request_id)
    except Exception as exc:
        log.exception("unexpected failure rid=%s", request_id)
        return _json_error(
            500,
            "internal_error",
            f"{type(exc).__name__}: {exc}",
            request_id,
        )
    finally:
        _delete_file(upload_path)

    safe_title = "".join(
        ch for ch in ((title or "").strip() or "Bilingual_Lecture")
        if ch.isalnum() or ch in " -_"
    ).strip()[:80].replace(" ", "_") or "Bilingual_Lecture"

    log.info(
        "translated rid=%s pages=%s blocks=%s unfitted=%s provider=%s in %.1fs",
        request_id,
        result.output_pages,
        result.translated_blocks,
        result.unfitted_blocks,
        result.provider,
        time.time() - started,
    )

    headers = {
        "X-Engine-Version": VERSION,
        "X-Engine-Request-Id": request_id,
        "X-Engine-Provider": result.provider,
        "X-Engine-Source-Pages": str(result.source_pages),
        "X-Engine-Output-Pages": str(result.output_pages),
        "X-Engine-Warnings": json.dumps(result.warnings, ensure_ascii=True)[:900],
    }
    return FileResponse(
        path=result.path,
        media_type="application/pdf",
        filename=f"{safe_title}_EN-AR.pdf",
        headers=headers,
        background=BackgroundTask(_delete_file, result.path),
    )


# ---------------------------------------------------------------------------
# Optional Gradio UI (handy for manual testing; the API works without it)
# ---------------------------------------------------------------------------

def _build_ui():
    try:
        os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")
        import gradio as gr
    except Exception:  # pragma: no cover - only when gradio is absent
        return None

    def run(pdf_path, title):
        if not pdf_path:
            raise gr.Error("Upload a PDF first.")
        result = process_pdf(pdf_path, (title or "").strip() or None)
        note = " ".join(result.warnings)
        return result.path, note or f"{result.output_pages} pages written."

    with gr.Blocks(title="Bilingual PDF Translation Engine") as demo:
        gr.Markdown(
            "# Bilingual PDF Translation Engine\n"
            "English pages are copied untouched; each one is followed by an "
            "Arabic page built in the same layout."
        )
        with gr.Row():
            pdf_in = gr.File(label="English PDF", file_types=[".pdf"], type="filepath")
            title_in = gr.Textbox(label="Document title (optional)")
        run_btn = gr.Button("Translate", variant="primary")
        pdf_out = gr.File(label="Bilingual PDF")
        status = gr.Textbox(label="Status", interactive=False)
        run_btn.click(run, [pdf_in, title_in], [pdf_out, status])
    return demo


_UI = _build_ui()
if _UI is not None:
    try:
        import gradio as gr

        # Mounted last, so /health and /translate keep priority. The free
        # Gradio SDK Space serves this same process on port 7860.
        app = gr.mount_gradio_app(app, _UI, path="/", ssr_mode=False)
    except Exception as exc:  # pragma: no cover
        log.warning("Gradio UI disabled: %s", exc)


if __name__ == "__main__":
    import uvicorn

    # Hugging Face's Gradio SDK runs `python app.py` and expects port 7860 via
    # GRADIO_SERVER_PORT. Docker/Render use PORT. A non-numeric or zero port
    # (some sandboxes export PORT=0) must not silently bind a random port.
    raw_port = (
        os.environ.get("GRADIO_SERVER_PORT")
        or os.environ.get("PORT")
        or "7860"
    )
    try:
        port = int(raw_port)
    except ValueError:
        port = 7860
    if port <= 0:
        port = 7860
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
