"""OFFLINE ESTIMATE (zero spend, no model): the D27 6x9 probe protocol on the real engine, store and MCP code.

Rebuilds the second live H1 run's content turns byte for byte with the committed ``run.plan()``, feeds
them through the real lane A ``RewriteEngine`` (main defaults: bound 150,000, headroom floor 25,000,
the engine's running headroom rule) with a real ``SessionStore`` in a temporary directory, and runs a
deterministic ideal search policy through the real ``mcp.Server`` against that store. The engine
decides every rotation and page out. Usage is synthesized (see METHOD in the output).

Prints numbers only, never page text, plant values or statements.

    uv run python spikes/m2/estimate/sim_6x9.py
"""
from __future__ import annotations

import io
import json
import math
import random
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for _p in (ROOT / "spikes" / "m2", ROOT / "spikes" / "m0", ROOT / "src"):
    sys.path.insert(0, str(_p))
from acceptance import run  # noqa: E402

from contexto import mcp, rewrite  # noqa: E402
from contexto.canonical import canonical_bytes  # noqa: E402
from contexto.store import SessionStore  # noqa: E402

# Live H1 rerun figures (the second live H1 run) -----------------------------------------------------
PAGED = 15  # the live run paged out turns 0..14; turns 15..17 stayed in view
WINDOW, BPT = 81_737, 2.123986182634373  # its recorded window budget W and measured bytes per token
STANDALONE_ROOM = 23_000  # the live probe-turn room, used for the standalone (worst-first) charges
BOUND, FLOOR = 150_000, 25_000  # main defaults (rewrite.Config)
STATIC_ROOMS = (29_562, 10_758, 9_749, 8_728, 7_689, 6_685)  # builder-4's per-turn rooms (pessimistic)
D27_SEED = 20261016
EXPECTED_T1 = ["q_a_001", "q_a_020", "q_a_028", "q_p_03", "q_a_016", "q_a_038", "q_a_046", "q_a_002", "q_a_006"]

# Usage synthesis (stated in METHOD) ---------------------------------------------------------------
TOOL_LOOP_OUTPUT = 200  # output tokens of every request whose response is a tool_use
ANSWER_OUTPUT = 600  # output tokens of the request whose response is a probe turn's final answer
CONTENT_OUTPUT = 20  # output tokens of a content turn's "OK" reply (acceptance budget.Params.reply_tokens)
ANSWER_CHARS = round(ANSWER_OUTPUT * BPT)  # about 1,275 characters of final answer text
MODEL = "sim-model"
TOOLS_IN_HEAD = 21  # the live probe-turn request carried 21 tools

script, _ = run.plan()
# Content turns only: after D27 (PR #112) the script ends with six probe turns, before it with one.
TURNS = list(getattr(script, "content_turns", script.turns[:-1]))
PROBES = list(script.probes)
TOTALS = Counter(p.type for p in PROBES)
CATS = ("paged_only", "multihop", "chain", "obligations", "distractor")
STATIC_IN_VIEW = "\n".join(TURNS[PAGED:])

# The acceptance script's probe-turn wording (script.build); it names no tools.
PROBE_HEAD = [
    "Final step. Answer the questions below about this conversation. Reply with one line per question, "
    "in the form",
    "q_id: answer",
    "with the answer only. For a list, give the ids separated by commas. If the conversation does not "
    "contain it, say unknown.",
    "",
]

OUT: list[str] = []


def say(*parts) -> None:
    line = " ".join(str(p) for p in parts)
    OUT.append(line)
    print(line)


def d27_order() -> list:
    order = list(PROBES)
    random.Random(D27_SEED).shuffle(order)
    return order


# The ideal policy (copied from .contexto/spikes/b15b16-estimate/whatif.py, kept exactly) ----------

def find(sess, query, needed):
    """Search ``query``; if the needed statements are not all returned or in view, one recall on the
    first hit at its last shown matching line's offset. Returns the text the model would have seen."""
    seen = sess.call("contexto_search", {"query": query})
    if all(s in seen or sess.visible(s) for s in needed):
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
    have = sess.have
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


def ran_out_text(text: str) -> bool:
    return text == mcp.BUDGET_SPENT or text.startswith("The recall budget for this turn is nearly used up")


def tool_call(server, name, args) -> str:
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    return "".join(b["text"] for b in server.handle_line(line)["result"]["content"])


# Harness-style static session (pages 0..14 paged by hand, turns 15..17 in view) --------------------

