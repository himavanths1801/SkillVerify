"""Tests for the tech stack scanner. All HTTP is mocked with `responses`; no network."""
import base64
import json
from urllib.parse import quote

import pytest
import requests
import responses

from skillverify import tech_scanner as ts
from skillverify.github_validator import GitHubValidator
from skillverify.taxonomy import Taxonomy
from skillverify.tech_scanner import TechScanner, TechScannerError, compare_claimed_skills

API = "https://api.github.com"


# --------------------------------------------------------------------------
# helpers
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
    data = text if isinstance(text, bytes) else text.encode("utf-8")
    return base64.b64encode(data).decode("ascii")


def mock_repo(name, files=None, branch="main", truncated=False, sizes=None, owner="alice"):
    """Register a fake repo: metadata, tree and file contents."""
    files = files or {}
    sizes = sizes or {}
    base = f"{API}/repos/{owner}/{name}"
    responses.add(responses.GET, base, json={"default_branch": branch})
    tree = [{"path": p, "type": "blob", "size": sizes.get(p, len(c))} for p, c in files.items()]
    responses.add(responses.GET, f"{base}/git/trees/{quote(branch, safe='')}",
                  json={"tree": tree, "truncated": truncated})
    for path, content in files.items():
        responses.add(responses.GET, f"{base}/contents/{quote(path, safe='/')}",
                      json={"encoding": "base64", "content": b64(content)})


def validation(*names, status="ok", extra=None):
    """A validator-shaped result; later names are older."""
    repos = [{"name": n, "counted": True, "is_fork": False,
              "last_commit_date": f"2025-0{9 - i}-01T00:00:00Z"} for i, n in enumerate(names)]
    result = {"username": "alice", "status": status, "repos": repos,
              "summary": {"languages_combined": {"Python": 7000, "JavaScript": 2000, "HTML": 1000}}}
    result.update(extra or {})
    return result


def skill_map(tech):
    return {s["label"]: s for s in tech["skills"]}


def content_calls():
    return [c.request.url for c in responses.calls if "/contents/" in c.request.url]


# --------------------------------------------------------------------------
# 1. Signal file
# --------------------------------------------------------------------------
def test_real_signal_file_loads_and_ids_exist(validator, tax):
    scanner = make_scanner(validator, tax)
    for skill_id in scanner.signals.skill_ids:
        assert tax.get(skill_id) is not None
    detectable = scanner.detectable_skills()
    assert {"Pandas", "SQL", "Python", "Docker", "GitHub Actions"} <= detectable
    assert "REST APIs" not in detectable and "Agile" not in detectable


def write_signals(tmp_path, data):
    path = tmp_path / "signals.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def sig(label="X", skills=("Pandas",), **fields):
    return {"label": label, "skills": skills if isinstance(skills, str) else list(skills), **fields}


@pytest.mark.parametrize("data, message", [
    ({"signals": [sig(skills=["Nope"])]}, "'Nope' is not in the taxonomy"),
    ({"signals": [sig("A", python_packages=["x"]), sig("B", python_packages=["X"])]}, "'x'.*'A'.*'B'|'X'.*'A'.*'B'"),
    ({"signals": [sig("A", python_packages=["a_b"]), sig("B", python_packages=["a.b"])]}, "python_packages"),
    ({"signals": [sig("A", js_packages=["x"]), sig("B", js_packages=["X"])]}, "js_packages"),
    ({"signals": [sig("A", python_imports=["MySQLdb"]), sig("B", python_imports=["mysqldb"])]}, "python_imports"),
    ({"signals": []}, "non-empty 'signals'"),
    ([], "non-empty 'signals'"),
    ({"signals": [{"skills": []}]}, "no 'label'"),
    ({"signals": [sig(skills="Pandas")]}, "'skills' must be a list of strings"),
    ({"signals": [sig()], "file_signals": [sig("F", path_regex="(", evidence="file_signal")]}, "bad path_regex"),
    ({"signals": [sig()], "file_signals": [sig("F", evidence="file_signal")]}, "missing path_regex"),
    ({"signals": [sig()], "file_signals": [sig("F", path_regex="x", evidence="nope")]}, "evidence must be one of"),
    ({"signals": [sig()], "file_signals": [sig("F", path_regex=3, evidence="file_signal")]}, "bad path_regex"),
])
def test_bad_signal_files(validator, tax, tmp_path, data, message):
    with pytest.raises(TechScannerError, match=message):
        make_scanner(validator, tax, signals_path=write_signals(tmp_path, data))


def test_signal_file_missing_invalid_unreadable(validator, tax, tmp_path):
    with pytest.raises(TechScannerError, match="not found"):
        make_scanner(validator, tax, signals_path=tmp_path / "missing.json")
    with pytest.raises(TechScannerError, match="Could not read"):
        make_scanner(validator, tax, signals_path=write_signals(tmp_path, "{oops"))
    with pytest.raises(TechScannerError, match="Could not read"):
        make_scanner(validator, tax, signals_path=tmp_path)  # a directory


def test_same_name_twice_inside_one_entry_is_fine(validator, tax, tmp_path):
    path = write_signals(tmp_path, {"signals": [sig(python_packages=["pandas", "Pandas"])]})
    assert make_scanner(validator, tax, signals_path=path).signals.python_packages


def test_taxonomy_without_sql_is_refused(validator, tmp_path):
    skills = tmp_path / "skills.json"
    skills.write_text(json.dumps({"skills": [{"id": "Python", "category": "language"}]}), encoding="utf-8")
    small = Taxonomy.load(skills)
    path = write_signals(tmp_path, {"signals": [sig(skills=["Python"])]})
    with pytest.raises(TechScannerError, match="no 'SQL' skill"):
        TechScanner(validator, taxonomy=small, signals_path=path)


def test_default_taxonomy_is_loaded(validator):
    assert TechScanner(validator).taxonomy.get("Python") is not None


# --------------------------------------------------------------------------
# 2. Dependency parsing
# --------------------------------------------------------------------------
def test_requirements_txt():
    text = """
# comment
pandas==2.0.1  # inline
Scikit_Learn>=1.0
requests[security]>=2; python_version < "3.9"
-r other.txt
-e .
--index-url https://example.com/simple
git+https://github.com/x/y.git
numpy @ https://example.com/numpy.whl
./local/path
Flask.Login
"""
    assert ts.parse_requirements_txt(text) == ["pandas", "scikit-learn", "requests", "flask-login"]


