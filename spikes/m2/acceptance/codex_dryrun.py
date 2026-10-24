"""Zero-spend dry run of the Codex acceptance protocol (M2 A16): the full D27 script through
``contexto run codex`` against codex_stub.py on 127.0.0.1. Proves requests reach the stub through the
proxy, rotations happen, recall is callable (the stub's probe turns call contexto_search and
contexto_recall through Codex's exec tool), and the harness scores and writes its record.

    uv run python spikes/m2/acceptance/codex_dryrun.py <contexto executable> <out dir> [--bound N]
        [--turn-kbytes K] [--seed S] [--home-workaround] [--approved N] [--fail-post N]

``--approved`` sets the H1 approval line (default: the H1 worst case) so the spend watcher can be made
to trip; ``--fail-post`` makes the stub answer that POST (1-based) with a 500, to show the pinned
retries (A21).

The run sets CONTEXTO_HOME (PR #148, v0.1.2); ``--home-workaround`` uses the scratch-HOME workaround
for a build before it (codex_driver.child_env). Writes ``<out>/codex-dry-run.json`` and prints it. No credential is
read: the key helper prints a dummy, and the stub checks none."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from acceptance import script  # noqa: E402  (first: it puts spikes/m0 on the path for fidelity)

# isort: split
from fidelity import l2f  # noqa: E402

from acceptance import codex_budget, codex_driver, codex_recall_credit  # noqa: E402
from acceptance.codex_stub import CodexStub  # noqa: E402

DEFAULT_BOUND = 64_000
DEFAULT_TURN_KBYTES = 20


def build(seed: int, bound: int, turn_bytes: int) -> script.Script:
    """The fewest content turns whose history reaches 2.5x the bound (as run.build_script does)."""
    n = 4
    while True:
        s = script.build(seed, n, turn_bytes)
        if sum(codex_budget.turn_tokens(x) + codex_budget.REPLY_TOKENS for x in s.content_bytes) >= 2.5 * bound:
            return s
        n += 1


def acceptance_3(s: script.Script, answers: str, events: list[dict], session_dir: Path) -> dict:
    """Acceptance 3 through the harness path (A21 item 3): codex_recall_credit.credit (A20) over the
    session log and the stored pages, with D27's probe turns and the expected turn count, plus the
    in-view spans search returned (content-free: turn, message index, offsets)."""
    pages = [json.loads(p.read_text()) for p in sorted((session_dir / "pages").glob("pg_*.json"))]
    result = codex_recall_credit.credit(list(s.probes), answers, events, pages, probe_turn_ids=s.probe_turn_ids,
                                        expected_turns=len(s.turns))
    in_view = [r for e in events if e.get("event") == "mcp_call" for r in e.get("returned") or []
               if r.get("source") == "in_view"]
    return {"credit": result, "in_view_spans": len(in_view), "in_view": in_view}


def turn_estimates(s: script.Script, b: codex_budget.CodexBudget) -> list[int]:
    """Each turn's worst case under the H1 terms of codex_budget (requests x (bound + output))."""
    per = b.bound + codex_budget.OUTPUT_ALLOWANCE
    content = (1 + b.retries_per_turn) * per
    probe = (1 + b.tool_rounds + b.retries_per_turn) * per
    return [content] * s.n_content + [probe] * len(s.probe_turn_ids)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("contexto")
    ap.add_argument("out", type=Path)
    ap.add_argument("--bound", type=int, default=DEFAULT_BOUND)
    ap.add_argument("--turn-kbytes", type=int, default=DEFAULT_TURN_KBYTES)
    ap.add_argument("--seed", type=int, default=script.DEFAULT_SEED)
    ap.add_argument("--home-workaround", action="store_true")
    ap.add_argument("--approved", type=int, default=None)
    ap.add_argument("--fail-post", type=int, action="append", default=[])
    a = ap.parse_args(argv)
    s = build(a.seed, a.bound, a.turn_kbytes * 1000)
    b = codex_budget.estimate(s, a.bound)
    root = Path(tempfile.mkdtemp(prefix="contexto-codex-dry-"))  # outside any Git work tree
    scratch, home = root / "scratch", root / "home"
    scratch.mkdir()
    home.mkdir()
    helper = root / "key-helper.sh"
    helper.write_text("#!/bin/sh\necho dummy-not-a-credential\n")
    helper.chmod(0o700)
    env, store = codex_driver.child_env(root / "store", home, mcp_home_workaround=a.home_workaround)
    session = "codex-dry"
    answers = {p.id: p.answer for p in s.probes}
    # Every paged-only plant is a "cfg <svc>.retention_days=<value>" line in turn 1 (script.build).
    approved = a.approved if a.approved is not None else b.h1_worst
    with CodexStub(answers, query="retention_days", fail_posts=frozenset(a.fail_post)) as stub:
        turns = codex_driver.CodexTurns(a.contexto, session, a.bound, stub.url, str(helper), scratch, env,
                                        codex_upstream=stub.url + "/v1")
        log = store / "sessions" / session / "log.jsonl"
        record = codex_driver.run_session(s, turns, log, approved, turn_estimates(s, b),
                                          inflight=a.bound + codex_budget.OUTPUT_ALLOWANCE)
        stub_requests = list(stub.requests)
    events = codex_driver.read_log(log)
    answers_text = record.pop("answers")
    scored = l2f.score(s.probes, answers_text)
    # The proxy's stream hold per reply kind: requests are sequential, so the stub's POSTs and the proxy's
    # /responses entries pair up in order.
    kinds = [r["kind"] for r in stub_requests if r["method"] == "POST"]
    held: dict[str, list[int]] = {}
    for kind, entry in zip(kinds, codex_driver.responses_requests(events)):
        held.setdefault(kind, []).append((entry.get("stream_hold") or {}).get("held_events", 0))
    summary = {
        "ran": record, "turns": {"content": s.n_content, "probe": len(s.probe_turn_ids)},
        "stub": {"posts": sum(1 for r in stub_requests if r["method"] == "POST"),
                 "failed_posts": sum(1 for r in stub_requests if r.get("kind") == "failed"),
                 "kinds": {k: sum(1 for r in stub_requests if r.get("kind") == k)
                           for k in ("ok", "recall_call", "answers", "failed")},
                 "gets": sum(1 for r in stub_requests if r["method"] == "GET")},
        "analysis": codex_driver.analyze(events, a.bound),
        "held_events": {k: {"requests": len(v), "min": min(v), "max": max(v)} for k, v in held.items()},
        "score": {"correct": scored["correct"], "total": scored["total"]},
        "acceptance_3": acceptance_3(s, answers_text, events, log.parent),
        "budget": {"h1_worst": b.h1_worst, "baseline_worst": b.baseline_worst, "total_worst": b.total_worst,
                   "baseline_fits": b.baseline_fits, "formula": codex_budget.formula(b)},
        "home_workaround": a.home_workaround,
        # The missing-tools check (contexto.toolcheck): a turn whose Codex never got the contexto tools.
        "mcp_tools_missing": sum(1 for e in events for x in [e, *(e.get("transform_events") or [])]
                                 if isinstance(x, dict) and x.get("event") == "mcp_tools_missing"),
    }
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "codex-dry-run.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0 if record["turns_completed"] == record["turns"] else 1


if __name__ == "__main__":
    sys.exit(main())