class StaticSession:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="sim6x9-static-")
        self.store = SessionStore(Path(self.tmp), "est")
        self.store.page_out([[{"role": "user", "content": [{"type": "text", "text": t}]},
                              {"role": "assistant", "content": [{"type": "thinking", "thinking": ""},
                                                                {"type": "text", "text": "OK"}]}]
                             for t in TURNS[:PAGED]], 0)
        self.store.set_window(WINDOW, BPT)
        self.server = mcp.Server(self.store, io.StringIO(), io.StringIO())
        self.calls, self.ran_out, self.charged = Counter(), 0, 0

    def turn(self, turn_id, room):
        self.store.set_turn_room(turn_id, max(0, int(room)))

    def call(self, name, args):
        before = self.store.turn_room()[2]
        text = tool_call(self.server, name, args)
        self.charged += self.store.turn_room()[2] - before
        self.calls[name] += 1
        self.ran_out += ran_out_text(text)
        return text

    def visible(self, s):
        return s in STATIC_IN_VIEW

    def have(self, s, seen):
        return s in seen or s in STATIC_IN_VIEW

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def standalone_charge(p) -> int:
    sess = StaticSession()
    sess.turn("s", STANDALONE_ROOM)
    answerable(sess, p)
    spent = sess.store.turn_room()[2]
    sess.close()
    return spent


# The engine-driven session ---------------------------------------------------------------------