def test_pyproject_pep621_and_poetry():
    pep621 = '[project]\ndependencies = ["pandas>=1", "Flask[async]"]\n[project.optional-dependencies]\ndev = ["pytest"]\n'
    assert ts.parse_pyproject(pep621) == ["pandas", "flask", "pytest"]
    poetry = ('[tool.poetry.dependencies]\npython = "^3.10"\nDjango = "^4"\n'
              '[tool.poetry.dev-dependencies]\nblack = "*"\n'
              '[tool.poetry.group.test.dependencies]\npytest = "*"\n')
    assert ts.parse_pyproject(poetry) == ["django", "black", "pytest"]


def test_pipfile():
    text = '[packages]\nFlask = "*"\nrequests = "*"\n[dev-packages]\npytest = "*"\n'
    assert ts.parse_pipfile(text) == ["flask", "requests", "pytest"]


def test_environment_yml():
    text = """name: demo
channels:
  - conda-forge
dependencies:
  - python=3.10
  - numpy=1.24
  - conda-forge::pandas
  - scikit-learn>=1.0   # comment
  - pip:
    - seaborn==0.12
    - -r other.txt
    - git+https://github.com/x/y.git
  - matplotlib
other:
  - ignored
"""
    assert ts.parse_environment_yml(text) == ["python", "numpy", "pandas", "scikit-learn", "seaborn", "matplotlib"]
    assert ts.parse_environment_yml("# only a comment\n") == []
    assert ts.parse_environment_yml("dependencies:\n  not-a-list-item\n") == []


def test_setup_py_is_parsed_not_executed():
    text = "setup(\n  name='x',\n  install_requires=[\n    'pandas>=1.0',\n    \"Flask\",\n  ],\n)\n"
    assert ts.parse_setup_py(text) == ["pandas", "flask"]
    assert ts.parse_setup_py("print('install_requires missing')") == []


def test_package_json_all_sections_and_errors():
    text = json.dumps({"dependencies": {"React": "1"}, "devDependencies": {"jest": "1"},
                       "peerDependencies": {"react-dom": "1"}, "scripts": {"x": "y"}})
    assert ts.parse_package_json(text) == ["react", "jest", "react-dom"]
    assert ts.parse_package_json('{"dependencies": 3}') == []
    with pytest.raises(ValueError):
        ts.parse_package_json("{broken")
    with pytest.raises(ValueError):
        ts.parse_package_json("[1]")


def test_broken_toml_and_no_tomllib(monkeypatch):
    with pytest.raises(ValueError, match="invalid TOML"):
        ts.parse_pyproject("[project\nbroken")
    monkeypatch.setattr(ts, "tomllib", None)
    with pytest.raises(ValueError, match="Python 3.11"):
        ts.parse_pipfile("[packages]\n")


def test_pip_install_lines_in_notebooks():
    code = ("!pip install pandas seaborn==0.12 'scikit-learn>=1' -q\n"
            "%pip install -r requirements.txt numpy\n"
            "!pip3 install --index-url https://x.example/simple matplotlib ./local git+https://a/b.git\n"
            "!python -m pip install nltk && echo done\n"
            "print('pip install nothing')\n")
    assert ts.parse_pip_install_lines(code) == ["pandas", "seaborn", "scikit-learn", "numpy", "matplotlib", "nltk"]


def test_notebook_code_cells_only():
    nb = {"cells": [
        {"cell_type": "markdown", "source": ["import secret_markdown"]},
        {"cell_type": "code", "source": ["import pandas\n", "x = 1"], "outputs": [{"text": "import hidden_output"}]},
        {"cell_type": "code", "source": "import numpy"},
        {"cell_type": "code", "source": 5},
        "not-a-cell",
    ]}
    code = ts.parse_notebook(json.dumps(nb))
    assert "pandas" in code and "numpy" in code
    assert "secret_markdown" not in code and "hidden_output" not in code
    assert ts.parse_notebook('{"cells": "oops"}') == ""
    for bad in ("{oops", "[]", '{"nope": 1}'):
        with pytest.raises(ValueError):
            ts.parse_notebook(bad)


def test_normalize_python_name():
    assert ts.normalize_python_name("Scikit_Learn.Extra--X") == "scikit-learn-extra-x"


# --------------------------------------------------------------------------
# 3. Imports
# --------------------------------------------------------------------------
def test_python_imports():
    code = '''"""
import docstring_only
"""
import os, sys as system, pandas.io as pio
import numpy
from sklearn.model_selection import train_test_split
from . import sibling
from .pkg import thing
from __future__ import annotations
import 3bad
  import indented_ok  # comment
'''
    assert ts.find_python_imports(code) == {"os", "sys", "pandas", "numpy", "sklearn", "__future__", "indented_ok"}
    assert ts.find_python_imports("import ,") == set()


def test_unterminated_triple_quote_removes_rest():
    assert ts.find_python_imports("import a\n'''\nimport b") == {"a"}


def test_js_imports():
    code = """
import React, { useState } from 'react';
import {
  a,
  b
} from "@angular/core/testing";
import 'bootstrap/dist/css/bootstrap.css';
const express = require('express');
const lazy = import("mongoose");
import local from './local';
import abs from '/abs/path';
import fs from 'node:fs';
import onlyscope from '@scope';
// import fake from 'vue';
/* import fake2 from 'svelte'; */
export * from 'redux';
"""
    assert ts.find_js_imports(code) == {"react", "@angular/core", "bootstrap", "express", "mongoose", "redux"}
    assert ts.find_js_imports("/* never closed import x from 'vue'") == set()


# --------------------------------------------------------------------------
# 5. SQL precision
# --------------------------------------------------------------------------
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


@pytest.mark.parametrize("text", POSITIVE_SQL)
def test_sql_positive(text):
    assert ts.looks_like_sql(text), text


@pytest.mark.parametrize("text", NEGATIVE_SQL)
def test_sql_negative(text):
    assert not ts.looks_like_sql(text), text


