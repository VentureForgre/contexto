"""Full-context replay baseline for acceptance 2: one direct request with the whole local history.

After the H1 run, Claude Code's session JSONL in the run's isolated config directory holds everything
the request needs, and nothing leaves that private directory except the answers and counts:

- ``api-request-shape``: the system prompt and tools Claude Code sent;
- ``api-request``: the request params (model, betas, thinking, output config);
- ``api-request`` records as deltas (``base``, ``keep``, ``tail`` hashes) and ``api-request-blob``,
  ``user`` and ``assistant`` records: each request's messages, which the proxy never shortened.

The replay sends H1's probe-turn request as Claude Code built it, decoded from its request records
(the session-start system message, per-turn reminders, and every turn), straight to the upstream
(the owner's proxy), bypassing Contexto. Thinking blocks are stripped, so it is a no-replayed-thinking
baseline. Contexto's tools are removed and ``tool_choice`` is ``none``, so the model answers from the
context alone. ``context_management`` is dropped so nothing edits the context away.

The key comes from the seat's apiKeyHelper at call time, held in memory and sent only in the request
header; it is never written, logged, or printed. The first 401 or 403 stops the replay.
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acceptance import driver  # noqa: E402

CONTEXTO_TOOL_PREFIX = "mcp__contexto__"
ANTHROPIC_VERSION = "2023-06-01"
# An honest client name. urllib's default user agent is refused by some fronts (M0: Cloudflare 1010),
# and this harness never presents itself as Claude Code.
USER_AGENT = "contexto-acceptance-harness/0.1 (+VentureForgre/contexto)"
# How the key is presented: "x-api-key", "bearer" (Authorization: Bearer), or "both". Intake approved
# "both" with the honest user agent (2026-10-16 03:24Z): the owner's proxy refused direct requests
# with urllib's user agent (403) and with x-api-key alone (401), and accepted this set (200).
AUTH_HEADER = "both"


# Betas that mark the request as coming from Claude Code. Intake (2026-10-16): no direct harness send
# presents a Claude Code client marker; the functional betas the request needs stay.
CLIENT_MARKER_BETA_PREFIX = "claude-code-"


def harness_betas(betas: Any) -> list[str]:
    """The recorded betas a direct harness request may send: every string except claude-code-*."""
    return [b for b in betas or [] if isinstance(b, str) and not b.startswith(CLIENT_MARKER_BETA_PREFIX)]


def beta_headers(request: dict[str, Any]) -> dict[str, str]:
    betas = harness_betas(request.get("_betas"))
    return {"anthropic-beta": ",".join(betas)} if betas else {}


def auth_headers(key: str, auth: str | None = None) -> dict[str, str]:
    """The credential headers only; the key goes nowhere else."""
    mode = auth or AUTH_HEADER
    if mode == "bearer":
        return {"authorization": f"Bearer {key}"}
    if mode == "both":
        return {"x-api-key": key, "authorization": f"Bearer {key}"}
    return {"x-api-key": key}


class ReplayError(Exception):
    pass


def message_hash(message: dict[str, Any]) -> str:
    """How Claude Code 2.1.301 names a message in its request records: sha256 of sorted compact JSON."""
    canonical = json.dumps(message, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _known_messages(cc_records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Every message a request record can name, by hash: request blobs, user records, and assistant
    replies. Claude Code writes one assistant record per content block (thinking, then text) under
    the same message id, so consecutive records of one message are merged first, as it sent them.
    Parallel tool results are merged too (``_merged_tool_results``)."""
    known: dict[str, dict[str, Any]] = {}
    current_id, current = None, None

    def flush() -> None:
        if current is not None:
            known[message_hash(current)] = current

    for r in cc_records:
        kind = r.get("type")
        if kind == "api-request-blob" and isinstance(r.get("message"), dict):
            known[r["hash"]] = r["message"]
        if r.get("isSidechain") or not isinstance(r.get("message"), dict):
            continue
        if kind == "assistant":
            msg_id = r["message"].get("id")
            blocks = r["message"].get("content")
            blocks = blocks if isinstance(blocks, list) else [{"type": "text", "text": str(blocks or "")}]
            if current is not None and msg_id is not None and msg_id == current_id:
                current["content"] = current["content"] + blocks
            else:
                flush()
                current_id, current = msg_id, {"role": "assistant", "content": list(blocks)}
        elif kind == "user":
            flush()
            current_id, current = None, None
            m = r["message"]
            user = {"role": m.get("role"), "content": m.get("content")}
            known[message_hash(user)] = user
    flush()
    for h, merged in _merged_tool_results(cc_records):
        known.setdefault(h, merged)
    return known


