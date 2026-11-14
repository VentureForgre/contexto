"""``contexto bench --replay``: the parts of a replay, each on its own (D66).

The query rule and the reach measurement, the scripted model, the fake upstream, the synthetic
client, the modelled lines and the trace. The replay itself is in ``test_bench_replay_run.py``.
"""

from __future__ import annotations

import copy
import http.client
import inspect
import json
import math
import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest

from contexto import mcp, rewrite, toolcheck
from contexto.bench import script as bench_script
from contexto.bench.model import NEW_CLASSES, words
from contexto.bench.replay import client, lines, reach, scripted, session, trace, upstream

SMALL = dict(seed=3, protocol="v2", bound=4_000, turn_bytes=2_000)


@pytest.fixture(scope="module")
def small():
    return bench_script.build(**SMALL)


@pytest.fixture(scope="module")
def pinned():
    p = trace.PINNED
    return bench_script.build(p["seed"], p["protocol"], p["bound"], p["turn_bytes"], p["turns"])


@pytest.fixture(scope="module")
def spike():
    spikes = Path(__file__).resolve().parents[1] / "spikes"
    for path in (spikes / "m2", spikes / "m0"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    from acceptance import detectors, stubrun
    from acceptance import reach as spike_reach

    return {"reach": spike_reach, "detectors": detectors, "stubrun": stubrun}


def _probe(script, cls, **where):
    return next(p for p in script.probes if p.cls == cls and all(getattr(p, k) == v for k, v in where.items()))


# The query rule


def test_d27_key_terms_are_the_acceptance_spikes(small, spike):
    for probe in small.probes:
        if probe.cls not in NEW_CLASSES:
            assert reach.key_terms(probe) == spike["reach"].key_terms(probe), probe.id
            if probe.type != "multihop":  # the spike needs every hop's statement too
                assert [s for _, s in reach.needs(probe)] == spike["reach"].needed_statements(
                    probe, reach.key_terms(probe)), probe.id


def test_a_single_key_probe_searches_its_key_for_the_statement_with_the_answer(small):
    for cls in ("paged_only", "distractor"):
        probe = _probe(small, cls)
        assert reach.needs(probe) == [(probe.subjects[0], probe.statements[0])]
        assert probe.subjects[0] in probe.question and probe.answer in probe.statements[0]
    current = _probe(small, "chain", tier="depth6-current")
    assert reach.needs(current) == [(current.subjects[0], current.statements[-1])]
    previous = _probe(small, "chain", tier="depth6-previous")
    assert reach.needs(previous) == [(previous.subjects[0], previous.statements[-2])]
    single = _probe(small, "obligations", tier="single")
    assert reach.needs(single) == [(single.subjects[0], single.statements[-1])]  # its state is its last statement


def test_a_multihop_probe_searches_each_hops_name_for_that_hops_statement(small):
    for probe in (p for p in small.probes if p.cls == "multihop"):
        pairs = reach.needs(probe)
        assert [q for q, _ in pairs] == list(probe.subjects) and [s for _, s in pairs] == list(probe.statements)
        assert all(query in statement for query, statement in pairs)
        assert pairs[0][0] in probe.question
        # Only the first hop is in the question: the later names come from the statements found.
        assert all(query not in probe.question for query, _ in pairs[1:])
        for (_, statement), (later, _) in zip(pairs, pairs[1:]):
            assert later in statement
        assert reach.first_query(probe) == probe.subjects[0]


def test_an_obligation_set_searches_each_id_for_its_last_statement(small):
    probe = _probe(small, "obligations", kind="set")
    pairs = reach.needs(probe)
    assert len(pairs) == 6 and all(query.startswith("AI-") and query in probe.question for query, _ in pairs)
    for query, statement in pairs:
        assert statement == [s for s in probe.statements if query in s][-1]


def test_the_new_classes_search_the_questions_content_words(small):
    for probe in (p for p in small.probes if p.cls in NEW_CLASSES):
        (query, statement), = reach.needs(probe)
        assert query.split() == [w for w in reach.content_words(probe.question)
                                 if w not in reach.instruction_words(probe)]
        assert set(query.split()) == words(probe.question) - reach.instruction_words(probe)
        assert not any(subject in query for subject in probe.subjects)  # the rule has no access to the key
        assert statement == (probe.statements[-1] if probe.cls == "concluded" else probe.statements[0])
        assert probe.answer in statement
    concluded = _probe(small, "concluded")
    assert reach.needs(concluded)[0][1].startswith("DECISION ")


def test_content_words_are_in_order_once_each():
    question = "Which build label is the billing core service running? The billing one."
    in_order = [w for w in ("which", "build", "label", "billing", "core", "service", "running", "one")
                if w in words(question)]
    assert reach.content_words(question) == in_order and in_order.count("billing") == 1 and "billing" in in_order
    assert reach.content_words("What is it?") == []


def test_the_search_tools_detailed_places_are_the_mcp_servers():
    assert reach.SHOWN_IN_DETAIL == mcp.SEARCH_LIMIT


# The reach measurement, against a store that is a dict


def _measure(probes, pages, search, view="[]"):
    return reach.measure(probes, pages, search, view, lambda probe: [1], seed=3)


def _find(pages):
    """A search like the store's: the pages holding every word, newest (highest id) first."""
    def search(query):
        wanted = query.casefold().split()
        return sorted((pid for pid, text in pages.items() if all(w in text.casefold() for w in wanted)), reverse=True)
    return search


def _page(*texts):
    return reach.page_text({"messages": [{"role": "user", "content": [{"type": "text", "text": t}]} for t in texts]})


def test_a_fact_on_a_page_the_search_returns_is_reachable_with_its_rank(small):
    probe = _probe(small, "paged_only")
    pages = {"pg_1": _page("filler\n" + probe.statements[0] + "\nfiller"),
             "pg_2": _page(f"another line that names {probe.subjects[0]}"), "pg_3": _page("nothing")}
    (row,) = _measure([probe], pages, _find(pages))
    assert row == {"seed": 3, "id": probe.id, "cls": "paged_only", "overlap": probe.overlap, "plant_turns": [1],
                   "paged": True, "query": probe.subjects[0], "reachable": True, "rank": 2,
                   "queries": [probe.subjects[0]], "needed": 1, "in_view": False, "covered": True, "returned": 2,
                   "best_rank": 2, "best_query": probe.subjects[0]}


def test_a_search_that_cannot_find_the_plant_is_unreachable(small):
    probe = _probe(small, "paged_only")
    pages = {"pg_1": _page(probe.statements[0])}
    (row,) = _measure([probe], pages, lambda query: [])
    assert (row["paged"], row["reachable"], row["rank"], row["covered"]) == (True, False, None, False)
    # Found, but only on a page that does not hold the statement: still not reachable.
    pages = {"pg_1": _page(probe.statements[0]), "pg_2": _page(probe.subjects[0])}
    (row,) = _measure([probe], pages, lambda query: ["pg_2"])
    assert (row["reachable"], row["rank"]) == (False, None)
    totals = reach.totals([row])
    assert (totals["probes"], totals["paged"], totals["reachable"], totals["covered"], totals["neither"]) == (
        1, 1, 0, 0, 1)


def test_a_statement_still_in_view_is_covered_but_not_paged_or_reachable(small):
    probe = _probe(small, "chain", tier="depth6-current")
    view = json.dumps([{"role": "user", "content": "log\n" + "\n".join(probe.statements) + "\nlog"}])
    (row,) = _measure([probe], {}, lambda query: [], view)
    assert (row["paged"], row["reachable"], row["rank"], row["in_view"], row["covered"]) == (
        False, False, None, True, True)


def test_hops_are_followed_only_as_far_as_the_statements_are_found(small):
    probe = next(p for p in small.probes if p.cls == "multihop" and len(p.statements) == 4)
    pages = {f"pg_{i}": _page(statement) for i, statement in enumerate(probe.statements)}
    (row,) = _measure([probe], pages, _find(pages))
    assert row["reachable"] and row["rank"] == 1 and row["queries"] == list(probe.subjects) and row["needed"] == 4
    # Hop 2's statement is nowhere: hops 3 and 4 are never searched, because their names are in it.
    broken = {pid: text for pid, text in pages.items() if pid != "pg_1"}
    asked = []

    def search(query):
        asked.append(query)
        return _find(broken)(query)

    (row,) = _measure([probe], broken, search)
    assert not row["reachable"] and not row["covered"] and row["rank"] is None
    assert asked == list(probe.subjects[:2]) and row["queries"] == list(probe.subjects[:2])
    # The same statement in view instead of on a page: the hops go on, and the probe is covered.
    view = json.dumps([{"role": "user", "content": probe.statements[1]}])
    (row,) = _measure([probe], broken, _find(broken), view)
    assert not row["reachable"] and row["covered"] and row["queries"] == list(probe.subjects)


def test_the_rank_of_a_probe_with_several_statements_is_its_worst(small):
    probe = _probe(small, "obligations", kind="set")
    pairs = reach.needs(probe)
    pages = {f"pg_{i}": _page(statement) for i, (_, statement) in enumerate(pairs)}
    pages["pg_9"] = _page(f"a newer page that also names {pairs[0][0]}")
    (row,) = _measure([probe], pages, _find(pages))
    assert row["reachable"] and row["rank"] == 2 and row["query"] == pairs[0][0] and len(row["queries"]) == 6


def test_a_new_class_question_finds_nothing_unless_a_page_holds_every_word(small):
    probe = _probe(small, "paraphrase")
    pages = {"pg_1": _page("log line\n" + probe.statements[0] + "\nlog line"), "pg_2": _page("log line")}
    (row,) = _measure([probe], pages, _find(pages))
    assert (row["paged"], row["reachable"], row["rank"], row["returned"]) == (True, False, None, 0)
    # The best query from the question's words does find it: the service's two words, as a pair.
    service = probe.subjects[1]
    assert row["best_rank"] == 1 and row["best_query"] in reach.question_queries(probe)
    assert reach.totals([row])["best_query"] == 1 and reach.totals([row])["neither_by_any_query"] == 0
    assert reach.totals([row])["rule_returned_nothing"] == 1
    assert service in reach.question_queries(probe) and service.replace("-", " ") in reach.question_queries(probe)
    concluded = _probe(small, "concluded")
    pages = {"pg_1": _page(*concluded.statements)}
    (row,) = _measure([concluded], pages, _find(pages))
    assert (row["paged"], row["reachable"], row["best_rank"], row["best_query"]) == (True, False, None, None)
    assert reach.totals([row])["best_query"] == 0 and reach.totals([row])["neither_by_any_query"] == 1


def test_the_best_query_takes_the_best_rank_over_every_query_it_tries(small):
    probe = _probe(small, "toolloop")
    a, b = probe.subjects[1].split("-", 1)
    hyphenated = f"{a}-{b}"
    pages = {"pg_1": _page(probe.statements[0])}
    pages.update({f"pg_{k}": _page(f"{a} and {b} on a later page") for k in range(2, 6)})

    def search(query):
        if query == hyphenated:  # only the hyphenated pair is exact enough to put the plant first
            return ["pg_1"]
        return _find(pages)(query)

    (row,) = _measure([probe], pages, search)
    assert (row["best_rank"], row["best_query"]) == (1, hyphenated)
    assert reach.question_queries(probe).index(hyphenated) > reach.question_queries(probe).index(a)


def test_instruction_words_come_from_the_generators_tables(small, pinned):
    """The fixed phrases around a new-class question are found from ATTRIBUTES and SCENARIOS, not from a
    list of words: what is not the table's text is the wrapper."""
    import re

    def tokens(text):
        return set(re.findall(r"[a-z0-9]+", text.casefold()))

    for script in (small, pinned):
        by_class: dict[str, set[frozenset[str]]] = {}
        for probe in (p for p in script.probes if p.cls in NEW_CLASSES):
            dropped = reach.instruction_words(probe)
            own = reach._table_part(probe.question)
            assert own is not None, probe.question  # every new question holds the text of its table row
            assert {"answer", "only"} <= dropped <= tokens(probe.question) and not dropped & tokens(own)
            if probe.cls != "concluded":
                assert "earlier" in dropped
            queries = reach.question_queries(probe)
            assert not any(set(re.split(r"[ -]", q)) & dropped for q in queries), probe.id
            by_class.setdefault(probe.cls, set()).add(dropped)
        # The wrapper is fixed per class: what one question drops is the class's wrapper less the tokens
        # its own table text shares with it, so every set is part of the largest.
        for found in by_class.values():
            largest = max(found, key=len)
            assert all(dropped <= largest for dropped in found)
        assert set(by_class) == set(NEW_CLASSES)
    assert reach.instruction_words(_probe(small, "chain")) == frozenset()


def test_question_queries_are_singles_then_pairs_with_a_space_and_a_hyphen():
    probe = bench_script.Probe("q", "b", "paraphrase", "t", "From the logs earlier in this conversation: which "
                               "update track is the relay mesh service subscribed to? Answer with the value only.",
                               "x", "value", (), ("relay-mesh.release_channel", "relay-mesh", "release_channel"),
                               ("cfg relay-mesh.release_channel=x",), cls="paraphrase", overlap=0)
    queries = reach.question_queries(probe)
    assert queries[:6] == ["update", "track", "relay", "mesh", "service", "subscribed"]
    assert "relay mesh" in queries and "relay-mesh" in queries and "mesh service" in queries
    assert not any(w in q.split() or w in q.split("-") for q in queries
                   for w in ("answer", "value", "only", "earlier", "conversation", "logs"))
    assert reach.first_query(probe) == "update track relay mesh service subscribed"


def test_totals_count_by_class_in_script_order(small):
    pages = {f"pg_{i:03d}": _page(text) for i, text in enumerate(small.content_turns)}
    rows = _measure(small.probes, pages, _find(pages))
    totals = reach.totals(rows)
    assert list(totals["by_class"]) == ["paged_only", "multihop", "chain", "obligations", "distractor", "paraphrase",
                                        "concluded", "toolloop"]
    assert totals["probes"] == 72 == sum(c["probes"] for c in totals["by_class"].values())
    for key in ("paged", "reachable", "shown", "covered", "neither", "best_query", "neither_by_any_query",
                "rule_returned_nothing"):
        assert totals[key] == sum(c[key] for c in totals["by_class"].values()), key
        assert isinstance(totals[key], int)
    assert totals["by_class"]["paged_only"]["reachable"] == 4
    assert totals["covered"] + totals["neither"] == 72


# The scripted model


def test_the_scripted_model_has_one_round_per_reply(small):
    planned = scripted.steps(small)
    rounds = scripted.rounds(small)
    assert len(planned) == sum(rounds) and len(rounds) == len(small.turns)
    for turn, kind in enumerate(small.kinds):
        mine = [s for s in planned if s.turn == turn]
        assert [s.round for s in mine] == list(range(len(mine)))
        assert mine[-1].kind == "text" and all(s.kind == "tool_use" for s in mine[:-1])
        if kind == "log":
            assert [s.text for s in mine] == ["OK"]
        elif kind == "decision":
            assert len(mine) == 1 and bench_script.decision_reply_ok(small, turn, mine[0].text)
        elif kind == "toolloop":
            assert [s.argument for s in mine[:-1]] == list(small.reads) and len(mine) == len(small.reads) + 1
            assert all(s.tool == scripted.READ_TOOL and s.tool_input == {"file_path": s.argument} for s in mine[:-1])
            assert mine[-1].text == "OK"
        else:
            ids = small.probe_turn_ids[turn - small.n_content]
            by_id = {p.id: p for p in small.probes}
            assert [s.argument for s in mine[:-1]] == [reach.first_query(by_id[i]) for i in ids]
            assert all(s.tool == scripted.SEARCH_TOOL and s.tool_input == {"query": s.argument} for s in mine[:-1])
            assert mine[-1].text.splitlines() == [f"{i}: {by_id[i].answer}" for i in ids]
    assert len({s.tool_id for s in planned if s.kind == "tool_use"}) == sum(s.kind == "tool_use" for s in planned)
    assert set(scripted.RULES) == set(small.kinds)


def test_the_scripted_answers_are_the_generators_and_the_digest_pins_them(small):
    planned = scripted.steps(small)
    answers = "\n".join(s.text for s in planned if small.kinds[s.turn] == "probe" and s.kind == "text")
    assert bench_script.score(small.probes, answers)["correct"] == 72  # scripted, so it says nothing of a model
    digest = scripted.digest(planned)
    assert digest == scripted.digest(scripted.steps(bench_script.build(**SMALL))) and len(digest) == 64
    changed = [*planned[:-1], scripted.Step(planned[-1].turn, planned[-1].round, "text", text="OK")]
    assert scripted.digest(changed) != digest


# The fake upstream


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "::1", "127.0.0.2", "192.168.1.5", "api.anthropic.com", ""])
def test_the_fake_upstream_refuses_any_address_but_loopback(small, host):
    with pytest.raises(ValueError, match="binds 127.0.0.1 only"):
        upstream.FakeUpstream(small, scripted.steps(small), host=host)


