"""FastAPI app and routes. Run with: uvicorn api.app:app --port 8000

Never logs request bodies, file contents, CV text, tokens or keys; only job ids, stages and error codes.
"""
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import PurePath
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as FormFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from skillverify import is_valid_username

from api import errors, pipeline, settings
from api.jobs import JobManager, StoreFullError
from api.questions import QuestionError, QuestionManager

log = logging.getLogger("skillverify.api")

MAX_MB = settings.MAX_UPLOAD_BYTES // (1024 * 1024)


class InvalidInput(Exception):
    """A request problem described in plain words."""


def _invalid(message: str) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": errors.error_object(errors.INVALID_INPUT, message)})


def _message(status: int, text: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"message": text})


async def _read_upload(upload: UploadFile, what: str, default_stem: str) -> pipeline.UploadedFile:
    """Check an uploaded file and keep it in memory. Only the extension and a sanitized stem are used."""
    name = PurePath((upload.filename or "").replace("\\", "/")).name
    extension = PurePath(name).suffix.lower()
    if extension not in settings.ALLOWED_EXTENSIONS:
        raise InvalidInput(f"The {what} must be a .pdf, .docx or .txt file.")
    data = await upload.read(settings.MAX_UPLOAD_BYTES + 1)
    if not data:
        raise InvalidInput(f"The {what} is empty.")
    if len(data) > settings.MAX_UPLOAD_BYTES:
        raise InvalidInput(f"The {what} is larger than {MAX_MB} MB.")
    return pipeline.UploadedFile(extension=extension, stem=pipeline.safe_stem(name, default_stem), data=data)


def _has_file(upload: object) -> bool:
    return isinstance(upload, FormFile) and bool(upload.filename)


async def build_inputs(jd_text: Optional[str], jd_file: object, cv_file: object, github_username: Optional[str],
                       job_name: Optional[str]) -> pipeline.JobInputs:
    """Validate the form and build the stored job inputs. Raises ``InvalidInput`` with a plain message."""
    has_text = jd_text is not None and jd_text.strip() != ""
    has_jd_file = _has_file(jd_file)
    if has_text and has_jd_file:
        raise InvalidInput("Give the job description as text or as a file, not both.")
    if not has_text and not has_jd_file:
        raise InvalidInput("The job description text is empty." if jd_text is not None
                           else "Give the job description as text or as a file.")
    if not _has_file(cv_file):
        raise InvalidInput("The CV file is required.")

    text = None
    jd_upload = None
    if has_text:
        text = jd_text  # the form parser already refuses pasted text above 1 MB (see handler below)
    else:
        jd_upload = await _read_upload(jd_file, "job description file", "job")  # type: ignore[arg-type]
    cv_upload = await _read_upload(cv_file, "CV file", "cv")  # type: ignore[arg-type]

    username = (github_username or "").strip() or None
    if username is not None and not is_valid_username(username):
        raise InvalidInput("That does not look like a GitHub username (letters, digits and hyphens only).")
    name = (job_name or "").strip()
    if not name:
        name = jd_upload.stem if jd_upload is not None else "job"
    elif pipeline.NAME_PATTERN.fullmatch(name) is None:
        raise InvalidInput("The job name must be 1-60 characters: letters, digits, '.', '_' or '-'.")
    return pipeline.JobInputs(jd_text=text, jd_file=jd_upload, cv_file=cv_upload, github_username=username,
                              job_name=name)


