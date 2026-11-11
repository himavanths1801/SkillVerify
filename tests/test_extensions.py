"""Tests for the extras: extended taxonomy, label promotion and strict SQL detection. Mocked HTTP only."""
import base64
import copy
import importlib.util
import json
from pathlib import Path
from urllib.parse import quote

import pytest
import responses

from skillverify import github_validator as gv
from skillverify.github_validator import GitHubValidator
from skillverify.sql_strict import (
    StrictSqlTechScanner,
    find_embedded_sql_strict,
    looks_like_sql_strict,
)
from skillverify.taxonomy import Taxonomy, TaxonomyError
from skillverify.taxonomy_extra import (
    DEFAULT_EXTRA_SKILLS_PATH,
    extended_detectable,
    load_extended_taxonomy,
    promote_label_items,
)
from skillverify.tech_scanner import (
    TechScanner,
    TechScannerError,
    compare_claimed_skills,
    find_embedded_sql,
    looks_like_sql,
)

API = "https://api.github.com"

# Copied from tests/test_tech_scanner.py (this file imports nothing from other test files).
POSITIVE_SQL = [
    "SELECT id, name FROM users", "SELECT * FROM `users`", 'SELECT a FROM "my table"',
    "select * from users", "select name from users where id = 1", "select name from users where id = %s",
    "select name from users limit 5", "Select a From b Join c On 1=1", "select a from b group by a",
    "select a from b order by a", "select a from b having a > 1", "select a from b where x = :name",
    "select a from b -- ? maybe", "select x from [dbo].[t] where id = @id", "select a from b where c = ${x}",
    "INSERT INTO users VALUES (1)", "insert into users (a, b) values (1, 2)", "insert into users values (1)",
    "insert into t select * from u", "DELETE FROM users", "delete from users where id = 1", "delete from users;",
    "UPDATE users SET a = 1", "update users set a = 1", "update users set name = 'x', age = 3",
    "CREATE TABLE users (id int)", "CREATE DATABASE shop", "CREATE INDEX i ON t(a)", "CREATE VIEW v AS SELECT 1",
    "create table users (id int)", "create table if not exists users (id int)",
    "  SELECT\n  id\n  FROM\n  users  ",
]
NEGATIVE_SQL = [
    "Please select an option from the list", "Select a file from disk", "Update the settings set by admin",
    "Insert the card into the reader", "Delete from favourites", "create table of contents",
    "cannot select from an empty sequence", "Select a column set from the table", "hello world", "",
    "   ", "select", "update users", "insert into", "Choose from the list below",
]
NEW_POSITIVE = [
    "SELECT COUNT(*) FROM orders", "SELECT DISTINCT city FROM customers",
    "SELECT u.name AS n FROM users u WHERE u.id = ?", "SELECT * FROM users u JOIN orders o ON o.uid = u.id",
    "WITH t AS (SELECT 1 FROM x) SELECT * FROM t", "(SELECT id FROM t)", "DELETE FROM sessions",
    "INSERT INTO users (a) VALUES (?)", "select id from users u where u.id=1", "SELECT name FROM users;",
]
# Strings the OLD detector wrongly accepts.
OLD_FALSE_POSITIVES = [
    "Click here to SELECT * FROM the menu", "SELECT AN OPTION FROM THE LIST BELOW",
    "SELECT * FROM menu items please.", "INSERT INTO the box", "DELETE FROM FAVOURITES AFTER 30 DAYS",
    "CREATE TABLE OF CONTENTS", "Rows are changed by UPDATE users SET a = 1",
    "SELECT ONE ITEM FROM EACH GROUP", "SELECT * FROM users.", "UPDATE settings SET by admin",
]


@pytest.fixture(scope="module")
def base_tax():
    return Taxonomy.load()


@pytest.fixture(scope="module")
def ext():
    return load_extended_taxonomy()


@pytest.fixture
def validator():
    return GitHubValidator(token="test-token", backoff_seconds=0, max_retries=0)


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


def validation(*names, languages=None):
    repos = [{"name": n, "counted": True, "is_fork": False,
              "last_commit_date": f"2025-0{9 - i}-01T00:00:00Z"} for i, n in enumerate(names)]
    return {"username": "alice", "status": "ok", "repos": repos,
            "summary": {"languages_combined": languages if languages is not None else {"Python": 100}}}


