"""Cache-aware rotation, offline (packet A28): replay a recorded run's requests through the engine under
candidate configurations and model what each would send, cache, and cost. Zero spend: nothing is sent.

    uv run python spikes/m2/acceptance/cache_rotation.py codex <codex session dir> [--rate B] [--json out]
    uv run python spikes/m2/acceptance/cache_rotation.py claude <run dir> <session id> [--rate B] [--json out]

Requests. Codex (Responses): each request's ``input`` is rebuilt from Codex's own rollout
(``codex-home/sessions/**/rollout-*.jsonl``): every response item before the request's first output
item, without the rollout-only ``internal_chat_message_metadata_passthrough`` field. The head (the
instructions, tools, and parameters, which the rollout does not keep) is a constant-size filler in
``instructions``, sized so request 1 has its logged ``original_request_bytes``; the check reports how
far the other requests are from theirs. Claude (Messages): lane B's ``classa.decode_requests`` and
the body shape of ``classa.original_bodies``, as builder-4's B27 replay does, for every request.

Engine. A fresh ``SessionStore`` under a temporary directory with a fresh key (run #3's ``page.key``
is not copied, per manager-1's ruling): page ids differ from the original run but keep their length,
so byte and token counts do not change. Usage fed back after each request: the run's measured input
plus the byte difference from the baseline replay at the run's own config, converted at a bytes-per-token
rate (builder-4's B27 method), so a candidate's decisions see what it would have sent. The default is
the run's average, head included; ``--rate`` sets a content rate (A27's calibration gives 3.0193 on
Codex A22; B27 used 1.8274 on Claude run #3). The differences are conversation content, so the
content rate is the better estimate; the document reports both as a range.

Cache model (an assumption, stated, and checked against both runs): checkpoints. A request reads from
cache the whole prompt of the longest earlier request of the same replay that is a byte prefix of it
(at that request's tokens), else the head alone (tools and system or instructions) when the head is
unchanged, at the run's measured head tokens: what each run read after every rotation (9,088 on Codex
A22, 24,601 on Claude run #3). Prompts are compared in the order the provider caches them (head first,
then the conversation), not in JSON key order. Both runs' measured reads fit this, not a longest
shared prefix: after a rotation they read the head only, though the pinned part of the preamble
matched earlier requests. The check rows compare the model with the measured cached tokens.

Output is numbers only: sizes, token counts, rotation counts and reasons, rooms, and costs.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contexto.rewrite import Config, RewriteEngine, _drop_cache_control  # noqa: E402
from contexto.store import SessionStore  # noqa: E402
from contexto.store import pages as store_pages  # noqa: E402

# USD per million tokens, docs/research/M0-cost-model.md, "Prices" table (vendor-documented 2026-10-15).
# Every uncached input token is counted as a cache write (an upper bound for Codex, where writes are
# not billed separately in the logged usage). Long-context multipliers as in that table.
PRICES = {
    "claude-haiku-5-5": {"input": 0.1, "output": 0.5, "read": 0.01, "write": 0.125,
                         "long_at": 100_000, "long_in": 5.0, "long_out": 5.0},
    "gpt-6-astra": {"input": 10.0, "output": 50.0, "read": 1.0, "write": 12.5,
                    "long_at": 272_000, "long_in": 2.0, "long_out": 1.5},
}
ROLLOUT_ONLY = "internal_chat_message_metadata_passthrough"
OUTPUT_ITEMS = ("reasoning",)  # plus assistant messages and every *_call item


def ser(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "backslashreplace")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _is_output(item: dict[str, Any]) -> bool:
    kind = str(item.get("type") or "")
    return kind in OUTPUT_ITEMS or kind.endswith("_call") or (kind == "message" and item.get("role") == "assistant")


def codex_inputs(rollout: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Each request's input items, rebuilt from the rollout: the items before its first output item."""
    history: list[dict[str, Any]] = []
    inputs: list[list[dict[str, Any]]] = []
    snapshot: list[dict[str, Any]] | None = None
    for record in rollout:
        if record.get("type") == "response_item":
            item = {k: v for k, v in record["payload"].items() if k != ROLLOUT_ONLY}
            if _is_output(item) and snapshot is None:
                snapshot = list(history)
            history.append(item)
        elif record.get("type") == "token_usage_record":
            inputs.append(snapshot if snapshot is not None else list(history))
            snapshot = None
    return inputs


