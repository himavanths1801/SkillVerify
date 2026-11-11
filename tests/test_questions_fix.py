"""Vendored folders are not the candidate's code, and fix questions need a concrete, checkable symptom. Offline."""
import logging

import pytest
from test_questions_support import (PY_ORDERS, FakeFiles, FakeWriter, github_result, good_answer, repo_record,
                                    skill_row, tech_item, tree_entry)

from skillverify_questions import config, files, select, service, writer
from skillverify_questions.signals import Signals

SIGNALS = Signals()


# --------------------------------------------------------------------------
# Tweak 1: vendored copies
# --------------------------------------------------------------------------
NEW_FOLDERS = ["libs", "external", "extern", "deps", "third-party", "thirdparty", "vendored"]


@pytest.mark.parametrize("folder", NEW_FOLDERS)
def test_the_new_vendored_folders_are_junk_at_any_depth_in_any_case(folder):
    assert files.skip_reason(f"{folder}/micropython/datetime.py", 1000) == files.JUNK_FOLDER
    assert files.skip_reason(f"app/{folder}/datetime.py", 1000) == files.JUNK_FOLDER
    assert files.skip_reason(f"app/{folder.upper()}/x/datetime.py", 1000) == files.JUNK_FOLDER


def test_plain_lib_and_names_that_only_contain_the_word_are_not_excluded():
    assert "lib" not in config.SKIP_FOLDERS
    for path in ("lib/dates.py", "src/lib/dates.py", "src/libs_helper/dates.py", "src/mylibs/dates.py",
                 "src/external_api/client.py", "deps.py"):
        assert files.skip_reason(path, 1000) is None, path


def test_the_vendored_file_is_excluded_before_any_content_is_fetched():
    trees = {"app": [tree_entry("libs/micropython/datetime.py", 1500), tree_entry("src/app/dates.py", 1200)]}
    contents = {("app", "libs/micropython/datetime.py"): PY_ORDERS, ("app", "src/app/dates.py"): PY_ORDERS}
    fake = FakeFiles(trees, contents)
    github = github_result([repo_record("app")], [tech_item("Python", ["app"])])
    prepared = service.prepare([skill_row("Python")], github, fake, SIGNALS)
    assert fake.fetched == [("app", "src/app/dates.py")]
    assert [p.file.path for p in prepared.selection.picks] == ["src/app/dates.py"]


# --------------------------------------------------------------------------
# Tweak 2: fix questions
# --------------------------------------------------------------------------
CANDIDATE = select.FileCandidate("orders-analysis", "analysis.py", "python", PY_ORDERS, 50, 3, None)
TREE = {"analysis.py"}
QUESTION = ("Look at clean_orders in analysis.py below. The starter code is meant to drop rows without a customer and "
            "still return a frame, but it misbehaves. Find the bug and fix it.")
STARTER = ('def clean_orders(frame):\n    frame = frame.dropna(subset=["customer_id"])\n    if frame.empty:\n'
           '        raise ValueError("no rows left")\n    return frame.head(0)')
EXAMPLE = {"input": "clean_orders(frame)", "expected": "3 rows", "actual": "0 rows"}


def answer(**overrides):
    base = {"kind": "fix_code", "question": QUESTION, "starter_code": STARTER, "file": "analysis.py",
            "symbols": ["clean_orders"], "reference_notes": "head(0) empties the frame.",
            "failing_example": dict(EXAMPLE)}
    base.update(overrides)
    return base


def check(**overrides):
    return writer.check(answer(**overrides), CANDIDATE, TREE, set())


def test_a_valid_fix_question_passes_and_gets_the_example_sentence_exactly_once():
    checked = check()
    assert checked.problems == []
    question = checked.question
    sentence = "Example: clean_orders(frame) should give 3 rows, but the starter code gives 0 rows."
    assert question["question"] == f"{QUESTION} {sentence}" and question["question"].count("Example:") == 1
    assert question["failing_example"] == EXAMPLE and question["kind"] == "fix_code"


def test_values_are_trimmed_and_a_write_question_never_gets_the_sentence_or_the_field():
    padded = check(failing_example={"input": "  f(1) ", "expected": " 2", "actual": "3  "}).question
    assert padded["failing_example"] == {"input": "f(1)", "expected": "2", "actual": "3"}
    write = writer.check(answer(kind="write_code", starter_code="", failing_example=dict(EXAMPLE)), CANDIDATE, TREE,
                         set())
    assert write.problems == [] and "failing_example" not in write.question
    assert "Example:" not in write.question["question"] and write.question["question"] == QUESTION
    for missing in ({}, {"failing_example": None}):
        data = {k: v for k, v in answer(kind="write_code", starter_code="").items() if k != "failing_example"}
        data.update(missing)
        assert writer.check(data, CANDIDATE, TREE, set()).question is not None


@pytest.mark.parametrize("example", [
    None, "3 rows", [], {}, {"input": "f()", "expected": "1"}, {"input": "f()", "expected": "1", "actual": 2},
    {"input": "", "expected": "1", "actual": "2"}, {"input": "f()", "expected": "  ", "actual": "2"},
    {"input": "x" * 121, "expected": "1", "actual": "2"}, {"input": "f()", "expected": "1\n2", "actual": "3"},
    {"input": "f()", "expected": "1", "actual": "3\r"},
])
def test_an_unusable_example_is_fix_without_example(example):
    assert check(failing_example=example).problems == [writer.FIX_NO_EXAMPLE]
    assert writer.REASON_CODE[writer.FIX_NO_EXAMPLE] == "fix_without_example"


def test_the_longest_allowed_value_passes():
    assert check(failing_example={"input": "x" * 120, "expected": "1", "actual": "2"}).problems == []


