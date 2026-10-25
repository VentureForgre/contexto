"""Carried content stays bounded (decisions D32 and D33).

Request shapes follow Claude Code 2.1.302 as captured with zero spend in spikes/m2/scale/reminders
(48-turn sessions against a scripted loopback model): on 4.x model ids every later turn-start user
message carries ``<system-reminder>`` context blocks (the token countdown, the plan-mode entry and
exit, file-changed notes, the task-tools reminder); on 5.x ids ``messages[1]`` is the session's
environment message (``# Environment`` first line; with ``output_config`` on claude-opus-5-5 and
claude-haiku-5-5, wrapped in ``<system-reminder>`` and without it on claude-sonnet-5), per-turn
reminders arrive as role ``system`` messages, and an ``--effort`` change attaches ``output_config`` to
that turn's first system message. First lines, block order and sizes match the captures; the rest of
the text is synthetic.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from contexto.canonical import canonical_bytes
from contexto.rewrite import (
    SESSION_START_SIGNATURE_CLAUDE_CODE_VERSION,
    Config,
    EpochState,
    RewriteEngine,
    carried_content,
    carried_systems,
    carried_tokens,
    classify_system,
    context_block_kind,
    message_hash,
    message_text,
    page_carried_text,
    turn_starts,
)
from contexto.store.memory import MemoryStore
from contexto.tokens import TokenEstimator
from test_epoch_resume import EpochStore

MESSAGES = "/v1/messages"
FILL = "synthetic filler text " * 90  # about 2,000 bytes


# Claude Code 2.1.302 shapes ---------------------------------------------------------------------

def rem(body: str) -> dict:
    """A context block as 4.x ids send it in a turn-start message."""
    return {"type": "text", "text": f"<system-reminder>\n{body}\n</system-reminder>\n"}


ENVIRONMENT = ("# Environment\nYou have been invoked in the following environment: \n"
               " - Primary working directory: /tmp/synthetic/project\n - Is a git repository: false\n"
               + " - synthetic environment line\n" * 300)  # the 5.x message is 9.2 to 9.7 KB
CLAUDE_MD = ("Codebase and user instructions are shown below. Be sure to adhere to these instructions.\n\n"
             "Contents of /tmp/synthetic/project/CLAUDE.md (project instructions):\n\n"
             + "- synthetic project rule\n" * 40)
ATTRIBUTION = "Attribution for git commits and pull requests you create from here on: synthetic.\n" * 6
PLAN_ENTRY = ("Plan mode is active. The user indicated that they do not want you to execute yet -- you MUST "
              "NOT make any edits.\n" + "synthetic plan-mode instruction line\n" * 130)  # about 4,970 bytes
PLAN_EXIT = "## Exited Plan Mode\n\nYou have exited plan mode. You can now make edits, run tools, and take actions."
TASKS = ("The task tools haven't been used recently. If you're working on tasks that would benefit from "
         "tracking progress, consider using TaskCreate.\n" + "synthetic task line\n" * 20)


def countdown(t: int) -> str:
    return f"<total_tokens>{15_000_000 - 37 * t} tokens left</total_tokens>"


def file_note(t: int) -> str:
    return (f"Note: /tmp/synthetic/project/out/f{t}.txt changed on disk since you last read it. That's "
            "usually deliberate, so take it as the current state.\n" + f"{t}\tsynthetic changed line\n" * 12)


def m0_blocks_4x() -> list[dict]:
    """messages[0] on 4.x ids: environment, model, agents, MCP instructions, skills, countdown,
    CLAUDE.md, date, attribution (2.1.302 order)."""
    return [rem(ENVIRONMENT[:700]), rem("You are powered by the model named Sonnet 4.6. Synthetic."),
            rem("Available agent types for the Agent tool:\n" + "- synthetic agent\n" * 90),
            rem("# MCP Server Instructions\n\nsynthetic MCP instructions"),
            rem("The following skills are available for use with the Skill tool:\n" + "- synthetic skill\n" * 300),
            rem(countdown(0)), rem(CLAUDE_MD), rem("Today's date is 2026-10-17."), rem(ATTRIBUTION)]


def reminders_4x(t: int) -> list[str]:
    """Later turn-start reminders on a 4.x id, in the busy 12-turn cycle of the reminders spike."""
    c = (t - 1) % 12 + 1
    out = []
    if t % 10 == 5:
        out.append(TASKS)
    if c in (4, 8):
        out.append(file_note(t))
    if c == 9:
        out.append(PLAN_ENTRY)
    if c == 11:
        out += [file_note(t), PLAN_EXIT]
    return [*out, countdown(t)]


def turn_4x(t: int) -> list[dict]:
    blocks = m0_blocks_4x() if t == 1 else [rem(r) for r in reminders_4x(t)]
    prompt = {"type": "text", "text": f"Turn {t}. Synthetic user request number {t}. {FILL}"}
    return [{"role": "user", "content": [*blocks, prompt]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": f"toolu_{t}", "name": "Read",
                                               "input": {"file_path": "/tmp/synthetic/project/data.txt"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"toolu_{t}",
                                          "content": f"synthetic output {t} {FILL}\n\n" + rem(countdown(t))["text"]}]},
            {"role": "assistant", "content": [{"type": "text", "text": f"ok {t} {FILL}"}]}]


def session_4x(turns: int) -> list[dict]:
    return [m for t in range(1, turns + 1) for m in turn_4x(t)]


EFFORT = {6: "high", 7: "medium", 8: "low", 9: "medium"}  # the spike's effort schedule per 12-turn cycle


def system_5x(text: str, effort: str | None = None, tagged: bool = False) -> dict:
    if tagged:
        text = f"<system-reminder>\n{text}\n</system-reminder>"
    m = {"role": "system", "content": text}
    if effort:
        m["output_config"] = {"effort": effort}
    return m


def turn_5x(t: int, opus: bool = True) -> list[dict]:
    """One 5.x turn. On claude-opus-5-5 an effort change rides on the turn's first system message;
    claude-sonnet-5 keeps effort at the top level and tags its system text."""
    c = (t - 1) % 12 + 1
    tagged = not opus
    if t == 1:
        start = [{"role": "user", "content": [rem(CLAUDE_MD), rem(ATTRIBUTION),
                                              {"type": "text",
                                               "text": f"Turn 1. Synthetic user request number 1. {FILL}"}]},
                 system_5x(ENVIRONMENT, "medium" if opus else None, tagged)]
    else:
        items = []
        if c in (4, 8):
            items.append(file_note(t))
        if c == 9:
            items.append(PLAN_ENTRY)
        if c == 11:
            items.append(PLAN_EXIT)
        items.append(countdown(t))
        effort = EFFORT.get(c) if opus else None
        start = [{"role": "user", "content": f"Turn {t}. Synthetic user request number {t}. {FILL}"},
                 system_5x("\n\n".join(items), effort, tagged)]
    return [*start,
            {"role": "assistant", "content": [{"type": "tool_use", "id": f"toolu_{t}", "name": "Read",
                                               "input": {"file_path": "/tmp/synthetic/project/data.txt"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"toolu_{t}",
                                          "content": f"synthetic output {t} {FILL}"}]},
            system_5x(countdown(t) + " ", None, tagged),
            {"role": "assistant", "content": [{"type": "text", "text": f"ok {t} {FILL}"}]}]


def session_5x(turns: int, opus: bool = True) -> list[dict]:
    return [m for t in range(1, turns + 1) for m in turn_5x(t, opus)]


# Small enough that 5.x sessions rotate and engage the fallback within 40 turns (restart tests, and
# the v0.2.8 record in fixtures/, which tests/fixtures/make_v028_epoch_record.py wrote with it).
RESUME_CONFIG = dict(bound=60_000, headroom_floor=2_000, pin_budget=200, index_budget=200, min_turn_room=2_000,
                     carried_text_budget=1_500, carried_budget=1_000_000)


def request(messages: list[dict]) -> dict:
    return {"model": "claude-opus-5-5", "max_tokens": 64, "stream": True,
            "system": [{"type": "text", "text": "synthetic head", "cache_control": {"type": "ephemeral"}}],
            "tools": [{"name": "Read", "input_schema": {"type": "object"}}], "messages": messages}


def names(events: list[dict]) -> list[str]:
    return [e["event"] for e in events]


def fallback_view(messages: list[dict], budget: int, upto: int | None = None):
    """The fallback applied at every turn start, as the reminders spike's analyze.py does: with every
    turn before the current one paged, page_carried_text runs on M[0..k) and its paged set is kept.
    Yields (turn, k, carried tokens after the fallback, paged set)."""
    est, store = TokenEstimator(), MemoryStore(key=b"k" * 32)
    cfg = Config(carried_text_budget=budget)
    starts = turn_starts(messages)
    paged, initial = frozenset(), None
    for t, k in enumerate(starts[1:upto], start=2):
        sub = messages[:k]
        if initial is None:
            initial = carried_tokens(messages[:starts[1]], est)
        state = EpochState(k=k, initial_carried=initial, paged_carried=paged)
        paged = page_carried_text(sub, state, store, cfg, est, []).paged_carried
        yield t, k, carried_tokens(sub, est, paged), paged, store


def last(iterable):
    item = None
    for item in iterable:  # noqa: B007 - the last value bound is the result
        pass
    return item


# D33: what makes a system message session-start -------------------------------------------------

def test_the_environment_message_is_session_start_with_or_without_output_config():
    opus = session_5x(2, opus=True)
    sonnet = session_5x(2, opus=False)
    assert "output_config" in opus[1] and "output_config" not in sonnet[1]
    assert classify_system(opus, 1) == "session_start"
    assert classify_system(sonnet, 1) == "session_start"  # tagged, no settings fields
    assert SESSION_START_SIGNATURE_CLAUDE_CODE_VERSION == "2.1.310"


# D45: a subagent's environment message with tool search on (2.1.310 by default), captured with zero
# spend (spikes/m2/subagents); trimmed after the heading's first line.
DEFERRED_LISTING = ("The following deferred tools are now available via ToolSearch. Their schemas are NOT loaded "
                    "— calling them directly will fail with InputValidationError. Use ToolSearch with query "
                    "\"select:<name>[,<name>...]\" to load tool schemas before calling them:\nCronCreate\nCronDelete\n"
                    "CronList\nEnterWorktree\nExitWorktree\nMonitor\nNotebookEdit\nSendMessage\nTaskStop\nWebFetch\n"
                    "WebSearch\n")
SUBAGENT_ENVIRONMENT = DEFERRED_LISTING + "\n# Environment\nYou have been invoked in the following environment: \n"


def test_a_subagent_environment_message_led_by_the_deferred_tool_listing_is_session_start():
    sub = [{"role": "user", "content": [{"type": "text", "text": "SUB-FG task"}]},
           {"role": "system", "content": [{"type": "text", "text": SUBAGENT_ENVIRONMENT}]}]
    assert classify_system(sub, 1) == "session_start"
    crlf = SUBAGENT_ENVIRONMENT.replace("\n", "\r\n")
    assert classify_system([sub[0], {"role": "system", "content": crlf}], 1) == "session_start"
    tagged = f"<system-reminder>\n{SUBAGENT_ENVIRONMENT}</system-reminder>"
    assert classify_system([sub[0], {"role": "system", "content": tagged}], 1) == "session_start"


def test_a_deferred_tool_listing_without_the_environment_heading_is_not_session_start():
    # An MCP server that connects mid-session: the listing alone, as a later role "system" message.
    alone = [{"role": "user", "content": "hi"}, {"role": "system", "content": DEFERRED_LISTING + "mcp__probe__x\n"}]
    assert classify_system(alone, 1) == "carried_text"
    # The heading must follow the listing's blank line, not sit inside other text.
    inside = DEFERRED_LISTING + "\nSome other notice.\n# Environment\n"
    assert classify_system([alone[0], {"role": "system", "content": inside}], 1) == "carried_text"
    no_blank = DEFERRED_LISTING + "# Environment\n"
    assert classify_system([alone[0], {"role": "system", "content": no_blank}], 1) == "carried_text"


def test_an_effort_change_on_a_reminder_message_is_a_settings_change_not_session_start():
    msgs = session_5x(9)
    changed = [i for i, m in enumerate(msgs) if m.get("role") == "system" and "output_config" in m and i != 1]
    assert len(changed) == 4  # turns 6 to 9: countdown, file note, countdown, plan-mode entry
    assert {classify_system(msgs, i) for i in changed} == {"settings_change"}
    settings_only = msgs + [{"role": "system", "content": [], "output_config": {"effort": "low"}}]
    assert classify_system(settings_only, len(msgs)) == "settings"
    plain = [i for i, m in enumerate(msgs) if m.get("role") == "system" and "output_config" not in m]
    assert {classify_system(msgs, i) for i in plain} == {"carried_text"}


def test_a_settings_change_with_non_text_content_is_unclassified_carried_and_logged():
    # Contract step 2(b) covers text-only content; anything with non-text blocks is step 5 whether or
    # not it has settings fields: carried, never paged, and logged so an unmeasured shape shows up.
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    odd = {"role": "system", "content": [{"type": "text", "text": "synthetic"}, image],
           "output_config": {"effort": "low"}}
    msgs = session_5x(20)
    at = turn_starts(msgs)[3] + 1  # turn 4's system message
    msgs[at] = odd
    assert classify_system(msgs, at) == "unclassified"
    k = turn_starts(msgs)[-1]
    events: list[dict] = []
    new = page_carried_text(msgs, EpochState(k=k, initial_carried=0), MemoryStore(key=b"k" * 32),
                            Config(carried_text_budget=0), TokenEstimator(), events)
    kept, unclassified = carried_systems(msgs, k, new.paged_carried)
    assert at in kept and unclassified == 1
    cfg = Config(bound=40_000, headroom_floor=2_000, pin_budget=200, index_budget=200, min_turn_room=2_000)
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=cfg)
    logged = []
    for t in range(1, 30):
        turn = turn_5x(t)
        prefix = msgs[:turn_starts(msgs)[t - 1] + 2] if t <= 20 else msgs + turn[:2]
        _, _, ev = engine(request(copy.deepcopy(prefix)), {}, MESSAGES)
        logged += [e for e in ev if e["event"] == "unclassified_system_message"]
        if t > 20:
            msgs = msgs + turn
    assert logged and all(e["count"] == 1 for e in logged)


def test_effort_changes_no_longer_grow_what_the_fallback_cannot_page():
    # Budget 0 pages everything the fallback may page; what stays is the unpageable part. Before D33
    # it grew by the size of each effort change's message (2,770 -> 9,272 over 48 turns measured).
    msgs = session_5x(48)
    plan = TokenEstimator().estimate_messages([system_5x(PLAN_ENTRY, "medium")])
    view = list(fallback_view(msgs, budget=0))
    floor = view[0][2]
    for t, _, carried, _, _ in view:
        # messages[0]'s blocks and the environment message, plus at most the latest effort change and
        # the latest plan-mode text: bounded, not growing with the number of changes.
        assert carried <= floor + 2 * plan + 200, (t, carried, floor)
    _, k, _, paged, _ = view[-1]
    kept = carried_systems(msgs[:k], k, paged)[0]
    assert 1 in kept  # the environment message is never paged
    effort_msgs = [i for i in range(2, k) if msgs[i].get("role") == "system" and "output_config" in msgs[i]]
    assert effort_msgs[-1] in kept  # the latest effort change is carried
    assert not any(i in kept for i in effort_msgs[:-2])  # superseded ones were paged


def test_the_5x_environment_message_without_output_config_is_never_paged():
    msgs = session_5x(30, opus=False)
    _, k, _, paged, store = last(fallback_view(msgs, budget=0))
    blocks, systems = carried_content(msgs, k, paged)
    assert msgs[1] in systems and systems[0] is msgs[1]
    assert all("# Environment" not in canonical_bytes(p).decode() for p in store.pages.values())
    assert store.pages  # the per-turn countdowns and notes were paged


def test_the_latest_effort_change_survives_an_identical_older_copy_being_paged():
    # Measured: the effort reset lands on byte-identical countdown messages in different cycles (for
    # example turns 6, 18, 30 of opus55-stream), so a paged older copy shares the latest one's hash.
    msgs = session_5x(20)
    same = [i for i, m in enumerate(msgs) if m.get("role") == "system" and m.get("output_config") == {"effort": "high"}]
    for i in same:
        msgs[i] = system_5x(countdown(0), "high")
    tail = [{"role": "user", "content": f"Turn 21. {FILL}"}]
    msgs += tail
    k = turn_starts(msgs)[-1]
    est = TokenEstimator()
    state = EpochState(k=k, initial_carried=0)
    new = page_carried_text(msgs, state, MemoryStore(key=b"k" * 32), Config(carried_text_budget=0), est, [])
    kept = carried_systems(msgs, k, new.paged_carried)[0]
    latest = max(i for i in range(k) if msgs[i].get("role") == "system" and "output_config" in msgs[i] and i != 1)
    assert latest in kept


# D32: later turn-start context blocks are pageable -------------------------------------------------

def test_context_block_kinds_are_recognised_by_their_first_line():
    kinds = [context_block_kind(rem(text)) for text in
             (countdown(3), PLAN_ENTRY, PLAN_EXIT, file_note(4), TASKS, ENVIRONMENT, CLAUDE_MD,
              DEFERRED_LISTING, "Synthetic reminder nobody has measured")]
    assert kinds == ["token_countdown", "plan_mode", "plan_mode", "file_changed", "task_reminder",
                     "environment", "instructions", "deferred_tools", None]


def test_later_turn_start_blocks_are_paged_and_growth_stays_under_the_budget():
    msgs = session_4x(48)
    budget = 8_000
    plan = TokenEstimator().estimate(PLAN_ENTRY)
    initial = None
    for t, _, carried, _, store in fallback_view(msgs, budget=budget):  # noqa: B007 - store is read after the loop
        if initial is None:
            initial = carried
        # measured before D32: 3,382 -> 13,256 (one process) with nothing paged
        assert carried - initial <= budget + plan, (t, carried - initial)
    assert any('"<system-reminder>' in canonical_bytes(p).decode()
               or "Plan mode is active" in canonical_bytes(p).decode()
               for p in store.pages.values())


def test_the_first_turn_start_blocks_and_the_latest_of_each_kind_are_never_paged():
    msgs = session_4x(30)
    _, k, _, paged, _ = last(fallback_view(msgs, budget=0))
    blocks, _ = carried_content(msgs, k, paged)
    m0 = [b for b in msgs[0]["content"] if b["text"].startswith("<system-reminder>")]
    assert blocks[: len(m0)] == m0  # messages[0]'s blocks, in order
    later = blocks[len(m0):]
    assert sorted(context_block_kind(b) for b in later) == ["file_changed", "plan_mode", "task_reminder",
                                                            "token_countdown"]
    starts = [i for i in turn_starts(msgs) if i < k]
    newest = {}
    for i in starts[1:]:
        for b in msgs[i]["content"]:
            if b["type"] == "text" and b["text"].startswith("<system-reminder>"):
                newest[context_block_kind(b)] = b
    assert all(any(b is newest[kind] for b in later) for kind in newest)


def test_the_fallback_pages_superseded_blocks_first_then_unrecognised_oldest_first():
    msgs = session_4x(12)
    unknown = rem("Synthetic reminder nobody has measured " + "x" * 400)
    msgs[turn_starts(msgs)[1]]["content"].insert(0, unknown)  # turn 2: older than every superseded one
    k = turn_starts(msgs)[-1]
    est = TokenEstimator()
    store = MemoryStore(key=b"k" * 32)
    initial = carried_tokens(msgs[:turn_starts(msgs)[1]], est)
    growth = carried_tokens(msgs[:k], est) - initial
    events: list[dict] = []
    # A budget that one superseded countdown's page satisfies.
    page_carried_text(msgs[:k], EpochState(k=k, initial_carried=initial), store, Config(carried_text_budget=growth - 1),
                      est, events)
    pages = sorted(store.pages.values(), key=lambda p: p["turn"])
    assert len(pages) == 1 and "<total_tokens>" in canonical_bytes(pages[0]).decode()
    paged = next(e for e in events if e["event"] == "carried_paged")
    assert paged["count"] == 1 and paged["context_blocks"] == 1 and paged["unrecognised"] == 0
    # The countdown paged is the oldest superseded one (turn 2's).
    assert countdown(2) in canonical_bytes(pages[0]).decode()
    # With no budget left, the unrecognised block goes after every superseded one, and pages are in
    # position order.
    store2, events2 = MemoryStore(key=b"k" * 32), []
    page_carried_text(msgs[:k], EpochState(k=k, initial_carried=initial), store2, Config(carried_text_budget=0), est,
                      events2)
    texts = [canonical_bytes(p).decode() for p in sorted(store2.pages.values(), key=lambda p: p["turn"])]
    assert "nobody has measured" in texts[0]  # positions: turn 2's unknown block is the oldest page
    # A CLI that adds a new kind shows up in the log: unrecognised items are counted, content-free.
    assert next(e for e in events2 if e["event"] == "carried_paged")["unrecognised"] == 1
    assert all('"role":"user"' in t for t in texts)


def test_paged_blocks_are_recallable_and_the_prefix_is_stable_between_rotations():
    store = MemoryStore(key=b"k" * 32)
    cfg = Config(bound=60_000, headroom_floor=2_000, pin_budget=200, index_budget=200, min_turn_room=2_000,
                 carried_text_budget=1_500, carried_budget=1_000_000)
    engine = RewriteEngine(store, config=cfg)
    msgs: list[dict] = []
    saw_paged, prefixes = False, []
    for t in range(1, 40):
        turn = turn_4x(t)
        msgs = msgs + turn[:1]
        up, _, ev = engine(request(msgs), {}, MESSAGES)
        rotated = "rotation" in names(ev)
        if any(e["event"] == "carried_paged" and e.get("context_blocks") for e in ev):
            saw_paged = True
        # Within a tool loop nothing rotates: the carried prefix is byte-identical to the turn start's.
        first = canonical_bytes(up["messages"][0])
        msgs = msgs + turn[1:3]
        up2, _, ev2 = engine(request(msgs), {}, MESSAGES)
        assert "rotation" not in names(ev2) and canonical_bytes(up2["messages"][0]) == first
        assert "bound_too_small" not in names(ev) and "over_bound" not in names(ev2)
        msgs = msgs + turn[3:]
        prefixes.append((rotated, first))
    assert saw_paged
    paged_text = "".join(canonical_bytes(p).decode() for p in store.pages.values())
    assert "Plan mode is active" in paged_text  # an older plan-mode entry was paged and can be recalled
    state = next(iter(engine.states.values()))
    m0 = [b for b in msgs[0]["content"] if b["text"].startswith("<system-reminder>")]
    blocks, _ = carried_content(msgs, state.k, state.paged_carried)
    assert blocks[: len(m0)] == m0
    # Consecutive turn starts without a rotation send the same first message.
    for (_, p1), (r2, p2) in zip(prefixes, prefixes[1:]):
        if not r2:
            assert p1 == p2


def test_a_long_4x_session_does_not_reach_bound_too_small():
    # Before D32 carried blocks grew without limit; with plan-mode entries every 12 turns F passes the
    # bound in this configuration.
    cfg = Config(bound=20_000, headroom_floor=2_000, pin_budget=200, index_budget=200, min_turn_room=2_000,
                 carried_text_budget=3_000)
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=cfg)
    msgs: list[dict] = []
    for t in range(1, 151):
        turn = turn_4x(t)
        msgs = msgs + turn[:1]
        _, _, ev = engine(request(msgs), {}, MESSAGES)
        assert "bound_too_small" not in names(ev), t
        msgs = msgs + turn[1:]


def test_carried_over_budget_reports_only_growth_the_fallback_could_not_page():
    # Rule 13: once the fallback engages it pages until growth is at most 8,000 tokens, so a 4,000
    # report fired at nearly every rotation (581 of 582 at 30M). The report now means the fallback
    # could not bound carried growth: growth after it still above the fallback budget.
    cfg = Config(bound=60_000, headroom_floor=2_000, pin_budget=200, index_budget=200, min_turn_room=2_000)
    assert (cfg.carried_budget, cfg.carried_text_budget) == (4_000, 8_000)  # defaults: v0.2.8 records resume
    engine = RewriteEngine(MemoryStore(key=b"k" * 32), config=cfg)
    msgs: list[dict] = []
    events: list[dict] = []
    for t in range(1, 90):
        msgs = msgs + turn_4x(t)[:1]
        events += engine(request(msgs), {}, MESSAGES)[2]
        msgs = msgs + turn_4x(t)[1:]
    assert "carried_paged" in names(events) and names(events).count("rotation") > 3
    assert "carried_over_budget" not in names(events)
    state = next(iter(engine.states.values()))
    growth = carried_tokens(msgs[:state.k], TokenEstimator(), state.paged_carried) - state.initial_carried
    assert cfg.carried_budget < growth <= cfg.carried_text_budget  # the old threshold would have reported it


def test_a_restart_after_block_paging_sends_the_same_bytes():
    cfg = Config(bound=60_000, headroom_floor=2_000, pin_budget=200, index_budget=200, min_turn_room=2_000,
                 carried_text_budget=1_500, carried_budget=1_000_000)
    steady = RewriteEngine(EpochStore(), config=cfg)
    store = EpochStore()
    before = RewriteEngine(store, config=cfg)
    msgs: list[dict] = []
    blocks_paged = 0
    for t in range(1, 30):
        msgs = msgs + turn_4x(t)[:1]
        steady(request(msgs), {}, MESSAGES)
        _, _, ev = before(request(msgs), {}, MESSAGES)
        blocks_paged += sum(e.get("context_blocks", 0) for e in ev if e["event"] == "carried_paged")
        msgs = msgs + turn_4x(t)[1:]
    assert blocks_paged, "blocks must have been paged before the restart"
    after = RewriteEngine(store, config=cfg)
    final = msgs + turn_4x(30)[:1]
    expected, _, _ = steady(request(copy.deepcopy(final)), {}, MESSAGES)
    got, _, events = after(request(copy.deepcopy(final)), {}, MESSAGES)
    assert "epoch_resumed" in names(events)
    assert canonical_bytes(got) == canonical_bytes(expected)


@pytest.mark.parametrize("opus", [True, False], ids=["opus-5-5", "sonnet-5"])
@pytest.mark.parametrize("restart_at", [22, 27, 33, 40])  # rotations at turns 21 and 32 or 33
def test_a_restart_after_settings_change_paging_sends_the_same_bytes(opus, restart_at):
    # D33 pages superseded settings changes and protects the latest one and the session-start message
    # (with output_config on opus, without it on sonnet-5): a resumed engine must carry exactly what
    # the uninterrupted one does, at the turn start and inside the tool loop.
    cfg = Config(**RESUME_CONFIG)
    steady = RewriteEngine(EpochStore(), config=cfg)
    store = EpochStore()
    before = RewriteEngine(store, config=cfg)
    msgs: list[dict] = []
    for t in range(1, restart_at):
        turn = turn_5x(t, opus)
        msgs = msgs + turn[:2]
        steady(request(copy.deepcopy(msgs)), {}, MESSAGES)
        before(request(copy.deepcopy(msgs)), {}, MESSAGES)
        msgs = msgs + turn[2:4]
        steady(request(copy.deepcopy(msgs)), {}, MESSAGES)
        before(request(copy.deepcopy(msgs)), {}, MESSAGES)
        msgs = msgs + turn[4:]
    state = next(iter(before.states.values()))
    systems = [i for i in range(state.k) if msgs[i].get("role") == "system"]
    assert state.paged_carried, "the fallback must have paged before the restart"
    if opus:
        changes = [i for i in systems if classify_system(msgs, i) == "settings_change"]
        assert any(message_hash(msgs[i]) in state.paged_carried for i in changes), "a settings change was paged"
        assert changes[-1] in carried_systems(msgs, state.k, state.paged_carried)[0]  # the latest is protected
    assert 1 in carried_systems(msgs, state.k, state.paged_carried)[0]  # the session-start message
    after = RewriteEngine(store, config=cfg)
    resumed = None
    for t in range(restart_at, restart_at + 4):
        turn = turn_5x(t, opus)
        for cut in (2, 4, len(turn)):
            sent = msgs + turn[:cut]
            expected, _, _ = steady(request(copy.deepcopy(sent)), {}, MESSAGES)
            got, _, events = after(request(copy.deepcopy(sent)), {}, MESSAGES)
            if resumed is None:
                resumed = names(events)
            assert canonical_bytes(got) == canonical_bytes(expected), (t, cut)
        msgs = msgs + turn
    assert "epoch_resumed" in resumed and "carried_reclassified" not in resumed


def test_the_fallback_still_pages_carried_developer_items_on_codex():
    # The Responses dialect has no context blocks and classifies developer items after the lead as
    # carried text (input_text parts); D32 and D33 must not stop the fallback from paging them.
    import test_codex as codex
    from contexto.codex import RESPONSES
    from contexto.rewrite import _DIALECT

    items = codex.history(1)
    for t in range(1, 12):
        items += [codex.prompt(f"prompt {t} {FILL}"), codex.dev(f"synthetic standing note {t} " + "x" * 600),
                  codex.answer(f"answer {t}")]
    items.append(codex.prompt("new prompt"))
    token = _DIALECT.set(RESPONSES)
    try:
        k = turn_starts(items)[-1]
        events: list[dict] = []
        store = MemoryStore(key=b"k" * 32)
        new = page_carried_text(items, EpochState(k=k, initial_carried=0), store, Config(carried_text_budget=0),
                                TokenEstimator(), events)
    finally:
        _DIALECT.reset(token)
    paged = next(e for e in events if e["event"] == "carried_paged")
    assert paged["count"] == 11 and paged["context_blocks"] == 0 and len(new.paged_carried) == 11
    assert paged["unrecognised"] == 11  # standing notes of no recognisable kind


# Rule 16 across the upgrade: records written before D32 and D33 ----------------------------------

FIXTURES = Path(__file__).with_name("fixtures")


def placeholder_pages(store: EpochStore, count: int) -> None:
    """A resume checks only that the store holds the record's ``turns_paged`` pages (packet A31); the
    request bytes come from the record's texts and the CLI's messages, not from the pages."""
    store.page_out([[{"role": "user", "content": f"placeholder page {i}"}] for i in range(count)], 0)


def test_a_v028_record_resumes_and_logs_that_it_carries_again_what_v028_paged():
    # Written by v0.2.8's rewrite.py (tests/fixtures/make_v028_epoch_record.py): its fallback paged
    # the claude-sonnet-5 environment message as carried text. D33 classifies it as session-start,
    # never paged, so the resumed engine carries it again from this request on.
    record = json.loads((FIXTURES / "v028_epoch_sonnet5.json").read_text(encoding="utf-8"))
    msgs = session_5x(39, opus=False)
    assert message_hash(msgs[1]) in record["paged_carried"]
    store = EpochStore(record)
    placeholder_pages(store, record["turns_paged"])
    engine = RewriteEngine(store, config=Config(**RESUME_CONFIG))
    final = msgs + turn_5x(40, opus=False)[:2]
    got, _, events = engine(request(copy.deepcopy(final)), {}, MESSAGES)
    assert names(events)[:2] == ["epoch_resumed", "carried_reclassified"]
    assert events[1] == {"event": "carried_reclassified", "epoch": record["epoch"], "count": 1}
    assert "rotation" not in names(events)
    carried = [m for m in got["messages"] if m.get("role") == "system" and ENVIRONMENT[:40] in message_text(m)]
    assert len(carried) == 1  # the environment message is carried again


def test_a_record_this_version_wrote_resumes_without_carried_reclassified():
    for opus in (True, False):
        store = EpochStore()
        before = RewriteEngine(store, config=Config(**RESUME_CONFIG))
        msgs: list[dict] = []
        for t in range(1, 40):
            msgs = msgs + turn_5x(t, opus)[:2]
            before(request(copy.deepcopy(msgs)), {}, MESSAGES)
            msgs = msgs + turn_5x(t, opus)[2:]
        assert next(iter(before.states.values())).paged_carried
        after = RewriteEngine(store, config=Config(**RESUME_CONFIG))
        _, _, events = after(request(msgs + turn_5x(40, opus)[:2]), {}, MESSAGES)
        assert "epoch_resumed" in names(events) and "carried_reclassified" not in names(events), opus
