"""Edge-case tests for the Repo Fingerprint Score: bad inputs, every GitHub-evidence path, config errors. Offline."""
import copy
import json
import math

import pytest

from skillverify.jd import growth
from skillverify.scoring import ScoringConfigError, score_candidate
from skillverify.scoring import score as S
from skillverify.taxonomy import TaxonomyError

TOP_KEYS = {"schema_version", "status", "message", "generated_at", "job_name", "cv_label", "weights", "github", "score",
            "skills", "hidden_strengths", "cv_extra_skills", "jd_unmapped_terms", "cv_warnings", "flags", "sensitivity",
            "counts"}


@pytest.fixture(autouse=True)
def isolated_overlay(tmp_path, monkeypatch):
    monkeypatch.setattr(growth, "LEARNED_PATH", tmp_path / "no_learned_skills.json")


def make_jd(skills=(("Python", "must_have"), ("SQL", "must_have"), ("Docker", "must_have"), ("Git", "nice_to_have"),
                    ("Tableau", "nice_to_have")), job_name="backend", **over):
    result = {"schema_version": 1, "status": "ok", "job_name": job_name, "unmapped": [],
              "skills": [{"skill": n, "category": "tools", "tier": t, "tier_basis": "explicit_cue"} for n, t in skills]}
    result.update(over)
    return result


def make_cv(claimed=("Python", "SQL", "Git", "Tableau"), label="cv_one", username="ghost", candidates=None,
            warnings=(), truncated=False, **over):
    result = {"schema_version": 1, "status": "ok", "label": label, "warnings": list(warnings),
              "source": {"format": "text", "truncated": truncated},
              "github": {"status": "found" if username else "none", "username": username,
                         "candidates": [username] if candidates is None and username else (candidates or [])},
              "skills": [{"skill": n} for n in claimed]}
    result.update(over)
    return result


def item(skill, confidence="medium"):
    return {"skill": skill, "label": skill, "in_taxonomy": True, "category": "tools", "confidence": confidence,
            "evidence_types": ["file_signal"], "repos": ["a"], "examples": []}


def make_gh(username="ghost", languages=None, items=(item("Git"),), complete=True, tech="ok", counted=3, status="ok"):
    result = {"username": username, "status": status,
              "summary": {"counted_repos": counted, "total_commits_in_window": 40, "active_repos_in_window": 2,
                          "languages_combined": languages if languages is not None else {"Python": 9000, "HTML": 100}}}
    if tech is not None:
        result["tech_stack"] = {"status": tech, "complete": complete, "repos_scanned": 3, "skills": list(items)}
    return result


# --------------------------------------------------------------------------
# invalid inputs
# --------------------------------------------------------------------------
def assert_invalid(result, fragment):
    assert result["status"] == "invalid_input" and fragment in result["message"]
    assert set(result) == TOP_KEYS and result["score"]["repo_fingerprint"] is None
    assert result["skills"] == [] and result["flags"] == []
    json.dumps(result)


@pytest.mark.parametrize("bad", [None, [], "text", 5, True])
def test_a_jd_or_cv_that_is_not_a_dict(bad):
    assert_invalid(score_candidate(bad, make_cv(), make_gh()), "JD result is not a JSON object")
    assert_invalid(score_candidate(make_jd(), bad, make_gh()), "CV result is not a JSON object")


@pytest.mark.parametrize("version", [2, 0, True, "1", None, 1.0])
def test_wrong_schema_version(version):
    assert_invalid(score_candidate(make_jd(schema_version=version), make_cv(), make_gh()), "schema_version")
    assert_invalid(score_candidate(make_jd(), make_cv(schema_version=version), make_gh()), "schema_version")


def test_a_result_with_a_missing_schema_version():
    jd = make_jd()
    del jd["schema_version"]
    assert_invalid(score_candidate(jd, make_cv(), make_gh()), "schema_version")


