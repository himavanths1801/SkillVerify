"""Whole-term matching in the JD parser: "Java" must not be found inside "JavaScript". Offline."""
import json

import pytest

from skillverify.jd.growth import contains_term, evaluate_proposal, squash
from skillverify.jd.parser import JDParser
from skillverify.jd.provider import LLMProvider
from skillverify.taxonomy_extra import load_extended_taxonomy


def found(haystack, term):
    """contains_term the way the code uses it: on squashed (whitespace-collapsed, lowercase) text."""
    return contains_term(squash(haystack), term)


# --------------------------------------------------------------------------
# contains_term
# --------------------------------------------------------------------------
@pytest.mark.parametrize("haystack, term", [
    ("JavaScript", "Java"), ("Google Cloud", "Go"), ("Required: Python", "R"), ("C++ and C#", "C"),
    ("C#", "C"), ("C++", "C"), ("Pythonic style", "Python"), ("Python3", "Python"), ("my_python", "Python"),
    ("ASP.NET", ".NET"), ("xnode.js", "Node.js"), ("nodexjs", "Node.js"), ("Cpp", "C++"),
    ("c++ is used", "C+"), ("MySQLdb", "SQL"), ("Rust and Ruby", "R"),
])
def test_glued_inside_another_word_is_not_a_match(haystack, term):
    assert found(haystack, term) is False


@pytest.mark.parametrize("haystack, term", [
    ("Python,", "Python"), ("(Python)", "Python"), ("python.", "Python"), ("Python/Django", "Python"),
    ("We use Node.js daily", "Node.js"), ("HTML/CSS and Git", "HTML/CSS"), ("knowledge of C++, Java", "C++"),
    ("Experience with Go (Golang).", "Go"), ("Python, R and SQL", "R"),
    ("Machine\n Learning models", "machine learning"), ("Machine   learning", "Machine Learning"),
    ("Python-based tools", "Python"), ("skills: SQL;Python", "SQL"), ("a C# and C", "C"), ("C# and C, Java", "C"),
    ("f(x) is used", "f(x)"), ("Knowledge of Java.", "Java"), ("Java, JavaScript", "Java"),
])
def test_punctuation_neighbours_and_phrases_match(haystack, term):
    assert found(haystack, term) is True


@pytest.mark.parametrize("haystack, term", [
    ("Python", "Python"), ("use Python", "Python"), ("Python rocks", "Python"), ("C++", "C++"), ("C#", "C#"),
    ("PYTHON", "python"), ("python", "PYTHON"), ("École Python", "Python"), ("a\tb\nPython", "Python"),
])
def test_start_end_and_case(haystack, term):
    assert found(haystack, term) is True


def test_non_ascii_letters_count_as_word_characters():
    assert found("éPython", "Python") is False and found("Pythoné", "Python") is False
    assert found("é Python é", "Python") is True


@pytest.mark.parametrize("term", ["", "   ", "\n\t", "   "])
def test_empty_or_blank_needle_never_matches(term):
    assert contains_term(squash("Python and more"), term) is False


def test_the_needle_is_taken_literally_not_as_a_pattern():
    assert found("nodexjs", "Node.js") is False and found("a.b", "a.b") is True
    assert found("price (5)", "(5)") is True and found("price 5", "(5)") is False


def test_each_occurrence_is_checked_not_just_the_first():
    assert found("JavaScript, and later Java", "Java") is True
    assert found("C++, C#, C", "C") is True and found("C++, C#, Cobol", "C") is False


# --------------------------------------------------------------------------
# the parser
# --------------------------------------------------------------------------
class FakeProvider(LLMProvider):
    name = "fake"
    model = "fake-1"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def generate_json(self, system_prompt, user_prompt):
        self.calls.append(user_prompt)
        return self.answers.pop(0)


@pytest.fixture(scope="module")
def ext():
    return load_extended_taxonomy()


def parse(tmp_path, ext, jd, *answers, **kwargs):
    provider = FakeProvider(*answers)
    parser = JDParser(provider, taxonomy=ext, learned_path=tmp_path / "learned.json",
                      queue_path=tmp_path / "queue.json", **kwargs)
    return parser.parse(jd, "job"), provider


def req(written, evidence):
    return {"as_written": written, "name": written, "signal": "required", "evidence": evidence}


