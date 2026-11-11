"""The closed set of error codes, their plain messages, and the error object shape."""
from typing import Optional

INVALID_INPUT = "invalid_input"  # HTTP 422 only
JD_UNREADABLE = "jd_unreadable"
CV_UNREADABLE = "cv_unreadable"
GITHUB_USER_NOT_FOUND = "github_user_not_found"
GITHUB_UNAVAILABLE = "github_unavailable"
AI_BUSY = "ai_busy"
AI_RATE_LIMITED = "ai_rate_limited"
AI_ALL_LIMITED = "ai_all_limited"
AI_NOT_CONFIGURED = "ai_not_configured"
TIMEOUT = "timeout"
INTERNAL_ERROR = "internal_error"
ANALYSIS_NOT_READY = "analysis_not_ready"            # questions: HTTP 409 only
QUESTIONS_ALREADY_STARTED = "questions_already_started"  # questions: HTTP 409 only
NO_REPLACEMENT_AVAILABLE = "no_replacement_available"    # questions: HTTP 409 only

MESSAGES = {
    INVALID_INPUT: "Please check the information you entered and try again.",
    JD_UNREADABLE: "The job description could not be read.",
    CV_UNREADABLE: "The CV could not be read.",
    GITHUB_USER_NOT_FOUND: "That GitHub username was not found. Check the spelling and try again.",
    GITHUB_UNAVAILABLE: ("GitHub could not be reached or its request limit was reached. "
                         "Wait a few minutes and try again."),
    AI_BUSY: "The AI service is busy right now. Please try again.",
    AI_RATE_LIMITED: "The AI service's request limit was reached. Wait a little while and try again.",
    AI_ALL_LIMITED: "All available AI models have reached their limits. Wait a few minutes and try again.",
    AI_NOT_CONFIGURED: "The AI service is not set up on this server. Check the server configuration.",
    TIMEOUT: "The analysis took too long and was stopped. Please try again.",
    INTERNAL_ERROR: "Something went wrong on the server. Please try again.",
    ANALYSIS_NOT_READY: "The analysis is not finished or has no usable score, so questions cannot be written yet.",
    QUESTIONS_ALREADY_STARTED: "Questions were already started for this analysis. Check their status instead.",
    NO_REPLACEMENT_AVAILABLE: "There is no other code in the candidate's repos to ask about.",
}

RETRYABLE = {
    INVALID_INPUT: False, JD_UNREADABLE: False, CV_UNREADABLE: False, GITHUB_USER_NOT_FOUND: False,
    GITHUB_UNAVAILABLE: True, AI_BUSY: True, AI_RATE_LIMITED: True, AI_ALL_LIMITED: True, AI_NOT_CONFIGURED: False,
    TIMEOUT: True, INTERNAL_ERROR: True, ANALYSIS_NOT_READY: False, QUESTIONS_ALREADY_STARTED: False,
    NO_REPLACEMENT_AVAILABLE: False,
}

CODES = tuple(MESSAGES)

MAX_MESSAGE_CHARS = 300


def error_object(code: str, message: Optional[str] = None, fallback_available: Optional[bool] = None,
                 stage: Optional[str] = None) -> dict:
    """``{"code", "message", "retryable"}`` (plus ``fallback_available`` and, for a timeout, ``stage`` when given).
    Only closed-set codes; ``stage`` is one of the fixed stage names."""
    if code not in MESSAGES:
        raise ValueError("unknown error code")
    text = (message or MESSAGES[code])[:MAX_MESSAGE_CHARS]
    error = {"code": code, "message": text, "retryable": RETRYABLE[code]}
    if fallback_available is not None:
        error["fallback_available"] = fallback_available
    if stage is not None:
        error["stage"] = stage
    return error


class PipelineError(Exception):
    """A pipeline failure with a code from the closed set and a plain, safe message."""

    def __init__(self, code: str, message: Optional[str] = None) -> None:
        self.error = error_object(code, message)
        super().__init__(self.error["message"])
