# Decision log

Every decision that shapes Contexto, with who made it, when, and why. Newest last within each
section. "Owner" is the human who owns the project; "intake" is the intake lead seat acting under
the owner's delegation. Open decisions are listed at the end with the options and the current lean.

## Standing authority

- **D0. Intake has free rein to finish the project.** Owner, 2026-10-15. The owner prefers to return
  to a finished project and asked that STATUS show decisions taken, why, and decisions still open.
  The owner's one limit: nothing is published outside this repository (no PyPI, npm, Homebrew,
  public repos, or public posts). Intake added two limits of its own: the owner's personal Claude
  and Codex configuration is not changed, and heavy spend on the owner's ChatGPT plan waits for
  the owner. Intake reads D0 as superseding the owner gates written into earlier briefs,
  including the M0 brief's Q5 gate (see D12).

## Product direction

- **D1. Contexto targets near-unlimited sessions for terminal agents.** Owner, 2026-10-15. Claude
  Code and Codex resend the whole conversation every turn, so long sessions burn usage, and
  compaction adds cost (intake adds: and loses detail). Wanted: a standalone tool that takes over how the session
  is sent to the model. Not RAG, not a vector database. See
  `docs/milestones/M0-infinite-session-study.md`.
- **D2. Success bar: compaction never fires, and accuracy equals full context.** Owner, 2026-10-15,
  in the owner's words: the compaction number "is there for a reason"; results near it are
  horrible; the project should allow super long sessions in which compaction never even gets
  called, "while keeping the same accuracy". Intake's reading and ranking rule: quality is already
  degrading near the threshold, so designs are ranked first by how small and stable they keep the
  working context and whether recall and task accuracy match the full-context baseline, then by
  token cost; built-in compaction counts as a failure.
- **D3. Experiments use default CLI compaction settings.** Owner, 2026-10-15. An autocompact
  threshold override was proposed to save tokens and revoked: results must reflect real sessions.
- **D4. Measurements guide decisions.** Owner, 2026-10-15. Every number in a decision carries its
  source and is marked measured, vendor-documented, modeled, or inferred.

## Study (M0)

- **D5. Run M0 as a study before building.** Intake, 2026-10-15. Interception surfaces, economics,
  and fidelity were unknown; a wrong guess would build the wrong proxy correctly.
- **D6. Phase 1 is pass-through only, with numeric token caps.** Intake after reviewer-1, 2026-10-15.
  500k per experiment, 3M per builder packet, 6M per lane; spike proxies on 127.0.0.1; shared
  routing never edited. Raised for specific runs since: a 2M lab budget for the quality-versus-context-size probe, and
  separate lines for the direct-API replay (D10) and the cache-miss test (400k).
- **D7. Dedicated test seats instead of throwaway processes.** Owner, 2026-10-15. `lab.subject`
  (Claude Code, claude-haiku-5-5) and `labcodex.subject` (Codex, gpt-6-astra) are real long-running
  OpenRig seats, the owner's actual use case, and avoid builder-seat hook and resume-token
  collisions.
- **D8. Seats stay on `builtin:yolo`; the Codex test seat uses the owner's ChatGPT login.** Owner,
  2026-10-15. Under yolo Codex drops `-p` profiles and project-local config cannot set
  `model_provider`, so routing the seat to the rig proxy would change the owner's personal default.
- **D9. The 2M compaction-loss run was replaced by a quality-versus-context-size probe.** Intake under
  D2, 2026-10-15. Forcing a session into compaction measures the wrong thing; the probe finds the
  context size where recall starts to slip, which bounds the working window.
- **D10. Direct-API replay arm: up to 450k tokens.** Intake, 2026-10-15. First approved at 309,507
  tokens for two checkpoints with thinking stripped; then widened to a 40k checkpoint in both arms
  and at about 92k, each with thinking preserved and stripped (408,851 tokens planned), so the
  replay is symmetric and comparable with the seat run at the same size. The third large checkpoint runs only if the smaller
  ones show a gap of at least 2 probes.
- **D11. The rig proxy is the owner's infrastructure.** Owner, 2026-10-15. Auth headers reaching
  `cliproxy.sullylab.de` are not an exposure.
