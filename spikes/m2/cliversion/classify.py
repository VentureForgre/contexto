"""Read check.sh's captured requests with Contexto's own recognisers and print what they find (D38).

For each CLI: the version its User-Agent names and whether Contexto was tested with it; whether the
first main-shaped request's session start is recognised (``Dialect.session_start_seen``); and, for
Claude Code, the kind of each role ``system`` message and context block (``classify_system``,
``context_block_kind``), with the first line of any block or system message of no recognised kind so
a changed heading is visible. Codex: each lead item's role and whether its parts are recognised
context sections. Text comes from a throwaway session in a temp directory; nothing is written.

Usage: python3 spikes/m2/cliversion/classify.py <out dir from check.sh>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from contexto.cliversions import TESTED_VERSIONS, client_version, is_tested
from contexto.codex import RESPONSES, is_context_part
from contexto.rewrite import ANTHROPIC, CONTEXT_TAG, _anthropic_classify_system, context_block_kind, message_text


def first_line(text: str) -> str:
    text = text.lstrip()
    if text.startswith(CONTEXT_TAG):
        text = text[len(CONTEXT_TAG):]
    return next((line.strip() for line in text.splitlines() if line.strip()), "")[:70]


def claude(body: dict) -> None:
    messages = body.get("messages") or []
    print(f"  model {body.get('model')}, {len(messages)} messages, tools {len(body.get('tools') or [])}")
    print(f"  session start recognised: {ANTHROPIC.session_start_seen(messages)}")
    for i, m in enumerate(messages):
        if m.get("role") == "system":
            cls = _anthropic_classify_system(messages, i)
            note = "" if cls == "session_start" else f"  first line: {first_line(message_text(m))!r}"
            print(f"  messages[{i}] system: {cls}{note}")
            continue
        for j, block in enumerate(m.get("content") if isinstance(m.get("content"), list) else []):
            text = block.get("text") if isinstance(block, dict) else None
            if isinstance(text, str) and text.lstrip().startswith(CONTEXT_TAG):
                kind = context_block_kind(block)
                note = "" if kind else f"  first line: {first_line(text)!r}"
                print(f"  messages[{i}] block {j}: {kind or 'no recognised kind'}{note}")


def codex(body: dict) -> None:
    items = body.get("input") or []
    lead = RESPONSES.lead_count(items)
    print(f"  model {body.get('model')}, {len(items)} items, lead {lead}")
    print(f"  session start recognised: {RESPONSES.session_start_seen(items)}")
    for i, item in enumerate(items):
        parts = item.get("content") if isinstance(item.get("content"), list) else []
        flags = ["section" if is_context_part(p) else f"text {first_line(p.get('text') or '')!r}"
                 for p in parts if isinstance(p, dict)]
        where = "lead" if i < lead else "turn"
        print(f"  input[{i}] {where} {item.get('type', 'message')}/{item.get('role')}: {', '.join(flags) or '-'}")


def main(out: Path) -> int:
    rows = [json.loads(line) for line in (out / "requests.jsonl").read_text().splitlines() if line.strip()]
    done: set[str] = set()
    for row in rows:
        found = client_version({"user-agent": row.get("user_agent") or ""})
        if not found or not row.get("body") or found[0] in done:
            continue
        cli, client, version = found
        body = json.loads((out / "bodies" / row["body"]).read_text())
        if cli == "claude" and not body.get("tools"):
            continue  # a side request; the main one carries the session start
        done.add(cli)
        tested = "tested" if is_tested(cli, version) else f"NOT tested (tested: {', '.join(TESTED_VERSIONS[cli])})"
        print(f"{cli}: {client}/{version}, {tested}")
        (claude if cli == "claude" else codex)(body)
    for cli in ("claude", "codex"):
        if cli not in done:
            print(f"{cli}: no main request captured")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
