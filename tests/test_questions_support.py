"""Shared fixtures for the question-generator tests (no tests here). Fictional candidate, offline fakes only."""
import base64
import json

from skillverify.jd.provider import LLMCallError, LLMProvider

# --------------------------------------------------------------------------
# fictional files
# --------------------------------------------------------------------------
PY_ORDERS = '''"""Orders report for the fictional shop."""
import pandas as pd
import numpy as np

TAX_RATE = 0.2


def load_orders(path):
    frame = pd.read_csv(path)
    frame["total"] = frame["price"] * frame["quantity"]
    return frame


def clean_orders(frame):
    frame = frame.dropna(subset=["customer_id"])
    if frame.empty:
        raise ValueError("no rows left")
    for column in ("price", "quantity"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["net"] = np.round(frame["total"] / (1 + TAX_RATE), 2)
    return frame


def monthly_revenue(frame):
    grouped = frame.groupby(frame["ordered_at"].str[:7])["net"].sum()
    return grouped.reset_index(name="revenue")


class OrderReport:
    def __init__(self, frame):
        self.frame = frame

    def top_customers(self, limit=5):
        ranked = self.frame.groupby("customer_id")["net"].sum()
        return ranked.sort_values(ascending=False).head(limit)
'''

PY_SMALL = '''def tiny():
    return 1
'''

PY_LOGIC_HEAVY = '''import pandas as pd


def a(x):
    if x:
        for i in range(3):
            while i < 2:
                try:
                    i += 1
                except ValueError:
                    pass
    return pd.DataFrame()


def b(x):
    if x:
        for i in range(3):
            pass
    return [i for i in range(10) if i % 2]


class C:
    def m(self, y):
        with open(y) as handle:
            return handle.read()
''' + "\n".join(f"v{i} = {i}" for i in range(20)) + "\n"

SQL_REPORT = '''-- Monthly revenue per customer segment (fictional shop)
CREATE VIEW monthly_revenue AS
SELECT
    c.segment,
    strftime('%Y-%m', o.ordered_at) AS month,
    SUM(o.price * o.quantity) AS revenue,
    COUNT(DISTINCT o.customer_id) AS customers
FROM orders AS o
JOIN customers AS c ON c.id = o.customer_id
WHERE o.status = 'paid'
GROUP BY c.segment, month
HAVING SUM(o.price * o.quantity) > 100
ORDER BY month, revenue DESC;

SELECT segment, AVG(revenue) AS avg_revenue
FROM monthly_revenue
WHERE month >= '2026-01'
GROUP BY segment
ORDER BY avg_revenue DESC
LIMIT 10;
'''

SHELL_RELEASE = '''#!/bin/bash
# Release helper for the fictional shop
set -e

release() {
  git checkout main
  git pull origin main
  git merge --no-ff feature/orders
  git tag -a "v$1" -m "release $1"
  git push origin main --tags
}

cleanup() {
  git branch --merged main | grep -v main | while read branch; do
    git branch -d "$branch"
  done
  git remote prune origin
  git fetch --all
}

if [ -z "$1" ]; then
  echo "usage: release.sh <version>"
  exit 1
fi

release "$1"
cleanup
git status
git log --oneline -n 5
'''

NOTEBOOK = json.dumps({"cells": [
    {"cell_type": "markdown", "source": ["# Notes"]},
    {"cell_type": "code", "source": ["import numpy as np\n", "import matplotlib.pyplot as plt\n"],
     "outputs": [{"text": ["OUTPUT-MUST-NOT-APPEAR"]}]},
    {"cell_type": "code", "source": ["def simulate(n):\n", "    values = np.random.default_rng(1).normal(size=n)\n",
                                      "    if n > 100:\n", "        values = values[:100]\n",
                                      "    return values\n"], "outputs": []},
    {"cell_type": "code", "source": ["data = simulate(500)\n", "for i in range(3):\n",
                                      "    print(np.mean(data[i:]))\n", "plt.hist(data, bins=20)\n",
                                      "plt.title('Normal samples')\n", "plt.xlabel('value')\n",
                                      "plt.ylabel('count')\n", "plt.grid(True)\n", "plt.show()\n",
                                      "summary = {'mean': float(np.mean(data)), 'std': float(np.std(data))}\n",
                                      "print(summary)\n"], "outputs": []},
], "metadata": {}, "nbformat": 4, "nbformat_minor": 5})

JS_APP = '''import React, { useState } from "react";

export function OrderList({ orders }) {
  const [filter, setFilter] = useState("");
  const visible = orders.filter((order) => order.name.includes(filter));
  const total = visible.reduce((sum, order) => sum + order.total, 0);

  function onChange(event) {
    setFilter(event.target.value);
  }

  if (visible.length === 0) {
    return <p>No orders match.</p>;
  }

  return (
    <div>
      <input value={filter} onChange={onChange} />
      <ul>
        {visible.map((order) => (
          <li key={order.id}>{order.name}</li>
        ))}
      </ul>
      <p>{total}</p>
    </div>
  );
}
'''

# --------------------------------------------------------------------------
# a fictional analysis
# --------------------------------------------------------------------------
USER = "priya-sample"


def skill_row(skill, tier="must_have", claimed=True, status="found", weight=None):
    return {"skill": skill, "category": "x", "tier": tier, "tier_basis": "x",
            "weight": weight if weight is not None else (3 if tier == "must_have" else 1),
            "claimed": claimed, "github_status": status, "confidence": "strong", "basis": None, "credit": 1.0,
            "points": 3.0, "reason": "confirmed"}


def repo_record(name, pushed="2026-09-01T10:00:00Z", languages=None, fork=False, counted=True):
    return {"name": name, "url": "u", "is_fork": fork, "counted": counted, "primary_language": "Python",
            "languages": languages or {"Python": 9000}, "last_commit_date": pushed}


