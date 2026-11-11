"""Strict SQL detection (SkillVerify extras), as an opt-in scanner.

The base detector (``tech_scanner.looks_like_sql``) can still call prose-like text a
SQL query, and a SQL match is rated strong. ``looks_like_sql_strict`` adds guards on
top of it, so it can only ever be MORE strict: it is ``looks_like_sql`` AND the guards.

``StrictSqlTechScanner`` is a ``TechScanner`` that uses the strict check. Everything
else about a scan (file signals, ``.sql`` files, dependencies, imports, caps,
``complete``, output shape) is identical to the base scanner.

This module may import ``skillverify.tech_scanner``; nothing old imports it.
"""
from __future__ import annotations

import re

from skillverify.tech_scanner import (
    MAX_STRING_CHARS,
    TechScanner,
    _cap_lines,
    extract_string_literals,
    find_js_imports,
    find_python_imports,
    looks_like_sql,
    parse_notebook,
    parse_pip_install_lines,
)

# One identifier part (plain, `quoted`, "quoted" or [bracketed]); names may be dotted.
_PART = r"""(?:[A-Za-z_][\w$]*|`[^`]+`|"[^"]+"|\[[^\]]+\])"""
_ID = _PART + r"(?:\." + _PART + r")*"

_ANCHOR = re.compile(r"(SELECT|INSERT\s+INTO|DELETE\s+FROM|UPDATE|CREATE\s+(TABLE|DATABASE|INDEX|VIEW)|WITH|EXPLAIN)\b",
                     re.IGNORECASE)
_SELECT = re.compile(
    r"SELECT\s+(?:(?:DISTINCT|ALL)\s+)?(?:TOP\s+\d+\s+)?(?P<list>.+?)\s+FROM\s+(?P<table>" + _ID + r")(?P<tail>.*)",
    re.IGNORECASE)
_SIMPLE_SELECT_LIST = re.compile(_ID + r"(?:\s+AS\s+" + _ID + r")?", re.IGNORECASE)
_SELECT_CLAUSE_WORDS = ("where join inner left right full cross natural outer group order limit "
                        "having union intersect except offset fetch on window for").split()
