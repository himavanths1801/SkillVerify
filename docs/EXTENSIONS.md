# Extensions (extra skills and strict SQL detection)

## What is new
Two optional improvements, all in **new files**. Nothing that already existed was changed: the
165-skill `skills.json`, `TechScanner`, `run_tech_scan.py` and every test behave exactly as before.
The new behavior is used only through the files below.

| file | purpose |
|---|---|
| `skillverify/data/skills_extra.json` | three extra skills |
| `skillverify/taxonomy_extra.py` | extended taxonomy loader, label promotion, `extended_detectable` |
| `skillverify/sql_strict.py` | strict SQL check and `StrictSqlTechScanner` |
| `examples/run_tech_scan_ext.py` | command-line demo using both |

## 1. Extra skills: YOLO, PyCharm, VS Code
`load_extended_taxonomy()` loads the base taxonomy plus `skills_extra.json` (168 skills). It refuses an extra id that
repeats a base id (case-insensitive) and any alias collision. `YOLOv8`, `yolo-v5`, `VSCode`, `vs-code` and
`Visual Studio Code` match; **`Visual Studio` (a different product), `JetBrains IDE` and `Torch` stay unknown on purpose.**

`promote_label_items(tech, taxonomy)` returns a copy of a scan result in which label-only items the taxonomy knows
(YOLO, PyCharm, VS Code) become proper skill items (`skill`, `in_taxonomy`, `category`), plus a `promoted_labels` list.
`JetBrains IDE` stays label-only because it is not a skill. Nothing is promoted twice and the input is never changed.

**Detectable or not.** `extended_detectable(scanner)` is the scanner's set plus `YOLO`: YOLO comes from the
`ultralytics` / `yolov5` packages, so "not found" means something when the scan is complete. **PyCharm and VS Code are
deliberately left out**: `.idea/` and `.vscode/` are often gitignored, so absence proves nothing, and an unfound claim
stays `not_detectable_from_github`.

## 2. Strict SQL detector (opt-in)
`looks_like_sql_strict` is the old `looks_like_sql` **and** these guards, so it can never accept anything the old test rejects:
1. The statement must **start** the text: SELECT, INSERT INTO, DELETE FROM, UPDATE, CREATE TABLE/DATABASE/INDEX/VIEW, WITH or EXPLAIN. SQL in the middle of a sentence is rejected.
2. After removing one trailing `;`, text ending in `.` or `!` is rejected (`?` is a SQL placeholder, so it is not tested).
3. WITH and EXPLAIN need nothing more.
4. SELECT: a select list with no `*`, `(` or `,` must be one identifier (or `identifier AS identifier`); after the table only a clause word (where, join, group, order, limit ...), `;`, `)`, `,`, a comment, or one alias word followed by one of those is allowed.
5. INSERT INTO table must be followed by `(`, VALUES, SELECT or DEFAULT.
6. DELETE FROM table: nothing, `;`, `)`, a comment, or WHERE / USING / RETURNING.
7. UPDATE table must be followed by `SET column =`.
8. CREATE: TABLE needs `(` or AS; INDEX needs `ON table`; VIEW needs AS; DATABASE needs nothing or `;`, `)`, CHARACTER, COLLATE, WITH.

`StrictSqlTechScanner` is a `TechScanner` with the same constructor that uses this check; everything else in a scan
(file signals, `.sql` files, dependencies, imports, caps, `complete`, output shape) is identical. Use it instead of
`TechScanner` when you want fewer false SQL matches. **Known limit:** a prose line that starts with a SQL-shaped phrase and
contains only identifier-like words, such as `SELECT ALL RESULTS FROM SEARCH PAGE`, can still pass.

## Running the demo
```
python examples/run_tech_scan_ext.py <github-username> [--max-repos 40] [--claimed "Python and Docker, YOLO, VS Code"]
```
It needs `GITHUB_TOKEN` like the other demos, scans with `StrictSqlTechScanner`, promotes the label-only tools, and
saves `output/<username>_techstack_ext.json` (never the other demos' files). Claim lines are labelled with the
canonical skill, e.g. `Docker (claimed as "Python and Docker")`.

For the scoring step: the old scanner and the strict scanner both report evidence from the candidate's repos, not proof of who wrote the code.
