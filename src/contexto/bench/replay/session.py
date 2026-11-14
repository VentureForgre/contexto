"""One replay: the real proxy between the synthetic client and the fake upstream, and what was measured.

``replay(script, home)`` does what ``contexto run`` does to start the proxy, with the same functions
(``contexto.launcher``): it checks the bound (``check_options``), opens the session in ``home``
(``open_session``) and starts the proxy with the rewrite engine on 127.0.0.1 (``start_proxy``). The
upstream is the fake one on 127.0.0.1. Then the client sends every turn of the script, one request
per model round, and at the end the proxy is stopped as the launcher stops it.

Search reachability is measured once: after the proxy has forwarded the first request of the first
probe turn and before the client answers that turn's first search call. That is when the probe turns
start, and it is the store the first search reads.

What is measured, and from where:

- every request's size, from the fake upstream's records (its modelled count of the request the
  proxy sent it), with the proxy's own estimate of the same request beside it;
- requests over the bound, counted twice: by the fake upstream's count, and by the proxy's own log
  (a request entry flagged ``over_bound``, or carrying an ``over_bound`` event). The second count is
  stricter than the acceptance harness, which reports the engine's pre-send estimate inside a tool
  loop without failing on it: here any such record fails the replay;
- rotations, every engine event by name, and the failure events, from the session log
  (``FAILURE_EVENTS`` is the acceptance harness's list, ``spikes/m2/acceptance/detectors.py``);
- compaction markers in any request, from the fake upstream (``upstream.compaction_markers``).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contexto.bench import script as bench_script
from contexto.bench.model import Script
from contexto.bench.replay import lines, reach, scripted
from contexto.bench.replay.client import Client, ReplayError, user_message
from contexto.bench.replay.upstream import MESSAGES_PATH, FakeUpstream, model_tokens

# The session's working directory as the store records it: a fixed name that is nobody's directory, so
# a store kept with --out holds no path of the machine it ran on. Nothing is read from or written to it.
WORKDIR = Path("/contexto-bench-replay")
UPSTREAM_SOURCE = "bench-replay"
# Any of these in the session log fails the replay, as it fails acceptance criterion 1
# (spikes/m2/acceptance/detectors.py::FAILURE_EVENTS; tests check the two lists are equal).
FAILURE_EVENTS = (
    "degraded_enter", "store_error", "page_conflict", "fallback_failed", "transform_error", "turn_room_missing",
    "unclassified_system_message", "turn_room_low", "retained_stale", "epoch_record_stale", "mcp_tools_missing",
    "mcp_tools_denied", "toolcheck_error", "side_room_close_failed", "request_too_deep",
)


# Every environment variable the package reads (each ``os.environ`` read under src/contexto, checked by a
# test), held at its default for the length of a replay whatever the user's environment says, so a
# setting such as CONTEXTO_RECALL_VARIANT=legacy cannot change what the pinned trace measures. The
# default of each is "not set". Any other CONTEXTO_ variable is held unset too, so a knob added later is
# covered before anyone lists it, and so is any other CLAUDE_CODE_USE_ variable: the package reads that
# prefix as a prefix, for Claude Code's cloud-provider switches (contexto.upstream.PROVIDER_PREFIX, D70).
# The cloud-provider switches and CLAUDE_CODE_GATEWAY_HINT_HEADERS are read only by what ``contexto run
# claude`` decides before it starts Claude Code (``launcher.claude_launch_settings``), which a replay does
# not call; they are held all the same, so that what a replay measures cannot depend on them as the
# package changes. HOME and PATH are read as well, but only to find a CLI or a memory file's ``@~/``
# import, and a replay has neither, so they are left as they are.
ENVIRONMENT = (
    "ANTHROPIC_BASE_URL", "CLAUDE_CODE_CUSTOM_OAUTH_URL", "CLAUDE_CODE_GATEWAY_HINT_HEADERS",
    "CLAUDE_CODE_IGNORE_PERSISTED_TRUST", "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CONFIG_DIR",
    "CODEX_HOME", "CONTEXTO_CODEX_KEY_HELPER", "CONTEXTO_CODEX_UPSTREAM", "CONTEXTO_HOME", "CONTEXTO_NO_VERSION_CHECK",
    "CONTEXTO_RECALL_VARIANT", "CONTEXTO_SEARCH_CACHE_MB", "CONTEXTO_SESSION", "OPENRIG_SESSION_NAME",
)
ENVIRONMENT_PREFIXES = ("CONTEXTO_", "CLAUDE_CODE_USE_")


def _pinned(name: str) -> bool:
    return name in ENVIRONMENT or name.startswith(ENVIRONMENT_PREFIXES)


@contextlib.contextmanager
def pinned_environment() -> Iterator[dict[str, Any]]:
    """Unset every variable of ``ENVIRONMENT`` and every variable starting with one of
    ``ENVIRONMENT_PREFIXES`` for the block, and put the user's values back after it. Yields what goes
    in ``manifest.replay``: the variables pinned, and the names (never the values) of those the user's
    environment had set."""
    held = {name: os.environ[name] for name in os.environ if _pinned(name)}
    for name in held:
        del os.environ[name]
    try:
        yield {"pinned": [*ENVIRONMENT, *(prefix + "*" for prefix in ENVIRONMENT_PREFIXES)],
               "value": "not set (each one's default)", "set_by_the_user_and_ignored": sorted(held)}
    finally:
        for name in [n for n in os.environ if _pinned(n)]:
            del os.environ[name]
        os.environ.update(held)


@dataclass
class Measured:
    """What one replay measured. ``requests`` are the ``contexto`` arm's ``requests.jsonl`` rows and
    ``reach`` the ``reach.jsonl`` rows."""

    session_id: str
    turns: int
    head_tokens: int
    requests: list[dict[str, Any]]
    reach: list[dict[str, Any]]
    rotations: int
    over_bound: int  # requests above the bound by the fake upstream's count
    over_bound_proxy: int  # requests the proxy's own log marks over the bound
    failure_events: dict[str, int]
    events: dict[str, int]  # every engine and proxy event in the session log, by name
    compaction_markers: int
    pages: int
    toolloop_reads: int
    searches: int
    probe_turns_completed: int
    seconds: float
    problems: list[str] = field(default_factory=list)  # anything that did not go as scripted
    notices: list[str] = field(default_factory=list)  # what the launcher would have printed on stderr
    environment: dict[str, Any] = field(default_factory=dict)  # what pinned_environment held, for the manifest
    private_files_removed: list[str] = field(default_factory=list)  # remove_private_files, with --out

    @property
    def max_input(self) -> int:
        return max((r["uncached"] for r in self.requests), default=0)


def session_name(script: Script) -> str:
    return f"bench-replay-{script.seed}"


def read_log(path: Path) -> list[dict[str, Any]]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _inner(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The engine's events carried on a request entry."""
    return [e for e in [*(entry.get("transform_events") or []), *(entry.get("usage_events") or [])]
            if isinstance(e, dict)]


