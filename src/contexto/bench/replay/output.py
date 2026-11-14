"""What a replay writes and prints: the output files of the shared schema, and the terminal text.

Files, in ``--out DIR``: ``manifest.json`` (kind ``replay``, upstream ``local``),
``scripts/seed-<seed>.json``, ``requests.jsonl`` (the ``contexto`` arm with basis ``estimated``, then
the ``full`` and ``plain`` arms with basis ``modelled``), ``reach.jsonl``, ``results.json`` and
``results.md``. The session's store is kept beside them in ``store/``.

Every text that shows a number says what kind of number it is. The terminal summary and
``results.md`` open with what a replay shows and what it cannot show.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from contexto.bench import script as bench_script
from contexto.bench.model import NEW_CLASSES, Script
from contexto.bench.replay import lines, reach, trace, upstream

FORMAT = 1
SHOWN_DIFFERENCES = 5  # differences listed in the result before the rest are counted
CANNOT_SHOW = ["answer accuracy", "cache reads", "real token counts", "what a real CLI does",
               "what a real model does", "what the proxy does with what only Claude Code sends (its session start, "
               "its version, the role system messages of 5.x model ids, thinking blocks, side requests)"]
# Events the proxy logs for the synthetic client that are not failures: the first request is sized before
# any usage is known (estimate_error, which the acceptance harness reports only), and the client is not a
# CLI whose session start the proxy knows (session_start_unrecognised, in neither failure list).
CLIENT_DIAGNOSTICS = ("session_start_unrecognised", "estimate_error")
NOT_EXERCISED = (
    "the client sends no Claude Code User-Agent, so the proxy logged no CLI version",
    "it sends no role system message inside messages, where Claude Code on 5.x model ids puts its environment "
    "text, countdowns and settings, so the proxy's handling of those (D33) is not exercised",
    "it sends no thinking blocks and no side requests",
)
# A series' label names the series only: the chart adds its basis (estimated, modelled) after it.
LABELS = {"contexto": "Contexto (replay)", "full": "Full context", "plain": "Plain Claude Code compacting at the bound"}
RATES_TEXT = (f"{upstream.LOG_BYTES_PER_TOKEN} bytes per token for pasted log text and "
              f"{upstream.OTHER_BYTES_PER_TOKEN} for everything else")


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def request_rows(script: Script, measured: Any) -> list[dict[str, Any]]:
    """Every ``requests.jsonl`` row: the replayed arm, then the two modelled arms."""
    return [*measured.requests, *lines.full_rows(script, measured.head_tokens),
            *lines.plain_rows(script, measured.head_tokens, script.bound)]


def manifest(script: Script, turns: int, measured: Any, version: str, pinned: bool, started: str,
             finished: str) -> dict[str, Any]:
    modelled = "modelled from the script and not run: a replay has no model and no CLI"
    return {
        "format": FORMAT, "kind": "replay", "contexto_version": version, "claude_code_version": None, "model": None,
        "bound": script.bound, "protocol": script.protocol, "turn_kbytes": script.turn_size // 1_000,
        "seed": script.seed, "seeds": [script.seed], "repeats": 1, "arms": ["contexto", "full", "plain"],
        "upstream": "local", "plain_compaction": None, "cache_ttl": None, "started": started, "finished": finished,
        "units": [
            {"arm": "contexto", "seed": script.seed, "repeat": 0, "status": "complete" if not measured.problems
             else "stopped", "reason": "; ".join(measured.problems) or None, "cli_exit": None, "compactions": 0,
             "compaction_pre_tokens": [], "concluded_ok": {}, "toolloop_reads": measured.toolloop_reads or None,
             "probe_turns_completed": measured.probe_turns_completed},
            *[{"arm": arm, "seed": script.seed, "repeat": 0, "status": "not_run", "reason": modelled,
               "cli_exit": None, "compactions": None, "compaction_pre_tokens": [], "concluded_ok": {},
               "toolloop_reads": None, "probe_turns_completed": 0} for arm in ("full", "plain")],
        ],
        # Fields this builder added to the schema:
        "replay": {
            "pinned": pinned, "trace": trace.file_name(script.seed) if pinned else None,
            "checked": ("the regenerated session against the trace's hashes, and the measurements against its "
                        "expectations") if pinned else "nothing: this is not the pinned trace",
            "turns": turns, "script_sha256": script.digest(), "session": measured.session_id,
            "rates": dict(trace.RATES), "head_tokens": measured.head_tokens,
            "environment": dict(measured.environment),
            "modelled": {"arms": ["full", "plain"], "trigger": script.bound, "summary_tokens": lines.SUMMARY_TOKENS,
                         "summary_source": lines.SUMMARY_SOURCE, "survives": lines.SURVIVES},
        },
    }


def notes(script: Script, pinned: bool) -> list[str]:
    out = [
        "Synthetic: a generated session replayed through the real Contexto proxy against a local fake upstream. "
        "No model was called.",
        f"Request sizes are estimates, not token counts: {RATES_TEXT}, with no tokenizer.",
        "The full-context and plain Claude Code lines are modelled from the script, not run: one request per turn "
        f"on the script's own estimate ({lines.RATES_TEXT}).",
        f"The plain line compacts when a request would pass {script.bound:,} tokens and continues from the head, a "
        f"summary of {lines.SUMMARY_TOKENS:,} tokens ({lines.SUMMARY_SOURCE}) and the turn being served; that "
        "nothing else survives is an assumption, and it errs toward plain Claude Code: anything re-attached would "
        "make its requests larger and its compactions more frequent.",
        "Search reach is a floor under mechanical queries, not what a model can reach: one fixed query per fact, "
        "and beside it the best query made from the question's own words. " + WHOLE_WORDS + " " + STRICT_SEARCH
        + (" " + pinned_fallback() if pinned else "") + " Which query a model makes is for the live run.",
        TOOL_LOOP_NOTE,
        "A replay cannot show " + ", ".join(CANNOT_SHOW) + ".",
    ]
    if not pinned:
        out.append("This is not the pinned trace: nothing was checked against recorded hashes or expected "
                   "measurements.")
    return out


def results(script: Script, manifest_data: dict[str, Any], rows: list[dict[str, Any]], measured: Any, pinned: bool,
            differences: list[str], failed: list[str], recording: bool = False) -> dict[str, Any]:
    series = {}
    for arm in ("contexto", "full", "plain"):
        part = [r for r in rows if r["arm"] == arm and r["main"]]
        series[arm] = {"basis": part[0]["basis"] if part else None, "label": LABELS[arm],
                       "points": [[r["history_tokens"], r["uncached"] + r["cache_write"] + r["cache_read"]]
                                  for r in part]}
    plain = [r for r in rows if r["arm"] == "plain"]
    sizes = bench_script.estimate(script)
    return {
        "format": FORMAT, "kind": "replay", "manifest": manifest_data, "request_series": series,
        "bound": script.bound, "notes": notes(script, pinned),
        "replay": {
            "turns": measured.turns, "requests": len(measured.requests), "max_input_estimated": measured.max_input,
            "over_bound": measured.over_bound, "rotations": measured.rotations,
            "failure_events": dict(measured.failure_events), "compaction_markers": measured.compaction_markers,
            "reach": reach.totals(measured.reach), "cannot_show": list(CANNOT_SHOW),
            # Fields this builder added to the schema:
            "over_bound_proxy": measured.over_bound_proxy, "pages": measured.pages, "events": dict(measured.events),
            "history_tokens": sizes["tokens"], "content_history_tokens": sizes["history_tokens"],
            "head_tokens": measured.head_tokens, "seconds": measured.seconds, "pinned": pinned,
            "recording": recording,
            "differences": list(differences), "problems": list(measured.problems),
            "notices": list(measured.notices), "passed": not failed, "failures": list(failed),
            "modelled": {"full_max": max((r["uncached"] for r in rows if r["arm"] == "full"), default=0),
                         "plain_max": max((r["uncached"] for r in plain if r["main"]), default=0),
                         "plain_compactions": lines.compactions(plain), "rates": lines.RATES_TEXT},
            "fit": fit(script, measured),
            "synthetic_client": {"diagnostics": {name: measured.events.get(name, 0) for name in CLIENT_DIAGNOSTICS},
                                 "not_exercised": list(NOT_EXERCISED)},
            "private_files_removed": list(measured.private_files_removed),
        },
    }


def fit(script: Script, measured: Any) -> dict[str, Any]:
    """Whether a request holding the head and the largest turn can be under the bound at all."""
    largest = max(lines.turn_estimates(script), default=0)
    return {"head_tokens": measured.head_tokens, "largest_turn_tokens": largest,
            "fits": measured.head_tokens + largest <= script.bound}


def _fit_text(script: Script, cell: dict[str, Any]) -> str | None:
    if cell["fits"]:
        return None
    return (f"the bound of {script.bound:,} is too small for this session: the synthetic head is "
            f"{cell['head_tokens']:,} estimated tokens and the largest turn about {cell['largest_turn_tokens']:,}, so "
            "a request that holds that turn cannot be under the bound")


def failures(script: Script, measured: Any, differences: list[str]) -> list[str]:
    """Why the replay fails, in plain words; empty when it passes."""
    out = []
    if measured.over_bound:
        out.append(f"{measured.over_bound} requests were over the bound of {script.bound:,} tokens by the fake "
                   f"upstream's count (largest {measured.max_input:,})")
    if measured.over_bound_proxy:
        out.append(f"the proxy's own log marks {measured.over_bound_proxy} requests over the bound")
    if measured.failure_events:
        named = ", ".join(f"{name} ({count})" for name, count in measured.failure_events.items())
        out.append(f"failure events in the session log: {named}")
    if measured.events.get("bound_too_small"):
        out.append(f"the engine logged bound_too_small {measured.events['bound_too_small']} times: a new turn alone "
                   "did not fit its window")
    too_small = _fit_text(script, fit(script, measured))
    if too_small:
        out.append(too_small)
    if measured.compaction_markers:
        out.append(f"{measured.compaction_markers} compaction markers in the requests")
    out += [f"the session did not go as scripted: {problem}" for problem in measured.problems]
    shown = differences[:SHOWN_DIFFERENCES]
    out += [f"differs from the pinned trace: {difference}" for difference in shown]
    if len(differences) > len(shown):
        out.append(f"differs from the pinned trace in {len(differences) - len(shown)} more places "
                   "(results.json, replay.differences)")
    return out


BEST_QUERY_COLUMN = "best query from the question's words"
WHOLE_WORDS = ("The best-query figure counts a page for a query only when every word of the query is on the page as "
               "a whole word, not only inside a longer one, as the store's search may match it.")
STRICT_SEARCH = ("Reach is measured with the store's strict search (SessionStore.search), which counts a page only "
                 "when it holds every word of the query, not with the reply of the contexto_search tool: since D65 "
                 "that tool falls back to approximate matches when no page holds every word, and those are not "
                 "counted.")
FALLBACK_UNCOUNTED = ("The search tool's approximate fallback, which this replay does not count, can still show a "
                      "plant.")
# What the tool's fallback does on the pinned session, stated for the pinned trace only. These are facts of that
# session, not measurements this command makes or checks; the replay tests check each of them against the run
# (check_the_pinned_fallback_sentence_holds_in_the_run), so a change to the proxy that alters them fails there.
FALLBACK_EMPTY = 18  # rule queries the strict search finds nothing for
FALLBACK_ANSWERED = 1  # of those, the queries whose tool reply holds approximate matches
FALLBACK_PROBE, FALLBACK_CLASS = "q_r_02", "paraphrase"
TOOL_LOOP_NOTE = ("Every request of a turn, the rounds of a tool loop included, is plotted at that turn's history "
                  "length, so a turn with a tool loop (the file reads of the toolloop turn, the searches of a probe "
                  "turn) shows as a vertical run of points.")


def pinned_fallback(code: Any = str) -> str:
    """What the search tool's approximate fallback (D65) answers on the pinned session."""
    return (f"On the pinned session the tool's fallback answers {FALLBACK_ANSWERED} of the {FALLBACK_EMPTY} queries "
            f"the strict search finds nothing for: for {code(FALLBACK_PROBE)} ({code(FALLBACK_CLASS)}) its reply "
            "shows the plant's line on the second of three pages, beside two decoys.")


