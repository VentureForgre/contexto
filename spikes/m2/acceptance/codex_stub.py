"""A zero-spend Responses API upstream for the Codex acceptance dry run (M2 A16).

It serves ``POST /v1/responses`` (and ``/responses``) on 127.0.0.1 as a streamed Responses API
reply, in the order the real API streams it (``stream_events``): ``response.created``, then for each
output item ``response.output_item.added`` (the item still empty), its delta
(``response.output_text.delta`` or ``response.custom_tool_call_input.delta``), and
``response.output_item.done`` (the full item), then ``response.completed`` with usage. The
``.added`` event of a tool call is what starts the proxy's stream hold (PR #74, builder-1's A20
finding: without it the dry run never exercised the hold). ``GET .../models`` answers an empty list.
Nothing leaves the machine and no credential is checked or stored.

Replies follow the D27 protocol the harness drives:
- a content turn gets ``OK``;
- a probe turn's first request gets one ``exec`` custom tool call whose JavaScript calls the Contexto
  MCP tools Codex exposes inside code mode (``contexto_search``, then ``contexto_recall`` on the
  first page id the search names, else on the first page id the request carries in its preamble's
  index), so the run proves recall is callable end to end;
- the request that carries that call's output gets the probe answers, one ``q_id: answer`` line per
  probe of the turn, from ``answers`` (the stub is told them, so the dry run exercises scoring, not
  model quality).

Usage is synthetic: ``input_tokens`` is the request's bytes over ``BYTES_PER_TOKEN``,
``cached_tokens`` half of it after the first request, ``output_tokens`` the reply's bytes over 3.
The stub keeps a content-free record per request (path, bytes, tokens, reply kind)."""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

BYTES_PER_TOKEN = 3.0
PROBE_MARK = "Answer the questions below about this conversation."
INDEX_LINE = re.compile(r"(pg_[0-9a-f]{16}) turn \d+:")  # store.pages.index_line, not a lookalike in a log
SEARCH_JS = """\
const find = (suffix) => ALL_TOOLS.find((t) => t.name.endsWith(suffix));
const search = find("contexto_search");
const recall = find("contexto_recall");
const hits = await tools[search.name]({query: %s});
const seen = JSON.stringify(hits);
text("search: " + seen.length + " chars");
const found = seen.match(/pg_[0-9a-f]{16}/);
const page = found ? found[0] : %s;
if (page) {
  const got = await tools[recall.name]({page_id: page});
  text("recall: " + JSON.stringify(got).length + " chars");
}
"""


def _user_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content or [] if isinstance(p, dict))


def reply_items(items: list[dict[str, Any]], answers: dict[str, str], query: str) -> tuple[str, list[dict]]:
    """The reply kind and output items for a request's ``input`` (see the module docstring)."""
    last_user = max((i for i, it in enumerate(items) if it.get("role") == "user" and PROBE_MARK in _user_text(it)),
                    default=None)
    prompts = [i for i, it in enumerate(items) if it.get("role") == "user"]
    probing = last_user is not None and prompts and last_user == prompts[-1]
    if not probing:
        return "ok", [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OK"}]}]
    called = any(it.get("type") == "custom_tool_call_output" for it in items[last_user:])
    if not called:
        indexed = INDEX_LINE.search(json.dumps(items))  # the preamble's index lists the paged ids
        code = SEARCH_JS % (json.dumps(query), json.dumps(indexed.group(1) if indexed else None))
        return "recall_call", [{"type": "custom_tool_call", "call_id": f"call_{len(items)}", "name": "exec", "input": code}]
    ids = [line.split(":", 1)[0].strip() for line in _user_text(items[last_user]).splitlines()
           if line.startswith("q_") and ":" in line]
    text = "\n".join(f"{q}: {answers.get(q, 'unknown')}" for q in ids)
    return "answers", [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}]


def stream_events(rid: str, output: list[dict[str, Any]], usage: dict[str, Any]) -> list[dict[str, Any]]:
    """The Responses API events for ``output``, in the real stream's order (module docstring)."""
    events: list[dict[str, Any]] = [{"type": "response.created", "response": {"id": rid, "status": "in_progress"}}]
    for i, item in enumerate(output):
        if item["type"] == "message":
            text = "".join(p.get("text", "") for p in item["content"])
            events += [{"type": "response.output_item.added", "output_index": i,
                        "item": {**item, "content": [], "status": "in_progress"}},
                       {"type": "response.output_text.delta", "output_index": i, "content_index": 0, "delta": text}]
        else:
            events += [{"type": "response.output_item.added", "output_index": i,
                        "item": {**item, "input": "", "status": "in_progress"}},
                       {"type": "response.custom_tool_call_input.delta", "output_index": i, "delta": item["input"]}]
        events.append({"type": "response.output_item.done", "output_index": i, "item": {**item, "status": "completed"}})
    events.append({"type": "response.completed",
                   "response": {"id": rid, "status": "completed", "output": output, "usage": usage}})
    return events


class CodexStub:
    """``with CodexStub(answers, query) as stub:`` serves on ``stub.url`` (no ``/v1``) until exit."""

    def __init__(self, answers: dict[str, str] | None = None, query: str = "retention_days") -> None:
        self.answers = dict(answers or {})
        self.query = query
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # /v1/models and anything else Codex asks before a turn
                stub._record({"method": "GET", "path": self.path.split("?")[0]})
                self._send(200, b'{"object": "list", "data": [], "models": []}')  # Codex reads "models"

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                path = self.path.split("?")[0]
                if not path.endswith("/responses"):
                    stub._record({"method": "POST", "path": path, "status": 404})
                    self._send(404, b'{"error": {"message": "stub"}}')
                    return
                body = json.loads(raw)
                kind, output = reply_items(body.get("input") or [], stub.answers, stub.query)
                tokens = int(len(raw) / BYTES_PER_TOKEN)
                with stub._lock:
                    cached = tokens // 2 if any(r.get("kind") for r in stub.requests) else 0
                out_tokens = max(1, len(json.dumps(output)) // 3)
                usage = {"input_tokens": tokens, "input_tokens_details": {"cached_tokens": cached},
                         "output_tokens": out_tokens, "output_tokens_details": {"reasoning_tokens": 0},
                         "total_tokens": tokens + out_tokens}
                stub._record({"method": "POST", "path": path, "bytes": len(raw), "input_tokens": tokens,
                              "cached_tokens": cached, "output_tokens": out_tokens, "kind": kind,
                              "input_items": len(body.get("input") or []),
                              "shape": [(it.get("type", "message"), it.get("role")) for it in body.get("input") or []],
                              "previous_response_id": bool(body.get("previous_response_id")),
                              "store": body.get("store")})
                events = stream_events(f"resp_stub_{len(stub.requests)}", output, usage)
                # Chunked, one event per chunk, as a real upstream streams (so the proxy's stream hold applies).
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for e in events:
                    chunk = f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode()
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.write(b"0\r\n\r\n")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _record(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self.requests.append(entry)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> CodexStub:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
