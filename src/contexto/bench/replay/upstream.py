"""The local fake upstream of a replay. It can never forward anything.

It is an HTTP server on 127.0.0.1 and nothing else: this module opens no outgoing connection and
imports no HTTP client, and ``FakeUpstream`` refuses any address but ``127.0.0.1``. It stands where
the model API would be, behind the proxy.

For each ``POST /v1/messages`` it

1. prices the request it received (``model_tokens``): a text of ``LOG_TEXT_MIN_BYTES`` bytes or more
   in a user message (a text block, or the text of a tool result) is pasted log text and counts
   ``LOG_BYTES_PER_TOKEN`` bytes per token, the rate M0 measured for this generated log text; every
   other byte of the request's compact JSON counts ``OTHER_BYTES_PER_TOKEN`` bytes per token. These
   are the rates of ``spikes/m2/acceptance/stubrun.py``. There is no tokenizer here, so this is a
   modelled count, not a real one; the proxy's estimator learns from it as it would from real usage;
2. looks for a compaction in the request (``compaction_markers``);
3. checks the request against the Messages API's rules that need no model (``check_request``): roles
   alternating from the user, every tool result answering a tool call of the message before it and
   every tool call answered in the next, no empty content, and well-formed ``model``, ``max_tokens``,
   ``system``, ``tools`` and ``tool_choice``. A request that breaks one gets the 400
   ``invalid_request_error`` the real API gives, the rule is recorded as unexpected, and the replay
   stops with it named: a proxy that sent such a request to a real upstream would have broken the
   session;
4. replies with the next step of the scripted model (``contexto.bench.replay.scripted``), as the
   Messages API streams it, after checking that the request is the one that step answers: its last
   message holds the turn's text, or the result of the tool call the step before it made. A request
   that does not fit gets a 400 and is recorded as unexpected, and the script does not advance.

All of the modelled input is reported as ``input_tokens``; the cache fields are 0, because a replay
cannot show cache reads. Any other path gets a 404.
"""

from __future__ import annotations

import json
import math
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from contexto.bench.model import Script
from contexto.bench.replay.scripted import Step

LOOPBACK = "127.0.0.1"
MESSAGES_PATH = "/v1/messages"
LOG_BYTES_PER_TOKEN = 1.8
OTHER_BYTES_PER_TOKEN = 3.0
LOG_TEXT_MIN_BYTES = 5_000
MODEL_NAME = "contexto-bench-replay"  # named in the replies; not a model
NO_SUCH_PATH = "the replay's fake upstream has no such path"
# The start of the summary that opens a conversation Claude Code compacted
# (contexto.rewrite.CLAUDE_CODE_COMPACTION_SUMMARY; tests check the two are equal).
COMPACTION_SUMMARY = ("This session is being continued from a previous conversation that ran out of context. The "
                      "summary below covers the earlier portion of the conversation.\n")


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _is_log(text: Any) -> bool:
    return isinstance(text, str) and len(text.encode("utf-8")) >= LOG_TEXT_MIN_BYTES


def _without_log(content: Any) -> tuple[Any, int]:
    """``content`` with its log texts emptied, and the bytes of log text taken out."""
    if _is_log(content):
        return "", len(content.encode("utf-8"))
    if not isinstance(content, list):
        return content, 0
    blocks, taken = [], 0
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and _is_log(block.get("text")):
            taken += len(block["text"].encode("utf-8"))
            block = {**block, "text": ""}
        elif isinstance(block, dict) and block.get("type") == "tool_result":
            inner, n = _without_log(block.get("content"))
            if n:
                taken += n
                block = {**block, "content": inner}
        blocks.append(block)
    return blocks, taken


def model_tokens(body: dict[str, Any]) -> dict[str, int]:
    """The modelled input tokens of a request body: log text at 1.8 bytes per token, the rest of the
    compact JSON at 3.0, each part rounded up."""
    log_bytes = 0
    messages = []
    for message in body.get("messages") or []:
        if isinstance(message, dict) and message.get("role") == "user":
            content, taken = _without_log(message.get("content"))
            if taken:
                log_bytes += taken
                message = {**message, "content": content}
        messages.append(message)
    other_bytes = _size({**body, "messages": messages})
    tokens = math.ceil(log_bytes / LOG_BYTES_PER_TOKEN) + math.ceil(other_bytes / OTHER_BYTES_PER_TOKEN)
    return {"tokens": tokens, "log_bytes": log_bytes, "other_bytes": other_bytes}


