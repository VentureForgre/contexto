import copy
import hashlib
import json

import pytest

from contexto.model import (
    GENESIS_PREV,
    KINDS,
    Record,
    Source,
    ValidationError,
    canonical_json,
    compute_id,
    utc_timestamp,
)

# Golden vector: computed once and pinned. If this changes, every stored id changes.
GOLDEN_DRAFT = {
    "kind": "decision",
    "epistemic": "verified",
    "created_at": "2026-10-15T22:00:00.000000Z",
    "author": "build.builder-1",
    "session": "s-1",
    "topic": "storage-format",
    "body": "Ledger is append-only JSONL. Café ✓",
    "provenance": [
        {
            "type": "file",
            "locator": "docs/milestones/M1-ledger-and-recovery.md",
            "sha256": "a" * 64,
            "span": "L20-L40",
        },
        {"type": "command", "locator": "uv run pytest"},
    ],
    "supersedes": ["rec_000000000000000000000001"],
}
GOLDEN_ID = "rec_7a9092fad710c356fd2788ac"

MINIMAL_DRAFT = {
    "kind": "event",
    "epistemic": "reported",
    "created_at": "2026-10-15T22:00:00.000000Z",
    "author": "a",
    "body": "b",
    "provenance": [],
}


def full_dict(draft, prev=GENESIS_PREV):
    d = copy.deepcopy(draft)
    d["id"] = compute_id(d)
    d["prev"] = prev
    return d


def test_golden_id_vector():
    assert compute_id(GOLDEN_DRAFT) == GOLDEN_ID


def test_golden_id_matches_contract_formula():
    # Independent restatement of the contract: canonical JSON of to_dict() minus id and prev,
    # sources carrying all four keys.
    payload = copy.deepcopy(GOLDEN_DRAFT)
    payload["provenance"][1].update({"sha256": None, "span": None})
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert GOLDEN_ID == "rec_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def test_id_ignores_absent_optional_spelling():
    spelled = dict(MINIMAL_DRAFT, session=None, topic=None, target=None, owner=None, supersedes=None)
    empty_supersedes = dict(MINIMAL_DRAFT, supersedes=[])
    assert compute_id(spelled) == compute_id(MINIMAL_DRAFT) == compute_id(empty_supersedes)

    explicit_nulls = copy.deepcopy(GOLDEN_DRAFT)
    explicit_nulls["provenance"][1].update({"sha256": None, "span": None})
    explicit_nulls["owner"] = None
    explicit_nulls["target"] = None
    assert compute_id(explicit_nulls) == GOLDEN_ID


def test_id_ignores_id_and_prev():
    d = dict(GOLDEN_DRAFT, id="rec_" + "f" * 24, prev="1" * 64)
    assert compute_id(d) == GOLDEN_ID


@pytest.mark.parametrize(
    "field, value",
    [
        ("kind", "event"),
        ("epistemic", "hypothesis"),
        ("created_at", "2026-10-15T22:00:00.000001Z"),
        ("author", "build.builder-2"),
        ("session", "s-2"),
        ("topic", "other"),
        ("body", "Ledger is append-only JSONL. Cafe ✓"),
        ("supersedes", ["rec_000000000000000000000002"]),
        ("provenance", [{"type": "url", "locator": "https://example.com/spec"}]),
    ],
)
def test_changing_hashed_field_changes_id(field, value):
    assert compute_id(dict(GOLDEN_DRAFT, **{field: value})) != GOLDEN_ID


def test_changing_target_or_owner_changes_id():
    closure = dict(MINIMAL_DRAFT, kind="closure", target="rec_" + "c" * 24)
    assert compute_id(closure) != compute_id(dict(closure, target="rec_" + "d" * 24))
    obligation = dict(MINIMAL_DRAFT, kind="obligation", owner="build.builder-1")
    assert compute_id(obligation) != compute_id(dict(obligation, owner="build.builder-2"))


@pytest.mark.parametrize("key, value", [("type", "url"), ("locator", "x"), ("sha256", "b" * 64), ("span", "L1")])
def test_changing_source_field_changes_id(key, value):
    d = copy.deepcopy(GOLDEN_DRAFT)
    d["provenance"][0][key] = value
    assert compute_id(d) != GOLDEN_ID


