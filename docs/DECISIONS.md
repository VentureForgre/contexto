# Decision log

Every decision that shapes Contexto, with who made it, when, and why. Newest last within each
section. "Owner" is the human who owns the project; "intake" is the intake lead seat acting under
the owner's delegation. Open decisions are listed at the end with the options and the current lean.

## Standing authority

- **D0. Intake has free rein to finish the project.** Owner, 2026-10-15. The owner prefers to return
  to a finished project and asked that STATUS show decisions taken, why, and decisions still open.
  The owner's one limit: nothing is published outside this repository (no PyPI, npm, Homebrew,
  public repos, or public posts). (Superseded in part by D42: the repository itself goes public;
  the rest of the limit stands.) Intake added two limits of its own: user-level Claude and Codex
  configuration on the owner's machine is not changed, and heavy spend on the ChatGPT plan the
  team's Codex seats use waits for the owner. Intake reads D0 as superseding the owner gates written into earlier briefs,
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
- **D8. Seats stay on `builtin:yolo`; the Codex test seat uses a ChatGPT login.** Owner,
  2026-10-15. Under yolo Codex drops `-p` profiles and project-local config cannot set
  `model_provider`, so routing the seat to the rig proxy would change the user-level Codex default.
- **D9. The 2M compaction-loss run was replaced by a quality-versus-context-size probe.** Intake under
  D2, 2026-10-15. Forcing a session into compaction measures the wrong thing; the probe finds the
  context size where recall starts to slip, which bounds the working window.
- **D10. Direct-API replay arm: up to 450k tokens.** Intake, 2026-10-15. First approved at 309,507
  tokens for two checkpoints with thinking stripped; then widened to a 40k checkpoint in both arms
  and at about 92k, each with thinking preserved and stripped (408,851 tokens planned), so the
  replay is symmetric and comparable with the seat run at the same size. The third large checkpoint runs only if the smaller
  ones show a gap of at least 2 probes.
- **D11. The rig proxy is the owner's infrastructure.** Owner, 2026-10-15. The rig proxy is the
  owner's CLIProxyAPI deployment; auth headers that reach it stay on owner infrastructure.
- **D12. Phase 2 rewriting is authorized under D0, on owner infrastructure only.** Intake,
  2026-10-15. The M0 brief gated rewriting on an owner decision about Q5; the owner delegated that
  decision through D0. This settles Q5 for development: rewriting experiments run through the
  owner's proxy with the rig key (Claude) or the same proxy through a throwaway `CODEX_HOME` whose
  provider uses `auth.command` (Codex). The team's rewritten traffic never uses a ChatGPT or
  claude.ai login, and the team never edits user-level config files. What end users
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
  no-registry scope rests on D14, D0, and `RELEASING.md` in any case; the private-only part is
  superseded by D42. Live run #3 on main
  fbb1303 passed acceptance 1 to 4 (STATUS.md), and the M2 contract's criterion 6 says intake then
  cuts v0.1.0. Intake sets the repository variable `RELEASES_ENABLED` to `true` before merging the
  pull request that records this decision, so that merge's push releases. Semantic-release 10.7.0
  (the workflow's pin), run locally with `--noop` on main 75105f5, proposes `v0.1.0` with no
  earlier tag. The release is a tag and a GitHub Release on this repository (private until D42) with
  the sdist and wheel attached; no package registry (D14, D0). Options considered: wait for the owner to
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
  leaves behind is A33's subject. This rests on an inferred premise, not yet verified: that
  Claude Code's idle compaction counts the bounded usage Contexto reports. If it counts the local
  history instead, no bound helps, and only Claude Code's own `idleCompaction: false` setting, which
  belongs to whoever runs the CLI, prevents it.

- **D32. The bounded fallback also pages context blocks of later turn starts.** Owner approval,
  2026-10-18. Claude Code 2.1.302 on the 4.x model ids puts `<system-reminder>` blocks in every
  later turn-start user message (the token countdown, a 2,720-token plan-mode entry, file-changed
  notes repeated on each resume, task reminders; `spikes/m2/scale/reminders/`). The contract carried
  every one forever and the 8,000-token fallback paged only system messages, so carried content
  grew 45 to 425 tokens per turn until `bound_too_small` and over-bound requests
  (`docs/research/M2-scale-30m.md`). Now, at a rotation and only while carried growth exceeds 8,000
  tokens, the fallback may also page the context blocks of every turn-start message after the
  first. It keeps the first turn-start message's blocks (CLAUDE.md and the session context on
  `messages[0]`) and the latest block of each recognisable kind, from a documented table of
  first-line signatures measured on 2.1.302 (`CONTEXT_BLOCK_KINDS`: token countdown, plan mode
  entry and exit as one kind, file changed on disk, task-tools reminder, and the kinds that make up
  `messages[0]`). Superseded blocks of a kind go first, then unrecognised ones, oldest first. Each
  paged block is its own immutable page (a user message holding that block as sent), listed in
  the index and recallable; `paged_carried` identifies it by a hash of its position and content,
  so byte-identical countdowns in different turn starts stay distinct and a restart (rule 16)
  resumes the same carried bytes. Measured on the captured bodies with the engine's own functions
  (`analyze.py`, fallback applied at every turn start), what the fallback cannot page on
  claude-sonnet-4-6 over 48 turns now stays between 3,386 and 3,557 with bare prompts and between
  3,382 and 5,251 in the busy runs (the top while plan mode is on and its entry is the latest of its
  kind), and is 3,557, 3,794 and 3,794 at turn 48, against 5,452, 13,256 and 22,953 before (bare
  prompts, one process, resume every turn). Carried content after the fallback peaks at 11,330 and
  11,381 in the busy runs, against 13,256 and 22,953. Options
  considered: page every later block with no kept kinds (rejected: the latest countdown,
  plan-mode state and file note are current instructions the model should keep seeing); keep the
  latest file note per path (rejected: unbounded in the number of files edited outside the CLI);
  cap carried blocks by count instead of tokens (rejected: one plan-mode entry is 60 times a
  countdown, so a count cap does not bound F); strip reminders from the request (rejected: carried
  content is forwarded as the CLI sends it, and what is not sent must stay recallable).

