"""ESTIMATE (offline, zero spend): one probe turn, shared 23,000 room, on the code as it is.

The 54 probes of the second live H1 run (acceptance-20261016T044723Z), with the real contexto MCP
tool code over a SessionStore of that run's 15 paged turns, rebuilt byte for byte by the committed
run.plan(). A deterministic search policy stands in for the model (it searches every probe). Prints
numbers only. Run: uv run python spikes/m2/estimate/one_turn.py
"""
import io
import json
import math
import re
import statistics
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # the repository
for p in (ROOT / "spikes" / "m2", ROOT / "spikes" / "m0", ROOT / "src"):
    sys.path.insert(0, str(p))
from acceptance import run  # noqa: E402

from contexto import mcp  # noqa: E402
from contexto.store import SessionStore  # noqa: E402

PAGED, ROOM, WINDOW, BPT = 15, 23_000, 81_737, 2.123986182634373
CALL_OUTPUT = 30  # tokens of tool_use output per call (assumed)
ANSWER_OUTPUT = 10  # tokens per answered line "q_id: answer" (assumed)
script, _ = run.plan()
turns = list(getattr(script, "content_turns", script.turns[:-1]))  # D27 (PR #112) adds probe turns
in_view = "\n".join(turns[PAGED:])
probes = list(script.probes)
totals = Counter(p.type for p in probes)
class Session:
    def __init__(self):
        self.store = SessionStore(Path(tempfile.mkdtemp()), "est")
        self.store.page_out([[{"role": "user", "content": [{"type": "text", "text": t}]},
                              {"role": "assistant", "content": [{"type": "thinking", "thinking": ""},
                                                                {"type": "text", "text": "OK"}]}]
                             for t in turns[:PAGED]], 0)
        self.store.set_window(WINDOW, BPT)
        self.server = mcp.Server(self.store, io.StringIO(), io.StringIO())
        self.calls, self.chars, self.ran_out, self.per_hit = 0, 0, False, []
        self.spent_total = 0

    def turn(self, turn_id, room):
        self.spent_total += self.store.turn_room()[2]
        self.store.set_turn_room(turn_id, max(0, int(room)))

    def call(self, name, args):
        before = self.store.turn_room()[2]
        line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
        text = "".join(b["text"] for b in self.server.handle_line(line)["result"]["content"])
        self.calls += 1
        self.chars += len(text)
        hits = text.count("\npg_") + text.startswith("pg_")
        if name == "contexto_search" and hits:
            self.per_hit.append((self.store.turn_room()[2] - before) / hits)
        if text == mcp.BUDGET_SPENT or text.startswith("The recall budget for this turn is nearly used up"):
            self.ran_out = True
        return text


def find(sess, query, needed):
    seen = sess.call("contexto_search", {"query": query})
    if all(s in seen or s in in_view for s in needed):
        return seen
    m = re.match(r"(pg_[0-9a-f]{16}) ", seen)
    offsets = re.findall(r"^> (\d+): ", seen, re.MULTILINE)
    if m and offsets:
        seen += "\n" + sess.call("contexto_recall", {"page_id": m.group(1), "offset": int(offsets[-1])})
    return seen


def latest_per_id(statements):
    last = {}
    for s in statements:
        last[re.search(r"AI-\d+", s).group()] = s
    return last


def answerable(sess, p):
    have = lambda s, seen: s in seen or s in in_view  # noqa: E731
    if p.type in ("paged_only", "distractor"):
        [key] = p.subjects
        return have(f"{key}={p.answer}", find(sess, key, [f"{key}={p.answer}"]))
    if p.type == "chain":
        return have(p.statements[-1], find(sess, p.subjects[0], [p.statements[-1]]))
    if p.type == "multihop":
        return all(have(st, find(sess, q, [st])) for q, st in zip(p.subjects, p.statements))
    if p.type == "obligations":
        return all(have(s, find(sess, oid, [s])) for oid, s in latest_per_id(p.statements).items())
    raise ValueError(p.type)


def standalone(p):
    sess = Session()
    sess.turn("s", ROOM)
    answerable(sess, p)
    return sess.store.turn_room()[2]


def q_tokens(ps):
    return sum(len(f"{p.id}: {p.question}\n") for p in ps) / BPT


def run_turns(order, n_turns, knock_on):
    """Probes in ``order`` split into ``n_turns`` contiguous user turns, each with its own room."""
    size = math.ceil(len(order) / n_turns)
    chunks = [order[i:i + size] for i in range(0, len(order), size)]
    sess, ok, out_at, rooms, traffic = Session(), Counter(), None, [], 0.0
    asked = []
    for k, chunk in enumerate(chunks):
        asked += chunk
        later = [p for p in order if p not in asked]
        # Live room 23,000 had all 54 questions in input; turn k carries only questions asked so far,
        # plus (knock-on) the earlier turns' tool results and outputs, in real tokens.
        room = ROOM + q_tokens(later) - (traffic if knock_on else 0) if knock_on else ROOM
        rooms.append(int(room))
        sess.turn(f"t{k}", room)
        chars0, calls0 = sess.chars, sess.calls
        for p in chunk:
            before = sess.ran_out
            if answerable(sess, p):
                ok[p.type] += 1
            if sess.ran_out and not before and out_at is None:
                out_at = (k + 1, len(asked) - len(chunk) + chunk.index(p) + 1)
        traffic += (sess.chars - chars0) / BPT + (sess.calls - calls0) * CALL_OUTPUT + len(chunk) * ANSWER_OUTPUT
    used = sess.spent_total + sess.store.turn_room()[2]
    return ok, used, out_at, rooms, sess


def line(label, ok, used, out_at, rooms):
    cats = " ".join(f"{t} {ok[t]}/{totals[t]}" for t in totals)
    where = f"ran out at turn {out_at[0]}, probe #{out_at[1]}" if out_at else "never ran out"
    print(f"  {label}: {sum(ok.values())}/54 [{cats}]; charged {used}; rooms {rooms}; {where}")


def main():
    import subprocess
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        sha = "unknown"
    print("ESTIMATE (offline, simulated policy, no model): one probe turn, shared room")
    print(f"code: {sha}")
    print(f"METHOD: real MCP tool code; {PAGED} paged turns rebuilt by run.plan(); window {WINDOW}, bytes/token {BPT:.4f} "
          f"(charge ratio 1.5); one shared room of {ROOM}; ideal policy searches every probe; needed lines count if "
          "returned or in a non-paged turn; one recall at the first hit's last match offset if a hit lacks the line.")
    charges = {p.id: standalone(p) for p in probes}
    worst = sorted(probes, key=lambda p: charges[p.id], reverse=True)
    by_type = {}
    for p in probes:
        by_type.setdefault(p.type, []).append(charges[p.id])
    probe_sess = Session()
    probe_sess.turn("h", 10**9)
    for p in probes:
        answerable(probe_sess, p)
    print("median per-hit charge:", round(statistics.median(probe_sess.per_hit)), "(compact entries count as hits)")
    print("median standalone charge per category:", {t: int(statistics.median(v)) for t, v in by_type.items()},
          "; standalone sum", sum(charges.values()))
    for name, order in (("probe-list order", probes), ("worst order (largest standalone charge first)", worst)):
        line(name, *run_turns(order, 1, False)[:4])


if __name__ == "__main__":
    main()
