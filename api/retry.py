"""RetryingProvider: an outer retry layer around an LLM provider (same ``generate_json`` interface)."""
import re
import time

from skillverify.jd.provider import LLMCallError, LLMProvider

from api import settings

_sleep = time.sleep  # a module-level reference, so tests can replace it and never really wait
_TEMPORARY = re.compile(r"HTTP (500|502|503|504)\b|^Network problem")


def is_temporary(message: str) -> bool:
    """True for failures that may pass by themselves: HTTP 500/502/503/504 or a network problem."""
    return _TEMPORARY.search(message) is not None


class RetryingProvider(LLMProvider):
    """Retries temporary Gemini failures quietly, then lets the last ``LLMCallError`` through.

    HTTP 429, rejected keys and answer-shape problems are never retried.
    """

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner

    @property
    def name(self) -> str:  # type: ignore[override]
        return getattr(self.inner, "name", "unknown")

    @property
    def model(self) -> str:  # type: ignore[override]
        return getattr(self.inner, "model", "unknown")

    def generate_json(self, system_prompt: str, user_prompt: str) -> dict:
        delays = settings.GEMINI_OUTER_RETRY_DELAYS
        attempt = 0
        while True:
            try:
                return self.inner.generate_json(system_prompt, user_prompt)
            except LLMCallError as exc:
                if attempt >= len(delays) or not is_temporary(str(exc)):
                    raise
            _sleep(delays[attempt])
            attempt += 1
