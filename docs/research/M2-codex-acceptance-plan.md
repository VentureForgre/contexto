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

- **Model** [proposed]: pin `gpt-6-astra` with `model_reasoning_effort=low`. It is the only Codex model measured through the proxy (A5, A6). A cheaper model is open (section 6).
- **Bound: 64,000** [proposed]. Two conditions fix the range:
  - The full-context baseline must stay under Codex's compaction point, about 245,000 tokens (244,800 of `gpt-6-astra`'s 272,000 window).
  - Acceptance 1 needs a history of at least 2.5 times the bound.
  - So the bound must sit near or under about 98,000. 64,000 leaves room for the head, the probes, their answers, and the output allowance: the last baseline request is 210,370 in plus 32,000 out.
  - The shipped default of 150,000 goes untested on Codex. Its 2.5-times history is 375,000 tokens, which no full-context baseline can hold (intake records this in STATUS). Claude's acceptance ran at 150,000.
- **Turns of 20 KB.** With the measured Codex head (about 15,300 real tokens, A6), W = 64,000 - 25,000 (headroom floor) - 15,300 ≈ 23,700. That holds two turns of about 11,100 tokens each, so rotations follow rule 4 normally; with 40 KB turns every turn would rotate to the minimal window.
  - `min_turn_room` stays at its 12,000 default.
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

[proposed] **A replay of the 6 probe requests, built as Claude's D27 baseline** (`run.py replay`):
- **What each request carries.** Each of the 6 requests carries the full content history: every content turn and its `OK` reply, taken from Codex's rollout (the session's `codex-home` holds it in full).
- **Earlier probe turns.** It also carries the earlier probe turns as the baseline itself asked and answered them. The probes are sent in order and each answer is appended before the next request, so these are the baseline's own answers.
- **Dropped from the rollout:**
  - H1's tool calls and their results (the `exec` calls and outputs, and with them every search and recall);
  - H1's reasoning items;
  - H1's answers to the probe turns.
- **Why:** the arms must be independent. The baseline measures what the model answers from the full context alone. Had it seen H1's retrievals or answers, its score would include H1's work, and the fidelity gap (acceptance 2) would shrink toward zero whatever H1 did.
- **No tools.** No tool definitions are sent, so there is no MCP server and there are no tool rounds.
- **Content turns are not repeated** as separate requests; they are only carried as history.
- Requests go straight to the upstream, so Codex's own compaction does not apply. Each must fit the 272,000-token window with its output allowance: 210,370 + 32,000 at 64,000.
- It sends no Codex client marker; D23 applies.
- It is not built yet (section 6).

**The first draft** ran the whole session again with no rotation (`--bound 1000000 --allow-large-bound -c model_auto_compact_token_limit=1000000`). It is kept in the estimate for comparison (`baseline_worst`), at about 2.5 times the replay's cost (3,497,451 against 1,383,936 before the margin).

## 5. Worst-case estimates (for intake's budget line)

`spikes/m2/acceptance/codex_budget.py`: `estimate`, `replay_baseline`, `baseline_breakdown` and `staged`. `uv run python spikes/m2/acceptance/codex_budget.py` prints the figures below. Each is a closed formula, and every term is an upper bound.

| Term | Value | Source |
|---|---|---|
| H1 request input | at most the bound, 64,000 | the engine's bound (acceptance 1) |
| Output per request | 32,000, reasoning included | `budget.Params.max_tokens_reserve`, the reserve the Claude runs used. `gpt-6-astra`'s maximum output is not documented in what this plan can read: no limit appears in codex-cli 0.154.0's binary strings, and the A5 and A6 notes do not record one. Codex sends no `max_output_tokens` (measured: its request has no such field) and has no client-side cap. The Responses `output_tokens` include reasoning. **Open:** the owner or the proxy's model list gives the real maximum. Until then the watcher bounds the risk: a request that outputs more than 32,000 is logged, and the next poll stops the turn once spent + 2 x 96,000 would pass the approval. |
| Tool rounds per H1 probe turn | 5 | run #3 measured up to 5, and its record proposes 5 (reviewer-1); Codex makes one `exec` call per round |
| Retries | 0 billed | `codex_driver.RETRY_PIN` sets the provider's `request_max_retries` and `stream_max_retries` to 0. Measured against a 500-ing stub: 30 POSTs for one turn by default, 1 when pinned. A failure ends the turn and the session. |
| Content turns | 15 | D27: history at least 2.5 times the bound, at 20 KB per turn (1.8 bytes per token) |
| Probe turns | 6, of 9 probes each | D27 |
| Codex head per request | 21,312 | A6 pre-flight seed estimate of the first request: developer instructions, `additional_tools` (the `exec` code-mode tool, whose description carries the deferred MCP tools note, plus the clock and collaboration tools), and the environment context. The live head measured 15,309; the larger figure is used. |
| Baseline tool rounds | 0 | the replay sends no tool definitions, so no MCP server |
| Margin | 10% | on each step |

