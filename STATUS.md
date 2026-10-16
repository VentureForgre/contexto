# Contexto status

Updated 2026-10-16 10:15Z by intake.lead. Decisions taken and still open, with reasons:
`docs/DECISIONS.md`. Every number below is measured unless marked otherwise.

## Where the project is

Contexto keeps very long Claude Code (and Codex) sessions under a fixed context bound so the CLI
never compacts, by putting a local proxy between the CLI and the model API. The owner's bar (D2):
compaction never fires, and accuracy equals full context.

- M0 study: complete. Recommendation `docs/research/M0-recommendation.md`.
- M2 build: complete on main. Runs end to end through `contexto run claude ...` and
  `contexto run codex ...`. Contract: `docs/milestones/M2-contexto-proxy.md`.
- Acceptance: **all four live criteria passed in one run** (live run #3, main fbb1303, below).
  Contexto answered 51 of 54 probes against 48 for the full-context baseline, with zero
  compaction and every request under the bound.

## Acceptance, live run #3 (main fbb1303, claude-haiku-5-5, bound 150,000, owner's proxy)

| Criterion | Result |
|---|---|
| 1. Long run | PASS: 24 of 24 turns, 0 compaction events, max upstream input 109,868 of 150,000, 0 model 4xx, 0 local 5xx, 0 failure events (`turn_room_low` 0, `retained_stale` 0), usage on 44 of 44 requests, 6 rotations; the spend watcher stopped the CLI after the last response was fully logged and stored (exit 143, nothing lost) |
| 2. Fidelity (D27: 54 probes over 6 turns of 9) | PASS: Contexto 51, baseline 48; no category below the baseline |
| 3. Recall | PASS: the 4 probes answerable only from paged-out turns were all correct and credited through search hits from those pages (lines from the conversation in view are never credited) |
| 4. Cache | 36 of 37 eligible requests read the cached prefix (97.3 percent); the one miss is route-side (O6) |
| 5. Tests | 1071 passed, 1 skipped on main (75105f5); CI green |

Per category (Contexto / baseline): chain 28 / 25, obligations 5 / 5 of 6, multihop 8 / 8,
distractor 6 / 6, paged-only 4 / 4. Contexto missed 3 (2 chain, 1 obligations); the baseline
missed 6, every one a stale value, which fits the owner's premise that very long contexts hurt
accuracy. One run on one model, so the 3-probe lead is a measurement, not a claim of
superiority. The baseline is a no-replayed-thinking baseline (O6).

How it got here, all on record:

| Run | Protocol | Contexto | Baseline | Why the next change |
|---|---|---|---|---|
| H1 #1 | single turn | 16 | 44 | model made 0 tool calls: recall preamble V2 (D25) |
| H1 #2 | single turn | 18 | 44 | one recall used 89 percent of the turn's room: line hits, half-room cap (B15, B16, B17); 54 questions in one turn cannot fit the room even for an ideal searcher, so 6 turns of 9 (D27) |
| Live #2 (c8f0b08) | D27 | 44 | 45 | criteria 1, 3, 4 passed; chain 22 vs 24 failed by one probe: search showed old values first (D28) |
| Live #3 (fbb1303) | D27, new content | 51 | 48 | passed |

D28's targeted experiment (532,157 tokens) replayed one of live run #2's failing turns on the
fixed search before the full rerun: 5 of 9 became 8 of 9, 0 regressions.

## Token spend on live model calls (owner's proxy unless noted)

- M0 study, lane 1: about 4.9M as reported by manager-1 (documented parts in
  `docs/research/M0-proxy-measurements.md`, `M0-codex-measurements.md`, `M0-rollover.md`, and
  81,319 for the route-identity test). Lane 2: about 1.0M, summed from its research docs.
- Codex smoke: 161,549 (API-key mode). Earlier Codex seat measurement: 410,827 on the owner's
  ChatGPT plan.
- Acceptance before D27: H1 #1 1,783,815; aborted attempt up to about 65,000; recall experiment
  415,413; H1 #2 2,015,165; baseline 447,659.
- Live run #2: 5,897,784 (Contexto 3,260,912, baseline 2,636,872).
- D28 experiment: 532,157 of a 740,000 line.
- Live run #3: 6,382,449 of a 6,880,000 line (Contexto 3,748,084, baseline 2,634,365). Contexto
  used more than the modelled 2,952,139 because the model made more searches and tool-loop
  requests per probe turn than the model assumed; still under its 4,007,279 cap.

## Decisions taken in M2 and why (full text in DECISIONS.md)

- D18: build H1, context virtual memory at the API boundary; the only approach that bounds the
  context without a lossy reset.
- D19: recall through MCP tools, not a hidden proxy tool; far simpler and no shadow history.
- D20: strip all thinking at each rotation; the only strategy measured to return 200.
- D25: recall preamble V2, after the model made no tool calls in H1 #1.
- D26: acceptance fails on any off-path event, not only over-bound or 4xx.
- D27: probes over 6 turns of 9 for both arms; one turn cannot hold 54 deep questions' evidence.
- D28: fix stale chain answers in search (matches in view first, latest lines per page), then
  rerun both arms. Rejected: accept as a limitation, widen the tolerance, rerun one arm only.

## Open decisions (details in DECISIONS.md)

- O3 (intake): the working-window bound stays 150,000; it passed acceptance. Raising it toward
  the 170,000 launcher cap for heavy heads (an OpenRig seat) is unmeasured.
- O4 (owner): end-user auth guidance (API key only, or subscription logins too).
- O5 (intake): first release v0.1.0, now unblocked by acceptance.
- O6 (owner): per-session upstream affinity on the owner's proxy (cache misses and dropped
  thinking traced to the proxy spreading requests across identities).
