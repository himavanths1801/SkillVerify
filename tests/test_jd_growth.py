"""Tests for guarded taxonomy growth: overlay, proposal guards, atomic writes, review queue. Offline."""
import json
import os

import pytest

from skillverify.jd import growth
from skillverify.jd.growth import (
    AUTO_ADD_MIN_CONFIDENCE,
    LEARNED_PATH,
    MAX_ALIASES_PER_SKILL,
    MAX_AUTO_ADDS_PER_RUN,
    MAX_NAME_LENGTH,
    QUEUE_PATH,
    QueueError,
    add_to_queue,
    apply_auto_add,
    evaluate_proposal,
    load_jd_taxonomy,
    load_queue,
    read_overlay,
)
from skillverify.taxonomy import Taxonomy, TaxonomyError
from skillverify.taxonomy_extra import load_extended_taxonomy

JD = "We run Zorbify in production, plus PostgresDB and ReactJS-lite and k8sx."


@pytest.fixture(scope="module")
def ext():
    return load_extended_taxonomy()


def proposal(**over):
    base = {"term": "Zorbify", "is_technical_skill": True, "canonical_name": "Zorbify", "category": "tools",
            "alias_of": None, "aliases": [], "confidence": 0.95}
    base.update(over)
    return base


def evaluate(ext, term="Zorbify", jd=JD, so_far=0, **over):
    return evaluate_proposal(proposal(**over), term, jd, ext, so_far)


# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------
def test_constants_and_default_paths():
    assert (AUTO_ADD_MIN_CONFIDENCE, MAX_AUTO_ADDS_PER_RUN, MAX_ALIASES_PER_SKILL, MAX_NAME_LENGTH) == (0.9, 3, 5, 60)
    assert LEARNED_PATH.name == "skills_learned.json" and LEARNED_PATH.parent.name == "data"
    assert QUEUE_PATH.name == "taxonomy_review_queue.json" and QUEUE_PATH.parent.name == "output"


# --------------------------------------------------------------------------
# guards: every one has a failing case and queues with a reason that names it
# --------------------------------------------------------------------------
def test_a_clean_proposal_is_auto_added(ext):
    decision = evaluate(ext, aliases=["zorb"])
    assert decision["action"] == "auto_add_skill" and decision["name"] == "Zorbify"
    assert decision["category"] == "tools" and decision["aliases"] == ["zorb"] and decision["confidence"] == 0.95


def test_the_jd_wording_becomes_an_alias_when_it_differs(ext):
    decision = evaluate(ext, term="Zorbify.io", jd="We use Zorbify.io", canonical_name="Zorbify")
    assert decision["aliases"] == ["Zorbify.io"]


@pytest.mark.parametrize("over", [{"is_technical_skill": False}, {"is_technical_skill": "yes"},
                                  {"is_technical_skill": None}])
def test_guard_1_must_be_a_technical_skill(ext, over):
    decision = evaluate(ext, **over)
    assert decision["action"] == "queue" and "guard 1" in decision["reason"]


@pytest.mark.parametrize("confidence", [None, "0.95", True, 1.5, -0.1, float("nan"), [0.95]])
def test_guard_2_confidence_must_be_a_real_number_from_0_to_1(ext, confidence):
    decision = evaluate(ext, confidence=confidence)
    assert decision["action"] == "queue" and "guard 2" in decision["reason"]


def test_guard_2_confidence_threshold(ext):
    below = evaluate(ext, confidence=0.89)
    assert below["action"] == "queue" and "below 0.9" in below["reason"]
    assert evaluate(ext, confidence=0.9)["action"] == "auto_add_skill"
    assert evaluate(ext, confidence=1)["action"] == "auto_add_skill"


@pytest.mark.parametrize("category", ["nonsense", None, 5, ""])
def test_guard_3_category_must_be_valid(ext, category):
    decision = evaluate(ext, category=category)
    assert decision["action"] == "queue" and "guard 3" in decision["reason"]
    assert decision["suggested_category"] is None


