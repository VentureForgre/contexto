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
  synthetic credentials. Its end-user terms stayed with O4, settled by D35.
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
  b 0.75; D41 changed b to 0 and counts a term once per text, at most 3) over the words and the
  whole query, times a recency prior `1 + 0.5 r`, `r` the page's
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
  fixes fail: b 0 (no length discount) brings back the bias toward long old log dumps (D41 measured
  this and pairs b 0 with counting a term once per text); a second
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
- **D35. Supported upstreams for people who run Contexto themselves: the vendor API with an API key,
  or a CLIProxyAPI instance; this settles O4.** Owner, 2026-10-24: "this project should work with
  CLIProxyAPI" (https://github.com/router-for-me/CLIProxyAPI, a local or hosted proxy that serves
  Claude Code, Codex, and others through subscription logins and API keys). The owner already runs
  Contexto through a CLIProxyAPI deployment (the rig proxy, D11). What it means: (1) Contexto
  supports two kinds of upstream for end users: the vendor's API with the user's API key
  (Anthropic for Claude Code, OpenAI for Codex), or a CLIProxyAPI instance reached with one of its
  client keys; (2) a subscription login (claude.ai, ChatGPT) lives in CLIProxyAPI or in the
  vendor's own CLI, never in Contexto. Contexto forwards the credential the CLI sends and never
  stores or logs it. `--codex-auth chatgpt`, in which Codex keeps its own ChatGPT login and the
  proxy passes it to the ChatGPT backend, stays as it is, tested only with synthetic credentials;
  its launch note and help now give this guidance instead of pointing at O4; (3) users must follow
  their providers' terms, including any terms on subscription use through a proxy; Contexto does
  not decide that for them. Compatibility, read
  from CLIProxyAPI's source (local checkout, v7.2.142; the running binary is 7.3.12) and checked
  against the code: its
  `/v1/messages`, `/v1/messages/count_tokens`, and `/v1/responses` sit under the root that
  `ANTHROPIC_BASE_URL` names, so Claude Code needs the root and Codex the root plus `/v1`, which is
  already Contexto's Codex default (D22); it accepts its client key as `Authorization: Bearer`,
  `X-Api-Key`, `X-Goog-Api-Key`, `?key=`, or `?auth_token=`, and Contexto forwards all five
  unchanged; model
  names reach it unchanged and it resolves them (aliases, prefixes); its errors, including 401
  bodies that are not in Anthropic's shape (`{"error":"Missing API key"}`), pass through unchanged
  (rule 7). One change in the proxy: CLIProxyAPI sends `: keep-alive` SSE comment events when
  `streaming.keepalive-seconds` is set, and the stream hold (contract PR #74) passed only `ping`
  events, so a heartbeat that arrived after a tool call started was held with the content. Comment
  events now pass at once, like `ping`: they carry no content, so the order of content events is
  unchanged, and the CLI keeps receiving bytes while a long tool input is held. A comment passes
  at once only when it ends with its line ending; an unterminated one at end of stream is held
  and released last, so no held event is glued onto it (reviewer-A). Two launcher changes from
  the review (reviewer-B): the user settings file is `$CLAUDE_CONFIG_DIR/settings.json` when
  `CLAUDE_CONFIG_DIR` is set, as in Claude Code (used as given, without `~` expansion; empty
  counts as unset), for both the upstream and the Codex key helper
  (before, Contexto always read `~/.claude/settings.json`); and the Codex session line and
  `proxy_start` event name where the Codex upstream came from (`codex-flag`, `codex-env`,
  `chatgpt-default`, or `claude-<source>+/v1`), never the URL. Measured on
  2026-10-24 through Contexto's real engine, outcomes only: through the owner's proxy,
  `claude-haiku-5-5` `count_tokens`, a non-streaming message, and a streaming message each returned
  200 (both messages `end_turn`, with usage read), and one Codex Responses request on
  `gpt-5.6-luna` to `/responses` reached `/v1/responses` and returned 200 with `response.completed`
  and usage; the local CLIProxyAPI 7.3.12 binary, sent no key through Contexto, returned its 401,
  relayed. Reviewer-B then ran the real CLIs as the guide says (outcomes only): Claude Code
  2.1.302, `contexto run claude -p ... --model claude-haiku-5-5`, exit 0, `POST /v1/messages` 200
  `end_turn` with usage; Codex 0.163.0, `contexto run --codex-key-helper ... codex exec -m
  gpt-5.6-luna ...`, exit 0, `POST /responses` to `<root>/v1/responses` 200 `completed` with usage.
  Claude Code's keyless startup `HEAD /api/hello` gets a 401, which is expected. The owner's
  proxy answered `X-Api-Key` with 401 and `Authorization: Bearer` with 200
  on `/v1/models` and `count_tokens` (measured), so the guide recommends the bearer forms
  (`ANTHROPIC_AUTH_TOKEN` or `apiKeyHelper`). Tests: `tests/test_cliproxyapi.py`, a fake upstream
  built from CLIProxyAPI's routes, auth, error bodies, headers, keep-alives, and failure framing.
  Setup guide: `docs/cliproxyapi.md`. Options considered: API key only (rejected by the owner's
  decision); supporting subscription logins inside Contexto (rejected: credentials would live in
  Contexto, and CLIProxyAPI already handles login, refresh, and rotation).

- **D36. Each request carries a stable session signal a routing proxy can pin to one upstream
  credential: the CLI's own signals plus Contexto's salted `X-Session-Affinity`; on the proxy side,
  consistent-hash affinity; this settles O6.** Owner request ("improve this"), 2026-10-24, including
  the contract change. Cause: the owner's CLIProxyAPI deployment runs several CLIProxyAPI replicas
  behind one load balancer, each rotating credentials on its own (`routing.strategy: round-robin`,
  no session affinity), so a session's requests reach whichever credential each replica's counter
  points at. That gives O6's classes. CLIProxyAPI's own `routing.session-affinity` does not fix it
  alone (read in the source): bindings live in each process, and a new session binds to that
  replica's next round-robin credential, so N replicas can bind one session N ways (reproduced by
  `TestSessionAffinityPerReplicaCacheSplitsSessionsAcrossReplicas` in the proposed CLIProxyAPI
  change, `sdk/cliproxy/auth/session_affinity_consistent_hash_test.go`). What changed: (1) on the
  proxy, a CLIProxyAPI option proposed for upstream and not yet submitted there,
  `routing.session-affinity-consistent-hash`: each session is routed by rendezvous hashing of its
  root session id and each available credential id (the root when the request names it, otherwise
  the parent), so every replica picks the same credential with no shared state; a credential in
  cooldown moves only its own sessions, each to its next-ranked credential, and they return when it
  recovers. It is not deployed. (2) In Contexto, the proxy adds `X-Session-Affinity: contexto-<key>`
  to every request whose upstream host name is not an Anthropic, OpenAI (including Azure OpenAI,
  `*.openai.azure.com`, and Azure AI Foundry, `*.services.ai.azure.com`), or ChatGPT host, unless
  the CLI sent one (contract, Stack). The host is IDNA-normalized first, as the socket layer does
  when it connects, so a name such as `api.anthro<soft hyphen>pic.com` counts as Anthropic's; a name
  that cannot be normalized gets no header. Matching is by name: an IP literal upstream gets the
  header, even a vendor's address, because Contexto cannot tell a vendor IP from a local proxy's.
  The key is the first 32 hex digits of HMAC-SHA-256 of the session id under a random 32-byte salt
  written once per install at `$CONTEXTO_HOME/affinity.salt` (0600), so it is the same after a
  restart and on a resume, and the session id, even a guessable `--session` name, cannot be checked
  against it without the salt. The salt is created only when a header will be sent (never for a
  vendor upstream) and read without following a symlink or blocking on a FIFO; if it cannot be read,
  the session runs without the header and logs `affinity_salt_unavailable`. The proxy routes Claude
  Code on the CLI's own session id first. `claude --resume` keeps that id (measured), so routing
  survives a resume; only a fresh launch or `--fork-session` gets a new id, which can land on
  another credential and miss the cache once. Measured with a local stand-in upstream (zero spend):
  Claude Code 2.1.302 sends `X-Claude-Code-Session-Id` and `metadata.user_id` with the same
  `session_id` on every Messages request; Codex sends `session-id` and `prompt_cache_key`
  (`docs/research/M2-codex-adapter.md`). In CLIProxyAPI's order, `X-Claude-Code-Session-Id`,
  Claude's `metadata.user_id`, and Codex's `session-id` header rank above `X-Session-Affinity`;
  `prompt_cache_key` ranks below it. So both CLIs' requests route on the CLI's own id, which
  Contexto forwards unchanged, as long as Codex keeps sending `session-id`. CLIProxyAPI labels a
  request it routes on `X-Session-Affinity` as client type `opencode`, which only names the signal.
  What is pinned: each distinct session signal maps to one credential. The CLI's id and Contexto's
  key are different identities and generally hash to different credentials, so a request of the
  session that carries no CLI signal gets a stable credential of its own, not necessarily the
  session's. That is harmless for cache and thinking, since such a request shares no prefix with the
  conversation; in practice the key decides routing only for black-box checks. Subagents and side
  requests share the session's key on purpose, as they go through the same Contexto proxy, and
  CLIProxyAPI's consistent hash sends Claude Code subagents to their root session's credential. They
  share the system and tools prefix, so one credential keeps that cache warm. The cost: parallel
  subagents load one credential's rate limit, and a 429 there moves the whole session, main
  conversation included, to the next credential until the first recovers, with one cache miss and
  dropped thinking at each move. Verification once deployed: 10 byte-identical requests with one
  `X-Session-Affinity` should show 1 cache write and 9 reads, and with `--observe-thinking` no
  `end_user_binding_mismatch`. Limits: a replica that has not yet seen a credential's failure keeps
  sending that credential's sessions to it until its own request fails over; requests with no
  session signal at all still use CLIProxyAPI's per-process prefix matcher; all replicas must hold
  the same credentials. Options considered: enabling `session-affinity` alone (rejected: per-replica
  bindings, above); `fill-first` with affinity (config-only and the same on every replica, rejected:
  every session lands on one account until it cools down); sticky cookies at the load balancer (the
  CLIs keep no cookies); client-IP affinity at the load balancer (every request reaches the replicas
  from the same ingress); a shared binding store such as Redis (a new dependency and failure mode
  for the same result); an unsalted hash of the session id (rejected: a guessable name could be
  checked against the key); Contexto rewriting `X-Claude-Code-Session-Id` to its own key (rejected:
  it changes a header the CLI owns and that upstream also reads).
