"""D63: a second, independent replay of tool loops through the real engine and a real ``SessionStore``,
with in-turn paging off and on. Zero spend: the model is the fake upstream of ``tests/inturn_harness.py``
(text at 3.2 bytes a token), which refuses any thinking block whose prefix changed. The numbers D63
records come from the replay harness beside this file (``replay.py``); this script adds the engine's
own batch figures and the proof that paging off changes nothing.

    uv run python spikes/m2/inturn/d63_measure.py            # one JSON line per scenario, paging off and on
    uv run python spikes/m2/inturn/d63_measure.py --digest   # a hash of every forwarded body, paging off
    uv run python spikes/m2/inturn/d63_measure.py --replay   # the PR table, from the replay harness (#268)
    uv run --with pytest python spikes/m2/inturn/d63_measure.py --cases   # the large head and the PDFs

``--replay`` runs the replay harness's seven Claude Code scenarios (``small`` at 1,500 rounds) through the
real engine and prints one JSON line each with the numbers D63's table gives, two of which the harness
does not report: the largest cost of one rewrite, and the requests sent with any thinking of the running
turn missing (the harness counts only those whose newest response lost its thinking).

``--cases`` runs Reviewer B's two cases the replay harness cannot script, a 92,000-token head (400
rounds of 20 KB) and a 10-page PDF in every result (1,000 rounds), through ``tests/inturn_harness.py``,
and applies the harness's rewrite allowance and breakpoint cost ceiling to them by hand.

``--digest`` runs on any checkout from v0.5.24 on (it never names the new setting when the engine has
none), so the same command with ``PYTHONPATH`` at a v0.5.24 ``src`` and at this one shows whether paging
off sends v0.5.24's bytes and logs its events. Measured for D63: the four scenarios below other than
``small1500`` hash the same.

Scenarios, all at the default configuration (bound 150,000, headroom floor 25,000, minimum turn room
12,000) with a 20,000-token head:

- ``loop120``: one prompt, 120 tool rounds of about 6,400 tokens each.
- ``huge3``: 12 rounds, three of them one result of about 160,000 tokens.
- ``small1500``: 1,500 rounds of 400-byte results (several minutes).
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contexto.canonical import canonical_bytes  # noqa: E402
from contexto.rewrite import ANTHROPIC, COUNT_TOKENS, Config, RewriteEngine  # noqa: E402
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
        resent = [b["resent_tokens"] for b in batches]  # from the first changed message on
        # Every message, when the cache holds no entry in reach of the first changed message: the API looks
        # back about 20 blocks from a breakpoint and Claude Code marks the tail. A batch that only changed the
        # last message (a result stubbed unread) keeps the entry the previous request wrote.
        rewritten = [b["resent_tokens"] if b["messages"] - b["first_changed"] <= 2 else b["estimate_after"] - head
                     for b in batches]
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
            "triggers": sorted({b["trigger"] for b in batches}),
            "turns_paged": sum(b["turns_paged"] for b in batches),
            "unseen_stubbed": sum(b["unseen_results_stubbed"] for b in batches),
            "rewritten_mean": round(sum(rewritten) / len(rewritten)) if rewritten else 0,
            "rewritten_largest": round(max(rewritten, default=0)),
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


THINKING = ("thinking", "redacted_thinking")


def _signatures(messages: list) -> set[str]:
    """The signature (or redacted data) of every thinking block in ``messages``."""
    out = set()
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") in THINKING:
                out.add(str(block.get("signature") if block["type"] == "thinking" else block.get("data")))
    return out


class _Recording:
    """The real engine, keeping for each forwarded request whether any thinking block of the CLI's
    running turn was left out of it (``harness.run``'s ``engine`` hook; count_tokens is not counted)."""

    def __init__(self, store, config: Config) -> None:
        from inturn import upstream

        self.engine = RewriteEngine(store, config=config)
        self.missing: list[bool] = []
        self.heads: list[int] = []  # the forwarded head (system and tools) in the upstream's tokens
        self._upstream = upstream.FakeUpstream()

    def __getattr__(self, name):
        return getattr(self.engine, name)

    def __call__(self, request, headers, endpoint):
        forwarded, out_headers, events = self.engine(request, headers, endpoint)
        if endpoint is not COUNT_TOKENS:
            messages = request.get("messages") or []
            starts = [i for i, m in enumerate(messages) if ANTHROPIC.is_turn_start(m)]
            turn = messages[starts[-1]:] if starts else messages
            self.missing.append(bool(_signatures(turn) - _signatures(forwarded.get("messages") or [])))
            self.heads.append(self._upstream.head_tokens("/v1/messages", forwarded))
        return forwarded, out_headers, events


def replay(name: str) -> dict:
    """One Claude Code scenario of the replay harness, full size, with the numbers of D63's table."""
    from inturn import harness, scripts, targets

    holder: list[_Recording] = []

    def factory(store, config):
        holder.append(_Recording(store, config))
        return holder[0]

    started = time.time()
    run = harness.run(scripts.build(name, full=True), "claude", engine=factory)
    m = run.metrics()
    config = run.config
    loop_rewrites = [s for s in run.sent if s.rewrite and s.in_loop]
    rewrites = [s for s in run.sent if s.rewrite]
    allowed = {f"{c} turn {t + 1}": round(2 + sent[-1].loop_tokens / (config.bound / targets.REWRITE_BATCH), 1)
               for (c, t), sent in run.loops().items() if any(s.rewrite and s.in_loop for s in sent)}
    loop = [s for s in run.sent if s.in_loop]
    at_request = [s.room_at_request for s in loop if s.room_at_request is not None]
    at_response = [s.room for s in loop if s.room is not None]
    missing, heads = holder[0].missing, holder[0].heads
    assert len(missing) == len(run.sent) == len(heads)
    # The breakpoint cache model (the harness's next round): a real Messages cache stores a prefix only at
    # a cache_control mark, so after an in-loop rewrite only the head is read back and the rest is sent
    # again. Per loop, that must be at most max(the loop's own tokens, bound).
    breakpoint: dict[str, dict[str, int]] = {}
    index = {id(s): i for i, s in enumerate(run.sent)}
    for (c, t), sent in run.loops().items():
        cost = sum(s.tokens - heads[index[id(s)]] for s in sent if s.rewrite and s.in_loop)
        if cost:
            breakpoint[f"{c} turn {t + 1}"] = {"resent": cost, "ceiling": max(sent[-1].loop_tokens, config.bound)}
    # Truthful rooms (the same round): the request-time room at most bound - headroom_floor - 0.9 x real.
    untruthful = sum(1 for s in run.sent if s.room_at_request is not None
                     and s.room_at_request > config.bound - config.headroom_floor - 0.9 * s.tokens)
    return {
        "scenario": name, "requests": len(run.sent),
        "over_bound": m["over_bound"], "largest": m["largest"]["tokens"], "largest_estimate": m["largest"]["estimate"],
        "rewrites": len(rewrites), "rewrites_in_loop": len(loop_rewrites), "allowed_per_loop": allowed,
        "invalidated_mean": round(sum(s.invalidated for s in rewrites) / len(rewrites)) if rewrites else 0,
        "invalidated_largest": max((s.invalidated for s in rewrites), default=0),
        "resent_mean": m["rewrites"]["mean_resent"], "resent_largest": max((s.resent for s in rewrites), default=0),
        "resent_share": m["rewrites"]["resent_share"], "input_tokens": m["input_tokens"],
        "newest_thinking_missing": m["thinking"]["newest_missing"],
        "any_turn_thinking_missing": sum(missing),
        "room_min_at_request": min(at_request, default=None), "room_min_at_response": min(at_response, default=None),
        "breakpoint_resent": breakpoint, "rooms_over_truthful": untruthful,
        "probes": m["probes"], "violations": m["violations"], "unmet": targets.unmet(run),
        "seconds": round(time.time() - started, 1),
    }


REPLAY = ("loop120", "large", "small", "subagent", "images", "multiturn", "mixed")


def bighead(store) -> Cli:
    """Reviewer B's case: a 92,000-token head at the default bound, 400 rounds of 20 KB results."""
    cli = Cli(store, Config(), head_bytes=int(92_000 * 3.2))
    cli.prompt("Read every module and report what each one does.")
    cli.loop(400, lambda r: output(f"module-{r:04d}", RESULT))
    return cli


def pdf1000(store) -> Cli:
    """A 10-page PDF in every result, 1,000 rounds, at the default configuration."""
    import base64

    from test_pdf_pages import simple_pdf

    pdf = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                          "data": base64.b64encode(simple_pdf(10)).decode()}}
    cli = Cli(store, Config())
    cli.prompt("Read the reports.")
    cli.loop(1_000, lambda r: [pdf])
    return cli


