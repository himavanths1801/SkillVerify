"""No bug-hint comments in fix-the-bug starter code: the comment scanner, the stripping and the new drop reason.
Fake files and a fake writer only, offline."""
import logging

import pytest
from test_questions_support import FakeWriter

from skillverify_questions import comments, config, select, service, writer

SECRET_WORDS = "SECRET-COMMENT-TEXT"


def strip(starter, file_text, language):
    out = comments.strip_added_comments(starter, file_text, language)
    out.text = out.text.strip("\n")  # the trailing line break is not what these tests are about
    return out


# --------------------------------------------------------------------------
# the scanner and the removal
# --------------------------------------------------------------------------
def test_python_whole_line_trailing_and_kept_comments():
    file = "# the real header comment\ndef total(items):\n    count = len(items)  # real trailing\n    return count\n"
    starter = ("# the real header comment\ndef total(items):\n    # BUG: off by one\n"
               "    count = len(items) + 1  # should be 0\n    return count\n")
    out = strip(starter, file, "python")
    assert out.text == "# the real header comment\ndef total(items):\n    count = len(items) + 1\n    return count"
    assert out.removed == 2 and out.hinted is False
    kept = strip(file, file, "python")
    assert kept.text == file.strip("\n") and kept.removed == 0  # a comment that exists in the real file stays


def test_python_hash_in_strings_and_docstrings_is_never_a_comment():
    file = 'def f():\n    """Docstring with # and // inside."""\n    s = "a # b"\n    t = \'c # d\'\n    return s + t\n'
    starter = file.replace("return s + t", "return s + t  # note")
    out = strip(starter, file, "python")
    assert out.text == file.strip("\n") and out.removed == 1
    multi = 'def f():\n    x = """\n    # not a comment\n    """\n    return x\n'
    assert strip(multi, multi, "python").text == multi.strip("\n") and strip(multi, multi, "python").removed == 0
    assert strip(multi + "    # added\n", multi, "python").removed == 1
    assert comments.remove_all(multi, "python") == multi.rstrip("\n")


def test_javascript_whole_line_trailing_block_and_multiline_block():
    file = "function f(a) {\n  const b = a + 1;\n  return b;\n}\n"
    starter = ("function f(a) {\n  // BUG: wrong\n  const b = a + 2; // should be 1\n  /* a block */ return b;\n"
               "  /* many\n     lines */\n}\n")
    out = strip(starter, file, "javascript")
    assert out.text == "function f(a) {\n  const b = a + 2;\n   return b;\n}" and out.removed == 4
    assert out.hinted is False


def test_jsx_and_html_comments_are_removed_with_their_braces():
    file = "const A = () => (\n  <div>\n    <p>hi</p>\n  </div>\n);\n"
    starter = ("const A = () => (\n  <div>\n    {/* BUG: wrong tag */}\n    <p>hi</p> {/* tail */}\n"
               "    <!-- html note -->\n  </div>\n);\n")
    out = strip(starter, file, "javascript")
    assert out.text == "const A = () => (\n  <div>\n    <p>hi</p>\n  </div>\n);" and out.removed == 3


def test_comment_markers_inside_strings_templates_and_regexes_are_left_alone():
    file = ('const a = "http://x.y/z // not";\nconst b = `line // one\n  and /* two */`;\n'
            "const c = /\\/\\//.test(a);\nconst d = /[/]x/.test(b);\nconst e = a / 2 / 3;\nconst f = \"it's\";\n")
    assert strip(file, file, "javascript").text == file.strip("\n")
    added = file.replace("const e = a / 2 / 3;", "const e = a / 2 / 3; // added")
    out = strip(added, file, "javascript")
    assert out.removed == 1 and "// added" not in out.text and "http://x.y/z // not" in out.text
    assert "line // one" in out.text and "/\\/\\//" in out.text and "[/]" in out.text
    assert strip("return x\n/ 2;\n", "return x\n/ 2;\n", "javascript").removed == 0
    assert strip("x = (a / b) // c\n", "x = (a / b)\n", "javascript").removed == 1  # a division, then a real comment
    assert strip("typeof /a\\/b/.source; // z\n", "typeof /a\\/b/.source;\n", "javascript").removed == 1
    assert strip("x = a\n// c\n", "x = a\n", "javascript").text == "x = a"


