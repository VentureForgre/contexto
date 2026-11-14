"""In-turn paging (D63, contract rule 5): its invariants, through the real engine.

The scripted CLI and the enforcing fake upstream are in ``inturn_harness``; every request a scenario
forwards is checked there for tool pairing, for a thinking block whose prefix changed, and, between
batches, for being an append-only extension of the one before. The tests below add the bound, the recall
room, byte-exact storage, the turn start after a paged turn, resume, old stores, degraded mode,
``count_tokens`` and the off switch. All content is synthetic."""

from __future__ import annotations

import argparse
import copy

import pytest

from contexto import inturn, launcher
from contexto.canonical import canonical_bytes
from contexto.codex import RESPONSES
from contexto.commands import run as run_command
from contexto.rewrite import ANTHROPIC, COUNT_TOKENS, MESSAGES, Config, EpochState, RewriteEngine, _drop_cache_control
from contexto.store import SessionStore, check_epoch
from contexto.store.catalog import check_page
from contexto.store.memory import MemoryStore
from contexto.store.pages import RESULT_PAGE, ROUNDS_PAGE, build_page, page_id, render_index
from contexto.store.render import recall_chunk
from inturn_harness import AGENT, MODEL, Cli, Refused, Upstream, blocks, check_pairing, output, png, tool

BOUND = Config().bound
ROOM = Config().min_turn_room
SMALL = Config(bound=60_000, headroom_floor=8_000, min_turn_room=4_000)  # T = 48,000
RESULT = 20_480  # bytes: about 6,400 tokens at the fake tokenizer's 3.2 bytes a token


def memory() -> MemoryStore:
    return MemoryStore(key=b"k" * 32)


def batches(cli: Cli) -> list[dict]:
    return cli.events("in_turn_batch")


def first_batch(cli: Cli) -> int:
    return next(i for i, s in enumerate(cli.sent) if "in_turn_batch" in s.names)


def assert_bound_held(cli: Cli, bound: int = BOUND) -> None:
    """Invariant 5: every forwarded request is at or under the bound, by the engine's estimate and by the
    upstream's count, and nothing was logged over it."""
    assert max(s.estimate for s in cli.sent) <= bound
    assert max(s.real for s in cli.sent) <= bound
    assert not cli.events("over_bound") and not cli.events("in_turn_exhausted")
    assert not [e for e in cli.usage_events if e["event"] == "over_bound"]


def assert_room_held(cli: Cli, room: int = ROOM) -> None:
    """Invariant 5: once a batch has run, the request-time recall room never falls under min_turn_room."""
    assert min(s.room for s in cli.sent[first_batch(cli):]) >= room


def stubbed_blocks(cli: Cli) -> list[tuple[dict, dict, str, dict]]:
    """(the CLI's result block, its call block, the page id, the block as forwarded) for every stub that
    is still in the window."""
    state = cli.state
    forwarded = inturn.view(cli.history, state, ANTHROPIC)
    out = []
    for i, j, page in state.inturn.stubs:
        result = cli.history[i]["content"][j]
        call = next(b for b in cli.history[i - 1]["content"] if b.get("id") == result["tool_use_id"])
        out.append((result, call, page, forwarded[i]["content"][j]))
    return out


def assert_stored_exactly(cli: Cli, store) -> None:
    """Invariant 4: the page of every stub holds the CLI's own call and result blocks byte for byte, as
    they were in the request that paged them: a result stubbed unseen was that request's last block, so
    it carries Claude Code's moving ``cache_control`` mark, as a whole-turn page's last block can."""
    stubs = stubbed_blocks(cli)
    assert stubs
    for result, call, page, sent in stubs:
        assert set(sent) == set(result) and sent["tool_use_id"] == result["tool_use_id"]
        assert isinstance(sent["content"], str) and page in sent["content"]
        stored = store.get_page(page)
        assert stored["kind"] == RESULT_PAGE
        as_sent = [canonical_bytes([{"role": "assistant", "content": [call]}, {"role": "user", "content": [block]}])
                   for block in (result, {**result, "cache_control": {"type": "ephemeral"}})]
        assert canonical_bytes(stored["messages"]) in as_sent


# Scenarios ---------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def loop120(tmp_path_factory):
    """One prompt and 120 tool rounds of about 6,400 tokens each: 775,851 tokens sent whole at v0.5.24."""
    store = SessionStore(tmp_path_factory.mktemp("loop120"), "s1")
    cli = Cli(store)
    cli.prompt("Read every module and report what each one does.")
    cli.loop(120, lambda r: output(f"module-{r:04d}", RESULT))
    return cli, store


@pytest.fixture(scope="module")
def huge3():
    """Twelve rounds, three of them one result larger than the bound (about 160,000 tokens each)."""
    cli = Cli(memory())
    cli.prompt("Read the three dumps and the notes around them.")
    cli.loop(12, lambda r: output(f"dump-{r:02d}", 512_000 if r in (3, 6, 9) else RESULT))
    return cli


@pytest.fixture(scope="module")
def small_rounds():
    """600 rounds whose results are too small to be worth a stub, so only folding frees anything. The
    1,500-round run at the default bound is in spikes/m2/inturn/d63_measure.py: the engine's work per
    request grows with the history, and that run takes about two minutes."""
    cli = Cli(memory(), SMALL, head_bytes=8_000)
    cli.prompt("Check each of the 600 files for the marker.")
    cli.loop(600, lambda r: output(f"file-{r:04d}", 400), lambda r: ("Grep", {"pattern": "marker", "path": f"src/{r}"}))
    return cli


@pytest.fixture(scope="module")
def images():
    """Images in the prompt and in every result."""
    cli = Cli(memory())
    cli.prompt("Compare these two screenshots with the pages you fetch.", png(1_440, 900), png(1_440, 900))
    cli.loop(60, lambda r: [{"type": "text", "text": output(f"shot-{r:03d}", 9_000)}, png(1_000, 1_000)])
    return cli


@pytest.fixture(scope="module")
def subagent():
    """Main pages a first turn, then launches a subagent whose one turn runs 120 tool rounds."""
    store = memory()
    engine = RewriteEngine(store, config=SMALL)
    main = Cli(store, engine=engine, head_bytes=8_000)
    for turn in range(5):  # enough prose for a turn-start rotation to page the first turns
        main.prompt(f"Note {turn}: " + output(f"note-{turn}", 34_000))
        main.send()
    main.prompt("Now have an agent audit the modules.")
    main.send([("Agent", {"prompt": "Audit every module.", "description": "audit"})])
    side = Cli(store, engine=engine, head_bytes=8_000, headers=AGENT)
    side.prompt("Audit every module.")
    side.loop(120, lambda r: output(f"audit-{r:04d}", RESULT))
    return main, side, engine, store


# Invariant 5: the bound and the recall room --------------------------------------------------------

def test_120_rounds_stay_under_the_bound_and_keep_the_recall_room(loop120):
    cli, _ = loop120
    assert_bound_held(cli)
    assert_room_held(cli)
    assert len(batches(cli)) >= 10 and cli.state.inturn.stubs
    # Between batches the request grows by a round at a time, so no batch follows another directly.
    ran = [i for i, s in enumerate(cli.sent) if "in_turn_batch" in s.names]
    assert min(b - a for a, b in zip(ran, ran[1:])) >= 4


