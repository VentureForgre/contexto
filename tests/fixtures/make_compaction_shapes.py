"""Write the compaction-shape fixture that test_clear_main.py reads (D59).

Input: capture directories of Claude Code 2.1.314 and 2.1.318 against a loopback Messages stub (zero
spend: the summary is the stub's text, not a model's), one per scenario: headless ``/compact``
(``claude -p --input-format stream-json``), headless auto-compaction (``claude -p`` whose first
response is a Bash call reporting 995,000 input tokens) and interactive ``/compact`` in a pty, all
with Claude Code's default attribution reminder on; and, with ``attribution`` set empty, headless
``/compact`` on 2.1.318 and auto-compaction on 2.1.314. Each directory holds ``req/NNN.json``, one
request per file (``method``, ``path``, ``headers``, ``body``).

For each Messages request the fixture keeps the User-Agent, whether a tool list was sent, a
per-capture session number, and the ``messages``, scrubbed: a ``<system-reminder>`` text block of the
first user message and every role ``system`` message become placeholders, and the transcript path in
the summary is dropped. The summary's own text (the sentence, the blank line, the tools-used reminder)
and the command blocks stay as captured.

    .venv/bin/python tests/fixtures/make_compaction_shapes.py 2.1.318:compact=/tmp/cap/318-compact ...
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

OUT = Path(__file__).with_name("compaction_shapes_2_1_31x.json")
TRANSCRIPT = re.compile(r"read the full transcript at: \S+")
PERSONAL = re.compile(r"/Users/|/home/|/private/|/var/folders/|/tmp/")


def scrub(messages: list[dict]) -> list[dict]:
    out = []
    for i, message in enumerate(messages):
        content = message.get("content")
        if message.get("role") == "system":
            message = {**message, "content": [{"type": "text", "text": "(scrubbed)"}]}
        elif i == 0 and isinstance(content, list):
            message = {**message, "content": [
                {**b, "text": "<system-reminder>\n(scrubbed)\n</system-reminder>\n"}
                if b.get("type") == "text" and b["text"].startswith("<system-reminder>") else b for b in content]}
        text = TRANSCRIPT.sub("read the full transcript at: (scrubbed)", json.dumps(message, ensure_ascii=False))
        out.append(json.loads(text))
    return out


def requests(root: Path) -> list[dict]:
    sessions: dict[str, int] = {}
    found = []
    for path in sorted((root / "req").glob("*.json")):
        record = json.loads(path.read_text())
        body = record["body"]
        if not body or not record["path"].startswith("/v1/messages") or "count_tokens" in record["path"]:
            continue
        headers = {k.lower(): v for k, v in record["headers"].items()}
        session = sessions.setdefault(headers.get("x-claude-code-session-id", ""), len(sessions) + 1)
        found.append({"user_agent": headers["user-agent"], "tools": bool(body.get("tools")), "session": session,
                      "messages": scrub(body["messages"])})
    return found


def main(pairs: list[str]) -> None:
    fixture: dict[str, dict[str, list[dict]]] = {}
    for pair in pairs:
        name, root = pair.split("=", 1)
        version, scenario = name.split(":", 1)
        fixture.setdefault(version, {})[scenario] = requests(Path(root))
    text = json.dumps(fixture, indent=1, ensure_ascii=False) + "\n"
    found = PERSONAL.search(text)
    if found:
        sys.exit(f"a path survived the scrub: {text[max(0, found.start() - 80):found.end() + 80]!r}")
    OUT.write_text(text)
    print(f"wrote {OUT} ({len(text):,} bytes)")


if __name__ == "__main__":
    main(sys.argv[1:])