def test_typescript_java_and_c_like_files_use_slash_comments():
    ts = "export const f = (a: number): number => {\n  return a;\n};\n"
    assert strip(ts.replace("return a;", "return a; // BUG"), ts, "typescript").text == ts.rstrip("\n")
    java = "class A {\n  int f(int a) {\n    return a;\n  }\n}\n"
    out = strip(java.replace("return a;", "// wrong\n    return a + 1; /* x */"), java, "java")
    assert out.text == "class A {\n  int f(int a) {\n    return a + 1;\n  }\n}" and out.removed == 2
    go = 'package main\nfunc f() string {\n\treturn `raw // text`\n}\n'
    assert strip(go, go, "go").removed == 0 and strip(go + "// added\n", go, "go").removed == 1


def test_unsupported_languages_are_left_alone():
    shell = "echo hi # BUG\n"
    out = strip(shell, "echo hi\n", "shell")
    assert out.text == shell.strip("\n") and out.removed == 0 and out.hinted is False
    assert comments.remove_all(shell, "shell") == shell
    assert not comments.supported("shell") and comments.supported("python") and comments.supported("java")


def test_unclear_lines_are_left_alone_and_a_hint_word_there_is_flagged():
    file = "const a = <p>Don't</p>;\nconst b = 1;\n"
    # an apostrophe in JSX text: the line cannot be read safely, so nothing on it is removed ...
    plain = strip("const a = <p>Don't</p>; // note\nconst b = 1;\n", file, "javascript")
    assert plain.text == "const a = <p>Don't</p>; // note\nconst b = 1;" and plain.removed == 0 and not plain.hinted
    # ... unless an added comment on it has a hint word: then the question must be dropped
    assert strip("const a = <p>Don't</p>; // BUG: wrong text\nconst b = 1;\n", file, "javascript").hinted
    assert strip("const a = <p>Don't</p>; /* should be */\n", file, "javascript").hinted
    # the same line as in the real file is never flagged
    assert not strip(file, file, "javascript").hinted
    broken = strip("const r = /abc;  // typo here\n", "const r = /abc;\n", "javascript")  # a regex that does not close
    assert broken.hinted
    never = strip("/* never closed BUG\nconst a = 1;\n", "const a = 1;\n", "javascript")
    assert never.removed == 0 and never.hinted  # a block comment that never closes cannot be removed
    py = strip("s = 'open  # BUG: wrong\nx = 1\n", "s = 'open\nx = 1\n", "python")
    assert py.removed == 0 and py.hinted  # an unterminated string: the line is left alone, the hint word flags it
    assert not strip("s = 'open\nx = 1  # note\n", "s = 'open\nx = 1\n", "python").hinted


def test_the_hint_words_match_whole_words_only():
    for word in config.HINT_WORDS:
        text = f"const a = <p>Don't</p>; // {word.upper()}\n"
        assert strip(text, "const a = <p>Don't</p>;\n", "javascript").hinted, word
    assert not strip("const a = <p>Don't</p>; // debugging buggier\n", "const a = <p>Don't</p>;\n", "javascript").hinted


def test_remove_all_drops_every_comment_but_keeps_unclear_lines():
    text = "// a\nx = 1; // b\n/* c */\ny = <p>Don't</p>; // d\n"
    assert comments.remove_all(text, "javascript") == "x = 1;\ny = <p>Don't</p>; // d"


# --------------------------------------------------------------------------
# in the writer: the fix checks
# --------------------------------------------------------------------------
JSX = '''import React, { useState } from "react";

export function Chat({ socket }) {
  const [text, setText] = useState("");

  function handleSend() {
    if (!text) {
      return;
    }
    socket.emit("message", text);
    setText("");
  }

  return <button onClick={handleSend}>Send</button>;
}
'''
CAND = select.FileCandidate("chat", "Chat.jsx", "javascript", JSX, 60, 3, None)
QUESTION = ("Look at handleSend in Chat.jsx. Sending a message does not reach the other users. Find the bug in the "
            "starter code and fix it, then explain how you would test the change.")
