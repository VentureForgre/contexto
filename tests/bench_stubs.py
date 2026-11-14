"""Stubs for the tests of the live benchmark (``contexto bench`` with no mode flag): no model, no spend.

``StubModel`` is an upstream on 127.0.0.1 that plays the model. It knows the scripts, finds the turn a
request is serving from the request's own messages, and answers each kind of turn: ``OK`` for a log
turn, the decision line for a decision turn, one file-read tool call per file for the tool-loop turn,
and ``q_id: answer`` lines for a probe turn. It answers a probe correctly only when the answer is in
the text of the request it was sent, so an arm that lost the fact gets it wrong, and it can be told
to get a seeded share of each arm's probes wrong on top of that, so the three arms end with different,
known scores. For a request that came through Contexto it first calls Contexto's search tool, once
per probe, and answers from what the search returned: recall goes through the real MCP server.

It reports usage with the three input classes, checks each request's key in memory (booleans only),
and can answer chosen requests with an error status, cut a stream short, or report a token count that
does not fit. With ``split`` it answers a Claude Code arm's decision and probe turns in two messages
around a file-read tool call, as a model that checks something before it finishes: the decision line
and half the answers (in markdown, one of them wrong) first, then the rest and the corrected answer.
With ``tool_forever`` it answers every request of a Claude Code arm with another tool call.

``install_stub_claude`` writes a ``claude`` executable that runs ``tests/fixtures/bench/stub_claude.py``.
"""

from __future__ import annotations

import hmac
import json
import math
import os
import random
import re
import stat
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from contexto.bench.model import Probe, Script

STUB_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "bench" / "stub_claude.py"
LOG_BYTES_PER_TOKEN = 1.8
OTHER_BYTES_PER_TOKEN = 3.0
LOG_BLOCK_MIN_BYTES = 1_000  # a text block this large is pasted log text
SEARCH_TOOL = "mcp__contexto__contexto_search"
READ_TOOL = "Read"
AFFINITY_HEADER = "x-session-affinity"  # Contexto adds it for an upstream that is not a vendor's host
SUMMARY = "Summary of the earlier conversation: a working log was pasted in parts and acknowledged."


def model_tokens(body: dict[str, Any]) -> int:
    """Modelled input tokens of a request: large text at 1.8 bytes per token, every other byte at 3.0."""
    log_bytes = 0

    def strip(node: Any) -> Any:
        nonlocal log_bytes
        if isinstance(node, str):
            size = len(node.encode("utf-8"))
            if size >= LOG_BLOCK_MIN_BYTES:
                log_bytes += size
                return ""
            return node
        if isinstance(node, list):
            return [strip(x) for x in node]
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items()}
        return node

    rest = strip({k: body.get(k) for k in ("system", "tools", "messages")})
    other = len(json.dumps(rest, separators=(",", ":")).encode("utf-8"))
    return math.ceil(log_bytes / LOG_BYTES_PER_TOKEN) + math.ceil(other / OTHER_BYTES_PER_TOKEN)


def _texts(content: Any) -> list[str]:
    """Every text a message's content holds, tool results included."""
    if isinstance(content, str):
        return [content]
    out: list[str] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("text"), str):
            out.append(block["text"])
        if block.get("type") == "tool_result":
            out.extend(_texts(block.get("content")))
    return out


def _tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b for m in messages if m.get("role") == "user" and isinstance(m.get("content"), list)
            for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]


def wrong(arm: str, seed: int, probe_id: str, rate: float) -> bool:
    """Whether the stub is told to miss this probe for this arm: a seeded draw, the same on every run."""
    return random.Random(f"{arm}:{seed}:{probe_id}").random() < rate


