"""Starter code for a write_code question: a real excerpt of the fetched file, cut by CODE, never written by the model.

The model only says which symbol it means (the ``symbols`` of its answer). This module finds that symbol in the real
file text and copies the lines around it, verbatim:
  * the target is an EXISTING function or method: its signature stays and its whole body becomes one placeholder line in
    the file's own comment style (the answer is never shown); the imports and the enclosing class header stay;
  * otherwise (a variable, a class, an area): the largest enclosing block that fits the line limit, plus one closing
    placeholder line where new code is expected.
Python, JavaScript, TypeScript and Java are supported. For anything else, or when a safe cut is not possible (code that
does not parse or whose brackets do not balance, a one-line body, a line that looks like a secret, a URL or an e-mail
address), ``build_excerpt`` returns None and the starter stays empty. Nothing here logs or stores any text.
"""
import ast
import re
from typing import Optional, Sequence

from skillverify_questions import config, files
from skillverify_questions.writer import unsafe_line

PYTHON, BRACE = "python", ("javascript", "typescript", "java")
_COMMENT_MARK = {"python": "#", "javascript": "//", "typescript": "//", "java": "//"}
_CONTINUATION = re.compile(r"^\s*(?:else\b|catch\b|finally\b|[.)\]}?:]|&&|\|\|)")


def supported(language: str) -> bool:
    return language == PYTHON or language in BRACE


def _word(name: str, line: str) -> bool:
    return re.search(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", line) is not None


def _indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip())]


def _fits(lines: Sequence[str]) -> bool:
    return (len(lines) <= config.STARTER_MAX_LINES and sum(len(line) + 1 for line in lines) <= config.STARTER_MAX_CHARS
            and not any(unsafe_line(line) for line in lines))


def _imports(lines: Sequence[str], limit: int = config.EXCERPT_MAX_IMPORT_LINES) -> list[str]:
    return [line for line in lines if not unsafe_line(line)][:limit]


# --------------------------------------------------------------------------
# Python (the standard library parser gives exact line ranges)
# --------------------------------------------------------------------------
def _span(node: ast.AST) -> tuple[int, int]:
    starts = [node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]  # type: ignore[attr-defined]
    return min(starts), node.end_lineno  # type: ignore[attr-defined]


def _py_header(lines: list[str], node: ast.AST) -> Optional[list[str]]:
    """The def or class line(s) up to and including the colon (decorators too), or None for a one-line definition."""
    start, _ = _span(node)
    first_body = node.body[0].lineno  # type: ignore[attr-defined]
    if first_body == node.lineno:  # type: ignore[attr-defined]
        return None
    end = first_body - 1
    while end >= start and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
        end -= 1
    segment = lines[start - 1:end]
    return segment if segment and segment[-1].rstrip().endswith(":") else None


def _python(text: str, symbols: Sequence[str]) -> Optional[list[str]]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    lines = text.splitlines()
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    defs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    import_lines = _imports([lines[i - 1] for n in imports for i in range(n.lineno, n.end_lineno + 1)])  # type: ignore[attr-defined]
    prefix = import_lines + ([""] if import_lines else [])
    placeholder_implement = config.STARTER_PLACEHOLDERS["implement"][PYTHON]
    placeholder_add = config.STARTER_PLACEHOLDERS["add"][PYTHON]
    named = {s.split(".")[-1] for s in symbols} | {s.split(".")[0] for s in symbols}

    blocked: set[str] = set()  # existing functions whose body cannot be hidden: never shown as an "area" either
    for symbol in symbols:  # an existing function or method: keep its signature, hide its body
        parts = symbol.split(".")
        found = [d for d in defs if d.name == parts[-1] and (len(parts) < 2 or getattr(parents.get(d), "name", None) == parts[-2])]
        found.sort(key=lambda d: getattr(parents.get(d), "name", None) not in named)
        if found:
            blocked.add(symbol)
        for node in found:
            owner = parents.get(node)
            signature = _py_header(lines, node)
            body = node.body[0]
            if signature is None or any(unsafe_line(line) for line in signature):
                continue
            indent = lines[body.lineno - 1][:body.col_offset]
            out = list(prefix)
            if isinstance(owner, ast.ClassDef):
                head = _py_header(lines, owner)
                if head is None or any(unsafe_line(line) for line in head):
                    continue
                out += head
                members = [m for m in owner.body if m is not node and m.lineno < node.lineno
                           and not (isinstance(m, ast.Expr) and isinstance(getattr(m, "value", None), ast.Constant))]
                chosen: list[list[str]] = []
                room = config.STARTER_MAX_LINES - len(out) - len(signature) - 1
                for member in reversed(members):
                    start, end = _span(member)
                    chunk = lines[start - 1:end]
                    if len(chunk) + 1 > room or any(unsafe_line(line) for line in chunk):
                        break
                    chosen.insert(0, chunk)
                    room -= len(chunk) + 1
                for chunk in chosen:
                    out += chunk + [""]
            elif owner is not tree:
                continue  # nested in a function: not cut
            out += signature + [indent + placeholder_implement]
            if _fits(out):
                return out

    options: list[list[str]] = []  # an area: for every place a symbol is used, the largest enclosing block that fits
    for symbol in symbols:
        if symbol in blocked:
            continue
        name = symbol.split(".")[-1]
        places = [i for i, line in enumerate(lines, 1) if _word(name, line) and not line.lstrip().startswith("#")
                  and not any(n.lineno <= i <= n.end_lineno for n in imports)]  # type: ignore[attr-defined]
        for at in places[:config.EXCERPT_MAX_PLACES]:
            stmts = [n for n in ast.walk(tree) if isinstance(n, ast.stmt) and _span(n)[0] <= at <= n.end_lineno  # type: ignore[attr-defined]
                     and not isinstance(n, (ast.Import, ast.ImportFrom))]
            for node in sorted(stmts, key=lambda n: _span(n)[0] - n.end_lineno):  # type: ignore[attr-defined]
                start, end = _span(node)
                out = list(prefix)
                owner = parents.get(node)
                if isinstance(owner, ast.ClassDef):
                    head = _py_header(lines, owner)
                    if head is None:
                        continue
                    out += head
                elif owner is not tree:
                    continue
                chunk = lines[start - 1:end]
                out += chunk + [_indent(chunk[0]) + placeholder_add]
                if _fits(out):
                    options.append(out)
                    break
    return max(options, key=len) if options else None