def test_changing_prev_does_not_change_id():
    a = Record.from_dict(full_dict(GOLDEN_DRAFT, prev=GENESIS_PREV))
    b = Record.from_dict(full_dict(GOLDEN_DRAFT, prev="ab" * 32))
    assert a.prev != b.prev
    assert compute_id(a.to_dict()) == compute_id(b.to_dict()) == a.id == GOLDEN_ID


ROUND_TRIP_DRAFTS = [
    pytest.param(MINIMAL_DRAFT, id="minimal"),
    pytest.param(GOLDEN_DRAFT, id="every-optional-non-ascii"),
    pytest.param(
        dict(MINIMAL_DRAFT, kind="obligation", owner="build.builder-1", body="出力を確認する"),
        id="obligation",
    ),
    pytest.param(
        dict(MINIMAL_DRAFT, kind="closure", target="rec_" + "c" * 24, session="s", topic="t"),
        id="closure",
    ),
]


@pytest.mark.parametrize("draft", ROUND_TRIP_DRAFTS)
def test_round_trip(draft):
    r = Record.from_dict(full_dict(draft))
    assert Record.from_dict(r.to_dict()) == r
    line = canonical_json(r.to_dict())
    assert Record.from_dict(json.loads(line)) == r


def test_to_dict_serialization_rules():
    minimal = Record.from_dict(full_dict(MINIMAL_DRAFT)).to_dict()
    assert set(minimal) == {"id", "kind", "epistemic", "created_at", "author", "body", "provenance", "prev"}
    assert minimal["provenance"] == []
    golden = Record.from_dict(full_dict(GOLDEN_DRAFT)).to_dict()
    assert golden["provenance"][1] == {"type": "command", "locator": "uv run pytest", "sha256": None, "span": None}
    assert golden["supersedes"] == ["rec_000000000000000000000001"]


def test_canonical_json_form():
    assert canonical_json({"b": 1, "a": "é"}) == '{"a":"é","b":1}'


def test_utc_timestamp_format():
    stamp = utc_timestamp()
    assert len(stamp) == len("2026-10-15T22:00:00.000000Z") and stamp.endswith("Z")
    full = full_dict(dict(MINIMAL_DRAFT, created_at=stamp))
    assert Record.from_dict(full).created_at == stamp


def test_created_at_accepts_other_iso_utc_forms_and_hashes_as_written():
    a = dict(MINIMAL_DRAFT, created_at="2026-10-15T22:00:00Z")
    b = dict(MINIMAL_DRAFT, created_at="2026-10-15T22:00:00.000000Z")
    assert compute_id(a) != compute_id(b)


def test_sources_are_frozen_and_records_hashable():
    r = Record.from_dict(full_dict(GOLDEN_DRAFT))
    with pytest.raises(AttributeError):
        r.body = "x"
    assert hash(r) == hash(Record.from_dict(r.to_dict()))
    assert r.provenance[1] == Source(type="command", locator="uv run pytest")


def _without(field):
    d = full_dict(MINIMAL_DRAFT)
    del d[field]
    return d


