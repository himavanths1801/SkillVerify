"""Unit tests for the skill taxonomy (Step 2). No network, no mocking."""
import json

import pytest

from skillverify import taxonomy as tx
from skillverify.taxonomy import Taxonomy, TaxonomyError


@pytest.fixture(scope="module")
def tax():
    return Taxonomy.load()


def write_skills(tmp_path, skills):
    path = tmp_path / "skills.json"
    path.write_text(json.dumps({"skills": skills}), encoding="utf-8")
    return path


def skill(sid, category="language", aliases=(), langs=()):
    return {"id": sid, "category": category, "aliases": list(aliases), "github_languages": list(langs)}


# 1. Data quality of the real file -----------------------------------------
def test_real_file_data_quality(tax):
    assert len(tax.skills) >= 100
    ids = [s["id"].lower() for s in tax.skills]
    assert len(ids) == len(set(ids))
    for s in tax.skills:
        assert s["category"] in tx.CATEGORIES
        assert isinstance(s["github_languages"], list)
        assert all(isinstance(x, str) for x in s["github_languages"])
    # every category is represented
    assert {s["category"] for s in tax.skills} == set(tx.CATEGORIES)
    # Taxonomy.load() already refuses alias collisions; re-check explicitly
    keys = {}
    for s in tax.skills:
        for text in [s["id"], *s["aliases"]]:
            assert keys.setdefault(tx.make_key(text), s["id"]) == s["id"]


# 2. Matches ---------------------------------------------------------------
@pytest.mark.parametrize("text, skill_id, match_type", [
    ("JS", "JavaScript", "alias"),
    ("javascript", "JavaScript", "canonical"),
    ("JavaScript", "JavaScript", "canonical"),
    ("ReactJS", "React", "alias"),
    ("React.js", "React", "alias"),
    ("Node", "Node.js", "alias"),
    ("node.js", "Node.js", "canonical"),
    ("NodeJS", "Node.js", "canonical"),
    ("Golang", "Go", "alias"),
    ("k8s", "Kubernetes", "alias"),
    ("Postgres", "PostgreSQL", "alias"),
    ("c sharp", "C#", "alias"),
    ("Python3", "Python", "alias"),
    ("Python 3.11", "Python", "version_stripped"),
    ("Django 4", "Django", "version_stripped"),
    ("Angular 2", "Angular", "version_stripped"),
    ("Java 17", "Java", "version_stripped"),
    ("C++17", "C++", "version_stripped"),
    ("Python 3.11+", "Python", "version_stripped"),
    ("HTML5", "HTML", "alias"),
    ("S3", "Amazon S3", "alias"),
])
def test_matches(tax, text, skill_id, match_type):
    result = tax.normalize(text)
    assert (result["status"], result["skill"], result["match_type"]) == ("matched", skill_id, match_type)
    assert result["category"] == tax.get(skill_id)["category"]
    assert result["input"] == text
    assert "suggestions" not in result


# 3. Must not confuse ------------------------------------------------------
def test_similar_skills_stay_separate(tax):
    assert tax.normalize("Java")["skill"] == "Java"
    assert tax.normalize("JavaScript")["skill"] == "JavaScript"
    assert tax.normalize("C")["skill"] == "C"
    assert tax.normalize("C++")["skill"] == "C++"
    assert tax.normalize("C#")["skill"] == "C#"
    assert tax.normalize("React")["skill"] == "React"
    assert tax.normalize("React Native")["skill"] == "React Native"
    assert tax.normalize("Golang")["skill"] == tax.normalize("Go")["skill"] == "Go"
    assert tax.normalize("SQL")["skill"] == "SQL"
    assert tax.normalize("MySQL")["skill"] == "MySQL"
    result = tax.normalize("script")
    assert result["status"] == "unknown" and result["skill"] is None


# 4. Unknown and hints -----------------------------------------------------
def test_gibberish_is_unknown_without_suggestions(tax):
    result = tax.normalize("qwzxv blorp")
    assert result["status"] == "unknown"
    assert result["skill"] is None and result["match_type"] is None
    assert result["suggestions"] == []


