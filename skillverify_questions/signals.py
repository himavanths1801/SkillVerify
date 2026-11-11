"""Skill -> detection patterns, built from the skill taxonomy and the Step 1 scanner's own signal data.

A file "shows" a skill when this module finds at least one signal line for it. Plain matching only, no AI.
What is reused (imported, not copied): the taxonomy (canonical names, aliases, GitHub languages), the scanner's
signal map (``tech_signals.json``: package and import names) and its import finders. What this module adds:
  * the language rule (a file written in a skill's language shows that skill),
  * SQL keyword lines, ``git`` command lines, and word matches on the canonical name and longer aliases.
"""
import json
import re
from typing import Optional

from skillverify.taxonomy import Taxonomy
from skillverify.taxonomy_extra import load_extended_taxonomy
from skillverify.tech_scanner import (DEFAULT_SIGNALS_PATH, SignalMap, find_js_imports, find_python_imports,
                                      normalize_python_name)
from skillverify_questions import config, files

# GitHub language name (lower case) -> the language names used in config.EXTENSION_LANGUAGE.
GITHUB_LANGUAGE = {
    "python": "python", "jupyter notebook": "python", "javascript": "javascript", "typescript": "typescript",
    "java": "java", "kotlin": "kotlin", "c": "c", "c++": "cpp", "c#": "csharp", "go": "go", "rust": "rust",
    "ruby": "ruby", "php": "php", "r": "r", "shell": "shell", "scala": "scala",
    "sql": "sql", "tsql": "sql", "plsql": "sql", "plpgsql": "sql",
}
_SQL_KEYWORDS = re.compile(r"(?i)\b(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|CREATE\s+(?:TABLE|VIEW|INDEX)|"
                           r"JOIN|WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|UNION|WITH)\b")
_SQL_EMBEDDED = re.compile(r"\bSELECT\b.*\bFROM\b|\bINSERT\s+INTO\b|\bCREATE\s+TABLE\b|\bDELETE\s+FROM\b")
_GIT_COMMAND = re.compile(r"\bgit\s+(?:add|commit|push|pull|clone|checkout|switch|merge|rebase|branch|fetch|init|"
                          r"stash|tag|diff|log|reset|remote|status)\b")
_PY_ALIAS = re.compile(r"\bimport\s+([\w.]+)\s+as\s+(\w+)|\bfrom\s+[\w.]+\s+import\s+([\w, ]+)")