def test_java_go_and_r_are_not_invented_from_javascript_and_google(tmp_path, ext):
    jd = "Required: JavaScript and Google Cloud."
    ev = "Required: JavaScript and Google Cloud."
    result, provider = parse(tmp_path, ext, jd, {"skills": [
        req("JavaScript", ev), req("Java", ev), req("Go", ev), req("R", ev)]})
    assert [(s["skill"], s["tier"]) for s in result["skills"]] == [("JavaScript", "must_have")]
    assert result["rejected"] == [{"term": t, "reason": "not_in_jd"} for t in ("Java", "Go", "R")]
    assert result["status"] == "ok" and result["llm_calls"] == 1 and len(provider.calls) == 1
    assert result["unmapped"] == [] and result["counts"]["rejected"] == 3
    json.dumps(result)


def test_c_is_not_found_in_c_plus_plus_or_c_sharp(tmp_path, ext):
    jd = "Required: C++ and C#."
    result, _ = parse(tmp_path, ext, jd, {"skills": [req("C", jd), req("C++", jd), req("C#", jd)]})
    assert [s["skill"] for s in result["skills"]] == ["C++", "C#"]
    assert result["rejected"] == [{"term": "C", "reason": "not_in_jd"}]


def test_real_mentions_are_kept(tmp_path, ext):
    jd = "Python, R and SQL are used."
    items = [{"as_written": w, "name": w, "signal": "none", "evidence": w} for w in ("Python", "R", "SQL")]
    result, _ = parse(tmp_path, ext, jd, {"skills": items})
    assert [s["skill"] for s in result["skills"]] == ["Python", "R", "SQL"] and result["rejected"] == []


@pytest.mark.parametrize("jd, written, skill", [
    ("We use Node.js daily.", "Node.js", "Node.js"),
    ("HTML/CSS and Git are needed.", "HTML/CSS", "HTML"),
    ("Knowledge of C++, Java.", "C++", "C++"),
    ("Experience with Go (Golang).", "Go", "Go"),
    ("Machine\n Learning models.", "machine learning", "Machine Learning"),
    ("Python/Django (advanced).", "Python/Django (advanced)", "Python"),
])
def test_the_spec_examples_still_parse(tmp_path, ext, jd, written, skill):
    result, _ = parse(tmp_path, ext, jd, {"skills": [
        {"as_written": written, "name": written, "signal": "none", "evidence": written}]})
    assert skill in [s["skill"] for s in result["skills"]]
    assert result["rejected"] == []


def test_an_unknown_term_glued_inside_a_word_is_rejected_before_any_proposal(tmp_path, ext):
    result, provider = parse(tmp_path, ext, "We use Zorbify daily.", {"skills": [
        {"as_written": "Zorb", "name": "Zorb", "signal": "none", "evidence": "Zorb"}]})
    assert result["rejected"] == [{"term": "Zorb", "reason": "not_in_jd"}]
    assert result["unmapped"] == [] and result["llm_calls"] == 1  # no wasted proposal call


def test_the_evidence_check_is_still_a_sentence_substring_test(tmp_path, ext):
    jd = "You must know Docker well."
    result, _ = parse(tmp_path, ext, jd, {"skills": [
        {"as_written": "Docker", "name": "Docker", "signal": "required", "evidence": "must know Dock"}]})
    assert result["skills"][0]["tier"] == "must_have"  # unchanged on purpose: evidence is a whole phrase


# --------------------------------------------------------------------------
# guard 5 of the proposal evaluation
# --------------------------------------------------------------------------
def proposal(**over):
    base = {"term": "Zorb", "is_technical_skill": True, "canonical_name": "Zorb", "category": "tools",
            "alias_of": None, "aliases": [], "confidence": 0.95}
    base.update(over)
    return base


def test_guard_5_rejects_a_term_that_only_appears_glued_inside_a_word(ext):
    decision = evaluate_proposal(proposal(), "Zorb", "We use Zorbify daily.", ext, 0)
    assert decision["action"] == "queue" and "guard 5" in decision["reason"]
    alias = evaluate_proposal(proposal(term="Postgres9", alias_of="PostgreSQL"), "Postgres9",
                              "We use Postgres99 daily.", ext, 0)
    assert alias["action"] == "queue" and "guard 5" in alias["reason"]


def test_guard_5_accepts_a_real_whole_term(ext):
    for jd in ("We use Zorb daily.", "zorb, zorb2", "(ZORB)", "uses Zorb/Quux", "We use\n Zorb."):
        assert evaluate_proposal(proposal(), "Zorb", jd, ext, 0)["action"] == "auto_add_skill", jd
    multi_word = evaluate_proposal(proposal(term="Quux Lab"), "Quux Lab", "We use quux\n lab here", ext, 0)
    assert multi_word["action"] == "auto_add_skill"  # a multi-word term across a line break
