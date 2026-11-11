"""Shared helpers for the API tests (no tests here). Offline: fake Gemini provider, fake GitHub objects, and a tiny
in-process ASGI client (so no extra dependency such as httpx is needed). Fictional data only."""
import asyncio
import json
import time
import uuid
from urllib.parse import urlsplit

import pytest

from api import jobs, pipeline, retry, settings
from api.models import ModelChain
from api.app import create_app
from skillverify.cv import prompts as cv_prompts
from skillverify.jd import prompts as jd_prompts
from skillverify.jd.provider import LLMProvider

def rescored(result, kept):
    """What score_candidate gives for the kept inputs. Only the clock stamp is taken from ``result`` (a second may
    have passed since the run), so the comparison is exact in every other field."""
    from skillverify.scoring import score_candidate
    return {**score_candidate(kept["jd"], kept["cv"], kept["github"]), "generated_at": result["generated_at"]}


CHAIN = ("model-a", "model-b", "model-c")  # a fixed chain, so no test depends on the real environment
JD_TEXT = "Backend analytics role. Required: Python and SQL. Nice to have: Tableau.\n"
CV_TEXT = "Priya Sample\nGitHub: github.com/octocat\nSkills: Python, SQL, Pandas\n"
JD_ANSWER = {"skills": [
    {"as_written": "Python", "name": "Python", "signal": "required", "evidence": "Python"},
    {"as_written": "SQL", "name": "SQL", "signal": "required", "evidence": "SQL"},
    {"as_written": "Tableau", "name": "Tableau", "signal": "preferred", "evidence": "Tableau"},
]}
CV_ANSWER = {"name": "Priya Sample", "education": [], "projects": [], "skills": [
    {"as_written": n, "name": n, "context": "skills_section", "project": None, "evidence": n}
    for n in ("Python", "SQL", "Pandas")]}


def scan_item(skill, confidence="medium"):
    return {"skill": skill, "label": skill, "in_taxonomy": True, "category": "tools", "confidence": confidence,
            "evidence_types": ["file_signal"], "repos": ["a"], "examples": []}


def github_ok(username="octocat"):
    return {"username": username, "status": "ok",
            "summary": {"counted_repos": 3, "total_commits_in_window": 40, "active_repos_in_window": 2,
                        "languages_combined": {"Python": 9000, "HTML": 100}}}


TECH_OK = {"status": "ok", "complete": True, "repos_scanned": 3, "skills": [scan_item("Python"), scan_item("Pandas")]}


class FakeProvider(LLMProvider):
    """Answers the JD and CV extraction calls. ``failures`` are raised first, one per call, then the answers."""
    name = "fake"
    model = "fake-1"

    def __init__(self, failures=(), always=None, jd=JD_ANSWER, cv=CV_ANSWER, cv_error=None, gate=None):
        self.failures = list(failures)
        self.always = always
        self.cv_error = cv_error  # raised only for the CV call
        self.gate = gate          # a threading.Event: every call waits for it
        self.jd, self.cv = jd, cv
        self.calls = 0

    def generate_json(self, system_prompt, user_prompt):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(10)
        if self.always is not None:
            raise self.always
        if self.failures:
            raise self.failures.pop(0)
        if system_prompt == jd_prompts.extraction_system_prompt():
            return self.jd
        assert system_prompt == cv_prompts.extraction_system_prompt(), "unexpected extra LLM call"
        if self.cv_error is not None:
            raise self.cv_error
        return self.cv


class FakeValidator:
    def __init__(self, result=None, raises=None):
        self.result, self.raises, self.calls = result if result is not None else github_ok(), raises, []

    def validate(self, username):
        self.calls.append(username)
        if self.raises:
            raise self.raises
        return dict(self.result, username=username) if self.result.get("status") == "ok" else self.result


class FakeScanner:
    def __init__(self, tech=None, raises=None):
        self.tech, self.raises, self.calls = tech if tech is not None else TECH_OK, raises, 0

    def scan(self, validation):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.tech