- **D12. Phase 2 rewriting is authorized under D0, on owner infrastructure only.** Intake,
  2026-10-15. The M0 brief gated rewriting on an owner decision about Q5; the owner delegated that
  decision through D0. This settles Q5 for development: rewriting experiments run through the
  owner's proxy with the rig key (Claude) or the same proxy through a throwaway `CODEX_HOME` whose
  provider uses `auth.command` (Codex). Rewritten traffic never uses the owner's ChatGPT or
  claude.ai subscription login, and the owner's own config files are never edited. What end users
  should use is a product documentation question, listed under open decisions.

- **D16. Phase 2 attaches through a launcher wrapper.** Intake under D0, 2026-10-15. Measured by
  lane 1 (PR #13, approved at 5daaab4): inside a git worktree Claude Code also loads the main
  checkout's `.claude/settings.local.json`, and model requests go to that file's
  `ANTHROPIC_BASE_URL`, not the worktree's. Vendor-documented: "In a worktree, it uses the file at
  the main checkout's root." So a session cannot be reliably rerouted through its own settings
  file. A launcher such as `contexto run claude ...`
  passes an explicit `--settings` with the Contexto base URL, and `contexto run codex ...` uses a
  temporary `CODEX_HOME` whose provider fetches its key with `auth.command`. This beats every
  settings file (organization-managed settings still outrank `--settings`) and never edits the
  user's configuration.

- **D17. No paging inside a turn; rotate only at turn boundaries.** Manager-2, accepted by intake,
  2026-10-15. Vendor-documented and verified by both reviewers: within a tool-use turn, thinking
  must be passed back unchanged, and changing the prefix mid-turn while keeping thinking is
  invalid. Window rotation therefore happens only between user turns, with headroom left for a
  long turn. Server-side `clear_tool_uses` (context editing, not compaction) is a labeled fallback
  for a turn that still exceeds the bound; it is lossy, so its accuracy cost must be measured
  before it ships.

## Build (M2)

- **D18. Build H1, context virtual memory at the API boundary.** Intake under D0, 2026-10-16. It is
  the only approach that bounds working context without a lossy reset (modeled: 0 of 20 seeds over
  a 250k bound; at 100k with online headroom 4 of 20 seeds sent some requests over, logged as
  over-bound), and the cheapest modeled ($87.95 against $382.10 for compaction, 300 turns, Opus 5.5). Evidence and
  ranking: `docs/research/M0-recommendation.md`. Contract: `docs/milestones/M2-contexto-proxy.md`.
- **D19. Recall and pinning go through an MCP server, not a hidden proxy tool.** Intake, 2026-10-16.
  Lane 1 judged (inferred) that MCP recall avoids stream hold-back, stitching, and byte-exact
  reinsertion of hidden exchanges; this resolves the conflict with lane 2's hidden-tool design.
  Cost: recalled pages enter the CLI's local history until the next rotation pages them out.
- **D20. Strip all thinking at each rotation.** Intake, 2026-10-16. The only strategy measured to
  return 200 after a history edit. Its accuracy cost is measured by M2's acceptance run against the
  full-context baseline, because the owner's proxy drops replayed thinking on some requests and M0
  could not isolate it.
- **D21. M1 is folded into M2 (resolves O1).** Intake under D0, 2026-10-16. The M1 record model
  becomes M2's pinned-state store. PRs #2 and #3 are harvested by lane B and then closed; PR #4 is
  closed as superseded; branches are kept.

- **D22. Codex: forced HTTP and two auth modes.** Intake, 2026-10-16. Measured: Codex's built-in
  provider always tries WebSockets first, while a custom provider defined with `-c` is HTTP-only and
  sends the full input. The adapter forces HTTP through a custom provider. API-key mode (default,
  and the only mode the team runs live under D12): a temporary `CODEX_HOME` whose provider reads the
  rig key with `auth.command`. ChatGPT mode (behind `--codex-auth chatgpt`): the user's own
  `CODEX_HOME` with `-c` overrides only, credentials passed through unchanged; tested only with
  synthetic credentials. Its end-user terms stay with O4.
- **D23. Direct requests identify honestly.** Intake, 2026-10-16. Any request the team sends
  directly, rather than through the real CLI, uses a user agent naming the harness and never carries
  another product's client marker such as a `claude-code-*` beta. In the acceptance harness a shared
  filter with a test enforces it (PR #75); lane A's live-check spike sets an honest user agent and
  only the feature beta it tests (PR #79), without a test. Prompted by the recall experiment, whose four result sends carried
  `claude-code-20250219` copied from H1; that result is kept with the caveat recorded in
  `docs/research/M2-recall-experiment.md`.
- **D24. Engine behaviour settled during the build.** Intake, 2026-10-16, each from a measurement or
  review recorded in the M2 contract: per-turn hook system messages page with their turn while
  other system messages are carried (rules 12 and 13); the window is sized after fixed overhead,
  with a minimal-window fallback (rule 4); expected failures enter an append-only degraded mode, and
  only the terminal step (`fallback_failed`) or a genuine bug forwards the original request (rule
  14); compressed bodies are decoded or
  refused with a 415 (rule 15); recall size and a per-turn recall budget follow the window, with the
  stream held from the first tool call until the room is published. The store side of the budget is
  merged (PR #73); the engine-side room publish and stream hold (lane A's A9) are still pending.
- **D25. Recall preamble V2.** Intake, 2026-10-16. In the first live acceptance run the model made
  no tool calls and lost every paged-only fact. A first-response experiment (415,413 tokens) showed
  that explicit search-then-recall instructions, a worked example, identifier stubs without values,
  and the preamble placed first made the model search for every paged key. V2 is the chosen design:
  `preamble_first` is the engine default on main (PR #77); the store text and identifier stubs are
  in lane B's PR #71, still in review. Its effect on recall accuracy is measured by the H1 rerun.
- **D26. Acceptance fails on any off-path event.** Intake, 2026-10-16. Besides over-bound requests
  (except logged in-turn over-bound turns per rule 5) and model-request 4xx, acceptance 1 fails on `degraded_enter`, `store_error`, `page_conflict`,
  `fallback_failed`, `transform_error`, `turn_room_missing`, or `unclassified_system_message`.

- **D27. Acceptance asks the 54 probes over 6 user turns of 9, for H1 and the baseline alike.**
  Intake, 2026-10-16, decided after a failed run and recorded as such. The second live H1 run
  scored 18 of 54 against the full-context baseline's 44, both under the old single-turn protocol.
  An offline estimate on that run's pages (builder-3, zero spend, a simulated ideal searcher, not a
  model run; script and numbers-only output committed under `spikes/m2/estimate/`) found every
  probe answerable on its own (54 of 54, the search hit carrying the answer), but answering all 54
  in one turn needs about 91,000 tokens of returned evidence (31,002 with the leaner hits of B17)
  against a measured recall room of 23,042 (bound 150,000), so even an ideal searcher tops out
  near 38 to 43. Under D17 (no paging inside a turn) and the per-turn recall room, Contexto cannot
  pull more evidence into a single turn than that room holds, and long real sessions do not ask 54
  deep-history questions at once. The probes are therefore asked over 6 user turns of 9, a fixed
  assignment (seed 20261016) whose algorithm builder-4 recorded at 05:39:09Z before computing
  anything under it (queue row `qitem-m2-d27-protocol`; the assignment is in
  `docs/research/M2-acceptance-harness.md`). A what-if script already running when the stop
  arrived had computed 2-, 3-, and an unseeded 6-turn split in one pass; builder-3 deleted those
  numbers unread, they were never reported, and they played no part in the choice. To keep the
  comparison symmetric, the full-context baseline is also rerun under the same 6 by 9 assignment
  (6 sequential full-context requests, each including the earlier probe turns), and acceptance 2
  compares H1 with that baseline, not with the old 44. The single-turn scores (H1 18, baseline 44)
  stay on record, and a single turn that needs more evidence than the room holds is a stated known
  limitation.

- **D28. Fix stale chain answers in search, then rerun both arms on new content.** Intake,
  2026-10-16, decided after a failed run and recorded as such. Live run #2 (main c8f0b08, D27
  6 by 9) passed acceptance 1, 3, and 4 and failed acceptance 2 narrowly: H1 44 and baseline 45
  overall, but chain 22 against 24 (gap 2, limit 1). All 5 H1-only misses were deep chain probes
  answered with a stale value. A zero-spend analysis (builder-3) found 4 of the 5 had the newer
  link in the retained window while the model answered from a search reply whose newest paged hit
  looked current. Changes, each reviewed at exact head: search lists matches from the retained
  window first, newest first, at most 3 per reply (contract #125 and #134, code #131 and #132);
  a paged hit shows its last 3 matching lines with a note giving the first match's offset, and
  further pages are listed with their first and last offsets (B21, #134 and #135);
  the harness plants chain links in order (#127). Measured on main before the rerun (targeted
  experiment, samples on 04788ba, replay on 9b644fd, line 740,000, spent 532,157): two targets-only samples on run #2's first probe
  turn scored 3 of 4 (no search calls) and 4 of 4 (one search per target, retained-window lines
  first); a faithful replay of run #2's third probe turn (original wording and position, all 9
  probes, 2 requests and 14 searches as in run #2) went from 5 of 9 to 8 of 9, with 3 wins
  (including the target) and 0 regressions; the remaining miss is a probe whose content run #2
  planted in the wrong order, so it tested nothing. One sample of one turn, so this is evidence
  for the rerun, not an acceptance result. Options considered: (a) accept the failure and list
  stale chain answers as a known limitation, rejected because the miss had a found, fixable cause;
  (b) widen the per-category tolerance, rejected as moving the bar after seeing the result;
  (c) rerun H1 only against the old baseline of 45, rejected because the plant-order fix changes
  the content, so the arms would no longer be comparable. Chosen: rerun both arms on the new
  content under D27 on main with B19, B21, the plant-order fix, and B20, after a zero-spend dry
  run; H1 first, the baseline only if acceptance 1 and 3 pass. The gate's worst case is 6,874,704
  (head 47,628 measured after the tool definitions grew, #140); the approval line is a hard stop at
  6,880,000, on main fbb1303.

- **D29. Cut v0.1.0 now that M2 acceptance passed; this settles O5.** Intake, 2026-10-16, on the
  owner's instruction ("just keep going, I would prefer to come to a finished project ... just
  have free reign", spelling corrected; the owner's next message to intake in the same session
  added "but nothing to publish outside this repo though", which is not otherwise on file). The
  private-only scope rests on D14, D0, and `RELEASING.md` in any case. Live run #3 on main
  fbb1303 passed acceptance 1 to 4 (STATUS.md), and the M2 contract's criterion 6 says intake then
  cuts v0.1.0. Intake sets the repository variable `RELEASES_ENABLED` to `true` before merging the
  pull request that records this decision, so that merge's push releases. Semantic-release 10.7.0
  (the workflow's pin), run locally with `--noop` on main 75105f5, proposes `v0.1.0` with no
  earlier tag. The release is a tag and a GitHub Release on this private repository with the sdist
  and wheel attached; no package registry (D14, D0). Options considered: wait for the owner to
  return (rejected: the owner asked to come back to a finished project, and a private release
  is reversible by deleting the tag and release); release after B22 (rejected: B22 refines search
  but is not needed for what acceptance measured, and it ships in a later version).

- **D30. Codex acceptance: run H1 only, and skip the baseline once H1 scores 54 of 54.** Intake,
  2026-10-16. Codex acceptance (`docs/research/M2-codex-acceptance-plan.md`) uses gpt-6-astra at
  reasoning effort low and bound 64,000, because the full-context baseline must stay under Codex's
  own compaction point (about 245,000 of a 272,000 window) while acceptance 1 needs history of at
  least 2.5 times the bound; so the shipped default of 150,000 is untested on Codex. It ran in two
  stages with separate approvals: step 1, H1 alone for acceptance 1 and 3 (line 5,385,600, through
  the owner's proxy with the rig key under D12, retries pinned and Codex's analytics and plugin sync
  off in the harness only), and step 2, a no-tools replay of the 6 probe requests as the baseline
  (worst 1,522,330). Step 1 on main ca05b29 spent 922,201, passed acceptance 1, and answered 54 of
  54, which reviewer-1 rescored independently from the run's local files. Since no baseline can
  score above 54 overall or in any category, acceptance 2 passes by arithmetic, and step 2 was not
  run. Options considered: run step 2 anyway for a baseline figure (rejected: 1.5M tokens for a
  number that cannot change the verdict); run both arms at once as for Claude (rejected: staging
  let a failed H1 stop before the baseline spend).

- **D31. Keep the default bound at 150,000, including for OpenRig seats; this settles O3.**
  Intake, 2026-10-16. A32 (`docs/research/M2-seat-bound.md`, modeled on a synthetic seat session
  matched to M0's measured heads, labelled) compared 150,000, 160,000, and 170,000 on Opus 5.5.
  The decisive figure is the largest single turn that still stays under Claude Code's 200,000
  idle-compaction minimum: on a 92k head 87,141 at 150,000, 78,133 at 160,000, and 67,225 at
  170,000 (82k head 88,133, 77,225, 71,350). A larger bound lets smaller reads trigger the
  compaction the owner's first principle (D2) rules out. 170,000 would give a larger window (about
  51k against 31k, 10 rotations against 15) for up to 13.8 percent more cost on a 92k head, and no
  accuracy figure says it is needed (recall room never below 12,000; M0 found no slip by context
  size). Options considered: 170,000 for seats (rejected: smaller safe single turn, more cost, no
  measured gain); a per-head automatic bound (not pursued: it would still trade the safe turn size
  against window). Revisit only if a live seat check shows the 150,000 window costs recall. What
  no bound fixes: a single read near 149,000 tokens crosses 200,000 at every bound; the headroom it
  leaves behind is A33's subject.

## Repository and process

- **D13. Repository lives at VentureForgre/contexto, private, PR-only `main`.** Owner, 2026-10-15.
  The personal account's plan reports `free` through the API (the owner believed it was Pro), and
  branch rules are unavailable for private repos on that plan; the
  org is on Team. Ruleset 25202810: PR required, no force-push, no deletion, no bypass, 0 required
  approvals because all seats share one GitHub account. Exact-head review by a reviewer seat is a
  team rule.
- **D14. Releases follow discord-cli's Conventional Commit versioning, adapted.** Owner request,
  intake design, 2026-10-15. Versions come from tags, so CI never commits to `main` (which the
  ruleset would reject); artifacts attach to GitHub Releases on this private repo only. No package
  registry publishing (D0).
- **D15. M1 (ledger, checkpoints, recovery) paused when M0 started.** An operator message relaying
  the owner's setup boundary, 2026-10-15. The owner had not chosen M1; an earlier intake occupant drafted it. Its PRs #2, #3,
  #4 stay open and untouched until decision O1.

## Open decisions

All open decisions are intake's to make under D0 unless marked otherwise.

- **O2. History-edit strategy under preserved thinking.** Resolved by D20 (strip all thinking at
  rotation); kept here for its evidence. Vendor-documented: on Fable 5.1, Opus 5.5,
  Sonnet 5.5, and Haiku 5.5, editing history before a thinking block invalidates it, and the API
  returns 400 by default only for accounts created on or after 2026-08-31. Measured by builder-4's
  conformance probe (4,973 tokens; claude-haiku-5-5 and gpt-6-astra, both through the owner's
  proxy `cliproxy.sullylab.de`, D11; not yet reviewed; whether the probe set
  `prefix_mismatch_behavior` is not stated in the handback):
  stripping all thinking at a rotation returns 200; changing system or editing an earlier user turn
  while keeping thinking returns 400; dropping the oldest turns while keeping thinking returns 200
  but the API drops the thinking, so an untouched recent suffix does not preserve it; this account
  does not enforce by default; on the OpenAI Responses API, editing or dropping a turn before an
  encrypted reasoning item returns 200, and whether the reasoning was used is not observable.
  Lean: strip all thinking at rotation, pending the strip-thinking arm's accuracy numbers.
- **O3. Working-window bound.** Settled by D31 (150,000, including for seats).
- **O4. End-user authentication guidance.** API key only, or also subscription logins, for people
  who run Contexto themselves. Needs the terms question answered in the product docs. Owner
  decision, because it bears on what may be shared beyond this repository.
- **O5. First real release.** Settled by D29 (v0.1.0 after M2 acceptance).
- **O6. Upstream affinity on the owner's proxy.** Measured: full cache misses with byte-identical
  prefixes (4 of 14 eligible throwaway calls) and thinking dropped on every request
  (`end_user_binding_mismatch`) through `cliproxy.sullylab.de`. Lane 1's black-box test (L1-e,
  81,319 tokens): over 10 haiku requests the keep/drop pattern falls into exactly 3 consistent
  classes, and a byte-identical series shows exactly 3 cache writes before steady reads (counts
  measured; reported by manager-1, doc pending in lane 1's L1-f PR). Inferred: the route spreads
  requests across at least 3 upstream identities, and each byte-identical miss is the first request
  to reach an identity. Opus over 5 requests kept all thinking (inconclusive). Fix is a per-session upstream affinity setting on the owner's
  infrastructure. Owner decision; intake does not touch the proxy (D11).
- **O7. OpenRig context-usage clock bug.** Measured by lane 1: every seat's context usage reads
  `stale_generation` because tenure boot times are stored in UTC without `Z` and parsed as local
  time (UTC-5), blinding `rig ps`, `whoami`, and compaction planning for about 5 hours after boot.
  Upstream OpenRig fix; not Contexto code. Owner decision whether to report it.

