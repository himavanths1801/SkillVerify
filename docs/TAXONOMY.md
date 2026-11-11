# Skill Taxonomy (Step 2)

## What it is for
A CV, a job description and GitHub all describe the same skill in different words
("JS", "Javascript", "ES6"; "React.js", "ReactJS"; "Postgres", "PostgreSQL").
The taxonomy gives SkillVerify one shared list of canonical skill names and a
function that turns messy text into those names. Later steps (JD Parser, CV Parser,
scoring) all use it, so a wrong match here would silently corrupt every score.

```python
from skillverify.taxonomy import Taxonomy

tax = Taxonomy.load()                  # or Taxonomy.load("custom.json")
tax.normalize("React.js")              # {'status': 'matched', 'skill': 'React', ...}
tax.normalize_many(["JS", "javascript", "Pyhton"])
tax.map_github_languages({"Python": 5393481, "Makefile": 3025998})
```

This module is standalone: it does not import Step 1, and Step 1 does not import it.

## Data file: `skillverify/data/skills.json`
```json
{
  "skills": [
    {
      "id": "Node.js",
      "category": "backend",
      "aliases": ["nodejs", "node js", "node"],
      "github_languages": []
    }
  ]
}
```
- `id`: the canonical display name, unique (case-insensitive). It matches automatically; do not repeat it in `aliases`.
- `category`: one of `language`, `frontend`, `backend`, `database`, `cloud_devops`, `data_ml`, `mobile`, `testing`, `tools`, `concept`.
- `aliases`: other ways people write it.
- `github_languages`: GitHub language names (exactly as GitHub reports them) that count as direct evidence of the skill, e.g. Python is `["Python"]`, Docker is `["Dockerfile"]`. Leave `[]` when no GitHub language corresponds (React, Django, AWS ...).
- Extra fields are ignored, so later steps can add their own.

## Normalization rules
1. Text is Unicode-normalized (NFKC), lowercased and trimmed; surrounding bullets and punctuation (`• Python,`) are removed. `+` and `#` are never removed, so `C++` and `C#` stay distinct.
2. Spaces, dots, hyphens, underscores and slashes are removed to form a key: `Node.js`, `node js`, `Node-JS` and `nodejs` all become `nodejs`.
3. The key is matched exactly against canonical ids, then aliases (`match_type` is `canonical` or `alias`).
4. If nothing matches, a trailing version number is stripped and matching is tried again (`Python 3.11`, `Java 17`, `C++17`; `match_type: version_stripped`). The exact key is always tried first, so `S3`, `EC2`, `HTML5` and `OAuth2` match as written. Exception: when the digits are glued to the name (no space, hyphen or underscore) and the name is 2 characters or fewer (`SHORT_NAME_LIMIT`), nothing is stripped, so `C2`, `R2` and `Go2` are unknown. `R 4.2` and `Go 1.21` still work.
5. Otherwise the result is `unknown`, with up to three `suggestions` (hints from `difflib`).
6. `normalize_many` (only) also splits compound items such as `HTML/CSS`, `HTML & CSS` or `Node.js/Express` on `/`, `&` or the word `and`, but only when the whole text is unknown and **every** part matches a skill. If any part is unknown the whole text stays one unknown item; a split is never accepted partially. Commas and `+` are never split on (`C++` stays intact). Each part's `inputs` records the original compound text. Single `normalize("HTML/CSS")` is still `unknown`.

**Exact match only: "unknown" is better than a wrong guess.** Suggestions are never turned into matches automatically (`Pyhton` is unknown, with `Python` suggested). Invalid input (None, non-strings, empty text, over 200 characters) returns `invalid_input` and never raises.

## Adding a skill or alias safely
1. Add the entry (or add to `aliases`) in `skills.json`.
2. Only add an alias if it can mean one skill and nothing else. If in doubt, leave it out.
3. Run `python -m pytest`. Loading refuses the file with a `TaxonomyError` naming the entries if two skills share an id, or if one alias (after normalization) points at two skills.
4. Only fill `github_languages` when you are sure of GitHub's exact language name.

Deliberately separate skills: Java vs JavaScript; C, C++ and C#; React vs React Native; SQL vs any specific database.

## What it can and cannot confirm
This step maps only `github_languages` today. `confirmable_via_languages(skill_id)` tells you whether a skill has any; many (React, Django, AWS, Kubernetes ...) do not, so they cannot be confirmed from language data alone. `map_github_languages` is a lookup only: it sums bytes per canonical skill and lists `unmapped_languages`; it does no scoring.

## Known limits
- Text with extra words in brackets, such as `Python (advanced)` or `JavaScript (ES6)`, is not parsed and comes back unknown. The later parsers must pass bare skill names.
- Compound items like `HTML/CSS` are split only by `normalize_many`, and only when every part matches.
- Version stripping is skipped for glued digits on names of 2 characters or fewer (so `C2` is unknown); standards such as `C99` are listed as explicit aliases.
- Extra fields in `skills.json` are dropped when the file is loaded, so a later step that adds a field must also extend `taxonomy.py` to carry it.
