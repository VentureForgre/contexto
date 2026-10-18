# Where Claude Code 2.1.302 puts reminders in long sessions (zero model spend)

Question: does real Claude Code put `<system-reminder>` blocks in later turn-start user messages? Contexto
carries every context block of every paged turn start, uncapped (`carried_content`, rewrite.py:493).
The 30M scale test (`../proxy`, `mixed` profile) assumed such blocks and found carried content growing
about 215 tokens per turn until the session broke the 150,000 bound. This spike measures what the CLI
actually sends.

Claude Code **2.1.302** (`~/.local/bin/claude --version`), measured 2026-10-17. Every number here is
**measured** from captured request bodies unless it is marked **inferred**.

## Short answer

- **It depends on the model id.** On the 4.x ids (claude-sonnet-4-6, claude-opus-4-7, claude-sonnet-4-5,
  claude-haiku-4-5), Claude Code puts reminders in later turn-start user messages as
  `<system-reminder>` text blocks, every turn. Contexto carries these blocks uncapped. On the 5.x ids
  (claude-opus-5-5, claude-haiku-5-5, and the ids claude-sonnet-5 and claude-fable-5), no reminder
  leaves `messages[0]`. The same content arrives as untagged role-`system` messages after the turn's
  prompt or after a tool result.
- **4.x: the blocker is real.** Carried context blocks grew 45 tokens per turn with bare prompts, 215 per
  turn in a feature-heavy session run in one process, and 425 per turn when every turn was a new
  `--resume` process (Contexto's own estimator). The fallback never pages these blocks.
- **5.x: per-turn growth goes to role-`system` messages.** Contexto classifies most of them as
  carried text, which the 8,000-token fallback can page. The exception: an `--effort` change puts
  `output_config` on that turn's first system message. Contexto then classifies the message as
  session-start, and it is carried forever. That added 27 to 3,444 tokens (bytes/1.83) per effort change, depending
  on what the message held.

## Commands

```
python3 -m venv .venv && .venv/bin/pip install -q -e . pytest
spikes/m2/scale/reminders/run_all.sh 48          # about 1 minute; prints the temp dir with raw bodies
spikes/m2/scale/reminders/analyze_all.sh <dir>   # re-analyse a kept run dir into results/
```

| Script | What it does |
|---|---|
| `scripted_model.py` | Loopback fake Messages API (SSE and JSON). It saves every request body to `<run>/bodies/` (outside the repo) and never logs headers. The turn's prompt carries `STEP[a|b|...]`, and the model sends one tool_use per action in order (Read, Edit, Write, Bash, TaskCreate, TaskUpdate, TaskList, TodoWrite, EnterPlanMode, ExitPlanMode, MCP tools), skipping actions whose tool is not offered, then answers "ok" |
| `run_session.py` | Drives one session. `--mode resume` runs one `claude -p` process per turn: turn 1 `--session-id`, every 5th turn `--continue`, the rest `--resume <id>`, with plan mode and effort set by flags. `--mode stream` runs one `claude -p --input-format stream-json` process for the whole session, toggles plan mode with the `set_permission_mode` control request and effort with `apply_flag_settings {effortLevel}`. Both succeeded on every change. `--plain` sends bare prompts |
| `analyze.py` | Classifies every reminder span in every main request (requests with tools; there were 0 side requests) by location. Runs `carried_content`, `carried_tokens`, `carried_systems`, `classify_system` and `page_carried_text` from `src/contexto/rewrite.py` (imported, not changed) |
| `timeline.py`, `features.py`, `table.py`, `effort_check.py` | Per-run reminder timeline, per-feature attribution, the per-run table below, and where effort changes land |

Isolation (every launch, from `../cli/harness.py`): scrubbed env, temp `HOME` and `CLAUDE_CONFIG_DIR`, temp
project as cwd, fake `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL=http://127.0.0.1:<port>`,
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_TELEMETRY=1`, and `HTTPS_PROXY` set to the fake
server, which refuses CONNECT. lsof sampled the CLI's whole process tree, including the MCP server,
1,061 times across the 13 runs (back to back, one lsof after another). Every remote end was 127.0.0.1, with 0 non-loopback sockets and 0 refused
proxy attempts. No real model API was contacted. Tokens are bytes / 1.83 unless a column says
"Contexto", which means Contexto's own `TokenEstimator` (3.5 bytes per token for text before any usage
is observed).

## What the session exercises

The temp project has a `CLAUDE.md`, a nested `sub/CLAUDE.md`, a project skill, and Contexto's own MCP
server (`contexto mcp --session spike-reminders --home <temp>`, all five tools offered). The 48 turns
repeat a 12-turn cycle:

| Cycle turn | Actions | Before the turn |
|---|---|---|
| 1 | Read data.txt, Read notes.md (turn 1 also creates 3 tasks and starts one) | |
| 2 | Edit notes.md | |
| 3 | Read sub/inner.txt (loads sub/CLAUDE.md) | harness edits data.txt |
| 4 | Write out/f<t>.txt | |
| 5 | none | |
| 6 | Bash `ls`; effort high | |
| 7 | Edit data.txt (turns 7 and 13 update tasks) | harness edits notes.md |
| 8 | none; effort low | |
| 9 | plan mode on; Read data.txt (EnterPlanMode in cycles 2 and 4 when offered) | |
| 10 | plan mode; ExitPlanMode when offered | |
| 11 | plan mode off; MCP `contexto_state` | harness edits data.txt, sub/inner.txt, out/f<t-7>.txt |
| 12 | Read out/f<t-8>.txt | |

Task tools: TaskCreate, TaskUpdate and TaskList are the 2.1.302 todo tools. TodoWrite returned "disabled
for this session" on every model. Tasks are created on turn 1, updated on turns 7 and 13, a fourth is
created on turn 19, and then two stay open until turn 48. claude-opus-5-5 offers no task tools by default:
2.1.302 enables them only for a fixed list of older model ids or with `CLAUDE_CODE_ENABLE_TODO_TOOLS=1`
(run `opus55-stream-todo`).

## Results

### Placement and Contexto's carried content per run

The table shows the last turn's final request. "At k" means Contexto's view with every turn before the
current one paged (k is the current turn start), which is the upper bound on what it carries. The
fallback (`page_carried_text`, budget 8,000) is applied turn by turn, and its paged set is kept from turn
to turn. "Not pageable" means context blocks plus session-start-class, settings and unclassified system
messages.

| run | model | turns | later turn-start reminders n / tok | in tool_result n / tok | role-system msgs n / tok | Contexto carried at k after fallback, turn 2 -> last | context blocks at k | session-start class at k n / tok | not pageable at k, turn 2 -> last | not pageable growth per turn |
|---|---|---|---|---|---|---|---|---|---|---|
| opus55-resume | claude-opus-5-5 | 48 | 0 / 0 | 0 / 0 | 88 / 38474 | 3329 -> 12704 | 532 -> 532 | 17 / 11637 | 3302 -> 12169 | 193 |
| opus55-stream | claude-opus-5-5 | 48 | 0 / 0 | 0 / 0 | 88 / 21293 | 3329 -> 11254 | 532 -> 532 | 17 / 9272 | 3302 -> 9804 | 141 |
| sonnet46-resume | claude-sonnet-4-6 | 48 | 141 / 36037 | 53 / 3128 | 0 / 0 | 3382 -> 22953 | 3382 -> 22953 | 0 / 0 | 3382 -> 22953 | 425 |
| sonnet46-stream | claude-sonnet-4-6 | 48 | 69 / 17667 | 53 / 3128 | 0 / 0 | 3382 -> 13256 | 3382 -> 13256 | 0 / 0 | 3382 -> 13256 | 215 |
| haiku55-resume | claude-haiku-5-5 | 48 | 0 / 0 | 0 / 0 | 88 / 38245 | 3189 -> 12570 | 532 -> 532 | 17 / 11503 | 3161 -> 12035 | 193 |
| opus55-stream-todo | claude-opus-5-5 | 48 | 0 / 0 | 0 / 0 | 98 / 24235 | 3429 -> 11357 | 533 -> 533 | 17 / 9600 | 3304 -> 10133 | 148 |
| opus55-stream-plain | claude-opus-5-5 | 48 | 0 / 0 | 0 / 0 | 48 / 6720 | 3304 -> 3948 | 534 -> 534 | 1 / 2771 | 3305 -> 3305 | 0 |
| sonnet46-stream-plain | claude-sonnet-4-6 | 48 | 51 / 3210 | 0 / 0 | 0 / 0 | 3386 -> 5452 | 3386 -> 5452 | 0 / 0 | 3386 -> 5452 | 45 |
| probe-claude-opus-4-7 | claude-opus-4-7 | 12 | 16 / 4169 | 17 / 855 | 0 / 0 | 3384 -> 5686 | 3384 -> 5686 | 0 / 0 | 3384 -> 5686 | 230 |
| probe-claude-sonnet-4-5 | claude-sonnet-4-5 | 12 | 15 / 3913 | 17 / 856 | 0 / 0 | 3387 -> 5545 | 3387 -> 5545 | 0 / 0 | 3387 -> 5545 | 216 |
| probe-claude-haiku-4-5 | claude-haiku-4-5 | 12 | 15 / 3915 | 17 / 856 | 0 / 0 | 3386 -> 5545 | 3386 -> 5545 | 0 / 0 | 3386 -> 5545 | 216 |
| probe-claude-sonnet-5 | claude-sonnet-5 | 12 | 0 / 0 | 0 / 0 | 22 / 9937 | 3347 -> 5564 | 534 -> 534 | 0 / 0 | 534 -> 534 | 0 |
| probe-claude-fable-5 | claude-fable-5 | 12 | 0 / 0 | 0 / 0 | 22 / 9115 | 3192 -> 5176 | 534 -> 534 | 0 / 0 | 534 -> 534 | 0 |

All runs had 0 reminders embedded inside prompt text, 0 reminder-only user messages, 0 user text blocks
next to a tool_result, and 0 reminders dropped from the history before the last request. The probes ran
the first 12-turn cycle only. The fake upstream accepts any model id, so the claude-sonnet-5 and
claude-fable-5 rows show how the CLI's id-based behaviour places reminders. They say nothing about
whether those models exist.

### Growth over the session (sonnet-4-6, later turn-start reminders n / tok, Contexto carried context blocks at k)

| turn | stream (one process) | resume every turn | stream, bare prompts |
|---|---|---|---|
| 2 | 1 / 47; 3,382 | 1 / 47; 3,382 | 1 / 47; 3,386 |
| 12 | 16 / 4,158; 5,678 | 16 / 4,150; 5,529 | 12 / 767; 3,853 |
| 24 | 34 / 8,721; 8,237 | 46 / 11,767; 9,652 | 25 / 1,581; 4,386 |
| 36 | 51 / 13,011; 10,646 | 87 / 22,181; 15,342 | 38 / 2,396; 4,919 |
| 48 | 69 / 17,667; 13,256 | 141 / 36,037; 22,953 | 51 / 3,210; 5,452 |

Growth is close to linear, with a step at each plan-mode entry (turns 9, 21, 33 and 45). The 3,382 at
turn 2 is the `messages[0]` blocks, which on 4.x ids also hold the skill list, agent list, environment
and MCP instructions (6,255 tok by bytes/1.83).

### Which features produce reminders (whole run, outside messages[0])

From `results/features.md`. Location `ts_later` means a block in a later turn-start user message, and
Contexto carries it uncapped. `tool_result` means inside a tool result's content, which is paged with
its turn. `system_untagged` means a role-`system` message.

| Feature | 4.x (sonnet46-stream) | 5.x (opus55-stream) | Size per item (tok) | When |
|---|---|---|---|---|
| Token countdown `<total_tokens>N tokens left</total_tokens>` | 47 in turn starts, 50 in tool results | 88 role-system | 47 tagged, 27 untagged | every turn start and after every tool result; on by default in 2.1.302 (`CLAUDE_CODE_TOTAL_TOKENS_REMINDER`, default "padded-countdown") |
| Plan mode entry ("Plan mode is active...") | 4 in turn starts | 4 role-system | 2,721 | each plan-mode entry |
| Plan mode exit ("## Exited Plan Mode") | 4 in turn starts | 4 role-system | 75 / 55 | each exit |
| File changed on disk ("Note: <file> changed on disk...", with a snippet) | 8 in turn starts (stream), 80 (resume) | 8 role-system (stream), 80 (resume) | 237 to 287 | one per external change in one process; **on resume it is sent again on every launch** while the file stays unread (out/f4.txt from turn 11 to 22, and so on) |
| Task tools not used recently | 6 in turn starts, 2 in tool results | none (no task tools); 9 role-system with tools enabled | 330 (with the task list), 250 (no tasks) | about every 10 turns without task-tool use |
| Nested CLAUDE.md (sub/) | 1 in the Read tool result | 1 role-system | 79 to 100 | first Read under sub/ |
| `--effort` change | no message (top-level `output_config` only) | `output_config` added to that turn's first system message | n/a | each change |
| "The user hasn't heard from you in a while" | none | 1 role-system (opus55-stream-todo only) | 77 | once |

No reminders came from Edit, Write, Read or Bash beyond the countdown after each tool result, from the
MCP tool call, from task creates and updates, or from the resumes and `--continue` themselves. The
project CLAUDE.md and the attribution note stay in `messages[0]` on every model. On 5.x ids, one turn's
items share one system message, joined by a blank line. `analyze.py` splits them at known item starts
to attribute sizes.

### Effort changes on 5.x ids (measured, `effort_check.py`)

On 2.1.301 an effort change added a settings-only message (`{role: system, content: [], output_config}`).
On 2.1.302 with 5.x ids, the field lands on the first system message the turn already has. In
opus55-stream these were: turn 6, the countdown (27 tok); turn 7, a file note (274); turn 9, the plan-mode
entry (2,732); and so on, 16 changes in all. Because the message has content plus an extra field,
`classify_system` returns session-start. The message is then carried and never paged, and the
session-start class grew from 2,770 to 9,272 (stream) and 11,637 (resume) Contexto tokens. In this
schedule the effort reset on turn 9 coincides with the plan-mode entry, so 4 of the 16 changes carried
2.7k each. With bare prompts and no effort change, the not-pageable part stayed at 3,305 for 48 turns.

### What the fallback does on 5.x ids (measured with the engine's own function; inferred for live use)

Carried role-system text (the countdown, file notes, plan-mode text) is class carried-text. Once
carried growth passes 8,000, `page_carried_text` pages it. In opus55-resume it first fired at turn 34
(carried at k 12,704 -> 11,194) and had paged 40 messages by turn 48. From turn 34 on, carried content
at k stayed between 9,618 and 12,704, about the initial 3,329 plus the 8,000 budget plus what one turn
adds. What keeps rising underneath is the session-start class from effort changes. In the live engine
the fallback runs only at rotations. This simulation runs it every turn, so live carried size between
rotations can be higher.

## Findings

1. **[measured] 4.x ids put reminders in later turn-start user messages; 5.x ids do not.** On 4.x the
   reminders are separate text blocks starting with `<system-reminder>`, in the same user message as the
   prompt. `is_turn_start` holds for that message and `is_context_block` holds for each block, so
   `carried_content` carries them all. On 5.x, `messages[0]` keeps 2 blocks (CLAUDE.md, 656 tok;
   attribution note, 318 tok) and no later user message holds a reminder.
2. **[measured] Cause and rate on 4.x.** The countdown alone adds one 47-token block to every turn start
   (bytes/1.83). With bare prompts the total was 69 tok per turn (bytes/1.83), because the task-tools
   reminder joins the countdown, and 45 per turn in Contexto's estimator. The feature-heavy session run
   in one process reached 383 tok per turn by bytes/1.83 and 215 in Contexto's estimator. Plan-mode
   entries were 62 percent of that (10,883 of 17,667), then the countdown, task reminders and file
   notes. Resuming on every turn doubled it to 782 by bytes/1.83 and 425 in Contexto's estimator,
   because file-changed notes repeat on each launch (80 against 8).
3. **[measured] Contexto's carried content grows per turn on 4.x ids, and nothing pages it.** Carried
   context blocks at k grew 3,382 -> 5,452 (bare prompts), 3,382 -> 13,256 (one process) and
   3,382 -> 22,953 (resume) over 48 turns. The scale test's assumed `mixed` profile (297 bytes, about 162
   tok per turn by bytes/1.83) falls between the measured floor (126 bytes per turn) and the
   feature-heavy one-process rate (701 bytes per turn).
4. **[measured] On 5.x ids the per-turn growth is pageable, except at effort changes.** Role-system
   messages grew 343 tok per turn by bytes/1.83 in the feature-heavy stream run. Most of that is carried
   text, which the fallback pages. The part the fallback cannot page grows only at `--effort` changes:
   0 per turn with bare prompts, and 141 to 193 per turn in this schedule, which changes effort 4 times
   every 12 turns.
5. **[measured] A Claude Code change since 2.1.301 that the contract does not cover.** The countdown
   reminder is on by default. On 5.x ids, effort settings now ride on a content-bearing system message
   and no longer arrive as a settings-only message. Contexto's classifier was written against 2.1.301
   (`HOOK_SIGNATURES_CLAUDE_CODE_VERSION`) and treats these messages as session-start.
6. **[inferred] How fast it fails at scale.** In the 30M scale test, carried growth of about 215 tok
   per turn (Contexto's estimator) first hit `bound_too_small` at turn 181. That failure point depends on
   carried growth per turn, not on history size. Scaling linearly gives about turn 860 for a 4.x
   session of bare prompts (45 per turn), about turn 180 for the feature-heavy one-process session
   (215), and about turn 90 if every turn is a resume (425). This maps this spike's estimator tokens
   (3.5 bytes per token) onto the scale test's engine, which had usage feedback, so the turn numbers
   are a rough guide: by bytes per turn the busy one-process rate (701) is 2.4 times the scale test's
   assumed profile (297), so the real failure may come earlier. For 5.x ids the bound breaks only if
   effort changes keep coming. Each one adds the size of that turn's first system message, about 27 to
   2,700 tokens. Real per-turn rates depend on how often a user enters plan mode, edits files outside
   the CLI, leaves tasks idle and resumes. None of these was measured on real usage.

## Conclusion

Yes, on some models. Claude Code 2.1.302 on the 4.x model ids (claude-sonnet-4-6, claude-opus-4-7,
claude-sonnet-4-5, claude-haiku-4-5) puts `<system-reminder>` blocks in every later turn-start user
message. The token countdown appears on every turn, a plan-mode block (2.7k) on each plan-mode entry, a
file-changed note (about 260) on each external edit, repeated on each resume until the file is read
again, and a task-tools reminder (250 to 330) about every 10 turns. Contexto carries all of them
uncapped. Measured with Contexto's estimator, carried content grows 45 tokens per turn at the floor,
about 215 per turn in a busy one-process session (the rate the scale test assumed), and about 425 per
turn when every turn is a resume. So the blocker is real for 4.x ids.

On the 5.x ids (claude-opus-5-5, claude-haiku-5-5), reminders
never leave `messages[0]`. The same content comes as role-`system` messages that the 8,000-token
fallback can page. The exception is an `--effort` change: it makes that turn's system message
session-start class, carried forever. Contexto's carried content therefore grows on 5.x ids only at
effort changes (27 to 3,444 tokens each, bytes/1.83), not per turn.

## Limits

- The model is scripted. Only the CLI's own behaviour is measured. A real model calls tools at other
  rates, and real turns have more tool calls, each followed by a countdown that goes into the tool
  result (paged) on 4.x or a role-system message on 5.x.
- No IDE integration, no hooks (measured in `../../carried`, findings 7 to 10), no output-style changes,
  no compaction. EnterPlanMode and ExitPlanMode were never offered in these modes, so those actions
  were skipped. Plan mode was entered and left through the permission mode only.
- The external edits are deliberate and frequent (3 per 12 turns). Effort changes (4 per 12 turns)
  are far more frequent than in typical use.
- Raw bodies are not committed. They contain Claude Code's own prompt text from the temp environment
  and stay under the printed `/tmp/rem-runs.*` directory.

## Files

`results/table.md` (the per-run table), `results/features.md`, `results/summary.md` (per-turn tables
and kinds per run), `results/turns.csv` (one row per turn per run, with the Contexto columns),
`results/requests.csv` (one row per main request), `results/reminders.csv` (one row per distinct
reminder occurrence: run, message index, location, kind, bytes, first and last request seen), and
`results/timeline-*.csv`.
