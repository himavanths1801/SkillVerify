"""Tests for tech scanner follow-up 2 (file-count cap, `basis`, inferred evidence). Mocked HTTP only."""
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
from skillverify.tech_scanner import TechScanner, TechScannerError, compare_claimed_skills

API = "https://api.github.com"


# --------------------------------------------------------------------------
# helpers (copied on purpose; this file imports nothing from other test files)
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def tax():
    return Taxonomy.load()


@pytest.fixture
def validator():
    return GitHubValidator(token="test-token", backoff_seconds=0, max_retries=0)


def item(skill, confidence="strong", repos=("a",), in_taxonomy=True, types=("declared_dependency",)):
    return {"skill": skill if in_taxonomy else None, "label": skill, "in_taxonomy": in_taxonomy,
            "confidence": confidence, "evidence_types": list(types), "repos": list(repos)}


def tech(*items, complete=True, python_files=None):
    result = {"complete": complete, "skills": list(items)}
    if python_files is not None:
        result["per_repo"] = [{"python_files_read": python_files}]
    return result


def compare(claimed, languages=None, tech_stack=None, **kwargs):
    """compare_claimed_skills keyed by the first claimed text; checks JSON-serialisability."""
    val = {"summary": {"languages_combined": languages}} if languages is not None else None
    out = compare_claimed_skills(claimed, val, tech_stack if tech_stack is not None else tech(),
                                 Taxonomy.load(), **kwargs)
    json.dumps(out)
    return {e["claimed"][0]: e for e in out}


def b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def mock_repo(name, files):
    base = f"{API}/repos/alice/{name}"
    responses.add(responses.GET, base, json={"default_branch": "main"})
    responses.add(responses.GET, f"{base}/git/trees/main", json={
        "tree": [{"path": p, "type": "blob", "size": len(c)} for p, c in files.items()], "truncated": False})
    for path, content in files.items():
        responses.add(responses.GET, f"{base}/contents/{quote(path, safe='/')}",
                      json={"encoding": "base64", "content": b64(content)})


# --------------------------------------------------------------------------
# 1-4. Fix 1: file counts cannot make a trace share look strong
# --------------------------------------------------------------------------
@pytest.mark.parametrize("python_bytes, files", [(99, 3), (36, 42)])  # 0.99% / 0.36% of 10,000
def test_trace_share_caps_python_file_evidence(python_bytes, files):
    langs = {"Python": python_bytes, "Rust": 10_000 - python_bytes}
    python = compare(["Python"], langs, tech(python_files=files))["Python"]
    assert python["status"] == "found" and python["confidence"] == "weak"
    files_source = next(s for s in python["sources"] if s["type"] == "python_source_files")
    assert files_source == {"type": "python_source_files", "files": files, "trace": True}
    assert next(s for s in python["sources"] if s["type"] == "language_stats")["trace"] is True


def test_notebook_and_python_shares_are_added_together():
    # 0.6% + 0.6% = 1.2%: each language source is a trace, but together they are not.
    langs = {"Python": 60, "Jupyter Notebook": 60, "Rust": 9880}
    python = compare(["Python"], langs, tech(python_files=2))["Python"]
    files_source = next(s for s in python["sources"] if s["type"] == "python_source_files")
    assert files_source == {"type": "python_source_files", "files": 2}
    assert python["confidence"] == "strong"


def test_exactly_one_percent_combined_is_not_a_trace():
    langs = {"Python": 50, "Jupyter Notebook": 50, "Rust": 9900}
    python = compare(["Python"], langs, tech(python_files=2))["Python"]
    assert {"type": "python_source_files", "files": 2} in python["sources"]
    assert python["confidence"] == "strong"


def test_just_under_one_percent_combined_is_a_trace():
    langs = {"Python": 50, "Jupyter Notebook": 49, "Rust": 9901}
    python = compare(["Python"], langs, tech(python_files=2))["Python"]
    assert {"type": "python_source_files", "files": 2, "trace": True} in python["sources"]
    assert python["confidence"] == "weak"


