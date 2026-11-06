"""Write the subagent-shape fixture that test_subagent_room.py reads (D56).

Input: the capture directories of spikes/m2/subagents (``run.sh`` fg, stop, resume and ``drive.py``
bgidle, esc), one ``CX_SPIKE_DIR`` per Claude Code version. For each scenario the fixture keeps main's
last request's ``messages`` only, scrubbed: the context blocks of the first user message and the
environment system message become placeholders, and task output-file paths are dropped. The CLI's
own texts that the engine reads (the Agent results, the notices, the SendMessage results) stay as
captured. Zero spend: the captures came from a scripted stub, not a model.

    .venv/bin/python tests/fixtures/make_subagent_shapes.py 2.1.314=/tmp/sub-2.1.314 2.1.318=/tmp/sub-2.1.318
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

OUT = Path(__file__).with_name("subagent_shapes_2_1_31x.json")
SCENARIOS = ("fg", "stop", "resume", "bgidle", "esc")
OUTPUT_FILE = re.compile(r"<output-file>[^<]*</output-file>")
OUTPUT_LINE = re.compile(r"output_file: [^\\\s]+")  # in the launch receipt
PERSONAL = re.compile(r"/Users/|/home/|/private/|/var/folders/|/tmp/")


def scrub_text(text: str) -> str:
    text = OUTPUT_FILE.sub("<output-file>(scrubbed)</output-file>", text)
    return OUTPUT_LINE.sub("output_file: (scrubbed)", text)


def scrub(messages: list[dict]) -> list[dict]:
    out = []
    for i, message in enumerate(messages):
        content = message.get("content")
        if message.get("role") == "system" and "# Environment" in json.dumps(content):
            message = {**message, "content": "# Environment\n(scrubbed)"}
        elif i == 0 and isinstance(content, list):
            message = {**message, "content": [
                {**b, "text": "<system-reminder>\n(scrubbed)\n</system-reminder>"}
                if b.get("type") == "text" and b["text"].startswith("<system-reminder>") else b for b in content]}
        out.append(json.loads(scrub_text(json.dumps(message))))
    return out


def main(pairs: list[str]) -> None:
    fixture: dict[str, dict[str, list[dict]]] = {}
    for pair in pairs:
        version, root = pair.split("=", 1)
        fixture[version] = {}
        for scenario in SCENARIOS:
            last = sorted(Path(root, f"cap-{scenario}").glob("*-main.json"))[-1]
            fixture[version][scenario] = scrub(json.loads(last.read_text())["body"]["messages"])
    text = json.dumps(fixture, indent=1, ensure_ascii=False) + "\n"
    found = PERSONAL.search(text)
    if found:
        sys.exit(f"a path survived the scrub: {text[max(0, found.start() - 80):found.end() + 80]!r}")
    OUT.write_text(text)
    print(f"wrote {OUT} ({len(text):,} bytes)")


if __name__ == "__main__":
    main(sys.argv[1:])