# String and template literals (backtick, triple quotes, single-line quotes), then real statements inside them.
_STRING_LITERAL = re.compile(r'`[^`]*`|"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'')
_REAL_STATEMENT = re.compile(
    r"(?is)\bselect\b[^;]*?\bfrom\b|\binsert\s+into\b|\bupdate\s+[\w.\"`\[\]]+\s+set\b|\bdelete\s+from\b"
    r"|\bcreate\s+(?:temp\w*\s+|unlogged\s+)?table\b|\bjoin\b[^;]*?\bon\b")


_SQL_SHAPE = re.compile(r"(?i)[*,.(=$?]|\b(?:where|values|group\s+by|order\s+by)\b")


def embedded_sql_statements(text: str, language: str) -> int:
    """How many real SQL statements sit inside string or template literals (comments are ignored).

    A statement is a verb plus its clause. ``SELECT 1``, a function called ``query(`` and a keyword in a comment
    do not count. Each ``;``-separated part of a literal counts at most once.
    """
    total = 0
    for literal in _STRING_LITERAL.finditer(strip(text, language)):
        body = literal.group(0)
        trusted = body[0] == "`" or body[:3] in ('"""', "'''") or "\n" in body  # multi-line text is code, not prose
        for part in body.split(";"):
            found = _REAL_STATEMENT.search(part)
            # A one-line quoted string in lower case needs a SQL-looking shape, or "update the set of" counts.
            if found and (trusted or found.group(0).split()[0].isupper() or _SQL_SHAPE.search(part)):
                total += 1
    return total


class Signals:
    """Counts the lines of a file that show a skill."""

    def __init__(self, taxonomy: Optional[Taxonomy] = None, signal_map: Optional[SignalMap] = None) -> None:
        self.taxonomy = taxonomy or load_extended_taxonomy()
        if signal_map is None:
            raw = json.loads(DEFAULT_SIGNALS_PATH.read_text(encoding="utf-8"))
            signal_map = SignalMap(raw, self.taxonomy)
        self._python_modules: dict[str, set[str]] = {}  # lower-case module -> skills
        self._js_packages: dict[str, set[str]] = {}
        for index, target, norm in ((signal_map.python_imports, self._python_modules, str.lower),
                                    (signal_map.python_packages, self._python_modules, normalize_python_name),
                                    (signal_map.js_packages, self._js_packages, str.lower)):
            for name, entry in index.items():
                target.setdefault(norm(name), set()).update(entry["skills"])
        self._name_cache: dict[str, Optional[re.Pattern[str]]] = {}

    # -- what a skill is written in -------------------------------------------------------------
    def languages_of(self, skill: str) -> set[str]:
        record = self.taxonomy.get(skill) or {}
        return {GITHUB_LANGUAGE[name.lower()] for name in record.get("github_languages", [])
                if name.lower() in GITHUB_LANGUAGE}

    def _name_pattern(self, skill: str) -> Optional[re.Pattern[str]]:
        """Word match on the canonical name (case-sensitive) and on aliases of at least MIN_ALIAS_CHARS letters."""
        if skill not in self._name_cache:
            record = self.taxonomy.get(skill) or {}
            names = [skill] + [a for a in record.get("aliases", []) if len(a) >= config.MIN_ALIAS_CHARS]
            useful = sorted({n for n in names if len(n) >= 3 and re.fullmatch(r"[A-Za-z0-9 +#._-]+", n)},
                            key=lambda n: (-len(n), n))
            self._name_cache[skill] = (re.compile(r"(?<![\w.])(?:" + "|".join(re.escape(n) for n in useful)
                                                  + r")(?![\w])") if useful else None)
        return self._name_cache[skill]

    # -- the count --------------------------------------------------------------------------------
    def count(self, skill: str, language: str, text: str, gated: bool = True) -> int:
        """How many lines of this file show the skill (0 means the file does not show it).

        SQL has one extra rule: a file that is not a .sql file shows it only with enough real embedded statements
        (``gated=False`` skips that rule, to find the files it excludes).
        """
        if (gated and skill == "SQL" and language != "sql"
                and embedded_sql_statements(text, language) < config.MIN_EMBEDDED_SQL_STATEMENTS):
            return 0
        lines = strip(text, language).splitlines()
        shown: set[int] = set()
        if language in self.languages_of(skill):  # written in the skill's language: it shows the skill
            return max(1, files.logic_counts(text, language)[1]) if any(line.strip() for line in lines) else 0
        if skill == "SQL":
            pattern = _SQL_KEYWORDS if language == "sql" else _SQL_EMBEDDED
            shown.update(i for i, line in enumerate(lines) if pattern.search(line))
        elif skill == "Git":
            shown.update(i for i, line in enumerate(lines) if _GIT_COMMAND.search(line))
        shown.update(self._import_lines(skill, language, lines))
        name = self._name_pattern(skill)
        if name is not None and skill not in ("SQL", "Git"):
            shown.update(i for i, line in enumerate(lines) if name.search(line))
        return len(shown)

    def _import_lines(self, skill: str, language: str, lines: list[str]) -> set[int]:
        shown: set[int] = set()
        if language == "python":
            modules = {m for m, skills in self._python_modules.items() if skill in skills}
            finder, words = find_python_imports, set(modules)
            for i, line in enumerate(lines):
                if modules & finder(line):
                    shown.add(i)
                    words.update(self._aliases(line))
            if shown:  # usage lines: the module name or its alias used as a word
                usage = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in sorted(words)) + r")\b")
                shown.update(i for i, line in enumerate(lines) if usage.search(line))
        elif language in ("javascript", "typescript"):
            packages = {p for p, skills in self._js_packages.items() if skill in skills}
            for i, line in enumerate(lines):
                if packages & find_js_imports(line):
                    shown.add(i)
            if shown:
                usage = re.compile(r"\b(?:" + "|".join(re.escape(p.split("/")[-1]) for p in sorted(packages)) + r")\b")
                shown.update(i for i, line in enumerate(lines) if usage.search(line))
        return shown

    @staticmethod
    def _aliases(line: str) -> set[str]:
        found: set[str] = set()
        for match in _PY_ALIAS.finditer(line):
            if match.group(2):
                found.add(match.group(2))
            elif match.group(3):
                found.update(part.split(" as ")[-1].strip() for part in match.group(3).split(",") if part.strip())
        return {a for a in found if re.fullmatch(r"\w+", a)}


def strip(text: str, language: str) -> str:
    """The text without comments, so a mention in a comment never counts as a signal."""
    return files.strip_comments(text, language)