- **D33. Session-start is recognised by its content, not by settings fields.** Owner approval,
  2026-10-18. On claude-opus-5-5 and claude-haiku-5-5, Claude Code 2.1.302 attaches an `--effort`
  change's `output_config` to the turn's first system message (a countdown, a file note, or the
  plan-mode text), and the classifier then called it session-start, carried forever and never
  paged: the session-start class grew from 2,770 to 9,272 and 11,637 tokens over 16 effort
  changes. On claude-sonnet-5 and claude-fable-5 the session's environment message has no
  `output_config`, so it classified as carried text and was the first message the fallback would
  page. In every main request of the 13 captured runs the session-start message on 5.x ids is the
  one system message whose first line, after an optional `<system-reminder>` tag, is
  `# Environment`, and no other has it. That signature now makes a message session-start, with or
  without settings fields; a message with text-only content plus settings fields and no signature
  is a settings change (non-text content stays unclassified: carried and logged), carried while it is the latest per settings key (so the current effort stays in
  view) and pageable as carried text once a later one supersedes it. The fallback never hides the
  latest settings change per settings key or the latest text of a recognisable kind, even when an
  older byte-identical copy was paged (measured: effort resets land on byte-identical countdown
  messages); any other pageable message whose hash is in the paged set is hidden with it. Measured
  on the captured bodies, what the fallback cannot page in the busy one-process claude-opus-5-5 run
  now stays between 3,301 and 4,897 tokens over 48 turns (9,804 at turn 48 before), and between
  3,304 and 4,902 with task tools (10,133 before). With a resume every turn it reaches 5,643 at
  turn 48 (12,169 before; claude-haiku-5-5 5,504 against 12,035): the same three system messages
  and two blocks are carried, but the latest effort-change message the CLI sends grows as it
  re-sends unread file notes. On the claude-sonnet-5 and claude-fable-5 probes the
  environment message is now carried and never paged (not-pageable 534 before, 3,298 and 3,164
  now, which is that message). Options considered: position (`messages[1]`, rejected by the
  contract: a hand-written resume puts the system text after the new prompt); the first system
  message with settings fields (rejected: positional in effect, and absent on claude-sonnet-5);
  keep classing content plus settings fields as session-start but page superseded ones
  (rejected: leaves the claude-sonnet-5 environment message pageable); drop the in-message
  `output_config` (rejected: carried content is forwarded as sent). Risk: the signature is a
  Claude Code text, recorded with its version (`SESSION_START_SIGNATURE_CLAUDE_CODE_VERSION`); if a
  later CLI changes that heading, the message falls to the settings-change or carried-text class
  and can be paged once superseded, so a CLI upgrade should re-run the reminders spike.