def test_sql_only_inside_string_literals():
    assert not ts.find_embedded_sql("# SELECT * FROM users WHERE id = 1\nx = 1", "py")
    assert not ts.find_embedded_sql("// SELECT * FROM users WHERE id = 1\nlet x = 1", "js")
    assert not ts.find_embedded_sql("/* SELECT * FROM users */ let x = 1", "js")
    assert not ts.find_embedded_sql("SELECT * FROM users", "py")  # bare code, not a string
    assert ts.find_embedded_sql('cur.execute("SELECT * FROM users")', "py")
    assert ts.find_embedded_sql("q = '''\nSELECT id\nFROM users\n'''", "py")
    assert ts.find_embedded_sql('q = """select * from users where id = 1"""', "py")
    assert ts.find_embedded_sql("const q = `SELECT * FROM users\nWHERE id = ${id}`;", "js")
    assert ts.find_embedded_sql("db.query('DELETE FROM sessions') // done", "js")
    assert not ts.find_embedded_sql('print("Please select an option from the list")', "py")


def test_extract_string_literals():
    q3 = "'" * 3
    code = "\n".join([
        r'a = "he said \"hi\""  # "not a string"',
        r"b = 'it\'s'",
        'c = """triple',
        'line"""',
        'd = "unterminated',
        "e = " + q3 + "never closed",
    ])
    out = ts.extract_string_literals(code, "py")
    assert 'he said \\"hi\\"' in out and "it\\'s" in out and "triple\nline" in out and "unterminated" in out
    assert "not a string" not in out
    js = ts.extract_string_literals("let a = `x\ny` /* 'no' */ + 'z' // 'no2'\n/* open 'no3'", "js")
    assert js == ["x\ny", "z"]
    assert ts.extract_string_literals("x = `not a py string`", "py") == []
    assert ts.extract_string_literals("x = 1 # end", "py") == []


# --------------------------------------------------------------------------
# 4. Full "resume" example
# --------------------------------------------------------------------------
RESUME_REQUIREMENTS = """pandas
numpy
scikit-learn
matplotlib
seaborn
transformers
ultralytics
opencv-python
flask
pymongo
mysql-connector-python
some-unmapped-lib
"""
RESUME_NOTEBOOK = json.dumps({"cells": [{"cell_type": "code", "source": ["import pandas as pd\n"]}]})
RESUME_APP = "from flask import Flask\nimport pymongo\nimport mysql.connector\n"
RESUME_IML = '<module type="PYTHON_MODULE" version="4">\n</module>'


@responses.activate
def test_resume_example(validator, tax):
    mock_repo("ml-project", {
        "requirements.txt": RESUME_REQUIREMENTS,
        "app.py": RESUME_APP,
        "analysis.ipynb": RESUME_NOTEBOOK,
        "db/schema.sql": "CREATE TABLE t (id int);",
        ".idea/ml-project.iml": RESUME_IML,
    })
    tech = make_scanner(validator, tax).scan(validation("ml-project"))
    assert tech["status"] == "ok" and tech["complete"] is True and tech["incomplete_reasons"] == []
    found = skill_map(tech)
    expected = {
        "Pandas": "strong",       # imported in the notebook
        "Flask": "strong", "MongoDB": "strong", "MySQL": "strong", "SQL": "strong",
        "NumPy": "medium", "scikit-learn": "medium", "Matplotlib": "medium", "Seaborn": "medium",
        "Hugging Face": "medium", "OpenCV": "medium", "Computer Vision": "medium",
        "YOLO": "medium", "PyCharm": "weak",
    }
    for label, confidence in expected.items():
        assert found[label]["confidence"] == confidence, label
    assert found["Pandas"]["evidence_types"] == ["imported_in_code", "declared_dependency"]
    assert found["SQL"]["evidence_types"] == ["imported_in_code", "declared_dependency", "sql_file"]
    assert found["YOLO"]["skill"] is None and found["YOLO"]["in_taxonomy"] is False
    assert found["YOLO"]["category"] is None
    assert found["PyCharm"]["skill"] is None and "JetBrains IDE" not in found
    assert found["Pandas"]["category"] == "data_ml" and found["Pandas"]["in_taxonomy"] is True
    assert tech["unmapped_packages"] == [{"name": "some-unmapped-lib", "ecosystem": "python", "repos": 1}]
    per = tech["per_repo"][0]
    assert per["scanned"] and per["dependency_files"] == ["requirements.txt"]
    assert per["sql_files"] == 1 and per["sql_file_examples"] == ["db/schema.sql"]
    json.dumps(tech)


# --------------------------------------------------------------------------
# 6. Files and folders
# --------------------------------------------------------------------------
@responses.activate
def test_file_signals_no_fetch_needed(validator, tax):
    files = {p: "" for p in [
        ".github/workflows/ci.yml", "Jenkinsfile", "svc/.gitlab-ci.yml", "docker-compose.prod.yml",
        "compose.yaml", "pom.xml", "app/build.gradle.kts", ".vscode/settings.json",
        "a.sql", "b/C.SQL", "d.sql", "e.sql", "f.sql", "g.sql", "README.md"]}
    mock_repo("infra", files)
    tech = make_scanner(validator, tax).scan(validation("infra"))
    found = skill_map(tech)
    for label in ("GitHub Actions", "Jenkins", "GitLab CI", "Docker Compose", "Maven", "Gradle", "SQL"):
        assert found[label]["confidence"] == "medium", label
    assert found["SQL"]["evidence_types"] == ["sql_file"]
    assert found["VS Code"]["confidence"] == "weak" and found["VS Code"]["skill"] is None
    assert tech["per_repo"][0]["sql_files"] == 6 and len(tech["per_repo"][0]["sql_file_examples"]) == 5
    assert content_calls() == []  # file signals never fetch anything


@responses.activate
def test_idea_folder_variants(validator, tax):
    mock_repo("plain", {".idea/misc.xml": ""})
    mock_repo("java", {".idea/java.iml": '<module type="JAVA_MODULE">'})
    mock_repo("nested", {"sub/.idea/x.iml": '<module type="PYTHON_MODULE">', "sub/.idea/deeper/y.iml": ""})
    tech = make_scanner(validator, tax).scan(validation("plain", "java", "nested"))
    by_repo = {p["name"]: p["skills"] for p in tech["per_repo"]}
    assert by_repo == {"plain": ["JetBrains IDE"], "java": ["JetBrains IDE"], "nested": ["PyCharm"]}
    assert skill_map(tech)["JetBrains IDE"]["repos"] == ["plain", "java"]


