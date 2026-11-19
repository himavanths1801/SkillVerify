"""Fix 3: a skill only matches files that can show it (by extension). Pure code and fakes, nothing real."""
import pytest
from test_questions_support import (FakeFiles, FakeWriter, github_result, good_answer, repo_record, skill_row,
                                    tech_item, tree_entry)

from skillverify_questions import config, select, service
from skillverify_questions.signals import Signals, extension_allowed

SIGNALS = Signals()


# --------------------------------------------------------------------------
# the map and the plain check
# --------------------------------------------------------------------------
def test_the_map_is_documented_data_with_clean_entries():
    assert config.SKILL_EXTENSIONS and "sql" not in config.SKILL_EXTENSIONS  # SQL keeps its own rules
    for skill, extensions in config.SKILL_EXTENSIONS.items():
        assert skill == skill.lower() and extensions and len(set(extensions)) == len(extensions)
        assert all(ext.startswith(".") and ext == ext.lower() for ext in extensions)


@pytest.mark.parametrize("skill,allowed", [
    ("Python", [".py"]), ("Flask", [".py"]), ("Django", [".py"]), ("FastAPI", [".py"]), ("Pytest", [".py"]),
    ("Pandas", [".py"]), ("NumPy", [".py"]), ("Selenium", [".py"]), ("BeautifulSoup", [".py"]), ("Scrapy", [".py"]),
    ("JavaScript", [".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"]), ("Node.js", [".js", ".mjs", ".cjs", ".ts"]),
    ("Express", [".js", ".ts"]), ("Express.js", [".js", ".ts"]), ("Jest", [".js", ".tsx"]), ("Redux", [".js", ".jsx"]),
    ("TypeScript", [".ts", ".tsx"]), ("React", [".jsx", ".tsx", ".js", ".ts"]),
    ("Next.js", [".jsx", ".tsx", ".js", ".ts"]), ("HTML", [".html", ".htm", ".jsx", ".tsx", ".vue", ".svelte", ".ejs"]),
    ("CSS", [".css", ".scss", ".sass", ".less", ".html", ".jsx", ".tsx", ".vue", ".svelte"]),
    ("Sass", [".sass", ".scss", ".vue"]), ("Tailwind CSS", [".css", ".html", ".tsx"]),
    ("Java", [".java"]), ("C#", [".cs"]), ("Go", [".go"]), ("Rust", [".rs"]), ("PHP", [".php"]), ("Ruby", [".rb"]),
    ("Kotlin", [".kt"]),
])
def test_each_listed_skill_allows_its_extensions_and_rejects_the_others(skill, allowed):
    for ext in allowed:
        assert extension_allowed(skill, f"src/deep/file{ext}") is True
        assert extension_allowed(skill.upper(), f"FILE{ext.upper()}") is True  # case-insensitive, both ways
    every = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".cs", ".go", ".rs", ".php", ".rb", ".kt", ".css", ".html",
             ".sql", ".sh", ".md", ".json", ".c"}
    full = {e for e in config.SKILL_EXTENSIONS[skill.lower()]}
    for ext in every - full:
        assert extension_allowed(skill, f"a/b{ext}") is False, (skill, ext)


def test_the_cases_from_the_live_run():
    assert not extension_allowed("Flask", "app.js") and extension_allowed("Flask", "app.py")
    assert not extension_allowed("HTML", "scraper.py") and not extension_allowed("CSS", "cleaning.py")
    assert extension_allowed("MongoDB", "user.py")  # not listed: unrestricted, exactly as before
    assert extension_allowed("React", "src/Chat.jsx") and extension_allowed("HTML", "src/Chat.jsx")
    assert extension_allowed("Python", "notebooks/explore.ipynb")  # notebooks show Python, Pandas and NumPy work


@pytest.mark.parametrize("skill", ["Docker", "SQL", "Git", "MongoDB", "Terraform", "AWS", "Some New Skill", ""])
def test_a_skill_that_is_not_in_the_map_is_unrestricted(skill):
    for path in ("a.py", "b.js", "c.sql", "d.sh", "Makefile", "e.ipynb", "f.unknown"):
        assert extension_allowed(skill, path) is True


def test_a_file_without_an_extension_never_matches_a_listed_skill():
    assert extension_allowed("Python", "Makefile") is False and extension_allowed("Python", "dir.d/script") is False


# --------------------------------------------------------------------------
# in the ranking
# --------------------------------------------------------------------------
def body(kind, extra=""):
    if kind == "py":
        parts = [f"def f{i}(x):\n    if x:\n        return x + {i}\n    return {i}\n" for i in range(6)]
    else:
        parts = [f"function f{i}(x) {{\n  if (x) {{\n    return x + {i};\n  }}\n  return {i};\n}}\n" for i in range(6)]
    return extra + "\n" + "\n".join(parts)


