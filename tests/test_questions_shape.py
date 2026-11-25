"""Shape detail, safe normalising, one task per question and the repair hints. Fake writer only, offline."""
import json
import logging

import pytest
from test_questions_support import PY_ORDERS, FakeWriter

from skillverify.jd.provider import LLMCallError
from skillverify_questions import config, select, service, writer

CANDIDATE = select.FileCandidate("orders-analysis", "analysis.py", "python", PY_ORDERS, 50, 3, None)
TREE = {"analysis.py"}
QUESTION = ("Look at clean_orders in analysis.py. A reviewer says rows with a missing price slip through. "
            "Change the function so those rows are dropped, and explain how you would test it.")
GOOD = {"kind": "write_code", "question": QUESTION, "starter_code": "", "file": "analysis.py",
        "symbols": ["clean_orders"], "reference_notes": "Drop rows with a missing price."}
SECRET = "SECRET-MODEL-TEXT-42"


def without(*names, **changes):
    answer = {k: v for k, v in GOOD.items() if k not in names}
    answer.update(changes)
    return answer


def check(answer):
    return writer.check(answer, CANDIDATE, TREE, set())


def write(*answers):
    provider = FakeWriter({"analysis.py": list(answers) if len(answers) > 1 else answers[0]})
    return writer.write_question(provider, CANDIDATE, "Python", TREE, set()), provider


# --------------------------------------------------------------------------
# Fix 1: which field made an answer wrong_shape
# --------------------------------------------------------------------------
@pytest.mark.parametrize("field,value", [
    ("question", 5), ("starter_code", 7), ("file", ["x"]), ("reference_notes", {"a": 1}), ("symbols", "clean_orders"),
    ("symbols", [1]), ("kind", "poem"), ("kind", None),
])
def test_each_field_failing_alone_is_named(field, value):
    checked = check({**GOOD, field: value})
    assert checked.shape_fields == [field] and checked.question is None
    assert set(checked.shape_fields) <= set(config.SHAPE_FIELDS)


def test_a_missing_field_is_named_and_several_together_come_in_the_closed_order():
    assert check(without("reference_notes")).shape_fields == ["reference_notes"]
    several = check({**GOOD, "reference_notes": None, "kind": "x", "symbols": "a", "question": 1})
    assert several.shape_fields == ["kind", "question", "symbols", "reference_notes"]
    assert several.problems == [writer.WRONG_TYPES]


def test_too_many_symbols_names_symbols_and_a_good_answer_names_nothing():
    many = check({**GOOD, "symbols": [f"s{i}" for i in range(config.MAX_SYMBOLS + 1)]})
    assert "symbols" in many.shape_fields
    ok = check(GOOD)
    assert ok.question is not None and ok.shape_fields == [] and ok.example_fields == []
    assert check(["not", "a", "dict"]).shape_fields == []  # no field to name


def test_a_wrong_kind_with_other_problems_keeps_its_name_and_other_checks_name_no_field():
    both = check({**GOOD, "kind": "poem", "file": "other.py"})
    assert both.shape_fields == ["kind"] and set(both.problems) == {writer.KIND_INVALID, writer.FILE_MISMATCH}
    assert check({**GOOD, "file": "other.py"}).shape_fields == []


def test_the_dropped_entry_and_the_log_line_hold_names_only(caplog):
    caplog.set_level(logging.DEBUG)
    bad = without("reference_notes", question=SECRET)
    bad["symbols"] = [SECRET, 3]
    selection = select.Selection(picks=[select.Pick(select.Need("Python", "must_have", True), CANDIDATE, True)])
    prepared = service.Prepared(selection, {"orders-analysis": TREE}, {"files_ranked": 1})
    result, _ = service.write_all(prepared, FakeWriter({"analysis.py": [bad, without("question", reference_notes=SECRET)]}))
    drop = result["dropped"][0]
    assert drop["shape_fields"] == ["question", "reference_notes"] or drop["shape_fields"][0] in config.SHAPE_FIELDS
    assert set(drop["shape_fields"]) <= set(config.SHAPE_FIELDS) and drop["causes"] == ["wrong_shape"]
    assert SECRET not in json.dumps(result) and SECRET not in caplog.text
    line = [r.getMessage() for r in caplog.records if r.name == "skillverify.questions"][-1]
    assert line.startswith("question dropped skill=Python reasons=bad_json causes=wrong_shape shape_fields=")
    assert set(line.split("shape_fields=")[1].split(",")) <= set(config.SHAPE_FIELDS)