@pytest.mark.parametrize("status", ["llm_error", "parse_error", "empty_jd", None, 5])
def test_non_ok_parse_results(status):
    jd_result = score_candidate(make_jd(status=status), make_cv(), make_gh())
    assert_invalid(jd_result, "JD parse result is not usable")
    assert_invalid(score_candidate(make_jd(), make_cv(status=status), make_gh()), "CV parse result is not usable")
    if status == "llm_error":
        assert "status=llm_error" in jd_result["message"]


def test_bad_skills_lists_and_entries():
    for bad in (None, "x", {"a": 1}):
        jd, cv = make_jd(), make_cv()
        jd["skills"], cv["skills"] = bad, bad
        assert_invalid(score_candidate(jd, make_cv(), make_gh()), "no skills list")
        assert_invalid(score_candidate(make_jd(), cv, make_gh()), "no skills list")
    for entry in ("text", None, {}, {"skill": 5, "tier": "must_have"}, {"skill": "  ", "tier": "must_have"}):
        jd, cv = make_jd(), make_cv()
        jd["skills"], cv["skills"] = [entry], [entry]
        assert_invalid(score_candidate(jd, make_cv(), make_gh()), "no skill name")
        assert_invalid(score_candidate(make_jd(), cv, make_gh()), "no skill name")


@pytest.mark.parametrize("tier", ["required", "must", None, 3, "MUST_HAVE"])
def test_bad_tier(tier):
    jd = make_jd()
    jd["skills"][0]["tier"] = tier
    assert_invalid(score_candidate(jd, make_cv(), make_gh()), "invalid tier")


@pytest.mark.parametrize("name", ["", "bad name", "a" * 61, "../x", "jane/doe", None, 5, "x\n"])
def test_bad_names_that_would_go_into_a_file_name(name):
    assert_invalid(score_candidate(make_jd(job_name=name), make_cv(), make_gh()), "invalid job_name")
    assert_invalid(score_candidate(make_jd(), make_cv(label=name), make_gh()), "invalid label")


def test_good_names():
    for name in ("a", "My_job-2.v1", "x" * 60):
        result = score_candidate(make_jd(job_name=name), make_cv(label=name), make_gh())
        assert result["status"] == "ok" and result["job_name"] == name and result["cv_label"] == name


def test_invalid_input_messages_carry_no_data_from_the_files():
    secret = "Jane Private jane@example.com"
    result = score_candidate(make_jd(), make_cv(label=secret), make_gh())
    assert secret not in json.dumps(result) and result["cv_label"] == ""
    result = score_candidate(make_jd(status=secret), make_cv(), make_gh())
    assert "jane@example.com" not in result["message"] or len(result["message"]) < 120


def test_no_jd_skills():
    result = score_candidate(make_jd(()), make_cv(), make_gh())
    assert result["status"] == "no_jd_skills" and result["score"]["repo_fingerprint"] is None
    assert result["score"]["claim_coverage"] is None and "no_must_have_skills" in result["flags"]
    assert result["skills"] == [] and result["job_name"] == "backend"


def test_no_must_have_flag_does_not_stop_scoring():
    result = score_candidate(make_jd((("Git", "nice_to_have"), ("Python", "nice_to_have"))), make_cv(), make_gh())
    assert result["status"] == "ok" and "no_must_have_skills" in result["flags"]
    assert result["score"]["repo_fingerprint"] == 100.0


def test_duplicate_jd_skills_keep_one_entry_and_the_higher_tier():
    duplicate_first_nice = make_jd((("Python", "nice_to_have"), ("SQL", "must_have"), ("Python", "must_have"),
                                    ("Python", "nice_to_have")))
    result = score_candidate(duplicate_first_nice, make_cv(("Python", "SQL")), make_gh())
    by = {r["skill"]: r for r in result["skills"]}
    assert [r["skill"] for r in result["skills"]] == ["Python", "SQL"] and by["Python"]["tier"] == "must_have"
    assert by["Python"]["weight"] == 3 and result["counts"]["jd_skills"] == 2
    kept = score_candidate(make_jd((("Python", "must_have"), ("Python", "nice_to_have"))), make_cv(("Python",)), make_gh())
    assert [r["tier"] for r in kept["skills"]] == ["must_have"]


