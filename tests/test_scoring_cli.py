"""Tests for examples/run_score.py. main(argv) is called directly in a temp folder; offline; fictional files."""
import importlib.util
import json
from pathlib import Path

import pytest

from skillverify.jd import growth
from skillverify.jd.growth import LEARNED_PATH, QUEUE_PATH

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
PRIVATE = {"name": "Jane Private", "emails": ["jane.private@example.com"],
           "phones": [{"as_written": "+1 (555) 010-2345", "normalized": "+15550102345"}]}


def item(skill, confidence="medium"):
    return {"skill": skill, "label": skill, "in_taxonomy": True, "category": "tools", "confidence": confidence,
            "evidence_types": ["file_signal"], "repos": ["a"], "examples": []}


JD = {"schema_version": 1, "status": "ok", "job_name": "backend", "unmapped": [], "skills": [
    {"skill": n, "category": "tools", "tier": t, "tier_basis": "explicit_cue"}
    for n, t in (("Python", "must_have"), ("SQL", "must_have"), ("Docker", "must_have"), ("Git", "nice_to_have"),
                 ("Tableau", "nice_to_have"))]}
CV = {"schema_version": 1, "status": "ok", "label": "cv_one", "warnings": [], "source": {"truncated": False},
      "candidate": PRIVATE, "github": {"status": "found", "username": "ghost", "candidates": ["ghost"]},
      "skills": [{"skill": n} for n in ("Python", "SQL", "Git", "Tableau")]}
GH = {"username": "ghost", "status": "ok",
      "summary": {"counted_repos": 3, "total_commits_in_window": 40, "active_repos_in_window": 2,
                  "languages_combined": {"Python": 9000, "HTML": 100}},
      "tech_stack": {"status": "ok", "complete": True, "repos_scanned": 3, "skills": [item("Git")]}}


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(growth, "LEARNED_PATH", tmp_path / "no_learned_skills.json")
    spec = importlib.util.spec_from_file_location("run_score_under_test", EXAMPLES / "run_score.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def files(tmp_path):
    paths = {}
    for key, data in (("jd", JD), ("cv", CV), ("gh", GH)):
        paths[key] = tmp_path / f"{key}.json"
        paths[key].write_text(json.dumps(data), encoding="utf-8")
    return paths


def args(files, *extra, github=True):
    base = ["--jd", str(files["jd"]), "--cv", str(files["cv"])]
    if github:
        base += ["--github", str(files["gh"])]
    return base + list(extra)


def no_personal_data(out):
    for secret in ("Jane", "jane.private", "555", "Traceback", "GEMINI", "KEY"):
        assert secret not in out, secret


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------
def test_scores_and_writes_the_default_file(cli, files, tmp_path, capsys):
    assert cli.main(args(files)) == 0
    out = capsys.readouterr().out
    saved = json.loads((tmp_path / "output" / "cv_one__backend_score.json").read_text(encoding="utf-8"))
    assert saved["status"] == "ok" and saved["score"]["repo_fingerprint"] == 54.5
    assert "Job: backend   CV: cv_one   GitHub: ghost" in out and "Status: ok" in out
    assert "Repo Fingerprint Score: 54.5" in out and "claim coverage 72.7" in out
    assert "checkable only 55.0" in out and "verifiable share 90.9" in out and "Points: 6.0 of 11.0" in out
    assert "JD skills: 5 (3 must have, 2 nice to have)   claimed: 4   confirmed: 2   unconfirmed: 1" in out
    assert "Evidence for the interviewer, not a hiring decision." in out and "Full JSON saved to" in out
    no_personal_data(out)
    no_personal_data(json.dumps(saved))


def test_dry_run_writes_nothing(cli, files, tmp_path, capsys):
    before = sorted(p.name for p in tmp_path.iterdir())
    assert cli.main(args(files, "--dry-run")) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == before and "Full JSON saved" not in capsys.readouterr().out


def test_out_option(cli, files, tmp_path, capsys):
    assert cli.main(args(files, "--out", str(tmp_path / "result.json"))) == 0
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))["cv_label"] == "cv_one"
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("bad", ["result.txt", "result", "result.json.exe", "result.js"])
def test_out_must_end_in_json(cli, files, tmp_path, capsys, bad):
    assert cli.main(args(files, "--out", str(tmp_path / bad))) == 1
    assert "must end in .json" in capsys.readouterr().out and not (tmp_path / bad).exists()


