"""Starter code for write_code questions: a real excerpt cut by code. Fake files, fake writer, offline."""
import logging
import re

import pytest
from test_questions_support import (FakeFiles, FakeWriter, github_result, good_answer, repo_record, skill_row,
                                    tech_item, tree_entry)

from skillverify_questions import config, excerpt, select, service
from skillverify_questions.signals import Signals

PY = '''import requests
from bs4 import BeautifulSoup


class BaseScraper:
    """Base class."""
    BASE = "x"

    def fetch(self, url):
        resp = requests.get(url, timeout=5)
        return resp.text

    def parse(self, html):
        soup = BeautifulSoup(html, "html.parser")
        return [a.text for a in soup.find_all("a")]

    def run(self):
        return self.parse(self.fetch("u"))


def helper(value):
    total = 0
    for item in value:
        total += item
    return total
'''
JS = '''import debounce from "lodash";
const searchInput = document.getElementById("search");
const results = [];

searchInput.addEventListener("input", (event) => {
  const q = event.target.value;
  if (q.length > 2) {
    fetch("/api?q=" + q).then((r) => r.json()).then((d) => results.push(d));
  }
});

function render(items) {
  items.forEach((i) => console.log(i));
}

const total = (items) => {
  return items.length;
};

const double = (x) => x * 2;

class Store {
  add(item) {
    this.items.push(item);
  }
}
'''
TS = '''export class Cart {
  items: number[] = [];

  total(): number {
    let sum = 0;
    for (const i of this.items) {
      sum += i;
    }
    return sum;
  }
}
'''
JAVA = '''package shop;

import java.util.List;

public class Orders {
    private List<Integer> ids;

    public int count(List<Integer> input) {
        int n = 0;
        for (int i : input) {
            n += 1;
        }
        return n;
    }
}
'''


def build(text, language, *symbols):
    return excerpt.build_excerpt(text, language, list(symbols))


# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------
def test_python_existing_method_keeps_signature_and_loses_body():
    out = build(PY, "python", "BaseScraper.parse")
    assert out.splitlines() == [
        "import requests", "from bs4 import BeautifulSoup", "", "class BaseScraper:", '    BASE = "x"', "",
        "    def fetch(self, url):", "        resp = requests.get(url, timeout=5)", "        return resp.text", "",
        "    def parse(self, html):", "        # TODO: implement"]
    assert "soup" not in out and "find_all" not in out  # the answer is not shown
    assert out.splitlines()[-1] == "        # TODO: implement"


def test_python_unqualified_symbols_and_the_class_decides_between_same_named_methods():
    both = PY + "\n\nclass Other:\n    def parse(self, x):\n        return x\n"
    assert "class BaseScraper" in build(both, "python", "BaseScraper", "parse")
    assert "class Other" in build(both, "python", "Other.parse")
    assert "return x" not in build(both, "python", "Other.parse")


def test_python_top_level_function_and_indentation_is_kept_exactly():
    out = build(PY, "python", "helper")
    assert out.splitlines()[-2:] == ["def helper(value):", "    # TODO: implement"]
    tabbed = "import os\n\n\ndef f(x):\n\tif x:\n\t\treturn 1\n\treturn 2\n"
    assert build(tabbed, "python", "f").splitlines()[-1] == "\t# TODO: implement"


def test_python_decorators_async_and_multiline_signatures():
    text = ("import functools\n\n\n@functools.lru_cache(maxsize=2)\nasync def load(\n    a,\n    b,\n):\n"
            "    # a comment\n    return a + b\n")
    assert build(text, "python", "load").splitlines() == [
        "import functools", "", "@functools.lru_cache(maxsize=2)", "async def load(", "    a,", "    b,", "):",
        "    # TODO: implement"]