def _reach_table(totals: dict[str, Any]) -> list[tuple[str, ...]]:
    rows = [("class", "probes", "paged", "by search", "in the top 3", "by search or in view", BEST_QUERY_COLUMN)]
    for cls, cell in [*totals["by_class"].items(), ("all", totals)]:
        rows.append((cls, str(cell["probes"]), str(cell["paged"]), str(cell["reachable"]), str(cell["shown"]),
                     str(cell["covered"]), str(cell["best_query"])))
    return rows


def _unreached(totals: dict[str, Any]) -> list[str]:
    """The new classes the rule's query reaches none of."""
    return [cls for cls, cell in totals["by_class"].items() if cls in NEW_CLASSES and cell["reachable"] == 0]


def _search_headline(totals: dict[str, Any]) -> str:
    """Both figures at the same cut, the top 3, and the rule's figure at any rank beside it."""
    return (f"under one fixed query per fact, the store's strict search returns {totals['reachable']} of "
            f"{totals['probes']} planted facts, {totals['shown']} of them in the top 3, and "
            f"{totals['covered'] - totals['reachable']} more are still in view. This is a floor under one mechanical "
            "rule, not what a model can reach. The best query made from each question's own words puts "
            f"{totals['best_query']} in the top 3. " + WHOLE_WORDS + " " + STRICT_SEARCH)


