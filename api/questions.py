"""Question runs: a sub-resource of a finished analysis. Kept in memory, and saved on disk when storage is on.

Logs carry only analysis ids, stage names, counts, model names and error codes: never file text, question text or
model output.
"""
import copy
import hashlib
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Callable, Optional

from skillverify.jd.provider import LLMCallError, LLMConfigError
from skillverify_questions import config, github_files, select, service
from skillverify_questions.groq_provider import GroqKeyRejected, GroqRateLimited, GroqTooLarge
from skillverify_questions.signals import Signals

from api import errors, pacing, pipeline, settings
from api.jobs import DONE, FAILED, QUEUED, RUNNING, JobManager
from api.retry import RetryingProvider, is_temporary

log = logging.getLogger("skillverify.api")

IDLE = "idle"


class QuestionError(Exception):
    """A request problem: an HTTP status and an error object (code from the closed set)."""

    def __init__(self, status: int, error: dict) -> None:
        super().__init__(error["code"])
        self.status, self.error = status, error


def _meta() -> dict:
    return {"model": None, "fallback_used": False, "models_used": {}, "fallbacks": 0}


@dataclass
class QuestionJob:
    state: str = QUEUED
    stage: Optional[str] = service.STAGE_CHOOSING
    result: Optional[dict] = None
    error: Optional[dict] = None
    meta: dict = field(default_factory=_meta)
    attempt: int = 0
    # Private: never returned by any route and never logged.
    prepared: Optional[service.Prepared] = None
    run_state: Optional[service.RunState] = None
    partial: Optional[service.RunState] = None  # a run that ended early: a retry continues from it
    fingerprint: Optional[str] = None
    skip_cache: bool = False  # "regenerate": the question-set cache is not used for this run
    replaced: int = 0         # replacement questions saved beside the run (they count in the result the screen shows)
    busy: threading.Lock = field(default_factory=threading.Lock)


_FAILED_CALL = re.compile(r"HTTP \d{3}|^Network problem")  # a call that failed, not an answer that was badly shaped
_WAITS_PER_SWITCH = 3  # a safety net: waiting for a resting model is tried only a few times in a row


def _is_groq(entry: Optional[str]) -> bool:
    return entry is not None and entry.startswith(settings.GROQ_PREFIX)