class Env:
    """An app wired to fakes, plus a client."""

    def __init__(self, provider=None, validator=None, scanner=None, provider_factory=None, validator_factory=None,
                 chain=None, files_factory=None):
        self.provider = provider or FakeProvider()
        self.validator = validator or FakeValidator()
        self.scanner = scanner or FakeScanner()
        deps = pipeline.Dependencies(
            provider_factory=provider_factory or (lambda model=None: self.provider),
            validator_factory=validator_factory or (lambda: self.validator),
            scanner_factory=lambda validator, taxonomy: self.scanner,
            chain=chain or ModelChain(CHAIN),
            files_client_factory=files_factory or (lambda: pytest.fail("the question code reached for GitHub")))
        self.app = create_app(deps)
        self.manager = self.app.state.jobs
        self.questions = self.app.state.questions
        self.client = Client(self.app)

    def submit(self, **overrides):
        """POST a valid analysis (JD as text, CV as .txt) with optional overrides; returns the response."""
        fields = {"jd_text": JD_TEXT}
        files = {"cv_file": ("cv_priya.txt", CV_TEXT.encode())}
        for key, value in overrides.items():
            if key in ("cv_file", "jd_file"):
                if value is None:
                    files.pop(key, None)
                else:
                    files[key] = value
            elif value is None:
                fields.pop(key, None)
            else:
                fields[key] = value
        return self.client.request("POST", "/api/analyses", fields, files)

    def run(self, timeout=10, **overrides):
        """Submit and wait until the job is done or failed; returns the final job dict."""
        response = self.submit(**overrides)
        assert response.status == 202, response.text
        return self.wait(response.json()["id"], timeout)

    def wait(self, job_id, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.client.request("GET", f"/api/analyses/{job_id}").json()
            if job["state"] in ("done", "failed"):
                return job
            time.sleep(0.01)
        raise AssertionError("the job did not finish in time")


@pytest.fixture
def make_env(monkeypatch):
    """Factory for environments; every job store is shut down at the end. Nothing really sleeps."""
    monkeypatch.setattr(retry, "_sleep", lambda seconds: None)
    monkeypatch.setattr(settings, "_limits_cache", settings.load_limits({}))  # the defaults; .env is never read
    made = []

    def build(**kwargs):
        env = Env(**kwargs)
        made.append(env)
        return env

    yield build
    for env in made:
        env.manager.shutdown()
        env.questions.shutdown()


@pytest.fixture
def env(make_env):
    return make_env()


# -- a tiny ASGI client -----------------------------------------------------------------------------------------
class Response:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body)


def encode_multipart(fields, files):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in (fields or {}).items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, (filename, data) in (files or {}).items():
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                      "Content-Type: application/octet-stream\r\n\r\n").encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class Client:
    def __init__(self, app):
        self.app = app
        self.last_exception = None

    def request(self, method, path, fields=None, files=None, headers=None, raw_body=None, content_type=None):
        body, ctype = (raw_body, content_type) if raw_body is not None else (
            encode_multipart(fields, files) if (fields or files) else (b"", None))
        parts = urlsplit(path)
        header_list = [(b"host", b"testserver")]
        if ctype:
            header_list.append((b"content-type", ctype.encode()))
        header_list += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        header_list.append((b"content-length", str(len(body)).encode()))
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                 "scheme": "http", "path": parts.path, "raw_path": parts.path.encode(),
                 "query_string": parts.query.encode(), "headers": header_list, "server": ("testserver", 80),
                 "client": ("127.0.0.1", 50000), "root_path": ""}
        sent = []
        self.last_exception = None

        async def go():
            delivered = False

            async def receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return {"type": "http.disconnect"}

            async def send(message):
                sent.append(message)

            try:
                await self.app(scope, receive, send)
            except Exception as exc:  # noqa: BLE001 - the server layer re-raises after answering
                self.last_exception = exc

        asyncio.run(go())
        start = next(m for m in sent if m["type"] == "http.response.start")
        data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        head = {k.decode().lower(): v.decode() for k, v in start["headers"]}
        return Response(start["status"], head, data)


__all__ = ["CV_ANSWER", "CV_TEXT", "Client", "Env", "FakeProvider", "FakeScanner", "FakeValidator", "JD_ANSWER",
           "JD_TEXT", "TECH_OK", "env", "github_ok", "jobs", "make_env", "scan_item", "settings"]
