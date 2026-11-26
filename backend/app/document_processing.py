"""PDF text extraction and optional, explicitly enabled Gemini vision OCR."""
import os
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image

from .provider_diagnostics import provider_error as diagnose_provider_error, response_json


MAX_DOCUMENT_PAGES = 30
MAX_PAGE_DIMENSION_POINTS = 1800
VISION_PROMPT = """Transcribe only words that are visibly written on this page. This is transcription, not grading: do not score, evaluate, correct, summarize, or infer an answer. Preserve question numbers where visible. If any writing is unclear, do not guess; leave that span empty and add a short uncertainty note. Return only JSON with this shape: {\"raw_text\": string, \"confidence\": \"high\"|\"medium\"|\"low\", \"uncertain_spans\": [string], \"question_answers\": [{\"question\": string, \"text\": string, \"confidence\": \"high\"|\"medium\"|\"low\", \"uncertain_spans\": [string]}]}. Include the visible student roll number, with its label, in raw_text when present. Do not create question answers when question boundaries are not visible."""


class DocumentProcessingError(Exception):
    """A document could not safely be opened or processed."""


class VisionProviderError(Exception):
    """The configured vision provider did not return a usable transcription."""


@dataclass
class VisionResult:
    raw_text: str
    confidence: str
    question_answers: list[dict[str, Any]]
    uncertain_spans: list[str]


class GeminiVisionProvider:
    def __init__(self, api_key: str, model: str):
        from google import genai
        from google.genai import types

        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=25_000, retry_options={"attempts": 1}))
        self.types = types
        self.model = model

    def transcribe_page(self, png_bytes: bytes) -> VisionResult:
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=[self.types.Part.from_text(text=VISION_PROMPT), self.types.Part.from_bytes(data=png_bytes, mime_type="image/png")],
                config=self.types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema={"type": "object", "properties": {
                        "raw_text": {"type": "string"}, "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "uncertain_spans": {"type": "array", "items": {"type": "string"}},
                        "question_answers": {"type": "array", "items": {"type": "object", "properties": {
                            "question": {"type": "string"}, "text": {"type": "string"},
                            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                            "uncertain_spans": {"type": "array", "items": {"type": "string"}},
                        }, "required": ["question", "text", "confidence", "uncertain_spans"]}},
                    }, "required": ["raw_text", "confidence", "uncertain_spans", "question_answers"]},
                    max_output_tokens=5000,
                ),
            )
            data = response_json(response)
            if not isinstance(data.get("raw_text"), str) or not data["raw_text"].strip():
                raise VisionProviderError("Gemini returned no readable transcription.")
            confidence = normalize_confidence(data.get("confidence"))
            answers = data.get("question_answers", [])
            if not isinstance(answers, list):
                answers = []
            safe_answers = []
            for answer in answers:
                if not isinstance(answer, dict):
                    continue
                safe_answers.append({
                    "question": str(answer.get("question", ""))[:40],
                    "raw_text": str(answer.get("text", ""))[:8000],
                    "confidence": normalize_confidence(answer.get("confidence")),
                    "uncertain_spans": safe_string_list(answer.get("uncertain_spans")),
                })
            return VisionResult(
                raw_text=str(data.get("raw_text", ""))[:20000],
                confidence=confidence,
                question_answers=safe_answers,
                uncertain_spans=safe_string_list(data.get("uncertain_spans")),
            )
        except VisionProviderError:
            raise
        except Exception as exc:
            message, retryable = diagnose_provider_error(exc)
            error = VisionProviderError("Vision transcription failed. " + message)
            error.retryable = retryable
            raise error from None


def safe_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:300] for item in value[:30] if str(item).strip()]


def normalize_confidence(value: Any) -> str:
    value = str(value).lower()
    return value if value in {"high", "medium", "low"} else "unknown"


def configured_vision_provider():
    """Only contact Gemini when explicitly selected and a server-side key exists."""
    provider = os.getenv("EVALAI_OCR_PROVIDER", "demo").strip().lower()
    api_key = os.getenv("GEMINI_API_KEY")
    if provider != "gemini":
        return None, "demo", "No vision provider selected; scanned handwriting was not transcribed."
    if not api_key:
        return None, "demo_no_api_key", "Gemini was selected but no API key is configured; scanned handwriting was not transcribed."
    model = os.getenv("EVALAI_VISION_MODEL", "gemini-3.8-flash").strip() or "gemini-3.8-flash"
    try:
        return GeminiVisionProvider(api_key, model), "gemini_vision", f"Gemini vision transcription via {model}; faculty verification is required."
    except Exception as exc:
        raise VisionProviderError("Could not initialize the configured vision provider.") from exc


def image_to_png(image_bytes: bytes) -> bytes:
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            image.seek(0)
            image.load()
            image.thumbnail((2400, 3200))
            output = BytesIO()
            image.convert("RGB").save(output, format="PNG", optimize=True)
            return output.getvalue()
    except Exception as exc:
        raise DocumentProcessingError("The image could not be decoded.") from exc


def render_pdf_page(page) -> bytes:
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
    return pixmap.tobytes("png")


def split_questions(text: str) -> list[dict[str, str]]:
    """Best-effort segmentation for visible question labels; never fabricate labels."""
    # Prefer explicit labels so a number at the start of a wrapped solution
    # line does not become a new question.
    matches = list(re.finditer(r"(?im)^[^\S\n]*(Q(?:uestion)?[^\S\n]*\d{1,2})\b[.) :\u00b7]*", text))
    if not matches:
        matches = list(re.finditer(r"(?m)^[^\S\n]*(\d{1,2}[.):])[^\S\n]+(?=\S)", text))
    if not matches:
        return []
    sections = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = text[match.end():end].strip()
        if content:
            sections.append({"question": match.group(1).strip(), "raw_text": content, "confidence": "unknown", "uncertain_spans": []})
    return sections