@pytest.mark.parametrize("name", ["", "   ", "x" * 61, "Bad<name>", "Zorb!fy", None, 5, "...", "+++"])
def test_guard_4_name_must_be_sane(ext, name):
    decision = evaluate(ext, canonical_name=name)
    assert decision["action"] == "queue" and "guard 4" in decision["reason"]


def test_guard_4_accepts_the_allowed_characters(ext):
    for name in ("C++ Builder", "Foo.js", "A-B_C/D&E", "x" * 60, "Zorb#"):
        jd = f"we use {name}"
        assert evaluate(ext, term=name, jd=jd, canonical_name=name)["action"] == "auto_add_skill", name


def test_guard_5_term_must_be_in_the_jd(ext):
    decision = evaluate(ext, term="Zorbify", jd="Nothing about it here.")
    assert decision["action"] == "queue" and "guard 5" in decision["reason"]
    assert evaluate(ext, term="zorbify", jd="we use ZORBIFY  daily")["action"] == "auto_add_skill"  # case, spaces


@pytest.mark.parametrize("name", ["Python", "node js", "python 3", "Postgres"])
def test_guard_6_name_must_not_collide(ext, name):
    decision = evaluate(ext, canonical_name=name)
    assert decision["action"] == "queue" and "guard 6" in decision["reason"]


def test_guard_7_no_near_duplicates(ext):
    by_name = evaluate(ext, canonical_name="Pyhton")
    assert by_name["action"] == "queue" and "guard 7" in by_name["reason"] and "Python" in by_name["reason"]
    by_term = evaluate(ext, term="Pyhton", jd="we use Pyhton", canonical_name="Zorbify")
    assert by_term["action"] == "queue" and "guard 7" in by_term["reason"]


def test_guard_8_aliases_are_cleaned_not_fatal(ext):
    many = [f"zorb{letter}" for letter in "abcdefgh"]
    decision = evaluate(ext, aliases=["java", "bad<alias>", 7, "Zorbify", "zorb a"] + many)
    assert decision["action"] == "auto_add_skill"
    assert len(decision["aliases"]) == MAX_ALIASES_PER_SKILL
    assert "java" not in decision["aliases"] and "Zorbify" not in decision["aliases"]
    assert decision["aliases"][0] == "zorb a"
    assert evaluate(ext, aliases="not a list")["aliases"] == []
    assert evaluate(ext, aliases=["zorb", "ZORB", "zo-rb"])["aliases"] == ["zorb"]  # same key once


def test_guard_9_per_run_cap(ext):
    assert evaluate(ext, so_far=2)["action"] == "auto_add_skill"
    decision = evaluate(ext, so_far=3)
    assert decision["action"] == "queue" and "per-run cap" in decision["reason"]


@pytest.mark.parametrize("bad", [None, "text", 5, ["x"]])
def test_malformed_proposals_are_queued(ext, bad):
    decision = evaluate_proposal(bad, "Zorbify", JD, ext, 0)
    assert decision["action"] == "queue" and decision["reason"] == "malformed proposal"


def test_queue_decisions_carry_the_suggestions(ext):
    decision = evaluate(ext, is_technical_skill=False, canonical_name=" Zorbify ", category="tools")
    assert decision["suggested_name"] == "Zorbify" and decision["suggested_category"] == "tools"
    assert evaluate(ext, is_technical_skill=False, canonical_name=5)["suggested_name"] is None
    assert evaluate(ext, is_technical_skill=False, canonical_name="  ")["suggested_name"] is None


# --------------------------------------------------------------------------
# alias of an existing skill
# --------------------------------------------------------------------------
def test_alias_with_overlapping_names_is_auto_added(ext):
    decision = evaluate(ext, term="PostgresDB", alias_of="PostgreSQL")
    assert decision == {"action": "auto_add_alias", "reason": "all guards passed", "skill": "PostgreSQL",
                        "alias": "PostgresDB", "confidence": 0.95}
    contains = evaluate(ext, term="ReactJS-lite", alias_of="React")
    assert contains["action"] == "auto_add_alias" and contains["skill"] == "React"