def test_python_an_area_symbol_shows_the_enclosing_block_and_a_closing_placeholder():
    out = build(PY, "python", "BASE")
    assert out.splitlines()[-1] == "# TODO: add your code here" and "def run(self):" in out
    inside = "import os\n\n\ndef work(n):\n    limit = 3\n    for i in range(n):\n        limit += i\n    return limit\n"
    out = build(inside, "python", "limit")
    assert out.splitlines()[-2:] == ["    return limit", "# TODO: add your code here"]


def test_python_the_largest_block_that_fits_is_used_and_a_member_gets_its_class_header():
    big = ("class Big:\n" + "\n".join(f"    def m{i}(self):\n        return {i}\n" for i in range(30))
           + "\n    def target(self):\n        marker = 1\n        return marker\n")
    out = build(big, "python", "marker")
    assert out.splitlines()[0] == "class Big:" and "def target(self):" in out and "m3" not in out
    assert len(out.splitlines()) <= config.STARTER_MAX_LINES


def test_python_the_forty_line_limit_is_respected_and_sibling_members_are_dropped_first():
    members = "\n".join(f"    def m{i}(self):\n        return {i}\n" for i in range(30))
    text = f"import os\n\nclass C:\n{members}\n    def target(self):\n        return 1\n"
    out = build(text, "python", "C.target")
    lines = out.splitlines()
    assert len(lines) <= config.STARTER_MAX_LINES and lines[-2:] == ["    def target(self):", "        # TODO: implement"]
    assert "m29" in out and "m0" not in out  # the members nearest to the target stay
    assert all(line == line.rstrip("\n") for line in lines)


def test_python_unusable_files_give_nothing():
    assert build("def broken(:\n    pass\n", "python", "broken") is None          # does not parse
    assert build(PY, "python", "no_such_name") is None and build(PY, "python") is None
    assert build("# marker is only named in a comment\nx = 1\n", "python", "marker") is None
    huge = "def f():\n" + "\n".join(f"    value{i} = {i}" for i in range(60)) + "\n    marker = 1\n"
    assert build(huge, "python", "marker") is None                               # no block fits the limit
    assert build(PY, "python", "requests") is not None                           # a use of an import is an area


def test_python_an_existing_function_that_cannot_be_cut_is_never_shown_as_an_area():
    nested = "def outer():\n    def inner(x):\n        return x + 1\n    return inner\n"
    assert build(nested, "python", "inner") is None  # showing the area would show the answer
    assert build("def one(x): return x\n", "python", "one") is None


def test_python_unsafe_lines_are_never_included():
    text = PY.replace('    BASE = "x"', '    API_TOKEN = "abcd1234efgh5678"')
    assert "[REDACTED]" not in (build(text.replace("API_TOKEN", "api_token"), "python", "BaseScraper.parse") or "")
    text = PY.replace("    def run(self):", "    # docs: https://example.com/x\n    def run(self):")
    out = build(text, "python", "BaseScraper.parse")
    assert out is not None and "example.com" not in out
    mail = PY.replace("def helper(value):", "def helper(value):\n    owner = 'someone@example.com'")
    out = build(mail, "python", "helper")  # the unsafe line is inside the hidden body: the excerpt is still fine
    assert out is not None and "someone@example.com" not in out and out.endswith("# TODO: implement")


def test_python_an_unsafe_line_inside_the_only_block_drops_the_excerpt():
    text = "import os\n\nTOKEN = 'plain'\npassword = 'hunter2hunter2'\n"
    assert build(text, "python", "password") is None


# --------------------------------------------------------------------------
# JavaScript, TypeScript, Java
# --------------------------------------------------------------------------
def test_js_listener_area_is_shown_whole_with_a_closing_placeholder():
    out = build(JS, "javascript", "searchInput")
    assert out.splitlines() == [
        'import debounce from "lodash";', "",
        'searchInput.addEventListener("input", (event) => {', "  const q = event.target.value;", "  if (q.length > 2) {",
        '    fetch("/api?q=" + q).then((r) => r.json()).then((d) => results.push(d));', "  }", "});",
        "// TODO: add your code here"]


