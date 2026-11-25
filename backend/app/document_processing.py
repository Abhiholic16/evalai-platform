"""PDF text extraction and optional, explicitly enabled OpenAI vision OCR."""
import base64
import json
import os
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image


MAX_DOCUMENT_PAGES = 30
MAX_PAGE_DIMENSION_POINTS = 1800
VISION_PROMPT = """Transcribe only words that are visibly written on this page. This is transcription, not grading: do not score, evaluate, correct, summarize, or infer an answer. Preserve question numbers where visible. If any writing is unclear, do not guess; leave that span empty and add a short uncertainty note. Return only JSON with this shape: {\"raw_text\": string, \"confidence\": \"high\"|\"medium\"|\"low\", \"uncertain_spans\": [string], \"question_answers\": [{\"question\": string, \"text\": string, \"confidence\": \"high\"|\"medium\"|\"low\", \"uncertain_spans\": [string]}]}. Do not create question answers when question boundaries are not visible."""


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


class OpenAIVisionProvider:
    def __init__(self, api_key: str, model: str):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, timeout=90.0, max_retries=1)
        self.model = model

    def transcribe_page(self, png_bytes: bytes) -> VisionResult:
        image_data = base64.b64encode(png_bytes).decode("ascii")
        try:
            response = self.client.responses.create(
                model=self.model,
                store=False,
                max_output_tokens=1800,
                input=[{
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": VISION_PROMPT},
                        {"type": "input_image", "image_url": f"data:image/png;base64,{image_data}", "detail": "high"},
                    ],
                }],
            )
            data = json.loads(response.output_text)
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
        except Exception as exc:
            raise VisionProviderError("Vision transcription failed. Check the provider settings and retry.") from exc


def safe_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:300] for item in value[:30] if str(item).strip()]


def normalize_confidence(value: Any) -> str:
    value = str(value).lower()
    return value if value in {"high", "medium", "low"} else "unknown"


def configured_vision_provider():
    """Only contact OpenAI when both explicit provider selection and a key exist."""
    provider = os.getenv("EVALAI_OCR_PROVIDER", "demo").strip().lower()
    api_key = os.getenv("OPENAI_API_KEY")
    if provider != "openai":
        return None, "demo", "No vision provider selected; scanned handwriting was not transcribed."
    if not api_key:
        return None, "demo_no_api_key", "OpenAI was selected but no API key is configured; scanned handwriting was not transcribed."
    model = os.getenv("EVALAI_VISION_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
    try:
        return OpenAIVisionProvider(api_key, model), "openai_vision", f"OpenAI vision transcription via {model}; faculty verification is required."
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
    matches = list(re.finditer(r"(?m)^\s*((?:Q(?:uestion)?\s*)?\d{1,2}[.) :])\s*", text, re.IGNORECASE))
    if not matches:
        return []
    sections = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = text[match.end():end].strip()
        if content:
            sections.append({"question": match.group(1).strip(), "raw_text": content, "confidence": "unknown", "uncertain_spans": []})
    return sections


def process_document(file_path: Path, media_type: str, document_type: str, vision_provider=None) -> dict[str, Any]:
    """Process every PDF page or one image, keeping page references and uncertainty."""
    is_pdf = media_type == "application/pdf" or file_path.suffix.lower() == ".pdf"
    pages = []
    provider_error = None
    if is_pdf:
        try:
            pdf = pymupdf.open(file_path)
        except Exception as exc:
            raise DocumentProcessingError("This PDF could not be opened or is damaged.") from exc
        with pdf:
            if pdf.page_count < 1 or pdf.page_count > MAX_DOCUMENT_PAGES:
                raise DocumentProcessingError(f"PDFs must contain between 1 and {MAX_DOCUMENT_PAGES} pages.")
            for page_index in range(pdf.page_count):
                page = pdf.load_page(page_index)
                if page.rect.width > MAX_PAGE_DIMENSION_POINTS or page.rect.height > MAX_PAGE_DIMENSION_POINTS:
                    raise DocumentProcessingError("A PDF page is larger than the safe processing limit.")
                embedded_text = page.get_text("text").strip()
                use_vision = vision_provider is not None and (document_type == "answer_sheet" or len(embedded_text) < 30)
                if use_vision:
                    try:
                        result = vision_provider.transcribe_page(render_pdf_page(page))
                        pages.append({
                            "page_number": page_index + 1, "raw_text": result.raw_text,
                            "question_answers": result.question_answers, "source": "openai_vision",
                            "confidence_label": result.confidence, "confidence_source": "model_self_assessment",
                            "needs_review": True,
                        })
                        continue
                    except VisionProviderError:
                        provider_error = "Vision provider failed on one or more pages; inspect the source document manually."
                if embedded_text:
                    pages.append({
                        "page_number": page_index + 1, "raw_text": embedded_text[:20000],
                        "question_answers": split_questions(embedded_text), "source": "pdf_text",
                        "confidence_label": "not_applicable", "confidence_source": "embedded_pdf_text",
                        "needs_review": document_type == "answer_sheet",
                    })
                else:
                    pages.append({
                        "page_number": page_index + 1, "raw_text": "", "question_answers": [],
                        "source": "unavailable", "confidence_label": "unknown",
                        "confidence_source": "none", "needs_review": True,
                    })
    else:
        png_bytes = image_to_png(file_path.read_bytes())
        if vision_provider is None:
            pages.append({"page_number": 1, "raw_text": "", "question_answers": [], "source": "unavailable",
                          "confidence_label": "unknown", "confidence_source": "none", "needs_review": True})
        else:
            try:
                result = vision_provider.transcribe_page(png_bytes)
                pages.append({"page_number": 1, "raw_text": result.raw_text, "question_answers": result.question_answers,
                              "source": "openai_vision", "confidence_label": result.confidence,
                              "confidence_source": "model_self_assessment", "needs_review": True})
            except VisionProviderError:
                provider_error = "Vision provider failed; inspect the source image manually."
                pages.append({"page_number": 1, "raw_text": "", "question_answers": [], "source": "unavailable",
                              "confidence_label": "unknown", "confidence_source": "none", "needs_review": True})

    any_extraction = any(page["raw_text"].strip() for page in pages)
    all_available = all(page["source"] != "unavailable" for page in pages)
    if provider_error:
        status = "partial" if any_extraction else "failed"
        message = provider_error
    elif not any_extraction:
        status = "awaiting_provider"
        message = "No text was extracted. No OCR provider is configured; no answer text was invented. Configure a vision provider or transcribe manually."
    elif all_available:
        status = "extracted"
        message = "Text extraction completed. Review every page before using it; no marks were generated."
    else:
        status = "partial"
        message = "Some pages contain extractable text and some require manual review or a vision provider. No missing text was invented."
    return {"status": status, "message": message, "pages": pages}