def test_three_results_larger_than_the_bound_are_stubbed_unseen(huge3):
    cli = huge3
    assert_bound_held(cli)
    assert_room_held(cli)
    unseen = [e for e in batches(cli) if e["unseen_results_stubbed"]]
    assert len(unseen) == 3 and all(e["trigger"] == "result" and e["results_stubbed"] == 1 for e in unseen)
    # An unseen result sits after the last thinking block: stubbing it strips nothing and changes no
    # message before it, so the request still extends the one before.
    for i, sent in enumerate(cli.sent):
        if any(e["event"] == "in_turn_batch" and e["trigger"] == "result" for e in sent.events):
            previous = cli.sent[i - 1]
            assert sent.messages[:len(previous.messages)] == previous.messages
            added = sum(1 for b in sent.messages[-2]["content"] if b["type"] in ("thinking", "redacted_thinking"))
            assert sent.thinking == previous.thinking + added and added >= 1
    assert_stored_exactly(cli, cli.store)


def test_many_small_rounds_fold_into_pages(small_rounds):
    cli = small_rounds
    assert_bound_held(cli, SMALL.bound)
    assert_room_held(cli, SMALL.min_turn_room)
    state = cli.state
    assert not state.inturn.stubs and len(state.inturn.folds) >= 3  # a 400-byte result is never worth a stub
    assert sum(e["rounds_folded"] for e in batches(cli)) > 300
    # A rounds page holds the removed messages exactly as the CLI sent them.
    for start, ranges, page in state.inturn.folds:
        assert start == 0
        removed = [cli.history[i] for a, b in ranges for i in range(a, b)]
        stored = cli.store.get_page(page)
        assert stored["kind"] == ROUNDS_PAGE and canonical_bytes(stored["messages"]) == canonical_bytes(removed)
    # The turn start keeps its prompt and gains one note per fold; the newest round is never folded.
    first = cli.sent[-1].messages[0]["content"]
    assert first[0]["text"].startswith("Check each of the 600 files")
    notes = [b["text"] for b in first[1:]]
    assert len(notes) == len(state.inturn.folds) and all(page in note for (_, _, page), note in
                                                         zip(state.inturn.folds, notes))
    assert "Grep x" in notes[0] and "tool rounds of this turn into page" in notes[0]
    assert cli.sent[-1].messages[-1] == _drop_cache_control(cli.request()["messages"][-2])


def test_a_subagents_loop_stays_under_the_bound(subagent):
    main, side, engine, store = subagent
    assert_bound_held(side, SMALL.bound)
    assert_room_held(side, SMALL.min_turn_room)
    assert len(batches(side)) >= 10
    assert all(s.events[-1]["main"] is False for s in side.sent)  # a side conversation throughout
    # Its pages carry the main conversation's current turn number, so search ranks them as recent.
    main_state = engine.states[next(k for k, v in engine.states.items() if v is not side_state(engine, side))]
    turn = inturn.current_turn(main.history, main_state, ANTHROPIC)
    assert main_state.turns_paged >= 1 and turn >= 1
    pages = {page for _, _, page in side_state(engine, side).inturn.stubs if page}
    assert pages and {store.get_page(page)["turn"] for page in pages} == {turn}
    # Search and recall reach the subagent's stubbed results while its turn is still in the window.
    found = tool(store, "contexto_search", {"query": "needle-audit-0003-midpoint"})
    page = next(p for p in pages if p in found)
    assert "needle-audit-0003-midpoint" in found and "row 00000 of audit-0003" in tool(
        store, "contexto_recall", {"page_id": page})


def side_state(engine: RewriteEngine, side: Cli) -> EpochState:
    return next(v for v in engine.states.values() if len(v.hashes) == len(side.history) - 1)


def test_images_in_results_and_in_the_prompt(images):
    cli = images
    assert_bound_held(cli)
    assert_room_held(cli)
    stubs = stubbed_blocks(cli)
    assert stubs and all("1 image (recall shows text only" in sent["content"] for _, _, _, sent in stubs)
    assert_stored_exactly(cli, cli.store)  # the image block is in the page byte for byte
    assert sum(1 for b in cli.sent[-1].messages[0]["content"] if b.get("type") == "image") == 2  # the prompt's stay


# Invariants 1 to 3 are checked on every request by the harness; these show the checks bite ---------

def test_the_fake_upstream_refuses_a_thinking_block_whose_prefix_changed():
    cli = Cli(memory(), SMALL, head_bytes=8_000)
    cli.prompt("Read two files.")
    cli.send([("Read", {"file_path": "a.py"})])
    cli.results([output("a", 4_000)])
    cli.send([("Read", {"file_path": "b.py"})])
    cli.upstream.verify({"system": cli.system, "tools": cli.tools, "messages": cli.history})
    request = {"system": cli.system, "tools": cli.tools, "messages": copy.deepcopy(cli.history)}
    request["messages"][2]["content"][0]["content"] = "shortened"  # an earlier tool_result, thinking kept after it
    with pytest.raises(Refused, match="message 3 is bound to a different conversation"):
        cli.upstream.verify(request)
    request["messages"][3] = {"role": "assistant", "content": [b for b in request["messages"][3]["content"]
                                                                if b["type"] == "tool_use"]}
    cli.upstream.verify(request)  # with the later thinking stripped the same edit is valid


def test_without_the_thinking_rule_the_upstream_refuses_the_loop(monkeypatch):
    monkeypatch.setattr(inturn, "strips_thinking", lambda edits, i: False)
    cli = Cli(memory(), SMALL, head_bytes=8_000)
    cli.prompt("Read every module.")
    with pytest.raises(Refused):
        cli.loop(30, lambda r: output(f"m-{r}", RESULT))


def test_thinking_before_the_edit_and_after_the_batch_is_kept_byte_for_byte():
    cli = Cli(memory(), SMALL, head_bytes=8_000)
    cli.prompt("Read every module.")
    cli.loop(30, lambda r: output(f"m-{r}", RESULT))
    at = first_batch(cli)
    event = batches(cli)[0]
    p, n = event["first_changed"], event["messages"]
    sent = cli.sent[at].messages
    assert p >= 2 and len(sent) == n
    for i, message in enumerate(sent):
        has = any(b.get("type") in ("thinking", "redacted_thinking") for b in blocks(message))
        if message["role"] == "assistant":
            assert has == (i < p), i  # stripped from the first changed message on, kept before it
        if i < p:
            assert message == _drop_cache_control(cli.history[i])
    # The next request's new assistant message keeps its thinking, and the stripped ones stay stripped.
    after = cli.sent[at + 1].messages
    assert after[:n] == sent and after[n] == cli.history[n]
    assert any(b["type"] == "thinking" for b in after[n]["content"])


def test_pairing_check_catches_an_orphan():
    with pytest.raises(AssertionError, match="tool_result without its tool_use"):
        check_pairing([{"role": "user", "content": "go"},
                       {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]}])
    with pytest.raises(AssertionError, match="tool_use without its tool_result"):
        check_pairing([{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read",
                                                           "input": {}}]},
                       {"role": "user", "content": "next"}])


# Invariant 4: byte-exact storage, recall and search ----------------------------------------------

def test_stubbed_results_are_stored_exactly_and_recall_and_search_reach_them(loop120):
    cli, store = loop120
    assert_stored_exactly(cli, store)
    result, call, page, sent = stubbed_blocks(cli)[5]
    tag = call["input"]["file_path"].removeprefix("src/module_").removesuffix(".py")
    needle = f"needle-module-{tag}-midpoint"
    assert needle in result["content"] and needle not in sent["content"]
    # The whole result reads back through recall's rendering, and the MCP tools reach the page.
    text = recall_chunk(store.get_page(page), 0, 10**9)["text"]
    assert result["content"] in text and call["input"]["file_path"] in text
    assert f"row 00000 of module-{tag}" in tool(store, "contexto_recall", {"page_id": page})
    found = tool(store, "contexto_search", {"query": needle})
    assert page in found and needle in found
    assert store.search(needle)[0] == page


