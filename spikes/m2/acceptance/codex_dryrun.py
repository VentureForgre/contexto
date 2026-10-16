"""Zero-spend dry run of the Codex acceptance protocol (M2 A16): the full D27 script through
``contexto run codex`` against codex_stub.py on 127.0.0.1. Proves requests reach the stub through the
proxy, rotations happen, recall is callable (the stub's probe turns call contexto_search and
contexto_recall through Codex's exec tool), and the harness scores and writes its record.

    uv run python spikes/m2/acceptance/codex_dryrun.py <contexto executable> <out dir> [--bound N]
        [--turn-kbytes K] [--seed S] [--home-workaround]

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

from acceptance import codex_budget, codex_driver, script  # noqa: E402
from acceptance.codex_stub import CodexStub  # noqa: E402
from fidelity import l2f  # noqa: E402

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
    with CodexStub(answers, query="retention_days") as stub:
        turns = codex_driver.CodexTurns(a.contexto, session, a.bound, stub.url, str(helper), scratch, env,
                                        codex_upstream=stub.url + "/v1")
        log = store / "sessions" / session / "log.jsonl"
        record = codex_driver.run_session(s, turns, log, b.h1_worst, turn_estimates(s, b),
                                          inflight=a.bound + codex_budget.OUTPUT_ALLOWANCE)
        stub_requests = list(stub.requests)
    events = codex_driver.read_log(log)
    scored = l2f.score(s.probes, record.pop("answers"))
    summary = {
        "ran": record, "turns": {"content": s.n_content, "probe": len(s.probe_turn_ids)},
        "stub": {"posts": sum(1 for r in stub_requests if r["method"] == "POST"),
                 "kinds": {k: sum(1 for r in stub_requests if r.get("kind") == k)
                           for k in ("ok", "recall_call", "answers")},
                 "gets": sum(1 for r in stub_requests if r["method"] == "GET")},
        "analysis": codex_driver.analyze(events, a.bound),
        "score": {"correct": scored["correct"], "total": scored["total"]},
        "budget": {"h1_worst": b.h1_worst, "baseline_worst": b.baseline_worst, "total_worst": b.total_worst,
                   "baseline_fits": b.baseline_fits, "formula": codex_budget.formula(b)},
        "home_workaround": a.home_workaround,
    }
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "codex-dry-run.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0 if record["turns_completed"] == record["turns"] else 1


if __name__ == "__main__":
    sys.exit(main())
