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
from contexto.bench.model import Script
from contexto.bench.replay import lines, reach, trace, upstream

FORMAT = 1
CANNOT_SHOW = ["answer accuracy", "cache reads", "real token counts", "what a real CLI does",
               "what a real model does"]
LABELS = {"contexto": "Contexto (replay, estimated)", "full": "Full context (modelled)",
          "plain": "Plain Claude Code compacting at the bound (modelled)"}
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
        "on the script's own estimate (log text at 1.8 bytes per token, probe turns at 3.5).",
        f"The plain line compacts when a request would pass {script.bound:,} tokens and continues from the head, a "
        f"summary of {lines.SUMMARY_TOKENS:,} tokens ({lines.SUMMARY_SOURCE}) and the turn being served; that "
        "nothing else survives is an assumption.",
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
                         "plain_compactions": lines.compactions(plain)},
        },
    }


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
    if measured.compaction_markers:
        out.append(f"{measured.compaction_markers} compaction markers in the requests")
    out += [f"the session did not go as scripted: {problem}" for problem in measured.problems]
    out += [f"differs from the pinned trace: {difference}" for difference in differences]
    return out


def _reach_table(totals: dict[str, Any]) -> list[tuple[str, ...]]:
    rows = [("class", "probes", "paged", "by search", "in the top 3", "by search or in view", "one shared word")]
    for cls, cell in [*totals["by_class"].items(), ("all", totals)]:
        one_word = str(cell["one_word"]) if cls in ("paraphrase", "concluded", "toolloop") else "-"
        rows.append((cls, str(cell["probes"]), str(cell["paged"]), str(cell["reachable"]), str(cell["shown"]),
                     str(cell["covered"]), one_word))
    return rows


def _unreached(totals: dict[str, Any]) -> list[str]:
    return [cls for cls, cell in totals["by_class"].items() if cell["reachable"] == 0]


def summary(script: Script, data: dict[str, Any]) -> list[str]:
    """The lines printed to the terminal."""
    block, sizes = data["replay"], bench_script.estimate(script)
    totals = block["reach"]
    held = block["over_bound"] == 0 and block["over_bound_proxy"] == 0
    if held and not block["compaction_markers"]:
        compaction = ("  Compaction: none. No request reached the size at which a CLI compacts and none carried a "
                      "compaction summary or marker. There is no CLI in a replay, so this is what zero compaction "
                      "means here.")
    else:
        compaction = (f"  Compaction: {block['compaction_markers']} compaction markers in the requests, and "
                      f"{block['over_bound']} requests at the size at which a CLI compacts. There is no CLI in a "
                      "replay.")
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
        compaction,
        "  Request size per turn: in requests.jsonl and results.json when --out is given.",
        f"  Search: the store's search returns {totals['reachable']} of {totals['probes']} planted facts by the "
        f"fixed query rule; {totals['covered']} are returned or still in view.",
        "What it cannot show:",
        "  Answer accuracy, cache reads, real token counts, and what a real CLI or a real model does.",
        "",
        f"Session: {sizes['turns']} turns ({sizes['content_turns']} content, {sizes['probe_turns']} probe). The "
        f"content turns are {sizes['history_tokens']:,} estimated tokens of history, "
        f"{sizes['history_tokens'] / script.bound:.2f} times the bound ({sizes['tokens']:,} with the probe turns); "
        f"the synthetic head is {block['head_tokens']:,}.",
        f"Proxy: {block['rotations']} rotations, {block['pages']} pages stored, failure events: "
        + (", ".join(f"{k} ({v})" for k, v in block["failure_events"].items()) or "none") + ".",
        "",
        "Search reachability, measured when the probe turns start (probes per class):",
        *_table(_reach_table(totals)),
    ]
    unreached = _unreached(totals)
    if unreached:
        out.append(f"  Not reachable by the rule's query: {', '.join(unreached)}. The search returns a page only "
                   "when it holds every word of the query, and these questions do not name their subject.")
    modelled = block["modelled"]
    out += [
        "",
        "Modelled from the script, not run:",
        f"  Full context: the last request would be {modelled['full_max']:,} tokens.",
        f"  Plain Claude Code compacting at the bound: {modelled['plain_compactions']} compactions, largest request "
        f"{modelled['plain_max']:,}.",
        "",
    ]
    if block["notices"]:
        out += ["The proxy's warnings during the session:", *[f"  {line}" for line in block["notices"]], ""]
    if block["pinned"]:
        out.append("Pinned expectations: " + ("every measurement equals the trace." if not block["differences"]
                                              else f"{len(block['differences'])} differences."))
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
        f"{block['over_bound']} by the fake upstream's count, {block['over_bound_proxy']} by the proxy's own log.",
        f"- **Compaction markers: {block['compaction_markers']}.** "
        + ("No request reached the size at which a CLI compacts, and none carried a compaction summary or marker. "
           if held and not block["compaction_markers"] else "")
        + "There is no CLI in a replay, so this is all that zero compaction can mean here.",
        "- **Request size per turn**: `requests.jsonl`, and `request_series` in `results.json`.",
        f"- **Search reachability**: the store's search returns {totals['reachable']} of {totals['probes']} planted "
        f"facts by the fixed query rule; {totals['covered']} are returned or still in view.",
        "",
        "## What it cannot show",
        "",
        "- Answer accuracy: the answers in the conversation are scripted.",
        "- Cache reads: the fake upstream reports none.",
        "- Real token counts: sizes are estimates at stated byte rates.",
        "- What a real CLI or a real model does.",
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
        "turn. The query is fixed per class: the key term the question names for the first five classes (a "
        "multi-hop probe follows its hops), and the question's content words for `paraphrase`, `concluded` and "
        "`toolloop`, whose questions do not name their subject. \"By search\" counts the probes for which the store's "
        "search returned a page holding every statement the answer needs; \"in the top 3\" those whose page is among "
        "the three the search tool shows in detail; \"by search or in view\" adds the statements still in the "
        "request; \"one shared word\" counts the probes for which a single word the question shares with the "
        "statement, searched alone, puts the page in the top 3.",
        "",
        "| " + " | ".join(table[0]) + " |", "|" + "---|" * len(table[0]),
        *["| " + " | ".join(row) + " |" for row in table[1:]],
        "",
    ]
    unreached = _unreached(totals)
    if unreached:
        text += [f"Not reachable by the rule's query: {', '.join(f'`{c}`' for c in unreached)}. Contexto's search "
                 "returns a page only when it holds every word of the query, and has no fuzzy matching; these "
                 "questions do not name their subject. Whether a model finds its own words for them is a question "
                 "for the live run.", ""]
    text += [
        "## Modelled lines (not run)",
        "",
        f"- Full context: the head plus the whole history; the last request would be {modelled['full_max']:,} "
        "tokens.",
        f"- Plain Claude Code compacting at the bound: {modelled['plain_compactions']} compactions; largest request "
        f"{modelled['plain_max']:,} tokens. Summary size {lines.SUMMARY_TOKENS:,} tokens, from "
        f"{lines.SUMMARY_SOURCE}. What survives a compaction is {lines.SURVIVES}.",
        "",
        "These two lines are a model of what the clients would send, on the script's own estimate. They are not a "
        "run of Claude Code or of the API.",
        "",
    ]
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
