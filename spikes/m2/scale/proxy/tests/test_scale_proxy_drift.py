"""The scale harness against the package it measures: fail when the package moves from under it.

The harness wraps timers around methods of the store, the proxy handler and the engine by name
(``worker.py``), counts the package's events by name (``run.py``), and sends a synthetic session the
engine must take for Claude Code's (``synth.py``). On 2026-11-12 it had not run since the store was split
into ``contexto/store/*.py``: ``SessionStore._pages`` was gone and nothing in the suite noticed. These
tests name every place the harness depends on the package, so the next refactor fails here instead.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import io
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
for p in (ROOT / "spikes" / "m2", ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from scale.proxy import cputime, fidelity, run, synth, upstream, worker  # noqa: E402

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
    message = "worker.INSTRUMENTED must list every name worker.py wraps; update it with the wrapping code"
    assert set(worker.STORE_METHODS) < set(by_class[SessionStore]), message
    assert set(worker.CATALOG_METHODS) == set(by_class[worker.PageCatalog]), message
    assert set(worker.HANDLER_METHODS) == set(by_class[proxy_mod._Handler]), message
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
        assert written.get(key + "_n", 0) >= 1, (f"no call reached the {key} timer; the store no longer calls "
                                                 f"what worker.instrument_store wraps", written)
    worker.uninstrument_store(store)
    assert "_page_catalog" not in store.__dict__ and "index_text" not in store.__dict__, \
        "the store keeps its page catalog under another name; update worker.uninstrument_store, or pickling fails"

    again = SessionStore(tmp_path, "drift")
    worker.instrument_store(again)
    worker.METER.begin("read")
    again.index_text(4_000)
    first = worker.METER.take()[0]
    worker.METER.begin("read-again")
    again.index_text(4_000)
    second = worker.METER.take()[0]
    assert first.get("store_page_read_n", 0) >= 1 and second.get("store_page_read_n", 0) == 0, (
        "the page catalog no longer reads each page file once per process; the page_file_reads column and "
        "worker.CATALOG_METHODS describe that", first, second)
    assert second["store_pages_n"] == 1, "index_text no longer goes through PageCatalog.entries; move the timer"


def _strings(node: ast.AST) -> set[str]:
    """The string constants an expression can evaluate to: a literal, or either branch of ``a if c else b``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _strings(node.body) | _strings(node.orelse)
    return set()


def emitted_events(package: Path) -> set[str]:
    """Every event name the package writes: the value of an ``"event"`` key in a dict literal or of an
    ``event=`` keyword, as a string literal, a conditional of literals, or a variable that the same
    function assigns literals to (``kind = "page_conflict" if ... else "store_error"``). A name that is
    only compared or mentioned does not count, so an event that stops being written fails the check
    even while its name is still in the source."""
    names: set[str] = set()
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scopes = [tree, *(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))]
        for scope in scopes:
            values: list[ast.AST] = []
            assigned: dict[str, set[str]] = {}
            for node in ast.walk(scope):
                if isinstance(node, ast.Dict):
                    values += [v for k, v in zip(node.keys, node.values, strict=True)
                               if isinstance(k, ast.Constant) and k.value == "event"]
                elif isinstance(node, ast.keyword) and node.arg == "event":
                    values.append(node.value)
                elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            assigned.setdefault(target.id, set()).update(_strings(node.value))
            for value in values:
                names |= _strings(value)
                if isinstance(value, ast.Name):
                    names |= assigned.get(value.id, set())
    return names


def test_every_event_the_harness_counts_is_one_the_package_writes():
    written = emitted_events(Path(launcher.__file__).parent)
    names = {*worker.EVENT_NAMES, *run.COUNTED_EVENTS, *run.BREAK_EVENTS}
    gone = sorted(names - written)
    assert not gone, (f"the harness counts events the package no longer writes: {gone}; remove them from "
                      "worker.EVENT_NAMES, run.COUNTED_EVENTS or run.BREAK_EVENTS, or count the event that "
                      "replaced them")
    # A break event that is not counted is never seen (page_conflict and observe_usage_error, until 2026-11-12).
    assert set(run.BREAK_EVENTS) <= set(run.COUNTED_EVENTS), "add every run.BREAK_EVENTS name to run.COUNTED_EVENTS"
    assert all(f"n_{name}" in run.CHECKPOINT_FIELDS for name in run.COUNTED_EVENTS), "run.CHECKPOINT_FIELDS"


