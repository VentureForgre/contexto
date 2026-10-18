#!/usr/bin/env python3
"""Write a synthetic Claude Code 2.1.302 session JSONL of a target size, in the format observed
by probe.py (user / assistant records chained by parentUuid; tool results carry the
`toolUseResult` sidecar the CLI writes, so file contents appear twice on disk as they do in a
real session).

Each turn is about 20,000 to 26,000 tokens: a user request, an assistant text plus a Read
tool_use, a user tool_result holding a synthetic source file (with Read's line-number prefix),
and an assistant analysis. Tokens are counted as JSON bytes of message content / BPT (1.83, the
same ratio the fake upstream's honest mode uses).

--usage contexto: every assistant record reports input_tokens = --const (100,000), as a
                  session run through Contexto records.
--usage honest:   each assistant record reports the cumulative honest token count so far.

Usage: gen_session.py <projects_dir> <cwd> <target_tokens> [--usage contexto|honest]
                      [--const N] [--seed S]
Prints one JSON line: session_id, turns, tokens, content_bytes, file_bytes, path.
"""
import argparse
import json
import os
import random
import uuid
from datetime import UTC, datetime, timedelta

BPT = 1.83
VERSION = "2.1.302"
MODEL = "claude-opus-5-5"

WORDS = ("cache index buffer route token frame record window page store event batch slot queue "
         "parser reader writer config budget anchor marker shard ledger digest stream cursor").split()
VERBS = "load save merge split check build parse emit flush rotate trim score fetch apply".split()


def code_line(r: random.Random, turn: int) -> str:
    a, b, v = r.choice(WORDS), r.choice(WORDS), r.choice(VERBS)
    k = r.randrange(6)
    if k == 0:
        return f"def {v}_{a}_{b}_{turn}(self, {a}: dict, {b}: int = {r.randrange(1000)}) -> list[str]:"
    if k == 1:
        return f"    {a}_{b} = self.{v}_{a}({b}, limit={r.randrange(4096)})  # turn {turn} {r.randrange(10**6)}"
    if k == 2:
        return f"    if {a} is None or len({b}) > {r.randrange(9999)}:"
    if k == 3:
        return f"        raise ValueError(f\"{v} {a} failed for {{{b}!r}} at step {r.randrange(500)}\")"
    if k == 4:
        return f"    return [{a}.{v}() for {a} in self.{b}s if {a}.ok]"
    return f"class {a.title()}{b.title()}{turn}{r.randrange(100)}({v.title()}Base):"


def file_text(r: random.Random, turn: int, target_bytes: int) -> str:
    lines, n = [], 0
    while n < target_bytes:
        ln = code_line(r, turn)
        lines.append(ln)
        n += len(ln) + 1
    return "\n".join(lines) + "\n"


def numbered(text: str) -> str:
    rows = text.split("\n")
    return "\n".join(f"{i}\t{ln}" for i, ln in enumerate(rows, 1))


def prose(r: random.Random, turn: int, n_bytes: int) -> str:
    out, n = [], 0
    while n < n_bytes:
        s = (f"The {r.choice(WORDS)} {r.choice(VERBS)} path in turn {turn} keeps "
             f"{r.randrange(10000)} {r.choice(WORDS)} entries and the {r.choice(WORDS)} check "
             f"returns {r.choice(['early', 'late', 'twice', 'once'])}.")
        out.append(s)
        n += len(s) + 1
    return " ".join(out)


