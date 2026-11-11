"""Skill signals, candidate skills, repo choice, ranking, selection and reserve. Pure, offline, deterministic."""
import pytest
from test_questions_support import (JS_APP, NOTEBOOK, PY_ORDERS, SHELL_RELEASE, SQL_REPORT, github_result,
                                    repo_record, shop_analysis, skill_row, tech_item, tree_entry)

from skillverify_questions import config, select
from skillverify_questions.reasons import reason_for
from skillverify_questions.signals import Signals

SIGNALS = Signals()


# --------------------------------------------------------------------------
# skill matching
# --------------------------------------------------------------------------
def test_a_python_library_is_found_by_its_imports_and_use():
    assert SIGNALS.count("Pandas", "python", PY_ORDERS) == 3  # the import line and the two lines that use pd
    assert SIGNALS.count("NumPy", "python", PY_ORDERS) >= 2
    assert SIGNALS.count("Matplotlib", "python", PY_ORDERS) == 0


def test_python_skill_by_language_and_sql_by_keywords():
    assert SIGNALS.count("Python", "python", PY_ORDERS) >= 4  # one signal per definition
    assert SIGNALS.count("Python", "sql", SQL_REPORT) == 0
    assert SIGNALS.count("SQL", "sql", SQL_REPORT) == 1
    embedded = 'cur.execute("SELECT id FROM orders WHERE x = 1")\ncur.execute("INSERT INTO t VALUES (1)")\nx = 1\n'
    assert SIGNALS.count("SQL", "python", embedded) == 2
    assert SIGNALS.count("SQL", "python", "x = 'select and from'\n") == 0


def test_a_shell_script_shows_git():
    assert SIGNALS.count("Git", "shell", SHELL_RELEASE) >= 10
    assert SIGNALS.count("Git", "shell", "# git push\necho hi\n") == 0  # a comment is not a signal
    assert SIGNALS.count("Git", "python", PY_ORDERS) == 0


def test_a_notebook_shows_its_code_cells():
    from skillverify_questions import files
    code = files.notebook_code(NOTEBOOK)
    assert SIGNALS.count("NumPy", "python", code) >= 3 and SIGNALS.count("Matplotlib", "python", code) >= 3


def test_javascript_packages_and_word_matches():
    assert SIGNALS.count("React", "javascript", JS_APP) >= 1
    assert SIGNALS.count("Docker", "python", "client = Docker()\nimage = 'x'\n") == 1  # canonical name, case-sensitive
    assert SIGNALS.count("Docker", "python", "docker = 1\n") == 0
    assert SIGNALS.count("Not A Skill", "python", "Not A Skill here\n") == 1  # a name outside the taxonomy still matches


def test_short_aliases_are_not_matched_as_words():
    assert SIGNALS.count("JavaScript", "python", "js = 1\nes6 = 2\n") == 0
    assert SIGNALS.count("Unknown", "python", "x = 1\n") == 0


def test_empty_file_shows_nothing():
    assert SIGNALS.count("Python", "python", "\n\n") == 0


# --------------------------------------------------------------------------
# candidate skills
# --------------------------------------------------------------------------
def test_must_haves_come_first_by_weight_then_api_order_then_nice_to_haves():
    rows = [skill_row("N1", tier="nice_to_have"), skill_row("M1", weight=3), skill_row("M2", weight=5),
            skill_row("N2", tier="nice_to_have"), skill_row("M3", weight=3)]
    needs, skipped = select.candidate_skills(rows)
    assert [n.skill for n in needs] == ["M2", "M1", "M3", "N1", "N2"] and skipped == []


def test_only_found_skills_are_candidates_and_others_are_skipped_with_the_right_reason():
    rows = [skill_row("A"), skill_row("B", status="not_found"), skill_row("C", status="not_detectable_from_github"),
            skill_row("D", status="not_found_scan_incomplete"), skill_row("E", status=None),
            skill_row("F", tier="nice_to_have", status="not_detectable_from_github")]
    needs, skipped = select.candidate_skills(rows)
    assert [n.skill for n in needs] == ["A"]
    assert {s["skill"]: s["reason"] for s in skipped} == {
        "B": "not_found_in_code", "C": "not_checkable_from_code", "D": "not_found_in_code",
        "E": "not_found_in_code", "F": "not_checkable_from_code"}
    assert {s["skill"]: s["tier"] for s in skipped}["F"] == "nice_to_have"


def test_candidate_skills_never_invent_a_skill_and_tolerate_junk():
    needs, skipped = select.candidate_skills([skill_row("A", claimed=False), "junk", {"no": "skill"}, {"skill": 5}])
    assert [(n.skill, n.claimed) for n in needs] == [("A", False)] and skipped == []
    assert select.candidate_skills([{"skill": "X", "tier": "must_have", "weight": "heavy", "github_status": "found"}]
                                   )[0][0].skill == "X"


# --------------------------------------------------------------------------
# repos
# --------------------------------------------------------------------------
def needs_for(*names):
    return [select.Need(n, "must_have", True) for n in names]


