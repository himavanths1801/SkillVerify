"""Comments in code: finding them safely, and removing the ones a model ADDED to the starter code of a fix question.

A fix-the-bug starter must be the real file lines with ONE bug planted in the code, with no comment that explains,
labels or hints at it. ``strip_added_comments`` compares every comment of the starter with the comments of the real
file: a comment that does not exist in the file is removed by code (a whole-line comment, a trailing comment, a block
comment, a JSX ``{/* ... */}`` comment, an HTML ``<!-- -->`` comment); a comment that exists in the file stays.

The scanner is conservative. It understands Python (``#``; strings and docstrings are never comments) and the C-like
languages (``//``, ``/* */``; strings, template literals and regular expressions are skipped). When it cannot tell
safely what a line is (an apostrophe in JSX text, an unterminated string, a regular expression that does not close on
its line), it LEAVES THE LINE ALONE; if such a line was added by the model and has a hint word after a comment marker,
``hinted`` is set so the question is dropped instead. Nothing here keeps, logs or returns any comment text.
"""
import re
from dataclasses import dataclass, field
from typing import Optional

from skillverify_questions import config

PYTHON = "python"
SLASH_LANGUAGES = frozenset({"javascript", "typescript", "java", "c", "cpp", "csharp", "go", "rust", "kotlin", "scala"})
_TEMPLATE_LANGUAGES = frozenset({"javascript", "typescript", "go"})
_REGEX_BEFORE = re.compile(r"(?:^|[(,=:\[!&|?{};+\-*%~^])\s*$|\b(?:return|typeof|case|in|of|delete|void|throw|new|await|yield)\s*$")
_HINT = re.compile(r"(?i)(?<![\w])(?:" + "|".join(re.escape(w) for w in config.HINT_WORDS) + r")(?![\w])")
_MARKERS = re.compile(r"//|/\*|<!--|#")


def supported(language: str) -> bool:
    return language == PYTHON or language in SLASH_LANGUAGES


@dataclass
class Comment:
    pieces: list[tuple[int, int, int]] = field(default_factory=list)  # (line index, first column, end column)
    text: str = ""


@dataclass
class Scan:
    comments: list[Comment] = field(default_factory=list)
    uncertain: set[int] = field(default_factory=set)  # lines the scanner could not read safely


def _skip_string(line: str, start: int) -> Optional[int]:
    """The index after the quoted string that starts at ``start`` on this line, or None when it does not close here."""
    quote, j = line[start], start + 1
    while j < len(line):
        if line[j] == "\\":
            j += 1
        elif line[j] == quote:
            return j + 1
        j += 1
    return None


def _scan_python(lines: list[str]) -> Scan:
    scan, triple = Scan(), None
    for index, line in enumerate(lines):
        i = 0
        while i < len(line):
            if triple is not None:
                j = line.find(triple, i)
                if j < 0:
                    break
                i, triple = j + 3, None
                continue
            char = line[i]
            if char == "#":
                scan.comments.append(Comment([(index, i, len(line))], line[i + 1:]))
                break
            if line.startswith(('"""', "'''"), i):
                triple, i = line[i:i + 3], i + 3
            elif char in "\"'":
                end = _skip_string(line, i)
                if end is None:
                    scan.uncertain.add(index)
                    break
                i = end
            else:
                i += 1
    return scan


