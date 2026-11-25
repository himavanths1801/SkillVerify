"""Every tunable number and list for the question generator, in one place. Change a value here, nowhere else.

The selection logic that uses these values is LOCKED (see docs/QUESTIONS.md); only the values may be tuned.
"""
import re

# ============================================================================
# HOW MANY QUESTIONS
# ============================================================================
MAX_QUESTIONS = 5                  # a session never has more
MIN_QUESTIONS = 3                  # fewer is reported honestly ("too_few"), never padded
MAX_REPLACEMENT_CANDIDATES = 3     # extra files tried when a question is dropped or the recruiter asks for a new one

# ============================================================================
# HOW MUCH GITHUB IS READ (the existing scan is reused; only file contents are fetched)
# ============================================================================
MAX_REPOS_CONSIDERED = 5           # repos whose file trees are listed
MAX_FILES_PER_REPO_FETCHED = 8     # file contents fetched per repo (after the path filter, biggest code files first)
MAX_FILE_FETCHES = 40              # file contents fetched in total
MAX_FILE_BYTES = 200_000           # bigger files are never fetched
MIN_FILE_BYTES = 400               # smaller files are never fetched
MIN_LOGIC_LINES = 15               # a fetched file with fewer logic lines is "tiny" and dropped
TOP_FILES_PER_SKILL = 3            # best files kept per skill (the first is used, the rest feed the reserve)

# ============================================================================
# HOW A FILE IS RANKED ("logic score"; a plain sum, no randomness)
#   logic score = min(logic lines, LOGIC_LINES_CAP)
#               + DEFINITION_WEIGHT * min(definitions, DEFINITIONS_CAP)
#               + CONTROL_FLOW_WEIGHT * min(control-flow constructs, CONTROL_FLOW_CAP)
# Tie-break, in this order: higher logic score, more skill-signal lines, repo pushed more recently, path alphabetical.
# ============================================================================
LOGIC_LINES_CAP = 300              # a huge file cannot win just by size
DEFINITION_WEIGHT = 4              # per function / class / method definition
DEFINITIONS_CAP = 40
CONTROL_FLOW_WEIGHT = 2            # per if / for / while / try / match / catch / with
CONTROL_FLOW_CAP = 60

# ============================================================================
# WHAT GOES TO GEMINI
# ============================================================================
MAX_PROMPT_CODE_LINES = 250        # the file is cut to this many lines in the prompt (checks use the whole file)
WRITER_SETTINGS_VERSION = 4        # bump when the prompt, the checks or the selection rules change: saved question sets stop matching
MAX_QUESTION_ATTEMPTS = 2          # Gemini calls per question: the first try plus ONE repair

# ============================================================================
# WHAT A VALID QUESTION LOOKS LIKE
# ============================================================================
QUESTION_MIN_CHARS = 60
QUESTION_MAX_CHARS = 700
STARTER_MAX_LINES = 40
STARTER_MAX_CHARS = 2500
NOTES_MAX_CHARS = 800
MAX_SYMBOLS = 8
DUPLICATE_PREFIX_CHARS = 80        # two questions with the same first 80 normalised characters are duplicates

