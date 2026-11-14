"""The pinned session of ``contexto bench --replay``: the trace file, its checks and its recorder.

The shipped trace, ``contexto/bench/data/trace-<seed>.json``, is a few kilobytes. It does not hold
the session's text. It pins:

- the script's parameters (``PINNED``: seed, protocol, bound, turn size, and the number of ``d27``
  content turns given to ``contexto.bench.script.build``);
- every turn's byte length and SHA-256, every file's, and the script's digest (which also covers the
  probes and their answers);
- the scripted model behaviour: the rule per turn kind, the rounds per turn and the SHA-256 of every
  scripted reply (``contexto.bench.replay.scripted``);
- the SHA-256 of what the synthetic client generates (its system prompt, context block and tool
  definitions) and the byte rates the fake upstream prices requests at;
- the measurements a replay of this session gives (``expected``): the size of every request, the
  rotations, the requests over the bound, the failure events, the reach totals, and a digest of the
  reach rows.

Before anything runs, the session is regenerated from the seed and compared with the pinned hashes
(``check``). A difference is refused with a ``TraceError`` that names the first differing turn, so a
changed generator is never replayed silently under the old expectations.

The trace is written by ``record`` (``contexto bench --replay --record-trace FILE``) and never edited
by hand. The expectations are a record of what the proxy does to this session: a change to what the
proxy sends upstream (the rewrite engine, the page index, the wording of Contexto's MCP tool
descriptions, the search ranking) changes them, and the trace is then recorded again and the
difference reviewed.
"""

from __future__ import annotations

import hashlib
import json
import platform
from importlib import resources
from pathlib import Path
from typing import Any

from contexto.bench.model import Script
from contexto.bench.replay import client, reach, scripted, upstream

FORMAT = 1
KIND = "contexto-bench-replay-trace"
# The pinned session. ``turns`` is the number of d27 content turns given to ``script.build``: 40 of
# about 40,000 bytes, to which protocol v2 adds 8 content turns and 2 probe turns, 56 turns in all and
# 952,971 estimated tokens of history, 6.35 times the bound.
PINNED = {"seed": 20261016, "protocol": "v2", "bound": 150_000, "turn_bytes": 40_000, "turns": 40}
RATES = {"log_bytes_per_token": upstream.LOG_BYTES_PER_TOKEN, "other_bytes_per_token": upstream.OTHER_BYTES_PER_TOKEN,
         "log_text_min_bytes": upstream.LOG_TEXT_MIN_BYTES, "tool_result_text_is_log_text": True}
ROUNDING = 2  # tokens a request's size moves by from rounding up its two parts (trace._where)
REACH_FIELDS = ("id", "paged", "queries", "reachable", "rank", "in_view", "covered", "returned", "best_rank",
                "best_query")


class TraceError(ValueError):
    """The trace cannot be used: it is missing or damaged, or it and the generator disagree."""


def file_name(seed: int = PINNED["seed"]) -> str:
    return f"trace-{seed}.json"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# The sections a trace must have, and the JSON type of each.
SECTIONS = {"script": dict, "turn_bytes": list, "turn_sha256": list, "files": dict, "model": dict, "client": dict,
            "expected": dict}
_JSON_TYPES = {dict: "an object", list: "a list"}


def _json_type(value: Any) -> str:
    for kind, name in ((dict, "an object"), (list, "a list"), (str, "a string"), (bool, "true or false"),
                       ((int, float), "a number")):
        if isinstance(value, kind):
            return name
    return "null"