def _requests(log: list[dict[str, Any]], suffix: str) -> list[dict[str, Any]]:
    return [e for e in log if e.get("event") == "request" and str(e.get("path") or "").endswith(suffix)
            and isinstance(e.get("usage"), dict)]


def restarts(log: list[dict[str, Any]], suffix: str) -> set[int]:
    """Indices (0-based, among the logged requests) of each request that is the first after a
    ``proxy_start``: a new proxy process, whose engine starts with no epoch state."""
    out, n, fresh = set(), 0, False
    for e in log:
        if e.get("event") == "proxy_start":
            fresh = True
        elif e.get("event") == "request" and str(e.get("path") or "").endswith(suffix) and isinstance(e.get("usage"), dict):
            if fresh and n:
                out.add(n)
            fresh = False
            n += 1
    return out


def codex_run(session_dir: Path) -> dict[str, Any]:
    log = read_jsonl(session_dir / "log.jsonl")
    logged = _requests(log, "/responses")
    rollout = read_jsonl(next((session_dir / "codex-home" / "sessions").rglob("rollout-*.jsonl")))
    inputs = codex_inputs(rollout)
    if len(inputs) != len(logged):
        raise ValueError(f"rebuilt {len(inputs)} requests, logged {len(logged)}")
    model = logged[0].get("model") or "gpt-6-astra"
    shell = {"model": model, "instructions": "", "input": [], "stream": True}
    filler = logged[0]["original_request_bytes"] - len(ser({**shell, "input": inputs[0]}))
    bodies = [{**shell, "instructions": "h" * filler, "input": items} for items in inputs]
    measured = []
    for e in logged:
        u = e["usage"]
        total = int(u.get("input_tokens") or 0)
        cached = int((u.get("input_tokens_details") or {}).get("cached_tokens") or 0)
        measured.append({"input": total, "cached": cached, "output": int(u.get("output_tokens") or 0)})
    return {"api": "responses", "endpoint": "/responses", "model": model, "bodies": bodies, "headers": [{}] * len(bodies),
            "logged": logged, "measured": measured, "restarts": restarts(log, "/responses"),
            "rebuild_offsets": [e["original_request_bytes"] - len(ser(b)) for e, b in zip(logged, bodies)]}


def claude_run(run_dir: Path, session: str) -> dict[str, Any]:
    from acceptance import classa  # noqa: PLC0415  (lane B's decoder; needs spikes/m0 on the path via script)

    session_dir = run_dir / "home" / "sessions" / session
    log = read_jsonl(session_dir / "log.jsonl")
    logged = _requests(log, "/v1/messages")
    cc = read_jsonl(next((run_dir / "claude-config" / "projects").glob("*/*.jsonl")))
    decoded = classa.decode_requests(cc, stop=lambda m: False)
    shapes = {r.get("shapeHash"): r["shape"] for r in cc if r.get("type") == "api-request-shape"}
    bodies, headers = [], []
    for r in (r for r in cc if r.get("type") == "api-request" and isinstance(r.get("params"), dict)):
        if r["id"] not in decoded:
            continue
        shape, p = shapes[r["shapeHash"]], r["params"]
        body = {"model": p["model"], "messages": [copy.deepcopy(m) for m in decoded[r["id"]]],
                "system": copy.deepcopy(shape["system"]), "tools": copy.deepcopy(shape["tools"]), "stream": True}
        for k in ("max_tokens", "thinking", "context_management", "output_config", "safeguards"):
            if k in p:
                body[k] = copy.deepcopy(p[k])
        for mark in r.get("cacheControl") or []:
            target = body
            for part in mark["path"]:
                target = target[part]
            if isinstance(target, dict):
                target["cache_control"] = mark["value"]
        for m in body["messages"]:
            c = m.get("content")
            if m.get("role") == "system" and isinstance(c, list) and len(c) == 1 and set(c[0]) == {"type", "text"}:
                m["content"] = c[0]["text"]
        bodies.append(body)
        headers.append({"anthropic-beta": ",".join(p.get("betas") or [])})
    if len(bodies) != len(logged):
        raise ValueError(f"rebuilt {len(bodies)} requests, logged {len(logged)}")
    measured = []
    for e in logged:
        u = e["usage"]
        read = int(u.get("cache_read_input_tokens") or 0)
        total = read + int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
        measured.append({"input": total, "cached": read, "output": int(u.get("output_tokens") or 0)})
    return {"api": "messages", "endpoint": "/v1/messages", "model": logged[0].get("model") or "claude-haiku-5-5",
            "bodies": bodies, "headers": headers, "logged": logged, "measured": measured,
            "restarts": restarts(log, "/v1/messages"),
            "rebuild_offsets": [e["original_request_bytes"] - len(ser(b)) for e, b in zip(logged, bodies)]}


