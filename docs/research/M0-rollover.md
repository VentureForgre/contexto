# M0 rollover: H4 supervisor rollover evidence

Lane 1, packet L1-c. Author: build.builder-2 (rerouted from build.builder-1). Date: 2026-10-15.
Brief: `docs/milestones/M0-infinite-session-study.md`, hypothesis H4: wrap the CLI in a terminal
supervisor, detect a filling context, and start a fresh session seeded with a handoff, roughly what
OpenRig does today. Lane 2 owns H4 cost modeling; this document supplies the inputs.

Tags: **[measured: ...]** produced by a command here (section 6); **[measured by <seat or packet>]**
measured elsewhere and cited; **[documented: ...]** vendor or OpenRig documentation or source;
**[inferred]** not verified. OpenRig sources refer to the installed `@openrig/cli` 0.5.15, written as
`<openrig>/...` for its package directory (`daemon/dist` is the compiled daemon).

Owner principle (binding): compaction is a failure mode to avoid. H4 is judged first by how small it
keeps working context and how stable quality stays, then by token cost. Default compaction settings
only (D3); no threshold overrides were used.

## Summary

1. **H4 keeps working context in a sawtooth with a measurable floor.** A fresh Claude Code session
   seeded with a handoff starts at 44,500 tokens of context (system prompt, tools, inherited
   `CLAUDE.md` and rig onboarding text in this worktree) plus the handoff: +4,537, +18,099, and
   +72,367 tokens for 8 KB, 32 KB, and 128 KB synthetic handoffs [measured]. A real OpenRig seat
   starts higher: 81,808 tokens on `lab.subject`, about 36K of OpenRig onboarding over a 45,925
   throwaway [measured by L1-a, builder-1; doc pending]. On Codex, the `labcodex.subject` seat had
   32,331 tokens in use after OpenRig onboarding (58,985 tokens over 2 requests) [measured, PR #10].
2. **Recall of the handoff held at every size tested** (one synthetic key per handoff run, 3 of 3
   correct; the two empty runs asked only for "OK")
   [measured]. This is a single probe per size, not the fidelity baseline.
3. **A rollover is fast and can reuse the prompt cache.** Each seeded one-shot run took 2.5 to 4.3 s
   wall-clock [measured]. Back-to-back fresh sessions read the shared prefix from cache in 3 of 4
   runs (44,498 tokens with no handoff; 27,202 tokens of system and tools when a handoff sat in the
   first message); the 128 KB run read nothing despite an identical request shape [measured; cause
   inferred, section 3].
4. **OpenRig detects pressure from what the CLI reports about itself** (Claude's status-line JSON;
   Codex rollout `token_count`) and polls it every 30 s [documented]. On this rig every seat's
   reading is hidden as `stale_generation` by a timezone bug, and no automatic "context full, hand
   over" trigger is configured [measured by the read-only OpenRig study, section 1].
5. **Across a rollover the live conversation is lost**; durable artifacts, queue rows, the native
   session file, and the seat identity are kept [documented]. Quality depends on what the handoff
   carries.

## 1. How OpenRig does it today (read-only study)

All from the installed OpenRig 0.5.15, studied without changing any seat, queue, or setting.

### Detection

- **Claude Code:** on launch OpenRig merges a `statusLine` command into the seat's
  `.claude/settings.local.json`. The command copies Claude's status-line JSON
  (`context_window.used_percentage`, window size, token totals, `session_id`, `transcript_path`,
  `session_name`) into a per-seat sidecar under `~/.openrig/state/context-usage/` [documented:
  `<openrig>/daemon/dist/adapters/claude-code-adapter.js:608-633`,
  `<openrig>/daemon/assets/claude-statusline-context.cjs:25-47, 76-89`]. The status line refreshes
  only while the TUI renders, so an idle or hung seat produces no new samples [inferred].
- **Codex:** OpenRig finds the thread's rollout through Codex's state database, tails it, and takes
  the newest `token_count` event; used percentage is `last_token_usage.total_tokens /
  model_context_window` [documented: `daemon/dist/domain/context-usage-store.js:90-106, 140-174`].
- **Poller:** every 30 s into SQLite; a reading is fresh for 10 minutes [documented:
  `daemon/dist/domain/context-monitor.js:3, 131-141`; `context-usage-store.js:7, 272-279`].
- **Measured defect on this host:** every seat in the `contexto` rig reads
  `availability: unknown, reason: stale_generation`, although the database holds fresh `known`
  samples [measured: `rig ps --nodes -A --full --json` shows `unknown` for all 11 seats; the OpenRig
  study's read-only `rig whoami --json --full`, `rig compact-plan --rig contexto` (0 candidates,
  10 blocked) and a read-only SQLite query]. Cause: occupant `boot_at` is stored as SQLite
  `datetime('now')` (UTC without a zone suffix) and parsed by Node as local time (UTC-5 here), so
  every sample looks older than the boot for about 5 hours [documented:
  `daemon/dist/db/migrations/060_occupant_tenures.js:26`, `context-usage-store.js:34-38`; measured:
  `node -e` parse check and `date +%z`]. This is an OpenRig defect, reported to intake through
  manager-1.

