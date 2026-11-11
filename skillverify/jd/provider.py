"""LLM provider interface and the Google Gemini connector (SkillVerify, Step 3).

The JD parser only needs one thing from a language model: "take these two prompts
and give me back a JSON object". ``LLMProvider`` captures that, so another model
can be plugged in later by writing one small class.

The API key is read from the environment (``GEMINI_API_KEY``, optionally via a
local ``.env`` file). It is sent in a request header, never in the URL, and it
never appears in an exception message, ``repr`` or any output.
"""
from __future__ import annotations

import json
import os
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

import requests
from dotenv import load_dotenv

GEMINI_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"  # model names change; override with GEMINI_MODEL
REQUEST_TIMEOUT_SECONDS = 60
# Temporary errors (these HTTP statuses, or a network error/timeout) are retried at most twice,
# waiting this many seconds before each retry: 3 attempts in total. 429 is never retried.
RETRY_DELAYS_SECONDS = (2, 6)
RETRYABLE_STATUSES = (500, 502, 503, 504)
_sleep = time.sleep  # a module-level reference, so tests can replace it and never really wait
_MODEL_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")


class LLMConfigError(Exception):
    """Raised when the provider is not configured (for example no API key). CLI exit code 2."""


class LLMCallError(Exception):
    """Raised for network, HTTP or answer-shape problems. The parser reports status ``llm_error``."""


class LLMProvider(ABC):
    """Anything that can answer a prompt with a JSON object."""

    name: str
    model: str

    @abstractmethod
    def generate_json(self, system_prompt: str, user_prompt: str) -> dict:
        """Return the model's answer as a dict, or raise ``LLMCallError``."""


class GeminiProvider(LLMProvider):
    """Google Gemini over its REST API (no extra dependency: just ``requests``)."""

    name = "gemini"

    def __init__(self, model: Optional[str] = None, timeout: float = REQUEST_TIMEOUT_SECONDS) -> None:
        load_dotenv()  # real environment variables win over the .env file
        key = os.environ.get("GEMINI_API_KEY", "")
        if not key.strip():
            raise LLMConfigError(
                "GEMINI_API_KEY is not set. Create a key in Google AI Studio, then put "
                "GEMINI_API_KEY=your_key in your .env file or set it as an environment variable. "
                "Never share or commit the key."
            )
        self._api_key = key.strip()
        chosen = (model or os.environ.get("GEMINI_MODEL", "") or DEFAULT_GEMINI_MODEL).strip()
        if _MODEL_NAME.fullmatch(chosen) is None:
            raise LLMConfigError(f"Invalid Gemini model name: {chosen[:60]!r}.")
        self.model = chosen
        self.timeout = timeout

    def __repr__(self) -> str:  # never include the key
        return f"GeminiProvider(model={self.model!r})"

    def generate_json(self, system_prompt: str, user_prompt: str) -> dict:
        url = GEMINI_URL_TEMPLATE.format(model=self.model)
        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"parts": [{"text": user_prompt}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }
        attempt = 0
        while True:
            try:
                response = requests.post(
                    url, json=body, timeout=self.timeout,
                    headers={"x-goog-api-key": self._api_key, "Content-Type": "application/json"},
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= len(RETRY_DELAYS_SECONDS):  # out of retries: report it
                    raise self._network_error(exc) from None
            except requests.RequestException as exc:  # anything else will not fix itself: no retry
                raise self._network_error(exc) from None
            else:
                # Only temporary server errors are retried; everything else (429, 401, 403, 404,
                # other 4xx, answer-shape problems) is judged immediately, exactly as before.
                if response.status_code not in RETRYABLE_STATUSES or attempt >= len(RETRY_DELAYS_SECONDS):
                    self._check_status(response.status_code)
                    return self._parse_answer(response)
            _sleep(RETRY_DELAYS_SECONDS[attempt])
            attempt += 1

    @staticmethod
    def _network_error(exc: Exception) -> LLMCallError:
        # Only the exception type: its text could contain the request URL or details.
        return LLMCallError(f"Network problem while calling Gemini ({type(exc).__name__}).")

    def _check_status(self, status: int) -> None:
        if status == 200:
            return
        if status in (401, 403):
            raise LLMCallError(f"Gemini rejected the API key (HTTP {status}). Check GEMINI_API_KEY.")
        if status == 404:
            raise LLMCallError(
                f"Gemini model '{self.model}' was not found (HTTP 404). Set GEMINI_MODEL to a current "
                "model name: model names change."
            )
        if status == 429:  # deliberately no retry: report it and let the user wait
            raise LLMCallError("Gemini rate limit reached (HTTP 429). Wait a while and try again.")
        raise LLMCallError(f"Gemini returned HTTP {status}.")

    @staticmethod
    def _parse_answer(response: requests.Response) -> dict:
        try:
            data: Any = response.json()
        except ValueError:
            raise LLMCallError("Gemini sent a response that is not valid JSON.") from None
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            if not isinstance(text, str):
                raise TypeError
        except (KeyError, IndexError, TypeError):
            reason = ""
            if isinstance(data, dict) and isinstance(data.get("promptFeedback"), dict):
                reason = str(data["promptFeedback"].get("blockReason") or "")[:60]
            suffix = f" (blocked: {reason})" if reason else ""
            raise LLMCallError(f"Gemini returned no usable answer{suffix}.") from None
        try:
            answer = json.loads(text)
        except ValueError:
            raise LLMCallError("Gemini's answer was not valid JSON.") from None
        if not isinstance(answer, dict):
            raise LLMCallError("Gemini's answer was JSON but not an object.")
        return answer