def _scan_slash(lines: list[str], language: str) -> Scan:
    scan = Scan()
    block: Optional[Comment] = None
    closer = ""
    template = False
    first_col: Optional[int] = None  # where the block comment that is being read opened on its first line
    html = language in ("javascript", "typescript")
    for index, line in enumerate(lines):
        i = 0
        while i < len(line):
            if block is not None:
                j = line.find(closer, i)
                end = len(line) if j < 0 else j + len(closer)
                block.pieces.append((index, i if first_col is None else first_col, end))
                first_col = None
                block.text += line[i:end] + "\n"
                if j < 0:
                    break
                scan.comments.append(block)
                block, i = None, end
                continue
            if template:
                j = i
                while j < len(line) and line[j] != "`":
                    j += 2 if line[j] == "\\" else 1
                if j >= len(line):
                    break
                template, i = False, j + 1
                continue
            char = line[i]
            if line.startswith("//", i):
                scan.comments.append(Comment([(index, i, len(line))], line[i + 2:]))
                break
            if line.startswith("/*", i) or (html and line.startswith("<!--", i)):
                closer = "*/" if line.startswith("/*", i) else "-->"
                block, first_col, i = Comment(), i, i + (2 if closer == "*/" else 4)
                continue
            if char in "\"'":
                end = _skip_string(line, i)
                if end is None:
                    scan.uncertain.add(index)
                    break
                i = end
            elif char == "`" and language in _TEMPLATE_LANGUAGES:
                template, i = True, i + 1
            elif char == "/" and language in ("javascript", "typescript") and _REGEX_BEFORE.search(line[:i]):
                j, in_class = i + 1, False
                while j < len(line):
                    if line[j] == "\\":
                        j += 1
                    elif line[j] == "[":
                        in_class = True
                    elif line[j] == "]":
                        in_class = False
                    elif line[j] == "/" and not in_class:
                        break
                    j += 1
                if j >= len(line) or line[j + 1:j + 2] == "/":  # not closed here, or "//" read as an empty regex
                    scan.uncertain.add(index)
                    break
                i = j + 1
            else:
                i += 1
    if block is not None:  # a block comment that never closes: the rest of the text is not understood
        scan.uncertain.update(range(block.pieces[0][0], len(lines)))
    return scan


def scan_lines(lines: list[str], language: str) -> Scan:
    return _scan_python(lines) if language == PYTHON else _scan_slash(lines, language)


def _normal(comment: Comment) -> str:
    text = re.sub(r"/\*|\*/|<!--|-->", " ", comment.text)
    return " ".join(part.strip(" \t*/#-!<>") for part in text.splitlines()).strip().lower()


def _remove(lines: list[str], scan: Scan, doomed: list[Comment]) -> list[str]:
    """The lines without the given comments. A line that only held a comment disappears; a trailing comment goes and
    the code on the line stays (without trailing spaces); the braces of a JSX ``{/* ... */}`` go with it."""
    spans: dict[int, list[tuple[int, int]]] = {}
    for comment in doomed:
        for index, start, end in comment.pieces:
            line = lines[index]
            before, after = line[:start].rstrip(), line[end:]
            if before.endswith("{") and after.lstrip().startswith("}"):  # JSX: {/* ... */}
                start, end = len(before) - 1, end + (len(after) - len(after.lstrip())) + 1
            spans.setdefault(index, []).append((start, end))
    out = []
    for index, line in enumerate(lines):
        if index not in spans:
            out.append(line)
            continue
        for start, end in sorted(spans[index], reverse=True):
            line = line[:start] + line[end:]
        if line.strip():
            out.append(line.rstrip())
    return out


def remove_all(text: str, language: str) -> str:
    """The text without any comment (for comparing a starter with its file). Unsupported languages are returned as is."""
    if not supported(language):
        return text
    lines = text.splitlines()
    scan = scan_lines(lines, language)
    return "\n".join(_remove(lines, scan, scan.comments))


@dataclass
class Stripped:
    text: str
    removed: int = 0       # how many added comments were taken out
    hinted: bool = False   # an added comment that could not be removed safely has a hint word


def strip_added_comments(starter: str, file_text: str, language: str) -> Stripped:
    """The starter without the comments that are not in the real file (see the module docstring)."""
    if not supported(language):
        return Stripped(starter)
    lines = starter.splitlines()
    scan = scan_lines(lines, language)
    file_lines = file_text.splitlines()
    known = [_normal(c) for c in scan_lines(file_lines, language).comments]
    added = [c for c in scan.comments if _normal(c) and not any(_normal(c) in k for k in known)]
    real = {line.strip() for line in file_lines}
    hinted = False
    for index in sorted(scan.uncertain):
        line = lines[index]
        marker = _MARKERS.search(line)
        if line.strip() not in real and marker is not None and _HINT.search(line[marker.end():]):
            hinted = True
    text = "\n".join(_remove(lines, scan, added)) if added else starter
    if added and starter.endswith("\n"):
        text += "\n"
    return Stripped(text, len(added), hinted)
