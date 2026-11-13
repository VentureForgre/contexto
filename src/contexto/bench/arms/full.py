"""The ``full`` arm: direct Messages API requests that carry the whole script. No Claude Code.

Every request is built from the script alone, never from another arm's transcript, so the arm is
independent and a reader can rebuild each request from ``scripts/seed-<n>.json``:

- a one-line system prompt (``SYSTEM``);
- every content turn as a user message with the scripted assistant reply (``Script.scripted_reply``:
  ``OK``, or the right ``DECISION`` line);
- the tool-loop turn as the file reads the turn asks for: for each file in ``Script.reads`` an
  assistant ``tool_use`` of the one declared tool and a user ``tool_result`` holding the file's text,
  then ``OK``. With such a turn the request declares that tool and sets ``tool_choice`` to ``none``;
- a cache breakpoint on the last block of that history, so later requests can read it;
- one request per probe turn, in order: request k carries probe turns 1 to k-1 with this arm's own
  answers to them, then probe turn k (the symmetric form of D27).

Before a request is sent its exact input size is asked from ``count_tokens``. If it does not fit the
model's context window, or the first request does not fit what is left of the budget, nothing is
sent and the unit is ``not_run`` with the reason. A 429, a 529, a 5xx or a stream that ends early is
retried, at most ``RETRIES`` times with a doubling wait; any other 4xx is never retried.

The requests name an honest client (``contexto-bench``), never Claude Code, and send no thinking
parameter and no beta header: the model's API defaults apply. That is one of the ways this arm
differs from the two Claude Code arms, whose requests carry Claude Code's own system prompt, tools,
thinking settings and betas.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from typing import Any

from contexto.bench.arms import Stop, Unit, UnitResult, ledger_stop

SYSTEM = "You are a careful assistant in a long working session. Answer from this conversation only."
MAX_TOKENS = 8_192
ANTHROPIC_VERSION = "2023-06-01"
READ_TOOL = {"name": "read_file", "description": "Read one file from the working directory and return its text.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}
# Published context windows, in tokens (platform.claude.com/docs/en/about-claude/models/overview, read
# 2026-11-12): each of these models has a 1,000,000-token window with no beta header.
CONTEXT_WINDOWS = {"claude-haiku-5-5": 1_000_000, "claude-sonnet-5-5": 1_000_000, "claude-opus-5-5": 1_000_000,
                   "claude-fable-5-2": 1_000_000}
DEFAULT_CONTEXT_WINDOW = 200_000  # a model this table does not know: the smaller window, so nothing doomed is sent
COUNTED_FIELDS = ("model", "system", "messages", "tools", "tool_choice")  # what count_tokens is sent
RETRIES = 4
BACKOFF_S = 2.0  # the first wait; it doubles, up to BACKOFF_MAX_S
BACKOFF_MAX_S = 60.0
REQUEST_TIMEOUT_S = 900.0


def user_agent() -> str:
    from contexto import __version__

    return f"contexto-bench/{__version__} (+https://github.com/VentureForgre/contexto)"


def context_window(model: str, override: int | None = None) -> int:
    return override or CONTEXT_WINDOWS.get(model, DEFAULT_CONTEXT_WINDOW)


def _text(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text}]


def history(script: Any, cache_ttl: str = "5m") -> list[dict[str, Any]]:
    """The messages of the content turns, with the cache breakpoint on the last block."""
    messages: list[dict[str, Any]] = []
    for index, kind in enumerate(script.kinds):
        if kind == "probe":
            continue
        messages.append({"role": "user", "content": _text(script.turns[index])})
        if kind == "toolloop":
            for n, path in enumerate(script.reads):
                call = f"toolu_bench_{n:02d}"
                messages.append({"role": "assistant", "content": [
                    {"type": "tool_use", "id": call, "name": READ_TOOL["name"], "input": {"path": path}}]})
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": call, "content": script.files[path]}]})
        messages.append({"role": "assistant", "content": _text(script.scripted_reply(index) or "OK")})
    if messages:
        mark = {"type": "ephemeral", "ttl": "1h"} if cache_ttl == "1h" else {"type": "ephemeral"}
        messages[-1]["content"][-1]["cache_control"] = mark
    return messages


def request(script: Any, model: str, base: list[dict[str, Any]], answers: list[str], k: int) -> dict[str, Any]:
    """Request ``k`` (0-based): the history, probe turns 0..k-1 with ``answers``, then probe turn ``k``."""
    probes = script.probe_texts
    messages = list(base)
    for j in range(k + 1):
        messages.append({"role": "user", "content": _text(probes[j])})
        if j < k:
            messages.append({"role": "assistant", "content": _text(answers[j] or "(no answer)")})
    body: dict[str, Any] = {"model": model, "max_tokens": MAX_TOKENS, "system": _text(SYSTEM), "messages": messages,
                            "stream": True}
    if script.reads:
        body["tools"] = [READ_TOOL]
        body["tool_choice"] = {"type": "none"}
    return body


def _error_type(exc: urllib.error.HTTPError) -> str:
    """An error response's ``error.type``: an enum, never its message."""
    try:
        kind = (json.loads(exc.read() or b"{}").get("error") or {}).get("type")
    except (ValueError, OSError, AttributeError):
        kind = None
    return kind if isinstance(kind, str) and kind.replace("_", "").isalnum() and len(kind) <= 40 else "unknown"