### Acting on it

- **Claude auto-compaction enforcer:** opt-in, default off, 80% threshold; sends a prep turn,
  `/compact`, a restore prompt, and a read-depth audit [documented:
  `daemon/dist/domain/claude-compaction-enforcer.js:5-57`; measured: `rig config`].
- **Watchdog `context-usage-threshold`:** measures transcript bytes (113K to 153K tokens per MB),
  fires once per occupant generation; spec-driven seats with `managed-compaction` or
  `apprentice-handover` get prepare and cutover jobs at about 600K and 900K tokens [documented:
  `daemon/dist/domain/policies/context-usage-threshold.js`,
  `daemon/dist/domain/continuity-policy-materializer.js:51-105`]. None are registered on this host
  [measured: `rig watchdog list --json`].
- **Health detector:** warning at 95%, critical at 99% [measured: `rig config`].
- **Skill doctrine:** a planned handover is triggered "near your context threshold (~85%)"
  [documented: OpenRig skill `retiring-and-inheriting-a-seat`].
- **No automatic rollover:** with the shipped defaults, handover is started by an operator or agent
  [documented and measured, as above].

### Handover, restore, and compaction recovery

- **`rig handover <seat>` (default `fresh`):** captures the old pane's screen, resolves a recap of at
  most 6 exchanges of at most 500 characters from the native session file, respawns the CLI in the
  same tmux pane with the seat's pinned model, pastes a restore packet (screen capture, recap, pointer
  to the seat's `RECAP.md`), then mints a new occupant generation and rebinds the seat [documented:
  `daemon/dist/domain/seat-handover-service.js:201-298, 580-596, 635-681, 824-866`]. Other sources:
  `fork` (`claude --resume <id> --fork-session`), `rebuild` (primed from `RECAP.md`, `LEARNED.md`,
  the latest restore packet), `discovered` [documented: `rig handover --help`].
- **Startup context:** the successor also gets the seat's startup files (`role.md`,
  `startup/context.md`, culture files, onboarding) at launch [documented:
  `daemon/docs/reference/agent-startup-guide.md`; measured: `rig restore-check --rig contexto
  --full`]. That is the onboarding overhead in summary item 1.
- **Restore:** each session keeps `resume_type` (`claude_id` or `codex_id`), `resume_token`, and
  `restore_policy` (default `resume_if_possible`); a seat with no token stops at
  `awaiting-decision` rather than silently starting fresh [documented:
  `daemon/dist/db/migrations/006_resume_metadata.js`,
  `daemon/dist/domain/restore-orchestrator.js:771-799`].
- **Compaction recovery:** a PreCompact hook writes a restore packet and marker, and a SessionStart
  (`compact`) bridge injects one restore directive [documented:
  `daemon/assets/plugins/openrig-core/hooks/claude.json`]. On this rig those hooks are not in the
  seats' settings (only activity hooks), and `rig restore-check` flags them missing [measured].

## 2. Signals a supervisor can observe