def bytes_per_token(run: dict[str, Any]) -> float:
    """The run's own ratio of sent bytes to measured input tokens, over all requests."""
    return sum(e["request_bytes"] for e in run["logged"]) / sum(m["input"] for m in run["measured"])


def cache_view(api: str, body: dict[str, Any]) -> tuple[bytes, int]:
    """The request in the order the provider caches it, and the byte length of its head. Cache markers
    (``cache_control``) are left out: Claude Code moves them to the newest message on every request,
    and they are not part of the cached prompt. A message whose content is one text block is compared
    as its text: Claude Code sends the newest system message as a cache-marked one-block list and the
    same message as a string on the next request, the same prompt in a different JSON form."""
    body = _drop_cache_control(body)
    if api == "responses":
        head, rest = [body.get("instructions"), body.get("tools")], body.get("input")
    else:
        head, rest = [body.get("tools"), body.get("system")], [_plain(m) for m in body.get("messages") or []]
    h = ser(head)
    return h + ser(rest), len(h)


def _plain(message: Any) -> Any:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict) \
            and set(content[0]) == {"type", "text"} and content[0]["type"] == "text":
        return {**message, "content": content[0]["text"]}
    return message


def head_tokens(run: dict[str, Any]) -> int:
    """The run's measured head: the most common cached count on its logged rotation requests."""
    counts: dict[int, int] = {}
    for e, m in zip(run["logged"], run["measured"]):
        if m["cached"] and any(t.get("event") == "rotation" for t in e.get("transform_events") or []):
            counts[m["cached"]] = counts.get(m["cached"], 0) + 1
    if not counts:
        raise ValueError("no rotation with a cache read to calibrate the head")
    return max(counts, key=lambda k: (counts[k], k))


def prefix_tokens(prefix: int, size: int, head_bytes: int, head: int, tokens: int) -> int:
    """Tokens of a cached byte prefix: the head at its measured tokens, the rest in proportion."""
    if prefix <= head_bytes:
        return round(min(head, tokens) * prefix / head_bytes) if head_bytes else 0
    rest = (prefix - head_bytes) / (size - head_bytes) if size > head_bytes else 0.0
    return min(tokens, round(head + max(0, tokens - head) * rest))


def checkpoint_read(view: bytes, head_bytes: int, tokens: int, head: int,
                    earlier: list[tuple[bytes, int, int]]) -> int:
    """Cached tokens under the checkpoint model: the longest earlier whole prompt that prefixes this
    one, else the head if it is unchanged, else nothing."""
    best = 0
    for data, hb, t in earlier:
        if len(data) <= len(view) and view.startswith(data[:-1]):  # without the list's closing bracket
            best = max(best, t)
        elif hb == head_bytes and view[:head_bytes] == data[:hb]:
            best = max(best, head)
    return min(best, tokens)


def common_prefix(a: bytes, b: bytes) -> int:
    lo, hi = 0, min(len(a), len(b))
    while lo < hi:  # the longest equal prefix, by bisection on slice equality
        mid = (lo + hi + 1) // 2
        if a[:mid] == b[:mid]:
            lo = mid
        else:
            hi = mid - 1
    return lo


class AsRunStore(SessionStore):
    """The run's own store behaviour: no epoch record (rule 16, contract PR #176). The recorded runs
    predate it, so a new engine at a restart rotates from epoch 0, as each run's did. ``set_epoch`` is
    absent for the engine (it writes only through a callable) and ``epoch_record`` finds nothing."""

    set_epoch = None

    def epoch_record(self) -> None:
        return None


def oldest_first_index(pages: list[dict[str, Any]], budget_tokens: int, salient: bool = False) -> str:
    """Candidate (a): the page index oldest first, so a rotation appends lines and the previous index
    stays a byte prefix of the new one; past the budget, newer pages are counted, not listed."""
    ordered = sorted(pages, key=lambda p: (p["turn"], p["id"]))
    lines, used = [], 0
    for i, page in enumerate(ordered):
        line = store_pages.index_line(page, salient)
        cost = store_pages.estimate_tokens(line + "\n")
        if used + cost > budget_tokens:
            lines.append(f"({len(ordered) - i} newer pages omitted; use contexto_search to find them)")
            break
        lines.append(line)
        used += cost
    return "\n".join(lines)


