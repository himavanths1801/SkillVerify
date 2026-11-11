"""Tech stack scanner (SkillVerify, Step 1 extension).

Step 1 reports GitHub's language percentages. A CV also claims libraries and
tools (pandas, Flask, MongoDB, SQL ...) that never show up as "languages".
This module looks for evidence of them inside the candidate's repos:

* ``imported_in_code``    strong   a mapped package is imported in a source file
* ``embedded_query``      strong   a real SQL statement inside a string literal
* ``declared_dependency`` medium   listed in a dependency file / ``pip install`` line
* ``sql_file``            medium   the repo contains ``.sql`` files
* ``file_signal``         medium   a tool's config file exists (CI, Maven ...)
* ``ide_folder``          weak     ``.idea/`` or ``.vscode/`` is committed

It does NO scoring and makes NO hiring judgement. Accuracy beats coverage:
reporting nothing is better than reporting something wrong.

Everything fetched from GitHub is untrusted text: it is only ever parsed with
simple regexes and the standard JSON/TOML readers, never executed or written
to disk.

Dependency direction: this module may import Steps 1 and 2; they know nothing
about it.
"""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

try:  # tomllib is in the standard library from Python 3.11
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - only on Python 3.10 (tested via monkeypatch)
    tomllib = None

from skillverify.github_validator import (
    _ApiError,
    _FatalApiError,
    _NotFoundError,
    _RateLimitError,
    is_valid_username,
    parse_github_date,
)
from skillverify.taxonomy import Taxonomy, make_key

DEFAULT_SIGNALS_PATH = Path(__file__).resolve().parent / "data" / "tech_signals.json"
DEFAULT_IMPLICATIONS_PATH = Path(__file__).resolve().parent / "data" / "tech_implications.json"

MAX_FILE_BYTES = 400_000
MAX_LINE_CHARS = 2_000
MAX_STRING_CHARS = 20_000
MAX_DEPENDENCY_FILES = 10
MAX_EXAMPLES_OUTPUT = 3
MAX_EXAMPLES_COLLECTED = 30
MAX_UNMAPPED = 50
# A language share below this is a "trace": real, but GitHub also misclassifies small files.
MIN_LANGUAGE_SHARE_PERCENT = 1.0
# Skills the scan can add evidence for even though they are not in the signal map:
# SQL (embedded queries) and Python (source files read; GitHub labels notebooks
# "Jupyter Notebook", never "Python").
SCAN_ALWAYS_SKILLS = ("SQL", "Python")

SOURCE_EXTENSIONS = (".py", ".ipynb", ".js", ".jsx", ".ts", ".tsx", ".mjs")
SKIP_DIR_NAMES = ("node_modules", "venv", ".venv", "env", "site-packages", "dist", "build",
                  "vendor", "__pycache__", ".git")
REPO_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")

EVIDENCE_ORDER = ("imported_in_code", "embedded_query", "declared_dependency",
                  "sql_file", "file_signal", "ide_folder")
CONFIDENCE_OF = {
    "imported_in_code": "strong", "embedded_query": "strong",
    "declared_dependency": "medium", "sql_file": "medium", "file_signal": "medium",
    "ide_folder": "weak",
}
CONFIDENCE_RANK = {"strong": 0, "medium": 1, "weak": 2}
FILE_SIGNAL_EVIDENCE = ("sql_file", "file_signal", "ide_folder")

_DEPENDENCY_FILES = (
    (re.compile(r"(?i)(^|/)requirements[^/]*\.(txt|in)$"), "requirements"),
    (re.compile(r"(?i)(^|/)requirements/[^/]+\.(txt|in)$"), "requirements"),
    (re.compile(r"(^|/)Pipfile$"), "pipfile"),
    (re.compile(r"(^|/)pyproject\.toml$"), "pyproject"),
    (re.compile(r"(?i)(^|/)environment\.ya?ml$"), "conda"),
    (re.compile(r"(^|/)setup\.py$"), "setup_py"),
    (re.compile(r"(^|/)package\.json$"), "package_json"),
)


class TechScannerError(Exception):
    """Raised when the signal file is missing, invalid, or names an unknown skill."""


# --------------------------------------------------------------------------
# Small text helpers
# --------------------------------------------------------------------------
def _cap_lines(text: str) -> str:
    """Truncate every line to MAX_LINE_CHARS so regexes can never run away."""
    return "\n".join(line[:MAX_LINE_CHARS] for line in text.split("\n"))


def normalize_python_name(name: str) -> str:
    """PEP 503: lowercase, and runs of '-', '_' and '.' become one '-'."""
    return re.sub(r"[-_.]+", "-", name).lower()


_REQ_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _requirement_name(text: str) -> Optional[str]:
    """Package name at the start of a requirement string like 'pkg[extra]>=1; marker'."""
    match = _REQ_NAME.match(text.strip())
    return normalize_python_name(match.group(0)) if match else None


# --------------------------------------------------------------------------
# Dependency-file parsers (pure functions; raise ValueError on a broken file)
# --------------------------------------------------------------------------
def parse_requirements_txt(text: str) -> list[str]:
    names: list[str] = []
    for raw in _cap_lines(text).split("\n"):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-") or "://" in line:
            continue
        line = line.split(" #")[0]  # inline comment
        name = _requirement_name(line)
        if name:
            names.append(name)
    return names


def _require_toml(text: str) -> dict[str, Any]:
    if tomllib is None:
        raise ValueError("TOML parsing needs Python 3.11 or newer")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML: {exc}") from None
    return data


def parse_pyproject(text: str) -> list[str]:
    data = _require_toml(text)
    names: list[str] = []
    project = data.get("project", {})
    for requirement in project.get("dependencies", []):
        names.append(_requirement_name(str(requirement)))
    for group in project.get("optional-dependencies", {}).values():
        names.extend(_requirement_name(str(r)) for r in group)
    poetry = data.get("tool", {}).get("poetry", {})
    tables = [poetry.get("dependencies", {}), poetry.get("dev-dependencies", {})]
    tables.extend(g.get("dependencies", {}) for g in poetry.get("group", {}).values())
    for table in tables:
        names.extend(normalize_python_name(k) for k in table if k.lower() != "python")
    return [n for n in names if n]