def _tool_results_only(content: Any) -> bool:
    return isinstance(content, list) and bool(content) and all(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def _merged_tool_results(cc_records: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """The user messages Claude Code built from consecutive tool-result records, with their hashes.

    Claude Code writes each result of a parallel tool call as its own ``user`` record but sends them
    as one user message (live run #2, a probe turn's tool loop; the Opus run on 2.1.302: 31 of 182
    request hashes unresolved without the merge, 0 with it). A run is the
    consecutive non-sidechain ``user`` records whose content is only ``tool_result`` blocks; any
    other ``user`` or ``assistant`` record ends it, and records of other kinds (attachments, queue
    operations) do not. Every prefix of two or more records is returned, since a request may have
    been sent with part of a run. The caller only adds these, so a request still decodes only from
    hashes the CLI recorded."""
    out: list[tuple[str, dict[str, Any]]] = []
    blocks: list[Any] = []
    records = 0
    for r in cc_records:
        kind = r.get("type")
        if r.get("isSidechain") or kind not in ("user", "assistant") or not isinstance(r.get("message"), dict):
            continue
        content = r["message"].get("content")
        if kind == "user" and _tool_results_only(content):
            blocks, records = blocks + list(content), records + 1
            if records > 1:
                merged = {"role": r["message"].get("role"), "content": list(blocks)}
                out.append((message_hash(merged), merged))
        else:
            blocks, records = [], 0
    return out


def recorded_messages(cc_records: list[dict[str, Any]],
                      stop: Callable[[list[dict[str, Any]]], bool] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Each api-request's messages, decoded from Claude Code's delta records (``base`` request id,
    ``keep`` messages kept from it, ``tail`` message hashes), in order. With ``stop``, decoding ends
    after the first request whose messages satisfy it, so later records are never read. Raises
    ReplayError on a hash it cannot resolve in a request it decodes: no guessing."""
    known = _known_messages(cc_records)
    out: dict[str, list[dict[str, Any]]] = {}
    for r in cc_records:
        if r.get("type") != "api-request":
            continue
        base = r.get("base")
        if base and base not in out:
            raise ReplayError("a request record names a base request the JSONL does not hold")
        messages = list(out.get(base, []))[: int(r.get("keep") or 0)]
        for h in r.get("tail") or []:
            if h not in known:
                raise ReplayError("a request record names a message the JSONL does not hold")
            messages.append(known[h])
        out[r["id"]] = messages
        if stop is not None and stop(messages):
            break
    return out


def _has_text(message: dict[str, Any], text: str) -> bool:
    content = message.get("content")
    if isinstance(content, str):
        return content == text
    return any(isinstance(b, dict) and b.get("text") == text for b in content or [])


def build_request(cc_records: list[dict[str, Any]], probe_text: str, max_tokens: int) -> dict[str, Any]:
    """The replay request: H1's probe-turn request as Claude Code sent it (system and tools from its
    shape, params, and every message, including system messages and per-turn reminders), with
    thinking stripped from assistant messages. Raises ReplayError when the JSONL lacks a part."""
    shapes = {r.get("shapeHash"): r["shape"] for r in cc_records
              if r.get("type") == "api-request-shape" and isinstance(r.get("shape"), dict)}
    requests = [r for r in cc_records if r.get("type") == "api-request" and isinstance(r.get("params"), dict)]
    if not shapes or not requests:
        raise ReplayError("the session JSONL has no api-request-shape or api-request record")
    # Only up to the probe turn's first request: the probe turn's own tool loop after it is not part
    # of the baseline, and its assistant tool calls may not resolve (live H1 rerun, 2026-10-16).
    decoded = recorded_messages(cc_records, stop=lambda msgs: any(
        m.get("role") == "user" and _has_text(m, probe_text) for m in msgs))
    probe = next((r for r in requests if any(m.get("role") == "user" and _has_text(m, probe_text)
                                              for m in decoded.get(r["id"], []))), None)
    if probe is None:
        raise ReplayError("the probe turn is not in the session JSONL; the H1 run did not reach it")
    shape = shapes.get(probe.get("shapeHash"))
    if shape is None:
        raise ReplayError("the probe request's shape is not in the session JSONL")
    messages = []
    for m in decoded[probe["id"]]:
        if m.get("role") == "assistant":
            content = m.get("content") if isinstance(m.get("content"), list) else [{"type": "text", "text": m.get("content")}]
            kept = [b for b in content if isinstance(b, dict) and b.get("type") not in ("thinking", "redacted_thinking")]
            if not kept:
                raise ReplayError("an assistant message holds only thinking; nothing is left to replay")
            m = {"role": "assistant", "content": kept}
        messages.append(m)
    p = probe["params"]
    tools = [t for t in shape.get("tools") or [] if not str(t.get("name", "")).startswith(CONTEXTO_TOOL_PREFIX)]
    request: dict[str, Any] = {"model": p.get("model", driver.MODEL), "max_tokens": max_tokens,
                               "system": shape.get("system") or [], "messages": messages, "stream": True}
    if tools:
        request["tools"] = tools
        request["tool_choice"] = {"type": "none"}
    for key in ("thinking", "output_config"):
        if key in p:
            request[key] = p[key]
    # Claude Code may record thinking.display "updates" (a display beta). count_tokens accepts only
    # "summarized" or "omitted"; the display setting affects only what the response returns, so the
    # baseline uses "omitted" (intake, 2026-10-16 04:56Z) and labels the change.
    changes = {}
    thinking = request.get("thinking")
    if isinstance(thinking, dict) and thinking.get("display") not in (None, "summarized", "omitted"):
        changes["thinking.display"] = [thinking["display"], "omitted"]
        request["thinking"] = {**thinking, "display": "omitted"}
    request["_changes"] = changes
    request["_betas"] = harness_betas(p.get("betas"))
    return request


def summary(request: dict[str, Any]) -> dict[str, Any]:
    """Sizes and counts only."""
    body = json.dumps({k: v for k, v in request.items() if not k.startswith("_")}).encode("utf-8")
    return {"bytes": len(body), "messages": len(request["messages"]), "tools": len(request.get("tools") or []),
            "system_blocks": len(request["system"]), "max_tokens": request["max_tokens"],
            "thinking": (request.get("thinking") or {}).get("type"), "betas": len(request["_betas"])}


def parse_sse(lines: Any) -> dict[str, Any]:
    """Text, tool calls, usage, and stop reason from a Messages API event stream."""
    text: list[str] = []
    usage: dict[str, int] = {}
    stop_reason = None
    blocks: dict[str, int] = {}
    tools: dict[int, dict[str, Any]] = {}  # content block index -> {name, partial input JSON}
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
            block = event.get("content_block") or {}
            btype = str(block.get("type"))
            blocks[btype] = blocks.get(btype, 0) + 1
            if btype == "tool_use":
                tools[int(event.get("index") or 0)] = {"name": block.get("name"), "json": ""}
        elif kind == "content_block_delta" and (event.get("delta") or {}).get("type") == "input_json_delta":
            index = int(event.get("index") or 0)
            if index in tools:
                tools[index]["json"] += event["delta"].get("partial_json", "")
        elif kind == "content_block_delta" and (event.get("delta") or {}).get("type") == "text_delta":
            text.append(event["delta"].get("text", ""))
        elif kind == "message_delta":
            usage.update({k: v for k, v in (event.get("usage") or {}).items() if isinstance(v, int)})
            stop_reason = (event.get("delta") or {}).get("stop_reason", stop_reason)
        elif kind == "error":
            raise ReplayError(f"stream error: {(event.get('error') or {}).get('type')}")
    tool_uses = []
    for index in sorted(tools):
        try:
            tool_input = json.loads(tools[index]["json"] or "{}")
        except json.JSONDecodeError:
            tool_input = {"_unparsed_chars": len(tools[index]["json"])}
        tool_uses.append({"name": tools[index]["name"], "input": tool_input})
    return {"text": "".join(text), "usage": usage, "stop_reason": stop_reason, "blocks": blocks,
            "tool_uses": tool_uses}


def send(request: dict[str, Any], upstream: str, key: str, timeout: float = 900.0) -> dict[str, Any]:
    """POST the replay; the key goes only into the request header. Stops on 401 or 403."""
    body = json.dumps({k: v for k, v in request.items() if not k.startswith("_")}).encode("utf-8")
    headers = {"content-type": "application/json", "anthropic-version": ANTHROPIC_VERSION, **auth_headers(key),
               "user-agent": USER_AGENT,
               "accept": "text/event-stream"}
    headers.update(beta_headers(request))  # never a claude-code-* beta, whatever the caller passed
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
    headers = {"content-type": "application/json", "anthropic-version": ANTHROPIC_VERSION, **auth_headers(key),
               "user-agent": USER_AGENT}
    headers.update(beta_headers(request))  # never a claude-code-* beta, whatever the caller passed
    req = urllib.request.Request(upstream.rstrip("/") + "/v1/messages/count_tokens", data=body, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            tokens = json.loads(resp.read() or b"{}").get("input_tokens")
    except urllib.error.HTTPError as exc:
        kind = _error_type(exc)  # the error's type field only; never the message or headers
        if exc.code in (401, 403):
            raise driver.AuthFailure(f"count_tokens returned {exc.code} ({kind}); replay stopped at the first "
                                     "auth error") from None
        raise ReplayError(f"count_tokens returned {exc.code} ({kind})") from None
    if not isinstance(tokens, int):
        raise ReplayError("count_tokens returned no input_tokens")
    return tokens


def _error_type(exc: urllib.error.HTTPError) -> str | None:
    try:
        return (json.loads(exc.read() or b"{}").get("error") or {}).get("type")
    except (json.JSONDecodeError, OSError, AttributeError):
        return None


def check_fits(input_tokens: int, max_tokens: int, context_window: int, approved: int) -> str | None:
    """Why the replay must not be sent, or None. Intake: no silent trimming."""
    if input_tokens + max_tokens > context_window:
        return (f"the replay needs {input_tokens:,} input + {max_tokens:,} output tokens, over the "
                f"{context_window:,}-token context window")
    if input_tokens + max_tokens > approved:
        return f"the replay could cost {input_tokens + max_tokens:,} tokens, over the approved {approved:,}"
    return None


def turn_requests(cc_records: list[dict[str, Any]], probe_texts: list[str], max_tokens: int) -> dict[str, Any]:
    """D27's symmetric baseline (intake, 2026-10-16 05:42Z): the parts of the k-th full-context request.

    The first probe turn's request is built as for the one-request baseline (same replay rules). Its
    messages before the first probe turn are the recorded history; the messages Claude Code sent after
    that probe turn's text (its per-turn reminder) are the tail repeated after every probe turn.
    ``baseline_request(parts, answers, k)`` assembles request k from them and the baseline's OWN
    answers to turns 1..k-1; H1's answers are never read (decoding stops at the first probe turn).
    """
    first = build_request(cc_records, probe_texts[0], max_tokens)
    at = next(i for i, m in enumerate(first["messages"]) if m.get("role") == "user" and _has_text(m, probe_texts[0]))
    return {"template": {k: v for k, v in first.items() if k != "messages"},
            "history": first["messages"][:at], "tail": first["messages"][at + 1:], "probe_texts": list(probe_texts)}


def baseline_request(parts: dict[str, Any], answers: list[str], k: int) -> dict[str, Any]:
    """Request k (1-based): the history, probe turns 1..k-1 each answered with ``answers`` (the
    baseline's own, in order), then probe turn k."""
    if len(answers) < k - 1:
        raise ReplayError(f"request {k} needs the baseline's answers to turns 1..{k - 1}")
    messages = list(parts["history"])
    for j in range(k):
        messages.append({"role": "user", "content": [{"type": "text", "text": parts["probe_texts"][j]}]})
        messages.extend(parts["tail"])
        if j < k - 1:
            messages.append({"role": "assistant", "content": [{"type": "text", "text": answers[j]}]})
    return {**parts["template"], "messages": messages}
