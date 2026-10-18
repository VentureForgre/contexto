#!/usr/bin/env python3
"""Loopback fake Anthropic Messages API for the Claude Code scale spike. Zero model spend.

Answers POST /v1/messages (streamed SSE or JSON) and POST /v1/messages/count_tokens, and logs
one JSON line per request: path, body bytes, message count, receive times, model, max_tokens,
tool count, and structural flags. It never logs header values (so no auth material) and never
logs message text, only a few keyword flags used to spot compaction requests.

Usage modes (--mode):
  contexto  report input usage as a constant (--const, default 100,000), the way Contexto's
            bounded upstream request would report it, regardless of the body size.
  honest    report input usage as body_bytes / --bpt (default 1.83 bytes per token).

--tool-loop N: answer with a tool_use of the Read tool on --read-path until the trailing user
turn chain holds N tool_results, then answer with text. 0 (default) always answers text.
--delay S: wait S seconds before answering each /v1/messages request (keeps the socket open
so lsof sampling observes it).
--window W: in honest mode, answer 400 "prompt is too long" when the honest count exceeds W
(0, default, never refuses), as the real API would.

Usage: fake_upstream.py <port> <log.jsonl> [--mode contexto|honest] [--const N] [--bpt F]
                        [--tool-loop N] [--read-path P] [--window W]
"""
import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Phrases from Claude Code's compaction prompt (2 hits each in the 2.1.302 binary for the first).
COMPACT_MARKERS = (
    "create a detailed summary of the conversation",
    "summary of the conversation so far",
    "Your task is to create a detailed summary",
)

ap = argparse.ArgumentParser()
ap.add_argument("port", type=int)
ap.add_argument("log")
ap.add_argument("--mode", choices=("contexto", "honest"), default="contexto")
ap.add_argument("--const", type=int, default=100_000)
ap.add_argument("--bpt", type=float, default=1.83)
ap.add_argument("--tool-loop", type=int, default=0)
ap.add_argument("--read-path", default="/tmp/scale-cli-read.txt")
ap.add_argument("--window", type=int, default=0)
ap.add_argument("--delay", type=float, default=0.0)
A = ap.parse_args()

LOCK = threading.Lock()
COUNTER = {"n": 0}


def log(rec: dict) -> None:
    with LOCK:
        with open(A.log, "a") as f:
            f.write(json.dumps(rec) + "\n")


def blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in (content or []) if isinstance(b, dict)]


def trailing_tool_results(messages) -> int:
    """tool_results in the turn after the newest user text (the current tool loop)."""
    n = 0
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        bs = blocks(m.get("content"))
        n += sum(1 for b in bs if b.get("type") == "tool_result")
        if any(b.get("type") == "text" and not b.get("text", "").startswith("<system-reminder>")
               for b in bs) and not any(b.get("type") == "tool_result" for b in bs):
            break
    return n


def last_user_text(messages) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return " ".join(b.get("text", "") for b in blocks(m.get("content")) if b.get("type") == "text")
    return ""


def system_text(body) -> str:
    s = body.get("system")
    if isinstance(s, str):
        return s
    return " ".join(b.get("text", "") for b in (s or []) if isinstance(b, dict))


