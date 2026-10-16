"""Targeted class-A experiment (intake 2026-10-16 08:33Z, row qitem-m2-classA-experiment).

Live run #2 answered four deep-chain probes with a superseded link. This replays the request the model
saw at that run's first probe turn, with the probe turn asking only those four probes, and lets the
model use Contexto's tools as main's code answers them now (B19's search format and A14's retained-
window snapshot). Nothing here is part of the acceptance verdicts.

How (all local except the model requests, which run only on intake's GO):
1. Rebuild. Claude Code's session JSONL holds every original request (``replay.recorded_messages``);
   the proxy logged sizes and events only. The requests up to the first probe turn are replayed in
   order through the engine at the run's own code (``--run-code``, a ``git archive`` of the run's
   commit) on a private scratch store with the run's page key, feeding back each request's logged
   usage. The rebuild must reproduce every logged engine decision (event names; epoch, k, message
   counts; rotation reason, turns paged, retained messages) and only the run's own page ids, or the
   experiment refuses. Body bytes are reported, not required equal (see ``body_fields``).
2. Experiment. The same requests go through main's engine on a fresh scratch store, with main's
   Contexto tool definitions and the targets-only probe turn; main's engine writes the retained
   snapshot itself (A14) where main has it.
3. Loop. Each request is checked with ``count_tokens`` against what is left of the approval
   (spent + count + max_tokens), sent once, and its tool calls are answered in-process by main's MCP
   server over the scratch store. Continuations go through the engine as the proxy would. The loop
   stops at an answer with no tool call or at the request cap.

Credentials follow replay.py: the seat's apiKeyHelper output, in memory, in the request headers only;
the honest user agent; no claude-code-* beta. Content (requests, answers, tool output) stays in the
private output directory; the result records ids, counts, sizes, and booleans.
"""

from __future__ import annotations

import argparse
import copy
import importlib
import io
import json
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acceptance import detectors, driver, replay, run, script  # noqa: E402
from fidelity.l2f import normalize, parse_answers  # noqa: E402  (script.py puts spikes/m0 on the path)

TARGETS = ("q_a_016", "q_a_012", "q_a_035", "q_a_024")  # builder-3's class-A H1-only misses, live run #2
APPROVAL = 600_000
MAX_TOKENS = 16_000
LOOP_CAP = 3
MCP_PREFIX = "mcp__contexto__"
DECISION_KEYS = ("event", "epoch", "k", "messages", "upstream_messages", "reason", "turns_paged",
                 "retained_messages", "count", "first_turn")


class ExperimentError(Exception):
    pass


# Rebuild


def _ser_len(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "backslashreplace"))


def original_bodies(cc: list[dict[str, Any]], probe_text: str) -> list[dict[str, Any]]:
    """Each original Claude Code request up to and including the first one carrying ``probe_text``,
    as sent: shape system and tools, the record's params and cache markers, system-role messages with
    string content unless cache-marked (the wire form; the JSONL keeps a one-block list)."""
    decoded = replay.recorded_messages(cc, stop=lambda msgs: any(
        m.get("role") == "user" and replay._has_text(m, probe_text) for m in msgs))
    shapes = {r.get("shapeHash"): r["shape"] for r in cc if r.get("type") == "api-request-shape"}
    records = [r for r in cc if r.get("type") == "api-request" and isinstance(r.get("params"), dict)]
    out = []
    for r in records[:len(decoded)]:
        shape, p = shapes.get(r.get("shapeHash")), r["params"]
        if shape is None:
            raise ExperimentError("a request's shape is not in the session JSONL")
        # One copy per message: identical messages (the per-turn reminder) decode to one shared object,
        # and a single deepcopy would keep them aliased, so marking one would mark them all.
        body: dict[str, Any] = {"model": p["model"], "messages": [copy.deepcopy(m) for m in decoded[r["id"]]],
                                "system": copy.deepcopy(shape["system"]), "tools": copy.deepcopy(shape["tools"]),
                                "stream": True}
        for key in ("max_tokens", "thinking", "context_management", "output_config", "safeguards"):
            if key in p:
                body[key] = copy.deepcopy(p[key])
        for mark in r.get("cacheControl") or []:  # markers first: a marked system message keeps its block form
            target: Any = body
            for part in mark["path"]:
                target = target[part]
            if isinstance(target, dict):
                target["cache_control"] = mark["value"]
        for m in body["messages"]:
            c = m.get("content")
            if m.get("role") == "system" and isinstance(c, list) and len(c) == 1 and set(c[0]) == {"type", "text"}:
                m["content"] = c[0]["text"]
        body["_betas"] = list(p.get("betas") or [])
        out.append(body)
    if not out or not any(m.get("role") == "user" and replay._has_text(m, probe_text) for m in out[-1]["messages"]):
        raise ExperimentError("the probe turn is not in the session JSONL")
    return out


