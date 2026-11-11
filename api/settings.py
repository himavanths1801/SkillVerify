"""Constants for the API. Change a value here, nowhere else."""
import logging
import os
import re
import threading
from typing import Mapping, Optional

from dotenv import load_dotenv

from skillverify.jd.provider import _MODEL_NAME  # the very pattern GeminiProvider validates a model name with

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # per file, and for pasted job description text
ALLOWED_EXTENSIONS = (".pdf", ".docx", ".txt")

# Outer retry layer around Gemini: waits (seconds) before each retry of a temporary failure.
# The provider itself already retries 500/502/503/504 twice (2 s, 6 s); this is on top of that.
GEMINI_OUTER_RETRY_DELAYS = (5, 10, 20)

# Time limits and the repository cap. The defaults can be changed with ANALYSIS_TIMEOUT_SECONDS,
# QUESTIONS_TIMEOUT_SECONDS and SCAN_MAX_REPOS (environment or .env); see ``limits()``.
DEFAULT_ANALYSIS_TIMEOUT_SECONDS = 6 * 60   # unchanged from the old JOB_TIMEOUT_SECONDS (already above 300)
DEFAULT_QUESTIONS_TIMEOUT_SECONDS = 6 * 60  # unchanged: the questions run shared the same old constant
DEFAULT_SCAN_MAX_REPOS = 30
TIMEOUT_RANGE = (30, 1800)
SCAN_MAX_REPOS_RANGE = (5, 200)
MAX_WORKERS = 2          # jobs that run at the same time; others wait as "queued"
MAX_JOBS = 20            # jobs kept in memory
JOB_TTL_SECONDS = 60 * 60

ALLOWED_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")

# Gemini model chain: models are tried in this order. Different models have separate quotas, so a model that hit a
# rate limit rests for a while and the next run uses the next model. Model names change; this is only the default.
DEFAULT_MODEL_CHAIN = ("gemini-3.6-flash", "gemini-3.5-flash-lite")
MAX_CHAIN_MODELS = 8
RATE_LIMIT_COOLDOWN_SECONDS = 120
REPEATED_LIMIT_COOLDOWN_SECONDS = 30 * 60  # limited twice in a row, no success between: probably the daily quota
MODEL_UNAVAILABLE_SECONDS = 60 * 60        # a model name Gemini does not know

_log = logging.getLogger("skillverify.api")


def load_model_chain(environ: Mapping[str, str]) -> tuple[str, ...]:
    """The ordered model chain from GEMINI_MODEL_CHAIN, else GEMINI_MODEL + the default chain, else the default.

    Invalid names are dropped (one plain log line, never the text itself), duplicates removed, at most 8 kept.
    """
    raw = (environ.get("GEMINI_MODEL_CHAIN") or "").strip()
    if raw:
        entries = raw.split(",")
    else:
        single = (environ.get("GEMINI_MODEL") or "").strip()
        entries = ([single] if single else []) + list(DEFAULT_MODEL_CHAIN)
    chain: list[str] = []
    dropped = []
    for position, entry in enumerate(entries, start=1):
        name = entry.strip()
        if not name:
            continue
        if _MODEL_NAME.fullmatch(name) is None:
            dropped.append(position)
        elif name not in chain:
            chain.append(name)
    if dropped:
        _log.warning("Ignored %d invalid Gemini model name(s) in the model settings (entry %s).", len(dropped),
                     ", ".join(str(n) for n in dropped))
    return tuple(chain[:MAX_CHAIN_MODELS]) or DEFAULT_MODEL_CHAIN


# --------------------------------------------------------------------------
# time limits and the repository cap (read lazily, so importing the app never reads the .env file)
# --------------------------------------------------------------------------
_SETTINGS = (  # (key in the limits dict, environment variable, default, allowed range)
    ("analysis_timeout_seconds", "ANALYSIS_TIMEOUT_SECONDS", DEFAULT_ANALYSIS_TIMEOUT_SECONDS, TIMEOUT_RANGE),
    ("questions_timeout_seconds", "QUESTIONS_TIMEOUT_SECONDS", DEFAULT_QUESTIONS_TIMEOUT_SECONDS, TIMEOUT_RANGE),
    ("scan_max_repos", "SCAN_MAX_REPOS", DEFAULT_SCAN_MAX_REPOS, SCAN_MAX_REPOS_RANGE),
)


def load_limits(environ: Mapping[str, str]) -> dict[str, int]:
    """The three numbers from the environment. A missing value is the default; a non-numeric or out-of-range value
    is the default too, with ONE plain log line that names the setting and never prints the value."""
    values: dict[str, int] = {}
    for key, variable, default, (low, high) in _SETTINGS:
        raw = (environ.get(variable) or "").strip()
        value = default
        if raw:
            try:
                number = int(raw)
            except ValueError:
                number = None
            if number is not None and low <= number <= high:
                value = number
            else:
                _log.warning("Ignored an invalid %s setting (allowed %d to %d); using the default.", variable, low, high)
        values[key] = value
    return values


_limits_lock = threading.Lock()
_limits_cache: Optional[dict[str, int]] = None


def limits() -> dict[str, int]:
    global _limits_cache
    with _limits_lock:
        if _limits_cache is None:
            load_dotenv()  # real environment variables win over the .env file
            _limits_cache = load_limits(os.environ)
        return dict(_limits_cache)


def reset_limits() -> None:
    """Forget the loaded values (tests, or after the environment changed)."""
    global _limits_cache
    with _limits_lock:
        _limits_cache = None


def analysis_timeout_seconds() -> float:
    return limits()["analysis_timeout_seconds"]


def questions_timeout_seconds() -> float:
    return limits()["questions_timeout_seconds"]


def scan_max_repos() -> int:
    return limits()["scan_max_repos"]
