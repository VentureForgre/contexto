# M2 A16: Codex acceptance plan and zero-spend dry run

Packet A16 (lane A). This is a plan only; no live run happens until intake approves a budget line.
It covers how the D27 acceptance protocol (54 probes over 6 probe turns; `docs/DECISIONS.md` D27,
`docs/research/M2-acceptance-harness.md`) runs through `contexto run codex` in API-key mode with
the owner's proxy as upstream, and a dry run of the full protocol against a local stub on 127.0.0.1.

Tags: **measured** (seen in a run here or in A5/A6), **source** (read in code or `--help`),
**proposed** (a decision for intake).

## 1. Driving Codex through nine-plus user turns

- **Turn transport** [source: `codex exec --help` and `codex exec resume --help`, codex-cli 0.154.0; `run_codex`, `src/contexto/codex.py`].
  - Codex has no stream-json stdin session like `claude -p`, so each user turn is one process:
    `contexto run --bound B --session S --upstream U --codex-key-helper H codex exec --skip-git-repo-check --json -c model=gpt-6-astra -c model_reasoning_effort=low -o <last message file> [resume <thread id>] -`.
    The turn text goes on stdin.
  - The first turn's `thread.started` event (from `--json`) names the thread; later turns resume it by id.
  - `resume --last -` must not be used: `-` is read as the session id [measured: the turn started a new thread].
  - Resume by id carries the history [measured: the second turn's first request had 9 items, against 7].
  - Implementation: `spikes/m2/acceptance/codex_driver.py`, `CodexTurns` and `turn_command`.
- **One proxy per turn** [measured].
  - Every turn starts a new proxy and engine on the same Contexto session. A8's restore of the measured ratio applies at each process's first request.
  - Epochs restart per process, so the cache criterion skips rotations rather than epoch boundaries (section 3).
- **Isolation** [source: `codex_driver.child_env`, `run_codex`].
  - API-key mode only, with a key helper command. The key is never in a file here; the stub run uses a dummy helper.
  - Codex's home is the session directory's `codex-home`. The user's Codex and Claude config is never read or written.
  - The scratch directory is outside Git. The environment keeps only `PATH`, `TERM`, `LANG` and `TMPDIR`, with no OpenRig, Claude Code, OpenAI, Codex or Contexto variables.
- **MCP home** [measured, finding A18; met in v0.1.2, PR #148].
  - Before #148, Codex started the contexto MCP server without `CONTEXTO_HOME`, so the server opened `~/.contexto`, a different store from the proxy's.
  - The harness now sets `CONTEXTO_HOME` as usual. For an older build, `child_env(mcp_home_workaround=True)` (`codex_dryrun.py --home-workaround`) points `HOME` at a scratch home and leaves `CONTEXTO_HOME` unset instead.
- **How the model reaches the Contexto tools** [measured].
  - `gpt-6-astra` gets no Contexto tool by name. The MCP tools are deferred behind the code-mode `exec` tool, a custom tool whose input is JavaScript: `await tools[<name>]({...})`, with names from `ALL_TOOLS` such as `mcp__contexto__contexto_search`.
  - `--disable code_mode` and `--disable search_tool` change nothing for this model.
  - Exec mode refuses MCP calls unless pre-approved, which A8's `default_tools_approval_mode = "approve"` (on main) handles.

## 2. Model, bound, and headroom

- **Model** [proposed]: pin `gpt-6-astra` with `model_reasoning_effort=low`. It is the only Codex model measured through the proxy (A5, A6), and low effort keeps output small. Whether a cheaper Codex-capable model exists on the owner's proxy is open (section 6).
- **Bound** [proposed]: 64,000, with 20 KB content turns, the headroom floor at its 25,000 default, and `min_turn_room` at its 12,000 default.
  - **Why 64,000:** the baseline must hold the full history inside `gpt-6-astra`'s window without Codex's own compaction (244,800 of 272,000).
  - D27 needs a history of at least 2.5 times the bound. At 80,000 with 40 KB turns, the baseline's largest request is 267,966 tokens, which does not fit (`codex_budget.estimate(...).baseline_fits` is False).
  - At 64,000 with 20 KB turns, 15 content turns give a largest baseline request of 214,370.
- **Window.** With the measured Codex head (about 15,300 real tokens, A6), W = 64,000 - 25,000 - 15,300 ≈ 23,700. That holds two 20 KB turns of about 11,100 tokens each, so rotations follow rule 4 normally rather than a minimal window every turn.
  - **Measured in the stub run:** 18 rotations for size and 2 for the room condition, over 21 turns.

## 3. The four criteria for the Responses dialect

The shared detectors read only `/v1/messages` and Anthropic usage keys. `codex_driver.analyze` reads the same session log on `/responses` and `/v1/responses`, with the Responses usage: `input_tokens` (cached included), `input_tokens_details.cached_tokens`, `output_tokens`.

1. **Long run within the bound.** It requires all of these:
   - no 4xx on model requests;
   - no `over_bound` with source `real`, read from request entries' `usage_events` (A2e) and from top-level events;
   - no failure event (`detectors.FAILURE_EVENTS`);
   - the largest real input at or below the bound.
   - **Compaction:** Codex compacts on its own only at 244,800 tokens, which a bounded H1 never reaches. Its rollout records a `compacted` item if it ever does; checking for that is a proposed addition.
2. **Fidelity.** Probe-turn answers are scored by `fidelity.l2f.score` and compared with the baseline by the same rule as `run.compare`: overall gap at most 2, and no category more than 1 below.
3. **Recall.**
   - **What counts:** `contexto_recall` and `contexto_search` calls appear as `mcp_call` events in the session log, with their returned spans.
   - **Credit:** `recall_credit.credit` hard-codes `/v1/messages` in `answer_index`, so Codex credit needs a Responses-path counterpart. This is proposed as a new `codex_*` module before the live run (section 6).
   - **Search prerequisite:** paged search could not match a Codex page; lane B's B23 fixed it, met in v0.1.3 (section 6).
4. **Cache.** The share of 200 responses with `cached_tokens > 0`. The first request and any request that rotated (its prefix changed) are not counted. This is reported only, as for Claude.

**What differs from Claude:**
- **Tools:** reached through `exec` (above), and pre-approved (A8).
- **HTTP only:** `supports_websockets = false` in the provider config. The A6 run measured no WebSocket attempts.
- **Calls outside the proxy:** in API-key mode only `GET /models` and `POST /responses` reached the proxy in A6. Any call Codex makes outside the configured provider does not reach the proxy, is not billed to the run, and is not counted, because spend is read from the proxy's log only.

## 4. Baseline (full context, same content)

[proposed] The same script, turns and driver, with no rotation:
- `--bound 1000000 --allow-large-bound`, above the history;
- `-c model_auto_compact_token_limit=1000000`, so Codex does not compact on its own (the key is in codex-cli 0.154.0's config schema; source: binary strings);
- turns 1 to N sending the whole history.

At bound 64,000 and 20 KB turns, its largest request is 214,370 tokens, under Codex's compaction point and window. Unlike Claude's replay, the MCP server stays registered. The baseline's probe turns can still call tools, but with nothing paged they find nothing; their cost is inside the estimate's per-request allowance. A cheaper alternative is a Responses replay of only the probe requests from Codex's rollout (the `codex-home` rollout holds the full history). It is not built here.

## 5. Worst-case estimate (for intake's budget line)

`spikes/m2/acceptance/codex_budget.py`, `estimate(script, bound)`. It is a closed formula; every term is an upper bound:

| Term | Value | Source |
|---|---|---|
| H1 request input | at most the bound, 64,000 | the engine's bound (acceptance 1) |
| Output per request | 32,000 | `budget.Params.max_tokens_reserve` (Codex sets no `max_output_tokens`) |
| Content turns | 15 | D27: history at least 2.5 times the bound, 20 KB turns at 1.8 bytes per token |
| Probe turns | 6, of 9 probes each | D27 |
| Tool rounds per H1 probe turn | 2 | `script.MAX_RECALLS` |
| Billed retries | 1 per turn | Codex retries a dropped stream |
| Baseline request k | head 21,312 + history to k + replies | A6 seed head, the larger of seed and measured |
| Margin | 10% | proposed |

- **H1** = (15 + 6 × (1 + 2) + (15 + 6) × 1) × (64,000 + 32,000) = 54 × 96,000 = **5,184,000**.
- **Baseline** = **6,994,902** over 42 requests; its largest input is 214,370.
- **Total** = (H1 + baseline) × 1.10 = **13,396,793 tokens**.

The harness's cap is in `codex_driver.run_session`. It never starts a turn whose `spent + estimate + reserve` would pass the approved H1 share, which is `approved - baseline_worst`. Spend is read from the proxy's session log, and a turn's estimate is its request count × (bound + output).

For comparison, 64,000 with 40 KB turns gives 10,068,124. That has fewer content turns, but W cannot hold two 40 KB turns, so every turn rotates to the minimal window.

## 6. Risks and open questions

1. **Codex paged search found nothing** [measured; lane B B23]. **Met in v0.1.3** (031ca59).
   - Before B23, `store.pages.page_matches` read only Messages API blocks, and Codex pages' stub `user_text` was empty.
   - In the first dry run all 6 searches returned 0 paged hits. On main 031ca59 each returns 1 to 3 paged hits, and recall reads the page search names [measured, section 7].
2. **MCP server home** [measured; A18]. **Met in v0.1.2** (PR #148): the harness sets `CONTEXTO_HOME` as usual.
3. **Recall credit for Responses** (`recall_credit.answer_index` reads `/v1/messages`). **Answer:** a new `codex_recall_credit.py` in A16's follow-up, reusing `recall_credit` through a path parameter. It is a lane B file, so not edited here.
4. **No in-turn spend watcher.** The Claude driver's `SessionWatcher` kills a turn mid-way on projected spend; the Codex driver checks between turns only. **Answer:** add a watcher on the session log, killing the turn's process group, before the live run. Until then the per-turn worst case bounds a turn.
5. **Cheaper model.** Only `gpt-6-astra` is measured. **Answer:** ask the owner which Codex-capable models the proxy serves. The estimate scales with the request counts above, not with the model.
6. **Output allowance.** Codex sets no output cap. **Answer:** keep the 32,000 allowance per request in the estimate, and stop the run if a request's real output passes it (a check for the watcher in item 4).
7. **Baseline tool calls.** **Answer:** accept them, since the cost is inside the estimate, or disable the MCP server for the baseline if a config key is confirmed at zero spend.

## 7. Zero-spend dry run

One command, from the repository root (it needs the codex CLI on PATH and the `contexto` executable; no network beyond 127.0.0.1):

```
uv run python spikes/m2/acceptance/codex_dryrun.py "$PWD/.venv/bin/contexto" /tmp/codex-dry
```

The stub (`spikes/m2/acceptance/codex_stub.py`) serves streamed Responses replies:
- `OK` to content turns;
- on a probe turn's first request, an `exec` call whose JavaScript runs `contexto_search`, then `contexto_recall` on the first page id search names (else on the first page id the preamble's index lists, which a build before B23 needed);
- then the probe answers, which it is given, so the run checks scoring rather than model quality.

Measured run (bound 64,000, 20 KB turns, seed 20261016, main 031ca59 with B23 and A18, `CONTEXTO_HOME` set, about 16 seconds):

| | |
|---|---|
| Turns | 15 content + 6 probe; all 21 completed, every Codex exit 0 |
| Requests through the proxy | 27 `POST /v1/responses` (15 `OK`, 6 tool calls, 6 answers); 22 `GET /v1/models` |
| Rotations | 18 for size and 2 for room; largest real input 27,015 (stub tokens) |
| Acceptance 1 | holds: no 4xx, no real `over_bound`, no failure event |
| Recall | 6 `contexto_search` calls (1 to 3 paged hits each) and 6 `contexto_recall` calls on the page search named, through Codex's `exec`, 0 failed |
| Cache-hit rate | 1.0 (stub usage) |
| Score | 54 of 54 (stub answers) |
| Record | `codex-dry-run.json`: the run record, analysis, score, and the estimate |

Tests: `spikes/m2/acceptance/tests/test_codex_acceptance.py` (10 tests).
- They cover the turn command and resume by id, the isolated environment, Responses spend, the analysis, the session runner's cap and stops, the stub's reply sequence, and the estimate's formula and baseline fit.
- They also include the full dry run, which is skipped where no codex CLI is installed.
