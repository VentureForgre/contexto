"""Project memory in the engine (D69, contract rule 19): a new session's requests hold the records it
carries from the project's earlier sessions from the first request on, before any rotation.

The stores are real ``SessionStore``s under a temporary ``CONTEXTO_HOME``, built as ``test_project``
builds them: an earlier session whose pins the main conversation made, then the session under test.
A session here gets a fixed page key, so page ids, and with them the index text, are the same on
every run and a script's bytes can be compared with recorded ones. The sizes and the bound are in
``test_project_start_bound``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

import test_codex as codex
from contexto import store as store_module
from contexto.canonical import canonical_bytes
from contexto.codex import RESPONSES as RESPONSES_DIALECT
from contexto.proxy import serialize_request
from contexto.rewrite import (
    ANTHROPIC,
    COUNT_TOKENS,
    MESSAGES,
    Config,
    RewriteEngine,
    compose_preamble,
    count_tokens_view,
    keyed,
)
from contexto.store import SessionStore
from contexto.store.memory import MemoryStore
from contexto.store.project import forget
from test_clear_main import NEW, OLD, headers, main_request, side_request, subagent_request
from test_project import at, main_close, main_pin, repo, v0_5_24_guidance
from test_rewrite import FILL, FaultyStore, assistant, conversation, request, system, tool_result, tool_use, user

RESPONSES = "/v1/responses"
ENDPOINTS = (MESSAGES, RESPONSES)
DIALECTS = {MESSAGES: ANTHROPIC, RESPONSES: RESPONSES_DIALECT}
PAGE_KEY = "6b" * 32
MARK = "Carried from earlier sessions of this project"  # how the carried section starts
ROOMY = Config(bound=1_000_000)
SMALL = {MESSAGES: Config(bound=4_000, headroom_floor=500, pin_budget=200, index_budget=200, min_turn_room=300),
         RESPONSES: Config(bound=6_000, headroom_floor=500, pin_budget=200, index_budget=200, min_turn_room=300)}
# A bound a ten-turn script rotates under, with a pin budget that holds a few carried records.
ROTATING = Config(bound=8_000, headroom_floor=500, pin_budget=1_000, index_budget=300, min_turn_room=300)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.delenv("CONTEXTO_RECALL_VARIANT", raising=False)
    return tmp_path / "contexto-home"


def session(home: Path, sid: str, cwd: Path | None, n: float, *, memory: bool | None = None) -> SessionStore:
    """A session created at ``at(n)`` (see ``test_project``) with a fixed page key."""
    path = home / "sessions" / sid
    path.mkdir(parents=True, mode=0o700)
    key = path / "page.key"
    key.write_text(PAGE_KEY)
    key.chmod(0o600)
    store = SessionStore(home, sid)
    if cwd is not None:
        store.set_workdir(cwd)
    if memory is not None:
        store.set_project_memory(memory)
    os.utime(key, (at(n), at(n)))
    return store


def later_session(home: Path, tmp_path: Path, pins: int = 3, text: str = "", *, memory: bool | None = None,
                  author_main: bool = True) -> SessionStore:
    """Session ``s2`` of a project whose earlier session ``s1`` pinned ``pins`` obligations and a decision."""
    root = repo(tmp_path)
    first = session(home, "s1", root, 1)
    for i in range(pins):
        rule = f"standing rule {i}: whenever you give a date, use ISO 8601 {text}".strip()
        if author_main:
            main_pin(first, "obligation", rule)
        else:
            first.pin("obligation", rule)  # author "model": a subagent's pin, or an earlier release's
    if author_main:
        main_pin(first, "decision", "the codename is Obsidian")
    first.release()
    return session(home, "s2", root, 2, memory=memory)


def carried(store, budget: int = 6_000) -> str:
    text = store.carried_text(budget)
    assert text.startswith(MARK) and "standing rule 0" in text and "the codename is Obsidian" in text
    return text


class Failing:
    """A store whose named methods raise; every other call goes to the real store. ``calls`` counts."""

    def __init__(self, store, **fail: Exception) -> None:
        self.store, self.fail, self.calls = store, dict(fail), []

    def __getattr__(self, name: str):
        value = getattr(self.store, name)
        if not callable(value):
            return value

        def call(*args, **kwargs):
            self.calls.append(name)
            if name in self.fail:
                raise self.fail[name]
            return value(*args, **kwargs)

        return call


# The scripts: every request a CLI sends over a session, each an append to the one before -------------

def messages_script(turns: int, tag: str = "prompt") -> list[list[dict]]:
    """Claude Code: context blocks and a system message at the start, then turns with a tool loop."""
    history: list[dict] = []
    out = []
    for t in range(turns):
        history = [*history, user(f"{tag} {t} {FILL}", context=t == 0)]
        out.append(history)
        if t == 0:
            history = [*history, system()]
        history = [*history, tool_use(t, think=f"t{t}"), tool_result(t, f"output {t} {FILL}")]
        out.append(history)
        history = [*history, assistant(f"answer {t} {FILL}", think=f"a{t}")]
    return out


def responses_script(turns: int, tag: str = "prompt") -> list[list[dict]]:
    """Codex: the lead items, then turns with a call and its output."""
    history = codex.lead()
    out = []
    for t in range(turns):
        history = [*history, codex.prompt(f"{tag} {t} {FILL}")]
        out.append(history)
        history = [*history, codex.reasoning(t), codex.call(t), codex.output(t, f"out {t} {FILL}")]
        out.append(history)
        history = [*history, codex.answer(f"answer {t}")]
    return out


def script(endpoint: str, turns: int, tag: str = "prompt") -> list[list[dict]]:
    return messages_script(turns, tag) if endpoint == MESSAGES else responses_script(turns, tag)


def body(endpoint: str, items: list[dict], **extra) -> dict:
    return request(items, **extra) if endpoint == MESSAGES else codex.request(items, **extra)


def field(endpoint: str) -> str:
    return "messages" if endpoint == MESSAGES else "input"


def usage(upstream: dict, bytes_per_token: float = 4.0) -> dict:
    """What a stub upstream reports for the request it received: its bytes at a fixed ratio."""
    return {"input_tokens": int(len(serialize_request(upstream)) / bytes_per_token), "output_tokens": 5}


def drive(engine: RewriteEngine, endpoint: str, requests: list[list[dict]], bytes_per_token: float = 4.0):
    """Send each request and feed its usage back; (upstream, events, usage events) per request."""
    out = []
    for items in requests:
        sent = body(endpoint, items)
        upstream, _, events = engine(sent, {}, endpoint)
        out.append((upstream, events, engine.observe_usage(endpoint, usage(upstream, bytes_per_token), sent)))
    return out


def names(events: list[dict]) -> list[str]:
    return [e["event"] for e in events]


def wire(upstream: dict, endpoint: str) -> list[bytes]:
    """The request's messages as the proxy's serializer sends them, one by one."""
    return [serialize_request(item) for item in upstream[field(endpoint)]]


def marks(upstream: dict) -> int:
    return serialize_request(upstream).count(MARK.encode())


def state_of(engine: RewriteEngine, endpoint: str, items: list[dict]):
    return engine.states[keyed(DIALECTS[endpoint], body(endpoint, items))]


def digest(run: list[tuple]) -> str:
    """One hash over everything a run sent and logged."""
    h = hashlib.sha256()
    for upstream, events, usage_events in run:
        h.update(serialize_request(upstream) + b"\n" + canonical_bytes([events, usage_events]) + b"\n")
    return h.hexdigest()


# Nothing carried: byte for byte the requests and the log of the release before D69 ------------------

# Recorded from the engine before D69 by running ``recorded`` there (first at 57263d20; the Claude Code
# hash again on main 14586393, where #263, D64, added fields to the engine's events): the hash of
# every upstream request and every event of a 12-turn script that
# rotates several times, with its usage fed back and, for Claude Code, a tool-less side request and a
# count_tokens; and the hash of the epoch record the script leaves in the store. The page key is fixed,
# so each kind of store gave the same two hashes.
BEFORE_D69 = {MESSAGES: ("a60be926708cce3e3299873c19fca7959a67ebfe7911bf2b17b06490d9fb3c2b",
                         "5b19ab66b6d9ce783f9e5eef91cac4e8bc0d0505b658a5cbc505358e6b68db1b"),
              RESPONSES: ("4c7ae60ad65681f5c86c390789ca93f6a899d0dc76f2c20c129cae0e650135f7",
                          "a885834683122e1a67bf1f6b9e222d82765995eb6978089d29f19ed60d5cac5c")}
STORES = ("memory", "no_project", "nothing_carried", "memory_off")


def store_of(kind: str, home: Path, tmp_path: Path):
    if kind == "memory":
        return MemoryStore(key=bytes.fromhex(PAGE_KEY))
    if kind == "no_project":  # `contexto proxy` run by hand: no working directory recorded
        return session(home, "s2", None, 2)
    if kind == "nothing_carried":  # a project whose earlier session has pins, none of them the main conversation's
        return later_session(home, tmp_path, author_main=False)
    return later_session(home, tmp_path, memory=False)  # records to carry, and project memory off


def recorded(endpoint: str, store) -> tuple[str, str]:
    engine = RewriteEngine(store, config=SMALL[endpoint])
    requests = script(endpoint, 12)
    run = drive(engine, endpoint, requests[:9])
    if endpoint == MESSAGES:  # Claude Code's tool-less side request, and a count of the next request
        side = request([user("write a title for this conversation")])
        del side["tools"]
        upstream, _, events = engine(side, {}, MESSAGES)
        run.append((upstream, events, engine.observe_usage(MESSAGES, usage(upstream), side)))
        counted, _, events = engine(request(requests[9]), {}, COUNT_TOKENS)
        run.append((counted, events, []))
    run += drive(engine, endpoint, requests[9:])
    assert sum("rotation" in names(events) for _, events, _ in run) >= 3, "the script must rotate"
    return digest(run), hashlib.sha256(canonical_bytes(store.epoch_record())).hexdigest()


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("kind", STORES)
def test_with_nothing_carried_every_request_event_and_record_is_what_the_release_before_wrote(
        endpoint, kind, home, tmp_path, monkeypatch):
    v0_5_24_guidance(monkeypatch)  # the preamble's pin line as that release sent it (review B, finding 8)
    store = store_of(kind, home, tmp_path)
    assert store.carried_text(6_000) == ""
    assert recorded(endpoint, store) == BEFORE_D69[endpoint]


def test_a_store_without_the_method_is_read_as_nothing_carried():
    store = FaultyStore()  # the engine's Store protocol only: no carried_text
    assert not hasattr(store, "carried_text")
    engine = RewriteEngine(store, config=ROOMY)
    sent = request([user("first prompt", context=True)])
    upstream, _, events = engine(sent, {}, MESSAGES)
    assert serialize_request(upstream) == serialize_request(sent)
    assert not {"start_text", "start_text_unavailable"} & set(names(events))


# The first request -----------------------------------------------------------------------------------

@pytest.mark.parametrize("first", [user("first prompt", context=True), {"role": "user", "content": "first prompt"}])
def test_the_first_request_of_a_new_session_holds_the_carried_records_ahead_of_the_first_prompt(first, home, tmp_path):
    store = later_session(home, tmp_path)
    text = carried(store)
    engine = RewriteEngine(store, config=ROOMY)
    sent = request([first])
    before = copy.deepcopy(sent)
    upstream, _, events = engine(sent, {}, MESSAGES)
    own = first["content"] if isinstance(first["content"], list) else [{"type": "text", "text": "first prompt"}]
    assert upstream["messages"] == [{"role": "user", "content": [{"type": "text", "text": text}, *own]}]
    assert upstream["system"] is sent["system"] and upstream["tools"] is sent["tools"]
    assert sent == before  # the CLI's request is never changed
    assert {"event": "start_text", "epoch": 0, "tokens": engine.estimator.estimate(text)} in events
    assert "standing rule" not in repr(events) and "Obsidian" not in repr(events)  # content-free


def test_the_first_codex_request_keeps_its_lead_items_and_holds_the_records_in_the_first_prompt(home, tmp_path):
    store = later_session(home, tmp_path)
    text = carried(store)
    engine = RewriteEngine(store, config=ROOMY)
    lead, prompt = codex.lead(), codex.prompt("first prompt")
    upstream, _, events = engine(codex.request([*lead, prompt]), {}, RESPONSES)
    assert upstream["input"][:len(lead)] == lead and len(upstream["input"]) == len(lead) + 1  # in place, once
    assert upstream["input"][-1] == {**prompt, "content": [{"type": "input_text", "text": text}, *prompt["content"]]}
    assert "start_text" in names(events) and marks(upstream) == 1


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_the_prefix_is_byte_identical_across_every_request_before_the_first_rotation(endpoint, home, tmp_path):
    store = later_session(home, tmp_path)
    engine = RewriteEngine(store, config=ROOMY)
    requests = script(endpoint, 6)
    run = drive(engine, endpoint, requests)
    assert state_of(engine, endpoint, requests[-1]).epoch == 0
    for (earlier, _, _), (later, events, _) in zip(run, run[1:]):
        sent = wire(earlier, endpoint)
        assert wire(later, endpoint)[:len(sent)] == sent
        assert "start_text" not in names(events)  # read once, at the session's start
    assert all(marks(upstream) == 1 for upstream, _, _ in run)  # in every request, once
    assert all(len(upstream[field(endpoint)]) == len(items) for (upstream, _, _), items in zip(run, requests))


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_after_the_first_rotation_the_preamble_holds_the_records_and_the_start_text_is_not_sent(
        endpoint, home, tmp_path):
    store = later_session(home, tmp_path)
    text = carried(store, ROTATING.pin_budget)
    engine = RewriteEngine(store, config=ROTATING)
    requests = script(endpoint, 10)
    run = drive(engine, endpoint, requests)
    rotated = next(i for i, (_, events, _) in enumerate(run) if "rotation" in names(events))
    assert rotated > 0 and all(marks(upstream) == 1 for upstream, _, _ in run)
    state = state_of(engine, endpoint, requests[-1])
    assert state.k > 0 and state.start_text == text
    # The epoch record holds the start text only while it can still be sent.
    assert "start_text" not in store.epoch_record() and store.epoch_record()["k"] > 0
    assert sorted(store.epoch_record()) == BEFORE_D69_RECORD_FIELDS
    upstream = run[-1][0]
    pre = compose_preamble(ROTATING, state.pinned_text, state.index_text)
    assert text in state.pinned_text and serialize_request(upstream).count(serialize_request(pre)[1:-1]) == 1
    # Past k = 0 the transform is the one of a state with no start text.
    last = body(endpoint, requests[-1])
    view = count_tokens_view(last, state, DIALECTS[endpoint])
    assert serialize_request(view) == serialize_request(count_tokens_view(last, replace(state, start_text=""),
                                                                           DIALECTS[endpoint]))
    assert serialize_request(view) == serialize_request(upstream)


def test_a_head_change_that_pages_nothing_starts_an_epoch_that_still_holds_the_start_text(home, tmp_path):
    store = later_session(home, tmp_path)
    engine = RewriteEngine(store, config=ROOMY)
    requests = messages_script(3)
    before = drive(engine, MESSAGES, requests[:2])[-1][0]
    tools = [{"name": name, "input_schema": {"type": "object"}} for name in ("Read", "Write")]
    run = []
    for items in requests[2:]:  # an MCP server's tools arrive: rule 6's new epoch at the next turn start
        upstream, _, events = engine(request(items, tools=tools), {}, MESSAGES)
        run.append((upstream, events))
    rotation = next(e for e in run[0][1] if e["event"] == "rotation")
    assert (rotation["reason"], rotation["epoch"], rotation["k"]) == ("head_change", 1, 0)
    assert all(marks(upstream) == 1 for upstream, _ in run)
    assert wire(run[0][0], MESSAGES)[0] == wire(before, MESSAGES)[0]  # the first message keeps its form
    for (earlier, _), (later, _) in zip(run, run[1:]):
        assert wire(later, MESSAGES)[:len(earlier["messages"])] == wire(earlier, MESSAGES)


# Which conversation gets it --------------------------------------------------------------------------

def test_only_the_main_conversation_at_a_real_session_start_gets_the_records(home, tmp_path):
    store = later_session(home, tmp_path)
    engine = RewriteEngine(store, config=ROOMY)

    def send(req: dict, cli_session: str = OLD, agent: bool = False) -> tuple[dict, list[dict]]:
        upstream, _, events = engine(req, headers(cli_session, agent=agent), MESSAGES)
        engine.observe_usage(MESSAGES, {"input_tokens": 300}, req)
        return upstream, events

    upstream, events = send(side_request("write a title", OLD))  # the first conversation seen is main
    assert events[-1]["main"] is True and marks(upstream) == 0 and "start_text" not in names(events)
    upstream, events = send(main_request(conversation("first prompt", 1), OLD))
    assert events[-1]["main"] is True and marks(upstream) == 1 and "start_text" in names(events)
    upstream, events = send(subagent_request(OLD), agent=True)
    assert events[-1]["main"] is False and marks(upstream) == 0 and "start_text" not in names(events)
    upstream, events = send(side_request("name the topic", OLD))
    assert events[-1]["main"] is False and marks(upstream) == 0
    # /clear: Claude Code's next session is the new main conversation, and starts the same way.
    upstream, events = send(main_request(conversation("after clear", 1), NEW), NEW)
    assert {"event": "main_changed", "reason": "new_cli_session", "turn_starts": 1} in events
    assert marks(upstream) == 1 and "start_text" in names(events)


def test_a_conversation_that_becomes_main_later_gets_the_records_at_its_first_rotation_not_before(home, tmp_path):
    store = later_session(home, tmp_path)
    engine = RewriteEngine(store, config=ROTATING)
    drive(engine, MESSAGES, messages_script(2)[:3])  # main, with two turn starts
    requests = messages_script(10, tag="another conversation")
    run = drive(engine, MESSAGES, requests)
    mains = [events[-1]["main"] for _, events, _ in run]
    took_over = mains.index(True)
    assert mains[0] is False and took_over == 4  # at its third turn start: strictly more than main's two
    assert run[took_over][1][0]["event"] == "main_changed" and "start_text" not in names(run[took_over][1])
    rotated = next(i for i, (_, events, _) in enumerate(run) if "rotation" in names(events))
    assert rotated > took_over
    for i, (upstream, _, _) in enumerate(run):
        assert marks(upstream) == (0 if i < rotated else 1)  # the state existed: nothing changes mid-epoch
    for (earlier, _, _), (later, _, _) in zip(run[:rotated - 1], run[1:rotated]):
        assert wire(later, MESSAGES)[:len(earlier["messages"])] == wire(earlier, MESSAGES)
    state = state_of(engine, MESSAGES, requests[-1])
    assert state.start_text == "" and MARK in state.pinned_text


def test_a_store_that_fails_reads_as_nothing_carried_and_never_fails_or_degrades_the_request(home, tmp_path):
    store = Failing(later_session(home, tmp_path), carried_text=OSError("/home/someone/secret-project/ledger.jsonl"))
    engine = RewriteEngine(store, config=ROOMY)
    requests = messages_script(2)
    run = drive(engine, MESSAGES, requests[:3])
    upstream, events, _ = run[0]
    assert {"event": "start_text_unavailable", "epoch": 0, "error": "OSError"} in events
    assert "secret" not in repr(events) and names(events)[-1] == "request"
    assert serialize_request(upstream) == serialize_request(request(requests[0]))  # sent as the CLI sent it
    state = state_of(engine, MESSAGES, requests[0])
    assert not state.degraded and state.start_text == ""
    assert not any(e["event"] in ("degraded_enter", "store_error") for _, events, _ in run for e in events)
    assert store.calls.count("carried_text") == 1  # the state exists now: not read again mid-epoch
    assert all(marks(upstream) == 0 for upstream, _, _ in run)


# count_tokens, degraded mode, the terminal step ------------------------------------------------------

def test_count_tokens_gets_the_same_transform_and_changes_nothing(home, tmp_path):
    store = later_session(home, tmp_path)
    engine = RewriteEngine(store, config=ROOMY)
    requests = messages_script(3)
    early = request(requests[0])
    counted, _, events = engine(early, {}, COUNT_TOKENS)
    assert counted is early and events == [{"event": "count_tokens", "epoch": 0}]  # no state yet: as sent
    upstream = drive(engine, MESSAGES, requests[:2])[-1][0]
    states = copy.deepcopy(engine.states)
    counted, _, events = engine(request(requests[4]), {}, COUNT_TOKENS)
    assert wire(counted, MESSAGES)[:len(upstream["messages"])] == wire(upstream, MESSAGES) and marks(counted) == 1
    assert engine.states == states and events == [{"event": "count_tokens", "epoch": 0}]


def test_degraded_mode_before_the_first_rotation_keeps_the_start_text_and_the_terminal_step_sends_the_original(
        home, tmp_path, monkeypatch):
    store = Failing(later_session(home, tmp_path), page_out=OSError("synthetic disk full"))
    engine = RewriteEngine(store, config=ROTATING)
    requests = messages_script(10)
    run = drive(engine, MESSAGES, requests[:-1])
    entered = next(i for i, (_, events, _) in enumerate(run) if "degraded_enter" in names(events))
    state = state_of(engine, MESSAGES, requests[0])
    assert state.degraded and state.k == 0 and state.epoch == 0 and entered < len(run) - 2
    for (earlier, _, _), (later, events, _) in zip(run[entered:], run[entered + 1:]):
        assert events[-1]["degraded"] is True and marks(later) == 1  # the last good transform, extended
        assert wire(later, MESSAGES)[:len(earlier["messages"])] == wire(earlier, MESSAGES)

    def broken(messages, state):
        raise RuntimeError("synthetic")

    monkeypatch.setattr("contexto.rewrite.transform_messages", broken)
    sent = request(requests[-1])
    upstream, _, events = engine(sent, {}, MESSAGES)
    assert upstream is sent and marks(upstream) == 0  # rule 14's terminal step: the CLI's own request
    assert "fallback_failed" in names(events) and events[-1]["fallback_failed"] is True


# The epoch record (rule 16) --------------------------------------------------------------------------

BEFORE_D69_RECORD_FIELDS = sorted([
    "epoch", "k", "strip_upto", "turns_paged", "max_turn_growth", "last_turn_count", "carried_text_streak",
    "window_minimal_count", "last_turn_start_n", "head_tokens", "initial_carried", "last_carried_text_count",
    "turn_room_low_epoch", "window_budget", "pinned_text", "index_text", "preamble_sha256", "head_fingerprint",
    "dialect", "conversation", "model", "paged_carried", "config", "anchor", "head_rule", "version"])


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_a_restarted_engine_resumes_before_the_first_rotation_with_the_same_bytes(endpoint, home, tmp_path):
    store = later_session(home, tmp_path)
    text = carried(store)
    requests = script(endpoint, 3)
    before = RewriteEngine(store, config=ROOMY)
    last = drive(before, endpoint, requests[:4])[-1][0]
    record = store.epoch_record()
    assert record["start_text"] == text and (record["epoch"], record["k"]) == (0, 0)
    assert sorted(record) == sorted([*BEFORE_D69_RECORD_FIELDS, "start_text"])
    after = RewriteEngine(store, config=ROOMY)
    got, _, events = after(body(endpoint, requests[4]), {}, endpoint)
    expected, _, _ = before(body(endpoint, requests[4]), {}, endpoint)
    assert "epoch_resumed" in names(events) and not {"start_text", "start_text_changed"} & set(names(events))
    assert serialize_request(got) == serialize_request(expected) and marks(got) == 1
    assert wire(got, endpoint)[:len(last[field(endpoint)])] == wire(last, endpoint)
    assert state_of(after, endpoint, requests[4]).start_text == text


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("how", ["closed", "forget --record", "forget --session", "--no-project-memory"])
def test_a_removal_reaches_a_running_session_at_its_next_turn_start_and_a_resumed_one_at_once(
        endpoint, how, home, tmp_path):
    """Review B, poc5: a record taken away is not sent again, by the running engine from its next turn
    start, by a restarted one from its first request, or from the epoch record."""
    store = later_session(home, tmp_path)
    text = carried(store)
    gone = store.carried()[0]
    requests = script(endpoint, 3)  # [2] and [4] are turn starts, [3] is inside a tool loop
    engine = RewriteEngine(store, config=ROOMY)
    drive(engine, endpoint, requests[:2])
    if how == "closed":
        main_close(store, gone.record.id, "done")
    elif how == "forget --record":
        forget(home, gone.session, records=[gone.record.id])
    elif how == "forget --session":
        forget(home, store.session_id, leave=True)
    else:
        store.set_project_memory(False)  # what a resume with --no-project-memory records
    fresh = store.carried_text(6_000)
    assert fresh != text and gone.record.body not in fresh
    upstream, _, events = engine(body(endpoint, requests[2]), {}, endpoint)
    assert gone.record.body.encode() not in serialize_request(upstream)
    changed = [e for e in events if e["event"] == "start_text_changed"]
    assert changed == [{"event": "start_text_changed", "epoch": 0,
                        "tokens": engine.estimator.estimate(fresh) if fresh else 0}]
    assert marks(upstream) == (1 if fresh else 0)
    record = store.epoch_record()
    assert record.get("start_text", "") == fresh and gone.record.body not in json.dumps(record)
    # A restarted engine resumed from a record written before the removal reads the store again.
    store.set_epoch({**record, "start_text": text})
    again = RewriteEngine(store, config=ROOMY)
    upstream, _, events = again(body(endpoint, requests[3]), {}, endpoint)
    assert "epoch_resumed" in names(events) and "start_text_changed" in names(events)
    assert gone.record.body.encode() not in serialize_request(upstream)


def test_a_start_text_is_read_again_only_at_a_turn_start_of_the_main_conversation_at_k_0(home, tmp_path):
    store = later_session(home, tmp_path)
    requests = messages_script(3)
    engine = RewriteEngine(store, config=ROOMY)
    reads = []
    real = store.carried_text

    def counting(budget, **options):
        reads.append(budget)
        return real(budget, **options)

    store.carried_text = counting
    drive(engine, MESSAGES, requests[:5])
    assert len(reads) == 3  # the session start, then the turn starts of requests 2 and 4; never in a tool loop


def test_a_record_written_before_d69_resumes_and_gets_no_start_text_mid_epoch(home, tmp_path):
    root = repo(tmp_path)
    first = session(home, "s1", root, 1)
    store = session(home, "s2", root, 2)
    requests = messages_script(3)
    before = RewriteEngine(store, config=ROOMY)
    last = drive(before, MESSAGES, requests[:4])[-1][0]
    assert sorted(store.epoch_record()) == BEFORE_D69_RECORD_FIELDS  # nothing carried: the record of v0.5.24
    main_pin(first, "obligation", "standing rule 0: whenever you give a date, use ISO 8601")
    assert MARK in store.carried_text(6_000)  # what a session started now would get
    after = RewriteEngine(store, config=ROOMY)
    got, _, events = after(request(requests[4]), {}, MESSAGES)
    assert "epoch_resumed" in names(events) and "start_text" not in names(events)
    assert marks(got) == 0 and wire(got, MESSAGES)[:len(last["messages"])] == wire(last, MESSAGES)
    assert state_of(after, MESSAGES, requests[4]).start_text == ""


def test_the_store_takes_the_start_text_as_an_optional_string_and_a_release_before_d69_reads_no_record(
        home, tmp_path, monkeypatch):
    store = later_session(home, tmp_path)
    text = carried(store)
    drive(RewriteEngine(store, config=ROOMY), MESSAGES, messages_script(1))
    record = store.epoch_record()
    without = {k: v for k, v in record.items() if k != "start_text"}
    assert store_module.read_epoch(record) == record and store_module.read_epoch(without) == without
    assert record["start_text"] == text
    with pytest.raises(ValueError, match="wrong type"):
        store_module.check_epoch({**record, "start_text": 7})
    # v0.5.24's check knows one optional field. It refuses a record with another, so that engine reads
    # no record and starts from epoch 0 (epoch_not_resumed, no_record), as for any record it cannot use.
    monkeypatch.setattr(store_module, "EPOCH_OPTIONAL_FIELDS",
                        {"head_rule": store_module.EPOCH_OPTIONAL_FIELDS["head_rule"]})
    with pytest.raises(ValueError, match=r"unexpected \['start_text'\]"):
        store_module.check_epoch(record)
    assert store_module.read_epoch(record) is None and store_module.read_epoch(without) == without


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_a_project_that_cannot_be_read_at_a_turn_start_keeps_the_text_sent(endpoint, home, tmp_path):
    """Review A round 2, finding 2: a failure to list the sessions is not "nothing is carried now"."""
    store = later_session(home, tmp_path)
    text = carried(store)
    requests = script(endpoint, 3)
    engine = RewriteEngine(store, config=ROOMY)
    drive(engine, endpoint, requests[:2])
    sessions = home / "sessions"
    mode = sessions.stat().st_mode
    sessions.chmod(0o300)  # not listable, still traversable
    try:
        assert store.carried_text(6_000) == ""  # other callers: nothing carried, as before
        with pytest.raises(PermissionError):
            store.carried_text(6_000, strict=True)
        upstream, _, events = engine(body(endpoint, requests[2]), {}, endpoint)
    finally:
        sessions.chmod(mode)
    assert {"event": "start_text_unavailable", "epoch": 0, "error": "PermissionError"} in events
    assert "start_text_changed" not in names(events) and marks(upstream) == 1
    assert state_of(engine, endpoint, requests[2]).start_text == text
    upstream, _, events = engine(body(endpoint, requests[4]), {}, endpoint)  # the next turn start
    assert "start_text_changed" not in names(events) and marks(upstream) == 1


def test_a_project_part_that_raises_at_a_turn_start_keeps_the_text_sent(home, tmp_path, monkeypatch):
    store = later_session(home, tmp_path)
    text = carried(store)
    requests = script(MESSAGES, 3)
    engine = RewriteEngine(store, config=ROOMY)
    drive(engine, MESSAGES, requests[:2])

    def broken(self, own=None):
        raise MemoryError

    monkeypatch.setattr(store_module.project.Project, "carried", broken)
    upstream, _, events = engine(body(MESSAGES, requests[2]), {}, MESSAGES)
    assert {"event": "start_text_unavailable", "epoch": 0, "error": "MemoryError"} in events
    assert state_of(engine, MESSAGES, requests[2]).start_text == text and marks(upstream) == 1


def test_a_start_text_that_grows_at_a_turn_start_is_in_the_pre_send_estimate(home, tmp_path):
    """Review A round 2, finding 3 (repro_estimate.py): a record pinned before the session started and
    confirmed after it joins the start text at a turn start; rule 3's estimate counts it."""
    root = repo(tmp_path)
    first = session(home, "s1", root, 1)
    for i in range(3):
        main_pin(first, "obligation", f"standing rule {i}: use ISO 8601")
    late = first.pin("obligation", "日" * 480)  # pinned before s2 starts; its confirmation comes later
    second = session(home, "s2", root, 2)
    engine = RewriteEngine(second, config=ROOMY)
    requests = script(MESSAGES, 3)
    drive(engine, MESSAGES, requests[:2])
    first.add_main_records([late])
    upstream, _, events = engine(body(MESSAGES, requests[2]), {}, MESSAGES)
    assert "start_text_changed" in names(events)
    real = usage(upstream)["input_tokens"]
    estimate = state_of(engine, MESSAGES, requests[2]).last_estimate
    assert abs(real - estimate) < 100, (real, estimate)  # the control without the confirmation is 16 short


