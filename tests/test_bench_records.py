"""``contexto.bench.records``: readers and validators for the files of a bench output directory (D66).

This file also holds the fixture builders the report and chart tests share: ``make_run`` writes a
live output directory from a real generated script, with answers and request records invented by a
seeded generator. No arm is run and nothing is sent anywhere.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from contexto.bench import records, script
from contexto.bench.d27 import BYTES_PER_TOKEN_TEXT, REPLY_TOKENS, turn_tokens

SMALL_BOUND = 4_000
SMALL_TURN_BYTES = 2_000
ARMS = ("contexto", "full", "plain")
MODEL = "claude-haiku-5-5"

_SCRIPTS: dict[tuple, script.Script] = {}


def small_script(seed: int, protocol: str = "v2") -> script.Script:
    """The small script the stub tests use, built once per seed and protocol."""
    key = (seed, protocol)
    if key not in _SCRIPTS:
        _SCRIPTS[key] = script.build(seed, protocol, bound=SMALL_BOUND, turn_bytes=SMALL_TURN_BYTES)
    return _SCRIPTS[key]


def history_tokens(s: script.Script, turn: int) -> int:
    """The x axis of the request-size chart, by the rule of the schema document: the sum over turns
    0..turn of each turn's estimate, at the rates ``script.estimate`` uses."""
    total = 0
    for i in range(turn + 1):
        if i < s.n_content:
            total += turn_tokens(script.load_bytes(s, i)) + REPLY_TOKENS
        else:
            total += int(s.turn_bytes[i] / BYTES_PER_TOKEN_TEXT) + 1
    return total


def unit(arm: str, seed: int, repeat: int = 0, status: str = "complete", reason: str | None = None,
         **fields) -> dict:
    """One ``units`` entry of a manifest, as the schema document lists it."""
    cli = arm != "full"
    out = {"arm": arm, "seed": seed, "repeat": repeat, "status": status, "reason": reason,
           "cli_exit": 0 if cli else None, "compactions": 0 if cli else None, "compaction_pre_tokens": [],
           "concluded_ok": {}, "toolloop_reads": 10 if cli else None, "probe_turns_completed": 8}
    out.update(fields)
    return out


def manifest(units: list[dict], **fields) -> dict:
    """A live manifest, as the schema document lists it."""
    seeds = sorted({u["seed"] for u in units})
    out = {
        "format": 1, "kind": "live", "contexto_version": "0.5.24", "claude_code_version": "2.1.322",
        "model": MODEL, "bound": SMALL_BOUND, "protocol": "v2", "turn_kbytes": SMALL_TURN_BYTES // 1_000,
        "seed": seeds[0] if seeds else 1, "seeds": seeds, "repeats": 1 + max((u["repeat"] for u in units), default=0),
        "arms": [a for a in ARMS if any(u["arm"] == a for u in units)], "upstream": "local",
        "plain_compaction": {"variable": "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "value": SMALL_BOUND + 33_000,
                             "intended_trigger": SMALL_BOUND, "forced": True,
                             "note": "The window is the bound plus the 33,000 tokens the CLI reserves."},
        "cache_ttl": "5m", "started": "2026-11-12T10:00:00Z", "finished": "2026-11-12T12:00:00Z", "units": units,
    }
    out.update(fields)
    return out


def probe_rows(s: script.Script, arm: str, repeat: int, wrong: set[str], model: str = MODEL) -> list[dict]:
    """The ``probes.jsonl`` rows of one unit: every probe of the script, wrong for the ids in ``wrong``."""
    asked = {pid: s.n_content + k for k, ids in enumerate(s.probe_turn_ids) for pid in ids}
    rows = []
    for p in s.probes:
        ok = p.id not in wrong
        rows.append({"arm": arm, "seed": s.seed, "repeat": repeat, "model": model, "id": p.id, "cls": p.cls,
                     "type": p.type, "tier": p.tier, "kind": p.kind, "turn": asked[p.id], "overlap": p.overlap,
                     "expected": p.answer, "given": p.answer if ok else "no-such-value", "correct": ok,
                     "stale": False})
    return rows