- **D34. Search matches every word in any case, ranks by BM25 with a recency prior, and both
  processes cache page reads.** Owner approval, 2026-10-18, including the contract change. The
  store-scale spike (`spikes/m2/scale/store/`, synthetic, zero spend) measured the exact,
  case-sensitive, newest-first search at 17 to 6,000 pages: the planted page was among the 3
  detailed hits for 61 of 86 probe queries at 17 pages and 39 at 6,000; 25 queries missed at
  every size, 10 of them on case or word order alone; the latest value came first for 19 of 24
  changing-value queries at 17 pages and 12 at 6,000. Each search read and parsed every page (0.69 s
  at 6,000, `results/d34_before`), and `index_text` read every page, twice per rotation, in the
  proxy's request path (0.59 s per call). What changed: (1) a page matches when every
  whitespace-separated word of the query occurs in it, case-insensitively, in any order, each word
  as a substring, so for any query with a word every page the old rule matched still matches and
  identifiers, paths, and their parts keep working. The one exception is a query with no words
  (whitespace only), which the old rule matched on almost every page and which now finds nothing.
  Case is folded one character at a time and every `ς` written `σ`: `str.lower` on a whole string
  lowers capital sigma by its place in a word, so `ΑΣ` missed `ΑΣΑ` (reviewer-1; a property test
  over random stores checks the old matches are kept); (2) matching pages fall in three groups:
  turn pages holding the whole query, turn pages holding only its words, and pages holding only
  paged context blocks (system messages and Codex `developer` items, or user messages made only of
  `<system-reminder>` blocks, the pages D32 writes, recognised from the messages whatever their
  stub), which repeat the same words every few turns and are short, so they outranked the turn that
  is about the words (reviewer-2); they stay searchable and recallable. A Codex developer page has
  no searchable text (search reads Responses items through `as_message`, which drops system-like
  items, B23), so in practice the group holds Claude Code pages only. Within each group pages rank by BM25 (k1 1.2,
  b 0.75) over the words and the whole query, times a recency prior `1 + 0.5 r`, `r` the page's
  position among the matches from 0 (oldest) to 1 (newest): a newer page wins unless an older one is
  clearly more about the query, and the oldest needs 1.5 times the newest's relevance; (3) the
  newest matching turn page always takes one of the 3 detailed places (the third, if it ranked
  lower; owner's choice of reviewer-2's options): BM25 discounts long pages, and a long newest turn
  that states the current value lost to short older ones. It costs one place on common-word
  queries. It does not cover a short passing mention newer than the statement. In reviewer-2's
  13-turn cases turn 4 says "Set max_retries to 3", the long turn 10 says "max_retries is now 7",
  and short turns mention the key in passing. With mentions at turns 5, 6, and 13 the detailed
  pages are 13, 6, and 5, so neither value is shown, and turn 10 is listed first among the rest.
  With mentions at turns 6 and 13 only, it is worse: an older short page that states an old value
  is shown in detail while the long turn with the current value is only listed (detailed 13, 6,
  and 4, so 3 is shown and 7 is not; the old newest-first search showed 13, 10, and 6). The cheap
  fixes fail: b 0 (no length discount) brings back the bias toward long old log dumps; a second
  recency place fails when four later short mentions follow the statement (6, 8, 12, 13); a length
  cap helps only near the average length, which is b 0 again. Follow-up, both zero spend: choose b
  on a harness variant that draws page sizes from the real turn sizes in
  `spikes/m2/scale/store/calibration.json`, or take BM25's length from the messages that hold a
  match rather than the whole page, so a tool output without the word does not dilute the turn's
  own statement; (4) the 3 best pages are shown under the line `[best matching pages by relevance,
  shown newest first:]`, and the next 50 after them: in each part turn pages come first, newest
  first, then context pages, newest first, after the line `[paged reminders and system messages,
  not dated by turn:]`, which is shown only when a context page is. So among the turn pages shown
  the newest statement of a fact comes first, as D28 requires. A context page's turn is the turn at
  which the proxy paged the block (D32 pages blocks at a rotation, numbered after every turn paged
  by then), not the turn that sent it, so it cannot order a context page against a turn page: by
  turn alone, file-changed notes saying "take it as the current state" of an old value were shown
  above the turn that superseded them (reviewer-2, with D32's `RewriteEngine` over 59 turns); (5) a page's matching lines are the ones holding the most of the query,
  the whole query first; in-view entries match by the same rule and still come first (D28); (6)
  the MCP server keeps each page's folded searchable text as UTF-8 bytes (a `str` with one emoji
  takes 4 bytes per character), and the proxy each page's id, turn, stub, and index line, for the
  life of the process, each listing `pages/` per call and re-reading only new or changed files;
  `page_out` adds what it writes without a read; (7) pages are narrowed word by word and the scan
  stops when none is left. No file is added to the store, pages stay immutable, and old sessions
  are read as they are. The recall preamble is unchanged: it is acceptance-measured (D25), and the
  exact terms it asks for still match and rank first. The server instructions (`mcp.py`
  `INSTRUCTIONS`, "contexto_search for an exact substring") are also left as they are: an exact
  substring still matches and ranks in the first group, and changing them changes the initialize
  payload of every session. Measured on the same harness before and after
  (`spikes/m2/scale/store/README.md`; the after run is at 958e597, with the review fixes): at 6,000
  pages top 3 rose from 39 to 56 of 86, latest first from 12 to 20 of 24, facts from 26 to 29 of 30;
  at 3,000, 44 to 60, 14 to 21, 27 to 29; with the 0.2 percent topic floor at 6,000, 15 to 41, 9 to
  17, 12 to 24. Search at 6,000 pages takes 0.09 to 0.22 s instead of about 0.7 (the first search of
  a process loads the cache in about 0.74 s), `index_text` 0.025 s instead of 0.59 (0.55 once after
  a proxy restart), and the MCP server's peak memory fell from 456 to 293 MB. A 120-word query with
  one absent word takes 0.05 to 0.19 s at 6,000 pages instead of 7.5 s (`long_query.txt`); a query
  whose every word is on nearly every page still scans once per word. Tuning: k1 and b are the
  usual defaults; the recency prior and the whole-query group were added after BM25 alone put the
  latest value first for 7 of 24 on these same probes (`docs/research/M2-scale-30m.md`), so there is
  no held-out probe set; the 0.5 weight and the display order were not swept on the probes. Every
  synthetic page is 19,000 to 30,000 tokens, so the harness does not exercise BM25's length
  discount; the unit tests do. Limitations: the MCP server's memory grows with the store, with no
  cap; the first search of each process reads every page; single common words can still bury a
  fact at 6,000 pages (5 queries). Options considered: BM25 alone (rejected: the prototype put the
  latest value first for 7 of 24 at 6,000, breaking D28); case-insensitive all-words matching with
  newest-first order only (rejected: common words still push the planted page out; the prototype's
  count was not recorded); a time decay with a half-life in pages (rejected: any half-life suits
  one session length and not another, while a position among the matches does not depend on N);
  relevance bands, newest first inside the top band (rejected: for a common word every passing
  mention falls inside the band, which restores the old order); showing the best pages in rank
  order (rejected: the newest statement among them would not come first); no length discount, or
  a length cap chosen on a harness with spread-out page sizes (not chosen: the owner took the
  newest-page place, which keeps D28 by construction); excluding context-block pages from search
  (rejected: PR #203 relies on a paged block staying recallable); whole-word tokens or stemming
  (rejected: parts of identifiers such as `TIMEOUT` in `HTTP_TIMEOUT_S` would stop matching, and
  stemming is fuzzy matching, which the owner ruled out with "not RAG"); SQLite FTS5 written at
  page time (deferred: the trigram index that keeps substring semantics measured 632 MB at 6,000
  pages, 2.5 times the store, and search is no longer the slow part); an append-only stub manifest
  written by `page_out` for both processes (rejected for now: a new store file that must stay
  consistent with quarantine, with a crash between the page and the manifest write, and with
  stub-rule changes such as `current_stub`, to save one read of every page after a proxy restart).

- **D39. The MCP server holds search text up to a budget, 64 MB by default, newest pages first.**
  Assigned 2026-10-24 from the STATUS known limitation; the contract change is recorded here for
  review. D34's page cache kept every page's folded text in the MCP server for the life of the
  process, so a long session's server grew without limit (peak RSS 290 MB at 6,000 synthetic pages, 193 MB of it text). What changed: (1) the server
  holds text for at most `CONTEXTO_SEARCH_CACHE_MB` megabytes (1 MB = 1,048,576 bytes; default 64;
  `0` holds none), or `contexto mcp --search-cache-mb`, which overrides it. `contexto run` passes the
  variable to the server's environment for Claude Code and Codex when it is set (Codex starts MCP
  servers with a restricted environment). An invalid variable is logged `search_cache_invalid` and
  the default used: neither CLI reports an MCP server that fails to start, so refusing to start
  would silently remove every Contexto tool. An invalid flag is a usage error; (2) it holds the newest
  pages' text, by (turn, id): a page read during a search is held, and the oldest held text is let go
  until the held bytes fit. A page whose text is not held is read from its file during each search
  and let go after it. Not LRU: every search reads every page (a word matches as a substring, so no
  index can skip a page), in the same order, so with a store larger than the budget LRU would evict
  each page just before the next search needed it and save no read; a fixed newest set saves the
  reads of the pages it holds; (3) `rank` takes a page at a time and keeps of it only which words it
  holds, its length, and, for a match, its term counts, so a page read for a search is not held for
  the length of the query. Results are unchanged: each word's page count, the average length, and
  the scores come from the same integers in the same order. Narrowing keeps D34's property that a query reads no page twice when no page holds every
  word; when some page does, the pages that lacked an earlier word are read again for the
  frequencies of the later words, except that a page just read is tested for every word at once
  when the query has at most 6 words (a read costs about as much as 6 failing substring tests on a
  32 KB page: 92 us against 15 us) or a match is already known; (4) the server keeps, besides text,
  each page's id, turn, and whether it holds only context blocks (672 bytes per page, 4.0 MB at
  6,000). It no longer keeps the page stub (2.4 KB per page), which only the index reads and the
  server never builds; a process that builds the index reads it as before. Measured on the store
  harness at 6,000 pages (`spikes/m2/scale/store/README.md`, `results/d39/`): `quality.csv` is
  byte-identical before and after at 64 MB, unlimited, and 0, and every stdio search reply has the
  same length; `tests/test_search_cache.py` compares a 4 KB budget, budget 0, and no budget with a
  reference ranking on random stores. Peak RSS of the MCP child: 290 MB before, 129 MB at 64 MB, 45
  MB at 0, 277 MB unlimited. Search (rare / common / multi-word): 0.096 / 0.175 / 0.248 s before,
  0.548 / 0.633 / 0.693 at 64 MB (a third of the text held), 0.718 / 0.839 / 0.876 at 0, and 0.095 /
  0.167 / 0.239 unlimited, so the rewritten ranking costs nothing when every page is held; a page
  outside the budget costs about 0.1 ms per search to read, parse, and fold. The first search of a
  process is 0.78 to 0.85 s in every run. The 64 MB default holds about 2,000 of these pages (46M
  tokens of history); a 30M-token session (1,616 pages in `docs/research/M2-scale-30m.md`) is about
  42 MB of text at this store's 1.4 bytes per token, so it searches as fast as before. Options
  considered: LRU (rejected, above); no cap with a smaller representation, such as zlib-compressed
  text (rejected: memory still grows with the store, by a constant factor); an exact prefilter that
  skips pages lacking a word's trigrams or tokens (rejected: building it in Python took 0.7 ms per
  page, so 4 s more on a first search at 6,000 pages, and a per-page vocabulary string is 18 percent
  of the text on the synthetic pages, more on real ones, held for every page, so it grows with the
  store); folded text written beside the pages for fast re-reads (rejected: a new store file to keep
  consistent with quarantine and crashes, and about 75 percent more disk); a larger default (not
  chosen: 64 MB, the size the task suggested, already holds a 30M-token session).
  Limitation: a store larger than the budget pays the read of every page outside it at each search,
  0.55 to 0.69 s at 6,000 pages with the default; raise the budget when search time matters more
  than memory.

