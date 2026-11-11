"""Question runs: an in-memory sub-resource of a finished analysis. Nothing is persisted.

Logs carry only analysis ids, stage names and error codes: never file text, question text or model output.
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Callable, Optional

from skillverify.jd.provider import LLMCallError, LLMConfigError
from skillverify_questions import github_files, service
from skillverify_questions.signals import Signals

from api import errors, pipeline, settings
from api.jobs import DONE, FAILED, QUEUED, RUNNING, JobManager
from api.retry import RetryingProvider

log = logging.getLogger("skillverify.api")

IDLE = "idle"


class QuestionError(Exception):
    """A request problem: an HTTP status and an error object (code from the closed set)."""

    def __init__(self, status: int, error: dict) -> None:
        super().__init__(error["code"])
        self.status, self.error = status, error


def _meta() -> dict:
    return {"model": None, "fallback_used": False}


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
    busy: threading.Lock = field(default_factory=threading.Lock)


class _RunProvider:
    """One Gemini model for a whole run, picked from the shared chain exactly as the analysis pipeline does.

    A model Gemini does not know (HTTP 404 on the first call) is set aside and the next one is used at once.
    """

    def __init__(self, deps: pipeline.Dependencies, publish: Callable[[str, bool], None]) -> None:
        self._deps, self._publish = deps, publish
        self.model: Optional[str] = None
        self._provider: Any = None
        self._confirmed = False
        self._switch()

    def _switch(self) -> None:
        chain = self._deps.chain
        model = chain.pick()
        if model is None:
            raise errors.PipelineError(errors.AI_ALL_LIMITED)
        try:
            self._provider = RetryingProvider(self._deps.provider_factory(model))
        except LLMConfigError:
            raise errors.PipelineError(errors.AI_NOT_CONFIGURED) from None
        self.model = model
        self._publish(model, model != chain.models[0])

    def generate_json(self, system_prompt: str, user_prompt: str) -> dict:
        while True:
            try:
                answer = self._provider.generate_json(system_prompt, user_prompt)
            except LLMConfigError:
                raise errors.PipelineError(errors.AI_NOT_CONFIGURED) from None
            except LLMCallError as exc:
                if self._confirmed or pipeline._MODEL_NOT_FOUND not in str(exc):
                    raise
                self._deps.chain.mark_unavailable(self.model)  # type: ignore[arg-type]
                self._switch()
                continue
            self._confirmed = True
            return answer


class QuestionManager:
    def __init__(self, jobs: JobManager, deps: pipeline.Dependencies) -> None:
        self._analyses, self._deps = jobs, deps
        self._jobs: dict[str, QuestionJob] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=settings.MAX_WORKERS, thread_name_prefix="questions")

    @cached_property
    def _signals(self) -> Signals:
        return Signals()

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # -- reading ----------------------------------------------------------------------------------
    def _job(self, analysis_id: str) -> Optional[QuestionJob]:
        with self._lock:
            return self._jobs.get(analysis_id)

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
            return {"id": analysis_id, "state": job.state, "stage": job.stage, "result": job.result,
                    "error": job.error, "meta": dict(job.meta)}

    # -- starting ---------------------------------------------------------------------------------
    def start(self, analysis_id: str) -> dict:
        analysis = self._analyses.snapshot(analysis_id)
        ready = (analysis is not None and analysis["state"] == DONE and isinstance(analysis["result"], dict)
                 and analysis["result"].get("status") == "ok")
        if not ready:
            raise QuestionError(409, errors.error_object(errors.ANALYSIS_NOT_READY))
        with self._lock:
            if analysis_id in self._jobs:
                raise QuestionError(409, errors.error_object(errors.QUESTIONS_ALREADY_STARTED))
            self._jobs[analysis_id] = QuestionJob()
        self._submit(analysis_id, 0)
        return self.snapshot(analysis_id)  # type: ignore[return-value]

    def retry(self, analysis_id: str) -> Optional[bool]:
        """None: unknown analysis. False: nothing to retry. True: a fresh run started (the selection is reused)."""
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
            result, state = outcome["result"]
            if self._update(analysis_id, attempt, state=DONE, stage=service.STAGE_CHECKING, result=result,
                            run_state=state):
                log.info("questions %s done", analysis_id)
        elif self._update(analysis_id, attempt, state=FAILED, error=outcome["error"]):
            log.info("questions %s failed: %s", analysis_id, outcome["error"]["code"])

    def _set_stage(self, analysis_id: str, attempt: int) -> Callable[[str], None]:
        def set_stage(text: str) -> None:
            if self._update(analysis_id, attempt, stage=text):
                log.info("questions %s stage: %s", analysis_id, text)
        return set_stage

    def _publish(self, analysis_id: str, attempt: int) -> Callable[[str, bool], None]:
        def publish(model: str, fallback_used: bool) -> None:
            self._update(analysis_id, attempt, meta={"model": model, "fallback_used": fallback_used})
        return publish

    def _run(self, analysis_id: str, attempt: int, job: QuestionJob) -> tuple[dict, service.RunState]:
        chain = self._deps.chain
        provider = None
        try:
            provider = _RunProvider(self._deps, self._publish(analysis_id, attempt))  # no model left: fail at once
            if job.prepared is None:  # a retry after a Gemini failure reuses the cached selection
                analysis = self._analyses.snapshot(analysis_id)
                kept = self._analyses.intermediates(analysis_id) or {}
                job.prepared = self._prepare(analysis["result"]["skills"], kept.get("github"),  # type: ignore[index]
                                             self._set_stage(analysis_id, attempt))
            result, state = service.write_all(job.prepared, provider, self._set_stage(analysis_id, attempt))
        except LLMCallError as exc:
            failure = pipeline._llm_failure(str(exc))
            if failure.error["code"] == errors.AI_RATE_LIMITED and provider is not None:
                chain.mark_rate_limited(provider.model)  # type: ignore[arg-type]
                failure.error["fallback_available"] = chain.pick() is not None
            raise failure from None
        chain.mark_success(provider.model)  # type: ignore[arg-type]
        return result, state

    def _prepare(self, score_skills: Any, github: Any, set_stage: Callable[[str], None]) -> service.Prepared:
        try:
            return service.prepare(score_skills, github, self._deps.files_client_factory(), self._signals, set_stage)
        except github_files.GitHubFilesError as exc:
            raise errors.PipelineError(errors.INTERNAL_ERROR if exc.kind == github_files.NOT_CONFIGURED
                                       else errors.GITHUB_UNAVAILABLE) from None

    # -- replacing one question (synchronous) ---------------------------------------------------------
    def replace(self, analysis_id: str) -> dict:
        job = self._job(analysis_id)
        if job is None or job.state != DONE or job.prepared is None or job.run_state is None:
            raise QuestionError(409, errors.error_object(
                errors.ANALYSIS_NOT_READY, "The questions have not been written yet, so none can be replaced."))
        if not job.busy.acquire(blocking=False):
            raise QuestionError(409, errors.error_object(
                errors.QUESTIONS_ALREADY_STARTED, "A replacement is already being written. Please wait."))
        provider = None
        before = len(job.run_state.drops)
        try:
            provider = _RunProvider(self._deps, lambda model, fallback: None)
            outcome = service.replace_one(job.prepared, job.run_state, provider)
        except LLMCallError as exc:
            raise QuestionError(503, self._llm_error(exc, provider)) from None
        except errors.PipelineError as exc:
            raise QuestionError(503, exc.error) from None
        finally:
            job.busy.release()
        self._deps.chain.mark_success(provider.model)  # type: ignore[union-attr,arg-type]
        dropped = [dict(d) for d in job.run_state.drops[before:]]   # this replacement's own failed attempts
        if outcome is None:
            error = errors.error_object(errors.NO_REPLACEMENT_AVAILABLE)
            if dropped:
                error["dropped"] = dropped
            raise QuestionError(409, error)
        question, skipped = outcome
        return {"question": question, "skipped_skills": [dict(s) for s in skipped], "dropped": dropped}

    def _llm_error(self, exc: LLMCallError, provider: Optional[_RunProvider]) -> dict:
        failure = pipeline._llm_failure(str(exc))
        if failure.error["code"] == errors.AI_RATE_LIMITED and provider is not None:
            self._deps.chain.mark_rate_limited(provider.model)  # type: ignore[arg-type]
            failure.error["fallback_available"] = self._deps.chain.pick() is not None
        return failure.error
