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

from acceptance import budget, credcheck, detectors, driver, reach  # noqa: E402

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
    # (n, factor): from the n-th Messages request on (0-based), report usage times factor, so real
    # usage outruns the engine's estimate and a real over_bound is logged (a detector proof run).
    inflate: tuple[int, float] | None = None
    auth_fail_at: int | None = None  # answer this Messages request (0-based) with 401: an auth-stop run
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
                    nth = sum(1 for r in stub.records if r["path"] == "/v1/messages")
                    if stub.inflate and path == "/v1/messages" and nth >= stub.inflate[0]:
                        model = {**model, "tokens": int(model["tokens"] * stub.inflate[1]), "inflated": True}
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
                elif path == "/v1/messages" and nth == stub.auth_fail_at:
                    self._send_json({"type": "error", "error": {"type": "authentication_error", "message": "stub"}}, 401)
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
              probe_bytes: int | list[int], turn_bytes: list[int]) -> dict[str, Any]:
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

    # H1 from the run's own inputs. D27: the last len(probes) requests are the probe turns (the stub
    # calls no tools, so one request per turn); each is priced with the recall round trips the stub
    # cannot cause and the probe turn's output, as budget.simulate prices them.
    probes = probe_bytes if isinstance(probe_bytes, list) else [probe_bytes]
    output_per_probe_turn = max(200, p.probe_output // len(probes))
    inputs = [r["real"] for r in rows]
    probe_inputs = inputs[-len(probes):] if inputs else []
    probe_input = probe_inputs[-1] if probe_inputs else 0
    # Each recall priced as the budget prices it (the largest B9 chunk); B9's size at the engine's last
    # logged window budget W is reported beside it.
    budgets = [int(te["window_budget"]) for ev in requests for te in ev.get("transform_events") or []
               if isinstance(te, dict) and isinstance(te.get("window_budget"), (int, float))]
    window = budgets[-1] if budgets else 0
    per_recall = budget.recall_tokens(p)
    recall_inputs = [probe_input + k * per_recall for k in range(1, p.recalls + 1)]
    all_inputs = inputs[:len(inputs) - len(probe_inputs)]
    outputs = [p.reply_tokens] * len(all_inputs)
    for x in probe_inputs:
        all_inputs += [x] + [x + k * per_recall for k in range(1, p.recalls + 1)]
        outputs += [200] * p.recalls + [output_per_probe_turn]
    h1_expected = sum(all_inputs) + sum(outputs)
    h1_worst = sum(x + p.max_tokens_reserve for x in all_inputs)
    # D27's symmetric baseline: one full-context request per probe turn, request k carrying probe
    # turns 1..k-1 with their answers (budget.simulate's baseline_requests, with the measured head).
    history = sum(budget.turn_tokens(x) + p.reply_tokens for x in turn_bytes)
    probe_tokens = [math.ceil(x / budget.BYTES_PER_TOKEN_TEXT) for x in probes]
    baseline_requests = [head_tokens + history + sum(probe_tokens[:k]) + (k - 1) * output_per_probe_turn
                         for k in range(1, len(probes) + 1)]
    replay = sum(baseline_requests) + len(probes) * output_per_probe_turn
    replay_worst = sum(x + p.max_tokens_reserve for x in baseline_requests)
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
        "probe_turn": {"start": probe_input, "window_budget": window, "recall_tokens": per_recall,
                       "b9_recall_tokens_at_window": budget.b9_recall_tokens(window) if window else None,
                       "with_recalls": recall_inputs[-1] if recall_inputs else probe_input,
                       "margin_to_bound": p.bound - (recall_inputs[-1] if recall_inputs else probe_input)},
        "max_input": max(inputs, default=0),
        "budget": {"h1_expected": h1_expected, "h1_worst": h1_worst, "baseline_requests": baseline_requests,
                   "baseline_replay": replay, "baseline_replay_worst": replay_worst,
                   "total_expected": h1_expected + replay, "total_worst": h1_worst + replay_worst,
                   "simulated_with_measured_head": {"h1_expected": sim.h1_expected, "h1_worst": sim.h1_worst,
                                                     "rotations": sim.rotations,
                                                     "planned_over_bound": sim.planned_over_bound}},
    }


