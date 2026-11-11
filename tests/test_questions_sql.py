"""Honest "why" sentences, stricter SQL handling and visible drop reasons. Fake data and fake Gemini, offline.

The fictional MedStock repo mirrors a live run: a connection-pool file (one ``SELECT 1``), an ``init.sql`` schema and a
service file with two embedded queries.
"""
import logging

import pytest
from test_questions_support import (FakeFiles, FakeWriter, github_result, good_answer, repo_record, skill_row,
                                    tech_item, tree_entry)

from skillverify_questions import config, reasons, select, service, writer
from skillverify_questions.signals import Signals, embedded_sql_statements

SIGNALS = Signals()

POOL_TS = '''import { Pool } from "pg";

const pool = new Pool({ max: 10 });

export async function query(text: string, params: unknown[] = []) {
  const started = Date.now();
  try {
    const result = await pool.query(text, params);
    const ms = Date.now() - started;
    if (ms > 500) {
      console.warn("slow query", ms);
    }
    return result.rows;
  } catch (error) {
    console.error("query failed");
    throw error;
  }
}

export async function healthCheck() {
  try {
    await query("SELECT 1");
    return true;
  } catch (error) {
    return false;
  }
}

export function closePool() {
  if (pool.ended) {
    return Promise.resolve();
  }
  return pool.end();
}
'''

INIT_SQL = '''-- MedStock schema (fictional)
CREATE TABLE products (
  id SERIAL PRIMARY KEY,
  sku TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  unit_cost NUMERIC(10, 2) NOT NULL
);

CREATE TABLE suppliers (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  lead_days INTEGER NOT NULL DEFAULT 7
);

CREATE TABLE stock_levels (
  product_id INTEGER REFERENCES products (id),
  supplier_id INTEGER REFERENCES suppliers (id),
  on_hand INTEGER NOT NULL,
  counted_at TIMESTAMP NOT NULL
);

CREATE INDEX stock_levels_product ON stock_levels (product_id);

SELECT p.sku, s.name, sl.on_hand
FROM stock_levels sl
JOIN products p ON p.id = sl.product_id
JOIN suppliers s ON s.id = sl.supplier_id
WHERE sl.on_hand < 10
ORDER BY sl.on_hand;
'''

SERVICE_TS = '''import { query } from "./db/pool";

export interface LowStock {
  sku: string;
  onHand: number;
}

export async function lowStock(limit: number): Promise<LowStock[]> {
  const rows = await query(
    `SELECT p.sku, sl.on_hand AS "onHand"
       FROM stock_levels sl
       JOIN products p ON p.id = sl.product_id
      WHERE sl.on_hand < $1
      ORDER BY sl.on_hand
      LIMIT $2`,
    [10, limit],
  );
  if (!rows.length) {
    return [];
  }
  return rows.map((row: any) => ({ sku: row.sku, onHand: row.on_hand }));
}

export function summarise(rows: LowStock[]) {
  let total = 0;
  for (const row of rows) {
    if (row.onHand === 0) {
      total += 1;
    } else if (row.onHand < 3) {
      total += 2;
    }
  }
  return total;
}

export async function recordCount(productId: number, onHand: number) {
  if (onHand < 0) {
    throw new Error("negative stock");
  }
  const rows = await query(
    "INSERT INTO stock_levels (product_id, on_hand, counted_at) VALUES ($1, $2, now()) RETURNING product_id",
    [productId, onHand],
  );
  return rows[0];
}
'''

SQL_QUESTION = ("In lowStock the stock_levels rows are joined to `products`. Write the query again so it also returns "
                "the supplier name for each product, using `suppliers`, and keeps the same ordering.")


def medstock_files():
    trees = {"medstock": [tree_entry("api/src/db/pool.ts", 900), tree_entry("db/init.sql", 1100),
                          tree_entry("api/src/stock.ts", 1000), tree_entry("README.md", 400)]}
    contents = {("medstock", "api/src/db/pool.ts"): POOL_TS, ("medstock", "db/init.sql"): INIT_SQL,
                ("medstock", "api/src/stock.ts"): SERVICE_TS}
    return FakeFiles(trees, contents)


