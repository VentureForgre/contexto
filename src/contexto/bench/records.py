"""Readers and validators for the files of a ``contexto bench`` output directory.

The files are ``manifest.json``, ``probes.jsonl``, ``requests.jsonl`` and ``reach.jsonl``. The live
run and the replay write them; the report reads them through this module. A reader returns plain
dicts, exactly as written, after checking every field this package relies on.

Two rules:

- A bad file is an error, never a silent skip. ``RecordError`` names the file, the line (for a
  JSONL file) and the field, so a truncated or hand-edited file cannot shrink a result unnoticed.
- A field this module does not know is accepted and kept, so a later version can add fields.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FORMAT = 1
KINDS = ("live", "replay")
UPSTREAMS = ("anthropic", "custom", "local")
STATUSES = ("complete", "stopped", "not_run")
BASES = ("measured", "estimated", "modelled")


class RecordError(ValueError):
    """A file of an output directory that cannot be used. ``path`` is the file, ``line`` the
    1-based line of a JSONL file (None for ``manifest.json``) and ``field`` the field at fault
    (None when the whole line or file is at fault)."""

    def __init__(self, path: Path | str, problem: str, line: int | None = None, field: str | None = None):
        self.path, self.line, self.field, self.problem = Path(path), line, field, problem
        where = str(path) + (f": line {line}" if line is not None else "")
        if field is not None:
            where += f": field {field!r}"
        super().__init__(f"{where}: {problem}")


class _Bad(Exception):
    """A field that failed its check, before the file and line are known."""

    def __init__(self, field: str, problem: str):
        self.field, self.problem = field, problem


Check = Callable[[Any, str], None]


def _show(value: Any) -> str:
    text = json.dumps(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _integer(minimum: int | None = None, nullable: bool = False) -> Check:
    def check(value: Any, field: str) -> None:
        if value is None and nullable:
            return
        if not _is_int(value):
            raise _Bad(field, f"expected a whole number{' or null' if nullable else ''}, got {_show(value)}")
        if minimum is not None and value < minimum:
            raise _Bad(field, f"expected {minimum} or more, got {value}")
    return check


def _string(nullable: bool = False) -> Check:
    def check(value: Any, field: str) -> None:
        if value is None and nullable:
            return
        if not isinstance(value, str):
            raise _Bad(field, f"expected a string{' or null' if nullable else ''}, got {_show(value)}")
    return check


def _boolean(value: Any, field: str) -> None:
    if not isinstance(value, bool):
        raise _Bad(field, f"expected true or false, got {_show(value)}")


def _choice(*allowed: str) -> Check:
    def check(value: Any, field: str) -> None:
        if value not in allowed:
            raise _Bad(field, f"expected one of {', '.join(repr(a) for a in allowed)}, got {_show(value)}")
    return check


def _list_of(item: Check) -> Check:
    def check(value: Any, field: str) -> None:
        if not isinstance(value, list):
            raise _Bad(field, f"expected a list, got {_show(value)}")
        for i, element in enumerate(value):
            item(element, f"{field}[{i}]")
    return check


def _map_of(item: Check) -> Check:
    def check(value: Any, field: str) -> None:
        if not isinstance(value, dict):
            raise _Bad(field, f"expected an object, got {_show(value)}")
        for key, element in value.items():
            item(element, f"{field}.{key}")
    return check


def _object(fields: dict[str, Check], nullable: bool = False) -> Check:
    def check(value: Any, field: str) -> None:
        if value is None and nullable:
            return
        if not isinstance(value, dict):
            raise _Bad(field, f"expected an object{' or null' if nullable else ''}, got {_show(value)}")
        _fields(value, fields, f"{field}.")
    return check


def _fields(record: dict[str, Any], fields: dict[str, Check], prefix: str = "") -> None:
    for name, check in fields.items():
        if name not in record:
            raise _Bad(prefix + name, "missing")
        check(record[name], prefix + name)


def _format(value: Any, field: str) -> None:
    if value != FORMAT or isinstance(value, bool):
        raise _Bad(field, f"this version reads format {FORMAT}, got {_show(value)}")


_UNIT: dict[str, Check] = {
    "arm": _string(), "seed": _integer(0), "repeat": _integer(0), "status": _choice(*STATUSES),
    "reason": _string(nullable=True), "cli_exit": _integer(nullable=True),
    "compactions": _integer(0, nullable=True), "compaction_pre_tokens": _list_of(_integer(0)),
    "concluded_ok": _map_of(_boolean), "toolloop_reads": _integer(0, nullable=True),
    "probe_turns_completed": _integer(0),
}

_MANIFEST: dict[str, Check] = {
    "format": _format, "kind": _choice(*KINDS), "contexto_version": _string(),
    "claude_code_version": _string(nullable=True), "model": _string(nullable=True), "bound": _integer(1),
    "protocol": _string(), "turn_kbytes": _integer(1), "seed": _integer(0), "seeds": _list_of(_integer(0)),
    "repeats": _integer(1), "arms": _list_of(_string()), "upstream": _choice(*UPSTREAMS),
    "plain_compaction": _object({"variable": _string(), "value": _integer(0), "intended_trigger": _integer(0),
                                 "forced": _boolean, "note": _string()}, nullable=True),
    "cache_ttl": _string(nullable=True), "started": _string(), "finished": _string(nullable=True),
    "units": _list_of(_object(_UNIT)),
}

_PROBE: dict[str, Check] = {
    "arm": _string(), "seed": _integer(0), "repeat": _integer(0), "model": _string(), "id": _string(),
    "cls": _string(), "type": _string(), "tier": _string(), "kind": _string(), "turn": _integer(0),
    "overlap": _integer(0), "expected": _string(), "given": _string(nullable=True), "correct": _boolean,
    "stale": _boolean,
}

_REQUEST: dict[str, Check] = {
    "arm": _string(), "seed": _integer(0), "repeat": _integer(0), "model": _string(nullable=True),
    "n": _integer(0), "turn": _integer(0, nullable=True), "main": _boolean,
    "history_tokens": _integer(0, nullable=True), "uncached": _integer(0), "cache_write": _integer(0),
    "cache_read": _integer(0), "output": _integer(0), "status": _integer(0), "compaction": _boolean,
    "basis": _choice(*BASES),
}

_REACH: dict[str, Check] = {
    "seed": _integer(0), "id": _string(), "cls": _string(), "overlap": _integer(0),
    "plant_turns": _list_of(_integer(0)), "paged": _boolean, "query": _string(), "reachable": _boolean,
    "rank": _integer(1, nullable=True),
}


def _check_request(record: dict[str, Any]) -> None:
    _fields(record, _REQUEST)
    if record["history_tokens"] is None and record["turn"] is not None:
        raise _Bad("history_tokens", "may be null only when 'turn' is null: a request that serves a turn has a "
                                     "history length")


def _text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RecordError(path, f"cannot read the file: {exc}") from exc


def _jsonl(path: Path, check: Callable[[dict[str, Any]], None]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for number, line in enumerate(_text(path).splitlines(), start=1):
        if not line.strip():
            raise RecordError(path, "empty line: every line holds one JSON object", line=number)
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise RecordError(path, f"not JSON ({exc}); a line cut short means the file was not finished",
                              line=number) from exc
        if not isinstance(record, dict):
            raise RecordError(path, f"not a JSON object, got {_show(record)}", line=number)
        try:
            check(record)
        except _Bad as bad:
            raise RecordError(path, bad.problem, line=number, field=bad.field) from None
        out.append(record)
    return out


def read_manifest(path: Path | str) -> dict[str, Any]:
    """Read and check ``manifest.json``, of a live run or of a replay."""
    path = Path(path)
    try:
        data = json.loads(_text(path))
    except ValueError as exc:
        raise RecordError(path, f"not JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise RecordError(path, f"not a JSON object, got {_show(data)}")
    try:
        _fields(data, _MANIFEST)
    except _Bad as bad:
        raise RecordError(path, bad.problem, field=bad.field) from None
    return data


def read_probes(path: Path | str) -> list[dict[str, Any]]:
    """Read and check ``probes.jsonl``: one record per arm, seed, repeat and probe, in file order."""
    return _jsonl(Path(path), lambda record: _fields(record, _PROBE))


def read_requests(path: Path | str) -> list[dict[str, Any]]:
    """Read and check ``requests.jsonl``: one record per request that reached an upstream.

    ``history_tokens`` may be null only on a request whose ``turn`` is null (one that could not be
    attributed to a turn). The schema document lists it as a number; this one case is accepted
    because such a request has no place on the history axis.
    """
    return _jsonl(Path(path), _check_request)


def read_reach(path: Path | str) -> list[dict[str, Any]]:
    """Read and check ``reach.jsonl`` (replay only): one record per probe."""
    return _jsonl(Path(path), lambda record: _fields(record, _REACH))


@dataclass(frozen=True)
class Run:
    """One live output directory, read and checked: its manifest and its two record files."""

    path: Path
    manifest: dict[str, Any]
    probes: list[dict[str, Any]]
    requests: list[dict[str, Any]]


def unit_key(record: dict[str, Any]) -> tuple[str, int, int]:
    """What identifies a unit inside one directory: the arm, the seed and the repeat."""
    return (record["arm"], record["seed"], record["repeat"])


def _unit_words(key: tuple[str, int, int]) -> str:
    return f"{key[0]}, seed {key[1]}, repeat {key[2]}"


def read_run(out_dir: Path | str) -> Run:
    """Read a live output directory and check that its files agree with each other.

    Beyond the per-file checks: no unit is listed twice in the manifest, every probe and request
    record belongs to a unit the manifest lists, probe records exist only for units that are
    ``complete``, and no probe is answered twice in one unit. A replay directory is refused: its
    results are written by ``contexto bench --replay``, not recomputed from records.
    """
    out_dir = Path(out_dir)
    manifest_path = out_dir / "manifest.json"
    manifest = read_manifest(manifest_path)
    if manifest["kind"] != "live":
        raise RecordError(manifest_path, "this is a replay directory; its results.json is written by "
                                         "contexto bench --replay and is not recomputed from records",
                          field="kind")
    status: dict[tuple[str, int, int], str] = {}
    for i, unit in enumerate(manifest["units"]):
        key = unit_key(unit)
        if key in status:
            raise RecordError(manifest_path, f"the unit {_unit_words(key)} is listed twice", field=f"units[{i}]")
        status[key] = unit["status"]
    probes_path, requests_path = out_dir / "probes.jsonl", out_dir / "requests.jsonl"
    probes = read_probes(probes_path)
    seen: set[tuple[tuple[str, int, int], str]] = set()
    for number, row in enumerate(probes, start=1):
        key = unit_key(row)
        if key not in status:
            raise RecordError(probes_path, f"the unit {_unit_words(key)} is not in manifest.json", line=number)
        if status[key] != "complete":
            raise RecordError(probes_path, f"the unit {_unit_words(key)} is {status[key]!r} in manifest.json; "
                                           "probe records are written only for complete units", line=number)
        if (key, row["id"]) in seen:
            raise RecordError(probes_path, f"the probe {row['id']!r} appears twice for the unit {_unit_words(key)}",
                              line=number, field="id")
        seen.add((key, row["id"]))
    requests = read_requests(requests_path)
    for number, row in enumerate(requests, start=1):
        if unit_key(row) not in status:
            raise RecordError(requests_path, f"the unit {_unit_words(unit_key(row))} is not in manifest.json",
                              line=number)
    return Run(out_dir, manifest, probes, requests)