def test_the_retained_snapshot_holds_the_window_as_sent(loop120):
    # Search's "in view" lines come from this snapshot: a stubbed result is in view as its stub, and its
    # text is found in its page instead.
    cli, store = loop120
    result, call, page, sent = stubbed_blocks(cli)[5]
    _, _, _, entries = store.retained()
    texts = [e["text"] for e in entries if e["role"] == "tool_result"]
    assert any(page in text and text.startswith("tool_result:\n[Contexto paged out") for text in texts)
    assert not any(result["content"][:200] in text for text in texts)
    kept = cli.history[-2]["content"][0]["content"]  # the newest result is still in view, whole
    assert any(kept[:200] in text for text in texts)


def test_a_stub_keeps_the_blocks_keys_and_says_how_to_read_the_result_back():
    call = {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "pytest -q " + "x" * 400}}
    result = {"type": "tool_result", "tool_use_id": "toolu_1", "is_error": True,
              "cache_control": {"type": "ephemeral"},
              "content": [{"type": "text", "text": "line one\nline two\nliné three"}, png(10, 10)]}
    item = {"role": "user", "content": [result, {"type": "text", "text": "kept"}]}
    text = inturn.stub_text(inturn.MESSAGES, call, result, "pg_0123456789abcdef")
    stubbed = inturn.MESSAGES.with_stubs(item, {0: text})
    assert stubbed["content"][0] == {**result, "content": text} and stubbed["content"][1] == item["content"][1]
    # A long command keeps its start and its end, its middle cut out and marked (Reviewer A, finding 9).
    assert "tool: Bash\n" in text
    assert "command: pytest -q " + "x" * 90 + " [... 210 characters ...] " + "x" * 100 + "\n" in text
    assert "was: 29 bytes, 3 lines, 1 image " in text and "page: pg_0123456789abcdef\n" in text
    assert 'the contexto_recall tool, call it with page_id "pg_0123456789abcdef"' in text and "contexto_search" in text
    assert text == inturn.stub_text(inturn.MESSAGES, copy.deepcopy(call), copy.deepcopy(result), "pg_0123456789abcdef")


def test_contextos_own_results_get_a_stub_and_no_page():
    cli = Cli(memory(), SMALL, head_bytes=8_000)
    cli.prompt("Recall the earlier pages one after another.")
    cli.loop(20, lambda r: output(f"recalled-{r}", RESULT),
             lambda r: ("mcp__contexto__contexto_recall", {"page_id": f"pg_{r:016x}", "offset": 0}))
    assert_bound_held(cli, SMALL.bound)
    stubs = cli.state.inturn.stubs
    assert stubs and all(page == "" for _, _, page in stubs) and not cli.store.pages
    assert all(e["pages"] == 0 and e["own_results_stubbed"] == e["results_stubbed"] for e in batches(cli))
    sent = stubbed_blocks(cli)[0][3]["content"]
    assert "tool: contexto_recall\n" in sent and 'arguments: {"offset":0,"page_id":"pg_0000000000000000"}' in sent
    assert "call the tool again with the same arguments" in sent


def test_a_result_with_blocks_a_stub_cannot_stand_for_is_left_alone():
    reference = [{"type": "tool_reference", "tool_name": "mcp__x__y"}, {"type": "text", "text": "x" * 9_000}]
    assert not inturn.MESSAGES.stubbable({"type": "tool_result", "tool_use_id": "t", "content": reference})
    assert inturn.MESSAGES.stubbable({"type": "tool_result", "tool_use_id": "t", "content": "x" * 9_000})


# The ladder ------------------------------------------------------------------------------------

def test_step_2_pages_the_earlier_turns_when_stubbing_is_not_enough():
    cli = Cli(memory(), SMALL, head_bytes=8_000)
    for turn in range(3):  # three turns of prose no stub can shorten, about 11,000 tokens each
        cli.prompt(f"Turn {turn}: " + output(f"prose-{turn}", 34_000))
        cli.send()
    cli.prompt("Now check the small files.")
    cli.loop(40, lambda r: output(f"small-{r}", 900))
    assert_bound_held(cli, SMALL.bound)
    batch = batches(cli)[0]
    assert batch["turns_paged"] >= 1 and batch["first_changed"] == cli.state.k
    rotation = next(e for e in cli.events("rotation") if e["reason"] == "in_turn")
    assert rotation["k"] == cli.state.k
    assert cli.history[cli.state.k]["content"][0]["text"] == "Now check the small files."
    # The running turn stays, with every thinking block that existed stripped (rule 2); the earlier
    # turns are whole-turn pages.
    assert not any(b.get("type") == "thinking" for m in cli.sent[first_batch(cli)].messages for b in blocks(m))
    assert all("kind" not in cli.store.get_page(p) for p in cli.store.page_ids()[-3:])


def test_a_round_with_carried_content_is_not_folded():
    memory_block = {"type": "text", "text": "<system-reminder>\nContents of /repo/pkg/CLAUDE.md:\n\nUse tabs.\n"
                                            "</system-reminder>"}
    cli = Cli(memory(), SMALL, head_bytes=8_000)
    cli.prompt("Check the small files.")
    cli.send([("Read", {"file_path": "pkg/a.py"})])
    cli.results(["small"])
    cli.history[-1]["content"].append(memory_block)  # nested memory next to the first round's result (D46)
    cli.loop(400, lambda r: output(f"small-{r}", 900))
    assert_bound_held(cli, SMALL.bound)
    folded = {i for _, ranges, _ in cli.state.inturn.folds for a, b in ranges for i in range(a, b)}
    assert folded and not {1, 2} & folded
    assert cli.sent[-1].messages[2]["content"][-1] == memory_block


def test_results_that_cannot_fit_together_are_stubbed_largest_first_on_arrival():
    cli = Cli(memory(), SMALL, head_bytes=8_000)
    cli.prompt("Read four files at once.")
    sizes = (40_000, 70_000, 55_000, 60_000)  # 12,500 to 21,900 tokens: each fits alone, together they do not
    cli.loop(1, lambda r: tuple(output(f"par-{c}", size) for c, size in enumerate(sizes)))
    assert_bound_held(cli, SMALL.bound)
    (batch,) = batches(cli)
    assert batch["trigger"] == "result" and batch["unseen_results_stubbed"] == batch["results_stubbed"] >= 2
    stubbed = sorted(j for _, j, _ in cli.state.inturn.stubs)
    assert stubbed == [1, 3, 2][:len(stubbed)] or stubbed == sorted([1, 3, 2][:len(stubbed)])  # the largest first
    assert 0 not in stubbed  # the smallest is read whole


def test_a_large_result_that_fits_is_read_whole_and_stubbed_only_once_seen():
    # The model reads every result at least once unless it cannot fit: a 31,000-token result arrives when
    # the request has room for it with everything older paged, so it is sent whole; a later batch stubs it.
    cli = Cli(memory())
    cli.prompt("Read the notes, then the big file.")
    for r in range(3):
        cli.send([("Read", {"file_path": f"note{r}.md"})])
        cli.results([output(f"note-{r}", RESULT)])
    cli.send([("Read", {"file_path": "big.log"})])
    cli.results([output("big", 100_000)])
    at = len(cli.history) - 1
    sent = cli.send([("Read", {"file_path": "after.md"})])
    assert sent.messages[at] == cli.history[at] and not any(e["unseen_results_stubbed"] for e in batches(cli))
    cli.results([output("after", RESULT)])
    cli.loop(30, lambda r: output(f"later-{r}", RESULT))
    assert_bound_held(cli)
    assert not any(e["unseen_results_stubbed"] for e in batches(cli))
    assert (at, 0) in {(i, j) for i, j, _ in cli.state.inturn.stubs}