def parse(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise TraceError(f"the trace is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise TraceError(f"not a replay trace: the file holds {_json_type(data)}, not an object")
    if data.get("kind") != KIND:
        raise TraceError(f"not a replay trace: its kind is {data.get('kind')!r}, not {KIND!r}")
    if data.get("format") != FORMAT:
        raise TraceError(f"a replay trace of format {data.get('format')!r}; this build reads format {FORMAT}")
    missing = [key for key in SECTIONS if key not in data]
    if missing:
        raise TraceError(f"the trace has no {', '.join(missing)}")
    wrong = [f"{key} is {_json_type(data[key])}, not {_JSON_TYPES[kind]}" for key, kind in SECTIONS.items()
             if not isinstance(data[key], kind)]
    if wrong:
        raise TraceError(f"the trace is damaged: {'; '.join(wrong)}")
    return data


def load_packaged(seed: int = PINNED["seed"]) -> dict[str, Any]:
    """The trace shipped in the package. Raises ``TraceError`` when this build has none."""
    source = resources.files("contexto.bench").joinpath("data", file_name(seed))
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise TraceError(f"the pinned trace {file_name(seed)} is not UTF-8 text, so it is damaged") from exc
    except (OSError, ValueError) as exc:
        raise TraceError(f"this build has no pinned trace {file_name(seed)} ({type(exc).__name__})") from exc
    return parse(text)


def fingerprint(script: Script) -> dict[str, Any]:
    """What a trace pins of a session, computed from the script."""
    planned = scripted.steps(script)
    return {
        "turn_bytes": list(script.turn_bytes),
        "turn_sha256": [_sha(text) for text in script.turns],
        "files": {path: {"bytes": len(text.encode("utf-8")), "sha256": _sha(text)}
                  for path, text in script.files.items()},
        "script_sha256": script.digest(),
        "model": {"rules": dict(scripted.RULES), "rounds": scripted.rounds(script), "steps": len(planned),
                  "steps_sha256": scripted.digest(planned)},
        "client": {"synthetic_sha256": client.synthetic_digest()},
        "rates": dict(RATES),
    }


def check(trace: dict[str, Any], script: Script, turns: int) -> None:
    """Refuse (``TraceError``) unless ``script`` is the session ``trace`` pins. The first differing
    turn is named."""
    params = {"seed": script.seed, "protocol": script.protocol, "bound": script.bound,
              "turn_bytes": script.turn_size, "turns": turns}
    pinned = trace["script"]
    for key, value in params.items():
        if pinned.get(key) != value:
            raise TraceError(f"the trace pins {key} {pinned.get(key)!r}, not {value!r}; nothing was replayed")
    found = fingerprint(script)
    refuse = ("the generator and the pinned trace disagree: {what}. Nothing was replayed. Either the script "
              "generator changed, which changes the benchmark, or the trace file was altered; a maintainer who "
              "means the change records the trace again with `contexto bench --replay --record-trace FILE`.")
    if len(found["turn_sha256"]) != len(trace["turn_sha256"]):
        raise TraceError(refuse.format(what=f"the trace has {len(trace['turn_sha256'])} turns and the generator "
                                            f"gives {len(found['turn_sha256'])}"))
    for index, (digest, size) in enumerate(zip(found["turn_sha256"], found["turn_bytes"])):
        if digest != trace["turn_sha256"][index] or size != trace["turn_bytes"][index]:
            raise TraceError(refuse.format(
                what=f"turn {index + 1} of {len(script.turns)} (a {script.kinds[index]} turn) is "
                     f"{trace['turn_bytes'][index]:,} bytes with SHA-256 {str(trace['turn_sha256'][index])[:16]} in "
                     f"the trace, and {size:,} bytes with SHA-256 {digest[:16]} from the generator"))
    for path, pinned_file in found["files"].items():
        if path not in trace["files"]:
            raise TraceError(refuse.format(what=f"the generator gives the file {path}, which the trace does not have"))
        if trace["files"][path] != pinned_file:
            raise TraceError(refuse.format(what=f"the file {path} differs"))
    for path in trace["files"]:
        if path not in found["files"]:
            raise TraceError(refuse.format(what=f"the trace has the file {path}, which the generator does not give"))
    if trace.get("script_sha256") != found["script_sha256"]:
        raise TraceError(refuse.format(what="the turns are the same but the probes, answers or decisions differ"))
    if trace["model"].get("steps_sha256") != found["model"]["steps_sha256"]:
        raise TraceError("the scripted model and the pinned trace disagree: the replies the fake upstream would "
                         "give are not the ones the trace pins. Nothing was replayed.")
    if trace["client"].get("synthetic_sha256") != found["client"]["synthetic_sha256"]:
        raise TraceError("the synthetic client and the pinned trace disagree: its generated system prompt, context "
                         "block or tool definitions are not the ones the trace pins. Nothing was replayed.")
    if trace.get("rates") != found["rates"]:
        raise TraceError("the fake upstream's byte rates are not the ones the trace pins. Nothing was replayed.")


def reach_digest(rows: list[dict[str, Any]]) -> str:
    """SHA-256 of the reach rows' results (``REACH_FIELDS``), one JSON line per probe."""
    return _sha("".join(json.dumps({key: row[key] for key in REACH_FIELDS}, sort_keys=True) + "\n" for row in rows))


def expected(measured: Any) -> dict[str, Any]:
    """The measurements a trace pins, taken from a replay (``session.Measured``)."""
    return {
        "turns": measured.turns,
        "head_tokens": measured.head_tokens,
        "requests": len(measured.requests),
        "request_tokens": [r["uncached"] for r in measured.requests],
        "max_input_estimated": measured.max_input,
        "over_bound": measured.over_bound,
        "over_bound_proxy": measured.over_bound_proxy,
        "rotations": measured.rotations,
        "failure_events": dict(measured.failure_events),
        "compaction_markers": measured.compaction_markers,
        "pages": measured.pages,
        "reach": reach.totals(measured.reach),
        "reach_sha256": reach_digest(measured.reach),
    }


def differences(pinned: Any, found: Any, name: str = "") -> list[str]:
    """Every place the measured values differ from the pinned ones, in plain words. For the list of
    request sizes only the count and the first differing request are named."""
    if isinstance(pinned, dict) and isinstance(found, dict):
        out: list[str] = []
        for key in dict.fromkeys([*pinned, *found]):
            label = f"{name}.{key}" if name else str(key)
            if key not in pinned:
                out.append(f"{label}: not in the trace, measured {found[key]!r}")
            elif key not in found:
                out.append(f"{label}: expected {pinned[key]!r}, not measured")
            else:
                out += differences(pinned[key], found[key], label)
        return out
    if isinstance(pinned, list) and isinstance(found, list):
        if pinned == found:
            return []
        first = next((i for i, (a, b) in enumerate(zip(pinned, found)) if a != b), min(len(pinned), len(found)))
        changed = sum(1 for a, b in zip(pinned, found) if a != b) + abs(len(pinned) - len(found))
        a = pinned[first] if first < len(pinned) else "nothing"
        b = found[first] if first < len(found) else "nothing"
        return [f"{name}: {changed} of {max(len(pinned), len(found))} differ; the first is number {first + 1}, "
                f"expected {a}, measured {b}" + _where(pinned, found, first)]
    if type(pinned) is not type(found) and (isinstance(pinned, (dict, list)) or isinstance(found, (dict, list))):
        return [f"{name}: expected {_json_type(pinned)}, measured {_json_type(found)}"]
    return [] if pinned == found else [f"{name}: expected {pinned!r}, measured {found!r}"]


def _where(pinned: list[Any], found: list[Any], first: int) -> str:
    """For two lists of request sizes: where the difference is, which says what changed. Sizes are
    rounded up part by part, so a change of the same bytes in every request moves each by up to
    ``ROUNDING`` tokens more or less."""
    if len(pinned) != len(found) or not all(isinstance(v, int) for v in [*pinned, *found]):
        return ""
    deltas = [b - a for a, b in zip(pinned, found)]
    if all(deltas) and max(deltas) - min(deltas) <= ROUNDING:
        by = f"{min(deltas):+,}" if min(deltas) == max(deltas) else f"{min(deltas):+,} to {max(deltas):+,}"
        return (f". All {len(pinned)} differ, by {by} tokens: something every request carries changed by the "
                "same bytes (the head, the tool definitions)")
    if first:
        return (f". Requests 1 to {first} are equal and the difference starts at number {first + 1}: what the "
                "proxy sends changed from there (paging, a rotation, a search reply)")
    return ". The first request differs too, by another amount than the later ones"


def build(script: Script, turns: int, measured: Any, version: str) -> dict[str, Any]:
    """A whole trace for ``script``, with ``measured`` as its expectations."""
    return {
        "format": FORMAT, "kind": KIND,
        "note": "Written by `contexto bench --replay --record-trace FILE`. Do not edit by hand.",
        "script": {"seed": script.seed, "protocol": script.protocol, "bound": script.bound,
                   "turn_bytes": script.turn_size, "turns": turns},
        **fingerprint(script),
        "expected": expected(measured),
        "recorded_with": {"contexto": version, "commit": source_commit(), "python": platform.python_version()},
    }


def source_commit() -> str | None:
    """The git commit of the checkout this package runs from, with ``+changes`` when its tracked files
    differ from that commit, or None outside a checkout. Only ``record`` asks: a replay itself starts
    no process."""
    import subprocess

    root = Path(__file__).resolve().parents[4]
    if not (root / ".git").exists():
        return None
    try:
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=10, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return head + ("+changes" if dirty else "") if head else None


def to_json(trace: dict[str, Any]) -> str:
    return json.dumps(trace, indent=1) + "\n"


def record(path: Path, script: Script, turns: int, measured: Any, version: str) -> dict[str, Any]:
    """Write the trace for ``script`` to ``path`` and return it."""
    trace = build(script, turns, measured, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_json(trace), encoding="utf-8")
    return trace


def dump(directory: Path, script: Script, session_id: str) -> list[Path]:
    """Write the regenerated session so it can be read: every turn's text, the files the tool-loop
    turn reads, the script, the scripted model's replies and the synthetic client's head. Returns the
    paths written."""
    written: list[Path] = []

    def put(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)

    for index, text in enumerate(script.turns):
        put(directory / "turns" / f"turn-{index + 1:03d}-{script.kinds[index]}.txt", text)
    root = (directory / "files").resolve()
    for relative, text in script.files.items():
        target = (root / relative).resolve()
        if root not in target.parents:  # a script's file path never leaves the directory
            raise TraceError(f"the script names a file outside the session: {relative!r}")
        put(target, text)
    put(directory / "script.json", script.to_json())
    put(directory / "model-steps.jsonl",
        "".join(json.dumps(step.to_dict()) + "\n" for step in scripted.steps(script)))
    put(directory / "client-head.json", json.dumps({"context_block": client.context_block(),
                                                    **client.head(session_id)}, indent=1) + "\n")
    return written
