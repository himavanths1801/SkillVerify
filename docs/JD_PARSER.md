# JD Parser (Step 3)

## Purpose
`JDParser` reads the raw text of a job description, asks an LLM (Google Gemini) to extract the technical skills and say
how strongly each is required, maps them onto the skill taxonomy, and returns JSON. It reads no CV, calls no GitHub API
and computes no score (that is Step 5).

```python
from skillverify.jd import GeminiProvider, JDParser

with JDParser(GeminiProvider()) as parser:
    result = parser.parse(jd_text, job_name="data_analyst")
```

## Setup: the API key
Put your key in `.env` (never commit it; `.env` is git-ignored):
```
GEMINI_API_KEY=your_key_here
GEMINI_MODEL=gemini-3.6-flash      # optional
```
Model names change. If you get "model not found" (HTTP 404), set `GEMINI_MODEL` to a current model name.
The key is sent in a request header, never in the URL, and never appears in errors or output.

**Privacy: the job description text is sent to Google. Check the terms of your key's plan before using confidential JDs.**

## Command line
```
python examples/run_jd_parse.py --file examples/sample_jds/data_analyst.txt
python examples/run_jd_parse.py --text "Must have Python. Nice to have Docker."
type job.txt | python examples/run_jd_parse.py --name my_job
```
Options: `--name JOBNAME` (letters, digits, `.` `_` `-`; default: the file name), `--dry-run` (print only: no output file,
no learned-skills file, no review queue), `--no-auto-add` (never add to the taxonomy), `--model NAME`.
Saves `output/<jobname>_jdparsed.json`.

| exit code | meaning |
|---|---|
| 0 | ok |
| 1 | failed or invalid input: `llm_error`, `parse_error`, `invalid_input`, `empty_jd`, unreadable file |
| 2 | configuration problem (no API key, corrupt learned-skills file) |

The script never prints a traceback or the key. Input is checked before the key is needed, so an empty text or a missing
file exits with 1 even without a key.

## Output JSON (`schema_version` 1; every key is always present)
```
status: ok | empty_jd | invalid_input | llm_error | parse_error      message
job_name, generated_at, provider {name, model}, jd {characters, truncated}, llm_calls
skills:   [{skill, category, tier, tier_basis, evidence[], claimed_as[], match: taxonomy | auto_added}]
unmapped: [{term, tier, tier_basis, evidence[], disposition: "queued", reason}]
rejected: [{term, reason}]
taxonomy_changes: {auto_added: [...], queued: [...]}
counts: {skills, must_have, nice_to_have, unmapped, rejected, auto_added, queued}
```
`jd.characters` is the number of characters actually analysed (after cutting at 30,000, with `truncated: true`).
**Tiers only: there are no numeric weights here.** Step 5 owns the 3/1 weights.

## Tier rules
The LLM reports a `signal` per skill; code maps it:
- `required` and the evidence snippet is found verbatim in the JD -> `must_have` (`tier_basis: explicit_cue`)
- `required` but the evidence is not found verbatim -> `nice_to_have` (`downgraded_evidence_not_found`)
- `preferred` -> `nice_to_have` (`explicit_cue`)
- `none` or anything else -> `nice_to_have` (`default_no_signal`)

**Why default to `nice_to_have`:** a skill with no clear "must" wording should not become a hard requirement by accident.
A skill mentioned twice keeps its strongest tier (`must_have` wins); up to 3 evidence snippets are kept.

**Descriptive phrasing is not a requirement.** Phrases such as "you have used Docker", "you know X" or "you are familiar with X"
(for example in an "About you" list) are not treated as required. Only explicit cues (must, required, mandatory, essential,
"need to", "minimum qualifications") or a Requirements / Must-have section make a `must_have`; otherwise the skill is
`nice_to_have`. This wording is given to the model in the extraction prompt; the tier logic in code is unchanged
(`required` still needs evidence found verbatim in the JD). Because an LLM is involved, results can vary slightly between
runs, or between models, on genuinely ambiguous wording.

## Temporary Gemini errors are retried
HTTP 500, 502, 503 and 504 responses and network errors/timeouts (typical when the free tier is briefly overloaded) are retried
up to 2 times, waiting 2 s and then 6 s, inside the single provider call. If all 3 attempts fail you get the usual
`llm_error` message (for example `Gemini returned HTTP 503.`). 429 (rate limit), 401/403, 404, any other 4xx and answer-shape
problems (empty or invalid answers) are **not** retried. The "at most 2 LLM calls per JD" rule and the `llm_calls` count do not
change: a retry is not a new call.

## Anti-hallucination checks (all in code)
- The `as_written` wording must appear in the JD (whitespace and case ignored), otherwise it is `rejected: not_in_jd`.
- Evidence the model cannot prove is not shown as evidence, and cannot create a `must_have`.
- Matching uses the verbatim `as_written` text only; the model's own `name` is just a hint for the proposal stage.
- Bracketed text is removed first (`Python (advanced)` -> `Python`), terms with `,` or `;` are split, and `HTML/CSS`-style
  compounds are split by the taxonomy only if every part matches.
- Prompt injection: the JD is wrapped in delimiters and marked as untrusted data, and tiers come from code, so text inside
  the JD cannot decide them. At most 2 LLM calls are made per JD; at most 60 skills and 400 characters of evidence are kept.

## Guarded autonomy (growing the taxonomy)
Terms the taxonomy does not know go to one batched proposal call. A proposal is added automatically only if **every** guard passes:
1. the model says it is a technical skill; 2. confidence is a number from 0 to 1 and at least **0.9**; 3. the category is valid;
4. the name is 1-60 characters of letters, digits, space and `. + # - _ / &`; 5. the term really is in the JD;
6. the name's key does not collide with an existing skill or alias; 7. neither the name nor the term is a near-duplicate
(the taxonomy offers no "did you mean" suggestions); 8. at most 5 aliases, invalid or colliding ones dropped;
9. fewer than **3** automatic additions so far in this run.

A term that is just another spelling of an existing skill is added as an alias only if `alias_of` names that skill exactly
and the two names overlap (one contains the other, or they share a 4-character prefix): "Postgres" -> PostgreSQL yes,
"k8s" -> Kubernetes no (it is queued: "alias target unrelated by name").

Everything that fails a guard goes to the **review queue**, `output/taxonomy_review_queue.json`: one accumulating file,
deduplicated by the term's key, with `seen_count` and the JDs it came from. A corrupt queue file is reported and never overwritten.

Automatic additions never touch `skills.json`. They are written (atomically, after the new taxonomy has been built
successfully in memory) to the overlay `skillverify/data/skills_learned.json`, which only exists after the first real
auto-add. Review the file and the queue from time to time; delete an entry from the overlay to undo it.

## Notes for later steps
- **Steps 4 and 5 must use `load_jd_taxonomy()`** (extended taxonomy + learned skills), not `Taxonomy.load()`, or learned skills will be unknown to them.
- Auto-added skills have no GitHub signals, so they will be `not_detectable_from_github` when checked.
- Step 5 owns the 3/1 weights and the score. Everything here is evidence of what the JD says, not a judgement about a candidate.