# --------------------------------------------------------------------------
# which GitHub evidence is usable
# --------------------------------------------------------------------------
def unusable(result, status, fragment=""):
    assert result["status"] == status and fragment in result["message"]
    assert result["score"]["repo_fingerprint"] is None and result["score"]["checkable_only"] is None
    assert result["score"]["verifiable_share"] is None
    assert result["score"]["claim_coverage"] == 72.7            # needs no GitHub data
    by = {r["skill"]: r for r in result["skills"]}
    assert by["Python"]["claimed"] is True and by["Python"]["credit"] is None and by["Python"]["points"] is None
    assert by["Python"]["reason"] == "no_github_evidence" and by["Python"]["weight"] == 3
    assert by["Docker"]["claimed"] is False and by["Docker"]["credit"] == 0.0 and by["Docker"]["reason"] == "not_claimed"
    assert result["score"]["points_earned"] == 0.0 and result["score"]["points_possible"] == 11.0
    assert result["hidden_strengths"] == [] and result["counts"]["confirmed"] == 0
    json.dumps(result)


def test_no_github_file():
    result = score_candidate(make_jd(), make_cv(), None)
    unusable(result, "no_github_evidence", "No GitHub evidence file")
    assert result["flags"] == ["no_github_file"] and result["github"]["cv_username"] == "ghost"


def test_user_not_found_is_neutral():
    gh = {"username": "ghost", "status": "user_not_found", "message": "x", "summary": {}}
    result = score_candidate(make_jd(), make_cv(), gh)
    unusable(result, "github_user_not_found", "could not be found")
    for accusation in ("fake", "lied", "fraud", "false"):
        assert accusation not in result["message"].lower()
    assert result["github"]["validator_status"] == "user_not_found"


@pytest.mark.parametrize("gh", [
    {"status": "rate_limited", "summary": {}}, {"status": "error", "summary": {}}, {"status": "weird"}, {},
    {"status": "ok"}, {"status": "ok", "summary": "x"}, ["not", "a", "dict"], "text", 5,
])
def test_github_unavailable(gh):
    unusable(score_candidate(make_jd(), make_cv(), gh), "github_unavailable")


@pytest.mark.parametrize("gh", [
    {"username": "ghost", "status": "no_public_repos", "summary": {}},
    make_gh(counted=0),
])
def test_no_repositories_to_check(gh):
    unusable(score_candidate(make_jd(), make_cv(), gh), "no_github_evidence")


def test_failed_status_inside_a_scored_run_still_fills_the_block():
    result = score_candidate(make_jd(), make_cv(), {"username": "ghost", "status": "rate_limited", "summary": {"counted_repos": 4}})
    assert result["github"]["username"] == "ghost" and result["github"]["counted_repos"] == 4


# --------------------------------------------------------------------------
# the evidence must belong to the account named in the CV
# --------------------------------------------------------------------------
def test_matching_username_is_case_insensitive():
    result = score_candidate(make_jd(), make_cv(username="GhOsT"), make_gh(username="gHoSt"))
    assert result["status"] == "ok" and result["github"]["username_matches_cv"] is True and result["flags"] == []
    assert result["github"]["username"] == "ghost" and result["github"]["cv_username"] == "ghost"


def test_a_cv_candidate_match_is_ok_with_a_flag():
    cv = make_cv(username="other-name", candidates=["other-name", "Ghost"])
    result = score_candidate(make_jd(), cv, make_gh(username="ghost"))
    assert result["status"] == "ok" and "github_ambiguous_in_cv" in result["flags"]
    assert result["github"]["username_matches_cv"] is True


def test_mismatch_is_not_scored():
    result = score_candidate(make_jd(), make_cv(username="alice-dev"), make_gh(username="ghost"))
    assert result["status"] == "github_mismatch" and result["score"]["repo_fingerprint"] is None
    assert result["github"]["username_matches_cv"] is False and result["score"]["claim_coverage"] == 72.7
    assert "does not belong" in result["message"]


def test_a_cv_without_a_username_is_a_mismatch():
    result = score_candidate(make_jd(), make_cv(username=None), make_gh())
    assert result["status"] == "github_mismatch" and result["github"]["username_matches_cv"] is None
    assert result["github"]["cv_username"] is None


