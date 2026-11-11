"""Tests for the tech scanner follow-up fixes. All HTTP is mocked; no network."""
import base64
import importlib.util
import json
from pathlib import Path
from urllib.parse import quote

import pytest
import responses

from skillverify import tech_scanner as ts
from skillverify.github_validator import GitHubValidator
from skillverify.taxonomy import Taxonomy
from skillverify.tech_scanner import TechScanner, compare_claimed_skills

API = "https://api.github.com"


# --------------------------------------------------------------------------
# small helpers (copied in on purpose: this file does not import the old tests)
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def tax():
    return Taxonomy.load()


@pytest.fixture
def validator():
    return GitHubValidator(token="test-token", backoff_seconds=0, max_retries=0)


def make_scanner(validator, tax, **kwargs):
    return TechScanner(validator, taxonomy=tax, **kwargs)


def b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def mock_repo(name, files=None, branch="main"):
    files = files or {}
    base = f"{API}/repos/alice/{name}"
    responses.add(responses.GET, base, json={"default_branch": branch})
    tree = [{"path": p, "type": "blob", "size": len(c)} for p, c in files.items()]
    responses.add(responses.GET, f"{base}/git/trees/{quote(branch, safe='')}",
                  json={"tree": tree, "truncated": False})
    for path, content in files.items():
        responses.add(responses.GET, f"{base}/contents/{quote(path, safe='/')}",
                      json={"encoding": "base64", "content": b64(content)})


def validation(*names, languages=None):
    repos = [{"name": n, "counted": True, "is_fork": False,
              "last_commit_date": f"2025-0{9 - i}-01T00:00:00Z"} for i, n in enumerate(names)]
    return {"username": "alice", "status": "ok", "repos": repos,
            "summary": {"languages_combined": languages if languages is not None else {"Python": 100}}}


def notebook(*cells):
    return json.dumps({"cells": [{"cell_type": kind, "source": src} for kind, src in cells]})


def compare(claimed, languages=None, tech=None, **kwargs):
    """compare_claimed_skills keyed by claimed text; also checks JSON-serialisability."""
    taxonomy = Taxonomy.load()
    val = {"summary": {"languages_combined": languages}} if languages is not None else None
    out = compare_claimed_skills(claimed, val, tech if tech is not None else {"complete": True, "skills": []},
                                 taxonomy, **kwargs)
    json.dumps(out)
    return {e["claimed"][0]: e for e in out}


# --------------------------------------------------------------------------
# 1-2. python_files_read
# --------------------------------------------------------------------------
@responses.activate
def test_python_files_read_counts_only_parsed_files_with_code(validator, tax):
    mock_repo("py", {
        "analysis.ipynb": notebook(("code", ["import pandas as pd\n", "x = 1"])),
        "main.py": "print('hello')\n",
        "notes.ipynb": notebook(("markdown", "just text"), ("code", "   \n")),
        "empty_cells.ipynb": notebook(("code", [])),
        "broken.ipynb": "this is not json",
        "blank.py": "   \n\n",
        "gone.py": "x = 1",
        "app.js": "console.log(1)",
    })
    responses.replace(responses.GET, f"{API}/repos/alice/py/contents/gone.py", status=404, json={})
    tech = make_scanner(validator, tax).scan(validation("py"))
    per = tech["per_repo"][0]
    assert per["python_files_read"] == 2  # analysis.ipynb and main.py only
    assert per["files_failed"] == 2       # broken.ipynb and gone.py
    assert per["source_files_read"] == 6  # fetched and processed, whether or not they held Python


@responses.activate
def test_failed_and_invalid_repos_still_carry_the_key(validator, tax):
    responses.add(responses.GET, f"{API}/repos/alice/blocked", status=451, json={})
    tech = make_scanner(validator, tax).scan(validation("blocked", "bad name"))
    assert [p["python_files_read"] for p in tech["per_repo"]] == [0, 0]
    assert all("error" in p for p in tech["per_repo"])


@responses.activate
def test_python_files_are_summed_over_repos_and_skills_are_unchanged(validator, tax):
    mock_repo("one", {"a.py": "import pandas"})
    mock_repo("two", {"b.ipynb": notebook(("code", "import numpy"))})
    tech = make_scanner(validator, tax).scan(validation("one", "two"))
    assert [p["python_files_read"] for p in tech["per_repo"]] == [1, 1]
    labels = {s["label"] for s in tech["skills"]}
    assert labels == {"Pandas", "NumPy"}  # no "Python" item is added to skills
    assert all(s["skill"] != "Python" for s in tech["skills"])
    json.dumps(tech)


