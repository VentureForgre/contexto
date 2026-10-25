"""Rule 18: one nesting limit for JSON read from outside Contexto (``contexto.canonical``)."""

from __future__ import annotations

import json
import sys
import threading

import pytest

import contexto.canonical as canonical
from contexto.canonical import MAX_JSON_DEPTH, JSONTooDeepError, canonical_bytes, loads_bounded, nesting_exceeds
from contexto.store import redact
from contexto.store.recall import _json_keys
from contexto.store.render import render_page


def dicts(depth: int) -> str:
    return '{"a":' * depth + "1" + "}" * depth


def lists(depth: int) -> str:
    return "[" * depth + "]" * depth


def built(depth: int, kind: type) -> object:
    """A value nested ``depth`` levels, built without recursion (deeper than json.dumps can write)."""
    value: object = 1
    for _ in range(depth):
        value = {"a": value} if kind is dict else [value]
    return value


@pytest.mark.parametrize("make", [dicts, lists])
def test_the_limit_counts_levels_from_one(make):
    assert loads_bounded(make(MAX_JSON_DEPTH)) is not None
    with pytest.raises(JSONTooDeepError):
        loads_bounded(make(MAX_JSON_DEPTH + 1))
    assert not nesting_exceeds(json.loads(make(MAX_JSON_DEPTH)))
    assert nesting_exceeds(json.loads(make(MAX_JSON_DEPTH + 1)))
    assert not nesting_exceeds(1) and not nesting_exceeds("x") and not nesting_exceeds({}) and not nesting_exceeds([])


def test_the_deepest_branch_counts_wherever_it_is():
    value = {"wide": [[1] * 50] * 50, "deep": [1, 2, {"x": json.loads(lists(MAX_JSON_DEPTH - 2))}]}
    assert nesting_exceeds(value) and not nesting_exceeds(value, MAX_JSON_DEPTH + 1)


def test_too_deep_is_a_value_error_whether_or_not_json_loads_parses_it(monkeypatch):
    with pytest.raises(ValueError):
        loads_bounded(lists(1_000_000))  # beyond json.loads on any real stack: its RecursionError is converted
    def no_room(data):
        raise RecursionError("synthetic")
    monkeypatch.setattr(canonical, "json", type("J", (), {"loads": staticmethod(no_room)}))
    with pytest.raises(JSONTooDeepError):
        loads_bounded("[]")  # forced, since from Python 3.14 how deep json.loads gets depends on the stack


def recurse(depth: int, then):
    return then() if depth == 0 else recurse(depth - 1, then)


@pytest.mark.parametrize("make", [dicts, lists])
def test_every_value_within_the_limit_can_be_written_and_walked_on_this_python(make):
    """The limit sits well below this interpreter's json.dumps limit (about 61,500 objects on 3.14, about
    10,000 on 3.12) and its recursion limit, so a request read within it can be serialized, hashed,
    and walked by the recursive walkers (``redact``, the index's ``_json_keys``): checked here in a
    handler-like thread, under 200 frames of Python stack."""
    assert MAX_JSON_DEPTH + 300 < sys.getrecursionlimit()
    results: list[object] = []

    def check():
        value = loads_bounded(make(MAX_JSON_DEPTH))
        wrapped = {"messages": [{"content": [{"input": value}]}]}  # what the store and the engine add
        keys = _json_keys(value)
        results.append((len(canonical_bytes(wrapped)), redact(wrapped) == wrapped,
                        keys == (["a"] * MAX_JSON_DEPTH if make is dicts else []),
                        json.loads(json.dumps(wrapped)) == wrapped))

    for target in (lambda: recurse(200, check), check):
        thread = threading.Thread(target=target)
        thread.start()
        thread.join()
    assert len(results) == 2 and all(n > 0 and redacted and listed and same for n, redacted, listed, same in results)


@pytest.mark.parametrize("kind", [dict, list])
def test_a_tool_input_json_dumps_cannot_write_renders_as_a_placeholder(kind):
    deep = built(200_000, kind)  # past json.dumps on every Python
    page = {"turn": 1, "messages": [{"role": "assistant", "content": [
        {"type": "tool_use", "name": "exec", "input": deep}, {"type": "text", "text": "after"}]}]}
    text = render_page(page)
    assert f"tool_use exec <{kind.__name__}>" in text and "after" in text
    raw = {"turn": 1, "messages": [{"role": "assistant", "content": [{"type": "tool_use", "name": "exec",
                                                                      "input": lists(9)}]}]}
    assert f"tool_use exec {json.dumps(lists(9))}" in render_page(raw)  # a string input is written as JSON