def github_result(repos, skills=None, per_repo=None):
    """A validator result with the scan's ``tech_stack`` merged in, as the pipeline keeps it on the job."""
    return {"username": USER, "status": "ok", "repos": repos,
            "summary": {"counted_repos": len(repos), "languages_combined": {"Python": 9000}},
            "tech_stack": {"status": "ok", "complete": True, "skills": skills or [],
                           "per_repo": per_repo if per_repo is not None else [{"name": r["name"], "default_branch": "main"}
                                                                for r in repos]}}


def tech_item(skill, repos):
    return {"skill": skill, "label": skill, "in_taxonomy": True, "category": "x", "confidence": "strong",
            "evidence_types": ["imported_in_code"], "repos": repos, "examples": []}


def tree_entry(path, size=1000):
    return {"path": path, "type": "blob", "size": size}


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------
class FakeFiles:
    """The GitHub files client: ``trees`` is repo -> entries, ``contents`` is (repo, path) -> text."""

    def __init__(self, trees=None, contents=None, error=None, branch="main"):
        self.trees, self.contents, self.error, self.branch = trees or {}, contents or {}, error, branch
        self.calls = {"default_branch": 0, "tree": 0, "file": 0}
        self.fetched = []
        self.order = []  # ("tree" | "file", repo) in call order

    def default_branch(self, owner, repo):
        self.calls["default_branch"] += 1
        return self.branch

    def tree(self, owner, repo, branch):
        self.calls["tree"] += 1
        self.order.append(("tree", repo))
        if self.error:
            raise self.error
        return self.trees.get(repo, [])

    def file_text(self, owner, repo, path, branch):
        self.calls["file"] += 1
        self.fetched.append((repo, path))
        self.order.append(("file", repo))
        if self.error:
            raise self.error
        return self.contents.get((repo, path))

    def total(self):
        return sum(self.calls.values())


def shop_files():
    """Three repos' worth of files, small enough to read in a test."""
    trees = {"orders-analysis": [tree_entry("analysis.py", 1500), tree_entry("sql/report.sql", 800),
                                 tree_entry("README.md", 900), tree_entry("tests/test_analysis.py", 900),
                                 tree_entry("node_modules/x/index.js", 5000)],
             "release-tools": [tree_entry("scripts/release.sh", 700)],
             "shop-ui": [tree_entry("src/OrderList.jsx", 900), tree_entry("notebooks/explore.ipynb", 3000)]}
    contents = {("orders-analysis", "analysis.py"): PY_ORDERS, ("orders-analysis", "sql/report.sql"): SQL_REPORT,
                ("release-tools", "scripts/release.sh"): SHELL_RELEASE, ("shop-ui", "src/OrderList.jsx"): JS_APP,
                ("shop-ui", "notebooks/explore.ipynb"): NOTEBOOK}
    return FakeFiles(trees, contents)


def shop_analysis():
    """(score skills, github result) for a candidate whose code shows Python, SQL, Pandas, Git and React."""
    skills = [skill_row("Python"), skill_row("SQL"), skill_row("Pandas", claimed=False),
              skill_row("Git", tier="nice_to_have"), skill_row("React", tier="nice_to_have"),
              skill_row("Tableau", tier="nice_to_have", status="not_detectable_from_github"),
              skill_row("Docker", status="not_found")]
    repos = [repo_record("orders-analysis", "2026-09-10T10:00:00Z"), repo_record("release-tools", "2026-08-01T10:00:00Z"),
             repo_record("shop-ui", "2026-07-01T10:00:00Z", {"JavaScript": 5000}),
             repo_record("old-fork", fork=True), repo_record("broken", counted=False)]
    scan = [tech_item("Python", ["orders-analysis"]), tech_item("SQL", ["orders-analysis"]),
            tech_item("Pandas", ["orders-analysis"]), tech_item("Git", ["release-tools"]),
            tech_item("React", ["shop-ui"])]
    return skills, github_result(repos, scan)


def good_answer(path, symbols=None, kind="write_code", **overrides):
    """A question that passes every check for the fictional files, for the given chosen path."""
    base = {"python": ("analysis.py", ["clean_orders"]), }
    answer = {"kind": kind, "file": path, "symbols": symbols if symbols is not None else [],
              "question": f"Open {path.rsplit('/', 1)[-1]} and extend the code so it handles one more case. "
                          "Explain your approach, then write the code and say how you would test it.",
              "starter_code": "" if kind == "write_code" else "x = 1\n", "reference_notes": "A good answer is clear."}
    answer.update(overrides)
    return answer


class FakeWriter(LLMProvider):
    """A Gemini fake: answers by chosen path (a dict, an exception, or a list of them used in order)."""
    name = "fake"
    model = "fake-1"

    def __init__(self, answers=None, default=None):
        self.answers, self.default = answers or {}, default
        self.calls, self.prompts = 0, []

    def generate_json(self, system_prompt, user_prompt):
        self.calls += 1
        self.prompts.append((system_prompt, user_prompt))
        path = next(line.split(": ", 1)[1] for line in user_prompt.splitlines() if line.startswith("Chosen file path:"))
        entry = self.answers.get(path, self.default)
        if isinstance(entry, list):
            entry = entry.pop(0) if len(entry) > 1 else entry[0]
        if callable(entry):
            entry = entry(path)
        if isinstance(entry, Exception):
            raise entry
        return entry


# the same file as the GitHub API would return it (for the files-client tests)
def contents_payload(text):
    return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}


RATE_LIMIT = LLMCallError("Gemini rate limit reached (HTTP 429). Wait a while and try again.")
