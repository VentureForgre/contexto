"""Full-context replay baseline for acceptance 2: one direct request with the whole local history.

After the H1 run, Claude Code's session JSONL in the run's isolated config directory holds everything
the request needs, and nothing leaves that private directory except the answers and counts:

- ``api-request-shape``: the system prompt and tools Claude Code sent;
- ``api-request``: the request params (model, betas, thinking, output config);
- ``user`` and ``assistant`` records: the local history, which the proxy never shortened.

The replay sends the history up to the probe turn, then the probe turn, straight to the upstream
(the owner's proxy), bypassing Contexto. Thinking blocks are stripped, so it is a no-replayed-thinking
baseline. Contexto's tools are removed and ``tool_choice`` is ``none``, so the model answers from the
context alone. ``context_management`` is dropped so nothing edits the context away.

The key comes from the seat's apiKeyHelper at call time, held in memory and sent only in the request
header; it is never written, logged, or printed. The first 401 or 403 stops the replay.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acceptance import driver  # noqa: E402

CONTEXTO_TOOL_PREFIX = "mcp__contexto__"
ANTHROPIC_VERSION = "2023-06-01"


class ReplayError(Exception):
    pass


def _strip_assistant(content: Any) -> list[dict]:
    blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content or "")}]
    kept = [b for b in blocks if isinstance(b, dict) and b.get("type") not in ("thinking", "redacted_thinking")]
    return kept or [{"type": "text", "text": "OK"}]


def _content_list(content: Any) -> list[dict]:
    return content if isinstance(content, list) else [{"type": "text", "text": str(content or "")}]


def build_request(cc_records: list[dict[str, Any]], probe_text: str, max_tokens: int) -> dict[str, Any]:
    """The replay request: the head and params Claude Code recorded, the history before the probe
    turn, and the probe turn. Raises ReplayError when the JSONL lacks a part."""
    shapes = [r for r in cc_records if r.get("type") == "api-request-shape" and isinstance(r.get("shape"), dict)]
    params = [r for r in cc_records if r.get("type") == "api-request" and isinstance(r.get("params"), dict)]
    if not shapes or not params:
        raise ReplayError("the session JSONL has no api-request-shape or api-request record")
    shape, p = shapes[-1]["shape"], params[-1]["params"]

    messages: list[dict[str, Any]] = []
    probe_seen = False
    for r in cc_records:
        if r.get("type") not in ("user", "assistant") or r.get("isSidechain") or r.get("isMeta"):
            continue
        msg = r.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "user":
            blocks = _content_list(msg.get("content"))
            if any(isinstance(b, dict) and b.get("text") == probe_text for b in blocks):
                probe_seen = True
                break
        elif role == "assistant":
            blocks = _strip_assistant(msg.get("content"))
        else:
            continue
        if messages and messages[-1]["role"] == role:  # Claude Code can split one turn over records
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": list(blocks)})
    if not probe_seen:
        raise ReplayError("the probe turn is not in the session JSONL; the H1 run did not reach it")
    if messages and messages[-1]["role"] == "user":
        raise ReplayError("the history before the probe turn ends without an assistant reply")
    messages.append({"role": "user", "content": [{"type": "text", "text": probe_text}]})

    tools = [t for t in shape.get("tools") or [] if not str(t.get("name", "")).startswith(CONTEXTO_TOOL_PREFIX)]
    request: dict[str, Any] = {"model": p.get("model", driver.MODEL), "max_tokens": max_tokens,
                               "system": shape.get("system") or [], "messages": messages, "stream": True}
    if tools:
        request["tools"] = tools
        request["tool_choice"] = {"type": "none"}
    for key in ("thinking", "output_config"):
        if key in p:
            request[key] = p[key]
    request["_betas"] = [b for b in p.get("betas") or [] if isinstance(b, str)]
    return request


def summary(request: dict[str, Any]) -> dict[str, Any]:
    """Sizes and counts only."""
    body = json.dumps({k: v for k, v in request.items() if not k.startswith("_")}).encode("utf-8")
    return {"bytes": len(body), "messages": len(request["messages"]), "tools": len(request.get("tools") or []),
            "system_blocks": len(request["system"]), "max_tokens": request["max_tokens"],
            "thinking": (request.get("thinking") or {}).get("type"), "betas": len(request["_betas"])}


def parse_sse(lines: Any) -> dict[str, Any]:
    """Text, usage, and stop reason from a Messages API event stream."""
    text: list[str] = []
    usage: dict[str, int] = {}
    stop_reason = None
    blocks: dict[str, int] = {}
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
            btype = str((event.get("content_block") or {}).get("type"))
            blocks[btype] = blocks.get(btype, 0) + 1
        elif kind == "content_block_delta" and (event.get("delta") or {}).get("type") == "text_delta":
            text.append(event["delta"].get("text", ""))
        elif kind == "message_delta":
            usage.update({k: v for k, v in (event.get("usage") or {}).items() if isinstance(v, int)})
            stop_reason = (event.get("delta") or {}).get("stop_reason", stop_reason)
        elif kind == "error":
            raise ReplayError(f"stream error: {(event.get('error') or {}).get('type')}")
    return {"text": "".join(text), "usage": usage, "stop_reason": stop_reason, "blocks": blocks}


def send(request: dict[str, Any], upstream: str, key: str, timeout: float = 900.0) -> dict[str, Any]:
    """POST the replay; the key goes only into the request header. Stops on 401 or 403."""
    body = json.dumps({k: v for k, v in request.items() if not k.startswith("_")}).encode("utf-8")
    headers = {"content-type": "application/json", "anthropic-version": ANTHROPIC_VERSION, "x-api-key": key,
               "accept": "text/event-stream"}
    if request.get("_betas"):
        headers["anthropic-beta"] = ",".join(request["_betas"])
    req = urllib.request.Request(upstream.rstrip("/") + "/v1/messages", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = parse_sse(resp)
            result["status"] = resp.status
            return result
    except urllib.error.HTTPError as exc:
        error_type = None
        try:
            error_type = (json.loads(exc.read() or b"{}").get("error") or {}).get("type")
        except (json.JSONDecodeError, OSError):
            pass
        if exc.code in (401, 403):
            raise driver.AuthFailure(f"upstream returned {exc.code}; replay stopped at the first auth error") from None
        raise ReplayError(f"upstream returned {exc.code} ({error_type})") from None


CONTEXT_WINDOW = 1_000_000  # claude-haiku-5-5 (bundled claude-api models reference, 2026-10)
_COUNT_FIELDS = ("model", "system", "messages", "tools", "tool_choice", "thinking")


def count_tokens(request: dict[str, Any], upstream: str, key: str, timeout: float = 300.0) -> int:
    """Input tokens of the exact replay request, from ``/v1/messages/count_tokens`` (no output, no
    charge for tokens). Stops on 401 or 403 like ``send``."""
    body = json.dumps({k: request[k] for k in _COUNT_FIELDS if k in request}).encode("utf-8")
    headers = {"content-type": "application/json", "anthropic-version": ANTHROPIC_VERSION, "x-api-key": key}
    if request.get("_betas"):
        headers["anthropic-beta"] = ",".join(request["_betas"])
    req = urllib.request.Request(upstream.rstrip("/") + "/v1/messages/count_tokens", data=body, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            tokens = json.loads(resp.read() or b"{}").get("input_tokens")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise driver.AuthFailure(f"count_tokens returned {exc.code}; replay stopped at the first auth error") from None
        raise ReplayError(f"count_tokens returned {exc.code}") from None
    if not isinstance(tokens, int):
        raise ReplayError("count_tokens returned no input_tokens")
    return tokens


def check_fits(input_tokens: int, max_tokens: int, context_window: int, approved: int) -> str | None:
    """Why the replay must not be sent, or None. Intake: no silent trimming."""
    if input_tokens + max_tokens > context_window:
        return (f"the replay needs {input_tokens:,} input + {max_tokens:,} output tokens, over the "
                f"{context_window:,}-token context window")
    if input_tokens + max_tokens > approved:
        return f"the replay could cost {input_tokens + max_tokens:,} tokens, over the approved {approved:,}"
    return None
