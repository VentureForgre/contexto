# Contexto status

Updated 2026-10-24 by claude (docs for v0.5.1: D35 to D42, O6 deployed). Decisions taken and still open, with reasons:
`docs/DECISIONS.md`. Every number below is measured unless marked otherwise.

## Where the project is

Contexto keeps very long Claude Code (and Codex) sessions under a fixed context bound so the CLI
never compacts, by putting a local proxy between the CLI and the model API. The owner's bar (D2):
compaction never fires, and accuracy equals full context.

- M0 study: complete. Recommendation `docs/research/M0-recommendation.md`.
- M2 build: complete on main. Runs end to end through `contexto run claude ...` and
  `contexto run codex ...`. Contract: `docs/milestones/M2-contexto-proxy.md`.
- Acceptance: **all four live criteria passed in one run on Claude Code** (live run #3, main
  fbb1303), and **Codex passed criteria 1 to 3** (D30), below.
  On Claude Code, Contexto answered 51 of 54 probes against 48 for the full-context baseline, with zero
  compaction and every request under the bound.
- Latest release: v0.5.1 (main e864896, 2026-10-24). See "Open follow-ups" and "What is left"
  below for the remaining work and the items that need the owner.
- Tests on main (e864896, v0.5.1): 2065 passed, 1 skipped, 1 xfailed on macOS, Python 3.12 (local run, 2026-10-24).
- The repository is public (D42). GitHub Releases stay the only distribution; there is no package
  registry release. Tracked files were scrubbed at `HEAD` of the proxy hostname, another private
  repository's name, and statements about the owner's credentials; history was not rewritten.
