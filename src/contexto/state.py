"""Derived state over a ledger: active records, open obligations, conflicts, staleness.

Rules come from docs/milestones/M1-ledger-and-recovery.md. Everything here is
computed from ``list[Record]`` plus the working tree at read time. Nothing is
written back to disk.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from contexto.model import Record, Source

_CHUNK = 1 << 20


@dataclass(frozen=True)
class Conflict:
    topic: str
    record_ids: tuple[str, ...]  # ledger order


@dataclass(frozen=True)
class State:
    records: tuple[Record, ...]  # all records, ledger order
    active: tuple[Record, ...]  # not listed in any later record's supersedes
    superseded_by: Mapping[str, tuple[str, ...]]  # inactive id -> superseding ids
    open_obligations: tuple[Record, ...]  # active obligations with no closure targeting them
    conflicts: tuple[Conflict, ...]  # topics with two or more active decisions
    stale: frozenset[str]  # ids with a file source whose hash differs or file missing


def derive(records: Sequence[Record], root: Path) -> State:
    records = tuple(records)
    position = {r.id: i for i, r in enumerate(records)}

    superseding: dict[str, list[str]] = {}
    for i, r in enumerate(records):
        for target in r.supersedes:
            # Only a later record can supersede; ids that are unknown or later are ignored.
            if position.get(target, i) < i:
                superseding.setdefault(target, []).append(r.id)
    superseded_by = MappingProxyType({k: tuple(v) for k, v in superseding.items()})

    active = tuple(r for r in records if r.id not in superseded_by)

    # A superseded closure no longer closes anything, so its obligation reopens if still active.
    closed = {r.target for r in active if r.kind == "closure"}
    open_obligations = tuple(r for r in active if r.kind == "obligation" and r.id not in closed)

    by_topic: dict[str, list[str]] = {}
    for r in active:
        if r.kind == "decision" and r.topic is not None:
            by_topic.setdefault(r.topic, []).append(r.id)
    conflicts = tuple(Conflict(t, tuple(ids)) for t, ids in by_topic.items() if len(ids) > 1)

    cache: dict[Path, str | None] = {}
    stale = frozenset(r.id for r in records if _stale_sources(r, root, cache))

    return State(
        records=records,
        active=active,
        superseded_by=superseded_by,
        open_obligations=open_obligations,
        conflicts=conflicts,
        stale=stale,
    )


def stale_sources(record: Record, root: Path) -> list[Source]:
    """Return the record's file sources that no longer match; empty means fresh."""
    return _stale_sources(record, root, {})


def _stale_sources(record: Record, root: Path, cache: dict[Path, str | None]) -> list[Source]:
    stale = []
    for source in record.provenance:
        if source.type != "file":
            continue
        # Locators are root-relative POSIX paths and sha256 is required (contract M1).
        path = Path(root) / source.locator
        if path not in cache:
            cache[path] = _file_sha256(path)
        if cache[path] != source.sha256:
            stale.append(source)
    return stale


def _file_sha256(path: Path) -> str | None:
    """SHA-256 hex of a regular file, or None if it is missing or unreadable."""
    try:
        digest = hashlib.sha256()
        with path.open("rb") as f:
            while chunk := f.read(_CHUNK):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None