def notebook(*sources):
    return json.dumps({"cells": [{"cell_type": "code", "source": s} for s in sources]})


def write_skills(tmp_path, name, skills):
    path = tmp_path / name
    path.write_text(json.dumps({"skills": skills}), encoding="utf-8")
    return path


def skill(sid, category="tools", aliases=()):
    return {"id": sid, "category": category, "aliases": list(aliases), "github_languages": []}


def label_item(label, confidence="weak"):
    return {"skill": None, "label": label, "in_taxonomy": False, "category": None, "confidence": confidence,
            "evidence_types": ["ide_folder"], "repos": ["a"], "examples": [{"repo": "a", "path": "p", "type": "ide_folder"}]}


# --------------------------------------------------------------------------
# 1-2. Extended taxonomy
# --------------------------------------------------------------------------
def test_extended_taxonomy_sizes(base_tax, ext):
    assert len(ext.skills) == 168 and len(base_tax.skills) == 165
    assert len(Taxonomy.load().skills) == 165  # the default loader is untouched
    assert base_tax.get("YOLO") is None and ext.get("YOLO")["category"] == "data_ml"
    assert DEFAULT_EXTRA_SKILLS_PATH.name == "skills_extra.json"


@pytest.mark.parametrize("text, expected", [
    ("YOLO", "YOLO"), ("YOLOv8", "YOLO"), ("yolo-v5", "YOLO"), ("yolo 11", "YOLO"),
    ("PyCharm", "PyCharm"), ("VSCode", "VS Code"), ("vs-code", "VS Code"),
    ("Visual Studio Code", "VS Code"), ("microsoft vs code", "VS Code"),
])
def test_extra_skills_match(ext, text, expected):
    assert ext.normalize(text)["skill"] == expected


@pytest.mark.parametrize("text", ["Visual Studio", "JetBrains IDE", "Torch"])
def test_ambiguous_names_stay_unknown(ext, text):
    assert ext.normalize(text)["status"] == "unknown"