def test_alias_of_an_unrelated_name_is_queued(ext):
    decision = evaluate(ext, term="k8sx", alias_of="Kubernetes")
    assert decision["action"] == "queue" and decision["reason"] == "alias target unrelated by name"


@pytest.mark.parametrize("target", ["NoSuchSkill", "postgresql", 5, ["PostgreSQL"]])
def test_alias_of_must_name_an_existing_skill_exactly(ext, target):
    decision = evaluate(ext, term="PostgresDB", alias_of=target)
    assert decision["action"] == "queue" and "alias_of" in decision["reason"]


def test_alias_empty_string_means_a_new_skill(ext):
    assert evaluate(ext, alias_of="")["action"] == "auto_add_skill"


def test_alias_guards_still_apply(ext):
    insane = evaluate(ext, term="Postgres<DB>", jd="we use Postgres<DB>", alias_of="PostgreSQL")
    assert insane["action"] == "queue" and "guard 4" in insane["reason"]
    existing = evaluate(ext, term="Postgres", jd="we use Postgres", alias_of="PostgreSQL")
    assert existing["action"] == "queue" and "guard 6" in existing["reason"]
    capped = evaluate(ext, term="PostgresDB", alias_of="PostgreSQL", so_far=3)
    assert "per-run cap" in capped["reason"]
    low = evaluate(ext, term="PostgresDB", alias_of="PostgreSQL", confidence=0.5)
    assert "guard 2" in low["reason"]


def test_names_overlap_helper():
    assert growth._names_overlap("Postgres", "PostgreSQL") and growth._names_overlap("ReactJS", "React")
    assert growth._names_overlap("abcdXY", "abcdZZ")           # shared 4-character prefix
    assert not growth._names_overlap("k8s", "Kubernetes")
    assert not growth._names_overlap("...", "Kubernetes") and not growth._names_overlap("abc", "---")


# --------------------------------------------------------------------------
# the overlay file
# --------------------------------------------------------------------------
def skill_decision(**over):
    base = {"action": "auto_add_skill", "reason": "ok", "name": "Zorbify", "category": "tools",
            "aliases": ["zorb"], "confidence": 0.95}
    base.update(over)
    return base


def alias_decision(**over):
    base = {"action": "auto_add_alias", "reason": "ok", "skill": "PostgreSQL", "alias": "PostgresDB",
            "confidence": 0.93}
    base.update(over)
    return base


META = {"source_jd": "demo_job", "evidence": "We run Zorbify in production", "date": "2026-10-19T00:00:00+00:00"}


def test_missing_and_empty_overlay_give_the_168_skill_taxonomy(tmp_path):
    assert len(load_jd_taxonomy(tmp_path / "missing.json").skills) == 168
    for content in ("", "   \n", "{}", '{"skills": [], "aliases": []}'):
        path = tmp_path / "x.json"
        path.write_text(content, encoding="utf-8")
        assert len(load_jd_taxonomy(path).skills) == 168


def test_apply_auto_add_skill_writes_provenance_and_the_skill_matches(tmp_path):
    path = tmp_path / "learned.json"
    outcome = apply_auto_add(skill_decision(), path, META)
    assert outcome == {"applied": True, "reason": "added"}
    saved = json.loads(path.read_text(encoding="utf-8"))
    entry = saved["skills"][0]
    assert entry["id"] == "Zorbify" and entry["category"] == "tools" and entry["aliases"] == ["zorb"]
    assert entry["added_by"] == "jd_parser" and entry["source_jd"] == "demo_job" and entry["confidence"] == 0.95
    assert entry["date"] == META["date"] and entry["evidence"] == META["evidence"] and entry["github_languages"] == []
    taxonomy = load_jd_taxonomy(path)
    assert len(taxonomy.skills) == 169
    assert taxonomy.normalize("zorb")["skill"] == "Zorbify" and taxonomy.normalize("ZORBIFY")["status"] == "matched"
    assert taxonomy.get("Zorbify")["category"] == "tools"
    assert not list(tmp_path.glob("*.tmp"))  # no temp files left behind