@responses.activate
def test_idea_iml_unreadable_still_reports_jetbrains(validator, tax):
    base = f"{API}/repos/alice/ide"
    responses.add(responses.GET, base, json={"default_branch": "main"})
    responses.add(responses.GET, f"{base}/git/trees/main",
                  json={"tree": [{"path": ".idea/p.iml", "type": "blob", "size": 5}], "truncated": False})
    responses.add(responses.GET, f"{base}/contents/.idea/p.iml", status=404, json={})
    tech = make_scanner(validator, tax).scan(validation("ide"))
    assert skill_map(tech)["JetBrains IDE"]["confidence"] == "weak"
    assert tech["per_repo"][0]["files_failed"] == 1


# --------------------------------------------------------------------------
# JavaScript repos and embedded SQL in source files
# --------------------------------------------------------------------------
@responses.activate
def test_javascript_repo(validator, tax):
    mock_repo("web", {
        "package.json": json.dumps({"dependencies": {"react": "1", "express": "4", "left-pad": "1"},
                                    "devDependencies": {"jest": "1"}}),
        "src/App.jsx": "import React from 'react';\nimport axios from 'axios';\n",
        "server.js": "const express = require('express');\nconst q = `SELECT * FROM users WHERE id = ${id}`;\n",
        "node_modules/react/index.js": "import x from 'vue';",
        "node_modules/react/package.json": json.dumps({"dependencies": {"vue": "1"}}),
        "dist/bundle.js": "import x from 'vue';",
        "static/lib.min.js": "import x from 'vue';",
        "venv/lib/x.py": "import torch",
    })
    tech = make_scanner(validator, tax).scan(validation("web"))
    found = skill_map(tech)
    assert found["React"]["evidence_types"] == ["imported_in_code", "declared_dependency"]
    assert found["Express.js"]["confidence"] == "strong"
    assert found["Jest"]["confidence"] == "medium"
    assert found["SQL"]["evidence_types"] == ["embedded_query"]
    assert "Vue.js" not in found and "PyTorch" not in found
    assert not any(skip in url for url in content_calls() for skip in ("node_modules", "dist/", "venv", ".min.js"))
    assert tech["unmapped_packages"] == [{"name": "left-pad", "ecosystem": "js", "repos": 1}]


@responses.activate
def test_notebook_pip_installs_and_embedded_sql_in_python(validator, tax):
    nb = json.dumps({"cells": [
        {"cell_type": "code", "source": "!pip install seaborn pandas\nimport sqlite3\n"},
        {"cell_type": "code", "source": "cur.execute('select * from t where a = ?')"},
    ]})
    mock_repo("nb", {"nb.ipynb": nb, "run.py": "q = 'SELECT 1 FROM dual'\n"})
    found = skill_map(make_scanner(validator, tax).scan(validation("nb")))
    assert "declared_dependency" in found["Seaborn"]["evidence_types"]
    assert "SQLite" in found and "embedded_query" in found["SQL"]["evidence_types"]


# --------------------------------------------------------------------------
# 7. Repo selection
# --------------------------------------------------------------------------
@responses.activate
def test_forks_and_failed_repos_skipped_and_newest_first(validator, tax):
    mock_repo("old", {"a.py": "import pandas"})
    mock_repo("new", {"a.py": "import numpy"})
    result = validation("old", "new")
    result["repos"][0]["last_commit_date"] = "2024-01-01T00:00:00Z"
    result["repos"][1]["last_commit_date"] = "2025-06-01T00:00:00Z"
    result["repos"] += [
        {"name": "forky", "counted": False, "is_fork": True, "last_commit_date": "2025-07-01T00:00:00Z"},
        {"name": "broken", "counted": False, "error": "x", "last_commit_date": "2025-07-01T00:00:00Z"},
        {"name": "sneaky", "counted": True, "error": "x"},
        {"name": 5, "counted": True},
        "not a dict",
    ]
    tech = make_scanner(validator, tax).scan(result)
    assert [p["name"] for p in tech["per_repo"]] == ["new", "old"]
    assert tech["repos_scanned"] == 2
    assert not any("forky" in c.request.url or "broken" in c.request.url for c in responses.calls)


@responses.activate
def test_missing_or_bad_dates_sort_oldest(validator, tax):
    for name in ("dated", "nodate", "baddate"):
        mock_repo(name)
    result = validation("dated", "nodate", "baddate")
    del result["repos"][1]["last_commit_date"]
    result["repos"][2]["last_commit_date"] = "not a date"
    tech = make_scanner(validator, tax).scan(result)
    assert [p["name"] for p in tech["per_repo"]][0] == "dated"


@responses.activate
def test_max_repos_cap(validator, tax):
    for name in ("r1", "r2", "r3"):
        mock_repo(name)
    tech = make_scanner(validator, tax, max_repos=2).scan(validation("r1", "r2", "r3"))
    assert tech["repos_scanned"] == 2 and tech["repos_not_scanned"] == ["r3"]
    assert tech["complete"] is False and "repo cap reached" in tech["incomplete_reasons"]
    assert tech["scan_settings"]["max_repos"] == 2


# --------------------------------------------------------------------------
# 8. Caps and budget
# --------------------------------------------------------------------------
@responses.activate
def test_oversize_files_skipped_and_counted(validator, tax):
    mock_repo("big", {"huge.py": "import pandas", "small.py": "import numpy", "requirements.txt": "flask"},
              sizes={"huge.py": ts.MAX_FILE_BYTES + 1, "requirements.txt": ts.MAX_FILE_BYTES + 1})
    tech = make_scanner(validator, tax).scan(validation("big"))
    per = tech["per_repo"][0]
    assert per["source_files_read"] == 1 and per["source_files_skipped"] == 1
    assert per["files_failed"] == 1  # the oversize dependency file
    assert not any("huge.py" in u or "requirements.txt" in u for u in content_calls())
    assert "NumPy" in skill_map(tech) and "Pandas" not in skill_map(tech)


