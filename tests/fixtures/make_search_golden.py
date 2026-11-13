"""Record what strict search returns on the corpora this repository has (D65).

``search_golden.json`` was written by this script at main b0e2dbc (v0.5.24), before search had any
fallback. For every query it holds the ranked ids of ``store.search`` and the full ``contexto_search``
reply. ``tests/test_search_golden.py`` builds the same corpora and checks that every query with a
strict hit (a paged page, or a line of the conversation in view) still gives the recorded ids and
reply, byte for byte. Queries with no strict hit are recorded too: those are the only replies a
fallback may change.

The corpora, all synthetic, built in memory with a fixed page key so page ids repeat:

- ``scale17``: the store-scale corpus (``spikes/m2/scale/store``) at 17 pages, with every probe query
  of ``scale_probes`` and its ``LATENCY_QUERIES``.
- ``acceptance``: the acceptance session script (``spikes/m2/acceptance/script.py``): its first 16
  content turns paged out, the last 4 in view, with each probe's search terms (``reach.key_terms``)
  and each probe's question as a query.
- ``small``: pages shaped like the ones the search tests build (words in any case and order,
  identifiers, Greek, a tool input, a paged reminder, a Codex turn, a Contexto search echo, a value
  that changes), with a conversation in view.
- ``many``: 60 short pages that share words, so a reply lists further pages and cuts the list.

Rewrite the file only when strict search is changed on purpose:

    PYTHONPATH=src python tests/fixtures/make_search_golden.py
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
for extra in (ROOT / "spikes" / "m2" / "scale" / "store", ROOT / "spikes" / "m2"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import scale_synth  # noqa: E402
from acceptance import reach, script  # noqa: E402
from scale_probes import LATENCY_QUERIES, all_queries  # noqa: E402

from contexto import mcp  # noqa: E402
from contexto.store.memory import MemoryStore  # noqa: E402

OUT = Path(__file__).with_name("search_golden.json")
KEY = b"search-golden-page-key-012345678"  # fixed, so page ids are the same on every run
SCALE_PAGES = 17  # the smallest size of the store-scale spike
ACCEPTANCE_PAGED = 16  # of the script's 20 content turns; the rest are in view
ROOM = 10**9  # a room that never runs out: the per-call limit binds
FILLER = "\n".join(f"step {i}: ran the build and checked the output, nothing unusual" for i in range(40))


def _store(window: bool = True) -> MemoryStore:
    store = MemoryStore("golden", key=KEY)
    if window:
        store.set_window(110_000, 2.1, model="claude-sonnet-5")
    return store


def _in_view(store: MemoryStore, entries: list[dict[str, Any]]) -> None:
    """One request as the engine drives it: the snapshot of the window in view, then the room."""
    store.set_retained(1, 0, entries)
    store.set_turn_room("golden", ROOM)


def _entry(turn: int, index: int, text: str, role: str = "user") -> dict[str, Any]:
    return {"turn": turn, "message_index": index, "role": role, "text": text}


def _page(text: str, prompt: str = "continue") -> list[dict[str, Any]]:
    return [{"role": "user", "content": prompt},
            {"role": "assistant", "content": [{"type": "text", "text": text}]}]


def scale_corpus() -> tuple[MemoryStore, list[str]]:
    store = _store()
    for t in range(SCALE_PAGES):
        store.page_out([scale_synth.session_turn(t, SCALE_PAGES)], t)
    _in_view(store, [_entry(10**6, 0, "Continue with the next step."),
                     _entry(10**6, 1, "\n".join(scale_synth.pools()["prose"][:20]), "assistant")])
    queries = all_queries() + [q for qs in LATENCY_QUERIES.values() for q in qs]
    return store, list(dict.fromkeys(queries))


def acceptance_corpus() -> tuple[MemoryStore, list[str]]:
    built = script.build()
    store = _store()
    turns = built.content_turns
    for t, text in enumerate(turns[:ACCEPTANCE_PAGED]):
        store.page_out([[{"role": "user", "content": text},
                         {"role": "assistant", "content": [{"type": "text", "text": "OK"}]}]], t)
    entries = []
    for t, text in enumerate(turns[ACCEPTANCE_PAGED:], start=ACCEPTANCE_PAGED + 1):
        entries += [_entry(t, 0, text), _entry(t, 1, "OK", "assistant")]
    entries.append(_entry(len(turns) + 1, 0, built.probe_texts[0]))  # the current question: not searched
    _in_view(store, entries)
    queries = []
    for probe in built.probes:
        queries += reach.key_terms(probe)
        queries.append(probe.question)
    return store, list(dict.fromkeys(queries))


def _reminder(body: str) -> list[dict[str, Any]]:
    text = f"<system-reminder>\n{body}\n</system-reminder>\n"
    return [{"role": "user", "content": [{"type": "text", "text": text}]}]


def small_corpus() -> tuple[MemoryStore, list[str]]:
    store = _store()
    turns: list[list[dict[str, Any]]] = [
        _page(FILLER + "\nNightly backups run at 02:00 from the replica."),
        _page(FILLER + "\nerror E4127 in src/contexto/mcp.py:412 (ticket SUP-48213)\nHTTP_TIMEOUT_S = 30"),
        _page("the street sign says ΟΔΟΣ ΑΘΗΝΑΣ and the note says οδός"),
        _page("the label reads ΑΣΑ"),
        [{"role": "user", "content": "find where the retry budget is read"},
         {"role": "assistant", "content": [
             {"type": "text", "text": "Looking for it."},
             {"type": "tool_use", "id": "toolu_01", "name": "Bash",
              "input": {"command": 'grep -n "retry_budget" src/app.py'}}]},
         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_01", "content": (
             "41:    retry_budget = settings.retryBudget\n42:    return retry_budget")}]},
         {"role": "assistant", "content": [{"type": "text", "text": "It is read on line 41 of src/app.py."}]}],
        _reminder("The file src/app.py changed: retry_budget is 4. Take it as the current state."),
        [{"type": "message", "role": "user",
          "content": [{"type": "input_text", "text": ("cfg checkout-api.retention_days=juniper-2573\n"
                                                      "Reply with exactly: OK")}]},
         {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OK"}]}],
        _page("The login flow stores the refreshToken in the keychain and rotates it every 30 days.",
              "how does login keep the session"),
        _page("Set max_retries to 3 in the worker config.", "configure the worker"),
        _page(FILLER + "\nthe worker read max_retries from the config and started"),
        _page("\n".join(f"build line {i}: compiled module {i} without warnings" for i in range(900))
              + "\nmax_retries is now 7 after the incident review."),
        [{"role": "user", "content": "what did we set for retries"},
         {"role": "assistant", "content": [
             {"type": "tool_use", "id": "toolu_02", "name": "mcp__contexto__contexto_search",
              "input": {"query": "max_retries"}}]},
         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_02", "content": (
             "pg_0000000000000000 (turn 8)\n> 30: Set max_retries to 3 in the worker config.")}]},
         {"role": "assistant", "content": [{"type": "text", "text": ("The search shows an older value; the newest "
                                                                     "one is 7.")}]}],
        _page("The staging database listens on port 6543 and production on 5432.",
              "which ports do the databases use"),
        _page("Deploys go out on Tuesday after the freeze ends.", "when do we ship"),
        _page(FILLER + "\na passing mention of max_retries in the review notes"),
    ]
    for t, turn in enumerate(turns):
        store.page_out([turn], t)
    _in_view(store, [_entry(len(turns) + 1, 0, "intro\nretry_budget is 5 now\nmore"),
                     _entry(len(turns) + 2, 1, "the canary port moved to 7001", "assistant"),
                     _entry(len(turns) + 3, 0, "what changed?")])
    queries = [
        "nightly backup", "NIGHTLY BACKUPS", "backup nightly", "replica 02:00 nightly", "nightly restore",
        "E4127", "src/contexto/mcp.py", "TIMEOUT", "SUP-48213", "timeout_s = 30", "mcp.py:412",
        "Σ", "ΑΣ", "ΟΔΟΣ", "οδος",
        "retry_budget", 'grep -n "retry_budget"', "retry_budget is 5", "retrybudget", "retry-budget",
        "retention_days", "retentionDays", "juniper-2573",
        "max_retries", "max_retries is now", "MAX_RETRIES 7", "maxRetries",
        "refreshToken", "refresh_token", "refresh-token", "refresh.token",
        "staging database port", "what port is the staging database on", "staging databse port",
        "canary port", "port", "canary", "the", "when do deploys go out", "deploys tuesday freeze",
        "deploys tuesday rollback", "nightly bakcups", "zzqqxx", "zzqqxx nightly",
        "which ports do the databases use", "build", "line", "step 39", "x", "02:00", "é", "\ud83d",
        " ".join(f"w{i}" for i in range(120)), "what is the", "worker config max_retries retention_days",
    ]
    return store, queries


def many_corpus() -> tuple[MemoryStore, list[str]]:
    store = _store(window=False)  # no window recorded and no snapshot: the default limit, in view not checked
    for t in range(60):
        text = f"step {t}: deploy to region r{t % 9} finished in {30 + t} seconds"
        if t % 7 == 0:
            text += "\nthe rollout was paused for a canary check"
        store.page_out([_page(text, f"deploy number {t}")], t)
    return store, ["deploy", "deploy finished", "deploy region r7", "canary", "deploy rollback", "seconds 45",
                   "rollout paused", "rolout paused", "deploy number 59", "region"]


CORPORA = {"scale17": scale_corpus, "acceptance": acceptance_corpus, "small": small_corpus, "many": many_corpus}


def search_reply(store: Any, query: str) -> dict[str, Any]:
    """The ``contexto_search`` result for ``query``: its text blocks and whether it is a tool error."""
    server = mcp.Server(store, io.StringIO(), io.StringIO())
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "contexto_search", "arguments": {"query": query}}})
    result = server.handle_line(line)["result"]
    return {"texts": [block["text"] for block in result["content"]], "error": result["isError"]}


def record(store: Any, queries: list[str]) -> list[dict[str, Any]]:
    rows = []
    for query in queries:
        ids = store.search(query)
        reply = search_reply(store, query)
        rows.append({"query": query, "ids": ids, "reply": reply["texts"], "error": reply["error"],
                     # a strict hit: a paged page, or a line of the conversation in view
                     "strict": bool(ids) or (not reply["error"] and reply["texts"][0] != mcp.NO_HIT)})
    return rows


def main() -> None:
    golden = {name: record(*build()) for name, build in CORPORA.items()}
    OUT.write_text(json.dumps(golden, ensure_ascii=True, indent=0, separators=(",", ":")) + "\n", encoding="utf-8")
    for name, rows in golden.items():
        strict = sum(row["strict"] for row in rows)
        print(f"{name}: {len(rows)} queries, {strict} with a strict hit, {len(rows) - strict} without")
    print(f"{OUT.name}: {OUT.stat().st_size} bytes")


if __name__ == "__main__":
    main()