- **D37. `contexto sessions prune` deletes whole sessions, dry run by default, and never one in
  use.** Intake, 2026-10-24, from the 2026-10-16 report that session folders grow without limit and
  could only be freed by deleting files by hand. Contract change: a new subcommand, a new store file,
  and a new `sessions list` field. What changed: (1) `contexto sessions prune` selects sessions by
  `--older-than DURATION` (a positive whole number with `s`, `m`, `h`, `d`, or `w`), `--keep N` (keep
  the N with the most recent activity, ranked over all sessions), and `--session ID`; every given
  condition must hold, and at least one is required (exit 2 otherwise). Age is idle time since the
  session's last activity, the same value `sessions list` shows: the newest modification time of the
  session directory, `log.jsonl`, `ledger.jsonl`, and `pages/`. Without `--yes` it prints each
  selected session with its size and the total, and changes nothing, not even a lock file; with
  `--yes` it deletes them. `--json` prints `dry_run`, `total_bytes` (would be freed, or freed), and
  each selected session with `status` (`would_remove`, `removed`, `skipped`, `failed`) and `reason`;
  (2) liveness: the store had no reliable signal (the ledger and metadata locks are held only for
  one write, and `mcp_listed_at` is a time, not a holder), so every writable `SessionStore` (proxy,
  launcher, MCP server) now holds a shared `flock` on the session's `live.lock` for the life of the
  object. Several processes share it (a proxy and its MCP servers), the descriptor is not inherited
  by children (`claude` cannot keep a session pinned), and the kernel drops it when the process
  exits, crash included. Prune opens the lock read-write (so `flock` works on NFS too), never through
  a symlink and without blocking, refuses one that is not a regular file, takes the exclusive lock
  without waiting, and skips the session if that fails. It holds the lock while it deletes and
  removes `live.lock` last. A store opening the session at that moment (a resume with `--session ID`)
  waits on the lock, sees afterwards that its file was unlinked, recreates the session directory
  empty, logs `pruned_while_opening`, and prints ``contexto: warning: session ID was deleted by
  `contexto sessions prune` while it was being opened; it starts empty`` to stderr. A missing file
  counts as a prune only when the session entry itself is gone; any other (a session entry that is a
  symlink to a missing target, say) fails at once, and retries stop after 8 attempts with a short
  backoff. `contexto proxy`, `contexto mcp`, and `contexto run codex` report a session that cannot
  be opened as an error line (`cannot start` or `cannot open session`), not a traceback, as
  `contexto run claude` already did. A store that opens
  in the instant after `live.lock` is unlinked and before the final `rmdir` keeps its new files; prune
  then reports the session as skipped ("opened again while it was being deleted"). A process from
  before D37 holds no lock, so prune also skips any session active in the last 10 minutes (fixed, no
  override; a partly failed delete changes the directory, so a retry waits out the same 10 minutes);
  (3) path safety. The home is opened once (it may be a symlink); `sessions/` must be a real
  directory in it, and each session a real directory in `sessions/`. Every later step goes through
  those open directory descriptors, not paths: the session is opened with `O_NOFOLLOW` and must be
  the same inode (`st_dev`, `st_ino`) as the entry that was listed and selected, and its contents are
  deleted with `unlink` and `shutil.rmtree` relative to its descriptor (`rmtree`'s
  symlink-attack-safe mode; prune refuses to delete on a platform without it). Symlinks inside a
  session are removed as links and never followed. Guarantee: even if another process swaps
  `sessions/<id>` or `sessions/` for a symlink at any moment, prune deletes nothing outside the
  directory it checked and creates nothing there; if the entry no longer names that directory just
  before the final `rmdir`, prune stops with an error and leaves the entry. Not guaranteed: if
  the session directory itself is moved elsewhere after the check, its own contents are still
  removed, because they are the session that was selected; (4) errors are per session: prune
  reports each failure on stderr, goes on with the rest, and exits 1 if any failed. Skipping a
  session in use is not a failure, except when `--session` named it: then it exits 1 with
  `contexto: error: session ID: <reason>` on stderr. With `--json`, an error before any session is
  selected (an unknown or unsafe `--session`, a symlinked `sessions/`) also prints `{"error": ...}`
  on stdout, so stdout always parses; (5) `sessions list` gains a SIZE column, a
  total line, and `size_bytes` in `--json`: the allocated blocks under the directory, symlinks
  counted as links, which is the space a delete frees. Rejected: refusing only by recent activity (a
  proxy idle for an hour while the user reads is still live); a pid file (pids are reused and a
  crashed process leaves the file behind); moving the session to a trash directory before deleting
  (a failed delete would leave data `sessions list` cannot see and prune cannot reach); pruning
  single pages inside a live session (pages are cited by id from the ledger and the rendered index;
  out of scope here).

