"""Recall experiment: send rebuilt probe-turn requests once each and read the first response.

Each input is a Messages API request body, H1's final probe-turn request as the proxy would send
it, rebuilt offline with one preamble variant (builder-1's variant builder). Per send, in order:
count_tokens on the exact body, refuse if it is above the per-send input limit or if spent plus its
worst case would pass the approval, then send once with ``max_tokens`` set to the experiment's
output cap. Only the first response is read; a tool call in it is recorded, never run.

The record keeps counts, tool names and inputs (synthetic page ids and queries), and whether a call
targets the page that holds the paged-only plants. It keeps no key and no request body. The key
comes from the seat's apiKeyHelper, held in memory; the first 401 or 403 stops the experiment.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acceptance import credcheck, driver, replay  # noqa: E402

INPUT_LIMIT = 110_000  # per send: intake raised it from 102,000 (2026-10-16 03:17Z)
OUTPUT_CAP = 8_000
APPROVED = 500_000


def _texts(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, list):
        return [t for x in obj for t in _texts(x)]
    if isinstance(obj, dict):
        return [t for k, v in obj.items() if k in ("text", "content", "messages", "system") for t in _texts(v)]
    return []


def strip_thinking(body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """A copy of ``body`` without thinking blocks in assistant messages, and how many were removed.
    Raises ValueError if an assistant message would be left empty."""
    out = json.loads(json.dumps(body))
    removed = 0
    for message in out.get("messages") or []:
        if message.get("role") != "assistant" or not isinstance(message.get("content"), list):
            continue
        kept = [b for b in message["content"] if not (isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"))]
        removed += len(message["content"]) - len(kept)
        if not kept:
            raise ValueError("an assistant message holds only thinking")
        message["content"] = kept
    return out, removed


def paged_answers(text: str, ids: tuple[str, ...] = ("q_p_01", "q_p_02", "q_p_03", "q_p_04")) -> dict[str, str]:
    """The answer lines for the paged-only probes (synthetic values)."""
    out = {}
    for line in text.splitlines():
        qid, sep, answer = line.partition(":")
        if sep and qid.strip() in ids:
            out[qid.strip()] = answer.strip()
    return out


def target_page_id(body: dict[str, Any], turn: int) -> str | None:
    """The id the request's own page index gives the paged turn ``turn`` (``pg_... turn N:``)."""
    pattern = re.compile(rf"\b(pg_[0-9a-f]+) turn {turn}:")
    for text in _texts(body.get("messages")):
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def targets(tool_use: dict[str, Any], page_id: str | None, page_text: str) -> bool:
    """Whether a tool call would reach the plant page: a recall of its id, or a search whose query
    is in its text (contexto_search is an exact substring match)."""
    name, args = str(tool_use.get("name") or ""), tool_use.get("input") or {}
    if name.endswith("contexto_recall"):
        return page_id is not None and args.get("page_id") == page_id
    if name.endswith("contexto_search"):
        query = args.get("query")
        return isinstance(query, str) and bool(query) and query in page_text
    return False


def run(bodies: list[tuple[str, dict[str, Any]]], upstream: str, key: str, betas: list[str], page_text: str,
        turn: int = 0, approved: int = APPROVED, input_limit: int = INPUT_LIMIT,
        output_cap: int = OUTPUT_CAP, strip: bool = False,
        estimates: dict[str, int] | None = None) -> dict[str, Any]:
    """Send each body once. With ``estimates`` (variant -> the builder's token estimate), the gate
    uses that estimate instead of count_tokens, for an upstream that does not serve count_tokens."""
    spent = 0
    sends: list[dict[str, Any]] = []
    stopped = None
    for name, body in bodies:
        record: dict[str, Any] = {"variant": name}
        if strip:
            body, record["thinking_blocks_stripped"] = strip_thinking(body)
        request = {**body, "max_tokens": output_cap, "stream": True, "_betas": betas}
        try:
            if estimates is not None:
                tokens = int(estimates[name])
                record["estimated_input"] = tokens
            else:
                tokens = replay.count_tokens(request, upstream, key)
                record["count_tokens"] = tokens
            if tokens > input_limit:
                stopped = f"{name}: {tokens:,} input tokens is above the per-send limit {input_limit:,}"
            elif spent + tokens + output_cap > approved:
                stopped = f"{name}: spent {spent:,} plus up to {tokens + output_cap:,} would pass {approved:,}"
            if stopped:
                record["stopped"] = stopped
                sends.append(record)
                break
            result = replay.send(request, upstream, key)
        except (replay.ReplayError, driver.AuthFailure, OSError) as exc:
            record["stopped"] = stopped = f"{name}: {exc}"
            sends.append(record)
            break
        usage = result["usage"]
        cost = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                     "cache_read_input_tokens", "output_tokens"))
        spent += cost
        page_id = target_page_id(body, turn)
        record.update({
            "status": result["status"], "stop_reason": result["stop_reason"], "usage": usage, "spent": cost,
            "blocks": result["blocks"], "answer_chars": len(result["text"]), "plant_page_id": page_id,
            "tool_uses": [{**t, "targets_plant_page": targets(t, page_id, page_text)} for t in result["tool_uses"]],
            "paged_only_answers": paged_answers(result["text"]),
            "unknown_answers": sum(1 for line in result["text"].splitlines()
                                   if ":" in line and line.split(":", 1)[1].strip().lower().startswith("unknown")),
        })
        sends.append(record)
    return {"sends": sends, "spent": spent, "stopped": stopped}