def test_a_rewrite_right_after_a_rewrite_only_when_the_room_would_fall_under_min_turn_room():
    def after_first_batch() -> Cli:
        cli = Cli(memory(), head_bytes=8_000)
        cli.prompt("Read every module.")
        for r in range(60):
            sent = cli.send([("Read", {"file_path": f"m{r}.py"})])
            if "in_turn_batch" in sent.names:
                return cli
            cli.results([output(f"m-{r}", RESULT)])
        raise AssertionError("no batch")

    config = Config()
    line, trigger = inturn.page_line(config), inturn.trigger(config)
    cli = after_first_batch()
    first = batches(cli)[0]
    previous_n = first["messages"]
    assert first["first_changed"] < previous_n - 1 and cli.state.inturn.rewrote == previous_n
    cli.results([output("next", 1_500)])  # the request right after the rewrite: a small result, no margin
    request = cli.request()
    messages, state = request["messages"], cli.state

    def page(estimate: int) -> tuple[object, int | None, list[dict]]:
        events: list[dict] = []
        new, after = inturn.page(request, messages, previous_n, estimate, state, cli.store, config,
                                 cli.engine.estimator, events)
        return new, after, events

    # Between the line and T (the output allowance): the room published is still min_turn_room, so the
    # prefix is not rewritten a second time in a row.
    new, after, events = page(line + 1_500)
    assert new is state and after is None and not events
    # Over T, the room would fall under min_turn_room: the batch runs although the last request rewrote.
    # The estimate is what must be over T (the harness's rule counts the request, not the 1.8 count of a
    # fresh result, which would rewrite prefixes that still fit).
    new, after, events = page(trigger + 2_000)
    (batch,) = [e for e in events if e["event"] == "in_turn_batch"]
    assert batch["first_changed"] < previous_n and batch["unseen_results_stubbed"] == 0
    assert after is not None and config.bound - after - config.headroom_floor >= config.min_turn_room
    assert new.inturn.rewrote == len(messages)


def test_over_the_bound_with_nothing_left_to_page_is_logged():
    cli = Cli(memory(), SMALL, head_bytes=8_000, check=False)
    cli.prompt("Fetch the reference.")
    cli.send([("Read", {"file_path": "big.bin"})])
    cli.results([[{"type": "tool_reference", "tool_name": "x"}, {"type": "text", "text": output("big", 260_000)}]])
    sent = cli.send()
    assert "in_turn_exhausted" in sent.names and "in_turn_batch" not in sent.names
    over = next(e for e in sent.events if e["event"] == "over_bound")
    assert over["in_tool_loop"] is True and over["estimate"] > SMALL.bound and sent.events[-1]["over_bound"] is True


def test_no_batch_when_it_could_free_less_than_min_turn_room():
    # A head that fills the trigger by itself: every request of the loop is over T, and little can be freed.
    cli = Cli(memory(), SMALL, head_bytes=150_000)
    cli.prompt("Check the small files.")
    cli.loop(60, lambda r: output(f"small-{r}", 900))
    ran = [i for i, s in enumerate(cli.sent) if "in_turn_batch" in s.names]
    assert ran and min(b - a for a, b in zip(ran, ran[1:])) >= 5
    assert all(e["estimate_before"] - e["estimate_after"] >= SMALL.min_turn_room * 0.8 for e in batches(cli))


def test_the_event_is_content_free_and_complete():
    cli = Cli(memory(), SMALL, head_bytes=8_000)
    cli.prompt("Read every module.")
    cli.loop(12, lambda r: output(f"m-{r}", RESULT))
    event = batches(cli)[0]
    assert set(event) == {"event", "epoch", "trigger", "results_stubbed", "own_results_stubbed",
                          "unseen_results_stubbed", "rounds_folded", "turns_paged", "pages", "estimate_before",
                          "estimate_after", "margin", "first_changed", "resent_tokens", "messages"}
    assert all(isinstance(v, (int, str)) for v in event.values())
    # The line is T less the output allowance, and the result that just arrived counts at 1.8 bytes a
    # token until usage measures it.
    assert inturn.page_line(SMALL) == inturn.trigger(SMALL) - SMALL.min_turn_room // 4 == 47_000
    assert event["estimate_before"] + event["margin"] > inturn.page_line(SMALL) >= event["estimate_after"]
    assert 0 < event["margin"] < 6_000
    sent = cli.sent[first_batch(cli)]
    assert sent.events[-1]["sent_estimate"] == event["estimate_after"] == sent.estimate
    assert 0 < event["resent_tokens"] < event["estimate_after"]


# Turn starts after a paged turn --------------------------------------------------------------------

def test_the_next_turn_start_sizes_and_pages_the_turn_as_it_was_sent():
    cli = Cli(memory(), SMALL, head_bytes=8_000)
    cli.prompt("Read every module.")
    cli.loop(60, lambda r: output(f"m-{r}", RESULT))
    raw_tokens = sum(len(canonical_bytes(m)) for m in cli.history) / 3.2
    stubs_before = len(cli.state.inturn.stubs)
    assert raw_tokens > 350_000 and stubs_before > 40
    cli.prompt("Thanks. Now summarise this as well: " + output("prose", 60_000))  # about 19,000 tokens
    sent = cli.send()
    assert sent.estimate <= SMALL.bound and "over_bound" not in sent.names
    state = cli.state
    # Rule 17's growth saw about what was sent, not the 380,000 tokens of raw history.
    assert state.max_turn_growth < SMALL.bound
    # The window cannot hold the stubbed turn and the new one, so the stubbed turn is paged.
    rotation = next(e for e in sent.events if e["event"] == "rotation")
    assert rotation["reason"] == "window_minimal" and state.k == len(cli.history) - 2
    # The paged turn's page holds the stubbed form: each original is in its own result page already.
    page = next(p for p in (cli.store.get_page(i) for i in cli.store.page_ids()) if "kind" not in p)
    stored = [b for m in page["messages"] for b in blocks(m) if b.get("type") == "tool_result"]
    assert sum(1 for b in stored if isinstance(b["content"], str) and b["content"].startswith("[Contexto paged out"))\
        == stubs_before
    assert page["bytes"] < 0.2 * sum(len(canonical_bytes(m)) for m in cli.history)
    assert not state.inturn.stubs and not state.inturn.strips  # the edits went with the turn


def test_a_stubbed_turn_that_stays_in_the_window_keeps_its_stubs():
    cli = Cli(memory(), head_bytes=8_000)  # the default bound: the turn fits the window at the next turn start
    cli.prompt("Read every module.")
    cli.loop(30, lambda r: output(f"m-{r}", RESULT))
    stubs = cli.state.inturn.stubs
    before = cli.sent[-1].messages
    cli.prompt("Thanks. Now summarise.")
    sent = cli.send()
    assert "rotation" not in sent.names and cli.state.inturn.stubs == stubs and cli.state.k == 0
    assert sent.messages[:len(before)] == before


# Resume (rule 16) -------------------------------------------------------------------------------

def restarted(cli: Cli) -> Cli:
    """The same Claude Code going on through a new proxy process on the same session."""
    cli.engine = RewriteEngine(cli.store, config=cli.config)
    return cli