def _unreached_text(totals: dict[str, Any], pinned: bool, code: Any = str) -> str | None:
    """Why the rule's figure is 0 for the classes it reaches none of, and what the next column is."""
    unreached = _unreached(totals)
    if not unreached:
        return None
    cells = [totals["by_class"][cls] for cls in unreached]
    nothing = all(cell["rule_returned_nothing"] == cell["probes"] for cell in cells)
    named = ", ".join(code(c) for c in unreached[:-1]) + (" and " if len(unreached) > 1 else "") + code(unreached[-1])
    return (f"The rule's query finds none of {named}. For these classes the query is every content word of the "
            "question but its instruction words, and "
            + ("no stored page holds all of them, so the strict search returns nothing for them" if nothing
               else "no page the strict search returns for them holds the plant")
            + ". " + FALLBACK_UNCOUNTED + (" " + pinned_fallback(code) if pinned else "")
            + " The next column shows what a query made from the question's own words finds. Which query a model "
            "makes is for the live run.")


def _client_text(block: dict[str, Any]) -> str:
    named = ", ".join(f"{name} ({count})" for name, count in block["synthetic_client"]["diagnostics"].items())
    return (f"The proxy logged {named} for the synthetic client; a replay expects these and does not count them as "
            "failures. Not exercised: " + "; ".join(block["synthetic_client"]["not_exercised"]) + ".")


