# Repo Fingerprint Score (Step 5)

How well a candidate's **CV claims**, checked against their **real GitHub work**, match one **job description**.
It is a pure, offline calculation: no LLM, no network, no GitHub call. It only reads JSON saved by Steps 1-4, so the same
inputs always give the same score. **It produces a score and an evidence trail for a human interviewer. It never produces a
hiring verdict: there is no pass/fail, no threshold, and no "recommended" label anywhere.**

## The locked formula
```
Repo Fingerprint Score = ( sum of  weight x credit ) / ( sum of weight ) x 100        over every skill in the JD
weight : must_have = 3, nice_to_have = 1
credit : 1.0  claimed on the CV AND confirmed by GitHub evidence
         0.5  claimed on the CV but NOT confirmed
         0.0  not claimed on the CV
```
This is a custom original model. Everything else in the output (second figures, flags, hidden strengths) sits **next to** the
score and never changes it. Arithmetic uses exact decimals; the score is rounded half up to one decimal.

**Worked example.** JD: must-have Python, SQL, Docker; nice-to-have Git, Tableau (total weight 3+3+3+1+1 = 11). The CV claims
Python, SQL, Git and Tableau. GitHub: Python strong, Git medium, SQL not found on a complete scan, Docker not found, Tableau cannot be
checked on GitHub. Credits: Python 1.0, SQL 0.5, Docker 0 (not claimed), Git 1.0, Tableau 0.5. Points 3 + 1.5 + 0 + 1 + 0.5 = **6.0 of 11 = 54.5**.
With 2:1 weights the total is 8 (2+2+2+1+1) and the points 4.5, giving 56.3. With `NOT_DETECTABLE_CREDIT = None` Tableau leaves
the calculation (5.5 of 10 = 55.0).

## The editable weights block
At the top of `skillverify/scoring/score.py`:

| constant | default | meaning |
|---|---|---|
| `MUST_HAVE_WEIGHT` / `NICE_TO_HAVE_WEIGHT` | 3 / 1 | how much more a must-have counts (default ratio 3:1) |
| `CREDIT_CONFIRMED` | 1.0 | claimed and confirmed by GitHub evidence |
| `CREDIT_CLAIMED_UNCONFIRMED` | 0.5 | claimed, GitHub does not confirm it |
| `CREDIT_NOT_CLAIMED` | 0.0 | not claimed |
| `WEAK_EVIDENCE_CREDIT` | 0.5 | claimed, GitHub shows only a trace or a weak signal |
| `NOT_DETECTABLE_CREDIT` | 0.5 | claimed, but GitHub cannot show this kind of skill (Excel, Tableau ...). `None` leaves such skills out of the score |

They are validated when scoring starts (weights: a real number above 0, at most 1000; credits 0 to 1), otherwise `ScoringConfigError`.
A single call can override them with `weights={"must_have": 2, ...}`, and the CLI with `--must-weight` / `--nice-weight`.

## How each credit is decided
The real `compare_claimed_skills` (Step 1 extension) is run once over all JD skills; its answer is turned into a credit:

| situation | credit | reason |
|---|---|---|
| not on the CV | `CREDIT_NOT_CLAIMED` | `not_claimed` |
| claimed, found on GitHub, confidence strong or medium | `CREDIT_CONFIRMED` | `confirmed` |
| claimed, found, confidence weak (a trace, or only inferred evidence) | `WEAK_EVIDENCE_CREDIT` | `weak_evidence` |
| claimed, not found | `CREDIT_CLAIMED_UNCONFIRMED` | `not_found_on_github` |
| claimed, not found but the scan was incomplete | `CREDIT_CLAIMED_UNCONFIRMED` | `scan_incomplete` |
| claimed, GitHub cannot show it | `NOT_DETECTABLE_CREDIT` (`None`: left out) | `not_detectable` |
| anything else | `CREDIT_CLAIMED_UNCONFIRMED` | `unconfirmed` |

