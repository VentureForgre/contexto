#!/usr/bin/env python3
"""Classify where `<system-reminder>` text sits in every captured request, and what Contexto carries.

Run with the repo venv (it imports src/contexto/rewrite.py, read-only):
  .venv/bin/python spikes/m2/scale/reminders/analyze.py <label>=<run_root> [...] --out <dir>

Only main-loop requests count (POST /v1/messages with a non-empty tool list); side queries without
tools are counted and skipped. For each request, every `<system-reminder>...</system-reminder>`
span is assigned to one location:

  m0           text of messages[0] (the session's first user message)
  ts_later     a later turn-start user message, Contexto's definition (`is_turn_start`: role user,
               no tool_result, at least one block that is not a context block)
  ts_embedded  the same messages, but the tag sits inside a text block that does not START with it
               (Contexto's `is_context_block` misses these, so they are not carried)
  user_only    a user message holding only context blocks (not a turn start, so not carried)
  tool_result  inside a tool_result block's content
  next_to_tr   a text block in a user message that also holds a tool_result
  system_role  a role `system` message (tagged span)
  system_untagged  a role `system` message with no tag (the whole message counts as one item;
               settings-only messages are counted by their JSON)

Contexto's view per request: `carried_content(messages, len(messages))`, i.e. what would be carried
if every turn so far were paged: context blocks (split into those from messages[0] and from later
turn starts) and carried role-system messages, plus `carried_tokens` with a fresh TokenEstimator.
Token figures in the tables are bytes / 1.83 (the spike convention) unless the column says
`carried_tokens` (Contexto's own estimator).

Outputs: <out>/requests.csv (one row per main request), <out>/reminders.csv (one row per distinct
reminder occurrence: run, message index, location, kind, bytes, first and last request it was seen
in), and a per-turn summary on stdout.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "src"))
from contexto import rewrite as rw  # noqa: E402
from contexto.tokens import TokenEstimator  # noqa: E402

OPEN, CLOSE = "<system-reminder>", "</system-reminder>"
BPT = 1.83
LOCS = ("m0", "ts_later", "ts_embedded", "user_only", "tool_result", "next_to_tr", "system_role", "system_untagged")
TURN_RE = re.compile(r"Turn (\d+)\. Synthetic user request")
# Item starts seen in 2.1.302 role-system messages (measured); used only to attribute sizes to kinds.
ITEM_SPLIT = re.compile(r"\n\n(?=Note: \S+ changed on disk|Plan mode is active|## Exited Plan Mode|<total_tokens>"
                        r"|The task tools haven't|Contents of \S+ \()")


def blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in (content or []) if isinstance(b, dict)]


def spans(text: str) -> list[str]:
    out, i = [], 0
    while True:
        a = text.find(OPEN, i)
        if a < 0:
            return out
        b = text.find(CLOSE, a)
        b = len(text) if b < 0 else b + len(CLOSE)
        out.append(text[a:b])
        i = b


def tr_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    return "\n".join(x.get("text", "") for x in (c or []) if isinstance(x, dict) and x.get("type") == "text")


def kind(span: str) -> str:
    body = (span[len(OPEN):] if span.startswith(OPEN) else span).strip()
    first = body.splitlines()[0] if body else ""
    first = re.sub(r"(?<![<\w])/[^\s]+", "<path>", first)
    first = re.sub(r"\d+", "N", first)
    return first[:90]


def locate(messages: list[dict]):
    """Yield (message index, location, span) for every reminder span in the request."""
    for i, m in enumerate(messages):
        role = m.get("role")
        bs = blocks(m.get("content"))
        if role == "system":
            found = False
            for b in bs:
                for s in spans(b.get("text", "") if b.get("type") == "text" else ""):
                    found = True
                    yield i, "system_role", s
            if not found:  # untagged role-system message: reminder-like content on newer models
                text = "\n".join(b.get("text", "") for b in bs if b.get("type") == "text")
                if not text:
                    yield i, "system_untagged", json.dumps({k: v for k, v in m.items() if k != "role"})
                    continue
                # one turn's items share one message, joined by a blank line: split at known item starts
                for part in ITEM_SPLIT.split(text):
                    yield i, "system_untagged", part
            continue
        if role != "user":
            continue
        has_tr = any(b.get("type") == "tool_result" for b in bs)
        ts = rw.is_turn_start(m)
        for b in bs:
            if b.get("type") == "tool_result":
                for s in spans(tr_text(b)):
                    yield i, "tool_result", s
                continue
            if b.get("type") != "text":
                continue
            for s in spans(b.get("text", "")):
                if i == 0:
                    loc = "m0"
                elif has_tr:
                    loc = "next_to_tr"
                elif ts:
                    loc = "ts_later" if rw.is_context_block(b) else "ts_embedded"
                else:
                    loc = "user_only"
                yield i, loc, s


def current_turn(messages: list[dict]) -> int | None:
    for m in reversed(messages):
        if rw.is_turn_start(m):
            for b in blocks(m.get("content")):
                g = TURN_RE.search(b.get("text", "") if b.get("type") == "text" else "")
                if g:
                    return int(g.group(1))
    return None


def jbytes(x) -> int:
    return len(json.dumps(x, ensure_ascii=False).encode())


def analyze_run(label: str, root: Path, req_rows: list, rem_rows: dict) -> list[dict]:
    log = {}
    for line in (root / "model.jsonl").read_text().splitlines():
        r = json.loads(line)
        if "n" in r:
            log[r["n"]] = r
    side = 0
    rows = []
    for f in sorted((root / "bodies").glob("*.json")):
        n = int(f.stem)
        meta = log.get(n, {})
        if meta.get("path", "").endswith("/count_tokens"):
            continue
        body = json.loads(f.read_bytes() or b"{}")
        if not body.get("tools"):
            side += 1
            continue
        msgs = body.get("messages") or []
        row = {"run": label, "n": n, "turn": current_turn(msgs), "messages": len(msgs), "body_bytes": f.stat().st_size,
               "model": body.get("model"), "turn_starts": len(rw.turn_starts(msgs))}
        for loc in LOCS:
            row[f"{loc}_n"] = 0
            row[f"{loc}_bytes"] = 0
        for i, loc, s in locate(msgs):
            row[f"{loc}_n"] += 1
            row[f"{loc}_bytes"] += len(s.encode())
            key = (label, i, loc, hashlib.sha1(s.encode()).hexdigest()[:12])
            rec = rem_rows.setdefault(key, {"run": label, "msg_index": i, "location": loc, "kind": kind(s),
                                            "bytes": len(s.encode()), "first_n": n, "last_n": n,
                                            "first_turn": row["turn"], "seen_in": 0})
            rec["last_n"] = n
            rec["seen_in"] += 1
        sysm = [m for m in msgs if m.get("role") == "system"]
        row["system_msgs"] = len(sysm)
        row["system_msgs_bytes"] = sum(jbytes(m.get("content")) for m in sysm)
        cb, cs = rw.carried_content(msgs, len(msgs))
        m0_ids = {id(b) for b in blocks(msgs[0].get("content"))} if msgs else set()
        row["carried_blocks_n"] = len(cb)
        row["carried_blocks_bytes"] = sum(len(b.get("text", "").encode()) for b in cb)
        row["carried_blocks_later_n"] = sum(1 for b in cb if id(b) not in m0_ids)
        row["carried_blocks_later_bytes"] = sum(len(b.get("text", "").encode()) for b in cb if id(b) not in m0_ids)
        row["carried_systems_n"] = len(cs)
        row["carried_systems_bytes"] = sum(jbytes(m.get("content")) for m in cs)
        row["carried_tokens"] = rw.carried_tokens(msgs, TokenEstimator())
        rows.append(row)
    req_rows.extend(rows)
    print(f"[{label}] main requests {len(rows)}, side requests without tools {side}")
    return rows


def per_turn(rows: list[dict]) -> list[dict]:
    """The last main request of each turn (the fullest history the turn sent)."""
    last = {}
    for r in rows:
        if r["turn"] is not None:
            last[r["turn"]] = r
    return [last[t] for t in sorted(last)]


def tok(b: int) -> int:
    return round(b / BPT)


class _NullStore:
    def page_out(self, pages, turns_paged):  # the fallback's only store call
        return None


def contexto_view(root: Path, turns: list[dict]) -> None:
    """Contexto's carried content per turn as the engine would hold it if every turn before the
    current one were paged (k = index of the current turn start), with the Terms fallback
    (`page_carried_text`, carried_text_budget 8,000) applied turn by turn and its paged set kept, the
    way rotation keeps `paged_carried`. Adds carried_k (before the fallback), carried_after_fallback,
    carried_blocks_k (context blocks only: these the fallback cannot page) to each turn row."""
    est, cfg = TokenEstimator(), rw.Config()
    paged: frozenset = frozenset()
    initial = None
    for r in turns:
        msgs = json.loads((root / "bodies" / f"{r['n']:05d}.json").read_bytes())["messages"]
        if initial is None:
            initial = rw.carried_tokens(msgs, est)
        k = rw.turn_starts(msgs)[-1]
        sub = msgs[:k]
        r["carried_k"] = rw.carried_tokens(sub, est, paged) if k else 0
        state = rw.EpochState(k=k, initial_carried=initial, paged_carried=paged)
        new = rw.page_carried_text(sub, state, _NullStore(), cfg, est, []) if k else state
        paged = new.paged_carried
        r["carried_after_fallback"] = rw.carried_tokens(sub, est, paged) if k else 0
        blocks_k, _ = rw.carried_content(sub, k)
        r["carried_blocks_k"] = (rw.estimate_items(est, [{"role": "user", "content": blocks_k}])
                                 if blocks_k else 0)
        r["fallback_paged_total"] = len(paged)
        # carried role-system messages by Contexto class, after the fallback: only carried_text is pageable
        idx, _ = rw.carried_systems(sub, k, paged) if k else ([], 0)
        by = {}
        for i in idx:
            c = rw.classify_system(sub, i)
            by[c] = by.get(c, 0) + rw.estimate_items(est, [sub[i]])
        r["carried_session_start_k"] = by.get(rw.SESSION_START, 0)
        r["carried_settings_k"] = by.get(rw.SETTINGS, 0)
        r["carried_text_k"] = by.get(rw.CARRIED_TEXT, 0)
        r["carried_unclassified_k"] = by.get(rw.UNCLASSIFIED, 0)
        r["session_start_msgs_k"] = sum(1 for i in idx if rw.classify_system(sub, i) == rw.SESSION_START)
        # not pageable by the fallback: context blocks, session-start class, settings, unclassified
        r["carried_unpageable_k"] = (r["carried_blocks_k"] + r["carried_session_start_k"] + r["carried_settings_k"]
                                     + r["carried_unclassified_k"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", help="label=run_root")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    req_rows: list[dict] = []
    rem_rows: dict = {}
    turn_rows: list[dict] = []
    show = (1, 2, 3, 6, 12, 18, 24, 30, 36, 42, 48)
    for spec in a.runs:
        label, root = spec.split("=", 1)
        rows = analyze_run(label, Path(root), req_rows, rem_rows)
        turns = per_turn(rows)
        if not turns:
            continue
        contexto_view(Path(root), turns)
        turn_rows.extend(turns)
        print(f"\n[{label}] turns {turns[0]['turn']}..{turns[-1]['turn']} model {turns[-1]['model']} "
              f"(last request of each turn; reminder sizes bytes/1.83; Contexto columns its own estimator)")
        print("| turn | msgs | m0 reminders tok | later turn-start reminders n / tok | in tool_result n / tok | "
              "next to tool_result n | reminder-only user msg n | role-system msgs n / tok | "
              "Contexto carried, all paged | carried at k, before / after fallback | context blocks | "
              "session-start class n / tok | not pageable |")
        print("|" + "---|" * 13)
        for r in turns:
            if r["turn"] in show or r is turns[-1]:
                print(f"| {r['turn']} | {r['messages']} | {tok(r['m0_bytes'])} | {r['ts_later_n']} / {tok(r['ts_later_bytes'])} | "
                      f"{r['tool_result_n']} / {tok(r['tool_result_bytes'])} | {r['next_to_tr_n']} | {r['user_only_n']} | "
                      f"{r['system_msgs']} / {tok(r['system_msgs_bytes'])} | {r['carried_tokens']} | "
                      f"{r['carried_k']} / {r['carried_after_fallback']} | {r['carried_blocks_k']} | "
                      f"{r['session_start_msgs_k']} / {r['carried_session_start_k']} | {r['carried_unpageable_k']} |")
        f, l = turns[min(1, len(turns) - 1)], turns[-1]  # from turn 2: the first turn with k > 0
        span = max(1, l["turn"] - f["turn"])
        rate = lambda col, conv=tok: (conv(l[col]) - conv(f[col])) / span  # noqa: E731
        ident = lambda x: x  # noqa: E731
        print(f"  per-turn growth, turn {f['turn']} to {l['turn']}: later turn-start reminders "
              f"{rate('ts_later_bytes'):.0f} tok/turn; role-system messages {rate('system_msgs_bytes'):.0f} tok/turn; "
              f"Contexto carried_tokens {rate('carried_tokens', ident):.0f}/turn; carried at k after fallback "
              f"{rate('carried_after_fallback', ident):.0f}/turn; context blocks at k {rate('carried_blocks_k', ident):.0f}/turn; "
              f"not pageable at k {rate('carried_unpageable_k', ident):.0f}/turn; "
              f"ts_embedded {l['ts_embedded_n']}, reminder-only user msgs {l['user_only_n']}, next to tool_result {l['next_to_tr_n']}")
    if turn_rows:
        with open(out / "turns.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(turn_rows[0].keys()))
            w.writeheader()
            w.writerows(turn_rows)
    if req_rows:
        with open(out / "requests.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(dict.fromkeys(k for r in req_rows for k in r)), restval="")
            w.writeheader()
            w.writerows(req_rows)
    rems = sorted(rem_rows.values(), key=lambda r: (r["run"], r["first_n"], r["msg_index"]))
    if rems:
        with open(out / "reminders.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rems[0].keys()))
            w.writeheader()
            w.writerows(rems)
    # kinds per location per run, over distinct occurrences
    agg: dict = {}
    for r in rems:
        k = (r["run"], r["location"], r["kind"])
        g = agg.setdefault(k, {"count": 0, "bytes": 0, "transient": 0})
        g["count"] += 1
        g["bytes"] += r["bytes"]
        last_run_n = max(x["n"] for x in req_rows if x["run"] == r["run"])
        g["transient"] += r["last_n"] < last_run_n
    print("\n| run | location | kind (first line, digits as N) | distinct | tokens total | dropped before last request |")
    for (run, loc, kd), g in sorted(agg.items()):
        print(f"| {run} | {loc} | {kd} | {g['count']} | {tok(g['bytes'])} | {g['transient']} |")


if __name__ == "__main__":
    main()