def test_the_event_check_sees_an_event_that_is_no_longer_written(tmp_path):
    """Reviewer B's mutation of 2026-11-12: ``store_failure`` always says ``store_error``. The name
    ``page_conflict`` stays in the source, in a comparison, and the check must still miss it."""
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "m.py").write_text(
        'def store_failure(error):\n'
        '    kind = "page_conflict" if error else "store_error"\n'
        '    failure = {"event": kind}\n'
        '    return failure\n'
        'def other():\n'
        '    return [{"event": "rotation"}, dict(event="resync"), "carried_paged"]\n', encoding="utf-8")
    assert emitted_events(package) == {"page_conflict", "store_error", "rotation", "resync"}
    source = (package / "m.py").read_text(encoding="utf-8")
    (package / "m.py").write_text(source.replace('kind = "page_conflict" if error else "store_error"',
                                                 'kind = "store_error"\n    if kind == "page_conflict":\n'
                                                 '        pass'), encoding="utf-8")
    assert emitted_events(package) == {"store_error", "rotation", "resync"}


def test_the_launcher_and_proxy_take_what_the_harness_passes():
    start = inspect.signature(launcher.start_proxy).parameters
    assert list(start)[:5] == ["store", "upstream_url", "upstream_source", "config", "observe_thinking"], \
        "launcher.start_proxy changed its parameters; update worker.start_proxy, which calls it"
    assert {"stderr", "cli"} <= set(start), "launcher.start_proxy changed; update worker.start_proxy"
    assert list(inspect.signature(launcher.build_engine).parameters) == ["store", "config"], \
        "launcher.build_engine changed; update worker.start_proxy (it stands in for it) and worker.open_engine"
    assert list(inspect.signature(launcher.open_session).parameters) == ["home", "session_id", "workdir"], \
        "launcher.open_session changed; update worker.open_engine and cputime.child"
    assert list(inspect.signature(launcher.NoticeLog).parameters) == ["store", "stderr"], \
        "launcher.NoticeLog changed; update worker.fast_forward and cputime.child"
    assert callable(launcher.report_missing_tools) and callable(launcher.SessionRecorder.flush), \
        "the launcher's stop sequence changed; update worker.stop_proxy"
    for name in ("serialize_request", "_scalar_summary", "utc_now"):
        assert callable(getattr(proxy_mod, name, None)), f"contexto.proxy.{name} is gone; update worker.fast_forward"
    observe = inspect.signature(rewrite.RewriteEngine.observe_usage).parameters
    assert list(observe)[1:4] == ["endpoint", "usage", "request"] and {"tool_calls", "tool_call_ids"} <= set(observe), \
        "RewriteEngine.observe_usage changed; update worker.TimedEngine, worker.fast_forward and cputime.child"
    assert {"k", "epoch", "turns_paged"} <= set(rewrite.EpochState.__dataclass_fields__), \
        "rewrite.EpochState lost a field worker.fast_forward writes to ff_*.csv"
    assert set(fidelity.DECISION_FIELDS) <= set(rewrite.RECORD_INT), \
        "the epoch record lost a field fidelity.DECISION_FIELDS compares; update fidelity.py"


def test_the_timed_engine_is_the_engine_to_the_proxy_and_the_launcher(tmp_path):
    """The proxy passes ``tool_calls`` and ``tool_call_ids`` only to a callback that takes them, skips its
    defensive copy only for a transform that declares ``preserves_input``, and primes PDFs only through
    ``prime``; the launcher's recorder forwards all of them from what it wraps."""
    store = launcher.open_session(tmp_path, "timed", tmp_path)
    engine, _, name = launcher.build_engine(store, {"bound": 150_000})
    assert name == "rewrite"
    proxy = worker.start_proxy(store, engine, "http://127.0.0.1:9", 150_000)
    try:
        message = "the launcher wires the proxy differently; update worker.start_proxy and worker.TimedEngine"
        assert isinstance(proxy.transform, launcher.SessionRecorder), message
        assert isinstance(proxy.session_log, launcher.NoticeLog) and isinstance(proxy.session_log.stderr, io.StringIO), \
            message
        assert proxy.transform.preserves_input is True and proxy.transform.prime == engine.prime, \
            "worker.TimedEngine no longer forwards preserves_input or prime to the proxy"
        assert proxy_mod._takes_keyword(proxy.observe_usage, "tool_calls"), \
            "worker.TimedEngine.observe_usage hides tool_calls from the proxy"
        assert proxy_mod._takes_keyword(proxy.observe_usage, "tool_call_ids"), \
            "worker.TimedEngine.observe_usage hides tool_call_ids from the proxy"
        assert proxy.affinity_header, "a local upstream gets the session's X-Session-Affinity (D36)"
        assert launcher.build_engine.__module__ == "contexto.launcher", "the launcher's own function is back"
    finally:
        worker.stop_proxy(proxy)


