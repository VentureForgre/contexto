import pytest

from contexto.store import SessionStore
from contexto.store.memory import MemoryStore
from contexto.store.pages import EMPTY_SYSTEM_STUB, build_page

KEY = bytes(range(32))

NORMAL = [
    {"role": "user", "content": [{"type": "text", "text": "fix the parser in src/p.py"}]},
    {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read",
                                       "input": {"file_path": "src/p.py"}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
]
MIXED = [{"role": "system", "content": "Effort changed to high"}, {"role": "user", "content": "carry on"}]
# No tool_result, so the page's token estimate, and with it the id, does not depend on the tool seed.
TOOL_FREE = NORMAL[:2]


def test_pages_with_user_text_keep_their_ids():
    # Golden ids computed with the pre-B6 pages.py on main (1b2b262) and the same key. Both turns are
    # free of tool_result content, so the ids hold across the 2.0 to 1.5 tool seed change (B7).
    assert build_page(KEY, TOOL_FREE, 3)["id"] == "pg_c47cb9a1f16fcb53"
    assert build_page(KEY, MIXED, 3)["id"] == "pg_54695d1e9037b3d1"
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


def test_a_tool_result_page_records_the_1_5_seed_estimate():
    # The page body hashes its token estimate, so a page holding tool_result content has a different
    # id under a different seed; existing page files keep the ids they were written with.
    from contexto.tokens import estimate_message_tokens

    page = build_page(KEY, NORMAL, 3)
    assert page["tokens"] == estimate_message_tokens(NORMAL)
    assert page["id"] == "pg_b3934632e94cc207"  # was pg_4fd02b9dd10fc4da with the 2.0 seed
