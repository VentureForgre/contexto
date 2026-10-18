import pytest

from contexto.store import SessionStore
from contexto.store.memory import MemoryStore
from contexto.store.pages import EMPTY_REMINDER_STUB, EMPTY_SYSTEM_STUB, build_page

KEY = bytes(range(32))

NORMAL = [
    {"role": "user", "content": [{"type": "text", "text": "fix the parser in src/p.py"}]},
    {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read",
                                       "input": {"file_path": "src/p.py"}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
]
MIXED = [{"role": "system", "content": "Effort changed to high"}, {"role": "user", "content": "carry on"}]


def test_the_stub_rule_does_not_reach_page_ids():
    # B6 added system_text to the stub; ids hash only {turn, messages} (B8), so stubs never move ids.
    assert build_page(KEY, NORMAL, 3)["id"] == "pg_ddfe6e1db2f05ad6"
    assert "system_text" not in build_page(KEY, NORMAL, 3)["stub"]
    assert "system_text" not in build_page(KEY, MIXED, 3)["stub"]


@pytest.fixture(params=["session", "memory"])
def store(request, tmp_path):
    return SessionStore(tmp_path, "s1") if request.param == "session" else MemoryStore("s1")


def test_a_system_only_page_shows_its_first_line_in_the_index(store):
    [pid] = store.page_out([[{"role": "system", "content": "\n  Tool added: grep_files\nschema follows"}]], 4)
    assert store.get_page(pid)["stub"]["system_text"] == "Tool added: grep_files"
    assert store.index_text(4000) == f"{pid} turn 4: [system] Tool added: grep_files"


def test_system_text_in_blocks_and_the_200_character_limit(store):
    long = "S" * 250
    [pid] = store.page_out([[{"role": "system", "content": [{"type": "text", "text": long}]}]], 1)
    assert store.get_page(pid)["stub"]["system_text"] == "S" * 200


def test_a_mixed_turn_keeps_the_user_text_stub(store):
    [pid] = store.page_out([MIXED], 2)
    assert store.index_text(4000) == f"{pid} turn 2: carry on"


@pytest.mark.parametrize("content", ["", "   \n\t", [], [{"type": "text", "text": ""}]])
def test_an_empty_system_message_gets_a_clear_placeholder(store, content):
    [pid] = store.page_out([[{"role": "system", "content": content}]], 5)
    assert store.get_page(pid)["stub"]["system_text"] == EMPTY_SYSTEM_STUB
    assert store.index_text(4000) == f"{pid} turn 5: [system] {EMPTY_SYSTEM_STUB}"


def test_search_finds_system_only_pages(store):
    [pid] = store.page_out([[{"role": "system", "content": "Context window note: grep_files"}]], 1)
    assert store.search("grep_files") == [pid]


def test_a_tool_result_page_records_the_seed_estimate_without_it_reaching_the_id():
    from contexto.tokens import estimate_message_tokens

    page = build_page(KEY, NORMAL, 3)
    assert page["tokens"] == estimate_message_tokens(NORMAL)
    assert page["id"] == "pg_ddfe6e1db2f05ad6"


COUNTDOWN = {"type": "text", "text": "<system-reminder>\n<total_tokens>15000000 tokens left</total_tokens>\n</system-reminder>\n"}
PLAN = {"type": "text", "text": "<system-reminder>\nPlan mode is active. The user indicated that they do not want you to "
                                "execute yet -- you MUST NOT make any edits.\nmore\n</system-reminder>\n"}


def test_a_paged_context_block_page_is_listed_as_a_reminder_not_as_raw_tagged_text(store):
    # D32 pages a context block as a user message holding that one block. Its index line must not
    # start with "<system-reminder>": in the preamble that reads as a live instruction.
    [pid] = store.page_out([[{"role": "user", "content": [PLAN]}]], 7)
    stub = store.get_page(pid)["stub"]
    assert stub["user_text"] == ""
    assert stub["reminder_text"].startswith("Plan mode is active.")
    line = store.index_text(4000)
    assert line.startswith(f"{pid} turn 7: [reminder] Plan mode is active.")
    assert "<system-reminder>" not in line
    [cid] = store.page_out([[{"role": "user", "content": [COUNTDOWN]}]], 8)
    assert store.get_page(cid)["stub"]["reminder_text"] == "<total_tokens>15000000 tokens left</total_tokens>"
    assert store.search("Plan mode is active") == [pid]


@pytest.mark.parametrize("content", [
    [{"type": "text", "text": "do the thing"}],
    [COUNTDOWN, {"type": "text", "text": "do the thing"}],
    "do the thing",
])
def test_a_user_message_with_its_own_text_keeps_the_user_text_stub(store, content):
    [pid] = store.page_out([[{"role": "user", "content": content}]], 3)
    stub = store.get_page(pid)["stub"]
    assert "reminder_text" not in stub
    assert "do the thing" in stub["user_text"]


def test_an_empty_reminder_gets_a_placeholder_and_a_tagged_system_message_shows_its_inner_line(store):
    [pid] = store.page_out([[{"role": "user", "content": [{"type": "text", "text": "<system-reminder>\n\n</system-reminder>"}]}]], 2)
    assert store.get_page(pid)["stub"]["reminder_text"] == EMPTY_REMINDER_STUB
    # claude-sonnet-5 and claude-fable-5 wrap their system text in the tag: the index shows the line inside.
    [sid] = store.page_out([[{"role": "system", "content": "<system-reminder>\n<total_tokens>9 tokens left</total_tokens>\n</system-reminder>"}]], 9)
    assert store.get_page(sid)["stub"]["system_text"] == "<total_tokens>9 tokens left</total_tokens>"


def test_the_reminder_stub_does_not_move_the_page_id():
    turn = [{"role": "user", "content": [COUNTDOWN]}]
    from contexto.store.pages import page_id

    page = build_page(KEY, turn, 4)
    assert page["id"] == page_id(KEY, {"turn": 4, "messages": turn})