def test_typo_gives_suggestion_but_no_match(tax):
    result = tax.normalize("Pyhton")
    assert result["status"] == "unknown" and result["skill"] is None
    assert "Python" in result["suggestions"]
    assert len(result["suggestions"]) <= 3


# 5. Bad input -------------------------------------------------------------
@pytest.mark.parametrize("bad", [None, "", " ", "•", 123, ["Python"], "x" * 500, "3.11"[:0]])
def test_invalid_input(tax, bad):
    result = tax.normalize(bad)
    assert result["status"] == "invalid_input"
    assert result["skill"] is None


def test_version_only_text_is_unknown(tax):
    assert tax.normalize("3.11")["status"] == "unknown"


def test_invalid_input_keeps_a_safe_input_value(tax):
    assert tax.normalize(["Python"])["input"] == "['Python']"


# 6. Messy text ------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    ("• Python,", "Python"),
    ("- Python.", "Python"),
    ("  PYTHON ", "Python"),
    ("Node JS", "Node.js"),
    ("ＪａｖａＳｃｒｉｐｔ", "JavaScript"),          # full-width characters
    ("Java​Script", "JavaScript"),               # zero-width space
    ("(Docker)", "Docker"),
    ("C++", "C++"),
    ("C++,", "C++"),
    ("C#", "C#"),
    ("• C#;", "C#"),
])
def test_messy_text(tax, text, expected):
    assert tax.normalize(text)["skill"] == expected


def test_plus_and_hash_are_not_stripped():
    assert tx.make_key("C++") == "c++" and tx.make_key("C#") == "c#"
    assert tx.make_key("Node-JS") == tx.make_key("node js") == tx.make_key("Node.js") == "nodejs"


# 7. normalize_many --------------------------------------------------------
def test_normalize_many(tax):
    out = tax.normalize_many(["JS", "Python", "javascript", "Pyhton", None, "JavaScript", "Pyhton"])
    assert [m["skill"] for m in out["matched"]] == ["JavaScript", "Python"]
    assert out["matched"][0]["inputs"] == ["JS", "javascript", "JavaScript"]
    assert out["matched"][0]["category"] == "language"
    assert [u["input"] for u in out["unknown"]] == ["Pyhton", "Pyhton"]  # order and repeats kept
    assert "Python" in out["unknown"][0]["suggestions"]
    assert out["invalid"] == [{"input": None}]


def test_normalize_many_edge_inputs(tax):
    assert tax.normalize_many(None) == {"matched": [], "unknown": [], "invalid": []}
    assert tax.normalize_many("Python")["matched"][0]["skill"] == "Python"  # not split into letters
    assert tax.normalize_many(i for i in ["Go"])["matched"][0]["skill"] == "Go"


# 8. GitHub language lookup --------------------------------------------------
def test_github_language_to_skills(tax):
    assert tax.github_language_to_skills("C++") == ["C++"]
    assert tax.github_language_to_skills("Dockerfile") == ["Docker"]
    assert tax.github_language_to_skills("dockerfile") == ["Docker"]
    assert tax.github_language_to_skills("  c#  ") == ["C#"]
    assert tax.github_language_to_skills("Makefile") == []
    assert tax.github_language_to_skills(None) == []


def test_get_and_confirmable(tax):
    assert tax.get("Python")["category"] == "language"
    assert tax.get("python") is None  # get() wants the exact canonical id
    assert tax.get("Nope") is None
    assert tax.confirmable_via_languages("Python") is True
    assert tax.confirmable_via_languages("React") is False
    assert tax.confirmable_via_languages("Nope") is False


# 9. map_github_languages ----------------------------------------------------
def test_map_github_languages_real_shape(tax):
    languages = {"C": 1453629546, "Assembly": 9614575, "Rust": 6581417, "Shell": 6305342,
                 "Python": 5393481, "Makefile": 3025998, "Perl": 1143900, "JavaScript": 296567}
    out = tax.map_github_languages(languages)
    assert out["skills"] == {"C": 1453629546, "Rust": 6581417, "Bash": 6305342,
                             "Python": 5393481, "Perl": 1143900, "JavaScript": 296567}
    assert out["unmapped_languages"] == ["Assembly", "Makefile"]
    json.dumps(out)