- **D38. Contexto names the CLI versions it was tested with and warns about others, without
  blocking.** Intake, 2026-10-24. Contexto recognises Claude Code's session start by its
  `# Environment` heading (D33), its context blocks by their first lines (D32), and Codex's session
  context by its section tags. A CLI update that changes that text fails silently: the message falls
  into another class and can be paged. What changed: (1) `TESTED_VERSIONS` in
  `src/contexto/cliversions.py` is the single list: Claude Code 2.1.302; Codex 0.154.0 (shapes,
  acceptance) and 0.163.0 (MCP launch, missing-tools check). A test keeps
  `SESSION_START_SIGNATURE_CLAUDE_CODE_VERSION`, `CONTEXT_BLOCK_KINDS_CLAUDE_CODE_VERSION` and
  `CONTEXT_SECTIONS_CODEX_VERSION` equal to the newest tested version of their CLI. The Codex
  constant moves from 0.154.0 to 0.163.0. The routine (5) asks for its steps 1, 2 and 4 before that
  bump: the first check, the shape probe, and the suite. All three ran on 0.163.0, at zero spend.
  The first check (`spikes/m2/cliversion/check.sh`) showed the same six section openings, with
  `<environment_context>` last in the lead. `spikes/m2/codex/probe.sh`, re-run on 0.163.0 over its
  five auth and provider scenarios, matched the 0.154.0 table in `docs/research/M2-codex-adapter.md`:
  POST-only full input, `store: false`, no `previous_response_id`, 6 POSTs on a 401 with the custom
  providers in API-key mode, 2 POSTs and 2 `GET /models` in ChatGPT mode, and 7 WebSocket upgrades
  before 6 POSTs on the built-in provider, with the same section tags in every scenario. Codex now sends more header names (`x-client-request-id` and
  `x-codex-*`); none of them is read. Step 3 (`smoke.sh preflight`, the MCP launch) also ran: no
  `mcp_tools_missing`, version 0.163.0 tested. The AGENTS.md header was not exercised; (2)
  `contexto run claude|codex` runs `<cli> --version` after the session line and, for an untested
  version, prints one stderr line naming the version, the tested versions, what may break, where to
  report it, and `contexto run --no-version-check <cli>` (the flag goes before the CLI's name, or
  it is passed to the CLI). The command runs in its own process group with stdin closed. At most
  4 KB of output is read, until a line with a version, end of file, or 3 seconds, and then the group
  is killed. So neither endless output nor a child that keeps the pipe open can hold the launch
  (reviewer A measured 6.5 s and 20 GB with a `--version` that ran `yes`, before this). It never
  blocks the launch; a CLI that does not answer is logged, not warned about. Ctrl+C during the
  check exits 130 through the launcher's normal cleanup, with no traceback and without starting the
  CLI. `CONTEXTO_NO_VERSION_CHECK=1` also skips the check. Logged as `cli_version_check`; (3) the
  proxy reads each request's `User-Agent` (measured, zero spend:
  `claude-cli/2.1.302 (external, sdk-cli)` and `codex_exec/0.163.0 (...)`; other `codex_` surfaces
  are matched by the prefix, unmeasured), logs each distinct version once (`cli_version`, at most 8)
  and the first untested one as `cli_version_untested`; (4) it checks, once, that the session start
  was recognised, on the first request that has a tool list (any Codex request) and carries the
  CLI's injected context (Claude Code: a role `system` message or a `<system-reminder>` block;
  Codex: a developer item in the lead). That is the main conversation's first request: Claude
  Code's title, topic, and quota side requests have no tool list, and subagents start from a main
  turn's tool call. The engine's own main (`publishes`) is not used. When a tool-less side request
  comes first, it stays main through the real conversation's first turn, and the check then landed
  on a resumed subagent, which has a `<system-reminder>` but no `# Environment` (reviewer A). A
  request with a tool list but no injected context leaves the check open, for up to 8 of them.
  Recognised means: for Claude Code, a role `system` message with the signature or a context
  block of kind `environment`, which every main request of the 13 reminders-spike runs on 2.1.302
  has; for Codex, a user message of context sections in the lead. Otherwise the proxy logs one
  `session_start_unrecognised`. The check reads only the first 4,096 characters of each content
  block (a role `system` message is matched whole, and returns at the first match). Runtime events go
  to the session log only, since a TUI can draw over a mid-run line; (5) `docs/cli-updates.md` is the
  routine for a new version. It says how to set up (a `.venv` at the repository root) and which steps
  each constant needs: for the two Claude Code constants, the first check, the reminders spike, and
  the suite; for the Codex section constant, the first check, `probe.sh`, and the suite. The list and
  the constants change in one commit. Options considered: refuse to launch an untested version
  (rejected: most updates do not touch the text Contexto reads, and a refusal would stop users for
  no change); warn on a different minor or major version only (rejected: Claude Code ships patch
  versions often, and the tested text was measured on one patch version); warn on
  stderr at runtime too (rejected: drawn over by the interactive CLI, and the launch line already
  says it); check every request for the session start (rejected: one scan of a large history per
  request for an answer that does not change within a process); judge the engine's main
  conversation (rejected: wrong when a tool-less side request comes first, above); compare the
  system prompt's hash (rejected: it changes with the user's tools, MCP servers, and settings, so
  every session would warn). Limitations: a CLI that keeps its version but changes its text is not
  caught by the version checks; the session-start check sees only the first judged request, so it
  misses text that changes later in a session (a new reminder kind on a later turn); a CLI that
  injected no context at all is not judged; whether Claude Code's subagent requests carry the
  environment message is not measured, and the check relies on the main conversation's first
  request coming before any subagent's.