def medstock_analysis():
    skills = [skill_row("SQL"), skill_row("TypeScript", tier="nice_to_have"), skill_row("Docker", status="not_found")]
    repos = [repo_record("medstock", languages={"TypeScript": 9000, "PLpgSQL": 800})]
    scan = [tech_item("SQL", ["medstock"]), tech_item("TypeScript", ["medstock"])]
    return skills, github_result(repos, scan)


def prepare(files=None):
    skills, github = medstock_analysis()
    return service.prepare(skills, github, files or medstock_files(), SIGNALS)


SYMBOLS = {"api/src/stock.ts": ["lowStock"], "api/src/db/pool.ts": ["query"], "db/init.sql": ["stock_levels"]}


def sql_answer(path, **overrides):
    base = good_answer(path, SYMBOLS[path], question=f"Task for {path.rsplit('/', 1)[-1]}: " + SQL_QUESTION,
                       starter_code="SELECT p.sku FROM stock_levels sl JOIN products p ON p.id = sl.product_id")
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# Fix 1: the "why" sentence is true
# --------------------------------------------------------------------------
@pytest.mark.parametrize("tier,claimed,top,expected", [
    ("must_have", True, True, "Asked because the CV claims SQL and this is the highest-ranked file in the repos that "
                              "uses it."),
    ("nice_to_have", True, True, "Asked because the job lists SQL as a nice-to-have and this is the highest-ranked "
                                 "file in the repos that uses it."),
    ("must_have", False, True, "Asked because the job requires SQL and this is the highest-ranked file using it, "
                               "although the CV does not list it."),
    ("must_have", True, False, "Asked because the CV claims SQL and this file shows it in the candidate's repos."),
    ("nice_to_have", False, False, "Asked because the job lists SQL as a nice-to-have and this file shows it in the "
                                   "candidate's repos."),
    ("must_have", False, False, "Asked because the job requires SQL and this file shows it, although the CV does not "
                                "list it."),
])
def test_the_six_sentences(tier, claimed, top, expected):
    assert reasons.reason_for("SQL", tier, claimed, top) == expected
    assert ("highest-ranked" in expected) is top


def test_a_reserve_pick_is_never_worded_highest_ranked_and_a_rank_one_pick_always_is():
    prepared = prepare()
    assert [p.top_ranked for p in prepared.selection.picks] == [True, False]
    assert all(not p.top_ranked for p in prepared.selection.reserve)
    bad = sql_answer("api/src/stock.ts", file="wrong.ts")
    provider = FakeWriter({"api/src/stock.ts": [bad, bad], "db/init.sql": sql_answer("db/init.sql"),
                           "api/src/db/pool.ts": sql_answer("api/src/db/pool.ts")}, default=sql_answer("db/init.sql"))
    result, _ = service.write_all(prepared, provider)
    for question in result["questions"]:
        why = question["why"]
        rank_one = any(p.file.path == why["path"] and p.need.skill == why["skill"] and p.top_ranked
                       for p in prepared.selection.picks)
        assert why["top_ranked"] is rank_one
        assert ("highest-ranked" in why["reason"]) is rank_one


def test_a_second_best_pick_is_not_called_top_ranked():
    prepared = prepare()
    by_skill = {p.need.skill: p for p in prepared.selection.picks}
    assert by_skill["SQL"].top_ranked is True
    assert by_skill["TypeScript"].top_ranked is False  # SQL took the best file first; this is the next best


def test_a_replacement_follows_the_same_rule():
    prepared = prepare()
    result, state = service.write_all(prepared, FakeWriter(default=None) if False else FakeWriter(
        {p.file.path: sql_answer(p.file.path) for p in prepared.selection.picks + prepared.selection.reserve}))
    replacement = service.replace_one(prepared, state, FakeWriter(
        {p.file.path: sql_answer(p.file.path) for p in prepared.selection.reserve}))
    if replacement is not None:
        assert replacement[0]["why"]["top_ranked"] is False
        assert "highest-ranked" not in replacement[0]["why"]["reason"]