def output_tokens(text: str) -> int:
    """The modelled output tokens of a reply's text or tool input."""
    return max(1, math.ceil(len(text.encode("utf-8")) / OTHER_BYTES_PER_TOKEN))


def compaction_markers(body: dict[str, Any], raw: bytes) -> list[str]:
    """What in a request shows a compaction, by name; empty when nothing does.

    ``summary``: the sentence Claude Code opens a compacted conversation with, anywhere in the body.
    ``compaction_block``: a content block of type ``compaction``, which the API returns and a client
    sends back when the API compacts. ``context_management``: a request that asks the API to compact
    (an edit whose type starts with ``compact``). ``transcript_marker``: Claude Code's transcript
    markers ``compact_boundary`` or ``isCompactSummary``.
    """
    found = []
    if json.dumps(COMPACTION_SUMMARY.strip())[1:-1].encode("utf-8") in raw:
        found.append("summary")
    for message in body.get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "compaction" for b in content):
            found.append("compaction_block")
            break
    management = body.get("context_management")
    edits = management.get("edits") if isinstance(management, dict) else None
    if isinstance(edits, list) and any(isinstance(e, dict) and str(e.get("type") or "").startswith("compact")
                                       for e in edits):
        found.append("context_management")
    if b"compact_boundary" in raw or b"isCompactSummary" in raw:
        found.append("transcript_marker")
    return found


_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}")
TOOL_CHOICES = ("auto", "any", "tool", "none")


def _blocks(content: Any) -> list[dict[str, Any]]:
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _check_text_blocks(where: str, blocks: list[Any]) -> str | None:
    for k, block in enumerate(blocks):
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            return f"{where}.{k}: a content block must be an object with a type"
        if block["type"] == "text" and not (isinstance(block.get("text"), str) and block["text"]):
            return f"{where}.{k}: text content blocks must be non-empty"
    return None


def _check_tools(body: dict[str, Any]) -> str | None:
    tools = body.get("tools")
    if tools is None:
        return None
    if not isinstance(tools, list):
        return "tools: Input should be a valid list"
    names: set[str] = set()
    for k, tool in enumerate(tools):
        if not isinstance(tool, dict):
            return f"tools.{k}: Input should be an object"
        if tool.get("type") not in (None, "custom"):  # a server tool (web search and the like) has no schema
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            return f"tools.{k}.name: String should match pattern '^[a-zA-Z0-9_-]{{1,128}}$'"
        if name in names:
            return f"tools: Tool names must be unique, and {name} is given twice"
        names.add(name)
        schema = tool.get("input_schema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            return f"tools.{k}.input_schema: an input schema must be an object of type 'object'"
    return None


def _check_tool_choice(body: dict[str, Any]) -> str | None:
    if "tool_choice" not in body:
        return None
    choice = body["tool_choice"]
    if not isinstance(choice, dict) or choice.get("type") not in TOOL_CHOICES:
        return f"tool_choice: the type must be one of {', '.join(TOOL_CHOICES)}"
    names = {t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)}
    if choice["type"] in ("auto", "any", "tool") and not names:
        return f"tool_choice: tool_choice of type {choice['type']} needs tools"
    if choice["type"] == "tool" and choice.get("name") not in names:
        return f"tool_choice.name: no tool named {choice.get('name')!r} is in tools"
    return None


