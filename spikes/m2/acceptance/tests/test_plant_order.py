"""A chain's links appear in statement order within a turn (intake, 2026-10-16 08:10Z).

Before the fix, script.build inserted each plant at an independent random line of its turn, so two
links of one chain planted in the same turn could appear newest first: 13 of the 30 chains in live
run #2's 40 KB script. Fixtures are the deterministic scripts; no session content is committed.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from acceptance import run, script  # noqa: E402


def chain_inversions(s: script.Script) -> list[str]:
    """Chain probes whose links do not appear in statement order across the rendered content turns."""
    where: dict[str, tuple[int, int]] = {}
    for t, text in enumerate(s.content_turns):
        for i, line in enumerate(text.split("\n")):
            where.setdefault(line, (t, i))
    bad = []
    for p in s.probes:
        if p.type != "chain":
            continue
        at = [where[st.split("\n")[0]] for st in p.statements]
        if any(a >= b for a, b in zip(at, at[1:])):
            bad.append(p.id)
    return bad


@pytest.mark.parametrize("kb", [40, 70])
def test_every_chain_appears_in_statement_order(kb):
    s, _ = run.plan(turn_bytes=kb * 1_000)
    chains = [p for p in s.probes if p.type == "chain"]
    assert len(chains) == 30 and all(len(p.statements) in (6, 8) for p in chains)
    assert chain_inversions(s) == []


def test_live_run_2_script_has_no_inversions():
    # The live run #2 plan (seed 20261016, 40 KB): the old insertion inverted 13 chains here,
    # q_a_023 and q_a_031 among them (both arms missed those two).
    s, _ = run.plan()
    assert s.seed == script.DEFAULT_SEED == 20261016
    assert chain_inversions(s) == []


def test_the_d27_assignment_is_unchanged_by_the_fix():
    # Probe ids and their order are seed-derived, so the 6x9 assignment fixed at 05:39Z still holds.
    s, _ = run.plan()
    ids = [list(t) for t in s.probe_turn_ids]
    assert hashlib.sha256(json.dumps(ids).encode()).hexdigest()[:16] == "810348a60f71f216"
    assert script.restatements(s) == []
