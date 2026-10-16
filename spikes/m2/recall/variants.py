"""Offline recall-variant builder (M2 lane A packet A7, H1 recall fix). Zero spend, deterministic.

    python3 variants.py RUN_DIR --variants variants.json --out OUT_DIR

RUN_DIR is a recorded acceptance run (``claude-config/projects/*/<session>.jsonl`` and
``home/sessions/<id>/log.jsonl``). The builder:

1. rebuilds every request Claude Code sent from its session JSONL: ``api-request`` records are
   deltas (``base`` = previous request id, ``keep`` = messages kept, ``tail`` = message hashes), a
   hash is SHA-256 of the message's canonical JSON, resolved from ``api-request-blob`` or
   user/assistant records (builder-4's H1 diagnosis method; assistant tails whose signed thinking
   no longer matches the transcript are filled in order and counted as approximate);
2. replays them, per variant, through a fresh ``RewriteEngine`` and store, feeding the recorded
   upstream usage back after each request, so rotations follow the recorded run;
3. writes each variant's final request (the probe turn, as the engine would send it upstream) to
   ``OUT_DIR/<variant>/probe_request.json`` (synthetic content; keep OUT_DIR gitignored) and a
   numbers-only ``OUT_DIR/summary.json``.

Probe texts are left as recorded unless ``--probe-text`` substitutes one text for every variant
(intake: the probe must not mention the contexto tools). A variant changes only the engine knobs (``contexto.rewrite.Config``
recall fields) and the store variant (``CONTEXTO_RECALL_VARIANT``, lane B).

variants.json: ``{"name": {"engine": {<Config recall fields>}, "store_variant": "baseline",
"probe": "recorded" (optional: keep the recorded probe wording for this variant only)}}``. A
``baseline`` variant (no knobs, the store's default text) is always added and checked against the
recorded run's per-request engine decisions (epoch, k, upstream message count). Omit
``store_variant`` for the store default. ``--strip-thinking`` removes thinking from every written
probe request, so all live sends are uniform.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from contexto.canonical import canonical_json
from contexto.rewrite import ANTHROPIC, MESSAGES, Config, RewriteEngine, _DIALECT, is_turn_start, strip_thinking
from contexto.store import SessionStore
from contexto.tokens import TokenEstimator

RECALL_FIELDS = {"recall_rule", "recall_example", "preamble_first", "turn_reminder"}
CONTEXTO_TOOLS = ("contexto_search", "contexto_recall")
STORE_VARIANT_ENV = "CONTEXTO_RECALL_VARIANT"


def message_hash(message: dict) -> str:
    return hashlib.sha256(canonical_json(message).encode("utf-8")).hexdigest()


def rebuild_requests(run: Path) -> tuple[list[dict], int]:
    """Claude Code's requests in order, and how many assistant tails were approximated."""
    session = glob.glob(str(run / "claude-config" / "projects" / "*" / "*.jsonl"))
    if len(session) != 1:
        raise SystemExit(f"expected one Claude Code session JSONL under {run}, found {len(session)}")
    records = [json.loads(line) for line in Path(session[0]).read_text(encoding="utf-8").splitlines() if line.strip()]
    by_hash = {r["hash"]: r["message"] for r in records if r.get("type") == "api-request-blob"}
    for r in records:
        if r.get("type") in ("user", "assistant"):
            m = {"role": r["message"]["role"], "content": r["message"]["content"]}
            by_hash.setdefault(message_hash(m), m)
    assistants = [{"role": "assistant", "content": r["message"]["content"]} for r in records if r.get("type") == "assistant"]
    shape = [r for r in records if r.get("type") == "api-request-shape"][-1]["shape"]
    built, by_id, next_assistant, approximate = [], {}, 0, 0
    for r in (r for r in records if r.get("type") == "api-request"):
        messages = list(by_id.get(r.get("base"), [])[: r.get("keep", 0)])
        for h in r["tail"]:
            if h in by_hash:
                messages.append(by_hash[h])
                if by_hash[h].get("role") == "assistant":
                    next_assistant += 1
            else:  # an assistant turn whose signed thinking differs from the transcript copy
                messages.append(assistants[min(next_assistant, len(assistants) - 1)])
                next_assistant += 1
                approximate += 1
        by_id[r["id"]] = messages
        # Body fields as Claude Code sent them: the recorded params minus betas (an HTTP header).
        body = {k: v for k, v in r["params"].items() if k != "betas"}
        built.append({**body, "system": shape["system"], "tools": shape["tools"], "messages": messages, "stream": True})
    return built, approximate