@responses.activate
def test_notebook_only_account_is_found_as_python_end_to_end(validator, tax):
    mock_repo("nb", {"a.ipynb": notebook(("code", "import pandas"))})
    val = validation("nb", languages={"Jupyter Notebook": 9000, "HTML": 1000})
    tech = make_scanner(validator, tax).scan(val)
    entry = compare_claimed_skills(["Python"], val, tech, tax)[0]
    assert entry["status"] == "found" and entry["confidence"] == "strong"
    assert [s["type"] for s in entry["sources"]] == ["language_stats_notebook", "python_source_files"]


# --------------------------------------------------------------------------
# 3-4. The two new Python sources
# --------------------------------------------------------------------------
def test_python_found_through_notebook_bytes():
    langs = {"Jupyter Notebook": 7560, "TypeScript": 2400, "CSS": 3}
    python = compare(["Python"], langs)["Python"]
    assert python["status"] == "found" and python["confidence"] == "strong"
    assert python["sources"] == [
        {"type": "language_stats_notebook", "bytes": 7560, "percent": round(7560 * 100 / 9963, 2)}]
    assert not any(s["type"] == "language_stats" for s in python["sources"])


def test_python_keeps_normal_language_source_next_to_notebook_source():
    python = compare(["Python"], {"Python": 5000, "Jupyter Notebook": 5000})["Python"]
    assert [s["type"] for s in python["sources"]] == ["language_stats", "language_stats_notebook"]
    assert python["sources"][0] == {"type": "language_stats", "bytes": 5000, "percent": 50.0}


def test_python_found_through_source_files_without_any_language_stats():
    tech = {"complete": True, "skills": [], "per_repo": [{"python_files_read": 2}, {"python_files_read": 1}]}
    python = compare(["Python"], None, tech)["Python"]
    assert python["status"] == "found" and python["confidence"] == "strong"
    assert python["sources"] == [{"type": "python_source_files", "files": 3}]


def test_only_python_gets_the_extra_sources():
    tech = {"complete": True, "skills": [], "per_repo": [{"python_files_read": 4}]}
    out = compare(["Django", "Rust"], {"Jupyter Notebook": 100}, tech)
    assert out["Django"]["status"] == "not_found" and out["Django"]["sources"] == []
    assert out["Rust"]["sources"] == []


def test_python_without_evidence_keeps_todays_status():
    langs = {"HTML": 100}
    incomplete = {"complete": False, "skills": [], "per_repo": [{"python_files_read": 0}]}
    assert compare(["Python"], langs, incomplete)["Python"]["status"] == "not_found_scan_incomplete"
    assert compare(["Python"], langs)["Python"]["status"] == "not_found"


@pytest.mark.parametrize("per_repo", [
    "MISSING", "x", None, 5, [1, None, {"python_files_read": "many"}],
    [{"python_files_read": True}, {"python_files_read": -3}, {"python_files_read": 0}, {}],
])
def test_malformed_per_repo_never_raises_and_adds_no_python_source(per_repo):
    tech = {"complete": True, "skills": []}
    if per_repo != "MISSING":
        tech["per_repo"] = per_repo
    python = compare(["Python"], {"HTML": 100}, tech)["Python"]
    assert python["status"] == "not_found" and python["sources"] == []


# --------------------------------------------------------------------------
# 5. Trace handling
# --------------------------------------------------------------------------
TRACE_LANGS = {"Python": 8000, "Rust": 1872, "HTML": 100, "JavaScript": 25, "CSS": 3}  # total 10000


def test_trace_shares_are_found_but_weak():
    out = compare(["CSS", "JavaScript"], TRACE_LANGS)
    assert out["CSS"]["status"] == "found" and out["CSS"]["confidence"] == "weak"
    assert out["CSS"]["sources"] == [{"type": "language_stats", "bytes": 3, "percent": 0.03, "trace": True}]
    assert out["JavaScript"]["confidence"] == "weak"
    assert out["JavaScript"]["sources"][0]["trace"] is True and out["JavaScript"]["sources"][0]["percent"] == 0.25