INVALID = [
    pytest.param(dict(full_dict(MINIMAL_DRAFT), kind="note"), id="unknown-kind"),
    pytest.param(dict(full_dict(MINIMAL_DRAFT), epistemic="certain"), id="unknown-epistemic"),
    pytest.param(
        dict(full_dict(MINIMAL_DRAFT), provenance=[{"type": "email", "locator": "x"}]),
        id="unknown-source-type",
    ),
    *[pytest.param(_without(f), id=f"missing-{f}") for f in
      ("id", "kind", "epistemic", "created_at", "author", "body", "provenance", "prev")],
    pytest.param(dict(full_dict(MINIMAL_DRAFT), body=None), id="null-required-field"),
    pytest.param(
        dict(full_dict(MINIMAL_DRAFT), provenance=[{"type": "file"}]), id="source-missing-locator"
    ),
    pytest.param(
        dict(full_dict(MINIMAL_DRAFT), provenance=[{"type": "file", "locator": "a.txt"}]),
        id="file-source-without-sha256",
    ),
    pytest.param(
        dict(full_dict(MINIMAL_DRAFT), provenance=[{"type": "file", "locator": "a.txt", "sha256": "xyz"}]),
        id="file-source-bad-sha256",
    ),
    pytest.param(
        dict(full_dict(MINIMAL_DRAFT), provenance=[{"type": "file", "locator": "/etc/hosts", "sha256": "a" * 64}]),
        id="file-source-absolute-locator",
    ),
    pytest.param(dict(full_dict(MINIMAL_DRAFT), epistemic="verified"), id="verified-without-provenance"),
    pytest.param(dict(full_dict(MINIMAL_DRAFT), kind="closure"), id="closure-without-target"),
    pytest.param(dict(full_dict(MINIMAL_DRAFT), kind="obligation"), id="obligation-without-owner"),
    pytest.param(
        dict(full_dict(MINIMAL_DRAFT), created_at="2026-10-15T22:00:00+00:00"), id="created-at-offset"
    ),
    pytest.param(
        dict(full_dict(MINIMAL_DRAFT), created_at="2026-10-15T22:00:00"), id="created-at-naive"
    ),
    pytest.param(dict(full_dict(MINIMAL_DRAFT), created_at="yesterdayZ"), id="created-at-garbage"),
    pytest.param(dict(full_dict(MINIMAL_DRAFT), prev="0" * 63), id="prev-short"),
    pytest.param(dict(full_dict(MINIMAL_DRAFT), prev="g" * 64), id="prev-not-hex"),
    pytest.param(dict(full_dict(MINIMAL_DRAFT), prev="A" * 64), id="prev-uppercase"),
    pytest.param(dict(full_dict(MINIMAL_DRAFT), extra=1), id="unknown-field"),
    pytest.param(dict(full_dict(MINIMAL_DRAFT), id="rec_123"), id="bad-id-format"),
]


@pytest.mark.parametrize("bad", INVALID)
def test_validation_errors(bad):
    with pytest.raises(ValidationError):
        Record.from_dict(bad)


# Each draft carries the fields its kind requires, so the only defect is the forbidden field.
REQUIRED_BY_KIND = {"closure": {"target": "rec_" + "c" * 24}, "obligation": {"owner": "o"}}


@pytest.mark.parametrize(
    "field, value, kind",
    [("target", "rec_" + "d" * 24, kind) for kind in KINDS if kind != "closure"]
    + [("owner", "someone", kind) for kind in KINDS if kind != "obligation"],
)
def test_target_and_owner_rejected_on_other_kinds(field, value, kind):
    draft = dict(MINIMAL_DRAFT, kind=kind, **REQUIRED_BY_KIND.get(kind, {}))
    draft[field] = value
    with pytest.raises(ValidationError, match=f"{field} is only allowed on"):
        Record.from_dict(full_dict(draft))


def test_valid_file_and_verified_records_accepted():
    source = {"type": "file", "locator": "docs/notes.md", "sha256": "0" * 64}
    for epistemic in ("verified", "reported"):
        r = Record.from_dict(full_dict(dict(MINIMAL_DRAFT, epistemic=epistemic, provenance=[source])))
        assert r.provenance[0].locator == "docs/notes.md"
    assert Source(type="url", locator="https://example.com").sha256 is None


def test_id_hashes_utf8_bytes_of_non_ascii_canonical_json():
    body = GOLDEN_DRAFT["body"]
    assert not body.isascii()
    payload = Record.from_dict(full_dict(GOLDEN_DRAFT)).to_dict()
    del payload["id"], payload["prev"]
    text = canonical_json(payload)
    assert body in text  # ensure_ascii=False keeps the raw characters
    assert GOLDEN_ID == "rec_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def test_compute_id_validates_drafts():
    with pytest.raises(ValidationError):
        compute_id(dict(MINIMAL_DRAFT, kind="closure"))
    with pytest.raises(ValidationError):
        compute_id(dict(MINIMAL_DRAFT, kind="nope"))


def test_validation_error_is_value_error():
    assert issubclass(ValidationError, ValueError)