def test_sensitivity_table(cli, files, capsys):
    assert cli.main(args(files, "--sensitivity", "--dry-run")) == 0
    out = capsys.readouterr().out
    assert "Ratio check (must-have : nice-to-have):" in out
    for line in ("1:1   60.0", "2:1   56.3", "3:1   54.5", "4:1   53.6", "5:1   52.9"):
        assert line in out
    cli.main(args(files, "--dry-run"))
    assert "Ratio check" not in capsys.readouterr().out


def test_weight_options_apply_to_this_run_only(cli, files, tmp_path, capsys):
    assert cli.main(args(files, "--must-weight", "2", "--nice-weight", "1", "--dry-run")) == 0
    assert "Repo Fingerprint Score: 56.3" in capsys.readouterr().out
    assert cli.main(args(files, "--dry-run")) == 0 and "Repo Fingerprint Score: 54.5" in capsys.readouterr().out
    assert cli.main(args(files, "--nice-weight", "3", "--dry-run")) == 0
    assert "Repo Fingerprint Score: 54.5" not in capsys.readouterr().out


@pytest.mark.parametrize("flag, value", [("--must-weight", "0"), ("--must-weight", "-1"), ("--nice-weight", "0"),
                                         ("--must-weight", "nan"), ("--nice-weight", "inf"), ("--must-weight", "5000")])
def test_bad_weights_are_exit_2(cli, files, tmp_path, capsys, flag, value):
    assert cli.main(args(files, flag, value)) == 2
    out = capsys.readouterr().out
    assert "Configuration problem" in out and "Traceback" not in out and not (tmp_path / "output").exists()


def test_github_mismatch_and_the_override(cli, files, tmp_path, capsys):
    other = dict(GH, username="someone-else")
    files["gh"].write_text(json.dumps(other), encoding="utf-8")
    assert cli.main(args(files, "--dry-run")) == 1
    out = capsys.readouterr().out
    assert "Status: github_mismatch" in out and "Repo Fingerprint Score: n/a" in out and "claim coverage 72.7" in out
    assert cli.main(args(files, "--allow-github-mismatch", "--dry-run")) == 0
    out = capsys.readouterr().out
    assert "Repo Fingerprint Score: 54.5" in out and "Flags: github_username_mismatch" in out


def test_other_statuses_exit_1_and_still_write_the_partial_result(cli, files, tmp_path, capsys):
    assert cli.main(args(files, github=False)) == 1
    out = capsys.readouterr().out
    assert "Status: no_github_evidence" in out and "Flags: no_github_file" in out
    saved = json.loads((tmp_path / "output" / "cv_one__backend_score.json").read_text(encoding="utf-8"))
    assert saved["score"]["repo_fingerprint"] is None and saved["score"]["claim_coverage"] == 72.7


def test_hidden_strengths_and_flags_are_printed(cli, files, tmp_path, capsys):
    gh = dict(GH, summary=dict(GH["summary"], languages_combined={"Python": 9000, "Dockerfile": 500}))
    gh["tech_stack"] = dict(GH["tech_stack"], complete=False)
    files["gh"].write_text(json.dumps(gh), encoding="utf-8")
    assert cli.main(args(files, "--dry-run")) == 0
    out = capsys.readouterr().out
    assert "Hidden strengths (on GitHub, not claimed on the CV): Docker (strong)" in out
    assert "Flags: scan_incomplete" in out


def test_default_file_name_uses_the_validated_names(cli, files, tmp_path, capsys):
    jd = dict(JD, job_name="my_job-2.v1")
    cv = dict(CV, label="Cand.01")
    files["jd"].write_text(json.dumps(jd), encoding="utf-8")
    files["cv"].write_text(json.dumps(cv), encoding="utf-8")
    assert cli.main(args(files)) == 0
    assert (tmp_path / "output" / "Cand.01__my_job-2.v1_score.json").exists()


