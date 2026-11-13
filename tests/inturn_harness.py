"""A scripted Claude Code and a fake upstream for the in-turn paging tests (D63, contract rule 5).

``Cli`` keeps a local history the way Claude Code does (the whole history on every request, a
``cache_control`` mark that moves along the tail) and sends it through the real ``RewriteEngine``.
``Upstream`` answers each forwarded request and enforces preserved thinking the way an account created
on or after 2026-08-31 does: it signs every thinking block it returns with a hash of the request it
answered (thinking and ``cache_control`` aside) and refuses any later request that carries a thinking
block with anything changed before it. Each forwarded request is also checked for tool call and result
pairing and, between batches and rotations, for being an append-only extension of the one before.

All content is synthetic. No model is called.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import struct
from typing import Any, NamedTuple

from contexto import mcp
from contexto.canonical import canonical_bytes
from contexto.rewrite import MESSAGES, Config, RewriteEngine, _drop_cache_control
from contexto.tokens import media_cost, split_media

MODEL = "claude-haiku-5-5"
BYTES_PER_TOKEN = 3.2  # the fake tokenizer: text bytes a token
THINKING = ("thinking", "redacted_thinking")
EDITS = {"in_turn_batch", "rotation", "resync"}  # a request holding one may change earlier messages
SESSION = {"x-claude-code-session-id": "11111111-2222-3333-4444-555555555555",
           "user-agent": "claude-cli/2.1.318 (external, cli)"}
AGENT = {**SESSION, "x-claude-code-agent-id": "a36c784c53af45cc0"}


class Refused(AssertionError):
    """The fake upstream's 400: a thinking block bound to a different conversation."""


class Sent:
    """One forwarded request."""

    def __init__(self, forwarded: list[dict], digests: list[bytes], estimate: int, real: int, events: list[dict],
                 room: int, thinking: int) -> None:
        self.forwarded = forwarded  # the message list as forwarded
        self.digests = digests  # one per message, cache_control dropped, thinking included
        self.estimate = estimate  # the engine's pre-send estimate of what it forwarded
        self.real = real  # the fake upstream's token count
        self.events = events
        self.names = [e["event"] for e in events]
        self.room = room  # the recall room the request publishes
        self.thinking = thinking  # thinking blocks forwarded
        self._messages: list[dict] | None = None

    @property
    def messages(self) -> list[dict]:
        """The forwarded messages with ``cache_control`` dropped."""
        if self._messages is None:
            self._messages = _drop_cache_control(self.forwarded)
        return self._messages


class Measured(NamedTuple):
    """What the harness needs of one forwarded message, computed once per message object."""

    message: dict  # held, so its id stays its own
    digest: bytes  # of the message with cache_control dropped
    bound: bytes  # the same with thinking dropped too: what a later thinking block is bound to
    size: int  # canonical bytes, media left out
    media: int  # the media's tokens
    thinking: int  # thinking blocks


def blocks(message: dict) -> list:
    content = message.get("content")
    return content if isinstance(content, list) else []


def without_thinking(message: dict) -> dict:
    if not isinstance(message.get("content"), list):
        return message
    return {**message, "content": [b for b in message["content"]
                                   if not (isinstance(b, dict) and b.get("type") in THINKING)]}