def process_document(file_path: Path, media_type: str, document_type: str, vision_provider=None, progress=None, cached_pages=None) -> dict[str, Any]:
    """Process every PDF page or one image, keeping page references and uncertainty."""
    is_pdf = media_type == "application/pdf" or file_path.suffix.lower() == ".pdf"
    pages = []
    provider_error = None
    cached_pages = cached_pages or {}

    def store(page, total):
        pages.append(page)
        if progress:
            progress(page, total)

    def transcribe(png):
        for attempt in range(2):
            try:
                return vision_provider.transcribe_page(png)
            except VisionProviderError as exc:
                if attempt or not getattr(exc, "retryable", False):
                    raise

    if is_pdf:
        try:
            pdf = pymupdf.open(file_path)
        except Exception as exc:
            raise DocumentProcessingError("This PDF could not be opened or is damaged.") from exc
        with pdf:
            if pdf.needs_pass:
                raise DocumentProcessingError("Unlock this password-protected PDF and re-upload it.")
            if pdf.page_count < 1 or pdf.page_count > MAX_DOCUMENT_PAGES:
                raise DocumentProcessingError(f"PDFs must contain between 1 and {MAX_DOCUMENT_PAGES} pages.")
            for page_index in range(pdf.page_count):
                if page_index + 1 in cached_pages:
                    pages.append(cached_pages[page_index + 1])
                    continue
                page = pdf.load_page(page_index)
                if page.rect.width > MAX_PAGE_DIMENSION_POINTS or page.rect.height > MAX_PAGE_DIMENSION_POINTS:
                    raise DocumentProcessingError("A PDF page is larger than the safe processing limit.")
                embedded_text = page.get_text("text").strip()
                sections = split_questions(embedded_text)
                if document_type in {"question_paper", "model_answer"}:
                    ordered_text = page.get_text("text", sort=True).strip()
                    ordered_sections = split_questions(ordered_text)
                    if len(ordered_sections) > len(sections):
                        embedded_text, sections = ordered_text, ordered_sections
                meaningful_text = bool(sections) or len(re.sub(r"[^\w]", "", embedded_text)) >= 30
                # A bare page number or an empty final page is not a scan.
                has_graphics = bool(page.get_image_info() or page.get_drawings()) if not meaningful_text else False
                blank = not meaningful_text and not has_graphics and bool(
                    not embedded_text or re.fullmatch(r"(?:page\s+)?\d{1,3}(?:\s*/\s*\d{1,3})?", embedded_text, re.I)
                )
                if not has_graphics and not blank:
                    meaningful_text = True
                use_vision = vision_provider is not None and not meaningful_text and not blank
                if use_vision:
                    try:
                        result = transcribe(render_pdf_page(page))
                        store({
                            "page_number": page_index + 1, "raw_text": result.raw_text,
                            "question_answers": result.question_answers, "source": "gemini_vision",
                            "confidence_label": result.confidence, "confidence_source": "model_self_assessment",
                            "needs_review": True,
                        }, pdf.page_count)
                        continue
                    except VisionProviderError as exc:
                        provider_error = provider_error or f"Page {page_index + 1}: {exc}"
                        if not getattr(exc, "retryable", False):
                            vision_provider = None
                    except (RuntimeError, ValueError):
                        provider_error = f"Page {page_index + 1} could not be rasterized. Re-export the PDF and retry."
                if meaningful_text:
                    store({
                        "page_number": page_index + 1, "raw_text": embedded_text[:20000],
                        "question_answers": sections, "source": "pdf_text",
                        "confidence_label": "not_applicable", "confidence_source": "embedded_pdf_text",
                        "needs_review": document_type == "answer_sheet",
                    }, pdf.page_count)
                else:
                    store({
                        "page_number": page_index + 1, "raw_text": "" if blank else embedded_text[:20000], "question_answers": [],
                        "source": "blank" if blank else "unavailable", "confidence_label": "unknown",
                        "confidence_source": "none", "needs_review": not blank,
                    }, pdf.page_count)
    else:
        png_bytes = image_to_png(file_path.read_bytes())
        if vision_provider is None:
            pages.append({"page_number": 1, "raw_text": "", "question_answers": [], "source": "unavailable",
                          "confidence_label": "unknown", "confidence_source": "none", "needs_review": True})
        else:
            try:
                result = transcribe(png_bytes)
                pages.append({"page_number": 1, "raw_text": result.raw_text, "question_answers": result.question_answers,
                              "source": "gemini_vision", "confidence_label": result.confidence,
                              "confidence_source": "model_self_assessment", "needs_review": True})
            except VisionProviderError as exc:
                provider_error = f"Page 1: {exc}"
                pages.append({"page_number": 1, "raw_text": "", "question_answers": [], "source": "unavailable",
                              "confidence_label": "unknown", "confidence_source": "none", "needs_review": True})

    if not is_pdf and progress:
        progress(pages[0], 1)
    any_extraction = any(page["raw_text"].strip() and page["source"] != "unavailable" for page in pages)
    all_available = all(page["source"] != "unavailable" for page in pages)
    if provider_error:
        status = "partial" if any_extraction else "failed"
        message = provider_error
    elif not any_extraction:
        status = "awaiting_provider"
        message = "No readable page text was extracted. Open the original and add a faculty transcription."
    elif all_available:
        status = "extracted"
        message = "Text extraction completed. Review every page before using it; no marks were generated."
    else:
        status = "partial"
        message = "Some pages contain extractable text and some require manual review or a vision provider. No missing text was invented."
    return {"status": status, "message": message, "pages": pages}
