"""Safe provider diagnostics shared by OCR and evaluation."""
import json

import httpx


class ProviderResponseError(ValueError):
    pass


def response_json(response, *, evaluation=False):
    candidates = getattr(response, "candidates", None) or []
    reason = getattr(candidates[0], "finish_reason", None) if candidates else None
    reason = getattr(reason, "value", reason)
    if reason == "MAX_TOKENS":
        raise ProviderResponseError("Output was truncated. Review the source or split the input.")
    if reason and reason != "STOP":
        raise ProviderResponseError("Generation was stopped by Gemini. Review the source manually.")
    try:
        result = json.loads(response.text)
    except (TypeError, ValueError, AttributeError):
        raise ProviderResponseError("Gemini returned empty or invalid JSON.") from None
    if evaluation and isinstance(result, list) and result and all(isinstance(item, dict) for item in result):
        result = {"question_results": result}
    if not isinstance(result, dict):
        raise ProviderResponseError("Gemini returned an unexpected JSON structure.")
    return result


def provider_error(exc):
    code = getattr(exc, "code", None)
    if isinstance(exc, ProviderResponseError):
        return str(exc), False
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)) or code in (408, 504):
        return "Gemini timed out. Retry or review the source manually.", True
    if code in (401, 403):
        return "Gemini rejected access. Check API key permissions and project restrictions.", False
    if code == 404:
        return "Gemini model unavailable. Check model ID and project access.", False
    if code == 429:
        return "Gemini quota or rate limit reached. Check project limits before retrying.", False
    if isinstance(code, int) and 500 <= code < 600:
        return f"Gemini is temporarily unavailable (HTTP {code}).", True
    if code == 400:
        return "Gemini rejected the request (HTTP 400). Check API key, model, and schema settings.", False
    if isinstance(exc, httpx.TransportError):
        return "Gemini connection failed. Check network and proxy settings.", True
    return "Gemini request failed. Check SDK and provider configuration.", False