def _check_messages(body: dict[str, Any]) -> str | None:
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return "messages: at least one message is required"
    uses_tools = False
    for i, message in enumerate(messages):
        where = f"messages.{i}"
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
            return f"{where}.role: Input should be 'user' or 'assistant'"
        if i == 0 and message["role"] != "user":
            return "messages.0.role: the first message must use the user role"
        if i > 0 and message["role"] == messages[i - 1].get("role"):
            return f"{where}.role: roles must alternate between user and assistant, and messages {i - 1} and {i} " \
                   f"are both {message['role']}"
        content = message.get("content")
        if isinstance(content, str):
            if not content:
                return f"{where}: all messages must have non-empty content"
            continue
        if not isinstance(content, list) or not content:
            return f"{where}: all messages must have non-empty content"
        problem = _check_text_blocks(f"{where}.content", content)
        if problem:
            return problem
        kinds = [b["type"] for b in content]
        uses_tools = uses_tools or "tool_use" in kinds or "tool_result" in kinds
        if message["role"] == "assistant" and "tool_result" in kinds:
            return f"{where}: `tool_result` blocks can only be in user messages"
        if message["role"] == "user" and "tool_use" in kinds:
            return f"{where}: `tool_use` blocks can only be in assistant messages"
        # Every tool_result answers a tool_use of the assistant message just before it.
        asked = {b.get("id") for b in _blocks(messages[i - 1].get("content"))
                 if b.get("type") == "tool_use"} if i > 0 and isinstance(messages[i - 1], dict) else set()
        stray = [b.get("tool_use_id") for b in content if b["type"] == "tool_result" and b.get("tool_use_id") not in asked]
        if stray:
            return (f"{where}.content: unexpected `tool_use_id` found in `tool_result` blocks: {stray[0]}. Each "
                    "`tool_result` block must have a corresponding `tool_use` block in the previous message.")
        # Every tool_use is answered in the next message, which is a user message.
        calls = [b.get("id") for b in content if b["type"] == "tool_use"]
        if calls:
            following = messages[i + 1] if i + 1 < len(messages) and isinstance(messages[i + 1], dict) else {}
            answered = {b.get("tool_use_id") for b in _blocks(following.get("content")) if b.get("type") == "tool_result"}
            missing = [c for c in calls if c not in answered]
            if missing:
                return (f"{where}: `tool_use` ids were found without `tool_result` blocks immediately after: "
                        f"{missing[0]}. Each `tool_use` block must have a corresponding `tool_result` block in the "
                        "next message.")
    ids = [b.get("id") for m in messages if isinstance(m, dict) for b in _blocks(m.get("content"))
           if b.get("type") == "tool_use"]
    if len(ids) != len(set(ids)):
        return "messages: `tool_use` ids must be unique"
    if uses_tools and not body.get("tools"):
        return "tools: requests which include `tool_use` or `tool_result` blocks must define tools"
    return None


def check_request(body: dict[str, Any]) -> str | None:
    """The first rule of the Messages API that ``body`` breaks, in the API's words, or None.

    Only rules that need no model are checked, the ones a proxy that rewrites the conversation can
    break: ``model`` named and ``max_tokens`` a positive whole number; ``system`` a string or a list of
    non-empty text blocks; ``tools`` well formed with unique names; ``tool_choice`` of a known type that
    names a listed tool; at least one message, the first from the user, roles alternating, no empty
    content or empty text block; every ``tool_result`` answering a ``tool_use`` of the assistant
    message just before it, every ``tool_use`` answered in the next message, ids unique, and tools
    defined when the messages use them. The real API refuses each of these with a 400
    ``invalid_request_error``.
    """
    if not isinstance(body.get("model"), str) or not body["model"]:
        return "model: Field required"
    tokens = body.get("max_tokens")
    if type(tokens) is not int or tokens < 1:
        return "max_tokens: Field required, a whole number of 1 or more"
    system = body.get("system")
    if system is not None:
        if isinstance(system, str):
            pass
        elif not isinstance(system, list):
            return "system: Input should be a valid string or a list of text blocks"
        else:
            if any(not isinstance(b, dict) or b.get("type") != "text" for b in system):
                return "system: every block must be a text block"
            problem = _check_text_blocks("system", system)
            if problem:
                return problem
    return _check_tools(body) or _check_tool_choice(body) or _check_messages(body)


def _texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"
            and isinstance(b.get("text"), str)]


def sse(events: list[dict[str, Any]]) -> list[bytes]:
    """Each event as the bytes of one server-sent event."""
    return [f"event: {e['type']}\ndata: {json.dumps(e, separators=(',', ':'))}\n\n".encode() for e in events]


def reply_events(step: Step, index: int, input_tokens: int) -> tuple[list[dict[str, Any]], int]:
    """The stream events of one scripted reply, in the order the Messages API sends them, and its
    modelled output tokens."""
    usage = {"input_tokens": input_tokens, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    message = {"id": f"msg_bench_{index:04d}", "type": "message", "role": "assistant", "model": MODEL_NAME,
               "content": [], "stop_reason": None, "stop_sequence": None, "usage": {**usage, "output_tokens": 1}}
    events: list[dict[str, Any]] = [{"type": "message_start", "message": message}]
    if step.kind == "text":
        output = output_tokens(step.text)
        events += [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": step.text}},
            {"type": "content_block_stop", "index": 0},
        ]
        stop = "end_turn"
    else:
        partial = json.dumps(step.tool_input)
        output = output_tokens(partial)
        events += [
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": step.tool_id, "name": step.tool, "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": partial}},
            {"type": "content_block_stop", "index": 0},
        ]
        stop = "tool_use"
    events += [{"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": output}},
               {"type": "message_stop"}]
    return events, output


