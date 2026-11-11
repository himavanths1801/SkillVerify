"""The model chain, its configuration, and the list_models helper. Fake clock, no network."""
import logging
import runpy
import threading

import pytest
import requests

from api import models, settings
from api.models import ModelChain
from api.tools import list_models

NAMES = ("m1", "m2", "m3")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def chain(clock):
    return ModelChain(NAMES, clock=clock)


# --------------------------------------------------------------------------
# ModelChain
# --------------------------------------------------------------------------
def test_pick_is_the_first_model_and_models_keep_their_order(chain):
    assert chain.pick() == "m1" and chain.models == NAMES


def test_cooling_models_are_skipped_and_none_is_returned_when_all_cool(chain):
    chain.mark_rate_limited("m1")
    assert chain.pick() == "m2"
    chain.mark_rate_limited("m2")
    assert chain.pick() == "m3"
    chain.mark_rate_limited("m3")
    assert chain.pick() is None


def test_cooldown_expiry_returns_to_the_first_model(chain, clock):
    chain.mark_rate_limited("m1")
    chain.mark_rate_limited("m2")
    clock.now += settings.RATE_LIMIT_COOLDOWN_SECONDS - 1
    assert chain.pick() == "m3"
    clock.now += 1
    assert chain.pick() == "m1"  # the earliest ready model always wins


def test_a_second_limit_in_a_row_uses_the_long_cooldown(chain, clock):
    chain.mark_rate_limited("m1")
    clock.now += settings.RATE_LIMIT_COOLDOWN_SECONDS
    assert chain.pick() == "m1"
    chain.mark_rate_limited("m1")
    clock.now += settings.RATE_LIMIT_COOLDOWN_SECONDS
    assert chain.pick() == "m2"
    clock.now += settings.REPEATED_LIMIT_COOLDOWN_SECONDS - settings.RATE_LIMIT_COOLDOWN_SECONDS
    assert chain.pick() == "m1"


def test_a_success_clears_the_counter(chain, clock):
    chain.mark_rate_limited("m1")
    clock.now += settings.RATE_LIMIT_COOLDOWN_SECONDS
    chain.mark_success("m1")
    chain.mark_rate_limited("m1")
    clock.now += settings.RATE_LIMIT_COOLDOWN_SECONDS
    assert chain.pick() == "m1"  # short cooldown again


def test_unavailable_models_rest_for_an_hour(chain, clock):
    chain.mark_unavailable("m1")
    assert chain.pick() == "m2"
    clock.now += settings.MODEL_UNAVAILABLE_SECONDS - 1
    assert chain.pick() == "m2"
    clock.now += 1
    assert chain.pick() == "m1"


def test_snapshot_values(chain, clock):
    chain.mark_rate_limited("m1")
    chain.mark_unavailable("m2")
    clock.now += 0.5
    assert chain.snapshot() == [
        {"model": "m1", "state": "cooling_down", "seconds_left": 120},  # rounded up
        {"model": "m2", "state": "unavailable", "seconds_left": 3600},
        {"model": "m3", "state": "ready", "seconds_left": 0}]
    clock.now += settings.RATE_LIMIT_COOLDOWN_SECONDS
    assert chain.snapshot()[0] == {"model": "m1", "state": "ready", "seconds_left": 0}