def test_exactly_one_percent_is_strong_without_trace():
    html = compare(["HTML"], TRACE_LANGS)["HTML"]
    assert html["confidence"] == "strong"
    assert html["sources"] == [{"type": "language_stats", "bytes": 100, "percent": 1.0}]
    assert "trace" not in html["sources"][0]


def test_just_under_one_percent_is_a_trace_even_if_it_rounds_to_one():
    langs = {"Rust": 98_904, "CSS": 996, "HTML": 100}  # total 100,000: CSS is 0.996%, which rounds to 1.0
    css = compare(["CSS"], langs)["CSS"]
    assert css["confidence"] == "weak" and css["sources"][0]["percent"] == 1.0
    assert css["sources"][0]["trace"] is True


def test_normal_share_is_strong_and_has_exact_shape():
    python = compare(["Python"], TRACE_LANGS)["Python"]
    assert python["confidence"] == "strong"
    assert python["sources"] == [{"type": "language_stats", "bytes": 8000, "percent": 80.0}]


def test_trace_plus_strong_import_item_is_strong():
    tech = {"complete": True, "skills": [
        {"skill": "JavaScript", "label": "JavaScript", "in_taxonomy": True, "confidence": "strong",
         "evidence_types": ["imported_in_code"], "repos": ["a"]}]}
    js = compare(["JavaScript"], TRACE_LANGS, tech)["JavaScript"]
    assert js["confidence"] == "strong"
    assert [s["type"] for s in js["sources"]] == ["language_stats", "imported_in_code"]
    assert js["sources"][0]["trace"] is True


def test_notebook_source_also_gets_trace_under_one_percent():
    only_trace = compare(["Python"], {"HTML": 9950, "Jupyter Notebook": 50})["Python"]
    assert only_trace["confidence"] == "weak"
    assert only_trace["sources"] == [
        {"type": "language_stats_notebook", "bytes": 50, "percent": 0.5, "trace": True}]
    mixed = compare(["Python"], {"Python": 9950, "Jupyter Notebook": 50})["Python"]
    assert mixed["confidence"] == "strong"  # best of the sources
    assert mixed["sources"][1]["trace"] is True and "trace" not in mixed["sources"][0]


def test_zero_or_odd_notebook_bytes_add_no_source():
    for value in (0, True, "lots", None):
        python = compare(["Python"], {"HTML": 100, "Jupyter Notebook": value})["Python"]
        assert python["sources"] == [] and python["status"] == "not_found"


# --------------------------------------------------------------------------
# 7. @types/ packages
# --------------------------------------------------------------------------
@responses.activate
def test_types_packages_are_not_listed_as_unmapped(validator, tax):
    mock_repo("web", {"package.json": json.dumps({
        "dependencies": {"react": "1", "left-pad": "1"},
        "devDependencies": {"@types/node": "1", "@types/react": "1", "typescript": "5"}})})
    tech = make_scanner(validator, tax).scan(validation("web"))
    assert tech["unmapped_packages"] == [{"name": "left-pad", "ecosystem": "js", "repos": 1}]
    labels = {s["label"] for s in tech["skills"]}
    assert {"React", "TypeScript"} <= labels


def test_python_names_are_unaffected_by_the_types_rule(validator, tax):
    scanner = make_scanner(validator, tax)
    state = ts._ScanState(ts._Findings(tax))
    scanner._record_packages(state, ["@types/foo", "left-pad"], "python", "repo", "requirements.txt")
    scanner._record_packages(state, ["@types/foo", "left-pad"], "js", "repo", "package.json")
    assert set(state.findings.unmapped) == {("@types/foo", "python"), ("left-pad", "python"), ("left-pad", "js")}


def test_a_mapped_types_name_still_wins(validator, tax, tmp_path):
    signals = tmp_path / "signals.json"
    signals.write_text(json.dumps({"signals": [
        {"label": "Node types", "skills": ["Node.js"], "js_packages": ["@types/node"]}]}), encoding="utf-8")
    scanner = make_scanner(validator, tax, signals_path=signals)
    state = ts._ScanState(ts._Findings(tax))
    scanner._record_packages(state, ["@types/node", "@types/other"], "js", "repo", "package.json")
    assert ("skill", "Node.js") in state.findings.items and state.findings.unmapped == {}


