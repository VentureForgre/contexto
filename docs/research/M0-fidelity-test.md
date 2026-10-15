# M0 fidelity test: design, plain-CLI baseline, and offline simulation

Status: phase 1 deliverable for M0 question 4 (lane 2, packet L2b). Author: build.builder-4.
Date: 2026-10-15, revised the same day for review findings. Brief: `docs/milestones/M0-infinite-session-study.md`. Policy under test:
`docs/research/M0-paging-policy.md`. Code: `spikes/m0/fidelity/`.

Every claim is tagged **[measured]** (we ran it and give the numbers), **[vendor-documented]**, or
**[inferred]**. Simulated numbers are tagged **[measured, simulated]**: they are exact outputs of
deterministic code, but no model was involved.

**Owner principle** (binding): compaction is a failure mode to avoid. A design succeeds only if it
never compacts and its recall and task accuracy equal the full-context baseline on these probes. The
size-by-size baseline itself is measured by the L2d probe (`docs/research/M0-quality-vs-context.md`).

## What question 4 asks, and what phase 1 can answer

Question 4: what does the model lose with a bounded window, and does a recall tool recover it? In
phase 1 nothing rewrites requests, so the bounded conditions cannot be run against a model. Phase 1
therefore delivers three things:

1. A test design that phase 2 can run unchanged against the rewriting prototype.
2. A live baseline on the plain CLI (full history, and full history after the CLI's own `/compact`).
3. An offline simulation of all four conditions with a deterministic reader, clearly labeled
   simulated. It shows what each policy makes *available* to the model, not what a model does with it.

## Test design

### Synthetic session with planted ground truth

`fidelity/session.py` generates a seeded session in the Messages API content-block shape (text,
`tool_use`, `tool_result`), so paging is exercised against the real protocol rules. Planted items:

| Category | How it is planted | Pinned by the ledger? |
|---|---|---|
| `early_fact` | "Please remember this: the `<subject>` is `<value>`", in the first 10% of turns | yes, as a verified fact |
| `recent_fact` | same, near the end; a control that every window keeps | yes |
| `detail` | one line inside a large `read_file` tool output, never called out | no |
| `current_decision` | a decision, then one or two later "change of plan" supersessions | yes, current value only |
| `obligation_status` | "Action item obNN: ..." opened, half of them later "done" | yes, open ones only |

Values are random codes such as `garnet-4821`, so a model cannot answer from prior knowledge.
Filler turns add synthetic log output to grow the session. Ground truth comes from the generator's
bookkeeping, never from scanning the generated text; a test checks that every planted value really
appears at its recorded turn.

### Probes and scoring

After the session, one probe per planted item asks for the current value (or `open`/`closed`). The
answer format is one line per probe, `q_<id>: <answer>`. Scoring (`fidelity/scoring.py`) is exact
match after normalization (casefold, trim whitespace, quotes, backticks, trailing punctuation). No
model judge. A wrong answer that equals a superseded value is counted separately as **stale**, which
separates "forgot" from "remembered the old decision".

### Conditions

| Condition | Upstream request |
|---|---|
| `full` | the whole history (what the plain CLI sends) |
| `window` | newest turns only, under the window rule; an `[N earlier turns omitted]` marker |
| `pinned` | `window` plus the pinned state block in the system prompt |
| `pinned_recall` | `pinned` plus a page index of stubs, in-window tool outputs over 1,500 tokens stubbed, and the `recall` tool |

The window rule, stub format, pinned block, and recall contract are specified in the policy doc and
implemented in `fidelity/policy.py`. A protocol checker validates role alternation and
`tool_use`/`tool_result` pairing for every request built.

## Results

### Live baseline on the plain CLI [measured]

Setup:
- A throwaway headless Claude Code process (`claude -p --bare --tools "" --output-format json`, CLI
  2.1.301) on model `claude-haiku-5-5`, resumed across turns by session id.
- Auth comes from `--settings`, which names the worktree's existing settings file; the CLI runs its
  `apiKeyHelper` itself.
- **Base URL.** It came from that same file, not from the inherited environment as an earlier
  version of this doc said. The file's `env` block sets `ANTHROPIC_BASE_URL`, and the CLI applies it
  over the inherited environment.
  - Reviewer-2 showed this with loopback listeners: the settings port got the connections, the
    inherited one got none.
  - In these runs both values were equal, so traffic went through the rig proxy as intended
    [measured by reviewer-2, values compared without printing them].
  - The same block also sets `CLAUDE_CODE_EFFORT_LEVEL` in the child.
  - The runner now records which source decides the base URL and whether it equals the inherited
    value.
- **Hooks.** `--bare` is what keeps the settings file's OpenRig hooks from running in the
  throwaway. Reviewer-2 verified this for `SessionStart`; the other hook events are inferred.
- No credential was read, copied, or logged. OpenRig and parent-session environment variables were
  dropped from the child.
- Lane 1's measuring proxy was not used.

`fidelity/live.py` builds the session as 12 user turns. Six turns plant items; six paste about 160
synthetic log lines each, standing in for tool output, which a headless session cannot inject.
After the build turns the session is forked twice:
- **plain**: the probes are asked against the full history.
- **compacted**: the CLI's own `/compact` runs first, then the same probes.

| Experiment | Probes | Context at probe (tokens) | plain | compacted | Context after `/compact` | Stale answers |
|---|---|---|---|---|---|---|
| E1 flagged | 13 | 28,928 | 13/13 | 13/13 | 2,515 | 0 |
| E2 dense, `--dense 4` | 37 | 29,745 | 37/37 | 37/37 | 4,274 | 0 |

E2 adds 24 unflagged `cfg <key>=<value>` lines buried in the pasted logs (category
`detail_unflagged`). Nothing in the prompt calls them out.

What this shows:
- At about 30k tokens of history, the plain CLI recovers every planted item, including the current
  value after two supersessions [measured]. The probe set and scorer work end to end against a real
  model; nothing was unparseable or unanswered [measured].
- Both runs stayed far below the auto-compaction threshold: about 29k tokens against about 967K.
  `/compact` was invoked manually.
- `/compact` cut the context about 7x to 11x and still kept all 37 values, unflagged ones included,
  in this run [measured]. So at this scale the probe set does **not** reach the point where
  compaction loses detail. Claude Code compacts automatically only near the model's context limit
  (vendor-documented: about 967K tokens for 1M-window models), where a bounded summary covers far
  more material. Loss is plausible there, but not shown here [inferred].
- Finding for lanes 1 and 2: in `--output-format json`, the `/compact` call reports zero `usage`,
  but the session's cumulative `modelUsage` grows by 34,358 tokens for a 29.7k context [measured,
  E2; the figure is derived from recorded cumulative `modelUsage`]. The cost of compaction is invisible in per-turn usage, so a cost model built on per-turn usage
  undercounts compaction [inferred].