A skill that is **not claimed but found on GitHub** gets credit 0 (locked rule) and is listed in `hidden_strengths` for the interviewer.
**A skill missing because the scan was incomplete is never treated as a failed claim**: it gets the unconfirmed credit, not 0.

## The figures beside the score
- `claim_coverage`: the same formula with GitHub ignored (credit 1 if claimed, else 0): CV against JD only. It is filled even when no GitHub
  evidence exists.
- `checkable_only`: the locked formula over only the skills GitHub *can* check.
- `verifiable_share`: the percent of the total weight that GitHub can check. Below 50 the flag `mostly_unverifiable` is raised.
- `points_earned` / `points_possible` and `by_tier`: the sums behind the score.
- `sensitivity` (with `--sensitivity` or `include_sensitivity=True`): the same evidence re-scored at must-have weights 1:1 to 5:1, to show how
  much the chosen ratio matters. It never alters the main score.

## Status values
`ok`, `invalid_input` (a saved file is not usable; the message names the problem), `no_jd_skills`, `no_github_evidence` (no file, no public
repos, or all forks), `github_user_not_found` (neutral wording: the account named in the CV could not be found), `github_unavailable`
(rate limit, error or malformed file), `github_mismatch` (the evidence belongs to a different account than the CV names, so it was not used;
`--allow-github-mismatch` overrides this and adds a flag). Whenever the score is not computed, `repo_fingerprint` is `null`, but
`claim_coverage` is still given.

## Flags (they never change the score)
`scan_incomplete`, `tech_scan_missing`, `no_github_file`, `github_ambiguous_in_cv`, `github_username_mismatch`,
`github_supplied_without_cv_link`, `jd_has_unmapped_terms` (JD terms the taxonomy could not map are not scored),
`cv_keyword_stuffing_signal` (copied from the CV parser; no numeric effect), `cv_truncated`, `no_must_have_skills`,
`weak_evidence_used`, `mostly_unverifiable`.

## The full flow (Windows CMD)
```
python examples\run_jd_parse.py --file test_jds\test_jd_2_backend_engineer.txt --name backend
python examples\run_cv_parse.py --file sample_cvs_v2\cv2_arjun_backend.pdf
python examples\run_tech_scan_ext.py ghost
python examples\run_score.py --jd output\backend_jdparsed.json --cv output\cv2_arjun_backend_cvparsed.json --github output\ghost_techstack_ext.json
```
The saved names are `output\<job name>_jdparsed.json`, `output\<cv label>_cvparsed.json` and `output\<username>_techstack_ext.json`
(each script prints the real one). Extra options: `--sensitivity`, `--must-weight 2 --nice-weight 1`, `--allow-github-mismatch`,
`--dry-run`, `--out some.json`. The default result file is `output\<cv_label>__<job_name>_score.json`. Exit codes: 0 ok, 1 any other
status or unreadable input, 2 configuration problem.

## Limits (please read)
- The score reflects what a **public GitHub account shows, not ability**. Private repositories and work done elsewhere are invisible.
- Skills such as Excel or Tableau **cannot be seen on GitHub**; they get `NOT_DETECTABLE_CREDIT` or are left out, and `verifiable_share` tells you how much of the job is checkable.
- The same CV can parse slightly differently between runs because a language model is involved in Steps 3-4. Step 5 itself is deterministic.
- Project technology lists from the CV parser are not used by the score; only the CV's claimed skills are.
- The GitHub Validator does not tell a person from an organization account, so a CV that links an organization is not detected here
  (this needs a later Step 1 follow-up).
- Recency of commits appears only as the activity numbers in the `github` block; it is not part of the score.
- The username in the GitHub evidence must match the one in the CV (or be one of its candidates), so the wrong person's repositories are not scored.

## Fair use and privacy
The output is a **conversation aid for a human interviewer. It must not be used as an automatic accept/reject.** The score file contains the
CV label, the job name, skill names and the GitHub username only: no name, email, phone or CV text.