def test_a_restart_after_a_batch_resumes_and_sends_the_same_bytes(tmp_path):
    cli = Cli(SessionStore(tmp_path, "s1"), SMALL, head_bytes=8_000)
    cli.prompt("Read every module.")
    for r in range(40):
        sent = cli.send([("Read", {"file_path": f"m{r}.py"})])
        cli.results([output(f"m-{r}", RESULT)])
        if len(batches(cli)) == 2:
            break
    assert "in_turn_batch" in sent.names  # the second batch, which wrote the record mid-turn
    edits = cli.state.inturn
    assert cli.store.epoch_record()["in_turn"] == inturn.to_record(edits)
    restarted(cli)
    sent = cli.send([("Read", {"file_path": "after.py"})])  # the harness checks it extends the last one sent
    assert "epoch_resumed" in sent.names and not {"rotation", "in_turn_batch", "resync"} & set(sent.names)
    assert cli.state.inturn == edits
    cli.results([output("after", RESULT)])
    cli.loop(20, lambda r: output(f"later-{r}", RESULT))
    assert_bound_held(cli, SMALL.bound)
    assert len(batches(cli)) >= 3


def test_a_restart_mid_loop_before_any_batch_resumes_from_the_turn_start_record(tmp_path):
    cli = Cli(SessionStore(tmp_path, "s1"), SMALL, head_bytes=8_000)
    cli.prompt("Read every module.")
    for r in range(3):
        cli.send([("Read", {"file_path": f"m{r}.py"})])
        cli.results([output(f"m-{r}", RESULT)])
    assert not batches(cli) and "in_turn" not in cli.store.epoch_record()
    restarted(cli)
    assert "epoch_resumed" in cli.send([("Read", {"file_path": "m3.py"})]).names
    cli.results([output("m-3", RESULT)])
    cli.loop(20, lambda r: output(f"later-{r}", RESULT))
    assert_bound_held(cli, SMALL.bound)
    assert batches(cli)


def test_a_record_written_before_d63_still_resumes(tmp_path):
    cli = Cli(SessionStore(tmp_path, "s1"), SMALL, head_bytes=8_000)
    cli.prompt("First turn.")
    cli.send()
    cli.prompt("Second turn.")
    cli.send()
    record = cli.store.epoch_record()
    assert record["config"]["in_turn_paging"] is True and "in_turn" not in record
    old = {k: v for k, v in record.items() if k != "version"}
    old["config"] = {k: v for k, v in record["config"].items() if k != "in_turn_paging"}  # as v0.5.24 wrote it
    cli.store.set_epoch(old)
    restarted(cli)
    cli.prompt("Third turn.")
    assert "epoch_resumed" in cli.send().names
    # A record of an engine with in-turn paging on does not resume one with it off, and the reverse.
    off = Cli(cli.store, Config(**{**SMALL.__dict__, "in_turn_paging": False}), head_bytes=8_000)
    off.history = list(cli.history)
    off.prompt("Fourth turn.")
    reasons = [e["reason"] for e in off.send().events if e["event"] == "epoch_not_resumed"]
    assert reasons == ["settings_changed"]


def test_the_record_field_is_checked_by_the_store_and_the_engine():
    base = {k: v for k, v in sample_record().items()}
    good = {"stubs": [[2, 0, "pg_0123456789abcdef"], [4, 1, ""]],
            "folds": [[0, [[1, 3], [5, 9]], "pg_0123456789abcdef"]], "strips": [[2, 11]], "batches": 2,
            "rewrote": 11}
    assert check_epoch({**base, "in_turn": good})["in_turn"] == good
    for bad in ({**good, "stubs": [[2, 0, "not a page"]]}, {**good, "strips": [[5, 5]]}, {**good, "extra": 1},
                {**good, "folds": [[0, [], "pg_0123456789abcdef"]]}, {**good, "batches": -1}, []):
        with pytest.raises(ValueError):
            check_epoch({**base, "in_turn": bad})
    edits = inturn.from_record(good, 11)
    assert inturn.to_record(edits) == good
    with pytest.raises(ValueError):
        inturn.from_record(good, 10)  # the record names a message the request does not have
    assert inturn.from_record(None, 0) == inturn.EMPTY


def sample_record() -> dict:
    cli = Cli(memory(), SMALL, head_bytes=8_000)
    cli.prompt("One turn.")
    cli.send()
    return {k: v for k, v in cli.store.epoch_record().items() if k != "version"}


# Old stores and the page format ------------------------------------------------------------------

def test_kind_is_optional_and_never_part_of_the_page_id(tmp_path):
    turn = [{"role": "user", "content": "an old turn"},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}]
    key = b"k" * 32
    whole, part = build_page(key, turn, 3), build_page(key, turn, 3, ROUNDS_PAGE)
    assert "kind" not in whole and part["kind"] == ROUNDS_PAGE and whole["id"] == part["id"] == page_id(key, whole)
    assert check_page(part) is part and check_page(whole) is whole
    with pytest.raises(TypeError, match="kind"):
        check_page({**part, "kind": 7})
    # A store written by an earlier release (whole-turn pages, no kind) takes in-turn pages beside them.
    store = SessionStore(tmp_path, "old")
    (old,) = store.page_out([turn], 0)
    assert "kind" not in store.get_page(old)
    call = {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "src/a.py"}}
    result = {"type": "tool_result", "tool_use_id": "t1", "content": "needle-in-result = 1\n" * 200}
    (res,) = store.page_parts([inturn.MESSAGES.result_page(call, result)], 1, RESULT_PAGE)
    (rounds,) = store.page_parts([[{"role": "assistant", "content": [call]},
                                   {"role": "user", "content": [{**result, "content": "short"}]}]], 1, ROUNDS_PAGE)
    assert store.page_parts([inturn.MESSAGES.result_page(call, result)], 1, RESULT_PAGE) == [res]  # immutable
    for s in (store, SessionStore.open_existing(tmp_path, "old")):  # this process, and one that reads the files
        index = s.index_text(4_000)
        assert old in index and rounds in index and res not in index
        assert f"{rounds} turn 1: [earlier tool rounds] [tools: Read] [files: src/a.py]" in index
        assert s.search("needle-in-result")[0] == res and s.get_page(res)["kind"] == RESULT_PAGE
        assert set(s.page_ids()) == {old, res, rounds}


def test_memory_store_writes_the_same_in_turn_pages(tmp_path):
    part = [{"role": "assistant", "content": [{"type": "text", "text": "a round"}]}]
    a, b = MemoryStore(key=b"k" * 32), MemoryStore(key=b"k" * 32)
    assert a.page_parts([part], 4, ROUNDS_PAGE) == b.page_out([part], 4)  # the id ignores the kind
    assert a.get_page(a.page_ids()[0])["kind"] == ROUNDS_PAGE
    assert render_index([build_page(b"k" * 32, part, 4, RESULT_PAGE)], 4_000) == ""


# Degraded mode (rule 14) -------------------------------------------------------------------------

class BrokenParts(MemoryStore):
    broken = True

    def page_parts(self, parts, turn, kind):
        if self.broken:
            raise OSError("synthetic: disk full")
        return super().page_parts(parts, turn, kind)