def test_big_notebook_share_with_files_stays_strong():
    langs = {"Jupyter Notebook": 7500, "Rust": 2500}
    python = compare(["Python"], langs, tech(python_files=4))["Python"]
    assert python["confidence"] == "strong"
    assert python["sources"][-1] == {"type": "python_source_files", "files": 4}


@pytest.mark.parametrize("languages", [None, {}])
def test_without_language_statistics_files_stay_strong(languages):
    python = compare(["Python"], languages, tech(python_files=5))["Python"]
    assert python["confidence"] == "strong"
    assert python["sources"] == [{"type": "python_source_files", "files": 5}]


def test_overall_python_confidence_is_still_the_best_source():
    langs = {"Python": 50, "Rust": 9950}  # a 0.5% trace: the files source is capped to weak
    direct = tech(item("Python", "strong", types=("imported_in_code",)), python_files=3)
    python = compare(["Python"], langs, direct)["Python"]
    assert python["confidence"] == "strong"  # the strong scan item wins
    assert [s["type"] for s in python["sources"]] == ["language_stats", "python_source_files", "imported_in_code"]
    assert python["sources"][1]["trace"] is True
    capped_only = compare(["Python"], langs, tech(python_files=3))["Python"]
    assert capped_only["confidence"] == "weak"


# --------------------------------------------------------------------------
# 5-6. Fix 2: the `basis` key
# --------------------------------------------------------------------------
def test_basis_values_on_the_right_statuses():
    incomplete = tech(complete=False)
    langs = {"HTML": 100}
    out = compare(["Docker", "Rust", "Django", "REST APIs", "Python", "Foobar Lang"], langs, incomplete)
    assert (out["Docker"]["status"], out["Docker"]["basis"]) == ("not_found", "language_stats_only")
    assert (out["Rust"]["status"], out["Rust"]["basis"]) == ("not_found", "language_stats_only")
    assert (out["Django"]["status"], out["Django"]["basis"]) == ("not_found_scan_incomplete", "incomplete_scan")
    assert (out["Python"]["status"], out["Python"]["basis"]) == ("not_found_scan_incomplete", "incomplete_scan")
    assert (out["REST APIs"]["status"], out["REST APIs"]["basis"]) == ("not_detectable_from_github", "not_checkable")
    assert out["Foobar Lang"]["status"] == "not_in_taxonomy" and "basis" not in out["Foobar Lang"]


def test_complete_scan_gives_full_scan_basis():
    out = compare(["Django", "Rust", "Docker"], {"HTML": 100}, tech(complete=True))
    assert all(out[name]["status"] == "not_found" and out[name]["basis"] == "full_scan" for name in out)


def test_found_entries_have_no_basis():
    out = compare(["Python", "YOLO"], {"Python": 5000}, tech(item("YOLO", in_taxonomy=False)))
    assert out["Python"]["status"] == "found" and "basis" not in out["Python"]
    assert out["YOLO"]["status"] == "found" and "basis" not in out["YOLO"]