# ============================================================================
# FILE FILTER
# ============================================================================
SKIP_FOLDERS = frozenset({
    # vendored, dependency and build folders
    "node_modules", "vendor", "venv", ".venv", "env", "site-packages", "dist", "build", "target", "out",
    "__pycache__", ".git", "third_party", "bower_components", ".idea", ".vscode", ".gradle", ".next", "coverage",
    # copies of other people's code kept inside a repo. Plain "lib" is deliberately NOT here: many projects keep their
    # own real source in lib/, and skipping it would hide the candidate's code.
    "libs", "external", "extern", "deps", "third-party", "thirdparty", "vendored",
    # test folders
    "test", "tests", "__tests__", "spec", "e2e",
})
GENERATED_NAME_PATTERNS = (
    re.compile(r"(?i)\.min\.[^/]*$"), re.compile(r"(?i)\.lock$"), re.compile(r"(?i)(^|/)package-lock\.json$"),
    re.compile(r"(?i)_pb2(_grpc)?\.py$"), re.compile(r"(?i)\.generated\.[^/]*$"), re.compile(r"(?i)\.bundle\.[^/]*$"),
)
TEST_NAME_PATTERNS = (
    re.compile(r"(^|/)test_[^/]*\.py$"), re.compile(r"_test\.py$"), re.compile(r"(?i)\.test\.[^/]*$"),
    re.compile(r"(?i)\.spec\.[^/]*$"), re.compile(r"(^|/)conftest\.py$"),
)
SECRET_NAME_PATTERNS = (
    re.compile(r"(?i)(^|/)\.env[^/]*$"), re.compile(r"(?i)\.pem$"), re.compile(r"(?i)\.key$"),
    re.compile(r"(?i)(^|/)id_rsa[^/]*$"), re.compile(r"(?i)\.p12$"),
    re.compile(r"(?i)(^|/)secrets?\.[^/]*$"), re.compile(r"(?i)(^|/)credentials?\.[^/]*$"),
)
GENERATED_HEADER_LINES = 5         # the first lines checked for a "generated" notice
GENERATED_HEADER = re.compile(r"(?i)auto-?generated|generated by|do not edit")

# Code extensions only. Anything else (docs, data, images, binaries) never becomes a question source.
# The language name is what the question tells the student; the model never chooses it.
EXTENSION_LANGUAGE = {
    ".py": "python", ".ipynb": "python", ".sql": "sql", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".java": "java", ".kt": "kotlin", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cs": "csharp", ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php", ".r": "r",
    ".sh": "shell", ".scala": "scala",
}
# Line comments per language (block comments are removed first for the C-like and SQL languages).
LINE_COMMENT = {
    "python": "#", "shell": "#", "ruby": "#", "r": "#", "sql": "--",
    "javascript": "//", "typescript": "//", "java": "//", "kotlin": "//", "c": "//", "cpp": "//", "csharp": "//",
    "go": "//", "rust": "//", "php": "//", "scala": "//",
}
BLOCK_COMMENT_LANGUAGES = frozenset({"sql", "javascript", "typescript", "java", "kotlin", "c", "cpp", "csharp",
                                     "go", "rust", "php", "scala"})

# ============================================================================
# SKILL SIGNALS
# ============================================================================
MIN_ALIAS_CHARS = 4                # shorter aliases ("py", "js", "go") are too ambiguous to match as plain words
# File names that look like a file in a question but are really a product name (Node.js, Vue.js ...).
PRODUCT_NAMES_WITH_EXTENSION = re.compile(
    r"(?i)^(node|next|vue|react|express|nuxt|d3|three|chart|p5|ember|backbone|angular|socket|alpine|nest|deno|bun)\.js$")

# Extensions so short that "obj.c" or "x.r" in code is far more likely an attribute than a file: when a question
# mentions such a name it only counts as a file name if it has a folder part or is quoted.
AMBIGUOUS_EXTENSIONS = frozenset({".c", ".h", ".r", ".go", ".rs", ".kt", ".cs"})

# ============================================================================
# SKIP REASONS (a closed set) AND THE "WHY" SENTENCES (fixed text, written by code, never by Gemini)
# ============================================================================
NOT_FOUND_IN_CODE = "not_found_in_code"
NOT_CHECKABLE_FROM_CODE = "not_checkable_from_code"
NO_USABLE_FILE = "no_usable_file"
QUESTION_FAILED = "question_failed"
NOT_ENOUGH_ROOM = "not_enough_room"
SKIP_REASONS = (NOT_FOUND_IN_CODE, NOT_CHECKABLE_FROM_CODE, NO_USABLE_FILE, QUESTION_FAILED, NOT_ENOUGH_ROOM)

REASON_MUST_CLAIMED = ("Asked because the CV claims {skill} and this is the highest-ranked file in the repos "
                       "that uses it.")
REASON_NICE_TO_HAVE = ("Asked because the job lists {skill} as a nice-to-have and this is the highest-ranked file "
                       "in the repos that uses it.")
REASON_MUST_NOT_CLAIMED = ("Asked because the job requires {skill} and this is the highest-ranked file using it, "
                           "although the CV does not list it.")