@responses.activate
def test_max_files_per_repo_and_shallowest_first(validator, tax):
    files = {f"deep/er/f{i}.py": "import pandas" for i in range(3)}
    files["top.py"] = "import numpy"
    files["mid/m.py"] = "import scipy"
    mock_repo("many", files)
    tech = make_scanner(validator, tax, max_files_per_repo=2).scan(validation("many"))
    per = tech["per_repo"][0]
    assert per["source_files_read"] == 2 and per["source_files_skipped"] == 3
    assert set(skill_map(tech)) == {"NumPy", "SciPy"}  # shallowest two were read


@responses.activate
def test_shared_budget_is_split_fairly(validator, tax):
    names = ["r1", "r2", "r3", "r4"]
    for name in names:
        mock_repo(name, {f"f{i}.py": "import os" for i in range(10)})
    scanner = make_scanner(validator, tax, max_total_file_fetches=8)
    tech = scanner.scan(validation(*names))
    assert [p["source_files_read"] for p in tech["per_repo"]] == [3, 3, 2, 0]
    assert tech["per_repo"][3]["source_files_skipped"] == 10
    assert "source file budget exhausted" in tech["incomplete_reasons"] and tech["complete"] is False


@responses.activate
def test_api_calls_used_matches_requests(validator, tax):
    mock_repo("a", {"requirements.txt": "pandas", "x.py": "import numpy", ".idea/a.iml": "x"})
    mock_repo("b", {"y.js": "import 'react'"})
    tech = make_scanner(validator, tax).scan(validation("a", "b"))
    assert tech["api_calls_used"] == len(responses.calls) == (2 + 3) + (2 + 1)


@responses.activate
def test_only_ten_dependency_files_shallowest_first(validator, tax):
    files = {f"p{i}/requirements.txt": "pandas" for i in range(12)}
    files["requirements.txt"] = "numpy"
    files["node_modules/x/package.json"] = json.dumps({"dependencies": {"vue": "1"}})
    mock_repo("deps", files)
    tech = make_scanner(validator, tax).scan(validation("deps"))
    read = tech["per_repo"][0]["dependency_files"]
    assert len(read) == 10 and read[0] == "requirements.txt"
    assert not any("node_modules" in u for u in content_calls())


@responses.activate
def test_requirements_folder_and_variants_are_found(validator, tax):
    mock_repo("variants", {"requirements/dev.txt": "scipy", "requirements-test.in": "pytest",
                           "environment.yaml": "dependencies:\n  - nltk\n",
                           "setup.py": "install_requires=['spacy']",
                           "Pipfile": "[packages]\nstreamlit = '*'\n"})
    found = skill_map(make_scanner(validator, tax).scan(validation("variants")))
    assert {"SciPy", "Pytest", "Natural Language Processing", "Streamlit"} <= set(found)


@responses.activate
def test_unmapped_packages_capped_at_fifty(validator, tax):
    mock_repo("lots", {"requirements.txt": "\n".join(f"unknown-lib-{i:02d}" for i in range(60))})
    tech = make_scanner(validator, tax).scan(validation("lots"))
    assert len(tech["unmapped_packages"]) == 50


@responses.activate
def test_unmapped_sorted_by_repo_count(validator, tax):
    mock_repo("a", {"requirements.txt": "zzz\naaa"})
    mock_repo("b", {"requirements.txt": "zzz"})
    names = [(u["name"], u["repos"]) for u in make_scanner(validator, tax).scan(validation("a", "b"))["unmapped_packages"]]
    assert names == [("zzz", 2), ("aaa", 1)]


# --------------------------------------------------------------------------
# 9. Failure isolation
# --------------------------------------------------------------------------
@responses.activate
def test_file_level_failures_are_counted_and_scan_continues(validator, tax):
    base = f"{API}/repos/alice/mix"
    responses.add(responses.GET, base, json={"default_branch": "main"})
    tree = [{"path": p, "type": "blob", "size": 10} for p in
            ["gone.py", "bad.py", "weird.py", "requirements.txt", "pyproject.toml", "package.json", "good.py", "nb.ipynb"]]
    responses.add(responses.GET, f"{base}/git/trees/main", json={"tree": tree + [{"path": "d", "type": "tree"}, 5]})
    responses.add(responses.GET, f"{base}/contents/gone.py", status=404, json={})
    responses.add(responses.GET, f"{base}/contents/bad.py", body="<html>not json</html>")
    responses.add(responses.GET, f"{base}/contents/weird.py", json={"encoding": "none", "content": "x"})
    responses.add(responses.GET, f"{base}/contents/requirements.txt", json={"encoding": "base64", "content": b64("pandas")})
    responses.add(responses.GET, f"{base}/contents/pyproject.toml", json={"encoding": "base64", "content": b64("[project\nbroken")})
    responses.add(responses.GET, f"{base}/contents/package.json", json={"encoding": "base64", "content": b64("{broken")})
    responses.add(responses.GET, f"{base}/contents/good.py", json={"encoding": "base64", "content": b64("import flask")})
    responses.add(responses.GET, f"{base}/contents/nb.ipynb", json={"encoding": "base64", "content": b64("not a notebook")})
    tech = make_scanner(validator, tax).scan(validation("mix"))
    per = tech["per_repo"][0]
    assert tech["status"] == "ok" and per["scanned"]
    assert per["files_failed"] == 6  # gone, bad, weird, nb + broken toml + broken package.json
    assert per["source_files_read"] == 1 and per["dependency_files"] == ["requirements.txt"]
    assert {"Flask", "Pandas"} <= set(skill_map(tech))


@responses.activate
def test_repo_level_failures_are_isolated(validator, tax):
    responses.add(responses.GET, f"{API}/repos/alice/blocked", status=451, json={"message": "blocked"})
    responses.add(responses.GET, f"{API}/repos/alice/missing", status=404, json={})
    responses.add(responses.GET, f"{API}/repos/alice/nobranch", json={"nothing": 1})
    responses.add(responses.GET, f"{API}/repos/alice/badtree", json={"default_branch": "main"})
    responses.add(responses.GET, f"{API}/repos/alice/badtree/git/trees/main", json={"tree": "oops"})
    responses.add(responses.GET, f"{API}/repos/alice/treeerr", json={"default_branch": "main"})
    responses.add(responses.GET, f"{API}/repos/alice/treeerr/git/trees/main", status=451, json={})
    mock_repo("empty409", None)
    responses.replace(responses.GET, f"{API}/repos/alice/empty409/git/trees/main", status=409,
                      json={"message": "Git Repository is empty."})
    mock_repo("fine", {"x.py": "import pandas"})
    tech = make_scanner(validator, tax).scan(
        validation("blocked", "missing", "nobranch", "badtree", "treeerr", "empty409", "fine"))
    errors = {p["name"]: p.get("error") for p in tech["per_repo"]}
    assert "451" in errors["blocked"] and "404" in errors["missing"] and "default branch" in errors["nobranch"]
    assert "tree data" in errors["badtree"] and "451" in errors["treeerr"]
    assert errors["empty409"] is None and errors["fine"] is None
    assert tech["repos_failed"] == 5 and tech["repos_scanned"] == 2 and tech["status"] == "ok"
    assert tech["complete"] is False and "repo errors: 5" in tech["incomplete_reasons"]
    assert "Pandas" in skill_map(tech)