def substitute_probe_text(request: dict, text: str) -> dict:
    """Replace the probe turn's text (the last turn-start user message of the final request)."""
    messages = request["messages"]
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if is_turn_start(m):
            content = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
            last_text = max(j for j, b in enumerate(content) if isinstance(b, dict) and b.get("type") == "text")
            content = [*content[:last_text], {**content[last_text], "text": text}, *content[last_text + 1:]]
            messages = [*messages[:i], {**m, "content": content}, *messages[i + 1:]]
            return {**request, "messages": messages}
    raise SystemExit("no turn-start user message to substitute the probe text into")


def recorded_requests(run: Path) -> list[dict]:
    sessions = [d for d in (run / "home" / "sessions").iterdir() if d.is_dir()]
    log = [json.loads(line) for line in (sessions[0] / "log.jsonl").read_text(encoding="utf-8").splitlines() if line]
    return [e for e in log if e.get("event") == "request" and e.get("path") == "/v1/messages"]


def preamble_position(message: dict, knobs: Config, store_texts: tuple[str, ...]) -> dict[str, Any]:
    """Where the preamble block sits in the first retained message, and its size (no content)."""
    content = message.get("content") if isinstance(message.get("content"), list) else []
    markers = [t for t in (knobs.recall_rule, *store_texts) if t]
    for index, block in enumerate(content):
        text = block.get("text", "") if isinstance(block, dict) and block.get("type") == "text" else ""
        if markers and text.startswith(markers[0]):
            return {"block": index, "of": len(content), "chars": len(text)}
    return {"block": None, "of": len(content), "chars": 0}


