"""Constants for the API. Change a value here, nowhere else."""
import logging
import os
import re
import threading
from pathlib import Path
from typing import Mapping, Optional

from dotenv import load_dotenv

from skillverify.jd.provider import _MODEL_NAME  # the very pattern GeminiProvider validates a model name with
from skillverify_questions.groq_provider import DEFAULT_GROQ_MODEL, GROQ_MODEL_NAME

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
DEFAULT_QUESTIONS_MIN_GAP_SECONDS = 3      # pause between two calls to the SAME model
QUESTIONS_MIN_GAP_RANGE = (0, 30)
DEFAULT_QUESTIONS_MAX_WAIT_SECONDS = 25    # when every model is resting, wait at most this long for the first to be ready
QUESTIONS_MAX_WAIT_RANGE = (0, 120)
DEFAULT_READING_MIN_GAP_SECONDS = 2        # pause between two calls to the SAME model while reading the CV / job description
READING_MIN_GAP_RANGE = (0, 30)
DEFAULT_READING_MAX_WAIT_SECONDS = 20      # when every reading model is resting, wait at most this long for the first
READING_MAX_WAIT_RANGE = (0, 60)
DEFAULT_QUESTIONS_MAX_OUTPUT_TOKENS = 0    # 0: send no output cap to Groq (its own default applies), as before
QUESTIONS_MAX_OUTPUT_TOKENS_RANGE = (500, 8000)  # a value set in QUESTIONS_MAX_OUTPUT_TOKENS must be inside this range
DEFAULT_SAVED_RESULTS_DAYS = 14
SAVED_RESULTS_DAYS_RANGE = (1, 90)
DATA_DIR_VARIABLE = "SKILLVERIFY_DATA_DIR"
DEFAULT_DATA_DIR = "_data"                 # api/_data/, next to this file
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

# The question writer's chain can mix providers: a plain name is a Gemini model, "groq:<id>" is a Groq model.
GROQ_PREFIX = "groq:"
DEFAULT_GROQ_ENTRY = GROQ_PREFIX + DEFAULT_GROQ_MODEL
GROQ_OUTER_RETRY_DELAYS = (2,)             # one quiet retry of a temporary Groq failure; Gemini is the fallback

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


def _mixed_chain(environ: Mapping[str, str], raw: str, default_entries: list[str], fallback: list[str],
                 what: str) -> tuple[str, ...]:
    """A chain that can mix providers (plain name = Gemini, "groq:<id>" = Groq), from ``raw`` or else the defaults.

    Without a GROQ_API_KEY every ``groq:`` entry is dropped silently. Invalid names are dropped with one plain log
    line (``what`` names the setting group; the entry text is never printed). An empty result is ``fallback``.
    """
    entries = raw.split(",") if raw else default_entries
    has_groq_key = bool((environ.get("GROQ_API_KEY") or "").strip())
    chain: list[str] = []
    dropped = []
    for position, entry in enumerate(entries, start=1):
        name = entry.strip()
        if not name:
            continue
        is_groq = name.lower().startswith(GROQ_PREFIX)
        valid = (GROQ_MODEL_NAME.fullmatch(name[len(GROQ_PREFIX):]) if is_groq else _MODEL_NAME.fullmatch(name)) is not None
        if not valid:
            dropped.append(position)
        elif is_groq and not has_groq_key:
            continue  # no Groq key: Groq entries are skipped silently
        else:
            name = GROQ_PREFIX + name[len(GROQ_PREFIX):] if is_groq else name
            if name not in chain:
                chain.append(name)
    if dropped:
        _log.warning("Ignored %d invalid model name(s) in the %s model settings (entry %s).", len(dropped), what,
                     ", ".join(str(n) for n in dropped))
    return tuple(chain[:MAX_CHAIN_MODELS]) or tuple(fallback[:MAX_CHAIN_MODELS])


def load_questions_chain(environ: Mapping[str, str]) -> tuple[str, ...]:
    """The question writer's chain from QUESTIONS_MODEL_CHAIN (same parsing rules as GEMINI_MODEL_CHAIN).

    Unset: the Groq default first, then the Gemini chain exactly as ``load_model_chain`` gives it (so
    GEMINI_MODEL_CHAIN is still honoured). Without a GROQ_API_KEY every ``groq:`` entry is dropped silently, so the
    app then behaves exactly as it did with Gemini only. Invalid names are dropped with one plain log line.
    """
    gemini_default = list(load_model_chain(environ))
    return _mixed_chain(environ, (environ.get("QUESTIONS_MODEL_CHAIN") or "").strip(),
                        [DEFAULT_GROQ_ENTRY] + gemini_default, gemini_default, "question")