class _RunProvider:
    """The model of one question run, picked from the question chain (Groq first, then Gemini).

    Every entry hands over to the next one: a rate limit or a temporary failure rests that entry (the usual 120 s /
    1800 s, or the provider's own shorter retry-after) and the SAME call is made again on the next entry that is not
    resting, so a run is never ended while one entry is ready. A refused Groq key sets Groq aside for the run, a request
    that is too large for Groq goes to the next entry for that one request, and a model Gemini does not know (404 on
    its first call) is set aside. When every entry is resting, a wait of at most ``QUESTIONS_MAX_WAIT_SECONDS`` is
    taken if the run still has time; otherwise the run ends with the usual error. A badly shaped answer is passed on as
    it is, so the writer applies its one-repair rule. A handover is not a failed attempt: the writer never sees it.
    Calls to the same model are spaced by ``QUESTIONS_MIN_GAP_SECONDS``.
    """

    def __init__(self, deps: pipeline.Dependencies, publish: Callable[[str, bool], None],
                 publish_counts: Optional[Callable[[dict, int], None]] = None, deadline: Optional[float] = None) -> None:
        self._deps, self._publish = deps, publish
        self._publish_counts = publish_counts or (lambda used, fallbacks: None)
        self._chain = deps.question_chain
        self._deadline = deadline
        self._skip: set[str] = set()  # entries not used again in this run
        self._left: Optional[str] = None  # the entry one oversized request had to leave
        self._answered_by: Optional[str] = None
        self._tally = service.RunState()  # replaced by the run's own state (``attach``) so a retry keeps the counts
        self.model: Optional[str] = None
        self._provider: Any = None
        self._confirmed = False
        self._repair_from: Optional[str] = None  # set when the writer is about to make its repair call
        self._restore_first = False              # the last call went to another entry for a repair: go back after it
        self._next(None)

    def attach(self, state: service.RunState) -> None:
        """Count questions and handovers in the run's state (it is kept when a run ends early)."""
        self._tally = state
        self._publish_counts(dict(state.models_used), state.fallbacks)

    def note_question(self) -> None:
        """The writer accepted a question: credit it to the model that answered."""
        if self._answered_by is not None:
            used = self._tally.models_used
            used[self._answered_by] = used.get(self._answered_by, 0) + 1
            self._publish_counts(dict(used), self._tally.fallbacks)

    def _build(self, entry: str) -> Any:
        if _is_groq(entry):
            groq = self._deps.groq_factory(entry[len(settings.GROQ_PREFIX):])
            cap, apply_cap = settings.questions_max_output_tokens(), getattr(groq, "set_max_output_tokens", None)
            if cap and apply_cap is not None:
                apply_cap(cap)
            return RetryingProvider(groq, settings.GROQ_OUTER_RETRY_DELAYS)
        return RetryingProvider((self._deps.question_provider_factory or self._deps.provider_factory)(entry))

    def repair_elsewhere(self) -> None:
        """The writer is about to make its ONE repair call: send it to a different entry than the first attempt when
        another one is ready (not resting). When none is, the same entry is used."""
        self._repair_from = self.model

    def _use_another_for_repair(self) -> None:
        first, self._repair_from = self._repair_from, None
        entry = self._chain.pick(self._skip | {first or ""})
        if entry is None:
            return
        try:
            provider = self._build(entry)
        except LLMConfigError:
            if _is_groq(entry):
                self._set_groq_aside()
            return  # nothing usable there: the repair stays on the same entry
        self._provider, self.model, self._confirmed, self._restore_first = provider, entry, False, True
        log.info("repair on another model from=%s to=%s", first, entry)
        self._publish(entry, entry != self._chain.models[0])

    def _set_groq_aside(self) -> None:
        self._skip.update(entry for entry in self._chain.models if _is_groq(entry))

    def _next(self, reason: Optional[str], avoid: tuple[str, ...] = (), failure: Optional[BaseException] = None) -> None:
        """Make the first entry that is ready (and not set aside or in ``avoid``) the current one.

        ``reason`` names a handover (logged, counted); None is the first pick or a silent return. When every entry is
        resting: wait a short while if allowed, else raise ``failure`` (or ``ai_all_limited`` for a fresh run).
        """
        waits = 0
        while True:
            entry = self._chain.pick(self._skip | set(avoid))
            if entry is not None:
                try:
                    provider = self._build(entry)
                except LLMConfigError:
                    if _is_groq(entry):  # no usable Groq key: Groq is skipped silently, Gemini carries on
                        self._set_groq_aside()
                        continue
                    raise errors.PipelineError(errors.AI_NOT_CONFIGURED) from None
                previous = self.model
                self._provider, self.model, self._confirmed = provider, entry, False
                if reason is not None and previous is not None and entry != previous:
                    self._tally.fallbacks += 1
                    log.info("handover from=%s to=%s reason=%s", previous, entry, reason)
                    self._publish_counts(dict(self._tally.models_used), self._tally.fallbacks)
                self._publish(entry, entry != self._chain.models[0])
                return
            wait = self._chain.wait_seconds(self._skip | set(avoid))
            if (wait is not None and waits < _WAITS_PER_SWITCH and wait <= settings.questions_max_wait_seconds()
                    and (self._deadline is None or pacing.now() + wait < self._deadline)):
                waits += 1
                log.info("waiting seconds=%d for a model to be ready", round(wait))
                pacing.sleep(wait)
                continue
            raise failure if failure is not None else errors.PipelineError(errors.AI_ALL_LIMITED)

    def _hand_over(self, exc: LLMCallError) -> None:
        """Move on from a failed call to the next entry, or re-raise ``exc`` when it is an answer problem."""
        entry = self.model
        assert entry is not None
        text = str(exc)
        avoid: tuple[str, ...] = ()
        failure: BaseException = exc
        retry_after: Optional[float] = None
        rest = False
        if isinstance(exc, GroqKeyRejected):
            reason = "key_rejected"
            failure = errors.PipelineError(errors.AI_NOT_CONFIGURED)  # nothing but the refused Groq key was in the chain
            self._set_groq_aside()
        elif isinstance(exc, GroqTooLarge):
            reason, avoid, self._left = "too_large", (entry,), entry  # this one request only: Groq is not rested
        elif isinstance(exc, GroqRateLimited) or "HTTP 429" in text:
            reason, rest, retry_after = "rate_limited", True, getattr(exc, "retry_after", None)
        elif not _is_groq(entry) and not self._confirmed and pipeline._MODEL_NOT_FOUND in text:
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

    def generate_json(self, system_prompt: str, user_prompt: str) -> dict:
        if self._left is not None:  # the request that was too large is over: Groq is tried first again
            left, self._left = self._left, None
            if self._chain.pick(self._skip) == left:
                self._next(None)
        if self._repair_from is not None:
            self._use_another_for_repair()
        elif self._restore_first:  # the repair is over: the next question starts on the first ready entry again
            self._restore_first = False
            if self._chain.pick(self._skip) != self.model:
                self._next(None)
        while True:
            self._deps.pacer.wait(self.model, settings.questions_min_gap_seconds(), self._deadline)  # type: ignore[arg-type]
            try:
                answer = self._provider.generate_json(system_prompt, user_prompt)
            except LLMConfigError:
                raise errors.PipelineError(errors.AI_NOT_CONFIGURED) from None
            except LLMCallError as exc:
                self._hand_over(exc)
                continue
            self._confirmed = True
            self._answered_by = self.model
            return answer