def test_map_github_languages_sums_bytes_and_rejects_non_dict(tmp_path):
    small = Taxonomy.load(write_skills(tmp_path, [skill("SQL", "database", langs=["SQL", "TSQL"])]))
    out = small.map_github_languages({"SQL": 100, "TSQL": 50, "Other": 7})
    assert out == {"skills": {"SQL": 150}, "unmapped_languages": ["Other"]}
    with pytest.raises(TypeError):
        small.map_github_languages(["SQL"])


# 10. Loader errors ------------------------------------------------------------
def test_missing_file(tmp_path):
    with pytest.raises(TaxonomyError, match="not found"):
        Taxonomy.load(tmp_path / "nope.json")


def test_unreadable_path(tmp_path):
    with pytest.raises(TaxonomyError, match="Could not read"):
        Taxonomy.load(tmp_path)  # a directory, not a file


def test_invalid_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(TaxonomyError, match="not valid JSON"):
        Taxonomy.load(bad)


@pytest.mark.parametrize("content", ['{"skills": []}', "[]", '{"other": 1}'])
def test_no_skills(tmp_path, content):
    path = tmp_path / "s.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(TaxonomyError, match="no skills"):
        Taxonomy.load(path)


def test_duplicate_ids_case_insensitive(tmp_path):
    path = write_skills(tmp_path, [skill("Python"), skill("python")])
    with pytest.raises(TaxonomyError, match="Duplicate skill id.*Python.*python"):
        Taxonomy.load(path)


def test_missing_or_bad_category(tmp_path):
    with pytest.raises(TaxonomyError, match="'Foo'.*category"):
        Taxonomy.load(write_skills(tmp_path, [{"id": "Foo"}]))
    with pytest.raises(TaxonomyError, match="'Foo'.*category"):
        Taxonomy.load(write_skills(tmp_path, [skill("Foo", category="nonsense")]))


def test_missing_id_and_non_object_entries(tmp_path):
    with pytest.raises(TaxonomyError, match="missing an 'id'"):
        Taxonomy.load(write_skills(tmp_path, [{"category": "language"}]))
    with pytest.raises(TaxonomyError, match="not an object"):
        Taxonomy.load(write_skills(tmp_path, ["Python"]))


def test_bad_alias_and_language_lists(tmp_path):
    with pytest.raises(TaxonomyError, match="'aliases' must be a list of strings"):
        Taxonomy.load(write_skills(tmp_path, [{"id": "A", "category": "tools", "aliases": "x"}]))
    with pytest.raises(TaxonomyError, match="'github_languages' must be a list of strings"):
        Taxonomy.load(write_skills(tmp_path, [{"id": "A", "category": "tools", "github_languages": [1]}]))


def test_alias_collision_between_skills(tmp_path):
    path = write_skills(tmp_path, [skill("Alpha", aliases=["shared"]), skill("Beta", aliases=["Shared"])])
    with pytest.raises(TaxonomyError, match="Alias collision.*Alpha.*Beta"):
        Taxonomy.load(path)


def test_alias_colliding_with_another_skills_id(tmp_path):
    path = write_skills(tmp_path, [skill("Java"), skill("JS", aliases=["java"])])
    with pytest.raises(TaxonomyError, match="Alias collision.*Java.*JS"):
        Taxonomy.load(path)


def test_ids_that_normalize_to_same_key(tmp_path):
    path = write_skills(tmp_path, [skill("Node.js"), skill("NodeJS")])
    with pytest.raises(TaxonomyError, match="same key"):
        Taxonomy.load(path)


def test_empty_keys_rejected(tmp_path):
    with pytest.raises(TaxonomyError, match="Alias.*empty"):
        Taxonomy.load(write_skills(tmp_path, [skill("A", aliases=["..."])]))
    with pytest.raises(TaxonomyError, match="id.*empty"):
        Taxonomy.load(write_skills(tmp_path, [skill("...")]))


def test_alias_equal_to_own_id_is_harmless(tmp_path):
    tax = Taxonomy.load(write_skills(tmp_path, [skill("Node.js", aliases=["NodeJS", "nodejs"])]))
    assert tax.normalize("nodejs")["match_type"] == "canonical"