def test_a_drop_that_is_not_wrong_shape_has_no_shape_fields():
    written, _ = write({**GOOD, "file": "elsewhere.py"})
    assert written.shape_fields == [] and written.causes == []


# --------------------------------------------------------------------------
# Fix 2: normalising
# --------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["write_code", "Write code", "WRITE-CODE", "  write   code ", "code", "Code"])
def test_write_code_spellings(value):
    assert writer.normalise_kind(value) == "write_code"


@pytest.mark.parametrize("value", ["fix_code", "Fix Code", "fix-code", "fix the bug", "Fix The Bug", "bug fix", "fix"])
def test_fix_code_spellings(value):
    assert writer.normalise_kind(value) == "fix_code"


def test_other_kinds_are_not_guessed():
    for value in ("poem", "", "write codes", "fixing", 5, None):
        assert writer.normalise_kind(value) is None


@pytest.mark.parametrize("value,expected", [("js", "javascript"), ("JS", "javascript"), (" Ts ", "typescript"),
                                            ("py", "python"), ("node", "javascript"), ("NodeJS", "javascript")])
def test_language_aliases(value, expected):
    assert writer.normalise_language(value) == expected


def test_an_unknown_language_stays_as_given():
    assert writer.normalise_language("Rust") is None and writer.normalise_language(None) is None
    out, fired = writer.normalise_answer({**GOOD, "language": "Rust"})
    assert out["language"] == "Rust" and "language_normalised" not in fired


def test_the_normalised_answer_is_accepted_and_the_codes_are_recorded():
    written, provider = write({**GOOD, "kind": "Write code", "language": "JS"})
    assert written.question is not None and written.calls == 1
    assert written.normalised == ["kind_normalised", "language_normalised"]
    assert written.question["kind"] == "write_code" and written.question["language"] == "python"  # code decides


def test_starter_code_null_missing_and_list():
    for answer in ({**GOOD, "starter_code": None}, without("starter_code")):
        out, fired = writer.normalise_answer(answer)
        assert out["starter_code"] == "" and fired == ["starter_code_defaulted"]
    out, fired = writer.normalise_answer({**GOOD, "starter_code": ["a = 1", "b = 2"]})
    assert out["starter_code"] == "a = 1\nb = 2" and fired == ["starter_code_joined"]
    out, fired = writer.normalise_answer({**GOOD, "starter_code": ["a", 2]})
    assert out["starter_code"] == ["a", 2] and fired == []  # not all strings: left for the shape check
    assert write({**GOOD, "starter_code": None})[0].question is not None


@pytest.mark.parametrize("alias,canonical", [("question_text", "question"), ("prompt", "question"),
                                             ("starter", "starter_code"), ("code", "starter_code"),
                                             ("notes", "reference_notes"), ("solution_notes", "reference_notes")])
def test_each_alias_works(alias, canonical):
    answer = without(canonical)
    answer[alias] = GOOD[canonical]
    out, fired = writer.normalise_answer(answer)
    assert out[canonical] == GOOD[canonical] and fired == [f"alias_{canonical}"]
    assert write(answer)[0].question is not None


def test_an_alias_never_overrides_a_present_canonical_field_and_the_first_alias_wins():
    out, fired = writer.normalise_answer({**GOOD, "question_text": "other", "prompt": "another"})
    assert out["question"] == QUESTION and fired == []
    out, _ = writer.normalise_answer(without("question", question_text="first", prompt="second"))
    assert out["question"] == "first"
    out, _ = writer.normalise_answer({**GOOD, "starter_code": None, "code": "x = 1"})
    assert out["starter_code"] == ""  # a null canonical is present: the alias does not replace it


def test_whitespace_is_trimmed_on_the_string_fields_but_starter_indentation_is_kept():
    out, fired = writer.normalise_answer({**GOOD, "question": f"  {QUESTION}\n", "file": " analysis.py ",
                                          "reference_notes": " notes ", "starter_code": "  x = 1"})
    assert out["question"] == QUESTION and out["file"] == "analysis.py" and out["reference_notes"] == "notes"
    assert out["starter_code"] == "  x = 1" and fired == ["whitespace_trimmed"]


