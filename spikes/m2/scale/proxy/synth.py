"""Deterministic synthetic Claude Code-shaped Messages API traffic for the proxy scale harness.

Zero spend: nothing here talks to a network. The shapes follow what the repository has measured:

- ``system`` + ``tools`` head of ``head_tokens`` (live run #3's first request was 50,906 tokens with the
  first message; the task's range is 20,000 to 90,000).
- ``messages[0]`` is a user message with a ``<system-reminder>`` context block and then the prompt, and
  ``messages[1]`` is a role ``system`` message of about 8,470 characters with ``output_config`` (finding 1 of
  spikes/m2/carried/README.md).
- Every turn is a turn-start user message, then 0 to 4 tool rounds (assistant thinking + text + tool_use,
  then a user tool_result), then a final assistant message with thinking and text. Turns average
  ``turn_tokens`` (live run #3 averaged about 26,000 tokens per page) with +/-25 percent spread.
- Tokens are bytes / ``BYTES_PER_TOKEN`` (1.83; run #3 fitted 1.8274), the same rule the fake upstream
  uses for usage, so "history tokens" and "upstream tokens" are on one scale.

Reminder profiles for later turn-start messages (context blocks are carried, never paged, rule 13):

- ``first``: only ``messages[0]`` has a context block. This is the shape spikes/m2/carried measured on
  Claude Code 2.1.301 without IDE integration ("context blocks stay at messages[0]").
- ``mixed`` [assumed, not measured]: additionally 35 percent of later turn starts carry a todo-style
  reminder (420 bytes) and 10 percent a file-modified reminder with a snippet (1,500 bytes), the kinds of
  ``<system-reminder>`` blocks Claude Code adds with todos and IDE file watching. Expected 297 bytes,
  about 162 tokens, per turn.

The wire form is compact JSON in the CLI's key order (``serialize``), with ``cache_control`` on the
system blocks and on the last block of the newest message only, moving each request as the CLI does.
"""

from __future__ import annotations

import base64
import json
import math
import random
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

BYTES_PER_TOKEN = 1.83
SESSION_START_CHARS = 8_470
CACHE = {"type": "ephemeral"}
TOOLS = ("Read", "Bash", "Grep", "Edit", "Glob", "Write")

_WORDS = (
    "the a of to and in is for on with that this it as be are from by at or an not we can will def "
    "return self value result config request message session store page turn window bound token cache "
    "proxy engine rotate index epoch state error retry stream event usage header body json bytes count "
    "if else elif for while try except finally import class lambda yield async await None True False "
    "int str dict list tuple set len range print open path file line data item key name type id "
    "src/contexto/rewrite.py src/contexto/proxy.py tests/test_rewrite.py docs/notes.md ./build/out.log "
    "0 1 2 3 4 5 6 7 8 9 10 42 100 255 1024 65536 0x1f 3.14 -1 == != <= >= += -> => :: {} [] () ; , ."
).split()


@dataclass(frozen=True)
class Shape:
    head_tokens: int = 50_000
    turn_tokens: int = 26_000
    first_reminder_tokens: int = 3_000
    reminders: str = "first"  # "first" or "mixed"
    seed: int = 20261017
    model: str = "claude-haiku-5-5"

    def __post_init__(self) -> None:
        if self.reminders not in ("first", "mixed"):
            raise ValueError("reminders must be 'first' or 'mixed'")
        if self.head_tokens < 2_000 or self.turn_tokens < 2_000:
            raise ValueError("head_tokens and turn_tokens must be at least 2,000")


def tokens_of(nbytes: int) -> int:
    """The harness's one token rule: bytes / BYTES_PER_TOKEN, rounded up."""
    return math.ceil(nbytes / BYTES_PER_TOKEN) if nbytes else 0