EXAMPLE = {"input": "handleSend()", "expected": "emits message", "actual": "emits mesage"}
STARTER = JSX.split("\n\n  return")[0].split("export function")[1]
STARTER = "export function" + STARTER


def fix_answer(starter, **overrides):
    base = {"kind": "fix_code", "question": QUESTION, "starter_code": starter, "file": "Chat.jsx",
            "symbols": ["handleSend"], "reference_notes": "The event name is misspelled.", "failing_example": EXAMPLE}
    base.update(overrides)
    return base


BUGGY = STARTER.replace('socket.emit("message", text);', 'socket.emit("mesage", text);')


def check(answer, candidate=CAND):
    return writer.check(answer, candidate, {"Chat.jsx"}, set())


def test_a_real_code_change_plus_a_hint_comment_keeps_the_question_with_the_comment_removed():
    hinted = BUGGY.replace('socket.emit("mesage", text);', 'socket.emit("mesage", text); // BUG: wrong event name emitted')
    checked = check(fix_answer(hinted))
    assert checked.problems == [] and checked.hints_stripped == 1
    starter = checked.question["starter_code"]
    assert "BUG" not in starter and 'socket.emit("mesage", text);' in starter  # the bug itself is intact
    assert starter == BUGGY.strip("\n")
    whole_line = BUGGY.replace("    if (!text) {", "    // BUG: wrong event name emitted\n    if (!text) {")
    assert check(fix_answer(whole_line)).question["starter_code"] == BUGGY.strip("\n")


def test_comments_that_exist_in_the_real_file_are_kept():
    file = JSX.replace("    setText(\"\");", "    setText(\"\"); // clear the box")
    candidate = select.FileCandidate("chat", "Chat.jsx", "javascript", file, 60, 3, None)
    starter = BUGGY.replace('    setText("");', '    setText(""); // clear the box')
    checked = check(fix_answer(starter), candidate)
    assert checked.problems == [] and "// clear the box" in checked.question["starter_code"] and checked.hints_stripped == 0


def test_a_starter_that_differs_from_the_file_only_by_a_comment_is_not_modified():
    only_comment = STARTER.replace('socket.emit("message", text);', 'socket.emit("message", text); // BUG: here')
    assert writer.FIX_NOT_MODIFIED in check(fix_answer(only_comment)).problems
    removed_comment = JSX.replace("    setText(\"\");", "    setText(\"\"); // clear the box")
    candidate = select.FileCandidate("chat", "Chat.jsx", "javascript", removed_comment, 60, 3, None)
    assert writer.FIX_NOT_MODIFIED in check(fix_answer(STARTER), candidate).problems
    assert check(fix_answer(BUGGY), candidate).problems == []  # a real change is still accepted


def test_an_unclear_line_with_an_added_hint_word_drops_the_question_as_bug_hint_in_starter():
    text = JSX.replace('<button onClick={handleSend}>Send</button>', "<button onClick={handleSend}>Don't send</button>")
    candidate = select.FileCandidate("chat", "Chat.jsx", "javascript", text, 60, 3, None)
    unclear = BUGGY + "\n  const label = <i>Don't</i>; // BUG: wrong label"
    checked = check(fix_answer(unclear), candidate)
    assert writer.BUG_HINT in checked.problems and checked.question is None
    harmless = BUGGY + "\n  const label = <i>Don't</i>; // plain note"
    assert writer.BUG_HINT not in check(fix_answer(harmless), candidate).problems  # no hint word: left as is
    provider = FakeWriter({"Chat.jsx": [fix_answer(unclear), fix_answer(unclear)]})
    written = writer.write_question(provider, candidate, "React", {"Chat.jsx"}, set())
    assert written.question is None and written.reasons == ["bug_hint_in_starter"] and written.calls == 2
    assert "- bug_hint_in_starter: remove any comment from the starter code that explains or points at the bug" in provider.prompts[1][1]