def test_nothing_is_invented_a_missing_question_or_notes_still_fails_with_its_field():
    for name in ("question", "reference_notes"):
        written, _ = write(without(name), without(name))
        assert written.question is None and written.reasons == ["bad_json"] and written.shape_fields == [name]
    written, _ = write({"text": "A plain sentence that is not an answer"}, {"text": "still not"})
    assert written.question is None and written.shape_fields[0] in config.SHAPE_FIELDS


def test_normalised_answers_still_meet_every_strict_check():
    invented = {**GOOD, "kind": "Write Code", "symbols": ["no_such_function"]}
    assert write(invented, invented)[0].reasons == ["unknown_symbol"]
    url = {**GOOD, "kind": "code", "reference_notes": "See https://example.com/x for the idea."}
    assert write(url, url)[0].reasons == ["url_or_email"]
    short = {**GOOD, "kind": "fix", "question": "Too short.", "starter_code": None}
    reasons = write(short, short)[0].reasons
    assert "bad_length" in reasons and "fix_without_starter" in reasons
    call = {**GOOD, "kind": "code", "question": QUESTION + " Also call frobnicate(x) first."}
    assert write(call, call)[0].reasons == ["unknown_call"]


def test_recorded_codes_hold_no_content(caplog):
    caplog.set_level(logging.DEBUG)
    answer = {**GOOD, "kind": "Write code", "language": "JS", "starter_code": None, "reference_notes": f" {SECRET} "}
    selection = select.Selection(picks=[select.Pick(select.Need("Python", "must_have", True), CANDIDATE, True)])
    prepared = service.Prepared(selection, {"orders-analysis": TREE}, {"files_ranked": 1})
    result, state = service.write_all(prepared, FakeWriter({"analysis.py": answer}))
    assert len(result["questions"]) == 1 and result["counts"]["normalised"] == 4 == state.normalised
    written = writer.write_question(FakeWriter({"analysis.py": answer}), CANDIDATE, "Python", TREE, set())
    assert set(written.normalised) <= set(config.NORMALISE_CODES)
    lines = [r.getMessage() for r in caplog.records if r.name == "skillverify.questions"
             and not r.getMessage().startswith("starter question=")]
    assert lines == ["answer normalised skill=Python codes=starter_code_defaulted,kind_normalised,"
                     "language_normalised,whitespace_trimmed"]
    assert SECRET not in caplog.text


def test_the_normalised_count_survives_a_resume():
    result = {"counts": {"normalised": 5}, "questions": [], "dropped": []}
    assert service.state_from_result(result).normalised == 5
    assert service.state_from_result({"questions": []}).normalised == 0


# --------------------------------------------------------------------------
# Fix 3: one task per question
# --------------------------------------------------------------------------
@pytest.mark.parametrize("sentence", [
    "Implement parse_orders. Alternatively, write a small class that wraps it.",
    "Fix the bug in clean_orders, or alternatively rewrite it from scratch.",
    "Fix clean_orders or, alternatively, add a new helper.",
    "Write the function as an alternative to the loop.",
    "Update clean_orders or else write a new loader.",
    "Update clean_orders or instead write a new loader.",
    "Either write a new loader or modify clean_orders so it skips bad rows.",
    "Either add a column check or implement a filter in clean_orders.",
])
def test_offered_alternatives_are_rejected(sentence):
    assert writer.offers_alternatives(sentence)
    question = f"Look at clean_orders in analysis.py. {sentence} Explain how you would test the change you chose."
    checked = check({**GOOD, "question": question})
    assert writer.MULTIPLE_TASKS in checked.problems


@pytest.mark.parametrize("sentence", [
    "Return true or false depending on whether the row is valid.",
    "Treat a value that is null or undefined as missing.",
    "Say whether the total is positive or negative.",
    "Explain why a row is dropped when the price is zero or missing.",
    "Accept a string or a number as the quantity.",
    "Either way the function must keep the row order.",
    "Return True or False.",
])
def test_ordinary_sentences_are_not_rejected(sentence):
    assert not writer.offers_alternatives(sentence)
    question = f"Look at clean_orders in analysis.py. {sentence} Change the function and explain how you would test it."
    assert writer.MULTIPLE_TASKS not in check({**GOOD, "question": question}).problems


def test_the_writer_prompt_has_the_one_task_rule():
    text = writer.SYSTEM_PROMPT
    assert "exactly ONE task" in text and "never offer alternatives" in text.lower() and '"alternatively"' in text