@contextlib.contextmanager
def variant(name: str) -> Iterator[None]:
    """Engine-level prototypes that Config cannot express. Only candidate (a) needs one."""
    if name != "append_index":
        yield
        return
    import contexto.store as store_module  # noqa: PLC0415

    with mock.patch.object(store_module, "render_index", oldest_first_index):
        yield


def _usage(api: str, total: int, cached: int, output: int) -> dict[str, Any]:
    if api == "responses":
        return {"input_tokens": total, "input_tokens_details": {"cached_tokens": cached}, "output_tokens": output}
    return {"input_tokens": max(0, total - cached), "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": 0, "output_tokens": output}


def replay(run: dict[str, Any], config: Config, prototype: str = "", baseline_bytes: list[int] | None = None,
           resume: bool = False, rate: float | None = None) -> list[dict[str, Any]]:
    """Each request as the engine would send it under ``config``: bytes, modeled tokens and cache, and
    the engine's decisions. ``baseline_bytes`` is the baseline replay's bytes, the reference for the
    token model (None: this is the baseline). A new engine (same store) starts at each of the run's
    proxy restarts, as in the run, on a store with no epoch record (``AsRunStore``), so it rotates from
    epoch 0 as the run's did; ``resume`` keeps one engine instead, modeling a proxy that resumes its
    epoch state across restarts. Each call uses a fresh home, so no candidate reads another's record. ``rate`` converts byte differences to tokens (default: the run's
    average bytes per token, head included; ``--rate`` sets a content rate, as A27 calibrates)."""
    rate = rate or bytes_per_token(run)
    head = run.get("head_tokens") or head_tokens(run)
    home = Path(tempfile.mkdtemp(prefix="contexto-a28-"))
    rows: list[dict[str, Any]] = []
    sent: list[bytes] = []
    try:
        with variant(prototype):
            engine = RewriteEngine(AsRunStore(home, "a28"), config=config)
            for i, body in enumerate(run["bodies"]):
                if i in run["restarts"] and not resume:
                    engine = RewriteEngine(AsRunStore(home, "a28"), config=config)
                request = copy.deepcopy(body)
                original = json.loads(ser(request))
                upstream, _, events = engine(request, dict(run["headers"][i]), run["endpoint"])
                data = ser(upstream)
                ref = baseline_bytes[i] if baseline_bytes is not None else len(data)
                measured = run["measured"][i]
                tokens = max(1, round(measured["input"] + (len(data) - ref) / rate))
                view, head_bytes = cache_view(run["api"], upstream)
                cached = checkpoint_read(view, head_bytes, tokens, head, sent)
                engine.observe_usage(run["endpoint"], _usage(run["api"], tokens, cached, measured["output"]), original)
                sent.append((view, head_bytes, tokens))
                rotations = [e.get("reason") for e in events if isinstance(e, dict) and e.get("event") == "rotation"]
                epoch = next((e["epoch"] for e in reversed(events) if isinstance(e, dict) and "epoch" in e), None)
                rows.append({"n": i + 1, "bytes": len(data), "tokens": tokens, "cached": cached,
                             "uncached": tokens - cached, "output": measured["output"], "rotations": rotations,
                             "epoch": epoch, "room": config.bound - tokens - config.headroom_floor,
                             "items": len(upstream.get("input" if run["api"] == "responses" else "messages") or [])})
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return rows


def cost(rows: list[dict[str, Any]], model: str) -> float:
    p = PRICES[model]
    usd = 0.0
    for r in rows:
        long = r["tokens"] > p["long_at"]
        m_in, m_out = (p["long_in"], p["long_out"]) if long else (1.0, 1.0)
        usd += m_in * (r["cached"] * p["read"] + r["uncached"] * p["write"]) + m_out * r["output"] * p["output"]
    return usd / 1e6


