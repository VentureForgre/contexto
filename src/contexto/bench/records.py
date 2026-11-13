"""Readers and validators for the files of a ``contexto bench`` output directory.

The files are ``manifest.json``, ``probes.jsonl``, ``requests.jsonl``, ``reach.jsonl`` and
``results.json``. The live run and the replay write them; the report and the charts read them
through this module. A reader returns plain dicts, exactly as written, after checking every field
this package relies on.

Three rules:

- A bad file is an error, never a silent skip. ``RecordError`` names the file, the line (for a
  JSONL file) and the field, so a truncated or hand-edited file cannot shrink a result unnoticed.
- A field this module does not know is accepted and kept, so a later version can add fields.
- The JSON is read strictly (``loads``): a key repeated in one object, ``NaN``, ``Infinity``, a
  number too large for a float and a string that is not valid Unicode are errors wherever they
  are, because each of them is read differently, or not at all, by another JSON reader.

Fields the schema document does not list and this module reads when they are present: the
manifest's ``label`` (a short line the report prints at the top of ``results.md`` and in every
chart's title, for example to say that a stub answered), and on a unit ``probes`` and ``requests``
(how many records the unit wrote, so that a file cut at the end of a unit is noticed) and
``cache_ttl`` (the unit's own cache lifetime where it differs from the manifest's).
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FORMAT = 1
KINDS = ("live", "replay")
UPSTREAMS = ("anthropic", "custom", "local")
STATUSES = ("complete", "stopped", "not_run")
BASES = ("measured", "estimated", "modelled")
CACHE_TTLS = ("5m", "1h")
LARGEST = 10 ** 12  # no count of tokens, probes or requests is larger; a longer number is a damaged file
LABEL_MAX = 200


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
    try:
        text = json.dumps(value)
    except (ValueError, RecursionError):  # a number of thousands of digits, or nesting too deep to print
        return "a value too large to show"
    return text if len(text) <= 60 else text[:57] + "..."


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _integer(minimum: int | None = None, nullable: bool = False) -> Check:
    def check(value: Any, field: str) -> None:
        if value is None and nullable:
            return
        if not _is_int(value):
            raise _Bad(field, f"expected a whole number{' or null' if nullable else ''}, got {_show(value)}")
        if abs(value) > LARGEST:
            raise _Bad(field, f"expected {LARGEST:,} or less, got {_show(value)}")
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


def _or_null(check: Check) -> Check:
    def nullable(value: Any, field: str) -> None:
        if value is not None:
            check(value, field)
    return nullable


def _label(value: Any, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise _Bad(field, f"expected a string or null, got {_show(value)}")
    if len(value) > LABEL_MAX:
        raise _Bad(field, f"expected {LABEL_MAX} characters or fewer, got {len(value)}")
    if not value.isprintable():
        raise _Bad(field, f"expected one line of printable characters, got {_show(value)}")


def _number(low: float, high: float) -> Check:
    def check(value: Any, field: str) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _Bad(field, f"expected a number, got {_show(value)}")
        if not low <= value <= high:  # false for NaN too
            raise _Bad(field, f"expected a number from {low:,} to {high:,}, got {_show(value)}")
    return check


def _pair_of(item: Check) -> Check:
    def check(value: Any, field: str) -> None:
        if not isinstance(value, list) or len(value) != 2:
            raise _Bad(field, f"expected a pair of numbers, got {_show(value)}")
        for i, element in enumerate(value):
            item(element, f"{field}[{i}]")
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


def _object(fields: dict[str, Check], nullable: bool = False, optional: Mapping[str, Check] | None = None) -> Check:
    def check(value: Any, field: str) -> None:
        if value is None and nullable:
            return
        if not isinstance(value, dict):
            raise _Bad(field, f"expected an object{' or null' if nullable else ''}, got {_show(value)}")
        _fields(value, fields, f"{field}.", optional)
    return check


def _fields(record: dict[str, Any], fields: Mapping[str, Check], prefix: str = "",
            optional: Mapping[str, Check] | None = None) -> None:
    """Check the listed fields of a record: each of ``fields`` must be there, each of ``optional``
    is checked when it is there."""
    for name, check in fields.items():
        if name not in record:
            raise _Bad(prefix + name, "missing")
        check(record[name], prefix + name)
    for name, check in (optional or {}).items():
        if name in record:
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
_UNIT_OPTIONAL: dict[str, Check] = {
    "probes": _integer(0, nullable=True), "requests": _integer(0, nullable=True),
    "cache_ttl": _or_null(_choice(*CACHE_TTLS)),
}

_MANIFEST: dict[str, Check] = {
    "format": _format, "kind": _choice(*KINDS), "contexto_version": _string(),
    "claude_code_version": _string(nullable=True), "model": _string(nullable=True), "bound": _integer(1),
    "protocol": _string(), "turn_kbytes": _integer(1), "seed": _integer(0), "seeds": _list_of(_integer(0)),
    "repeats": _integer(1), "arms": _list_of(_string()), "upstream": _choice(*UPSTREAMS),
    "plain_compaction": _object({"variable": _string(), "value": _integer(0), "intended_trigger": _integer(1),
                                 "forced": _boolean, "note": _string()}, nullable=True),
    "cache_ttl": _string(nullable=True), "started": _string(), "finished": _string(nullable=True),
    "units": _list_of(_object(_UNIT, optional=_UNIT_OPTIONAL)),
}
_MANIFEST_OPTIONAL: dict[str, Check] = {"label": _label}

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


# results.json: what the charts read. The rest of the file is the report's own and is not checked here.

_SHARE = _number(0, 1)
_SERIES = _object({"basis": _choice(*BASES), "points": _list_of(_pair_of(_number(0, LARGEST)))},
                  optional={"label": _string(nullable=True)})
_RESULTS_MANIFEST = _object({}, nullable=True, optional={
    "model": _string(nullable=True), "protocol": _string(nullable=True), "seed": _integer(0, nullable=True),
    "seeds": _or_null(_list_of(_integer(0))), "contexto_version": _string(nullable=True),
    "claude_code_version": _string(nullable=True), "label": _label})


def _cell(nested: bool) -> Check:
    """An accuracy cell: ``probes``, ``correct``, ``accuracy`` and ``interval``, the last two null
    when no probe was asked. An arm's cell also holds ``by_class``, a cell per probe class."""
    def check(value: Any, field: str) -> None:
        if not isinstance(value, dict):
            raise _Bad(field, f"expected an object, got {_show(value)}")
        _fields(value, {"probes": _integer(0), "correct": _integer(0)}, f"{field}.")
        if value["correct"] > value["probes"]:
            raise _Bad(f"{field}.correct", f"expected {value['probes']} or less (the number of probes), got "
                                           f"{value['correct']}")
        if value["probes"] or value.get("accuracy") is not None or value.get("interval") is not None:
            _fields(value, {"accuracy": _SHARE, "interval": _pair_of(_SHARE)}, f"{field}.")
            if value["interval"][0] > value["interval"][1]:
                raise _Bad(f"{field}.interval", f"the lower end is above the upper, got {_show(value['interval'])}")
        if nested and value.get("by_class") is not None:
            _map_of(_cell(nested=False))(value["by_class"], f"{field}.by_class")
    return check


