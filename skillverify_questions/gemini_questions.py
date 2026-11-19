"""The Gemini provider the question writer uses: the frozen ``GeminiProvider`` with one change, how an answer is read.

The request, the retries, the status handling and every message are the frozen provider's own. Only the reading of the
reply differs: it is tolerant (code fences and text around the object are removed, see ``answer.py``) and an unusable
reply carries a closed cause (``empty_response``, ``cut_off`` when ``finishReason`` is ``MAX_TOKENS``, ``not_json``,
``wrong_shape``). The reading chain keeps the plain ``GeminiProvider``.
"""
from typing import Any

import requests

from skillverify.jd import GeminiProvider
from skillverify_questions import answer as answer_text


class QuestionGemini(GeminiProvider):
    @staticmethod
    def _parse_answer(response: requests.Response) -> dict:
        try:
            data: Any = response.json()
        except ValueError:
            raise answer_text.AnswerProblem("Gemini sent a response that is not valid JSON.",
                                            answer_text.NOT_JSON) from None
        text: Any = None
        truncated = False
        try:
            candidate = data["candidates"][0]
            truncated = candidate.get("finishReason") == "MAX_TOKENS"
            text = candidate["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError, AttributeError):
            pass
        if not isinstance(text, str) and not truncated:
            feedback = data.get("promptFeedback") if isinstance(data, dict) else None
            reason = str(feedback.get("blockReason") or "")[:60] if isinstance(feedback, dict) else ""
            suffix = f" (blocked: {reason})" if reason else ""
            raise answer_text.AnswerProblem(f"Gemini returned no usable answer{suffix}.", answer_text.EMPTY)
        return answer_text.parse_answer(text, "Gemini", truncated)