def create_app(deps: Optional[pipeline.Dependencies] = None) -> FastAPI:
    deps = deps if deps is not None else pipeline.default_dependencies()
    manager = JobManager(deps)
    chain = deps.chain
    questions = QuestionManager(manager, deps)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        manager.shutdown()
        questions.shutdown()

    app = FastAPI(title="SkillVerify API", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.jobs = manager
    app.state.questions = questions
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.ALLOWED_ORIGINS), allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type"], allow_credentials=False)

    @app.exception_handler(RequestValidationError)
    async def bad_request(_: Request, __: RequestValidationError) -> JSONResponse:
        return _invalid("The request could not be understood. Check the form fields and files.")

    @app.exception_handler(StarletteHTTPException)
    async def http_problem(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 400:  # the form could not be parsed (broken body, or pasted text above 1 MB)
            return _invalid("The request could not be read. Check the form fields and files "
                            "(pasted text can be at most 1 MB; use a file for longer text).")
        return await http_exception_handler(request, exc)

    @app.exception_handler(Exception)
    async def unexpected(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content={"error": errors.error_object(errors.INTERNAL_ERROR)})

    @app.get("/api/health")
    async def health() -> dict:
        load_dotenv()  # real environment variables win; only presence is reported, never a value
        return {"status": "ok",
                "gemini_key_set": bool(os.environ.get("GEMINI_API_KEY", "").strip()),
                "github_token_set": bool(os.environ.get("GITHUB_TOKEN", "").strip()),
                "model_chain": list(chain.models), "models": chain.snapshot(),
                "limits": settings.limits()}

    @app.post("/api/analyses", status_code=202)
    async def create_analysis(jd_text: Optional[str] = Form(None), jd_file: Optional[UploadFile] = File(None),
                              cv_file: Optional[UploadFile] = File(None), github_username: Optional[str] = Form(None),
                              job_name: Optional[str] = Form(None)):
        try:
            inputs = await build_inputs(jd_text, jd_file, cv_file, github_username, job_name)
        except InvalidInput as problem:
            return _invalid(str(problem))
        try:
            job_id = manager.create(inputs)
        except StoreFullError:
            return JSONResponse(status_code=503, content={"error": errors.error_object(
                errors.INTERNAL_ERROR, "Too many analyses are running right now. Please try again in a moment.")})
        return {"id": job_id}

    @app.get("/api/analyses/{job_id}")
    async def get_analysis(job_id: str):
        snapshot = manager.snapshot(job_id)
        return snapshot if snapshot is not None else _message(404, "That analysis was not found. It may have expired.")

    @app.post("/api/analyses/{job_id}/retry", status_code=202)
    async def retry_analysis(job_id: str):
        outcome = manager.retry(job_id)
        if outcome is None:
            return _message(404, "That analysis was not found. It may have expired.")
        if not outcome:
            return _message(409, "Only an analysis that failed with a retryable error can be retried.")
        return {"id": job_id}

    # -- questions: a sub-resource of a finished analysis ---------------------------------------------
    def _not_found() -> JSONResponse:
        return _message(404, "That analysis was not found. It may have expired.")

    @app.exception_handler(QuestionError)
    async def question_problem(_: Request, exc: QuestionError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"error": exc.error})

    @app.post("/api/analyses/{job_id}/questions", status_code=202)
    async def start_questions(job_id: str):
        return questions.start(job_id)

    @app.get("/api/analyses/{job_id}/questions")
    async def get_questions(job_id: str):
        status = questions.snapshot(job_id)
        return status if status is not None else _not_found()

    @app.post("/api/analyses/{job_id}/questions/retry", status_code=202)
    async def retry_questions(job_id: str):
        outcome = questions.retry(job_id)
        if outcome is None:
            return _not_found()
        if not outcome:
            return _message(409, "Only questions that failed with a retryable error can be retried.")
        return questions.snapshot(job_id)

    @app.post("/api/analyses/{job_id}/questions/replace")
    async def replace_question(job_id: str, request: Request):
        raw = await request.body()
        try:
            body = json.loads(raw) if raw.strip() else {}
        except ValueError:
            body = None
        after = body.get("after") if isinstance(body, dict) else None
        if not isinstance(body, dict) or (after is not None and (not isinstance(after, str) or len(after) > 64)):
            return _invalid("The body must be empty or a JSON object like {\"after\": \"q_1234abcd\"}.")
        if questions.snapshot(job_id) is None:
            return _not_found()
        return await run_in_threadpool(questions.replace, job_id)  # writes with Gemini: keep the event loop free

    return app


app = create_app()