@dataclass
class StubModel:
    scripts: list[Script]
    key: str = ""
    error_rates: dict[str, float] = field(default_factory=dict)  # arm -> share of probes answered wrongly
    usage_scale: dict[str, float] = field(default_factory=dict)  # arm -> factor on the reported input tokens
    wrong_decisions: set[int] = field(default_factory=set)  # turn indexes whose decision reply is wrong (CLI arms)
    count_tokens: dict[str, int] = field(default_factory=dict)  # arm -> the count_tokens answer, instead of the model
    delay_s: dict[str, float] = field(default_factory=dict)  # arm -> seconds to wait before answering a request
    read_paths: str = "relative"  # "relative", or "absolute": join the working directory named in the system prompt
    split: bool = False  # answer decision and probe turns in two messages around a tool call (Claude Code arms)
    tool_forever: set[str] = field(default_factory=set)  # arms whose every request is answered with a tool call
    long_answers: bool = False  # put 250 backticks before each right answer
    requests: list[dict[str, Any]] = field(default_factory=list)
    answered: dict[tuple[str, int, str], str] = field(default_factory=dict)  # (arm, seed, probe id) -> answer given
    searches: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self._turns: dict[str, tuple[Script, int]] = {}
        for script in self.scripts:
            for index, text in enumerate(script.turns):
                self._turns[text] = (script, index)
        self._faults: list[dict[str, Any]] = []
        self._previous: dict[tuple[str, str], int] = {}  # (arm, conversation) -> tokens of its last request
        self._searched: set[tuple[int, int]] = set()
        self._server: ThreadingHTTPServer | None = None

    # -- control ---------------------------------------------------------------------------------

    def fault(self, kind: int | str, *, arm: str | None = None, nth: int = 0, path: str = "/v1/messages",
              times: int = 1) -> None:
        """Answer the ``nth`` request (0-based, counted per arm and path among those not yet faulted)
        with HTTP status ``kind``, or cut its stream short when ``kind`` is ``"drop"``; ``times`` in a row."""
        self._faults.append({"kind": kind, "arm": arm, "nth": nth, "path": path, "left": times, "seen": 0})

    def start(self) -> str:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self._server.handle_error = lambda *args: None  # a client that left mid-answer is not worth a traceback
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def messages_requests(self, arm: str | None = None) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["path"] == "/v1/messages" and (arm is None or r["arm"] == arm)]

    # -- the model ----------------------------------------------------------------------------------

    def arm_of(self, headers: Any) -> str:
        if (headers.get("user-agent") or "").startswith("contexto-bench"):
            return "full"
        return "contexto" if headers.get(AFFINITY_HEADER) else "plain"

    def locate(self, messages: list[dict[str, Any]]) -> tuple[Script, int, list[dict[str, Any]]] | None:
        """The script turn a request is serving and the messages after that turn's user text."""
        for at in range(len(messages) - 1, -1, -1):
            message = messages[at]
            if message.get("role") != "user":
                continue
            content = message.get("content")
            blocks = [content] if isinstance(content, str) else [
                b.get("text") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
            for text in blocks:
                if isinstance(text, str) and text in self._turns:
                    script, index = self._turns[text]
                    return script, index, messages[at + 1:]
        return None

    def reply(self, arm: str, body: dict[str, Any]) -> list[dict[str, Any]]:
        """The content blocks of the assistant's reply to a Messages request."""
        messages = body.get("messages") if isinstance(body.get("messages"), list) else []
        found = self.locate(messages)
        if found is None:  # not a script turn: a compaction summary, a title, or another side request
            return [{"type": "text", "text": SUMMARY}]
        script, index, after = found
        kind = script.kinds[index]
        if arm in self.tool_forever:
            return [{"type": "tool_use", "id": f"toolu_again_{len(after):04d}", "name": READ_TOOL,
                     "input": {"file_path": "does-not-exist.txt"}}]
        split = self.split and arm != "full"
        checked = split and any(b.get("type") == "tool_use" and str(b.get("id", "")).startswith("toolu_split")
                                for m in after if m.get("role") == "assistant" and isinstance(m.get("content"), list)
                                for b in m["content"] if isinstance(b, dict))
        if kind == "log":
            return [{"type": "text", "text": "OK"}]
        if kind == "decision":
            right = script.scripted_reply(index) or ""
            if index in self.wrong_decisions and arm != "full":
                right = right.rsplit(":", 1)[0] + ": not-the-right-one"
            if split:  # the decision first, a check, then a last message with no decision line in it
                return [{"type": "text", "text": "Noted, nothing else to add."}] if checked else [
                    {"type": "text", "text": f"**Decision**\n\n- {right}"}, _split_read(index)]
            return [{"type": "text", "text": right}]
        if kind == "toolloop":
            done = len(_tool_results(after))
            if done < len(script.reads):
                path = script.reads[done]
                if self.read_paths == "absolute":
                    path = os.path.join(_working_directory(body), path)
                return [{"type": "tool_use", "id": f"toolu_read_{done:02d}", "name": READ_TOOL,
                         "input": {"file_path": path}}]
            return [{"type": "text", "text": "OK"}]
        turn = [i for i, k in enumerate(script.kinds) if k == "probe"].index(index)
        probes = {p.id: p for p in script.probes}
        asked = [probes[i] for i in script.probe_turn_ids[turn]]
        if arm == "contexto" and not _tool_results(after) and (script.seed, index) not in self._searched:
            with self.lock:  # once per probe turn: a model does not repeat a search whose results it was given
                self._searched.add((script.seed, index))
                self.searches += len(asked)
            return [{"type": "tool_use", "id": f"toolu_search_{n:02d}", "name": SEARCH_TOOL,
                     "input": {"query": _query(script, p)}} for n, p in enumerate(asked)]
        seen = "\n".join(t for m in messages for t in _texts(m.get("content")))
        lines = []
        for probe in asked:
            parts = [x.strip() for x in probe.answer.split(",")] if probe.kind == "set" else [probe.answer]
            recalled = probe.answer if all(part in seen for part in parts) else "unknown"
            label = next((d.label for d in script.decisions if d.probe == probe.id), None)
            if label is not None:  # what the assistant replied then, right or wrong, if that reply is in sight
                replies = [v for v in re.findall(rf"DECISION {re.escape(label)}: (\S+)", seen) if v != "<name>"]
                recalled = replies[-1] if replies else "unknown"
            given = "unknown" if wrong(arm, script.seed, probe.id, self.error_rates.get(arm, 0.0)) else recalled
            if self.long_answers and given != "unknown":
                given = "`" * 250 + given  # right when scored whole (backticks are trimmed), wrong once cut
            with self.lock:
                self.answered[(arm, script.seed, probe.id)] = given
            lines.append(f"{probe.id}: {given}")
        if not split:
            return [{"type": "text", "text": "\n".join(lines)}]
        half = len(lines) // 2
        if not checked:  # the first half, and the last probe's id with a wrong answer it corrects later
            first = ["**Answers so far**", "", *(f"- {line}" for line in lines[:half]), f"- {asked[-1].id}: unsure",
                     "", "Let me check one thing before the rest."]
            return [{"type": "text", "text": "\n".join(first)}, _split_read(index)]
        return [{"type": "text", "text": "\n".join(["The rest:", *lines[half:]])}]

    def usage(self, arm: str, conversation: str, body: dict[str, Any]) -> dict[str, int]:
        tokens = int(model_tokens(body) * self.usage_scale.get(arm, 1.0))
        with self.lock:
            previous = self._previous.get((arm, conversation), 0)
            self._previous[(arm, conversation)] = tokens
        read = previous if 0 < previous <= tokens - 3 else 0  # a shorter request does not share the prefix
        return {"input_tokens": 3, "cache_creation_input_tokens": max(0, tokens - read - 3),
                "cache_read_input_tokens": read}

    # -- HTTP ---------------------------------------------------------------------------------------

    def _take_fault(self, arm: str, path: str) -> int | str | None:
        with self.lock:
            for fault in self._faults:
                if fault["path"] != path or fault["arm"] not in (None, arm) or not fault["left"]:
                    continue
                fault["seen"] += 1
                if fault["seen"] > fault["nth"]:
                    fault["left"] -= 1
                    return fault["kind"]
        return None

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                path = self.path.split("?")[0].rstrip("/")
                try:
                    body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    body = {}
                if not isinstance(body, dict):
                    body = {}
                arm = stub.arm_of(self.headers)
                x_api_key = self.headers.get("x-api-key") or ""
                bearer = self.headers.get("authorization") or ""
                found = stub.locate(body.get("messages") or [])
                record = {
                    "path": path, "arm": arm, "turn": found[1] if found else None, "bytes": len(raw),
                    "key_ok": bool(stub.key) and (hmac.compare_digest(x_api_key, stub.key)
                                                  or hmac.compare_digest(bearer, f"Bearer {stub.key}")),
                    "x_api_key": bool(x_api_key), "bearer": bool(bearer),
                    "user_agent": self.headers.get("user-agent") or "",
                    "accept_encoding": self.headers.get("accept-encoding"),
                    "affinity": bool(self.headers.get(AFFINITY_HEADER)),
                    "request_class": self.headers.get("x-claude-code-request-class"),
                    "compaction": self.headers.get("x-claude-code-compaction"),
                    "tool_errors": sum(1 for b in _tool_results(body.get("messages") or []) if b.get("is_error")),
                    "tool_choice": (body.get("tool_choice") or {}).get("type"),
                    "tools": [t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)],
                    "cache_breakpoints": json.dumps(body.get("messages") or []).count('"cache_control"'),
                    "system_bytes": len(json.dumps(body.get("system") or "")),
                    "tools_bytes": len(json.dumps(body.get("tools") or [])),
                    "max_tokens": body.get("max_tokens"), "stream": bool(body.get("stream")),
                }
                with stub.lock:
                    stub.requests.append(record)
                fault = stub._take_fault(arm, path)
                if isinstance(fault, int):
                    record["status"] = fault
                    kind = {401: "authentication_error", 403: "permission_error", 429: "rate_limit_error",
                            529: "overloaded_error", 400: "invalid_request_error"}.get(fault, "api_error")
                    self._json({"type": "error", "error": {"type": kind, "message": "stub"}}, fault)
                    return
                if path == "/v1/messages/count_tokens":
                    self._json({"input_tokens": stub.count_tokens.get(arm, model_tokens(body))})
                    return
                if path != "/v1/messages":
                    self._json({"type": "error", "error": {"type": "not_found_error", "message": "stub"}}, 404)
                    return
                if stub.delay_s.get(arm):
                    time.sleep(stub.delay_s[arm])
                conversation = self.headers.get("x-claude-code-session-id") or ""
                usage = stub.usage(arm, conversation, body)
                record["usage"] = usage
                blocks = stub.reply(arm, body)
                record["status"] = 200
                self._stream(blocks, usage, str(body.get("model") or "stub"), cut=fault == "drop")

            def do_HEAD(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("content-length", "0")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                self._json({"type": "error", "error": {"type": "not_found_error", "message": "stub"}}, 404)

            def _stream(self, blocks: list[dict[str, Any]], usage: dict[str, int], model: str, cut: bool) -> None:
                message = {"id": f"msg_stub_{len(stub.requests):05d}", "type": "message", "role": "assistant",
                           "model": model, "content": [], "stop_reason": None, "stop_sequence": None,
                           "usage": {**usage, "output_tokens": 1}}
                events: list[dict[str, Any]] = [{"type": "message_start", "message": message}]
                output = 0
                for index, block in enumerate(blocks):
                    if block["type"] == "text":
                        output += max(1, len(block["text"]) // 4)
                        events += [
                            {"type": "content_block_start", "index": index,
                             "content_block": {"type": "text", "text": ""}},
                            {"type": "content_block_delta", "index": index,
                             "delta": {"type": "text_delta", "text": block["text"]}}]
                    else:
                        output += 20
                        events += [
                            {"type": "content_block_start", "index": index, "content_block": {
                                "type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}},
                            {"type": "content_block_delta", "index": index,
                             "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}}]
                    events.append({"type": "content_block_stop", "index": index})
                stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
                events += [{"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                            "usage": {"output_tokens": output}}, {"type": "message_stop"}]
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("transfer-encoding", "chunked")
                self.end_headers()
                for n, event in enumerate(events):
                    if cut and n == 2:  # after the message start and the first block start: no end, no terminator
                        self.wfile.flush()
                        self.close_connection = True
                        return
                    data = f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

            def _json(self, payload: dict[str, Any], status: int = 200) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        return Handler


def _split_read(index: int) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_split_{index:03d}", "name": READ_TOOL,
            "input": {"file_path": "notes-that-do-not-exist.md"}}


def _query(script: Script, probe: Probe) -> str:
    """What a model would search for: the key the fact was planted under, or the decision's label."""
    for decision in script.decisions:
        if decision.probe == probe.id:
            return decision.label
    return probe.subjects[0] if probe.subjects else probe.id


def _working_directory(body: dict[str, Any]) -> str:
    """The working directory Claude Code names in its system prompt, for an absolute file path."""
    text = json.dumps(body.get("system") or "") + json.dumps((body.get("messages") or [])[:1])
    for marker in ("Primary working directory: ", "Working directory: ", "working directory: "):
        at = text.find(marker)
        if at >= 0:
            rest = text[at + len(marker):]
            end = min((i for i in (rest.find("\\n"), rest.find('"'), rest.find("<")) if i >= 0), default=len(rest))
            return rest[:end].strip()
    return ""


def install_stub_claude(directory: Path) -> Path:
    """Write ``directory/claude``, a script that runs the stub CLI with this interpreter, and return
    the directory, to be put first on PATH."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "claude"
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{STUB_CLAUDE}" "$@"\n', encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return directory


# -- running the benchmark against the stubs -------------------------------------------------------

FAKE_KEY = "sk-ant-api03-PLANTED-bench-key-7f3a91c2"
HOME_NAME = "home-of-planted-user"


@dataclass
class BenchRun:
    code: int
    out: Path
    work: Path
    stub: StubModel
    printed: str
    commands: list[list[str]]  # the command line of every child process the test process started
    children: list[Any] = field(default_factory=list)  # those processes

    @property
    def manifest(self) -> dict[str, Any]:
        return json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))

    def lines(self, name: str) -> list[dict[str, Any]]:
        path = self.out / name
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def unit(self, arm: str, seed: int | None = None, repeat: int = 0) -> dict[str, Any]:
        return next(u for u in self.manifest["units"]
                    if u["arm"] == arm and u["repeat"] == repeat and (seed is None or u["seed"] == seed))


def bench_environment(monkeypatch: Any, tmp: Path, *, real_claude: bool = False) -> Path:
    """A temp HOME, no inherited credential or base URL, no managed settings read from this machine,
    and (unless ``real_claude``) the stub ``claude`` first on PATH. Returns the home directory."""
    home = tmp / HOME_NAME
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    for name in list(os.environ):
        if name.startswith("ANTHROPIC_"):
            monkeypatch.delenv(name)
    if not real_claude:
        monkeypatch.setenv("PATH", str(install_stub_claude(tmp / "bin")) + os.pathsep + os.environ["PATH"])
    from contexto.bench.arms import managed

    monkeypatch.setattr(managed, "sources", lambda platform=None: [])  # never this machine's managed settings
    return home


def run_bench(monkeypatch: Any, tmp: Path, scripts: list[Script], *, arms: tuple[str, ...], bound: int,
              stub: StubModel | None = None, credentials: Any = None, budget: int | None = 2_000_000_000,
              upstream: str | None = None, out: Path | None = None, **overrides: Any) -> BenchRun:
    """One ``runner.run`` against a ``StubModel`` (started here unless ``upstream`` names a running
    one), with everything it prints and every child command line captured."""
    import io
    import subprocess

    from contexto.bench import credentials as bench_credentials
    from contexto.bench import runner

    own = stub is None or upstream is None
    stub = stub or StubModel(scripts, key=FAKE_KEY)
    url = upstream or stub.start()
    commands: list[list[str]] = []
    children: list[Any] = []
    real_popen = subprocess.Popen

    class Recording(real_popen):  # type: ignore[misc,valid-type]
        def __init__(self, args: Any, *a: Any, **kw: Any) -> None:
            commands.append([str(x) for x in args] if isinstance(args, (list, tuple)) else [str(args)])
            super().__init__(args, *a, **kw)
            children.append(self)

    monkeypatch.setattr(subprocess, "Popen", Recording)
    credentials = credentials or bench_credentials.resolve(f"printf %s {FAKE_KEY}", None)
    first = scripts[0]
    config = runner.Config(
        model="claude-haiku-5-5", bound=bound, protocol=first.protocol, turn_kbytes=first.turn_size // 1_000,
        seed=first.seed, seeds=[s.seed for s in scripts], repeats=1, arms=arms, budget=budget,
        out=out or tmp / "out", upstream=url, credentials=credentials, work=tmp / "work", keep_work=True,
        turn_timeout=120.0)
    for name, value in overrides.items():
        setattr(config, name, value)
    printed_out, printed_err = io.StringIO(), io.StringIO()
    try:
        code = runner.run(config, {s.seed: s for s in scripts}, stdout=printed_out, stderr=printed_err)
    finally:
        monkeypatch.setattr(subprocess, "Popen", real_popen)
        if own:
            stub.stop()
    return BenchRun(code, config.out, config.work, stub, printed_out.getvalue() + printed_err.getvalue(), commands,
                    children)