# ============================================================================
# SQL QUESTIONS
# ============================================================================
# A file that is not a .sql file shows the skill SQL only with at least this many real statements (verb plus clause:
# SELECT..FROM, INSERT INTO, UPDATE..SET, DELETE FROM, CREATE TABLE, JOIN..ON) inside string or template literals.
MIN_EMBEDDED_SQL_STATEMENTS = 2
# Words that are never checked as table or column names in a SQL question (keywords and common functions).
SQL_NON_NAMES = frozenset(w.lower() for w in """
select from where join inner outer left right full cross on using as and or not in is null like ilike between exists
case when then else end group by order having limit offset union all distinct insert into values update set delete
create table view index alter drop add column primary key foreign references default unique constraint if with
recursive over partition asc desc true false interval date time timestamp year month day hour by returning
sum count avg min max coalesce round cast extract now concat lower upper trim length substring row_number rank
dense_rank lag lead nullif greatest least abs ceil floor
""".split())

# ============================================================================
# WHY A QUESTION WAS DROPPED (a closed set of short codes; never any content)
# ============================================================================
DROP_REASON_CODES = ("bad_json", "bad_length", "wrong_file", "unknown_file_reference", "unknown_symbol",
                     "unknown_call", "url_or_email", "secret_like", "duplicate", "fix_without_starter",
                     "not_sql_starter", "unknown_sql_name", "no_real_sql_in_file",
                     "fix_without_example", "fix_example_vacuous", "fix_not_modified", "multiple_tasks",
                     "bug_hint_in_starter")
# Why a model's answer was unusable JSON (a closed set; added to a dropped entry as ``causes``, never any content):
#   empty_response  the provider returned no text
#   cut_off         the provider said the output limit was reached, or the braces never balance
#   not_json        text without a JSON object in it
#   wrong_shape     JSON, but not an object, or a required field is missing or has the wrong type
ANSWER_CAUSES = ("empty_response", "cut_off", "not_json", "wrong_shape")

# Which field made an answer ``wrong_shape`` (a closed set of NAMES; never a value). The first six are the fields the
# writer asks for; the last three are the parts of a fix question's ``failing_example`` (named in the repair hint of
# ``fix_without_example``, a check that keeps its own drop code).
SHAPE_FIELDS = ("kind", "question", "starter_code", "file", "symbols", "reference_notes")
FAILING_EXAMPLE_FIELDS = ("failing_example.input", "failing_example.expected", "failing_example.actual")

# ============================================================================
# HARMLESS VARIANTS THAT ARE NORMALISED BEFORE THE CHECKS (conservative; every other difference is judged as it is)
# ============================================================================
# ``kind``: lower case, with spaces, underscores and hyphens read as one space, then looked up here.
KIND_VARIANTS = {"write code": "write_code", "code": "write_code", "fix code": "fix_code", "fix the bug": "fix_code",
                 "bug fix": "fix_code", "fix": "fix_code"}
# ``language`` (lower case). Any other language name is left exactly as the model gave it. (The language of a
# question is set by code from the file's extension; the model's value is never used.)
LANGUAGE_ALIASES = {"js": "javascript", "ts": "typescript", "py": "python", "node": "javascript",
                    "nodejs": "javascript"}
# A field the model named differently: used only when the canonical field is absent, first alias present wins.
FIELD_ALIASES = {"question": ("question_text", "prompt"), "starter_code": ("starter", "code"),
                 "reference_notes": ("notes", "solution_notes")}
# The steps that can fire (a closed set of codes; kept only in a log line and counted, never with content).
NORMALISE_CODES = ("kind_normalised", "language_normalised", "starter_code_defaulted", "starter_code_joined",
                   "alias_question", "alias_starter_code", "alias_reference_notes", "whitespace_trimmed")

# ============================================================================
# NO BUG HINTS IN FIX STARTERS
# A comment the model ADDED to a fix question's starter code is removed by code (see comments.py). If one that could not
# be removed safely contains one of these words or phrases (whole words, any case), the question is dropped
# (``bug_hint_in_starter``). Never echoed anywhere.
# ============================================================================
HINT_WORDS = ("bug", "buggy", "wrong", "incorrect", "mistake", "error here", "fix this", "fixme", "should be",
              "off-by-one", "off by one", "broken", "typo")