def test_fast_forward_sends_the_headers_the_driver_sends(tmp_path, capsys):
    """The engine reads the CLI version and the session id from the request headers. Fast-forward calls
    it directly, and called it with none until 2026-11-13 (Reviewer A, nit 6): its session logged no
    ``cli_version`` event, where the HTTP path logs one."""
    assert synth.CLI_HEADERS == {"User-Agent": synth.USER_AGENT, "X-Claude-Code-Session-Id": synth.CLAUDE_CODE_SESSION}
    shape = json.dumps({"head_tokens": 20_000, "turn_tokens": 3_000})
    args = argparse.Namespace(home=str(tmp_path), session="ff", bound=150_000, engine_in=None, shape=shape,
                              from_turn=0, to_turn=2, engine_out=str(tmp_path / "engine.pkl"),
                              csv_out=str(tmp_path / "ff.csv"))
    assert worker.fast_forward(args) == 0
    capsys.readouterr()
    counts, _ = run.log_events(tmp_path / "sessions" / "ff" / "log.jsonl")
    assert counts["cli_version"] == 1 and counts["session_start_unrecognised"] == 0, counts


def test_cputime_handles_requests_on_this_checkout(capsys):
    """cputime.py calls the handler's ``_transform`` and the engine without a server; a changed signature
    fails here."""
    assert cputime.child(str(ROOT / "src"), 2, 1) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows and rows[0]["t"] == 2 and rows[0]["j"] == 0
    assert all(row["cpu_s"] >= row["transform_s"] >= 0 and 0 < row["upstream_tokens"] <= row["history_tokens"]
               for row in rows)
    # observe_usage is told what the proxy tells it: the reply's output tokens and whether it called a tool.
    assert all(row["observe_keywords"] == ["tool_call_ids", "tool_calls"] for row in rows), rows[0]
    assert all(row["output_tokens"] == upstream.OUTPUT_TOKENS[row["reply"]] for row in rows), rows


def test_the_engine_takes_the_synthetic_session_for_claude_code():
    head = synth.head(SHAPE)
    first = synth.turn(SHAPE, 0)
    request = synth.request(head, [], first[:2])
    headers = {"User-Agent": synth.USER_AGENT, "X-Claude-Code-Session-Id": synth.CLAUDE_CODE_SESSION}
    # The session-start message, by the signature of D33.
    signature = "the engine no longer knows Claude Code's session start; update synth.SESSION_START_HEADING"
    assert rewrite.is_session_start(first[1]), signature
    assert len(first[1]["content"][0]["text"]) == synth.SESSION_START_CHARS
    dialect = rewrite.dialect_for("/v1/messages")
    assert dialect.session_start_seen(request["messages"]) is True, signature
    token = rewrite._DIALECT.set(dialect)
    try:
        assert rewrite.classify_system(request["messages"], 1) == rewrite.SESSION_START, signature
    finally:
        rewrite._DIALECT.reset(token)
    # A tested Claude Code version, and one session id in the header and in metadata.user_id.
    assert synth.CLAUDE_CODE_VERSION == cliversions.TESTED_VERSIONS["claude"][-1], \
        "a newer Claude Code version is tested; set synth.CLAUDE_CODE_VERSION to it"
    assert cliversions.client_version(headers) == ("claude", "claude-cli", synth.CLAUDE_CODE_VERSION), \
        "the package reads the CLI version from another header; update synth.USER_AGENT and run.Client.send"
    ids = "the package reads Claude Code's session id differently; update synth.head's metadata and run.Client.send"
    session = rewrite.claude_code_session(request, headers)
    assert session == rewrite.ClaudeCodeSession(synth.CLAUDE_CODE_SESSION, None, "cli", False), ids
    assert rewrite.claude_code_session(request, {}).id == synth.CLAUDE_CODE_SESSION, ids
    assert launcher.claude_session({}, request) == launcher.claude_session(headers, request) \
        == synth.CLAUDE_CODE_SESSION, ids
    # Contexto's MCP tools, as the tool check looks for them.
    assert set(synth.CONTEXTO_TOOLS) == {toolcheck.TOOL_PREFIX + tool["name"] for tool in mcp.TOOLS}, \
        "Contexto's MCP tools changed; update synth.CONTEXTO_TOOLS"
    assert toolcheck.anthropic_tools_offered(request) is True, "update synth.CONTEXTO_TOOLS to what toolcheck reads"
    # And the checks themselves report nothing for it.
    assert cliversions.ClientVersions().observe(request, headers, dialect) == [
        {"event": "cli_version", "cli": "claude", "client": "claude-cli", "version": synth.CLAUDE_CODE_VERSION,
         "tested": True, "tested_versions": cliversions.tested_list("claude")}], \
        "the CLI version check reports the synthetic session; update synth.CLAUDE_CODE_VERSION or synth.USER_AGENT"