CASES = {"bighead": bighead, "pdf1000": pdf1000}


def case(name: str) -> dict:
    """One of ``CASES`` with the replay harness's cache rules applied by hand: rewrites against the
    head-scaled allowance, and the breakpoint model's re-sent tokens against max(loop tokens, bound)."""
    from inturn import targets

    with tempfile.TemporaryDirectory() as home:
        store = SessionStore(Path(home), "d63")
        started = time.time()
        cli = CASES[name](store)
        seconds = time.time() - started
        config = cli.config
        head = cli.upstream.tokens({**cli.request(), "messages": []})
        prompt = cli.upstream.tokens({**cli.request(), "messages": cli.history[:1]}) - head
        loop_tokens = cli.upstream.tokens({**cli.request(), "messages": cli.history}) - head - prompt
        rewrites, resent = 0, 0
        for a, b in zip(cli.sent, cli.sent[1:]):
            if b.digests[:len(a.digests)] != a.digests:
                rewrites += 1
                resent += b.real - head
        room = config.bound - config.headroom_floor - config.min_turn_room - head
        d = min(config.bound / targets.REWRITE_BATCH, max(config.bound / targets.REWRITE_FLOOR, targets.HEAD_SHARE * room))
        ceiling = max(loop_tokens, config.bound)
        return {
            "case": name, "requests": len(cli.sent), "head": head, "loop_tokens": loop_tokens,
            "over_bound": len(cli.events("over_bound")) + len(cli.events("in_turn_exhausted")),
            "largest_real": max(s.real for s in cli.sent), "largest_estimate": max(s.estimate for s in cli.sent),
            "rewrites": rewrites, "allowed": round(2 + loop_tokens / d, 1),
            "breakpoint_resent": resent, "ceiling": ceiling, "share_of_ceiling": round(resent / ceiling, 3),
            "folds": len(cli.state.inturn.folds), "stubs": len(cli.state.inturn.stubs),
            "smallest_room": min(s.room for s in cli.sent),
            "engine_ms_per_request": round(1000 * seconds / len(cli.sent), 1),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--digest", action="store_true", help="hash what is forwarded with in-turn paging off")
    parser.add_argument("--replay", action="store_true", help="the replay harness's Claude Code scenarios, full size")
    parser.add_argument("--cases", action="store_true", help="the large-head and PDF cases, through the test CLI")
    parser.add_argument("scenarios", nargs="*")
    args = parser.parse_args()
    if args.cases:
        for name in args.scenarios or CASES:
            print(json.dumps(case(name)), flush=True)
        return 0
    if args.replay:
        for name in args.scenarios or REPLAY:
            print(json.dumps(replay(name)), flush=True)
        return 0
    for name in args.scenarios or list(SCENARIOS):
        if args.digest:
            print(json.dumps({"scenario": name, "sha256": digest(name)}), flush=True)
        else:
            for paging in (False, True):
                print(json.dumps(measure(name, paging)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
