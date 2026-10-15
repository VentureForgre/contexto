#!/usr/bin/env python3
"""Per-request usage summary from a Codex rollout file (codex-cli 0.154.0).

Usage: codex_usage.py <rollout.jsonl> [--summary | --per-turn]

Reads only usage numbers, record types, and counts; never prints prompts, outputs,
or tool output. Emits CSV with the fields builder-1's proxy uses for the lane 2 cost
model, plus raw Codex columns:

  turn                         1-based index of event_msg task_started
  request                      1-based index of event_msg token_count (one per model response)
  ts                           timestamp of the token_count record
  cli, model                   "codex", model from the latest turn_context
  input_tokens                 codex input - cached; may include tokens written to the cache
  cache_creation_input_tokens  empty: Codex serde-defaults cache_write_input_tokens to 0 when the
                               server omits it, so a rollout 0 is not a measured zero
  cache_read_input_tokens      codex cached_input_tokens
  output_tokens                codex output_tokens (includes reasoning)
  request_bytes                empty: not observable from the rollout
  message_count                response_item records in history when the request was sent: all
                               recorded items minus this response's own output items (reset to
                               the replacement_history length at a compaction). Excludes base
                               instructions and tool definitions, which are not response_items.
  codex_input_tokens           raw input_tokens (includes cached)
  codex_cache_write_raw        raw cache_write_input_tokens (0 may mean "not reported")
  reasoning_output_tokens      raw reasoning_output_tokens
  total_tokens                 raw total_tokens (input + output) = context in use after the response
  model_context_window         as reported in the token_count event
  compactions_so_far           count of `compacted` records seen so far

With --summary, prints record-type counts, compaction records (field names and
replacement_history length only), and totals instead of the CSV. With --per-turn,
prints one CSV row per turn: requests, summed usage, and context after the turn.
"""
import collections
import csv
import json
import sys

FIELDS = ["turn", "request", "ts", "cli", "model", "input_tokens", "cache_creation_input_tokens",
          "cache_read_input_tokens", "output_tokens", "request_bytes", "message_count",
          "codex_input_tokens", "codex_cache_write_raw", "reasoning_output_tokens", "total_tokens",
          "model_context_window", "compactions_so_far"]

# Item types the model produces in a response; everything else in history is request input.
OUTPUT_ITEM_TYPES = {"reasoning", "function_call", "custom_tool_call", "local_shell_call",
                     "web_search_call", "image_generation_call", "tool_search_call"}


def is_output_item(item):
    if item.get("type") == "message":
        return item.get("role") == "assistant"
    return item.get("type") in OUTPUT_ITEM_TYPES


def rows(path):
    turn = request = compactions = 0
    model = None
    items_total = 0  # response_item records currently in history
    outputs_since = 0  # output items recorded since the previous token_count
    with open(path) as f:
        for line in f:
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = o.get("type")
            p = o.get("payload") if isinstance(o.get("payload"), dict) else {}
            sub = p.get("type")
            if t == "turn_context":
                model = p.get("model") or model
            elif t == "compacted":
                compactions += 1
                items_total = len(p.get("replacement_history") or [])
                outputs_since = 0
            elif t == "response_item":
                items_total += 1
                outputs_since += is_output_item(p)
            elif t == "event_msg" and sub == "task_started":
                turn += 1
            elif t == "event_msg" and sub == "token_count":
                info = p.get("info") or {}
                last = info.get("last_token_usage")
                if not last:
                    continue
                request += 1
                raw_in = last.get("input_tokens", 0)
                cached = last.get("cached_input_tokens", 0)
                yield {
                    "turn": turn, "request": request, "ts": o.get("timestamp"), "cli": "codex",
                    "model": model, "input_tokens": raw_in - cached,
                    "cache_creation_input_tokens": "", "cache_read_input_tokens": cached,
                    "output_tokens": last.get("output_tokens", 0), "request_bytes": "",
                    "message_count": items_total - outputs_since, "codex_input_tokens": raw_in,
                    "codex_cache_write_raw": last.get("cache_write_input_tokens", 0),
                    "reasoning_output_tokens": last.get("reasoning_output_tokens", 0),
                    "total_tokens": last.get("total_tokens", 0),
                    "model_context_window": info.get("model_context_window"),
                    "compactions_so_far": compactions,
                }
                outputs_since = 0


def summary(path):
    counts = collections.Counter()
    with open(path) as f:
        for line in f:
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                counts[("unparseable", None)] += 1
                continue
            p = o.get("payload") if isinstance(o.get("payload"), dict) else {}
            counts[(o.get("type"), p.get("type"))] += 1
            if o.get("type") == "compacted":
                rh = p.get("replacement_history") or []
                print(f"compacted at {o.get('timestamp')}: fields={sorted(p)} "
                      f"replacement_history_items={len(rh)} "
                      f"item_types={dict(collections.Counter(i.get('type') for i in rh))}")
    for (t, sub), n in sorted(counts.items(), key=str):
        print(f"{n:6d} {t} {sub or ''}")
    rs = list(rows(path))
    if rs:
        print(f"requests={len(rs)} turns={rs[-1]['turn']} "
              f"sum_codex_input={sum(r['codex_input_tokens'] for r in rs)} "
              f"sum_cached={sum(r['cache_read_input_tokens'] for r in rs)} "
              f"sum_output={sum(r['output_tokens'] for r in rs)} "
              f"sum_total={sum(r['total_tokens'] for r in rs)} last_total={rs[-1]['total_tokens']}")


def per_turn(path):
    turns = {}
    for r in rows(path):
        t = turns.setdefault(r["turn"], {"turn": r["turn"], "requests": 0, "codex_input_tokens": 0,
                                         "cache_read_input_tokens": 0, "output_tokens": 0,
                                         "reasoning_output_tokens": 0, "total_tokens": 0})
        t["requests"] += 1
        for k in ("codex_input_tokens", "cache_read_input_tokens", "output_tokens",
                  "reasoning_output_tokens", "total_tokens"):
            t[k] += r[k]
        t["context_after_turn"] = r["total_tokens"]
        t["message_count_last_request"] = r["message_count"]
    out = list(turns.values())
    w = csv.DictWriter(sys.stdout, fieldnames=list(out[0]) if out else ["turn"])
    w.writeheader()
    for t in out:
        w.writerow(t)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    if "--summary" in sys.argv[2:]:
        summary(sys.argv[1])
        return
    if "--per-turn" in sys.argv[2:]:
        per_turn(sys.argv[1])
        return
    w = csv.DictWriter(sys.stdout, fieldnames=FIELDS)
    w.writeheader()
    for r in rows(sys.argv[1]):
        w.writerow(r)


if __name__ == "__main__":
    main()