def dry_run(seat_settings: Path, contexto: str, out: Path, p: budget.Params = budget.Params(),
            seed: int = 20261016, turn_bytes: int = 40_000, timeout: float = 1_800.0,
            inflate: tuple[int, float] | None = None, fault: tuple[str, int] | None = None) -> dict[str, Any]:
    """H1 through run.run_h1, live's own function, with the stub as its upstream and the live gate's
    approval. ``fault`` drives a stop branch: ("auth", n) answers the n-th Messages request (0-based)
    with 401, so the spend watcher's model-path auth stop fires; ("raise", n) raises a harness error
    after turn n (0-based), so the H1 record is written on the error path and the error re-raised."""
    from acceptance import run

    if shutil.which("claude") is None:
        return {"ran": False, "reason": "claude CLI not installed"}
    if "/" in contexto:  # the launcher starts in the scratch directory, so a relative path would not resolve
        contexto = str(Path(contexto).resolve())
    try:
        key = credcheck._helper_output(seat_settings)
    except (RuntimeError, OSError):
        return {"ran": False, "reason": "the seat's apiKeyHelper did not produce a key; output not shown"}
    stub = Stub(key, inflate=inflate, auth_fail_at=fault[1] if fault and fault[0] == "auth" else None)
    del key
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), stub.handler())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    scratch = Path(tempfile.mkdtemp(prefix="contexto-dry-"))
    s, b = run.plan(seed, 0, turn_bytes, p)
    session = f"dry{int(time.time())}"
    home = scratch / "contexto-home"
    session_log = home / "sessions" / session / "log.jsonl"
    result: dict[str, Any] = {"ran": True, "scratch": str(scratch), "session": session, "turn_bytes": turn_bytes,
                              "fault": list(fault) if fault else None}
    deadline = time.monotonic() + timeout

    def inject(index: int) -> None:
        if time.monotonic() > deadline:
            raise TimeoutError(f"dry run passed {timeout:.0f}s at turn {index + 1}")
        if fault and fault[0] == "raise" and index == fault[1]:
            raise ValueError(f"dry run: injected harness error after turn {index + 1}")

    try:
        status = run.run_h1(s, b, p, b.h1_worst + b.baseline_replay_worst, scratch, home, session, seat_settings,
                            f"http://127.0.0.1:{httpd.server_port}", contexto, inject=inject)
        if status != 0:
            result.update({"ran": False, "reason": f"run_h1 returned {status}"})
    except Exception as exc:  # noqa: BLE001  run_h1 re-raises a harness error after writing the H1 record
        result.update({"ran": False, "reason": f"{type(exc).__name__}: {exc}"})
    finally:
        httpd.shutdown()
    record_path = home / f"acceptance-{session}-h1.json"
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {}
    result["h1_record"] = {k: v for k, v in record.items() if k != "turns"}
    result["stream_compactions"] = record.get("stream_compactions", 0)
    result["exit"] = record.get("cli_exit")
    log = detectors.read_jsonl(session_log) if session_log.exists() else []  # a launch failure logs nothing
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
                      "compaction_events": report.compaction_events, "engine_events": report.engine_events,
                      **{name: getattr(report, name) for name in (
                          "over_bound_real", "over_bound_real_max_tokens", "estimate_errors", "failure_events",
                          "pre_send_tool_loop", "pre_send_other", "window_minimal", "bound_too_small",
                          "non_model_4xx", "model_request_4xx") if hasattr(report, name)}},
        "paged_only_in_pages": paged,
        "inflate": list(inflate) if inflate else None,
    })
    pages = reach.load_pages(home / "sessions" / session)
    if pages:
        r = reach.reach(list(s.probes), pages, turn_texts=list(s.content_turns))
        result["reach"] = {k: v for k, v in r.items() if k != "rows"}
    if messages and log:
        result.update(summarize(stub.records, log, s.n_content, p, list(s.probe_bytes), list(s.content_bytes)))
    out.mkdir(parents=True, exist_ok=True)
    (out / "stub-requests.json").write_text(json.dumps(stub.records, indent=1))
    (out / "dry-run.json").write_text(json.dumps(result, indent=1, default=str))
    return result


def unexercised(results: list[dict[str, Any]]) -> list[str]:
    """The live-only paths (run.LIVE_PATHS) that none of these dry runs took, from each H1 record's
    "paths". Intake's requirement 4: a dry run is clean only when this is empty."""
    from acceptance import run

    taken = {name for r in results for name in (r.get("h1_record") or {}).get("paths") or []}
    return [name for name in run.LIVE_PATHS if name not in taken]


EXPECTED_STOP = {"auth": "stop_handled", "raise": "error_handled"}


def main(argv: list[str]) -> int:
    if argv[:1] == ["check-paths"]:  # check-paths <dry run output dir>...
        results = [json.loads((Path(d) / "dry-run.json").read_text(encoding="utf-8")) for d in argv[1:]]
        missing = unexercised(results)
        print(json.dumps({"runs": len(results), "unexercised": missing}))
        return 1 if missing or not results else 0
    fault = None
    if "--fault" in argv:  # --fault auth:N or raise:N, a stop-path run
        i = argv.index("--fault")
        kind, n = argv[i + 1].split(":")
        if kind not in EXPECTED_STOP:
            print("--fault takes auth:N or raise:N", file=sys.stderr)
            return 2
        fault = (kind, int(n))
        argv = argv[:i] + argv[i + 2:]
    inflate = None
    if "--inflate" in argv:  # --inflate N:FACTOR, a detector proof run
        i = argv.index("--inflate")
        n, factor = argv[i + 1].split(":")
        inflate = (int(n), float(factor))
        argv = argv[:i] + argv[i + 2:]
    if len(argv) not in (3, 4):
        print("usage: stubrun.py <seat settings file> <contexto executable> <output dir> [turn kbytes, default 40]"
              " [--inflate N:FACTOR] [--fault auth:N|raise:N]\n       stubrun.py check-paths <output dir>...",
              file=sys.stderr)
        return 2
    turn_bytes = int(argv[3]) * 1_000 if len(argv) == 4 else 40_000
    result = dry_run(Path(argv[0]), argv[1], Path(argv[2]), turn_bytes=turn_bytes, inflate=inflate, fault=fault)
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=1, default=str), flush=True)
    if fault:  # a stop-path run passes when it stopped on the branch it drove and wrote the record
        record = result.get("h1_record") or {}
        return 0 if record.get("stopped") and EXPECTED_STOP[fault[0]] in (record.get("paths") or []) else 1
    return 0 if result.get("ran") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