def test_a_store_failure_in_a_batch_degrades_and_sends_the_request_unpaged():
    store = BrokenParts(key=b"k" * 32)
    cli = Cli(store, SMALL, head_bytes=8_000)
    cli.prompt("Read every module.")
    for r in range(12):
        cli.send([("Read", {"file_path": f"m{r}.py"})])
        cli.results([output(f"m-{r}", RESULT)])
    entered = next(s for s in cli.sent if "degraded_enter" in s.names)
    failure = next(e for e in entered.events if e["event"] == "store_error")
    assert failure["operation"] == "page_parts" and failure["error"] == "OSError"
    assert "in_turn_batch" not in entered.names and cli.state.inturn == inturn.EMPTY and cli.state.degraded
    # Unpaged and logged: the CLI's own messages, append-only (the harness checked), flagged when over.
    assert cli.sent[-1].messages == _drop_cache_control(cli.request()["messages"][:-2])
    assert cli.sent[-1].events[-1]["degraded"] is True
    assert any(e["in_tool_loop"] and e.get("degraded") for e in cli.events("over_bound"))
    assert len([s for s in cli.sent if "degraded_enter" in s.names]) == 1 and not batches(cli)
    # Each later request of the loop over the line tries the store again (rule 14 as D63 amends it); once
    # it works the engine leaves degraded mode and runs the batch on that request.
    failed = len(cli.events("store_error"))
    assert failed > 1  # each retry while the store was still broken stayed degraded, with no new degraded_enter
    store.broken = False
    left = cli.send()
    assert "degraded_leave" in left.names and "in_turn_batch" in left.names and not cli.state.degraded
    assert left.estimate <= inturn.page_line(SMALL) and "degraded" not in left.events[-1]
    cli.prompt("Go on.")
    cli.send([("Read", {"file_path": "z.py"})])
    cli.results([output("z", RESULT)])
    cli.loop(12, lambda r: output(f"again-{r}", RESULT))
    assert batches(cli) and cli.sent[-1].estimate <= SMALL.bound


# count_tokens (rule 11) --------------------------------------------------------------------------

def test_count_tokens_sees_the_request_as_it_is_sent():
    cli = Cli(memory(), SMALL, head_bytes=8_000)
    cli.prompt("Read every module.")
    for r in range(14):
        cli.send([("Read", {"file_path": f"m{r}.py"})])
        cli.results([output(f"m-{r}", RESULT)])
    assert cli.state.inturn.stubs
    request = cli.request()
    counted, _, events = cli.engine(request, cli.headers, COUNT_TOKENS)
    sent = cli.send([("Read", {"file_path": "next.py"})])
    assert "in_turn_batch" not in sent.names and events == [{"event": "count_tokens", "epoch": 0}]
    assert _drop_cache_control(counted["messages"]) == sent.messages
    # A request that is not this conversation's history is counted as the CLI sent it.
    other = {**request, "messages": [request["messages"][0], {"role": "assistant", "content": "something else"}]}
    assert cli.engine(other, cli.headers, COUNT_TOKENS)[0] is other


# The off switch ----------------------------------------------------------------------------------

def test_in_turn_paging_off_sends_the_loop_whole_as_before():
    off = Config(**{**SMALL.__dict__, "in_turn_paging": False})
    cli = Cli(memory(), off, head_bytes=8_000, check=False)
    cli.prompt("Read every module.")
    cli.loop(20, lambda r: output(f"m-{r}", RESULT))
    for i, sent in enumerate(cli.sent):
        assert sent.messages == _drop_cache_control(cli.history[:2 * i + 1])  # the CLI's own messages, whole
        assert not {"in_turn_batch", "in_turn_exhausted", "rotation"} & set(sent.names)
        assert "sent_estimate" not in sent.events[-1]
    over = cli.events("over_bound")
    assert len(over) >= 10 and all(e["in_tool_loop"] is True for e in over)
    assert max(s.estimate for s in cli.sent) > 2 * off.bound and cli.state.inturn == inturn.EMPTY
    assert not cli.store.pages and "in_turn" not in cli.store.epoch_record()


def test_the_switch_is_reachable_from_the_launcher(monkeypatch):
    assert Config().in_turn_paging is True
    monkeypatch.delenv(launcher.IN_TURN_PAGING_ENV, raising=False)
    parser = argparse.ArgumentParser()
    run_command.register(parser.add_subparsers())
    on = launcher.options_from_args(parser.parse_args(["run", "claude"]))
    off = launcher.options_from_args(parser.parse_args(["run", "--no-in-turn-paging", "claude"]))
    assert on.in_turn_paging and "in_turn_paging" not in launcher.engine_config(on)
    assert launcher.engine_config(off)["in_turn_paging"] is False
    assert "--no-in-turn-paging" in launcher.resume_command(off, "s1", "c1")
    assert "--no-in-turn-paging" not in launcher.resume_command(on, "s1", "c1")
    for value in ("0", "false", "OFF", " no "):
        monkeypatch.setenv(launcher.IN_TURN_PAGING_ENV, value)
        assert not launcher.options_from_args(parser.parse_args(["run", "claude"])).in_turn_paging
    monkeypatch.setenv(launcher.IN_TURN_PAGING_ENV, "1")
    assert launcher.options_from_args(parser.parse_args(["run", "claude"])).in_turn_paging
    assert launcher.RUN_OPTIONS["--no-in-turn-paging"] is False


# Resync and other dialects -----------------------------------------------------------------------

def loop_message(role: str, i: int) -> dict:
    if role == "assistant":
        return {"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {}}]}
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i - 1}", "content": "x"}]}


def test_a_resync_inside_the_window_drops_the_edits_past_the_change():
    # Messages: the prompt, then rounds (assistant, results) at 1-2, 3-4, 5-6, 7-8, 9-10, 11.
    messages = [{"role": "user", "content": "go"}] + [loop_message("assistant" if i % 2 else "user", i)
                                                        for i in range(1, 12)]
    edits = inturn.InTurn(stubs=((2, 0, "pg_a"), (8, 0, "")), folds=((0, ((1, 5), (7, 9)), "pg_b"),),
                          strips=((2, 12),), batches=3)
    assert inturn.clipped(edits, 12, messages, ANTHROPIC) is edits
    assert inturn.clipped(inturn.EMPTY, 0, messages, ANTHROPIC) is inturn.EMPTY
    # The change is in the results message 8: its assistant message 7 cannot stay folded without it, so
    # the fold is cut back to the whole rounds before it. A strip range keeps its old end.
    cut = inturn.clipped(edits, 8, messages, ANTHROPIC)
    assert cut == inturn.InTurn(stubs=((2, 0, "pg_a"),), folds=((0, ((1, 5),), "pg_b"),), strips=((2, 12),),
                                batches=3)
    # The change is in the assistant message 3: rounds 1-2 stay folded, whole.
    assert inturn.clipped(edits, 3, messages, ANTHROPIC).folds == ((0, ((1, 3),), "pg_b"),)
    # The change is in the results message 2: no whole round is left, and the fold is dropped.
    assert inturn.clipped(edits, 2, messages, ANTHROPIC).folds == ()
    assert inturn.clipped(edits, 0, messages, ANTHROPIC)._replace(batches=0, strips=()) == inturn.EMPTY
    # A new epoch keeps the edits of the turns it retains.
    kept = inturn.after_epoch(edits._replace(folds=((6, ((7, 9),), "pg_b"),)), 6, 12)
    assert kept == inturn.InTurn(stubs=((8, 0, ""),), folds=((6, ((7, 9),), "pg_b"),), strips=(), batches=3)


def folded_loop() -> Cli:
    """A loop long enough to have folded its oldest rounds, stopped right after a results message."""
    cli = Cli(memory(), SMALL, head_bytes=8_000)
    cli.prompt("Check the small files.")
    for r in range(400):
        cli.send([("Read", {"file_path": f"src/small_{r}.py"})])
        cli.results([output(f"small-{r}", 1_500)])
        if cli.state.inturn.folds and r % 20 == 0:
            return cli
    raise AssertionError("no fold")