def test_a_multiple_task_question_goes_through_repair_and_drop_with_its_own_reason():
    many = {**GOOD, "question": QUESTION + " Alternatively, write a new loader."}
    written, provider = write(many, many)
    assert written.question is None and written.calls == 2 and written.reasons == ["multiple_tasks"]
    assert "multiple_tasks" in config.DROP_REASON_CODES and len(config.DROP_REASON_CODES) == 18
    repair = provider.prompts[1][1]
    assert "- multiple_tasks: ask for exactly one task, with no alternatives" in repair
    fixed, _ = write(many, GOOD)
    assert fixed.question is not None and fixed.calls == 2


def test_the_writer_settings_version_was_raised_because_the_checks_changed():
    assert config.WRITER_SETTINGS_VERSION >= 2  # raised again when the starter excerpts were added


# --------------------------------------------------------------------------
# Fix 4: the repair hint
# --------------------------------------------------------------------------
def test_every_check_has_a_fixed_sentence_and_the_repair_lists_each():
    assert set(writer.PROBLEM_TEXT) == set(writer.REASON_CODE)
    for name, text in writer.PROBLEM_TEXT.items():
        prompt = writer.build_prompt(CANDIDATE, "Python", [name])
        assert f"- {name}: {text}" in prompt and "failed these checks" in prompt


@pytest.mark.parametrize("name,expected", [
    (writer.FIX_NOT_MODIFIED, "the starter code must differ from the file: change at least one line"),
    (writer.QUESTION_LENGTH, "keep the question between the allowed lengths"),
    (writer.STARTER_TOO_LONG, "keep starter_code within the allowed length"),
    (writer.NOTES_TOO_LONG, "keep reference_notes within the allowed length"),
    (writer.MULTIPLE_TASKS, "ask for exactly one task, with no alternatives"),
    (writer.SYMBOL_NOT_IN_FILE, "Only name things that appear in the file; do not include URLs or e-mail addresses"),
    (writer.FUNCTION_NOT_IN_FILE, "Only name things that appear in the file; do not include URLs or e-mail addresses"),
    (writer.UNKNOWN_FILE, "Only name things that appear in the file; do not include URLs or e-mail addresses"),
    (writer.URL_OR_EMAIL, "Only name things that appear in the file; do not include URLs or e-mail addresses"),
])
def test_the_hint_sentences(name, expected):
    assert expected in writer.PROBLEM_TEXT[name]


def test_wrong_shape_lists_the_field_names_in_the_repair():
    bad = without("reference_notes", symbols="x")
    written, provider = write(bad, GOOD)
    assert written.question is not None
    repair = provider.prompts[1][1]
    assert "- wrong_types: a field was missing or had the wrong type (fields: symbols, reference_notes)" in repair
    assert provider.prompts[0][1] == writer.build_prompt(CANDIDATE, "Python")  # the first call has no hint


def test_a_wrong_kind_is_named_in_the_repair_too():
    written, provider = write({**GOOD, "kind": "poem"}, GOOD)
    assert written.question is not None and "(fields: kind)" in provider.prompts[1][1]


def test_the_failing_example_parts_are_named_in_the_repair():
    fix = {**GOOD, "kind": "fix_code", "starter_code": "x = 1",
           "failing_example": {"input": "f()", "expected": 3, "actual": ""}}
    written, provider = write(fix, GOOD)
    assert "(fields: failing_example.expected, failing_example.actual)" in provider.prompts[1][1]
    missing = {**fix, "failing_example": None}
    _, provider = write(missing, GOOD)
    assert "(fields: failing_example.input, failing_example.expected, failing_example.actual)" in provider.prompts[1][1]
    assert set(config.FAILING_EXAMPLE_FIELDS) == {"failing_example.input", "failing_example.expected",
                                                  "failing_example.actual"}


def test_the_repair_never_echoes_the_model_text():
    leaky = {**GOOD, "question": SECRET, "reference_notes": SECRET, "symbols": [SECRET]}
    _, provider = write(leaky, leaky)
    assert SECRET not in provider.prompts[1][1] and SECRET not in provider.prompts[0][1]
    error = LLMCallError("Groq's answer was not valid JSON. " + SECRET)
    _, provider = write(error, error)
    assert SECRET not in provider.prompts[1][1] and "- invalid_json:" in provider.prompts[1][1]


def test_the_repair_is_still_the_single_attempt():
    many = {**GOOD, "question": QUESTION + " Alternatively, do something else."}
    written, provider = write(many, many, GOOD)
    assert written.calls == 2 and provider.calls == 2 and written.question is None