class QuestionManager:
    def __init__(self, jobs: JobManager, deps: pipeline.Dependencies) -> None:
        self._analyses, self._deps = jobs, deps
        self._store = getattr(deps, "store", None)
        self._jobs: dict[str, QuestionJob] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=settings.MAX_WORKERS, thread_name_prefix="questions")

    @cached_property
    def _signals(self) -> Signals:
        return Signals()

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # -- saved runs ---------------------------------------------------------------------------------------------
    def _restore(self, analysis_id: str) -> Optional[QuestionJob]:
        """A question job rebuilt from the saved run of this analysis, or None."""
        row = self._store.load_run(analysis_id) if self._store is not None else None
        if row is None:
            return None
        job = QuestionJob(meta={**_meta(), **row["meta"]}, fingerprint=row["fingerprint"], stage=service.STAGE_CHECKING,
                          replaced=len(row["replacements"]))
        if row["status"] == "partial":  # ended early: a retry continues from the questions it has
            job.state, job.error = FAILED, row["error"]
            job.partial = service.state_from_result(row["result"], row["replacements"], row["meta"])
        else:
            job.state, job.result = DONE, row["result"]
        return job

    def _fingerprint(self, result: dict) -> Optional[str]:
        """What a question set depends on: the account, the score, which skills are asked about, the writer version."""
        github = result.get("github") if isinstance(result.get("github"), dict) else {}
        username = github.get("username") or github.get("cv_username")
        if not isinstance(username, str):
            return None
        needs, _ = select.candidate_skills(result.get("skills") if isinstance(result.get("skills"), list) else [])
        pairs = sorted([need.skill, need.tier] for need in needs)
        score = (result.get("score") or {}).get("repo_fingerprint") if isinstance(result.get("score"), dict) else None
        payload = json.dumps([username.lower(), score, pairs, config.WRITER_SETTINGS_VERSION], sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _save_run(self, analysis_id: str, job: QuestionJob, result: dict, meta: dict) -> None:
        if self._store is not None:
            self._store.save_run(analysis_id, result["status"], result, copy.deepcopy(meta), job.fingerprint)

    def _keep_partial(self, analysis_id: str, job: QuestionJob, error: dict) -> None:
        """A run that ended early keeps the questions it has: saved as ``partial``, and a retry continues from them."""
        if self._store is None or job.partial is None or job.prepared is None or not job.partial.questions:
            return
        result = service.build_result(job.prepared, job.partial)
        with self._lock:
            meta = copy.deepcopy(job.meta)
        self._store.save_run(analysis_id, "partial", result, meta, job.fingerprint, error, keep_finished=True)

    # -- reading ----------------------------------------------------------------------------------
    def _job(self, analysis_id: str) -> Optional[QuestionJob]:
        with self._lock:
            job = self._jobs.get(analysis_id)
            if job is None:
                job = self._restore(analysis_id)
                if job is not None:
                    self._jobs[analysis_id] = job
            return job

    def _gone(self, analysis_id: str) -> bool:
        """True when the analysis does not exist (any question job of it is dropped too)."""
        if self._analyses.snapshot(analysis_id) is not None:
            return False
        with self._lock:
            self._jobs.pop(analysis_id, None)
        return True

    def snapshot(self, analysis_id: str) -> Optional[dict]:
        """The status object, or None when the analysis does not exist."""
        if self._gone(analysis_id):
            return None
        job = self._job(analysis_id)
        with self._lock:
            if job is None:
                return {"id": analysis_id, "state": IDLE, "stage": None, "result": None, "error": None,
                        "meta": _meta()}
            result = service.with_replacements(job.result, job.replaced) if job.result is not None else None
            return {"id": analysis_id, "state": job.state, "stage": job.stage, "result": result,
                    "error": job.error, "meta": copy.deepcopy(job.meta)}

    # -- starting ---------------------------------------------------------------------------------
    def start(self, analysis_id: str, regenerate: bool = False) -> dict:
        analysis = self._analyses.snapshot(analysis_id)
        ready = (analysis is not None and analysis["state"] == DONE and isinstance(analysis["result"], dict)
                 and analysis["result"].get("status") == "ok")
        if not ready:
            raise QuestionError(409, errors.error_object(errors.ANALYSIS_NOT_READY))
        fingerprint = self._fingerprint(analysis["result"])  # type: ignore[index]
        existing = self._job(analysis_id)
        if existing is not None:
            if not regenerate or existing.state in (QUEUED, RUNNING):
                raise QuestionError(409, errors.error_object(errors.QUESTIONS_ALREADY_STARTED))
            with self._lock:  # regenerate: a fresh run that overwrites the saved one; the file cache still helps
                existing.state, existing.stage = QUEUED, service.STAGE_CHOOSING
                existing.result = existing.error = None
                existing.meta = _meta()
                existing.prepared = existing.run_state = existing.partial = None
                existing.fingerprint, existing.skip_cache, existing.replaced = fingerprint, True, 0
                existing.attempt += 1
                attempt = existing.attempt
            self._submit(analysis_id, attempt)
            return self.snapshot(analysis_id)  # type: ignore[return-value]
        hit = None
        if not regenerate and self._store is not None and fingerprint is not None:
            hit = self._store.find_cached_run(fingerprint, analysis_id)
        with self._lock:
            if analysis_id in self._jobs:
                raise QuestionError(409, errors.error_object(errors.QUESTIONS_ALREADY_STARTED))
            job = QuestionJob(fingerprint=fingerprint, skip_cache=regenerate)
            if hit is not None:  # the same account, score, skills and writer version: no GitHub, no model call
                job.state, job.stage, job.result = DONE, service.STAGE_CHECKING, hit["result"]
                job.meta = {**_meta(), **hit["meta"], "cached": True, "cached_from": hit["analysis_id"]}
            self._jobs[analysis_id] = job
        if hit is not None:
            self._save_run(analysis_id, job, job.result, job.meta)  # type: ignore[arg-type]
            log.info("questions %s served from the saved set of %s", analysis_id, hit["analysis_id"])
        else:
            self._submit(analysis_id, 0)
        return self.snapshot(analysis_id)  # type: ignore[return-value]

    def retry(self, analysis_id: str) -> Optional[bool]:
        """None: unknown analysis. False: nothing to retry. True: a fresh run started (the selection is reused, and a
        run that ended early continues from the questions it already has)."""
        if self._gone(analysis_id):
            return None
        job = self._job(analysis_id)
        with self._lock:
            if job is None or job.state != FAILED or not (job.error and job.error["retryable"]):
                return False
            job.state, job.stage = QUEUED, service.STAGE_CHOOSING
            job.result = job.error = None
            job.meta = _meta()
            job.attempt += 1
            attempt = job.attempt
        self._submit(analysis_id, attempt)
        return True

    # -- running ----------------------------------------------------------------------------------
    def _submit(self, analysis_id: str, attempt: int) -> None:
        self._pool.submit(self._execute, analysis_id, attempt)

    def _update(self, analysis_id: str, attempt: int, **changes: Any) -> bool:
        with self._lock:
            job = self._jobs.get(analysis_id)
            if job is None or job.attempt != attempt or job.state != RUNNING:
                return False
            for key, value in changes.items():
                setattr(job, key, value)
            return True

    def _update_meta(self, analysis_id: str, attempt: int, **changes: Any) -> bool:
        with self._lock:
            job = self._jobs.get(analysis_id)
            if job is None or job.attempt != attempt or job.state != RUNNING:
                return False
            job.meta.update(changes)
            return True

    def _execute(self, analysis_id: str, attempt: int) -> None:
        with self._lock:
            job = self._jobs.get(analysis_id)
            if job is None or job.attempt != attempt or job.state != QUEUED:
                return
            job.state = RUNNING
        outcome: dict[str, Any] = {}

        def work() -> None:
            try:
                outcome["result"] = self._run(analysis_id, attempt, job)
            except errors.PipelineError as exc:
                outcome["error"] = exc.error
            except Exception:  # noqa: BLE001 - never expose exception text, paths, code or keys
                outcome["error"] = errors.error_object(errors.INTERNAL_ERROR)

        thread = threading.Thread(target=work, name=f"questions-{analysis_id[:8]}", daemon=True)
        thread.start()
        thread.join(settings.questions_timeout_seconds())
        if thread.is_alive():  # abandoned (not stopped): its late writes are ignored, the state is no longer running
            with self._lock:
                stage = job.stage
            outcome = {"error": errors.error_object(errors.TIMEOUT, stage=stage)}
        if "result" in outcome:
            result, meta = outcome["result"]
            self._save_run(analysis_id, job, result, meta)  # on disk before anyone can read "done"
            if self._update(analysis_id, attempt, state=DONE, stage=service.STAGE_CHECKING, result=result, meta=meta,
                            run_state=job.partial, partial=None):
                log.info("questions %s done", analysis_id)
        else:
            self._keep_partial(analysis_id, job, outcome["error"])
            if self._update(analysis_id, attempt, state=FAILED, error=outcome["error"]):
                log.info("questions %s failed: %s", analysis_id, outcome["error"]["code"])

    def _set_stage(self, analysis_id: str, attempt: int) -> Callable[[str], None]:
        def set_stage(text: str) -> None:
            if self._update(analysis_id, attempt, stage=text):
                log.info("questions %s stage: %s", analysis_id, text)
        return set_stage

    def _publish(self, analysis_id: str, attempt: int) -> Callable[[str, bool], None]:
        def publish(model: str, fallback_used: bool) -> None:
            self._update_meta(analysis_id, attempt, model=model, fallback_used=fallback_used)
        return publish

    def _publish_counts(self, analysis_id: str, attempt: int) -> Callable[[dict, int], None]:
        def publish(models_used: dict, fallbacks: int) -> None:
            self._update_meta(analysis_id, attempt, models_used=models_used, fallbacks=fallbacks)
        return publish

    def _final_meta(self, provider: _RunProvider, state: service.RunState) -> dict:
        """``meta.model`` is the model that wrote the most questions (the earlier entry on a tie)."""
        chain = self._deps.question_chain
        order = list(chain.models)
        used = dict(state.models_used)
        model = (max(used, key=lambda m: (used[m], -(order.index(m) if m in order else len(order))))
                 if used else provider.model)
        return {"model": model, "fallback_used": model != order[0], "models_used": used, "fallbacks": state.fallbacks}

    def _run(self, analysis_id: str, attempt: int, job: QuestionJob) -> tuple[dict, dict]:
        chain = self._deps.question_chain
        provider = None
        try:
            deadline = pacing.now() + settings.questions_timeout_seconds()
            provider = _RunProvider(self._deps, self._publish(analysis_id, attempt),
                                    self._publish_counts(analysis_id, attempt), deadline)  # no model left: fail at once
            if job.prepared is None:  # a retry after a Gemini failure reuses the cached selection
                job.prepared = self._load_prepared(analysis_id, self._set_stage(analysis_id, attempt))
            if job.partial is None:
                job.partial = service.new_state(job.prepared)
            provider.attach(job.partial)  # counts and handovers go into the state a retry would continue from
            result, state = service.write_all(job.prepared, provider, self._set_stage(analysis_id, attempt),
                                              state=job.partial)
        except LLMCallError as exc:
            failure = pipeline._llm_failure(str(exc))
            if failure.error["code"] == errors.AI_RATE_LIMITED:  # the handover already rested the model that said so
                failure.error["fallback_available"] = chain.pick() is not None
            raise failure from None
        chain.mark_success(provider.model)  # type: ignore[arg-type]
        return result, self._final_meta(provider, state)

    def _load_prepared(self, analysis_id: str, set_stage: Callable[[str], None]) -> service.Prepared:
        analysis = self._analyses.snapshot(analysis_id)
        kept = self._analyses.intermediates(analysis_id) or {}
        return self._prepare(analysis["result"]["skills"], kept.get("github"), set_stage)  # type: ignore[index]

    def _prepare(self, score_skills: Any, github: Any, set_stage: Callable[[str], None]) -> service.Prepared:
        cache = self._store if self._store is not None and self._store.enabled else None  # file texts: no GitHub on a hit
        try:
            return service.prepare(score_skills, github, self._deps.files_client_factory(), self._signals, set_stage,
                                   cache=cache)
        except github_files.GitHubFilesError as exc:
            raise errors.PipelineError(errors.INTERNAL_ERROR if exc.kind == github_files.NOT_CONFIGURED
                                       else errors.GITHUB_UNAVAILABLE) from None

    # -- replacing one question (synchronous) ---------------------------------------------------------
    def replace(self, analysis_id: str, after: Optional[str] = None) -> dict:
        job = self._job(analysis_id)
        if job is None or job.state != DONE or job.result is None:
            raise QuestionError(409, errors.error_object(
                errors.ANALYSIS_NOT_READY, "The questions have not been written yet, so none can be replaced."))
        if not job.busy.acquire(blocking=False):
            raise QuestionError(409, errors.error_object(
                errors.QUESTIONS_ALREADY_STARTED, "A replacement is already being written. Please wait."))
        provider = None
        try:
            if job.prepared is None or job.run_state is None:  # a run loaded from disk: rebuild what it needs
                job.prepared = job.prepared or self._load_prepared(analysis_id, lambda stage: None)
                row = self._store.load_run(analysis_id) if self._store is not None else None
                job.run_state = service.state_from_result(job.result, row["replacements"] if row else [], job.meta)
            before = len(job.run_state.drops)
            provider = _RunProvider(self._deps, lambda model, fallback: None,
                                    deadline=pacing.now() + settings.questions_timeout_seconds())
            provider.attach(job.run_state)
            outcome = service.replace_one(job.prepared, job.run_state, provider)
        except LLMCallError as exc:
            raise QuestionError(503, self._llm_error(exc)) from None
        except errors.PipelineError as exc:
            raise QuestionError(503, exc.error) from None
        finally:
            job.busy.release()
        self._deps.question_chain.mark_success(provider.model)  # type: ignore[union-attr,arg-type]
        dropped = [dict(d) for d in job.run_state.drops[before:]]   # this replacement's own failed attempts
        if outcome is None:
            error = errors.error_object(errors.NO_REPLACEMENT_AVAILABLE)
            if dropped:
                error["dropped"] = dropped
            raise QuestionError(409, error)
        question, skipped = outcome
        with self._lock:
            job.replaced += 1  # the result the screen shows now counts this question
            job.meta.update(self._final_meta(provider, job.run_state))
            meta = copy.deepcopy(job.meta)
        if self._store is not None:  # the saved run learns about the replacement (which file it used, who wrote it)
            self._store.add_replacement(analysis_id, {"after": after, "question": question}, meta)
        return {"question": question, "skipped_skills": [dict(s) for s in skipped], "dropped": dropped}

    def _llm_error(self, exc: LLMCallError) -> dict:
        failure = pipeline._llm_failure(str(exc))
        if failure.error["code"] == errors.AI_RATE_LIMITED:
            failure.error["fallback_available"] = self._deps.question_chain.pick() is not None
        return failure.error