def test_allow_mismatch_scores_with_a_flag():
    wrong = score_candidate(make_jd(), make_cv(username="alice-dev"), make_gh(), allow_github_mismatch=True)
    assert wrong["status"] == "ok" and wrong["score"]["repo_fingerprint"] == 54.5
    assert "github_username_mismatch" in wrong["flags"] and wrong["github"]["username_matches_cv"] is False
    none = score_candidate(make_jd(), make_cv(username=None), make_gh(), allow_github_mismatch=True)
    assert none["status"] == "ok" and "github_supplied_without_cv_link" in none["flags"]
    assert "github_username_mismatch" not in none["flags"] and none["github"]["username_matches_cv"] is None


def test_an_evidence_username_that_is_not_a_valid_username_never_matches():
    result = score_candidate(make_jd(), make_cv(), make_gh(username="../../etc"))
    assert result["status"] == "github_mismatch" and result["github"]["username"] is None
    assert score_candidate(make_jd(), make_cv(), make_gh(username=None))["status"] == "github_mismatch"


def test_a_malformed_cv_github_block_means_no_username():
    cv = make_cv()
    for bad in (None, "ghost", [], {"username": 5, "candidates": "ghost"}):
        cv["github"] = bad
        assert score_candidate(make_jd(), cv, make_gh())["status"] == "github_mismatch"
    cv["github"] = {"username": "ghost", "candidates": [5, None, "  ", "ghost"]}
    assert score_candidate(make_jd(), cv, make_gh())["status"] == "ok"


# --------------------------------------------------------------------------
# the technology scan
# --------------------------------------------------------------------------
@pytest.mark.parametrize("tech", [None, "skipped", "error"])
def test_missing_skipped_or_failed_tech_scan(tech):
    result = score_candidate(make_jd(), make_cv(), make_gh(tech=tech))
    assert result["status"] == "ok" and "tech_scan_missing" in result["flags"] and "scan_incomplete" in result["flags"]
    by = {r["skill"]: r for r in result["skills"]}
    assert by["Python"]["reason"] == "confirmed"              # language statistics still count
    assert by["SQL"]["reason"] == "scan_incomplete" and by["SQL"]["credit"] == 0.5
    assert by["Docker"]["reason"] == "not_claimed" and by["Git"]["reason"] == "not_detectable"
    assert result["github"]["scan_complete"] is (None if tech is None else False)


def test_a_malformed_tech_stack_counts_as_missing():
    gh = make_gh()
    gh["tech_stack"] = "oops"
    result = score_candidate(make_jd(), make_cv(), gh)
    assert result["status"] == "ok" and "tech_scan_missing" in result["flags"]
    assert result["github"]["tech_scan_status"] is None and result["github"]["repos_scanned"] is None


def test_an_incomplete_scan_never_lowers_a_claimed_skill_below_the_unconfirmed_credit():
    jd = make_jd((("SQL", "must_have"), ("Django", "must_have"), ("Docker", "must_have"), ("Git", "nice_to_have")))
    result = score_candidate(jd, make_cv(("SQL", "Django", "Docker", "Git")), make_gh(complete=False, items=()))
    for row in result["skills"]:
        assert row["claimed"] and row["credit"] >= 0.5, row
    assert "scan_incomplete" in result["flags"] and result["score"]["repo_fingerprint"] == 50.0
    unclaimed = score_candidate(jd, make_cv(()), make_gh(complete=False, items=()))
    assert unclaimed["score"]["repo_fingerprint"] == 0.0  # only a skill the CV does not claim scores 0


def test_a_partial_scan_is_incomplete_but_not_missing():
    gh = make_gh(complete=False, tech="partial")
    result = score_candidate(make_jd(), make_cv(), gh)
    assert "scan_incomplete" in result["flags"] and "tech_scan_missing" not in result["flags"]
    assert result["github"]["tech_scan_status"] == "partial"