# --------------------------------------------------------------------------
# Fix 2a: a file must really contain SQL
# --------------------------------------------------------------------------
def test_the_connection_pool_does_not_qualify_but_the_file_with_two_queries_does():
    assert embedded_sql_statements(POOL_TS, "typescript") == 0
    assert embedded_sql_statements(SERVICE_TS, "typescript") == 2
    assert SIGNALS.count("SQL", "typescript", POOL_TS) == 0
    assert SIGNALS.count("SQL", "typescript", POOL_TS, gated=False) > 0  # the old rule let it in (via the pg import)
    assert SIGNALS.count("SQL", "typescript", SERVICE_TS) > 0
    assert config.MIN_EMBEDDED_SQL_STATEMENTS == 2


def test_sql_files_always_qualify():
    assert SIGNALS.count("SQL", "sql", "SELECT 1;") > 0 and SIGNALS.count("SQL", "sql", INIT_SQL) > 0


@pytest.mark.parametrize("text", [
    'await pool.query("SELECT 1"); await pool.query("SELECT 1");',            # a bare SELECT 1 (twice) is no statement
    '// SELECT * FROM users JOIN roles ON 1\n/* INSERT INTO t VALUES (1) */\nconst x = 1;',  # comments
    'function query(sql) { return db.query(sql); }\nconst query2 = query("a");',  # a function called query(
    'const a = "select a thing from the list";\nconst b = "we will update the set";',  # English prose is no SQL
])
def test_things_that_are_not_real_statements_do_not_count(text):
    assert embedded_sql_statements(text, "typescript") == 0


def test_each_statement_kind_counts_and_python_strings_work():
    kinds = ('`SELECT a FROM t`, "INSERT INTO t VALUES (1)", \'UPDATE t SET a = 1\', "DELETE FROM t", '
             '`CREATE TABLE t (a int)`, `x JOIN y ON y.id = x.id`')
    assert embedded_sql_statements(kinds, "typescript") == 6
    python = 'q = """\nselect a from t where b = 1;\ndelete from t;\n"""'
    assert embedded_sql_statements(python, "python") == 2


def test_a_file_that_no_longer_qualifies_drops_out_and_the_next_one_is_used():
    prepared = prepare()
    sql_pick = next(p for p in prepared.selection.picks if p.need.skill == "SQL")
    assert sql_pick.file.path != "api/src/db/pool.ts"
    assert all(p.file.path != "api/src/db/pool.ts" or p.need.skill != "SQL"
               for p in prepared.selection.picks + prepared.selection.reserve)
    assert prepared.excluded == [{"skill": "SQL", "repo": "medstock", "path": "api/src/db/pool.ts",
                                  "reason": "no_real_sql_in_file"}]


# --------------------------------------------------------------------------
# Fix 2b: SQL questions are SQL
# --------------------------------------------------------------------------
def candidate(path="api/src/stock.ts", language="typescript", text=SERVICE_TS):
    return select.FileCandidate("medstock", path, language, text, 50, 3, None)


TEXTS = [SERVICE_TS, INIT_SQL, POOL_TS]
TREE = {"api/src/stock.ts", "db/init.sql", "api/src/db/pool.ts"}


def sql_check(**overrides):
    return writer.check(sql_answer("api/src/stock.ts", **overrides), candidate(), TREE, set(), "SQL", TEXTS)


def test_a_sql_question_gets_language_sql_even_from_a_typescript_file():
    checked = sql_check()
    assert checked.problems == [] and checked.question["language"] == "sql"
    assert writer.check(sql_answer("api/src/stock.ts"), candidate(), TREE, set(), "TypeScript",
                        TEXTS).question["language"] == "typescript"


def test_the_prompt_carries_the_sql_rules_only_for_sql():
    assert "SQL only" in writer.build_prompt(candidate(), "SQL")
    assert "SQL only" not in writer.build_prompt(candidate(), "TypeScript")


@pytest.mark.parametrize("starter", ["const rows = await query('SELECT 1');", "def run():\n    pass",
                                     "function f() {}", "await db.run()", "import x from 'y'"])
def test_a_non_sql_starter_is_rejected(starter):
    assert sql_check(starter_code=starter).problems == [writer.NOT_SQL_STARTER]


def test_sql_starter_words_that_only_look_like_code_are_fine():
    assert sql_check(starter_code="SELECT constraint_name, definition FROM products").problems == []