**Staged design.** Each step is its own approval and its own stop cap, and step 2 runs only if step 1 passes.

| Step | Requests | Worst case |
|---|---|---|
| 1. H1 arm alone (acceptance 1 and 3) | 15 + 6 × (1 + 5) = 51, each at most 96,000 | 51 × 96,000 × 1.10 = **5,385,600** |
| 2. Baseline replay + scoring (acceptance 2) | 6 probe requests, history up to 210,370 + 32,000 out | 1,383,936 × 1.10 = **1,522,330** |
| Total | | **6,907,930** |

**With reviewer-1's stricter retry term** (2 billed retries per turn, not pinned):
- Step 1: 93 requests, 9,820,800.
- Step 2: 4,566,989.
- Total: 14,387,789.

**Against reviewer-1's figures** (15,297,593 with T = 5, and so on): those price the session baseline, which sends every content turn again (3,497,451 once, before the margin). This plan prices the 6-request replay. Claude's run #3 baseline is the same replay shape: 6 requests, 2,872,721 worst.

**Breakdown of the session baseline**, kept for comparison (`baseline_breakdown`):
- 21 requests: head 21,312 × 21 = 447,552, plus history 2,377,899, plus output 32,000 × 21 = 672,000, giving 3,497,451.
- The first draft's 6,994,902 doubled that for a retry allowance. Retries are now pinned to 0.

**The stops in each step** (`codex_driver.run_session`):
- A turn is never started when spent + its estimate + reserve would pass the step's approval.
- Inside a turn, the spend watcher (`SpendWatch`, polled every 0.25 s) terminates the turn's process group (contexto, codex, the MCP server) once spent + 2 × 96,000 would pass it. Twice one request's worst case covers a request that started before the last one was logged.
- **The watcher fails closed** (intake 11:47Z and 11:48Z). The turn is stopped on any of:
  - a malformed complete (newline-terminated) line in the session log;
  - an unreadable log;
  - any exception inside the watcher;
  - a log that stays missing;
  - an unterminated last line still open past the grace, which is at most 2 polls and at most 5 seconds, whichever comes first.
  - An unterminated last line is a write in progress: it is never parsed, only re-read. The session then ends with the reason recorded.
- **Resume after a failure.** With retries pinned, a failed request ends the turn and the session (the turn does not count as completed). To resume:
  - rerun the driver with `start` = the record's `turns_completed` and the record's `thread_id` (`run_session(..., start=...)`, `CodexTurns.thread`);
  - it continues the same Contexto session and Codex thread from the turn that failed;
  - spend is read from the same session log, so the step's cap counts what the failed attempt was billed;
  - the new record shows `started_at`.
  - If Codex recorded the failed turn's prompt in its rollout, the resumed turn sends it again. That one-turn duplicate is noted in the run record.
- So spend stays at or under the approval as long as no single request passes its 96,000 worst case. A request that does is still logged and fails acceptance 1.

## 6. Risks and open questions

**Must be in place before any live step:**
1. **In-turn spend watcher, fail closed.** Implemented: `CodexTurns` runs each turn in its own process group, and `SpendWatch` polls the session log.
   - Tests cover the spend stop and the quiet case.
   - They cover a half-written line completed within the grace, a line still open after it (by polls and by seconds), a malformed complete line, and a missing log.
   - A parametrized test kills a real process group for an open line, a malformed line, and an exception inside the watcher.
2. **Retries pinned.** Implemented (`RETRY_PIN`) and measured: 1 POST per failing turn, where the default sent 30.
3. **Recall credit for the Responses paths.**
   - Step 1's acceptance 3 verdict needs it: `recall_credit.answer_index` reads `/v1/messages`.
   - Answer: a new `codex_recall_credit.py` that reuses `recall_credit` with the Responses paths. `recall_credit.py` is lane B's file. Not built yet.

**Before step 2 only:**

4. **The probe-request replay baseline** (section 4): build it from the Codex rollout, with no tools and the D23 User-Agent.

**Can wait (a cost or a decision, not correctness):**

5. **Cheaper model.** Ask the owner which Codex-capable models the proxy serves.

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

Measured run (bound 64,000, 20 KB turns, seed 20261016, main 031ca59 with B23 and A18, `CONTEXTO_HOME` set, Codex retries pinned, spend watcher on, about 16 seconds):

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

Tests: `spikes/m2/acceptance/tests/test_codex_acceptance.py` (21 tests).
- They cover the turn command (resume by id, retries pinned), the fail-closed in-turn spend watcher, resume after a failed turn, the isolated environment, Responses spend, the analysis, the session runner's cap and stops, the stub's reply sequence, and the estimate's formula and baseline fit.
- They also include the full dry run, which is skipped where no codex CLI is installed.