def tok(content) -> int:
    return int(len(json.dumps(content)) / BPT)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("projects")
    ap.add_argument("cwd")
    ap.add_argument("target_tokens", type=int)
    ap.add_argument("--usage", choices=("contexto", "honest"), default="contexto")
    ap.add_argument("--const", type=int, default=100_000)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    r = random.Random(a.seed)
    sid = str(uuid.uuid4())
    enc = "".join(c if c.isalnum() else "-" for c in a.cwd)
    d = os.path.join(a.projects, enc)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, sid + ".jsonl")
    # Records are 2 s apart and the newest lands about two hours ago, so no timestamp is in the
    # future even at 300M tokens (about 52,000 records).
    est_records = 4 * (a.target_tokens // 20_000 + 2)
    t = datetime.now(UTC) - timedelta(hours=2, seconds=2 * est_records)
    common = {"isSidechain": False, "userType": "external", "entrypoint": "sdk-cli", "cwd": a.cwd,
              "sessionId": sid, "version": VERSION, "gitBranch": "HEAD"}
    parent = None
    total = content_bytes = turns = 0
    n_msg = 0

    def ts() -> str:
        nonlocal t
        t += timedelta(seconds=2)
        return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"

    last_usage = [0]  # input_tokens of the newest assistant record: what the CLI counts from

    def usage() -> dict:
        inp = a.const if a.usage == "contexto" else total
        last_usage[0] = inp
        return {"input_tokens": inp, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                "output_tokens": 600, "service_tier": "standard"}

    with open(path, "w") as f:
        def put(rec: dict) -> None:
            nonlocal parent, total, content_bytes, n_msg
            c = rec["message"]["content"]
            total += tok(c)
            content_bytes += len(json.dumps(c))
            n_msg += 1
            f.write(json.dumps(rec) + "\n")
            parent = rec["uuid"]

        while total < a.target_tokens:
            turns += 1
            turn_tokens = r.randrange(20_000, 26_001)
            fname = f"{a.cwd}/src/{r.choice(WORDS)}_{r.choice(WORDS)}_{turns}.py"
            pid = str(uuid.uuid4())
            put({"parentUuid": parent, **common, "promptId": pid, "type": "user",
                 "message": {"role": "user", "content": f"Turn {turns}: open {fname} and explain how the "
                             f"{r.choice(WORDS)} {r.choice(VERBS)} step handles the {r.choice(WORDS)} limit. "
                             + prose(r, turns, 200)},
                 "uuid": str(uuid.uuid4()), "timestamp": ts(), "permissionMode": "default",
                 "promptSource": "sdk"})
            tid = f"toolu_synth{turns:06d}"
            put({"parentUuid": parent, **common, "type": "assistant", "requestedModel": MODEL,
                 "message": {"id": f"msg_synthA{turns:06d}", "type": "message", "role": "assistant", "model": MODEL,
                             "content": [{"type": "text", "text": prose(r, turns, 300)},
                                         {"type": "tool_use", "id": tid, "name": "Read",
                                          "input": {"file_path": fname}}],
                             "stop_reason": "tool_use", "stop_sequence": None, "usage": usage()},
                 "uuid": str(uuid.uuid4()), "timestamp": ts()})
            src = file_text(r, turns, int((turn_tokens - 900) * BPT * 0.93))
            body = numbered(src)
            a_uuid = parent
            put({"parentUuid": parent, **common, "promptId": pid, "type": "user",
                 "message": {"role": "user", "content": [{"tool_use_id": tid, "type": "tool_result", "content": body}]},
                 "uuid": str(uuid.uuid4()), "timestamp": ts(),
                 "toolUseResult": {"type": "text", "file": {"filePath": fname, "content": src,
                                   "numLines": src.count("\n") + 1, "startLine": 1,
                                   "totalLines": src.count("\n") + 1}},
                 "sourceToolAssistantUUID": a_uuid})
            put({"parentUuid": parent, **common, "type": "assistant", "requestedModel": MODEL,
                 "message": {"id": f"msg_synthB{turns:06d}", "type": "message", "role": "assistant", "model": MODEL,
                             "content": [{"type": "text", "text": prose(r, turns, 1200)}],
                             "stop_reason": "end_turn", "stop_sequence": None, "usage": usage()},
                 "uuid": str(uuid.uuid4()), "timestamp": ts()})
        f.write(json.dumps({"type": "last-prompt", "lastPrompt": f"Turn {turns}", "leafUuid": parent,
                            "sessionId": sid}) + "\n")
    print(json.dumps({"session_id": sid, "turns": turns, "messages": n_msg, "tokens": total,
                      "planted_usage": last_usage[0] + 600,  # input + output_tokens of that record
                      "content_bytes": content_bytes, "file_bytes": os.path.getsize(path), "path": path}))


if __name__ == "__main__":
    main()