def _decisions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: e.get(k) for k in DECISION_KEYS if k in e} for e in events if isinstance(e, dict)]


def _engine(code: Path | None) -> tuple[Any, Any, Any]:
    """RewriteEngine, Config and SessionStore from ``code`` (a source tree) or from the installed package."""
    if code is None:
        from contexto.rewrite import Config, RewriteEngine
        from contexto.store import SessionStore
        return RewriteEngine, Config, SessionStore
    saved = {k: v for k, v in sys.modules.items() if k == "contexto" or k.startswith("contexto.")}
    for k in saved:
        del sys.modules[k]
    sys.path.insert(0, str(code))
    try:
        rewrite = importlib.import_module("contexto.rewrite")
        store = importlib.import_module("contexto.store")
        if not Path(rewrite.__file__).resolve().is_relative_to(code.resolve()):
            raise ExperimentError("the run's engine code did not load")
        return rewrite.RewriteEngine, rewrite.Config, store.SessionStore
    finally:
        sys.path.remove(str(code))
        for k in [k for k in sys.modules if k == "contexto" or k.startswith("contexto.")]:
            del sys.modules[k]
        sys.modules.update(saved)


def _scratch_store(store_cls: Any, session_dir: Path, session: str, under: Path) -> tuple[Any, Path]:
    home = Path(tempfile.mkdtemp(prefix="classa-", dir=under))
    (home / "sessions" / session).mkdir(parents=True)
    shutil.copy(session_dir / "page.key", home / "sessions" / session / "page.key")  # same keyed page ids
    return store_cls(home, session), home


def replay_through_engine(bodies: list[dict[str, Any]], log: list[dict[str, Any]], engine: Any,
                          last: int | None = None) -> list[dict[str, Any]]:
    """Send ``bodies[:last]`` through ``engine`` in order, feeding back each logged usage; per request,
    the engine's decision events and the logged ones."""
    logged = [e for e in log if e.get("event") == "request" and detectors.request_path(e) == "/v1/messages"]
    rows = []
    for i, body in enumerate(bodies[:last]):
        req = {k: v for k, v in body.items() if not k.startswith("_")}
        original = json.loads(json.dumps(req))
        upstream, _, events = engine(req, {"anthropic-beta": ",".join(body["_betas"])}, "/v1/messages")
        rows.append({"i": i + 1, "ours": _decisions(events), "logged": _decisions(logged[i].get("transform_events") or []),
                     "bytes_delta": _ser_len(upstream) - int(logged[i].get("request_bytes") or 0)})
        if logged[i].get("usage"):
            engine.observe_usage("/v1/messages", logged[i]["usage"], original)
    return rows


def rebuild_check(bodies: list[dict[str, Any]], log: list[dict[str, Any]], session_dir: Path, session: str,
                  run_code: Path, under: Path) -> dict[str, Any]:
    """The run's own engine reproduces every logged decision and only the run's page ids, or refuse."""
    engine_cls, config_cls, store_cls = _engine(run_code)
    store, home = _scratch_store(store_cls, session_dir, session, under)
    launch = next(e for e in log if e.get("event") == "launch")
    rows = replay_through_engine(bodies, log, engine_cls(store, config=config_cls(**launch["config"])))
    rebuilt = {p.stem for p in (home / "sessions" / session / "pages").glob("*.json")}
    run_pages = {p.stem for p in (session_dir / "pages").glob("*.json")}
    out = {"requests": len(rows), "decisions_equal": all(r["ours"] == r["logged"] for r in rows),
           "unequal": [r["i"] for r in rows if r["ours"] != r["logged"]],
           "rotations": [r["i"] for r in rows if any(e.get("event") == "rotation" for e in r["logged"])],
           "pages_rebuilt": len(rebuilt), "pages_are_the_runs": rebuilt <= run_pages,
           "bytes_delta": [r["bytes_delta"] for r in rows]}
    shutil.rmtree(home, ignore_errors=True)
    out["ok"] = out["decisions_equal"] and out["pages_are_the_runs"] and out["requests"] == len(bodies)
    return out


# Experiment request