def request_rows(s: script.Script, arm: str, repeat: int, model: str | None = MODEL,
                 basis: str = "measured") -> tuple[list[dict], list[int]]:
    """Invented ``requests.jsonl`` rows of one unit, one main request per turn, and the ``pre_tokens``
    of the compactions the plain arm made.

    The shapes are the ones the arms are expected to have, so the charts have something to draw:
    Contexto's input stops growing at 80% of the bound, the full arm's input is the whole history,
    and the plain arm's input falls to a fifth of the bound whenever it passes the bound.
    """
    rows: list[dict] = []
    pre_tokens: list[int] = []
    floor = 0
    for turn in range(len(s.turns)):
        history = history_tokens(s, turn)
        if arm == "contexto":
            size = min(history, s.bound * 8 // 10)
        elif arm == "full":
            size = history
        else:
            size = history - floor
            if size > s.bound:
                pre_tokens.append(size)
                rows.append({"arm": arm, "seed": s.seed, "repeat": repeat, "model": model, "n": len(rows),
                             "turn": turn, "main": False, "history_tokens": history, "uncached": size,
                             "cache_write": 0, "cache_read": 0, "output": 900, "status": 200, "compaction": True,
                             "basis": basis})
                floor = history - s.bound // 5
                size = history - floor
        read = size * 7 // 10
        write = size * 2 // 10
        rows.append({"arm": arm, "seed": s.seed, "repeat": repeat, "model": model, "n": len(rows), "turn": turn,
                     "main": True, "history_tokens": history, "uncached": size - read - write, "cache_write": write,
                     "cache_read": read, "output": 12, "status": 200, "compaction": False, "basis": basis})
    return rows, pre_tokens


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def write_run(out: Path, units: list[dict], probes: list[dict], requests: list[dict], **fields) -> Path:
    """Write the three files of a live output directory."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(manifest(units, **fields), indent=1) + "\n", encoding="utf-8")
    write_jsonl(out / "probes.jsonl", probes)
    write_jsonl(out / "requests.jsonl", requests)
    return out


def make_run(out: Path, seeds=(11, 12, 13), repeats: int = 2, arms=ARMS, accuracy=None, rng_seed: int = 5,
             model: str = MODEL, not_run=(), **fields) -> Path:
    """A live output directory for ``seeds`` with ``repeats`` repeats of the first seed.

    Each probe of each unit is right with the arm's probability in ``accuracy``, drawn from one
    seeded generator, so the directory is the same on every run. ``not_run`` lists ``(arm, seed)``
    units to record as not run, with no probe or request rows.
    """
    accuracy = {"contexto": 0.95, "full": 0.9, "plain": 0.7, **(accuracy or {})}
    rng = random.Random(rng_seed)
    units, probes, requests = [], [], []
    for seed in seeds:
        s = small_script(seed, fields.get("protocol", "v2"))
        for repeat in range(repeats if seed == seeds[0] else 1):
            for arm in arms:
                if (arm, seed) in not_run:
                    units.append(unit(arm, seed, repeat, status="not_run", cli_exit=None, compactions=None,
                                      toolloop_reads=None, probe_turns_completed=0,
                                      reason="the history does not fit the model's context window"))
                    continue
                wrong = {p.id for p in s.probes if rng.random() >= accuracy[arm]}
                rows, pre_tokens = request_rows(s, arm, repeat, model)
                concluded = {} if arm == "full" else {
                    p.id: rng.random() < 0.8 for p in s.probes if p.cls == "concluded"}
                units.append(unit(arm, seed, repeat, concluded_ok=concluded,
                                  compactions=None if arm == "full" else len(pre_tokens),
                                  compaction_pre_tokens=pre_tokens))
                probes += probe_rows(s, arm, repeat, wrong, model)
                requests += rows
    fields.setdefault("model", model)
    return write_run(out, units, probes, requests, **fields)


# The fixtures themselves


def test_history_tokens_at_the_last_turn_is_the_script_estimate():
    for protocol in ("v2", "d27"):
        s = small_script(11, protocol)
        assert history_tokens(s, len(s.turns) - 1) == script.estimate(s)["tokens"]
        assert history_tokens(s, s.n_content - 1) == script.estimate(s)["history_tokens"]


def test_the_fixture_directory_is_reproducible_and_reads_back(tmp_path):
    a, b = make_run(tmp_path / "a"), make_run(tmp_path / "b")
    for name in ("manifest.json", "probes.jsonl", "requests.jsonl"):
        assert (a / name).read_bytes() == (b / name).read_bytes()
    run = records.read_run(a)
    assert run.manifest["model"] == MODEL and len(run.manifest["units"]) == 12
    assert len(run.probes) == 12 * 72
    assert {r["arm"] for r in run.requests} == set(ARMS)
    assert any(r["compaction"] for r in run.requests if r["arm"] == "plain")


# manifest.json


def test_a_manifest_as_the_schema_document_lists_it_is_accepted(tmp_path):
    out = write_run(tmp_path, [unit("contexto", 1), unit("full", 1)], [], [])
    assert records.read_manifest(out / "manifest.json")["units"][1]["arm"] == "full"


def test_a_replay_manifest_is_accepted(tmp_path):
    data = manifest([], kind="replay", model=None, claude_code_version=None, plain_compaction=None, cache_ttl=None,
                    finished=None, arms=["contexto", "full", "plain"])
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert records.read_manifest(path)["kind"] == "replay"


def test_unknown_extra_fields_are_accepted(tmp_path):
    units = [unit("contexto", 1, extra_unit_field=[1, 2])]
    row = {**probe_rows(small_script(1), "contexto", 0, set())[0], "later_addition": {"x": 1}}
    request = {**request_rows(small_script(1), "contexto", 0)[0][0], "ttl": "1h"}
    out = write_run(tmp_path, units, [row], [request], added_by_a_later_version=True)
    run = records.read_run(out)
    assert run.manifest["added_by_a_later_version"] is True
    assert run.probes[0]["later_addition"] == {"x": 1} and run.requests[0]["ttl"] == "1h"


@pytest.mark.parametrize("change, field, problem", [
    ({"bound": "150000"}, "bound", "a whole number"),
    ({"bound": True}, "bound", "a whole number"),
    ({"bound": 0}, "bound", "1 or more"),
    ({"kind": "dry"}, "kind", "one of 'live', 'replay'"),
    ({"upstream": "api.example.test"}, "upstream", "one of 'anthropic', 'custom', 'local'"),
    ({"seeds": [1, "2"]}, "seeds[1]", "a whole number"),
    ({"model": 5}, "model", "a string or null"),
    ({"format": 2}, "format", "format 1"),
    ({"plain_compaction": {"variable": "X"}}, "plain_compaction.value", "missing"),
    ({"units": [{"arm": "contexto"}]}, "units[0].seed", "missing"),
])
def test_a_bad_manifest_field_is_named(tmp_path, change, field, problem):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({**manifest([unit("contexto", 1)]), **change}), encoding="utf-8")
    with pytest.raises(records.RecordError) as exc:
        records.read_manifest(path)
    assert exc.value.field == field and exc.value.line is None
    text = str(exc.value)
    assert text.startswith(f"{path}: field {field!r}: ") and problem in text


def test_a_bad_unit_field_is_named_with_its_position(tmp_path):
    units = [unit("contexto", 1), unit("full", 1, status="finished")]
    out = write_run(tmp_path, units, [], [])
    with pytest.raises(records.RecordError) as exc:
        records.read_manifest(out / "manifest.json")
    assert exc.value.field == "units[1].status"
    assert "one of 'complete', 'stopped', 'not_run'" in str(exc.value)
    units = [unit("contexto", 1, concluded_ok={"q_c_01": "yes"})]
    out = write_run(tmp_path, units, [], [])
    with pytest.raises(records.RecordError) as exc:
        records.read_manifest(out / "manifest.json")
    assert exc.value.field == "units[0].concluded_ok.q_c_01"


def test_a_missing_field_is_an_error_not_a_default(tmp_path):
    data = manifest([unit("contexto", 1)])
    del data["protocol"]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(records.RecordError, match="field 'protocol': missing"):
        records.read_manifest(path)


def test_a_manifest_that_is_not_json_or_not_an_object_is_an_error(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(records.RecordError, match="not JSON"):
        records.read_manifest(path)
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(records.RecordError, match="not a JSON object"):
        records.read_manifest(path)
    with pytest.raises(records.RecordError, match="cannot read"):
        records.read_manifest(tmp_path / "absent.json")


# probes.jsonl, requests.jsonl, reach.jsonl


def _good_rows() -> tuple[list[dict], list[dict]]:
    s = small_script(1)
    return probe_rows(s, "contexto", 0, {"q_c_01"}), request_rows(s, "contexto", 0)[0]


def test_good_lines_are_read_in_order(tmp_path):
    probes, requests = _good_rows()
    write_jsonl(tmp_path / "probes.jsonl", probes)
    write_jsonl(tmp_path / "requests.jsonl", requests)
    assert records.read_probes(tmp_path / "probes.jsonl") == probes
    assert records.read_requests(tmp_path / "requests.jsonl") == requests


@pytest.mark.parametrize("index, change, field, problem", [
    (0, {"correct": "true"}, "correct", "true or false"),
    (3, {"given": 7}, "given", "a string or null"),
    (5, {"seed": 1.5}, "seed", "a whole number"),
    (2, {"cls": None}, "cls", "a string"),
])
def test_a_bad_probe_line_names_the_file_the_line_and_the_field(tmp_path, index, change, field, problem):
    probes, _ = _good_rows()
    probes[index] = {**probes[index], **change}
    path = tmp_path / "probes.jsonl"
    write_jsonl(path, probes)
    with pytest.raises(records.RecordError) as exc:
        records.read_probes(path)
    assert (exc.value.line, exc.value.field) == (index + 1, field)
    assert str(exc.value).startswith(f"{path}: line {index + 1}: field {field!r}: ") and problem in str(exc.value)


@pytest.mark.parametrize("index, change, field, problem", [
    (0, {"basis": "guessed"}, "basis", "one of 'measured', 'estimated', 'modelled'"),
    (1, {"cache_read": -4}, "cache_read", "0 or more"),
    (2, {"main": 1}, "main", "true or false"),
    (4, {"turn": "12"}, "turn", "a whole number or null"),
    (6, {"history_tokens": None}, "history_tokens", "null only when 'turn' is null"),
])
def test_a_bad_request_line_names_the_file_the_line_and_the_field(tmp_path, index, change, field, problem):
    _, requests = _good_rows()
    requests[index] = {**requests[index], **change}
    path = tmp_path / "requests.jsonl"
    write_jsonl(path, requests)
    with pytest.raises(records.RecordError) as exc:
        records.read_requests(path)
    assert (exc.value.line, exc.value.field) == (index + 1, field)
    assert str(exc.value).startswith(f"{path}: line {index + 1}: field {field!r}: ") and problem in str(exc.value)


def test_a_request_that_cannot_be_attributed_may_have_no_turn_and_no_history(tmp_path):
    _, requests = _good_rows()
    requests[0] = {**requests[0], "turn": None, "history_tokens": None, "main": False, "model": None}
    path = tmp_path / "requests.jsonl"
    write_jsonl(path, requests)
    assert records.read_requests(path)[0]["turn"] is None


def test_a_line_that_is_not_a_json_object_is_an_error_never_skipped(tmp_path):
    probes, _ = _good_rows()
    lines = [json.dumps(r) for r in probes[:4]]
    path = tmp_path / "probes.jsonl"
    for bad, problem in (('{"arm": "contexto", "seed"', "not JSON"), ("[1]", "not a JSON object"),
                         ("", "empty line")):
        path.write_text("\n".join([*lines[:2], bad, *lines[2:]]) + "\n", encoding="utf-8")
        with pytest.raises(records.RecordError) as exc:
            records.read_probes(path)
        assert exc.value.line == 3 and exc.value.field is None
        assert str(exc.value).startswith(f"{path}: line 3: ") and problem in str(exc.value)


def test_a_missing_field_on_a_line_is_named(tmp_path):
    _, requests = _good_rows()
    del requests[2]["output"]
    path = tmp_path / "requests.jsonl"
    write_jsonl(path, requests)
    with pytest.raises(records.RecordError, match="line 3: field 'output': missing"):
        records.read_requests(path)


def test_an_empty_file_is_no_records_and_a_missing_file_is_an_error(tmp_path):
    (tmp_path / "probes.jsonl").write_text("", encoding="utf-8")
    assert records.read_probes(tmp_path / "probes.jsonl") == []
    with pytest.raises(records.RecordError, match="cannot read"):
        records.read_requests(tmp_path / "requests.jsonl")


def test_reach_lines_are_read_and_checked(tmp_path):
    row = {"seed": 20261016, "id": "q_a_001", "cls": "chain", "overlap": 3, "plant_turns": [4, 9], "paged": True,
           "query": "ledger-core retries", "reachable": True, "rank": 1}
    path = tmp_path / "reach.jsonl"
    write_jsonl(path, [row, {**row, "id": "q_c_01", "reachable": False, "rank": None, "plant_turns": []}])
    assert [r["rank"] for r in records.read_reach(path)] == [1, None]
    for change, field in (({"plant_turns": [4, "9"]}, "plant_turns[1]"), ({"rank": 0}, "rank"),
                          ({"paged": "yes"}, "paged")):
        write_jsonl(path, [row, {**row, **change}])
        with pytest.raises(records.RecordError) as exc:
            records.read_reach(path)
        assert (exc.value.line, exc.value.field) == (2, field)


# A whole directory


def test_read_run_refuses_rows_of_a_unit_the_manifest_does_not_list(tmp_path):
    s = small_script(1)
    out = write_run(tmp_path, [unit("contexto", 1)], probe_rows(s, "full", 0, set()), [])
    with pytest.raises(records.RecordError) as exc:
        records.read_run(out)
    assert exc.value.line == 1 and "full, seed 1, repeat 0" in str(exc.value)
    assert "not in manifest.json" in str(exc.value)


def test_read_run_refuses_a_probe_answered_twice_in_one_unit(tmp_path):
    rows = probe_rows(small_script(1), "contexto", 0, set())
    out = write_run(tmp_path, [unit("contexto", 1)], [*rows, rows[4]], [])
    with pytest.raises(records.RecordError) as exc:
        records.read_run(out)
    assert exc.value.line == len(rows) + 1 and f"{rows[4]['id']!r} appears twice" in str(exc.value)


def test_read_run_refuses_probe_rows_of_a_unit_that_is_not_complete(tmp_path):
    rows = probe_rows(small_script(1), "contexto", 0, set())
    out = write_run(tmp_path, [unit("contexto", 1, status="stopped", reason="the CLI exited")], rows, [])
    with pytest.raises(records.RecordError, match="is 'stopped' in manifest.json"):
        records.read_run(out)


def test_read_run_refuses_a_unit_listed_twice(tmp_path):
    out = write_run(tmp_path, [unit("contexto", 1), unit("contexto", 1)], [], [])
    with pytest.raises(records.RecordError) as exc:
        records.read_run(out)
    assert exc.value.field == "units[1]" and "listed twice" in str(exc.value)


def test_read_run_says_when_the_directory_is_a_replay(tmp_path):
    out = write_run(tmp_path, [], [], [], kind="replay", model=None)
    with pytest.raises(records.RecordError, match="replay"):
        records.read_run(out)


def test_read_run_refuses_a_probe_record_of_another_model_and_accepts_such_a_request(tmp_path):
    s = small_script(1)
    rows = probe_rows(s, "contexto", 0, set())
    side = {**request_rows(s, "contexto", 0)[0][0], "model": "claude-haiku-5-5-small", "main": False}
    out = write_run(tmp_path, [unit("contexto", 1)], rows, [side])
    assert records.read_run(out).requests[0]["model"] == "claude-haiku-5-5-small"
    rows[3] = {**rows[3], "model": "claude-opus-5-5"}
    out = write_run(tmp_path, [unit("contexto", 1)], rows, [side])
    with pytest.raises(records.RecordError) as exc:
        records.read_run(out)
    assert (exc.value.line, exc.value.field) == (4, "model") and "'claude-opus-5-5'" in str(exc.value)


# Strict reading: what JSON allows and a careless writer gets wrong


def _write_lines(path: Path, rows: list[dict], **dumps) -> None:
    path.write_text("".join(json.dumps(r, **dumps) + "\n" for r in rows), encoding="utf-8")


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085", "\x0b", "\x0c", "\x1c"])
def test_a_line_separator_character_inside_a_string_is_data_not_a_line_end(tmp_path, separator):
    probes, _ = _good_rows()
    probes[4] = {**probes[4], "given": f"first{separator}second"}
    path = tmp_path / "probes.jsonl"
    # A raw control character below U+0020 is not JSON, so those three are written escaped; the
    # others are written raw, as ensure_ascii=False does with a model's own text.
    _write_lines(path, probes, ensure_ascii=ord(separator) < 0x20)
    assert records.read_probes(path)[4]["given"] == f"first{separator}second"


def test_crlf_line_ends_and_a_missing_final_newline_are_accepted(tmp_path):
    probes, _ = _good_rows()
    path = tmp_path / "probes.jsonl"
    path.write_bytes("\r\n".join(json.dumps(r) for r in probes).encode("utf-8"))
    assert records.read_probes(path) == probes


def test_a_repeated_key_is_an_error_that_names_the_key(tmp_path):
    probes, requests = _good_rows()
    path = tmp_path / "probes.jsonl"
    line = json.dumps(probes[1])[:-1] + ', "correct": true}'
    path.write_text(json.dumps(probes[0]) + "\n" + line + "\n", encoding="utf-8")
    with pytest.raises(records.RecordError, match="line 2: field 'correct': the key appears twice") as exc:
        records.read_probes(path)
    assert (exc.value.line, exc.value.field) == (2, "correct")
    path = tmp_path / "requests.jsonl"
    path.write_text(json.dumps(requests[0])[:-1] + ', "uncached": 5}\n', encoding="utf-8")
    with pytest.raises(records.RecordError, match="line 1: field 'uncached': the key appears twice"):
        records.read_requests(path)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest([unit("contexto", 1)]))[:-1] + ', "bound": 9}', encoding="utf-8")
    with pytest.raises(records.RecordError, match="manifest.json: field 'bound': the key appears twice"):
        records.read_manifest(path)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e400"])
def test_a_number_that_is_not_finite_is_refused_wherever_it_is(tmp_path, constant):
    path = tmp_path / "manifest.json"
    text = json.dumps(manifest([unit("contexto", 1)]))
    path.write_text(text[:-1] + f', "extra": {{"deeper": [1, {constant}]}}}}', encoding="utf-8")
    with pytest.raises(records.RecordError, match=r"field 'extra.deeper\[1\]': not a finite number") as exc:
        records.read_manifest(path)
    assert exc.value.field == "extra.deeper[1]"
    _, requests = _good_rows()
    path = tmp_path / "requests.jsonl"
    path.write_text(json.dumps(requests[0])[:-1] + f', "note": {constant}}}\n', encoding="utf-8")
    with pytest.raises(records.RecordError, match="line 1: field 'note': not a finite number"):
        records.read_requests(path)
    # A known field still gets its own message.
    path.write_text(json.dumps(requests[0]).replace('"output": 12', f'"output": {constant}') + "\n",
                    encoding="utf-8")
    with pytest.raises(records.RecordError, match="line 1: field 'output': expected a whole number"):
        records.read_requests(path)


def test_a_token_count_too_large_to_be_one_is_an_error_not_an_overflow(tmp_path):
    _, requests = _good_rows()
    path = tmp_path / "requests.jsonl"
    for field in ("uncached", "history_tokens"):
        huge = "1" + "0" * 400
        path.write_text(json.dumps(requests[0]).replace(f'"{field}": {requests[0][field]}', f'"{field}": {huge}')
                        + "\n", encoding="utf-8")
        with pytest.raises(records.RecordError, match=f"line 1: field '{field}': expected 1,000,000,000,000 or "
                                                      "less") as exc:
            records.read_requests(path)
        assert len(str(exc.value)) < 400  # the number is not printed in full
    path.write_text(json.dumps({**requests[0], "uncached": records.LARGEST}) + "\n", encoding="utf-8")
    assert records.read_requests(path)[0]["uncached"] == 10 ** 12


@pytest.mark.parametrize("opening", ["[", '{"a": '])
def test_nesting_too_deep_to_read_is_an_error_not_a_recursion_error(tmp_path, opening):
    path = tmp_path / "requests.jsonl"
    path.write_text(opening * 200_000 + "\n", encoding="utf-8")
    with pytest.raises(records.RecordError, match="line 1: ") as exc:
        records.read_requests(path)
    assert "not JSON" in str(exc.value) or "nested too deeply" in str(exc.value)
    path = tmp_path / "manifest.json"
    path.write_text(opening * 200_000, encoding="utf-8")
    with pytest.raises(records.RecordError):
        records.read_manifest(path)


def test_a_lone_surrogate_is_refused_because_it_cannot_be_written_as_utf8(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest([unit("contexto", 1)], model="m\ud800")), encoding="utf-8")
    with pytest.raises(records.RecordError, match="field 'model': .*not valid Unicode"):
        records.read_manifest(path)


def test_a_missing_file_is_said_to_be_missing_and_is_not_called_bad_json(tmp_path):
    for read in (records.read_manifest, records.read_probes, records.read_requests):
        with pytest.raises(records.RecordError) as exc:
            read(tmp_path / "absent.json")
        assert str(exc.value) == f"{tmp_path / 'absent.json'}: cannot read the file: it is missing"
    (tmp_path / "manifest.json").mkdir()
    with pytest.raises(records.RecordError) as exc:
        records.read_manifest(tmp_path / "manifest.json")
    assert "cannot read the file" in str(exc.value) and "not JSON" not in str(exc.value)


# The manifest's optional fields


def test_a_label_is_optional_short_and_one_line(tmp_path):
    path = tmp_path / "manifest.json"
    for label in ("Stub run: no model answered", None):
        path.write_text(json.dumps(manifest([unit("contexto", 1)], label=label)), encoding="utf-8")
        assert records.read_manifest(path)["label"] == label
    path.write_text(json.dumps(manifest([unit("contexto", 1)])), encoding="utf-8")
    assert "label" not in records.read_manifest(path)
    for label, problem in ((5, "expected a string or null"), ("x" * 201, "expected 200 characters or fewer"),
                           ("two\nlines", "expected one line"), ("bell\x07", "expected one line")):
        path.write_text(json.dumps(manifest([unit("contexto", 1)], label=label)), encoding="utf-8")
        with pytest.raises(records.RecordError, match=f"field 'label': {problem}"):
            records.read_manifest(path)


def test_the_intended_compaction_trigger_is_one_token_or_more(tmp_path):
    data = manifest([unit("plain", 1)])
    data["plain_compaction"]["intended_trigger"] = 0
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(records.RecordError, match=r"field 'plain_compaction.intended_trigger': expected 1 or more"):
        records.read_manifest(path)


def test_a_unit_may_carry_its_record_counts_and_its_cache_lifetime(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest([unit("contexto", 1, probes=72, requests=40, cache_ttl="1h")])),
                    encoding="utf-8")
    assert records.read_manifest(path)["units"][0]["requests"] == 40
    for field, value, problem in (("probes", -1, "expected 0 or more"), ("requests", "40", "expected a whole number"),
                                  ("cache_ttl", "2h", "expected one of '5m', '1h'")):
        path.write_text(json.dumps(manifest([unit("contexto", 1, **{field: value})])), encoding="utf-8")
        with pytest.raises(records.RecordError, match=rf"field 'units\[0\].{field}': {problem}"):
            records.read_manifest(path)


@pytest.mark.parametrize("value", ["x", "2h", "", 5])
def test_the_manifests_cache_lifetime_is_5m_1h_or_null(tmp_path, value):
    path = tmp_path / "manifest.json"
    for good in ("5m", "1h", None):
        path.write_text(json.dumps(manifest([unit("contexto", 1)], cache_ttl=good)), encoding="utf-8")
        assert records.read_manifest(path)["cache_ttl"] == good
    path.write_text(json.dumps(manifest([unit("contexto", 1)], cache_ttl=value)), encoding="utf-8")
    with pytest.raises(records.RecordError, match=r"manifest.json: field 'cache_ttl': expected one of '5m', '1h'"):
        records.read_manifest(path)


def test_an_integer_too_large_for_any_count_is_an_error_even_in_a_field_no_reader_checks(tmp_path):
    big = 10 ** 18 + 1
    with pytest.raises(ValueError, match=r"field 'extra': a whole number larger than 10\^18"):
        records.strict(json.dumps({"extra": big}))
    assert records.strict(json.dumps({"extra": 10 ** 18, "negative": -(10 ** 18)})) == {
        "extra": 10 ** 18, "negative": -(10 ** 18)}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest([unit("contexto", 1)], extra={"deep": [1, big]})), encoding="utf-8")
    with pytest.raises(records.RecordError, match=r"field 'extra.deep\[1\]': a whole number larger than 10\^18"):
        records.read_manifest(path)
    results = tmp_path / "results.json"
    results.write_text(json.dumps({"format": 1, "kind": "replay", "bound": 100, "request_series": {},
                                   "manifest": {"bound": -big}}), encoding="utf-8")
    with pytest.raises(records.RecordError, match=r"field 'manifest.bound': a whole number larger than 10\^18"):
        records.read_results(results)


# read_run: records that were cut, dropped or written twice


def _one_unit(tmp_path, change=lambda probes, requests: None, **unit_fields) -> Path:
    s = small_script(1)
    probes = probe_rows(s, "contexto", 0, set())
    requests = request_rows(s, "contexto", 0)[0]
    change(probes, requests)
    return write_run(tmp_path / "run", [unit("contexto", 1, **unit_fields)], probes, requests)


def test_read_run_refuses_a_request_recorded_twice(tmp_path):
    out = _one_unit(tmp_path, lambda probes, requests: requests.extend([dict(r) for r in requests[:3]]))
    with pytest.raises(records.RecordError, match="requests.jsonl: line .*: field 'n': the request 0 appears twice "
                                                  "for the unit contexto, seed 1, repeat 0") as exc:
        records.read_run(out)
    assert exc.value.line == len(small_script(1).turns) + 1


def test_a_gap_in_n_is_not_an_error_and_a_lost_record_shows_in_the_count(tmp_path):
    # The live meter takes n for a request before it is forwarded and writes no record for one that
    # got no response, so n can skip a number with nothing lost. The count is what shows a loss.
    turns = len(small_script(1).turns)
    out = _one_unit(tmp_path, lambda probes, requests: requests.pop(5))
    run = records.read_run(out)
    assert len(run.requests) == turns - 1
    assert run.unchecked == ((("contexto", 1, 0), (0,)),)  # no count: a cut file cannot be detected here
    out = _one_unit(tmp_path, lambda probes, requests: requests.pop(5), requests=turns - 1)
    assert records.read_run(out).unchecked == ()
    out = _one_unit(tmp_path, lambda probes, requests: requests.pop(5), requests=turns)
    with pytest.raises(records.RecordError, match=f"requests.jsonl: the unit contexto, seed 1, repeat 0 has "
                                                  f"{turns - 1} request records and manifest.json says {turns}"):
        records.read_run(out)
    out = _one_unit(tmp_path, lambda probes, requests: requests.reverse())  # the order in the file is free
    assert len(records.read_run(out).requests) == turns


def test_the_sessions_whose_records_have_no_count_are_named_attempt_by_attempt(tmp_path):
    s = small_script(1)
    rows = request_rows(s, "contexto", 0)[0]
    earlier = [{**r, "attempt": 0} for r in rows[:2]]
    final = [{**r, "attempt": 1} for r in rows]
    counted = unit("contexto", 1, attempt=1, requests=len(final),
                   attempts=[{"attempt": 0, "status": "stopped"}, {"attempt": 1, "status": "complete"}])
    stopped = unit("full", 1, status="stopped", reason="budget")
    not_run = unit("plain", 1, status="not_run")
    out = write_run(tmp_path / "run", [counted, stopped, not_run], probe_rows(s, "contexto", 0, set()),
                    earlier + final)
    # Attempt 1 is counted by the unit's requests; attempt 0 by nothing. A stopped unit with no count is
    # named too, since a cut could have removed all its records; a unit not run has none to lose.
    assert records.read_run(out).unchecked == ((("contexto", 1, 0), (0,)), (("full", 1, 0), (0,)))


@pytest.mark.parametrize("keep", [0, 1])
def test_read_run_refuses_a_complete_unit_whose_requests_are_gone(tmp_path, keep):
    s = small_script(1)
    units = [unit("contexto", 1), unit("full", 1)]
    probes = probe_rows(s, "contexto", 0, set()) + probe_rows(s, "full", 0, set())
    requests = (request_rows(s, "contexto", 0)[0] + request_rows(s, "full", 0)[0])[:keep]
    out = write_run(tmp_path / "run", units, probes, requests)
    arm = "contexto" if keep == 0 else "full"
    with pytest.raises(records.RecordError, match=f"requests.jsonl: the unit {arm}, seed 1, repeat 0 is complete in "
                                                  "manifest.json but has no request record"):
        records.read_run(out)


def test_read_run_accepts_a_stopped_unit_with_no_requests(tmp_path):
    s = small_script(1)
    units = [unit("contexto", 1), unit("plain", 1, status="stopped", reason="the CLI exited")]
    out = write_run(tmp_path / "run", units, probe_rows(s, "contexto", 0, set()), request_rows(s, "contexto", 0)[0])
    assert len(records.read_run(out).manifest["units"]) == 2


def test_read_run_checks_the_record_counts_a_unit_carries(tmp_path):
    turns = len(small_script(1).turns)
    assert records.read_run(_one_unit(tmp_path, probes=72, requests=turns)).manifest["units"][0]["probes"] == 72
    # The tail of a unit cut at a line boundary: nothing else in the directory shows it.
    out = _one_unit(tmp_path, lambda probes, requests: requests.pop(), probes=72, requests=turns)
    with pytest.raises(records.RecordError, match=f"requests.jsonl: the unit contexto, seed 1, repeat 0 has "
                                                  f"{turns - 1} request records and manifest.json says {turns}"):
        records.read_run(out)
    out = _one_unit(tmp_path, lambda probes, requests: probes.pop(), probes=72, requests=turns)
    with pytest.raises(records.RecordError, match="probes.jsonl: the unit contexto, seed 1, repeat 0 has 71 probe "
                                                  "records and manifest.json says 72"):
        records.read_run(out)
