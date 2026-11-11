"""Tests for examples/run_jd_parse.py. main() is called directly with a fake provider; no network."""
import importlib.util
import io
import json
from pathlib import Path

import pytest

from skillverify.jd import parser as parser_module
from skillverify.jd import provider as provider_module
from skillverify.jd.growth import LEARNED_PATH, QUEUE_PATH
from skillverify.jd.provider import LLMCallError, LLMConfigError, LLMProvider

KEY = "SECRET-KEY-123"
JD = "Required: Python and Docker. Nice to have: Redis."


class FakeProvider(LLMProvider):
    name = "fake"
    model = "fake-1"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def generate_json(self, system_prompt, user_prompt):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def extraction(*items):
    return {"skills": [{"as_written": w, "name": w, "signal": s, "evidence": e or w} for w, s, e in items]}


GOOD = extraction(("Python", "required", "Required: Python and Docker."), ("Docker", "required", "Required: Python and Docker."),
                  ("Redis", "preferred", "Nice to have: Redis."))


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """The script's module, run inside a temp folder, with the parser's default paths redirected there."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(parser_module, "LEARNED_PATH", tmp_path / "skills_learned.json")
    monkeypatch.setattr(parser_module, "QUEUE_PATH", tmp_path / "queue.json")
    monkeypatch.setattr(provider_module, "load_dotenv", lambda *a, **k: None)  # a real .env must not leak in
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    path = Path(__file__).resolve().parent.parent / "examples" / "run_jd_parse.py"
    spec = importlib.util.spec_from_file_location("run_jd_parse_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(cli, argv, provider=None, stdin=None):
    seen = {}

    def factory(model):
        seen["model"] = model
        return provider if provider is not None else FakeProvider(GOOD)

    code = cli.main(argv, provider_factory=factory, stdin=stdin)
    return code, seen


def no_leaks(out):
    assert "Traceback" not in out and KEY not in out


# --------------------------------------------------------------------------
# input sources
# --------------------------------------------------------------------------
def test_file_input_writes_json_and_prints_a_summary(cli, tmp_path, capsys):
    jd_file = tmp_path / "my job.txt"
    jd_file.write_text(JD, encoding="utf-8")
    code, _ = run(cli, ["--file", str(jd_file)])
    out = capsys.readouterr().out
    assert code == 0
    saved = json.loads((tmp_path / "output" / "my_job_jdparsed.json").read_text(encoding="utf-8"))
    assert saved["status"] == "ok" and saved["job_name"] == "my_job" and saved["counts"]["must_have"] == 2
    assert "Job: my_job" in out and "Must have (2): Python, Docker" in out and "Nice to have (1): Redis" in out
    assert "0 skills auto-added, 0 flagged for review (see output/taxonomy_review_queue.json)" in out
    assert "Full JSON saved to" in out
    no_leaks(out)


def test_text_input_with_a_name(cli, tmp_path, capsys):
    code, _ = run(cli, ["--text", JD, "--name", "data_analyst"])
    assert code == 0 and (tmp_path / "output" / "data_analyst_jdparsed.json").exists()
    assert "Job: data_analyst" in capsys.readouterr().out


def test_stdin_input(cli, tmp_path, capsys):
    code, _ = run(cli, [], stdin=io.StringIO(JD))
    assert code == 0 and (tmp_path / "output" / "job_jdparsed.json").exists()
    assert "Job: job" in capsys.readouterr().out


class Terminal(io.StringIO):
    def isatty(self):
        return True


def test_no_input_at_all_is_a_friendly_failure(cli, tmp_path, capsys):
    code, seen = run(cli, [], stdin=Terminal())
    out = capsys.readouterr().out
    assert code == 1 and "No job description given" in out and seen == {} and not (tmp_path / "output").exists()


def test_default_stdin_is_used_when_none_is_passed(cli, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(JD))
    code = cli.main([], provider_factory=lambda model: FakeProvider(GOOD))
    assert code == 0 and "Job: job" in capsys.readouterr().out


def test_file_and_text_together_are_refused_by_argparse(cli, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--file", "a.txt", "--text", "b"], provider_factory=lambda m: FakeProvider())


def test_model_option_reaches_the_provider_factory(cli):
    _, seen = run(cli, ["--text", JD, "--model", "gemini-x", "--dry-run"])
    assert seen == {"model": "gemini-x"}
    _, seen = run(cli, ["--text", JD, "--dry-run"])
    assert seen == {"model": None}


def test_name_from_file_is_made_safe(cli):
    assert cli.name_from_file("dir/Data Analyst (1).txt") == "Data_Analyst__1_"
    assert cli.name_from_file("....txt") == "..."
    assert cli.name_from_file(".txt") == ".txt"  # a dotfile's whole name is its stem
    assert cli.name_from_file("日本語.txt") == "___"
    assert cli.name_from_file("x" * 100 + ".txt") == "x" * 60


# --------------------------------------------------------------------------
# --dry-run and --no-auto-add
# --------------------------------------------------------------------------
def test_dry_run_writes_nothing_at_all(cli, tmp_path, capsys):
    unknown = extraction(("Zorbify", "required", "Zorbify is required"))
    proposals = {"proposals": [{"term": "Zorbify", "is_technical_skill": True, "canonical_name": "Zorbify",
                                "category": "tools", "alias_of": None, "aliases": [], "confidence": 0.95}]}
    before = sorted(p.name for p in tmp_path.iterdir())
    code, _ = run(cli, ["--text", "Zorbify is required", "--dry-run"], FakeProvider(unknown, proposals))
    out = capsys.readouterr().out
    assert code == 0 and sorted(p.name for p in tmp_path.iterdir()) == before  # no output file, overlay or queue
    assert "Must have (1): Zorbify" in out and "1 skills auto-added, 0 flagged for review (dry run: nothing was written)" in out
    assert "Full JSON saved" not in out


def test_no_auto_add_queues_unknown_terms(cli, tmp_path, capsys):
    provider = FakeProvider(extraction(("Zorbify", "preferred", "Zorbify")))
    code, _ = run(cli, ["--text", "Zorbify is nice", "--no-auto-add"], provider)
    out = capsys.readouterr().out
    assert code == 0 and provider.calls == 1
    assert "0 skills auto-added, 1 flagged for review (see output/taxonomy_review_queue.json)" in out
    assert "not in taxonomy: Zorbify (nice_to_have): auto-add disabled" in out
    queue = json.loads((tmp_path / "queue.json").read_text(encoding="utf-8"))
    assert queue["entries"][0]["term"] == "Zorbify" and not (tmp_path / "skills_learned.json").exists()


def test_summary_shows_rejected_truncated_and_notes(cli, capsys):
    long_jd = "Python is used. " + "x" * 31000
    provider = FakeProvider(extraction(("Python", "none", None), ("Rust", "none", None)))
    code, _ = run(cli, ["--text", long_jd, "--dry-run"], provider)
    out = capsys.readouterr().out
    assert code == 0 and "rejected: Rust: not_in_jd" in out and "too long" in out
    (Path("queue.json")).write_text("{corrupt", encoding="utf-8")
    provider = FakeProvider(extraction(("Zorbify", "none", None)))
    code, _ = run(cli, ["--text", "Zorbify here", "--no-auto-add"], provider)
    assert code == 0 and "Note: The review queue could not be updated" in capsys.readouterr().out


# --------------------------------------------------------------------------
# failures and exit codes (never a traceback, never the key)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("answers, expected", [
    ([LLMCallError("network down")], "llm_error"),
    ([{"nothing": 1}], "parse_error"),
])
def test_failed_parses_exit_with_1(cli, tmp_path, capsys, answers, expected):
    code, _ = run(cli, ["--text", JD], FakeProvider(*answers))
    out = capsys.readouterr().out
    assert code == 1 and expected in out and not (tmp_path / "output").exists()
    no_leaks(out)


def test_invalid_name_exits_with_1(cli, capsys):
    code, _ = run(cli, ["--text", JD, "--name", "bad name"])
    out = capsys.readouterr().out
    assert code == 1 and "invalid_input" in out


@pytest.mark.parametrize("text", ["", "   \n"])
def test_empty_text_exits_with_1_before_any_provider_is_created(cli, capsys, text):
    code, seen = run(cli, ["--text", text])
    assert code == 1 and seen == {} and "empty" in capsys.readouterr().out


def test_unreadable_file_exits_with_1(cli, tmp_path, capsys):
    code, seen = run(cli, ["--file", str(tmp_path / "does_not_exist.txt")])
    assert code == 1 and seen == {} and "Could not read the file" in capsys.readouterr().out
    (tmp_path / "bad.txt").write_bytes(b"\xff\xfe\x00bad")
    assert run(cli, ["--file", str(tmp_path / "bad.txt")])[0] == 1
    assert run(cli, ["--file", str(tmp_path)])[0] == 1  # a directory


def test_missing_api_key_is_exit_2_with_a_friendly_message(cli, capsys):
    code = cli.main(["--text", JD, "--dry-run"])  # the real provider factory, with no key anywhere
    out = capsys.readouterr().out
    assert code == 2 and "Configuration problem" in out and "GEMINI_API_KEY" in out
    no_leaks(out)


def test_default_factory_builds_a_gemini_provider(cli, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    provider = cli.default_provider("some-model")
    assert provider.name == "gemini" and provider.model == "some-model" and KEY not in repr(provider)


def test_config_error_during_the_parse_is_exit_2(cli, capsys):
    code, _ = run(cli, ["--text", JD, "--dry-run"], FakeProvider(LLMConfigError("key problem")))
    assert code == 2 and "key problem" in capsys.readouterr().out


def test_corrupt_overlay_is_exit_2(cli, tmp_path, capsys):
    (tmp_path / "skills_learned.json").write_text("{corrupt", encoding="utf-8")
    code, _ = run(cli, ["--text", JD])
    out = capsys.readouterr().out
    assert code == 2 and "Taxonomy problem" in out
    no_leaks(out)


def test_unexpected_errors_print_only_the_type(cli, capsys):
    code, _ = run(cli, ["--text", JD], FakeProvider(RuntimeError(f"boom {KEY}")))
    out = capsys.readouterr().out
    assert code == 1 and "Unexpected problem (RuntimeError)" in out
    no_leaks(out)
    assert "boom" not in out


# --------------------------------------------------------------------------
# the sample files and the real paths
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["data_analyst.txt", "backend_developer.txt", "ambiguous.txt"])
def test_sample_jds_exist_and_are_realistic(name):
    text = (Path(__file__).resolve().parent.parent / "examples" / "sample_jds" / name).read_text(encoding="utf-8")
    assert 12 <= len(text.splitlines()) <= 40 and "Python" in text


def test_the_real_overlay_and_queue_were_never_created():
    assert not LEARNED_PATH.exists() and not QUEUE_PATH.exists()