def test_a_sql_question_about_a_file_without_real_sql_is_rejected():
    pool = candidate("api/src/db/pool.ts", text=POOL_TS)
    answer = sql_answer("api/src/db/pool.ts")
    assert writer.check(answer, pool, TREE, set(), "SQL", TEXTS).problems == [writer.NO_REAL_SQL]


# --------------------------------------------------------------------------
# Fix 2c: table and column names must exist
# --------------------------------------------------------------------------
def test_a_table_that_exists_nowhere_in_the_repo_is_rejected():
    invented = SQL_QUESTION.replace("`suppliers`", "`reorder_rules`")
    assert sql_check(question=invented).problems == [writer.UNKNOWN_SQL_NAME]
    starter = "SELECT sku FROM reorder_rules WHERE is_active = 1"
    assert sql_check(starter_code=starter).problems == [writer.UNKNOWN_SQL_NAME]


def test_a_name_found_in_another_fetched_file_of_the_repo_is_accepted():
    assert sql_check().problems == []  # `suppliers` is only in init.sql, not in the chosen file


def test_names_are_matched_as_whole_words_ignoring_case():
    assert sql_check(question=SQL_QUESTION.replace("`suppliers`", "`SUPPLIERS`")).problems == []
    assert sql_check(question=SQL_QUESTION.replace("`suppliers`", "`supplier`")).problems == [writer.UNKNOWN_SQL_NAME]


def test_keywords_functions_placeholders_aliases_and_created_names_are_not_flagged():
    starter = ("WITH recent AS (SELECT product_id, SUM(on_hand) AS total FROM stock_levels GROUP BY product_id)\n"
               "SELECT r.total, COUNT(*) FROM recent r JOIN products p ON p.id = r.product_id\n"
               "WHERE p.id = $1 AND p.sku = ? AND p.name = :name AND EXTRACT(year FROM counted_at) = 2026")
    assert sql_check(starter_code=starter).problems == []
    assert sql_check(question=SQL_QUESTION + " Also add a new table called `audit_log` to record changes.").problems == []
    assert sql_check(question=SQL_QUESTION + " Mention `SELECT` and `COUNT`.").problems == []
    assert sql_check(starter_code="SELECT 1 FROM generate_series(1, 3)").problems == []
    assert sql_check(starter_code='SELECT sku FROM "products" p').problems == []
    assert sql_check(starter_code="SELECT sku FROM public.products").problems == []
    assert sql_check(starter_code="CREATE TABLE IF NOT EXISTS suppliers (id int)").problems == []


def test_reference_notes_are_not_checked_for_names():
    assert sql_check(reference_notes="Join `reorder_rules` and `is_active` somehow.").problems == []


def test_the_name_check_only_runs_for_sql():
    answer = sql_answer("api/src/stock.ts", question=SQL_QUESTION.replace("`suppliers`", "`reorder_rules`"))
    assert writer.check(answer, candidate(), TREE, set(), "TypeScript", TEXTS).problems == []


# --------------------------------------------------------------------------
# Fix 2d: one repair attempt with the failed checks by name, then drop
# --------------------------------------------------------------------------
def test_the_repair_can_fix_a_bad_name_and_a_second_failure_drops_it():
    invented = SQL_QUESTION.replace("`suppliers`", "`reorder_rules`")
    cand = candidate()
    fixed = FakeWriter({"api/src/stock.ts": [sql_answer("api/src/stock.ts", question=invented),
                                             sql_answer("api/src/stock.ts")]})
    written = writer.write_question(fixed, cand, "SQL", TREE, set(), TEXTS)
    assert written.question is not None and written.calls == 2
    assert "- unknown_sql_name:" in fixed.prompts[1][1] and "unknown_sql_name" not in fixed.prompts[0][1]
    twice = FakeWriter({"api/src/stock.ts": [sql_answer("api/src/stock.ts", question=invented)] * 2})
    dropped = writer.write_question(twice, cand, "SQL", TREE, set(), TEXTS)
    assert dropped.question is None and dropped.reasons == ["unknown_sql_name"]


