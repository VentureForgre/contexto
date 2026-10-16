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

- **Model** [proposed]: pin `gpt-6-astra` with `model_reasoning_effort=low`. It is the only Codex model measured through the proxy (A5, A6), and low effort keeps output small. A cheaper model is open (section 6).
- **The shipped default bound, 150,000** (`launcher.DEFAULT_BOUND`), works for the H1 arm. Every H1 request stays at or below the bound, and Codex compacts from the usage the proxy returns, so its 244,800-token trigger is never reached [source: docs/research/M2-codex-adapter.md].
  - D27 needs a history of 2.5 times the bound: 375,000 tokens, or 18 content turns of 40 KB.
  - A full-context baseline of that history cannot exist for `gpt-6-astra` at all. The last probe request would need 441,733 input tokens against a 272,000-token window (`codex_budget.replay_baseline`, `fits` false). Codex compaction does not apply to a direct replay, but the model's window does.
  - So at the default bound, acceptance 1 and 3 can be tested, and acceptance 2 cannot.
- **Why 64,000** [proposed]: so that acceptance 2 is possible at all, not chiefly to cut cost.
  - It is the largest bound whose 2.5-times history, plus the head, probes and answers, fits the window with the output allowance. Its last baseline request is 210,370 input + 32,000 output.
  - The cost reduction follows from that choice.
  - **Smallest change that keeps the default bound:** none exists with this model, since the history D27 requires (2.5 times the bound) exceeds the window.
  - **Alternative:** test acceptance 1 and 3 at the default bound and acceptance 2 at 64,000. The prices are in section 5.
- **Headroom and room.** The defaults: a headroom floor of 25,000 and `min_turn_room` 12,000.
  - At 64,000 with the measured Codex head (about 15,300 real tokens, A6), W = 64,000 - 25,000 - 15,300 ≈ 23,700.
  - That holds two 20 KB turns of about 11,100 tokens each, so rotations follow rule 4 normally.
  - With 40 KB turns, every turn rotates to the minimal window.
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

[proposed] **A replay of the 6 probe requests, built as Claude's** (`run.py replay`):
- Each probe request is sent once to the upstream with the whole history before it, taken from Codex's rollout (the session's `codex-home` holds it in full), with tools off.
- The H1 arm's content turns are not repeated.
- Requests go straight to the upstream, so Codex's own compaction does not apply. Each must fit the 272,000-token window with its output allowance: 210,370 + 32,000 at 64,000.
- It sends no Codex client marker; D23 applies.
- It is not built yet (section 6).

**The first draft** ran the whole session again with no rotation (`--bound 1000000 --allow-large-bound -c model_auto_compact_token_limit=1000000`). It is kept in the estimate for comparison (`baseline_worst`), at about twice the replay's cost.

## 5. Worst-case estimates (for intake's budget line)

`spikes/m2/acceptance/codex_budget.py`: `estimate`, `baseline_breakdown`, `replay_baseline` and `staged`. `uv run python spikes/m2/acceptance/codex_budget.py` prints the figures below. Each is a closed formula, and every term is an upper bound.

**Shared terms:**

| Term | Value | Source |
|---|---|---|
| Codex head per request | 21,312 | A6 pre-flight seed estimate of the first request: developer instructions, `additional_tools` (the `exec` code-mode tool, whose description carries the deferred MCP tools note, plus the clock and collaboration tools), and the environment context. The live head measured 15,309; the larger figure is used. |
| Turn tokens | bytes / 1.8 | `budget.turn_tokens`, the dense log ratio |
| Output per request | 32,000 | `budget.Params.max_tokens_reserve`. Codex sets no `max_output_tokens`; the Responses API's `output_tokens` include reasoning tokens, so reasoning is inside this allowance. |
| Billed retries | 1 per turn | an allowance for a dropped stream that Codex retries |
| Margin | 10% | on each step's total |

**H1 arm** (bound B): every request is at most B in, plus 32,000 out.
- Requests = content turns + 6 × (1 + 2 tool rounds) + (content turns + 6) × 1 retry.
- At 64,000 with 20 KB turns: (15 + 18 + 21) = 54 requests × 96,000 = 5,184,000, or 5,702,400 with the margin.

**Where the first draft's 6,994,902 baseline came from** (64,000, 20 KB turns, session design, before the margin):
- 21 requests, one per turn: 15 content and 6 probe.
- Head: 21,312 × 21 = 447,552.
- History: the growing turns and replies, 2,377,899 in total. The request for turn k carries every earlier turn; the last one is 210,370 in.
- Output: 32,000 × 21 = 672,000.
- Once: 3,497,451. With one retry allowance per request: + 3,497,451 = 6,994,902, over the 42 requests.
- **Against Claude's run #3 baseline (2,872,721 worst):** Claude's replays only the 6 probe requests (`docs/research/M2-acceptance-run3.md`). The session design also sent 15 content-turn requests and doubled everything for retries.
- **The replay design** comes to 3,044,660 with retries and the margin, about 1.38M per pass. That is in line with Claude's.

**Staged design** (each step is its own approval and its own stop cap; step 2 runs only if step 1 passes):

| Configuration | Step 1: H1 arm (acceptance 1, 3) | Step 2: baseline replay + scoring (acceptance 2) | Total |
|---|---|---|---|
| 64,000, 20 KB turns (recommended) | 5,702,400 | 3,044,660 | 8,747,060 |
| 64,000, 40 KB turns | 4,224,000 | 3,175,683 | 7,399,683 |
| Default 150,000, 40 KB turns | 12,012,001 | not possible: 441,733 > 272,000 | 12,012,001 |
| Default for step 1, 64,000 (20 KB) for step 2 | 12,012,001 | 5,702,400 (H1 at 64,000) + 3,044,660 | 20,759,061 |

- **Recommended:** both steps at 64,000 with 20 KB turns, 8,747,060 in total.
- **If intake wants the shipped default exercised:** step 1 at 150,000, then step 2 as its own H1 and replay pair at 64,000, 20,759,061 in total.
- **The cap in each step:** `codex_driver.run_session` never starts a turn that could pass that step's approval. For step 2 the H1 share is the approval minus the replay's worst case.

## 6. Risks and open questions

**Must be fixed before any live step:**
1. **In-turn spend watcher.**
   - The Codex driver checks only between turns. The Claude driver's `SessionWatcher` stops a turn mid-way.
   - Answer: a watcher on the session log that kills the turn's process group when spent plus the in-flight allowance would pass the step's cap.
   - It also enforces the 32,000 output allowance, since Codex sets no output cap.
2. **Recall credit for the Responses paths.**
   - Step 1's acceptance 3 verdict needs it: `recall_credit.answer_index` reads `/v1/messages`.
   - Answer: a new `codex_recall_credit.py` that reuses `recall_credit` with the Responses paths. `recall_credit.py` is lane B's file, so it is not edited.

**Before step 2 only:**

3. **The probe-request replay baseline** (section 4): build it from the Codex rollout, with tools off and the D23 User-Agent.

**Can wait (a cost or a decision, not correctness):**

4. **Cheaper model.** Ask the owner which Codex-capable models the proxy serves. The estimate scales with the request counts, not the model.

**Met:**
- **Codex paged search found nothing** [measured; lane B B23]. **Met in v0.1.3** (031ca59).
  - In the first dry run all 6 searches returned 0 paged hits. On main 031ca59 each returns 1 to 3 paged hits, and recall reads the page search names [measured, section 7].
- **MCP server home** [measured; A18]. **Met in v0.1.2** (PR #148): the harness sets `CONTEXTO_HOME` as usual.

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