Smallest informative N: 12 build turns at about 30k tokens was enough to validate the harness and
to show that compaction at this scale is lossless on these probes. Producing loss needs a session
near the auto-compaction threshold. Even with Haiku and caching, that costs more than the 500k
per-experiment cap (see open questions).

### Token spend [measured unless marked]

Totals count `input_tokens + cache_creation_input_tokens + cache_read_input_tokens + output_tokens`,
the conservative figure the brief's caps are checked against.

| Experiment | Build | Plain probe | `/compact` | Compacted probe | Total |
|---|---|---|---|---|---|
| smoke (one call) | | | | | 121 |
| E1 flagged | 192,590 | 29,345 | about 31,500 [inferred: not captured; context 28,928 plus summary] | 2,890 | 224,825 measured, about 256,300 with compact |
| E2 dense | 198,358 | 32,315 | 34,358 (derived) + 34,643 | 5,595 | 305,269 |

- E2 ran `/compact` twice. The first attempt was stopped by a bug in the runner's cap guard, since
  fixed: it read cumulative `modelUsage` as a per-call figure and halted before the compacted probe.
  The second attempt is a `--compact-only` continuation of the same build session. Both compactions
  are counted.
  - The raw summary file still shows the bugged 463,389 running total; 305,269 is the corrected
    figure.
  - Continuations now start from the experiment's corrected prior spend, and spend is also checked
    after every call.
- Packet total: about 561,700 tokens, of which 530,215 were measured. That is under the 3M packet
  cap, and each experiment stayed under the 500k cap.
- Cost reported by the CLI for E2's build session and its forks was under $0.02 per fork.
- `--bare --tools ""` keeps the base prompt to about 120 tokens. The real interactive CLI also sends
  its system prompt and tool definitions every turn. That matters for cost (question 3, lane 1's
  measurements) but not for whether a planted value is visible.

### Offline simulation of all four conditions [measured, simulated]

`python3 -m fidelity.simulate` (default seed 20261015) generates 242 turns (882 messages, about
196k estimated tokens of history). It replays the session turn by turn, building the request after
every turn under each condition with default settings:
- window: high 24,000, low 12,000
- pinned budget: 2,000
- tool outputs paged over 1,500
- 8-turn page groups

**Checks on every request**
- protocol validity;
- the truth of the pinned obligations header, against ground truth as of the turn it names;
- whether the request only appends to the previous one.

**Probes.** The reader answers the probes at four points: a quarter, half, three quarters, and the
end of the session. Each point is scored against the values current then, which gives 84
probe-answers in total, 24 of them at the end. An earlier version scored only the final request,
which hid a false pinned header (review finding B2).

Token figures are estimates (characters / 4). The live runs showed this undercounts numeric log
text by about 2.2x, so read them as relative sizes.

