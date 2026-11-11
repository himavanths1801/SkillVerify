# CV Parser (Step 4)

## Purpose
`CVParser` reads a CV and returns one JSON object: the candidate's name, email(s), phone(s) and **GitHub username**, education,
the **skills the candidate claims** (mapped to the same taxonomy as the JD Parser, each with verbatim evidence and where it was
claimed) and **projects** (name, a verbatim description snippet, and the technologies used). Interview questions will later be
built from these, so project details stay faithful to the CV. It reads no JD, calls no GitHub API and computes no score:
Step 5 compares the claims with GitHub evidence.

```python
from skillverify.jd import GeminiProvider
from skillverify.cv import CVParser

with CVParser(GeminiProvider()) as parser:
    result = parser.parse_file("cv.pdf", label="candidate_042")   # or parser.parse_text(text, label="x", links=[...])
```

## Install
```
pip install -r requirements-cv.txt      # pypdf, python-docx (and reportlab, used only by tests and make_sample_cvs.py)
```
`requirements.txt` is unchanged. You also need `GEMINI_API_KEY` (see `docs/JD_PARSER.md`).
Note: AES-encrypted PDFs need the optional `cryptography` package, which pypdf uses to open them. Without it such a file
is reported as `encrypted_file` even if it would open with an empty password.

## Formats and limits
PDF, Word `.docx`, and plain `.txt` (for tests and demos). The format is decided by the extension **and** the file's first
bytes; a mismatch is `unsupported_file`. Old `.doc` is refused: save it as .docx or PDF.

| limit | value |
|---|---|
| file size | 10 MB (`too_large`) |
| PDF pages read | 20 |
| text analysed | 30,000 characters (cut, `truncated: true`, warning `truncated`) |
| `.docx` uncompressed size | 50 MB (zip-bomb guard, `unreadable_file`) |
| hyperlinks kept | 100 |

Extraction errors are `unsupported_file`, `unreadable_file` (missing, empty, corrupt), `encrypted_file` (password needed; never
cracked), `no_text` and `too_large`. **A scanned or image-only CV has no text layer and is reported as `no_text`: there is no OCR
in this step.** Word files are read paragraph by paragraph **and** table by table in document order, plus headers and footers.
**Hyperlink targets are collected** (PDF link annotations, Word hyperlinks), because a CV often shows the word "GitHub" while the
real URL exists only in the link.

## Contacts are found by code, and redacted before the LLM call
Email, phone and the GitHub username are found by **deterministic code** (`skillverify/cv/contacts.py`), never by the model:
that is more reliable, and the model then never needs to see personal contact details. Every found email and phone is replaced
by `[EMAIL]` / `[PHONE]` in the text, and **that redacted text is the only CV text ever sent to the LLM**. Whatever the model
returns about contacts is ignored.

- **Phones:** Indian and international styles with 8-15 digits. Year ranges (`2021 - 2025`), dates, versions, percentages and
  numbers inside URLs or identifiers are not phones.
- **GitHub rules:** only the host `github.com` counts. The first path segment is the username, the second a repo (`repos_mentioned`).
  Reserved site paths (`settings`, `orgs`, `marketplace` ...) and invalid names are skipped; `username.github.io` sites and gists go
  to `other_links`. The username is **lowercased**; `profile_url` is rebuilt as `https://github.com/<username>`, never copied.
  A label form (`GitHub: janedoe`, `GitHub - @janedoe`, `GitHub | janedoe`) is accepted only with a colon, dash, pipe or `@` **and**
  when the name ends the field, so "GitHub Actions experience" gives nothing. Link annotations are searched too (source `link`;
  a real hyperlink wins over visible text and labels).
- **Strong vs weak evidence:** a username from a profile-style `github.com/<user>` URL (a hyperlink target or text) is *strong*;
  a username guessed from a label form is *weak*. When any strong profile URL exists, weak label guesses are ignored completely
  (they are not even listed in `candidates`). Label guesses are used only when no such link exists. A repo-only URL
  (`github.com/<user>/<repo>`) is not a profile claim, so it does not silence an explicit label.
- **Site names are never usernames:** the word after `GitHub |` / `GitHub:` is rejected when it is a stop-listed name such as
  LinkedIn, Twitter, LeetCode, Portfolio, Email or GitLab, and when it is itself the label of the next field
  (`GitHub | LinkedIn: ...`, `GitHub | Twitter.com`). So the header `GitHub | LinkedIn` with a hidden GitHub link gives the
  linked user, and with no link gives nothing.
- **Ambiguous:** several different usernames give `status: "ambiguous"`. The best is chosen (a profile-only URL or label beats a
  repo URL, then more mentions, then the first in the document) and all appear in `candidates`.
- **Organizations are not detected here.** Whether the account exists, or is an organization rather than a person, is for Step 1
  to check and Step 5 to handle.