def test_apply_auto_add_alias_and_learned_alias_matches(tmp_path):
    path = tmp_path / "learned.json"
    assert apply_auto_add(alias_decision(), path, {"source_jd": "j"})["applied"] is True
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["aliases"][0]["skill"] == "PostgreSQL" and saved["aliases"][0]["date"]  # date defaults to now
    taxonomy = load_jd_taxonomy(path)
    assert taxonomy.normalize("PostgresDB")["skill"] == "PostgreSQL"
    assert len(taxonomy.skills) == 168


def test_second_add_appends_and_aliases_can_target_learned_skills(tmp_path):
    path = tmp_path / "learned.json"
    apply_auto_add(skill_decision(), path, META)
    apply_auto_add(skill_decision(name="Quuxify", aliases=[]), path, META)
    assert apply_auto_add(alias_decision(skill="Zorbify", alias="Zorbify2000"), path, META)["applied"] is True
    taxonomy = load_jd_taxonomy(path)
    assert len(taxonomy.skills) == 170 and taxonomy.normalize("Zorbify2000")["skill"] == "Zorbify"


def test_base_taxonomies_are_never_mutated(tmp_path):
    path = tmp_path / "learned.json"
    apply_auto_add(skill_decision(), path, META)
    apply_auto_add(alias_decision(skill="Python", alias="Pythonista"), path, META)
    taxonomy = load_jd_taxonomy(path)
    assert "Pythonista" in taxonomy.get("Python")["aliases"]
    fresh = load_extended_taxonomy()
    assert "Pythonista" not in fresh.get("Python")["aliases"] and len(fresh.skills) == 168
    assert len(Taxonomy.load().skills) == 165 and fresh.get("Zorbify") is None