@pytest.mark.parametrize("which", ["meta.json", "ledger.jsonl"])
def test_an_earlier_session_that_fails_to_read_for_a_passing_reason_keeps_the_text_sent(which, home, tmp_path,
                                                                                         monkeypatch):
    """Review A round 3, finding 3: an I/O error reading one earlier session's metadata or ledger at a turn
    start is a failure to read the project, not a removal: the start text stays, and once the file reads
    again nothing has been lost."""
    import errno

    from contexto.store import project as project_module
    from contexto.store.project import forget_cache

    store = later_session(home, tmp_path)
    text = store.carried_text(6_000)
    assert text
    requests = script(MESSAGES, 4)
    engine = RewriteEngine(store, config=ROOMY)
    drive(engine, MESSAGES, requests[:2])
    target = home / "sessions" / "s1" / which
    real = project_module.read_regular

    def failing(path, *args, **kwargs):
        if path == target:
            raise OSError(errno.EIO, "Input/output error", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(project_module, "read_regular", failing)
    forget_cache()
    with pytest.raises(OSError):
        store.carried_text(6_000, strict=True)
    engine(body(MESSAGES, requests[2]), {}, MESSAGES)
    assert state_of(engine, MESSAGES, requests[2]).start_text == text
    monkeypatch.setattr(project_module, "read_regular", real)
    forget_cache()
    engine(body(MESSAGES, requests[4]), {}, MESSAGES)
    assert state_of(engine, MESSAGES, requests[4]).start_text == text


def test_a_permission_error_is_not_a_passing_failure(home, tmp_path):
    """Review B round 4, N12: a session whose files cannot be read for want of permission (anywhere in the
    home) does not stop the strict refresh: it lasts until someone changes the mode, so it is a session
    skipped, as damage is, and a removal still reaches the running session."""
    from contexto.store.project import forget_cache

    store = later_session(home, tmp_path)
    assert store.carried_text(6_000)
    target = home / "sessions" / "s1" / "meta.json"
    mode = target.stat().st_mode
    target.chmod(0)
    forget_cache()
    try:
        assert store.carried_text(6_000, strict=True) == ""
    finally:
        target.chmod(mode)


@pytest.mark.parametrize("damage", ["deleted", "fifo", "corrupt"])
def test_damage_at_a_turn_start_reads_as_that_sessions_records_removed(damage, home, tmp_path):
    """Review A round 4, nit c: only a passing read error holds the refresh; a session deleted since the
    listing, a non-regular file or a ledger that does not verify reads as its records removed."""
    import os
    import shutil

    from contexto.store.project import forget_cache

    store = later_session(home, tmp_path)
    assert store.carried_text(6_000)
    session = home / "sessions" / "s1"
    if damage == "deleted":
        shutil.rmtree(session)
    elif damage == "fifo":
        (session / "ledger.jsonl").unlink()
        os.mkfifo(session / "ledger.jsonl")
    else:
        (session / "ledger.jsonl").write_bytes(b"{not json}\n" + (session / "ledger.jsonl").read_bytes())
    forget_cache()
    assert store.carried_text(6_000, strict=True) == ""