def event_counts(log: list[dict[str, Any]]) -> dict[str, int]:
    """Every event in the session log by name: the log's own lines, and the engine's events carried
    on request entries. The engine's per-request ``request`` event is the entry itself and is not
    counted twice."""
    counts: dict[str, int] = {}
    for entry in log:
        names = [str(entry.get("event"))]
        if entry.get("event") == "request":
            names += [str(e.get("event")) for e in _inner(entry) if e.get("event") != "request"]
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items()))


def failure_events(log: list[dict[str, Any]]) -> dict[str, int]:
    """``FAILURE_EVENTS`` seen in the session log, by name, wherever they appear."""
    counts: dict[str, int] = {}
    for entry in log:
        found = [entry, *_inner(entry)] if entry.get("event") == "request" else [entry]
        for event in found:
            name = str(event.get("event"))
            if name in FAILURE_EVENTS:
                counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items()))


def proxy_requests(log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The proxy's own record of each ``/v1/messages`` request, in order."""
    return [e for e in log if e.get("event") == "request" and e.get("path") == MESSAGES_PATH]


def _marked_over(entry: dict[str, Any]) -> bool:
    return bool(entry.get("over_bound")) or any(e.get("event") == "over_bound" for e in _inner(entry))


def _estimate(entry: dict[str, Any]) -> int | None:
    """The engine's estimate of the request it sent, from its ``request`` event."""
    for event in entry.get("transform_events") or []:
        if isinstance(event, dict) and event.get("event") == "request":
            value = event.get("sent_estimate", event.get("estimate"))
            return int(value) if isinstance(value, (int, float)) else None
    return None


def head_tokens(fields: dict[str, Any]) -> int:
    """The fake upstream's count of a request that holds the head and no messages."""
    return model_tokens({"model": "", "messages": [], **fields})["tokens"]


def replay(script: Script, home: Path) -> Measured:
    """Replay ``script`` with the session's store under ``home``, with every environment knob of the
    package at its default (``pinned_environment``). Raises ``ReplayError`` when the session cannot be
    carried to its end, and ``contexto.launcher.LaunchError`` for a bound the launcher refuses."""
    with pinned_environment() as environment:
        measured = _replay(script, home)
    measured.environment = environment
    return measured


def _replay(script: Script, home: Path) -> Measured:
    from contexto import launcher
    from contexto.store import SessionStore

    options = launcher.LaunchOptions(bound=script.bound)
    launcher.check_options(options)
    planned = scripted.steps(script)
    session_id = session_name(script)
    started = time.monotonic()
    notices = io.StringIO()
    upstream = FakeUpstream(script, planned)
    upstream.start()
    proxy = None
    rows: list[dict[str, Any]] = []
    completed = 0
    try:
        store = launcher.open_session(home, session_id, WORKDIR)
        config = launcher.engine_config(options)
        proxy, engine = launcher.start_proxy(store, upstream.url, UPSTREAM_SOURCE, config, False, stderr=notices,
                                             cli="claude")
        store.log({"event": "launch", "cli": "bench-replay", "session": session_id, "config": config,
                   "engine": engine, "upstream_source": UPSTREAM_SOURCE})
        # A second handle on the same session, as the MCP server has in a real launch.
        mcp_store = SessionStore(home, session_id)
        client = Client(proxy.port, session_id, script, mcp_store)
        messages: list[dict[str, Any]] = []
        for turn, text in enumerate(script.turns):
            messages.append(user_message(text, first=turn == 0))
            first_round = True
            while True:
                content = client.send(messages)
                messages.append({"role": "assistant", "content": content})
                if turn == script.n_content and first_round:
                    rows = measure_reach(script, mcp_store, upstream.last_messages)
                first_round = False
                calls = [block for block in content if block.get("type") == "tool_use"]
                if not calls:
                    break
                messages.append({"role": "user", "content": [client.tool_result(call) for call in calls]})
            if turn >= script.n_content:
                completed += 1
    except ReplayError as exc:
        if upstream.unexpected:  # the fake upstream refused what the proxy sent, and recorded why
            raise ReplayError("the fake upstream refused the request the proxy sent: "
                              + upstream.unexpected[-1]) from exc
        raise
    finally:
        if proxy is not None:  # as the launcher ends a run
            proxy.stop()
            launcher.report_missing_tools(proxy)
            proxy.transform.flush()
        upstream.stop()
    store.log({"event": "exit", "code": 0})
    log = read_log(store.dir / "log.jsonl")
    entries = proxy_requests(log)
    problems = list(upstream.unexpected)
    if len(entries) != len(upstream.records):
        problems.append(f"the proxy logged {len(entries)} requests and the fake upstream received "
                        f"{len(upstream.records)}")
    if upstream.cursor != len(planned):
        problems.append(f"the session ended after {upstream.cursor} of {len(planned)} scripted rounds")
    history = lines.history_tokens(script)
    requests = []
    for record, entry in zip(upstream.records, entries):
        if entry.get("status") != 200:
            problems.append(f"request {record['n'] + 1} got status {entry.get('status')}")
        turn = record["turn"]
        requests.append({
            "arm": "contexto", "seed": script.seed, "repeat": 0, "model": None, "n": record["n"], "turn": turn,
            "main": True, "history_tokens": history[turn] if turn is not None else None,
            "uncached": record["tokens"], "cache_write": 0, "cache_read": 0, "output": record["output"],
            "status": record["status"], "compaction": bool(record["markers"]), "basis": "estimated",
            # Fields this builder added to the schema:
            "round": record["round"], "proxy_estimate": _estimate(entry), "epoch": entry.get("epoch"),
            "rotation": any(e.get("event") == "rotation" for e in _inner(entry)),
        })
    return Measured(
        session_id=session_id, turns=len(script.turns), head_tokens=head_tokens(client.fields), requests=requests,
        reach=rows, rotations=sum(1 for r in requests if r["rotation"]),
        over_bound=sum(1 for r in upstream.records if r["tokens"] > script.bound),
        over_bound_proxy=sum(1 for e in entries if _marked_over(e)),
        failure_events=failure_events(log), events=event_counts(log),
        compaction_markers=sum(len(r["markers"]) for r in upstream.records), pages=len(mcp_store.page_ids()),
        toolloop_reads=client.reads, searches=client.searches, probe_turns_completed=completed,
        seconds=round(time.monotonic() - started, 1), problems=problems,
        notices=[line for line in notices.getvalue().splitlines() if line.strip()],
    )


# Made for the replay's own store, random, and of no use outside it: the HMAC key of its page ids
# (``page.key``) and the salt of its upstream affinity key (``affinity.salt``). They are removed from a
# store kept with --out, which is meant to be published, so that no file in it looks like a secret.
PRIVATE_FILES = ("affinity.salt", "sessions/*/page.key")


def remove_private_files(home: Path) -> list[str]:
    """Remove ``PRIVATE_FILES`` from the store under ``home``; return their paths relative to it. The
    store stays readable: a store opened to read pages does not need its key."""
    removed = []
    for pattern in PRIVATE_FILES:
        for path in sorted(home.glob(pattern)):
            path.unlink(missing_ok=True)
            removed.append(path.relative_to(home).as_posix())
    return removed


def measure_reach(script: Script, store: Any, view: str) -> list[dict[str, Any]]:
    """``reach.jsonl`` rows for the store as it is now. ``store.search`` is the function the
    ``contexto_search`` MCP tool calls."""
    pages = {}
    for page_id in store.page_ids():
        page = store.get_page(page_id)
        if page is not None:
            pages[page_id] = reach.page_text(page)
    return reach.measure(script.probes, pages, store.search, view,
                         lambda probe: bench_script.plant_turns(script, probe), script.seed)


__all__ = ["FAILURE_EVENTS", "Measured", "ReplayError", "replay"]