def head(head_bytes: int) -> tuple[list, list]:
    """A synthetic Claude Code head: one system text block and 21 tool definitions of neutral filler,
    about ``head_bytes`` bytes of canonical JSON in all."""
    unit = "Follow the operator's instructions and use the tools when they help. "
    sys_bytes = head_bytes // 3
    system = [{"type": "text", "text": (unit * (sys_bytes // len(unit) + 1))[:sys_bytes]}]
    per_tool = max(0, (head_bytes - sys_bytes) // TOOLS_IN_HEAD - 120)
    desc = (unit * (per_tool // len(unit) + 1))[:per_tool]
    tools = [{"name": f"tool_{i:02d}", "description": desc,
              "input_schema": {"type": "object", "properties": {"arg": {"type": "string"}}}}
             for i in range(TOOLS_IN_HEAD)]
    return system, tools


def text_of(message: dict) -> list[str]:
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    out = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            out.append(b.get("text", ""))
        elif b.get("type") == "tool_result":
            c = b.get("content")
            out.extend([c] if isinstance(c, str) else [x.get("text", "") for x in c or [] if isinstance(x, dict)])
    return out


class EngineSession:
    """Claude Code's request sequence driven through the real engine, store and MCP server."""

    def __init__(self, head_bytes: int, usage_mode: str = "estimate"):
        self.tmp = tempfile.mkdtemp(prefix="sim6x9-engine-")
        self.store = SessionStore(Path(self.tmp), "sim")
        # The engine's own restore path (packet A8): a previous engine's ratio seeds the estimator.
        self.store.set_window(WINDOW, BPT, model=MODEL)
        self.engine = rewrite.RewriteEngine(self.store, config=rewrite.Config(bound=BOUND))  # launcher passes bound only
        self.server = mcp.Server(self.store, io.StringIO(), io.StringIO())
        system, tools = head(head_bytes)
        self.base = {"model": MODEL, "max_tokens": 32_000, "system": system, "tools": tools}
        self.usage_mode = usage_mode
        self.messages: list[dict] = []
        self.pending = None
        self.events: list[tuple[int, dict]] = []  # (request number, event)
        self.requests = 0
        self.view_all = self.view_turns = ""
        self.calls, self.n_tool = Counter(), 0
        self.turn_log: dict | None = None
        self.sources = Counter()

    # requests and usage, as the proxy drives them
    def send(self):
        request = {**self.base, "messages": list(self.messages)}
        upstream, _, events = self.engine(request, {}, rewrite.MESSAGES)
        self.requests += 1
        self.events += [(self.requests, e) for e in events]
        state = self.engine.states[rewrite.keyed(rewrite.ANTHROPIC, request)]
        blocks = [t for m in upstream["messages"] for t in text_of(m)]
        self.view_all = "\n".join(blocks)
        retained = set(blocks)
        self.view_turns = "\n".join(t for t in TURNS if t in retained)
        self.pending = (request, state.last_estimate, len(canonical_bytes(upstream)))
        return state

    def usage(self, output: int):
        request, estimate, sent = self.pending
        real = estimate if self.usage_mode == "estimate" else math.ceil(sent / BPT)
        events = self.engine.observe_usage(rewrite.MESSAGES, {"input_tokens": real, "output_tokens": output}, request)
        self.events += [(self.requests, e) for e in events]
        self.pending = None

    def state(self):
        return list(self.engine.states.values())[0]

    def content_turn(self, text: str):
        self.messages.append({"role": "user", "content": [{"type": "text", "text": text}]})
        self.send()
        self.usage(CONTENT_OUTPUT)
        self.messages.append({"role": "assistant", "content": [{"type": "thinking", "thinking": ""},
                                                               {"type": "text", "text": "OK"}]})

    # a probe turn
    def start_probe_turn(self, chunk):
        text = "\n".join([*PROBE_HEAD, *[f"{p.id}: {p.question}" for p in chunk]])
        self.messages.append({"role": "user", "content": [{"type": "text", "text": text}]})
        first_request = self.requests + 1
        st = self.send()
        hr = rewrite.headroom(st, self.engine.config)
        request_room = self.store.turn_room()[1]
        self.turn_log = {"first_request": first_request, "headroom": hr, "request_room": request_room,
                         "rooms": [], "charged": 0, "ran_out": 0, "calls": Counter()}

    def end_probe_turn(self, chunk):
        if self.pending is not None:
            self.usage(ANSWER_OUTPUT)
        lines, per = [], max(1, ANSWER_CHARS // len(chunk))
        for p in chunk:
            prefix = f"{p.id}: "
            lines.append(prefix + ("answer " * per)[:max(0, per - len(prefix) - 1)])
        self.messages.append({"role": "assistant", "content": [{"type": "text", "text": "\n".join(lines)}]})
        log, self.turn_log = self.turn_log, None
        log["last_request"] = self.requests
        return log

    def call(self, name, args):
        if self.pending is not None:
            self.usage(TOOL_LOOP_OUTPUT)  # the response that carried this tool_use
        room = self.store.turn_room()[1]
        self.turn_log["rooms"].append(room)
        before = self.store.turn_room()[2]
        text = tool_call(self.server, name, args)
        self.turn_log["charged"] += self.store.turn_room()[2] - before
        self.turn_log["ran_out"] += ran_out_text(text)
        self.turn_log["calls"][name] += 1
        self.n_tool += 1
        tid = f"toolu_{self.n_tool:05d}"
        self.messages.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": f"mcp__contexto__{name}", "input": args}]})
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": [{"type": "text", "text": text}]}]})
        self.send()
        return text

    def visible(self, s):
        return s in self.view_all

    def have(self, s, seen):
        if s in seen:
            self.sources["returned_by_this_probe"] += 1
            return True
        if s in self.view_turns:
            self.sources["in_retained_content_turn"] += 1
            return True
        if s in self.view_all:
            self.sources["in_other_context(prior_tool_results_or_preamble)"] += 1
            return True
        return False

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def k_label(k: int, starts: list[int]) -> str:
    """The first retained turn for a message index k: content turn c (0..17) or probe turn Tj."""
    if k == 0:
        return "none paged"
    idx = starts.index(k) if k in starts else None
    if idx is None:
        return f"msg {k}"
    return f"content turn {idx}" if idx < len(TURNS) else f"probe T{idx - len(TURNS) + 1}"


def run_engine(head_bytes: int, chunks, usage_mode="estimate", verbose=True):
    sess = EngineSession(head_bytes, usage_mode)
    for t in TURNS:
        sess.content_turn(t)
    pre = sess.state()
    pre_info = {"k": pre.k, "turns_paged": pre.turns_paged, "window_store": sess.store.window()[0],
                "rotations": sum(1 for _, e in sess.events if e["event"] == "rotation"),
                "requests": sess.requests}
    ok, turn_rows, per_probe_ok = Counter(), [], {}
    for _k, chunk in enumerate(chunks, 1):
        sess.start_probe_turn(chunk)
        tok = Counter()
        for p in chunk:
            if answerable(sess, p):
                ok[p.type] += 1
                tok[p.type] += 1
                per_probe_ok[p.id] = True
            else:
                per_probe_ok[p.id] = False
        log = sess.end_probe_turn(chunk)
        log["ok"] = sum(tok.values())
        log["store_window"] = sess.store.window()[0]
        turn_rows.append(log)
    starts = rewrite.turn_starts(sess.messages)
    result = {"ok": ok, "turns": turn_rows, "pre": pre_info, "events": sess.events, "starts": starts,
              "ratio": sess.engine.estimator.measured_bytes_per_token, "sources": sess.sources,
              "final_state": sess.state(), "requests": sess.requests, "per_probe": per_probe_ok}
    sess.close()
    return result


def calibrate_head() -> tuple[int, int]:
    """Head bytes so the store's W after the content turns equals the live run's recorded 81,737."""
    head_bytes = 90_000
    w = None
    best = None
    for _ in range(5):
        sess = EngineSession(head_bytes)
        for t in TURNS:
            sess.content_turn(t)
        w = sess.store.window()[0]
        sess.close()
        if best is None or abs(w - WINDOW) < abs(best[1] - WINDOW):
            best = (head_bytes, w)
        if w == WINDOW:
            break
        head_bytes = max(1_000, head_bytes + round((w - WINDOW) * BPT))
    return best


def run_static(chunks):
    sess = StaticSession()
    ok, rows = Counter(), []
    for k, (chunk, room) in enumerate(zip(chunks, STATIC_ROOMS), 1):
        sess.turn(f"t{k}", room)  # B14: a new publish resets the spent counter
        c0, r0, tok = sess.charged, sess.ran_out, 0
        for p in chunk:
            if answerable(sess, p):
                ok[p.type] += 1
                tok += 1
        rows.append({"room": room, "charged": sess.charged - c0, "ran_out": sess.ran_out - r0, "ok": tok})
    sess.close()
    return ok, rows


def cats_line(ok: Counter) -> str:
    return " ".join(f"{t} {ok[t]}/{TOTALS[t]}" for t in CATS)


def report_engine(label: str, res) -> None:
    say(f"=== {label}")
    pre = res["pre"]
    say(f"  before probes: {pre['requests']} requests, {pre['rotations']} rotations, first retained "
        f"{k_label(pre['k'], res['starts'])}, turns paged {pre['turns_paged']}, store W {pre['window_store']}")
    for i, row in enumerate(res["turns"], 1):
        rooms = row["rooms"]
        rots = [e for n, e in res["events"] if e["event"] == "rotation" and n == row["first_request"]]
        rot = "; ".join(f"rotation({e['reason']}) k-> {k_label(e['k'], res['starts'])}, turns_paged "
                        f"{e['turns_paged']}, F {e.get('fixed_overhead')}, W {e.get('window_budget')}"
                        for e in rots) or "no rotation"
        noop = [e for n, e in res["events"] if e["event"] == "rotation_noop" and n == row["first_request"]]
        if noop:
            rot += f"; rotation_noop W {noop[0].get('window_budget')}"
        say(f"  T{i}: headroom {row['headroom']}; room at request time {row['request_room']}; room seen by "
            f"first call {rooms[0] if rooms else None}; min/max room seen {min(rooms) if rooms else None}/"
            f"{max(rooms) if rooms else None}; calls {dict(row['calls'])}; charged {row['charged']}; "
            f"budget-spent replies {row['ran_out']}; answerable {row['ok']}/9; {rot}")
    ok = res["ok"]
    total = sum(ok.values())
    say(f"  answerable: {cats_line(ok)}; total {total}/54 vs bar 44/54 ({'meets' if total >= 44 else 'below'})")
    names = Counter(e["event"] for _, e in res["events"])
    say("  engine events:", dict(sorted(names.items())))
    fs = res["final_state"]
    say(f"  requests {res['requests']}; final k -> {k_label(fs.k, res['starts'])}; turns paged {fs.turns_paged}; "
        f"degraded {fs.degraded}; estimator bytes/token at end {res['ratio']:.4f}")
    say("  where needed statements were found:", dict(res["sources"]))


def main() -> int:
    order = d27_order()
    assert [p.id for p in order[:9]] == EXPECTED_T1, [p.id for p in order[:9]]
    chunks = [order[9 * k:9 * (k + 1)] for k in range(6)]
    assert TOTALS == Counter({"chain": 30, "multihop": 8, "obligations": 6, "distractor": 6, "paged_only": 4})

    charges = {p.id: standalone_charge(p) for p in PROBES}
    worst_chunks = [sorted(c, key=lambda p: charges[p.id], reverse=True) for c in chunks]
    head_bytes, w_cal = calibrate_head()

    say("ESTIMATE (offline, simulated policy, no model)")
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent, capture_output=True,
                             text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        sha = "unknown"
    say(f"code: {sha}")
    say("METHOD")
    say("  content: run.plan() 18 content turns byte for byte (user text, then assistant [empty thinking, 'OK']),"
        " one request each through the real RewriteEngine; Config(bound=150000) as the launcher builds it"
        " (headroom floor 25000, running headroom rule, window_fraction 0.5); real SessionStore in a temp dir;"
        " the engine decides every rotation and page out.")
    say(f"  head: synthetic system text plus {TOOLS_IN_HEAD} tool definitions, {head_bytes} bytes, sized so the"
        f" store's W after the content turns equals the live recorded {WINDOW} (got {w_cal}).")
    say(f"  usage synthesis: input_tokens = the engine's own pre-send estimate of each upstream request"
        f" (state.last_estimate), no cache fields; output_tokens = {TOOL_LOOP_OUTPUT} per tool-loop request,"
        f" {ANSWER_OUTPUT} for the request answered by a probe turn's final answer, {CONTENT_OUTPUT} per"
        f" content-turn 'OK'. Fed back with engine.observe_usage after each request, as the proxy does, so"
        f" rooms are published at request time and at usage time (each publish resets spent, B14).")
    say(f"  estimator seed: the store holds the live ratio {BPT:.4f} bytes/token before the first request, so"
        f" the engine's own restore path (A8) seeds it; without this the 3.5 text seed would be fed back as"
        f" usage and never corrected.")
    say("  D27: order = PROBES shuffled with random.Random(20261016); turn k asks order[9(k-1):9k]; user"
        " message = the acceptance probe-turn wording (names no tools) then 'q_id: question' lines.")
    say("  tool loop: each tool call is one more request (assistant tool_use, user tool_result with the"
        f" exact server text); final answer about {ANSWER_CHARS} characters of neutral 'q_id: answer' filler.")
    say("  policy: the ideal policy searches every probe (exact key or subject; one recall on the first hit"
        " at its last shown matching line when the needed line is neither returned nor in view). In view ="
        " any text in the latest upstream request (retained turns, preamble, earlier tool results).")
    say(f"  worst-first: within each turn, largest standalone charge first (fresh harness store, pages 0..14,"
        f" room {STANDALONE_ROOM}).")
    say("  static: harness store (pages 0..14 paged by hand, turns 15..17 in view, W 81737), rooms fixed per"
        f" turn {list(STATIC_ROOMS)}, reset at each turn, no republish within a turn; in view = turns 15..17.")
    say(f"totals: {cats_line(TOTALS)}; bar 44/54")
    say("T1 assignment check: passed")
    say("standalone charge per probe (fresh 23000 room): min", min(charges.values()), "max",
        max(charges.values()), "sum", sum(charges.values()))

    res = run_engine(head_bytes, chunks)
    report_engine("A engine sim, D27 order", res)
    res_w = run_engine(head_bytes, worst_chunks)
    report_engine("B engine sim, worst-first within each turn", res_w)

    ok_s, rows = run_static(chunks)
    say("=== C static rooms (pessimistic), D27 order")
    for i, row in enumerate(rows, 1):
        say(f"  T{i}: room {row['room']}; charged {row['charged']}; budget-spent replies {row['ran_out']};"
            f" answerable {row['ok']}/9")
    total = sum(ok_s.values())
    say(f"  answerable: {cats_line(ok_s)}; total {total}/54 vs bar 44/54 ({'meets' if total >= 44 else 'below'})")

    res_b = run_engine(head_bytes, chunks, usage_mode="bytes")
    say("=== D sensitivity: engine sim, D27 order, input usage = upstream request bytes / 2.124 instead of"
        " the engine's estimate")
    say(f"  answerable: {cats_line(res_b['ok'])}; total {sum(res_b['ok'].values())}/54; rooms seen by first"
        f" call per turn {[r['rooms'][0] if r['rooms'] else None for r in res_b['turns']]}; charged per turn"
        f" {[r['charged'] for r in res_b['turns']]}")
    lost = [(i, pid) for i, c in enumerate(chunks, 1) for pid in [p.id for p in c] if not res_b["per_probe"][pid]]
    say(f"  min room seen per turn {[min(r['rooms']) if r['rooms'] else None for r in res_b['turns']]};"
        f" budget-spent replies per turn {[r['ran_out'] for r in res_b['turns']]}; not answerable (turn, id) {lost};"
        f" rotations at probe turns {[i for i, r in enumerate(res_b['turns'], 1) if any(e['event'] == 'rotation' and n == r['first_request'] for n, e in res_b['events'])]}")
    reasons = [(i, e["reason"]) for i, r in enumerate(res_b["turns"], 1) for n, e in res_b["events"]
               if e["event"] == "rotation" and n == r["first_request"]]
    say(f"  rotation reasons at probe turns {reasons}; engine events:",
        dict(sorted(Counter(e["event"] for _, e in res_b["events"]).items())))

    out = Path(__file__).with_name("sim_6x9.out.txt")
    try:
        out.write_text("\n".join(OUT) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"could not write {out.name}: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