def test_an_example_where_expected_equals_actual_is_vacuous():
    assert check(failing_example={"input": "f()", "expected": " Yes ", "actual": "yes"}).problems == [writer.FIX_VACUOUS]
    assert writer.REASON_CODE[writer.FIX_VACUOUS] == "fix_example_vacuous"


def test_a_verbatim_excerpt_or_a_mostly_rewritten_starter_is_not_modified():
    excerpt = 'def clean_orders(frame):\n    frame = frame.dropna(subset=["customer_id"])\n\n    if frame.empty:\n'
    assert check(starter_code=excerpt).problems == [writer.FIX_NOT_MODIFIED]
    assert check(starter_code="  def clean_orders(frame):  \n      return frame.head()  ").problems == []  # one new line, half known
    rewritten = "def a():\n    x = 1\n    y = 2\n    return 3"
    assert check(starter_code=rewritten).problems == [writer.FIX_NOT_MODIFIED]
    assert writer.REASON_CODE[writer.FIX_NOT_MODIFIED] == "fix_not_modified"


def test_a_one_line_changed_statement_is_a_valid_starter():
    assert check(starter_code="frame = frame.dropna(subset=['order_id'])").problems == []
    assert check(starter_code='frame = frame.dropna(subset=["customer_id"])').problems == [writer.FIX_NOT_MODIFIED]


def test_a_blank_starter_is_only_starter_missing():
    assert check(starter_code="  \n").problems == [writer.STARTER_MISSING, writer.FIX_NO_EXAMPLE][:1]


def test_example_values_pass_the_url_and_secret_checks_too():
    assert check(failing_example={"input": "f('https://example.org')", "expected": "1", "actual": "2"}).problems == [
        writer.URL_OR_EMAIL]
    assert check(failing_example={"input": "f()", "expected": "a@b.org", "actual": "2"}).problems == [writer.URL_OR_EMAIL]
    secret = {"input": "f()", "expected": "ok", "actual": "password = 'hunter2hunter2'"}
    assert check(failing_example=secret).problems == [writer.SECRET_LIKE]


# -- repair and drop ------------------------------------------------------------------------------------------
BAD = {"fix_without_example": {"failing_example": None},
       "fix_example_vacuous": {"failing_example": {"input": "f()", "expected": "1", "actual": "1"}},
       "fix_not_modified": {"starter_code": 'def clean_orders(frame):\n    frame = frame.dropna(subset=["customer_id"])'}}


@pytest.mark.parametrize("code", sorted(BAD))
def test_the_repair_attempt_can_fix_each_failure_and_names_the_check(code):
    provider = FakeWriter({"analysis.py": [answer(**BAD[code]), answer()]})
    written = writer.write_question(provider, CANDIDATE, "Python", TREE, set())
    assert written.question is not None and written.calls == 2
    assert f"- {code}:" in provider.prompts[1][1] and f"- {code}:" not in provider.prompts[0][1]
    assert written.question["question"].count("Example:") == 1


@pytest.mark.parametrize("code", sorted(BAD))
def test_a_second_failure_drops_the_question_with_its_code(code):
    provider = FakeWriter({"analysis.py": [answer(**BAD[code])] * 2})
    written = writer.write_question(provider, CANDIDATE, "Python", TREE, set())
    assert written.question is None and written.reasons == [code]


def test_the_prompt_asks_for_a_real_behavioural_bug_and_an_example():
    prompt = " ".join(writer.SYSTEM_PROMPT.split())
    for phrase in ("failing_example", "observable behaviour", "never as a bug in the candidate's own code",
                   "exactly one", "no line breaks"):
        assert phrase in prompt
    assert set(config.DROP_REASON_CODES) >= {"fix_without_example", "fix_example_vacuous", "fix_not_modified"}


# -- through the service: the dropped entry, the object and the logs -------------------------------------------
def fix_run(answers):
    trees = {"app": [tree_entry("src/app/dates.py", 1500)]}
    fake = FakeFiles(trees, {("app", "src/app/dates.py"): PY_ORDERS})
    github = github_result([repo_record("app")], [tech_item("Python", ["app"])])
    prepared = service.prepare([skill_row("Python")], github, fake, SIGNALS)
    return service.write_all(prepared, FakeWriter({"src/app/dates.py": answers}), make_id=iter(["q_00000001"]).__next__)


def dates_answer(**overrides):
    return answer(file="src/app/dates.py", question=QUESTION.replace("analysis.py", "dates.py"), **overrides)


def test_a_valid_fix_question_has_the_object_and_a_dropped_one_has_its_code(caplog):
    caplog.set_level(logging.DEBUG)
    result, _ = fix_run([dates_answer()])
    question = result["questions"][0]
    assert question["failing_example"] == EXAMPLE and question["question"].endswith("but the starter code gives 0 rows.")
    assert result["dropped"] == []
    bad = dates_answer(failing_example={"input": "f()", "expected": "SECRET-X", "actual": "secret-x"})
    result, _ = fix_run([bad, bad])
    assert result["questions"] == []
    assert result["dropped"] == [{"skill": "Python", "tier": "must_have", "repo": "app", "path": "src/app/dates.py",
                                  "attempts": 2, "reasons": ["fix_example_vacuous"]}]
    assert "SECRET-X" not in repr(result) and "SECRET-X" not in caplog.text
    assert "question dropped skill=Python reasons=fix_example_vacuous" in caplog.text
    assert "head(0)" not in caplog.text and "customer_id" not in caplog.text


def test_write_questions_in_a_run_have_no_failing_example_key():
    result, _ = fix_run([dates_answer(kind="write_code", starter_code="")])
    assert "failing_example" not in result["questions"][0]