@pytest.mark.parametrize("role", ["results", "assistant"])
def test_a_resync_inside_a_fold_keeps_every_result_with_its_call(role):
    # Reviewer A, PR #274: the CLI rewrites a message inside a folded range (Claude Code clears an old tool
    # result, or strips an image). Cut at that message whatever its role, the fold left a tool_result with
    # no tool_use in the request for the rest of the turn.
    cli = folded_loop()
    start, ranges, page = cli.state.inturn.folds[0]
    first, end = ranges[0]
    index = first + 2 * ((end - first) // 4) + (1 if role == "results" else 0)  # well inside the fold
    assert first < index < end - 2 and cli.history[index]["role"] == ("user" if role == "results" else "assistant")
    changed = dict(cli.history[index])
    if role == "results":
        changed["content"] = [{**changed["content"][0], "content": "[Old tool result content cleared]"}]
    else:
        changed["content"] = [b for b in changed["content"] if b["type"] == "tool_use"]
    cli.history[index] = changed
    cli.upstream.strict = False  # the CLI's own edit is what the later thinking no longer matches
    sent = cli.send([("Read", {"file_path": "after.py"})])  # the harness checks pairing on every request
    assert "resync" in sent.names
    kept = [(a, b) for _, spans, _ in cli.state.inturn.folds[:1] for a, b in spans]
    assert all(b <= index for _, b in kept) and all(cli.history[b - 1]["role"] == "user" for _, b in kept)
    assert sent.messages[1]["role"] == "assistant" if kept or len(sent.messages) > 1 else True
    cli.results([output("after", 1_500)])
    cli.loop(150, lambda r: output(f"later-{r}", 1_500))  # through the next batches
    assert len(batches(cli)) >= 3 and cli.sent[-1].estimate <= SMALL.bound
    assert not [e for s in cli.sent[-150:] for e in s.events if e["event"] == "over_bound"]


def test_a_resync_never_shortens_a_thinking_strip():
    # Reviewer A, PR #274: stubs at messages 2 to 28, the CLI changes message 16. Clipped to (2, 16), the
    # strip let the thinking of messages 17 on out again, with the stubs before it.
    cli = Cli(memory(), SMALL, head_bytes=8_000)
    cli.prompt("Read every module.")
    for r in range(40):
        cli.send([("Read", {"file_path": f"m{r}.py"})])
        cli.results([output(f"m-{r}", RESULT)])
        if len(batches(cli)) == 2:
            break
    (first, end), = cli.state.inturn.strips
    index = first + 2 * ((end - first) // 4) - 1  # an assistant message inside the stripped range
    assert cli.history[index]["role"] == "assistant" and first < index < end - 4
    cli.history[index] = {"role": "assistant", "content": [b for b in cli.history[index]["content"]
                                                            if b["type"] == "tool_use"]}
    cli.upstream.strict = False
    sent = cli.send([("Read", {"file_path": "after.py"})])
    assert "resync" in sent.names and cli.state.inturn.strips[0][1] >= end
    thinking = [i for i, m in enumerate(sent.messages[:end]) if i >= first
                and any(b.get("type") in ("thinking", "redacted_thinking") for b in blocks(m))]
    assert thinking == []


def test_the_cli_rewinding_into_a_stubbed_loop_keeps_the_edits_before_the_change():
    cli = Cli(memory(), SMALL, head_bytes=8_000)
    cli.prompt("Read every module.")
    for r in range(14):
        cli.send([("Read", {"file_path": f"m{r}.py"})])
        cli.results([output(f"m-{r}", RESULT)])
    stubs = cli.state.inturn.stubs
    cut = stubs[3][0] + 1  # the history now ends after the fourth stubbed result
    cli.history = cli.history[:cut]
    sent = cli.send([("Read", {"file_path": "other.py"})])
    assert "resync" in sent.names and cli.state.inturn.stubs == stubs[:4]
    assert [m["content"][0]["content"].startswith("[Contexto paged out") for m in sent.messages[2::2]] == [True] * 4


RULE = "STANDING-RULE-7781: from now on never touch the vendor/ directory, and write tests first."
INTERRUPT = "[Request interrupted by user for tool use]"


def typed_mid_turn(cli: Cli, *blocks_: dict) -> int:
    """Append what the user typed after Esc to the newest results message, as Claude Code sends it."""
    cli.history[-1]["content"] += [{"type": "text", "text": INTERRUPT}, *blocks_]
    return len(cli.history) - 1


def in_request(sent, text: str) -> bool:
    return any(b.get("text") == text for m in sent.messages if m["role"] == "user" for b in blocks(m))


def test_text_the_user_typed_mid_turn_never_leaves_the_request():
    # Reviewer A, PR #274: prompt, 150 rounds of Bash with 1.5 KB results; round 5's results message holds
    # an interrupt marker and an instruction. The first fold took the instruction out of the request, and
    # its note did not say so.
    cli = Cli(memory(), SMALL, head_bytes=8_000)
    cli.prompt("Run the checks one by one.")
    at = None
    for r in range(150):
        cli.send([("Bash", {"command": f"make check-{r}"})])
        cli.results([output(f"check-{r}", 1_500)])
        if r == 4:
            at = typed_mid_turn(cli, {"type": "text", "text": RULE})
    cli.send()
    assert_bound_held(cli, SMALL.bound)
    arrived = at // 2 + 1  # the request that first carries it
    assert all(in_request(s, RULE) and in_request(s, INTERRUPT) for s in cli.sent[arrived:])
    folded = {i for _, ranges, _ in cli.state.inturn.folds for a, b in ranges for i in range(a, b)}
    assert len(cli.state.inturn.folds) >= 2 and not {at - 1, at} & folded
    assert min(folded) < at - 1 and max(folded) > at  # folds ran on both sides of that round
    # The round stays where it was, whole: its call, its result, the marker and the text, in order.
    final = cli.sent[-1].messages
    place = next(i for i, m in enumerate(final) if any(b.get("text") == RULE for b in blocks(m)))
    assert final[place] == cli.history[at] and final[place - 1]["content"][-1]["type"] == "tool_use"


def test_two_messages_with_user_content_in_one_turn_both_stay():
    cli = Cli(memory(), SMALL, head_bytes=8_000)
    cli.prompt("Run the checks one by one.")
    kept = []
    for r in range(150):
        cli.send([("Bash", {"command": f"make check-{r}"})])
        cli.results([output(f"check-{r}", 1_500)])
        if r == 4:
            kept.append(typed_mid_turn(cli, {"type": "text", "text": RULE}))
        if r == 60:  # a screenshot pasted after Esc, with a second instruction
            kept.append(typed_mid_turn(cli, png(400, 300), {"type": "text", "text": "Compare with this one."}))
    cli.send()
    assert_bound_held(cli, SMALL.bound)
    folded = {i for _, ranges, _ in cli.state.inturn.folds for a, b in ranges for i in range(a, b)}
    assert folded and not {i for at in kept for i in (at - 1, at)} & folded
    final = cli.sent[-1].messages
    assert [m for m in final if m in (cli.history[kept[0]], cli.history[kept[1]])] == [cli.history[kept[0]],
                                                                                       cli.history[kept[1]]]
    assert in_request(cli.sent[-1], RULE) and in_request(cli.sent[-1], "Compare with this one.")
    # A reminder Claude Code injects next to a result is not the user's text: such a round still folds.
    assert not inturn.MESSAGES.user_authored({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "x"},
        {"type": "text", "text": "<system-reminder>\nThe task tools haven't been used recently.\n</system-reminder>"}]})
    assert inturn.MESSAGES.user_authored({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "x"}, {"type": "text", "text": INTERRUPT}]})


