"""The real Phase 1 pipeline for one candidate, in the same call order as the example scripts.

JD text/file -> JDParser -> CV file -> CVParser -> GitHub validate + extended tech scan -> score_candidate.

Nothing is written to the Phase 1 data: the parsers run with ``persist=False`` and ``auto_add=False``.
Uploaded bytes are written to a temporary folder only long enough to extract text, then deleted.
"""
import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from skillverify import GitHubConfigError, GitHubValidator
from skillverify.cv import CVExtractError, CVParser, extract_text
from skillverify.cv.parser import label_from_path
from skillverify.jd import GeminiProvider, JDParser, LLMConfigError
from skillverify.scoring import ScoringConfigError, score_candidate
from skillverify.sql_strict import StrictSqlTechScanner
from skillverify.taxonomy import TaxonomyError
from skillverify.taxonomy_extra import load_extended_taxonomy, promote_label_items
from skillverify.tech_scanner import TechScannerError

from api import errors, meta, repocap, settings
from api.models import ModelChain
from skillverify_questions.github_files import GitHubFiles
from api.retry import RetryingProvider, is_temporary

log = logging.getLogger("skillverify.api")

STAGE_QUEUED = "Analyzing, this can take a moment."
STAGE_JD = "Reading the job description"
STAGE_CV = "Reading the CV"
STAGE_GITHUB = "Checking GitHub"
STAGE_SCAN = "Scanning repositories"
STAGE_SCORING = "Scoring"
STAGE_DONE = "Done"

NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,60}")  # the same rule the parsers apply to job names and labels


@dataclass(frozen=True)
class UploadedFile:
    """An uploaded file kept in memory. Only the extension and a sanitized stem are ever used."""
    extension: str
    stem: str
    data: bytes


@dataclass(frozen=True)
class JobInputs:
    jd_text: Optional[str]
    jd_file: Optional[UploadedFile]
    cv_file: UploadedFile
    github_username: Optional[str]  # as typed by the user, already validated
    job_name: str


@dataclass
class Dependencies:
    """Everything that talks to the outside world, so tests can inject fakes."""
    provider_factory: Callable[..., Any]  # (model) -> an LLM provider
    validator_factory: Callable[[], Any]
    scanner_factory: Callable[[Any, Any], Any]  # (validator, taxonomy) -> scanner
    chain: ModelChain = field(default_factory=ModelChain)  # which Gemini model each run uses
    files_client_factory: Callable[[], Any] = GitHubFiles  # reads file trees and contents for the questions


def _gemini(model: Optional[str] = None) -> GeminiProvider:
    return GeminiProvider(model=model)


def default_dependencies() -> Dependencies:
    return Dependencies(
        provider_factory=_gemini,
        validator_factory=GitHubValidator,
        scanner_factory=lambda validator, taxonomy: StrictSqlTechScanner(
            validator, taxonomy=taxonomy, max_repos=settings.scan_max_repos()),
    )


@dataclass
class PipelineOutput:
    result: dict
    jd: dict
    cv: dict
    github: Optional[dict] = field(default=None)


def safe_stem(name: str, default: str) -> str:
    """A safe label from an uploaded file name (never used as a path)."""
    stem = label_from_path(name)
    return stem if NAME_PATTERN.fullmatch(stem) and stem.strip(".") else default


def _extract(upload: UploadedFile, unreadable: str) -> dict:
    """Write the bytes to a temporary folder, extract the text, and always delete the folder."""
    with tempfile.TemporaryDirectory(prefix="skillverify_api_") as folder:
        path = Path(folder) / f"upload{upload.extension}"
        path.write_bytes(upload.data)
        try:
            return extract_text(path)
        except CVExtractError as err:
            raise errors.PipelineError(unreadable, err.message) from None


def _llm_failure(message: str) -> errors.PipelineError:
    """Map a Gemini failure message (from the provider) to one of our codes. The text is never echoed."""
    if "HTTP 429" in message:
        return errors.PipelineError(errors.AI_RATE_LIMITED)
    if "rejected the API key" in message or "was not found (HTTP 404)" in message:
        return errors.PipelineError(errors.AI_NOT_CONFIGURED)
    if is_temporary(message):
        return errors.PipelineError(errors.AI_BUSY)
    return errors.PipelineError(errors.INTERNAL_ERROR)


def _check_parsed(result: Any, unreadable: str, what: str) -> dict:
    if not isinstance(result, dict):
        raise errors.PipelineError(errors.INTERNAL_ERROR)
    status = result.get("status")
    if status == "ok":
        return result
    message = str(result.get("message") or "")
    if status == "llm_error":
        raise _llm_failure(message)
    raise errors.PipelineError(unreadable, f"The {what} could not be used. {message}".strip())