@pytest.mark.parametrize("symbol", ["render", "total", "add"])
def test_js_existing_functions_keep_the_signature_and_lose_the_body(symbol):
    out = build(JS, "javascript", symbol)
    assert "// TODO: implement" in out and "items.forEach" not in out and "return items.length" not in out
    assert "this.items.push" not in out
    if symbol == "add":
        assert out.splitlines()[-3:] == ["  add(item) {", "    // TODO: implement", "  }"] or "class Store {" in out
        assert out.splitlines()[-1] == "}"


def test_js_arrow_without_braces_and_unreadable_code_are_not_cut_as_a_function():
    assert build(JS, "javascript", "double") is None  # an arrow function without a block: no safe cut, nothing shown
    assert build("const a = `open {\n", "javascript", "a") is None                 # unterminated template
    assert build('const s = "open\nconst b = 1;\n', "javascript", "s") is None    # unterminated string
    assert build("function f() {\n  /* never closed\n}\n", "javascript", "f") is None
    assert build("function f() {\n  return 1;\n", "javascript", "f") is None      # brackets do not balance
    assert build("function f() { return 1; }\n", "javascript", "f") is None       # a one-line body


def test_js_braces_in_strings_comments_and_templates_do_not_confuse_the_cut():
    text = ('// { not code\nfunction f(a) {\n  const s = "}" + `{${a}}`; /* } */\n  return s;\n}\n\n'
            "function g() {\n  return 2;\n}\n")
    out = build(text, "javascript", "f")
    assert out.splitlines() == ["function f(a) {", "  // TODO: implement", "}"]


def test_ts_method_inside_a_class_gets_its_header_and_closing_brace():
    out = build(TS, "typescript", "total")
    assert out.splitlines() == ["export class Cart {", "  total(): number {", "    // TODO: implement", "  }", "}"]


def test_java_method_and_area_with_package_and_imports():
    out = build(JAVA, "java", "count")
    assert out.splitlines() == ["package shop;", "import java.util.List;", "", "public class Orders {",
                                "    public int count(List<Integer> input) {", "        // TODO: implement", "    }", "}"]
    area = build(JAVA, "java", "ids")
    assert "private List<Integer> ids;" in area and area.splitlines()[-1] == "// TODO: add your code here"
    assert "n += 1" in area  # the whole class fits and is shown: the target was a field


def test_brace_languages_give_up_when_a_header_or_line_is_unsafe_or_too_deep():
    deep = "class A {\n  class B {\n    class C {\n      void f() {\n        int x = 1;\n      }\n    }\n  }\n}\n"
    assert build(deep, "java", "f") is None
    allman = "class A\n{\n  void f() {\n    int x = 1;\n  }\n}\n"
    assert build(allman, "java", "f").splitlines()[:3] == ["class A", "{", "  void f() {"]  # a brace on its own line
    assert build("{\n  void f() {\n    int x = 1;\n  }\n}\n", "java", "f") is None  # nothing above the lone brace
    assert build("class A // https://example.com\n{\n  void f() {\n    int x = 1;\n  }\n}\n", "java", "f") is None
    odd = "class A extends B,\n    C\n{\n  void f() {\n    int x = 1;\n  }\n}\n"
    assert build(odd, "java", "f") is not None
    url = JS.replace("function render(items) {", "function render(items) { // https://example.com")
    assert build(url, "javascript", "render") is None or "example.com" not in build(url, "javascript", "render")
    secret = 'function f() {\n  const apiKey = "abcd1234efgh5678";\n  return apiKey;\n}\n'
    hidden = build(secret, "javascript", "f")  # the secret is inside the hidden body: never shown
    assert hidden is not None and "abcd1234" not in hidden
    assert build('const apiKey = "abcd1234efgh5678";\nconst b = 1;\n', "javascript", "apiKey") is None  # an area with it
    mail = "function f() {\n  return 1;\n}\n// someone@example.com\nconst z = 1;\n"
    assert "someone@example.com" not in (build(mail, "javascript", "f") or "")