# 11. Extra fields ignored -------------------------------------------------------
def test_unknown_extra_fields_are_ignored(tmp_path):
    entry = skill("Python", aliases=["py"])
    entry["weight"] = 5
    entry["notes"] = {"anything": True}
    tax = Taxonomy.load(write_skills(tmp_path, [entry]))
    assert tax.normalize("py")["skill"] == "Python"
    assert "weight" not in tax.get("Python")


# ---------------------------------------------------------------------------
# Follow-up #1
# ---------------------------------------------------------------------------
import importlib.util
from pathlib import Path

NEW_SKILLS = [
    ("Artificial Intelligence", "concept"), ("NoSQL", "database"), ("Streamlit", "data_ml"),
    ("SciPy", "data_ml"), ("Seaborn", "data_ml"), ("Docker Compose", "cloud_devops"),
    ("Hibernate", "backend"), ("MariaDB", "database"), ("Apache Cassandra", "database"),
    ("Neo4j", "database"), ("Helm", "cloud_devops"), ("Prometheus", "cloud_devops"),
    ("Grafana", "cloud_devops"), ("Gradle", "tools"), ("Mocha", "testing"),
]


# 1. Glued digits on very short names are not stripped
@pytest.mark.parametrize("text", ["C2", "R2", "Go2", "C4"])
def test_short_names_with_glued_digits_are_unknown(tax, text):
    result = tax.normalize(text)
    assert result["status"] == "unknown" and result["skill"] is None


def test_short_name_limit_constant():
    assert tx.SHORT_NAME_LIMIT == 2


# 2. Explicit aliases replace what stripping used to do
@pytest.mark.parametrize("text, skill_id", [
    ("C99", "C"), ("C11", "C"), ("c17", "C"), ("py3", "Python"), ("ES2015", "JavaScript"),
])
def test_standard_aliases(tax, text, skill_id):
    result = tax.normalize(text)
    assert (result["skill"], result["match_type"]) == (skill_id, "alias")


# 3. Stripping still works with a separator or longer names
@pytest.mark.parametrize("text, skill_id", [
    ("R 4.2", "R"), ("Go 1.21", "Go"), ("C++17", "C++"),
    ("Python 3.11", "Python"), ("Java 17", "Java"), ("Django4", "Django"), ("Java17", "Java"),
])
def test_version_stripping_still_works(tax, text, skill_id):
    result = tax.normalize(text)
    assert (result["skill"], result["match_type"]) == (skill_id, "version_stripped")


def test_c_with_separator_still_matches(tax):
    # "C 11" is also the exact alias "c11", so it matches as an alias.
    assert tax.normalize("C 11")["skill"] == "C"
    assert tax.normalize("C 99")["skill"] == "C"
    assert tax.normalize("C 5")["match_type"] == "version_stripped"


# 4. Torch is ambiguous
def test_torch_unknown_pytorch_matches(tax):
    assert tax.normalize("Torch")["status"] == "unknown"
    assert tax.normalize("PyTorch")["skill"] == "PyTorch"


# 5. New skills
@pytest.mark.parametrize("skill_id, category", NEW_SKILLS)
def test_new_skills_load_and_match(tax, skill_id, category):
    assert tax.get(skill_id)["category"] == category
    assert tax.get(skill_id)["github_languages"] == []
    assert tax.normalize(skill_id)["skill"] == skill_id


def test_new_skill_aliases_and_separation(tax):
    assert tax.normalize("AI")["skill"] == "Artificial Intelligence"
    assert tax.normalize("cassandra")["skill"] == "Apache Cassandra"
    assert tax.normalize("no sql")["skill"] == "NoSQL"
    assert tax.normalize("NoSQL Databases")["skill"] == "NoSQL"
    assert tax.normalize("Docker Compose")["skill"] == "Docker Compose"
    assert tax.normalize("Docker")["skill"] == "Docker"
    assert tax.normalize("docker-compose")["skill"] == "Docker Compose"


# 6. Size of the real file
def test_real_file_size_and_no_collisions(tax):
    assert len(tax.skills) == 165  # 150 before + the 15 skills in the table
    keys = {}
    for s in tax.skills:
        for text in [s["id"], *s["aliases"]]:
            assert keys.setdefault(tx.make_key(text), s["id"]) == s["id"]


