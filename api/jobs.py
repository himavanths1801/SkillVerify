"""In-memory job store and worker pool. Thread-safe; nothing is persisted."""
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from api import errors, pipeline, settings

log = logging.getLogger("skillverify.api")

_now = time.time  # a module-level reference, so tests can move the clock
_clock = time.monotonic  # measures how long each stage takes (tests replace it)

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"


class StoreFullError(Exception):
    """Every kept job is still queued or running, so there is no room for another one."""


def blank_meta() -> dict:
    return {"candidate_name": None, "job_title": None, "model": None, "fallback_used": False,
            "scan": None, "timings": {}}


@dataclass
class Job:
    id: str
    inputs: pipeline.JobInputs
    created: float
    state: str = QUEUED
    stage: str = pipeline.STAGE_QUEUED
    result: Optional[dict] = None
    error: Optional[dict] = None
    attempt: int = 0
    meta: dict = field(default_factory=lambda: blank_meta())
    # Kept for the later question generator; never returned by the API yet.
    intermediates: dict = field(default_factory=dict)
    # The stage being timed (None before the first stage) and when it began.
    timed_stage: Optional[str] = None
    timed_since: float = 0.0


class JobManager:
    def __init__(self, deps: Optional[pipeline.Dependencies] = None,
                 runner: Callable[..., pipeline.PipelineOutput] = pipeline.run_pipeline) -> None:
        self._deps = deps if deps is not None else pipeline.default_dependencies()
        self._runner = runner
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=settings.MAX_WORKERS, thread_name_prefix="analysis")

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # -- store -----------------------------------------------------------------
    def _purge(self) -> None:
        """Drop jobs older than the time-to-live. The caller holds the lock."""
        cutoff = _now() - settings.JOB_TTL_SECONDS
        for job_id in [j.id for j in self._jobs.values() if j.created < cutoff]:
            del self._jobs[job_id]

    def create(self, inputs: pipeline.JobInputs) -> str:
        with self._lock:
            self._purge()
            if len(self._jobs) >= settings.MAX_JOBS:
                finished = sorted((j for j in self._jobs.values() if j.state in (DONE, FAILED)),
                                  key=lambda j: j.created)
                if not finished:
                    raise StoreFullError
                del self._jobs[finished[0].id]
            job = Job(id=str(uuid.uuid4()), inputs=inputs, created=_now())
            self._jobs[job.id] = job
            attempt = job.attempt
        self._submit(job.id, attempt)
        return job.id

    def snapshot(self, job_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            self._purge()
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return {"id": job.id, "state": job.state, "stage": job.stage, "result": job.result, "error": job.error,
                    "meta": dict(job.meta)}

    def intermediates(self, job_id: str) -> Optional[dict]:
        """The JD, CV and GitHub results of a finished job (for the later question generator)."""
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job.intermediates) if job is not None else None

    def retry(self, job_id: str) -> Optional[bool]:
        """None: unknown id. False: not retryable. True: a fresh run started from the stored inputs."""
        with self._lock:
            self._purge()
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.state != FAILED or not (job.error and job.error["retryable"]):
                return False
            job.state, job.stage = QUEUED, pipeline.STAGE_QUEUED
            job.result = job.error = None
            job.intermediates = {}
            job.meta = blank_meta()
            job.timed_stage = None
            job.attempt += 1
            attempt = job.attempt
        self._submit(job_id, attempt)
        return True

    # -- running ---------------------------------------------------------------
    def _submit(self, job_id: str, attempt: int) -> None:
        self._pool.submit(self._execute, job_id, attempt)

    def _execute(self, job_id: str, attempt: int) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.attempt != attempt or job.state != QUEUED:
                return
            job.state = RUNNING
            inputs = job.inputs
        outcome: dict[str, Any] = {}

        def work() -> None:
            def set_stage(text: str) -> None:
                with self._lock:
                    job = self._jobs.get(job_id)
                    if job is None or job.attempt != attempt or job.state != RUNNING:
                        return
                    self._close_stage(job)
                    job.stage, job.timed_stage, job.timed_since = text, text, _clock()
                log.info("job %s stage: %s", job_id, text)
            def set_meta(**values: Any) -> None:
                with self._lock:
                    job = self._jobs.get(job_id)
                    if job is not None and job.attempt == attempt and job.state == RUNNING:
                        job.meta.update(values)
            try:
                outcome["output"] = self._runner(inputs, self._deps, set_stage, set_meta)
            except errors.PipelineError as exc:
                outcome["error"] = exc.error
            except Exception:  # noqa: BLE001 - never expose exception text, paths or secrets
                outcome["error"] = errors.error_object(errors.INTERNAL_ERROR)

        thread = threading.Thread(target=work, name=f"analysis-{job_id[:8]}", daemon=True)
        thread.start()
        # A run that is still going after the limit is abandoned, not stopped: Python cannot stop a thread, and
        # the frozen validator and scanner make blocking calls. Its late writes are ignored (the state changes).
        thread.join(settings.analysis_timeout_seconds())
        timed_out = thread.is_alive()
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.attempt != attempt or job.state != RUNNING:
                return
            stage = self._close_stage(job)  # the stage that was running, with the time spent so far
            if timed_out:
                error = errors.error_object(errors.TIMEOUT, stage=stage)
            else:
                error = outcome.get("error")
            if error is None:
                out = outcome["output"]
                job.state, job.stage, job.result = DONE, pipeline.STAGE_DONE, out.result
                job.intermediates = {"jd": out.jd, "cv": out.cv, "github": out.github}
                log.info("job %s done", job_id)
            else:
                job.state, job.error = FAILED, error
                log.info("job %s failed: %s", job_id, error["code"])

    def _close_stage(self, job: Job) -> Optional[str]:
        """Record how long the timed stage ran in ``meta.timings`` and log it. The caller holds the lock."""
        name = job.timed_stage
        if name is not None:
            seconds = round(max(0.0, _clock() - job.timed_since), 1)
            job.meta["timings"][name] = seconds
            job.timed_stage = None
            log.info('stage="%s" seconds=%.1f', name, seconds)
        return name
