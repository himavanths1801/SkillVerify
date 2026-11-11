"""File filter, language detection, logic score and secret redaction. Pure functions: no network, no AI."""
import ast
import json
import re
from typing import Optional

from skillverify_questions import config

# --------------------------------------------------------------------------
# path filter (applied to the file tree BEFORE anything is fetched)
# --------------------------------------------------------------------------
NOT_CODE, JUNK_FOLDER, GENERATED, TEST_FILE, SECRET_FILE, TOO_SMALL, TOO_BIG = (
    "not_code", "junk_folder", "generated", "test_file", "secret_file", "too_small", "too_big")


def extension_of(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name[name.rfind("."):].lower() if "." in name else ""


def language_of(path: str) -> Optional[str]:
    """The language name for a code file, or None when the extension is not a code extension."""
    return config.EXTENSION_LANGUAGE.get(extension_of(path))


def skip_reason(path: str, size: Optional[int] = None) -> Optional[str]:
    """Why a path can never be a question source, or None when it survives the filter.

    The checks run in a fixed order: secret-bearing names first (so such a file is never even listed as code),
    then non-code, vendored/build folders, generated names, tests, and finally the size window.
    """
    if any(pattern.search(path) for pattern in config.SECRET_NAME_PATTERNS):
        return SECRET_FILE
    if language_of(path) is None:
        return NOT_CODE
    folders = path.split("/")[:-1]
    if any(part.lower() in config.SKIP_FOLDERS and part.lower() not in _TEST_FOLDERS for part in folders):
        return JUNK_FOLDER
    if any(pattern.search(path) for pattern in config.GENERATED_NAME_PATTERNS):
        return GENERATED
    if any(part.lower() in _TEST_FOLDERS for part in folders) or any(
            pattern.search(path) for pattern in config.TEST_NAME_PATTERNS):
        return TEST_FILE
    if size is not None:
        if size < config.MIN_FILE_BYTES:
            return TOO_SMALL
        if size > config.MAX_FILE_BYTES:
            return TOO_BIG
    return None


_TEST_FOLDERS = frozenset({"test", "tests", "__tests__", "spec", "e2e"})


# --------------------------------------------------------------------------
# content
# --------------------------------------------------------------------------
def is_generated(text: str) -> bool:
    """True when one of the first lines says the file is generated."""
    head = text.splitlines()[:config.GENERATED_HEADER_LINES]
    return any(config.GENERATED_HEADER.search(line) for line in head)


def notebook_code(text: str) -> str:
    """The code cells of a notebook joined together; outputs and markdown are dropped. '' if unreadable."""
    try:
        cells = json.loads(text).get("cells", [])
    except (ValueError, AttributeError):
        return ""
    parts = []
    for cell in cells if isinstance(cells, list) else []:
        if isinstance(cell, dict) and cell.get("cell_type") == "code":
            source = cell.get("source", "")
            parts.append("".join(source) if isinstance(source, list) else str(source))
    return "\n\n".join(part.rstrip("\n") for part in parts if isinstance(part, str))


_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def strip_comments(text: str, language: str) -> str:
    if language in config.BLOCK_COMMENT_LANGUAGES:
        text = _BLOCK_COMMENT.sub(lambda m: "\n" * m.group().count("\n"), text)
    marker = config.LINE_COMMENT.get(language)
    if marker is None:
        return text
    return "\n".join("" if line.lstrip().startswith(marker) else line for line in text.splitlines())


def logic_lines(text: str, language: str) -> int:
    """Non-blank, non-comment lines."""
    return sum(1 for line in strip_comments(text, language).splitlines() if line.strip())


_DEF_REGEX = {
    "sql": re.compile(r"(?i)\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|FUNCTION|PROCEDURE|INDEX)\b"),
    "shell": re.compile(r"(?m)^\s*(?:function\s+\w+|\w+\s*\(\s*\)\s*\{)"),
    "default": re.compile(r"\b(?:function|def|fn|func|fun|class|interface|struct|enum|trait|impl)\b|=>"),
}
_FLOW_REGEX = {
    "sql": re.compile(r"(?i)\b(?:JOIN|WHERE|GROUP\s+BY|HAVING|CASE|UNION|WITH)\b"),
    "default": re.compile(r"\b(?:if|elif|for|foreach|while|switch|case|catch|try|match|select|when)\b"),
}


def _python_counts(text: str) -> Optional[tuple[int, int]]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    defs = flow = 0
    flow_nodes = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.ListComp,
                  ast.SetComp, ast.DictComp, ast.GeneratorExp) + ((ast.Match,) if hasattr(ast, "Match") else ())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs += 1
        elif isinstance(node, flow_nodes):
            flow += 1
    return defs, flow