def serialize(obj: Any) -> bytes:
    """The CLI's wire form (and the proxy's ``serialize_request``): insertion order, compact, UTF-8."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def text(rng: random.Random, nbytes: int, *, code: bool = False) -> str:
    """About ``nbytes`` of word salad (exactly ``nbytes`` characters, ASCII). ``code`` adds ``key = value``
    lines so the index's salient-identifier scan has work like real tool output gives it."""
    if nbytes <= 0:
        return ""
    words = rng.choices(_WORDS, k=nbytes // 4 + 8)
    lines, line = [], []
    for i, w in enumerate(words):
        line.append(w)
        if len(line) >= 11:
            if code and i % 3 == 0:
                lines.append(f"{line[0]}_{i % 997} = {' '.join(line[1:])}")
            else:
                lines.append(" ".join(line))
            line = []
    lines.append(" ".join(line))
    out = "\n".join(lines)
    while len(out) < nbytes:
        out += "\n" + out
    return out[:nbytes]


def head(shape: Shape) -> dict[str, Any]:
    """The request fields other than ``messages``, in Claude Code's key order, sized to ``head_tokens``."""
    rng = random.Random(f"{shape.seed}:head")
    target = int(shape.head_tokens * BYTES_PER_TOKEN)
    system_bytes = int(target * 0.35)
    system = [
        {"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.301; cc_entrypoint=cli",
         "cache_control": CACHE},
        {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude.\n"
         + text(rng, system_bytes), "cache_control": CACHE},
    ]
    tools = []
    per_tool = max(400, (target - system_bytes) // 40)
    for i in range(40):
        name = TOOLS[i] if i < len(TOOLS) else f"mcp__server{i // 8}__tool_{i}"
        tools.append({"name": name, "description": text(rng, per_tool - 445),
                      "input_schema": {"type": "object", "properties": {
                          "file_path": {"type": "string", "description": "Absolute path to the file"},
                          "command": {"type": "string", "description": "The command to run"},
                          "pattern": {"type": "string", "description": "A regular expression"}},
                          "required": ["file_path"], "additionalProperties": False,
                          "$schema": "http://json-schema.org/draft-07/schema#"}})
    return {"model": shape.model, "system": system, "tools": tools,
            "metadata": {"user_id": "user_scale_harness_session_0000"},
            "max_tokens": 32_000, "thinking": {"type": "enabled", "budget_tokens": 31_999}, "stream": True}


def _reminder(body: str) -> dict[str, Any]:
    return {"type": "text", "text": f"<system-reminder>\n{body}\n</system-reminder>"}


def _turn_reminders(shape: Shape, rng: random.Random, t: int) -> list[dict[str, Any]]:
    if t == 0:
        nbytes = int(shape.first_reminder_tokens * BYTES_PER_TOKEN)
        return [_reminder("As you answer the user's questions, you can use the following context:\n# claudeMd\n"
                          + text(rng, nbytes))]
    if shape.reminders != "mixed":
        return []
    out = []
    roll = rng.random()
    if roll < 0.35:
        out.append(_reminder("The TodoWrite tool hasn't been used recently. " + text(rng, 340)))
    if 0.35 <= roll < 0.45:
        out.append(_reminder(f"Note: src/mod_{t % 97}/file_{t}.py was modified, either by the user or by a "
                             "linter. Here are the relevant changes:\n" + text(rng, 1_380, code=True)))
    return out


def _assistant(rng: random.Random, nbytes: int, tool: tuple[str, str, str] | None) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        {"type": "thinking", "thinking": text(rng, 900),
         "signature": base64.b64encode(rng.randbytes(240)).decode("ascii")},
        {"type": "text", "text": text(rng, nbytes)},
    ]
    if tool is not None:
        name, tool_id, path = tool
        content.append({"type": "tool_use", "id": tool_id, "name": name,
                        "input": {"file_path": path} if name in ("Read", "Edit", "Write")
                        else {"command": f"rg -n turn_{tool_id[-6:]} {path}"}})
    return {"role": "assistant", "content": content}


def turn(shape: Shape, t: int) -> list[dict[str, Any]]:
    """Every message turn ``t`` adds to the history, in order. Deterministic in (shape, t)."""
    rng = random.Random(f"{shape.seed}:turn:{t}")
    size = int(shape.turn_tokens * BYTES_PER_TOKEN * rng.uniform(0.75, 1.25))
    rounds = rng.choices((0, 1, 2, 3, 4), weights=(1, 2, 3, 2, 2))[0]
    fixed = 2_600 + 1_750 * rounds  # prompt, thinking, signatures, tool_use and JSON framing (approximate)
    bulk = max(1_000, size - fixed)
    prompt = f"Turn {t}: please look at src/mod_{t % 97}/file_{t}.py and fix marker_{t:06d}. " + text(
        rng, 500 if rounds else bulk // 2)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [*_turn_reminders(shape, rng, t), {"type": "text", "text": prompt}]}]
    if t == 0:
        messages.append({"role": "system", "content": [{"type": "text", "text": text(rng, SESSION_START_CHARS)}],
                         "output_config": {"effort": "medium"}})
    for j in range(rounds):
        name = TOOLS[rng.randrange(len(TOOLS))]
        tool_id = f"toolu_{t:06d}{j:02d}"
        path = f"/repo/src/mod_{(t + j) % 97}/file_{t}_{j}.py"
        messages.append(_assistant(rng, 300, (name, tool_id, path)))
        messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                                      "content": text(rng, bulk // rounds, code=True)}]})
    messages.append(_assistant(rng, 600 if rounds else bulk // 2, None))
    return messages


def request_points(shape: Shape, t: int, messages: list[dict[str, Any]]) -> list[tuple[int, str]]:
    """The requests the CLI sends during turn ``t``: (messages of this turn included, reply kind). The
    first is the turn start (with turn 0's trailing system message); one more after each tool_result."""
    first = 2 if t == 0 else 1
    points = [(first, "tool_use" if len(messages) > first + 1 else "end_turn")]
    for i in range(first, len(messages)):
        m = messages[i]
        if m["role"] == "user":
            points.append((i + 1, "tool_use" if i + 2 < len(messages) else "end_turn"))
    return points


def with_cache_control(message: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``message`` with ``cache_control`` on its last block (the CLI's moving breakpoint)."""
    content = message["content"]
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    blocks = [dict(b) for b in content]
    blocks[-1]["cache_control"] = CACHE
    return {**message, "content": blocks}


def request(shape_head: dict[str, Any], history: list[dict[str, Any]], tail: list[dict[str, Any]]) -> dict[str, Any]:
    """One request dict: the head, then ``history + tail`` with the newest message's cache breakpoint."""
    messages = [*history, *tail]
    messages[-1] = with_cache_control(messages[-1])
    return {"model": shape_head["model"], "messages": messages,
            **{k: v for k, v in shape_head.items() if k != "model"}}


class WireBuilder:
    """Builds request bodies from per-message serialized bytes, so a 50 MB body is a join, not a dump.
    ``body(tail)`` is byte-identical to ``serialize(request(head, history, tail))``."""

    def __init__(self, shape_head: dict[str, Any]) -> None:
        self.prefix = b'{"model":' + serialize(shape_head["model"]) + b',"messages":['
        rest = serialize({k: v for k, v in shape_head.items() if k != "model"})
        self.suffix = b"]," + rest[1:]
        self.history: list[bytes] = []
        self.history_bytes = 0

    def extend(self, messages: list[dict[str, Any]]) -> None:
        for m in messages:
            data = serialize(m)
            self.history.append(data)
            self.history_bytes += len(data) + 1

    def body(self, tail: list[dict[str, Any]]) -> bytes:
        parts = [*self.history, *(serialize(m) for m in tail[:-1]), serialize(with_cache_control(tail[-1]))]
        return self.prefix + b",".join(parts) + self.suffix


def turns(shape: Shape, start: int = 0) -> Iterator[tuple[int, list[dict[str, Any]]]]:
    t = start
    while True:
        yield t, turn(shape, t)
        t += 1