def load_demo():
    path = Path(__file__).resolve().parent.parent / "examples" / "run_tech_scan.py"
    spec = importlib.util.spec_from_file_location("run_tech_scan_followup2", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_not_found_wording_per_basis():
    demo = load_demo()

    def line(status, **extra):
        return demo.claim_line({"claimed": ["Docker"], "status": status, "sources": [], **extra})

    assert line("not_found", basis="language_stats_only") == \
        "  [not_found] Docker: not found in GitHub language statistics (the scan itself was incomplete)"
    assert line("not_found", basis="full_scan") == "  [not_found] Docker: not found (scan was complete)"
    assert line("not_found") == "  [not_found] Docker: not found (scan was complete)"
    assert line("not_found", basis="something_else") == "  [not_found] Docker: not found (scan was complete)"
    assert line("not_found_scan_incomplete", basis="incomplete_scan") == \
        "  [not_found_scan_incomplete] Docker: not found so far, but the scan was incomplete"
    assert line("not_detectable_from_github", basis="not_checkable") == \
        "  [not_detectable_from_github] Docker: cannot be checked from GitHub"
    assert line("not_in_taxonomy") == "  [not_in_taxonomy] Docker: not in the skill taxonomy"


def test_demo_inferred_and_trace_file_lines():
    demo = load_demo()
    inferred = {"claimed": ["Machine Learning"], "status": "found", "confidence": "medium",
                "sources": [{"type": "inferred", "from": ["scikit-learn", "PyTorch"], "repos": 2}]}
    assert demo.claim_line(inferred) == "  [found, medium] Machine Learning: inferred from scikit-learn, PyTorch"
    single = {**inferred, "claimed": ["Machine Learning"],
              "sources": [{"type": "inferred", "from": ["scikit-learn"], "repos": 1}]}
    assert demo.claim_line(single) == "  [found, medium] Machine Learning: inferred from scikit-learn"
    trace = {"claimed": ["Python"], "status": "found", "confidence": "weak",
             "sources": [{"type": "python_source_files", "files": 3, "trace": True}]}
    assert demo.claim_line(trace) == "  [found, weak] Python: 3 Python file(s) (trace)"
    normal = {**trace, "confidence": "strong", "sources": [{"type": "python_source_files", "files": 3}]}
    assert demo.claim_line(normal) == "  [found, strong] Python: 3 Python file(s)"


# --------------------------------------------------------------------------
# 7-9. Fix 3: inferred evidence
# --------------------------------------------------------------------------
def test_express_implies_node():
    node = compare(["Node.js"], {"HTML": 10}, tech(item("Express.js", repos=("a", "b"))))["Node.js"]
    assert node["status"] == "found" and node["confidence"] == "medium"
    assert node["sources"] == [{"type": "inferred", "from": ["Express.js"], "repos": 2}]
    assert "basis" not in node


def test_machine_learning_lists_impliers_in_file_order_and_counts_distinct_repos():
    stack = tech(item("PyTorch", repos=("b", "c")), item("scikit-learn", repos=("a", "b")))  # not in file order
    ml = compare(["Machine Learning"], {"HTML": 10}, stack)["Machine Learning"]
    assert ml["sources"] == [{"type": "inferred", "from": ["scikit-learn", "PyTorch"], "repos": 3}]
    assert ml["status"] == "found" and ml["confidence"] == "medium"


def test_single_implier_cases():
    assert compare(["Deep Learning"], {"HTML": 1}, tech(item("Keras")))["Deep Learning"]["status"] == "found"
    ai = compare(["AI"], {"HTML": 1}, tech(item("LangChain")))["AI"]
    assert ai["skill"] == "Artificial Intelligence" and ai["sources"][0]["from"] == ["LangChain"]


def test_confidence_is_medium_at_best_and_weak_when_every_implier_is_weak():
    strong = compare(["Deep Learning"], {"HTML": 1}, tech(item("Keras", "strong")))["Deep Learning"]
    assert strong["confidence"] == "medium"  # never strong
    weak = compare(["Deep Learning"], {"HTML": 1}, tech(item("Keras", "weak")))["Deep Learning"]
    assert weak["confidence"] == "weak"
    mixed = compare(["Deep Learning"], {"HTML": 1},
                    tech(item("Keras", "weak"), item("PyTorch", "medium")))["Deep Learning"]
    assert mixed["confidence"] == "medium"


def test_docker_compose_implies_docker():
    docker = compare(["Docker"], {"Rust": 100}, tech(item("Docker Compose", types=("file_signal",))))["Docker"]
    assert docker["status"] == "found" and docker["sources"][0] == {
        "type": "inferred", "from": ["Docker Compose"], "repos": 1}


def test_items_with_missing_confidence_or_repos_are_tolerated():
    bare = {"skill": "Keras", "label": "Keras", "in_taxonomy": True}
    deep = compare(["Deep Learning"], {"HTML": 1}, tech(bare))["Deep Learning"]
    assert deep["confidence"] == "medium" and deep["sources"][0]["repos"] == 0


def test_label_only_items_do_not_imply_anything():
    stack = tech(item("Express.js", in_taxonomy=False))
    assert compare(["Node.js"], {"HTML": 1}, stack)["Node.js"]["status"] == "not_detectable_from_github"


def test_no_implier_keeps_todays_status():
    stack = tech(item("Pandas"), item("NumPy"), item("Matplotlib"), item("YOLO", in_taxonomy=False),
                 item("Computer Vision"), item("SQL"), item("Flask"))
    out = compare(["Node.js", "Machine Learning", "Deep Learning", "Docker"], {"HTML": 1}, stack)
    for name in ("Node.js", "Machine Learning", "Deep Learning"):
        assert out[name]["status"] == "not_detectable_from_github" and out[name]["basis"] == "not_checkable"
        assert out[name]["sources"] == []
    assert (out["Docker"]["status"], out["Docker"]["basis"]) == ("not_found", "full_scan")
    incomplete = compare(["Docker"], None, tech(item("Pandas"), complete=False))["Docker"]
    assert (incomplete["status"], incomplete["basis"]) == ("not_found_scan_incomplete", "incomplete_scan")


def test_direct_evidence_wins_over_inference():
    stack = tech(item("Docker Compose"), item("Express.js"))
    docker = compare(["Docker"], {"Dockerfile": 500, "Rust": 9500}, stack)["Docker"]
    assert [s["type"] for s in docker["sources"]] == ["language_stats"]
    node = compare(["Node.js"], {"HTML": 1}, tech(item("Node.js", "strong", types=("imported_in_code",)),
                                                  item("Express.js")))["Node.js"]
    assert [s["type"] for s in node["sources"]] == ["imported_in_code"] and node["confidence"] == "strong"


# --------------------------------------------------------------------------
# 10. Loader, custom implications, and an unchanged scan
# --------------------------------------------------------------------------
def write(tmp_path, data):
    path = tmp_path / "implications.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def test_default_implications_file_loads_and_ids_exist(tax):
    loaded = ts._load_implications(None, tax)
    assert [e["skill"] for e in loaded] == [
        "Node.js", "Machine Learning", "Deep Learning", "Artificial Intelligence", "Docker"]
    for entry in loaded:
        assert tax.get(entry["skill"]) is not None
        assert all(tax.get(i) is not None for i in entry["implied_by"])
    flat = {i for e in loaded for i in e["implied_by"]}
    assert not flat & {"Pandas", "NumPy", "Matplotlib", "Computer Vision"}
    assert "REST APIs" not in {e["skill"] for e in loaded}
    assert ts.DEFAULT_IMPLICATIONS_PATH.name == "tech_implications.json"


GOOD = {"skill": "Node.js", "implied_by": ["Express.js"]}


@pytest.mark.parametrize("data, message", [
    ({"implications": []}, "non-empty 'implications'"),
    ([], "non-empty 'implications'"),
    ({"other": 1}, "non-empty 'implications'"),
    ({"implications": ["text"]}, "no 'skill'"),
    ({"implications": [{"implied_by": ["Express.js"]}]}, "no 'skill'"),
    ({"implications": [{"skill": 5, "implied_by": ["Express.js"]}]}, "no 'skill'"),
    ({"implications": [{"skill": " ", "implied_by": ["Express.js"]}]}, "no 'skill'"),
    ({"implications": [{"skill": "Node.js"}]}, "'implied_by' must be a non-empty list"),
    ({"implications": [{"skill": "Node.js", "implied_by": []}]}, "'implied_by' must be a non-empty list"),
    ({"implications": [{"skill": "Node.js", "implied_by": "Express.js"}]}, "'implied_by' must be a non-empty list"),
    ({"implications": [{"skill": "Node.js", "implied_by": [3]}]}, "'implied_by' must be a non-empty list"),
    ({"implications": [{"skill": "Nope", "implied_by": ["Express.js"]}]}, "'Nope' is not in the taxonomy"),
    ({"implications": [{"skill": "Node.js", "implied_by": ["Nope"]}]}, "'Nope' is not in the taxonomy"),
    ({"implications": [GOOD, {"skill": "Node.js", "implied_by": ["NestJS"]}]}, "appears in two entries"),
    ({"implications": [{"skill": "Node.js", "implied_by": ["Express.js", "Node.js"]}]}, "implied by itself"),
])
def test_bad_implication_files(tax, tmp_path, data, message):
    with pytest.raises(TechScannerError, match=message):
        ts._load_implications(write(tmp_path, data), tax)


def test_implication_file_missing_unreadable_or_invalid(tax, tmp_path):
    with pytest.raises(TechScannerError, match="not found"):
        ts._load_implications(tmp_path / "missing.json", tax)
    with pytest.raises(TechScannerError, match="Could not read"):
        ts._load_implications(write(tmp_path, "{oops"), tax)
    with pytest.raises(TechScannerError, match="Could not read"):
        ts._load_implications(tmp_path, tax)  # a directory


def test_custom_implications_argument_is_honored():
    custom = [{"skill": "Node.js", "implied_by": ["jQuery"]}]
    stack = tech(item("jQuery"), item("Express.js"))
    node = compare(["Node.js"], {"HTML": 1}, stack, implications=custom)["Node.js"]
    assert node["sources"][0]["from"] == ["jQuery"]
    # a custom list that does not mention Machine Learning switches that inference off
    ml = compare(["Machine Learning"], {"HTML": 1}, tech(item("PyTorch")), implications=custom)["Machine Learning"]
    assert ml["status"] == "not_detectable_from_github"


def test_implications_are_loaded_lazily_not_by_the_scanner(validator, tmp_path):
    skills = tmp_path / "skills.json"
    skills.write_text(json.dumps({"skills": [{"id": "Python", "category": "language"},
                                             {"id": "SQL", "category": "database"}]}), encoding="utf-8")
    small = Taxonomy.load(skills)
    signals = tmp_path / "signals.json"
    signals.write_text(json.dumps({"signals": [
        {"label": "Pandas-ish", "skills": ["Python"], "python_packages": ["pandas"]}]}), encoding="utf-8")
    TechScanner(validator, taxonomy=small, signals_path=signals)  # must not need tech_implications.json
    with pytest.raises(TechScannerError, match="not in the taxonomy"):
        compare_claimed_skills(["Python"], None, tech(), small)  # default file is loaded here, on demand


@responses.activate
def test_scan_output_never_contains_inferred_evidence(validator, tax):
    mock_repo("web", {"package.json": json.dumps({"dependencies": {"express": "4"}}),
                      "ml.py": "import sklearn\nimport torch\n", "docker-compose.yml": "services: {}"})
    val = {"username": "alice", "status": "ok", "repos": [
        {"name": "web", "counted": True, "last_commit_date": "2025-01-01T00:00:00Z"}], "summary": {}}
    scan = TechScanner(validator, taxonomy=tax).scan(val)
    labels = {s["label"] for s in scan["skills"]}
    assert {"Express.js", "scikit-learn", "PyTorch", "Docker Compose"} <= labels
    assert not labels & {"Node.js", "Machine Learning", "Deep Learning", "Artificial Intelligence", "Docker"}
    assert "inferred" not in json.dumps(scan)
    # ...but the comparison infers from that same scan:
    out = compare_claimed_skills(["Node.js", "Machine Learning", "Docker"], val, scan, tax)
    assert [e["status"] for e in out] == ["found", "found", "found"]
    assert all(e["sources"][0]["type"] == "inferred" and e["confidence"] == "medium" for e in out)
    json.dumps(out)