def test_imports_with_unsafe_text_are_skipped_and_many_imports_are_capped():
    many = "".join(f'import a{i} from "m{i}";\n' for i in range(20)) + "function f() {\n  return 1;\n}\n"
    out = build(many, "javascript", "f")
    assert out.count("import ") == config.EXCERPT_MAX_IMPORT_LINES
    unsafe = 'import a from "https://cdn.example.com/a.js";\nfunction f() {\n  return 1;\n}\n'
    assert "cdn.example.com" not in build(unsafe, "javascript", "f")
    assert build("'use strict';\nconst x = require('y');\nfunction f() {\n  return 1;\n}\n", "javascript", "f").startswith(
        "const x = require('y');")


def test_unsupported_languages_and_empty_symbols_give_nothing():
    assert build("package main\nfunc f() {}\n", "go", "f") is None and build("x", "rust", "x") is None
    assert build(JS, "javascript") is None and build(JS, "javascript", "") is None
    assert excerpt.supported("python") and excerpt.supported("java") and not excerpt.supported("go")


# --------------------------------------------------------------------------
# the model's own starter, and the choice of source
# --------------------------------------------------------------------------
def test_a_model_starter_with_invented_lines_is_replaced_by_the_code_built_excerpt():
    question = {"symbols": ["BaseScraper.parse"], "starter_code": "def parse(self, html):\n    return invented_call(html)"}
    starter, source = excerpt.starter_for(question, PY, "python")
    assert source == "code" and "invented_call" not in starter and starter.endswith("# TODO: implement")


def test_a_valid_model_starter_is_only_a_fallback_when_nothing_can_be_built():
    own = "import os\n# a comment\n// TODO: add your code here\n\nTOKEN_NAME = 1"
    text = "import os\nTOKEN_NAME = 1\n"
    assert excerpt.model_starter_is_real(own, text, "python")
    assert not excerpt.model_starter_is_real("import os\nsecret_call()", text, "python")
    assert excerpt.starter_for({"symbols": ["zzz"], "starter_code": "import os"}, text, "python") == ("import os", "model")
    assert excerpt.starter_for({"symbols": ["zzz"], "starter_code": "invented()"}, text, "python") == ("", "none")
    assert excerpt.starter_for({"symbols": ["zzz"], "starter_code": ""}, text, "python") == ("", "none")
    assert excerpt.starter_for({"symbols": ["x"], "starter_code": None}, "package main", "go") == ("", "none")
    assert excerpt.starter_for({"symbols": ["zzz"], "starter_code": "import os\nx = 'a@b.co'"}, "import os\nx = 'a@b.co'\n",
                               "python") == ("", "none")  # real lines, but unsafe: not used


def test_an_excerpt_failure_never_fails_a_question(monkeypatch):
    def boom(*args):
        raise RuntimeError("cannot cut")

    monkeypatch.setattr(excerpt, "build_excerpt", boom)
    assert excerpt.starter_for({"symbols": ["BaseScraper.parse"], "starter_code": ""}, PY, "python") == ("", "none")


def test_the_placeholder_table_is_closed():
    assert set(config.STARTER_PLACEHOLDERS) == {"implement", "add"}
    for table in config.STARTER_PLACEHOLDERS.values():
        assert set(table) == {"python", "javascript", "typescript", "java"}
        assert all(re.fullmatch(r"(#|//) TODO: (implement|add your code here)", line) for line in table.values())
    assert set(config.STARTER_SOURCES) == {"code", "model", "none"}