def main_tools() -> list[dict[str, Any]]:
    """Main's Contexto tool definitions in Claude Code's request form."""
    from contexto.mcp import TOOLS
    return [{"name": MCP_PREFIX + t["name"], "description": t["description"], "input_schema": t["inputSchema"]}
            for t in TOOLS]


def targets_text(s: script.Script, targets: tuple[str, ...] = TARGETS) -> str:
    """The probe turn with only the targets: the same header, the same question lines."""
    by_id = {p.id: p for p in s.probes}
    return "\n".join([*script.PROBE_HEADER, "", *[f"{i}: {by_id[i].question}" for i in targets]])


def experiment_bodies(bodies: list[dict[str, Any]], probe_text: str, new_text: str,
                      tools: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Main's tool definitions in every request and the targets-only probe turn in the last; the changes,
    as sizes."""
    out = copy.deepcopy(bodies)
    by_name = {t["name"]: t for t in tools}
    changed = {}
    for body in out:
        for j, t in enumerate(body["tools"]):
            if t.get("name") in by_name and t != by_name[t["name"]]:
                changed[t["name"]] = [_ser_len(t), _ser_len(by_name[t["name"]])]
                body["tools"][j] = copy.deepcopy(by_name[t["name"]])
    probe_msgs = [m for m in out[-1]["messages"] if m.get("role") == "user" and replay._has_text(m, probe_text)]
    if len(probe_msgs) != 1:
        raise ExperimentError("the probe turn message is not unique")
    m = probe_msgs[0]
    if isinstance(m["content"], str):
        m["content"] = new_text
    else:
        for b in m["content"]:
            if isinstance(b, dict) and b.get("text") == probe_text:
                b["text"] = new_text
    return out, {"tools_changed": changed, "probe_text_bytes": [len(probe_text.encode()), len(new_text.encode())]}


# Model calls


def parse_message(lines: Any) -> dict[str, Any]:
    """The full assistant message from a Messages event stream: every content block as sent (thinking
    with its signature, text, tool_use with id and input), usage, and stop reason."""
    blocks: dict[int, dict[str, Any]] = {}
    partial: dict[int, str] = {}
    usage: dict[str, int] = {}
    stop_reason = None
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        if not line.startswith("data: "):
            continue
        try:
            event = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "message_start":
            usage.update({k: v for k, v in (event.get("message", {}).get("usage") or {}).items() if isinstance(v, int)})
        elif kind == "content_block_start":
            blocks[int(event.get("index") or 0)] = copy.deepcopy(event.get("content_block") or {})
        elif kind == "content_block_delta":
            i, delta = int(event.get("index") or 0), event.get("delta") or {}
            block = blocks.setdefault(i, {})
            if delta.get("type") == "text_delta":
                block["text"] = block.get("text", "") + delta.get("text", "")
            elif delta.get("type") == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
            elif delta.get("type") == "signature_delta":
                block["signature"] = block.get("signature", "") + delta.get("signature", "")
            elif delta.get("type") == "input_json_delta":
                partial[i] = partial.get(i, "") + delta.get("partial_json", "")
        elif kind == "message_delta":
            usage.update({k: v for k, v in (event.get("usage") or {}).items() if isinstance(v, int)})
            stop_reason = (event.get("delta") or {}).get("stop_reason", stop_reason)
        elif kind == "error":
            raise replay.ReplayError(f"stream error: {(event.get('error') or {}).get('type')}")
    for i, text in partial.items():
        try:
            blocks[i]["input"] = json.loads(text or "{}")
        except json.JSONDecodeError:
            blocks[i]["input"] = {}
    return {"content": [blocks[i] for i in sorted(blocks)], "usage": usage, "stop_reason": stop_reason}


def send_message(request: dict[str, Any], upstream: str, key: str, timeout: float = 900.0) -> dict[str, Any]:
    """POST once with replay's headers (honest user agent, both key headers, no claude-code-* beta)."""
    body = json.dumps({k: v for k, v in request.items() if not k.startswith("_")}).encode("utf-8")
    headers = {"content-type": "application/json", "anthropic-version": replay.ANTHROPIC_VERSION,
               **replay.auth_headers(key), "user-agent": replay.USER_AGENT, "accept": "text/event-stream"}
    headers.update(replay.beta_headers(request))
    req = urllib.request.Request(upstream.rstrip("/") + "/v1/messages", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {**parse_message(resp), "status": resp.status}
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise driver.AuthFailure(f"upstream returned {exc.code}; stopped at the first auth error") from None
        raise replay.ReplayError(f"upstream returned {exc.code}") from None


def _usage_tokens(usage: dict[str, Any]) -> int:
    return sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                "cache_read_input_tokens", "output_tokens"))