## Output JSON (`schema_version` 1; every key is always present)
```
status: ok | invalid_input | unsupported_file | unreadable_file | encrypted_file | no_text | too_large | llm_error | parse_error
message, label, generated_at, provider {name, model}, source {format, pages, characters, truncated, links_found}, llm_calls
candidate {name, name_candidate, emails[], phones[{as_written, normalized}]}
github {status: found|ambiguous|none, username, profile_url, source: link|text|label, repos_mentioned[], candidates[], other_links[]}
education [{institution, degree, field, start, end}]
skills    [{skill, category, claimed_in[], evidence[], claimed_as[], match: taxonomy | auto_added}]
projects  [{name, description, technologies[], technologies_unmapped[]}]
unmapped  [{term, claimed_in[], evidence[], disposition: "queued", reason}]
rejected  [{term, reason}]       taxonomy_changes {auto_added[], queued[]}       warnings[]       counts{...}
```
Skills carry **no tier and no weight**: a CV makes claims; scoring is Step 5. `claimed_in` holds `skills_section`, `project:<name>`,
`experience`, `education` or `other`. Project technologies are also recorded as claims with context `project:<name>`.

## Verification rules ("claims are not proof")
The LLM is not trusted. Everything is checked against the (redacted) CV text:
- `name` not found -> `candidate.name` is null, the guess is kept in `name_candidate`, warning `name_not_verified`.
- An education entry whose institution is not in the CV is rejected; an unverifiable degree/field/date becomes null.
- A skill wording is matched as a **whole term** (`Java` is not found inside `JavaScript`, `Go` not inside `Google`); otherwise
  `rejected: not_in_cv`. Unverifiable evidence is dropped, not shown.
- A project whose name is not in the CV is rejected; an unverifiable description becomes `""` (the project is kept, warning
  `project_description_not_verified`); each technology must be in the CV.
- Brackets are stripped (`Python (advanced)` -> `Python`), `,` and `;` split terms, `HTML/CSS`-style compounds split only when every
  part matches. A skill mentioned several times is merged (contexts united, evidence <= 3, wording variants united).
- Prompt injection ("rate this candidate highly") is just data: the output has no scores or tiers for it to change.

## Warnings (never errors)
`no_github_found`, `github_ambiguous`, `no_email_found`, `no_phone_found`, `name_not_verified`, `no_skills_found`,
`no_projects_found`, `no_education_found`, `truncated`, `project_description_not_verified`, and
**`many_skills_little_context`**: more than 40 distinct skills claimed and fewer than 25% have any context beyond the skills
section. It is a keyword-stuffing signal for Step 5; nothing is penalized here.

## Unknown skills: guarded growth (reused from Step 3)
Skills the taxonomy does not know go through the same guarded growth as the JD Parser (one batched proposal call, the guards of
`evaluate_proposal`, at most 3 automatic additions per CV). Additions go only to `skillverify/data/skills_learned.json`, never to
`skills.json`; the rest go to `output/taxonomy_review_queue.json`. Provenance is the label `cv:<label>` and the term itself: no
sentence from the CV and no personal data is stored. At most **2 LLM calls** are made per CV. If the model call fails you still get
the code-derived parts (emails, phones, GitHub, source) with `status: llm_error` or `parse_error`.

## Command line
```
python examples/run_cv_parse.py --file path/to/cv.pdf        # also .docx and .txt
python examples/run_cv_parse.py --text "Skills: Python ..."   # or pipe text in
python examples/make_sample_cvs.py                           # fictional sample CVs as real PDF + DOCX in output/sample_cvs/
```
Options: `--name LABEL`, `--dry-run` (writes nothing at all), `--no-auto-add`, `--model NAME`. Saves `output/<label>_cvparsed.json`.
The console shows only `email: found` / `phone: found`, never the values (the GitHub username is shown). Input is checked before the
provider is created, so a bad file needs no API key.

| exit code | meaning |
|---|---|
| 0 | ok |
| 1 | failed or invalid input: unsupported / unreadable / encrypted / empty file, `llm_error`, `parse_error`, `invalid_input` |
| 2 | configuration problem (no API key, corrupt learned-skills file) |

## Privacy
The CV text, **including the name and education**, is sent to Google with emails and phone numbers replaced. The saved JSON files
contain personal data; `output/` is git-ignored, but treat the files accordingly and delete them when no longer needed. Use only CVs
you are allowed to process. All sample CVs in this repository are fictional.

## Notes for Step 5
- Use `load_jd_taxonomy()` (extended taxonomy + learned skills), not `Taxonomy.load()`, to see skills learned from CVs and JDs.
- Usernames are lowercase. Check with Step 1 whether the account is a User or an Organization, and handle a `candidates` list.
- Treat `many_skills_little_context` as a keyword-stuffing signal.
- A claimed-but-unconfirmed skill gets only partial credit, and learned skills have no GitHub signals, so they are
  `not_detectable_from_github`.