# --------------------------------------------------------------------------
# JavaScript, TypeScript and Java (a small scanner; anything it cannot read safely is given up)
# --------------------------------------------------------------------------
def _mask(text: str, language: str) -> Optional[str]:
    """The text with comments and string contents blanked (same length, same line breaks), or None if it cannot be read."""
    out: list[str] = []
    i, n = 0, len(text)
    blank = lambda seg: re.sub(r"[^\n]", " ", seg)  # noqa: E731
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                return None
            out.append(blank(text[i:j + 2]))
            i = j + 2
        elif language == "java" and text.startswith('"""', i):
            j = text.find('"""', i + 3)
            if j < 0:
                return None
            out.append(blank(text[i:j + 3]))
            i = j + 3
        elif c in "\"'" or (c == "`" and language != "java"):
            j = i + 1
            while j < n and text[j] != c:
                if text[j] == "\\":
                    j += 1
                elif text[j] == "\n" and c != "`":
                    return None  # an unterminated string: not something to cut safely
                j += 1
            if j >= n:
                return None
            out.append(blank(text[i:j + 1]))
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _depths(code_lines: list[str]) -> Optional[list[int]]:
    """The bracket depth at the start of every line, or None when the brackets do not balance."""
    depth, before = 0, []
    for line in code_lines:
        before.append(depth)
        for char in line:
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
                if depth < 0:
                    return None
    return before if depth == 0 else None


def _def_regex(name: str, language: str) -> re.Pattern[str]:
    n = re.escape(name)
    if language == "java":
        return re.compile(r"^\s*(?:(?:public|private|protected|static|final|abstract|synchronized|native|default)\s+)*"
                          r"(?:<[^>]+>\s*)?(?:[\w<>\[\],.?]+\s+)?" + n + r"\s*\(")
    return re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:"
                      r"(?:async\s+)?function\s*\*?\s*" + n + r"\s*[(<]"
                      r"|(?:const|let|var)\s+" + n + r"\b[^=;]*=\s*(?:async\s+)?(?:function\b|\(|[\w$]+\s*=>)"
                      r"|(?:(?:public|private|protected|static|async|get|set|readonly|override)\s+)*" + n + r"\s*[(<])")


def _body_brace(code: str, start: int) -> Optional[int]:
    """The offset of the ``{`` that opens the body of the definition starting at ``start`` (None: none before ``;``)."""
    parens, i = 0, start
    while i < len(code):
        char = code[i]
        if char in "([":
            parens += 1
        elif char in ")]":
            parens -= 1
        elif parens == 0 and char == ";":
            return None
        elif parens == 0 and char == "=" and code.startswith("=>", i):
            rest = code[i + 2:].lstrip()
            return len(code) - len(rest) if rest.startswith("{") else None
        elif parens == 0 and char == "{":
            return i
        i += 1
    return None


