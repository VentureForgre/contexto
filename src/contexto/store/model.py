"""Record and source model for the session ledger.

Harvested from PR #2 (m1/a1-model at a537364). Normative rules: docs/milestones/M1-ledger-and-recovery.md,
as amended by docs/milestones/M2-contexto-proxy.md (Store): a ``page`` source cites a page id in
its locator, and file sources require ``sha256``. Records are
immutable; ``Record.to_dict`` gives the canonical dict form, and the record id
is derived from that form minus ``id`` and ``prev`` so it does not depend on how
a draft spelled absent optional fields.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from contexto.canonical import canonical_json

KINDS = ("event", "decision", "obligation", "artifact", "reference", "closure")
EPISTEMICS = ("verified", "reported", "summary", "hypothesis")
SOURCE_TYPES = ("file", "url", "record", "command", "transcript", "page")

GENESIS_PREV = "0" * 64
ID_PREFIX = "rec_"

_HEX64 = re.compile(r"[0-9a-f]{64}")
_PAGE_ID = re.compile(r"pg_[0-9a-f]{16}")
_RECORD_ID = re.compile(r"rec_[0-9a-f]{24}")

_SOURCE_FIELDS = ("type", "locator", "sha256", "span")
_REQUIRED_HASHED = ("kind", "epistemic", "created_at", "author", "body", "provenance")
_OPTIONAL_STR = ("session", "topic", "target", "owner")
_HASHED_FIELDS = frozenset(_REQUIRED_HASHED + _OPTIONAL_STR + ("supersedes",))
_RECORD_FIELDS = _HASHED_FIELDS | {"id", "prev"}


class ValidationError(ValueError):
    """A record or source does not satisfy the field-level rules."""


def utc_timestamp(now: datetime | None = None) -> str:
    """Format a time as ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` in UTC."""
    now = datetime.now(UTC) if now is None else now.astimezone(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class Source:
    type: str
    locator: str
    sha256: str | None = None
    span: str | None = None

    def __post_init__(self) -> None:
        if self.type not in SOURCE_TYPES:
            raise ValidationError(f"unknown source type: {self.type!r}")
        if not isinstance(self.locator, str) or not self.locator:
            raise ValidationError("source locator must be a non-empty string")
        for name in ("sha256", "span"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValidationError(f"source {name} must be a string or null")
        if self.type == "file":
            # File sources are relative to the ledger root and pin content for staleness checks.
            if self.sha256 is None or not _HEX64.fullmatch(self.sha256):
                raise ValidationError("file source requires sha256 as 64 lowercase hex chars")
            if PurePosixPath(self.locator).is_absolute():
                raise ValidationError(f"file source locator must be a relative path: {self.locator!r}")
        if self.type == "page" and not _PAGE_ID.fullmatch(self.locator):
            raise ValidationError(f"page source locator must be a page id: {self.locator!r}")

    @classmethod
    def from_dict(cls, d: dict) -> Source:
        if not isinstance(d, dict):
            raise ValidationError("source must be an object")
        unknown = set(d) - set(_SOURCE_FIELDS)
        if unknown:
            raise ValidationError(f"unknown source fields: {sorted(unknown)}")
        for name in ("type", "locator"):
            if d.get(name) is None:
                raise ValidationError(f"source missing required field: {name}")
        return cls(type=d["type"], locator=d["locator"], sha256=d.get("sha256"), span=d.get("span"))

    def to_dict(self) -> dict:
        return {"type": self.type, "locator": self.locator, "sha256": self.sha256, "span": self.span}


@dataclass(frozen=True)
class Record:
    id: str
    kind: str
    epistemic: str
    created_at: str
    author: str
    body: str
    prev: str
    provenance: tuple[Source, ...] = ()
    session: str | None = None
    topic: str | None = None
    supersedes: tuple[str, ...] = ()
    target: str | None = None
    owner: str | None = None

    def __post_init__(self) -> None:
        # Normalize sequences to tuples so equal records compare equal and stay immutable.
        object.__setattr__(self, "provenance", tuple(self.provenance))
        object.__setattr__(self, "supersedes", tuple(self.supersedes or ()))
        _validate(self)

    @classmethod
    def from_dict(cls, d: dict) -> Record:
        if not isinstance(d, dict):
            raise ValidationError("record must be an object")
        unknown = set(d) - _RECORD_FIELDS
        if unknown:
            raise ValidationError(f"unknown record fields: {sorted(unknown)}")
        for name in ("id", "prev", *_REQUIRED_HASHED):
            if d.get(name) is None:
                raise ValidationError(f"missing required field: {name}")
        provenance = d["provenance"]
        if not isinstance(provenance, list):
            raise ValidationError("provenance must be a list")
        supersedes = d.get("supersedes")
        if supersedes is not None and not isinstance(supersedes, list):
            raise ValidationError("supersedes must be a list")
        return cls(
            id=d["id"],
            kind=d["kind"],
            epistemic=d["epistemic"],
            created_at=d["created_at"],
            author=d["author"],
            body=d["body"],
            prev=d["prev"],
            provenance=tuple(
                s if isinstance(s, Source) else Source.from_dict(s) for s in provenance
            ),
            session=d.get("session"),
            topic=d.get("topic"),
            supersedes=tuple(supersedes or ()),
            target=d.get("target"),
            owner=d.get("owner"),
        )

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "epistemic": self.epistemic,
            "created_at": self.created_at,
            "author": self.author,
            "body": self.body,
            "provenance": [s.to_dict() for s in self.provenance],
            "prev": self.prev,
        }
        for name in _OPTIONAL_STR:
            value = getattr(self, name)
            if value is not None:
                d[name] = value
        if self.supersedes:
            d["supersedes"] = list(self.supersedes)
        return d


def compute_id(record_dict: dict) -> str:
    """Return the record id for a record or draft dict.

    The dict is validated and normalized through ``Record`` first, so absent
    optional fields hash the same whether omitted or spelled as None. ``id``
    and ``prev`` are ignored and may be absent.
    """
    if not isinstance(record_dict, dict):
        raise ValidationError("record must be an object")
    draft = dict(record_dict)
    draft["id"] = ID_PREFIX + "0" * 24
    draft["prev"] = GENESIS_PREV
    payload = Record.from_dict(draft).to_dict()
    del payload["id"], payload["prev"]
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return ID_PREFIX + digest[:24]


def _validate(r: Record) -> None:
    if not isinstance(r.id, str) or not _RECORD_ID.fullmatch(r.id):
        raise ValidationError(f"id must be 'rec_' followed by 24 hex chars: {r.id!r}")
    if r.kind not in KINDS:
        raise ValidationError(f"unknown kind: {r.kind!r}")
    if r.epistemic not in EPISTEMICS:
        raise ValidationError(f"unknown epistemic: {r.epistemic!r}")
    for name in ("author", "body"):
        if not isinstance(getattr(r, name), str):
            raise ValidationError(f"{name} must be a string")
    _validate_created_at(r.created_at)
    if not isinstance(r.prev, str) or not _HEX64.fullmatch(r.prev):
        raise ValidationError("prev must be 64 lowercase hex chars")
    for source in r.provenance:
        if not isinstance(source, Source):
            raise ValidationError("provenance entries must be sources")
    if r.epistemic == "verified" and not r.provenance:
        raise ValidationError("verified record requires at least one provenance source")
    for value in r.supersedes:
        if not isinstance(value, str):
            raise ValidationError("supersedes entries must be record ids")
    for name in _OPTIONAL_STR:
        value = getattr(r, name)
        if value is not None and not isinstance(value, str):
            raise ValidationError(f"{name} must be a string")
    if r.kind == "closure" and not r.target:
        raise ValidationError("closure record requires a target")
    if r.kind != "closure" and r.target is not None:
        raise ValidationError(f"target is only allowed on closure records, not {r.kind!r}")
    if r.kind == "obligation" and not r.owner:
        raise ValidationError("obligation record requires an owner")
    if r.kind != "obligation" and r.owner is not None:
        raise ValidationError(f"owner is only allowed on obligation records, not {r.kind!r}")


def _validate_created_at(value: Any) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValidationError(f"created_at must be an ISO 8601 UTC timestamp ending in Z: {value!r}")
    try:
        parsed = datetime.fromisoformat(value[:-1])
    except ValueError as exc:
        raise ValidationError(f"created_at is not ISO 8601: {value!r}") from exc
    if parsed.tzinfo is not None:
        raise ValidationError(f"created_at must use the Z suffix only: {value!r}")