@responses.activate
def test_invalid_repo_names_make_no_request(validator, tax):
    result = validation("bad name", "a/b", "..", "ok")
    mock_repo("ok")
    tech = make_scanner(validator, tax).scan(result)
    assert tech["repos_failed"] == 3
    assert all("invalid repository name" in p["error"] for p in tech["per_repo"] if p["name"] != "ok")
    assert len(responses.calls) == 2  # only the "ok" repo was requested


@responses.activate
def test_unexpected_error_in_one_repo_is_isolated(validator, tax, monkeypatch):
    mock_repo("one", {"a.py": "import pandas"})
    mock_repo("two", {"a.py": "import numpy"})
    scanner = make_scanner(validator, tax)
    original = scanner._file_signals

    def flaky(state, repo, per, blobs):
        if repo == "one":
            raise RuntimeError("boom")
        return original(state, repo, per, blobs)

    monkeypatch.setattr(scanner, "_file_signals", flaky)
    tech = scanner.scan(validation("one", "two"))
    assert tech["repos_failed"] == 1 and "RuntimeError" in tech["per_repo"][0]["error"]
    assert "NumPy" in skill_map(tech)


@responses.activate
def test_rate_limit_stops_scan_with_partial_results(validator, tax):
    mock_repo("first", {"a.py": "import pandas"})
    base = f"{API}/repos/alice/second"
    responses.add(responses.GET, base, json={"default_branch": "main"})
    responses.add(responses.GET, f"{base}/git/trees/main",
                  json={"tree": [{"path": "b.py", "type": "blob", "size": 5}], "truncated": False})
    responses.add(responses.GET, f"{base}/contents/b.py", status=403, json={"message": "limit"},
                  headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1760000000"})
    mock_repo("third")
    tech = make_scanner(validator, tax).scan(validation("first", "second", "third"))
    assert tech["status"] == "partial" and tech["complete"] is False
    assert "rate limit" in tech["incomplete_reasons"]
    assert "Pandas" in skill_map(tech)  # earlier findings are kept
    assert tech["repos_not_scanned"] == ["second", "third"]
    assert tech["per_repo"][1]["scanned"] is False and "scan stopped" in tech["per_repo"][1]["error"]
    assert "rate limit" in tech["message"].lower()


@responses.activate
def test_bad_token_stops_scan(validator, tax):
    responses.add(responses.GET, f"{API}/repos/alice/first", status=401, json={"message": "Bad credentials"})
    tech = make_scanner(validator, tax).scan(validation("first", "second"))
    assert tech["status"] == "partial" and "bad token" in tech["incomplete_reasons"]
    assert "rejected the token" in tech["message"]


@responses.activate
def test_nearly_exhausted_rate_limit_stops_early(validator, tax):
    validator._rate_remaining = 2
    tech = make_scanner(validator, tax).scan(validation("first"))
    assert tech["status"] == "partial" and len(responses.calls) == 0


@responses.activate
def test_dependency_and_ide_reads_propagate_rate_limits(validator, tax):
    base = f"{API}/repos/alice/dep"
    responses.add(responses.GET, base, json={"default_branch": "main"})
    responses.add(responses.GET, f"{base}/git/trees/main",
                  json={"tree": [{"path": "requirements.txt", "type": "blob", "size": 5}]})
    responses.add(responses.GET, f"{base}/contents/requirements.txt", status=429, json={})
    tech = make_scanner(validator, tax).scan(validation("dep"))
    assert tech["status"] == "partial" and "rate limit" in tech["incomplete_reasons"]

    responses.reset()
    base = f"{API}/repos/alice/ide"
    responses.add(responses.GET, base, json={"default_branch": "main"})
    responses.add(responses.GET, f"{base}/git/trees/main",
                  json={"tree": [{"path": ".idea/p.iml", "type": "blob", "size": 5}]})
    responses.add(responses.GET, f"{base}/contents/.idea/p.iml", status=401, json={})
    validator._rate_remaining = None
    tech = make_scanner(validator, tax).scan(validation("ide"))
    assert tech["status"] == "partial" and "bad token" in tech["incomplete_reasons"]


@responses.activate
def test_truncated_tree_marks_scan_incomplete(validator, tax):
    mock_repo("huge", {"a.py": "import pandas"}, truncated=True)
    tech = make_scanner(validator, tax).scan(validation("huge"))
    assert tech["per_repo"][0]["tree_truncated"] is True
    assert "tree truncated: huge" in tech["incomplete_reasons"] and tech["complete"] is False
    assert "Pandas" in skill_map(tech)


@responses.activate
def test_connection_error_on_a_repo_is_isolated(validator, tax):
    responses.add(responses.GET, f"{API}/repos/alice/down", body=requests.ConnectionError("boom"))
    mock_repo("up", {"a.py": "import pandas"})
    tech = make_scanner(validator, tax).scan(validation("down", "up"))
    assert tech["repos_failed"] == 1 and "Pandas" in skill_map(tech)


# --------------------------------------------------------------------------
# 10. Skipped runs
# --------------------------------------------------------------------------
@responses.activate
@pytest.mark.parametrize("status", ["user_not_found", "error", "no_public_repos", "rate_limited"])
def test_non_ok_validation_is_skipped_without_http(validator, tax, status):
    tech = make_scanner(validator, tax).scan(validation("x", status=status))
    assert tech["status"] == "skipped" and tech["complete"] is False and status in tech["message"]
    assert len(responses.calls) == 0


@responses.activate
def test_other_skip_reasons(validator, tax):
    scanner = make_scanner(validator, tax)
    no_repos = validation()
    assert "No eligible" in scanner.scan(no_repos)["message"]
    forks_only = validation("f")
    forks_only["repos"][0].update(counted=False, is_fork=True)
    assert scanner.scan(forks_only)["status"] == "skipped"
    bad_user = validation("x", extra={"username": "../evil"})
    assert "valid GitHub username" in scanner.scan(bad_user)["message"]
    assert scanner.scan(validation("x", extra={"username": None}))["status"] == "skipped"
    assert len(responses.calls) == 0


# --------------------------------------------------------------------------
# 11. Output contract
# --------------------------------------------------------------------------
@responses.activate
def test_output_is_json_sorted_and_examples_capped(validator, tax):
    files = {f"m{i}.py": "import pandas" for i in range(5)}
    files.update({"requirements.txt": "numpy\nflask", "docs/x.sql": "", "pom.xml": "<x/>"})
    mock_repo("alpha", files)
    mock_repo("beta", {"app.py": "import pandas", "requirements.txt": "numpy"})
    tech = make_scanner(validator, tax).scan(validation("alpha", "beta"))
    json.dumps(tech)
    ranks = [ts.CONFIDENCE_RANK[s["confidence"]] for s in tech["skills"]]
    assert ranks == sorted(ranks)
    pandas = skill_map(tech)["Pandas"]
    assert len(pandas["examples"]) == 3 and pandas["repos"] == ["alpha", "beta"]
    assert all(set(e) == {"repo", "path", "type"} for e in pandas["examples"])
    strong = [s for s in tech["skills"] if s["confidence"] == "strong"]
    assert strong[0]["label"] == "Pandas"  # most repos first within a confidence level
    numpy = skill_map(tech)["NumPy"]
    assert numpy["repos"] == ["alpha", "beta"] and numpy["confidence"] == "medium"
    medium_labels = [s["label"] for s in tech["skills"] if s["confidence"] == "medium"]
    assert medium_labels == sorted(medium_labels, key=lambda lab: (-len(skill_map(tech)[lab]["repos"]), lab.lower()))


@pytest.mark.parametrize("bad", [None, 5, "text", [], {"repos": 3}, {"status": "ok", "repos": "x"}])
def test_scan_never_raises_on_malformed_input(validator, tax, bad):
    tech = make_scanner(validator, tax).scan(bad)
    assert tech["status"] in ("skipped", "error") and tech["complete"] is False
    json.dumps(tech)


def test_scan_catch_all(validator, tax, monkeypatch):
    scanner = make_scanner(validator, tax)

    def explode(*args, **kwargs):
        raise KeyError("x")

    monkeypatch.setattr(scanner, "_scan", explode)
    tech = scanner.scan(validation("x"))
    assert tech["status"] == "error" and "KeyError" in tech["message"]


# --------------------------------------------------------------------------
# 12. Step 1 untouched
# --------------------------------------------------------------------------
@responses.activate
def test_validate_output_has_no_tech_stack_and_copy_is_separate(validator, tax):
    responses.add(responses.GET, f"{API}/users/alice", json={"name": "A", "html_url": "u", "public_repos": 1})
    responses.add(responses.GET, f"{API}/users/alice/repos", json=[], match=[
        responses.matchers.query_param_matcher({"type": "owner", "per_page": "100", "page": "2"})])
    responses.add(responses.GET, f"{API}/users/alice/repos", json=[
        {"name": "p", "html_url": "u/p", "fork": False, "size": 1, "pushed_at": "2025-05-01T00:00:00Z",
         "owner": {"login": "alice"}}], match=[
        responses.matchers.query_param_matcher({"type": "owner", "per_page": "100", "page": "1"})])
    responses.add(responses.GET, f"{API}/repos/alice/p/languages", json={"Python": 10})
    responses.add(responses.GET, f"{API}/repos/alice/p/commits", json=[])
    result = validator.validate("alice")
    snapshot = json.dumps(result, sort_keys=True)
    assert "tech_stack" not in result

    mock_repo("p", {"a.py": "import pandas"})
    tech = make_scanner(validator, tax).scan(result)
    combined = {**result, "tech_stack": tech}
    assert "tech_stack" in combined and "tech_stack" not in result
    assert json.dumps(result, sort_keys=True) == snapshot  # the validator result is untouched
    assert tech["status"] == "ok"


# --------------------------------------------------------------------------
# 13. compare_claimed_skills
# --------------------------------------------------------------------------
def tech_stack(complete=True):
    return {"complete": complete, "skills": [
        {"skill": "Pandas", "label": "Pandas", "in_taxonomy": True, "confidence": "strong",
         "evidence_types": ["imported_in_code", "declared_dependency"], "repos": ["a", "b"]},
        {"skill": "Flask", "label": "Flask", "in_taxonomy": True, "confidence": "medium",
         "evidence_types": ["declared_dependency"], "repos": ["a"]},
        {"skill": None, "label": "YOLO", "in_taxonomy": False, "confidence": "medium",
         "evidence_types": ["declared_dependency"], "repos": ["a"]},
    ]}


def by_claim(entries):
    return {tuple(e["claimed"]): e for e in entries}


def test_compare_statuses(tax):
    val = validation("x", extra={"summary": {"languages_combined": {
        "Python": 6000, "JavaScript": 2000, "HTML": 1000, "CSS": 500, "MATLAB": 5}}})
    claimed = ["Python", "py", "JavaScript", "HTML/CSS", "MATLAB", "Pandas", "Flask", "YOLO", "yolo",
               "REST APIs", "Machine Learning", "Agile", "Django", "Rust", "Foobar Lang", None, "SQL"]
    out = by_claim(compare_claimed_skills(claimed, val, tech_stack(), tax))

    python = out[("Python", "py")]  # duplicates collapse into one entry
    assert python["status"] == "found" and python["confidence"] == "strong"
    assert python["sources"] == [{"type": "language_stats", "bytes": 6000, "percent": round(6000 * 100 / 9505, 2)}]
    for name in ("JavaScript", "MATLAB"):
        assert out[(name,)]["status"] == "found"
    assert out[("Pandas",)]["confidence"] == "strong"
    assert {s["type"] for s in out[("Pandas",)]["sources"]} == {"imported_in_code", "declared_dependency"}
    assert out[("Flask",)]["confidence"] == "medium"
    for text in ("YOLO", "yolo"):
        assert out[(text,)]["status"] == "found" and out[(text,)]["skill"] is None
    for text in ("REST APIs", "Machine Learning", "Agile"):
        assert out[(text,)]["status"] == "not_detectable_from_github" and out[(text,)]["confidence"] is None
    assert out[("Django",)]["status"] == "not_found"
    assert out[("Rust",)]["status"] == "not_found"
    assert out[("Foobar Lang",)]["status"] == "not_in_taxonomy" and out[("Foobar Lang",)]["skill"] is None
    assert out[(None,)]["status"] == "not_in_taxonomy"
    assert out[("SQL",)]["status"] == "not_found"


def test_compare_compound_and_incomplete(tax):
    val = validation("x", extra={"summary": {"languages_combined": {"HTML": 100, "CSS": 50}}})
    out = compare_claimed_skills(["HTML/CSS", "Django"], val, tech_stack(complete=False), tax)
    assert [(e["skill"], e["status"], e["claimed"]) for e in out] == [
        ("HTML", "found", ["HTML/CSS"]), ("CSS", "found", ["HTML/CSS"]),
        ("Django", "not_found_scan_incomplete", ["Django"])]


def test_compare_with_missing_or_odd_data(tax):
    out = compare_claimed_skills(["Python", "Django"], None, None, tax)
    assert [e["status"] for e in out] == ["not_found_scan_incomplete"] * 2
    odd = {"summary": {"languages_combined": "oops"}}
    assert compare_claimed_skills(["Python"], odd, {"skills": ["junk"], "complete": True}, tax)[0]["status"] == "not_found"
    assert compare_claimed_skills(["Python"], {"summary": 3}, {}, tax)[0]["status"] == "not_found_scan_incomplete"


def test_compare_accepts_explicit_detectable_set(tax):
    out = compare_claimed_skills(["Django"], validation("x"), tech_stack(), tax, detectable=set())
    assert out[0]["status"] == "not_detectable_from_github"


def test_compare_uses_scanner_detectable_set(validator, tax):
    scanner = make_scanner(validator, tax)
    out = compare_claimed_skills(["Flask"], validation("x"), {"complete": True, "skills": []}, tax,
                                 detectable=scanner.detectable_skills())
    assert out[0]["status"] == "not_found"


# --------------------------------------------------------------------------
# 14. Paths and names
# --------------------------------------------------------------------------
@responses.activate
def test_special_characters_in_paths_and_branches_are_encoded(validator, tax):
    mock_repo("odd", {"my notebook.ipynb": json.dumps({"cells": [{"cell_type": "code", "source": "import pandas"}]}),
                      "dir with space/ünï.py": "import numpy"}, branch="feature/x y")
    tech = make_scanner(validator, tax).scan(validation("odd"))
    assert {"Pandas", "NumPy"} <= set(skill_map(tech))
    urls = [c.request.url for c in responses.calls]
    assert any("my%20notebook.ipynb" in u for u in urls)
    assert any("dir%20with%20space/%C3%BCn%C3%AF.py" in u for u in urls)
    assert any("/git/trees/feature%2Fx%20y" in u for u in urls)
    assert all(" " not in u for u in urls)


# --------------------------------------------------------------------------
# Demo script helpers (no network)
# --------------------------------------------------------------------------
def load_demo():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "examples" / "run_tech_scan.py"
    spec = importlib.util.spec_from_file_location("run_tech_scan", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_filename_is_safe_and_separate():
    from pathlib import Path
    demo = load_demo()
    assert demo.output_path_for("alice") == Path("output") / "alice_techstack.json"
    for bad in ("../../x", "bad name", "a/b", "", "a" * 40):
        assert demo.output_path_for(bad) is None


def test_demo_invalid_username_writes_nothing(tmp_path, monkeypatch, capsys):
    demo = load_demo()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["run_tech_scan.py", "bad name"])
    assert demo.main() == 1
    assert "Invalid GitHub username" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_demo_missing_token_is_friendly(tmp_path, monkeypatch, capsys):
    from skillverify import github_validator as gv
    demo = load_demo()
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(gv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["run_tech_scan.py", "alice"])
    assert demo.main() == 2
    assert "GITHUB_TOKEN" in capsys.readouterr().out


def test_demo_formatting_helpers(tax, capsys):
    demo = load_demo()
    assert demo.split_claimed(" Python, ,SQL ,REST APIs") == ["Python", "SQL", "REST APIs"]
    val = validation("x")
    lines = [demo.claim_line(e) for e in compare_claimed_skills(
        ["Python", "Pandas", "Django", "REST APIs", "Foobar"], val, tech_stack(complete=False), tax)]
    assert "language stats" in lines[0] and "[found, strong]" in lines[1]
    assert "incomplete" in lines[2] and "cannot be checked" in lines[3] and "taxonomy" in lines[4]
    assert "not found (scan was complete)" in demo.claim_line(
        {"claimed": ["X"], "status": "not_found", "confidence": None, "sources": []})
    tech = {"status": "partial", "message": "stopped", "complete": False, "incomplete_reasons": ["rate limit"],
            "repos_scanned": 1, "repos_not_scanned": ["b"], "repos_failed": 0, "api_calls_used": 7,
            "skills": [{"skill": "Pandas", "label": "Pandas", "in_taxonomy": True, "confidence": "strong",
                        "evidence_types": ["imported_in_code"], "repos": ["a"]},
                       {"skill": None, "label": "YOLO", "in_taxonomy": False, "confidence": "medium",
                        "evidence_types": ["declared_dependency"], "repos": ["a"]}],
            "unmapped_packages": [{"name": "zzz", "ecosystem": "python", "repos": 2}]}
    demo.print_tech_stack(tech)
    out = capsys.readouterr().out
    assert "INCOMPLETE" in out and "Strong evidence" in out and "YOLO" in out and "zzz" in out
    tech.update(complete=True, skills=[], unmapped_packages=[], message="")
    demo.print_tech_stack(tech)
    assert "Scan complete" in capsys.readouterr().out
    demo.print_validator_headline({"summary": {"total_public_repos": 2, "counted_repos": 1,
                                               "forked_repos_excluded": 1, "languages_combined": {"Python": 10}}})
    assert "Python" in capsys.readouterr().out