def test_the_writer_prompt_and_the_closed_tables():
    assert "Add or change NO\n  comments" in writer.SYSTEM_PROMPT and "FIXME or TODO" in writer.SYSTEM_PROMPT
    assert "reference_notes" in writer.SYSTEM_PROMPT.split("FIXME or TODO")[1][:200]
    assert writer.REASON_CODE[writer.BUG_HINT] == "bug_hint_in_starter" in config.DROP_REASON_CODES
    assert len(config.DROP_REASON_CODES) == 18 and writer.BUG_HINT in writer.PROBLEM_TEXT
    assert config.WRITER_SETTINGS_VERSION >= 4


def test_write_code_and_sql_questions_are_unchanged_byte_for_byte():
    write = {"kind": "write_code", "question": QUESTION.replace("Find the bug in the starter code and fix it",
                                                                 "Add a retry to handleSend"),
             "starter_code": "// BUG: kept exactly as the model wrote it\nlet a = 1;", "file": "Chat.jsx",
             "symbols": ["handleSend"], "reference_notes": "Notes."}
    checked = check(write)
    assert checked.question["starter_code"] == "// BUG: kept exactly as the model wrote it\nlet a = 1;"
    assert checked.hints_stripped == 0 and checked.problems == []
    sql = {**write, "starter_code": "SELECT 1 -- BUG here"}
    assert check(sql).question["starter_code"] == "SELECT 1 -- BUG here"


# --------------------------------------------------------------------------
# counts, logs, resume, version
# --------------------------------------------------------------------------
def run_fix(starters, caplog=None):
    selection = select.Selection(picks=[select.Pick(select.Need("React", "must_have", True), CAND, True)])
    prepared = service.Prepared(selection, {"chat": {"Chat.jsx"}}, {"files_ranked": 1})
    answer = fix_answer(starters)
    return service.write_all(prepared, FakeWriter({"Chat.jsx": answer}))


def test_the_count_and_the_log_line_hold_numbers_only(caplog):
    caplog.set_level(logging.DEBUG)
    hinted = BUGGY.replace('socket.emit("mesage", text);',
                           f'socket.emit("mesage", text); // BUG {SECRET_WORDS}').replace(
        "    if (!text) {", f"    // wrong {SECRET_WORDS}\n    if (!text) {{")
    result, state = run_fix(hinted)
    assert len(result["questions"]) == 1 and result["counts"]["hints_stripped"] == 2 == state.hints_stripped
    assert SECRET_WORDS not in repr(result) and SECRET_WORDS not in caplog.text and "BUG" not in caplog.text
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("starter ")]
    assert len(lines) == 1 and lines[0].endswith("hints_stripped=2") and lines[0].startswith("starter question=q_")
    clean, _ = run_fix(BUGGY)
    assert clean["counts"]["hints_stripped"] == 0


def test_the_count_survives_a_resume_and_the_old_version_misses():
    state = service.state_from_result({"counts": {"hints_stripped": 4}, "questions": []})
    assert state.hints_stripped == 4 and service.state_from_result({"questions": []}).hints_stripped == 0
    from api.questions import QuestionManager
    result = {"github": {"username": "someone"}, "skills": [], "score": {"repo_fingerprint": "x"}}
    manager = QuestionManager.__new__(QuestionManager)
    now, old = manager._fingerprint(result), config.WRITER_SETTINGS_VERSION
    try:
        config.WRITER_SETTINGS_VERSION = 3
        assert manager._fingerprint(result) != now
    finally:
        config.WRITER_SETTINGS_VERSION = old


def test_escaped_quotes_do_not_end_a_string_early():
    file = 'def f():\n    return "say \\" # still a string"\n'
    assert strip(file, file, "python").removed == 0
    out = strip(file.replace("string\"\n", "string\"  # added\n"), file, "python")
    assert out.removed == 1 and "still a string" in out.text