def strip_all_thinking(upstream: dict) -> tuple[dict, int]:
    """Remove every thinking and redacted-thinking block (rule 2's strip, with its placeholder for a
    thinking-only assistant message), so a rebuilt transcript signature that may not match the
    original cannot cause a 400 or a silent thinking drop in a live send. Returns the count."""
    count = sum(1 for m in upstream.get("messages") or [] if isinstance(m.get("content"), list)
                for b in m["content"] if isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"))
    token = _DIALECT.set(ANTHROPIC)
    try:
        messages = [x for x in (strip_thinking(m) for m in upstream.get("messages") or []) if x is not None]
    finally:
        _DIALECT.reset(token)
    return {**upstream, "messages": messages}, count


def replay(requests: list[dict], recorded: list[dict], name: str, spec: dict, out: Path,
           strip: bool = False) -> dict[str, Any]:
    engine_knobs = {k: v for k, v in (spec.get("engine") or {}).items() if k in RECALL_FIELDS}
    unknown = set(spec.get("engine") or {}) - RECALL_FIELDS
    if unknown:
        raise SystemExit(f"variant {name}: unknown engine knobs {sorted(unknown)}")
    previous = os.environ.get(STORE_VARIANT_ENV)
    store_variant = spec.get("store_variant")  # None: the store's own default
    if store_variant is None:
        os.environ.pop(STORE_VARIANT_ENV, None)
    else:
        os.environ[STORE_VARIANT_ENV] = store_variant
    try:
        home = Path(tempfile.mkdtemp(prefix=f"a7-{name}-"))
        store = SessionStore(home, "replay")
        config = Config(bound=spec.get("bound", 150_000), **engine_knobs)
        engine = RewriteEngine(store, config=config, estimator=TokenEstimator())
        rotations, decisions_match, last = 0, 0, None
        for i, request in enumerate(requests):
            upstream, _, events = engine(json.loads(json.dumps(request)), {}, MESSAGES)
            rotations += sum(1 for e in events if e.get("event") == "rotation")
            mine = next((e for e in events if e.get("event") == "request"), {})
            theirs = next((e for e in (recorded[i].get("transform_events") or []) if e.get("event") == "request"), {}) \
                if i < len(recorded) else {}
            decision = ("epoch", "k", "upstream_messages")
            decisions_match += int(bool(theirs) and all(mine.get(f) == theirs.get(f) for f in decision))
            if i < len(recorded) and recorded[i].get("usage"):
                engine.observe_usage(MESSAGES, recorded[i]["usage"], request)
            last = (upstream, mine)
        upstream, event = last
        stripped = 0
        if strip:
            upstream, stripped = strip_all_thinking(upstream)
        store_texts = (store.pinned_text(config.pin_budget), store.index_text(config.index_budget))
        directory = out / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "probe_request.json").write_text(json.dumps(upstream, ensure_ascii=False))
        estimator = TokenEstimator()
        first = upstream["messages"][0]
        position = preamble_position(first, config, store_texts)
        tool_names = [t.get("name", "") for t in upstream.get("tools") or []]
        reminder = config.turn_reminder
        return {
            "variant": name,
            "store_variant": store_variant or "store default",
            "thinking_blocks_stripped": stripped,
            "engine_knobs": sorted(k for k, v in engine_knobs.items() if v),
            "requests_replayed": len(requests),
            "rotations": rotations,
            # Fidelity of the replay: same epoch, k, and upstream message count as the recorded run.
            # Bytes cannot match exactly: Claude Code does not record every body field (about 400
            # bytes, inferred metadata) and transcript copies lack signed thinking (approximate tails).
            "decisions_match_recorded": f"{decisions_match}/{len(recorded)}",
            "probe": {
                "epoch": event.get("epoch"), "k": event.get("k"), "upstream_messages": len(upstream["messages"]),
                "sent_bytes": event.get("sent_bytes"), "estimate_tokens": event.get("estimate"),
                "preamble_block": position["block"], "first_message_blocks": position["of"],
                "preamble_chars": position["chars"],
                "preamble_tokens_est": estimator.estimate("x" * position["chars"]) if position["chars"] else 0,
                "turn_reminders": sum(1 for m in upstream["messages"]
                                      if reminder and is_turn_start(m) and reminder in json.dumps(m, ensure_ascii=False)),
                "contexto_tools_present": [t for t in CONTEXTO_TOOLS if any(n.endswith(t) for n in tool_names)],
            },
        }
    finally:
        if previous is None:
            os.environ.pop(STORE_VARIANT_ENV, None)
        else:
            os.environ[STORE_VARIANT_ENV] = previous


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path)
    parser.add_argument("--variants", type=Path, default=None, help="variants JSON (baseline is always added)")
    parser.add_argument("--out", type=Path, required=True, help="output dir; keep it under a gitignored .contexto/")
    parser.add_argument("--strip-thinking", action="store_true",
                        help="remove thinking blocks from every written probe request (uniform live sends)")
    parser.add_argument("--probe-text", type=Path, default=None,
                        help="file whose text replaces the recorded probe turn's text in the final request, for "
                             "every variant (default: the probe exactly as recorded)")
    args = parser.parse_args(argv)
    variants = {"baseline": {}}
    if args.variants:
        variants.update(json.loads(args.variants.read_text(encoding="utf-8")))
    requests, approximate = rebuild_requests(args.run)
    recorded = recorded_requests(args.run)
    neutral = list(requests)
    if args.probe_text:
        neutral[-1] = substitute_probe_text(requests[-1], args.probe_text.read_text(encoding="utf-8"))
    rows = []
    for name, spec in variants.items():
        # "probe": "recorded" keeps the recorded probe wording for one variant (intake's control).
        use = requests if spec.get("probe") == "recorded" else neutral
        row = replay(use, recorded, name, spec, args.out, args.strip_thinking)
        row["probe_text"] = "recorded" if use is requests or not args.probe_text else "substituted"
        rows.append(row)
    summary = {"requests": len(requests), "recorded_requests": len(recorded),

               "approximate_assistant_tails": approximate, "variants": rows}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