def logic_counts(text: str, language: str) -> tuple[int, int, int]:
    """(logic lines, definitions, control-flow constructs). Python is counted with ``ast``, the rest with regexes."""
    lines = logic_lines(text, language)
    if language == "python":
        counted = _python_counts(text)
        if counted is not None:
            return lines, counted[0], counted[1]
    code = strip_comments(text, language)
    defs = len(_DEF_REGEX.get(language, _DEF_REGEX["default"]).findall(code))
    flow = len(_FLOW_REGEX.get(language, _FLOW_REGEX["default"]).findall(code))
    return lines, defs, flow


def logic_score(text: str, language: str) -> int:
    """How much real logic a file has (see the weights in config.py). Capped, so size alone cannot win."""
    lines, defs, flow = logic_counts(text, language)
    return (min(lines, config.LOGIC_LINES_CAP) + config.DEFINITION_WEIGHT * min(defs, config.DEFINITIONS_CAP)
            + config.CONTROL_FLOW_WEIGHT * min(flow, config.CONTROL_FLOW_CAP))


# --------------------------------------------------------------------------
# names defined in a file (used to check the names a question mentions)
# --------------------------------------------------------------------------
_NAME_REGEXES = (
    re.compile(r"\b(?:function|def|fn|func|fun|class|interface|struct|enum|trait)\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"),
    re.compile(r"(?im)^\s*(?:public|private|protected|static|final|abstract|override|\s)+[\w<>\[\],.? ]+?\s+"
               r"([A-Za-z_]\w*)\s*\([^;{)]*\)\s*(?:throws\s+[\w., ]+)?\{"),
    re.compile(r"(?i)\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|FUNCTION|PROCEDURE|INDEX)\s+"
               r"(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w.]*)"),
    re.compile(r"(?m)^\s*(?:function\s+)?([A-Za-z_]\w*)\s*\(\s*\)\s*\{"),
)


def _python_names(text: str) -> Optional[set[str]]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    names: set[str] = set()

    def targets(node: ast.AST) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                names.add(child.id)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                targets(target)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            targets(node.target)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    targets(item.optional_vars)
    return names


def defined_names(text: str, language: str) -> tuple[set[str], bool]:
    """(names defined in the file, whether the list is exact).

    Exact only for Python that parses (def, class and assignment targets). For other languages it is a best-effort
    regex list, so a name that is not in it is still accepted when it appears as a word in the file.
    """
    if language == "python":
        names = _python_names(text)
        if names is not None:
            return names, True
    found: set[str] = set()
    for regex in _NAME_REGEXES:
        found.update(match.group(1) for match in regex.finditer(text))
    return found, False


def has_definitions(text: str, language: str) -> bool:
    """True when the file defines something nameable (a function, class, view ...)."""
    if language == "python":
        counted = _python_counts(text)
        if counted is not None:
            return counted[0] > 0
    return bool(defined_names(text, language)[0])


# --------------------------------------------------------------------------
# secret redaction (input AND output)
# --------------------------------------------------------------------------
REDACTED = "[REDACTED]"
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL)
_TOKEN_FORMS = re.compile(r"\bAKIA[0-9A-Z]{16}\b|\bghp_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b"
                          r"|\bsk-[A-Za-z0-9_-]{20,}\b|\bAIza[0-9A-Za-z_-]{30,}\b|\bxox[abprs]-[A-Za-z0-9-]{10,}\b")
_SECRET_NAME = r"(?:[A-Za-z_][\w.\-]*)?(?:key|token|secret|password|passwd|pwd)[\w.\-]*"
_QUOTED_ASSIGNMENT = re.compile(r"(?i)(\b" + _SECRET_NAME + r"[\"']?\s*[:=]\s*)([\"'])([^\"'\n]{4,})\2")
_BARE_ASSIGNMENT = re.compile(r"(?i)(\b" + _SECRET_NAME + r"\s*=\s*)([A-Za-z0-9+/_\-]{12,})(?=\s*$)", re.MULTILINE)


def redact(text: str) -> str:
    """Replace anything that looks like a secret with ``[REDACTED]``. Idempotent."""
    text = _PRIVATE_KEY.sub(REDACTED, text)
    text = _TOKEN_FORMS.sub(REDACTED, text)
    text = _QUOTED_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}{m.group(2)}", text)
    return _BARE_ASSIGNMENT.sub(lambda m: m.group(1) + REDACTED, text)