def _github_evidence(username: str, deps: Dependencies, set_stage: Callable[[str], None],
                     publish: Optional[Callable[..., None]] = None) -> dict:
    """The validator result plus its ``tech_stack``, exactly like the saved file of run_tech_scan_ext.py.

    GitHub failures are plain errors: no retries, no fallback, never a partial score.
    """
    set_stage(STAGE_GITHUB)
    try:
        taxonomy = load_extended_taxonomy()
        validator = deps.validator_factory()
        scanner = deps.scanner_factory(validator, taxonomy)
    except (GitHubConfigError, TaxonomyError, TechScannerError):
        raise errors.PipelineError(errors.INTERNAL_ERROR) from None
    cap = settings.scan_max_repos()
    listing = repocap.limit_listing(validator, cap)  # only the newest ``cap`` repos are inspected and scanned
    try:
        validation = validator.validate(username)
    except Exception:  # noqa: BLE001 - anything the validator raises means GitHub failed
        raise errors.PipelineError(errors.GITHUB_UNAVAILABLE) from None
    status = validation.get("status") if isinstance(validation, dict) else None
    if status == "user_not_found":
        raise errors.PipelineError(errors.GITHUB_USER_NOT_FOUND)
    if status == "no_public_repos":
        return validation  # nothing to scan; score_candidate reports "no_github_evidence"
    if status != "ok":  # rate_limited, error, or anything unexpected
        raise errors.PipelineError(errors.GITHUB_UNAVAILABLE)
    set_stage(STAGE_SCAN)
    if publish is not None:
        publish(scan=listing.meta())
    try:
        tech = promote_label_items(scanner.scan(validation), taxonomy)
    except Exception:  # noqa: BLE001
        raise errors.PipelineError(errors.GITHUB_UNAVAILABLE) from None
    if not isinstance(tech, dict) or tech.get("status") in ("error", "skipped"):
        raise errors.PipelineError(errors.GITHUB_UNAVAILABLE)
    return {**validation, "tech_stack": tech}


_MODEL_NOT_FOUND = "was not found (HTTP 404)"


def _start_ai(inputs: JobInputs, deps: Dependencies, set_stage: Callable[[str], None],
              publish: Callable[..., None], picked: dict) -> tuple[Any, dict, str]:
    """Pick the model, read the job description with it, and return (provider, jd_result, jd_text).

    A model Gemini does not know (HTTP 404 on this first call) is set aside and the next model is used straight
    away, inside the same run. Each model is tried at most once.
    """
    chain = deps.chain
    set_stage(STAGE_JD)
    jd_text = inputs.jd_text if inputs.jd_file is None else _extract(inputs.jd_file, errors.JD_UNREADABLE)["text"]
    for _ in range(len(chain.models)):
        model = chain.pick()
        if model is None:
            break
        picked["model"] = model
        publish(model=model, fallback_used=model != chain.models[0])
        try:
            provider = RetryingProvider(deps.provider_factory(model))
        except LLMConfigError:
            raise errors.PipelineError(errors.AI_NOT_CONFIGURED) from None
        try:
            with JDParser(provider, auto_add=False, persist=False) as jd_parser:
                jd_result = jd_parser.parse(jd_text, inputs.job_name)
        except LLMConfigError:
            raise errors.PipelineError(errors.AI_NOT_CONFIGURED) from None
        if isinstance(jd_result, dict) and jd_result.get("status") == "llm_error"                 and _MODEL_NOT_FOUND in str(jd_result.get("message") or ""):
            chain.mark_unavailable(model)
            continue
        return provider, _check_parsed(jd_result, errors.JD_UNREADABLE, "job description"), jd_text
    picked["model"] = None
    raise errors.PipelineError(errors.AI_ALL_LIMITED)


def run_pipeline(inputs: JobInputs, deps: Dependencies, set_stage: Callable[[str], None],
                 set_meta: Optional[Callable[..., None]] = None) -> PipelineOutput:
    """Run the whole pipeline. Raises ``PipelineError`` (code + plain message) on any failure.

    One run uses one Gemini model for every call. A rate limit (HTTP 429) rests that model, so the next run
    (a retry or a new analysis) takes the next model in the chain.
    """
    picked: dict = {"model": None}
    try:
        output = _run(inputs, deps, set_stage, set_meta or (lambda **_: None), picked)
    except errors.PipelineError as failure:
        if failure.error["code"] == errors.AI_RATE_LIMITED and picked["model"]:
            deps.chain.mark_rate_limited(picked["model"])
            failure.error["fallback_available"] = deps.chain.pick() is not None
        raise
    deps.chain.mark_success(picked["model"])
    return output


def _run(inputs: JobInputs, deps: Dependencies, set_stage: Callable[[str], None], publish: Callable[..., None],
         picked: dict) -> PipelineOutput:
    try:
        # 1. Job description (and the model for this whole run)
        provider, jd_result, jd_text = _start_ai(inputs, deps, set_stage, publish, picked)
        publish(job_title=meta.job_title(jd_text))

        # 2. CV
        set_stage(STAGE_CV)
        extracted = _extract(inputs.cv_file, errors.CV_UNREADABLE)
        info = {"format": extracted["format"], "pages": extracted["pages"], "characters": extracted["characters"],
                "truncated": extracted["truncated"], "links_found": len(extracted["links"])}
        with CVParser(provider, auto_add=False, persist=False) as cv_parser:
            cv_result = _check_parsed(
                cv_parser.parse_text(extracted["text"], inputs.cv_file.stem, extracted["links"], info),
                errors.CV_UNREADABLE, "CV")
        publish(candidate_name=meta.candidate_name(cv_result))
    except LLMConfigError:
        raise errors.PipelineError(errors.AI_NOT_CONFIGURED) from None

    # 3. GitHub: the username the user typed, else the one found in the CV, else none.
    cv_username = (cv_result.get("github") or {}).get("username")
    username = inputs.github_username or cv_username
    github_result = _github_evidence(username, deps, set_stage, publish) if username else None

    # 4. Score. A username typed by the user is used even if the CV names another (the score flags it).
    set_stage(STAGE_SCORING)
    try:
        result = score_candidate(jd_result, cv_result, github_result,
                                 allow_github_mismatch=inputs.github_username is not None)
    except (ScoringConfigError, TaxonomyError):
        raise errors.PipelineError(errors.INTERNAL_ERROR) from None
    if result.get("status") == "invalid_input":
        raise errors.PipelineError(errors.INTERNAL_ERROR)
    return PipelineOutput(result=result, jd=jd_result, cv=cv_result, github=github_result)