## Repository and process

- **D13. Repository lives at VentureForgre/contexto, private, PR-only `main`.** Owner, 2026-10-15.
  (Private status superseded by D42; the PR-only rules stand.)
  The personal account's plan reports `free` through the API, and branch rules are unavailable
  for private repos on that plan; the org is on Team. Ruleset 25202810: PR required, no force-push, no deletion, no bypass, 0 required
  approvals because all seats share one GitHub account. Exact-head review by a reviewer seat is a
  team rule.
- **D14. Releases follow discord-cli's Conventional Commit versioning, adapted.** Owner request,
  intake design, 2026-10-15. Versions come from tags, so CI never commits to `main` (which the
  ruleset would reject); artifacts attach to GitHub Releases on this private repo only. No package
  registry publishing (D0). (The private-only wording is superseded by D42: releases stay on this
  repository's GitHub Releases, which become public with it.)
- **D15. M1 (ledger, checkpoints, recovery) paused when M0 started.** An operator message relaying
  the owner's setup boundary, 2026-10-15. The owner had not chosen M1; an earlier intake occupant drafted it. Its PRs #2, #3,
  #4 stay open and untouched until decision O1.
- **D42. The repository goes public; GitHub Releases stay the only distribution.** Owner,
  2026-10-24. The owner decided to make VentureForgre/contexto public. GitHub Releases on this
  repository remain the only way Contexto is distributed; nothing goes to a package registry (PyPI,
  npm, Homebrew) yet. This supersedes D0's "no public repos" limit for this repository and the
  private-only wording in D13, D14, D29 and `RELEASING.md`.
  - **Scrub at `HEAD`.** Before publishing, tracked files were scrubbed: the owner's proxy
    hostname, statements about the owner's own accounts, plan usage and the organization's
    billing, and the self-hosted runner's machine name were replaced with neutral wording that
    keeps the technical content (which auth modes, headers and endpoints were measured, and how
    they behaved).
  - **Terms analysis removed on purpose.** The vendor terms quotes and the inferred terms reading
    in `docs/research/M0-interception.md` section 6, and the summaries of them elsewhere, were
    removed. They are public vendor text, but here they were applied to the owner's own use. The
    repository makes no claim about terms compliance. End-user auth guidance stays open as O4,
    and the README tells users to follow their providers' terms for the credentials they use.
  - **History is not rewritten** (owner decision). Old commits, pull request diffs and pull request
    comments keep the proxy hostname, one home-directory path, the owner's commit email, the
    runner's machine name and billing notes. A rewrite would need force pushes, which the ruleset
    forbids, and would still leave the pull request comments.
  - **CI on fork pull requests.** CI runs every job on the organization's self-hosted runner, so
    a fork pull request on a public repository could run code on it. When the repository goes
    public, the Actions approval policy for fork pull requests is set to "require approval for all
    external contributors" at the same moment. CI moves to GitHub-hosted runners in the CI pull
    request. The organization's runner group stays open to public repositories because other
    public repositories in the organization use it.

## Open decisions

All open decisions are intake's to make under D0 unless marked otherwise.

- **O2. History-edit strategy under preserved thinking.** Resolved by D20 (strip all thinking at
  rotation); kept here for its evidence. Vendor-documented: on Fable 5.1, Opus 5.5,
  Sonnet 5.5, and Haiku 5.5, editing history before a thinking block invalidates it, and the API
  returns 400 by default only for accounts created on or after 2026-08-31. Measured by builder-4's
  conformance probe (4,973 tokens; claude-haiku-5-5 and gpt-6-astra, both through the owner's
  proxy, D11; not yet reviewed; whether the probe set
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
  (`end_user_binding_mismatch`) through the owner's proxy. Lane 1's black-box test (L1-e,
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