| Signal | What it gives | Reliability | Evidence |
|---|---|---|---|
| Claude status-line JSON (`statusLine` command stdin) | exact used percentage, window size, token totals, session id, transcript path | Exact when rendered; no samples from an idle or hung TUI | documented (section 1); OpenRig's primary source |
| Claude session JSONL `message.usage` | per-request input, cache write, cache read, output | Exact after each response; dedupe by `message.id` (streamed records repeat) | measured (section 3) |
| Codex rollout `token_count` | per-request usage and `model_context_window` | Exact after each response | measured (PR #10) |
| Proxy-reported usage | the same numbers on the wire, before the CLI | Exact; needs a proxy in the path | measured by L1-a (doc pending) |
| Transcript file size | crude growth proxy (113K to 153K tokens per MB) | Coarse; content-dependent | documented (section 1) |
| Hook events (PreCompact, SessionStart `compact`, Stop) | turn boundaries and compaction boundaries | Exact but late: PreCompact fires when compaction is already due | measured (PR #9 section 1) |
| Terminal output (tmux capture) | screen text, idle state, resume prompts | Fragile; layout-dependent | documented (section 1) |
| OpenRig `contextUsage` | the status-line reading after OpenRig's freshness and generation checks | Currently blind on this host (`stale_generation` bug) | measured (section 1) |

Lesson [inferred from the defect]: a supervisor should act on the raw per-request usage, and treat
its own freshness or generation filters as a possible point of failure.

## 3. Measurements

### Seeded fresh sessions (Claude Code)

Five throwaway sessions with the lane 1 live-call recipe (section 6), `claude-haiku-5-5` pinned,
default compaction settings. Each piped a synthetic handoff (lines `fact-NNNN: key-NNNN = <value>`)
into `claude -p` and asked for one key's value. Usage from the session JSONL [measured:
`spikes/m0/rollover/run_seed.sh`, numbers in `docs/research/M0-rollover-seeded-sessions.csv`]:

| Run | Handoff | Handoff tokens (delta) | Context | Cache write | Cache read | Output | Wall-clock | Recall |
|---|---|---|---|---|---|---|---|---|
| empty-1 | none | 0 | 44,500 | 44,498 | 0 | 4 | 2.5 s | n/a |
| empty-2 (10 s later) | none | 0 | 44,500 | 0 | 44,498 | 39 | 2.9 s | n/a |
| handoff-8KB | 8,017 bytes | 4,537 | 49,037 | 21,833 | 27,202 | 103 | 3.1 s | ok |
| handoff-32KB | 32,012 bytes | 18,099 | 62,599 | 35,395 | 27,202 | 109 | 3.6 s | ok |
| handoff-128KB | 128,030 bytes | 72,367 | 116,867 | 116,865 | 0 | 143 | 4.3 s | ok |

- **Floor:** 44,500 tokens before any handoff in this worktree, which includes the inherited
  `CLAUDE.md` and rig onboarding text; that is realistic for the owner's OpenRig sessions. A real
  seat adds OpenRig's startup files and onboarding turn: 81,808 tokens at the head of `lab.subject`
  [measured by L1-a]. Handoff tokens (`handoff_tokens_delta` in the CSV) are the measured context
  minus the 44,500 floor; this synthetic format runs about
  1.8 bytes per token, so prose handoffs will be cheaper per byte [inferred].
- **Wall-clock** covers CLI start to result for a one-shot `-p` run. It does not include detecting
  the need to roll over or writing the handoff. OpenRig's own steps add a 30 s readiness wait, about
  3.2 s of resume verification, and a 200 ms paste settle; no end-to-end handover time is documented
  [documented, section 1].
- **Cache across sessions:** a fresh session 10 s after another read the full 44,498-token prefix
  from cache. With a handoff in the first user message, only the system and tools part (27,202) was
  read and the rest written. The 128 KB run read nothing, although its `api-request` records had the
  same `shapeHash` (system and tools) and the same cache markers (5-minute default TTL) as the others
  [measured: `api-request` fields only]. This matches L1-a's byte-identical full cache miss on
  `lab.subject` [measured by L1-a; doc pending]. Causes on the route side are inferred and were not
  probed: the rig proxy is owner infrastructure (D11). Rule [inferred]: count cross-session cache
  reuse as likely within the TTL, never guaranteed.
- **Placement rule** [inferred from the cache rows]: put the handoff after the stable prefix (first
  user message), not in the system prompt, so a rollover can still reuse the cached system and tools.

### Codex (extrapolated, not run)

No Codex rollover was run, to save the owner's ChatGPT plan (manager-1's decision). From the
measured `labcodex.subject` onboarding [measured, PR #10]: a fresh Codex seat costs about 58,985
tokens over 2 requests to orient and holds 32,331 tokens of context before its first task. A seeded
Codex rollover would start from that floor plus the handoff [inferred, extrapolated from those
measurements].

### Grown sessions for comparison

- Codex: 32,331 to 44,552 tokens over five small turns on `labcodex.subject`, about 3,000 tokens per
  tool turn [measured, PR #10].
- Claude: L1-a's mid-turn run reached 136,866 tokens of context on its seventh request after reading
  four logs of about 50 KB [measured by L1-a; doc pending; the run violated D3 and is cited only for
  sizes].
- Reading [inferred]: a rollover resets context to the floor plus the handoff. With a floor of
  44.5K to 82K tokens on Claude and about 32K on Codex, H4 can hold working context well below either
  CLI's compaction threshold (about 967K on 1M-window Claude models, about 244.8K on Codex) if it
  rolls over early, for example at a fixed token budget rather than near the wall.

## 4. What is kept and lost across a rollover

- **Kept** [documented, section 1]: seat name, edges, inbound queue work, tmux pane, the seat
  transcript log across generations, durable artifacts (`RECAP.md`, `LEARNED.md`, queue rows), the
  predecessor's native session file (wakeable once), and lineage.
- **Lost:** the live conversation. A `fresh` handover passes one screen capture, at most 6 exchanges of
  500 characters, and pointers [documented]. In-flight tool state (a running tool call, its pending
  result) does not cross [inferred]. Unsaved reasoning: thinking blocks and encrypted reasoning are
  opaque and cannot be carried in a handoff [inferred from PR #9 section 4]. The prompt cache resets
  except for the shared prefix (section 3).
- **Failure modes** [documented unless marked]: packet delivery fails (candidate unwound, seat kept);
  a pasted packet sits unsent without a settle delay; the successor overwrites the name-keyed sidecar
  if the recap is not resolved first; resume menus need attention; the classic packet failures:
  "drops the hot potato", loses the user's intent, mistakes a resume for a handover, carries stale
  instructions; and, measured here, a supervisor blinded by its own freshness filter (section 1).

## 5. H4 against the owner principle

- **Working-context size:** H4 is the simplest way to keep context bounded with no proxy: each
  rollover resets to the floor plus the handoff. Its weakness is the floor itself: 44.5K to 82K tokens
  on Claude and about 32K on Codex before any work, paid again at every rollover [measured inputs;
  reading inferred].
- **Quality stability:** quality between rollovers stays in the low-context regime if the trigger is
  early. Each rollover is a step change: whatever the handoff omits is gone, and thinking continuity
  resets. The 3 of 3 recall result shows a handoff can be read back reliably at these sizes; it does
  not show that a handoff captures what later turns need [measured result; reading inferred].
- **Token cost (second):** each rollover costs the floor (mostly a cache read within the TTL, a full
  write otherwise) plus the handoff as a write [measured, section 3].
- **Attachment:** D16 makes a launcher wrapper the preferred phase 2 attachment layer (decision
  record pending on main). A supervisor fits that: `contexto run claude ...` can own the process,
  read per-request usage from the session JSONL or a proxy, and relaunch with a handoff, without
  editing any settings file [inferred].
- **Relation to H3 and H1:** H3 (session-file rebirth) is the same sawtooth with the seed written as a
  session file instead of a first message. H1 (bounded-prompt proxy) avoids the step change by
  managing context every turn [inferred; see `M0-interception.md`].

## 6. Reproduction

Throwaway sessions follow the lane 1 live-call recipe proven in `M0-interception.md` section 7: cwd
under this worktree's `.contexto/spikes/L1-c/` (inherits the seat credential as configured, nothing
copied), seat variables stripped (`CLAUDECODE`, `CLAUDE_CODE_*` session variables, `CLAUDE_PID`,
all `OPENRIG_*` and `RIGGED_*`), `--settings '{"disableAllHooks": true}'`, `--strict-mcp-config`,
and `--model claude-haiku-5-5`. No spike proxy was used; usage comes from the session JSONL. With no
flag-level base URL, the runs reached the model through the route set by the main checkout's
`.claude/settings.local.json`, which a git worktree also loads [measured by L1-a; recorded in
`M0-interception.md` section 7].

```sh
python3 spikes/m0/rollover/make_handoff.py <size_unit> <out.md>     # synthetic handoff of size_unit * 4 bytes
spikes/m0/rollover/run_seed.sh <label> <handoff.md> <key_index>     # one seeded run; key 0 = no recall question
python3 spikes/m0/rollover/session_usage.py <session.jsonl>           # usage per message id, hook count
```

Pre-run safety check [measured]: the first run's session file had 0 hook attachments, and
`rig ps --nodes -A --full --json` before and after showed this seat's resume token and last hook
event unchanged. Every later run also had 0 hook attachments. Throwaway transcripts stay local in
`~/.claude/projects` and are never committed.

## 7. Token spend

| Scope | Model | Tokens |
|---|---|---|
| Five seeded runs (section 3) | `claude-haiku-5-5` | 317,901 (context 317,503 plus output 398) |
| Codex | none run | 0 |

Under the 500K per-experiment cap and the 1M packet cap. The read-only OpenRig study ran as an
in-session research subagent and used 228,738 tokens of this seat's own consumption; it called no
model on the experiment path.

## 8. Open questions

- Why the 128 KB run missed the cache with an identical request shape (and L1-a's full miss on
  `lab.subject`); route-side causes are inferred and not to be probed (D11).
- The fidelity probe set and full-context baseline (builder-4) are pending; recall here is one key per
  run.
- End-to-end rollover time for a real seat (detection, handoff authoring, relaunch, readiness) is not
  measured or documented.
- Whether OpenRig's compaction-recovery hooks reach seats by a route other than project settings and
  user plugins was not established.
- The OpenRig `stale_generation` timezone defect needs an owner of the fix (OpenRig, not Contexto).