# --------------------------------------------------------------------------
# 8. Wording of the incomplete status for language-only skills
# --------------------------------------------------------------------------
def test_language_only_skill_is_plain_not_found_when_scan_is_incomplete():
    incomplete = {"complete": False, "skills": []}
    out = compare(["Rust", "Django", "Python", "SQL", "Go"], {"HTML": 100}, incomplete)
    assert out["Rust"]["status"] == "not_found" and out["Go"]["status"] == "not_found"
    assert out["Django"]["status"] == "not_found_scan_incomplete"
    assert out["Python"]["status"] == "not_found_scan_incomplete"
    assert out["SQL"]["status"] == "not_found_scan_incomplete"
    assert all(e["sources"] == [] and e["confidence"] is None for e in out.values())


def test_explicit_detectable_set_still_wins():
    out = compare(["Rust"], {"HTML": 100}, {"complete": False, "skills": []}, detectable=set())
    assert out["Rust"]["status"] == "not_detectable_from_github"


def test_without_language_stats_a_language_only_skill_stays_incomplete():
    # No language statistics at all means the premise ("already complete") does not hold.
    incomplete = {"complete": False, "skills": []}
    assert compare(["Rust"], None, incomplete)["Rust"]["status"] == "not_found_scan_incomplete"
    assert compare_claimed_skills(["Rust"], {"summary": {"languages_combined": "oops"}}, incomplete,
                                  Taxonomy.load())[0]["status"] == "not_found_scan_incomplete"


def test_scan_skills_argument_and_scanner_helper(validator, tax):
    incomplete = {"complete": False, "skills": []}
    assert compare(["Python"], {"HTML": 100}, incomplete, scan_skills=set())["Python"]["status"] == "not_found"
    scanner = make_scanner(validator, tax)
    scannable = scanner.scannable_skills()
    assert {"Python", "SQL", "Pandas"} <= scannable and "Rust" not in scannable
    # detectable_skills() is unchanged by this follow-up
    assert "Python" in scanner.detectable_skills() and "Rust" in scanner.detectable_skills()
    assert compare(["Rust"], {"HTML": 100}, incomplete, detectable=scanner.detectable_skills(),
                   scan_skills=scannable)["Rust"]["status"] == "not_found"


# --------------------------------------------------------------------------
# 9. Demo script
# --------------------------------------------------------------------------
def load_demo():
    path = Path(__file__).resolve().parent.parent / "examples" / "run_tech_scan.py"
    spec = importlib.util.spec_from_file_location("run_tech_scan_followup", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def found(*sources, confidence="strong"):
    return {"claimed": ["X"], "skill": "X", "status": "found", "confidence": confidence, "sources": list(sources)}


def test_claim_line_shows_trace_notebook_and_python_files():
    demo = load_demo()
    trace = demo.claim_line(found({"type": "language_stats", "bytes": 3, "percent": 0.03, "trace": True},
                                  confidence="weak"))
    assert trace == "  [found, weak] X: language stats 0.03% (trace)"
    notebook_line = demo.claim_line(found({"type": "language_stats_notebook", "bytes": 5, "percent": 75.88}))
    assert notebook_line == "  [found, strong] X: language stats (notebook) 75.88%"
    trace_notebook = demo.claim_line(found(
        {"type": "language_stats_notebook", "bytes": 5, "percent": 0.5, "trace": True}, confidence="weak"))
    assert "language stats (notebook) 0.5% (trace)" in trace_notebook
    files = demo.claim_line(found({"type": "python_source_files", "files": 3}))
    assert files == "  [found, strong] X: 3 Python file(s)"


def test_claim_line_normal_lines_are_unchanged():
    demo = load_demo()
    assert demo.claim_line(found({"type": "language_stats", "bytes": 5, "percent": 63.12})) == \
        "  [found, strong] X: language stats 63.12%"
    assert demo.claim_line(found({"type": "imported_in_code", "repos": 2},
                                 {"type": "declared_dependency", "repos": 1})) == \
        "  [found, strong] X: imported_in_code, declared_dependency"
    assert "cannot be checked" in demo.claim_line(
        {"claimed": ["REST APIs"], "status": "not_detectable_from_github", "sources": []})