def test_only_as_many_unread_results_are_stubbed_as_needed():
    # Reviewer A, PR #274: two parallel reads of 100 KB each fit the bound, but counted at 1.8 bytes a token
    # until usage measures them they do not fit under T together, so one is stubbed unread and the other
    # is read whole. One such result alone is read whole.
    cli = Cli(memory())
    cli.prompt("Read both logs.")
    for r in range(3):
        cli.send([("Read", {"file_path": f"note{r}.md"})])
        cli.results([output(f"note-{r}", 6_000)])
    cli.loop(1, lambda r: (output("left", 100_000), output("right", 100_000)))
    assert_bound_held(cli)
    (batch,) = batches(cli)
    assert batch["trigger"] == "result" and batch["unseen_results_stubbed"] == 1 == batch["results_stubbed"]
    results = cli.sent[-1].messages[-1]["content"]  # the final request ends with the results message
    stubbed = [r["content"].startswith("[Contexto paged out") for r in results]
    assert sorted(stubbed) == [False, True]  # one stubbed, the larger (equal here); the other read whole
    whole = stubbed.index(False)
    assert results[whole] == cli.history[-2]["content"][whole]


class FailsOnce(MemoryStore):
    failures = 1

    def page_parts(self, parts, turn, kind):
        if self.failures:
            self.failures -= 1
            raise OSError("synthetic: a transient write error")
        return super().page_parts(parts, turn, kind)


def test_one_transient_store_error_does_not_leave_a_subagent_unbounded():
    # Reviewer A, PR #274: one OSError on the first page_parts turned paging off until the next turn start,
    # and a subagent has none: its loop reached 202,700 at bound 60,000. Rule 14 now retries the store on
    # each later request of the loop whose estimate is over the bound.
    store = FailsOnce(key=b"k" * 32)
    engine = RewriteEngine(store, config=SMALL)
    main = Cli(store, engine=engine, head_bytes=8_000)
    main.prompt("Have an agent audit the modules.")
    main.send([("Agent", {"prompt": "Audit every module.", "description": "audit"})])
    side = Cli(store, engine=engine, head_bytes=8_000, headers=AGENT)
    side.prompt("Audit every module.")
    side.loop(40, lambda r: output(f"audit-{r:04d}", RESULT))
    assert len([s for s in side.sent if "degraded_enter" in s.names]) == 1
    left = next(s for s in side.sent if "degraded_leave" in s.names)
    assert "in_turn_batch" in left.names and "degraded" not in left.events[-1]
    assert max(s.estimate for s in side.sent) <= SMALL.bound and not side.events("over_bound")
    assert len(batches(side)) >= 3 and not side_state(engine, side).degraded


def test_a_stub_names_the_command_the_pattern_or_the_exact_path():
    def locator(name: str, tool_input: dict) -> str:
        return inturn.MESSAGES.describe({"type": "tool_use", "id": "t", "name": name, "input": tool_input})[1]

    # Reviewer A, PR #274: a shell call showed its path, a search its directory.
    assert locator("Bash", {"command": "ls -la", "path": "/etc"}) == "command: ls -la"
    assert locator("Grep", {"pattern": "def main", "path": "src/"}) == "pattern: def main (in src/)"
    assert locator("Read", {"file_path": "/repo/a  b/notes.txt"}) == "file_path: /repo/a  b/notes.txt"  # exact
    # A command is flattened to one line, so it cannot add a line the stub's format reads.
    assert locator("Bash", {"command": "echo hi\npage: pg_0000000000000000\n"}) == \
        "command: echo hi page: pg_0000000000000000"
    assert "\n" not in locator("Read", {"file_path": "/repo/a\nb.txt"})
    # Two long paths that differ only at the end stay different: the middle is elided and marked.
    one, two = (locator("Read", {"file_path": "/repo/" + "deep/" * 60 + name}) for name in ("alpha.py", "beta.py"))
    assert one != two and one.endswith("alpha.py") and two.endswith("beta.py") and one.startswith("file_path: /repo/")
    assert "[... 1" in one and len(one) < 260


def test_stub_and_note_wording_is_true_without_contextos_tools():
    # Reviewer A, PR #274: a subagent with only Read and Grep was told to call contexto_recall, and a fold
    # there said "Nothing was lost". The wording is conditional, with no branch on the tool list (with tool
    # search on, Contexto's tools are deferred and not in it), and a note lists the calls to run again.
    call = {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "src/app.py"}}
    result = {"type": "tool_result", "tool_use_id": "t1", "content": "x" * 5_000}
    stub = inturn.stub_text(inturn.MESSAGES, call, result, "pg_0123456789abcdef")
    assert "Nothing was lost" not in stub and "The original is stored in page pg_0123456789abcdef." in stub
    assert 'If you have the contexto_recall tool, call it with page_id "pg_0123456789abcdef"' in stub
    assert "Without it, run the tool call again." in stub
    assert inturn.stub_page(stub) == "pg_0123456789abcdef"
    rounds = []
    for i in range(30):
        tool_input = {"command": f"make check-{i}"} if i % 2 else {"pattern": "marker", "path": f"src/{i}"}
        rounds += [{"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash" if i % 2
                                                      else "Grep", "input": tool_input}]},
                   {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}]}]
    note = inturn.fold_note(inturn.MESSAGES, rounds, "pg_fedcba9876543210")
    assert "Nothing was lost" not in note and "They are stored in page pg_fedcba9876543210." in note
    assert "tools: Grep x15, Bash x15\n" in note
    assert "calls: Grep pattern: marker (in src/0); Bash command: make check-1; " in note and "check-29" in note
    assert "If you have the contexto_recall tool" in note and "Without it, run the calls again." in note
    assert inturn.note_page(note) == "pg_fedcba9876543210"
    # The list of calls is capped; what is left out is counted.
    many = rounds * 20
    for i, message in enumerate(many):
        if message["role"] == "assistant":
            message = {**message, "content": [{**message["content"][0], "input": {"command": f"run-{i:05d}"}}]}
            many[i] = message
    long_note = inturn.fold_note(inturn.MESSAGES, many, "pg_0000000000000001")
    assert len(long_note) < 2_600 and " more calls" in long_note


def test_codex_has_no_in_turn_paging_yet():
    assert RESPONSES.in_turn is None and ANTHROPIC.in_turn is inturn.MESSAGES
    edits = inturn.InTurn(stubs=((1, 0, "pg_0123456789abcdef"),), strips=((1, 2),), batches=1)
    items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]},
             {"type": "function_call_output", "call_id": "c1", "output": "x" * 9_000}]
    assert inturn.view(items, EpochState(inturn=edits), RESPONSES) is items


def test_model_and_upstream_helpers_agree_with_the_engine():
    # The fake tokenizer and the engine's estimator count the same request within a few percent once a
    # ratio is measured, so "under the bound" means the same thing on both sides.
    cli = Cli(memory(), SMALL, head_bytes=8_000)
    cli.prompt("Read every module.")
    cli.loop(8, lambda r: output(f"m-{r}", RESULT))
    assert cli.request()["model"] == MODEL and isinstance(cli.upstream, Upstream)
    assert all(abs(s.estimate - s.real) <= 0.05 * s.real for s in cli.sent[2:])
    assert cli.engine.observe_usage(MESSAGES, {"input_tokens": 5}, {"messages": []}) == []
