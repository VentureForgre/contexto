"""Recall guidance: the default (H1 variant V2) and the legacy text kept for diagnosis (M2 H1 recall fix)."""

import json
import re

import pytest

from contexto.store import SessionStore
from contexto.store.memory import MemoryStore
from contexto.store.recall import (
    DEFAULT_LINES,
    ENV_VAR,
    EXAMPLE_LINE,
    LEGACY_FIRST_LINE,
    RULE_LINES,
    salient_identifiers,
)

FILLER = [f"s0 line {i:04d} level=DEBUG latency_ms={100 + i} shard={i % 50} ok" for i in range(200)]
PLANT_TURN = [{"role": "user", "content": "\n".join([
    "This is a long working session. Each message pastes the next part of a working log.", *FILLER[:70],
    "cfg checkout-api.retention_days=juniper-2573", *FILLER[70:140],
    "cfg ingest-api.retention_days=amber-1036", *FILLER[140:], "Reply with exactly: OK"])},
    {"role": "assistant", "content": [{"type": "text", "text": "OK"}]}]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)


@pytest.fixture(params=["session", "memory"])
def store(request, tmp_path):
    return SessionStore(tmp_path, "s1") if request.param == "session" else MemoryStore("s1")


def test_the_default_text_meets_the_contract():
    text = " ".join(RULE_LINES)
    assert DEFAULT_LINES[0].startswith("Earlier turns of this conversation are paged out")
    for required in ("contexto_search", "contexto_recall", "before you rely on a detail",
                     "before you answer that something is unknown", "contexto_pin", "contexto_close"):
        assert required in text


def test_legacy_reproduces_the_text_shipped_before_the_fix(store):
    assert store.pinned_text(6000, variant="legacy").splitlines() == [LEGACY_FIRST_LINE]


def test_the_default_opens_with_the_rule_then_the_worked_example(store):
    lines = store.pinned_text(6000).splitlines()
    assert tuple(lines[:4]) == (*RULE_LINES, EXAMPLE_LINE) == DEFAULT_LINES
    assert store.state_text().splitlines()[:4] == list(DEFAULT_LINES)
    assert EXAMPLE_LINE not in store.pinned_text(6000, variant="legacy")


@pytest.mark.parametrize("retired", ["v1", "v2", "rule"])
def test_experiment_only_variants_are_not_selectable(store, retired):
    with pytest.raises(ValueError):
        store.pinned_text(6000, variant=retired)


def test_salient_identifiers_list_keys_never_values_rarest_first():
    ids = salient_identifiers([PLANT_TURN[0]["content"]])
    assert ids[:2] == ["checkout-api.retention_days", "ingest-api.retention_days"]
    joined = " ".join(ids)
    for value in ("juniper-2573", "amber-1036", "2573", "1036"):
        assert value not in joined
    assert "latency_ms" in ids  # common filler keys rank after the one-off keys
    assert ids.index("latency_ms") > 1


def test_values_in_prose_and_identifiers_with_digits_are_never_listed():
    text = ("Cohort update: the archive-hub rollout cohort is now onyx-4681.\n"
            "Action item AI-93532 is done.\nrelease v2.13.0 of payments-api shipped\n"
            "the canary region for ledger-edge is west-lo2")
    ids = salient_identifiers([text])
    assert "archive-hub" in ids and "payments-api" in ids and "ledger-edge" in ids
    for value in ("onyx-4681", "AI-93532", "v2.13.0", "west-lo2"):
        assert value not in ids
    assert salient_identifiers(["upload to s3-bucket"]) == []  # the documented cost of the digit rule


def test_no_answer_of_the_acceptance_script_reaches_the_index():
    """Contamination check on the real acceptance script: index lines never carry a probe answer."""
    import sys
    from pathlib import Path

    spikes = Path(__file__).resolve().parents[1] / "spikes"
    for path in (spikes / "m2", spikes / "m0"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    from acceptance import script as acceptance

    script = acceptance.build(acceptance.DEFAULT_SEED)
    store = MemoryStore()
    store.page_out([[{"role": "user", "content": text}, {"role": "assistant", "content": "OK"}]
                    for text in script.turns[:-1]], 0)
    index = store.index_text(1_000_000)
    assert "retention_days" in index
    answers = {a.strip() for p in script.probes for a in p.answer.split(",")} - {""}
    words = set(re.findall(r"[\w.\-/]+", index))
    assert sorted(answers & words) == []


def test_the_default_index_lists_ids_and_legacy_does_not(store):
    [pid] = store.page_out([PLANT_TURN], 1)
    legacy = store.index_text(4000, variant="legacy")
    default = store.index_text(4000)
    assert "[ids:" not in legacy
    assert default.startswith(f"{pid} turn 1: ")
    assert "[ids: checkout-api.retention_days, ingest-api.retention_days" in default
    assert "juniper-2573" not in default and "amber-1036" not in default


def test_index_with_ids_stays_within_its_budget(store):
    store.page_out([PLANT_TURN for _ in range(40)], 1)
    from contexto.tokens import estimate_tokens
    text = store.index_text(1000)
    assert estimate_tokens(text) <= 1000 + 60  # the omitted-pages line may sit just past the budget
    assert text.splitlines()[-1].endswith("older pages omitted; use contexto_search to find them)")


def test_variant_comes_from_the_environment_and_is_recorded_once(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "legacy")
    store = SessionStore(tmp_path, "s1")
    assert store.recall_variant() == "legacy"
    assert json.loads((store.dir / "meta.json").read_text())["recall_variant"] == "legacy"
    monkeypatch.setenv(ENV_VAR, "default")  # a mid-session change does not move the text
    assert SessionStore(tmp_path, "s1").recall_variant() == "legacy"
    assert store.recall_variant("default") == "default"  # an explicit argument wins and is not recorded
    assert json.loads((store.dir / "meta.json").read_text())["recall_variant"] == "legacy"


def test_a_retired_recorded_variant_falls_back_to_the_default(tmp_path):
    store = SessionStore(tmp_path, "s1")
    store._update_meta({"recall_variant": "v1"})  # an H1 experiment session
    assert store.recall_variant() == "default"


def test_default_variant_when_unset_and_errors_for_unknown(tmp_path, monkeypatch):
    assert SessionStore(tmp_path, "a").recall_variant() == "default"
    monkeypatch.setenv(ENV_VAR, "v9")
    with pytest.raises(ValueError):
        SessionStore(tmp_path, "b").recall_variant()
    with pytest.raises(ValueError):
        MemoryStore().pinned_text(6000, variant="nope")


def test_a_read_only_store_does_not_record_the_variant(tmp_path, monkeypatch):
    SessionStore(tmp_path, "s1")
    monkeypatch.setenv(ENV_VAR, "legacy")
    reader = SessionStore.open_existing(tmp_path, "s1")
    assert reader.recall_variant() == "legacy"
    assert not (reader.dir / "meta.json").exists()
