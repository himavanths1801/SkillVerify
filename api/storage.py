"""Saved results on disk: one SQLite file (standard ``sqlite3``), opened lazily and used behind one lock.

What is stored (and nothing else): finished analyses (the ``result`` and ``meta`` the API returns, plus the small
public repo list the question writer needs), finished or partial question runs (what the API returns), and a cache of
GitHub file texts (already redacted), and the parsed answers of CV / job description reads (keyed by a hash
of the redacted text; the text itself is never kept). Keys, tokens and raw model answers are never stored.

If the file cannot be opened or is corrupt, one plain line is logged and the store switches itself off: every method
then does nothing and returns "nothing found", so a request never fails because of storage.
"""
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Union

from skillverify_questions import config

log = logging.getLogger("skillverify.api")

FILE_NAME = "skillverify.sqlite3"
MAX_CACHED_FILES = 2000
MAX_CACHED_READINGS = 500
SECONDS_PER_DAY = 24 * 60 * 60

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS analyses (id TEXT PRIMARY KEY, created REAL NOT NULL, result TEXT NOT NULL,"
    " meta TEXT NOT NULL, evidence TEXT)",
    "CREATE TABLE IF NOT EXISTS question_runs (analysis_id TEXT PRIMARY KEY, status TEXT NOT NULL, saved REAL NOT NULL,"
    " fingerprint TEXT, result TEXT NOT NULL, meta TEXT NOT NULL, error TEXT, replacements TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS question_runs_fingerprint ON question_runs (fingerprint)",
    "CREATE TABLE IF NOT EXISTS file_cache (key TEXT PRIMARY KEY, stored REAL NOT NULL, text TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS file_cache_stored ON file_cache (stored)",
    "CREATE TABLE IF NOT EXISTS reading_cache (key TEXT PRIMARY KEY, stored REAL NOT NULL, model TEXT NOT NULL,"
    " answer TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS reading_cache_stored ON reading_cache (stored)",
)


def question_evidence(github: Any) -> Optional[dict]:
    """The part of the GitHub evidence the question writer reads, cut down to public repo metadata (no profile, no
    commit statistics): repo names, languages, last commit dates, default branches and which repo shows which skill."""
    if not isinstance(github, dict) or not isinstance(github.get("username"), str):
        return None
    repos = [{key: record[key] for key in ("name", "counted", "is_fork", "languages", "last_commit_date") if key in record}
             | ({"error": True} if "error" in record else {})
             for record in github.get("repos", []) if isinstance(record, dict)]
    tech = github.get("tech_stack") if isinstance(github.get("tech_stack"), dict) else {}
    skills = [{"skill": item.get("skill"), "repos": item.get("repos")} for item in tech.get("skills", [])
              if isinstance(item, dict)]
    per_repo = [{"name": item.get("name"), "default_branch": item.get("default_branch")}
                for item in tech.get("per_repo", []) if isinstance(item, dict)]
    return {"username": github["username"], "repos": repos, "tech_stack": {"skills": skills, "per_repo": per_repo}}