- O7 (owner): OpenRig context-usage clock bug (UTC parsed as local time).

## Known limitations (v0.1.0)

- Acceptance ran on one model (claude-haiku-5-5), one bound (150,000), and the owner's proxy.
- Codex: runs end to end, but has no live acceptance run. HTTP is forced through a custom
  provider (no WebSocket). Contexto's tools sit behind Codex tool search and need
  `default_tools_approval_mode = approve`, which `contexto run codex` sets. Only API-key mode is
  live-tested; ChatGPT login mode is tested with synthetic credentials and its terms question is
  open (O4). Some Codex calls go to chatgpt.com directly and bypass the proxy. `preamble_first` is
  on but unmeasured there.
- One turn that needs more evidence than its recall room holds cannot get it all (D27); the room
  per turn is the bound minus usage minus a 12,000-token floor (`min_turn_room`).
- Subagents and side requests share the main conversation's recall room and in-view snapshot;
  concurrent subagents can overspend the room.
- Small bounds with large fixed heads leave little window: the launcher warns when the window
  budget is under 25,000; at 42,000 with a 16,000 head nearly every turn rotates. Use a larger
  bound for OpenRig seat-sized heads (O3).
- Very large single turns can still exceed the bound inside a tool loop (logged `over_bound`).
- Cache efficiency depends on the upstream route (O6): 29 of 31 and 36 of 37 in the live runs.
- Organization-managed Claude Code settings outrank the launcher's `--settings`; a user-supplied
  `--settings` is refused (exit 2); a user `--mcp-config` is allowed.
- The proxy retries once, on a new connection, only for connect, TLS, or send failures (rule 8);
  a failure after the body is sent returns 502 and relies on the CLI's own retry.
- Compressed request bodies: gzip and deflate are decoded (zstd only on Python 3.14+); other
  encodings get 415 (rule 15). `count_tokens` errors from upstream pass through unchanged (the
  owner's proxy returns 403; Claude Code calls it only around compaction).
- On restart, sizing restores the measured token ratio (clamped 1.0 to 6.0) unless the model
  changed; mixed-model sessions blend the ratio under the last model's label.
- In a question turn, the question itself can take one of the 3 in-view search slots (B22,
  queued: exclude the current turn's own text).

## Process notes

- Only intake runs in the main checkout; every other seat works in its own worktree.
- Direct API requests identify honestly and carry no other product's client marker (D23).
- PR approvals are not enforced by GitHub (one shared account); exact-head review by a reviewer
  seat is a team rule. Ruleset `main: require pull request` (id 25202810) is active.
