"""The scale harness against the package it measures: fail when the package moves from under it.

The harness wraps timers around methods of the store, the proxy handler and the engine by name
(``worker.py``), counts the package's events by name (``run.py``), and sends a synthetic session the
engine must take for Claude Code's (``synth.py``). On 2026-11-12 it had not run since the store was split
into ``contexto/store/*.py``: ``SessionStore._pages`` was gone and nothing in the suite noticed. These
tests name every place the harness depends on the package, so the next refactor fails here instead.
"""

from __future__ import annotations

import inspect
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
for p in (ROOT / "spikes" / "m2", ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from scale.proxy import fidelity, run, synth, worker  # noqa: E402

from contexto import cliversions, launcher, mcp, rewrite, toolcheck  # noqa: E402
from contexto import proxy as proxy_mod  # noqa: E402
from contexto.store import SessionStore  # noqa: E402

SHAPE = synth.Shape(head_tokens=20_000, turn_tokens=8_000)


def test_every_instrumented_name_is_an_attribute_of_its_class():
    assert worker.INSTRUMENTED, "the harness instruments nothing"
    missing = [f"{cls.__name__}.{name}" for cls, names in worker.INSTRUMENTED for name in names
               if not hasattr(cls, name) and name not in getattr(cls, "__dataclass_fields__", {})]
    assert not missing, f"the harness instruments names the package no longer has: {missing}"


def test_instrumented_lists_are_the_ones_the_harness_wraps():
    by_class = dict(worker.INSTRUMENTED)
    assert set(worker.STORE_METHODS) < set(by_class[SessionStore])
    assert set(worker.CATALOG_METHODS) == set(by_class[worker.PageCatalog])
    assert set(worker.HANDLER_METHODS) == set(by_class[proxy_mod._Handler])
    # A timer key per store method, and the columns the driver reads are keys the worker writes.
    keys = {"store_" + name for name in worker.STORE_METHODS} | set(worker.CATALOG_METHODS.values())
    source = inspect.getsource(run.merge_stats) + inspect.getsource(worker.fast_forward)
    read = set(re.findall(r'rec\.get\("(store_[a-z_]+?)(?:_n)?"', source))
    assert read and read <= keys, f"the driver reads timer keys the worker does not write: {read - keys}"


def test_the_store_timers_are_reached_by_the_calls_the_engine_makes(tmp_path):
    """A page written and indexed through an instrumented store shows up under every key the CSV
    columns are read from. A second store on the same files reads the page once and not again."""
    worker.METER.take()
    store = launcher.open_session(tmp_path, "drift", tmp_path)
    worker.instrument_store(store)
    worker.METER.begin("write")
    store.page_out([synth.turn(SHAPE, 1)], 1)
    store.index_text(4_000)
    store.pinned_text(1_000)
    written = worker.METER.take()[0]
    for key in ("store_page_out", "store_index_text", "store_pinned_text", "store_pages"):
        assert written.get(key + "_n", 0) >= 1, (key, written)
    worker.uninstrument_store(store)
    assert "_page_catalog" not in store.__dict__ and "index_text" not in store.__dict__

    again = SessionStore(tmp_path, "drift")
    worker.instrument_store(again)
    worker.METER.begin("read")
    again.index_text(4_000)
    first = worker.METER.take()[0]
    worker.METER.begin("read-again")
    again.index_text(4_000)
    second = worker.METER.take()[0]
    assert first.get("store_page_read_n", 0) >= 1 and second.get("store_page_read_n", 0) == 0, (first, second)
    assert second["store_pages_n"] == 1


def _source(module) -> str:
    return "".join(path.read_text(encoding="utf-8") for path in sorted(Path(module.__file__).parent.rglob("*.py")))


def test_every_event_the_harness_counts_is_one_the_package_writes():
    package = _source(launcher)
    names = {*worker.EVENT_NAMES, *run.COUNTED_EVENTS, *run.BREAK_EVENTS}
    gone = sorted(name for name in names if f'"{name}"' not in package)
    assert not gone, f"the harness counts events the package no longer writes: {gone}"
    # A break event that is not counted is never seen (page_conflict and observe_usage_error, until 2026-11-12).
    assert set(run.BREAK_EVENTS) <= set(run.COUNTED_EVENTS)
    assert all(f"n_{name}" in run.CHECKPOINT_FIELDS for name in run.COUNTED_EVENTS)


def test_the_launcher_and_proxy_take_what_the_harness_passes():
    start = inspect.signature(launcher.start_proxy).parameters
    assert list(start)[:5] == ["store", "upstream_url", "upstream_source", "config", "observe_thinking"]
    assert {"stderr", "cli"} <= set(start)
    assert list(inspect.signature(launcher.build_engine).parameters) == ["store", "config"]
    assert list(inspect.signature(launcher.open_session).parameters) == ["home", "session_id", "workdir"]
    assert list(inspect.signature(launcher.NoticeLog).parameters) == ["store", "stderr"]
    assert callable(launcher.report_missing_tools) and callable(launcher.SessionRecorder.flush)
    for name in ("serialize_request", "_scalar_summary", "utc_now"):
        assert callable(getattr(proxy_mod, name)), name
    observe = inspect.signature(rewrite.RewriteEngine.observe_usage).parameters
    assert list(observe)[1:4] == ["endpoint", "usage", "request"] and {"tool_calls", "tool_call_ids"} <= set(observe)
    assert {"k", "epoch", "turns_paged"} <= set(rewrite.EpochState.__dataclass_fields__)
    assert set(fidelity.DECISION_FIELDS) <= set(rewrite.RECORD_INT)


def test_the_timed_engine_is_the_engine_to_the_proxy_and_the_launcher(tmp_path):
    """The proxy passes ``tool_calls`` and ``tool_call_ids`` only to a callback that takes them, skips its
    defensive copy only for a transform that declares ``preserves_input``, and primes PDFs only through
    ``prime``; the launcher's recorder forwards all of them from what it wraps."""
    store = launcher.open_session(tmp_path, "timed", tmp_path)
    engine, _, name = launcher.build_engine(store, {"bound": 150_000})
    assert name == "rewrite"
    proxy = worker.start_proxy(store, engine, "http://127.0.0.1:9", 150_000)
    try:
        assert isinstance(proxy.transform, launcher.SessionRecorder)
        assert isinstance(proxy.session_log, launcher.NoticeLog) and isinstance(proxy.session_log.stderr, io.StringIO)
        assert proxy.transform.preserves_input is True and proxy.transform.prime == engine.prime
        assert proxy_mod._takes_keyword(proxy.observe_usage, "tool_calls")
        assert proxy_mod._takes_keyword(proxy.observe_usage, "tool_call_ids")
        assert proxy.affinity_header, "a local upstream gets the session's X-Session-Affinity (D36)"
        assert launcher.build_engine.__module__ == "contexto.launcher", "the launcher's own function is back"
    finally:
        worker.stop_proxy(proxy)


def test_the_engine_takes_the_synthetic_session_for_claude_code():
    head = synth.head(SHAPE)
    first = synth.turn(SHAPE, 0)
    request = synth.request(head, [], first[:2])
    headers = {"User-Agent": synth.USER_AGENT, "X-Claude-Code-Session-Id": synth.CLAUDE_CODE_SESSION}
    # The session-start message, by the signature of D33.
    assert rewrite.is_session_start(first[1]) and len(first[1]["content"][0]["text"]) == synth.SESSION_START_CHARS
    dialect = rewrite.dialect_for("/v1/messages")
    assert dialect.session_start_seen(request["messages"]) is True
    token = rewrite._DIALECT.set(dialect)
    try:
        assert rewrite.classify_system(request["messages"], 1) == rewrite.SESSION_START
    finally:
        rewrite._DIALECT.reset(token)
    # A tested Claude Code version, and one session id in the header and in metadata.user_id.
    assert synth.CLAUDE_CODE_VERSION == cliversions.TESTED_VERSIONS["claude"][-1]
    assert cliversions.client_version(headers) == ("claude", "claude-cli", synth.CLAUDE_CODE_VERSION)
    session = rewrite.claude_code_session(request, headers)
    assert session == rewrite.ClaudeCodeSession(synth.CLAUDE_CODE_SESSION, None, "cli", False)
    assert rewrite.claude_code_session(request, {}).id == synth.CLAUDE_CODE_SESSION
    assert launcher.claude_session({}, request) == launcher.claude_session(headers, request) == synth.CLAUDE_CODE_SESSION
    # Contexto's MCP tools, as the tool check looks for them.
    assert set(synth.CONTEXTO_TOOLS) == {toolcheck.TOOL_PREFIX + tool["name"] for tool in mcp.TOOLS}
    assert toolcheck.anthropic_tools_offered(request) is True
    # And the checks themselves report nothing for it.
    assert cliversions.ClientVersions().observe(request, headers, dialect) == [
        {"event": "cli_version", "cli": "claude", "client": "claude-cli", "version": synth.CLAUDE_CODE_VERSION,
         "tested": True, "tested_versions": cliversions.tested_list("claude")}]