_SELECT_TAIL_START = re.compile(r"(?:;|\)|,|--|/\*)|(?:" + "|".join(_SELECT_CLAUSE_WORDS) + r")\b", re.IGNORECASE)
_ALIAS_THEN = re.compile(r"(?:AS\s+)?[A-Za-z_]\w*\s*(?P<rest>.*)", re.IGNORECASE)
_INSERT = re.compile(r"INSERT\s+INTO\s+" + _ID + r"\s*(?P<tail>.*)", re.IGNORECASE)
_INSERT_TAIL = re.compile(r"\(|(?:VALUES|SELECT|DEFAULT)\b", re.IGNORECASE)
_DELETE = re.compile(r"DELETE\s+FROM\s+" + _ID + r"\s*(?P<tail>.*)", re.IGNORECASE)
_DELETE_TAIL = re.compile(r"(?:;|\)|--)|(?:WHERE|USING|RETURNING)\b", re.IGNORECASE)
_UPDATE = re.compile(r"UPDATE\s+" + _ID + r"\s+SET\s+" + _ID + r"\s*=", re.IGNORECASE)
_CREATE_TABLE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" + _ID + r"\s*(?:\(|AS\b)", re.IGNORECASE)
_CREATE_INDEX = re.compile(r"CREATE\s+INDEX\s+" + _ID + r"\s+ON\s+" + _ID, re.IGNORECASE)
_CREATE_VIEW = re.compile(r"CREATE\s+VIEW\s+" + _ID + r"\s+AS\b", re.IGNORECASE)
_CREATE_DATABASE = re.compile(r"CREATE\s+DATABASE\s+" + _ID + r"\s*(?P<tail>.*)", re.IGNORECASE)
_DATABASE_TAIL = re.compile(r";|\)|(?:CHARACTER|COLLATE|WITH)\b", re.IGNORECASE)


def _select_tail_ok(tail: str) -> bool:
    tail = tail.strip()
    if not tail or _SELECT_TAIL_START.match(tail):
        return True
    alias = _ALIAS_THEN.fullmatch(tail)  # ONE alias word (optionally after AS), then a normal tail
    if alias is None:
        return False
    rest = alias.group("rest").strip()
    return not rest or bool(_SELECT_TAIL_START.match(rest))


def _select_ok(text: str) -> bool:
    match = _SELECT.fullmatch(text)
    if match is None:  # pragma: no cover - the base check already required "SELECT ... FROM <identifier>"
        return False
    select_list = match.group("list").strip()
    if not any(c in select_list for c in "*(,") and not _SIMPLE_SELECT_LIST.fullmatch(select_list):
        return False
    return _select_tail_ok(match.group("tail"))


def _tail_ok(tail: str, pattern: "re.Pattern[str]", allow_empty: bool) -> bool:
    tail = tail.strip()
    return (allow_empty and not tail) or bool(pattern.match(tail))


def looks_like_sql_strict(literal: str) -> bool:
    """True only if the base detector accepts the literal AND all of these guards hold.

    1. The statement STARTS the literal (SELECT, INSERT INTO, DELETE FROM, UPDATE,
       CREATE TABLE/DATABASE/INDEX/VIEW, WITH, EXPLAIN).
    2. After removing one trailing ';', the literal does not end with '.' or '!'.
    3. WITH / EXPLAIN need nothing more.
    4-8. Each statement kind must have its normal SQL shape (see docs/EXTENSIONS.md).
    """
    if not looks_like_sql(literal):
        return False
    text = re.sub(r"\s+", " ", literal[:MAX_STRING_CHARS]).strip().lstrip(" (")
    anchor = _ANCHOR.match(text)
    if anchor is None:
        return False
    body = text[:-1].rstrip() if text.endswith(";") else text
    if body.endswith((".", "!")):
        return False

    keyword = anchor.group(1).upper()
    if keyword in ("WITH", "EXPLAIN"):
        return True
    if keyword == "SELECT":
        return _select_ok(text)
    if keyword.startswith("INSERT"):
        match = _INSERT.fullmatch(text)
        return match is not None and bool(_INSERT_TAIL.match(match.group("tail")))
    if keyword.startswith("DELETE"):
        match = _DELETE.fullmatch(text)
        return match is not None and _tail_ok(match.group("tail"), _DELETE_TAIL, allow_empty=True)
    if keyword == "UPDATE":
        return _UPDATE.match(text) is not None
    kind = anchor.group(2).upper()  # CREATE TABLE / DATABASE / INDEX / VIEW
    if kind == "TABLE":
        return _CREATE_TABLE.match(text) is not None
    if kind == "INDEX":
        return _CREATE_INDEX.match(text) is not None
    if kind == "VIEW":
        return _CREATE_VIEW.match(text) is not None
    match = _CREATE_DATABASE.fullmatch(text)
    return match is not None and _tail_ok(match.group("tail"), _DATABASE_TAIL, allow_empty=True)


def find_embedded_sql_strict(code: str, language: str) -> bool:
    """Like ``tech_scanner.find_embedded_sql`` but with the strict check."""
    return any(looks_like_sql_strict(s) for s in extract_string_literals(_cap_lines(code), language))


class StrictSqlTechScanner(TechScanner):
    """A TechScanner whose embedded-SQL check is strict. Same constructor, same output shape."""

    # Copy of TechScanner._read_source with ONE change: find_embedded_sql_strict.
    def _read_source(self, state, base: str, branch: str, repo: str, path: str) -> None:
        text = self._fetch_text(state, base, path, branch)
        lower = path.lower()
        if lower.endswith(".ipynb"):
            code = parse_notebook(text)  # raises on a broken notebook: it is not counted
            self._count_python_file(state, code)
            self._record_packages(state, parse_pip_install_lines(code), "python", repo, path)
            self._python_evidence(state, code, repo, path)
        elif lower.endswith(".py"):
            self._count_python_file(state, text)
            self._python_evidence(state, text, repo, path)
        else:
            for package in sorted(find_js_imports(text)):
                entry = self.signals.js_packages.get(package)
                if entry is not None:
                    state.findings.add_signal(entry, repo, path, "imported_in_code")
            if find_embedded_sql_strict(text, "js"):
                state.findings.add_signal(self._sql_entry, repo, path, "embedded_query")

    # Copy of TechScanner._python_evidence with ONE change: find_embedded_sql_strict.
    def _python_evidence(self, state, code: str, repo: str, path: str) -> None:
        for module in sorted(find_python_imports(code)):
            entry = self.signals.python_imports.get(module)
            if entry is not None:
                state.findings.add_signal(entry, repo, path, "imported_in_code")
        if find_embedded_sql_strict(code, "py"):
            state.findings.add_signal(self._sql_entry, repo, path, "embedded_query")