def test_repos_with_the_most_needed_skills_first_then_newest_then_name():
    repos = [repo_record("zeta", "2026-01-01T00:00:00Z"), repo_record("alpha", "2026-01-01T00:00:00Z"),
             repo_record("fresh", "2026-09-01T00:00:00Z"), repo_record("rich", "2020-01-01T00:00:00Z"),
             repo_record("nodate", None)]
    scan = [tech_item("Pandas", ["rich"]), tech_item("SQL", ["rich", "fresh"]), tech_item("Other", ["zeta"])]
    github = github_result(repos, scan)
    chosen = select.choose_repos(needs_for("Pandas", "SQL"), github, SIGNALS, 10)
    assert [r.name for r in chosen] == ["rich", "fresh", "alpha", "zeta", "nodate"]  # alpha before zeta: same date
    assert [r.name for r in select.choose_repos(needs_for("Pandas", "SQL"), github, SIGNALS, 2)] == ["rich", "fresh"]
    assert chosen[0].evidences == {"Pandas", "SQL"} and chosen[0].default_branch == "main"


def test_forks_failed_and_uncounted_repos_are_never_considered():
    repos = [repo_record("ok"), repo_record("fork", fork=True), repo_record("off", counted=False),
             {**repo_record("bad"), "error": "x"}, {"name": 5}, "junk"]
    assert [r.name for r in select.choose_repos(needs_for("Python"), github_result(repos, per_repo=[]), SIGNALS, 9)] == ["ok"]


def test_a_language_skill_is_evidenced_by_the_repo_language():
    repos = [repo_record("js", languages={"JavaScript": 10}), repo_record("py", languages={"Python": 10})]
    chosen = select.choose_repos(needs_for("Python"), github_result(repos), SIGNALS, 9)
    assert [r.name for r in chosen] == ["py", "js"] and chosen[0].evidences == {"Python"}


def test_repo_choice_tolerates_missing_scan_data():
    assert select.choose_repos(needs_for("Python"), None, SIGNALS, 5) == []
    github = {"repos": [repo_record("a")], "tech_stack": {"skills": "x", "per_repo": "y"}}
    assert [r.name for r in select.choose_repos(needs_for("Python"), github, SIGNALS, 5)] == ["a"]
    assert select.choose_repos(needs_for("Python"), {"repos": "x"}, SIGNALS, 5) == []


# --------------------------------------------------------------------------
# the tree and the text
# --------------------------------------------------------------------------
def test_filter_tree_applies_path_and_size_rules_and_ignores_non_blobs():
    entries = [tree_entry("a.py", 900), tree_entry("b.md", 900), tree_entry("tests/c.py", 900), tree_entry("d.py", 5),
               tree_entry("node_modules/e.js", 900), {"path": "dir", "type": "tree"}, {"type": "blob"}, "junk",
               {"path": "nosize.py", "type": "blob"}, tree_entry("f.py", 900)]
    assert select.filter_tree(entries) == [("a.py", 900), ("f.py", 900)] or \
        {p for p, _ in select.filter_tree(entries)} == {"a.py", "f.py"}


def test_fetch_order_is_biggest_first_then_path_and_capped():
    survivors = [("b.py", 500), ("a.py", 500), ("c.py", 900), ("d.py", 400)]
    assert select.fetch_order(survivors, 3) == ["c.py", "a.py", "b.py"]
    assert select.fetch_order(survivors, 0) == []


def test_prepare_text_drops_generated_tiny_and_unreadable_files_and_redacts():
    assert select.prepare_text("a.py", PY_ORDERS) == ("python", PY_ORDERS)
    assert select.prepare_text("a.py", "# auto-generated\n" + PY_ORDERS) is None
    assert select.prepare_text("a.py", "x = 1\n" * (config.MIN_LOGIC_LINES - 1)) is None
    assert select.prepare_text("a.py", "   \n") is None
    assert select.prepare_text("a.md", PY_ORDERS) is None
    language, text = select.prepare_text("a.py", PY_ORDERS + 'API_KEY = "abcd1234efgh5678"\n')
    assert "abcd1234efgh5678" not in text and "[REDACTED]" in text
    language, code = select.prepare_text("n.ipynb", NOTEBOOK)
    assert language == "python" and "OUTPUT-MUST-NOT-APPEAR" not in code
    assert select.prepare_text("n.ipynb", "not json") is None


# --------------------------------------------------------------------------
# ranking, tie-break, selection, reserve
# --------------------------------------------------------------------------
def candidate(path, score, signal=0, repo="r", pushed="2026-01-01T00:00:00Z", text=None):
    return select.FileCandidate(repo, path, "python", text or "import pandas as pd\nx = pd.Series()\n", score, signal,
                                pushed)


def order(skill, candidates):
    return [(c.repo, c.path) for c in select.rank_for_skill(skill, candidates, SIGNALS)]


