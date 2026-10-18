"""Write the v0.2.8 epoch record that test_carried_bound.py resumes (reviewer-1, PR #203).

v0.2.8's ``rewrite.py`` (the released classifier, before D32 and D33) runs 39 claude-sonnet-5 shaped
turns (``turn_5x(t, opus=False)``) at the test's configuration; its fallback pages the environment
message, which v0.2.8 classified as carried text. The record it writes at the last turn start is
saved as JSON. Zero spend: no CLI and no model, only the engine and an in-memory store.

    git show v0.2.8:src/contexto/rewrite.py > /tmp/rewrite_v028.py
    PYTHONPATH=src:tests .venv/bin/python tests/fixtures/make_v028_epoch_record.py /tmp/rewrite_v028.py
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import test_carried_bound as T
from test_epoch_resume import EpochStore

OUT = Path(__file__).with_name("v028_epoch_sonnet5.json")
TURNS = 39


def main(old_rewrite: str) -> None:
    spec = importlib.util.spec_from_file_location("rewrite_v028", old_rewrite)
    old = importlib.util.module_from_spec(spec)
    sys.modules["rewrite_v028"] = old
    spec.loader.exec_module(old)
    store = EpochStore()
    engine = old.RewriteEngine(store, config=old.Config(**T.RESUME_CONFIG))
    msgs: list[dict] = []
    for t in range(1, TURNS + 1):
        turn = T.turn_5x(t, opus=False)
        msgs = msgs + turn[:2]
        engine(T.request(copy.deepcopy(msgs)), {}, T.MESSAGES)
        msgs = msgs + turn[2:]
    record = store.epoch_record()
    if old.message_hash(msgs[1]) not in record["paged_carried"]:
        raise SystemExit("v0.2.8 did not page the environment message; the fixture would test nothing")
    OUT.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} (k {record['k']}, turns_paged {record['turns_paged']}, "
          f"paged_carried {len(record['paged_carried'])})")


if __name__ == "__main__":
    main(sys.argv[1])
