"""Reading the job description and the CV: model handover, pacing, the same-input cache and contact redaction.

``ReadingProvider`` wraps the models of the reading chain and is given to the frozen JD / CV parsers as their
provider (they take any object with ``generate_json``), so no Phase 1 code changes. For each call it

1. removes e-mail addresses and phone numbers from the text (CV only) with the same detectors the CV parser uses,
2. looks the exact text up in the saved results (SHA-256 of the system prompt + the redacted text + a prompt version)
   and, on a hit, answers with no model call,
3. otherwise calls the first ready model, spaced from the previous call to that model; a rate limit or a temporary
   failure rests that model and the SAME call goes to the next ready one; when every model rests, a short wait is
   taken if it fits the time limit, else the failure ends the analysis with the usual error code.

A model that answers with unusable output is NOT a handover: the answer goes to the parser, whose own rules apply.
Logs carry only the document kind (jd / cv), model names, reasons and seconds, never any text.
"""
import copy
import hashlib
import logging
import re
from typing import Any, Callable, Optional

from skillverify.cv.contacts import find_emails, find_phones, redact_contacts
from skillverify.jd.provider import LLMCallError, LLMConfigError, LLMProvider
from skillverify_questions.groq_provider import GroqKeyRejected, GroqRateLimited, GroqTooLarge

from api import errors, pacing, settings
from api.retry import RetryingProvider, is_temporary

log = logging.getLogger("skillverify.api")

# Bump when the way a document is read changes in a way the prompts do not show (the prompts themselves are part of
# the key already), so older saved readings are not used any more.
READING_PROMPT_VERSION = 1

JD, CV = "jd", "cv"
MODEL_NOT_FOUND = "was not found (HTTP 404)"
_FAILED_CALL = re.compile(r"HTTP \d{3}|^Network problem")  # a call that failed, not an answer that was badly shaped
_WAITS_PER_SWITCH = 3  # a safety net: waiting for a resting model is tried only a few times in a row

Report = Callable[[str, str, bool], None]  # (document kind, model, cached)


def redact(text: str) -> str:
    """The text with every e-mail address and phone number replaced by [EMAIL] / [PHONE] (the CV parser's detectors
    and placeholders). Names, links and skills stay."""
    return redact_contacts(text, find_emails(text), find_phones(text))


def reading_key(system_prompt: str, text: str) -> str:
    """The cache key: a hash of the exact (already redacted) text sent and the prompt version. The text is not kept."""
    digest = hashlib.sha256()
    for part in (str(READING_PROMPT_VERSION), system_prompt, text):
        digest.update(hashlib.sha256(part.encode("utf-8", "replace")).digest())
    return digest.hexdigest()


def _is_groq(entry: Optional[str]) -> bool:
    return entry is not None and entry.startswith(settings.GROQ_PREFIX)


def _cacheable(answer: Any) -> bool:
    """Only a well-shaped answer is saved, so a retry after a badly shaped one reads the document again."""
    return isinstance(answer, dict) and isinstance(answer.get("skills"), list)