# 7-8. Compound items in normalize_many
def test_compound_html_css(tax):
    out = tax.normalize_many(["HTML/CSS"])
    assert [m["skill"] for m in out["matched"]] == ["HTML", "CSS"]
    assert all(m["inputs"] == ["HTML/CSS"] for m in out["matched"])
    assert out["unknown"] == [] and out["invalid"] == []


def test_compound_variants(tax):
    out = tax.normalize_many(["HTML & CSS", "Node.js/Express", "C/C++", "Python and Java"])
    assert [m["skill"] for m in out["matched"]] == [
        "HTML", "CSS", "Node.js", "Express.js", "C", "C++", "Python", "Java"]
    html = out["matched"][0]
    assert html["inputs"] == ["HTML & CSS"]
    assert out["unknown"] == []


def test_compound_dedupes_with_plain_items(tax):
    out = tax.normalize_many(["HTML", "HTML/CSS", "HTML/HTML"])
    html = next(m for m in out["matched"] if m["skill"] == "HTML")
    assert html["inputs"] == ["HTML", "HTML/CSS", "HTML/HTML"]


# 9. Never accept a split partially
@pytest.mark.parametrize("text", ["HTML/Foobar", "HTML & Foobar", "Foobar and CSS", "HTML/ /Foobar"])
def test_compound_with_unknown_part_stays_whole(tax, text):
    out = tax.normalize_many([text])
    assert out["matched"] == []
    assert [u["input"] for u in out["unknown"]] == [text]


def test_dangling_separator_is_just_trimmed_punctuation(tax):
    # Surrounding punctuation is stripped before matching, so this is a whole-text match.
    assert tax.normalize("HTML/")["skill"] == "HTML"
    out = tax.normalize_many(["HTML//CSS"])  # empty middle part -> not every part matches
    assert out["matched"] == [] and len(out["unknown"]) == 1


def test_commas_and_plus_are_not_split(tax):
    out = tax.normalize_many(["C++", "Python, Java"])
    assert [m["skill"] for m in out["matched"]] == ["C++"]
    assert [u["input"] for u in out["unknown"]] == ["Python, Java"]


# 10. Whole-text matches win; single normalize does not split
def test_whole_text_matches_are_not_split(tax):
    out = tax.normalize_many(["CI/CD", "Data Structures and Algorithms"])
    assert [m["skill"] for m in out["matched"]] == ["CI/CD", "Data Structures and Algorithms"]
    assert out["matched"][0]["inputs"] == ["CI/CD"]


def test_single_normalize_does_not_split(tax):
    assert tax.normalize("HTML/CSS")["status"] == "unknown"


# 11. Demo script handles a malformed output file
def _load_demo():
    path = Path(__file__).resolve().parent.parent / "examples" / "run_taxonomy.py"
    spec = importlib.util.spec_from_file_location("run_taxonomy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("languages, message", [
    (["Python"], "is not a dict"),
    ("Python", "is not a dict"),
    ({"Python": "lots"}, "not numbers"),
    ({"Python": True}, "not numbers"),
])
def test_demo_malformed_languages_combined(tax, tmp_path, capsys, languages, message):
    bad = tmp_path / "out.json"
    bad.write_text(json.dumps({"summary": {"languages_combined": languages}}), encoding="utf-8")
    assert _load_demo().show_github_json(tax, bad) == 1
    printed = capsys.readouterr().out
    assert "does not look like a validator output" in printed and message in printed


def test_demo_other_bad_files(tax, tmp_path, capsys):
    demo = _load_demo()
    assert demo.show_github_json(tax, tmp_path / "missing.json") == 1
    notjson = tmp_path / "x.json"
    notjson.write_text("{oops", encoding="utf-8")
    assert demo.show_github_json(tax, notjson) == 1
    noshape = tmp_path / "y.json"
    noshape.write_text("[1]", encoding="utf-8")
    assert demo.show_github_json(tax, noshape) == 1
    good = tmp_path / "z.json"
    good.write_text(json.dumps({"summary": {"languages_combined": {"Python": 10, "Zzz": 5}}}), encoding="utf-8")
    assert demo.show_github_json(tax, good) == 0
    assert "Zzz" in capsys.readouterr().out