def answer_tools(server: Any, content: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """tool_result blocks for every tool_use: Contexto's from main's MCP server in-process; any other
    tool gets an error result (this replay runs no other tools). Also the calls, as counts."""
    results, calls = [], []
    for block in content:
        if block.get("type") != "tool_use":
            continue
        name = str(block.get("name") or "")
        if name.startswith(MCP_PREFIX):
            r = server._tools_call({"name": name.removeprefix(MCP_PREFIX), "arguments": block.get("input") or {}})
            results.append({"type": "tool_result", "tool_use_id": block["id"], "content": r["content"],
                            **({"is_error": True} if r.get("isError") else {})})
            calls.append({"tool": name.removeprefix(MCP_PREFIX), "ok": not r.get("isError")})
        else:
            results.append({"type": "tool_result", "tool_use_id": block["id"], "is_error": True,
                            "content": [{"type": "text", "text": "This tool is not available in this replay."}]})
            calls.append({"tool": name, "ok": False, "unavailable": True})
    return results, calls


def run_sample(bodies: list[dict[str, Any]], log: list[dict[str, Any]], session_dir: Path, session: str,
               upstream: str, key: str, budget: dict[str, int], under: Path, count: Callable[..., int],
               send: Callable[..., dict[str, Any]], require_snapshot: bool = True) -> dict[str, Any]:
    """One sample: main's engine over a fresh scratch store through the first probe turn, then the
    targets-only turn's tool loop, gated per request against ``budget`` (approved, spent)."""
    from contexto.mcp import Server
    from contexto.rewrite import Config, RewriteEngine
    from contexto.store import SessionStore

    store, home = _scratch_store(SessionStore, session_dir, session, under)
    launch = next(e for e in log if e.get("event") == "launch")
    engine = RewriteEngine(store, config=Config(**launch["config"]))
    replay_through_engine(bodies, log, engine, last=len(bodies) - 1)  # state up to the probe turn
    server = Server(SessionStore(home, session), io.StringIO())
    body = copy.deepcopy(bodies[-1])
    betas = body.pop("_betas")
    requests, calls, answer, stopped = [], [], "", None
    snapshot: dict[str, Any] = {}
    for n in range(LOOP_CAP):
        original = json.loads(json.dumps(body))
        upstream_req, _, events = engine(copy.deepcopy(body), {"anthropic-beta": ",".join(betas)}, "/v1/messages")
        if n == 0:
            # A14: the engine itself writes the retained snapshot for this request; the live run
            # refuses before any model call unless it did (intake's precondition).
            current = store.retained_current() if hasattr(store, "retained_current") else None
            epoch = next((e.get("epoch") for e in events if "epoch" in e), None)
            snapshot = {"written": current is not None, "epoch": current[0] if current else None,
                        "k": current[1] if current else None, "turns": len(current[3]) if current else 0,
                        "request_epoch": epoch}
            if require_snapshot and not current:
                raise ExperimentError("the engine wrote no retained snapshot (A14 is not on this main)")
        upstream_req = {**upstream_req, "max_tokens": MAX_TOKENS}
        thinking = upstream_req.get("thinking")
        if isinstance(thinking, dict) and thinking.get("display") not in (None, "summarized", "omitted"):
            upstream_req["thinking"] = {**thinking, "display": "omitted"}  # count_tokens rejects 'updates'
        upstream_req["_betas"] = replay.harness_betas(betas)
        tokens = count(upstream_req, upstream, key)
        left = budget["approved"] - budget["spent"]
        if tokens + MAX_TOKENS > left:
            stopped = f"request {n + 1}: {tokens:,} + {MAX_TOKENS:,} would pass the {left:,} left"
            break
        result = send(upstream_req, upstream, key)
        spent = _usage_tokens(result["usage"])
        budget["spent"] += spent
        engine.observe_usage("/v1/messages", result["usage"], original)
        tool_results, turn_calls = answer_tools(server, result["content"])
        calls += turn_calls
        requests.append({"n": n + 1, "count_tokens": tokens, "spent": spent, "stop_reason": result["stop_reason"],
                         "tool_calls": len(turn_calls), "epoch": next((e.get("epoch") for e in events if "epoch" in e), None)})
        if not tool_results:
            answer = "".join(b.get("text", "") for b in result["content"] if b.get("type") == "text")
            break
        body["messages"] = body["messages"] + [{"role": "assistant", "content": result["content"]},
                                               {"role": "user", "content": tool_results}]
    else:
        stopped = f"loop cap {LOOP_CAP} reached with tool calls pending"
    mcp_log = detectors.read_jsonl(home / "sessions" / session / "log.jsonl") \
        if (home / "sessions" / session / "log.jsonl").exists() else []
    return {"requests": requests, "calls": calls, "answer": answer, "stopped": stopped, "home": str(home),
            "snapshot": snapshot,
            "mcp_events": [e for e in mcp_log if e.get("event") == "mcp_call"]}


def score_targets(s: script.Script, answer: str, targets: tuple[str, ...] = TARGETS) -> dict[str, Any]:
    """Per target: correct, or which link of its chain was given (newest = the last statement)."""
    given = parse_answers(answer)
    rows = {}
    for p in s.probes:
        if p.id not in targets:
            continue
        g = given.get(p.id)
        older = [normalize(v) for v in p.older]
        rows[p.id] = {"answered": g is not None, "correct": g is not None and normalize(g) == normalize(p.answer),
                      "older_link": g is not None and normalize(g) in older}
    return {"targets": rows, "correct": sum(r["correct"] for r in rows.values())}


# Stub upstream for the zero-spend dry run


class Stub:
    """A local upstream that never forwards: count_tokens answers a size-based count; Messages answers
    a contexto_search call first, then text naming every target."""

    def __init__(self, key: str, fail_count_at: int | None = None) -> None:
        self.key, self.fail_count_at, self.records, self.lock = key, fail_count_at, [], threading.Lock()

    def handler(self) -> type[BaseHTTPRequestHandler]:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                body = json.loads(raw or b"{}")
                path = self.path.split("?")[0]
                with stub.lock:
                    stub.records.append({"path": path, "bytes": len(raw), "ua": self.headers.get("user-agent"),
                                         "key_ok": self.headers.get("x-api-key") == stub.key,
                                         "betas": self.headers.get("anthropic-beta") or "",
                                         "max_tokens": body.get("max_tokens"),
                                         "thinking_display": (body.get("thinking") or {}).get("display"),
                                         "tool_results": sum(1 for m in body.get("messages") or [] if isinstance(m.get("content"), list)
                                                             for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result")})
                if path == "/v1/messages/count_tokens":
                    n = len(raw) // 3
                    self._json({"input_tokens": n})
                elif path == "/v1/messages":
                    last = (body.get("messages") or [{}])[-1]
                    answered = isinstance(last.get("content"), list) and any(
                        isinstance(b, dict) and b.get("type") == "tool_result" for b in last["content"])
                    self._sse(len(raw) // 3, answered)
                else:
                    self._json({"type": "error", "error": {"type": "not_found_error"}}, 404)

            def _json(self, payload: dict[str, Any], status: int = 200) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _sse(self, tokens: int, answered: bool) -> None:
                if answered:
                    blocks = [{"type": "text", "text": "\n".join(f"{t}: stub" for t in TARGETS)}]
                    stop = "end_turn"
                else:
                    blocks = [{"type": "thinking", "thinking": "", "signature": "stub-signature"},
                              {"type": "tool_use", "id": "toolu_stub_1", "name": MCP_PREFIX + "contexto_search",
                               "input": {"query": "stub query"}}]
                    stop = "tool_use"
                events = [{"type": "message_start", "message": {"id": "msg_stub", "type": "message", "role": "assistant",
                                                                "content": [], "usage": {"input_tokens": tokens, "output_tokens": 1}}}]
                for i, b in enumerate(blocks):
                    start = {k: v for k, v in b.items() if k not in ("text", "input", "signature")}
                    start.update({"text": ""} if b["type"] == "text" else {"input": {}} if b["type"] == "tool_use" else {"signature": ""})
                    events.append({"type": "content_block_start", "index": i, "content_block": start})
                    if b["type"] == "text":
                        events.append({"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": b["text"]}})
                    elif b["type"] == "tool_use":
                        events.append({"type": "content_block_delta", "index": i,
                                       "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}})
                    else:
                        events.append({"type": "content_block_delta", "index": i,
                                       "delta": {"type": "signature_delta", "signature": b["signature"]}})
                    events.append({"type": "content_block_stop", "index": i})
                events += [{"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 50}},
                           {"type": "message_stop"}]
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                for e in events:
                    self.wfile.write(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode())

        return Handler


def body_fields(bodies: list[dict[str, Any]], exp: list[dict[str, Any]]) -> dict[str, Any]:
    """Every request field of the experiment's probe request, its size, and whether it is the run's."""
    a = {k: v for k, v in bodies[-1].items() if not k.startswith("_")}
    b = {k: v for k, v in exp[-1].items() if not k.startswith("_")}
    return {k: {"bytes_run": _ser_len(a.get(k)), "bytes_experiment": _ser_len(b.get(k)), "same": a.get(k) == b.get(k)}
            for k in sorted(set(a) | set(b))}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="class-A experiment (zero spend with --stub)")
    ap.add_argument("--run-dir", type=Path, required=True)
    ap.add_argument("--session", required=True)
    ap.add_argument("--run-code", type=Path, required=True, help="source tree of the run's commit (git archive)")
    ap.add_argument("--out", type=Path, required=True, help="private output directory")
    ap.add_argument("--seat-settings", type=Path, required=True)
    ap.add_argument("--stub", action="store_true", help="answer every model call with a local stub (zero spend)")
    ap.add_argument("--upstream", default=None)
    ap.add_argument("--approved", type=int, default=APPROVAL)
    ap.add_argument("--samples", type=int, default=1)
    args = ap.parse_args(argv)
    if args.approved > APPROVAL:
        print(f"refusing: the approval is at most {APPROVAL:,}", file=sys.stderr)
        return 2
    if not args.stub and not args.upstream:
        print("refusing: pass --upstream for a live run, or --stub", file=sys.stderr)
        return 2
    from acceptance import credcheck
    from contexto.store import SessionStore

    # Preconditions (intake 08:33Z): B19 (b) and A14 on main. run_sample also checks, before any model
    # call, that the engine itself wrote the retained snapshot for the probe request (live only).
    if not args.stub and not hasattr(SessionStore, "set_retained"):
        print("refusing: main has no retained-window snapshot (set_retained); B19 (b) and A14 come first", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    session_dir = args.run_dir / "home" / "sessions" / args.session
    log = detectors.read_jsonl(session_dir / "log.jsonl")
    cc = detectors.read_jsonl(next((args.run_dir / "claude-config" / "projects").glob("*/*.jsonl")))
    s, _ = run.plan()
    probe_text = s.probe_texts[0]
    bodies = original_bodies(cc, probe_text)
    result: dict[str, Any] = {"targets": list(TARGETS), "approved": args.approved, "stub": args.stub,
                              "store_has_set_retained": hasattr(SessionStore, "set_retained")}
    result["rebuild"] = rebuild_check(bodies, log, session_dir, args.session, args.run_code, args.out)
    if not result["rebuild"]["ok"]:
        (args.out / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print("refusing: the rebuild does not reproduce the run's engine decisions", file=sys.stderr)
        return 1
    exp, changes = experiment_bodies(bodies, probe_text, targets_text(s), main_tools())
    result["changes"] = {**changes, "fields": body_fields(bodies, exp),
                         "sent_as": {"max_tokens": MAX_TOKENS, "thinking_display": "omitted",
                                     "betas_dropped": [b for b in bodies[-1]["_betas"] if b not in replay.harness_betas(bodies[-1]["_betas"])]}}
    key = credcheck._helper_output(args.seat_settings)
    httpd = None
    upstream = args.upstream
    if args.stub:
        stub = Stub(key)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), stub.handler())
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        upstream = f"http://127.0.0.1:{httpd.server_port}"
    budget = {"approved": args.approved, "spent": 0}
    samples = []
    try:
        for _ in range(args.samples):
            sample = run_sample(exp, log, session_dir, args.session, upstream, key, budget, args.out,
                                replay.count_tokens, send_message, require_snapshot=not args.stub)
            answer = sample.pop("answer")
            (Path(sample["home"]) / "answer.txt").write_text(answer + "\n", encoding="utf-8")  # private
            sample["score"] = score_targets(s, answer)
            samples.append(sample)
            if sample["stopped"]:
                break
    except (driver.AuthFailure, replay.ReplayError, ExperimentError, OSError) as exc:
        result["stopped"] = f"{type(exc).__name__}: {exc}"
    finally:
        del key
        if httpd is not None:
            httpd.shutdown()
    result["samples"] = samples
    result["spent"] = budget["spent"]
    if args.stub:
        result["stub_records"] = stub.records
    (args.out / "result.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "stub_records"}, indent=1, default=str)[:4000])
    return 0 if samples and not result.get("stopped") and not any(x["stopped"] for x in samples) else 1


if __name__ == "__main__":
    sys.exit(main())