# --------------------------------------------------------------------------
# verifiable share and flags
# --------------------------------------------------------------------------
def test_mostly_unverifiable():
    jd = make_jd((("Tableau", "must_have"), ("Machine Learning", "must_have"), ("Python", "nice_to_have")))
    result = score_candidate(jd, make_cv(("Tableau", "Machine Learning", "Python")), make_gh())
    assert result["score"]["verifiable_share"] == 14.3 and "mostly_unverifiable" in result["flags"]
    fine = score_candidate(make_jd(), make_cv(), make_gh())
    assert fine["score"]["verifiable_share"] == 90.9 and "mostly_unverifiable" not in fine["flags"]


def test_nothing_checkable_gives_no_github_score(monkeypatch):
    monkeypatch.setattr(S, "NOT_DETECTABLE_CREDIT", None)
    jd = make_jd((("Tableau", "must_have"), ("Machine Learning", "nice_to_have")))
    result = score_candidate(jd, make_cv(("Tableau", "Machine Learning")), make_gh())
    assert result["status"] == "ok" and result["score"]["repo_fingerprint"] is None
    assert "No skill of this job can be checked on GitHub" in result["message"]
    assert result["score"]["claim_coverage"] == 100.0
    assert result["score"]["verifiable_share"] == 0.0 and result["score"]["checkable_only"] is None


def test_cv_flags_have_no_numeric_effect():
    plain = score_candidate(make_jd(), make_cv(), make_gh())
    flagged = score_candidate(make_jd(), make_cv(warnings=["many_skills_little_context", "truncated"], truncated=True), make_gh())
    assert flagged["score"] == plain["score"]
    assert {"cv_keyword_stuffing_signal", "cv_truncated"} <= set(flagged["flags"])
    assert flagged["cv_warnings"] == ["many_skills_little_context", "truncated"]


def test_cv_warnings_are_copied_defensively():
    odd = make_cv(warnings=["ok", 5, None, "x" * 300])
    assert score_candidate(make_jd(), odd, make_gh())["cv_warnings"] == ["ok", "x" * 100]
    many = make_cv(warnings=[f"w{n}" for n in range(50)])
    assert len(score_candidate(make_jd(), many, make_gh())["cv_warnings"]) == 30
    for bad in (None, "text"):
        cv = make_cv()
        cv["warnings"] = bad
        assert score_candidate(make_jd(), cv, make_gh())["cv_warnings"] == []


def test_a_cv_source_that_is_not_a_dict_is_ignored():
    assert "cv_truncated" not in score_candidate(make_jd(), make_cv(source="x"), make_gh())["flags"]


def test_jd_unmapped_terms_are_counted_flagged_and_not_scored():
    plain = score_candidate(make_jd(), make_cv(), make_gh())
    result = score_candidate(make_jd(unmapped=[{"term": "Zorbify"}, {"term": "Quuxify"}]), make_cv(), make_gh())
    assert result["jd_unmapped_terms"] == 2 and "jd_has_unmapped_terms" in result["flags"]
    assert result["score"] == plain["score"]
    assert score_candidate(make_jd(unmapped="x"), make_cv(), make_gh())["jd_unmapped_terms"] == 0


# --------------------------------------------------------------------------
# configuration errors
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["must_have", "nice_to_have"])
@pytest.mark.parametrize("bad", [True, False, 0, -1, math.nan, math.inf, -math.inf, "3", None, [3], 1001, 1e9])
def test_bad_weights(name, bad):
    with pytest.raises(ScoringConfigError, match=name):
        score_candidate(make_jd(), make_cv(), make_gh(), weights={name: bad})


@pytest.mark.parametrize("name", ["credit_confirmed", "credit_claimed_unconfirmed", "credit_not_claimed",
                                  "weak_evidence_credit", "not_detectable_credit"])
@pytest.mark.parametrize("bad", [True, 1.01, -0.1, math.nan, math.inf, "0.5", [0.5], 2])
def test_bad_credits(name, bad):
    with pytest.raises(ScoringConfigError, match=name):
        score_candidate(make_jd(), make_cv(), make_gh(), weights={name: bad})


