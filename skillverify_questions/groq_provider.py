"""Groq as a model provider for the question writer. Same ``generate_json`` interface as ``GeminiProvider``.

Groq's API is OpenAI-compatible: POST /openai/v1/chat/completions with ``Authorization: Bearer <key>``.
The default model id ``openai/gpt-oss-120b`` is listed in Groq's model documentation
(https://console.groq.com/docs/models and https://console.groq.com/docs/model/openai/gpt-oss-120b, which list JSON
Object Mode for it). That was read from the documentation, not checked with a live call: model names change, so
set ``QUESTIONS_MODEL_CHAIN`` if Groq renames or retires it.

Outcomes are mapped like Gemini's, and the messages never contain the key, the request or the reply text:
  * HTTP 429                    -> ``GroqRateLimited`` ("HTTP 429", with ``retry_after`` seconds when Groq sent them)
  * 5xx, timeout, network error -> a temporary failure ("HTTP 5xx" / "Network problem ...")
  * HTTP 401 / 403              -> ``GroqKeyRejected``: Groq is unusable, the caller moves on to another provider
  * HTTP 413, or a reply saying the request is too large -> ``GroqTooLarge``: this request does not fit Groq's
    per-minute token limit; the caller tries the next entry for this one request
  * an empty reply or a reply that is not a JSON object -> the same "not valid JSON" / "no usable answer" messages
    Gemini uses, so the writer counts them as ``bad_json`` and applies its one-repair rule. The reply is read
    tolerantly (code fences and text around the object are removed, see ``answer.py``) and an unusable reply carries a
    closed ``cause``: ``empty_response``, ``cut_off`` (``finish_reason`` is ``length``, or the braces never balance),
    ``not_json`` or ``wrong_shape``.

The request body is: ``model``, ``messages``, ``temperature`` 0 and ``response_format`` ``json_object`` (JSON mode). When
``max_output_tokens`` is set (``QUESTIONS_MAX_OUTPUT_TOKENS``) it is sent as ``max_completion_tokens``, Groq's documented
output cap (reasoning tokens of a reasoning model count against it). Unset, nothing is sent and Groq's own default
applies, exactly as before. No reasoning setting is sent.
"""
import os
import re
from typing import Any, Optional

import requests
from dotenv import load_dotenv

from skillverify.jd.provider import REQUEST_TIMEOUT_SECONDS, LLMCallError, LLMConfigError, LLMProvider
from skillverify_questions import answer as answer_text

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_MODEL_NAME = re.compile(r"[A-Za-z0-9._/-]{1,100}")  # Groq ids can contain a slash (owner/model)
TEMPERATURE = 0  # the same low temperature the Gemini writer uses
_TOO_LARGE_TEXT = re.compile(r"request too large|reduce your message size|too large for model", re.IGNORECASE)


class GroqRateLimited(LLMCallError):
    """HTTP 429. ``retry_after`` is the wait Groq asked for, in seconds, or None."""

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class GroqKeyRejected(LLMCallError):
    """HTTP 401 or 403: the key is missing its rights or wrong. Groq cannot be used."""


class GroqTooLarge(LLMCallError):
    """This one request is too big for Groq's limits (HTTP 413 or a "request too large" reply)."""


def parse_retry_after(value: Any) -> Optional[float]:
    """Seconds from a ``Retry-After`` header; None when it is missing, not a number or not positive."""
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


class GroqProvider(LLMProvider):
    """Groq over its REST API (just ``requests``, like the Gemini provider)."""

    name = "groq"

    def __init__(self, model: Optional[str] = None, timeout: float = REQUEST_TIMEOUT_SECONDS,
                 max_output_tokens: Optional[int] = None) -> None:
        load_dotenv()  # real environment variables win over the .env file
        key = os.environ.get("GROQ_API_KEY", "")
        if not key.strip():
            raise LLMConfigError("GROQ_API_KEY is not set.")
        chosen = (model or DEFAULT_GROQ_MODEL).strip()
        if GROQ_MODEL_NAME.fullmatch(chosen) is None:
            raise LLMConfigError(f"Invalid Groq model name: {chosen[:60]!r}.")
        self._api_key = key.strip()
        self.model = chosen
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens

    def set_max_output_tokens(self, tokens: Optional[int]) -> None:
        """The output cap to send (None: send nothing). Set by the caller from ``QUESTIONS_MAX_OUTPUT_TOKENS``."""
        self.max_output_tokens = tokens

    def __repr__(self) -> str:  # never include the key
        return f"GroqProvider(model={self.model!r})"

    def generate_json(self, system_prompt: str, user_prompt: str) -> dict:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            "temperature": TEMPERATURE,
            "response_format": {"type": "json_object"},
        }
        if self.max_output_tokens:
            body["max_completion_tokens"] = self.max_output_tokens
        try:
            response = requests.post(GROQ_URL, json=body, timeout=self.timeout,
                                     headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"})
        except requests.RequestException as exc:
            # Only the exception type: its text could contain the request URL or details.
            raise LLMCallError(f"Network problem while calling Groq ({type(exc).__name__}).") from None
        self._check_status(response)
        return self._parse_answer(response)

    @staticmethod
    def _says_too_large(response: requests.Response) -> bool:
        try:
            data = response.json()
            text = str(data["error"]["message"])
        except (ValueError, KeyError, TypeError):
            return False
        return _TOO_LARGE_TEXT.search(text) is not None

    def _check_status(self, response: requests.Response) -> None:
        status = response.status_code
        if status == 200:
            return
        if status in (401, 403):
            raise GroqKeyRejected(f"Groq rejected the API key (HTTP {status}). Check GROQ_API_KEY.")
        if status == 413 or (status in (400, 429) and self._says_too_large(response)):
            raise GroqTooLarge("Groq request too large for this model (HTTP 413).")
        if status == 429:
            raise GroqRateLimited("Groq rate limit reached (HTTP 429).",
                                  parse_retry_after(response.headers.get("Retry-After")))
        raise LLMCallError(f"Groq returned HTTP {status}.")

    @staticmethod
    def _parse_answer(response: requests.Response) -> dict:
        try:
            data: Any = response.json()
        except ValueError:
            raise answer_text.AnswerProblem("Groq sent a response that is not valid JSON.", answer_text.NOT_JSON) from None
        text: Any = None
        truncated = False
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
            truncated = choice.get("finish_reason") == "length"
        except (KeyError, IndexError, TypeError, AttributeError):
            pass
        return answer_text.parse_answer(text, "Groq", truncated)