def _brace(text: str, symbols: Sequence[str], language: str) -> Optional[list[str]]:
    code_text = _mask(text, language)
    if code_text is None:
        return None
    lines, code_lines = text.splitlines(), code_text.splitlines()
    before = _depths(code_lines)
    if before is None or len(code_lines) != len(lines):
        return None
    offsets = [0]
    for line in code_lines:
        offsets.append(offsets[-1] + len(line) + 1)
    import_re = re.compile(r"^\s*(?:import\b|package\b|const\s+[\w${}, ]+=\s*require\()")
    head_imports: list[str] = []
    for line, code in zip(lines, code_lines):
        if import_re.match(code):
            head_imports.append(line)
        elif code.strip() not in ("", ";"):
            break
    prefix = _imports(head_imports)
    prefix = prefix + ([""] if prefix else [])

    def headers(first: int, level: int) -> Optional[tuple[list[str], list[str]]]:
        """The lines that open and close the blocks around line ``first``: (opening lines, closing lines)."""
        if level > 2:
            return None
        opening, closing = [], []
        for d in range(level):
            at = max((i for i in range(first) if before[i] == d and code_lines[i].strip()), default=None)
            if at is None or not code_lines[at].rstrip().endswith("{") or unsafe_line(lines[at]):
                return None
            if code_lines[at].strip() == "{":  # a brace on its own line: the line above is the real header
                above = max((i for i in range(at) if code_lines[i].strip()), default=None)
                if above is None or unsafe_line(lines[above]):
                    return None
                opening.append(lines[above])
            opening.append(lines[at])
            closing.insert(0, _indent(lines[at]) + "}")
        return opening, closing

    blocked: set[str] = set()  # existing functions whose body cannot be hidden: never shown as an "area" either
    for symbol in symbols:  # an existing function or method: keep its signature, hide its body
        name = symbol.split(".")[-1]
        regex = _def_regex(name, language)
        for index, code in enumerate(code_lines):
            if not regex.match(code):
                continue
            blocked.add(symbol)
            opened = _body_brace(code_text, offsets[index] + len(_indent(code)))
            if opened is None:
                continue
            brace_line = max(i for i in range(len(offsets) - 1) if offsets[i] <= opened)
            level = before[index]
            close = next((i for i in range(brace_line + 1, len(lines))
                          if (before[i + 1] if i + 1 < len(before) else 0) <= level), None)
            if (close is None or not code_lines[close].lstrip().startswith("}")
                    or code_lines[brace_line].rstrip()[-1:] != "{"):
                continue  # a one-line body, or something that is not a plain block: not cut
            wrap = headers(index, before[index])
            signature = lines[index:brace_line + 1]
            if wrap is None or any(unsafe_line(line) for line in signature + [lines[close]]):
                continue
            body = [i for i in range(brace_line + 1, close) if code_lines[i].strip()]
            indent = _indent(lines[body[0]]) if body else _indent(lines[index]) + "    "
            out = prefix + wrap[0] + signature + [indent + config.STARTER_PLACEHOLDERS["implement"][language],
                                                  lines[close]] + wrap[1]
            if _fits(out):
                return out

    options: list[list[str]] = []  # an area: for every place a symbol is used, the largest enclosing block that fits
    for symbol in symbols:
        if symbol in blocked:
            continue
        name = symbol.split(".")[-1]
        places = [i for i, code in enumerate(code_lines) if _word(name, code) and not import_re.match(code)]
        for at in places[:config.EXCERPT_MAX_PLACES]:
            for level in range(0, before[at] + 1):
                start = max((i for i in range(at + 1) if before[i] == level and code_lines[i].strip()
                             and not _CONTINUATION.match(code_lines[i])), default=None)
                if start is None:
                    continue
                end = start
                while end + 1 < len(lines) and (before[end + 1] > level or not code_lines[end + 1].strip()
                                                or _CONTINUATION.match(code_lines[end + 1])):
                    end += 1
                while end > start and not code_lines[end].strip():
                    end -= 1
                wrap = headers(start, level)
                if wrap is None:
                    continue
                chunk = lines[start:end + 1]
                out = (prefix + wrap[0] + chunk + [_indent(chunk[0]) + config.STARTER_PLACEHOLDERS["add"][language]]
                       + wrap[1])
                if _fits(out):
                    options.append(out)
                    break
    return max(options, key=len) if options else None


# --------------------------------------------------------------------------
# the public entry points
# --------------------------------------------------------------------------
def build_excerpt(text: str, language: str, symbols: Sequence[str]) -> Optional[str]:
    """The starter code cut from the real file text for these symbols, or None when it cannot be done safely."""
    if not supported(language) or not any(symbols):
        return None
    wanted = [s.strip() for s in symbols if s.strip()]
    lines = _python(text, wanted) if language == PYTHON else _brace(text, wanted, language)
    return "\n".join(lines).strip("\n") if lines else None


def model_starter_is_real(starter: str, text: str, language: str) -> bool:
    """True when every line of a model-written starter is a line of the file, blank, a comment or a placeholder line."""
    known = {line.strip() for line in text.splitlines() if line.strip()}
    marks = {_COMMENT_MARK.get(language, "#"), "#", "//", "/*", "*"}
    holders = {p.strip() for table in config.STARTER_PLACEHOLDERS.values() for p in table.values()}
    for line in starter.splitlines():
        text_line = line.strip()
        if text_line and text_line not in known and text_line not in holders and not text_line.startswith(tuple(marks)):
            return False
    return True


def starter_for(question: dict, text: str, language: str) -> tuple[str, str]:
    """(starter code, source) for an accepted write_code question: ``code`` (built here), ``model`` (the model's own,
    kept only when every line is real) or ``none`` (empty). Never raises."""
    try:
        built = build_excerpt(text, language, question.get("symbols") or [])
    except Exception:  # noqa: BLE001 - an excerpt problem must never fail a question
        built = None
    if built:
        return built, "code"
    own = question.get("starter_code") or ""
    if own.strip() and model_starter_is_real(own, text, language) and files.redact(own) == own and _fits(own.splitlines()):
        return own, "model"
    return "", "none"