def load_reading_chain(environ: Mapping[str, str]) -> tuple[str, ...]:
    """The chain that reads the CV and the job description, from READING_MODEL_CHAIN (same format as
    QUESTIONS_MODEL_CHAIN). Unset: the Gemini chain exactly as ``load_model_chain`` gives it. Groq is NEVER in the
    default: it reads only when the owner adds a ``groq:`` entry (and a GROQ_API_KEY is set)."""
    gemini_default = list(load_model_chain(environ))
    return _mixed_chain(environ, (environ.get("READING_MODEL_CHAIN") or "").strip(), gemini_default, gemini_default,
                        "reading")


# --------------------------------------------------------------------------
# time limits and the repository cap (read lazily, so importing the app never reads the .env file)
# --------------------------------------------------------------------------
_SETTINGS = (  # (key in the limits dict, environment variable, default, allowed range)
    ("analysis_timeout_seconds", "ANALYSIS_TIMEOUT_SECONDS", DEFAULT_ANALYSIS_TIMEOUT_SECONDS, TIMEOUT_RANGE),
    ("questions_timeout_seconds", "QUESTIONS_TIMEOUT_SECONDS", DEFAULT_QUESTIONS_TIMEOUT_SECONDS, TIMEOUT_RANGE),
    ("scan_max_repos", "SCAN_MAX_REPOS", DEFAULT_SCAN_MAX_REPOS, SCAN_MAX_REPOS_RANGE),
    ("questions_min_gap_seconds", "QUESTIONS_MIN_GAP_SECONDS", DEFAULT_QUESTIONS_MIN_GAP_SECONDS, QUESTIONS_MIN_GAP_RANGE),
    ("questions_max_wait_seconds", "QUESTIONS_MAX_WAIT_SECONDS", DEFAULT_QUESTIONS_MAX_WAIT_SECONDS,
     QUESTIONS_MAX_WAIT_RANGE),
    ("reading_min_gap_seconds", "READING_MIN_GAP_SECONDS", DEFAULT_READING_MIN_GAP_SECONDS, READING_MIN_GAP_RANGE),
    ("reading_max_wait_seconds", "READING_MAX_WAIT_SECONDS", DEFAULT_READING_MAX_WAIT_SECONDS, READING_MAX_WAIT_RANGE),
    ("questions_max_output_tokens", "QUESTIONS_MAX_OUTPUT_TOKENS", DEFAULT_QUESTIONS_MAX_OUTPUT_TOKENS,
     QUESTIONS_MAX_OUTPUT_TOKENS_RANGE),
    ("saved_results_days", "SAVED_RESULTS_DAYS", DEFAULT_SAVED_RESULTS_DAYS, SAVED_RESULTS_DAYS_RANGE),
)


def load_limits(environ: Mapping[str, str]) -> dict[str, int]:
    """The numbers from the environment. A missing value is the default; a non-numeric or out-of-range value
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


def questions_min_gap_seconds() -> float:
    return limits()["questions_min_gap_seconds"]


def questions_max_wait_seconds() -> float:
    return limits()["questions_max_wait_seconds"]


def reading_min_gap_seconds() -> float:
    return limits()["reading_min_gap_seconds"]


def reading_max_wait_seconds() -> float:
    return limits()["reading_max_wait_seconds"]


def questions_max_output_tokens() -> int:
    """The output cap sent to Groq when the question writer asks it for a question; 0 means no cap is sent."""
    return limits()["questions_max_output_tokens"]


def saved_results_days() -> int:
    return limits()["saved_results_days"]


def data_dir() -> Path:
    """Where the saved results live: SKILLVERIFY_DATA_DIR, else api/_data/ (never printed)."""
    load_dotenv()
    raw = (os.environ.get(DATA_DIR_VARIABLE) or "").strip()
    return Path(raw) if raw else Path(__file__).resolve().parent / DEFAULT_DATA_DIR