def diagnose_count_tokens(body: dict[str, Any], upstream: str, key: str, betas: list[str],
                          auth: str | None = None) -> dict[str, Any]:
    """One count_tokens call that records only its status, error type, and error message (the key,
    if the message ever echoed it, is redacted). For the owner's proxy's 403 (2026-10-16)."""
    request = {**body, "_betas": betas}
    data = json.dumps({k: request[k] for k in replay._COUNT_FIELDS if k in request}).encode("utf-8")
    headers = {"content-type": "application/json", "anthropic-version": replay.ANTHROPIC_VERSION,
               **replay.auth_headers(key, auth), "user-agent": replay.USER_AGENT,
               **replay.beta_headers(request)}  # D23: never a claude-code-* beta
    req = urllib.request.Request(upstream.rstrip("/") + "/v1/messages/count_tokens", data=data, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return {"status": resp.status, "input_tokens": json.loads(resp.read() or b"{}").get("input_tokens")}
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read() or b"{}").get("error") or {}
        except (json.JSONDecodeError, OSError):
            error = {}
        message = str(error.get("message") or "")
        message = (message.replace(key, "[redacted]") if key else message)[:500]  # redact first, then truncate
        return {"status": exc.code, "error_type": error.get("type"), "message": message}


def main(argv: list[str]) -> int:
    """experiment.py [--strip-thinking] [--estimates SUMMARY] <seat settings> <upstream> <H1 run dir> <out json>
    <variant.json>...   (--estimates: the variant builder's summary.json; its probe.estimate_tokens gates each
    send instead of count_tokens, and a variant file is named after its variant, e.g. V1.json)"""
    strip = "--strip-thinking" in argv
    argv = [a for a in argv if a != "--strip-thinking"]
    estimates = None
    if "--estimates" in argv:
        i = argv.index("--estimates")
        summary = json.loads(Path(argv[i + 1]).read_text(encoding="utf-8"))
        estimates = {v["variant"]: int(v["probe"]["estimate_tokens"]) for v in summary.get("variants", [])}
        argv = argv[:i] + argv[i + 2:]
    if len(argv) < 5:
        print(main.__doc__, file=sys.stderr)
        return 2
    seat, upstream, run_dir, out = Path(argv[0]), argv[1], Path(argv[2]), Path(argv[3])
    bodies = [(Path(p).stem, json.loads(Path(p).read_text(encoding="utf-8"))) for p in argv[4:]]
    cc = sorted(run_dir.glob("claude-config/projects/*/*.jsonl"))
    if not seat.is_file() or not cc:
        print("refusing: the seat settings file or the H1 run's Claude Code JSONL is missing", file=sys.stderr)
        return 2
    params = [json.loads(line).get("params") for line in cc[0].read_text(encoding="utf-8").splitlines()
              if '"api-request"' in line]
    betas = [b for b in (params[-1] or {}).get("betas") or [] if isinstance(b, str)] if params else []
    pages = [json.loads(p.read_text(encoding="utf-8")) for p in run_dir.glob("home/sessions/*/pages/*.json")]
    plant = [p for p in pages if p.get("turn") == 0]
    page_text = "\n".join(_texts(plant[0]["messages"])) if plant else ""
    try:
        key = credcheck._helper_output(seat)  # in memory; request headers only
    except (RuntimeError, OSError):
        print("refusing: the seat's apiKeyHelper did not produce a key; output not shown", file=sys.stderr)
        return 2
    try:
        result = run(bodies, upstream, key, betas, page_text, strip=strip, estimates=estimates)
    finally:
        del key
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if not result["stopped"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