def output(tag: str, size: int) -> str:
    """About ``size`` bytes of tool output: numbered lines with one line in the middle that names
    ``tag``, for search to find."""
    lines = [f"row {i:05d} of {tag}: synthetic tool output, nothing of note" for i in range(max(3, size // 58))]
    lines[len(lines) // 2] = f"needle-{tag}-midpoint = garnet"
    return "\n".join(lines)


def png(width: int, height: int) -> dict:
    """An image block whose PNG header says ``width`` by ``height`` (the estimator reads only the header)."""
    header = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height)
              + b"\x08\x06\0\0\0")
    data = base64.b64encode(header + b"synthetic pixels " * 40).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def check_pairing(messages: list[dict]) -> None:
    """Every ``tool_use`` has its ``tool_result`` in the next message, a user message, and no result is
    orphaned; no two assistant messages follow each other."""
    for i, message in enumerate(messages):
        role = message.get("role")
        uses = [b["id"] for b in blocks(message) if isinstance(b, dict) and b.get("type") == "tool_use"]
        results = [b["tool_use_id"] for b in blocks(message)
                   if isinstance(b, dict) and b.get("type") == "tool_result"]
        if uses:
            assert role == "assistant", f"message {i}: a tool_use outside an assistant message"
            following = messages[i + 1] if i + 1 < len(messages) else None
            if following is not None:  # the newest assistant message's calls may still be running
                assert following.get("role") == "user", f"message {i + 1}: not the results of message {i}"
                answered = {b["tool_use_id"] for b in blocks(following) if isinstance(b, dict)
                            and b.get("type") == "tool_result"}
                assert set(uses) <= answered, f"message {i}: tool_use without its tool_result"
        if results:
            assert role == "user", f"message {i}: a tool_result outside a user message"
            previous = messages[i - 1] if i else {}
            called = {b["id"] for b in blocks(previous) if isinstance(b, dict) and b.get("type") == "tool_use"}
            assert previous.get("role") == "assistant" and set(results) <= called, \
                f"message {i}: tool_result without its tool_use"
        if role == "assistant" and i:
            assert messages[i - 1].get("role") != "assistant", f"message {i}: two assistant messages in a row"
            assert blocks(message), f"message {i}: an assistant message with no content"


class Upstream:
    """The fake model API. ``tokens`` is its tokenizer; ``respond`` verifies and answers a request."""

    def __init__(self) -> None:
        self.replies = 0
        self._seen: dict[int, Measured] = {}
        self._heads: dict[int, tuple[Any, bytes, int]] = {}

    def measure(self, message: dict) -> Measured:
        seen = self._seen.get(id(message))
        if seen is None or seen.message is not message:
            plain = _drop_cache_control(message)
            stripped, media = split_media(message)
            seen = self._seen[id(message)] = Measured(
                message, hashlib.sha256(canonical_bytes(plain)).digest(),
                hashlib.sha256(canonical_bytes(without_thinking(plain))).digest(), len(canonical_bytes(stripped)),
                media_cost(media).tokens,
                sum(1 for b in blocks(message) if isinstance(b, dict) and b.get("type") in THINKING))
        return seen

    def head(self, request: dict) -> tuple[bytes, int]:
        system = request.get("system")
        seen = self._heads.get(id(system))
        if seen is None or seen[0] is not system:
            head = {"system": system, "tools": request.get("tools")}
            digest = hashlib.sha256(canonical_bytes(_drop_cache_control(head))).digest()
            seen = self._heads[id(system)] = (system, digest, len(canonical_bytes(head)))
        return seen[1], seen[2]

    def chain(self, request: dict) -> list[str]:
        """``chain[j]``: a hash of the head and the first ``j`` messages, thinking and ``cache_control``
        aside. A thinking block in message ``j`` was produced after exactly that prefix."""
        digest = self.head(request)[0]
        out = [digest.hex()]
        for message in request["messages"]:
            digest = hashlib.sha256(digest + self.measure(message).bound).digest()
            out.append(digest.hex())
        return out

    def verify(self, request: dict) -> str:
        chain = self.chain(request)
        for j, message in enumerate(request["messages"]):
            if not self.measure(message).thinking:
                continue
            for block in blocks(message):
                if isinstance(block, dict) and block.get("type") in THINKING:
                    signed = block.get("signature") if block["type"] == "thinking" else block.get("data")
                    if signed != "sig-" + chain[j]:
                        raise Refused(f"400: the thinking block in message {j} is bound to a different conversation")
        return chain[-1]

    def tokens(self, request: dict) -> int:
        measured = [self.measure(m) for m in request["messages"]]
        size = self.head(request)[1] + sum(m.size for m in measured)
        return math.ceil(size / BYTES_PER_TOKEN) + sum(m.media for m in measured)

    def respond(self, request: dict, calls: list[tuple[str, dict]] | None) -> dict:
        signature = "sig-" + self.verify(request)
        self.replies += 1
        content: list[dict] = [{"type": "thinking", "thinking": f"synthetic thought {self.replies}",
                                "signature": signature}]
        if self.replies % 7 == 0:
            content.append({"type": "redacted_thinking", "data": signature})
        if calls:
            content += [{"type": "tool_use", "id": f"toolu_{self.replies:05d}_{c}", "name": name, "input": tool_input}
                        for c, (name, tool_input) in enumerate(calls)]
        else:
            content.append({"type": "text", "text": f"synthetic answer {self.replies}"})
        return {"role": "assistant", "content": content}


class Cli:
    """One conversation of a scripted Claude Code going through ``engine``."""

    def __init__(self, store: Any, config: Config | None = None, *, engine: RewriteEngine | None = None,
                 head_bytes: int = 64_000, headers: dict[str, str] | None = None, check: bool = True) -> None:
        self.store = store
        self.engine = engine or RewriteEngine(store, config=config or Config())
        self.config = self.engine.config
        self.upstream = Upstream()
        self.headers = dict(SESSION if headers is None else headers)
        self.history: list[dict] = []
        self.sent: list[Sent] = []
        self.usage_events: list[dict] = []
        self.check = check
        text = ("synthetic head: system prompt and tool descriptions. " * (head_bytes // 52 + 1))[:head_bytes]
        self.system = [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]
        self.tools = [{"name": name, "description": "synthetic", "input_schema": {"type": "object"}}
                      for name in ("Read", "Bash", "Grep", "Agent", "mcp__contexto__contexto_recall",
                                   "mcp__contexto__contexto_search")]

    def request(self) -> dict:
        """The whole history, with Claude Code's moving ``cache_control`` on the last block."""
        messages = list(self.history)
        last = messages[-1]
        content = last["content"]
        content = [{"type": "text", "text": content}] if isinstance(content, str) else list(content)
        content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
        messages[-1] = {**last, "content": content}
        return {"model": MODEL, "max_tokens": 32_000, "stream": True, "thinking": {"type": "adaptive"},
                "system": self.system, "tools": self.tools, "messages": messages}

    def prompt(self, text: str, *extra: dict) -> None:
        self.history.append({"role": "user", "content": [*extra, {"type": "text", "text": text}]})

    def results(self, contents: list[Any]) -> None:
        """The results of the newest assistant message's calls, one content each, in order."""
        uses = [b for b in blocks(self.history[-1]) if b.get("type") == "tool_use"]
        assert len(uses) == len(contents)
        self.history.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": use["id"], "content": content}
            for use, content in zip(uses, contents)]})

    def send(self, calls: list[tuple[str, dict]] | None = None) -> Sent:
        """One request: transform, check what is forwarded, answer it, and feed the usage back."""
        request = self.request()
        upstream, _, events = self.engine(request, self.headers, MESSAGES)
        names = [e["event"] for e in events]
        event = events[-1]
        assert event["event"] == "request"
        estimate = event.get("sent_estimate", event.get("estimate"))
        forwarded = upstream["messages"]
        measured = [self.upstream.measure(m) for m in forwarded]
        digests = [m.digest for m in measured]
        if self.check:
            check_pairing(forwarded)
            previous = self.sent[-1] if self.sent else None
            if previous is not None and not EDITS & set(names):
                assert digests[:len(previous.digests)] == previous.digests, \
                    f"request {len(self.sent)}: not an append-only extension of the previous one"
        reply = self.upstream.respond(upstream, calls)  # refuses a thinking block whose prefix changed
        real = self.upstream.tokens(upstream)
        sent = Sent(forwarded, digests, estimate, real, events,
                    self.config.bound - estimate - self.config.headroom_floor, sum(m.thinking for m in measured))
        self.sent.append(sent)
        ids = tuple(b["id"] for b in reply["content"] if b.get("type") == "tool_use")
        self.usage_events += self.engine.observe_usage(MESSAGES, {"input_tokens": real, "output_tokens": 200}, request,
                                                       tool_calls=bool(calls), tool_call_ids=ids)
        self.history.append(reply)
        return sent

    def loop(self, rounds: int, result: Any, call: Any = None) -> None:
        """``rounds`` tool rounds and the final answer. ``result(r)`` is round ``r``'s result content, or a
        tuple of them for parallel calls; ``call(r)`` its ``(name, input)``."""
        for r in range(rounds):
            contents = result(r)
            contents = list(contents) if isinstance(contents, tuple) else [contents]
            calls = [call(r) if call else ("Read", {"file_path": f"src/module_{r:04d}.py"})]
            calls += [("Read", {"file_path": f"src/module_{r:04d}_{c}.py"}) for c in range(1, len(contents))]
            self.send(calls)
            self.results(contents)
        self.send()

    # What the run did ------------------------------------------------------------------------

    def events(self, name: str) -> list[dict]:
        return [e for s in self.sent for e in s.events if e["event"] == name]

    @property
    def state(self) -> Any:
        return next(iter(self.engine.states.values())) if len(self.engine.states) == 1 else None


def tool(store: Any, name: str, arguments: dict) -> str:
    """The text of one MCP tool call against ``store``, as the model would get it."""
    server = mcp.Server(store, io.StringIO())
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    reply = server.handle_line(line)
    return "".join(part.get("text", "") for part in reply["result"]["content"])
