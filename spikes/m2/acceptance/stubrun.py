"""Zero-spend dry run of the acceptance session through the real launcher.

``contexto run claude`` runs the whole acceptance script against a local stub upstream that never
forwards. The stub answers every turn with ``OK`` (SSE when the request streams) and reports a
usage it computes from the request: pasted log text at 1.8 bytes per token (the rate M0 measured
for this content) and every other byte at ``OTHER_BYTES_PER_TOKEN`` [assumed, errs high against the
contract's 3.5]. That modelled usage stands in for real token counts, so the run measures the
launcher's real head, the engine's real rotations, and the gap between the engine's estimate and
the modelled count, without a model call.

The stub also checks the credential in memory, as ``credcheck`` does: each request's key header is
compared with the seat helper's output and only booleans are kept. Nothing records a header value,
a key, or request content; per-request records hold sizes and counts.
"""

from __future__ import annotations

import glob
import hmac
import json
import math
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acceptance import budget, credcheck, detectors, driver  # noqa: E402

OTHER_BYTES_PER_TOKEN = 3.0
LOG_BLOCK_MIN_BYTES = 5_000  # a user text block this large is a pasted log turn


def model_tokens(body: dict) -> dict[str, int]:
    """Modelled input tokens for a request body: log text at 1.8 bytes per token, the rest at 3.0."""
    log_bytes = 0
    stripped = json.loads(json.dumps(body))
    for msg in stripped.get("messages") or []:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and len(content.encode("utf-8")) >= LOG_BLOCK_MIN_BYTES:
            log_bytes += len(content.encode("utf-8"))
            msg["content"] = ""
        elif isinstance(content, list):
            for block in content:
                text = block.get("text") if isinstance(block, dict) and block.get("type") == "text" else None
                if isinstance(text, str) and len(text.encode("utf-8")) >= LOG_BLOCK_MIN_BYTES:
                    log_bytes += len(text.encode("utf-8"))
                    block["text"] = ""
    other_bytes = len(json.dumps(stripped, separators=(",", ":")).encode("utf-8"))
    tokens = math.ceil(log_bytes / budget.BYTES_PER_TOKEN_LOG) + math.ceil(other_bytes / OTHER_BYTES_PER_TOKEN)
    return {"tokens": tokens, "log_bytes": log_bytes, "other_bytes": other_bytes}


@dataclass
class Stub:
    key: str  # the seat helper's output, held in memory for comparison only
    records: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def handler(self) -> type[BaseHTTPRequestHandler]:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                path = self.path.split("?")[0]
                try:
                    body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    body = {}
                x_api_key = self.headers.get("x-api-key") or ""
                auth = self.headers.get("authorization") or ""
                model = model_tokens(body)
                with stub.lock:
                    index = len(stub.records)
                    stub.records.append({
                        "index": index, "path": path, "stream": bool(body.get("stream")), "bytes": len(raw),
                        "messages": len(body.get("messages") or []), "tools": len(body.get("tools") or []),
                        "system_bytes": len(json.dumps(body.get("system") or "").encode("utf-8")),
                        "tools_bytes": len(json.dumps(body.get("tools") or []).encode("utf-8")),
                        "max_tokens": body.get("max_tokens"), **model,
                        "key_matches_seat_helper": hmac.compare_digest(x_api_key, stub.key)
                        or hmac.compare_digest(auth, f"Bearer {stub.key}"),
                        "oauth_beta_header": "oauth" in (self.headers.get("anthropic-beta") or "").lower(),
                    })
                if path == "/v1/messages/count_tokens":
                    self._send_json({"input_tokens": model["tokens"]})
                elif path == "/v1/messages":
                    self._reply(index, model["tokens"], bool(body.get("stream")))
                else:
                    self._send_json({"type": "error", "error": {"type": "not_found_error", "message": "stub"}}, 404)

            def do_HEAD(self) -> None:  # noqa: N802
                self.send_response(200)
                self.end_headers()

            def _reply(self, index: int, tokens: int, stream: bool) -> None:
                cached = tokens // 2 if index else 0
                usage = {"input_tokens": tokens - cached, "cache_creation_input_tokens": 0,
                         "cache_read_input_tokens": cached}
                message = {"id": f"msg_stub_{index}", "type": "message", "role": "assistant", "model": driver.MODEL}
                if not stream:
                    self._send_json({**message, "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn",
                                     "stop_sequence": None, "usage": {**usage, "output_tokens": 2}})
                    return
                events = [
                    {"type": "message_start", "message": {**message, "content": [], "stop_reason": None,
                                                          "stop_sequence": None, "usage": {**usage, "output_tokens": 1}}},
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                     "usage": {"output_tokens": 2}},
                    {"type": "message_stop"},
                ]
                data = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_json(self, payload: dict, status: int = 200) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        return Handler