def test_the_fake_upstream_cannot_forward():
    """It has no way to open a connection: no HTTP client, no socket and no URL library in the module."""
    source = inspect.getsource(upstream)
    for name in ("http.client", "urllib", "socket", "ssl", "requests", "subprocess"):
        assert f"import {name}" not in source and f"from {name}" not in source, name
    assert not any(name in vars(upstream) for name in ("socket", "urllib", "ssl", "subprocess"))


def _valid_body():
    """A request with a tool loop in it, in the shape the synthetic client sends: valid by every rule."""
    messages = [
        client.user_message("Next part of the working log.", first=True),
        {"role": "assistant", "content": [{"type": "text", "text": "Reading it."},
                                          {"type": "tool_use", "id": "toolu_a", "name": "read_file",
                                           "input": {"file_path": "x"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_a", "content": "file text"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "OK"}]},
        {"role": "user", "content": [{"type": "text", "text": "Next part."}]},
    ]
    return client.request_body(client.head("s"), messages)


def _broken(change):
    body = copy.deepcopy(_valid_body())
    change(body)
    return body


def _drop_tool_use(body):
    body["messages"][1]["content"] = body["messages"][1]["content"][:1]


def _drop_assistant(body):
    del body["messages"][1]


@pytest.mark.parametrize("change, said", [
    (lambda b: b.pop("model"), "model: Field required"),
    (lambda b: b.update(max_tokens=0), "max_tokens: Field required"),
    (lambda b: b.update(max_tokens="9"), "max_tokens: Field required"),
    (lambda b: b.pop("max_tokens"), "max_tokens: Field required"),
    (lambda b: b.update(system=7), "system: Input should be a valid string or a list of text blocks"),
    (lambda b: b["system"].append({"type": "text", "text": ""}), "system.2: text content blocks must be non-empty"),
    (lambda b: b["system"].append({"type": "image"}), "system: every block must be a text block"),
    (lambda b: b.update(tools={}), "tools: Input should be a valid list"),
    (lambda b: b["tools"].append(dict(b["tools"][0])), "tools: Tool names must be unique"),
    (lambda b: b["tools"][0].update(name="read file"), "tools.0.name: String should match pattern"),
    (lambda b: b["tools"][0].pop("input_schema"), "tools.0.input_schema: an input schema must be an object"),
    (lambda b: b.update(tool_choice={"type": "sometimes"}), "tool_choice: the type must be one of auto, any, tool"),
    (lambda b: b.update(tool_choice={"type": "tool", "name": "nope"}), "tool_choice.name: no tool named 'nope'"),
    (lambda b: b.update(tools=[], tool_choice={"type": "any"}), "tool_choice: tool_choice of type any needs tools"),
    (lambda b: b.update(messages=[]), "messages: at least one message is required"),
    (lambda b: b["messages"].pop(0), "messages.0.role: the first message must use the user role"),
    (lambda b: b["messages"][3].update(role="system"), "messages.3.role: Input should be 'user' or 'assistant'"),
    (_drop_assistant, "messages.1.role: roles must alternate between user and assistant, and messages 0 and 1 are "
                      "both user"),
    (lambda b: b["messages"][3].update(content=[]), "messages.3: all messages must have non-empty content"),
    (lambda b: b["messages"][4].update(content=""), "messages.4: all messages must have non-empty content"),
    (lambda b: b["messages"][4].update(content="text"), None),  # a string is valid content
    (lambda b: b["messages"][3]["content"][0].update(text=""), "messages.3.content.0: text content blocks must be "
                                                               "non-empty"),
    (_drop_tool_use, "messages.2.content: unexpected `tool_use_id` found in `tool_result` blocks: toolu_a. Each "
                     "`tool_result` block must have a corresponding `tool_use` block in the previous message."),
    (lambda b: b["messages"][2]["content"][0].update(tool_use_id="toolu_b"),
     "messages.1: `tool_use` ids were found without `tool_result` blocks immediately after: toolu_a."),
    (lambda b: b["messages"][2].update(content=[{"type": "text", "text": "no result"}]),
     "messages.1: `tool_use` ids were found without `tool_result` blocks immediately after: toolu_a."),
    (lambda b: b["messages"][3]["content"].append({"type": "tool_result", "tool_use_id": "toolu_a", "content": "x"}),
     "messages.3: `tool_result` blocks can only be in user messages"),
    (lambda b: b["messages"][4].update(content=[{"type": "tool_use", "id": "t", "name": "x", "input": {}}]),
     "messages.4: `tool_use` blocks can only be in assistant messages"),
    (lambda b: b.pop("tools"), "tools: requests which include `tool_use` or `tool_result` blocks must define tools"),
])
def test_the_fake_upstream_refuses_what_the_messages_api_refuses(change, said):
    assert upstream.check_request(_valid_body()) is None
    found = upstream.check_request(_broken(change))
    assert found is None if said is None else said in found, found


def test_repeated_tool_use_ids_are_refused():
    body = _valid_body()
    body["messages"] += [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_a", "name": "read_file", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_a", "content": "again"}]}]
    assert upstream.check_request(body) == "messages: `tool_use` ids must be unique"


def test_a_request_the_api_refuses_gets_its_400_and_is_recorded(small):
    """The proxy broke the conversation: the reply is the API's 400, and the script does not advance."""
    planned = scripted.steps(small)
    server = upstream.FakeUpstream(small, planned)
    body = _broken(_drop_tool_use)
    status, kind, raw = server.answer("/v1/messages", json.dumps(body).encode())
    assert (status, kind) == (400, "application/json")
    error = json.loads(raw)
    assert error["type"] == "error" and error["error"]["type"] == "invalid_request_error"
    assert "unexpected `tool_use_id` found in `tool_result` blocks: toolu_a" in error["error"]["message"]
    assert server.cursor == 0 and server.records[0]["status"] == 400
    assert server.unexpected == [f"request 1 breaks a rule of the Messages API, which would refuse it with a 400: "
                                 f"{error['error']['message']}"]


def test_model_tokens_prices_log_text_and_the_rest_at_the_stated_rates(spike):
    log = "x line 0001 level=INFO latency_ms=12 shard=3 ok\n" * 200  # 9,600 bytes
    short = "Reply with exactly: OK"
    body = {"model": "m", "system": [{"type": "text", "text": "s" * 900}], "tools": [{"name": "t"}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": short}, {"type": "text", "text": log}]},
                         {"role": "assistant", "content": [{"type": "text", "text": log}]},  # not a user message
                         {"role": "user", "content": log}]}
    counted = upstream.model_tokens(body)
    assert counted["log_bytes"] == 2 * len(log)
    stripped = copy.deepcopy(body)
    stripped["messages"][0]["content"][1]["text"] = ""
    stripped["messages"][2]["content"] = ""
    other = len(json.dumps(stripped, separators=(",", ":")).encode())
    assert counted["other_bytes"] == other
    assert counted["tokens"] == math.ceil(2 * len(log) / 1.8) + math.ceil(other / 3.0)
    assert counted == spike["stubrun"].model_tokens(body)  # the acceptance stub's rule, for what both can see
    assert body["messages"][2]["content"] == log  # the request is not changed


def test_model_tokens_counts_a_tool_results_log_text_at_the_log_rate():
    log = "f01 line 0001 level=WARN latency_ms=400 shard=9 ok\n" * 150
    as_string = {"messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "a", "content": log}]}]}
    as_blocks = {"messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "a", "content": [{"type": "text", "text": log}]}]}]}
    small_result = {"messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "a", "content": log[:4_999]}]}]}
    for body in (as_string, as_blocks):
        counted = upstream.model_tokens(body)
        assert counted["log_bytes"] == len(log) and counted["tokens"] < len(log) / 1.8 + 60
    # A tool result's text is log text at any size (A's nit: a short search reply holds log lines too).
    assert upstream.model_tokens(small_result)["log_bytes"] == 4_999
    short_text = {"messages": [{"role": "user", "content": [{"type": "text", "text": log[:4_999]}]}]}
    assert upstream.model_tokens(short_text)["log_bytes"] == 0  # a short text block is everything else, at 3.0
    assert (upstream.LOG_BYTES_PER_TOKEN, upstream.OTHER_BYTES_PER_TOKEN, upstream.LOG_TEXT_MIN_BYTES) == (
        1.8, 3.0, 5_000)


def test_compaction_markers_are_found_and_an_ordinary_request_has_none():
    def markers(body):
        return upstream.compaction_markers(body, json.dumps(body).encode())

    plain = {"messages": [{"role": "user", "content": [{"type": "text", "text": "Next part of the working log:"}]}]}
    assert markers(plain) == []
    assert upstream.COMPACTION_SUMMARY == rewrite.CLAUDE_CODE_COMPACTION_SUMMARY
    summary = {"messages": [{"role": "user", "content": upstream.COMPACTION_SUMMARY + "\nAnalysis: ..."}]}
    assert markers(summary) == ["summary"] and rewrite.compaction_summary(summary["messages"])
    block = {"messages": [{"role": "assistant", "content": [{"type": "compaction", "content": "..."}]}]}
    assert markers(block) == ["compaction_block"]
    asked = {"messages": [], "context_management": {"edits": [{"type": "compact_20260112"}]}}
    assert markers(asked) == ["context_management"]
    assert markers({"messages": [], "context_management": {"edits": [{"type": "clear_tool_uses_20250919"}]}}) == []
    assert markers({"messages": [{"role": "user", "content": '{"subtype":"compact_boundary"}'}]}) == [
        "transcript_marker"]
    assert markers({"messages": [{"role": "user", "content": "isCompactSummary"}]}) == ["transcript_marker"]


def _post(port, path, body):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("POST", path, body=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type"), response.read()
    finally:
        connection.close()


def test_the_fake_upstream_replies_from_the_script_and_refuses_what_does_not_fit(small):
    planned = scripted.steps(small)
    server = upstream.FakeUpstream(small, planned)
    port = server.start()
    try:
        assert server._server.server_address[0] == "127.0.0.1" and server.url == f"http://127.0.0.1:{port}"
        first = client.request_body(client.head("s"), [client.user_message(small.turns[0], first=True)])
        status, kind, raw = _post(port, "/v1/messages", first)
        assert (status, kind) == (200, "text/event-stream")
        content, stop = client.parse_stream(raw)
        assert content == [{"type": "text", "text": "OK"}] and stop == "end_turn"
        usage = json.loads(raw.split(b"data: ", 2)[1].split(b"\n")[0])["message"]["usage"]
        assert usage["input_tokens"] == upstream.model_tokens(first)["tokens"] == server.records[0]["tokens"]
        assert usage["cache_read_input_tokens"] == usage["cache_creation_input_tokens"] == 0
        assert server.records[0] | {"bytes": 0} == {
            "n": 0, "bytes": 0, "turn": 0, "round": 0, "status": 200, "tokens": usage["input_tokens"],
            "log_bytes": 0, "other_bytes": server.records[0]["other_bytes"], "output": 1, "messages": 1, "markers": []}
        # The same request again is not what round 1 of turn 2 answers: refused, and the script stays put.
        status, kind, raw = _post(port, "/v1/messages", first)
        assert status == 400 and json.loads(raw)["error"]["type"] == "invalid_request_error"
        assert server.cursor == 1 and server.unexpected == [
            "request 2 does not fit round 1 of turn 2: its last message does not hold the text of turn 2"]
        assert _post(port, "/v1/messages/count_tokens", first)[0] == 404
        assert _post(port, "/v1/complete", {})[0] == 404
        assert len(server.records) == 2  # other paths are not recorded
        status, _, raw = _post(port, "/v1/messages", [1, 2])
        assert status == 400 and server.unexpected[-1] == "request 3 is not a JSON object"
    finally:
        server.stop()


def test_a_tool_round_must_carry_the_result_of_the_call_before_it(small):
    planned = scripted.steps(small)
    server = upstream.FakeUpstream(small, planned)
    loop = small.toolloop_turn
    server.cursor = next(i for i, s in enumerate(planned) if s.turn == loop)
    fields = client.head("s")

    def body(messages):
        return json.dumps(client.request_body(fields, messages)).encode()

    messages = [client.user_message(small.turns[loop], first=True)]
    status, _, events = server.answer("/v1/messages", body(messages))
    content, stop = client.parse_stream(b"".join(events))
    assert status == 200 and stop == "tool_use"
    assert content == [{"type": "tool_use", "id": f"toolu_bench_{loop:03d}_00", "name": "read_file",
                        "input": {"file_path": small.reads[0]}}]
    messages.append({"role": "assistant", "content": content})
    # A valid request that answers the call with another file's text in a text block: it does not fit.
    wrong = [*messages, {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": content[0]["id"], "content": "x"}]}]
    server.cursor += 1  # as if the script had moved on past this call
    assert server.answer("/v1/messages", body(wrong))[0] == 400
    assert "does not hold the result of tool call" in server.unexpected[-1]
    server.cursor -= 1
    right = [*messages, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": content[0]["id"],
                                                      "content": small.files[small.reads[0]]}]}]
    status, _, events = server.answer("/v1/messages", body(right))
    assert status == 200 and client.parse_stream(b"".join(events))[0][0]["input"] == {"file_path": small.reads[1]}
    server.cursor = len(planned)
    assert server.answer("/v1/messages", body(right))[0] == 400
    assert server.unexpected[-1].endswith("came after the last scripted round")


# The synthetic client


def test_the_head_is_synthetic_text_of_a_realistic_size():
    fields = client.head("bench-replay-20261016")
    tokens = session.head_tokens(fields)
    assert 18_641 <= tokens <= 27_925  # the range measured for Claude Code 2.1.314 (launcher.MEASURED_OVERHEADS)
    assert tokens == 21_277
    text = json.dumps({"system": client.system_blocks(), "context": client.context_block(),
                       "tools": client.synthetic_tools()})
    for name in ("Claude", "Anthropic", "claude"):
        assert name not in text, name  # none of a real client's prompt or tool text
    assert client.system_blocks()[0]["text"].startswith("This is the synthetic system prompt")
    assert all("Synthetic tool definition" in tool["description"] for tool in client.synthetic_tools())
    assert client.context_block()["text"].startswith("<system-reminder>\nSynthetic context block")
    assert client.context_block()["text"].endswith("</system-reminder>")
    assert list(fields) == ["system", "tools", "metadata", "max_tokens", "stream"] and fields["stream"] is True
    assert json.loads(fields["metadata"]["user_id"]) == {"session_id": "bench-replay-20261016"}
    assert client.MODEL == upstream.MODEL_NAME == "contexto-bench-replay"


def test_filler_is_deterministic_and_exactly_sized():
    assert client.filler("system", 1_000) == client.filler("system", 1_000)
    assert client.filler("system", 1_000) != client.filler("tool/x", 1_000)
    for size in (1, 57, 1_000, 24_000):
        assert len(client.filler("a", size).encode()) == size
    assert client.filler("a", 500) == client.filler("a", 1_000)[:500]
    assert len(client.synthetic_digest()) == 64 and client.synthetic_digest() == client.synthetic_digest()


def test_contextos_own_tools_are_in_the_list_as_claude_code_names_them():
    tools = client.head("s")["tools"]
    ours = [t for t in tools if t["name"].startswith("mcp__contexto__")]
    assert [t["name"] for t in ours] == [f"mcp__contexto__{t['name']}" for t in mcp.TOOLS]
    assert [(t["description"], t["input_schema"]) for t in ours] == [
        (t["description"], t["inputSchema"]) for t in mcp.TOOLS]
    assert scripted.SEARCH_TOOL in {t["name"] for t in ours} and scripted.READ_TOOL == tools[0]["name"]
    assert len(tools) == 16 + len(mcp.TOOLS) and len({t["name"] for t in tools}) == len(tools)
    request = client.request_body(client.head("s"), [client.user_message("hello", first=True)])
    assert toolcheck.anthropic_tools_offered(request) is True  # so the proxy does not report mcp_tools_missing
    assert rewrite.compaction_summary(request["messages"]) is False


def test_a_request_moves_the_cache_breakpoint_without_changing_the_history():
    history = [client.user_message("turn one", first=True),
               {"role": "assistant", "content": [{"type": "text", "text": "OK"}]},
               client.user_message("turn two", first=False)]
    before = copy.deepcopy(history)
    body = client.request_body({"system": [], "tools": []}, history)
    assert history == before
    assert list(body)[:2] == ["model", "messages"] and body["model"] == client.MODEL
    assert body["messages"][:2] == history[:2]
    assert body["messages"][2]["content"] == [{"type": "text", "text": "turn two", "cache_control": client.CACHE}]
    assert [b["type"] for b in history[0]["content"]] == ["text", "text"]
    assert history[0]["content"][0]["text"].startswith("<system-reminder>")


def test_the_client_connects_to_loopback_only(small):
    for host in ("localhost", "0.0.0.0", "example.com"):
        with pytest.raises(ValueError, match="connects to 127.0.0.1 only"):
            client.Client(1, "s", small, object(), host=host)


# The modelled lines


def test_history_tokens_end_at_the_scripts_estimate(small, pinned):
    for script in (small, pinned, bench_script.build(seed=5, protocol="d27", bound=4_000, turn_bytes=2_000)):
        history = lines.history_tokens(script)
        assert len(history) == len(script.turns) and history == sorted(history)
        assert history[-1] == bench_script.estimate(script)["tokens"]
        assert history[script.n_content - 1] == bench_script.estimate(script)["history_tokens"]


def _check_row(row, arm):
    assert list(row) == ["arm", "seed", "repeat", "model", "n", "turn", "main", "history_tokens", "uncached",
                         "cache_write", "cache_read", "output", "status", "compaction", "basis"]
    assert (row["arm"], row["repeat"], row["model"], row["status"], row["basis"]) == (arm, 0, None, 200, "modelled")
    assert row["cache_write"] == row["cache_read"] == 0
    for key in ("seed", "n", "turn", "history_tokens", "uncached", "output"):
        assert type(row[key]) is int, key
    assert type(row["main"]) is bool and type(row["compaction"]) is bool


def test_the_full_line_is_the_head_plus_the_whole_history(small):
    rows = lines.full_rows(small, 1_000)
    history = lines.history_tokens(small)
    assert [r["uncached"] for r in rows] == [1_000 + h for h in history]
    assert [r["n"] for r in rows] == [r["turn"] for r in rows] == list(range(len(small.turns)))
    for row in rows:
        _check_row(row, "full")
        assert row["main"] and not row["compaction"]


def test_the_plain_line_compacts_at_the_trigger_and_starts_again_from_a_summary(small):
    head, trigger = 1_000, 12_000
    rows = lines.plain_rows(small, head, trigger)
    estimates, history = lines.turn_estimates(small), lines.history_tokens(small)
    main = [r for r in rows if r["main"]]
    assert [r["turn"] for r in main] == list(range(len(small.turns)))
    assert [r["n"] for r in rows] == list(range(len(rows)))
    assert lines.compactions(rows) == len(rows) - len(main) >= 3
    carried = 0
    for row in rows:
        _check_row(row, "plain")
        turn = row["turn"]
        assert row["history_tokens"] == history[turn]
        if row["compaction"]:
            assert not row["main"] and row["uncached"] == head + carried and row["output"] == lines.SUMMARY_TOKENS
            assert head + carried + estimates[turn] > trigger  # the request that would have passed the trigger
            carried = lines.SUMMARY_TOKENS
        else:
            assert row["uncached"] == head + carried + estimates[turn]
            # Under the trigger, unless the head, a summary and this one turn are already more than it.
            assert row["uncached"] <= max(trigger, head + lines.SUMMARY_TOKENS + estimates[turn])
            carried += estimates[turn]
    assert main[0]["uncached"] == head + estimates[0]
    assert lines.compactions(lines.plain_rows(small, head, 10**9)) == 0
    assert lines.SUMMARY_TOKENS == round((5_510 + 8_908 + 9_342) / 3)


def test_the_summary_size_is_the_one_measured_in_the_research_notes():
    note = (Path(__file__).resolve().parents[1] / "docs" / "research" / "M0-proxy-measurements.md").read_text("utf-8")
    for measured in ("| 139,320 | 139,318 | 0 | 5,510 |", "| 78,469 | 52,131 | 26,336 | 8,908 |",
                     "| 86,968 | 60,630 | 26,336 | 9,342 |"):
        assert measured in note
    assert "M0-proxy-measurements.md" in lines.SUMMARY_SOURCE and lines.SURVIVES.startswith("assumed, not measured")


# The trace


def test_the_shipped_trace_pins_the_default_session(pinned):
    shipped = trace.load_packaged()
    assert shipped["script"] == trace.PINNED == {"seed": 20261016, "protocol": "v2", "bound": 150_000,
                                                 "turn_bytes": 40_000, "turns": 40}
    assert trace.file_name() == "trace-20261016.json"
    trace.check(shipped, pinned, 40)  # the generator gives the pinned session
    found = trace.fingerprint(pinned)
    for key, value in found.items():
        assert shipped[key] == value, key
    assert len(shipped["turn_sha256"]) == len(pinned.turns) and shipped["script_sha256"] == pinned.digest()
    assert shipped["model"]["rounds"] == scripted.rounds(pinned)
    assert sum(shipped["model"]["rounds"]) == shipped["model"]["steps"] == shipped["expected"]["requests"]
    assert shipped["model"]["rules"] == scripted.RULES
    assert list(shipped["files"]) == list(pinned.reads)
    estimate = bench_script.estimate(pinned)
    assert estimate["history_tokens"] >= 6 * 150_000  # several times the bound, so the engine rotates
    raw = resources.files("contexto.bench").joinpath("data", "trace-20261016.json").read_bytes()
    assert raw.decode() == trace.to_json(shipped) and len(raw) < 16_000  # written by record(), a few kilobytes


def test_the_default_command_line_is_the_pinned_session():
    from contexto import cli

    args = cli.build_parser().parse_args(["bench", "--replay"])
    assert {"seed": args.seed, "protocol": args.protocol, "bound": args.bound,
            "turn_bytes": args.turn_kbytes * 1_000} == {k: v for k, v in trace.PINNED.items() if k != "turns"}
    assert (args.replay, args.replay_turns, args.dump_trace, args.record_trace) == (True, None, None, None)


def test_a_trace_and_a_generator_that_disagree_are_refused_naming_the_first_turn(pinned):
    shipped = trace.load_packaged()
    altered = copy.deepcopy(shipped)
    altered["turn_sha256"][6] = "0" * 64
    altered["turn_sha256"][20] = "1" * 64
    with pytest.raises(trace.TraceError) as refused:
        trace.check(altered, pinned, 40)
    message = str(refused.value)
    assert message.startswith("the generator and the pinned trace disagree: turn 7 of 56 (a ")
    assert "SHA-256 0000000000000000 in the trace" in message and "Nothing was replayed." in message
    assert pinned.digest()[:16] not in message and trace.fingerprint(pinned)["turn_sha256"][6][:16] in message
    altered = copy.deepcopy(shipped)
    altered["turn_bytes"][0] += 1
    with pytest.raises(trace.TraceError, match="turn 1 of 56"):
        trace.check(altered, pinned, 40)


@pytest.mark.parametrize("change, said", [
    (lambda t: t["turn_sha256"].pop(), "the trace has 55 turns and the generator gives 56"),
    (lambda t: t["files"]["bench-files/log-03.txt"].update(sha256="0" * 64), "the file bench-files/log-03.txt differs"),
    (lambda t: t["files"].update(extra={"bytes": 1, "sha256": "0"}),
     "the trace has the file extra, which the generator does not give"),
    (lambda t: t["files"].pop("bench-files/log-03.txt"),
     "the generator gives the file bench-files/log-03.txt, which the trace does not have"),
    (lambda t: t.update(script_sha256="0" * 64), "the probes, answers or decisions differ"),
    (lambda t: t["model"].update(steps_sha256="0" * 64), "the scripted model and the pinned trace disagree"),
    (lambda t: t["client"].update(synthetic_sha256="0" * 64), "the synthetic client and the pinned trace disagree"),
    (lambda t: t["rates"].update(log_bytes_per_token=1.9), "byte rates are not the ones the trace pins"),
    (lambda t: t["script"].update(bound=140_000), "the trace pins bound 140000, not 150000"),
    (lambda t: t["script"].update(turns=41), "the trace pins turns 41, not 40"),
])
def test_every_pinned_part_is_checked(pinned, change, said):
    altered = copy.deepcopy(trace.load_packaged())
    change(altered)
    with pytest.raises(trace.TraceError) as refused:
        trace.check(altered, pinned, 40)
    assert said in str(refused.value) and "othing was replayed" in str(refused.value)


def test_a_file_that_is_not_a_trace_is_refused():
    for text in ("", "{", "[]", "{}", json.dumps({"kind": trace.KIND, "format": 2}),
                 json.dumps({"kind": "other", "format": 1})):
        with pytest.raises(trace.TraceError):
            trace.parse(text)
    with pytest.raises(trace.TraceError, match="a replay trace of format 2; this build reads format 1"):
        trace.parse(json.dumps({"kind": trace.KIND, "format": 2}))
    with pytest.raises(trace.TraceError, match="not a replay trace: the file holds a list, not an object"):
        trace.parse("[]")
    with pytest.raises(trace.TraceError, match="has no turn_sha256, expected"):
        trace.parse(json.dumps({"kind": trace.KIND, "format": 1, "script": {}, "turn_bytes": [], "files": {},
                                "model": {}, "client": {}}))
    with pytest.raises(trace.TraceError, match="this build has no pinned trace trace-5.json"):
        trace.load_packaged(5)


@pytest.mark.parametrize("section, value, said", [
    ("script", [], "script is a list, not an object"),
    ("files", [], "files is a list, not an object"),
    ("model", None, "model is null, not an object"),
    ("expected", [], "expected is a list, not an object"),
    ("turn_sha256", "x", "turn_sha256 is a string, not a list"),
])
def test_a_section_of_the_wrong_type_is_refused_without_a_traceback(section, value, said):
    """Reviewer B: a hand-altered trace with a section of the wrong type raised AttributeError."""
    altered = copy.deepcopy(trace.load_packaged())
    altered[section] = value
    with pytest.raises(trace.TraceError, match=f"the trace is damaged: {said}"):
        trace.parse(json.dumps(altered))


def test_a_pinned_trace_that_is_not_utf8_is_damaged_not_missing(monkeypatch, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "trace-20261016.json").write_bytes(b'{"kind": "\xff"}')
    monkeypatch.setattr(trace.resources, "files", lambda package: tmp_path)
    with pytest.raises(trace.TraceError, match="the pinned trace trace-20261016.json is not UTF-8 text"):
        trace.load_packaged()


def test_differences_are_listed_in_plain_words():
    pinned = {"requests": 3, "request_tokens": [10, 20, 30], "failure_events": {},
              "reach": {"reachable": 48, "by_class": {"chain": {"reachable": 27}}}, "reach_sha256": "aa"}
    assert trace.differences(pinned, copy.deepcopy(pinned)) == []
    found = {"requests": 4, "request_tokens": [10, 21, 33, 40], "failure_events": {"turn_room_low": 1},
             "reach": {"reachable": 47, "by_class": {"chain": {"reachable": 26}}}, "reach_sha256": "bb"}
    assert trace.differences(pinned, found) == [
        "requests: expected 3, measured 4",
        "request_tokens: 3 of 4 differ; the first is number 2, expected 20, measured 21",
        "failure_events.turn_room_low: not in the trace, measured 1",
        "reach.reachable: expected 48, measured 47",
        "reach.by_class.chain.reachable: expected 27, measured 26",
        "reach_sha256: expected 'aa', measured 'bb'",
    ]
    assert trace.differences({"a": 1, "b": 2}, {"a": 1}) == ["b: expected 2, not measured"]
    assert trace.differences({"n": [1, 2, 3]}, {"n": [1, 2]}) == [
        "n: 1 of 3 differ; the first is number 3, expected 3, measured nothing"]
    # A section of another type is named by its type, not printed whole (Reviewer B's nit).
    assert trace.differences({"reach": []}, {"reach": {"probes": 72, "by_class": {"chain": {}}}}) == [
        "reach: expected a list, measured an object"]


def test_a_size_difference_says_where_it_is():
    """Reviewer A: every request grown by the same bytes is a change to what all of them carry; requests
    that differ only from some number on are a change from there."""
    pinned = [100, 200, 300, 400]
    assert trace.differences({"t": pinned}, {"t": [102, 203, 302, 402]}) == [
        "t: 4 of 4 differ; the first is number 1, expected 100, measured 102. All 4 differ, by +2 to +3 tokens: "
        "something every request carries changed by the same bytes (the head, the tool definitions)"]
    assert trace.differences({"t": pinned}, {"t": [99, 199, 299, 399]})[0].endswith(
        "All 4 differ, by -1 tokens: something every request carries changed by the same bytes (the head, the tool "
        "definitions)")
    assert trace.differences({"t": pinned}, {"t": [100, 200, 350, 380]}) == [
        "t: 2 of 4 differ; the first is number 3, expected 300, measured 350. Requests 1 to 2 are equal and the "
        "difference starts at number 3: what the proxy sends changed from there (paging, a rotation, a search "
        "reply)"]
    assert trace.differences({"t": pinned}, {"t": [150, 200, 300, 401]})[0].endswith(
        ". The first request differs too, by another amount than the later ones")


def test_the_trace_records_the_commit_it_was_recorded_on():
    commit = trace.source_commit()
    in_checkout = (Path(trace.__file__).resolve().parents[4] / ".git").exists()
    if in_checkout:
        assert commit is not None and len(commit.split("+")[0]) == 40
    else:
        assert commit is None


def test_dump_writes_the_whole_session_to_read(small, tmp_path):
    written = trace.dump(tmp_path / "dump", small, "bench-replay-3")
    root = tmp_path / "dump"
    turns = sorted((root / "turns").iterdir())
    assert [p.read_text("utf-8") for p in turns] == list(small.turns)
    assert turns[0].name == "turn-001-log.txt" and turns[-1].name == f"turn-{len(small.turns):03d}-probe.txt"
    assert {str(p.relative_to(root / "files")): p.read_text("utf-8")
            for p in (root / "files").rglob("*.txt")} == small.files
    assert (root / "script.json").read_text("utf-8") == small.to_json()
    steps = [json.loads(line) for line in (root / "model-steps.jsonl").read_text("utf-8").splitlines()]
    assert steps == [s.to_dict() for s in scripted.steps(small)]
    head = json.loads((root / "client-head.json").read_text("utf-8"))
    assert head["system"] == client.system_blocks() and head["context_block"] == client.context_block()
    assert len(written) == len(small.turns) + len(small.files) + 3 and all(p.is_file() for p in written)


# The session log readers


def test_the_failure_list_is_the_acceptance_harnesss(spike):
    assert session.FAILURE_EVENTS == spike["detectors"].FAILURE_EVENTS


def test_failure_events_are_found_wherever_the_log_carries_them(spike):
    log = [
        {"event": "proxy_start"},
        {"event": "request", "path": "/v1/messages", "status": 200, "over_bound": False,
         "transform_events": [{"event": "rotation"}, {"event": "turn_room_low"}, {"event": "request", "estimate": 9}]},
        {"event": "request", "path": "/v1/messages", "status": 200, "over_bound": True,
         "transform_events": [{"event": "request", "estimate": 5, "sent_estimate": 7}],
         "usage_events": [{"event": "over_bound", "source": "real"}, {"event": "estimate_error"}]},
        {"event": "request", "path": "/v1/messages/count_tokens", "status": 200},
        {"event": "transform_error", "level": "error"},
        {"event": "mcp_call", "tool": "contexto_search", "ok": True},
        {"event": "turn_room_missing", "tool": "contexto_search"},
    ]
    assert session.failure_events(log) == {"transform_error": 1, "turn_room_low": 1, "turn_room_missing": 1}
    assert session.failure_events(log) == dict(sorted(spike["detectors"].failure_events(log).items()))
    assert session.event_counts(log) == {"estimate_error": 1, "mcp_call": 1, "over_bound": 1, "proxy_start": 1,
                                         "request": 3, "rotation": 1, "transform_error": 1, "turn_room_low": 1,
                                         "turn_room_missing": 1}
    entries = session.proxy_requests(log)
    assert len(entries) == 2 and [session._marked_over(e) for e in entries] == [False, True]
    assert [session._estimate(e) for e in entries] == [9, 7]
    assert session.failure_events([{"event": "request", "transform_events": None}]) == {}


def test_every_environment_variable_the_package_reads_is_pinned():
    """Every name a module of the package reads from the environment is held at its default in a replay.
    The names are the string constants of the modules that read ``os.environ``; a new knob that is not a
    ``CONTEXTO_`` variable fails here until it is added to ``session.ENVIRONMENT``."""
    import ast
    import re

    root = Path(session.__file__).resolve().parents[2]
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        if "bench" in path.relative_to(root).parts:
            continue
        source = path.read_text("utf-8")
        if "environ" not in source and "getenv" not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.fullmatch(
                    r"(?:CONTEXTO|ANTHROPIC|CLAUDE|CODEX|OPENRIG)_[A-Z0-9_]+", node.value):
                found.setdefault(node.value, path.name)
    assert {"CONTEXTO_RECALL_VARIANT", "CONTEXTO_SEARCH_CACHE_MB", "OPENRIG_SESSION_NAME"} <= set(found)
    missing = {name: where for name, where in found.items()
               if name not in session.ENVIRONMENT and not name.startswith(session.ENVIRONMENT_PREFIX)}
    assert missing == {}


def test_the_pinned_environment_is_put_back(monkeypatch):
    import os

    monkeypatch.setenv("CONTEXTO_RECALL_VARIANT", "legacy")
    monkeypatch.delenv("CONTEXTO_HOME", raising=False)
    with session.pinned_environment() as held:
        assert "CONTEXTO_RECALL_VARIANT" not in os.environ
        assert "CONTEXTO_RECALL_VARIANT" in held["set_by_the_user_and_ignored"]
        os.environ["CONTEXTO_HOME"] = "set inside"  # nothing set inside the block leaks out of it
    assert os.environ["CONTEXTO_RECALL_VARIANT"] == "legacy" and "CONTEXTO_HOME" not in os.environ


def test_the_log_reader_skips_a_torn_line(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text('{"event": "a"}\n[1]\n{"event": "b"\n{"event": "c"}\n', encoding="utf-8")
    assert [e["event"] for e in session.read_log(path)] == ["a", "c"]


# Starting any contexto command does not load the replay


def test_the_mode_module_imports_nothing_heavy():
    code = ("import sys\nimport contexto.bench.modes.replay as m\n"
            "print(sorted(n for n in sys.modules if n.startswith('contexto')))\n"
            "print('http.server' in sys.modules, m.FLAG)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.splitlines()
    assert out[0] == "['contexto', 'contexto.bench', 'contexto.bench.modes', 'contexto.bench.modes.replay']"
    assert out[1] == "False --replay"
