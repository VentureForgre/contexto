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


def test_a_stub_keeps_the_blocks_keys_and_says_how_to_read_the_result_back():
    call = {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "pytest -q " + "x" * 400}}
    result = {"type": "tool_result", "tool_use_id": "toolu_1", "is_error": True,
              "cache_control": {"type": "ephemeral"},
              "content": [{"type": "text", "text": "line one\nline two\nliné three"}, png(10, 10)]}
    item = {"role": "user", "content": [result, {"type": "text", "text": "kept"}]}
    text = inturn.stub_text(inturn.MESSAGES, call, result, "pg_0123456789abcdef")
    stubbed = inturn.MESSAGES.with_stubs(item, {0: text})
    assert stubbed["content"][0] == {**result, "content": text} and stubbed["content"][1] == item["content"][1]
    assert "tool: Bash\n" in text and "command: pytest -q " + "x" * 190 + " ...\n" in text
    assert "was: 29 bytes, 3 lines, 1 image " in text and "page: pg_0123456789abcdef\n" in text
    assert 'contexto_recall with page_id "pg_0123456789abcdef"' in text and "contexto_search" in text
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


def test_unseen_results_are_stubbed_largest_first_when_the_request_is_over_the_bound():
    cli = Cli(memory(), SMALL, head_bytes=8_000)
    cli.prompt("Read four files at once.")
    sizes = (40_000, 70_000, 55_000, 60_000)  # 12,500 to 21,900 tokens: each fits alone, together they do not
    cli.loop(1, lambda r: tuple(output(f"par-{c}", size) for c, size in enumerate(sizes)))
    assert_bound_held(cli, SMALL.bound)
    (batch,) = batches(cli)
    assert batch["trigger"] == "bound" and batch["unseen_results_stubbed"] == batch["results_stubbed"]
    assert sorted(j for _, j, _ in cli.state.inturn.stubs) == [1, 3][:batch["results_stubbed"]] or \
        {j for _, j, _ in cli.state.inturn.stubs} >= {1}  # the largest first


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
                          "estimate_after", "first_changed", "resent_tokens", "messages"}
    assert all(isinstance(v, (int, str)) for v in event.values())
    assert event["estimate_before"] > inturn.trigger(SMALL) >= event["estimate_after"]
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
            "folds": [[0, [[1, 3], [5, 9]], "pg_0123456789abcdef"]], "strips": [[2, 11]], "batches": 2}
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
    # The next turn start tries the store once, leaves degraded mode, and in-turn paging works again.
    store.broken = False
    cli.send()
    cli.prompt("Go on.")
    assert "degraded_leave" in cli.send([("Read", {"file_path": "z.py"})]).names
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

def test_a_resync_inside_the_window_drops_the_edits_past_the_change():
    edits = inturn.InTurn(stubs=((2, 0, "pg_a"), (8, 0, "")), folds=((0, ((1, 5), (7, 9)), "pg_b"),),
                          strips=((2, 12),), batches=3)
    assert inturn.clipped(edits, 12) is edits and inturn.clipped(inturn.EMPTY, 0) is inturn.EMPTY
    cut = inturn.clipped(edits, 8)
    assert cut == inturn.InTurn(stubs=((2, 0, "pg_a"),), folds=((0, ((1, 5), (7, 8)), "pg_b"),), strips=((2, 8),),
                                batches=3)
    assert inturn.extent(cut) == 8 and inturn.clipped(edits, 0)._replace(batches=0) == inturn.EMPTY
    # A new epoch keeps the edits of the turns it retains.
    kept = inturn.after_epoch(edits._replace(folds=((6, ((7, 9),), "pg_b"),)), 6, 12)
    assert kept == inturn.InTurn(stubs=((8, 0, ""),), folds=((6, ((7, 9),), "pg_b"),), strips=(), batches=3)


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