def test_loader_errors(tmp_path):
    with pytest.raises(TaxonomyError, match="not found"):
        load_extended_taxonomy(extra_path=tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    with pytest.raises(TaxonomyError, match="not valid JSON"):
        load_extended_taxonomy(extra_path=bad)
    empty = tmp_path / "empty.json"
    empty.write_text('{"skills": []}', encoding="utf-8")
    with pytest.raises(TaxonomyError, match="no skills"):
        load_extended_taxonomy(extra_path=empty)
    with pytest.raises(TaxonomyError, match="not found"):
        load_extended_taxonomy(base_path=tmp_path / "nope.json")


def test_duplicate_id_with_a_base_skill_is_refused_case_insensitively(tmp_path):
    extra = write_skills(tmp_path, "extra.json", [skill("python")])
    with pytest.raises(TaxonomyError, match="'python' duplicates base skill id 'Python'"):
        load_extended_taxonomy(extra_path=extra)


def test_alias_and_key_collisions_with_base_are_refused(tmp_path):
    alias = write_skills(tmp_path, "alias.json", [skill("Zed", aliases=["js"])])
    with pytest.raises(TaxonomyError, match="Alias collision"):
        load_extended_taxonomy(extra_path=alias)
    key = write_skills(tmp_path, "key.json", [skill("Node JS")])
    with pytest.raises(TaxonomyError, match="same key"):
        load_extended_taxonomy(extra_path=key)


def test_custom_paths_are_honored(tmp_path):
    base = write_skills(tmp_path, "base.json", [skill("Python", "language")])
    extra = write_skills(tmp_path, "extra.json", [skill("Zed", aliases=["zedd"])])
    custom = load_extended_taxonomy(extra_path=extra, base_path=base)
    assert [s["id"] for s in custom.skills] == ["Python", "Zed"] and custom.normalize("zedd")["skill"] == "Zed"


# --------------------------------------------------------------------------
# 3. Promotion
# --------------------------------------------------------------------------
def test_promotion_of_the_three_tools(ext):
    scan = {"status": "ok", "skills": [label_item("YOLO", "medium"), label_item("VS Code"),
                                       label_item("PyCharm"), label_item("JetBrains IDE")]}
    original = copy.deepcopy(scan)
    out = promote_label_items(scan, ext)
    assert scan == original  # the input is not mutated
    assert out["promoted_labels"] == ["PyCharm", "VS Code", "YOLO"]
    by_label = {s["label"]: s for s in out["skills"]}
    yolo = by_label["YOLO"]
    assert (yolo["skill"], yolo["in_taxonomy"], yolo["category"], yolo["confidence"]) == ("YOLO", True, "data_ml", "medium")
    assert yolo["examples"] == original["skills"][0]["examples"] and yolo["repos"] == ["a"]
    assert by_label["VS Code"]["category"] == "tools" and by_label["PyCharm"]["skill"] == "PyCharm"
    jb = by_label["JetBrains IDE"]
    assert jb["skill"] is None and jb["in_taxonomy"] is False
    json.dumps(out)


def test_no_duplicate_skill_ids_after_promotion(ext):
    real = {"skill": "YOLO", "label": "YOLO", "in_taxonomy": True, "category": "data_ml", "confidence": "strong",
            "evidence_types": [], "repos": [], "examples": []}
    out = promote_label_items({"skills": [real, label_item("YOLO")]}, ext)
    assert out["promoted_labels"] == []
    assert out["skills"][1]["skill"] is None  # not promoted: that skill id already exists in the scan
    twice = promote_label_items({"skills": [label_item("VS Code"), label_item("vscode")]}, ext)
    assert twice["promoted_labels"] == ["VS Code"] and twice["skills"][1]["skill"] is None


def test_promotion_with_the_base_taxonomy_changes_nothing_but_adds_the_key(base_tax):
    out = promote_label_items({"skills": [label_item("YOLO")]}, base_tax)
    assert out["promoted_labels"] == [] and out["skills"][0]["skill"] is None


@pytest.mark.parametrize("odd", [None, 5, "text", [], {}, {"skills": "x"}, {"skills": None}])
def test_promotion_never_raises_and_returns_unchanged(ext, odd):
    out = promote_label_items(odd, ext)
    assert out == odd and "promoted_labels" not in (out if isinstance(out, dict) else {})
    json.dumps(out)


def test_promotion_skips_odd_items(ext):
    skills = ["junk", None, {"label": 5, "in_taxonomy": False}, {"in_taxonomy": False},
              {"skill": "Pandas", "label": "Pandas", "in_taxonomy": True}, label_item("PyCharm")]
    out = promote_label_items({"skills": skills}, ext)
    assert out["promoted_labels"] == ["PyCharm"] and out["skills"][:5] == skills[:5]


def test_extended_detectable_adds_only_yolo(validator, ext):
    scanner = TechScanner(validator, taxonomy=ext)
    detectable = extended_detectable(scanner)
    assert "YOLO" in detectable and "YOLO" not in scanner.detectable_skills()
    assert "PyCharm" not in detectable and "VS Code" not in detectable
    assert detectable - {"YOLO"} == scanner.detectable_skills()


# --------------------------------------------------------------------------
# 4. End-to-end claims
# --------------------------------------------------------------------------
def run_claims(validator, ext, claimed, files, taxonomy=None, scanner_class=TechScanner, promote=True):
    taxonomy = taxonomy or ext
    scanner = scanner_class(validator, taxonomy=taxonomy)
    val = validation("proj")
    tech = scanner.scan(val)
    if promote:
        tech = promote_label_items(tech, taxonomy)
    detectable = extended_detectable(scanner) if taxonomy is ext else scanner.detectable_skills()
    out = compare_claimed_skills(claimed, val, tech, taxonomy, detectable=detectable,
                                 scan_skills=scanner.scannable_skills())
    json.dumps(out)
    return {e["skill"] or e["claimed"][0]: e for e in out}, tech


@responses.activate
def test_claims_found_with_the_extended_taxonomy(validator, ext):
    mock_repo("proj", {"requirements.txt": "ultralytics\n", ".vscode/settings.json": "{}",
                       ".idea/x.iml": '<module type="PYTHON_MODULE"/>'})
    out, tech = run_claims(validator, ext, ["YOLO", "YOLOv8", "VS Code", "PyCharm"], None)
    assert out["YOLO"]["status"] == "found" and out["YOLO"]["confidence"] == "medium"
    assert out["YOLO"]["claimed"] == ["YOLO", "YOLOv8"]
    assert out["VS Code"]["status"] == "found" and out["VS Code"]["confidence"] == "weak"
    assert out["PyCharm"]["status"] == "found" and out["PyCharm"]["confidence"] == "weak"
    assert tech["promoted_labels"] == ["PyCharm", "VS Code", "YOLO"]


@responses.activate
def test_unfound_claims_with_a_complete_scan(validator, ext):
    mock_repo("proj", {"app.py": "x = 1\n"})
    out, tech = run_claims(validator, ext, ["YOLO", "VS Code", "PyCharm"], None)
    assert tech["complete"] is True
    assert (out["YOLO"]["status"], out["YOLO"]["basis"]) == ("not_found", "full_scan")
    for name in ("VS Code", "PyCharm"):  # absence of .vscode/.idea proves nothing
        assert (out[name]["status"], out[name]["basis"]) == ("not_detectable_from_github", "not_checkable")


@responses.activate
def test_old_default_taxonomy_behaves_as_before(validator, base_tax):
    mock_repo("proj", {"app.py": "x = 1\n"})
    out, tech = run_claims(validator, None, ["YOLO", "VS Code"], None, taxonomy=base_tax, promote=False)
    assert out["YOLO"]["status"] == "not_in_taxonomy" and out["VS Code"]["status"] == "not_in_taxonomy"
    assert "promoted_labels" not in tech
    responses.reset()
    mock_repo("proj", {"requirements.txt": "ultralytics\n", ".vscode/a.json": "{}"})
    out, _ = run_claims(validator, None, ["YOLO", "VS Code"], None, taxonomy=base_tax, promote=False)
    assert out["YOLO"]["status"] == "found" and out["VS Code"]["status"] == "found"  # via label-only items


# --------------------------------------------------------------------------
# 5-7. Strict SQL
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text", POSITIVE_SQL + NEW_POSITIVE)
def test_strict_positives(text):
    assert looks_like_sql_strict(text), text
    assert looks_like_sql(text), text


@pytest.mark.parametrize("text", NEGATIVE_SQL)
def test_strict_rejects_every_old_negative(text):
    assert not looks_like_sql_strict(text), text


@pytest.mark.parametrize("text", OLD_FALSE_POSITIVES)
def test_strict_fixes_old_false_positives(text):
    assert looks_like_sql(text), text          # the old detector wrongly accepts it
    assert not looks_like_sql_strict(text), text


EXTRA_CASES = [
    "SELECT 1", "SELECT * FROM", "select a from b c d e", "SELECT id name FROM users", "WITH x", "EXPLAIN SELECT * FROM t",
    "EXPLAIN SELECT * FROM t.", "WITH t AS (SELECT 1 FROM x) SELECT * FROM t!", "SELECT * FROM t x y where 1",
    "SELECT * FROM t x where a = 1", "SELECT * FROM t AS x where a = 1", "SELECT * FROM t AS x", "SELECT * FROM t x",
    "SELECT * FROM t x, u", "SELECT * FROM t), x", "SELECT TOP 5 name FROM users", "SELECT ALL name FROM users",
    "SELECT a AS b FROM t", "SELECT a.b FROM t", "SELECT [a] FROM t", "SELECT * FROM t /* c */", "SELECT * FROM t -- c",
    "INSERT INTO t DEFAULT VALUES", "INSERT INTO t SELECT * FROM u", "INSERT INTO t (a) VALUES (1);",
    "DELETE FROM t USING u WHERE 1", "DELETE FROM t RETURNING id", "DELETE FROM t);", "DELETE FROM t -- c",
    "UPDATE t SET a.b = 1", "UPDATE t SET a", "UPDATE t", "CREATE TABLE t AS SELECT 1", "CREATE TABLE IF NOT EXISTS t (a int)",
    "CREATE TABLE t x", "CREATE INDEX i ON t", "CREATE INDEX i t", "CREATE VIEW v AS SELECT 1", "CREATE VIEW v SELECT 1",
    "CREATE DATABASE d", "CREATE DATABASE d;", "CREATE DATABASE d CHARACTER SET utf8", "CREATE DATABASE d COLLATE x",
    "CREATE DATABASE d WITH OWNER x", "CREATE DATABASE d please", "CREATE DATABASE d)",
    "select name from users where id = 1.", "delete from users;!",
]


def test_strict_invariant_never_accepts_what_the_old_check_rejects():
    everything = POSITIVE_SQL + NEGATIVE_SQL + NEW_POSITIVE + OLD_FALSE_POSITIVES + EXTRA_CASES
    for text in everything:
        if looks_like_sql_strict(text):
            assert looks_like_sql(text), text


def test_strict_guard_details():
    ok = ["SELECT TOP 5 name FROM users", "SELECT ALL name FROM users", "SELECT a AS b FROM t", "SELECT [a] FROM t",
          "SELECT * FROM t /* c */", "SELECT * FROM t x where a = 1", "SELECT * FROM t AS x where a = 1",
          "SELECT * FROM t AS x", "SELECT * FROM t x", "SELECT * FROM t -- c", "EXPLAIN SELECT * FROM t",
          "INSERT INTO t DEFAULT VALUES", "INSERT INTO t SELECT * FROM u", "DELETE FROM t USING u WHERE 1",
          "DELETE FROM t RETURNING id", "DELETE FROM t -- c", "CREATE TABLE t AS SELECT 1",
          "CREATE TABLE IF NOT EXISTS t (a int)", "CREATE INDEX i ON t", "CREATE VIEW v AS SELECT 1",
          "CREATE DATABASE d", "CREATE DATABASE d;", "CREATE DATABASE d CHARACTER SET utf8",
          "CREATE DATABASE d COLLATE x", "CREATE DATABASE d WITH OWNER x", "UPDATE t SET a.b = 1"]
    for text in ok:
        assert looks_like_sql_strict(text), text
    for text in ["EXPLAIN SELECT * FROM t.", "SELECT * FROM t x y where 1", "SELECT id name FROM users",
                 "CREATE VIEW v SELECT 1", "CREATE INDEX i t", "CREATE DATABASE d please",
                 "select name from users where id = 1.", "DELETE FROM t please"]:
        assert not looks_like_sql_strict(text), text


def test_known_limit_is_documented_behaviour():
    assert looks_like_sql_strict("SELECT ALL RESULTS FROM SEARCH PAGE")


def test_strict_tail_that_is_not_a_word_is_rejected():
    for text in ("SELECT * FROM t = 5", "SELECT * FROM t 123", "select * from t 'x'"):
        assert looks_like_sql(text) and not looks_like_sql_strict(text), text


def test_strict_works_on_the_text_cut_to_the_length_cap():
    padded = "x" * 20_001 + " SELECT * FROM users"  # the SQL lies beyond the cap, so neither check sees it
    assert not looks_like_sql(padded) and not looks_like_sql_strict(padded)
    assert looks_like_sql_strict("SELECT * FROM users WHERE id = ? -- " + "x" * 30_000)


# --------------------------------------------------------------------------
# 8. find_embedded_sql_strict
# --------------------------------------------------------------------------
def test_find_embedded_sql_strict():
    assert find_embedded_sql_strict('cur.execute("SELECT id FROM users WHERE id = ?")', "py")
    assert find_embedded_sql_strict("db.query(`SELECT id FROM users WHERE id = ${id}`)", "js")
    assert find_embedded_sql_strict("q = '''\nSELECT id\nFROM users\n'''", "py")
    assert not find_embedded_sql_strict("# SELECT id FROM users WHERE id = 1\nx = 1", "py")
    assert not find_embedded_sql_strict("// SELECT id FROM users WHERE id = 1\nlet x = 1", "js")
    assert not find_embedded_sql_strict("SELECT id FROM users", "py")  # bare code, not a string
    prose = 'print("SELECT AN OPTION FROM THE LIST BELOW")'
    assert find_embedded_sql(prose, "py") and not find_embedded_sql_strict(prose, "py")


# --------------------------------------------------------------------------
# 9. StrictSqlTechScanner vs TechScanner
# --------------------------------------------------------------------------
@responses.activate
def test_scans_are_identical_when_there_are_no_sql_like_strings(validator, base_tax):
    mock_repo("proj", {
        "app.py": "import pandas\nfrom flask import Flask\nprint('hello')\n",
        "nb.ipynb": notebook("!pip install seaborn\nimport numpy\n"),
        "package.json": json.dumps({"dependencies": {"react": "1", "left-pad": "1"}}),
        "web.js": "import express from 'express';\nconsole.log('hi')\n",
        "db/schema.sql": "CREATE TABLE t (id int);",
        "docker-compose.yml": "services: {}",
        ".vscode/settings.json": "{}",
    })
    val = validation("proj")
    base = TechScanner(validator, taxonomy=base_tax).scan(val)
    strict = StrictSqlTechScanner(validator, taxonomy=base_tax).scan(val)
    assert strict == base
    assert {"Pandas", "Flask", "Express.js", "SQL"} <= {s["label"] for s in base["skills"]}
    json.dumps(strict)


PROSE = "SELECT AN OPTION FROM THE LIST BELOW"
REAL = "SELECT id FROM users WHERE id = ?"


def sql_item(tech):
    return next((s for s in tech["skills"] if s["label"] == "SQL"), None)


@responses.activate
def test_prose_is_sql_for_the_base_scanner_but_not_the_strict_one(validator, base_tax):
    mock_repo("proj", {"msg.py": f'MSG = "{PROSE}"\n', "msg.js": f"const m = '{PROSE}';\n",
                       "msg.ipynb": notebook(f'print("{PROSE}")')})
    val = validation("proj")
    base = TechScanner(validator, taxonomy=base_tax).scan(val)
    strict = StrictSqlTechScanner(validator, taxonomy=base_tax).scan(val)
    assert sql_item(base)["evidence_types"] == ["embedded_query"] and sql_item(base)["confidence"] == "strong"
    assert sql_item(strict) is None


@responses.activate
def test_strict_scanner_keeps_sql_files_and_real_queries(validator, base_tax):
    mock_repo("files", {"msg.py": f'MSG = "{PROSE}"\n', "db/q.sql": "SELECT 1;"})
    val = validation("files")
    strict = StrictSqlTechScanner(validator, taxonomy=base_tax).scan(val)
    assert sql_item(strict)["evidence_types"] == ["sql_file"] and sql_item(strict)["confidence"] == "medium"

    responses.reset()
    mock_repo("real", {"q.py": f'cur.execute("{REAL}")\n', "q.js": f"db.query('{REAL}');\n",
                       "q.ipynb": notebook(f'cur.execute("{REAL}")')})
    val = validation("real")
    for scanner_class in (TechScanner, StrictSqlTechScanner):
        tech = scanner_class(validator, taxonomy=base_tax).scan(val)
        item = sql_item(tech)
        assert item["evidence_types"] == ["embedded_query"] and item["confidence"] == "strong"
        assert {e["path"] for e in item["examples"]} == {"q.py", "q.js", "q.ipynb"}


@responses.activate
def test_strict_scanner_keeps_the_base_constructor_and_bookkeeping(validator, base_tax):
    mock_repo("proj", {"a.py": "import pandas\n"})
    scanner = StrictSqlTechScanner(validator, taxonomy=base_tax, max_repos=3, max_files_per_repo=5,
                                   max_total_file_fetches=7)
    tech = scanner.scan(validation("proj"))
    assert tech["scan_settings"]["max_repos"] == 3 and tech["per_repo"][0]["python_files_read"] == 1
    assert tech["complete"] is True and isinstance(scanner, TechScanner)


# --------------------------------------------------------------------------
# 10. Demo script
# --------------------------------------------------------------------------
def load_demo():
    path = Path(__file__).resolve().parent.parent / "examples" / "run_tech_scan_ext.py"
    spec = importlib.util.spec_from_file_location("run_tech_scan_ext_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setattr(gv, "load_dotenv", lambda *a, **k: None)  # never read a real .env
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    return load_demo()


def run_main(demo, monkeypatch, tmp_path, *args):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["run_tech_scan_ext.py", *args])
    return demo.main()


def test_demo_filename_is_safe_and_separate(demo):
    assert demo.output_path_for("alice") == Path("output") / "alice_techstack_ext.json"
    assert str(demo.output_path_for("alice")).endswith("_techstack_ext.json")
    for bad in ("../../x", "bad name", "a/b", "", "a" * 40):
        assert demo.output_path_for(bad) is None


def test_demo_invalid_username_writes_nothing(demo, monkeypatch, tmp_path, capsys):
    assert run_main(demo, monkeypatch, tmp_path, "bad name") == 1
    assert capsys.readouterr().out.strip() == "Invalid GitHub username. Nothing was fetched or saved."
    assert list(tmp_path.iterdir()) == []


def test_demo_missing_token_is_friendly(demo, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("GITHUB_TOKEN")
    assert run_main(demo, monkeypatch, tmp_path, "alice") == 2
    assert "GITHUB_TOKEN" in capsys.readouterr().out


def test_demo_configuration_errors(demo, monkeypatch, tmp_path, capsys):
    def taxonomy_problem():
        raise TaxonomyError("bad extra file")

    monkeypatch.setattr(demo, "load_extended_taxonomy", taxonomy_problem)
    assert run_main(demo, monkeypatch, tmp_path, "alice") == 2
    assert "Taxonomy problem: bad extra file" in capsys.readouterr().out
    monkeypatch.undo()

    def signal_problem(*args, **kwargs):
        raise TechScannerError("bad signals")

    monkeypatch.setattr(gv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    fresh = load_demo()
    monkeypatch.setattr(fresh, "StrictSqlTechScanner", signal_problem)
    assert run_main(fresh, monkeypatch, tmp_path, "alice") == 2
    assert "Signal file problem: bad signals" in capsys.readouterr().out


def test_ext_claim_line(demo):
    docker = {"claimed": ["Python and Docker"], "skill": "Docker", "status": "found", "confidence": "strong",
              "sources": [{"type": "language_stats", "bytes": 1, "percent": 5.0}]}
    assert demo.ext_claim_line(docker) == \
        '  [found, strong] Docker (claimed as "Python and Docker"): language stats 5.0%'
    python = {**docker, "skill": "Python"}
    assert demo.ext_claim_line(python).startswith('  [found, strong] Python (claimed as "Python and Docker")')
    same = {**docker, "claimed": ["docker", "docker"]}
    assert demo.ext_claim_line(same).startswith("  [found, strong] Docker:")
    several = {**docker, "skill": "JavaScript", "claimed": ["JS", "javascript"]}
    assert 'JavaScript (claimed as "JS, javascript")' in demo.ext_claim_line(several)
    unknown = {"claimed": ["Foobar"], "skill": None, "status": "not_in_taxonomy", "confidence": None, "sources": []}
    assert demo.ext_claim_line(unknown) == "  [not_in_taxonomy] Foobar: not in the skill taxonomy"
    assert unknown["claimed"] == ["Foobar"]  # the entry is not modified


def mock_account(files, languages):
    responses.add(responses.GET, f"{API}/users/alice", json={"name": "A", "html_url": "u", "public_repos": 1})
    page = {"type": "owner", "per_page": "100"}
    repo = {"name": "proj", "html_url": "u/proj", "fork": False, "size": 1, "pushed_at": "2025-05-01T00:00:00Z",
            "owner": {"login": "alice"}}
    responses.add(responses.GET, f"{API}/users/alice/repos", json=[repo],
                  match=[responses.matchers.query_param_matcher({**page, "page": "1"})])
    responses.add(responses.GET, f"{API}/users/alice/repos", json=[],
                  match=[responses.matchers.query_param_matcher({**page, "page": "2"})])
    responses.add(responses.GET, f"{API}/repos/alice/proj/languages", json=languages)
    responses.add(responses.GET, f"{API}/repos/alice/proj/commits",
                  json=[{"commit": {"author": {"date": "2025-09-01T00:00:00Z"}}}])
    mock_repo("proj", files)


@responses.activate
def test_demo_full_run_saves_the_ext_file_and_labels_claims(demo, monkeypatch, tmp_path, capsys):
    mock_account({"requirements.txt": "ultralytics\n", ".vscode/settings.json": "{}",
                  "q.py": f'cur.execute("{REAL}")\nMSG = "{PROSE}"\n'},
                 {"Python": 5000, "Dockerfile": 200})
    code = run_main(demo, monkeypatch, tmp_path, "alice", "--claimed",
                    "Python and Docker, YOLO, VS Code, PyCharm, REST APIs, Foobar")
    out = capsys.readouterr().out
    assert code == 0
    assert 'Python (claimed as "Python and Docker")' in out and 'Docker (claimed as "Python and Docker")' in out
    assert "[found, medium] YOLO:" in out and "[found, weak] VS Code:" in out
    assert "[not_detectable_from_github] PyCharm: cannot be checked from GitHub" in out
    assert "[not_detectable_from_github] REST APIs" in out and "[not_in_taxonomy] Foobar" in out
    assert "Tools promoted to skills: VS Code, YOLO" in out and "Extended taxonomy: 168 skills" in out
    assert "Demo only, not the Repo Fingerprint Score." in out
    files = sorted(p.name for p in (tmp_path / "output").iterdir())
    assert files == ["alice_techstack_ext.json"]
    saved = json.loads((tmp_path / "output" / "alice_techstack_ext.json").read_text(encoding="utf-8"))
    assert saved["tech_stack"]["promoted_labels"] == ["VS Code", "YOLO"] and saved["username"] == "alice"
    sql = next(s for s in saved["tech_stack"]["skills"] if s["label"] == "SQL")
    assert sql["evidence_types"] == ["embedded_query"]  # the real query counts; the prose line does not
    assert [e["path"] for e in sql["examples"]] == ["q.py"]


@responses.activate
def test_demo_without_claims_and_with_failures(demo, monkeypatch, tmp_path, capsys):
    mock_account({"a.py": "x = 1\n"}, {"Python": 10})
    assert run_main(demo, monkeypatch, tmp_path, "alice", "--max-repos", "5") == 0
    assert "Claimed skills" not in capsys.readouterr().out

    # a scan that reports status "error" makes the run fail (exit 1), but the file is still saved
    def failing_scan(self, validation_result):
        blank = self._blank_result()
        blank.update(status="error", message="boom")
        return blank

    monkeypatch.setattr(demo.StrictSqlTechScanner, "scan", failing_scan)
    assert run_main(demo, monkeypatch, tmp_path, "alice") == 1
    monkeypatch.undo()

    # an unexpected error prints only its type, never a traceback
    monkeypatch.setattr(gv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    fresh = load_demo()

    def explode(*args, **kwargs):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(fresh, "promote_label_items", explode)
    capsys.readouterr()
    assert run_main(fresh, monkeypatch, tmp_path, "alice") == 1
    printed = capsys.readouterr().out
    assert "Unexpected problem (RuntimeError)" in printed and "secret detail" not in printed
    assert "Traceback" not in printed and "test-token" not in printed


@responses.activate
def test_demo_non_ok_validator_results(demo, monkeypatch, tmp_path, capsys):
    responses.add(responses.GET, f"{API}/users/ghost", status=404, json={"message": "Not Found"})
    assert run_main(demo, monkeypatch, tmp_path, "ghost") == 1
    assert "user_not_found" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []
    responses.add(responses.GET, f"{API}/users/empty", json={"name": "E", "html_url": "u", "public_repos": 0})
    responses.add(responses.GET, f"{API}/users/empty/repos", json=[])
    assert run_main(demo, monkeypatch, tmp_path, "empty") == 0
    assert "no_public_repos" in capsys.readouterr().out


# --------------------------------------------------------------------------
# 11. Everything is JSON-serialisable
# --------------------------------------------------------------------------
@responses.activate
def test_everything_is_json_serialisable(validator, ext):
    mock_repo("proj", {"requirements.txt": "ultralytics\n", ".vscode/s.json": "{}", "a.py": f'x = "{REAL}"'})
    scanner = StrictSqlTechScanner(validator, taxonomy=ext)
    val = validation("proj")
    tech = promote_label_items(scanner.scan(val), ext)
    claims = compare_claimed_skills(["YOLO", "VS Code", "PyCharm", "SQL", "Python and Docker"], val, tech, ext,
                                    detectable=extended_detectable(scanner), scan_skills=scanner.scannable_skills())
    json.dumps({"tech": tech, "claims": claims, "skills": ext.skills})