_ARMS = _or_null(_map_of(_cell(nested=True)))
_RESULTS: dict[str, Check] = {"format": _format, "kind": _choice(*KINDS), "bound": _integer(1)}
_RESULTS_OPTIONAL: dict[str, Check] = {
    "notes": _or_null(_list_of(_string())), "manifest": _RESULTS_MANIFEST, "arms": _ARMS,
    "by_model": _or_null(_map_of(_object({}, optional={"arms": _ARMS, "request_series": _or_null(_map_of(_SERIES))}))),
}


def _check_results(data: dict[str, Any]) -> None:
    if "request_series" not in data:
        raise _Bad("request_series", "missing: not a bench results file")
    _map_of(_SERIES)(data["request_series"], "request_series")
    _fields(data, _RESULTS, optional=_RESULTS_OPTIONAL)


def _check_request(record: dict[str, Any]) -> None:
    _fields(record, _REQUEST)
    if record["history_tokens"] is None and record["turn"] is not None:
        raise _Bad("history_tokens", "may be null only when 'turn' is null: a request that serves a turn has a "
                                     "history length")


def _text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RecordError(path, "cannot read the file: it is missing") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise RecordError(path, f"cannot read the file: {exc}") from exc


class _Repeated(Exception):
    """A key that an object holds twice."""

    def __init__(self, key: str):
        self.key = key