def parse_pipfile(text: str) -> list[str]:
    data = _require_toml(text)
    names: list[str] = []
    for section in ("packages", "dev-packages"):
        names.extend(normalize_python_name(k) for k in data.get(section, {}))
    return names


def parse_environment_yml(text: str) -> list[str]:
    """Tiny line reader for conda files (no YAML library): list items under
    ``dependencies:`` and under a nested ``pip:`` list."""
    names: list[str] = []
    in_deps = False
    pip_indent: Optional[int] = None
    for raw in _cap_lines(text).split("\n"):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw[0].isspace() and not raw.startswith("-"):
            in_deps = re.match(r"dependencies\s*:", raw) is not None
            pip_indent = None
            continue
        if not in_deps:
            continue
        item = re.match(r"^(\s*)-\s*(.+?)\s*$", raw)
        if not item:
            continue
        indent, value = len(item.group(1)), item.group(2).split(" #")[0].strip()
        if pip_indent is not None and indent <= pip_indent:
            pip_indent = None
        if re.match(r"pip\s*:?$", value):
            pip_indent = indent
            continue
        if pip_indent is not None:
            name = None if value.startswith("-") or "://" in value else _requirement_name(value)
        else:
            value = value.split("::")[-1]  # drop a channel prefix like conda-forge::
            name = _requirement_name(re.split(r"[=<>! ]", value)[0])
        if name:
            names.append(name)
    return names


def parse_setup_py(text: str) -> list[str]:
    """Conservative: only the quoted strings inside ``install_requires = [...]``.
    The file is never executed."""
    block = re.search(r"install_requires\s*=\s*\[(.*?)\]", _cap_lines(text), re.DOTALL)
    if not block:
        return []
    names = (_requirement_name(s) for s in re.findall(r"[\"']([^\"'\n]+)[\"']", block.group(1)))
    return [n for n in names if n]


def parse_package_json(text: str) -> list[str]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"invalid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError("package.json is not an object")
    names: list[str] = []
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        table = data.get(section)
        if isinstance(table, dict):
            names.extend(str(k).lower() for k in table)
    return names


_PIP_OPTIONS_WITH_VALUE = {"-r", "--requirement", "-c", "--constraint", "-i", "--index-url",
                           "--extra-index-url", "-f", "--find-links", "-e", "--editable",
                           "-t", "--target"}
_PIP_LINE = re.compile(r"^\s*[!%]\s*(?:python\d?\s+-m\s+)?pip\d?\s+install\s+(.*)$")


def parse_pip_install_lines(code: str) -> list[str]:
    """Package names from ``!pip install a b`` / ``%pip install ...`` lines."""
    names: list[str] = []
    for line in _cap_lines(code).split("\n"):
        match = _PIP_LINE.match(line)
        if not match:
            continue
        command = re.split(r"&&|;|#", match.group(1))[0]
        skip_next = False
        for token in command.split():
            token = token.strip("\"'")
            if skip_next:
                skip_next = False
            elif token in _PIP_OPTIONS_WITH_VALUE:
                skip_next = True
            elif token.startswith("-") or "/" in token or "://" in token:
                continue
            else:
                name = _requirement_name(token)
                if name:
                    names.append(name)
    return names


def parse_notebook(text: str) -> str:
    """Code from the code cells of a notebook (outputs and markdown are ignored)."""
    try:
        data = json.loads(text)
        cells = data["cells"]
    except (ValueError, KeyError, TypeError):
        raise ValueError("not a valid notebook") from None
    chunks: list[str] = []
    for cell in cells if isinstance(cells, list) else []:
        if not isinstance(cell, dict) or cell.get("cell_type") != "code":
            continue
        source = cell.get("source", "")
        if isinstance(source, list):
            source = "".join(s for s in source if isinstance(s, str))
        if isinstance(source, str):
            chunks.append(source)
    return "\n".join(chunks)


_DEPENDENCY_PARSERS = {
    "requirements": (parse_requirements_txt, "python"),
    "pipfile": (parse_pipfile, "python"),
    "pyproject": (parse_pyproject, "python"),
    "conda": (parse_environment_yml, "python"),
    "setup_py": (parse_setup_py, "python"),
    "package_json": (parse_package_json, "js"),
}