def candidate(path, language, text):
    return select.FileCandidate("repo", path, language, text, 60, 0, "2026-09-01T10:00:00Z")


def test_ranking_never_offers_a_file_the_skill_cannot_show():
    js = candidate("app.js", "javascript", body("js", 'const BACKEND = "Flask";'))
    py = candidate("app.py", "python", body("py", "from flask import Flask\napp = Flask(__name__)"))
    assert [c.path for c in select.rank_for_skill("Flask", [js, py], SIGNALS)] == ["app.py"]
    assert select.rank_for_skill("Flask", [js], SIGNALS) == []
    scraper = candidate("scraper.py", "python", body("py", 'KIND = "HTML"'))
    jsx = candidate("Chat.jsx", "javascript", body("js", 'const label = "HTML";'))
    assert [c.path for c in select.rank_for_skill("HTML", [scraper, jsx], SIGNALS)] == ["Chat.jsx"]
    cleaning = candidate("cleaning.py", "python", body("py", 'STYLE = "CSS"'))
    assert select.rank_for_skill("CSS", [cleaning, js], SIGNALS) == []


def test_an_unlisted_skill_ranks_files_of_any_extension_as_before():
    text = body("py", 'import mongoengine\nclient = "MongoDB"')
    assert [c.path for c in select.rank_for_skill("MongoDB", [candidate("user.py", "python", text)], SIGNALS)] == ["user.py"]


# --------------------------------------------------------------------------
# the whole path: nothing excluded reaches the model; a skill with no file is skipped as no_usable_file
# --------------------------------------------------------------------------
SCRAPER = body("py", 'import requests\nfrom bs4 import BeautifulSoup\nKIND = "HTML"')
FLASK_APP = body("py", "from flask import Flask\napp = Flask(__name__)")
CLEANING = body("py", 'import pandas as pd\nSTYLE = "CSS"')
CHAT = body("js", 'import React, { useState } from "react";\nconst label = "HTML";')
APP_JS = body("js", 'const express = require("express");\nconst BACKEND = "Flask";')


def run(skills=("Python", "Flask", "React", "CSS", "HTML")):
    trees = {"demo": [tree_entry(p, 1500) for p in ("scraper.py", "app.py", "cleaning.py", "Chat.jsx", "app.js")]}
    contents = {("demo", "scraper.py"): SCRAPER, ("demo", "app.py"): FLASK_APP, ("demo", "cleaning.py"): CLEANING,
                ("demo", "Chat.jsx"): CHAT, ("demo", "app.js"): APP_JS}
    rows = [skill_row(s) for s in skills]
    github = github_result([repo_record("demo", languages={"Python": 9000, "JavaScript": 4000})],
                           [tech_item(s, ["demo"]) for s in skills])
    prepared = service.prepare(rows, github, FakeFiles(trees, contents), SIGNALS)
    writer_fake = FakeWriter(default=lambda path: good_answer(path, ["f0"]))
    result, _ = service.write_all(prepared, writer_fake)
    return prepared, result, writer_fake


def asked(fake):
    pairs = []
    for _, user in fake.prompts:
        lines = user.splitlines()
        skill = next(l.split(": ", 1)[1] for l in lines if l.startswith("Skill to ask about:"))
        path = next(l.split(": ", 1)[1] for l in lines if l.startswith("Chosen file path:"))
        pairs.append((skill, path))
    return pairs


def test_the_excluded_file_never_reaches_the_model_and_skills_without_a_file_are_skipped():
    prepared, result, fake = run()
    pairs = asked(fake)
    assert pairs  # the run really asked questions
    for skill, path in pairs:
        assert extension_allowed(skill, path), (skill, path)
    assert ("Flask", "app.js") not in pairs and ("HTML", "scraper.py") not in pairs and ("CSS", "cleaning.py") not in pairs
    assert ("Flask", "app.py") in pairs and ("React", "Chat.jsx") in pairs
    for pick in prepared.selection.picks + prepared.selection.reserve:  # the reserve is filtered the same way
        assert extension_allowed(pick.need.skill, pick.file.path)
    skipped = {s["skill"]: s["reason"] for s in prepared.selection.skipped}
    assert skipped.get("CSS") == config.NO_USABLE_FILE
    assert result["skipped_skills"] and {s["skill"] for s in result["skipped_skills"]} >= {"CSS"}


def test_an_unlisted_skill_is_still_asked_about_from_any_file():
    skills = ("MongoDB",)
    trees = {"demo": [tree_entry("user.py", 1500)]}
    text = body("py", 'import mongoengine\nclient = "MongoDB"')
    github = github_result([repo_record("demo")], [tech_item("MongoDB", ["demo"])])
    prepared = service.prepare([skill_row(s) for s in skills], github, FakeFiles(trees, {("demo", "user.py"): text}), SIGNALS)
    assert [(p.need.skill, p.file.path) for p in prepared.selection.picks] == [("MongoDB", "user.py")]