def _post(unit: Unit, path: str, body: dict[str, Any]) -> Any:
    headers = {"content-type": "application/json", "anthropic-version": ANTHROPIC_VERSION,
               "user-agent": user_agent(), "accept": "text/event-stream" if body.get("stream") else "application/json",
               **unit.credentials.headers()}
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(unit.upstream.rstrip("/") + path, data=data, headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S)  # noqa: S310 - the unit's meter on 127.0.0.1


def _parse_stream(response: Any) -> tuple[str, str | None]:
    """The text and stop reason of a streamed reply. Raises ``ValueError`` if the stream ended early."""
    text: list[str] = []
    stop_reason, ended = None, False
    for raw in response:
        line = raw.decode("utf-8", "replace")
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:])
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "content_block_delta" and (event.get("delta") or {}).get("type") == "text_delta":
            text.append(event["delta"].get("text", ""))
        elif kind == "message_delta":
            stop_reason = (event.get("delta") or {}).get("stop_reason", stop_reason)
        elif kind == "message_stop":
            ended = True
        elif kind == "error":
            raise ValueError("the stream carried an error event")
    if not ended:
        raise ValueError("the stream ended early")
    return "".join(text), stop_reason


def _retrying(unit: Unit, path: str, body: dict[str, Any], what: str) -> Any:
    """POST with the retry rule; returns the parsed reply (count: int; message: (text, stop reason))."""
    wait = BACKOFF_S
    for attempt in range(RETRIES + 1):
        problem: str
        try:
            with _post(unit, path, body) as response:
                if body.get("stream"):
                    return _parse_stream(response)
                tokens = json.loads(response.read() or b"{}").get("input_tokens")
                if not isinstance(tokens, int):
                    raise Stop("api_error", f"{what}: the upstream's answer had no input_tokens")
                return tokens
        except urllib.error.HTTPError as exc:
            kind = _error_type(exc)
            if (stop := ledger_stop(unit)) is not None:
                raise stop from None
            if exc.code != 429 and exc.code != 529 and exc.code < 500:
                raise Stop(f"http_{exc.code}", f"{what}: the upstream answered {exc.code} ({kind})") from None
            problem = f"the upstream answered {exc.code} ({kind})"
            retry_after = exc.headers.get("retry-after") if exc.headers else None
            if retry_after and retry_after.isdecimal():
                wait = max(wait, float(retry_after))
        except (ValueError, OSError, http.client.HTTPException) as exc:
            if (stop := ledger_stop(unit)) is not None:
                raise stop from None
            problem = f"the response did not arrive whole ({type(exc).__name__})"
        if attempt == RETRIES:
            raise Stop("api_error", f"{what}: {problem}, {RETRIES + 1} times in a row")
        time.sleep(min(wait, BACKOFF_MAX_S))
        wait *= 2
    raise AssertionError("unreachable")


def run(unit: Unit) -> UnitResult:
    script = unit.script
    result = UnitResult(compactions=None, toolloop_reads=None)
    base = history(script, unit.cache_ttl)
    window = context_window(unit.model, unit.context_window)
    probe_turns = [i for i, k in enumerate(script.kinds) if k == "probe"]
    answers: list[str] = []
    try:
        for k, turn in enumerate(probe_turns):
            unit.meter.turn = turn
            body = request(script, unit.model, base, answers, k)
            what = f"probe turn {k + 1}"
            counted = {name: body[name] for name in COUNTED_FIELDS if name in body}
            tokens = _retrying(unit, "/v1/messages/count_tokens", counted, f"{what}, count_tokens")
            if tokens + MAX_TOKENS > window:
                raise Stop("too_large", f"{what} needs {tokens:,} input and up to {MAX_TOKENS:,} output tokens, which "
                                        f"does not fit the model's context window of {window:,} tokens")
            left = unit.meter.ledger.remaining
            if left is not None and tokens + MAX_TOKENS > left:
                raise Stop("budget", f"{what} could cost {tokens + MAX_TOKENS:,} tokens, more than the "
                                     f"{max(0, left):,} left of the budget")
            text, stop_reason = _retrying(unit, "/v1/messages", body, what)
            if stop_reason == "max_tokens":
                raise Stop("api_error", f"{what}: the reply was cut off at max_tokens ({MAX_TOKENS:,})")
            answers.append(text)
            result.answers[turn] = text
            result.probe_turns_completed += 1
            result.turns_completed += 1
    except Stop as stop:
        # Nothing was answered yet and the request did not fit, or the upstream refused it outright.
        if not answers and stop.code in ("too_large", "budget", "http_400", "http_413"):
            result.status, result.stop, result.reason = "not_run", None, stop.reason
            return result
        if stop.code == "budget":
            unit.meter.ledger.halt("budget")
        code = "auth" if stop.code in ("http_401", "http_403") else stop.code
        result.stopped(code if not code.startswith("http_") and code != "too_large" else "api_error", stop.reason)
    return result