- **D40. Each side conversation in a tool loop has its own recall room, and a tool call is charged
  against every open room.** Claude, 2026-10-24, under D0; contract change. Since v0.1.0 only the
  main conversation published a room, so a subagent's `contexto_recall` was charged against main's
  room, which was measured from main's request, not the subagent's. Each conversation the CLI runs
  (main, each subagent, each side request) is its own upstream request with its own context: the
  engine keeps state per conversation key, and a tool result lands only in the window of the
  conversation that called the tool. The overspend is therefore per request, not a race on a
  shared counter: the charge was already an atomic, locked read-modify-write. Reproduced at zero
  spend (`tests/test_subagent_room.py` on `main` at 4b87ec2): with main's room at 104,000 and a
  subagent's own room at 4,000 (input 120,000, bound 150,000), one recall put over 20,000 tokens
  into the subagent's window, and six concurrent recalls from that subagent took over 40,000 (the
  exact figure depends on how the calls interleave). What changed: (1) every conversation that is
  not main publishes a side room (`SessionStore.set_side_room(room_id, room)`) when its response's
  real usage arrives, with the same formula `bound - (input + output) - headroom_floor`, if the
  response holds a tool call the client runs, and closes it (`room` None) if it holds none. The
  proxy tells the engine which: when there is a stream hold, whether a client call started it;
  without one, the Messages `stop_reason` or the Responses `output` list; else unknown, treated as
  a call. Hosted Responses calls (`web_search_call`, `file_search_call`, `image_generation_call`,
  `code_interpreter_call`, the hosted `mcp_call`) hold the stream but run on OpenAI's servers and
  do not count; any other `*_call` type, known or not, counts as the client's, so a new type fails
  safe. There is no request-time side publish, since the hold, when it runs, makes every tool call
  wait for its response's publish; (2) a room belongs to a request lineage, not to a conversation
  key. Reviewer A showed that keying by the first message's hash lets two subagents with the same
  prompt share a room (the second's publish reset the first's 4,000 to 114,000) and lets a fork
  close a running subagent's room; in round 2 that a lineage rule alone still lets twins take each
  other's rooms, because after their first responses both rooms have the same lineage and whichever
  twin steps first would continue the other's. The request carries no conversation id Contexto can
  rely on, but each response's tool calls have ids that only that conversation's next request
  returns. So the proxy passes the client calls' ids (`tool_use` ids, Responses `call_id`s), the
  engine records them with the room, and a request continues a room only if it extends the room's
  request, adds no turn start, and returns a result for one of those ids. A twin carries only its
  own ids. A fork that copies results for the room's calls does continue it, which is harmless once
  those calls have finished: the holder has nothing in flight, and its next response opens a fresh
  room. Reviewer A's round-3 probes showed a fork made while a call still runs, closing that call
  with a placeholder result: one that then adds its own user prompt is now refused by the turn-start
  rule; one that asks inside the placeholder's message cannot be told apart from a system reminder
  appended to real results, so it can replace or close the room. That is a documented limitation;
  no Claude Code path is known to fork a running subagent that way. A room recorded without ids (no
  hold saw them) is continued by a request that extends it and adds no turn start. When two or more
  rooms still tie, the request continues none: it opens its own room and the tied ones keep their
  spend until they expire. A fork of main has main's key and is treated as main, so it uses main's
  room; (3) the MCP server cannot tell which conversation made a call, so a charge gets the smallest
  remaining room among main's and every live side room and is counted against all of them, under
  the same lock. However calls of several conversations interleave, none with a live room receives
  more than that room. The call's `mcp_call` event records which room limited it (`limited_by: main`
  or `side`), the number of live side rooms (`side_rooms`), and for a side limit that room's
  seconds since its last publish (`limiting_age_s`), so a stale room can be told from a live
  subagent; a room dropped at expiry logs `side_room_expired`, and a closed one `side_room_closed`
  with its reason; (4) a publish resets only its own room's spend, since that conversation's input
  now holds its own results but none of the others'; (5) a side room expires 15 minutes after its
  publish unless its holder publishes again. Inside a main turn, main's room resets at each main
  response but a side room does not, so a side room whose holder stopped (a subagent that failed
  after a tool call, a stopped background agent, an unknown signal, tied rooms; see (6) for the
  stops that now close early) spends down with every charge, main's included, and can hold main's
  recall at nothing; both reviewers measured this lasting the whole turn, which in an autonomous
  run can be hours. Expiry bounds it. The first version used 120 seconds on the reasoning that it covered a
  call queued behind a Bash command at Claude Code's default 2-minute timeout; both reviewers showed
  it does not (the call starts after the Bash command is killed, past expiry), and Bash's timeout
  can be raised to 10 minutes. Claude Code starts an MCP call 5 to 16 ms after its `tool_use` block
  completes, so a holder's own calls normally land within a second, but a call can wait behind
  another tool of the same response; 15 minutes covers the 10-minute Bash maximum with margin. A
  call held longer, most often by a permission prompt the user has not answered, finds its room
  expired; that limit is kept and documented. The trade-off: over the bound is the worse failure,
  and a long starvation is visible as `limited_by: side` with a large `limiting_age_s`, so the
  expiry errs long. The session store's times are wall-clock, because the proxy and the MCP server
  are separate processes; a publish time more than 15 minutes in the future (a clock stepped back)
  counts as expired, and `MemoryStore` uses a monotonic clock; (6) reviewer B measured a stale room
  holding main's recall at nothing from minute 8 to minute 15 in a simulated Esc. Round 3 closed,
  during a main turn, side rooms published before it and idle for 2 minutes. Reviewer A showed in
  round 4 that any user message that is not a tool result starts a main turn, including ones Claude
  Code injects, so a running background agent's room was closed and its queued recall went over its
  room (38,447 against 4,000). So the shapes were measured instead, at zero spend: Claude Code
  2.1.302 against a scripted stub (`spikes/m2/subagents/`). (a) A background agent's or command's
  stop reaches main as a `<task-notification>` (with the launching call's `<tool-use-id>` and a
  `<status>` of `completed`, `failed`, or `killed`), in a `system` message during main's tool loop
  and in a new `<system-reminder>` user message while main is idle; resent later, that message
  can be a plain string, which the engine counts as a turn start. (b) A background launch returns
  `Async agent launched successfully.` at once; in print mode `run_in_background: false` ran the
  subagent in the foreground and returned a `[Subagent hand-back]` report, but in interactive mode
  the same call was launched in the background, so Esc, pressed while the subagent's tool ran,
  stopped nothing: the subagent finished and main got a `completed` notification. The result of an
  interrupted foreground subagent could not be produced. (c) A subagent's first request carries the
  `Agent` call's `prompt` unchanged, after a `<system-reminder>` block. So now a side room closes
  early only on evidence in main's history that its own agent stopped, with the room's first
  prompt matching an `Agent` call. Round 5 tightened that evidence after reviewer A showed that a
  rule based on where a notice sits in main's history let the late notice of an agent's first run
  close the room of its resumed second run (35,894 against 4,000), and that a notice pasted into a
  human message counted. A resume was then measured the same way: main's `SendMessage` result
  names the `resumedAgentId`; the subagent's next request adds one user message (a plain string,
  `The coordinator sent a message while you were working: ...`), so each resume adds a turn start
  to its own conversation; and the resumed run's stop notice names the `SendMessage` call's
  tool-use id under the same task id. The rule now counts runs, independent of timing: a room's
  run is the number of turn starts in the request that published it, and it closes only when its
  agent's stops (foreground hand-backs, plus notices naming the `Agent` call or a `SendMessage`
  that resumed its agent) number at least that run. Notices count only in the measured shapes, a
  `system` message or a user message whose content or first text block opens with the
  `<system-reminder>` wrapper or `[SYSTEM NOTIFICATION - NOT USER INPUT]`, never from assistant
  text or plain user text. Only the known hand-back shape (`[Subagent hand-back]`) is a stop; the
  launch receipt is not, an error result starts no agent, and any other result shape leaves the
  call unknown, which keeps its prompt's rooms on the expiry. Every call with that prompt must be
  known and stopped that often (twins). These shapes are tied to the CLI version (D38):
  `cliversions.SUBAGENT_SHAPES_TESTED` lists 2.1.302, and when the latest Claude Code version seen
  in a request's `User-Agent` this session is any other (or none was seen), side rooms only expire
  until the spike is re-run. Round 6 made the count distinct: each run stops under its own call id
  (the `Agent` call, then one `SendMessage` per resume), so the stops are the number of those ids
  with stop evidence, and a stop reported twice (a notice resent, or a hand-back plus a notice)
  counts once. It is checked
  at each main request, whatever started the turn; no idle time and no human prompt are involved. A
  room matching no call, or an agent still running, keeps the 15-minute expiry, so reviewer A's
  probes pass. A store failure on that close logs `side_room_close_failed` and leaves the room to
  expire, without degraded mode. This covers the stops Claude Code reports (an agent finished,
  failed, or killed with `TaskStop` or the UI); it does not cover a subagent that dies without a
  notification, which keeps the expiry. The first
  version closed every side room at a new main turn, and round 3 closed idle ones at a turn start;
  both hit live background agents. Cost: concurrent conversations slow each other's recall; a
  holder that stopped without a notification holds it down for up to 15 minutes. Limitations:
  main's room is the only bound for a side conversation's calls when it has no room of its own:
  the hold was skipped (`hold_skipped`), its transform failed, it entered degraded mode (or its
  `set_side_room` failed) before its first publish, or its call starts more than 15 minutes after
  its response's publish; a degraded side conversation keeps the last room it opened until expiry;
  search's "in view" still means main's window. Options considered: a per-subagent share of main's
  room (rejected: main's room says nothing about a subagent's window, so a share can still overflow
  it); a stronger lock on the shared room (rejected: the room was already locked; the fault is what
  it was measured from); attributing each MCP call to its conversation (rejected for now: the MCP
  request carries no conversation id that Contexto can rely on, and two conversations can make
  identical calls; the tool-call ids above attribute a request to a room, not an MCP call); a
  tie-break by room age (rejected: reviewer A showed each order fails for one twin); resetting a side
  room's spend at main's publishes (rejected: the holder's own unmeasured results would be
  forgotten while it is still live); closing side rooms only when their holder closes them
  (rejected: a holder that stops never does); closing rooms idle for 2 minutes at a turn start
  (rejected: CLI-injected turns start main turns too, and background agents legitimately idle
  longer); closing only foreground agents' rooms (not chosen: in interactive mode every launch ran
  in the background, so it would close nothing, while a notification is direct evidence of a stop).

