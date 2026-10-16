"""The session store: paged-out turns, the page index, pinned text, and the session log.

``SessionStore`` keeps one directory per session (``$CONTEXTO_HOME/sessions/<id>/``):

Directories are 0700 and files 0600 whatever the umask (``contexto.private``, contract PR #25).

- ``pages/<page id>.json``: one immutable page per paged-out turn (see ``contexto.store.pages``).
- ``page.key``: the random per-store HMAC key for page ids. It never leaves the store.
- ``ledger.jsonl`` (with ``ledger.lock`` and ``quarantine/``): the hash-chained session ledger.
  Opening a store repairs a crash-truncated tail and logs ``ledger_repaired``; other damage is
  left for a human and logged as ``ledger_damaged`` (pins and closes then fail).
- ``meta.json``: session metadata. ``workdir`` (the directory file sources are checked against,
  recorded by lane A's launcher with ``set_workdir``) and ``window_tokens`` and ``bytes_per_token``
  (recorded by lane A's engine with ``set_window``; recall sizes chunks from them). Writers
  serialize on ``meta.lock``; the file is replaced atomically, so readers never see a partial file.
- ``log.jsonl``: the session log, one canonical JSON event per line.

Ledger methods (``pin``, ``close``, ``state``, ``state_text``, ``pinned_text``) come from
``contexto.store.records.LedgerOps``; see that module for the API the MCP server uses.

``contexto.store.memory.MemoryStore`` has the same methods and keeps everything in memory.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from contexto.canonical import canonical_bytes, canonical_json
from contexto.paths import session_dir
from contexto.private import open_private, private_dir, write_new
from contexto.store.ledger import Ledger, LedgerCorruptError
from contexto.store.pages import build_page, render_index, same_content, search_pages
from contexto.store.recall import lists_identifiers
from contexto.store.records import LedgerOps

__all__ = ["SessionStore", "PageConflictError", "PageNotFoundError", "KeyFileError", "SessionNotFoundError",
           "redact"]

_PAGE_ID = re.compile(r"pg_[0-9a-f]{16}")


def check_page_id(page_id: Any) -> str:
    """``page_id`` if it is a well-formed page id; ValueError before any path is built from it."""
    if not isinstance(page_id, str) or not _PAGE_ID.fullmatch(page_id):
        raise ValueError(f"not a page id: {page_id!r}")
    return page_id

KEY_BYTES = 32

_SECRET_KEYS = {"authorization", "x-api-key", "cookie", "set-cookie", "proxy-authorization"}


class PageConflictError(RuntimeError):
    """A page file exists under an id with different content; pages are immutable.

    ``page_id`` names the conflicting page, so a caller can quarantine it without parsing the message.
    """

    def __init__(self, message: str, *, page_id: str | None = None) -> None:
        super().__init__(message)
        self.page_id = page_id


def check_window(window_tokens: Any, bytes_per_token: Any) -> None:
    if not isinstance(window_tokens, int) or isinstance(window_tokens, bool) or window_tokens <= 0:
        raise ValueError(f"window_tokens must be a positive integer, not {window_tokens!r}")
    if (not isinstance(bytes_per_token, (int, float)) or isinstance(bytes_per_token, bool)
            or not bytes_per_token > 0 or bytes_per_token == float("inf")):
        raise ValueError(f"bytes_per_token must be a positive number, not {bytes_per_token!r}")


MODEL_CHARS = 200  # a model name, at most


def check_model(model: Any) -> str | None:
    """A model name for the window record: None, or 1 to 200 printable characters."""
    if model is None:
        return None
    if not isinstance(model, str) or not 0 < len(model) <= MODEL_CHARS or not model.isprintable():
        raise ValueError(f"model must be None or 1 to {MODEL_CHARS} printable characters, not {model!r}")
    return model


def read_window_model(meta: dict[str, Any]) -> str | None:
    """The recorded window model; anything malformed reads as unknown."""
    try:
        return check_model(meta.get("window_model"))
    except ValueError:
        return None


def read_window(meta: dict[str, Any]) -> tuple[int | None, float | None]:
    """Valid recorded window values from session metadata; anything malformed reads as unknown."""
    w, r = meta.get("window_tokens"), meta.get("bytes_per_token")
    w = w if isinstance(w, int) and not isinstance(w, bool) and w > 0 else None
    r = float(r) if isinstance(r, (int, float)) and not isinstance(r, bool) and 0 < r < float("inf") else None
    return w, r


def check_turn_room(turn_id: Any, room_tokens: Any) -> str:
    """The turn id as a string; ValueError for an empty id or a room that is not an integer.

    A room may be zero or negative (a turn already at or over the bound): nothing remains.
    """
    if isinstance(turn_id, int) and not isinstance(turn_id, bool):
        turn_id = str(turn_id)
    if not isinstance(turn_id, str) or not turn_id:
        raise ValueError(f"turn_id must be a non-empty string, not {turn_id!r}")
    if not isinstance(room_tokens, int) or isinstance(room_tokens, bool):
        raise ValueError(f"room_tokens must be an integer, not {room_tokens!r}")
    return turn_id


RETAINED_ROLES = ("user", "assistant", "tool_result")


def check_retained(epoch: Any, k: Any, turns: Any) -> list[dict[str, Any]]:
    """The retained-window entries ``{turn, message_index, role, text}`` (contract PR #125); ValueError otherwise."""
    for name, value in (("epoch", epoch), ("k", k)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer, not {value!r}")
    if not isinstance(turns, list):
        raise ValueError("turns must be a list of entries")
    entries = []
    for entry in turns:
        if not isinstance(entry, dict) or set(entry) != {"turn", "message_index", "role", "text"}:
            raise ValueError("each entry must be exactly {turn, message_index, role, text}")
        for name in ("turn", "message_index"):
            if not isinstance(entry[name], int) or isinstance(entry[name], bool) or entry[name] < 0:
                raise ValueError(f"entry {name} must be a non-negative integer")
        if entry["role"] not in RETAINED_ROLES or not isinstance(entry["text"], str):
            raise ValueError(f"entry role must be one of {', '.join(RETAINED_ROLES)} and text a string")
        entries.append(dict(entry))
    return entries


def read_retained(data: Any) -> tuple[int, int, int, list[dict[str, Any]]] | None:
    """(epoch, k, stamp, entries) from a retained snapshot; anything malformed reads as no snapshot."""
    if not isinstance(data, dict):
        return None
    stamp = data.get("stamp")
    if not isinstance(stamp, int) or isinstance(stamp, bool):
        return None
    try:
        entries = check_retained(data.get("epoch"), data.get("k"), data.get("entries"))
    except ValueError:
        return None
    return data["epoch"], data["k"], stamp, entries


EPOCH_VERSION = 1
_HEX64 = re.compile(r"[0-9a-f]{64}")
EPOCH_DIALECTS = ("anthropic", "responses")
_CONVERSATION = re.compile(r"(?:anthropic|responses):[0-9a-f]{64}")  # rewrite.keyed(dialect, request)


def _hex64(v: Any) -> bool:
    return isinstance(v, str) and bool(_HEX64.fullmatch(v))


def _nat(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _nat_or_none(v: Any) -> bool:
    return v is None or _nat(v)


# Rule 16 (contract PR #176): the main conversation's epoch record. Names follow the engine's
# EpochState and Config; the store checks shape only, the engine checks meaning (the resume checks).
EPOCH_FIELDS: dict[str, Callable[[Any], bool]] = {
    **{name: _nat for name in ("epoch", "k", "strip_upto", "turns_paged", "max_turn_growth", "last_turn_count",
                               "carried_text_streak", "window_minimal_count")},
    **{name: _nat_or_none for name in ("last_turn_start_n", "head_tokens", "initial_carried",
                                       "last_carried_text_count", "turn_room_low_epoch")},
    # W = bound - headroom - F can be negative (bound_too_small).
    "window_budget": lambda v: v is None or (isinstance(v, int) and not isinstance(v, bool)),
    **{name: (lambda v: isinstance(v, str)) for name in ("pinned_text", "index_text")},
    "preamble_sha256": _hex64,
    "head_fingerprint": _hex64,
    "dialect": lambda v: v in EPOCH_DIALECTS,
    "conversation": lambda v: isinstance(v, str) and bool(_CONVERSATION.fullmatch(v)),
    "model": lambda v: v is None or isinstance(v, str),
    "paged_carried": lambda v: isinstance(v, list) and all(_hex64(h) for h in v),
    # dataclasses.asdict(Config), kept opaque: the engine compares it for equality, so a new setting
    # needs no store change.
    "config": lambda v: isinstance(v, dict),
    "anchor": lambda v: isinstance(v, dict) and set(v) == {"n", "hash"} and _nat(v["n"]) and _hex64(v["hash"]),
}


def check_epoch(record: Any) -> dict[str, Any]:
    """An epoch record with exactly the rule 16 fields, version stamped; ValueError otherwise."""
    if not isinstance(record, dict):
        raise ValueError("an epoch record must be an object")
    if record.get("version", EPOCH_VERSION) != EPOCH_VERSION:
        raise ValueError(f"epoch record version must be {EPOCH_VERSION}")
    fields = {k: v for k, v in record.items() if k != "version"}
    if set(fields) != set(EPOCH_FIELDS):
        missing, extra = sorted(set(EPOCH_FIELDS) - set(fields)), sorted(set(fields) - set(EPOCH_FIELDS))
        raise ValueError(f"epoch record fields differ (missing {missing}, unexpected {extra})")
    bad = sorted(name for name, ok in EPOCH_FIELDS.items() if not ok(fields[name]))
    if bad:
        raise ValueError(f"epoch record fields have the wrong type: {bad}")
    return {**json.loads(canonical_json(fields)), "version": EPOCH_VERSION}


def read_epoch(data: Any) -> dict[str, Any] | None:
    """The epoch record if its shape and version are right; anything else reads as no record."""
    if not isinstance(data, dict) or data.get("version") != EPOCH_VERSION:
        return None
    try:
        return check_epoch(data)
    except ValueError:
        return None


def read_room_stamp(meta: dict[str, Any]) -> int | None:
    """The request sequence the current turn room was published under; None when unstamped."""
    room = meta.get("turn_room")
    seq = room.get("seq") if isinstance(room, dict) else None
    return seq if isinstance(seq, int) and not isinstance(seq, bool) else None


def read_turn_room(meta: dict[str, Any]) -> tuple[str | None, int | None, int]:
    """(turn id, room tokens, tokens spent this turn) from session metadata; malformed reads as unpublished."""
    room = meta.get("turn_room")
    if not isinstance(room, dict):
        return None, None, 0
    turn_id, tokens, spent = room.get("turn_id"), room.get("room_tokens"), room.get("spent_tokens", 0)
    if (not isinstance(turn_id, str) or not turn_id or not isinstance(tokens, int) or isinstance(tokens, bool)):
        return None, None, 0
    spent = spent if isinstance(spent, int) and not isinstance(spent, bool) and spent >= 0 else 0
    return turn_id, tokens, spent


def charge_room(current: tuple[str | None, int | None, int], charge: Callable[[int | None], int]) -> dict[str, Any] | None:
    """Run ``charge(remaining)`` and return the updated record, or None when no room is published.

    ``charge`` gets the tokens left this turn (None when no room is published, which leaves the
    turn unbudgeted) and returns the tokens it used, between 0 and what remained.
    """
    turn_id, room_tokens, spent = current
    if turn_id is None or room_tokens is None:
        charge(None)
        return None
    remaining = max(0, room_tokens - spent)
    used = charge(remaining)
    if not isinstance(used, int) or isinstance(used, bool) or not 0 <= used <= remaining:
        raise ValueError(f"a turn-room charge must be between 0 and {remaining} tokens, not {used!r}")
    return {"turn_id": turn_id, "room_tokens": room_tokens, "spent_tokens": spent + used}


class SessionNotFoundError(LookupError):
    """``SessionStore.open_existing`` was asked for a session that does not exist."""


class PageNotFoundError(LookupError):
    """``quarantine_page`` was given a page that is neither stored nor already quarantined."""


class KeyFileError(RuntimeError):
    """The store's page key file is empty, short, or not hex; page ids cannot be trusted."""


def redact(event: Any) -> Any:
    """Copy of ``event`` with credential-bearing keys replaced, at any depth.

    Redaction is by key name only (``authorization``, ``x-api-key``, cookies). A credential inside
    a string value under another key is not detected, so callers must not log raw header blobs
    or request bodies.
    """
    if isinstance(event, dict):
        return {k: ("[redacted]" if str(k).lower() in _SECRET_KEYS else redact(v)) for k, v in event.items()}
    if isinstance(event, list):
        return [redact(v) for v in event]
    return event


class SessionStore(LedgerOps):
    def __init__(self, home: Path, session_id: str) -> None:
        self.session_id = session_id
        self.dir = session_dir(home, session_id)
        self.pages_dir = self.dir / "pages"
        # Narrow an existing wide CONTEXTO_HOME and sessions/ too, not only directories we create.
        private_dir(Path(home))
        private_dir(Path(home) / "sessions")
        private_dir(self.dir)
        private_dir(self.pages_dir)
        self._key_cache = self._load_key()
        self.ledger = Ledger(self.dir)
        self._repair_ledger()

    def _repair_ledger(self) -> None:
        if not self.ledger.path.exists():
            return
        try:
            quarantined = self.ledger.repair()
        except LedgerCorruptError as e:
            # Class and position only: the message can quote stored values.
            line = re.search(r"\bline (\d+)\b", str(e))
            self.log({"event": "ledger_damaged", "error": type(e).__name__,
                      "line": int(line.group(1)) if line else None})
            return
        if quarantined is not None:
            self.log({"event": "ledger_repaired", "quarantine": quarantined.name})

    def set_workdir(self, path: Path) -> None:
        """Record the session's working directory (absolute), which file sources are checked against."""
        self._update_meta({"workdir": str(Path(path).resolve())})

    def set_window(self, window_tokens: int, bytes_per_token: float, model: str | None = None) -> None:
        """Record the engine's window budget W (tokens) and the session's measured bytes per token.

        Lane A's engine calls this when either value changes; recall sizes its chunks from it.
        ``model`` is the request model the ratio was measured on, so the engine can tell whether a
        restored ratio still applies; it is stored as ``window_model``, and None removes it.
        """
        check_window(window_tokens, bytes_per_token)
        model = check_model(model)

        def change(meta: dict[str, Any]) -> dict[str, Any]:
            meta.pop("window_model", None)
            updates: dict[str, Any] = {"window_tokens": window_tokens, "bytes_per_token": float(bytes_per_token)}
            if model is not None:
                updates["window_model"] = model
            return updates

        self._modify_meta(change)

    def set_turn_room(self, turn_id: str, room_tokens: int) -> None:
        """Publish the room left in the current turn (M2 contract, per-turn recall budget; PR #89).

        Lane A's engine calls this at request time, from the pre-send estimate, and again when the
        response's real usage arrives; the proxy holds the stream until then, so a tool call sees
        the current room. Every publish replaces the room and resets the tokens spent to 0, even
        for the same ``turn_id``: the room was measured from input that already holds the recall
        results returned so far, so keeping the counter would charge them twice. The write takes
        meta.lock, so a racing charge lands either before the reset or against the new room.
        """
        turn_id = check_turn_room(turn_id, room_tokens)
        # B19: stamped with this request's sequence only if this request's set_retained persisted it.
        stamp = getattr(self, "_retained_stamp", None)
        self._update_meta({"turn_room": {"turn_id": turn_id, "room_tokens": room_tokens, "spent_tokens": 0,
                                         "seq": stamp}})

    def set_retained(self, epoch: int, k: int, turns: list[dict[str, Any]]) -> None:
        """Snapshot the retained window for search (contract PR #125, lane A's A14 calls this once per request).

        Increments the session's persisted request sequence, then overwrites ``retained.json`` (0600,
        atomic) stamped with it; the room publishes that follow carry the same stamp, and search uses
        the snapshot only when the stamps match. If the increment cannot be persisted, the old
        snapshot is removed, and in any case this store's rooms are stamped None until a later call
        succeeds, so a previous request's snapshot can never match. If the snapshot write fails, the
        sequence has moved on, so the old snapshot no longer matches (lane A logs ``retained_stale``).
        Errors are raised for the caller to handle. Never logged; the index and stubs never read it.
        """
        entries = check_retained(epoch, k, turns)
        self._retained_stamp = None
        seq: dict[str, int] = {}

        def bump(meta: dict[str, Any]) -> dict[str, Any]:
            current = meta.get("request_seq")
            seq["n"] = (current if isinstance(current, int) and not isinstance(current, bool) else 0) + 1
            return {"request_seq": seq["n"]}

        try:
            self._modify_meta(bump)
        except OSError:
            try:
                (self.dir / "retained.json").unlink(missing_ok=True)
            except OSError:
                pass  # rooms stay stamped None in this store, so the old snapshot still cannot match
            raise
        self._retained_stamp = seq["n"]
        try:
            self._write_atomic(self.dir / "retained.json",
                               canonical_bytes({"stamp": seq["n"], "epoch": epoch, "k": k, "entries": entries}))
        except OSError:
            try:  # defense in depth: no previous request's snapshot stays behind (reviewer-1, PR #128)
                (self.dir / "retained.json").unlink(missing_ok=True)
            except OSError:
                pass  # retained_current() still refuses it: its stamp is behind the persisted sequence
            raise

    def set_epoch(self, record: dict[str, Any]) -> None:
        """Save the main conversation's epoch record (rule 16, contract PR #176), for a restart to resume.

        Writes ``epoch.json`` atomically at mode 0600 with ``version: 1``; never logged. The store
        checks shape and version only (ValueError for a wrong one); the engine checks meaning when
        it resumes. On a failed write the previous record is unlinked and the error is raised; if
        the unlink fails too, that older record stays, which the contract accepts (the newest record
        written is trusted, and rules 4 and 5 bound a resume with a smaller ``k``).
        """
        data = canonical_bytes(check_epoch(record))
        try:
            self._write_atomic(self.dir / "epoch.json", data)
        except OSError:
            try:
                (self.dir / "epoch.json").unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def epoch_record(self) -> dict[str, Any] | None:
        """The saved epoch record, or None when there is none or it is unreadable, malformed, or of
        an unknown version."""
        try:
            data = json.loads((self.dir / "epoch.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return read_epoch(data)

    def retained_current(self) -> tuple[int, int, int, list[dict[str, Any]]] | None:
        """The snapshot only if it is the current request's: its stamp equals both the room's stamp and
        the persisted request sequence. Search uses this, so it cannot serve an older request's window."""
        snapshot = self.retained()
        meta = self._meta()
        seq = meta.get("request_seq")
        if snapshot is None or not isinstance(seq, int) or isinstance(seq, bool):
            return None
        return snapshot if snapshot[2] == seq == read_room_stamp(meta) else None

    def retained(self) -> tuple[int, int, int, list[dict[str, Any]]] | None:
        """(epoch, k, stamp, entries) of the retained snapshot, or None when there is none or it is malformed."""
        try:
            data = json.loads((self.dir / "retained.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return read_retained(data)

    def room_stamp(self) -> int | None:
        """The request sequence of the current turn room; None when unpublished or unstamped."""
        return read_room_stamp(self._meta())

    def turn_room(self) -> tuple[str | None, int | None, int]:
        """(turn id, room tokens, tokens spent this turn); (None, None, 0) until the engine publishes."""
        return read_turn_room(self._meta())

    def charge_turn_room(self, charge: Callable[[int | None], int]) -> None:
        """Charge this turn's room atomically: ``charge`` runs while meta.lock is held.

        See ``charge_room``. Parallel tool calls serialize here, so together they never spend
        more than the room.
        """
        def update(meta: dict[str, Any]) -> dict[str, Any] | None:
            record = charge_room(read_turn_room(meta), charge)
            return None if record is None else {"turn_room": {**record, "seq": read_room_stamp(meta)}}

        self._modify_meta(update)

    def _write_atomic(self, path: Path, data: bytes) -> None:
        """Write ``path`` privately and atomically: a fsynced temp file renamed into place."""
        tmp = self.dir / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        fd = open_private(tmp, os.O_WRONLY | os.O_EXCL)
        try:
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)  # leave no partial temp file behind
            raise

    def _update_meta(self, updates: dict[str, Any]) -> None:
        """Merge ``updates`` into meta.json: writers serialize on meta.lock; readers never block."""
        self._modify_meta(lambda meta: updates)

    def _modify_meta(self, change: Callable[[dict[str, Any]], dict[str, Any] | None]) -> None:
        """Merge ``change(meta)`` into meta.json under meta.lock; None writes nothing.

        The new file is written privately, fsynced, and renamed into place, so a concurrent reader
        sees either the old or the new complete file, and no writer drops another writer's field.
        """
        lock = open_private(self.dir / "meta.lock", os.O_RDWR)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            meta = self._meta()
            updates = change(meta)
            if updates is None:
                return
            meta.update(updates)
            tmp = self.dir / f".meta.json.{os.getpid()}.{secrets.token_hex(4)}.tmp"
            fd = open_private(tmp, os.O_WRONLY | os.O_EXCL)
            try:
                os.write(fd, canonical_bytes(meta))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self.dir / "meta.json")
        finally:
            os.close(lock)  # releases the flock

    def _meta(self) -> dict[str, Any]:
        try:
            meta = json.loads((self.dir / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):  # missing, unreadable, not UTF-8, or not JSON: treat as unrecorded
            return {}
        return meta if isinstance(meta, dict) else {}

    def workdir(self) -> Path | None:
        value = self._meta().get("workdir")
        return Path(value) if isinstance(value, str) and value else None

    def window(self) -> tuple[int | None, float | None]:
        """(window budget W in tokens, measured bytes per token), each None until the engine records it."""
        return read_window(self._meta())

    def window_model(self) -> str | None:
        """The model the recorded window was measured on; None when not recorded."""
        return read_window_model(self._meta())

    @classmethod
    def open_existing(cls, home: Path, session_id: str) -> SessionStore:
        """Open a session for reading without creating anything; SessionNotFoundError if it is absent.

        Writing pages through a store opened this way loads (or creates) the page key on first use.
        """
        path = session_dir(home, session_id)
        if not path.is_dir():
            raise SessionNotFoundError(f"no session {session_id!r} under {Path(home) / 'sessions'}")
        store = cls.__new__(cls)
        store.session_id = session_id
        store.dir = path
        store.pages_dir = path / "pages"
        store._key_cache = None
        store.ledger = Ledger(path)
        store._read_only = True
        return store

    @property
    def _key(self) -> bytes:
        if self._key_cache is None:
            private_dir(self.pages_dir)
            self._key_cache = self._load_key()
        return self._key_cache

    def _load_key(self) -> bytes:
        """The store's HMAC key, created atomically on first use; refuses an empty or short key."""
        path = self.dir / "page.key"
        if not path.exists():
            # A losing racer's write_new returns False and reads the winner's complete key below.
            write_new(path, secrets.token_bytes(KEY_BYTES).hex().encode("ascii"))
        try:
            key = bytes.fromhex(path.read_text(encoding="ascii").strip())
        except (ValueError, UnicodeDecodeError) as e:
            raise KeyFileError(f"{path} is not a hex key: {e}") from e
        if len(key) != KEY_BYTES:
            raise KeyFileError(f"{path} holds a {len(key)}-byte key, expected {KEY_BYTES}")
        return key

    def page_out(self, turns: list[list[dict]], first_turn: int) -> list[str]:
        """Write one page per turn (turn numbers from ``first_turn``); return their ids in order.

        Re-paging an existing id is a no-op that keeps the stored page, even if the stub or the
        estimates were computed differently when it was written; ``PageConflictError`` only when the
        stored ``{turn, messages}`` differ under the same id (contract PR #47).
        """
        ids = []
        for offset, turn in enumerate(turns):
            page = build_page(self._key, turn, first_turn + offset)
            path = self.pages_dir / f"{page['id']}.json"
            data = canonical_bytes(page)
            if not write_new(path, data):
                try:
                    stored = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as e:
                    raise PageConflictError(f"{path} exists but cannot be read: {e}", page_id=page["id"]) from e
                if not same_content(stored, page):
                    raise PageConflictError(f"{path} exists with different turn or messages", page_id=page["id"])
            ids.append(page["id"])
        return ids

    def quarantine_page(self, page_id: str) -> Path:
        """Move a stored page aside so the same id can be paged afresh (contract rule 14, PR #52).

        ``pages/<id>.json`` is renamed atomically to ``quarantine/<id>.<UTC timestamp>.<suffix>.json``
        (directory 0700, file 0600); its content is never changed or deleted. Returns the quarantine
        path and logs ``page_quarantined`` with the page id and file name only. Idempotent: if the
        page is no longer stored but was quarantined before, returns its most recent quarantine
        path; if it was never stored, raises ``PageNotFoundError``. Concurrent calls move the file
        once and all return that path.
        """
        check_page_id(page_id)
        source = self.pages_dir / f"{page_id}.json"
        quarantine = self.dir / "quarantine"
        private_dir(quarantine)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"{time.time_ns() % 1_000_000_000:09d}Z"
        target = quarantine / f"{page_id}.{stamp}.{secrets.token_hex(4)}.json"
        try:
            os.rename(source, target)  # atomic on one filesystem; the unique name never overwrites
        except FileNotFoundError:
            earlier = self._quarantined(page_id)
            if earlier is None:
                raise PageNotFoundError(f"no stored or quarantined page {page_id}") from None
            return earlier
        os.chmod(target, 0o600)
        fd = os.open(quarantine, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self.log({"event": "page_quarantined", "page_id": page_id, "path": f"quarantine/{target.name}"})
        return target

    def _quarantined(self, page_id: str) -> Path | None:
        found = sorted((self.dir / "quarantine").glob(f"{page_id}.*.json"))
        return found[-1] if found else None

    def _pages(self) -> list[dict[str, Any]]:
        out = []
        if not self.pages_dir.is_dir():
            return out
        for path in self.pages_dir.glob("pg_*.json"):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError) as e:
                self.log({"event": "page_unreadable", "path": path.name, "error": str(e)})
        return out

    def get_page(self, page_id: str) -> dict[str, Any] | None:
        """The page record for ``page_id``, or None if this store has no such page."""
        if not page_id.startswith("pg_") or not all(c in "0123456789abcdef" for c in page_id[3:]):
            return None
        try:
            return json.loads((self.pages_dir / f"{page_id}.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def page_ids(self) -> list[str]:
        """Every page id in this store, newest turn first."""
        return [p["id"] for p in sorted(self._pages(), key=lambda p: (p["turn"], p["id"]), reverse=True)]

    def search(self, query: str) -> list[str]:
        """Ids of pages whose stub or text contains ``query`` exactly, newest first."""
        return search_pages(self._pages(), query)

    def index_text(self, budget_tokens: int, variant: str | None = None) -> str:
        return render_index(self._pages(), budget_tokens, salient=lists_identifiers(self.recall_variant(variant)))

    def _recorded_variant(self) -> str | None:
        value = self._meta().get("recall_variant")
        return value if isinstance(value, str) else None

    def _record_variant(self, variant: str) -> None:
        if getattr(self, "_read_only", False):  # inspecting a session never writes to it
            return
        try:
            self._update_meta({"recall_variant": variant})
        except OSError:
            pass  # recording is a convenience; an unwritable meta.json must not break rendering

    def log(self, event: dict) -> None:
        """Append ``event`` (credentials redacted, ``ts`` added if absent) to ``log.jsonl``."""
        record = redact(dict(event))
        record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        line = (canonical_json(record) + "\n").encode("utf-8")
        fd = open_private(self.dir / "log.jsonl", os.O_WRONLY | os.O_APPEND)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