def test_thread_safety_smoke(chain):
    def hammer():
        for _ in range(300):
            chain.mark_rate_limited("m1")
            chain.mark_success("m1")
            chain.pick()
            chain.snapshot()
    threads = [threading.Thread(target=hammer) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert chain.pick() in NAMES + (None,) and len(chain.snapshot()) == 3


def test_the_chain_is_read_from_the_environment_lazily(monkeypatch):
    calls = []
    monkeypatch.setattr(models, "load_dotenv", lambda: calls.append("dotenv"))
    monkeypatch.setenv("GEMINI_MODEL_CHAIN", "x1,x2")
    lazy = ModelChain()
    assert calls == []  # building it reads nothing
    assert lazy.models == ("x1", "x2") and lazy.pick() == "x1" and calls == ["dotenv"]
    assert lazy.snapshot()[1]["model"] == "x2" and calls == ["dotenv"]  # read once


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------
D = settings.DEFAULT_MODEL_CHAIN


def test_default_chain_has_the_two_built_in_models():
    assert D == ("gemini-3.6-flash", "gemini-3.5-flash-lite")
    assert settings.load_model_chain({}) == D


def test_chain_variable_wins_and_keeps_order():
    env = {"GEMINI_MODEL_CHAIN": " a-1 , b.2,c_3 ", "GEMINI_MODEL": "ignored"}
    assert settings.load_model_chain(env) == ("a-1", "b.2", "c_3")


def test_single_model_goes_first_before_the_defaults():
    assert settings.load_model_chain({"GEMINI_MODEL": "mine"}) == ("mine",) + D
    assert settings.load_model_chain({"GEMINI_MODEL": D[1]}) == (D[1], D[0])  # duplicate removed
    assert settings.load_model_chain({"GEMINI_MODEL_CHAIN": "  ", "GEMINI_MODEL": "mine"}) == ("mine",) + D


def test_duplicates_removed_empty_entries_skipped_cap_of_eight():
    assert settings.load_model_chain({"GEMINI_MODEL_CHAIN": "a,b,a,,b,c,"}) == ("a", "b", "c")
    many = ",".join(f"m{i}" for i in range(12))
    assert settings.load_model_chain({"GEMINI_MODEL_CHAIN": many}) == tuple(f"m{i}" for i in range(8))


def test_invalid_names_are_dropped_with_one_plain_log_line(caplog):
    caplog.set_level(logging.WARNING, logger="skillverify.api")
    env = {"GEMINI_MODEL_CHAIN": "good,bad name,also/bad,fine," + "x" * 101}
    assert settings.load_model_chain(env) == ("good", "fine")
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1 and "3 invalid" in lines[0]
    assert "bad name" not in lines[0] and "also/bad" not in lines[0]  # the text itself is never logged


def test_a_chain_with_no_valid_names_falls_back_to_the_default():
    assert settings.load_model_chain({"GEMINI_MODEL_CHAIN": "a b,c/d"}) == D


# --------------------------------------------------------------------------
# api/tools/list_models.py (requests mocked)
# --------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status=200, data=None, bad_json=False):
        self.status_code, self._data, self._bad = status, data, bad_json

    def json(self):
        if self._bad:
            raise ValueError("no json")
        return self._data


def run_tool(monkeypatch, pages, key="SECRET-KEY-VALUE"):
    monkeypatch.setattr(list_models, "load_dotenv", lambda: None)
    if key is None:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("GEMINI_API_KEY", key)
    calls, lines = [], []

    def get(url, params=None, headers=None, timeout=None):
        calls.append((url, dict(params), dict(headers)))
        page = pages[len(calls) - 1]
        if isinstance(page, Exception):
            raise page
        return page
    monkeypatch.setattr(list_models.requests, "get", get)
    return list_models.main(lines.append), lines, calls


def test_tool_lists_generate_content_models_and_follows_pages(monkeypatch):
    pages = [
        FakeResponse(data={"models": [
            {"name": "models/gemini-a", "supportedGenerationMethods": ["generateContent", "countTokens"]},
            {"name": "models/embed-x", "supportedGenerationMethods": ["embedContent"]}], "nextPageToken": "T2"}),
        FakeResponse(data={"models": [{"name": "models/gemini-b", "supportedGenerationMethods": ["generateContent"]},
                                      {"name": "no-prefix", "supportedGenerationMethods": ["generateContent"]}]}),
    ]
    code, lines, calls = run_tool(monkeypatch, pages)
    assert code == 0 and lines == ["gemini-a", "gemini-b", "no-prefix"]
    assert calls[0][0] == list_models.URL and "pageToken" not in calls[0][1] and calls[1][1]["pageToken"] == "T2"
    assert calls[0][2] == {"x-goog-api-key": "SECRET-KEY-VALUE"}  # in a header, never in the URL or the output
    assert "SECRET" not in " ".join(lines) and all("SECRET" not in c[0] for c in calls)


def test_tool_with_no_key_says_so(monkeypatch):
    code, lines, calls = run_tool(monkeypatch, [], key=None)
    assert code == 2 and calls == [] and "GEMINI_API_KEY is not set" in lines[0]


def test_tool_reports_failures_plainly_without_the_key_or_raw_answer(monkeypatch):
    for page, text in [(requests.ConnectionError("https://x/?key=SECRET-KEY-VALUE"), "Could not reach Gemini"),
                       (FakeResponse(status=403, data={"error": "SECRET-KEY-VALUE"}), "HTTP 403"),
                       (FakeResponse(bad_json=True), "could not read"),
                       (FakeResponse(data={"models": [{"supportedGenerationMethods": ["generateContent"]}]}),
                        "could not read"),
                       (FakeResponse(data=["not", "a", "dict"]), "could not read")]:
        code, lines, _ = run_tool(monkeypatch, [page])
        assert code == 1 and text in lines[0] and "SECRET" not in " ".join(lines)


def test_tool_stops_after_the_page_limit(monkeypatch):
    page = FakeResponse(data={"models": [], "nextPageToken": "again"})
    code, lines, calls = run_tool(monkeypatch, [page] * list_models.MAX_PAGES)
    assert code == 0 and lines == [] and len(calls) == list_models.MAX_PAGES


def test_tool_runs_as_a_script(monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", "SECRET-KEY-VALUE")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(data={"models": [
        {"name": "models/gemini-z", "supportedGenerationMethods": ["generateContent"]}]}))
    with pytest.raises(SystemExit) as stop:
        runpy.run_module("api.tools.list_models", run_name="__main__")
    assert stop.value.code == 0
    assert capsys.readouterr().out == "gemini-z\n"