def test_good_edge_values_are_accepted():
    ok = score_candidate(make_jd(), make_cv(), make_gh(), weights={
        "must_have": 1000, "nice_to_have": 0.001, "credit_confirmed": 1, "credit_claimed_unconfirmed": 0,
        "credit_not_claimed": 1, "weak_evidence_credit": 0, "not_detectable_credit": None})
    assert ok["status"] == "ok" and ok["weights"]["not_detectable_credit"] is None and ok["weights"]["nice_to_have"] == 0.001
    assert score_candidate(make_jd(), make_cv(), make_gh(), weights={})["score"]["repo_fingerprint"] == 54.5


def test_weights_must_be_a_dict_with_known_names():
    for bad in ("3:1", [3, 1], 3):
        with pytest.raises(ScoringConfigError, match="dict"):
            score_candidate(make_jd(), make_cv(), make_gh(), weights=bad)
    with pytest.raises(ScoringConfigError, match="Unknown weight name"):
        score_candidate(make_jd(), make_cv(), make_gh(), weights={"must": 3, "nice": 1})


def test_config_errors_come_before_input_checks():
    with pytest.raises(ScoringConfigError):
        score_candidate("junk", "junk", None, weights={"must_have": 0})


@pytest.mark.parametrize("constant, bad", [
    ("MUST_HAVE_WEIGHT", 0), ("NICE_TO_HAVE_WEIGHT", -2), ("MUST_HAVE_WEIGHT", True), ("CREDIT_CONFIRMED", 1.5),
    ("CREDIT_NOT_CLAIMED", "0"), ("WEAK_EVIDENCE_CREDIT", math.nan), ("NOT_DETECTABLE_CREDIT", "none"),
])
def test_the_module_constants_are_validated(monkeypatch, constant, bad):
    monkeypatch.setattr(S, constant, bad)
    with pytest.raises(ScoringConfigError):
        score_candidate(make_jd(), make_cv(), make_gh())


def test_the_module_constants_are_honoured(monkeypatch):
    monkeypatch.setattr(S, "MUST_HAVE_WEIGHT", 2)
    monkeypatch.setattr(S, "CREDIT_CLAIMED_UNCONFIRMED", 0.25)
    result = score_candidate(make_jd(), make_cv(), make_gh())
    assert result["weights"]["must_have"] == 2 and result["weights"]["credit_claimed_unconfirmed"] == 0.25
    # Python 2 + SQL 0.5 + Docker 0 + Git 1 + Tableau 0.5 (not-detectable credit is a separate constant) = 4.0 of 8
    assert result["score"]["repo_fingerprint"] == 50.0
    assert score_candidate(make_jd(), make_cv(), make_gh(), weights={"must_have": 3})["weights"]["must_have"] == 3


def test_a_corrupt_overlay_raises_taxonomy_error(tmp_path, monkeypatch):
    corrupt = tmp_path / "corrupt_learned.json"
    corrupt.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(growth, "LEARNED_PATH", corrupt)
    with pytest.raises(TaxonomyError):
        score_candidate(make_jd(), make_cv(), make_gh())
    # inputs that never reach the taxonomy do not touch the overlay
    assert score_candidate(make_jd(), make_cv(), None)["status"] == "no_github_evidence"


def test_a_learned_skill_in_the_overlay_is_known(tmp_path, monkeypatch):
    overlay = tmp_path / "learned.json"
    overlay.write_text(json.dumps({"skills": [{"id": "Zorbify", "category": "tools", "aliases": []}], "aliases": []}),
                       encoding="utf-8")
    monkeypatch.setattr(growth, "LEARNED_PATH", overlay)
    jd = make_jd((("Zorbify", "must_have"),))
    result = score_candidate(jd, make_cv(("Zorbify",)), make_gh())
    row = result["skills"][0]
    assert row["github_status"] == "not_detectable_from_github" and row["reason"] == "not_detectable"  # no GitHub signals
    assert row["credit"] == 0.5


def test_inputs_survive_a_run_unchanged():
    jd, cv, gh = make_jd(), make_cv(), make_gh()
    snapshot = copy.deepcopy((jd, cv, gh))
    score_candidate(jd, cv, gh, weights={"must_have": 2}, allow_github_mismatch=True)
    assert (jd, cv, gh) == snapshot
