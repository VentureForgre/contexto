"""D63 measurement: tool loops replayed through the real engine and a real ``SessionStore``, with
in-turn paging off (what v0.5.24 sends) and on. Zero spend: the model is the fake upstream of
``tests/inturn_harness.py``, which also refuses any thinking block whose prefix changed.

    uv run python spikes/m2/inturn/d63_measure.py            # the table in docs/DECISIONS.md D63
    uv run python spikes/m2/inturn/d63_measure.py --digest   # a hash of every forwarded body, paging off

``--digest`` runs on any checkout from v0.5.24 on (it never names the new setting when the engine has
none), so the same command on two checkouts shows whether paging off sends v0.5.24's bytes.

Scenarios, all at the default configuration (bound 150,000, headroom floor 25,000, minimum turn room
12,000, so the in-turn trigger is 113,000) with a 20,000-token head, the fake tokenizer at 3.2 bytes a
token:

- ``loop120``: one prompt, 120 tool rounds of about 6,400 tokens each.
- ``huge3``: 12 rounds, three of them one result of about 160,000 tokens.
- ``small1500``: 1,500 rounds of 400-byte results.
- ``subagent``: main launches a subagent whose one turn runs 120 rounds of about 6,400 tokens.
- ``images``: two screenshots in the prompt, 60 rounds of 9,000 bytes of text and one image each.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))

from contexto.canonical import canonical_bytes  # noqa: E402
from contexto.rewrite import Config, RewriteEngine  # noqa: E402
from contexto.store import SessionStore  # noqa: E402
from inturn_harness import AGENT, Cli, output, png  # noqa: E402

RESULT = 20_480  # bytes: about 6,400 tokens
HAS_SWITCH = "in_turn_paging" in Config.__dataclass_fields__


def config(paging: bool) -> Config:
    return Config(in_turn_paging=paging) if HAS_SWITCH else Config()


def loop120(store, paging: bool) -> list[Cli]:
    cli = Cli(store, config(paging))
    cli.prompt("Read every module and report what each one does.")
    cli.loop(120, lambda r: output(f"module-{r:04d}", RESULT))
    return [cli]


def huge3(store, paging: bool) -> list[Cli]:
    cli = Cli(store, config(paging))
    cli.prompt("Read the three dumps and the notes around them.")
    cli.loop(12, lambda r: output(f"dump-{r:02d}", 512_000 if r in (3, 6, 9) else RESULT))
    return [cli]


def small1500(store, paging: bool) -> list[Cli]:
    cli = Cli(store, config(paging))
    cli.prompt("Check each of the 1,500 files for the marker.")
    cli.loop(1_500, lambda r: output(f"file-{r:04d}", 400), lambda r: ("Grep", {"pattern": "marker", "path": f"src/{r}"}))
    return [cli]


def subagent(store, paging: bool) -> list[Cli]:
    engine = RewriteEngine(store, config=config(paging))
    main = Cli(store, engine=engine)
    main.prompt("Have an agent audit the modules.")
    main.send([("Agent", {"prompt": "Audit every module.", "description": "audit"})])
    side = Cli(store, engine=engine, headers=AGENT)
    side.prompt("Audit every module.")
    side.loop(120, lambda r: output(f"audit-{r:04d}", RESULT))
    return [side]


def images(store, paging: bool) -> list[Cli]:
    cli = Cli(store, config(paging))
    cli.prompt("Compare these two screenshots with the pages you fetch.", png(1_440, 900), png(1_440, 900))
    cli.loop(60, lambda r: [{"type": "text", "text": output(f"shot-{r:03d}", 9_000)}, png(1_000, 1_000)])
    return [cli]


SCENARIOS = {"loop120": loop120, "huge3": huge3, "small1500": small1500, "subagent": subagent, "images": images}


def measure(name: str, paging: bool) -> dict:
    with tempfile.TemporaryDirectory() as home:
        store = SessionStore(Path(home), "d63")
        started = time.time()
        (cli,) = SCENARIOS[name](store, paging)
        sent = cli.sent
        batches = cli.events("in_turn_batch")
        head = cli.upstream.head(cli.request())[1] / 3.2
        resent = [b["resent_tokens"] for b in batches]
        rewritten = [b["estimate_after"] - head for b in batches]  # every message, when no cache entry is in reach
        total = sum(s.real for s in sent)
        first = next((i for i, s in enumerate(sent) if "in_turn_batch" in s.names), None)
        stripped = sum(1 for a, b in zip(sent, sent[1:]) if b.thinking < a.thinking)
        return {
            "scenario": name, "in_turn_paging": paging, "requests": len(sent),
            "over_bound": len(cli.events("over_bound")),
            "over_bound_real": sum(1 for e in cli.usage_events if e["event"] == "over_bound"),
            "largest_estimate": max(s.estimate for s in sent), "largest_real": max(s.real for s in sent),
            "batches": len(batches),
            "results_stubbed": sum(b["results_stubbed"] for b in batches),
            "rounds_folded": sum(b["rounds_folded"] for b in batches),
            "pages": len(store.page_ids()),
            "resent_mean": round(sum(resent) / len(resent)) if resent else 0,
            "resent_largest": max(resent, default=0),
            "resent_share": round(sum(resent) / total, 4),
            "rewritten_mean": round(sum(rewritten) / len(rewritten)) if rewritten else 0,
            "rewritten_share": round(sum(rewritten) / total, 4),
            "tokens_sent": total,
            "thinking_stripped_requests": stripped,
            "smallest_room": min(s.room for s in sent),
            "smallest_room_after_first_batch": min(s.room for s in sent[first:]) if first is not None else None,
            "seconds": round(time.time() - started, 1),
        }


def digest(name: str) -> str:
    """A hash over every forwarded request's messages and events, in-turn paging off."""
    with tempfile.TemporaryDirectory() as home:
        (cli,) = SCENARIOS[name](SessionStore(Path(home), "d63"), False)
        h = hashlib.sha256()
        for sent in cli.sent:
            h.update(canonical_bytes(sent.forwarded))
            h.update(canonical_bytes([{k: v for k, v in e.items() if k != "ts"} for e in sent.events]))
        return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--digest", action="store_true", help="hash what is forwarded with in-turn paging off")
    parser.add_argument("scenarios", nargs="*", default=list(SCENARIOS))
    args = parser.parse_args()
    for name in args.scenarios:
        if args.digest:
            print(json.dumps({"scenario": name, "sha256": digest(name)}), flush=True)
        else:
            for paging in (False, True):
                print(json.dumps(measure(name, paging)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