@pytest.mark.parametrize("field, value", [("job_name", "../escape"), ("job_name", "a b"), ("job_name", "x" * 61)])
def test_bad_names_are_rejected_before_any_file_name_is_built(cli, files, tmp_path, capsys, field, value):
    files["jd"].write_text(json.dumps(dict(JD, **{field: value})), encoding="utf-8")
    assert cli.main(args(files)) == 1
    out = capsys.readouterr().out
    assert "Status: invalid_input" in out and "invalid job_name" in out
    assert not (tmp_path / "output").exists() and not (tmp_path.parent / "escape_score.json").exists()
    no_personal_data(out)


def test_bad_cv_label(cli, files, tmp_path, capsys):
    files["cv"].write_text(json.dumps(dict(CV, label="../../x")), encoding="utf-8")
    assert cli.main(args(files)) == 1
    assert "invalid label" in capsys.readouterr().out and not (tmp_path / "output").exists()


def test_corrupt_overlay_is_exit_2(cli, files, tmp_path, capsys, monkeypatch):
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{corrupt", encoding="utf-8")
    monkeypatch.setattr(growth, "LEARNED_PATH", corrupt)
    assert cli.main(args(files)) == 2
    out = capsys.readouterr().out
    assert "Taxonomy problem" in out and "Traceback" not in out and not (tmp_path / "output").exists()


# --------------------------------------------------------------------------
# unreadable input
# --------------------------------------------------------------------------
@pytest.mark.parametrize("which", ["jd", "cv", "github"])
def test_missing_file(cli, files, tmp_path, capsys, which):
    argv = args(files)
    argv[argv.index("--" + which) + 1] = str(tmp_path / "does_not_exist.json")
    assert cli.main(argv) == 1
    out = capsys.readouterr().out
    assert "was not found" in out and "Traceback" not in out and not (tmp_path / "output").exists()


def test_directory_corrupt_list_and_oversize_files(cli, files, tmp_path, capsys):
    (tmp_path / "folder").mkdir()
    assert cli.main(["--jd", str(tmp_path / "folder"), "--cv", str(files["cv"])]) == 1
    assert "not found (or is not a file)" in capsys.readouterr().out
    files["jd"].write_text("{corrupt", encoding="utf-8")
    assert cli.main(args(files)) == 1
    assert "could not be read as JSON" in capsys.readouterr().out
    files["jd"].write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert cli.main(args(files)) == 1
    assert "must contain a JSON object" in capsys.readouterr().out
    files["jd"].write_text(json.dumps(JD), encoding="utf-8")
    files["cv"].write_text(" " * (cli.MAX_INPUT_BYTES + 1), encoding="utf-8")
    assert cli.main(args(files)) == 1
    assert "larger than 5 MB" in capsys.readouterr().out
    files["cv"].write_bytes(b"\xff\xfe\x00bad bytes")
    assert cli.main(args(files)) == 1
    assert "could not be read as JSON" in capsys.readouterr().out


def test_non_ok_parse_results_are_exit_1(cli, files, tmp_path, capsys):
    files["cv"].write_text(json.dumps(dict(CV, status="llm_error")), encoding="utf-8")
    assert cli.main(args(files)) == 1
    out = capsys.readouterr().out
    assert "Status: invalid_input" in out and "CV parse result is not usable (status=llm_error)" in out
    assert not (tmp_path / "output").exists()


def test_no_jd_skills_is_exit_1(cli, files, capsys):
    files["jd"].write_text(json.dumps(dict(JD, skills=[])), encoding="utf-8")
    assert cli.main(args(files, "--dry-run")) == 1
    assert "Status: no_jd_skills" in capsys.readouterr().out


def test_the_script_needs_no_network_or_key(cli, files, monkeypatch, capsys):
    import socket

    def blocked(*a, **k):
        raise AssertionError("the scoring script must not use the network")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert cli.main(args(files, "--dry-run", "--sensitivity")) == 0


def test_the_real_overlay_and_queue_were_never_created():
    assert not LEARNED_PATH.exists() and not QUEUE_PATH.exists()
