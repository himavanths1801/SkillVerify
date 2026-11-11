"""Tests for examples/run_cv_parse.py and examples/make_sample_cvs.py. A fake provider is injected; offline."""
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
from docx import Document
from reportlab.pdfgen import canvas

from skillverify.cv import extract_text, find_github
from skillverify.cv import parser as cv_parser_module
from skillverify.jd import provider as provider_module
from skillverify.jd.growth import LEARNED_PATH, QUEUE_PATH
from skillverify.jd.provider import LLMCallError, LLMConfigError, LLMProvider

KEY = "SECRET-KEY-123"
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
CV = ("JANE EXAMPLE\njane.example@example.com | +1 (555) 010-2345\nGitHub: github.com/janeexample\n"
      "Skills: Python, SQL and Docker used in several data projects\nEducation: Lakeview University\n")


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


def good_answer(*extra_skills):
    skills = [{"as_written": w, "name": w, "context": "skills_section", "project": None, "evidence": w}
              for w in ("Python", "SQL", "Docker") + extra_skills]
    return {"name": "Jane Example", "education": [{"institution": "Lakeview University", "degree": None,
                                                    "field": None, "start": None, "end": None}],
            "skills": skills, "projects": []}


def load_script(name):
    spec = importlib.util.spec_from_file_location(name + "_under_test", EXAMPLES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """The script, run inside a temp folder, with the parser's default paths redirected there."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cv_parser_module, "LEARNED_PATH", tmp_path / "skills_learned.json")
    monkeypatch.setattr(cv_parser_module, "QUEUE_PATH", tmp_path / "queue.json")
    monkeypatch.setattr(provider_module, "load_dotenv", lambda *a, **k: None)  # a real .env must not leak in
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return load_script("run_cv_parse")


def run(cli, argv, provider=None, stdin=None):
    seen = {}

    def factory(model):
        seen["model"] = model
        return provider if provider is not None else FakeProvider(good_answer())

    return cli.main(argv, provider_factory=factory, stdin=stdin), seen


def no_leaks(out):
    assert "Traceback" not in out and KEY not in out
    assert "jane.example@example.com" not in out and "010-2345" not in out and "5550102345" not in out


# --------------------------------------------------------------------------
# input sources
# --------------------------------------------------------------------------
def test_text_file(cli, tmp_path, capsys):
    cv_file = tmp_path / "Jane CV.txt"
    cv_file.write_text(CV, encoding="utf-8")
    code, _ = run(cli, ["--file", str(cv_file)])
    out = capsys.readouterr().out
    assert code == 0
    saved = json.loads((tmp_path / "output" / "Jane_CV_cvparsed.json").read_text(encoding="utf-8"))
    assert saved["status"] == "ok" and saved["label"] == "Jane_CV" and saved["source"]["format"] == "txt"
    assert saved["candidate"]["emails"] == ["jane.example@example.com"]  # the saved file holds the data; the console does not
    assert "CV: Jane_CV   (format: txt" in out and "name: found   email: found   phone: found" in out
    assert "github: janeexample (found, from text)" in out and "claimed skills: Python, SQL, Docker" in out
    assert "0 skills auto-added, 0 flagged for review (see output/taxonomy_review_queue.json)" in out
    assert "Full JSON saved to" in out
    no_leaks(out)


def test_pdf_file_with_a_hidden_github_link(cli, tmp_path, capsys):
    pdf = canvas.Canvas(str(tmp_path / "cv.pdf"))
    pdf.drawString(50, 780, "Jane Example. Skills: Python, SQL and Docker used in several data projects.")
    pdf.drawString(50, 760, "GitHub")
    pdf.linkURL("https://github.com/LinkUser", (50, 758, 90, 772), relative=0)
    pdf.showPage()
    pdf.save()
    code, _ = run(cli, ["--file", str(tmp_path / "cv.pdf"), "--name", "pdf_cv"])
    out = capsys.readouterr().out
    assert code == 0 and "github: linkuser (found, from link)" in out and "format: pdf, 1 page(s)" in out
    assert (tmp_path / "output" / "pdf_cv_cvparsed.json").exists()


def test_docx_file(cli, tmp_path, capsys):
    document = Document()
    for line in CV.splitlines():
        document.add_paragraph(line)
    document.save(str(tmp_path / "cv.docx"))
    code, _ = run(cli, ["--file", str(tmp_path / "cv.docx")])
    out = capsys.readouterr().out
    assert code == 0 and "format: docx" in out and "github: janeexample" in out
    no_leaks(out)


def test_text_argument(cli, tmp_path, capsys):
    code, _ = run(cli, ["--text", CV, "--name", "from_text"])
    assert code == 0 and (tmp_path / "output" / "from_text_cvparsed.json").exists()
    assert "CV: from_text   (format: text" in capsys.readouterr().out


def test_stdin(cli, tmp_path, capsys):
    code, _ = run(cli, [], stdin=io.StringIO(CV))
    assert code == 0 and (tmp_path / "output" / "cv_cvparsed.json").exists()


class Terminal(io.StringIO):
    def isatty(self):
        return True


def test_no_input_is_a_friendly_failure(cli, tmp_path, capsys):
    code, seen = run(cli, [], stdin=Terminal())
    assert code == 1 and "No CV given" in capsys.readouterr().out and seen == {} and not (tmp_path / "output").exists()


def test_default_stdin_and_argparse_exclusion(cli, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(CV))
    assert cli.main([], provider_factory=lambda model: FakeProvider(good_answer())) == 0
    with pytest.raises(SystemExit):
        cli.main(["--file", "a.txt", "--text", "b"], provider_factory=lambda m: FakeProvider())


def test_model_option_reaches_the_factory(cli):
    assert run(cli, ["--text", CV, "--model", "gemini-x", "--dry-run"])[1] == {"model": "gemini-x"}
    assert run(cli, ["--text", CV, "--dry-run"])[1] == {"model": None}


# --------------------------------------------------------------------------
# --dry-run, --no-auto-add and what is printed
# --------------------------------------------------------------------------
def test_dry_run_writes_nothing_at_all(cli, tmp_path, capsys):
    unknown = good_answer("Looker")
    proposals = {"proposals": [{"term": "Looker", "is_technical_skill": True, "canonical_name": "Looker",
                                "category": "tools", "alias_of": None, "aliases": [], "confidence": 0.95}]}
    cv = CV + "Also Looker dashboards.\n"
    before = sorted(p.name for p in tmp_path.iterdir())
    code, _ = run(cli, ["--text", cv, "--dry-run"], FakeProvider(unknown, proposals))
    out = capsys.readouterr().out
    assert code == 0 and sorted(p.name for p in tmp_path.iterdir()) == before  # no output file, overlay or queue
    assert "1 skills auto-added, 0 flagged for review (dry run: nothing was written)" in out and "Full JSON saved" not in out


def test_no_auto_add_queues_unknown_terms(cli, tmp_path, capsys):
    provider = FakeProvider(good_answer("Looker"))
    code, _ = run(cli, ["--text", CV + "Also Looker dashboards.\n", "--no-auto-add"], provider)
    out = capsys.readouterr().out
    assert code == 0 and provider.calls == 1
    assert "0 skills auto-added, 1 flagged for review" in out and "not in taxonomy: Looker: auto-add disabled" in out
    queue = json.loads((tmp_path / "queue.json").read_text(encoding="utf-8"))
    assert queue["entries"][0]["term"] == "Looker" and queue["entries"][0]["source_jds"] == ["cv:cv"]
    assert not (tmp_path / "skills_learned.json").exists()


def test_summary_shows_projects_warnings_and_notes(cli, capsys):
    answer = good_answer()
    answer["projects"] = [{"name": "Data Projects", "description": "", "technologies": ["Python"]}]
    code, _ = run(cli, ["--text", "Jane Example. Skills: Python, SQL and Docker used in several data projects. "
                                  "Data Projects was one of them.", "--dry-run"], FakeProvider(answer))
    out = capsys.readouterr().out
    assert code == 0 and "project: Data Projects [Python]" in out
    assert "github: not found" in out and "email: not found" in out and "warnings: " in out and "no_github_found" in out


def test_summary_notes_a_queue_problem(cli, tmp_path, capsys):
    (tmp_path / "queue.json").write_text("{corrupt", encoding="utf-8")
    code, _ = run(cli, ["--text", CV + "Also Looker.\n", "--no-auto-add"], FakeProvider(good_answer("Looker")))
    assert code == 0 and "Note: The review queue could not be updated" in capsys.readouterr().out


# --------------------------------------------------------------------------
# bad input needs no key; failures; exit codes
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name, content", [
    ("old.doc", b"\xd0\xcf word"), ("fake.pdf", b"not a pdf"), ("bad.pdf", b"%PDF-1.4\ngarbage garbage garbage garbage garbage"),
    ("bad.docx", b"PK\x03\x04junk"), ("empty.txt", b""), ("tiny.txt", b"Jane"),
])
def test_bad_files_exit_1_before_any_provider_is_created(cli, tmp_path, capsys, name, content):
    (tmp_path / name).write_bytes(content)
    code, seen = run(cli, ["--file", str(tmp_path / name)])
    out = capsys.readouterr().out
    assert code == 1 and seen == {} and "Could not read the CV (" in out
    assert not (tmp_path / "output").exists()
    no_leaks(out)


def test_missing_file_exits_1(cli, tmp_path, capsys):
    code, seen = run(cli, ["--file", str(tmp_path / "does_not_exist.pdf")])
    assert code == 1 and seen == {} and "unreadable_file" in capsys.readouterr().out


@pytest.mark.parametrize("text", ["", "   \n"])
def test_empty_text_exits_1_before_any_provider_is_created(cli, capsys, text):
    code, seen = run(cli, ["--text", text])
    assert code == 1 and seen == {} and "empty" in capsys.readouterr().out


def test_missing_api_key_is_exit_2_with_a_friendly_message(cli, capsys):
    code = cli.main(["--text", CV, "--dry-run"])  # the real provider factory, with no key anywhere
    out = capsys.readouterr().out
    assert code == 2 and "Configuration problem" in out and "GEMINI_API_KEY" in out
    no_leaks(out)


def test_default_factory_builds_a_gemini_provider(cli, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    provider = cli.default_provider("some-model")
    assert provider.name == "gemini" and provider.model == "some-model" and KEY not in repr(provider)


@pytest.mark.parametrize("answers, expected", [
    ([LLMCallError("network down")], "llm_error"), ([{"nothing": 1}], "parse_error"),
])
def test_failed_parses_exit_with_1(cli, tmp_path, capsys, answers, expected):
    code, _ = run(cli, ["--text", CV], FakeProvider(*answers))
    out = capsys.readouterr().out
    assert code == 1 and expected in out and not (tmp_path / "output").exists()
    no_leaks(out)


def test_invalid_label_exits_with_1(cli, capsys):
    code, _ = run(cli, ["--text", CV, "--name", "bad name"])
    assert code == 1 and "invalid_input" in capsys.readouterr().out


def test_config_error_during_the_parse_is_exit_2(cli, capsys):
    code, _ = run(cli, ["--text", CV, "--dry-run"], FakeProvider(LLMConfigError("key problem")))
    assert code == 2 and "key problem" in capsys.readouterr().out


def test_corrupt_overlay_is_exit_2(cli, tmp_path, capsys):
    (tmp_path / "skills_learned.json").write_text("{corrupt", encoding="utf-8")
    code, _ = run(cli, ["--text", CV])
    out = capsys.readouterr().out
    assert code == 2 and "Taxonomy problem" in out
    no_leaks(out)


def test_unexpected_errors_print_only_the_type(cli, capsys):
    code, _ = run(cli, ["--text", CV], FakeProvider(RuntimeError(f"boom {KEY} jane.example@example.com")))
    out = capsys.readouterr().out
    assert code == 1 and "Unexpected problem (RuntimeError)" in out and "boom" not in out
    no_leaks(out)


def test_helper_functions(cli):
    assert cli.found(["x"]) == "found" and cli.found([]) == "not found" and cli.found(None) == "not found"


# --------------------------------------------------------------------------
# the sample CVs and make_sample_cvs.py
# --------------------------------------------------------------------------
SAMPLES = ["cv_data_analyst", "cv_backend_developer", "cv_ml_student", "cv_messy_ambiguous"]


@pytest.mark.parametrize("name", SAMPLES)
def test_sample_cvs_are_fictional_and_realistic(name):
    text = (EXAMPLES / "sample_cvs" / f"{name}.txt").read_text(encoding="utf-8")
    assert 25 <= len(text.splitlines()) <= 60
    assert "@example.com" in text and "github" in text.lower()
    assert find_github(text)["username"] is not None


def test_messy_sample_has_the_traps():
    text = (EXAMPLES / "sample_cvs" / "cv_messy_ambiguous.txt").read_text(encoding="utf-8")
    result = find_github(text)
    assert result["status"] == "ambiguous" and result["username"] == "kabirnaik-dev"
    assert "someone-else" in result["candidates"] and result["other_links"]       # a second person, github.io sites
    assert "GitHub Actions" in text and "2018 - 2024" in text and "Python (advanced)" in text
    assert "Ignore all previous instructions" in text


def test_make_sample_cvs_writes_readable_pdf_and_docx(tmp_path, capsys):
    maker = load_script("make_sample_cvs")
    assert maker.main(["--out", str(tmp_path / "samples")]) == 0
    out = capsys.readouterr().out
    written = sorted((tmp_path / "samples").iterdir())
    assert len(written) == 8 and out.count("Wrote ") == 8
    for name in SAMPLES:
        source = (EXAMPLES / "sample_cvs" / f"{name}.txt").read_text(encoding="utf-8")
        expected = find_github(source)["username"]
        for extension in (".pdf", ".docx"):
            result = extract_text(tmp_path / "samples" / f"{name}{extension}")
            assert len(result["text"]) > 400 and "@example.com" in result["text"]
            github = find_github(result["text"], result["links"])
            assert github["username"] == expected, (name, extension, github)
    pdf = extract_text(tmp_path / "samples" / "cv_data_analyst.pdf")
    assert pdf["links"] == ["https://github.com/meera-data-lab"] and "GitHub" in pdf["text"]
    assert "github.com/meera" not in pdf["text"]  # the URL lives only in the hyperlink, like many real CVs


def test_make_sample_cvs_segments_and_missing_libraries(tmp_path, monkeypatch, capsys):
    maker = load_script("make_sample_cvs")
    assert maker.segments("plain line") == [("plain line", None)]
    assert maker.segments("") == [("", None)]
    pieces = maker.segments("Code: github.com/janedoe. Also https://github.com/other/repo end")
    assert pieces == [("Code: ", None), ("GitHub", "https://github.com/janedoe"), (". Also ", None),
                      ("GitHub", "https://github.com/other/repo"), (" end", None)]
    monkeypatch.setitem(sys.modules, "docx", None)  # makes "import docx" fail
    assert maker.main(["--out", str(tmp_path / "x")]) == 2
    assert "pip install -r requirements-cv.txt" in capsys.readouterr().out
    assert not (tmp_path / "x").exists()


def test_make_sample_cvs_long_documents_get_new_pages(tmp_path):
    maker = load_script("make_sample_cvs")
    lines = [f"Line {n} of a very long fictional CV with some words" for n in range(120)]
    maker.write_pdf(lines, tmp_path / "long.pdf")
    result = extract_text(tmp_path / "long.pdf")
    assert result["pages"] >= 2 and "Line 119" in result["text"]
    maker.write_pdf(["word " * 60], tmp_path / "wrapped.pdf")  # a long line is wrapped, not cut
    assert extract_text(tmp_path / "wrapped.pdf")["text"].count("word") == 60


def test_the_real_overlay_and_queue_were_never_created():
    assert not LEARNED_PATH.exists() and not QUEUE_PATH.exists()