def test_write_failure_leaves_nothing_half_written(tmp_path, monkeypatch):
    path = tmp_path / "learned.json"

    def broken_replace(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(growth.os, "replace", broken_replace)
    outcome = apply_auto_add(skill_decision(), path, META)
    assert outcome["applied"] is False and "disk full" in outcome["reason"]
    assert not path.exists() and list(tmp_path.iterdir()) == []  # not even a temp file


def test_write_failure_keeps_an_existing_overlay_intact(tmp_path, monkeypatch):
    path = tmp_path / "learned.json"
    apply_auto_add(skill_decision(), path, META)
    before = path.read_bytes()
    monkeypatch.setattr(growth.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    assert apply_auto_add(skill_decision(name="Other", aliases=[]), path, META)["applied"] is False
    assert path.read_bytes() == before and [p.name for p in tmp_path.iterdir()] == ["learned.json"]


def test_cleanup_failure_does_not_hide_the_original_error(tmp_path, monkeypatch):
    monkeypatch.setattr(growth.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("first")))
    monkeypatch.setattr(growth.os, "unlink", lambda *a, **k: (_ for _ in ()).throw(OSError("second")))
    outcome = apply_auto_add(skill_decision(), tmp_path / "learned.json", META)
    assert outcome["applied"] is False and "first" in outcome["reason"]


def test_a_taxonomy_that_cannot_be_built_is_never_written(tmp_path):
    path = tmp_path / "learned.json"
    clash = apply_auto_add(skill_decision(name="Python", aliases=[]), path, META)
    assert clash["applied"] is False and "same key" in clash["reason"]
    alias_clash = apply_auto_add(skill_decision(aliases=["js"]), path, META)
    assert alias_clash["applied"] is False and "collision" in alias_clash["reason"]
    unknown_target = apply_auto_add(alias_decision(skill="NoSuchSkill"), path, META)
    assert unknown_target["applied"] is False
    assert not path.exists()


def test_apply_rejects_other_decisions_and_bad_shapes(tmp_path):
    path = tmp_path / "learned.json"
    assert apply_auto_add({"action": "queue"}, path, META)["applied"] is False
    assert apply_auto_add({"action": "auto_add_skill"}, path, META)["applied"] is False  # missing fields
    assert apply_auto_add(skill_decision(aliases=None), path, META)["applied"] is False
    assert not path.exists()


def test_apply_with_a_corrupt_overlay_changes_nothing(tmp_path):
    path = tmp_path / "learned.json"
    path.write_text("{corrupt", encoding="utf-8")
    outcome = apply_auto_add(skill_decision(), path, META)
    assert outcome["applied"] is False and "not valid JSON" in outcome["reason"]
    assert path.read_text(encoding="utf-8") == "{corrupt"


@pytest.mark.parametrize("content, message", [
    ("{corrupt", "not valid JSON"),
    ("[1, 2]", "must contain a JSON object"),
    ('{"skills": "x"}', "must be lists"),
    ('{"aliases": {}}', "must be lists"),
    ('{"skills": ["text"]}', "has no 'id'"),
    ('{"skills": [{"category": "tools"}]}', "has no 'id'"),
    ('{"skills": [{"id": "  ", "category": "tools"}]}', "has no 'id'"),
    ('{"skills": [{"id": "Foo", "category": "nonsense"}]}', "invalid category"),
    ('{"skills": [{"id": "Foo", "category": "tools", "aliases": "x"}]}', "list of strings"),
    ('{"skills": [{"id": "Foo", "category": "tools", "aliases": [3]}]}', "list of strings"),
    ('{"aliases": ["text"]}', "Learned alias #1"),
    ('{"aliases": [{"skill": "NoSuchSkill", "alias": "x"}]}', "Learned alias #1"),
    ('{"aliases": [{"skill": "Python", "alias": ""}]}', "Learned alias #1"),
    ('{"aliases": [{"skill": "Python", "alias": 5}]}', "Learned alias #1"),
    ('{"skills": [{"id": "Python", "category": "tools"}]}', "same key"),
    ('{"skills": [{"id": "Foo", "category": "tools", "aliases": ["js"]}]}', "Alias collision"),
    ('{"aliases": [{"skill": "Python", "alias": "js"}]}', "Alias collision"),
])
def test_corrupt_overlays_are_reported_never_ignored(tmp_path, content, message):
    path = tmp_path / "learned.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(TaxonomyError, match=message):
        load_jd_taxonomy(path)


def test_unreadable_overlay_is_reported(tmp_path):
    with pytest.raises(TaxonomyError, match="Could not read"):
        read_overlay(tmp_path)  # a directory
    bad_bytes = tmp_path / "b.json"
    bad_bytes.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(TaxonomyError, match="Could not read"):
        read_overlay(bad_bytes)


# --------------------------------------------------------------------------
# the review queue
# --------------------------------------------------------------------------
def entry(term="Zorbify", **over):
    base = {"term": term, "reason": "low confidence", "suggested_name": "Zorbify",
            "suggested_category": "tools", "source_jd": "job_a"}
    base.update(over)
    return base


def test_queue_adds_dedupes_counts_and_merges(tmp_path):
    path = tmp_path / "queue.json"
    add_to_queue([entry(), entry("Quuxify", suggested_name=None, suggested_category=None)], path, now="t1")
    queue = add_to_queue([entry("zorbify", reason="still low", source_jd="job_b"),
                          entry("ZORB-ify", reason="still low", source_jd="job_a")], path, now="t2")
    zorb = next(e for e in queue if e["key"] == "zorbify")
    assert zorb["seen_count"] == 3 and zorb["source_jds"] == ["job_a", "job_b"]
    assert zorb["first_seen"] == "t1" and zorb["last_seen"] == "t2" and zorb["reason"] == "still low"
    assert zorb["term"] == "Zorbify" and len(queue) == 2
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["version"] == 1 and on_disk["entries"] == queue
    assert load_queue(path) == queue and not list(tmp_path.glob("*.tmp"))


def test_queue_fills_in_missing_suggestions_later_but_never_overwrites(tmp_path):
    path = tmp_path / "queue.json"
    add_to_queue([entry(suggested_name=None, suggested_category=None)], path)
    queue = add_to_queue([entry(suggested_name="First", suggested_category="tools")], path)
    assert queue[0]["suggested_name"] == "First" and queue[0]["suggested_category"] == "tools"
    queue = add_to_queue([entry(suggested_name="Second", suggested_category="concept")], path)
    assert queue[0]["suggested_name"] == "First" and queue[0]["suggested_category"] == "tools"


def test_queue_skips_entries_without_a_usable_term(tmp_path):
    path = tmp_path / "queue.json"
    queue = add_to_queue([{"reason": "x"}, entry(term=None), entry(term=5), entry(term="..."), entry(reason="")], path)
    assert [e["term"] for e in queue] == ["Zorbify"] and queue[0]["reason"] == ""


def test_queue_tolerates_hand_edited_entries(tmp_path):
    path = tmp_path / "queue.json"
    path.write_text(json.dumps({"entries": [{"term": "Zorbify", "key": "zorbify", "reason": "old"}]}), encoding="utf-8")
    queue = add_to_queue([entry()], path)
    assert queue[0]["seen_count"] == 1 and queue[0]["source_jds"] == ["job_a"]


def test_missing_and_empty_queue_files_are_empty_queues(tmp_path):
    assert load_queue(tmp_path / "missing.json") == []
    empty = tmp_path / "empty.json"
    empty.write_text("  \n", encoding="utf-8")
    assert load_queue(empty) == []


@pytest.mark.parametrize("content", ["{corrupt", "[1]", '{"entries": "x"}', '{"entries": [1, 2]}', '{"other": 1}'])
def test_corrupt_queue_is_reported_and_never_overwritten(tmp_path, content):
    path = tmp_path / "queue.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(QueueError):
        add_to_queue([entry()], path)
    with pytest.raises(QueueError):
        load_queue(path)
    assert path.read_text(encoding="utf-8") == content


def test_unreadable_queue_is_reported(tmp_path):
    with pytest.raises(QueueError, match="Could not read"):
        load_queue(tmp_path)  # a directory
    bad_bytes = tmp_path / "b.json"
    bad_bytes.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(QueueError, match="Could not read"):
        load_queue(bad_bytes)


def test_queue_write_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "queue.json"
    add_to_queue([entry()], path)
    before = path.read_bytes()
    monkeypatch.setattr(growth.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    with pytest.raises(OSError):
        add_to_queue([entry("Other")], path)
    assert path.read_bytes() == before and [p.name for p in tmp_path.iterdir()] == ["queue.json"]


def test_default_queue_location_when_no_path_given(tmp_path, monkeypatch):
    target = tmp_path / "default_queue.json"
    monkeypatch.setattr(growth, "QUEUE_PATH", target)
    add_to_queue([entry()])
    assert load_queue()[0]["term"] == "Zorbify" and target.exists()


def test_default_learned_location_when_no_path_given(tmp_path, monkeypatch):
    target = tmp_path / "default_learned.json"
    monkeypatch.setattr(growth, "LEARNED_PATH", target)
    assert apply_auto_add(skill_decision(), None, META)["applied"] is True and target.exists()
    assert len(load_jd_taxonomy().skills) == 169 and read_overlay()["skills"][0]["id"] == "Zorbify"


def test_the_real_overlay_and_queue_were_never_created():
    assert not LEARNED_PATH.exists() and not QUEUE_PATH.exists()
    assert os.path.basename(str(LEARNED_PATH)) == "skills_learned.json"