- **D39. The MCP server holds search text up to a budget, 64 MB by default: the newest pages as they
  are, older pages compressed.** Assigned 2026-10-24 from the STATUS known limitation; the contract
  change is recorded here for review, and the compressed tier was added on reviewer-2's measurement
  in the PR #215 review. D34's page cache kept every page's folded text in the MCP server for the
  life of the process, so a long session's server grew without limit (peak RSS 290 MB at 6,000
  synthetic pages, 184 MiB of it text). What changed: (1) the server holds text for at most
  `CONTEXTO_SEARCH_CACHE_MB` megabytes (1 MB = 1,048,576 bytes; default 64; `0` holds none), or
  `contexto mcp --search-cache-mb`, which overrides it. `contexto run` passes the variable to the
  server's environment for Claude Code and Codex when it is set (Codex starts MCP servers with a
  restricted environment). An invalid variable is logged `search_cache_invalid` and the default
  used: neither CLI reports an MCP server that fails to start, so refusing to start would silently
  remove every Contexto tool. An invalid flag is a usage error; (2) the budget counts the bytes of
  text held, as it is or compressed (zlib level 1). A page read during a search is held as it is;
  while the held bytes exceed the budget, the oldest text held as it is, by (turn, id), is
  compressed, and once none is left the oldest compressed text is let go. So a store whose
  compressed text fits keeps every page, the newest as they are, and a larger store keeps its
  newest pages compressed and reads the rest from their files at each search. A page that would be
  compressed and then let go first (every held text is compressed and it is older than all of
  them) is not held, so a search over a large store does not compress what it will drop, and
  neither is a page larger than the budget even compressed, which would let every other text go
  first (reviewer-1). When the
  store's text does not fit the budget as it is (estimated from the listed pages' file sizes and the
  text per file byte of the pages read so far), at most a quarter of the budget is held as it is:
  text held as it is and compressed later leaves its memory with the allocator, and at 6,000 pages
  peak RSS was 185 MB with no limit on that share, 145 MB with half, and 124 MB with a quarter,
  with the same pages left uncompressed at the end (292). The quarter costs something when the
  budget is raised above the store's compressed size but below its uncompressed size: part of the
  budget stays unused and search is slower (reviewer-2, 128 MB on this store: 80 of 128 MiB held,
  1,044 pages uncompressed instead of 3,337, 0.31 s instead of 0.25 s, for 140 MB of peak RSS
  instead of 232). Without it peak RSS reached 1.8 times the budget, so the budget would not bound
  memory. On this store zlib level 1 makes text 3.2
  times smaller; compressing a 32 KB page takes about 137 us once, decompressing it 27 us per
  search, against about 110 us to read, parse, and fold it again (reviewer-2's measurements). Not
  LRU: every search reads every page (a word matches as a substring, so no index can skip a page),
  in the same order, so with a store larger than the budget LRU would evict each page just before
  the next search needed it and save no read; (3) `rank` takes a page at a time and keeps of it only
  which words it holds, its length, and, for a match, its term counts, so a page read or
  decompressed for a search is not held for the length of the query. Results are unchanged: each
  word's page count, the average length, and the scores come from the same integers in the same
  order. (D41 later dropped the length and the average length from `rank` and counts, for a match,
  the texts holding each term; the page-at-a-time loop is unchanged.) Narrowing keeps D34's property that a query reads no page twice when no page holds every
  word; when some page does, the pages that lacked an earlier word are read again for the
  frequencies of the later words, except that a page just read is tested for every word at once
  when the query has at most 6 words (a read costs about as much as 6 failing substring tests on a
  32 KB page: 92 us against 15 us) or a match is already known. A page removed while a search runs
  is left out whatever the budget: held text would still rank it and a read would not, so when a
  listed file is gone at the end the search runs again on a new listing, up to 3 passes; if a page
  is removed during each of them, the third pass's result is returned, and it can still depend on
  the budget (reviewer-1). A page whose
  id is not a string or whose turn is not an integer is damaged: it is logged `page_unreadable` and
  left out, rather than breaking the ordering of every search (reviewer-1); (4) besides held text,
  the server keeps each page's id, turn, and whether it holds only context blocks: 695 bytes per
  page, and about 210 more for each held page (object headers and its heap item), outside the
  budget (`tracemalloc`: 4.2 MB at 6,000 pages with budget 0, 5.4 MB beyond the 67.1 MB counted at
  64 MB). It no longer keeps the page stub (2.4 KB per page), which only the index reads and the
  server never builds; a process that builds the index reads it as before. Measured on the store
  harness at 6,000 pages (`spikes/m2/scale/store/README.md`, `results/d39/`; before is main
  18b9955): `quality.csv` is byte-identical before and after at 64 MB, unlimited, and 0, and every
  stdio search reply has the same length; `tests/test_search_cache.py` compares budgets of 4 KB, an
  eighth and a third of the text, and 0 with no budget and with a reference ranking on random
  stores. Search (rare / common / multi-word) and peak RSS of the MCP child: before 0.100 / 0.174 /
  0.243 s, 290 MB; at 64 MB 0.263 / 0.334 / 0.405 s, 134 MB (every page held, 292 as they are,
  5,708 compressed); at 0 0.799 / 0.880 / 0.908 s, 47 MB; unlimited 0.100 / 0.171 / 0.246 s, 278
  MB, so the rewritten ranking costs nothing when every page is held. Without the compressed tier
  (this PR's first head, 8a96500) 64 MB gave 0.548 / 0.633 / 0.693 s at 129 MB: compression removes
  about 64 percent of the time the budget added. The first search of a process takes 1.6 s at 64 MB
  (0.8 s before), because it compresses most pages once. 64 MiB holds 2,085 of these pages as they
  are (32.1 KB of folded text each, about 48M tokens of history), and about 3.2 times that
  compressed; a 30M-token session (1,616 pages in `docs/research/M2-scale-30m.md`) is about 40 MiB
  of text at this store's 1.4 bytes per token, so it is held as it is and searches as fast as
  before. Options considered: LRU (rejected, above); every held page compressed (measured by
  reviewer-2: 0.24 / 0.29 / 0.37 s for the whole store in 57.8 MiB; the newest pages as they are
  save the decompression where the budget has room); compressed text with no cap (rejected: memory
  still grows with the store); an exact prefilter that skips pages lacking a word's trigrams or
  tokens (rejected: building it in Python took 0.7 ms per page, so 4 s more on a first search at
  6,000 pages, and a per-page vocabulary string is 18 percent of the text on the synthetic pages,
  more on real ones, held for every page, so it grows with the store); a per-term posting cache
  (rejected by reviewer-2: it helps only queries that repeat a word, and a common word still needs
  every page's counts); folded text written beside the pages for fast re-reads (rejected: a new
  store file to keep consistent with quarantine and crashes, and about 75 percent more disk); a
  larger default (not chosen: 64 MB, the size the task suggested, holds a 30M-token session as it
  is and 6,000 synthetic pages compressed). Limitations: the budget bounds the text held between
  searches, not the pages one search reads and renders to answer (up to 53, with no cap on their
  size; D41 measured 18.5 MB of page JSON for one multi-word query at 3,000 real-size pages); the
  3.2 ratio is from synthetic text and
  real text is unmeasured; a store larger than the budget's compressed capacity reads every page
  outside it at each search; the first search compresses what the budget keeps; raise the budget
  when search time matters more than memory.

- **D41. Search counts a term once per text that holds it, at most 3 per page, and drops BM25's length
  term (b 0), so neither a long turn nor a dump that repeats a key buries the turn that states its
  current value.** The follow-up D34 (3) left open, zero spend, under the owner's 2026-10-18 approval
  to fix what the 30M measurement found; it changes the ranking rule in the contract. Problem: D34
  ranked by BM25 with b 0.75 over the whole page, and a turn's length is mostly its tool output. In
  reviewer-2's 13-turn cases (turn 4 "Set max_retries to 3", the long turn 10 "max_retries is now 7"
  after 900 lines of output that never name the key, short passing mentions elsewhere) the detailed
  pages were 13, 6, 5 (mentions at 5, 6, 13), 13, 6, 4 (mentions at 6 and 13: only the old value
  shown), and 13, 12, 8 (mentions at 6, 8, 12, 13); only the case with one mention, at 13, showed 7.
  What changed: (1) BM25's b is 0, so page length does not count; (2) a term's frequency on a page is
  the number of the page's texts that hold it, at most 3 (`search.py` `TEXT_CAP` 1, `PAGE_CAP` 3), not
  its number of occurrences. A text is one stub field (the prompt's first 200 characters, each tool
  name, each path) or one text of the turn as `pages._texts` reads it: each text block, each string
  field of a tool input, each tool output. The stub repeats the prompt and the paths, so a prompt or a
  path that names a key counts as two texts: "set max_retries to 7" with an Edit of
  `/r/max_retries.py` holds the key in 6 texts (the prompt twice, the path twice, `old_string`,
  `new_string`), capped to 3. A page's search text joins its texts with the byte 0xFF, which UTF-8
  never holds (`search.TEXT_SEP`), so `rank` counts them with no offsets and no change to the cache's
  size or format (D39), and a word matches exactly where it did (a word holds no whitespace, so it
  never spanned two texts; a phrase that holds a newline no longer matches across two). k1 1.2 still
  saturates: a page with the term in 3 or more texts scores 1.57 times a page with it in one. PR #216
  first shipped (1) alone, then (1) with one count per text and no page cap; the reviews measured what
  each left, and the caps were chosen on the grid below.

  Measured, all zero spend (`spikes/m2/scale/store/README.md`, `results/d41/`). (a) The store harness
  on pages sized like real turns: `scale_bench.py --page-sizes calibration` draws each turn's token
  goal from a lognormal fitted to `calibration.json` (median 1,947; sigma 1.98 from the 90th and 95th
  percentiles, 24,649 and 50,840; capped at 566,461). Only the upper half of the pages matches real
  turns: one synthetic tool step is about 2,770 tokens, so the 37 percent of goals under 1,000 tokens
  come out at a median of 2,772, and the pages at 6,000 have a median of 5,509 (real 1,947), a mean of
  15,178 (10,431), p90 25,755 (24,649), p95 50,557 (50,840); 54 of the 6,000 have no tool call. A real
  store has more tiny pages than this one, which makes b 0.75's bias toward short pages larger, not
  smaller. The harness also counts "stale only": an older value among the detailed pages and the
  latest not. Top 3 (of 86), latest first (of 24), and stale only, D34 then D41: spike-size pages
  63/21/0 and 63/21/0 at 1,000, 60/21/0 and 60/21/0 at 3,000, 56/20/0 and 58/20/0 at 6,000; real-size
  pages 61/20/0 and 63/21/0, 57/20/0 and 60/21/0, 53/17/3 and 58/20/0; facts 29 of 30 for D41 at every
  size (D34 28 at 6,000 real-size). With the 0.2 percent topic floor at 6,000: spike-size 41/17/0 and
  44/19/0, real-size 29/15/2 and 44/19/0 (facts 17 and 24). All four 13-turn cases show 7 first (13,
  10, 6; 13, 10, 6; 13, 12, 10; 13, 10, 4), the pages the old newest-first search showed. (b) Reviewer
  A's simulation (`scale_dumps.py`, `scale_dumps_gen.sh`): sessions of 40 to 600 turns built only from
  this repository's text, where 1, 2, or 4 old tool outputs repeat a key before the turn that states
  its new value, with 0 to 3 passing mentions after it. Five sets: "loud" (500 sessions; the dump
  turn's prompt and reply also name the key: `grep -rn`, `grep -C3`, pytest tracebacks, JSON dumps),
  "quiet" (500; only its command and output do), and three built to defeat per-text counting (300
  each): many calls (5, 10, or 20 `grep -n KEY file` calls in one turn), split output (one tool result
  in blocks of 5, 20, or 50 lines), and refactor (3, 6, or 12 Edits whose strings name the key).
  Statement among the 3 detailed pages / an old dump ranked above it / stale only, D34 then D41: loud
  300/272/171 and 316/260/184, quiet 308/235/158 and 363/131/121, many calls 173/195/127 and
  197/153/103, split output 171/190/125 and 202/140/98 (D34 is 171/191/125 on reviewer A's machine:
  cells with no cap can move by a session or two with the regenerated pytest outputs), refactor
  157/205/143 and 191/151/109.

  (c) The grid (`results/d41/grid/`, `scale_grid.py`): each text counts the term up to c times (c 1,
  2, 3) and the page up to p (p 2, 3, none), each with the stub's repeats of the prompt and paths kept
  and dropped, on the 86 probes at 1,000, 3,000, and 6,000 pages on both page-size models, the 13-turn
  cases, the five dump sets, and main's two D34 scenarios ("a page about the query": turn 1's reply
  says "rate limit" in 4 lines of one text, 30 newer pages once; "the newest of equally relevant
  statements": turns 8, 30, and 45 set the timeout in one reply that says it twice, every 4th turn
  mentions it). The rule for choosing was no loss against the previous D41 (c 1, no page cap) on any
  probe row or 13-turn case, then the fewest stale only over the five dump sets, then main's two
  scenarios. Dropping the stub's repeats lost on 4 to 6 probe rows in every cell (55 or 56 of 86 at
  6,000 on both models, against 58), so they are kept (reviewer A's note on PR #216). With them kept,
  stale only summed over the five sets: c 1 with p 2, 614 (but 2 probe rows lower); c 1 with p 3, 615;
  c 1 with no cap, 633; c 2 or 3 with p 2, 655 (6 probe rows lower); c 2 or 3 with p 3, 656; c 2 with
  no cap, 680; c 3 with no cap, 684; D34 724. Every cell passed the 13-turn cases. Only c 3 with p 3
  or no cap keeps both of main's scenarios, and c 2 or 3 keeps the second; no c 1 cell keeps either. c
  1 with p 3 is chosen. Keeping the scenarios costs 41 stale only, all in the quiet set (c 3 with p 3:
  162 against 121), which is the owner's call to make; D41 does not make it.

  Options measured before the grid, all with D34's matching, groups, recency prior, and newest-page
  place: b 0.5 and 0.3 on whole pages (no 13-turn case fixed; at 6,000 real-size pages 55 and 54 in
  the top 3, stale only 2); a larger recency weight, 2 or 10 (no 13-turn case fixed: turn 10 is 42,550
  bytes against a mean page of 3,320); length taken from the texts that hold a query word (fixes the
  four cases; 51 and 48 at 6,000, stale only 3 on real sizes); BM25F over the page's texts (53 and 53
  at 6,000: a text shorter than the mean is boosted, so one mention in a short line nearly saturates);
  the same BM25F with each text at least the mean (56 and 56); whole page minus tool outputs that hold
  no query word (a scratch prototype at 1,000 pages: fails the cases); (1) alone (best on the probes,
  but stale only 195 against D34's 171 loud and 170 against 158 quiet); (1) with each page's
  occurrence count capped at 3 (reviewer A: ties on the probes, loud 316/260/184, quiet 333/210/162);
  (1) with k1 0.5 or 0.8, or 1 + ln tf (reviewer A: 55 of 86 at 6,000 on both page-size models,
  against 58). No b above 0 beat b 0 on top 3 at any size (b 0.5 tied it at 6,000 spike-size pages,
  58, with latest first 21 against 20).

  What D41 gives up, measured. (i) Repetition inside one text does not count: a reply or a file that
  discusses a key at length weighs as one passing mention, so "more about the query" now means "in
  more texts", and a newer passing mention outranks it. Main's two D34 scenarios now fail and are kept
  as tests that pin the new outcome: in the first, turn 1 is not shown (detailed 31, 30, 29; D34 31,
  30, 1); in the second, `timeout` and `client timeout` show 60, 56, 52 and no value (D34 showed 45
  first), while `HTTP_TIMEOUT_S`, which only the statements hold, still shows 45, 30, 8. (ii) An old
  turn that names the key in 2 or more texts (a prompt that names it counts twice, since the stub
  repeats the prompt) usually outranks a newer one-line reply that states its value: 2 texts score
  1.375 against the reply's 1, which wins unless the reply is about 0.75 or more of the way newer
  among the matches; 3 texts outrank it even from the oldest position against the newest match (1.57
  against 1.5). So once a passing mention follows the reply, the reply leaves the detailed pages more
  often than under D34: one-line replies shown 38 of 122 against D34's 64 (loud), 61 of 132 against 65
  (quiet), and in the three sets built against per-text counting 28 of 85 against 37 (many calls), 20
  of 74 against 27 (split output), 18 of 68 against 22 (refactor). Many separate tool calls that name
  the key are the shape that still costs most: the page cap holds them to 3, but stale only stays at
  103 of 300 (D34 127). The newest-page place (D34 (3)) stays and covers a statement only while it is
  the newest mention: in (b) the newest mention was among the detailed pages in every session under
  every variant, and a statement that was the newest mention was always shown (241 of 241 in loud and
  quiet), but a later passing mention takes that place. (iii) Memory and time per search:
  `contexto_search` reads and renders each of the up to 53 pages it shows (`get_page`, 3 in detail and
  50 listed), and that read has no cap: D39 bounds the text held between searches, not the pages one
  search reads. With no length term, a query of common words can show the long pages: at 3,000
  real-size pages (in-process, scratch checks) `failed with exit code` shows 18.5 MB of page JSON with
  a 28.1 MB allocation peak, against 0.5 and 1.5 MB under D34; one-word queries on every page (`self`,
  `return`, `import`) show 2.0 to 3.3 MB, as under D34, because the page cap ties those pages and
  recency orders them. A store whose matching pages are near the 566,461-token maximum can take
  several times these figures. With D39's 64 MB budget on, one pass each, back to back
  (`results/d41/before_*` at main cb1986f, `after_*`): spike-size pages are unchanged or faster
  (common words 0.303 to 0.222 s at 6,000; peak RSS 134 and 134 MB); on real-size pages rare and
  multi-word medians are unchanged, common words are faster (0.037 to 0.034 s at 1,000, 0.072 to 0.042
  at 3,000, 0.231 to 0.158 at 6,000), and the MCP server's peak RSS is higher, 65 to 80, 112 to 142,
  and 112 to 136 MB, in line with that multi-word query's reads. Follow-up (tracked in STATUS): a byte
  budget per search on the pages it reads, for example reading the 50 listed rows' offsets from the
  held search text rather than parsing whole pages, or listing fewer rows once the budget is spent.
  Owner's call (tracked in STATUS): the default is text cap 1; text cap 3 with page cap 3 restores
  main's two D34 scenarios at 41 more stale only, all in the quiet set (162 against 121). Tuning: the
  86 probes informed D34 and D41, and the variants and the grid were compared on them and on (b),
  which reviewer A wrote for this review; there is no held-out set. Limitations: synthetic pages and
  distractors; (b)'s sessions are built from one repository; how often real turns repeat a key is
  still not measured from real sessions; a value shown only inside a long tool output, with three or
  more newer passing mentions, can still miss the detailed pages, as before D34.

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
    repository makes no claim about terms compliance. End-user auth guidance was left to O4, which
    D35 settles: the vendor API with an API key or a CLIProxyAPI instance, logins in CLIProxyAPI or
    the vendor's own CLI, and users follow their providers' terms for the credentials they use.
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
- **O4. End-user authentication guidance.** Resolved by D35 (the vendor API with an API key, or a
  CLIProxyAPI instance; logins live in CLIProxyAPI or the vendor's own CLI, never in Contexto;
  users follow their providers' terms).
- **O5. First real release.** Settled by D29 (v0.1.0 after M2 acceptance).
- **O6. Upstream affinity on the owner's proxy.** Resolved by D36 (a proposed consistent-hash
  session affinity option for CLIProxyAPI, not deployed, and Contexto's `X-Session-Affinity`); kept
  here for its evidence. Measured: full cache misses with byte-identical
  prefixes (4 of 14 eligible throwaway calls) and thinking dropped on every request
  (`end_user_binding_mismatch`) through the owner's proxy. Lane 1's black-box test (L1-e,
  81,319 tokens): over 10 haiku requests the keep/drop pattern falls into exactly 3 consistent
  classes, and a byte-identical series shows exactly 3 cache writes before steady reads (counts
  measured; reported by manager-1, doc pending in lane 1's L1-f PR). Inferred: the route spreads
  requests across at least 3 upstream identities, and each byte-identical miss is the first request
  to reach an identity. Opus over 5 requests kept all thinking (inconclusive). At the time the fix
  was left to the owner, since intake did not touch the proxy (D11); D36's owner request covers the
  proxy-side work.
- **O7. OpenRig context-usage clock bug.** Measured by lane 1: every seat's context usage reads
  `stale_generation` because tenure boot times are stored in UTC without `Z` and parsed as local
  time (UTC-5), blinding `rig ps`, `whoami`, and compaction planning for about 5 hours after boot.
  Upstream OpenRig fix; not Contexto code. Owner decision whether to report it.

