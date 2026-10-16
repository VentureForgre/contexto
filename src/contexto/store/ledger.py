"""Append-only, hash-chained ledger of records, one per session.

Written by builder-2 for M1 packet A2 (unpushed commit 1ab5f29) and moved here for M2: the files
now live in the session directory, ``ledger.jsonl``, ``ledger.lock``, and ``quarantine/``.
``MemoryLedger`` applies the same append rules in memory. Each line is the
canonical JSON of ``Record.to_dict()`` followed by a single ``\\n``.

Hash chain: a record's ``prev`` is the SHA-256 hex digest of the previous
line's UTF-8 bytes, excluding that line's trailing newline. The first record's
``prev`` is ``GENESIS_PREV`` (64 zeros). ``Ledger.head()`` returns the value the
next appended record will carry as ``prev``.

Files are 0600 and directories 0700 whatever the umask (``contexto.private``, contract PR #25).

Durability and concurrency: every mutation (append, repair) holds an exclusive
``fcntl.flock`` on ``ledger.lock`` for the whole read, validate,
write sequence, and writes are followed by ``os.fsync``. Reads hold a shared
lock so they never observe a half-written line from a live writer.

Truncated tail: the final line is truncated when it lacks a trailing newline
or is not parseable JSON, which is what a crash mid-append leaves behind.
``records()`` and ``head()`` stop before a truncated tail, since it was never
committed, while ``verify()`` reports it. ``append`` refuses to extend a ledger
that does not verify clean. ``repair()`` moves a truncated tail into
``quarantine/``; any other damage, including a parseable final
line with a bad id or chain link, is left for a human and ``repair()`` refuses.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from contexto.canonical import canonical_json
from contexto.private import open_private, private_dir
from contexto.store.model import GENESIS_PREV, Record, ValidationError, compute_id, utc_timestamp

LEDGER_FILE = "ledger.jsonl"
LOCK_FILE = "ledger.lock"
QUARANTINE_DIR = "quarantine"


class LedgerError(Exception):
    """The ledger refused an operation."""


class LedgerCorruptError(LedgerError):
    """The ledger file is damaged in a way the operation cannot work around."""


@dataclass(frozen=True)
class _Line:
    number: int  # 1-based
    offset: int  # byte offset of the line start
    raw: bytes  # without the trailing newline
    complete: bool  # ended with a newline
    record: Record | None
    problem: str | None  # line-local damage, or None
    truncated: bool = False  # final line that is incomplete or not parseable JSON


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse(data: bytes) -> list[_Line]:
    lines: list[_Line] = []
    offset = 0
    parts = data.split(b"\n")
    for index, raw in enumerate(parts):
        last = index == len(parts) - 1
        if last and raw == b"":
            break
        number = index + 1
        complete = not last
        # A complete line is final only when nothing follows its newline.
        final = index == len(parts) - 2 and parts[-1] == b""
        obj = _load(raw) if complete else None
        if not complete:
            line = _Line(number, offset, raw, False, None,
                         f"line {number}: truncated final line (no trailing newline)", True)
        elif obj is _UNPARSEABLE and final:
            line = _Line(number, offset, raw, True, None,
                         f"line {number}: truncated final line (unparseable JSON)", True)
        elif obj is _UNPARSEABLE:
            line = _Line(number, offset, raw, True, None, f"line {number}: unparseable JSON")
        else:
            line = _Line(number, offset, raw, True, *_decode(number, raw, obj))
        lines.append(line)
        offset += len(raw) + 1
    return lines


_UNPARSEABLE = object()


def _load(raw: bytes) -> object:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _UNPARSEABLE


def _decode(number: int, raw: bytes, obj: object) -> tuple[Record | None, str | None]:
    try:
        record = Record.from_dict(obj)
    except ValidationError as exc:
        return None, f"line {number}: invalid record ({exc})"
    expected_id = compute_id(obj)
    if record.id != expected_id:
        return record, f"line {number}: id mismatch (stored {record.id}, computed {expected_id})"
    if canonical_json(record.to_dict()).encode("utf-8") != raw:
        return record, f"line {number}: not canonical JSON"
    return record, None


def _problems(lines: list[_Line]) -> list[tuple[int, str]]:
    """Return (line number, message) pairs in line order."""
    found = [(line.number, line.problem) for line in lines if line.problem]
    expected = GENESIS_PREV
    for line in lines:
        if line.record is not None and line.record.prev != expected:
            found.append(
                (line.number, f"line {line.number}: broken chain (prev {line.record.prev}, expected {expected})")
            )
            break
        expected = _sha256(line.raw)
    found.sort(key=lambda item: item[0])
    return found


def _intact(lines: list[_Line]) -> list[_Line]:
    """Return the lines before a truncated tail."""
    return lines[:-1] if lines and lines[-1].truncated else lines


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _draft_fields(draft: dict) -> dict:
    if not isinstance(draft, dict):
        raise ValidationError("draft must be an object")
    for name in ("id", "prev"):
        if name in draft:
            raise ValidationError(f"draft must not set {name!r}; the ledger computes it")
    fields = dict(draft)
    if fields.get("created_at") is None:
        fields["created_at"] = utc_timestamp()
    return fields


def _chain(fields: dict, prev: str, existing: list[Record]) -> Record:
    record = Record.from_dict({**fields, "id": compute_id(fields), "prev": prev})
    Ledger._check_references(record, existing)
    return record


class Ledger:
    def __init__(self, data_dir: Path):
        """``data_dir`` holds ledger.jsonl, ledger.lock, and quarantine/ (the session directory)."""
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / LEDGER_FILE
        self.lock_path = self.data_dir / LOCK_FILE
        self.quarantine_dir = self.data_dir / QUARANTINE_DIR

    @contextmanager
    def _locked(self, exclusive: bool) -> Iterator[bool]:
        """Hold the ledger lock. Yields False, without locking, for reads of a ledger never created."""
        if not exclusive and not self.data_dir.exists():
            yield False
            return
        private_dir(self.data_dir)
        fd = open_private(self.lock_path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield True
        finally:
            os.close(fd)  # closing the descriptor releases the flock

    def _read(self) -> bytes:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return b""

    def _lines(self) -> list[_Line]:
        return _parse(self._read())

    def records(self) -> list[Record]:
        """Return intact records in file order, stopping before a truncated tail.

        A line above the tail that cannot be decoded into a record raises
        LedgerCorruptError. Hash and chain damage does not raise here; call
        verify() to check integrity.
        """
        with self._locked(exclusive=False) as exists:
            if not exists:
                return []
            lines = self._lines()
        out = []
        for line in _intact(lines):
            if line.record is None:
                raise LedgerCorruptError(line.problem)
            out.append(line.record)
        return out

    def head(self) -> str:
        """Return the SHA-256 of the last intact line, or GENESIS_PREV when there is none."""
        with self._locked(exclusive=False) as exists:
            if not exists:
                return GENESIS_PREV
            intact = _intact(self._lines())
        return _sha256(intact[-1].raw) if intact else GENESIS_PREV

    def verify(self) -> list[str]:
        """Return integrity problems; an empty list means the ledger is intact."""
        with self._locked(exclusive=False) as exists:
            if not exists:
                return []
            lines = self._lines()
        return [message for _, message in _problems(lines)]

    def append(self, draft: dict) -> Record:
        """Validate a draft, chain it, and durably append it as one line."""
        fields = _draft_fields(draft)
        with self._locked(exclusive=True):
            lines = self._lines()
            problems = _problems(lines)
            if problems:
                raise LedgerCorruptError(
                    f"ledger does not verify; run repair or inspect manually: {problems[0][1]}"
                )
            prev = _sha256(lines[-1].raw) if lines else GENESIS_PREV
            record = _chain(fields, prev, [line.record for line in lines])

            created = not self.path.exists()
            fd = open_private(self.path, os.O_WRONLY | os.O_APPEND)
            try:
                _write_all(fd, canonical_json(record.to_dict()).encode("utf-8") + b"\n")
                os.fsync(fd)
            finally:
                os.close(fd)
            if created:
                _fsync_dir(self.data_dir)
        return record

    @staticmethod
    def _check_references(record: Record, existing: list[Record]) -> None:
        by_id = {r.id: r for r in existing}
        if record.id in by_id:
            raise LedgerError(f"record id already exists: {record.id}")
        if record.kind == "closure":
            target = by_id.get(record.target)
            if target is None:
                raise LedgerError(f"closure target does not exist: {record.target}")
            if target.kind != "obligation":
                raise LedgerError(f"closure target is a {target.kind}, not an obligation: {record.target}")
            if any(record.target in r.supersedes for r in existing):
                raise LedgerError(f"closure target is a superseded obligation: {record.target}")
        missing = [rid for rid in record.supersedes if rid not in by_id]
        if missing:
            raise LedgerError(f"supersedes unknown record ids: {missing}")

    def repair(self) -> Path | None:
        """Quarantine a truncated tail and return the quarantine file path.

        Returns None when the ledger already verifies clean. Raises
        LedgerCorruptError, changing nothing, for any damage other than a
        truncated tail. Quarantined bytes are written and fsynced before the
        ledger is truncated, so no data is lost on a crash between the steps.
        """
        with self._locked(exclusive=True):
            lines = self._lines()
            problems = _problems(lines)
            if not problems:
                return None
            tail = lines[-1]
            other = [message for number, message in problems if not (tail.truncated and number == tail.number)]
            if other:
                raise LedgerCorruptError(
                    f"damage other than a truncated tail; left for a human, refusing to repair: {other[0]}"
                )
            data = self._read()
            bad = data[tail.offset :]
            quarantined = self._quarantine(bad)
            fd = os.open(self.path, os.O_WRONLY)
            try:
                os.ftruncate(fd, tail.offset)
                os.fsync(fd)
            finally:
                os.close(fd)
        return quarantined

    def _quarantine(self, data: bytes) -> Path:
        private_dir(self.quarantine_dir)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        attempt = 0
        while True:
            name = f"{stamp}.jsonl" if attempt == 0 else f"{stamp}-{attempt}.jsonl"
            path = self.quarantine_dir / name
            try:
                fd = open_private(path, os.O_WRONLY | os.O_EXCL)
            except FileExistsError:
                attempt += 1
                continue
            try:
                _write_all(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            _fsync_dir(self.quarantine_dir)
            return path


class MemoryLedger:
    """In-memory ledger with the same append rules and line format, for ``MemoryStore``."""

    def __init__(self) -> None:
        self._lines: list[bytes] = []
        self._records: list[Record] = []

    def records(self) -> list[Record]:
        return list(self._records)

    def head(self) -> str:
        return _sha256(self._lines[-1]) if self._lines else GENESIS_PREV

    def verify(self) -> list[str]:
        return []

    def append(self, draft: dict) -> Record:
        record = _chain(_draft_fields(draft), self.head(), self._records)
        self._lines.append(canonical_json(record.to_dict()).encode("utf-8"))
        self._records.append(record)
        return record