def _compaction(script: Script, block: dict[str, Any], held: bool) -> str:
    if held and not block["compaction_markers"]:
        shown = (f"no request was over the bound of {script.bound:,} by the estimate, and none carried a compaction "
                 "summary or marker.")
    else:
        shown = (f"{block['over_bound']} requests over the bound by the estimate, and {block['compaction_markers']} "
                 "compaction markers in the requests.")
    return "Compaction: a replay has no CLI, so it cannot show this. What it shows: " + shown


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def summary(script: Script, data: dict[str, Any]) -> list[str]:
    """The lines printed to the terminal."""
    block, sizes = data["replay"], bench_script.estimate(script)
    totals = block["reach"]
    held = block["over_bound"] == 0 and block["over_bound_proxy"] == 0
    out = [
        f"contexto bench --replay: seed {script.seed}, protocol {script.protocol}, bound {script.bound:,} tokens"
        + (" (the pinned trace)" if block["pinned"] else " (recording a trace)" if block["recording"]
           else " (NOT the pinned trace)"),
        "",
        "A replay sends a generated session through the real Contexto proxy to a fake upstream on 127.0.0.1.",
        "No model is called and nothing leaves this machine. The replies are scripted.",
        f"Sizes are estimates, not token counts: {RATES_TEXT}.",
    ]
    if block["recording"]:
        out.append("This run records a trace, so nothing was checked against recorded hashes or expected "
                   "measurements.")
    elif not block["pinned"]:
        out.append("This is not the pinned trace, so nothing was checked against recorded hashes or expected "
                   "measurements.")
    out += [
        "",
        "What this replay shows:",
        f"  Bound: {'held' if held else 'NOT held'}. {block['requests']} requests, the largest "
        f"{block['max_input_estimated']:,} estimated tokens against the bound of {script.bound:,}; "
        f"{block['over_bound']} over the bound by the fake upstream's count, {block['over_bound_proxy']} by the "
        "proxy's own log.",
    ]
    too_small = _fit_text(script, block["fit"])
    if too_small:
        out.append(f"  Why: {too_small}.")
    out += [
        "  " + _compaction(script, block, held),
        "  Request size per turn: in requests.jsonl and results.json when --out is given.",
        "  Search: " + _search_headline(totals),
        "What it cannot show:",
        "  Answer accuracy, cache reads, real token counts, what a real CLI or a real model does, and what the proxy "
        "does with what only Claude Code sends.",
        "",
        f"Session: {sizes['turns']} turns ({sizes['content_turns']} content, {sizes['probe_turns']} probe). The "
        f"content turns are {sizes['history_tokens']:,} estimated tokens of history, "
        f"{sizes['history_tokens'] / script.bound:.2f} times the bound ({sizes['tokens']:,} with the probe turns); "
        f"the synthetic head is {block['head_tokens']:,} estimated tokens.",
        f"Proxy: {block['rotations']} rotations, {block['pages']} pages stored, failure events: "
        + (", ".join(f"{k} ({v})" for k, v in block["failure_events"].items()) or "none") + ".",
        "Client: " + _client_text(block),
        "",
        "Search reachability, measured when the probe turns start (probes per class):",
        *_table(_reach_table(totals)),
    ]
    unreached = _unreached_text(totals, block["pinned"])
    if unreached:
        out.append("  " + unreached)
    modelled = block["modelled"]
    out += [
        "",
        f"Modelled from the script, not run ({modelled['rates']}):",
        f"  Full context: the last request would be {modelled['full_max']:,} estimated tokens.",
        "  Plain Claude Code compacting at the bound: "
        f"{_plural(modelled['plain_compactions'], 'modelled compaction')}, "
        f"largest request {modelled['plain_max']:,} estimated tokens.",
        "",
    ]
    if block["notices"]:
        out += ["The proxy's warnings during the session:", *[f"  {line}" for line in block["notices"]], ""]
    if block["pinned"]:
        out.append("Pinned expectations: " + ("every measurement equals the trace." if not block["differences"]
                                              else _plural(len(block["differences"]), "difference") + "."))
    if block["failures"]:
        out += ["Result: FAIL", *[f"  {line}" for line in block["failures"]]]
    else:
        out.append("Result: pass (the bound held, no failure event"
                   + (", measurements equal the pinned trace)" if block["pinned"] else ")"))
    out.append(f"Wall time: {block['seconds']} seconds.")
    return out