def sse(events) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _read_body(self) -> bytes:
        if self.headers.get("transfer-encoding", "").lower() == "chunked":
            out = bytearray()
            while True:
                size = int(self.rfile.readline().strip().split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    break
                out += self.rfile.read(size)
                self.rfile.readline()
            return bytes(out)
        n = int(self.headers.get("content-length") or 0)
        return self.rfile.read(n) if n else b""

    def _send(self, status: int, ctype: str, data: bytes) -> None:
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        self._send(200, "application/json", b"")

    def do_CONNECT(self):
        # The harness also sets HTTPS_PROXY to this server, so any attempt to reach a non-loopback
        # host is caught and refused here instead of leaving the machine. Host name only is logged.
        log({"t": time.time(), "method": "CONNECT", "path": self.path, "refused": True})
        self._send(403, "text/plain", b"blocked by scale spike")
        self.close_connection = True

    def do_GET(self):
        if self.path.startswith("http"):
            log({"t": time.time(), "method": "GET", "path": self.path.split("/")[2], "refused": True})
            self._send(403, "text/plain", b"blocked by scale spike")
            return
        log({"t": time.time(), "method": "GET", "path": self.path.split("?")[0]})
        self._send(200, "application/json", b"{}")

    def do_POST(self):
        t_start = time.time()
        raw = self._read_body()
        t_recv = time.time()
        path = self.path.split("?")[0]
        if path.startswith("http"):  # absolute-form request through the proxy setting: refuse
            log({"t": t_recv, "method": "POST", "path": path.split("/")[2], "refused": True})
            self._send(403, "text/plain", b"blocked by scale spike")
            return
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        messages = body.get("messages") or []
        honest = int(len(raw) / A.bpt)
        reported = A.const if A.mode == "contexto" else honest
        lut = last_user_text(messages)
        st = system_text(body)
        rec = {
            "t_start": t_start, "t": t_recv, "method": "POST", "path": path, "body_bytes": len(raw),
            "messages": len(messages), "model": body.get("model"), "max_tokens": body.get("max_tokens"),
            "stream": bool(body.get("stream")), "tools": len(body.get("tools") or []),
            "honest_tokens": honest, "reported_input": reported,
            "compaction": any(k in lut or k in st for k in COMPACT_MARKERS),
            "last_user_chars": len(lut), "system_chars": len(st),
            "header_names": sorted(h.lower() for h in self.headers.keys()),
            "beta": self.headers.get("anthropic-beta"),
        }
        if path.endswith("/count_tokens"):
            log(rec)
            self._send(200, "application/json", json.dumps({"input_tokens": reported}).encode())
            return
        if not path.endswith("/v1/messages"):
            log(rec)
            self._send(200, "application/json", b"{}")
            return
        if A.mode == "honest" and A.window and honest > A.window:
            rec["status"] = 400
            log(rec)
            err = {"type": "error", "error": {"type": "invalid_request_error",
                   "message": f"prompt is too long: {honest} tokens > {A.window} maximum"}}
            self._send(400, "application/json", json.dumps(err).encode())
            return
        if A.delay:  # hold the connection open so the harness's lsof sampling can see it
            time.sleep(A.delay)
        with LOCK:
            COUNTER["n"] += 1
            n = COUNTER["n"]
        use_tool = A.tool_loop > 0 and not rec["compaction"] and trailing_tool_results(messages) < A.tool_loop
        rec["status"] = 200
        rec["reply"] = "tool_use" if use_tool else ("summary" if rec["compaction"] else "text")
        log(rec)
        usage = {"input_tokens": reported, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        message = {"id": f"msg_fake{n:06d}", "type": "message", "role": "assistant",
                   "model": body.get("model") or "claude-opus-5-5"}
        if use_tool:
            block = {"type": "tool_use", "id": f"toolu_fake{n:06d}", "name": "Read", "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps({"file_path": A.read_path})}
            stop = "tool_use"
        else:
            txt = ("<summary>Synthetic summary from the fake upstream.</summary>" if rec["compaction"]
                   else "ok")
            block = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": txt}
            stop = "end_turn"
        if not body.get("stream"):
            final = dict(block)
            if use_tool:
                final["input"] = {"file_path": A.read_path}
            else:
                final["text"] = delta["text"]
            self._send(200, "application/json", json.dumps(
                {**message, "content": [final], "stop_reason": stop, "stop_sequence": None,
                 "usage": {**usage, "output_tokens": 5}}).encode())
            return
        events = [
            {"type": "message_start", "message": {**message, "content": [], "stop_reason": None,
                                                  "stop_sequence": None, "usage": {**usage, "output_tokens": 1}}},
            {"type": "content_block_start", "index": 0, "content_block": block},
            {"type": "content_block_delta", "index": 0, "delta": delta},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
             "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        ]
        self._send(200, "text/event-stream", sse(events))

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", A.port), H)
    srv.daemon_threads = True
    print(f"fake upstream on 127.0.0.1:{A.port} mode={A.mode}", flush=True)
    srv.serve_forever()