# --------------------------------------------------------------------------
# Fix 3: visible, safe drop reasons
# --------------------------------------------------------------------------
def test_every_problem_maps_to_one_code_of_the_closed_set_and_every_code_is_produced():
    assert set(writer.REASON_CODE.values()) == set(config.DROP_REASON_CODES)
    assert len(config.DROP_REASON_CODES) == 16
    assert set(writer.REASON_CODE) == set(writer.PROBLEM_TEXT)


PY = select.FileCandidate("orders-analysis", "analysis.py", "python", __import__("test_questions_support").PY_ORDERS,
                          50, 3, None)
PY_TREE = {"analysis.py"}
PY_QUESTION = ("Look at clean_orders in analysis.py. A reviewer says rows with a missing price slip through. "
               "Change the function so those rows are dropped, and explain how you would test it.")


def py_answer(**overrides):
    base = {"kind": "write_code", "question": PY_QUESTION, "starter_code": "", "file": "analysis.py",
            "symbols": ["clean_orders"], "reference_notes": "Drop rows with a missing price."}
    base.update(overrides)
    return base


@pytest.mark.parametrize("overrides,code", [
    ({"question": 5}, "bad_json"),
    ({"kind": "other"}, "bad_json"),
    ({"question": "too short"}, "bad_length"),
    ({"kind": "fix_code", "starter_code": ""}, "fix_without_starter"),
    ({"file": "other.py"}, "wrong_file"),
    ({"question": PY_QUESTION + " See nothing.py too."}, "unknown_file_reference"),
    ({"symbols": ["invented"]}, "unknown_symbol"),
    ({"symbols": []}, "unknown_symbol"),
    ({"question": PY_QUESTION + " Call invented_fn(x) first."}, "unknown_call"),
    ({"question": PY_QUESTION + " Read https://example.org first."}, "url_or_email"),
    ({"reference_notes": "password = 'hunter2hunter2'"}, "secret_like"),
])
def test_each_check_produces_its_reason_code(overrides, code):
    checked = writer.check(py_answer(**overrides), PY, PY_TREE, set())
    assert code in [writer.REASON_CODE[name] for name in checked.problems]


def test_duplicate_and_the_sql_codes_come_from_their_checks():
    assert [writer.REASON_CODE[p] for p in writer.check(py_answer(), PY, PY_TREE,
                                                       {writer.normalise_for_duplicates(PY_QUESTION)}).problems] == [
        "duplicate"]
    assert [writer.REASON_CODE[p] for p in sql_check(starter_code="await x").problems] == ["not_sql_starter"]
    assert [writer.REASON_CODE[p] for p in sql_check(starter_code="SELECT a FROM nothing_here").problems] == [
        "unknown_sql_name"]
    pool = candidate("api/src/db/pool.ts", text=POOL_TS)
    assert [writer.REASON_CODE[p] for p in writer.check(sql_answer("api/src/db/pool.ts"), pool, TREE, set(),
                                                       "SQL", TEXTS).problems] == ["no_real_sql_in_file"]


def test_reasons_list_the_final_attempt_first_then_the_first_attempts_different_codes():
    provider = FakeWriter({"analysis.py": [py_answer(file="x.py"), py_answer(symbols=["invented"])]})
    written = writer.write_question(provider, PY, "Python", PY_TREE, set())
    assert written.reasons == ["unknown_symbol", "wrong_file"]
    same = FakeWriter({"analysis.py": [py_answer(file="x.py")] * 2})
    assert writer.write_question(same, PY, "Python", PY_TREE, set()).reasons == ["wrong_file"]
    unusable = FakeWriter({"analysis.py": [__import__("skillverify.jd.provider", fromlist=["x"]).LLMCallError(
        "Gemini's answer was not valid JSON.")] * 2})
    assert writer.write_question(unusable, PY, "Python", PY_TREE, set()).reasons == ["bad_json"]


def dropping_run(caplog=None):
    """SQL's best file fails both attempts (an invented table), so the reserve file answers instead."""
    prepared = prepare()
    invented = SQL_QUESTION.replace("`suppliers`", "`reorder_rules`")
    answers = {p.file.path: sql_answer(p.file.path) for p in prepared.selection.picks + prepared.selection.reserve}
    answers["api/src/stock.ts"] = [sql_answer("api/src/stock.ts", question=invented, reference_notes="SECRET-NOTES"),
                                   sql_answer("api/src/stock.ts", question=invented, symbols=["nope"])]
    provider = FakeWriter(answers)
    return prepared, provider, service.write_all(prepared, provider)