def test_ranking_prefers_logic_then_signal_lines_then_newer_repo_then_path():
    three_signals = "import pandas as pd\na = pd.Series()\nb = pd.Series()\n"
    assert order("Pandas", [candidate("low.py", 10), candidate("high.py", 90)]) == [("r", "high.py"), ("r", "low.py")]
    assert order("Pandas", [candidate("few.py", 50), candidate("many.py", 50, text=three_signals)])[0] == ("r", "many.py")
    assert order("Pandas", [candidate("a.py", 50, repo="old", pushed="2025-01-01T00:00:00Z"),
                            candidate("b.py", 50, repo="new", pushed="2026-06-01T00:00:00Z")])[0] == ("new", "b.py")
    assert order("Pandas", [candidate("z.py", 50), candidate("a.py", 50), candidate("m.py", 50)]) == [
        ("r", "a.py"), ("r", "m.py"), ("r", "z.py")]
    assert order("Pandas", [candidate("a.py", 50, repo="b"), candidate("a.py", 50, repo="a")])[0] == ("a", "a.py")
    assert order("Pandas", [candidate("a.py", 50, pushed=None), candidate("b.py", 50)])[0] == ("r", "b.py")


def test_only_files_that_show_the_skill_are_ranked_and_only_the_top_three_kept():
    plain = candidate("plain.py", 999, text="x = 1\ny = 2\n")
    many = [candidate(f"f{i}.py", 100 - i) for i in range(6)]
    ranked = order("Pandas", [plain] + many)
    assert len(ranked) == config.TOP_FILES_PER_SKILL and ("r", "plain.py") not in ranked
    assert [p for _, p in ranked] == ["f0.py", "f1.py", "f2.py"]


def ranked_for(**per_skill):
    return {skill: [candidate(path, score) for path, score in options] for skill, options in per_skill.items()}


def test_selection_takes_the_best_file_per_skill_in_priority_order_and_never_reuses_a_file():
    needs = [select.Need("A", "must_have", True), select.Need("B", "must_have", True),
             select.Need("C", "nice_to_have", True)]
    ranked = ranked_for(A=[("x.py", 9), ("y.py", 8)], B=[("x.py", 9), ("z.py", 5)], C=[("x.py", 9)])
    result = select.select(needs, ranked)
    assert [(p.need.skill, p.file.path) for p in result.picks] == [("A", "x.py"), ("B", "z.py")]
    assert result.skipped == [{"skill": "C", "tier": "nice_to_have", "reason": "no_usable_file"}]
    assert [(p.need.skill, p.file.path) for p in result.reserve] == [("A", "y.py")]


def test_a_skill_without_files_is_skipped_as_no_usable_file():
    result = select.select([select.Need("A", "must_have", True)], {})
    assert result.picks == [] and result.skipped[0]["reason"] == "no_usable_file"


def test_at_most_five_questions_and_the_rest_are_not_enough_room_with_a_reserve():
    names = [f"S{i}" for i in range(7)]
    needs = [select.Need(n, "must_have", True) for n in names]
    ranked = {n: [candidate(f"{n}a.py", 9), candidate(f"{n}b.py", 5)] for n in names}
    result = select.select(needs, ranked)
    assert [p.need.skill for p in result.picks] == names[:5]
    assert [s["skill"] for s in result.skipped if s["reason"] == "not_enough_room"] == ["S5", "S6"]
    # reserve order: the best file of each skill left out, then each asked skill's next best, then the rest
    assert [(p.need.skill, p.file.path) for p in result.reserve] == [
        ("S5", "S5a.py"), ("S6", "S6a.py"), ("S0", "S0b.py"), ("S1", "S1b.py"), ("S2", "S2b.py"), ("S3", "S3b.py"),
        ("S4", "S4b.py"), ("S5", "S5b.py"), ("S6", "S6b.py")]


def test_determinism_the_same_input_gives_the_same_selection():
    skills, github = shop_analysis()
    needs, _ = select.candidate_skills(skills)
    candidates = [candidate(f"f{i}.py", i % 3, repo=f"r{i % 2}") for i in range(10)]
    first = [order("Pandas", list(reversed(candidates))), order("Pandas", candidates)]
    assert first[0] == first[1]
    runs = {tuple(repo.name for repo in select.choose_repos(needs, github, SIGNALS, 5)) for _ in range(5)}
    assert len(runs) == 1


# --------------------------------------------------------------------------
# the "why" sentences
# --------------------------------------------------------------------------
def test_reason_templates():
    assert reason_for("Python", "must_have", True) == (
        "Asked because the CV claims Python and this is the highest-ranked file in the repos that uses it.")
    assert reason_for("Tableau", "nice_to_have", False) == (
        "Asked because the job lists Tableau as a nice-to-have and this is the highest-ranked file in the repos "
        "that uses it.")
    assert reason_for("Docker", "must_have", False) == (
        "Asked because the job requires Docker and this is the highest-ranked file using it, although the CV "
        "does not list it.")
    assert reason_for("SQL", "nice_to_have", True).startswith("Asked because the job lists SQL as a nice-to-have")


def test_aliases_in_from_imports_count_as_usage():
    code = "from numpy import zeros as z, ones\nx = z(3)\ny = ones(2)\nz2 = 5\n"
    assert SIGNALS.count("NumPy", "python", code) == 3  # the import, and the lines using z and ones
