"""Provider adapter contract tests with a fake Google Gen AI client."""
import json
import sys
from types import ModuleType, SimpleNamespace

from app.document_processing import GeminiVisionProvider, VisionProviderError
from app.evaluation import GeminiEvaluationProvider
from app.schemas import EvaluationLLMResponse, EvaluationQuestionResult


def install_fake_genai(monkeypatch, response):
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            calls.append(("client", kwargs))
            self.models = SimpleNamespace(generate_content=self.generate_content)

        def generate_content(self, **kwargs):
            calls.append(("generate_content", kwargs))
            return response

    class FakePart:
        @staticmethod
        def from_text(text):
            return ("text", text)

        @staticmethod
        def from_bytes(data, mime_type):
            return ("image", data, mime_type)

    class FakeHttpOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    genai_module = ModuleType("google.genai")
    genai_module.Client = FakeClient
    types_module = ModuleType("google.genai.types")
    types_module.Part = FakePart
    types_module.HttpOptions = FakeHttpOptions
    types_module.GenerateContentConfig = FakeConfig
    genai_module.types = types_module
    google_module = ModuleType("google")
    google_module.genai = genai_module
    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.genai", genai_module)
    monkeypatch.setitem(sys.modules, "google.genai.types", types_module)
    return calls


def test_gemini_vision_sends_image_and_parses_transcription(monkeypatch):
    result_json = {
        "raw_text": "Q1. Handwritten answer", "confidence": "medium",
        "uncertain_spans": ["last word"],
        "question_answers": [{"question": "Q1", "text": "Handwritten answer", "confidence": "medium", "uncertain_spans": []}],
    }
    calls = install_fake_genai(monkeypatch, SimpleNamespace(text=json.dumps(result_json)))
    provider = GeminiVisionProvider("fake-server-key", "gemini-3.8-flash")
    result = provider.transcribe_page(b"png bytes")

    assert result.raw_text == "Q1. Handwritten answer"
    assert result.question_answers[0]["raw_text"] == "Handwritten answer"
    request = next(args for name, args in calls if name == "generate_content")
    assert request["contents"][1] == ("image", b"png bytes", "image/png")
    assert request["config"].kwargs["response_mime_type"] == "application/json"
    assert request["config"].kwargs["response_json_schema"]["required"]


def test_gemini_vision_rejects_invalid_json_without_revealing_provider_details(monkeypatch):
    install_fake_genai(monkeypatch, SimpleNamespace(text="not json"))
    provider = GeminiVisionProvider("fake-server-key", "gemini-3.8-flash")
    try:
        provider.transcribe_page(b"png bytes")
    except VisionProviderError as exc:
        assert "transcription failed" in str(exc).lower()
    else:
        raise AssertionError("invalid provider output should fail safely")


def test_gemini_evaluation_requests_schema_and_validates_result(monkeypatch):
    answer = EvaluationLLMResponse(question_results=[EvaluationQuestionResult(
        question_id="Q1", maximum_marks=4, proposed_marks=3, reasoning="Meets most criteria.",
        strengths=["Correct concept"], improvement_areas=["Explain one step"],
        uncertainty_flags=["faculty_review_required"], requires_faculty_review=True,
    )])
    calls = install_fake_genai(monkeypatch, SimpleNamespace(text=answer.model_dump_json()))
    provider = GeminiEvaluationProvider("fake-server-key", "gemini-3.8-flash")
    result = provider.evaluate([{"question_id": "Q1"}], [], "")

    assert result.question_results[0].proposed_marks == 3
    request = next(args for name, args in calls if name == "generate_content")
    assert request["config"].kwargs["response_mime_type"] == "application/json"
    from app.evaluation import GEMINI_EVALUATION_SCHEMA
    assert request["config"].kwargs["response_json_schema"] == GEMINI_EVALUATION_SCHEMA


def test_gemini_vision_retries_a_timeout_and_sanitizes_provider_failures(monkeypatch, tmp_path):
    import httpx
    from PIL import Image
    from app.document_processing import process_document

    path = tmp_path / "synthetic.png"
    Image.new("RGB", (40, 40), "white").save(path)
    install_fake_genai(monkeypatch, None)
    provider = GeminiVisionProvider("fake-server-key", "gemini-3.8-flash")
    attempts = []

    def send(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise httpx.ReadTimeout("private-request-data")
        return SimpleNamespace(text=json.dumps({
            "raw_text": "Roll No: 269021001\nQ1. Synthetic answer.",
            "confidence": "high", "uncertain_spans": [], "question_answers": [],
        }))

    provider.client.models.generate_content = send
    result = process_document(path, "image/png", "answer_sheet", provider)
    assert result["status"] == "extracted"
    assert len(attempts) == 2

    def reject(**_):
        attempts.append(None)
        failure = RuntimeError("fake-server-key and private-request-data")
        failure.code = 401
        raise failure

    provider.client.models.generate_content = reject
    result = process_document(path, "image/png", "answer_sheet", provider)
    assert result["status"] == "failed"
    assert len(attempts) == 3
    assert "Check API key permissions" in result["message"]
    assert "fake-server-key" not in result["message"]
    assert "private-request-data" not in result["message"]