| Condition | Final request (est. tokens) | Cumulative over session (est.) | Window moves | Edit points | Recalls (final) | Score, final | Score, 4 points | early | detail | decision | obligation | recent |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| full | 195,601 | 22,720,319 | 0 | 0 | 0 | 24/24 | 84/84 | 5/5 | 6/6 | 4/4 | 8/8 | 1/1 |
| window | 20,451 | 3,898,078 | 13 | 13 | 0 | 2/24 | 7/84 | 0/5 | 0/6 | 1/4 | 0/8 | 1/1 |
| pinned | 20,670 | 3,945,818 | 13 | 13 | 0 | 18/24 | 66/84 | 5/5 | 0/6 | 4/4 | 8/8 | 1/1 |
| pinned_recall | 22,010 | 4,117,517 | 13 | 13 | 14 | 24/24 | 84/84 | 5/5 | 6/6 | 4/4 | 8/8 | 1/1 |

The category columns are for the final point.

What this shows, and what it does not:
- **Edit points equal window moves.** Every rewrite of earlier content happens at an epoch, and
  every other request only appends. That is the structural property the policy's preserved-thinking
  strategy depends on (policy doc, section 7) [measured, simulated].
- **The scores are largely by construction.**
  - Pinned scores 18/24, missing exactly the details, because the generator's ledger records
    exactly the pinned categories (perfect capture).
  - Pinned plus recall scores 24/24 partly because every probe's hint word appears in the turn
    headline the generator writes.
  - These two scores follow from the generator's design. They are not evidence that the policy
    works on real sessions, only that the policy code does what it specifies.
- A bounded window without either loses almost everything older than the window [measured,
  simulated].
- The bounded conditions cut cumulative input by about 5.5x to 5.8x over 242 turns. The window start moved
  13 times, so with prompt caching the prefix would be rebuilt about 13 times rather than on every
  turn [measured, simulated]. Lane 2's cost model (packet L2a) owns the priced comparison.
- The reader is a deterministic upper bound. It finds values by exact phrasing, resolves conflicts
  by turn order, and recalls a page when the probe's hint word appears in its stub. A model may
  miss what is available, recall the wrong page, or prefer a stale value [inferred]. Phase 2 must
  run the same probes against a model through the prototype before any fidelity claim is made for
  the hypothesis.
- No stale answers occurred under any condition. The pinned block carries only current values, and
  the window holds the newest statements [measured, simulated].

## How to run

From the repository root:

```sh
uv run --with pytest pytest spikes/m0/fidelity        # generator, policy, scorer, reader, runner
cd spikes/m0
python3 -m fidelity.simulate                          # offline table above
python3 -m fidelity.live --out ../../.contexto/spikes/l2b              # dry run: script sizes only
python3 -m fidelity.live --out ../../.contexto/spikes/l2b --live \
    --settings ../../.claude/settings.local.json [--dense 4] [--cap 500000]
```

Live output is `.contexto/spikes/l2b/baseline-<UTC time>/summary.json`, which is gitignored:
- per call: phase, usage numbers, timing, and character counts;
- per run: scores, wrong probe ids, and stale probe ids.

Prompts, responses, and answers are not written. The CLI's own session files land under
`~/.claude/projects/` for the throwaway run directory, as for any Claude Code session.

## Threats to validity

- **Planted phrasing.** Planted values use fixed phrasing, which makes them easy for a summary or a
  model to spot. Real sessions bury decisions in prose [inferred].
- **Perfect capture.** The pinned condition assumes every fact, decision, and obligation was
  captured into the ledger when it happened. Capture is the open problem, not paging [inferred].
- **Pasted logs, not tool calls.** The live baseline pastes logs as user text instead of producing
  real tool calls. The tool-use path is only exercised offline [measured].
- **One run each.** Each live experiment ran once on one model, so no variance is measured.
- **No thinking blocks.** The synthetic session has none and the reader is not a model. The
  simulation checks only that history changes at epochs, not the accuracy cost of stripping
  thinking there. The L2d strip-thinking arm measures that cost.

## Open questions

1. **Compaction loss at scale.** Measuring loss from built-in compaction needs a session near the
   auto-compaction threshold. One option is to lower it with `CLAUDE_CODE_AUTO_COMPACT_WINDOW=100000`
   (vendor-documented range 100000 to 1000000) in the throwaway process's environment. With
   caching, a run to 100k tokens costs roughly 1M to 2M tokens in total, so it needs intake's
   approval over the 500k per-experiment cap [inferred].
2. **Phase 2 run.** Run the same probes against a model through the rewriting prototype for
   `window`, `pinned`, and `pinned_recall`. This measures the gap between the simulated upper bound
   and real use, especially recall selection.
3. **Lane 1 proxy numbers.** Use lane 1's measuring proxy for per-request sizes of the full
   interactive CLI (system prompt and tools included), to replace the `--bare` approximation in cost
   figures.