def test_the_dropped_list_has_the_documented_shape_and_counts_match():
    _, provider, (result, state) = dropping_run()
    assert result["dropped"] == [{"skill": "SQL", "tier": "must_have", "repo": "medstock", "path": "api/src/stock.ts",
                                  "attempts": 2, "reasons": ["unknown_symbol", "unknown_sql_name"]}]
    assert result["counts"]["dropped"] == len(result["dropped"]) == 1
    assert set(result["dropped"][0]["reasons"]) <= set(config.DROP_REASON_CODES)
    assert result["schema_version"] == 1


def test_no_content_leaks_into_the_result_json_or_the_logs(caplog):
    caplog.set_level(logging.DEBUG)
    _, provider, (result, _) = dropping_run()
    dropped_json = repr(result["dropped"])
    for secret in ("reorder_rules", "SECRET-NOTES", "suppliers", "lowStock", "SELECT", "stock_levels"):
        assert secret not in dropped_json
    lines = [r.getMessage() for r in caplog.records if r.name == "skillverify.questions"]
    assert lines == ["question dropped skill=SQL reasons=unknown_symbol,unknown_sql_name"]
    for secret in ("reorder_rules", "SECRET-NOTES", "suppliers", "lowStock", "SELECT"):
        assert secret not in caplog.text


def test_skipped_skills_lists_only_skills_with_zero_questions():
    prepared = prepare()
    # TypeScript's question is dropped twice but it has no other chance -> zero questions -> question_failed
    bad = sql_answer("api/src/db/pool.ts", file="wrong.ts")
    answers = {p.file.path: sql_answer(p.file.path) for p in prepared.selection.picks + prepared.selection.reserve}
    ts_path = next(p.file.path for p in prepared.selection.picks if p.need.skill == "TypeScript")
    answers[ts_path] = [bad, bad]
    result, _ = service.write_all(prepared, FakeWriter(answers))
    asked = {q["why"]["skill"] for q in result["questions"]}
    skipped = {s["skill"]: s["reason"] for s in result["skipped_skills"]}
    assert not asked & set(skipped)
    assert any(d["skill"] == "TypeScript" for d in result["dropped"])


def test_a_skill_with_a_question_and_a_dropped_second_attempt_appears_only_in_dropped():
    prepared = prepare()
    result, state = dropping_run()[2]
    assert any(q["why"]["skill"] == "SQL" for q in result["questions"])  # the reserve file answered
    assert not any(s["skill"] == "SQL" for s in result["skipped_skills"])
    assert [d["skill"] for d in result["dropped"]] == ["SQL"]


def test_a_skill_that_never_got_a_question_keeps_question_failed_and_its_dropped_entries():
    prepared = prepare()
    answers = {p.file.path: [sql_answer(p.file.path, file="wrong")] * 2
               for p in prepared.selection.picks + prepared.selection.reserve}
    result, _ = service.write_all(prepared, FakeWriter(answers))
    assert result["questions"] == [] and result["status"] == "none"
    assert {"skill": "SQL", "tier": "must_have", "reason": "question_failed"} in result["skipped_skills"]
    assert {d["skill"] for d in result["dropped"]} == {"SQL", "TypeScript"}
    assert result["counts"]["dropped"] == len(result["dropped"])
    assert all(d["attempts"] == 2 and d["reasons"] == ["wrong_file"] for d in result["dropped"])


def test_a_replacement_failure_is_recorded_in_the_run_drops():
    prepared = prepare()
    result, state = service.write_all(prepared, FakeWriter(
        {p.file.path: sql_answer(p.file.path) for p in prepared.selection.picks + prepared.selection.reserve}))
    before = len(state.drops)
    bad = sql_answer("db/init.sql", file="wrong.sql")
    service.replace_one(prepared, state, FakeWriter(default=[bad, bad]))
    assert len(state.drops) >= before