def _table(rows: list[tuple[str, ...]]) -> list[str]:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return ["  " + "  ".join(cell.ljust(width) if i == 0 else cell.rjust(width)
                             for i, (cell, width) in enumerate(zip(row, widths))).rstrip() for row in rows]


def markdown(script: Script, data: dict[str, Any]) -> str:
    """``results.md``: the same facts as the terminal summary, as a document."""
    block, sizes = data["replay"], bench_script.estimate(script)
    totals, modelled = block["reach"], block["modelled"]
    held = block["over_bound"] == 0 and block["over_bound_proxy"] == 0
    table = _reach_table(totals)
    too_small = _fit_text(script, block["fit"])
    text = [
        "# contexto bench --replay",
        "",
        f"Seed {script.seed}, protocol {script.protocol}, bound {script.bound:,} tokens, Contexto "
        f"{data['manifest']['contexto_version']}. "
        + ("This is the pinned trace: the session was checked against its recorded hashes and the measurements "
           "against its expectations." if block["pinned"] else
           "**This is not the pinned trace: nothing was checked against recorded hashes or expected "
           "measurements.**"),
        "",
        "A replay sends a generated, synthetic session through the real Contexto proxy to a fake upstream on "
        "127.0.0.1. No model is called. The replies are scripted, and every size is an estimate at "
        f"{RATES_TEXT}, with no tokenizer.",
        "",
        "## What this replay shows",
        "",
        f"- **The bound {'held' if held else 'did not hold'}.** {block['requests']} requests; the largest was "
        f"{block['max_input_estimated']:,} estimated tokens against the bound of {script.bound:,}. Over the bound: "
        f"{block['over_bound']} by the fake upstream's count, {block['over_bound_proxy']} by the proxy's own log."
        + (f" Why: {too_small}." if too_small else ""),
        f"- **{_compaction(script, block, held).replace('Compaction:', 'Compaction:**', 1)}",
        "- **Request size per turn**: `requests.jsonl`, and `request_series` in `results.json`.",
        "- **Search reachability**: " + _search_headline(totals),
        "",
        "## What it cannot show",
        "",
        "- Answer accuracy: the answers in the conversation are scripted.",
        "- Cache reads: the fake upstream reports none.",
        "- Real token counts: sizes are estimates at stated byte rates.",
        "- What a real CLI or a real model does.",
        "- What the proxy does with what only Claude Code sends: see the next section.",
        "",
        "## How the proxy saw the synthetic client",
        "",
        _client_text(block),
        "",
        "## Measurements",
        "",
        "| | |", "|---|---|",
        f"| Turns | {sizes['turns']} ({sizes['content_turns']} content, {sizes['probe_turns']} probe) |",
        f"| History of the content turns, estimated tokens | {sizes['history_tokens']:,} "
        f"({sizes['history_tokens'] / script.bound:.2f} times the bound) |",
        f"| History with the probe turns, estimated tokens | {sizes['tokens']:,} |",
        f"| Synthetic head, estimated tokens | {block['head_tokens']:,} |",
        f"| Requests | {block['requests']} |",
        f"| Largest request, estimated tokens | {block['max_input_estimated']:,} |",
        f"| Requests over the bound, fake upstream's count | {block['over_bound']} |",
        f"| Requests over the bound, proxy's own log | {block['over_bound_proxy']} |",
        f"| Rotations | {block['rotations']} |",
        f"| Pages stored | {block['pages']} |",
        "| Failure events | " + (", ".join(f"{k} ({v})" for k, v in block["failure_events"].items()) or "none")
        + " |",
        f"| Compaction markers | {block['compaction_markers']} |",
        f"| Wall time, seconds | {block['seconds']} |",
        "",
        "## Search reachability",
        "",
        "Measured once, when the probe turns start: after the proxy forwarded the first request of the first probe "
        "turn. The figures are a floor under mechanical queries, not what a model can reach. The rule's query is "
        "fixed per class: the key term the question names for the first five classes (a multi-hop probe follows its "
        "hops), and for `paraphrase`, `concluded` and `toolloop` the question's content words without its "
        "instruction words (the fixed phrases the generator wraps every such question in, including the frame "
        "every `concluded` question shares: \"Earlier you had to pick the ..., and you replied with your pick. What "
        "did you pick?\"). \"By search\" counts the probes for which the store's strict search returned a page "
        "holding every statement the answer needs; \"in the top 3\" those whose page is among the three the search "
        "tool shows in detail; \"by search or in view\" adds the statements still in the request. \"Best query from "
        "the question's words\" tries each single content word of the question and each pair of adjacent content "
        "words, every pair with a space and with a hyphen, and counts the probe when the best of them puts the page "
        "in the top 3; for the first five classes the rule's query is already taken from the question, so there the "
        "column takes the rule's query. " + WHOLE_WORDS + " " + STRICT_SEARCH
        + " Which query a model makes is for the live run.",
        "",
        "| " + " | ".join(table[0]) + " |", "|" + "---|" * len(table[0]),
        *["| " + " | ".join(row) + " |" for row in table[1:]],
        "",
    ]
    unreached = _unreached_text(totals, block["pinned"], lambda word: f"`{word}`")
    if unreached:
        text += [unreached, ""]
    text += [
        "## Modelled lines (not run)",
        "",
        f"Both are one request per turn on the script's own estimate: {modelled['rates']}.",
        "",
        f"- Full context: the head plus the whole history; the last request would be {modelled['full_max']:,} "
        "estimated tokens.",
        "- Plain Claude Code compacting at the bound: "
        f"{_plural(modelled['plain_compactions'], 'modelled compaction')}; "
        f"largest request {modelled['plain_max']:,} estimated tokens. Summary size {lines.SUMMARY_TOKENS:,} tokens, "
        f"from {lines.SUMMARY_SOURCE}. What survives a compaction is {lines.SURVIVES}.",
        "",
        "These two lines are a model of what the clients would send, on the script's own estimate. They are not a "
        "run of Claude Code or of the API.",
        "",
    ]
    if block["private_files_removed"]:
        text += ["## The store", "",
                 "`store/` is the session's own store, kept so that its pages can be read. Two files were removed "
                 "from it: " + ", ".join(f"`{p}`" for p in block["private_files_removed"]) + ". They were random, "
                 "made for this store alone (the key of its page ids and the salt of its upstream affinity key), "
                 "and are of no use outside it. Reading the store with `contexto.store.SessionStore.open_existing` "
                 "writes nothing into it; opening it as a session (`SessionStore(home, session)`, as the proxy and "
                 "Contexto's MCP server do) writes a new random `page.key`, so remove that again before publishing "
                 "the store.", ""]
    if block["notices"]:
        text += ["## The proxy's warnings during the session", "", *[f"- {line}" for line in block["notices"]], ""]
    text += ["## Result", ""]
    if block["failures"]:
        text += ["**Fail.**", "", *[f"- {line}" for line in block["failures"]]]
    else:
        text.append("Pass: the bound held, no failure event occurred"
                    + (", and every measurement equals the pinned trace." if block["pinned"] else "."))
    return "\n".join(text) + "\n"


def write(out: Path, script: Script, manifest_data: dict[str, Any], rows: list[dict[str, Any]],
          reach_rows: list[dict[str, Any]], data: dict[str, Any]) -> list[Path]:
    """Write the output directory and return the paths written."""
    out.mkdir(parents=True, exist_ok=True)
    script_path = out / "scripts" / f"seed-{script.seed}.json"
    script.write(script_path)
    files = {
        "manifest.json": json.dumps(manifest_data, indent=1) + "\n",
        "requests.jsonl": "".join(json.dumps(row) + "\n" for row in rows),
        "reach.jsonl": "".join(json.dumps(row) + "\n" for row in reach_rows),
        "results.json": json.dumps(data, indent=1) + "\n",
        "results.md": markdown(script, data),
    }
    written = [script_path]
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8")
        written.append(out / name)
    return written