class FakeUpstream:
    """The server. ``records`` has one entry per ``/v1/messages`` request received, in order;
    ``unexpected`` says, in plain words, each request that did not fit the script; ``last_messages``
    is the JSON text of the newest request's messages (what the model would have in view)."""

    def __init__(self, script: Script, planned: list[Step], host: str = LOOPBACK) -> None:
        if host != LOOPBACK:
            raise ValueError(f"the replay's fake upstream binds {LOOPBACK} only, never {host!r}")
        self.host = host
        self.script = script
        self.planned = planned
        self.cursor = 0
        self.records: list[dict[str, Any]] = []
        self.unexpected: list[str] = []
        self.last_messages = "[]"
        self.lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("the fake upstream is not started")
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> int:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, status: int, kind: str, payload: bytes | list[bytes]) -> None:
                """A list is streamed chunked, one event per chunk, as the Messages API streams, so the
                proxy relays it through its stream hold; bytes are sent whole."""
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Connection", "close")
                self.close_connection = True
                if isinstance(payload, list):
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for event in payload:
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(event), event))
                    self.wfile.write(b"0\r\n\r\n")
                else:
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(payload)
                self.wfile.flush()

            def do_POST(self) -> None:  # noqa: N802 - stdlib name
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self._send(*owner.answer(self.path.split("?", 1)[0], raw))

            def do_GET(self) -> None:  # noqa: N802 - stdlib name
                self._send(*owner.error(404, "not_found_error", NO_SUCH_PATH))

            do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_GET  # noqa: N815 - stdlib names

        self._server = ThreadingHTTPServer((self.host, 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                         name="bench-replay-upstream", daemon=True).start()
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @staticmethod
    def error(status: int, kind: str, message: str) -> tuple[int, str, bytes]:
        body = json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode("utf-8")
        return status, "application/json", body

    def _fits(self, step: Step, body: dict[str, Any]) -> str | None:
        """None when the request is the one ``step`` answers, else what is wrong with it."""
        messages = body.get("messages") or []
        last = messages[-1] if messages and isinstance(messages[-1], dict) else {}
        if last.get("role") != "user":
            return "its last message is not a user message"
        if step.round == 0:
            if not any(self.script.turns[step.turn] in text for text in _texts(last.get("content"))):
                return f"its last message does not hold the text of turn {step.turn + 1}"
            return None
        wanted = self.planned[self.cursor - 1].tool_id
        content = last.get("content") if isinstance(last.get("content"), list) else []
        if not any(isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") == wanted
                   for b in content):
            return f"its last message does not hold the result of tool call {wanted}"
        return None

    def answer(self, path: str, raw: bytes) -> tuple[int, str, bytes | list[bytes]]:
        """``(status, content type, body)`` for a POST: the body is a list of stream events for a
        scripted reply, bytes for an error. Records every ``/v1/messages`` request."""
        if path != MESSAGES_PATH:
            return self.error(404, "not_found_error", NO_SUCH_PATH)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = None
        with self.lock:
            index = len(self.records)
            record: dict[str, Any] = {"n": index, "bytes": len(raw), "turn": None, "round": None, "status": 400,
                                      "tokens": 0, "log_bytes": 0, "other_bytes": 0, "output": 0, "messages": 0,
                                      "markers": []}
            self.records.append(record)
            if not isinstance(body, dict):
                self.unexpected.append(f"request {index + 1} is not a JSON object")
                return self.error(400, "invalid_request_error", "the request body is not a JSON object")
            record.update(model_tokens(body), messages=len(body.get("messages") or []),
                          markers=compaction_markers(body, raw))
            self.last_messages = json.dumps(body.get("messages") or [], ensure_ascii=False)
            invalid = check_request(body)
            if invalid:
                self.unexpected.append(f"request {index + 1} breaks a rule of the Messages API, which would refuse "
                                       f"it with a 400: {invalid}")
                return self.error(400, "invalid_request_error", invalid)
            if self.cursor >= len(self.planned):
                self.unexpected.append(f"request {index + 1} came after the last scripted round")
                return self.error(400, "invalid_request_error", "the scripted model has no reply left")
            step = self.planned[self.cursor]
            problem = self._fits(step, body)
            if problem:
                self.unexpected.append(f"request {index + 1} does not fit round {step.round + 1} of turn "
                                       f"{step.turn + 1}: {problem}")
                return self.error(400, "invalid_request_error", f"the scripted model did not expect this: {problem}")
            self.cursor += 1
            events, output = reply_events(step, index, record["tokens"])
            record.update(turn=step.turn, round=step.round, status=200, output=output)
            return 200, "text/event-stream", sse(events)