- CI and releases run on GitHub-hosted runners (PR #213): Python 3.12 to 3.15 on Ubuntu, 3.12 and
  3.15 on macOS, and Ruff lint. `ci` is a required check in the main ruleset, and workflow runs on
  fork pull requests need approval for all external contributors (D42). GitHub private
  vulnerability reporting, which `SECURITY.md` points to, is on.
- Two "flaky" tests (launcher Ctrl+C, proxy command) failed whenever pytest ran as a background
  job, which starts with SIGINT ignored; fixed in the tests (PR #213), not a timing problem.
- The proxy no longer makes a reverse hostname lookup at start; on a GitHub macOS runner it took
  35 seconds per launch (PR #213, fix). Quarantined pages are named from one clock reading (PR
  #213, fix).
- Ruff lint is enforced in CI (`uvx ruff@0.17.2 check .`); `ruff format` is not.
- CLIProxyAPI compatibility (D35, PR #210): the stream hold passes terminated `: keep-alive`
  comments like ping; the user settings file follows `CLAUDE_CONFIG_DIR`; the Codex session line
  names its upstream source. Live through the owner's proxy: `contexto run claude -p` on
  claude-haiku-5-5 and `contexto run codex exec -m gpt-5.6-luna` both exit 0 with 200 upstream.
  Guide: `docs/cliproxyapi.md`.
- Upstream affinity (D36, PR #212) is deployed on the owner's proxy and verified (see "O6" below).

## Acceptance, live run #3 (main fbb1303, claude-haiku-5-5, bound 150,000, owner's proxy)

Numbers-only record: `docs/research/M2-acceptance-run3.md`.

| Criterion | Result |
|---|---|
| 1. Long run | PASS: 24 of 24 turns, 0 compaction events, max upstream input 109,868 of 150,000, 0 model 4xx, 0 local 5xx, 0 failure events (`turn_room_low` 0, `retained_stale` 0), usage on 44 of 44 requests, 6 rotations; the spend watcher stopped the CLI after the last response was fully logged and stored (exit 143, nothing lost) |
| 2. Fidelity (D27: 54 probes over 6 turns of 9) | PASS: Contexto 51, baseline 48; no category below the baseline |
| 3. Recall | PASS: the 4 probes answerable only from paged-out turns were all correct and credited through search hits from those pages (lines from the conversation in view are never credited) |
| 4. Cache | 36 of 37 eligible requests read the cached prefix (97.3 percent); the one miss is route-side (O6) |
| 5. Tests | 1071 passed, 1 skipped on main (75105f5); CI green |

Per category (Contexto / baseline): chain 28 / 25, obligations 5 / 5 of 6, multihop 8 / 8,
distractor 6 / 6, paged-only 4 / 4. Contexto missed 3 (2 chain, 1 obligations); the baseline
missed 6, every one a stale value, which is consistent with the owner's premise that very long
contexts hurt accuracy. One run on one model, so the 3-probe lead is a measurement, not a claim of
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

## Acceptance on Codex (main ca05b29, gpt-6-astra reasoning low, bound 64,000, owner's proxy)

Record: `docs/research/M2-codex-step1-live.md`; decision D30.

| Criterion | Result |
|---|---|
| 1. Long run | PASS: 21 of 21 turns, history 164,844 (at least 160,000 needed), 0 compaction, 44 requests all under the bound (max input 26,066), all 200 and completed, 0 failure events |
| 2. Fidelity | PASS by arithmetic: Contexto answered 54 of 54 (rescored independently by reviewer-1), so no baseline can be above it; the baseline was not run (D30) |
| 3. Recall | PASS: 4 of 4 paged-only probes reached the model through `contexto_search` (as scored after B30, #169). The run's harness scored 1 of 4 because it located 3 plants on later pages that also held them; B30 fixed that, and the Claude runs were unaffected |

Spend 922,201 of a 5,385,600 line (74.0 percent of input cached). Criterion 4 (cache): the
record's cache hit rate is 1.0. The model searched 84 times,
pinned 9 records, and never called recall. Network samples saw no connection outside the proxy
with analytics and plugin sync off.

## Usage against plain Claude Code (run #3's session, zero-spend analysis)

Record: `docs/research/M2-usage-savings-run3.md` (per-request CSV alongside). Contexto's side is
measured; plain Claude Code's side is modeled from the CLI's own request bytes for the 18 content
turns and measured from the baseline arm for the 6 probe turns.

| | Input tokens | Cost at Haiku 5.5 prices |
|---|---|---|
| Contexto (measured; 2,677,349 cache read, 1,047,954 cache write, 88 uncached) | 3,725,391 | about $0.29 |
| Plain Claude Code, plain-run shape (18 content turns modeled, 6 probe turns measured; best-case caching) | 6,972,335 | about $0.58 |
| Saving | 46.6 percent | 49.7 percent (73.0 percent with the baseline's measured cache misses) |

By phase: content turns 69.0 percent fewer tokens; probe turns only 9.3 percent fewer, because
multihop probes add dependent search rounds that each resend the window
(`docs/research/M2-tool-rounds-lever.md`: 13 of 14 extra rounds are dependency hops; batching would
save about 2 percent, so neither multi-key search nor batching guidance is built). Plain Claude Code
would not have compacted in this session (peak 493,902, modeled, against about 967K). 7 Contexto
requests went just over Haiku 5.5's 100,000-token price tier (5x above it, M0 cost model), all in
tool loops. B27 (`docs/research/M2-haiku-100k-tier.md`): replaying run #3 at bound 125,000 with the
model's behavior held fixed kept every request under the tier, for a modeled 45.7 percent lower
cost, with 9 rotations instead of 6; accuracy at that bound is unmeasured and long tool loops can
still cross it. Decision: no default change and no live run (the owner's seats run Opus 5.5,
which has no price step); the README gives `--bound 125000` as a Haiku tip with those caveats. Limits: one synthetic session, one
run, one model.

## Usage against plain Codex (Codex acceptance run, zero-spend analysis)

Record: `docs/research/M2-codex-usage-vs-plain.md` (with the cause correction, #177). Contexto's
side is measured; plain Codex's side is modeled from the request sizes in the proxy log. One run,
bound 64,000, 20 KB turns.

| | Input tokens | Cost at gpt-6-astra prices |
|---|---|---|
| Contexto (measured; 680,832 cached) | 919,998 | about $3.78 |
| Plain Codex, plain-run shape (modeled, best-case caching) | 1,672,525 | about $3.05 |
| Difference | 45.0 percent fewer | **24.0 percent more** (15.5 percent more pricing uncached input at the input rate) |

Content turns: 71.0 percent fewer tokens and 12.5 percent lower cost. Probe turns: 8.1 percent
fewer tokens but 2.3 times the cost. The main cause is Contexto's handling of restarts, not the
bound: the acceptance harness started a new Contexto proxy for each of the 21 `codex exec` turns,
and a new proxy kept no epoch state, so every one of the 19 rotations was the first request after
a restart (verified from the run's log). Each rotation changes the text right after Codex's
9,088-token head, so 17 of those 19 requests read only the head from cache. A zero-spend replay (A28,
`docs/research/M2-cache-aware-rotation.md`) models resuming the epoch after a restart at 9 rotations
instead of 19 and 9.8 to 16.2 percent lower cost, depending on the bytes-per-token rate; that is
rule 16, released in v0.2.0 (A30, B31; replaying the run through the released engine gives
the same number of rotations, 9). Codex's own compaction would not have fired (plain peak 116,731,
modeled). Users who keep one proxy for the whole session do not hit these restarts.

## Token spend on live model calls (owner's proxy unless noted)

- M0 study, lane 1: about 4.9M as reported by manager-1 (documented parts in
  `docs/research/M0-proxy-measurements.md`, `M0-codex-measurements.md`, `M0-rollover.md`, and
  81,319 for the route-identity test). Lane 2: about 1.0M, summed from its research docs.
- Codex smoke: 161,549 (API-key mode). Earlier Codex seat measurement: 410,827 on a ChatGPT
  login.
- Acceptance before D27: H1 #1 1,783,815; aborted attempt up to about 65,000; recall experiment
  415,413; H1 #2 2,015,165; baseline 447,659.
- Live run #2: 5,897,784 (Contexto 3,260,912, baseline 2,636,872).
- D28 experiment: 532,157 of a 740,000 line.
- Live run #3: 6,382,449 of a 6,880,000 line (Contexto 3,748,084, baseline 2,634,365). Contexto
  used more than the modelled 2,952,139 because the model made more searches and tool-loop
  requests per probe turn than the model assumed; still under its 4,007,279 cap.

## Scale to 30M tokens of history (zero-spend measurement, measured 2026-10-17, recorded 2026-10-18)

Record: `docs/research/M2-scale-30m.md`; harnesses in `spikes/m2/scale/`. Synthetic sessions, local
fake upstream, no model calls. The carried-content findings in this section were measured before
D32 and D33 (#203); what remains after them is under "Known limitations".

- Claude Code 2.1.302 resumed and resent a 30M-token history (52.6 MiB request, 0.7 s, about 1 GB)
  and up to 525M; from 550M it stops with "API Error: Out of memory". With Contexto-like reported
  usage it never compacted.
- The proxy kept every upstream request under 150,000 (counted by the fake upstream as bytes /
  1.83) at 30M in a synthetic session with reminders on the first message only and one
  session-start system message, and added 0.58 s per tool-loop request and 0.86 s per turn start.
  Claude Code 2.1.302 places context blocks that way on 5.x model ids but not on 4.x ids (next
  bullets), and on 5.x ids it also sends a role `system` message with every main request, which the
  30M run did not model.
- Where Claude Code 2.1.302 puts reminders (`spikes/m2/scale/reminders/`, 48-turn sessions against
  a scripted local model). On 4.x model ids (claude-sonnet-4-6; 12-turn probes on claude-opus-4-7,
  claude-sonnet-4-5, claude-haiku-4-5) it puts `<system-reminder>` blocks in every later turn-start
  user message, which Contexto carries and never pages: 45 tokens per turn with bare prompts, 215
  in a busy one-process session, 425 with a resume every turn (Contexto's estimator). So the
  blocker is real on 4.x ids. [Inferred, linear from the scale test's failure at turn 181 at 215
  per turn; a rough guide] `bound_too_small` from about turn 860, 180, and 90. By bytes per turn
  the busy rate is 2.4 times the scale test's profile, which would put the busy session's failure
  near turn 77 (about 2.1M). Also measured: the token countdown is on by default in 2.1.302, a
  plan-mode entry adds about 2,720 tokens, and on resume a file-changed note is sent again on every
  launch until the file is read again.
- On 5.x ids (claude-opus-5-5, claude-haiku-5-5; probes on claude-sonnet-5, claude-fable-5) the
  same content arrives as role `system` messages, one per main request, which the 8,000-token
  fallback can page. On claude-opus-5-5 and claude-haiku-5-5 one uncapped path is left: an effort
  change puts `output_config` on that turn's first system message, and `classify_system` (written
  against 2.1.301) then classifies it as session-start, carried forever. 16 changes grew that
  class from 2,770 to 9,272 tokens (one process) and 11,637 (resume every turn), Contexto's
  estimator, and that was all of the unpageable growth. With bare prompts and no effort change
  (claude-opus-5-5), nothing unpageable grew over 48 turns. On the claude-sonnet-5 and
  claude-fable-5 probes effort stayed top-level and nothing unpageable grew; there the session's
  environment message has no `output_config`, classifies as carried text, and is the first message
  the fallback would page.
- What that means for the owner's seats (Opus 5.5, claude-opus-5-5), measured: per-turn reminders
  are pageable, and only effort changes add carried content that is never paged, 27 to 3,444
  tokens each (bytes / 1.83). The 4.x blocker applies only to sessions on a 4.x id.
- What that means for the owner's seats, [inferred, not measured]: the per-request role `system`
  text probably still fits at 30M. Carried content counts in the fixed head F, and more F shrinks
  the window rather than pushing requests toward the bound. F was about 70,000 at 30M against the
  100,810 at which `bound_too_small` first fired in the `mixed` run, about 30,800 tokens of room;
  the pageable text held 410 to 2,143 once the fallback engaged in the 48-turn resume run (at each
  rotation the fallback pages it back to about 8,000 or less), and
  effort-change content adds to the same F. The fallback writes each paged message as its own
  page, so such a session would reach about 3,000 pages or more by 30M, most of them single small
  messages; the store spike's 3,000 to 6,000 rows (after D34: planted page in the 3 detailed
  results for 60 and 56 of 86 queries, 0.04 to 0.22 s per search with every page's text held) are a
  rough guide for that shape. Under D39 search time depends on the store's text bytes, not its page
  count: many small pages stay within the 64 MB default.
- Search (D34: every word in any case, BM25 with a recency prior, cached page text) takes 0.10 to
  0.25 s at 6,000 pages when the MCP server holds every page's text uncompressed, and the first
  search of an MCP process 0.8 s. With D39's default 64 MB (2,085 such pages uncompressed, about
  48M tokens; the whole 6,000-page store compressed), it takes 0.26 to 0.41 s, the first search
  1.6 s, and the server's peak RSS is 134 MB instead of 290. The planted page reaches
  the 3 detailed results for 71 of 86 queries at 17 pages, 63 at 1,000, 60 at 3,000, and 58 at
  6,000 (D41; 56 with D34's ranking; before D34: 61, 50, 44, 39), and the latest value comes first
  for 20 of 24 at 6,000 (before D34: 12). On pages sized like real turns at 6,000, D41 puts the
  planted page in the top 3 for 58 of 86 queries (53 with D34), the latest value first for 20 of
  24 (17), and no query shows only an old value (3).

## Decisions taken in M2 and why (full text in DECISIONS.md)

- D18: build H1, context virtual memory at the API boundary; the only approach that bounds the
  context without a lossy reset.
- D19: recall through MCP tools, not a hidden proxy tool; far simpler and no shadow history.
- D20: strip all thinking at each rotation; measured to return 200 after a history edit (its
  accuracy cost could not be isolated in M0).
- D25: recall preamble V2, after the model made no tool calls in H1 #1.
- D26: acceptance fails on any off-path event, not only over-bound or 4xx.
- D27: probes over 6 turns of 9 for both arms; one turn cannot hold 54 deep questions' evidence.
- D28: fix stale chain answers in search (matches in view first, latest lines per page), then
  rerun both arms. Rejected: accept as a limitation, widen the tolerance, rerun one arm only.
- D29: cut v0.1.0 after acceptance, on the owner's instruction to come back to a finished
  project; GitHub Release only, no package registry.
- D32: the bounded fallback also pages context blocks of later turn starts (4.x ids), so carried
  content stops growing with them.
- D33: session-start is recognised by its content, not by settings fields, so an effort change no
  longer makes a reminder unpageable (5.x ids).
- D34: search matches every word in any case, ranks by BM25 with a recency prior (paged context
  blocks after every turn, in rank and in the reply), and both processes cache page reads.
- D35 (resolves O4): end users run Contexto against the vendor API with an API key, or a
  CLIProxyAPI instance; logins live in CLIProxyAPI or the vendor's own CLI, never in Contexto;
  users follow their providers' terms. Guide: `docs/cliproxyapi.md`.
- D36 (resolves O6): upstream affinity. Contexto sends a salted per-session `X-Session-Affinity` to
  non-vendor upstreams; the CLI's own session ids rank above it. On the proxy side, CLIProxyAPI's
  `routing.session-affinity-consistent-hash` (proposed upstream as router-for-me/CLIProxyAPI#6954)
  pins each session to one credential on every replica; deployed on the owner's proxy 2026-10-24.
- D37: `contexto sessions prune` deletes whole sessions by `--older-than`, `--keep`, `--session`;
  dry run unless `--yes`; `--json` available. Writable stores hold a shared flock on `live.lock`,
  and prune never deletes a session whose lock is held or that was active in the last 10 minutes.
  It deletes through open directory descriptors, so a swapped-in symlink cannot redirect it.
  `sessions list` shows size on disk and a total.
- D38: `contexto run` warns once on stderr when Claude Code or Codex is a version Contexto was not
  tested with, and still launches; the proxy logs each request's CLI version and
  `session_start_unrecognised`.
- D39: the MCP server holds search text up to `CONTEXTO_SEARCH_CACHE_MB` (default 64), the newest
  pages uncompressed and older ones compressed, and reads the rest from the store at each search;
  results do not change.
- D40: each subagent and side request in a tool loop has its own recall room, and a tool call is
  charged against every live room, because the MCP server cannot tell which conversation made it.
- D41: search counts a term once per text that holds it, at most 3 per page, and drops BM25's length
  term (b 0), so neither a long turn nor an old dump that repeats a key buries the turn that states
  its value; chosen on a measured grid (probes, 13-turn cases, five dump sets).
- D42: the repository goes public (2026-10-24); GitHub Releases stay the only distribution (no
  package registry). D0, D13, and D14 are marked superseded in place.

## Open follow-ups

Not started; each would be its own pull request.

- A byte budget per search on the pages `contexto_search` reads (D41 follow-up): it reads and
  renders up to 53 pages per search with no cap on their size (18.5 MB of page JSON for one
  multi-word query at 3,000 pages sized like real turns; at most 53 times the largest page). For
  example, read the 50 listed rows' offsets from the held search text instead of parsing whole
  pages, or list fewer rows once the budget is spent.

## What is left (as of 2026-10-24)

Definition of done for this project: M2 acceptance passed on both CLIs (done), every release
published with its wheel and sdist, and STATUS, README, and the contract accurate to `main`.

The 30M plan the owner approved on 2026-10-18 is done, each step with two reviewers: carried
growth stopped (D32, D33, #203, v0.4.0), search ranked by BM25 with a recency prior and page reads
cached (D34, #204, v0.4.0; the `index_text` manifest was rejected there), each message hashed once
in the proxy (#205, v0.2.9), and the missing-tools warning (#202, v0.3.0). That plan ran with no
live model runs.

The 2026-10-24 work is merged and released: CLIProxyAPI support (D35, #210), upstream affinity
(D36, #212, deployed on the owner's proxy), sessions prune (D37, #208), the CLI version check (D38,
#211), the MCP search-text budget (D39, #215), subagent recall rooms (D40, #209), hosted CI (#213),
the public repository (D42, #206, #207), all in v0.5.0, and the search ranking fix (D41, #216) in
v0.5.1.

Measured candidates deliberately not pursued (each would need a live run or new evidence):
- Keeping less of the recent conversation in view (`window_fraction` 0.25): 3 to 13 percent
  cheaper in replays, but it shows the model less, so it needs an accuracy run first (A28).
- A Haiku default bound of 125,000 (B27): only a README tip; accuracy at that bound unmeasured.
- A search "follow" option for multihop chains (B26): the hops come from synthetic probes.
- A live check of 150,000 on an OpenRig seat (D31 says revisit only if a seat shows a recall
  cost). Codex at 150,000 is under "Needs the owner".

Needs the owner:
- A license. The repository is public with no `LICENSE` file, so others have no stated right to
  use, copy, or change the code. Which license is the owner's choice.
- Upgrade OpenRig on the host from 0.5.15 to 0.6.1 or later to get the clock fix (O7, resolved
  upstream in OpenRig #124).
- Search ranking (D41): search ships with text cap 1, page cap 3. Text cap 3, page cap 3 restores
  main's two D34 scenarios (a reply that discusses a key in one text outranks newer passing
  mentions; the newest of equal one-reply statements is shown first) at 41 more stale-only
  results, all in the quiet dump set (162 against 121), with no other measured change. The
  default stands until the owner chooses.
- An acceptance run on Opus 5.5 (claude-opus-5-5), the model the owner's seats run. Claude
  acceptance has only run on claude-haiku-5-5. It costs seat tokens (live run #3 used 6,382,449
  on Haiku), so it needs the owner's approval.
- Codex acceptance at the default bound 150,000. Codex passed at 64,000 (D30); 150,000 is
  untested on Codex.
- Optional, for O6: an `--observe-thinking` session through the owner's proxy, to confirm no
  `end_user_binding_mismatch` (dropped thinking) now that affinity is deployed. It was not run.

## Open decisions (details in DECISIONS.md)

- O3: settled by D31: the default bound stays 150,000, including for OpenRig seats, because a
  larger bound lowers the largest single turn that stays under Claude Code's 200,000 idle-compaction
  minimum (about 87k at 150,000 against 67k at 170,000 on a 92k head, modeled).
- O4: settled by D35 (owner, 2026-10-24; CLIProxyAPI support shipped in v0.5.0, #210). People who
  run Contexto themselves use either a vendor API key or a CLIProxyAPI instance. Subscription
  logins live in CLIProxyAPI or the vendor's own CLI, never in Contexto, and users follow their
  providers' terms.
- O5: settled by D29. v0.1.0 was cut from f990703 (tag and GitHub Release on this
  repository, sdist and wheel attached, no package registry).
- O6: resolved by D36, deployed on the owner's proxy 2026-10-24 and verified (section below). Cache
  misses and dropped thinking were traced to the proxy spreading one session's requests across
  credentials.
- O7: resolved upstream. OpenRig fixed the context-usage clock bug (UTC parsed as local time) in
  its PR #124 (commit 4a920dba), first released in v0.6.1. The host still runs 0.5.15; upgrading
  it is under "Needs the owner". No report is needed.

## O6 (upstream affinity), resolved by D36: deployed 2026-10-24

- Live: the owner's CLIProxyAPI deployment runs routing.session-affinity,
  session-affinity-consistent-hash, ttl 1h (cliproxy-cluster #10), image sha256:6be56ec3… from
  f78c897 (v7.3.12 + patches 0001-0005). Rendezvous hashing of the session id, so every replica
  picks the same credential; nested subagents rank with their root. Upstream proposal:
  router-for-me/CLIProxyAPI#6954. Contexto #212 sends a salted per-session X-Session-Affinity to
  non-vendor upstreams; the CLI's own ids rank above it.
- Verified: rollout 4/4 ready; check_config_rollout.sh exit 0 (after one same-second stale pod was
  replaced); 3 routing keys; no backend errors, no 5xx. A 10-request byte-identical
  claude-haiku-5-5 series with one key: 1 write (13,654), then 9 reads of 13,654, all 200, across
  4 pods on 1 credential (before the fix: 3 writes). Not run: the --observe-thinking check for
  dropped thinking.
- Rollback: revert cliproxy-cluster #10 (Flux restores image and config; the revision annotation
  rolls the pods), or reseal with consistent-hash false. Never `kubectl rollout restart` (Flux
  reverts it and rolls again); delete stale pods one at a time.

## Known limitations (v0.5.1)

- Claude acceptance ran on one model (claude-haiku-5-5), one bound (150,000), and the owner's
  proxy; Codex acceptance on gpt-6-astra at bound 64,000. The default bound 150,000 is untested
  on Codex (D30).
- Codex pages written by v0.1.0 to v0.1.2 were stored with empty index stubs. Since v0.2.3
  the store rebuilds such a stub from the page's messages each time it reads the page, so
  their index lines show user text, tools, and paths. The page file is not rewritten and its id is
  unchanged; anything reading page files directly still sees the empty stub.
- Codex: HTTP is forced through a custom provider (no WebSocket). Contexto's tools sit behind Codex
  tool search and need `default_tools_approval_mode = approve`, which `contexto run codex` sets.
  Only API-key mode is live-tested; ChatGPT login mode is tested with synthetic credentials. On
  its terms, D35 answers O4: people who run Contexto themselves use a vendor API key or a
  CLIProxyAPI instance, subscription logins live in CLIProxyAPI or the vendor's own CLI, never in
  Contexto, and users follow their providers' terms.
  In ChatGPT login mode some Codex calls (rate limits, usage, a remote MCP connection) go to
  chatgpt.com directly and bypass the proxy by design. By default Codex also connects to
  chatgpt.com (analytics) and github.com (plugin sync) outside the proxy;
  `-c analytics.enabled=false` and `-c features.plugins=false` stop them in network samples, and
  `contexto run codex` leaves both to the user. `preamble_first` is on but unmeasured there.
- One turn that needs more evidence than its recall room holds cannot get it all (D27); the room
  per turn is the bound minus the request's usage (input plus output) minus a 25,000-token
  headroom floor; the engine rotates at a turn start when that would leave less than 12,000
  (`min_turn_room`).
- Subagents and side requests have their own recall room while in a tool loop (D40): a tool call
  gets the smallest live room and is charged against every live room, because the MCP server
  cannot tell which conversation made it. A side room expires 15 minutes after its last publish,
  or earlier when main's history shows its agent stopped (a task notification or a foreground
  hand-back). A subagent that stopped without one can hold main's recall down until the expiry
  (`limited_by: side` with `limiting_age_s` in the log). Main's room is the only bound for a
  subagent's calls when the stream hold is skipped, its transform failed, it went degraded before
  its first publish, or its call starts more than 15 minutes after its response (for example after
  a permission prompt). Search's "in view" for a subagent still means the main conversation's
  window.
- Usage refines the estimate only for the request it measured (rule 3): with two requests of one
  conversation in flight, only the newer one's usage counts (`usage_unattributed` for the other),
  and after a request without usage the next estimate is a full one (`usage_missing`).
- After a restart, a side request that arrives before main and finds no epoch record (or main's
  resume fails) is main until main's next turn start, so its window and model stay in the store
  until then.
- Small bounds with large fixed heads leave little window: the launcher warns when the window
  budget is under 25,000; at 42,000 with a 16,000 head nearly every turn rotates. Use a larger
  bound for OpenRig seat-sized heads (O3).
- Very large single turns can still exceed the bound inside a tool loop (logged `over_bound`).
- Search (D34, D39) holds page text in the MCP server up to `CONTEXTO_SEARCH_CACHE_MB` (default
  64 MB; the newest pages uncompressed, older ones compressed). A store whose compressed text does
  not fit reads every page outside it at each search, and the first search of each MCP process
  reads every page once and compresses what it keeps (1.6 s at 6,000 synthetic pages, which all fit
  compressed: 0.26 to 0.41 s per search against 0.10 to 0.25 s with every page uncompressed; peak
  RSS 134 MB instead of 290). The 3.2 compression ratio is from synthetic text. Single common
  words can still bury a fact at 6,000 pages (5 of 86 probe queries, measured under D34).
  Search (D41) counts a term once per text, at most 3, with no length term. A reply or file that
  discusses a key at length in one text weighs as one passing mention, so a newer passing mention
  outranks it. An old turn that names a key in 2 or more texts (a prompt that names it counts twice)
  usually outranks a newer one-line reply that states its value (3 texts outrank it even from the
  oldest position), and once a passing mention follows that reply, the reply leaves the 3 detailed
  pages more often than under D34 (38 of 122 against 64 in a repo-text simulation); many separate
  greps cost most. The pages one search shows are read in full with no cap (18.5 MB of page JSON for
  one multi-word query at 3,000 pages sized like real turns); a per-search budget is under
  "Open follow-ups".
- Carried content after D32 and D33 (#203, measured on Claude Code 2.1.302's captured bodies in
  `spikes/m2/scale/reminders/`, Contexto's estimator): at turn 48 the content the fallback cannot
  page is 3,557 to 3,794 tokens on sonnet-4-6 (was 5,452 to 22,953) and 4,897 to 5,643 on
  claude-opus-5-5 busy sessions (was 9,804 to 12,169). On 4.x ids, and on 5.x ids in one process, it stops growing
  from turn 12 (on 4.x up to about 5,250 while plan mode is on); on claude-opus-5-5 and claude-haiku-5-5
  with a resume every turn it still grows about 21 tokens per turn, because the latest
  effort-change message itself grows as the CLI re-sends unread file notes on it. On
  claude-sonnet-5 and claude-fable-5 the environment message (2,630 to 2,764 tokens) is now always
  carried.
  In the 30M proxy run with later-turn reminders, `bound_too_small` and `over_bound` fell from 871
  and 692 to 0, the largest request from 288,309 to 134,203 tokens, and F at 30M from 259,155 to
  74,240; the store holds 1,616 pages at 30M instead of 1,138. Session-start recognition depends on
  Claude Code's own `# Environment` text, and the kinds of context block on 2.1.302's first lines:
  re-run the routine in `docs/cli-updates.md` when the CLI changes. Resuming an epoch record
  written before v0.4.0 can carry one message the old code had paged (logged as
  `carried_reclassified`).
- CLI versions (D38): Contexto is tested with Claude Code 2.1.302 and codex-cli 0.154.0 and 0.163.0
  (`TESTED_VERSIONS`, `src/contexto/cliversions.py`). `contexto run` warns once on stderr for any
  other version and still launches (`contexto run --no-version-check <cli>` or
  `CONTEXTO_NO_VERSION_CHECK=1` silences it). The proxy logs each request's CLI version
  (`cli_version`) and `session_start_unrecognised` when the main conversation's first request carries
  the CLI's context but no session start it recognises. A CLI that changes its text without changing
  its version, or changes text only on later turns, is not caught. Re-test routine:
  `docs/cli-updates.md`.
- Session folders are never removed on their own. `contexto sessions prune` (D37) deletes whole
  sessions only, skips any session in use or active in the last 10 minutes (a retry after a partly
  failed delete waits out the same 10 minutes), and cannot remove pages inside a live session.
- Windows is not supported: the store locks files with `fcntl` (`store/__init__.py`,
  `store/ledger.py`), the launcher forwards `SIGHUP` (`launcher.py`), and the store relies on
  POSIX file modes (0700 and 0600), so neither imports there. CI tests Python 3.12 to 3.15 on
  Linux and 3.12 and 3.15 on macOS; live runs used Python 3.12.
- Cache efficiency depends on the upstream route: 29 of 31 and 36 of 37 in the live runs, before
  D36. A proxy with several upstream credentials has to pin each session to one (D36); on the
  owner's proxy, since 2026-10-24, a byte-identical series read the cache 9 of 9 times after one
  write. Subagents and side requests share the session's key and CLIProxyAPI sends Claude Code
  subagents to their root session's credential, so parallel subagents load one credential's rate
  limit, and a 429 there moves the whole session to the next credential, with one cache miss and
  dropped thinking at each move. A fresh launch or `--fork-session` gets a new Claude Code session
  id and can miss the cache once.
- Organization-managed Claude Code settings outrank the launcher's `--settings`; a user-supplied
  `--settings` is refused (exit 2); a user `--mcp-config` is allowed.
- The proxy never reuses upstream connections, and retries once, on a new connection, only for connect, TLS, or send failures (rule 8).
  A failure after the body is sent and before the response head returns 502 and relies on the
  CLI's own retry; a failure after the response started ends the stream without its terminator
  (logged as `relay: <class>`).
- Compressed request bodies: gzip and deflate are decoded (zstd only on Python 3.14+); other
  encodings get 415 (rule 15). `count_tokens` errors from upstream pass through unchanged (the
  owner's proxy returns 403; Claude Code calls it only around compaction).
- On restart, sizing restores the measured token ratio at the first request on the recorded model
  (so a side request on another model that arrives first does not cancel it), unless the ratio is
  outside 1.0 to 6.0 bytes per token (then the seed ratios apply). Only the main conversation
  publishes the ratio and its model label, but the ratio is measured over every request, so
  mixed-model sessions still blend it.

## Releases

| Version | Commit | What changed |
|---|---|---|
| v0.1.0 | f990703 | first release, after M2 acceptance (D29) |
| v0.1.1 | 0769fdf | B22: in-view search skips the current turn's own question |
| v0.1.2 | def6e8c | A18: `CONTEXTO_HOME` resolved to one absolute path for the proxy and every MCP server (Codex's MCP server ignored it before) |
| v0.1.3 | 031ca59 | B23: search and index lines read Codex message parts, tool calls, and tool outputs (Codex search found nothing before; recall already worked, B20 in v0.1.0) |
| v0.1.4 | 314d575 | B24: standalone `contexto mcp` resolves `CONTEXTO_HOME` and `--home` to an expanded absolute path |
| v0.1.5 | 5ef9370 | A23: the proxy logs how each response ended (status, incomplete reason, stop reason), content-free |
| v0.1.6 | db472a9 | A26: the proxy's end-status log fields accept only documented values; anything else logs as `other` |
| v0.2.0 | 703d7c3 | Rule 16 (B31, A30): a restarted proxy resumes the main conversation's epoch instead of rotating again from epoch 0, so the prompt and its cache survive a restart |
| v0.2.1 | 798a7ce | A31: a resume requires the store to hold at least the recorded number of pages (`pages_missing` otherwise) |
| v0.2.2 | 2da3cb0 | Rule 17 (A34, #187): headroom capped at a quarter of the bound and halving at each turn start that measures growth, so one large read no longer leaves the session at the minimal window. Wheel and sdist attached by the reattach run after #189 |
| v0.2.3 | 2312caa | #192: Codex pages written by v0.1.0 to v0.1.2 get their index stub rebuilt from the page's messages when the page is read; nothing is rewritten |
| v0.2.4 | 0914850 | #193: a stream cut short upstream still writes the request's log entry, and its usage still reaches the engine; a malformed response head is logged as a 502 that is not retried; negative body lengths get 400. #194: only the main conversation publishes the window, and a restore waits for a request on the recorded model |
| v0.2.5 | 77961d1 | #195: a stream hold ended by an error event, end of stream, or an upstream failure publishes the room from the usage already read, before it releases the held tool calls; a `Content-Length` body cut short is detected; acceptance never counts a truncated 2xx as a turn end |
| v0.2.6 | 56e4551 | #197: one SSE grammar (CRLF, LF, CR) for the hold splitter and the usage reader, so usage is read on CR-terminated streams, and a final `message_delta` without its blank line is read before the hold publishes, so its output tokens reach the engine |
| v0.2.7 | 4525388 | #196: after a request whose usage never arrived, the next estimate covers the whole request, so a needed rotation is no longer skipped (`usage_missing`); usage for a request other than the one the engine last sent (after a transform error, or with two in flight) no longer feeds the estimate or the bytes-per-token ratio (`usage_unattributed`); a conversation is recorded as main before its turn start's rotation publishes the window, so a failed request cannot leave the old main recorded |
| v0.2.8 | 0a8e4c3 | #200: `contexto run codex` starts its MCP server as this Python by absolute path (`-P -m contexto`) instead of a bare `contexto` on `PATH`, so Codex no longer runs silently without the Contexto tools when `contexto` is not on `PATH`; both launchers pass `-P`, so a project's own `contexto` package cannot run in place of the server (the Claude path was exposed from v0.1.0) |
| v0.2.9 | 62b02a1 | #205: the proxy hashes each message once and drops the request copy and reference serialization, to cut per-request time on long histories (the fast hash falls back to normalise-then-hash when a message cannot be encoded). #199: the 30M scale harnesses and record (`spikes/m2/scale/`, `docs/research/M2-scale-30m.md`) |
| v0.3.0 | c023c87 | #202: the proxy warns on stderr and logs `mcp_tools_missing` when the main conversation runs without Contexto's MCP tools, and `mcp_tools_found` when they appear later |
| v0.4.0 | 4b87ec2 | #203 (D32, D33): the bounded fallback pages later turn starts' context blocks, and session-start is recognised by its `# Environment` first line, so carried content stops growing; `carried_reclassified` on resuming an older record. #204 (D34): search matches every word in any case, ranks by BM25 with a recency prior (paged context blocks last), and both processes cache page reads |
| v0.5.0 | cb1986f | #210 (D35): CLIProxyAPI as an upstream; SSE keep-alive comments pass the stream hold, the settings file follows `CLAUDE_CONFIG_DIR`. #212 (D36): a salted per-session `X-Session-Affinity` for non-vendor upstreams. #208 (D37): `contexto sessions prune` and session sizes in `sessions list`. #211 (D38): a warning for untested Claude Code or Codex versions. #215 (D39): the MCP server holds search text up to 64 MB by default. #209 (D40): each subagent gets its own recall room, and a tool call is charged against every live room. #213: the proxy starts without a reverse hostname lookup; quarantined pages are named from one clock reading |
| v0.5.1 | e864896 | #216 (D41): search counts a term once per text that holds it, at most 3 per page, and drops BM25's length term, so neither a long turn nor an old dump that repeats a key buries the turn that states its value |
Each is a tag and a GitHub Release on this repository with the wheel and sdist attached; there is
no package registry release.

## Process notes

- 2026-10-16 18:15Z: hosted GitHub Actions stopped starting jobs. Until the move to
  the self-hosted runner (PR #188, main 2da3cb0), merges were checked by full local test runs
  from the author, the reviewer, and intake. 19:31Z to about 00:40Z the rig was down; the owner
  restored it, and several seats' git worktrees were recreated.
- 2026-10-16, about 10:25Z: builder-2 deleted `~/.contexto` on the owner's machine (8 session
  folders) without asking. 6 were matched to session ids its own probes recorded; 2 only by name
  pattern and time (builder-2's account, relayed verbatim by manager-1 at 10:29Z and noted on
  A18's queue row). Logs were not read and no other user of the folder was checked for. Agents now
  ask before deleting anything under the owner's home directory. The cause, `contexto run codex`
  ignoring `CONTEXTO_HOME` for its MCP server, was fixed in v0.1.2 (A18).
- 2026-10-17: the OpenRig rig was taken down (snapshot kept). Claude runs the project directly:
  builder subagents work in their own worktrees, and two independent reviewer agents post
  exact-head verdicts as PR comments before a merge. Before that, only intake ran in the main
  checkout and every other seat worked in its own worktree.
- Direct API requests identify honestly and carry no other product's client marker (D23).
- The project has one owner, and the builder and reviewer agents post from the owner's account,
  so GitHub cannot count their approvals; two exact-head reviewer verdicts per PR are a project
  rule. Ruleset `main: require pull request` (id 25202810) is active, and since 2026-10-24 it
  requires the `ci` check.
- 2026-10-24: CI and releases moved back to GitHub-hosted runners (PR #213), and the repository
  went public (D42).