def _engine_request(record: dict) -> dict:
    for ev in record.get("transform_events") or []:
        if ev.get("event") == "request":
            return ev
    return {}


def summarize(stub_records: list[dict], session_log: list[dict], script_turns: int, p: budget.Params,
              probe_bytes: int, turn_bytes: list[int]) -> dict[str, Any]:
    """Budget and estimate figures from the stub's records and the proxy's session log."""
    messages = [r for r in stub_records if r["path"] == "/v1/messages"]
    head = messages[0]
    head_bytes = head["system_bytes"] + head["tools_bytes"]
    head_tokens = math.ceil(head_bytes / OTHER_BYTES_PER_TOKEN)
    requests = [ev for ev in session_log if ev.get("event") == "request" and ev.get("path") == "/v1/messages"]
    rows = []
    for i, ev in enumerate(requests):
        usage = ev.get("usage") or {}
        real = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        eng = _engine_request(ev)
        estimate = eng.get("sent_estimate", eng.get("estimate"))  # after a rotation, the request actually sent
        rows.append({"request": i + 1, "epoch": ev.get("epoch"), "real": real, "estimate": estimate,
                     "gap": None if estimate is None else real - int(estimate),
                     "rotation": any(t.get("event") == "rotation" for t in ev.get("transform_events") or [])})
    first_epoch = rows[0]["epoch"] if rows else None
    first_epoch_max = max((r["real"] for r in rows if r["epoch"] == first_epoch), default=0)
    gaps = [r for r in rows if r["gap"] is not None]
    worst_gap = max(gaps, key=lambda r: r["gap"], default=None)

    # H1 from the run's own inputs, plus the probe turn's recall round trips the stub cannot cause.
    inputs = [r["real"] for r in rows]
    probe_input = inputs[-1] if inputs else 0
    recall_inputs = [probe_input + k * p.recall_tokens for k in range(1, p.recalls + 1)]
    outputs = [p.reply_tokens] * (len(inputs) - 1) + [200] + [200] * (p.recalls - 1) + [p.probe_output]
    all_inputs = inputs + recall_inputs
    h1_expected = sum(all_inputs) + sum(outputs)
    h1_worst = sum(x + p.max_tokens_reserve for x in all_inputs)
    history = sum(budget.turn_tokens(x) + p.reply_tokens for x in turn_bytes)
    probe = math.ceil(probe_bytes / budget.BYTES_PER_TOKEN_TEXT)
    replay = head_tokens + history + probe
    sim = budget.simulate(turn_bytes, probe_bytes, budget.Params(**{**p.__dict__, "head": head_tokens}))
    return {
        "head": {"system_bytes": head["system_bytes"], "tools": head["tools"], "tools_bytes": head["tools_bytes"],
                 "tokens_at_3.0": head_tokens, "tokens_at_3.5": math.ceil(head_bytes / budget.BYTES_PER_TOKEN_TEXT),
                 "first_request_tokens": head["tokens"], "max_tokens": head["max_tokens"]},
        "requests": len(rows), "content_turns": script_turns,
        "rows": rows,
        "pre_usage": rows[0] if rows else None,
        "largest_gap": worst_gap,
        "first_epoch": {"epoch": first_epoch, "max_input": first_epoch_max, "margin_to_bound": p.bound - first_epoch_max},
        "max_input": max(inputs, default=0),
        "budget": {"h1_expected": h1_expected, "h1_worst": h1_worst, "baseline_replay": replay + p.probe_output,
                   "baseline_replay_worst": replay + p.max_tokens_reserve,
                   "total_expected": h1_expected + replay + p.probe_output,
                   "total_worst": h1_worst + replay + p.max_tokens_reserve,
                   "simulated_with_measured_head": {"h1_expected": sim.h1_expected, "h1_worst": sim.h1_worst,
                                                     "rotations": sim.rotations,
                                                     "planned_over_bound": sim.planned_over_bound}},
    }