# --------------------------------------------------------------------------
# in a run: counts, the log line, fix and SQL questions untouched, replacements
# --------------------------------------------------------------------------
SKILLS = ["Python", "JavaScript", "Java", "Go"]
JAVA_BIG = JAVA.replace("    private List<Integer> ids;\n", "    private List<Integer> ids;\n" + "".join(
    f"    public int twice{i}(int v) {{\n        if (v > {i}) {{\n            return v * 2;\n        }}\n        return v;\n    }}\n"
    for i in range(4)))
FILES = {"scraper.py": PY, "search.js": JS, "Orders.java": JAVA_BIG,
         "main.go": "package main\n\nfunc main() {\n" + "\n".join(f"\tprintln({i})" for i in range(20)) + "\n}\n"}


def run(symbols_by_path, kinds=None, files_map=None, writer_answers=None, skills=SKILLS):
    files_map = files_map or FILES
    trees = {"demo": [tree_entry(path, 1500) for path in files_map]}
    contents = {("demo", path): text for path, text in files_map.items()}
    github = github_result([repo_record("demo", languages={"Python": 9000, "JavaScript": 4000, "Java": 3000, "Go": 2000})],
                           [tech_item(s, ["demo"]) for s in skills])
    prepared = service.prepare([skill_row(s) for s in skills], github, FakeFiles(trees, contents), Signals())
    answers = writer_answers or {path: good_answer(path, symbols_by_path[path]) for path in files_map}
    return prepared, service.write_all(prepared, FakeWriter(answers))


def test_a_run_builds_starters_counts_them_and_logs_ids_and_numbers_only(caplog):
    caplog.set_level(logging.DEBUG)
    prepared, (result, state) = run({"scraper.py": ["BaseScraper.parse"], "search.js": ["searchInput"],
                                     "Orders.java": ["count"], "main.go": ["main"]})
    by_path = {q["why"]["path"]: q for q in result["questions"]}
    assert by_path["scraper.py"]["starter_code"].endswith("        # TODO: implement")
    assert by_path["search.js"]["starter_code"].endswith("// TODO: add your code here")
    assert by_path["Orders.java"]["starter_code"].splitlines()[-1] == "}"
    assert by_path["main.go"]["starter_code"] == ""  # unsupported: empty, no failure, no dropped question
    assert result["counts"]["starter_built"] == 3 == state.starter_built
    assert result["counts"]["starter_empty"] == 1 == state.starter_empty and result["counts"]["dropped"] == 0
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("starter ")]
    assert len(lines) == 4 and all(re.fullmatch(r"starter question=q_\w+ lines=\d+ source=(code|model|none)", l)
                                   for l in lines)
    assert sorted(l.split("source=")[1] for l in lines) == ["code", "code", "code", "none"]
    for text in ("BeautifulSoup", "addEventListener", "println", "TODO"):
        assert text not in caplog.text


def test_a_new_target_is_not_in_the_excerpt_and_a_secret_line_nearby_is_left_out():
    flask = ("from flask import Flask\n\napp = Flask(__name__)\nSECRET_KEY = 'abcd1234efgh5678abcd'\n\n\n"
             "@app.route('/items')\ndef items():\n    return list_items()\n\n\ndef list_items():\n    return [1, 2]\n")
    out = build(flask, "python", "app")
    assert "delete_item" not in out and "abcd1234" not in out and "SECRET_KEY" not in out
    assert out.splitlines()[0] == "from flask import Flask"


def test_fix_and_sql_questions_are_byte_for_byte_unchanged(monkeypatch):
    fix = good_answer("scraper.py", ["BaseScraper.parse"], kind="fix_code", starter_code="x = 1\n",
                      failing_example={"input": "f()", "expected": "1", "actual": "2"})
    calls = []
    monkeypatch.setattr(excerpt, "starter_for", lambda *a: calls.append(a) or ("CHANGED", "code"))
    _, (result, state) = run({}, files_map={"scraper.py": PY}, writer_answers={"scraper.py": fix})
    question = result["questions"][0]
    assert question["kind"] == "fix_code" and question["starter_code"] == "x = 1" and calls == []
    assert state.starter_built == 0 == state.starter_empty and result["counts"]["starter_built"] == 0


