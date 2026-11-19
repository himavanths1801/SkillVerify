"""Reading a model's answer text tolerantly, and naming why an answer was unusable.

Used by the Groq provider and by the Gemini provider the question writer uses. Plain code only: no ``eval``, and no
syntax repair beyond what is listed here:
  1. surrounding code fences (```` ``` ```` or ```` ```json ````) are removed;
  2. a whole text that is JSON is used as it is (an object is the answer; any other JSON is ``wrong_shape``);
  3. otherwise the text before the first ``{`` is dropped and the first balanced ``{...}`` is parsed.
The messages keep the words the writer already recognises ("not valid JSON", "no usable answer", "not an object"), so a
badly shaped answer is still a repairable ``bad_json``; the closed ``cause`` (see ``config.ANSWER_CAUSES``) rides along.
Nothing here keeps or logs the text.
"""
import json
import re
from typing import Any, Optional

from skillverify.jd.provider import LLMCallError
from skillverify_questions import config

EMPTY, CUT_OFF, NOT_JSON, WRONG_SHAPE = config.ANSWER_CAUSES

_FENCE = re.compile(r"\A\s*```[A-Za-z0-9_-]*[ \t]*\r?\n?(.*?)\s*```\s*\Z", re.DOTALL)
_MISSING = object()


class AnswerProblem(LLMCallError):
    """An answer that could not be used. ``cause`` is one of ``config.ANSWER_CAUSES``."""

    def __init__(self, message: str, cause: str) -> None:
        super().__init__(message)
        self.cause = cause


def message_for(who: str, cause: str) -> str:
    """The fixed sentence for a cause (``who`` is a provider name such as "Groq"). Never contains any answer text."""
    return {EMPTY: f"{who} returned no usable answer.",
            CUT_OFF: f"{who}'s answer was cut off (not valid JSON).",
            NOT_JSON: f"{who}'s answer was not valid JSON.",
            WRONG_SHAPE: f"{who}'s answer was JSON but not an object."}[cause]


def cause_of(exc: BaseException) -> str:
    """The cause of an answer problem; for a plain error that only has the usual wording, the cause that wording means."""
    cause = getattr(exc, "cause", None)
    if cause in config.ANSWER_CAUSES:
        return cause
    text = str(exc)
    if "no usable answer" in text:
        return EMPTY
    if "not an object" in text:
        return WRONG_SHAPE
    return NOT_JSON


def _balanced_end(text: str, start: int) -> Optional[int]:
    """The index of the ``}`` that closes the ``{`` at ``start`` (strings and escapes respected), or None."""
    depth, in_string, escaped = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def parse_answer(text: Any, who: str, truncated: bool = False) -> dict:
    """The JSON object in a model's answer text, or an ``AnswerProblem`` with the cause.

    ``truncated`` is True when the provider itself said the output limit was reached: an answer that cannot be used is
    then ``cut_off`` (even when the text is empty, for example when the model's thinking used up the limit).
    """
    def fail(cause: str) -> AnswerProblem:
        cause = CUT_OFF if truncated and cause != WRONG_SHAPE else cause
        return AnswerProblem(message_for(who, cause), cause)

    body = text.strip() if isinstance(text, str) else ""
    if not body:
        raise fail(EMPTY)
    fenced = _FENCE.match(body)
    if fenced:
        body = fenced.group(1).strip()
    try:
        whole: Any = json.loads(body)
    except ValueError:
        whole = _MISSING
    if whole is not _MISSING:
        if isinstance(whole, dict):
            return whole
        raise fail(WRONG_SHAPE)  # a top-level array or scalar
    start = body.find("{")
    if start < 0:
        raise fail(NOT_JSON)
    end = _balanced_end(body, start)
    if end is None:
        raise AnswerProblem(message_for(who, CUT_OFF), CUT_OFF)  # the braces never balance
    try:
        found = json.loads(body[start:end + 1])
    except ValueError:
        raise fail(NOT_JSON) from None
    return found  # a balanced "{...}" that parses is always an object