class ReadingProvider(LLMProvider):
    """The provider the JD or CV parser calls (one instance per document). See the module text."""

    def __init__(self, deps: Any, kind: str, report: Report, deadline: Optional[float] = None,
                 redact_first: bool = False, known: Optional[set] = None) -> None:
        self._deps, self._kind, self._report = deps, kind, report
        self._chain = deps.chain
        self._deadline = deadline
        self._redact = redact_first
        self._skip: set[str] = set()  # entries not used again for this document
        self._model: Optional[str] = None
        self._provider: Any = None
        # models that already answered a call in this analysis (shared by both documents): a 404 from one of them is a
        # configuration problem, not an unknown model name
        self._known: set[str] = known if known is not None else set()

    # the parsers record these in their result before the call
    @property
    def name(self) -> str:  # type: ignore[override]
        return "groq" if _is_groq(self._model) else "gemini"

    @property
    def model(self) -> str:  # type: ignore[override]
        return self._model or self._chain.models[0]

    # -- picking ---------------------------------------------------------------------------------------------
    def _build(self, entry: str) -> Any:
        """The provider of one entry. Quiet retries of a temporary failure are for the LAST resort only: while another
        entry is ready the failure hands over instead of waiting."""
        last_resort = self._chain.pick(self._skip | {entry}) is None
        if _is_groq(entry):
            delays = settings.GROQ_OUTER_RETRY_DELAYS if last_resort else ()
            return RetryingProvider(self._deps.groq_factory(entry[len(settings.GROQ_PREFIX):]), delays)
        return RetryingProvider(self._deps.provider_factory(entry), None if last_resort else ())

    def _set_groq_aside(self) -> None:
        self._skip.update(entry for entry in self._chain.models if _is_groq(entry))

    def _next(self, reason: Optional[str], avoid: tuple[str, ...] = (), failure: Optional[BaseException] = None) -> None:
        """Make the first entry that is ready the current one. ``reason`` names a handover (logged); None is the first
        pick. When every entry is resting: wait a short while if allowed, else raise ``failure``."""
        waits = 0
        while True:
            entry = self._chain.pick(self._skip | set(avoid))
            if entry is not None:
                try:
                    provider = self._build(entry)
                except LLMConfigError:
                    if _is_groq(entry):  # no usable Groq key: Groq is skipped, the next entry carries on
                        self._set_groq_aside()
                        continue
                    raise errors.PipelineError(errors.AI_NOT_CONFIGURED) from None
                previous = self._model
                self._provider, self._model = provider, entry
                if reason is not None and previous is not None and entry != previous:
                    log.info("reading handover doc=%s from=%s to=%s reason=%s", self._kind, previous, entry, reason)
                self._report(self._kind, entry, False)
                return
            wait = self._chain.wait_seconds(self._skip | set(avoid))
            if (wait is not None and waits < _WAITS_PER_SWITCH and wait <= settings.reading_max_wait_seconds()
                    and (self._deadline is None or pacing.now() + wait < self._deadline)):
                waits += 1
                log.info("reading waiting doc=%s seconds=%d for a model to be ready", self._kind, round(wait))
                pacing.sleep(wait)
                continue
            raise failure if failure is not None else errors.PipelineError(errors.AI_ALL_LIMITED)

    def _hand_over(self, exc: LLMCallError) -> None:
        """Move on from a failed call to the next entry, or re-raise ``exc`` when it is an answer problem."""
        entry = self._model
        assert entry is not None
        text = str(exc)
        avoid: tuple[str, ...] = ()
        failure: BaseException = exc
        retry_after: Optional[float] = None
        rest = False
        if isinstance(exc, GroqKeyRejected):
            reason = "key_rejected"
            failure = errors.PipelineError(errors.AI_NOT_CONFIGURED)
            self._set_groq_aside()
        elif isinstance(exc, GroqTooLarge):
            reason, avoid = "too_large", (entry,)  # this one request only: the entry is not rested
        elif isinstance(exc, GroqRateLimited) or "HTTP 429" in text:
            reason, rest, retry_after = "rate_limited", True, getattr(exc, "retry_after", None)
        elif not _is_groq(entry) and entry not in self._known and MODEL_NOT_FOUND in text:
            reason = "not_found"
            failure = errors.PipelineError(errors.AI_ALL_LIMITED)  # as before: no model Gemini knows is left
            self._chain.mark_unavailable(entry)
        elif is_temporary(text) or (_is_groq(entry) and _FAILED_CALL.search(text)):
            reason, rest = "temporary", True
        else:
            raise exc
        if rest:
            self._chain.mark_rate_limited(entry, retry_after)
        self._next(reason, avoid, failure)

    # -- the call --------------------------------------------------------------------------------------------
    def generate_json(self, system_prompt: str, user_prompt: str) -> dict:
        text = redact(user_prompt) if self._redact else user_prompt
        store = getattr(self._deps, "store", None)
        key = reading_key(system_prompt, text)
        saved = store.get_reading(key) if store is not None else None
        if saved is not None:
            self._report(self._kind, saved["model"], True)
            return copy.deepcopy(saved["answer"])
        if self._provider is None:
            self._next(None)
        while True:
            self._deps.pacer.wait(self._model, settings.reading_min_gap_seconds(), self._deadline)  # type: ignore[arg-type]
            try:
                answer = self._provider.generate_json(system_prompt, text)
            except LLMConfigError:
                raise errors.PipelineError(errors.AI_NOT_CONFIGURED) from None
            except LLMCallError as exc:
                self._hand_over(exc)
                continue
            self._known.add(self._model)  # type: ignore[arg-type]
            self._chain.mark_success(self._model)  # type: ignore[arg-type]
            if store is not None and _cacheable(answer):
                store.put_reading(key, self._model, answer)  # type: ignore[arg-type]
            return answer