def summary(rows: list[dict[str, Any]], model: str) -> dict[str, Any]:
    reasons: dict[str, int] = {}
    for r in rows:
        for reason in r["rotations"]:
            reasons[reason] = reasons.get(reason, 0) + 1
    tokens = sum(r["tokens"] for r in rows)
    cached = sum(r["cached"] for r in rows)
    return {"requests": len(rows), "rotations": sum(reasons.values()), "reasons": reasons,
            "input_tokens": tokens, "cached": cached, "uncached": tokens - cached,
            "cached_share": round(cached / tokens, 4) if tokens else 0.0,
            "peak_input": max(r["tokens"] for r in rows), "min_room": min(r["room"] for r in rows),
            "min_items": min(r["items"] for r in rows),
            "mean_items": round(sum(r["items"] for r in rows) / len(rows), 1),
            "over_long_context": sum(r["tokens"] > PRICES[model]["long_at"] for r in rows),
            "usd": round(cost(rows, model), 4)}


def candidates(bound: int) -> dict[str, tuple[Config, str, bool]]:
    """The candidates, each a Config, an optional engine prototype, and whether epoch state survives a
    proxy restart, at the run's ``bound``."""
    return {
        "baseline": (Config(bound=bound), "", False),
        "a_append_index": (Config(bound=bound), "append_index", False),
        "b_target_0.25": (Config(bound=bound, window_fraction=0.25), "", False),
        "b_target_0.10": (Config(bound=bound, window_fraction=0.10), "", False),
        "c_resume": (Config(bound=bound), "", True),
        "c_resume+b_0.25": (Config(bound=bound, window_fraction=0.25), "", True),
        "c_resume+a+b_0.25": (Config(bound=bound, window_fraction=0.25), "append_index", True),
        "ref_bound_100k": (Config(bound=100_000), "", False),
        "ref_bound_150k": (Config(bound=150_000), "", False),
    }


def evaluate(run: dict[str, Any], bound: int, rate: float | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    base_config, _, _ = candidates(bound)["baseline"]
    base = replay(run, base_config, rate=rate)
    base_bytes = [r["bytes"] for r in base]
    measured_cached = sum(m["cached"] for m in run["measured"])
    measured_tokens = sum(m["input"] for m in run["measured"])
    out["check"] = {
        "requests": len(run["bodies"]), "proxy_restarts": len(run["restarts"]),
        "rebuild_offset_min": min(run["rebuild_offsets"]), "rebuild_offset_max": max(run["rebuild_offsets"]),
        "baseline_bytes_vs_logged_max_abs": max(abs(r["bytes"] - e["request_bytes"]) for r, e in zip(base, run["logged"])),
        "baseline_rotations_at": [r["n"] for r in base if r["rotations"]],
        "logged_rotations_at": [i + 1 for i, e in enumerate(run["logged"])
                                if any(t.get("event") == "rotation" for t in e.get("transform_events") or [])],
        "measured_input": measured_tokens, "measured_cached": measured_cached,
        "measured_cached_share": round(measured_cached / measured_tokens, 4),
        "bytes_per_token": round(bytes_per_token(run), 4), "rate_used": round(rate or bytes_per_token(run), 4),
        "head_tokens": head_tokens(run),
        "modeled_cached_baseline": sum(r["cached"] for r in base),
        "modeled_cached_share_baseline": round(sum(r["cached"] for r in base) / sum(r["tokens"] for r in base), 4),
    }
    for name, (config, prototype, resume) in candidates(bound).items():
        rows = base if name == "baseline" else replay(run, config, prototype, base_bytes, resume, rate)
        out[name] = summary(rows, run["model"] if run["model"] in PRICES else
                            ("gpt-6-astra" if run["api"] == "responses" else "claude-haiku-5-5"))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="kind", required=True)
    c = sub.add_parser("codex")
    c.add_argument("session_dir", type=Path)
    c.add_argument("--bound", type=int, default=64_000)
    m = sub.add_parser("claude")
    m.add_argument("run_dir", type=Path)
    m.add_argument("session")
    m.add_argument("--bound", type=int, default=150_000)
    for p in (c, m):
        p.add_argument("--json", type=Path, default=None)
        p.add_argument("--rate", type=float, default=None,
                       help="bytes per token for byte differences (default: the run's average, head included)")
    args = ap.parse_args(argv)
    run = codex_run(args.session_dir) if args.kind == "codex" else claude_run(args.run_dir, args.session)
    result = evaluate(run, args.bound, args.rate)
    text = json.dumps(result, indent=2)
    if args.json:
        args.json.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