class Store:
    """``directory`` is a path or a function that gives it (read the first time the file is needed)."""

    def __init__(self, directory: Union[Path, Callable[[], Path]], clock: Callable[[], float] = time.time) -> None:
        self._directory = directory
        self._clock = clock
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._failed = False

    # -- opening --------------------------------------------------------------------------------------------
    def _ready(self) -> bool:
        if self._conn is not None:
            return True
        if self._failed:
            return False
        try:
            folder = self._directory() if callable(self._directory) else self._directory
            Path(folder).mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(Path(folder) / FILE_NAME), check_same_thread=False, timeout=10)
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.commit()
            self._conn = conn
            return True
        except (sqlite3.Error, OSError) as exc:
            self._switch_off(exc)
            return False

    def _switch_off(self, exc: BaseException) -> None:
        if not self._failed:  # one plain line, only the error type
            log.warning("Saved results are off: the data file could not be used (%s). Everything stays in memory.",
                        type(exc).__name__)
        self._failed = True
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
        self._conn = None

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._ready()

    def _run(self, work: Callable[[sqlite3.Connection], Any], default: Any = None) -> Any:
        """Run ``work`` in one transaction. Any storage problem switches the store off and returns ``default``."""
        with self._lock:
            if not self._ready():
                return default
            assert self._conn is not None
            try:
                with self._conn:  # commit, or roll back on an error
                    return work(self._conn)
            except (sqlite3.Error, OSError, ValueError) as exc:
                self._switch_off(exc)
                return default

    # -- analyses -------------------------------------------------------------------------------------------
    def save_analysis(self, job_id: str, created: float, result: dict, meta: dict, evidence: Optional[dict]) -> bool:
        def work(conn: sqlite3.Connection) -> bool:
            conn.execute("INSERT OR REPLACE INTO analyses (id, created, result, meta, evidence) VALUES (?, ?, ?, ?, ?)",
                         (job_id, created, json.dumps(result), json.dumps(meta),
                          json.dumps(evidence) if evidence is not None else None))
            return True
        return bool(self._run(work, False))

    def load_analysis(self, job_id: str) -> Optional[dict]:
        def work(conn: sqlite3.Connection) -> Optional[dict]:
            row = conn.execute("SELECT created, result, meta, evidence FROM analyses WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            return {"id": job_id, "created": row[0], "result": json.loads(row[1]), "meta": json.loads(row[2]),
                    "evidence": json.loads(row[3]) if row[3] else None}
        return self._run(work)

    # -- question runs --------------------------------------------------------------------------------------
    def save_run(self, analysis_id: str, status: str, result: dict, meta: dict, fingerprint: Optional[str],
                 error: Optional[dict] = None, replacements: Optional[list] = None, keep_finished: bool = False) -> bool:
        """Save a run. ``status`` is the result status, or ``partial``. With ``keep_finished`` an already finished run
        of this analysis is not overwritten (a failed regeneration must not wipe a good saved run)."""
        def work(conn: sqlite3.Connection) -> bool:
            if keep_finished:
                row = conn.execute("SELECT status FROM question_runs WHERE analysis_id = ?", (analysis_id,)).fetchone()
                if row is not None and row[0] != "partial":
                    return False
            conn.execute(
                "INSERT OR REPLACE INTO question_runs (analysis_id, status, saved, fingerprint, result, meta, error,"
                " replacements) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (analysis_id, status, self._clock(), fingerprint, json.dumps(result), json.dumps(meta),
                 json.dumps(error) if error is not None else None, json.dumps(replacements or [])))
            return True
        return bool(self._run(work, False))

    @staticmethod
    def _run_row(row: tuple) -> dict:
        return {"analysis_id": row[0], "status": row[1], "saved": row[2], "fingerprint": row[3],
                "result": json.loads(row[4]), "meta": json.loads(row[5]),
                "error": json.loads(row[6]) if row[6] else None, "replacements": json.loads(row[7])}

    _RUN_COLUMNS = "analysis_id, status, saved, fingerprint, result, meta, error, replacements"

    def load_run(self, analysis_id: str) -> Optional[dict]:
        def work(conn: sqlite3.Connection) -> Optional[dict]:
            row = conn.execute(f"SELECT {self._RUN_COLUMNS} FROM question_runs WHERE analysis_id = ?",
                               (analysis_id,)).fetchone()
            return self._run_row(row) if row is not None else None
        return self._run(work)

    def find_cached_run(self, fingerprint: str, exclude: str) -> Optional[dict]:
        """The newest finished run (not partial) of ANOTHER analysis with the same fingerprint."""
        def work(conn: sqlite3.Connection) -> Optional[dict]:
            row = conn.execute(
                f"SELECT {self._RUN_COLUMNS} FROM question_runs WHERE fingerprint = ? AND status != 'partial'"
                " AND analysis_id != ? ORDER BY saved DESC, rowid DESC LIMIT 1", (fingerprint, exclude)).fetchone()
            return self._run_row(row) if row is not None else None
        return self._run(work)

    def add_replacement(self, analysis_id: str, entry: dict, meta: Optional[dict] = None) -> bool:
        """Remember a replacement question written after the run (the run's own questions stay as they were)."""
        def work(conn: sqlite3.Connection) -> bool:
            row = conn.execute("SELECT replacements, meta FROM question_runs WHERE analysis_id = ?",
                               (analysis_id,)).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE question_runs SET replacements = ?, meta = ?, saved = ? WHERE analysis_id = ?",
                         (json.dumps(json.loads(row[0]) + [entry]), json.dumps(meta) if meta is not None else row[1],
                          self._clock(), analysis_id))
            return True
        return bool(self._run(work, False))

    # -- GitHub file contents -------------------------------------------------------------------------------
    @staticmethod
    def _file_key(repo: str, path: str, stamp: Optional[str]) -> str:
        return f"{repo}\n{path}\n{stamp or ''}"

    def get_file(self, repo: str, path: str, stamp: Optional[str]) -> Optional[str]:
        """The cached text of a file, or None. A different ``stamp`` (the repo's last push date) is a miss."""
        def work(conn: sqlite3.Connection) -> Optional[str]:
            row = conn.execute("SELECT text FROM file_cache WHERE key = ?", (self._file_key(repo, path, stamp),)).fetchone()
            return row[0] if row is not None else None
        return self._run(work)

    def put_file(self, repo: str, path: str, stamp: Optional[str], text: str) -> bool:
        """Cache a file text. Files over the size limit are not kept; at most 2000 files, the oldest dropped first."""
        if len(text.encode("utf-8", "replace")) > config.MAX_FILE_BYTES:
            return False

        def work(conn: sqlite3.Connection) -> bool:
            conn.execute("INSERT OR REPLACE INTO file_cache (key, stored, text) VALUES (?, ?, ?)",
                         (self._file_key(repo, path, stamp), self._clock(), text))
            conn.execute("DELETE FROM file_cache WHERE key NOT IN (SELECT key FROM file_cache"
                         " ORDER BY stored DESC, rowid DESC LIMIT ?)", (MAX_CACHED_FILES,))
            return True
        return bool(self._run(work, False))

    def count_files(self) -> int:
        return int(self._run(lambda conn: conn.execute("SELECT COUNT(*) FROM file_cache").fetchone()[0], 0))

    # -- readings (the parsed answer of one CV / job description read) -----------------------------------------
    def get_reading(self, key: str) -> Optional[dict]:
        """``{"model", "answer"}`` saved under ``key`` (a SHA-256 of the exact redacted text sent), or None."""
        def work(conn: sqlite3.Connection) -> Optional[dict]:
            row = conn.execute("SELECT model, answer FROM reading_cache WHERE key = ?", (key,)).fetchone()
            return {"model": row[0], "answer": json.loads(row[1])} if row is not None else None
        return self._run(work)

    def put_reading(self, key: str, model: str, answer: dict) -> bool:
        """Save one parsed answer (never the text that was sent). At most 500 are kept, the oldest dropped first."""
        def work(conn: sqlite3.Connection) -> bool:
            conn.execute("INSERT OR REPLACE INTO reading_cache (key, stored, model, answer) VALUES (?, ?, ?, ?)",
                         (key, self._clock(), model, json.dumps(answer)))
            conn.execute("DELETE FROM reading_cache WHERE key NOT IN (SELECT key FROM reading_cache"
                         " ORDER BY stored DESC, rowid DESC LIMIT ?)", (MAX_CACHED_READINGS,))
            return True
        return bool(self._run(work, False))

    def count_readings(self) -> int:
        return int(self._run(lambda conn: conn.execute("SELECT COUNT(*) FROM reading_cache").fetchone()[0], 0))

    # -- retention ------------------------------------------------------------------------------------------
    def purge(self, days: int) -> tuple[int, int]:
        """Delete saved analyses and question runs older than ``days``. Returns (analyses, runs) deleted."""
        cutoff = self._clock() - days * SECONDS_PER_DAY

        def work(conn: sqlite3.Connection) -> tuple[int, int]:
            analyses = conn.execute("DELETE FROM analyses WHERE created < ?", (cutoff,)).rowcount
            runs = conn.execute("DELETE FROM question_runs WHERE saved < ?", (cutoff,)).rowcount
            conn.execute("DELETE FROM reading_cache WHERE stored < ?", (cutoff,))
            return analyses, runs
        analyses, runs = self._run(work, (0, 0))
        if analyses or runs:
            log.info("Saved results older than %d days removed: analyses=%d question_runs=%d", days, analyses, runs)
        return analyses, runs