def test_replacement_questions_get_excerpts_too():
    other = PY.replace("BaseScraper", "OtherScraper")
    prepared, (result, state) = run({"a.py": ["BaseScraper.parse"], "b.py": ["OtherScraper.parse"]},
                                    files_map={"a.py": PY, "b.py": other}, skills=["Python"])
    assert len(result["questions"]) == 1 and prepared.selection.reserve
    answers = {"b.py": good_answer("b.py", ["OtherScraper.parse"]), "a.py": good_answer("a.py", ["BaseScraper.parse"])}
    question, _ = service.replace_one(prepared, state, FakeWriter(answers))
    assert question["why"]["path"] in ("a.py", "b.py") and question["kind"] == "write_code"
    assert question["starter_code"].endswith("        # TODO: implement")


def test_the_counts_survive_a_resume():
    result = {"counts": {"starter_built": 3, "starter_empty": 2}, "questions": [], "dropped": []}
    state = service.state_from_result(result)
    assert (state.starter_built, state.starter_empty) == (3, 2)
    assert service.state_from_result({"questions": []}).starter_built == 0


def test_the_writer_version_was_raised_so_old_saved_sets_are_not_reused():
    assert config.WRITER_SETTINGS_VERSION >= 3
    from api.questions import QuestionManager
    result = {"github": {"username": "someone"}, "skills": [], "score": {"repo_fingerprint": "x"}}
    manager = QuestionManager.__new__(QuestionManager)
    now = manager._fingerprint(result)
    old = config.WRITER_SETTINGS_VERSION
    try:
        config.WRITER_SETTINGS_VERSION = 2
        assert manager._fingerprint(result) != now
    finally:
        config.WRITER_SETTINGS_VERSION = old


def test_the_scanner_handles_text_blocks_escapes_and_stray_brackets():
    block = 'class A {\n  String s = """\n    { not code\n    """;\n  void f() {\n    int x = 1;\n  }\n}\n'
    assert build(block, "java", "f").splitlines()[-4:] == ["  void f() {", "    // TODO: implement", "  }", "}"]
    assert build('class A {\n  String s = """\n  never closed\n  void f() {\n  }\n}\n', "java", "f") is None
    escaped = 'function f() {\n  const s = "a \\" } b";\n  return s;\n}\n'
    assert build(escaped, "javascript", "f").splitlines() == ["function f() {", "  // TODO: implement", "}"]
    assert build("function f() {\n  return 1;\n}\n}\n", "javascript", "f") is None  # a stray closing bracket


def test_a_definition_without_a_block_is_never_shown():
    assert build("const f = (x);\nconst other = 1;\n", "javascript", "f") is None
    assert build("const f = (x)\n", "javascript", "f") is None


def test_classes_whose_header_cannot_be_used_give_nothing_or_a_smaller_block():
    text = "class A { // see https://example.com\n  void f() {\n    int x = 1;\n  }\n}\n"
    assert build(text, "java", "f") is None
    big = ("class A { // see https://example.com\n" + "".join(f"  int v{i} = {i};\n" for i in range(45))
           + "  void g() {\n    int marker = 1;\n  }\n}\n")
    assert build(big, "java", "marker") is None  # the class is too big and its header is unsafe: nothing
    assert build("  .marker()\n", "javascript", "marker") is None  # only a continuation line: no statement start
    py_head = "class A:  # trailing comment\n    def f(self):\n        return 1\n\n    marker = 2\n"
    assert build(py_head, "python", "A.f") is None
    py_big = ("class A:  # trailing comment\n" + "".join(f"    v{i} = {i}\n" for i in range(45))
              + "    def g(self):\n        marker = 1\n        return marker\n")
    assert build(py_big, "python", "marker") is None  # too big as a whole, and the member's class header is unusable