def _no_repeats(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _Repeated(key)
        out[key] = value
    return out


def _sound(value: Any) -> None:
    """Refuse what the strict reading does not allow, anywhere inside ``value``: a number that is
    not finite and a string that cannot be written as UTF-8. Raises ``_Bad`` with the path of the
    value as its field. It walks with a list of its own, not by recursion, so depth cannot stop it."""
    todo: list[tuple[str, Any]] = [("", value)]
    while todo:
        field, item = todo.pop()
        if isinstance(item, float):
            if not math.isfinite(item):
                raise _Bad(field, "not a finite number (NaN, Infinity or a number too large for a float); JSON "
                                  "has no such value")
        elif isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                raise _Bad(field, "a string that is not valid Unicode (half of a surrogate pair), which cannot "
                                  "be written as UTF-8") from None
        elif isinstance(item, dict):
            for key, element in item.items():
                try:
                    key.encode("utf-8")
                except UnicodeEncodeError:
                    raise _Bad(field, "a key that is not valid Unicode (half of a surrogate pair)") from None
                todo.append((f"{field}.{key}" if field else key, element))
        elif isinstance(item, list):
            todo.extend((f"{field}[{i}]", element) for i, element in enumerate(item))


def loads(text: str) -> Any:
    """Read JSON text strictly. A key repeated in one object raises ``_Repeated``; nesting too deep
    to read raises ``RecursionError``; anything else that is not JSON raises ``ValueError``. The
    caller then checks the value's known fields and calls ``_sound`` on the whole of it."""
    return json.loads(text, object_pairs_hook=_no_repeats)


def _parse(path: Path, text: str, check: Callable[[dict[str, Any]], None], line: int | None = None,
           hint: str = "") -> dict[str, Any]:
    """One JSON object of a file, read strictly and checked. Every failure is a ``RecordError``."""
    try:
        record = loads(text)
    except _Repeated as repeated:
        raise RecordError(path, "the key appears twice in one object, and readers disagree on which value "
                                "counts", line=line, field=repeated.key) from None
    except RecursionError:
        raise RecordError(path, "not JSON a reader can take: it is nested too deeply", line=line) from None
    except ValueError as exc:
        raise RecordError(path, f"not JSON ({exc}){hint}", line=line) from exc
    if not isinstance(record, dict):
        raise RecordError(path, f"not a JSON object, got {_show(record)}", line=line)
    try:
        check(record)
        _sound(record)
    except _Bad as bad:
        raise RecordError(path, bad.problem, line=line, field=bad.field) from None
    return record


def _jsonl(path: Path, check: Callable[[dict[str, Any]], None]) -> list[dict[str, Any]]:
    # Only a line feed ends a line (after an optional carriage return). str.splitlines() would also
    # split at U+2028, U+2029, U+0085 and others, which JSON allows inside a string.
    lines = _text(path).split("\n")
    if lines[-1] == "":
        lines.pop()
    out: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        line = line.removesuffix("\r")
        if not line.strip():
            raise RecordError(path, "empty line: every line holds one JSON object", line=number)
        out.append(_parse(path, line, check, line=number,
                          hint="; a line cut short means the file was not finished"))
    return out


def read_manifest(path: Path | str) -> dict[str, Any]:
    """Read and check ``manifest.json``, of a live run or of a replay."""
    path = Path(path)
    return _parse(path, _text(path), lambda data: _fields(data, _MANIFEST, optional=_MANIFEST_OPTIONAL))


def read_results(path: Path | str) -> dict[str, Any]:
    """Read ``results.json``, of a live run or of a replay, and check what the charts read from it.

    Checked: ``format``, ``kind`` and ``bound`` (1 or more); ``request_series`` (each series'
    ``basis``, its ``label`` when it has one, and ``points`` as pairs of numbers from 0 to
    10^12); ``notes`` as a list of strings; the manifest fields a chart prints; and for a live
    file each arm's accuracy cell, overall and by class, under ``arms`` and under ``by_model``:
    whole numbers of probes, ``correct`` no larger than ``probes``, ``accuracy`` and both ends of
    ``interval`` from 0 to 1. The rest of the file is returned as it is.
    """
    path = Path(path)
    return _parse(path, _text(path), _check_results)


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
    ``complete`` and name the manifest's model, and no probe is answered twice in one unit. A
    request record may name another model: a CLI sends some side requests to a smaller one. A
    replay directory is refused: its results are written by ``contexto bench --replay``, not
    recomputed from records.

    Against a ``requests.jsonl`` that lost records: no request number ``n`` is used twice in one
    unit, the numbers of a unit run from 0 with no gap (in any order in the file), and a
    ``complete`` unit has one request record or more. A file cut at the end of a unit leaves no
    gap, so a unit that carries its counts (``probes``, ``requests``) is also checked against them.
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
        if row["model"] != manifest["model"]:
            raise RecordError(probes_path, f"the record is of model {row['model']!r} but manifest.json says the run "
                                           f"used {manifest['model']!r}", line=number, field="model")
        if (key, row["id"]) in seen:
            raise RecordError(probes_path, f"the probe {row['id']!r} appears twice for the unit {_unit_words(key)}",
                              line=number, field="id")
        seen.add((key, row["id"]))
    requests = read_requests(requests_path)
    numbers: dict[tuple[str, int, int], set[int]] = {key: set() for key in status}
    for number, row in enumerate(requests, start=1):
        key = unit_key(row)
        if key not in status:
            raise RecordError(requests_path, f"the unit {_unit_words(key)} is not in manifest.json", line=number)
        if row["n"] in numbers[key]:
            raise RecordError(requests_path, f"the request {row['n']} appears twice for the unit "
                                             f"{_unit_words(key)}", line=number, field="n")
        numbers[key].add(row["n"])
    answered: dict[tuple[str, int, int], int] = {key: 0 for key in status}
    for key, _ in seen:
        answered[key] += 1
    for unit in manifest["units"]:
        key = unit_key(unit)
        found = numbers[key]
        lost = next((n for n in range(len(found)) if n not in found), None)
        if lost is not None:
            raise RecordError(requests_path, f"the unit {_unit_words(key)} has no request {lost}: a unit's "
                                             "requests are numbered from 0 with no gap, so a record was lost")
        if unit["status"] == "complete" and not found:
            raise RecordError(requests_path, f"the unit {_unit_words(key)} is complete in manifest.json but has "
                                             "no request record: the file was cut short or emptied")
        for name, path, have, words in (("requests", requests_path, len(found), "request"),
                                        ("probes", probes_path, answered[key], "probe")):
            if unit.get(name) is not None and unit[name] != have:
                raise RecordError(path, f"the unit {_unit_words(key)} has {have} {words} records and "
                                        f"manifest.json says {unit[name]}: the file was cut short or added to")
    return Run(out_dir, manifest, probes, requests)