# ============================================================================
# ONE TASK PER QUESTION
# A question that offers alternatives is rejected (``multiple_tasks``). Conservative on purpose: ordinary sentences such
# as "true or false", "null or undefined" or "positive or negative" are never matched.
# ============================================================================
ALTERNATIVE_PATTERNS = (
    re.compile(r"(?i)\balternatively\b"),
    re.compile(r"(?i)\bas an alternative\b"),
    re.compile(r"(?i)\bor\s+(?:else|instead)\b"),
    re.compile(r"(?i)\beither\b[^.?!]{0,150}?\bor\s+(?:else\s+)?(?:to\s+)?"
               r"(?:write|add|implement|modify|create|build|fix|refactor|change|update|rewrite|extend)\b"),
)

# ============================================================================
# STARTER CODE FOR write_code QUESTIONS (a real excerpt of the file, cut by code; see excerpt.py)
# ============================================================================
EXCERPT_MAX_PLACES = 20            # places a symbol is used that are tried when looking for the area it means
EXCERPT_MAX_IMPORT_LINES = 10      # import lines kept at the top of an excerpt (dropped first when the room is short)
# The only placeholder lines the system ever writes, in the file's own comment style. "implement" replaces the body of
# an existing target function; "add" closes an excerpt where new code is expected. Supported languages only: any other
# language gets no excerpt (an empty starter), exactly as before.
STARTER_PLACEHOLDERS = {
    "implement": {"python": "# TODO: implement", "javascript": "// TODO: implement", "typescript": "// TODO: implement",
                  "java": "// TODO: implement"},
    "add": {"python": "# TODO: add your code here", "javascript": "// TODO: add your code here",
            "typescript": "// TODO: add your code here", "java": "// TODO: add your code here"},
}
STARTER_SOURCES = ("code", "model", "none")

# ============================================================================
# WHICH FILES CAN SHOW A SKILL
# Skill name (lower case, as in the taxonomy) -> the extensions of files that can realistically show it. A file with any
# other extension is never a candidate for that skill and never reaches the model. A skill that is NOT listed is
# unrestricted, exactly as before: the list is deliberately conservative (never guess), so extend it as evidence
# comes in. SQL is not listed on purpose: it keeps its own rules (see SQL QUESTIONS above).
# ============================================================================
_PY = (".py",)
_PY_DATA = (".py", ".ipynb")           # notebooks are where pandas / numpy work usually lives
_JS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")
_REACT = (".jsx", ".tsx", ".js", ".ts")
_STYLE = (".css", ".scss", ".sass", ".less", ".html", ".jsx", ".tsx", ".vue", ".svelte")
SKILL_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "python": _PY_DATA, "pandas": _PY_DATA, "numpy": _PY_DATA,
    "flask": _PY, "django": _PY, "fastapi": _PY, "pytest": _PY, "selenium": _PY, "beautifulsoup": _PY, "scrapy": _PY,
    "javascript": _JS, "node.js": _JS, "express": _JS, "express.js": _JS, "jest": _JS, "redux": _JS,
    "typescript": (".ts", ".tsx"),
    "react": _REACT, "next.js": _REACT,
    "html": (".html", ".htm", ".jsx", ".tsx", ".vue", ".svelte", ".ejs"),
    "css": _STYLE, "sass": _STYLE, "tailwind css": _STYLE,
    "java": (".java",), "c#": (".cs",), "go": (".go",), "rust": (".rs",), "php": (".php",), "ruby": (".rb",),
    "kotlin": (".kt",),
}

FAILING_EXAMPLE_MAX_CHARS = 120    # each of input / expected / actual of a fix question's failing example
EXAMPLE_SENTENCE = "Example: {input} should give {expected}, but the starter code gives {actual}."

MESSAGE_TOO_FEW ="Only {n} question(s) could be written from the candidate's code. A session needs at least {min}."
MESSAGE_NONE = "No code was found that shows the job's skills, so no questions could be written."