def dry_run(seat_settings: Path, contexto: str, out: Path, p: budget.Params = budget.Params(),
            seed: int = 20261016, turn_bytes: int = 40_000, timeout: float = 1_800.0) -> dict[str, Any]:
    from acceptance.run import build_script

    if shutil.which("claude") is None:
        return {"ran": False, "reason": "claude CLI not installed"}
    try:
        key = credcheck._helper_output(seat_settings)
    except (RuntimeError, OSError):
        return {"ran": False, "reason": "the seat's apiKeyHelper did not produce a key; output not shown"}
    stub = Stub(key)
    del key
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), stub.handler())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    scratch = Path(tempfile.mkdtemp(prefix="contexto-dry-"))
    s = build_script(seed, 0, turn_bytes, p)
    session = f"dry{int(time.time())}"
    home = scratch / "contexto-home"
    session_log = home / "sessions" / session / "log.jsonl"
    result: dict[str, Any] = {"ran": True, "scratch": str(scratch), "session": session, "turn_bytes": turn_bytes}
    transport = None
    try:
        if driver.inside_git(scratch):
            return {"ran": False, "reason": "scratch directory is inside a git work tree"}
        config = driver.isolated_config(scratch, seat_settings)
        env = driver.child_env(home, config)
        env.pop("ANTHROPIC_BASE_URL", None)
        command = driver.launcher_command(session, p.bound, f"http://127.0.0.1:{httpd.server_port}", contexto)
        transport = driver.StreamJsonTransport(command, scratch, env, session_log)
        deadline = time.monotonic() + timeout
        for i, text in enumerate(s.turns):
            if time.monotonic() > deadline:
                raise TimeoutError(f"dry run passed {timeout:.0f}s at turn {i + 1}")
            event = transport.send(text)
            if event.get("is_error"):
                raise RuntimeError(f"turn {i + 1}: claude reported an error")
        result["stream_compactions"] = transport.compactions
        result["exit"] = transport.close()
        transport = None
    except (RuntimeError, TimeoutError, driver.AuthFailure, ValueError) as exc:
        result.update({"ran": False, "reason": str(exc)})
    finally:
        if transport is not None:
            transport.proc.kill()
        httpd.shutdown()
    log = detectors.read_jsonl(session_log)
    cc_jsonl = [rec for path in glob.glob(str(scratch / "claude-config" / "projects" / "*" / "*.jsonl"))
                for rec in detectors.read_jsonl(Path(path))]
    report = detectors.analyze(log, cc_jsonl, bound=p.bound, stream_compactions=result.get("stream_compactions", 0))
    paged = detectors.paged_statements(home / "sessions" / session,
                                       [st for probe in s.probes if probe.id in s.paged_only for st in probe.statements])
    messages = [r for r in stub.records if r["path"] == "/v1/messages"]
    result.update({
        "credential": {"requests_seen": len(stub.records),
                       "all_keys_match_seat_helper": bool(stub.records) and all(r["key_matches_seat_helper"] for r in stub.records),
                       "any_oauth_beta_header": any(r["oauth_beta_header"] for r in stub.records)},
        "stub_paths": sorted({r["path"] for r in stub.records}),
        "verdicts": report.verdicts(),
        "detectors": {"requests": report.requests, "rotations": report.rotations, "epochs": report.epochs,
                      "requests_per_epoch": report.requests_per_epoch, "over_bound_requests": report.over_bound_requests,
                      "unverified_requests": report.unverified_requests, "upstream_4xx": report.upstream_4xx,
                      "compaction_events": report.compaction_events, "engine_events": report.engine_events},
        "paged_only_in_pages": paged,
    })
    if messages and log:
        result.update(summarize(stub.records, log, len(s.turns) - 1, p, s.turn_bytes[-1], list(s.turn_bytes[:-1])))
    out.mkdir(parents=True, exist_ok=True)
    (out / "stub-requests.json").write_text(json.dumps(stub.records, indent=1))
    (out / "dry-run.json").write_text(json.dumps(result, indent=1, default=str))
    return result


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print("usage: stubrun.py <seat settings file> <contexto executable> <output dir> [turn kbytes, default 40]",
              file=sys.stderr)
        return 2
    turn_bytes = int(argv[3]) * 1_000 if len(argv) == 4 else 40_000
    result = dry_run(Path(argv[0]), argv[1], Path(argv[2]), turn_bytes=turn_bytes)
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=1, default=str), flush=True)
    return 0 if result.get("ran") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