# --------------------------------------------------------------------------
# Import detection
# --------------------------------------------------------------------------
_IDENT = re.compile(r"[A-Za-z_]\w*")
_PY_IMPORT = re.compile(r"^\s*import\s+([^\n#;]+)")
_PY_FROM = re.compile(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\b")
_JS_FROM = re.compile(r"""\bfrom\s*(['"])([^'"\n]+)\1""")
_JS_BARE = re.compile(r"""(?:^|[;\s{}])import\s*(['"])([^'"\n]+)\1""")
_JS_CALL = re.compile(r"""\b(?:require|import)\s*\(\s*(['"])([^'"\n]+)\1\s*\)""")


def _strip_between(text: str, opener: str, closer: str) -> str:
    """Remove ``opener ... closer`` blocks in linear time (an unterminated block
    removes the rest of the text)."""
    parts: list[str] = []
    pos = 0
    while True:
        start = text.find(opener, pos)
        if start == -1:
            parts.append(text[pos:])
            return "".join(parts)
        parts.append(text[pos:start])
        end = text.find(closer, start + len(opener))
        if end == -1:
            return "".join(parts)
        pos = end + len(closer)


def find_python_imports(code: str) -> set[str]:
    """Lowercased top-level module names imported by Python code."""
    code = _cap_lines(code)
    code = _strip_between(_strip_between(code, '"""', '"""'), "'''", "'''")  # docstrings
    modules: set[str] = set()
    for line in code.split("\n"):
        match = _PY_IMPORT.match(line)
        if match:
            for part in match.group(1).split(","):
                words = part.split()
                top = words[0].split(".")[0] if words else ""
                if _IDENT.fullmatch(top):
                    modules.add(top.lower())
            continue
        match = _PY_FROM.match(line)
        if match:  # relative imports ("from . import x") never match: they start with "."
            modules.add(match.group(1).split(".")[0].lower())
    return modules


def _js_package_name(specifier: str) -> Optional[str]:
    if not specifier or specifier[0] in "./" or ":" in specifier:  # relative, absolute, node:fs
        return None
    pieces = specifier.split("/")
    if specifier.startswith("@"):
        return "/".join(pieces[:2]).lower() if len(pieces) >= 2 else None
    return pieces[0].lower()


def find_js_imports(code: str) -> set[str]:
    """Lowercased package names imported or required by JavaScript/TypeScript."""
    code = _strip_between(_cap_lines(code), "/*", "*/")
    code = re.sub(r"(?m)^\s*//.*$", "", code)
    packages: set[str] = set()
    for pattern in (_JS_FROM, _JS_BARE, _JS_CALL):
        for match in pattern.finditer(code):
            name = _js_package_name(match.group(2))
            if name:
                packages.add(name)
    return packages


# --------------------------------------------------------------------------
# Embedded SQL detection
# --------------------------------------------------------------------------
def extract_string_literals(code: str, language: str) -> list[str]:
    """String literals of ``code`` (language "py" or "js"), skipping comments.

    Only literals are ever tested for SQL, never raw code or comments.
    """
    literals: list[str] = []
    i, n = 0, len(code)
    while i < n:
        c = code[i]
        if language == "py" and c == "#":
            i = code.find("\n", i)
            i = n if i == -1 else i
        elif language == "js" and code.startswith("//", i):
            i = code.find("\n", i)
            i = n if i == -1 else i
        elif language == "js" and code.startswith("/*", i):
            end = code.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif language == "py" and c in "\"'" and code.startswith(c * 3, i):
            end = code.find(c * 3, i + 3)
            end = n if end == -1 else end
            literals.append(code[i + 3:end])
            i = end + 3
        elif c in "\"'" or (c == "`" and language == "js"):
            j = i + 1
            while j < n and code[j] != c:
                if code[j] == "\\":
                    j += 1
                elif code[j] == "\n" and c != "`":
                    break
                j += 1
            literals.append(code[i + 1:j])
            i = j + 1
        else:
            i += 1
    return literals


_ID = r"""(?:[A-Za-z_][\w.$]*|`[^`]+`|"[^"]+"|\[[^\]]+\])"""
_PLACEHOLDER = re.compile(r"%s|\?|:[A-Za-z_]\w*|\$\d|\{[^{}]*\}|@\w+")
_SELECT_UPPER = re.compile(r"\bSELECT\s.{1,200}?\sFROM\s+" + _ID)
_SELECT_ANY = re.compile(r"\bselect\s(.{1,200}?)\sfrom\s+" + _ID + r"(.*)", re.IGNORECASE)
_SELECT_CLAUSE = re.compile(r"\b(?:where|join|group by|order by|limit|having)\b", re.IGNORECASE)
_INSERT_UPPER = re.compile(r"\bINSERT\s+INTO\s+" + _ID)
_INSERT_ANY = re.compile(r"\binsert\s+into\s+" + _ID + r"\s*(?:\(|values\b|select\b)", re.IGNORECASE)
_DELETE_UPPER = re.compile(r"\bDELETE\s+FROM\s+" + _ID)
_DELETE_ANY = re.compile(r"\bdelete\s+from\s+" + _ID + r"\s*(?:where\b|;)", re.IGNORECASE)
_UPDATE_UPPER = re.compile(r"\bUPDATE\s+" + _ID + r"\s+SET\b")
_UPDATE_ANY = re.compile(r"\bupdate\s+" + _ID + r"\s+set\s+\w+\s*=", re.IGNORECASE)
_CREATE_UPPER = re.compile(r"\bCREATE\s+(?:TABLE|DATABASE|INDEX|VIEW)\b")
_CREATE_ANY = re.compile(r"\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?" + _ID + r"\s*\(", re.IGNORECASE)


def looks_like_sql(literal: str) -> bool:
    """Conservative test: is this string literal a real SQL statement?

    UPPERCASE keywords are accepted on their own. Lowercase or mixed case needs
    extra proof (a clause word, ``*``, a placeholder ...) so ordinary prose such
    as "Please select an option from the list" is never matched.
    """
    text = re.sub(r"\s+", " ", literal[:MAX_STRING_CHARS]).strip()
    if not text:
        return False
    if (_SELECT_UPPER.search(text) or _INSERT_UPPER.search(text) or _DELETE_UPPER.search(text)
            or _UPDATE_UPPER.search(text) or _CREATE_UPPER.search(text)):
        return True
    if (_INSERT_ANY.search(text) or _DELETE_ANY.search(text) or _UPDATE_ANY.search(text)
            or _CREATE_ANY.search(text)):
        return True
    select = _SELECT_ANY.search(text)
    if select:
        select_list, after_from = select.group(1), select.group(2)
        # Clause words are only looked for AFTER the table name, so a prose
        # phrase like "select a column set from ..." cannot qualify.
        return bool("*" in select_list or _SELECT_CLAUSE.search(after_from)
                    or _PLACEHOLDER.search(text))
    return False


def find_embedded_sql(code: str, language: str) -> bool:
    return any(looks_like_sql(s) for s in extract_string_literals(_cap_lines(code), language))


# --------------------------------------------------------------------------
# Signal map
# --------------------------------------------------------------------------
class SignalMap:
    """The validated contents of ``tech_signals.json``."""

    def __init__(self, raw: Any, taxonomy: Taxonomy) -> None:
        if not isinstance(raw, dict) or not isinstance(raw.get("signals"), list) or not raw["signals"]:
            raise TechScannerError("Signal file needs a non-empty 'signals' list.")
        self.python_packages: dict[str, dict[str, Any]] = {}
        self.python_imports: dict[str, dict[str, Any]] = {}
        self.js_packages: dict[str, dict[str, Any]] = {}
        self.file_signals: list[dict[str, Any]] = []
        self.skill_ids: set[str] = set()

        for position, entry in enumerate(raw["signals"], start=1):
            parsed = self._parse_entry(entry, position, taxonomy)
            for field, index, norm in (
                ("python_packages", self.python_packages, normalize_python_name),
                ("python_imports", self.python_imports, str.lower),
                ("js_packages", self.js_packages, str.lower),
            ):
                for name in self._string_list(entry, field, parsed["label"]):
                    key = norm(name)
                    owner = index.get(key)
                    if owner is not None and owner is not parsed:
                        raise TechScannerError(
                            f"{field} name '{name}' appears in both '{owner['label']}' and '{parsed['label']}'.")
                    index[key] = parsed

        for position, entry in enumerate(raw.get("file_signals", []), start=1):
            parsed = self._parse_entry(entry, position, taxonomy, kind="file signal")
            try:
                parsed["regex"] = re.compile(entry.get("path_regex", ""))
            except (re.error, TypeError) as exc:
                raise TechScannerError(f"File signal '{parsed['label']}': bad path_regex ({exc}).") from None
            if not entry.get("path_regex"):
                raise TechScannerError(f"File signal '{parsed['label']}': missing path_regex.")
            if entry.get("evidence") not in FILE_SIGNAL_EVIDENCE:
                raise TechScannerError(
                    f"File signal '{parsed['label']}': evidence must be one of {', '.join(FILE_SIGNAL_EVIDENCE)}.")
            parsed["evidence"] = entry["evidence"]
            parsed["max_examples"] = int(entry.get("max_examples", MAX_EXAMPLES_OUTPUT))
            self.file_signals.append(parsed)

    @staticmethod
    def _string_list(entry: dict[str, Any], field: str, label: str) -> list[str]:
        value = entry.get(field, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise TechScannerError(f"Signal '{label}': '{field}' must be a list of strings.")
        return value

    def _parse_entry(self, entry: Any, position: int, taxonomy: Taxonomy, kind: str = "signal") -> dict[str, Any]:
        if not isinstance(entry, dict) or not isinstance(entry.get("label"), str) or not entry["label"].strip():
            raise TechScannerError(f"Entry #{position} ({kind}) has no 'label'.")
        label = entry["label"]
        skills = self._string_list(entry, "skills", label)
        for skill_id in skills:
            if taxonomy.get(skill_id) is None:
                raise TechScannerError(f"Signal '{label}': skill id '{skill_id}' is not in the taxonomy.")
        also = self._string_list(entry, "also_report_as", label)
        self.skill_ids.update(skills)
        # Tools without a taxonomy skill (YOLO, PyCharm, VS Code) are reported by name only.
        label_only = list(also) if also else ([] if skills else [label])
        return {"label": label, "skills": skills, "label_only": label_only}


def _load_signal_map(path: Optional[str | Path], taxonomy: Taxonomy) -> SignalMap:
    file_path = Path(path) if path is not None else DEFAULT_SIGNALS_PATH
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TechScannerError(f"Signal file not found: {file_path}") from None
    except (ValueError, OSError) as exc:  # ValueError covers bad JSON and bad encoding
        raise TechScannerError(f"Could not read signal file {file_path}: {exc}") from None
    return SignalMap(raw, taxonomy)


def _load_implications(path: Optional[str | Path], taxonomy: Taxonomy) -> list[dict[str, Any]]:
    """Load and validate ``tech_implications.json``: concepts that real evidence implies
    (Express.js implies Node.js). Used only by compare_claimed_skills, never by scan()."""
    file_path = Path(path) if path is not None else DEFAULT_IMPLICATIONS_PATH
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TechScannerError(f"Implications file not found: {file_path}") from None
    except (ValueError, OSError) as exc:
        raise TechScannerError(f"Could not read implications file {file_path}: {exc}") from None
    entries = raw.get("implications") if isinstance(raw, dict) else None
    if not isinstance(entries, list) or not entries:
        raise TechScannerError("Implications file needs a non-empty 'implications' list.")

    loaded: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, entry in enumerate(entries, start=1):
        skill = entry.get("skill") if isinstance(entry, dict) else None
        if not isinstance(skill, str) or not skill.strip():
            raise TechScannerError(f"Implication #{position} has no 'skill'.")
        implied_by = entry.get("implied_by")
        if (not isinstance(implied_by, list) or not implied_by
                or not all(isinstance(v, str) for v in implied_by)):
            raise TechScannerError(f"Implication '{skill}': 'implied_by' must be a non-empty list of strings.")
        for skill_id in [skill, *implied_by]:
            if taxonomy.get(skill_id) is None:
                raise TechScannerError(f"Implication '{skill}': skill id '{skill_id}' is not in the taxonomy.")
        if skill in seen:
            raise TechScannerError(f"Implication '{skill}' appears in two entries.")
        if skill in implied_by:
            raise TechScannerError(f"Implication '{skill}' cannot be implied by itself.")
        seen.add(skill)
        loaded.append({"skill": skill, "implied_by": list(implied_by)})
    return loaded


# --------------------------------------------------------------------------
# Findings collected during one scan
# --------------------------------------------------------------------------
class _Findings:
    def __init__(self, taxonomy: Taxonomy) -> None:
        self.taxonomy = taxonomy
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.unmapped: dict[tuple[str, str], set[str]] = {}
        self.repo_labels: set[str] = set()

    def add(self, kind: str, name: str, repo: str, path: str, evidence: str) -> None:
        item = self.items.get((kind, name))
        if item is None:
            record = self.taxonomy.get(name) if kind == "skill" else None
            item = {"skill": name if kind == "skill" else None, "label": name,
                    "in_taxonomy": kind == "skill", "category": record["category"] if record else None,
                    "evidence_types": set(), "repos": [], "examples": []}
            self.items[(kind, name)] = item
        item["evidence_types"].add(evidence)
        if repo not in item["repos"]:
            item["repos"].append(repo)
        example = {"repo": repo, "path": path, "type": evidence}
        if example not in item["examples"] and len(item["examples"]) < MAX_EXAMPLES_COLLECTED:
            item["examples"].append(example)
        self.repo_labels.add(name)

    def add_signal(self, entry: dict[str, Any], repo: str, path: str, evidence: str) -> None:
        for skill_id in entry["skills"]:
            self.add("skill", skill_id, repo, path, evidence)
        for label in entry["label_only"]:
            self.add("label", label, repo, path, evidence)

    def add_unmapped(self, name: str, ecosystem: str, repo: str) -> None:
        self.unmapped.setdefault((name, ecosystem), set()).add(repo)

    def skills_output(self) -> list[dict[str, Any]]:
        out = []
        for item in self.items.values():
            types = [t for t in EVIDENCE_ORDER if t in item["evidence_types"]]
            strongest = min((CONFIDENCE_OF[t] for t in types), key=CONFIDENCE_RANK.get)
            examples = sorted(item["examples"], key=lambda e: EVIDENCE_ORDER.index(e["type"]))
            out.append({**item, "confidence": strongest, "evidence_types": types,
                        "examples": examples[:MAX_EXAMPLES_OUTPUT]})
        out.sort(key=lambda s: (CONFIDENCE_RANK[s["confidence"]], -len(s["repos"]), s["label"].lower()))
        return out

    def unmapped_output(self) -> list[dict[str, Any]]:
        rows = [{"name": n, "ecosystem": e, "repos": len(r)} for (n, e), r in self.unmapped.items()]
        rows.sort(key=lambda r: (-r["repos"], r["name"]))
        return rows[:MAX_UNMAPPED]


# --------------------------------------------------------------------------
# The scanner
# --------------------------------------------------------------------------
class TechScanner:
    """Reads extra evidence (libraries, SQL, tools) from a candidate's repos.

    Uses the validator's authenticated session and its retry / rate-limit
    handling (``validator._get``), so no second token or HTTP stack is needed.
    """

    def __init__(
        self,
        validator: Any,
        taxonomy: Optional[Taxonomy] = None,
        signals_path: Optional[str | Path] = None,
        max_repos: int = 40,
        max_files_per_repo: int = 20,
        max_total_file_fetches: int = 400,
    ) -> None:
        self.validator = validator
        self.taxonomy = taxonomy or Taxonomy.load()
        self.signals = _load_signal_map(signals_path, self.taxonomy)
        if self.taxonomy.get("SQL") is None:
            raise TechScannerError("The taxonomy has no 'SQL' skill (needed for embedded queries).")
        self.max_repos = max_repos
        self.max_files_per_repo = max_files_per_repo
        self.max_total_file_fetches = max_total_file_fetches
        self._sql_entry = {"label": "SQL", "skills": ["SQL"], "label_only": []}

    def scannable_skills(self) -> set[str]:
        """Skill ids the scan itself can add evidence for (signal map, SQL, Python)."""
        return set(self.signals.skill_ids) | set(SCAN_ALWAYS_SKILLS)

    def detectable_skills(self) -> set[str]:
        """Skill ids this scanner could ever find (signal map + language evidence)."""
        ids = set(self.signals.skill_ids) | {"SQL"}
        ids.update(s["id"] for s in self.taxonomy.skills
                   if self.taxonomy.confirmable_via_languages(s["id"]))
        return ids

    # -- public entry point --------------------------------------------------
    def scan(self, validation_result: Any) -> dict[str, Any]:
        """Scan the candidate's counted repos. Never raises."""
        result = self._blank_result()
        try:
            self._scan(validation_result, result)
        except Exception as exc:  # noqa: BLE001 - contract: scan() never raises
            result.update(status="error", complete=False,
                          message=f"Unexpected error during tech scan ({type(exc).__name__}).")
            result["incomplete_reasons"] = ["unexpected error"]
        return result

    def _blank_result(self) -> dict[str, Any]:
        return {
            "status": "ok", "message": "", "complete": False, "incomplete_reasons": [],
            "scan_settings": {"max_repos": self.max_repos, "max_files_per_repo": self.max_files_per_repo,
                              "max_total_file_fetches": self.max_total_file_fetches,
                              "max_file_bytes": MAX_FILE_BYTES},
            "repos_scanned": 0, "repos_not_scanned": [], "repos_failed": 0, "api_calls_used": 0,
            "skills": [], "unmapped_packages": [], "per_repo": [],
        }

    def _skip(self, result: dict[str, Any], message: str, status: str = "skipped") -> None:
        result.update(status=status, message=message, complete=False)

    def _scan(self, validation: Any, result: dict[str, Any]) -> None:
        if not isinstance(validation, dict) or not isinstance(validation.get("repos", []), list):
            return self._skip(result, "validation_result is not a validator result.", status="error")
        if validation.get("status") != "ok":
            return self._skip(result, f"Validator status is '{validation.get('status')}'; nothing to scan.")
        username = validation.get("username")
        if not is_valid_username(username):
            return self._skip(result, "Validator result has no valid GitHub username.")

        eligible = [r for r in validation.get("repos", [])
                    if isinstance(r, dict) and r.get("counted") and "error" not in r
                    and isinstance(r.get("name"), str)]
        if not eligible:
            return self._skip(result, "No eligible repositories (forks and failed repos are not scanned).")
        eligible.sort(key=_recency_key, reverse=True)
        to_scan = eligible[:self.max_repos]
        result["repos_not_scanned"] = [r["name"] for r in eligible[self.max_repos:]]
        reasons: list[str] = ["repo cap reached"] if result["repos_not_scanned"] else []

        state = _ScanState(_Findings(self.taxonomy))
        for index, record in enumerate(to_scan):
            repos_left = len(to_scan) - index
            remaining = self.max_total_file_fetches - state.source_reads
            allowance = max(0, min(self.max_files_per_repo, max(3, remaining // repos_left), remaining))
            try:
                self._scan_repo(state, username, record["name"], allowance)
            except (_RateLimitError, _FatalApiError) as exc:
                result["status"] = "partial"
                result["message"] = str(exc)
                reasons.append("rate limit" if isinstance(exc, _RateLimitError) else "bad token")
                state.per_repo[-1].setdefault("error", "scan stopped: " + str(exc))
                result["repos_not_scanned"] = [r["name"] for r in to_scan[index:]] + result["repos_not_scanned"]
                break
            except Exception as exc:  # noqa: BLE001 - one odd repo must not stop the scan
                state.fail_current(f"unexpected error ({type(exc).__name__})")

        if state.failed:
            reasons.append(f"repo errors: {state.failed}")
        reasons.extend(f"tree truncated: {name}" for name in state.truncated)
        if state.budget_exhausted:
            reasons.append("source file budget exhausted")
        result.update(
            repos_scanned=sum(1 for p in state.per_repo if p["scanned"]),
            repos_failed=state.failed, api_calls_used=state.calls,
            skills=state.findings.skills_output(), unmapped_packages=state.findings.unmapped_output(),
            per_repo=state.per_repo, incomplete_reasons=reasons, complete=not reasons,
        )

    # -- one repository ------------------------------------------------------
    def _call(self, state: "_ScanState", path: str, params: Optional[dict[str, Any]] = None,
              allowed: tuple[int, ...] = ()) -> Any:
        state.calls += 1
        return self.validator._get(path, params=params, allowed_statuses=allowed)

    def _fetch_text(self, state: "_ScanState", base: str, path: str, branch: str) -> str:
        response = self._call(state, f"{base}/contents/{quote(path, safe='/')}", {"ref": branch})
        data = self.validator._json(response)
        if not isinstance(data, dict) or data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
            raise ValueError("unsupported file content")
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")

    def _scan_repo(self, state: "_ScanState", owner: str, name: str, allowance: int) -> None:
        per = {"name": name, "scanned": False, "default_branch": None, "tree_truncated": False,
               "dependency_files": [], "source_files_read": 0, "source_files_skipped": 0,
               "files_failed": 0, "python_files_read": 0, "skills": []}
        state.per_repo.append(per)
        state.findings.repo_labels = set()
        state.current = per

        if not REPO_NAME_RE.fullmatch(name) or name in (".", ".."):
            return state.fail_current("invalid repository name")
        base = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        try:
            meta = self.validator._json(self._call(state, base))
            branch = meta.get("default_branch") if isinstance(meta, dict) else None
            if not isinstance(branch, str) or not branch:
                return state.fail_current("no default branch")
            per["default_branch"] = branch
            tree = self._call(state, f"{base}/git/trees/{quote(branch, safe='')}", {"recursive": "1"}, (409,))
            if tree.status_code == 409:  # empty repository: valid, nothing to scan
                per["scanned"] = True
                return
            data = self.validator._json(tree)
            entries = data.get("tree")
            if not isinstance(entries, list):
                return state.fail_current("unexpected tree data")
        except _NotFoundError:
            return state.fail_current("repository not found (HTTP 404)")
        except (_RateLimitError, _FatalApiError):
            raise
        except _ApiError as exc:
            return state.fail_current(str(exc))

        if data.get("truncated"):
            per["tree_truncated"] = True
            state.truncated.append(name)
        blobs = [(e["path"], e.get("size") if isinstance(e.get("size"), int) else 0)
                 for e in entries if isinstance(e, dict) and e.get("type") == "blob"
                 and isinstance(e.get("path"), str)]

        self._file_signals(state, name, per, blobs)
        self._ide(state, base, branch, name, blobs)
        self._dependencies(state, base, branch, name, per, blobs)
        self._sources(state, base, branch, name, per, blobs, allowance)
        per["scanned"] = True
        per["skills"] = sorted(state.findings.repo_labels)

    def _file_signals(self, state: "_ScanState", repo: str, per: dict[str, Any],
                      blobs: list[tuple[str, int]]) -> None:
        for entry in self.signals.file_signals:
            matches = [p for p, _ in blobs if entry["regex"].search(p)]
            if not matches:
                continue
            state.findings.add_signal(entry, repo, matches[0], entry["evidence"])
            if entry["evidence"] == "sql_file":
                per["sql_files"] = len(matches)
                per["sql_file_examples"] = matches[:entry["max_examples"]]

    def _ide(self, state: "_ScanState", base: str, branch: str, repo: str,
             blobs: list[tuple[str, int]]) -> None:
        """.idea/ means a JetBrains IDE; an .iml with PYTHON_MODULE means PyCharm.
        A missing .idea/ proves nothing (it is often gitignored)."""
        idea_dirs = sorted({p[:p.index(".idea/") + 6] for p, _ in blobs
                            if p.startswith(".idea/") or "/.idea/" in p},
                           key=lambda d: (d.count("/"), d))
        if not idea_dirs:
            return
        label = "JetBrains IDE"
        imls = sorted(p for p, _ in blobs if p.endswith(".iml")
                      and p.startswith(idea_dirs[0]) and "/" not in p[len(idea_dirs[0]):])
        if imls:
            try:
                if 'type="PYTHON_MODULE"' in self._fetch_text(state, base, imls[0], branch):
                    label = "PyCharm"
            except (_RateLimitError, _FatalApiError):
                raise
            except (_NotFoundError, _ApiError, ValueError):
                state.current["files_failed"] += 1
        state.findings.add("label", label, repo, idea_dirs[0], "ide_folder")

    def _dependencies(self, state: "_ScanState", base: str, branch: str, repo: str,
                      per: dict[str, Any], blobs: list[tuple[str, int]]) -> None:
        candidates = []
        for path, size in blobs:
            if _in_skipped_dir(path):
                continue
            for pattern, kind in _DEPENDENCY_FILES:
                if pattern.search(path):
                    candidates.append((path.count("/"), path, kind, size))
                    break
        for _, path, kind, size in sorted(candidates)[:MAX_DEPENDENCY_FILES]:
            if size > MAX_FILE_BYTES:
                per["files_failed"] += 1
                continue
            try:
                parser, ecosystem = _DEPENDENCY_PARSERS[kind]
                names = parser(self._fetch_text(state, base, path, branch))
            except (_RateLimitError, _FatalApiError):
                raise
            except (_NotFoundError, _ApiError, ValueError, KeyError, TypeError):
                per["files_failed"] += 1
                continue
            per["dependency_files"].append(path)
            self._record_packages(state, names, ecosystem, repo, path)

    def _record_packages(self, state: "_ScanState", names: Iterable[str], ecosystem: str,
                         repo: str, path: str) -> None:
        index = self.signals.python_packages if ecosystem == "python" else self.signals.js_packages
        for name in dict.fromkeys(names):
            entry = index.get(name)
            if entry is None:
                if ecosystem == "js" and name.startswith("@types/"):
                    continue  # TypeScript helper packages are not skills; keep the unmapped list useful
                state.findings.add_unmapped(name, ecosystem, repo)
            else:
                state.findings.add_signal(entry, repo, path, "declared_dependency")

    def _sources(self, state: "_ScanState", base: str, branch: str, repo: str, per: dict[str, Any],
                 blobs: list[tuple[str, int]], allowance: int) -> None:
        candidates = [(p, s) for p, s in blobs
                      if p.lower().endswith(SOURCE_EXTENSIONS) and not _in_skipped_dir(p)
                      and not p.endswith(".min.js")]
        eligible = sorted((c for c in candidates if c[1] <= MAX_FILE_BYTES),
                          key=lambda c: (c[0].count("/"), -c[1], c[0]))
        per["source_files_skipped"] = len(candidates) - min(len(eligible), allowance)
        if eligible and allowance == 0:
            state.budget_exhausted = True
        for path, _ in eligible[:allowance]:
            state.source_reads += 1
            try:
                self._read_source(state, base, branch, repo, path)
                per["source_files_read"] += 1
            except (_RateLimitError, _FatalApiError):
                raise
            except (_NotFoundError, _ApiError, ValueError, KeyError, TypeError):
                per["files_failed"] += 1

    def _read_source(self, state: "_ScanState", base: str, branch: str, repo: str, path: str) -> None:
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
            if find_embedded_sql(text, "js"):
                state.findings.add_signal(self._sql_entry, repo, path, "embedded_query")

    @staticmethod
    def _count_python_file(state: "_ScanState", code: str) -> None:
        """Count a fetched and parsed Python file or notebook that has real code.

        GitHub labels notebook code "Jupyter Notebook", never "Python", so this
        count is how the comparison knows a candidate really wrote Python."""
        if code.strip():
            state.current["python_files_read"] += 1

    def _python_evidence(self, state: "_ScanState", code: str, repo: str, path: str) -> None:
        for module in sorted(find_python_imports(code)):
            entry = self.signals.python_imports.get(module)
            if entry is not None:
                state.findings.add_signal(entry, repo, path, "imported_in_code")
        if find_embedded_sql(code, "py"):
            state.findings.add_signal(self._sql_entry, repo, path, "embedded_query")


class _ScanState:
    """Mutable bookkeeping for one scan() call."""

    def __init__(self, findings: _Findings) -> None:
        self.findings = findings
        self.calls = 0
        self.source_reads = 0
        self.failed = 0
        self.truncated: list[str] = []
        self.budget_exhausted = False
        self.per_repo: list[dict[str, Any]] = []
        self.current: dict[str, Any] = {}

    def fail_current(self, message: str) -> None:
        self.current["error"] = message
        self.current["scanned"] = False
        self.failed += 1


def _in_skipped_dir(path: str) -> bool:
    return any(part in SKIP_DIR_NAMES for part in path.split("/")[:-1])


def _recency_key(record: dict[str, Any]) -> datetime:
    """Sort key: newest last_commit_date is largest; missing/odd dates sort oldest."""
    try:
        return parse_github_date(record["last_commit_date"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return datetime.min.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Demo helper: compare claimed skills with the evidence (NOT scoring)
# --------------------------------------------------------------------------
def compare_claimed_skills(
    claimed: Iterable[Any],
    validation_result: Any,
    tech_stack: Any,
    taxonomy: Taxonomy,
    detectable: Optional[set[str]] = None,
    scan_skills: Optional[set[str]] = None,
    implications: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """For each claimed skill, say what the evidence shows.

    Demo helper only: this is not scoring and not the Repo Fingerprint Score.
    ``detectable`` defaults to the skills the default signal map can find plus
    those with GitHub-language evidence. ``scan_skills`` (default: the signal
    map's skills plus SQL and Python) are the skills the tech scan itself can add
    evidence for; a skill outside it that has GitHub-language evidence is judged
    from the language statistics alone. ``implications`` (default: the file
    ``tech_implications.json``) lists concepts that real evidence implies, e.g.
    Express.js implies Node.js; it is used only when there is no direct evidence.
    """
    if implications is None:
        implications = _load_implications(None, taxonomy)
    implied_by = {e["skill"]: e["implied_by"] for e in implications}
    if detectable is None or scan_skills is None:
        signal_ids = set(_load_signal_map(None, taxonomy).skill_ids)
        if detectable is None:
            detectable = signal_ids | {"SQL"} | {
                s["id"] for s in taxonomy.skills if taxonomy.confirmable_via_languages(s["id"])}
        if scan_skills is None:
            scan_skills = signal_ids | set(SCAN_ALWAYS_SKILLS)
    tech = tech_stack if isinstance(tech_stack, dict) else {}
    items = [i for i in tech.get("skills", []) if isinstance(i, dict)]
    by_skill = {i["skill"]: i for i in items if i.get("skill")}
    by_label_key = {make_key(i["label"]): i for i in items if not i.get("in_taxonomy")}
    taxonomy_items = {k: v for k, v in by_skill.items() if v.get("in_taxonomy")}
    complete = tech.get("complete") is True
    python_files = _python_files_read(tech.get("per_repo"))

    languages: dict[str, Any] = {}
    languages_known = False  # True only when the validator result really had language statistics
    if isinstance(validation_result, dict) and isinstance(validation_result.get("summary"), dict):
        combined = validation_result["summary"].get("languages_combined")
        languages_known = isinstance(combined, dict)
        # Ignore non-numeric sizes from a malformed result instead of crashing on them.
        languages = ({k: v for k, v in combined.items()
                      if isinstance(v, (int, float)) and not isinstance(v, bool)}
                     if languages_known else {})
    mapped = taxonomy.map_github_languages(languages)["skills"] if languages else {}
    total_bytes = sum(v for v in languages.values() if isinstance(v, (int, float))) or 1
    notebook = languages.get("Jupyter Notebook")
    notebook_bytes = notebook if isinstance(notebook, (int, float)) and not isinstance(notebook, bool) else 0
    # Python and "Jupyter Notebook" are one share: GitHub files notebook code under the second name.
    python_share = ((languages.get("Python", 0) + notebook_bytes) * 100 / total_bytes) if languages else None

    entries: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for text in claimed:
        out = taxonomy.normalize_many([text])
        for match in out["matched"]:
            skill_id = match["skill"]
            if skill_id in entries:
                entries[skill_id]["claimed"].extend(match["inputs"])
                continue
            extras: list[tuple[dict[str, Any], str]] = []
            if skill_id == "Python":
                extras = _python_extra_sources(notebook_bytes, total_bytes, python_files, python_share)
            language_only = (languages_known and taxonomy.confirmable_via_languages(skill_id)
                             and skill_id not in scan_skills)
            entry = _judge_skill(skill_id, match["inputs"], by_skill.get(skill_id), mapped,
                                 total_bytes, detectable, complete, extras, language_only,
                                 implied_by.get(skill_id, []), taxonomy_items)
            entries[skill_id] = entry
            ordered.append(entry)
        for unknown in out["unknown"] + out["invalid"]:
            item = by_label_key.get(make_key(str(unknown["input"])))
            if item is not None:
                types = list(item["evidence_types"])
                ordered.append({"claimed": [unknown["input"]], "skill": None, "status": "found",
                                "confidence": item["confidence"],
                                "sources": [{"type": t, "repos": len(item["repos"])} for t in types]})
            else:
                ordered.append({"claimed": [unknown["input"]], "skill": None,
                                "status": "not_in_taxonomy", "confidence": None, "sources": []})
    return ordered


def _language_source(kind: str, size: float, total_bytes: float) -> tuple[dict[str, Any], str]:
    """A language-statistics source and its confidence. A share below
    MIN_LANGUAGE_SHARE_PERCENT is a trace: still real, but only weak evidence."""
    share = size * 100 / total_bytes
    source: dict[str, Any] = {"type": kind, "bytes": size, "percent": round(share, 2)}
    if share < MIN_LANGUAGE_SHARE_PERCENT:
        source["trace"] = True
        return source, "weak"
    return source, "strong"


def _python_files_read(per_repo: Any) -> int:
    """Total python_files_read over per_repo; tolerant of missing or odd data."""
    if not isinstance(per_repo, list):
        return 0
    total = 0
    for record in per_repo:
        count = record.get("python_files_read") if isinstance(record, dict) else None
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            total += count
    return total


def _python_extra_sources(notebook_bytes: float, total_bytes: float, python_files: int,
                          python_share: Optional[float]) -> list[tuple[dict[str, Any], str]]:
    """Extra Python evidence: GitHub's "Jupyter Notebook" bytes and Python files the scan read.

    File counts cannot make a trace look strong: if language statistics exist and the
    combined Python + notebook share is below MIN_LANGUAGE_SHARE_PERCENT, the files
    source is a weak trace too. Without any language statistics it stays strong."""
    extras: list[tuple[dict[str, Any], str]] = []
    if notebook_bytes > 0:
        extras.append(_language_source("language_stats_notebook", notebook_bytes, total_bytes))
    if python_files > 0:
        files_source: dict[str, Any] = {"type": "python_source_files", "files": python_files}
        if python_share is not None and python_share < MIN_LANGUAGE_SHARE_PERCENT:
            files_source["trace"] = True
            extras.append((files_source, "weak"))
        else:
            extras.append((files_source, "strong"))
    return extras


def _judge_skill(skill_id: str, inputs: list[Any], item: Optional[dict[str, Any]],
                 mapped: dict[str, Any], total_bytes: float, detectable: set[str],
                 complete: bool, extras: list[tuple[dict[str, Any], str]],
                 language_only: bool, implied_by: list[str],
                 taxonomy_items: dict[str, dict[str, Any]]) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    confidences: list[str] = []
    if skill_id in mapped:
        source, confidence = _language_source("language_stats", mapped[skill_id], total_bytes)
        sources.append(source)
        confidences.append(confidence)
    for source, confidence in extras:
        sources.append(source)
        confidences.append(confidence)
    if item is not None:
        sources.extend({"type": t, "repos": len(item["repos"])} for t in item["evidence_types"])
        confidences.append(item["confidence"])
    basis: Optional[str] = None
    inferred = None if sources else _infer(implied_by, taxonomy_items)
    if sources:
        status, confidence = "found", min(confidences, key=CONFIDENCE_RANK.get)
    elif inferred is not None:  # no direct evidence, but real evidence implies this skill
        status, confidence = "found", inferred[1]
        sources.append(inferred[0])
    else:
        confidence = None
        if skill_id not in detectable:
            status, basis = "not_detectable_from_github", "not_checkable"  # never "not found"
        elif complete:
            status, basis = "not_found", "full_scan"
        elif language_only:
            # Language statistics are already complete, so a language-only skill does not
            # depend on whether the tech scan finished.
            status, basis = "not_found", "language_stats_only"
        else:
            status, basis = "not_found_scan_incomplete", "incomplete_scan"
    entry = {"claimed": list(inputs), "skill": skill_id, "status": status,
             "confidence": confidence, "sources": sources}
    if basis is not None:
        entry["basis"] = basis
    return entry


def _infer(implied_by: list[str], taxonomy_items: dict[str, dict[str, Any]]
           ) -> Optional[tuple[dict[str, Any], str]]:
    """Inferred evidence from implier skills present in the scan, or None.

    Medium at best (weak if every implier found is weak), never strong, and never
    used next to direct evidence. The absence of an implier proves nothing."""
    present = [(skill_id, taxonomy_items[skill_id]) for skill_id in implied_by if skill_id in taxonomy_items]
    if not present:
        return None
    repos = {repo for _, item in present for repo in item.get("repos", [])}
    best = min((item.get("confidence", "medium") for _, item in present),
               key=lambda c: CONFIDENCE_RANK.get(c, CONFIDENCE_RANK["medium"]))
    return ({"type": "inferred", "from": [skill_id for skill_id, _ in present], "repos": len(repos)},
            "weak" if best == "weak" else "medium")
