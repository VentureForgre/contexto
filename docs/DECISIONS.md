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
  configuration on the owner's machine is not changed, and heavy spend on the team's Codex seats
  waits for the owner. Intake reads D0 as superseding the owner gates written into earlier briefs,
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
  let a failed H1 stop before the baseline spend). (Later, 2026-10-25, PR #219: step 1 ran again at
  the default 150,000 on main 9d2949a and codex-cli 0.163.0 and passed acceptance 1 to 3, 54 of 54,
  so 150,000 is no longer untested on Codex; `docs/research/M2-codex-acceptance-150k.md`.)

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
  proxy, a CLIProxyAPI option proposed upstream (router-for-me/CLIProxyAPI#6954),
  `routing.session-affinity-consistent-hash`: each session is routed by rendezvous hashing of its
  root session id and each available credential id (the root when the request names it, otherwise
  the parent), so every replica picks the same credential with no shared state; a credential in
  cooldown moves only its own sessions, each to its next-ranked credential, and they return when it
  recovers. It is deployed on the owner's proxy since 2026-10-24 (below). (2) In Contexto, the proxy adds `X-Session-Affinity: contexto-<key>`
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
  it changes a header the CLI owns and that upstream also reads). Deployed on the owner's
  CLIProxyAPI deployment on 2026-10-24, with `session-affinity` and the consistent-hash option on.
  Verified for cache reuse on the owner's proxy on 2026-10-24, measured by the deployer (no record
  of the run is in this repository): a 10-request byte-identical claude-haiku-5-5 series using one
  affinity key gave 1 cache write, then 9 reads, all served by one credential across several
  replicas (before: 3 writes). Not yet run: the `--observe-thinking` check for dropped thinking.
  (Later, 2026-10-25 UTC, PR #220: the check ran. A claude-opus-5-5 session reported 0
  `thinking_dropped` in 18 responses and by cache evidence stayed on one credential; a
  claude-haiku-5-5 control still reports drops when a replay lands on another credential, while
  Opus cross-credential replays reported none, so the signal cannot show Opus drops;
  `docs/research/O6-thinking-check.md`.)
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
  Branch rules for the private repository needed the org's plan. Ruleset 25202810: PR required,
  no force-push, no deletion, no bypass, 0 required approvals because every agent posts from one
  account. Exact-head review by a reviewer seat is a
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
    hostname, another private repository's name, statements about the owner's own accounts and
    plans, and the self-hosted runner's machine name were replaced with neutral wording that
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
    runner's machine name and notes on plans. A rewrite would need force pushes, which the ruleset
    forbids, and would still leave the pull request comments. The release source archives that held
    the hostname were removed by D62.
  - **CI on fork pull requests.** Since #213, CI and Release run on GitHub-hosted runners, and
    workflow runs on fork pull requests need approval for all external contributors.
- **D43. Contexto is MIT licensed.** Owner, 2026-10-25. The owner asked for the most permissive
  common license. MIT is short, widely known, and lets anyone use, change and ship the code with
  only the notice kept; 0BSD and the Unlicense drop even that, but are less familiar, and Apache-2.0
  adds patent and notice terms. `LICENSE` holds the MIT text, copyright VentureForgre.
  `pyproject.toml` sets `license = "MIT"` (the `License-Expression` field) and
  `license-files = ["LICENSE"]` (ships the file and the `License-File` field) in the wheel and sdist.
  The grant covers the project's own code and docs at every commit, not third-party text that old
  history quotes. The release files of v0.5.1 and earlier do not contain `LICENSE`; the first
  release built after this change does (later: v0.5.2, 2026-10-25). Contributions are accepted
  under the same license (`CONTRIBUTING.md`).
- **D44. `contexto run claude` allows Contexto's own MCP tools in its `--settings`.** Claude,
  2026-10-25, under D0; contract change. The live acceptance run on Claude Opus 5.5 (PR #221,
  `docs/research/M2-acceptance-opus.md`) found that Claude Code 2.1.302 in `-p` mode refuses every
  MCP tool call that no permission rule allows ("Claude requested permissions to use
  mcp__contexto__contexto_search, but you haven't granted it yet."). 2.1.301 let such calls through
  to its auto-mode classifier. All 23 of the run's Contexto calls were refused, and it scored 18 of
  54 with no recall until the harness allowed `mcp__contexto`. 2.1.302 is in `TESTED_VERSIONS`, so
  `contexto run claude -p` on it silently lost recall, and no acceptance 1 detector caught it.
  - **Rule.** The launcher's flag-level `--settings` adds `permissions.allow` with one rule per tool
    the MCP server serves, by exact name, under the server name the launcher's `--mcp-config` gives
    it: `mcp__contexto__contexto_recall`, `_pin`, `_close`, `_state`, `_search`. It is built from
    `contexto.mcp.TOOLS`, so a new tool is covered when it ships. No other key, rule, or mode.
  - **Why it is safe.** The five tools touch only the user's own local data. Recall, search, and
    state read the session store under `$CONTEXTO_HOME` and append `mcp_call` lines to the session
    log. `contexto_state` also reads and hashes the project files that ledger records cite, inside
    the session's working directory, to mark records whose sources changed as stale. Pin and close
    add records to the session's ledger. None runs a command, writes a project file, or reaches the
    network. Codex's config approves the whole `contexto` server
    (`default_tools_approval_mode = "approve"`); here the approval is narrower, the five exact names.
  - **Why exact names.** Measured with zero spend against a local stub on 2.1.302: a server rule
    (`mcp__contexto`), a wildcard (`mcp__contexto__*`), and exact names all let the tools run, and
    an exact name covers only that tool. A user's own MCP server that is also named `contexto` and
    serves a tool of the same name would be covered too; that is a name clash Claude Code does not
    resolve either.
  - **Merge with the user's rules (measured, 2.1.302, `-p`).** Flag-level allow rules join the
    user's: a user's `Bash(touch ...)` allow still ran next to them, and with no user rule that
    command was still refused, so nothing else is widened. Tools of other servers (`contexto_x`,
    `contexto2`) were still refused. The user's rules still win: a `deny` rule for a Contexto tool,
    or `--disallowedTools`, takes it out of the request ("No such tool available"); an `ask` rule
    outranks the allow and `-p` mode refuses the call. Organization-managed settings were not
    tested; they can limit permission rules to their own.
  - **Detection.** A refusal can still happen (an `ask` rule, managed settings, or a future Claude
    Code that ignores flag-level permissions), and the missing-tools check cannot see it, since the
    tools are offered. So the tool check also reads each Messages API request's newest user message
    and logs the first refusal of a Contexto tool as `mcp_tools_denied` (tool names only), and the
    launcher prints one warning. A result counts only when it answers a Contexto tool call of the
    assistant message before it and matches 2.1.302's refusal text for that tool, so a failed Bash
    command that prints the text does not count. A user's "No" at an interactive prompt has other
    text and does not count either. `mcp_tools_denied` fails an acceptance run, like
    `mcp_tools_missing`.
  - **Proof.** On the installed 2.1.302, `contexto run claude -p` against a local stub upstream
    that makes the model call all five tools. With the old base-URL-only settings, all five calls
    were refused and the new warning named them. With the rules, all five ran and the MCP server
    logged an `mcp_call` for each (haiku and opus model ids alike). On 2.1.301,
    search and recall ran too. In an interactive 2.1.302 session, search and recall ran with no
    input. `spikes/m2/permissions/permstub.py` repeats the `-p` check (`--before` for the old
    settings). `TESTED_VERSIONS` is unchanged: 2.1.302 works with the rules.
- **D45. One nesting limit for JSON read from outside Contexto.** PR #225,
  2026-10-25 (number confirmed once D44 merged). Reviewers of #222 found deeply nested JSON crash
  paths: a tool result too deep for `json.loads`, or too deep to walk, failed every later
  transform, so paging stopped for the session.
  PR #225 first caught `RecursionError` at each parse. Its review found the gap that leaves: from
  Python 3.14, `json.loads` parses deeper (about 116,000 levels) than `json.dumps` can write (about
  61,500 objects, 104,600 arrays), and on 3.12 there is a band of a few levels near 10,000. A tool input in
  that band parsed, then `canonical_bytes` raised on every later request; an MCP request `id` that
  deep killed the MCP server when its reply was written. Decision: every document from outside
  Contexto is read by `loads_bounded`, which measures nesting without recursion and refuses more
  than `MAX_JSON_DEPTH` = 512 levels as a `ValueError`, so each caller's existing invalid-JSON path
  handles it (contract rule 18). A request past the limit is forwarded untransformed and logged
  `request_too_deep`. 512 rather than the 2,000 first suggested: it is four times the Messages API's
  own limit (127 levels, measured), far below every Python's `dumps` limit, and below Python's
  default recursion limit of 1,000 with room for the proxy's stack, so recursive walkers
  (`redact`, the index's key listing) need no depth guards of their own. Measuring depth costs
  about a quarter of `json.loads` on real requests, and up to about as much as `json.loads` on a
  body made of tiny containers. A request past the limit fails acceptance 1 (`request_too_deep` is
  a failure event), since the Responses API's own limit is unmeasured.
- **D46. Claude Code 2.1.310 is tested; Contexto reads its tool-search shapes.** Claude, 2026-10-25,
  under D0; contract change. 2.1.310 became the installed `claude`. The D38 routine
  (`docs/cli-updates.md`, steps 1 to 6, all zero spend), the D44 permission check, and a small live
  recall check ran on it, with 2.1.302 side by side for the zero-spend steps; the numbers are in
  `docs/research/M2-claude-code-2.1.310.md`.
  - **What changed.** 2.1.310 turns tool search on by default (2.1.302 only with
    `ENABLE_TOOL_SEARCH`): `tools` holds `ToolSearch` and a `defer_loading` placeholder, and the other
    tools, Contexto's included, are listed by name ("The following deferred tools are now available via
    ToolSearch. ..."). Every request shape matched 2.1.302 with tool search on. With tool search off,
    2.1.310 matched 2.1.302 apart from one placement: on 5.x ids nested memory (a CLAUDE.md or
    CLAUDE.local.md below the project root, or a path-scoped `.claude/rules/*.md`, attached once per
    session when a file under it is first read) now arrives as one `<system-reminder>` block per file
    next to the Read results, not as a role `system` message.
  - **Three shapes Contexto misread.** (1) On 4.x ids the listing and the MCP notices are
    `<system-reminder>` blocks of user messages (also on 2.1.302 with tool search on), and the
    missing-tools check read only system text: `contexto run claude -p --model claude-sonnet-4-6` on
    2.1.310 logged `mcp_tools_missing` and printed the warning with the tools offered and working.
    (2) A subagent's environment message starts with the listing and `# Environment` follows it (also
    on 2.1.302 with tool search on), so it classified as a settings change (it carries
    `output_config`), pageable by the fallback once superseded, not as session-start: 14 of 14
    subagent requests captured. The main conversation's starts with `# Environment` on every id.
    (3) Those nested-memory blocks on 5.x ids sat in a tool-result message, so they were paged with
    their turn. Claude Code does not send them again, so the directory's rules were gone for the rest
    of the session on the default claude-opus-5-5. On 2.1.302 they were carried.
  - **Rule, missing-tools check.** Of a user message, the check reads a text block only when it is one
    whole `<system-reminder>` (it ends with the closing tag and holds no second opening tag) whose first
    line is a deferred-tool or MCP-server notice or `# MCP Server Instructions`, and never the
    message's last text block when the message holds no `tool_result`, since that is where the typed
    prompt sits (a plain-string message is all prompt), nor any text block after a
    `[Request interrupted by user` block: a prompt typed after Esc lands in the tool-result message
    after that marker (reviewer A's pty captures, 2.1.302 and 2.1.310). All 558 genuine notice blocks
    of the 2.1.310 step 2 matrix pass (328 of 328 with tool search off); a prompt that imitates a
    notice, alone, followed by text, or typed after Esc, does not. Still readable: an exact copy of a
    notice typed as an earlier block of a several-block prompt; Claude Code sends a typed prompt as one
    block. The check only logs and warns; nothing it reads changes what is carried or paged.
  - **Rule, rewrite.** The session-start signature accepts the listing paragraph before
    `# Environment`; the listing alone (an MCP server connecting mid-session) does not match.
    `CONTEXT_BLOCK_KINDS` gains `deferred_tools`, the extra block 2.1.310 puts in `messages[0]` on
    4.x ids, and `nested_instructions` (first line `Contents of <path>:` for a `CLAUDE.md`,
    `CLAUDE.local.md`, or `.claude/rules/*.md`). In a tool-result user message, a
    `nested_instructions` block is carried like a turn start's context block once its turn is paged,
    and not stored in the page, only when it has Claude Code's exact form (one whole reminder starting
    `<system-reminder>\nContents of `, as all 342 such blocks of the 2.1.310 matrix and the 366 with
    tool search off are) and comes before any `[Request interrupted by user` block; a prompt typed
    after Esc, which lands there, is paged with its turn. No other reminder next to a tool result is
    carried. The fallback protects the latest block of each path, newest first, up to 4,000 tokens
    (`NESTED_PROTECTED_TOKENS`, half the 8,000 `carried_text_budget`); an older copy of a path is
    superseded, and a latest block past the cap pages with the unrecognised items, oldest first, as
    its 2.1.302 form (a carried text message) did. So carried growth stays boundable however many
    directories are read: with one ~1,000-token CLAUDE.md per directory, growth after the fallback is
    7,769 tokens at 10 directories and 7,891 at 40 (reviewer A measured 49,549 at 40 without the cap,
    and 7,153 for the 2.1.302 form). Paged blocks stay recallable through their index line.
    (Later, 2026-10-25, from #227's round-3 review: the two fixtures differ. On reviewer A's fixture
    growth with the cap is 7,419 tokens at 10 directories and 7,421 at 40, against 12,374 and 49,549
    without it; 7,769 and 7,891 are the PR's own fixture.)
    Later deferred-tool listings name only the tools that just connected (reviewer A measured one
    server's tools per listing): they add tools, not replace them. With the kind, the fallback pages an
    older one first, as superseded, where it paged them oldest first before; that changes the paging
    order only, since every such block was pageable already, and the listing in `messages[0]` is never
    paged. On 4.x ids nested memory is part of the tool_result's own content, on 2.1.302 and 2.1.310
    alike, and stays paged with its turn; whether to carry it there too is the owner's call.
  - **Step 2 re-run with the new code.** 2.1.302: the same carried and paged figures in all 13 runs.
    2.1.310 (matrices re-run after the cap, zero spend): the same in the 7 runs without a nested
    CLAUDE.md on 5.x ids; the other 6 carry that block from its turn on (60 to 63 tokens), and in
    opus55-stream it pushed carried growth over the fallback budget, which then paged one item more.
  - **Live recall with tool search on.** Claude Code acceptance had never run with Contexto's tools
    deferred. A short `contexto run claude -p` session on 2.1.310 defaults, claude-haiku-5-5, through
    the owner's proxy: a plain-phrase fact planted past the 200-character page stub, paged out by
    filler turns at bound 60,000, then asked for. The model called `contexto_search` (a deferred
    tool), the result held the fact, the index did not, and the answer was correct: credited, at
    145,080 tokens. The first attempt, at bound 40,000, had no recall room and is not credited; both
    attempts together spent 305,223 tokens.
    The routine's full step 9 (millions of tokens) did not run.
  - **Versions.** 2.1.310 joins `TESTED_VERSIONS` and `SUBAGENT_SHAPES_TESTED`; the session-start,
    context-block, and hook measured-on constants name it. The subagent spike's 30-second wait now
    runs through perl, since 2.1.310 refuses a standalone `sleep 30` and the agent finished at once.
  - **D44 detection, amended** (PR #224's review). A refusal counts when its `tool_use_id` is a string
    naming a Contexto call of any assistant message in the request, and every user message after the
    last assistant message is read, so results split across consecutive user messages in any order
    count. A non-string id (a list) raised `TypeError`, logged as `toolcheck_error`; it now does not
    match. A request whose trailing messages hold no failed result returns before the history is
    scanned.
  - Amended by D47, which closes cases D46 missed and adds one rule. Added: a tool-result message with
    Claude Code's synthetic missing result carries nothing and is not read. Closed: a queued prompt on
    5.x ids, which the missing-tools check still read, and files nested memory imports, which were
    paged with their turn. Changed: the cap counts each block as its page.
- **D47. Nested-memory follow-ups to D46.** Claude, 2026-10-25, under D0; contract change. Reviewer
  A's round 3 findings on PR #227, checked at zero spend (loopback stub, temp config dirs, fake key)
  on 2.1.302 and 2.1.310. Every rule below was run over 13,097 captured request bodies (the 19 kept
  reminders matrices and every capture of PR #227's reviews and builders) with the code before and
  after: the only differences are the rejected imitations and the newly carried imports.
  - **Synthetic missing result.** In 2 of 24 pty runs on 2.1.302, a prompt queued during a tool run,
    then Esc and Enter, arrived as `[tool_result "[Tool result missing due to internal error]"
    (is_error), prompt]` with no interrupt marker: a pasted exact-form CLAUDE.md was carried on
    claude-opus-5-5, and a pasted listing was read as a notice on claude-sonnet-4-6. Reviewer A
    suggested carrying and reading only when the message has a `tool_result` that is not an error. That
    rule loses genuine blocks: a Read of a missing file attaches its directory's CLAUDE.md next to the
    error result (2.1.310, claude-opus-5-5, zero-spend capture). Rule: a tool-result message
    with that synthetic result carries nothing, and the missing-tools check reads none of its text
    blocks. A result counts only with `is_error` true and the marker as its whole text, as every
    captured one has: a Bash `echo` of the marker has `is_error` false and must not stop a Read's
    nested memory in the same round from being carried (reviewer A, PR #230). Only the 2 imitations
    hold that result in the 13,097 bodies. In reviewer A's 11 interrupted parallel rounds (a Read and
    a Bash) per binary, Claude Code never sent the Read's nested memory, so a genuine block next to a
    synthetic result was not seen.
  - **Queued prompts on 5.x ids.** A prompt typed during a tool run that is one whole
    `<system-reminder>` block arrives as a role `system` text "The user sent a new message while you
    were working:\n<prompt>\n\nThis is how Claude Code surfaces messages ... Address the message
    above as you continue this turn." with other reminders after it (2.1.302 and 2.1.310,
    claude-opus-5-5). The check read it whole: a queued fake listing gave offered True with no server,
    a fake disconnect a false warning (4 of 4 runs, also on main before PR #227). Any other queued
    prompt (plain text, a reminder followed by text, a one-line mention, the first of two queued
    prompts) arrives as a user text block next to the result, starting with the opening sentence; it
    is neither read nor carried (reviewer A, PR #230). Rule: a role `system` text that starts with the
    opening sentence is read from the end of the last closing sentence on; with no closing sentence,
    not at all. A text that only quotes the opening sentence later (a nested CLAUDE.md in 2.1.302's
    role `system` text) is read whole. The rewrite carries such a message as text, pageable by the
    fallback, as before.
  - **Imported memory.** A file a nested CLAUDE.md imports with `@path` arrives on 2.1.310 with 5.x ids
    as its own whole reminder, `Contents of <path>:`, after the importer in the same message, depth
    first; on 2.1.302 it was in the carried role `system` message. Captured forms: `@docs/x.md`,
    `@./x.md`, `@../x.md` and an imported file's own `@more.md` resolve against the importing file's
    directory (normalised), an absolute path is kept as written (`/tmp/...` is not resolved to
    `/private/tmp/...`); a reference in a code span, a missing file, and a reference followed by a
    comma send nothing; `@~/x.md` sent nothing in the `-p` capture, so its form is not known. Rule: such
    a block is carried when it has the exact form and its path is one an earlier carried block of the
    same message imports (`@` at a line start or after whitespace, up to whitespace; `~/` is expanded,
    a `#` fragment dropped). The fallback
    treats it as nested memory keyed by its own path, inside the same 4,000-token cap, and the
    `carried_paged` event counts it as recognised.
  - **Cap measure.** The cap counted block text while carried growth counts each block's JSON, so with
    many tiny files (one directory per Read) the protected blocks added up to 5,373 tokens of growth
    (reviewer A). Each block now counts as its page (`estimate_items` of a user message holding it), the
    measure `page_carried_text` frees by; the sum of those is at least the blocks' share of growth. With
    500 and 2,000 tiny files at budget 0 the protected blocks add 4,000 and 3,907 tokens (125 and 121
    blocks; before, 166 blocks and 5,312 and 5,360 tokens); the ~1,000-token fixture is unchanged
    (7,769 at 10 directories, 7,891 at 40).
  - **Also observed.** On 2.1.310 with claude-opus-5-5, a Bash command that reads a file (`cat
    bash/x.txt`) and a Write attach that directory's CLAUDE.md next to the result too; both are carried.
    A failed Edit attached nothing.
  - **Still readable by the missing-tools check:** an exact copy of a notice typed as an earlier block of
    a several-block prompt (D46); a queued prompt if Claude Code changes its two sentences; and a
    message under Claude Code's other mid-turn openers ("Another Claude session sent a message while
    you were working:", "A peer session sent a message while you were working:"), which were not
    measured and are not skipped. Also, not captured: on 2.1.302 a round's attachments share one
    role `system` text, so a one-reminder queued prompt placed after other text there (for example a
    nested CLAUDE.md from a Read in the same round) does not start the text and is read whole
    (reviewer B, PR #230 round 2).
- **D48. Images are priced by their pixels, and images and documents are kept out of the ratio.** Claude,
  2026-11-05, under D0; contract change (rule 3). The QA round of 2026-11-05 (verify-images, scale-engine
  finding 1) found every image and document block priced by its base64 bytes: a Read-tool screenshot
  that count_tokens measured at 641 to 3,243 tokens (claude-haiku-5-5, Claude Code 2.1.314, which scales
  Read images to a 2,000-pixel long edge) was estimated at 124,000 to 429,000 inside a `tool_result` and
  53,000 to 184,000 pasted; a 3-page PDF measured at 4,748 was estimated at 498,000. One pasted
  screenshot at the default 150,000 bound paged the whole retained window and logged `window_minimal`,
  `bound_too_small` and `turn_room_low`, so the launcher told the user to restart with a larger bound.
  The image bytes also went into the measured bytes-per-token ratio: 2.87 became 39.4 after two
  screenshots in a live run, and later text was under-estimated (a tool loop estimated at 61,000 was
  really 81,000 against a 60,000 bound). The Codex estimate had the same flaw for `input_image`.
  - **Scope (the orchestrator's decision, PR #234 round 9).** This decision prices images only. Nine
    review rounds each found a new way to count a PDF's pages low or to make the count slow, while the
    image pricing, the ratio rules and the head limits below held, so page counting moved to a separate
    branch and decision. Documents (a Claude `document` block with a base64 PDF, a URL or a file id;
    a Codex `input_file`) are estimated as on v0.5.8, by their bytes as text: 3.5 bytes a token pasted,
    1.5 inside a `tool_result`, or the measured ratio once there is one. For the ratio they are media
    with no floor (below), so they can never raise it.
  - **Images.** `width * height / 750` tokens after scaling the long edge down to 2,576 pixels, between
    64 and 4,784: Anthropic's documented formula and limits for the high-resolution tier (Opus 4.7 and
    later, Sonnet 5 and later); the 64-token minimum covers thin and tiny images, whose patch rounding
    is not modelled. The size comes from the decoded header: PNG IHDR, GIF screen descriptor, WebP VP8,
    VP8L and VP8X, and the first JPEG start-of-frame segment within the first 192 KB (after EXIF and ICC
    segments); line breaks in the base64 are dropped first. An image whose size cannot be read (a URL or
    file-id source, another format, a damaged header) gets 4,784, the largest cost an image can have.
    The formula gives 640, 1,728, 2,765 and 3,334 for the four measured sizes (measured 641, 1,719,
    2,694 and 3,243).
  - **Where media is found.** Message content, `tool_result` content, a document's content source, and
    Responses message content and tool output lists, at most 10 levels down. An image-shaped object in a
    tool's input is text the model wrote and is not priced as an image (and is never walked, so a tool
    input deeper than Python's recursion limit still estimates).
  - **The ratio.** It is learned from text only: a request's image and document bytes are left out of
    its bytes, and their cost out of its real tokens. That cost is not measured, only bounded. The
    estimate is at or over it for an image. The floor is meant to be at or under it: on Claude 95% of the
    image formula on the older 1,568-pixel, 1,568-token limit (measured costs ran up to 3% under the
    formula), 0 for an image of unknown size, and 0 for a document; on Codex 0, since OpenAI prices
    images by tiles (1,105 tokens for 1920x1080 at high detail, 85 at low). The floor can still run
    high (an API that prices an image under it), so nothing below relies on it being right.
  - **What the ratio learns from a request with media** (`text_usage`, `appended_usage`; reviewer B
    round 3, reviewer A round 4), in order:
    - **The same media as the last measured request.** When a request only appends to the last measured
      one (same epoch, window, degraded state and head fingerprint, and every message that request sent
      unchanged, compared as an anchor over rule 10's normalised hashes, so a moved `cache_control` is
      no change) and carries the same media (equal content keys), the ratio learns from their
      difference: the bytes and real tokens the request added. The media cancel exactly, so the appended
      text is measured with no floor or estimate, and real drift toward denser or sparser text is
      learned as it happens. Round 5 (reviewer A): at 2ef88a4 only that request's last message was
      compared, so an edit to an earlier message (a `window_changed` resync keeps the epoch and window)
      or a head change put its own bytes into the difference. Clearing old dense tool output while
      appending, with a screenshot in view, crossed the bound unflagged in 5 of 5 sessions (ratio
      2.64 to 2.79 against the media-free control's 2.52 to 2.68); now all 5 are flagged before sending
      (ratio 2.22 to 2.23). Tests show the chain does not form after an edit, a rotation, a head change,
      new media, or a request forwarded after a failed transform.
    - **With a measured ratio, otherwise.** The media are charged the cost that keeps the ratio, at most
      their estimate (a document's is its bytes): a request can lower the ratio (its text is denser than
      measured) but never raise it. When the usage leaves the media less than their floor, the request
      is not learned, since the floor may have run high. Upward moves come only from requests without
      media and from differences.
    - **Before a ratio is measured.** The media are charged their floor, so the first ratio errs low.
      On Claude, when the media have a floor, only if estimate minus floor is within 10% of the
      request's usage; otherwise the request is not learned and the estimator keeps its seeds until a
      difference or a request without media gives a ratio. A floor that would put the request's text
      above 3.5 bytes a token (the text seed; honest requests measured 2.9 to 3.0) ran high and is not
      taken off. On Codex the estimate is a deliberate over-estimate, so the range says nothing and the
      first ratio is learned at floor 0: gating it too kept the 3.5 seed and ran 1.9% under.
    - **Any request** whose learned text ratio lands outside 1.5 to 6 bytes a token is not learned. A
      document's whole cost counts as text before a ratio exists, so a first request with a PDF usually
      lands under 1.5 and waits for the next request's difference. Not always: a small PDF beside much
      text can land just above 1.5 and be learned (the #234 review measured ratios down to 1.585). A
      low ratio counts more tokens per byte, so that errs high, the safe side.

    Measured (synthetic upstream at 3.0 bytes a text token, PDFs priced by bytes with floor 0; the
    sessions of `tests/test_image_estimate.py`): the suite's forms of reviewer B's round-3 sessions (a 20-page PDF at 4,568
    a page read on turn 1, a 10-page one pasted in the first request) hold 3.00 after every turn, the
    pasted one learned from the second request's difference (d95e5df: 1.95 and 0.82 after the PDF).
    Reviewer A's round-4 sessions, a PDF read on turn 2 then text turns and a tool loop run until a
    request crosses the bound at 150,000 (10 pages at 300 or 1,583 a page, 1 page at 1,583, 20 pages at
    4,568; three Read sizes each): peak ratio 3.00, and 0 of 12 runs cross the bound unflagged
    (134f939: peaks 4.37 to 5.05, 14 of 60 runs unflagged, with a page floor that ran high). Drift with a 20-page PDF in view and
    tool output turning to JSON at 2.0 bytes a token, bound 400,000: the ratio ends at 2.39 (the
    media-free control 2.20, which weighs recent content more because it observes its whole history on
    every request; 2.78 at 134f939), the worst estimate against real 0.973 (control 0.955), and nothing
    crosses the bound unflagged. A Codex session opening with a 1920x1080 screenshot learns 2.70 rising
    to 2.94 (true 3.0), every estimate at or above real. The price of a floor of 0 is that an
    image-heavy first Codex request learns a low first ratio: 10 screenshots beside 45 KB of text learn
    1.74 for a true 3.0.
  - **Media in the conversation's first request** (reviewer B, rounds 4 and 5; D49 amended by #244).
    Rule 4 measures the head from the first measured request: its input minus its window, the window's
    text counting at its share of the request's text bytes applied to the input minus the request's
    media floor (or the estimate before the send when that is lower), and the window's media at their
    floor (`measured_head_tokens`). With a floor at or under the media's cost this errs high. With media
    in the window, two limits hold whatever the floor does: the head is never under the text-seed
    estimate of the request's own `system` and `tools`, since a floor can run high (B, round 5: at
    2ef88a4 a floor over the cost took the head to 0 and `W` about 38k too large, and one tool loop
    reached 153,954 on a 150,000 bound); and when all the request's media are in the window it is never
    over its own bytes at 1.5 bytes a token, the densest text the ratio accepts, since a floor far under
    the cost (a document's 0) leaves the head that much high (B, round 4: a 100-page PDF in the first
    request at 800,000 gave `W` 226,625 against the control's 517,858). Without media the measurement
    is D49's. Measured head (true head 16,715; text at 3.0 bytes a token; `W` beside an equal-cost text
    control), v0.5.8 then now:

    | first request carries | v0.5.8: head, W | now: head, W | control W |
    |---|---|---|---|
    | a 1440x900 screenshot | 1,374, 110,314 | 16,966, 108,034 | 108,026 |
    | a 10-page PDF, 4,568 a page | 9,854, 115,146 | 33,449, 91,551 | 101,747 |
    | a 10-page PDF, 1,583 a page | 8,857, 116,143 | 32,557, 92,443 | 106,012 |
    | 20 pages costing 300 | 14,624, 110,376 | 22,727, 102,273 | 107,416 |
    | a 100-page PDF, 4,568 a page, bound 800,000 | 11,546, 763,454 | 33,449, 741,551 | 693,016 |

    On v0.5.8 the base64 took most of the byte share, so the head measured low and `W` ran over its
    true size. Now a PDF's unknown cost lands in the head up to the 1.5-bytes limit, so `W` runs 5 to
    13% under the control's until the PDF's own pricing (the follow-up decision) gives it a floor. The
    100-page case's `W` is above its control's because the control's own head errs high by D49's
    design, and below the true `W` (about 758,000).
  - **Pages are unchanged.** A page keeps the image as sent, `render_page` shows `[image omitted]`, and
    search and recall skip it; only the page's `tokens` metadata is now realistic.
  - **Measured** (synthetic upstream at 3.0 bytes per text token plus `w*h/750` per image, QA-sized
    images; `tests/test_image_estimate.py`), v0.5.8 then this change:

    | case | v0.5.8 | D48 |
    |---|---|---|
    | 800x600 PNG in a `tool_result` / pasted (measured 641) | 124,088 / 53,166 | 678 / 640 |
    | 1440x900 PNG (measured 1,719) | 416,621 / 178,537 | 1,766 / 1,728 |
    | 1920x1080 JPEG (measured 2,694) | 406,955 / 174,394 | 2,803 / 2,765 |
    | 2000x1250 JPEG (measured 3,243) | 429,355 / 183,994 | 3,372 / 3,334 |
    | 3-page PDF, 1.7 MB base64 (measured 4,748) | 498,397 | 498,397 (unchanged) |
    | pasted screenshot after 6 turns, bound 150,000 | estimate 261,033; window_minimal, bound_too_small, turn_room_low; 1 of 13 messages sent | estimate 54,466 for a real 54,487; no events; 13 of 13 sent |
    | ratio after 2 Read screenshots, then after 25 text requests (true 3.0) | 36.44, then 59.15 | 3.00, then 3.00 |
    | 55 KB Read in the tool loop after them, bound 60,000 | estimate 23,877 for a real 59,618 | 59,601 for 59,618 |

  - **Not covered.** Image prices on models before the high-resolution tier are lower (at most about
    1,568 tokens), so there the estimate errs high by up to 3 times per large image; the ratio rules
    above keep the ratio safe. Codex uses the same formula; OpenAI prices images by tiles, lower than
    this, so a Codex estimate errs high: in reviewer B's Codex run two turns its equal-cost control keeps
    were paged (`window_minimal`), by design. A screenshot in a paged turn is still not recallable as an
    image (unchanged). PDFs keep v0.5.8's estimate, which errs high for a PDF over about 4 KB a page
    pasted (2 KB in a Read result) and low for a more compact one, since the API charges each page its
    text and a page image whatever the file's size: at an assumed 1,583 tokens a page (the measured
    3-page PDF), 12 of the 14 producer files the PDF branch tests with, 0.1 to 1.7 KB a page, are
    estimated at 0.02 to 0.41 of that pasted and 0.05 to 0.96 in a Read result, and the two at 4.6 KB
    a page or more at 1.2 to 4.0 times. Pricing documents by page is the follow-up decision.

- **D49. A head change is sized like any other rotation, and every forwarded request is checked
  against the bound.** Claude, 2026-11-05, under D0; contract change (rules 3 to 6). QA 2026-11-05
  (live-realworld F1, verify-headchange) found that any byte change to `system` or `tools` (tool
  search loading a tool, an MCP server connecting, a model switch, which renames the model in the
  system prompt) sent the next turn start down a branch of its own. It kept `choose_k`'s fallback,
  the newest complete turn whatever its size; it skipped rule 4's minimal window and
  `bound_too_small` and rule 3's room rotation; with `W` at or below zero it paged nothing and logged
  nothing; and the pre-send check skipped every rotated request. Live, after a plan-then-implement turn
  on bound 60,000, the turn started with recall room -4,243 against the 12,000 minimum, every
  `contexto_search` came back `turn_budget: spent`, and the answer was wrong.
  - **Rule.** A head change still starts a new epoch at the next turn start, even when nothing is
    paged (the cached prefix is lost anyway, so stripping thinking there costs nothing more). Its `k`
    is chosen by the same steps as a size rotation: the minimal window, and `bound_too_small` when even
    the new turn does not fit, when `W` cannot hold the newest complete turn; else rule 4's
    `0.5 * W` target, with rule 3's room target when it restores `min_turn_room`. With room to spare it
    pages exactly as before. The rotation keeps `reason: "head_change"`; `window_minimal` and
    `bound_too_small` are logged as for any rotation.
  - **Measured head.** Usage measures the head once, at the conversation's first request, so `F` kept
    the old head after a change. The engine now keeps the byte size of the head it measured and, at a
    head-change turn start, adds the change in bytes at the measured ratio (rounded up) to the
    measured head. In a measured engine run where a server adds about 7,600
    tokens of tools at bound 30,000, `F` went from 480 to 8,920. Not recorded in the epoch record: a
    resumed engine takes it from the request, whose head the record's fingerprint must match.
  - **Pre-send check.** Every forwarded request, rotated or not, is checked against the estimate of
    what is sent (`sent_estimate` after a rotation) and logged `over_bound` with `source: "estimate"`
    and `in_tool_loop`. Only `in_tool_loop: true`, and degraded mode, are the logged exception; at a
    turn start it goes with `bound_too_small` or means the sizing did not hold, so PR #48 review's
    concern (a turn-start overrun read as rule 5's exception) still holds through that field.
  - **Measured (engine, zero spend, `tests/test_head_change.py` scenarios, before → after).** Bound
    8,000, a 12-read loop: sent 17,158 to 17,353 → 599 to 794 estimated tokens, room -10,353 → 6,206
    or more, events `turn_room_low` → `window_minimal`. Bound 30,000 with `min_turn_room` 6,000, an
    18-read loop: room 2,349 to 2,544 → 27,191 or more. Bound 60,000 with a 20,000-token head and a
    22-read turn (the live shape): sent 51,050 to 51,406 → 20,681 to 21,037, room -16,406 → 13,963 or
    more. Each for tools added, system changed and model switched; the same runs with no head change
    are unchanged. 15 of the 23 new tests fail on main.
  - **Limits.** The pre-send check uses the estimator; real usage over the bound is still logged after
    the response, as before.
  - **Rule 4's first measurement, amended** (PR #244, from the reviews of PRs #232 and #244). The head
    was the first measured request's input minus the estimate of its non-carried messages made before
    the send. For a conversation's first request that estimate is close; when the first usage arrives
    later or the first request already carries a long history, it is made before the session's ratio is
    known, counts tool output at the seed's 1.5 bytes per token, and runs thousands of tokens high, so
    the head came out low, often 0, for the rest of the session, with or without a head change. `W`
    came out too large, `choose_k` kept the window, `turn_room_low` went unlogged, and turn starts went
    over the bound. The first usage arrives late when earlier responses carry usage Contexto cannot
    read (a 200 without usage, a stream cut before it, Esc), or after `usage_unattributed`; the first
    request carries a long history when a conversation starts with no epoch record it can resume (a
    restarted proxy, `--resume` after a CLI upgrade or a settings change). A 429 or 529 is not a
    case: the client retries the same body, and the retry is measured. D49's own Limits bullet (the
    change counted twice when the head changes before the first usage) was also real; its first
    version said the double count covers the low head, which holds only when the change is larger
    than the head lost.
    - **Rule.** The non-carried messages count at the measured request's own ratio, their share of its
      bytes (as the estimator counts them), or at the estimate made before the send when that is lower,
      so the head errs high, except as Limits says below (a window with more bytes per token than the
      head puts it low; PR #244's reviews measured up to 1,816 tokens, 7.3%, low with the bound held,
      and A12's `turn_room_low` unlogged in 21 of 240 runs). The head's byte size is re-based on the measured request, so a head change
      before the first usage is in the measurement and counted once. Both window sizes are kept only
      while the head is unmeasured.
    - **Usage with no input count** (`output_tokens` alone) is a response without usage. It was read
      as a real input of 0: the head 0 for the session, the next request estimated as 0 plus its new
      messages, and a recall room published from the output alone. Now nothing is measured or
      published, and the next request logs `usage_missing` and is estimated whole (rule 3), as after a
      200 with no usage. If no usage ever has an input count, the head is never measured and the
      estimator stands in at its seeds for the whole session (every request estimated whole, tool
      output at 1.5 bytes per token); real usage over the bound cannot be logged, so the pre-send check
      is the only bound check, and the recall room stays as last published, or unpublished.
    - **Measured, engine (zero spend, real usage at the sent bytes over a fixed ratio, before →
      after).** PR #244's repro (bound 30,000, a 20,000-token system prompt, real head 20,095, first
      usage at read 26 of a loop): head 490 → 20,694; turn starts 2 and 3 skipped paging
      (`rotation_noop`) and 4 and 5 went out at 32,047 and 32,031 real tokens, `over_bound` → every
      turn start pages to 20,657 to 20,710, as with usage from the first request. Bound 60,000, the
      same system prompt, an MCP server's 30 tools connecting at read 6 (real head 28,535), first usage
      at the last read: `F` 28,299, 22,226 and 19,189 for 12, 20 and 24 reads → 29,183 to 29,399; with
      no server (real head 20,095), 11,419 to 2,309 → 20,743 to 20,959. Bound 30,000 with the server on
      a 49-token head (real head 8,489), first usage at read 0, 1, 4, 7, 8, 12: `F` 8,920, 8,741,
      8,741, 12,048, 11,290, 8,741 → 8,921 to 9,137 (the change counted once). A first request with 18
      reads of history at bound 60,000: head 6,563 → 20,550 against 20,095, `W` 28,437 → 14,450, later
      turn starts 25,732 to 25,806 real → 20,742 to 20,816. Codex at bound 60,000, first usage after 26
      calls: at 4.5 bytes per token the head 18,896 → 22,401 against 22,238; at 3.5 unchanged (28,800
      against 28,591); at 2.5 unchanged and high (46,628 against 40,028), where the estimate before the
      send is the lower window. A33's seat replay (`spikes/m2/acceptance`, 92k head, 4 bytes per token):
      the lowest `W` 18,838 → 18,594 (with D51's pin sentence), the head no longer 244 low; rotations
      and cost unchanged.
    - **Measured, proxy** (the real `ContextoProxy` and engine over HTTP, a stub upstream counting 3.2
      bytes per token, first usage at request 1, 8 or 12, before → after). A 13,100-token head with the
      server at bound 30,000 (real 23,617): usage without an input count, head 9,266 to 9,276, turn
      starts up to 28,416 real, room -416 → head 23,838 to 24,112, turn starts at most 24,283, room
      3,717 with `turn_room_low` logged, the same as the 529 control. A 20,000-token head with the server
      at 60,000 (real 31,211): without an input count, head 9,258 → 31,426 to 31,716, room -1,064 →
      3,035 as the control; without usage, head 30,925 to 35,375 → 31,426 to 31,716. A 20,000-token head
      at 30,000 (real 21,979): without an input count, head 0 and turn starts at 32,975 real, over the
      bound → head 22,168 to 22,428, turn starts at most 22,645; without usage at request 12, head
      14,034, turn starts 26,779 → 22,428 and 22,645. A first request with 10, 18 or 40 reads of history
      and a 28,000-token head (real 27,979): head 21,555, 16,001 and 732 → 28,407, 28,590 and 29,072.
      Over the head, rule 4 counts the request's JSON around its messages and the carried text, 130 to
      1,100 tokens in these runs. On main, 38 of the 46 new engine tests and 17 of the 30 proxy tests
      fail (the 529 controls and some runs without usage pass).
    - **Limits.** Byte share assumes the head and the window tokenize alike. Tool output usually has
      fewer bytes per token than the head's prose and JSON, which puts the head high; a window with more
      bytes per token than the head would put it low by that difference, unless the estimate before the
      send is lower. As before, a conversation whose first request already rotates (`k > 0`) never
      measures its head; the estimator stands in. With the head measured right, a head too large for
      `min_turn_room` (bound 60,000, a 28,000-token head) leaves A12 nothing to restore, so later turn
      starts keep their window (about 34,200 real, room 806, `turn_room_low` logged once) where the low
      head used to page; PR #232 review B's finding 1, still open.
- **D50. Main follows Claude Code's session id across `/clear` and `/resume`, and its compaction
  summary across `/compact`; a side request that arrives first stays main only until the first
  prompt.** Claude, 2026-11-05, under D0; contract
  change. The engine keys a conversation by its first prompt and handed main over only at a turn
  start with strictly more turn starts than main (contract PR #125). QA's verify-clear and
  live-realworld F2 (v0.5.8): after `/clear` the new conversation has one turn start, so it stayed a
  side conversation until it outgrew the old one. No room, snapshot or window came from the
  conversation the user was in. Every tool call was charged against the old conversation's last
  room, which was never republished; once that was spent, `contexto_state` and `contexto_search`
  returned nothing, pins included. The live answer for a pinned codename was "I don't know ... my
  contexto recall budget for this turn is used up". The same report saw the user's first turn of a
  fresh session not be main, because Claude Code's tool-less side request arrives first and a tie
  keeps it.
  - **Measured** (zero spend; Claude Code 2.1.302, 2.1.310, 2.1.314 and 2.1.318 in a pty against a
    loopback stub that kept every request's headers and body, isolated config dirs, fake key; 15
    Messages requests per version). The steps: two turns, the first launching a subagent; `/clear`;
    the same again; `/resume` of the first conversation; one turn. On every request
    `X-Claude-Code-Session-Id` equals the `session_id` in `metadata.user_id`. `/clear` starts a new
    id; `/resume` sends the resumed conversation's old id again; the subagents and the one-message,
    tool-less side request Claude Code sends before each prompt carry the id of the conversation
    they run for. The first request of the session, and the first after `/clear`, is that side request.
    The request after `/resume` holds no marker: it is the old conversation plus the new prompt.
  - **Other sessions on one proxy** (reviewer A, PR #238; the same setup on 2.1.314 with
    `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`, the main conversation creating a team and spawning
    one teammate, then running `claude -p` from its Bash tool). A teammate's `metadata.user_id` adds
    `parent_session_id`, the lead's id. In process it keeps the lead's own session id; in its own
    tmux pane (`teammateMode` `tmux`, on an isolated tmux socket) it has its own. Its User-Agent
    entrypoint is `cli` either way. The nested `claude -p` has its own id, no parent, and the
    entrypoint `sdk-cli` (`claude-cli/<v> (external, <entrypoint>[, ...])`; the lead's is `cli`).
    With the first version of this rule the tmux teammate took main at its first turn start and the
    lead took it back at its next. The nested `claude -p` took main too, and the lead's next request
    was a side request. Under a headless lead (`claude -p` with a Bash call that runs `claude -p`,
    reviewer A round 2) the lead and the child both send `sdk-cli`: the binary keeps an inherited
    `CLAUDE_CODE_ENTRYPOINT` and only turns its own `cli` into `sdk-cli` in print mode. The child has
    its own id and no parent. With the second version of this rule the child took main, and the
    lead, whose run has one turn start, never took it back.
  - **Compaction** (reviewer B round 2, PR #238: live on 2.1.314 and 2.1.318, all 8 requests after a
    `/compact` were side, and with the old room spent `contexto_state` returned nothing). Captured
    on 2.1.314 and 2.1.318 against the stub, both manual `/compact` and auto-compaction (the stub
    reported 995,000 input tokens on a turn; 190,000 did not trigger it). The session id does not
    change. Claude Code first sends the summary request on main's own key: main's history with the
    instructions appended to its last prompt. The compacted conversation's first message is then a
    user message whose content is a plain string: "This session is being continued from a previous
    conversation that ran out of context. The summary below covers the earlier portion of the
    conversation.", a newline, then the summary. After it come a role `system` message, the last
    message from before compaction kept verbatim (main's last response, which no earlier request
    held), and the new prompt, which after `/compact` also holds the command's blocks. The same
    form came from both versions and both triggers. (Corrected by D59: those captures ran with
    Claude Code's attribution reminder off. With the default reminder the first message is a list,
    the reminder block and then the summary block, when Claude Code keeps nothing after the summary,
    and the rule below never matched that form. PR #246's review then found three forms on 2.1.314 and
    2.1.318: a plain string when Claude Code keeps tail messages after the summary (the previous
    assistant message after `/compact`, a pending tool pair after auto-compaction), which this rule
    did match; `[reminder blocks, summary, ...]`; and `[summary, ...]`.) It is a new key with two turn starts, so before
    this rule it never outgrew main. Every subagent request (8 on 2.1.302 to 2.1.318, and the
    compaction runs' 4) and the in-process teammate's carry `X-Claude-Code-Agent-Id`; no main, side,
    nested or headless request does.
  - **Rule.** At a turn start, a conversation also takes over main when its session id differs from
    main's, its entrypoint is main's, main is idle unless both are the interactive `cli` (see
    headless below), and either no conversation has been main in that session yet
    (`new_cli_session`, after `/clear`), or it was the first conversation main in its session, and
    that session lost main to another session's id and has sent no request of this conversation
    since (`resumed_cli_session`, after `/resume`). It also takes over when main has never sent a
    request with a tool list and this conversation has (`after_side_request`). Only a conversation
    seen with a tool list takes over by these, so side requests never do. A subagent carries main's
    id, so it never does either. A background subagent the old conversation launched before `/clear`
    keeps the old id, and the old conversation was that session's main, so it does not take over. A
    request with `parent_session_id` or `X-Claude-Code-Agent-Id` never takes over by any rule, so a
    subagent that auto-compacts, or that `SendMessage` resumed more often than main has turns, never
    becomes main. A conversation also takes over with reason `compacted` when its session id and
    entrypoint are main's and its first message is that summary, with the exact string
    start; a prompt that quotes it is a list with context blocks and does not match (D59: the summary
    is now found after leading `<system-reminder>` blocks, in a string or a list, and must be followed
    by a blank line, which a prompt quoting the sentence usually does not have; one that pastes the
    whole first line and the blank line, in main's own session and entrypoint, does match). It becomes its
    session's main, replacing the first one, since `/resume` of the session loads the compacted
    transcript. Its own epoch starts at 0 under the bound like any new conversation, and the old
    conversation's pins and pages stay in the session store. Requiring main's entrypoint keeps out
    a `claude -p` or SDK process run from an interactive lead's tool, since it sends `sdk-*`.
  - **Headless** (reviewer A round 3: `/clear` and `/compact` are declared `supportsNonInteractive`
    in 2.1.318; round 2 wrongly said they exist only in the interactive CLI). Captured on 2.1.314
    and 2.1.318 with `claude -p --input-format stream-json --output-format stream-json` against the
    stub: two prompts, `/compact`, a prompt, `/clear`, a prompt. `/compact` keeps the session id and
    sends the same summary form as interactive; `/clear` starts a new id, User-Agent
    `(external, sdk-cli)`, no parent, a tool list, and the same first message as an interactive
    `/clear` (the command's blocks, then the prompt). `/resume` is not declared `supportsNonInteractive`
    in the binary, so it was not run. A nested
    `claude -p` under a headless lead has the same shape: a new id under the lead's inherited
    `sdk-cli`. What tells them apart is main's state: the nested child runs inside one of main's
    tool calls (the response to main's newest request released a client call, the Bash running it), and a
    headless `/clear` follows main's final response. So for equal entrypoints other than `cli`, the
    two session-id rules also need main idle: the response to its newest request released no client
    tool call (the proxy's `tool_calls` is False, not unknown), no response of main's that may have
    released one (True or unknown, whichever request it answers) arrived after it, and main has
    sent no request since. A late or duplicate response, such as the end of a request the CLI gave
    up on and retried, must not leave main idle while a call is out: with "its latest response"
    (round 3), that late end made a headless lead idle while its Bash ran a nested `claude -p`, the
    child took main, and the lead's next request was estimated at 4,775 tokens against a 4,000 bound
    (reviewer A round 4). Updating idle from the newest request's response alone is not enough: when
    R1's late end comes first it is the attributed one, and the retry's own response, releasing the
    Bash, is then unattributed. So any response that may hold a client call marks main busy,
    whichever request it answers, and only the newest request's response without one marks it idle;
    the test runs both orders and a duplicate response. One short window remains (reviewer A, round
    5): when R1's late end comes first, main is idle until the retry's response arrives. A nested
    child cannot start in it, since it runs in a call that response releases; only a session already
    running can take main then (a `claude -p` the lead backgrounded earlier, or a call Claude Code
    started before `message_stop` on the `hold_skipped` path), and the lead's calls are then charged
    against its side room, so the bound holds. Closing it would need a count of the responses still
    outstanding per request, which would leave main busy after any response that never arrives. Compaction
    keeps the session id, which a nested child never shares, so `compacted` needs no idle check. In
    the interactive CLI the idle check is not applied, since after Esc main's latest response can
    still show a call that was cancelled and never runs. The quiet check means two sessions both sending trade main at most once: the one that lost main by
    id and kept sending returns only by turn starts, and the other one, having lost by turn starts,
    never returns by id. Two sessions that take turns, each idle while the other is prompted, pass
    the quiet check at every switch and follow the one prompted (10 switches in 10 alternations,
    reviewer A round 2), each switch logged. Within one Claude Code process (`/clear`, `/resume`,
    `/compact`) the switch is at the process's own next prompt, when its old conversation has no
    call left to run; a compaction that keeps a pending tool call after the summary switches at the
    compacted conversation's first request instead, once the summary request was answered with no
    client call (D59). Two interactive processes on one proxy are not a supported setup, and there
    the bound holds only while the session losing main is idle: a switch during its pending tool
    call charges that call against the new main's room (reviewer A round 3: 1,884 tokens against a
    500-token room). The turn-start rule on main opens the same window. Main is decided once per request, before
    the transform, and never moves at a response: at round 2 a response of a session whose request
    was in flight when another took over moved main back with no event (reviewer A). A session's main is the first conversation main in it, so a
    subagent that outgrew main does not replace it, and a later `/resume` still returns to the
    user's conversation. The turn-start rule is kept as the last case (`more_turn_starts`), so the
    only handover D50 removes is a child session's. The id is read
    from the header, else from `metadata.user_id`, only for the Messages API; Codex is unchanged.
    `metadata.user_id` is parsed by `loads_bounded` (D45): a string nested past the limit gives no
    session id; before, a deep one raised `RecursionError` and every request went upstream
    untransformed (reviewer A round 2).
    The request that takes over logs content-free `main_changed` with the reason and its turn
    starts (QA found no event saying main was stale).
  - **Result.** Replaying the captures through the engine: before, the user's first turn (3
    requests) and all 4 of the new conversation's requests after `/clear` were not main;
    after, every request of the user's conversation is main (from its first request after `/clear`
    and after `/resume`), and no subagent or side request is. Over the two team captures and the headless one no teammate
    or nested request takes main, and the lead stays main throughout. In the engine test of the live case
    (old room spent: bound 4,000, real input 3,600), `contexto_state` returned the budget-spent
    notice before and returns the pin after the new conversation's first request; `contexto_search`
    finds the old conversation's paged turn. The same holds after `/compact` (reviewer B's repro).
    Replaying the four compaction captures: before, every request of the compacted conversation was
    side (4 of 4 on each); after, it is main from its first request (`compacted`). The two
    stream-json captures: before, the compacted and the cleared conversations were side; after,
    `compacted` and `new_cli_session` at their first request, with each response's `tool_calls`
    as the stub's reply gives it. The earlier seven captures replay unchanged, the headless lead's
    nested `claude -p` staying side.
  - **Options considered.** Stop charging a stale main once another conversation has started a turn
    (QA's fallback). Rejected: a subagent's first request is a turn start inside main's tool loop,
    so main's own calls would then be bounded by the side room alone and could overfill main's
    window. A head match (same system and tools as main at a new conversation's turn start).
    Rejected: deferred tools and settings change the head inside one conversation, and the session id
    is on every request of every measured version.
  - **Not covered.** Without a session id (another client, or a Claude Code that drops both) the
    turn-start rule alone applies, as before. Editing the first prompt with `/rewind` would give a
    new key in the same session, which stays side until it outgrows the old one (inferred, not
    measured). After a
    restart whose epoch record holds another conversation, the new main still writes no record until
    it has more turn starts than the record's (rule 16, unchanged), so that conversation can resume
    only by rotating once. A subagent on a Claude Code that stopped sending `X-Claude-Code-Agent-Id`
    could again take over by the turn-start rule. In a headless session, `/clear` right after an
    SDK `interrupt()` (the response to main's newest request released a call), with a proxy that could not tell
    whether the response held a client call, or after a late end of an older request that may hold a
    call (it leaves main busy), falls back to the turn-start rule, main's behaviour before D50; a nested `claude -p`
    started in the background that sends its first request after the lead ended its turn takes
    main until the lead's next prompt, which takes it back by its id or its turn starts. If a Claude Code version sent a request of the old
    conversation after `/clear` (none of the four measured did), a later `/resume` of it would return
    only by turn starts. A second interactive Claude Code pointed at the same proxy with no parent
    id (not a supported setup) takes main at its first turn start; while both send, once.

- **D51. Standing rules are pinned when stated; Contexto's search, recall and state calls are not indexed.** Claude,
  2026-11-05, under D0; contract change. The QA run of 2026-11-05 (reports verify-obligations and
  live-fidelity) found three things.
  - **Standing rules were lost.** A "whenever X, do Y" rule placed after other content in its
    message is outside the 200-character index snippet, so once its turn paged out it was gone
    unless the model had pinned it, and the model pinned 1 of 16 such rules. The preamble's pin line
    is sent only from the first rotation (`rewrite.py`, `state.k == 0`), so in the first turns, where
    rules are usually given, the model sees only the MCP server instructions. Rule: both the
    instructions and the preamble's pin line tell the model to pin a standing rule, preference, or
    obligation the user states (whenever, from now on, always, never) as an obligation at once, even
    when its reply is one word, and to keep it open until the user withdraws it, because an unpinned
    one leaves view when its turn is paged; the instructions also give the pin arguments, since Claude
    Code defers the tool's schema. The words "keep it open until the user withdraws it" were added
    after the live run (reviewer B on PR #242: the same texts say to close an obligation when done, so
    a model could close a rule after applying it once) and were not measured live.
  - **Measured live** (claude-haiku-5-5 through the owner's proxy, Claude Code 2.1.314, bound
    60,000, one `contexto run ... claude -p --resume` per turn, Write, Edit and Bash disallowed so no
    rule could survive in Claude Code's auto-memory). Each session gave 4 rules, 2 natural and 2 terse
    ("Rule: ... Reply with just: OK"), each after a 2.5 KB log, then 2 pads of 14 KB. Before the
    trigger turns every rule turn was in the pages and none was in the retained window.
    - Shipped wording (main 4e2c4cd), rules ISO dates, Kelvin too, half-day estimates and "Bluebird":
      0 of 4 rules pinned, 0 of 4 applied, 0 of 1 fact pinned.
    - D51 wording, the same rules and prompts: 4 of 4 pinned, 4 of 4 applied, 0 of 1 fact pinned.
    - D51 wording, 4 other rules (sizes in MiB or GiB, steps labelled a) b) c), a closing ticket line,
      prices in EUR): 4 of 4 pinned, 4 of 4 applied, 0 of 2 facts pinned.
    - Shipped answers: "Thursday, November 26, 2026", 325°F (163°C) with no kelvin, "the database",
      "half a day to a day" and "two to three days". D51 answers: 2026-11-26, "about 163°C or 436 K",
      "Bluebird at 100% CPU", "1 to 2 half-days", and in the second session every answer followed all
      4 rules. Asked to list its standing rules there, the model listed 4 of 4 with no tool call.
    - In both sessions the first rule was pinned in the first turn, before any preamble, from the
      instructions alone. No fact turn ("FYI the staging cluster has 12 nodes ...") was pinned.
    - Cost: a rule turn takes a second request for the pin, a mean of 45,000 tokens (39,687 to
      52,467) against 21,426 for the shipped arm's rule turns after its first (the first turn of a
      session, 60,671, includes the session start); fact turns cost the
      same (21,421 to 22,897). The first `contexto_pin` load through ToolSearch gave one `head_changed`
      epoch reset per session (live-fidelity finding 4). The added text is sent on every request:
      the instructions grow from 426 to 695 characters and the preamble's pin line from 138 to 368
      (122 to 199 and 40 to 106 tokens by the estimator, which errs high) in the measured wording,
      and to 741 and 414 (212 and 119 tokens) with the keep-open words.
    - Health: 41 requests, all 200, largest input 33,127 of 60,000, no `over_bound`, no compaction,
      one `turn_room_low` per session (bound 60,000; live-fidelity finding 6). Spend 865,443 of the
      1,200,000 budget (shipped 233,817, D51 294,747 and 336,879).
    - Limits: one model, n=8 rules with the new wording, rules stated in their own turn. A rule the
      model does not pin is still lost once paged; the index carries only its turn's first 200
      characters.
  - **Search echo.** A turn that ran `contexto_search` holds, once paged, copies of lines from older
    pages; as the newest page holding them it ranked above its sources and was shown first, so a
    stale value could lead the reply. Rule: the inputs and outputs of Contexto's search, recall and
    state calls (`mcp__contexto__contexto_search`, `_recall`, `_state` by name, and a Codex code-mode
    `exec` call in whose code every mention of `tools` is a literal call of one of those, with no
    comment) are kept verbatim for recall but are not texts of the page for search, the stub's paths,
    the index line's identifiers, the matching lines a reply shows, or the retained-window entries.
    `contexto_pin` and `contexto_close` calls stay searchable: their inputs are the model's own words,
    not copies, and a decision superseded out of the pinned state is still found on its page
    (reviewer B on PR #242). Measured
    offline on the QA repro (a codename changed Juniper, Tamarind, Obsidian, with probe turns that
    searched for it; `tests/test_own_tool_echo.py`): with 2 probes, main's reply led with Juniper
    (an echo page's nested `> 519: > 488: > 3305:` line) and now leads with Obsidian; with 3 probes,
    main showed only the newest echo page in detail and listed the Obsidian source compactly, and now
    shows the Obsidian source in detail. The probe pages still match on their own question and
    answer, so in this repro they keep 2 of the 3 detailed places, and the Juniper and Tamarind
    sources are listed compactly. A page written before D51 that holds such a call has its stub
    rebuilt when read, in memory only, so its echoed paths no longer rank it first for a path search.
    - The `exec` rule is a text check (reviewer A on PR #242): every mention of the word `tools` must
      be a literal call of search, recall or state, and the code must hold no `//` or `/*`, in a
      string too. Code that names `tools` any other way (destructuring, an alias, `tools[n]`,
      `tools?.x`, a reference not called at once, a call of any other tool) or has a comment stays
      searchable, since hiding a shell command's output would lose it from search; recall has it
      either way. So do strings that hold `//`, `/*` or the word `tools`, which leave the echo
      searchable. What the check cannot see (round 2): code that reaches a tool without the word
      `tools`, through `eval`, `globalThis[...]` or Codex's `load()`, beside a Contexto call, so a
      shell call made that way is hidden from search. A function-call `exec` is read
      from the string fields of its JSON arguments, so escaped quotes neither hide a shell call nor
      miss a search call (reviewer B).
  - **`contexto_pin` description.** It named a kind "fact" the tool rejects, and said every record
    stays in the pinned state, which shows an event, artifact, or reference only when verified (a
    `reference` pinned with the default `epistemic: reported` was never shown). The description
    now names the accepted kinds and says that. The behaviour is unchanged.
  - The tool definitions grow from 2,827 to 2,985 bytes and the MCP instructions, part of Claude
    Code's system prompt, from 426 to 741, so the acceptance harness's head is 47,786 (47,628 before);
    its preamble base follows the fixed lines by the estimator, 380 tokens (300 before).

- **D52. Settings files as Claude Code reads them, and project settings for Codex only from a
  trusted folder.** Claude, 2026-11-05, under D0; contract change. QA run 2026-11-05 (verify-security
  B and C, security finding 2, launcher-ux finding 1). All measurements with real Claude Code 2.1.314
  and codex-cli 0.166.0 against local stub upstreams, with a scratch `CLAUDE_CONFIG_DIR` and `HOME`;
  zero spend.
  - **Which files.** In 27 layouts (`claude -p`, one stub port per settings file and one for the
    process environment), Claude Code read, highest first: the main checkout's
    `.claude/settings.local.json` (the parent of the git common dir, so the repository root also from
    a subdirectory, and the main checkout from a linked worktree, as M0 finding 1a measured; for a
    submodule, whose common dir is under the superproject's `.git/modules`, the submodule's own
    root), then the working directory's own `settings.local.json` and `settings.json`, then the user
    file. From a subdirectory it did not read the repository root's `settings.json` (nor a linked
    worktree root's files from the worktree's subdirectory), and outside git it read no parent's
    files. A committed `settings.local.json` is read like an untracked one (2 layouts). Contexto read
    the git toplevel's two files instead of the working directory's and got 8 of the 27 wrong; it now
    matches all 27 (`tests/test_upstream.py` has one test for each of the 25 that differ in which file
    wins). A project file that is also the
    user file (the working directory is the home directory) is listed once, as the user's.
  - **The hole.** `contexto run codex` took the `apiKeyHelper` and the base URL from the same files,
    project first, and wrote the helper as Codex's `auth.command`, which Codex runs without asking.
    QA's proof of concept: a repository whose `.claude/settings.json` names a helper that appends to a
    marker file and prints `sk-evil`, and a base URL on a stub; `contexto run codex exec` in it ran the
    helper twice and the stub got `Authorization: Bearer sk-evil`; with only the base URL in the
    project, the user's own helper's key went to the project's host. Plain Codex ignores `.claude/`
    (and an untrusted `.codex/config.toml`); interactive Claude Code asks "Quick safety check: Is this
    a project you created or one you trust?" (default "No, exit") before using them.
  - **Rule.** For Codex, a project file's `apiKeyHelper` and `env.ANTHROPIC_BASE_URL` count only when
    Claude Code has recorded the folder as trusted; otherwise the file is skipped, the next one wins
    (the user's, the process environment, the default), and stderr names the ignored file and the way
    on (`--codex-key-helper` or `--codex-upstream`, or run `claude` there once and accept the
    dialog; the helper message also names `CONTEXTO_CODEX_KEY_HELPER`). With no helper left, the
    launch stops with exit 2 before Codex starts and removes the session directory it created (a
    resumed `--session` is kept; reviewer B, PR #237, consistent with D54's rule for every failed
    launch). The flag and environment helpers, `--upstream`, `--codex-upstream`, and the user file
    are never gated. A trust lookup happens only when a project file sets the key.
  - **The layout comes from the file system, not git** (reviewer A, PR #237 round 1). A folder that
    does not come from `git clone` (a zip, a tarball, a release asset) writes its own `.git`, so
    `git rev-parse` answers what the folder says. Reviewer A found 8 layouts where the first version
    said trusted and Claude Code 2.1.314 showed its dialog, and the folder's `apiKeyHelper` became
    Codex's `auth.command`. With a trusted repository T: `H/.git` a file naming `T/.git` (absolute,
    and relative), H's own `.git/commondir` naming `T/.git`, a fake worktree entry inside H whose
    `commondir` names `T/.git`, and `H/.git` naming T's real worktree entry. With a trusted non-git
    parent P: `P/H/.git` a garbage file, an empty `.git` directory, and a valid repository with
    `core.worktree` set elsewhere (git fails or reports a toplevel that is not a parent, and the
    first version then counted every parent). Measured again here, with the pty and with `claude
    -p`: in all 8 Claude Code showed the dialog and read H's own `settings.json`, never T's
    `settings.local.json`; the first version read T's in the 5 with T. Rule now: the toplevel is
    the nearest directory, the working directory or a parent, with a `.git` entry of any kind, and
    nothing above it counts. The main checkout is another directory only for a genuine linked
    worktree: the `.git` file names `<M>/.git/worktrees/<name>`, that entry's `commondir` names
    `<M>/.git`, and its `gitdir` points back at this very `.git` file, which a hostile folder cannot
    write inside `<M>/.git`. Relative paths resolve against each file's directory, as git 2.48+
    writes them with `--relative-paths`. Files are read only when regular, at most 4,096 bytes, so a
    FIFO or huge `.git` cannot hang or flood the lookup. Contexto now matches Claude Code in all 8
    and in 4 controls that stay trusted (a linked worktree, its subdirectory, a relative-path
    worktree, a plain folder under a trusted parent), and still in all 27 order layouts. No `git`
    subprocess is run any more, and one walk serves both the order and the trust check.
  - **How Claude Code records trust** (read from the 2.1.314 binary, then measured in a pty: the
    dialog shown or not, 20 cases, and 12 more for the `.git` layouts above). Accepting the dialog
    in `repo/sub` writes `projects["<real path of repo>"].hasTrustDialogAccepted: true` to
    `$CLAUDE_CONFIG_DIR/.claude.json`
    (`~/.claude.json` without `CLAUDE_CONFIG_DIR`, measured with a scratch `HOME`; a non-empty legacy
    `.config.json` in the config directory is read instead, measured; `.claude-custom-oauth.json` with
    `CLAUDE_CODE_CUSTOM_OAUTH_URL`, read, not measured). A folder counts as trusted when the key is
    exactly `true` for the working directory or a parent up to the git toplevel (a key on `repo/sub`
    trusts `repo/sub/deeper`, not `repo`), or for the main checkout of a genuine linked worktree (a
    linked worktree and its subdirectories are trusted when the main checkout is); outside git, for
    the working directory or any parent. A folder above the git toplevel does not count, nor does a key recorded unresolved
    (`/tmp/...` for `/private/tmp/...`) or with a trailing slash. Not copied: Claude Code's own
    session-only trust (a home directory accepted in one session) and its sandbox flag; both only
    make Contexto ignore a file Claude Code would use, and the message says how to proceed.
  - **Why not gate `contexto run claude`.** Claude Code itself reads those files: in `-p` mode with no
    trust check (QA measured the project helper running and the key going to the project's base
    URL with plain `claude -p`), interactively after its dialog, which still appears behind
    Contexto because the launcher's `--settings` does not mark the folder trusted. The proxy's
    upstream must be the one Claude Code would use, or accepting the dialog would send requests
    somewhere else than plain `claude`. So `contexto run claude` (and `contexto proxy`) keep parity,
    exposed exactly as plain `claude -p` is; pass `--upstream` to pin it. The exception is
    `CLAUDE_CODE_IGNORE_PERSISTED_TRUST` (next bullet but one): with it set, plain `claude -p` drops a
    project file's base URL, and so does Contexto. One difference is left,
    found by reviewer B (PR #237) and measured here with the pty and a stub that logs `HEAD`: while
    the dialog is shown, Claude Code sends `HEAD /api/hello` (no credential header, no body). With
    the project setting only a base URL, a user helper in user settings, and the dialog declined,
    plain `claude` sent it to the user's own upstream and `contexto run claude` to the project's
    host, because the proxy's upstream is fixed at launch. The host learns the user's address, the
    Claude Code version and the time of the run. With `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`
    neither sent anything. Not changed here: answering `/api/hello` locally while the upstream comes
    from an untrusted project file is possible later; for now the contract documents it and
    `--upstream` pins the upstream.
  - **End to end** (real `contexto run codex exec`, codex-cli 0.166.0, QA's repository): untrusted, it
    printed both notes, exited 2, and the helper never ran; with the user's own helper and base URL
    in user settings, the helper did not run and all 10 requests went to the user's stub; after
    recording the folder as trusted, the project's helper ran and the requests went to the project's
    stub.
  - **Ignoring recorded trust (review round 2).** While `CLAUDE_CODE_IGNORE_PERSISTED_TRUST` is truthy,
    Claude Code ignores every trust record and shows its dialog in every folder; Contexto then treats
    every folder as untrusted for Codex (`1`, `true`, `yes` or `on`, any case). Before, it still read
    the records, the one case where it trusted a folder Claude Code would not.
  - **The same variable on the Claude side (review round 3).** Reviewer A found, and I measured, that
    Claude Code in `-p` mode with the variable truthy drops a project or local settings file's `env`
    (but a short safe list) and withholds its `apiKeyHelper`: "this session cannot show a trust
    dialog, so this folder's settings files may set only a short list of variables". Plain
    `claude -p` then goes to the user's upstream, while `contexto run claude -p` and `contexto
    proxy` forwarded to the project's `env.ANTHROPIC_BASE_URL` with the runner's credential, the
    case the variable exists for (a runner checking out content it does not trust). Measured with
    `claude -p` on 2.1.314 and 2.1.318 against stubs, one port per file: `settings.json`,
    `settings.local.json` and a linked worktree's main-checkout `settings.local.json`, the folder
    trusted or not, and the variable unset, `1` or U+FEFF `1`; Contexto differed in 10 of 15 per
    version and now matches all 30. A variable set by the project file's own `env` changed nothing in
    either. Interactively, with the variable set, both versions showed the dialog and, after it was
    accepted, sent the prompt to the project's base URL. Rule: while the variable is truthy, no
    project file counts for any caller; `Upstream.ignored` names the skipped files, and `contexto run
    claude` and `contexto proxy` print the same note as `contexto run codex`. Interactively that is
    stricter than Claude Code, which the note and `--upstream` cover. Real `contexto run claude -p`
    in a recorded-trusted repository went to the project's stub without the variable and to the
    user's with it. The variable is parsed as Claude Code parses it: lower case, then JavaScript's
    `trim()`, which strips U+FEFF (Python's `strip()` keeps it; the dialog appeared with U+FEFF
    `1` in reviewer A's pty runs) and keeps `\x1c` to `\x1f` and `\x85`, which Python would strip.
    With the variable set, the note says so ("is set, so no folder counts as trusted") and offers the flag or
    unsetting it, not the dialog, which would not help (reviewers A and B, round 3).
  - **Left open.** Claude Code may change where or how it records trust. The rule errs toward
    untrusted where it differs from Claude Code's (a dangling `.git` symlink; a `.git` that is a FIFO
    or another non-regular file; a bare repository's worktree): the cost is a warning and a flag.
- **D53. The proxy serves only the local CLI, and stops upstream when the CLI goes away.** Claude,
  2026-11-05, under D0; contract change (Stack, rule 8, the stream hold). From the QA run of 2026-11-05 on
  main 4e2c4cd (reports security 1, verify-security A, proxy-robustness 2, 3 and 7, verify-proxy 1,
  2, 4 and 5, suite F1). All measurements at zero spend, against loopback stubs.
  - **Host and Origin.** The proxy took any request on its port. A page served from localhost, or
    from anywhere in a browser without Chrome's Local Network Access, could scan the ephemeral
    ports (all 16,361 in 1,042 ms), POST `text/plain` with no preflight, and have the engine treat
    it as a conversation: in the QA proof of concept the store went from 25 to 79 pages, all five
    top results of `search("prod password")` were the attacker's, and the victim's next request
    logged `main: false`. The proxy also forwarded CORS preflights and relayed upstream's CORS
    headers. Rule: a request with an `Origin` header, or without exactly one `Host` naming
    `127.0.0.1:<port>` or `localhost:<port>`, gets a local 403 and `request_refused` (D59 accepts any
    port or none). Claude Code
    2.1.314 and Codex 0.166.0 send neither (QA header survey); both still answered through
    `contexto run` against a stub with no refusal. `localhost` is accepted because a user may point a
    CLI at it; IPv6 is not, since the proxy does not listen there. The body of a refused request is
    read before the 403, because closing on unread bytes sends a reset that can discard the answer.
    A per-launch secret in the base URL path would be stronger, but needs launcher and Codex config
    changes, and the header check already stops every browser path. `Sec-Fetch-*` is not checked:
    it would only add no-cors GETs, which are never transformed and whose answers a page cannot read.
  - **Client gone.** The proxy noticed a client had left only when a write failed, and while it
    waits for the response head or holds a tool call it writes nothing. Esc during a held tool
    input therefore let upstream generate, and bill, to the end. Measured with the real CLIs
    interrupted 3 s into a 40 s held stream (160 deltas 0.25 s apart) through `contexto proxy`:
    on main, upstream kept its socket 38.05 s after the interrupt and sent all 164 events, for both
    `claude -p` and `codex exec`. With this change, 0.21 s and 0.23 s and 15 events, close to the
    0.13 s and 0.16 s the QA run measured with the CLIs talking to upstream directly. Rule: each
    forwarded request has a watch thread that polls the client socket every 0.05 s (`poll` and a
    one-byte `MSG_PEEK`, so nothing is consumed) from the upstream connect to the end of the relay.
    When it sees a close or a reset, it shuts the upstream socket, so the blocked read returns at
    once, and the entry is logged `client_aborted: true`. A byte from the client (a pipelined
    request) ends the watch. A client that half-closes its sending side while it still reads now
    counts as gone; neither CLI does that, and nginx treats it the same way. A request cut off
    before the response head now gets its `request` entry, with `upstream_phase`, its transform
    events and its epoch; before, it got only `proxy_error`, and the upstream socket was left to the
    garbage collector. The response head is written inside the relay's `try`, and the watch closes
    the upstream connection on every path, so a failed head write or an internal error after
    upstream answered no longer leaks that socket.
  - **Early answer.** An upstream that answers 413 after the head and closes without reading a body
    over about 1 MB made the send fail with `BrokenPipeError`. Rule 8 then resent the whole body
    and answered a local 502, and Claude Code retried that 502. On main, with real `claude -p`, a
    2 MB prompt and such a stub, the 60 s cap stopped a run that had drawn 14 upstream 413s and
    forwarded none; the QA run had measured 11 client requests, 44 MB and 173 s ending in "API
    Error: 502". Rule: after a `BrokenPipeError` or `ConnectionResetError` in `send`, the proxy reads
    the answer upstream already sent (waiting at most 1 s, so a timeout or a silent close still
    goes to the retry) and forwards it, logging `upstream_answered_early`. With this change, in 4
    runs, Claude Code got the 413 in 0.2 to 0.3 s from one upstream request and printed its own
    "Request too large" advice. In 3 of those runs the send failed and the early path answered; in
    1 the body was written before the stub closed. Over TLS the answer is read the same way, but a
    TLS upstream was not tested.
  - **Small fixes.** The listen backlog goes from socketserver's 5 to 128: at 5, a burst of 200
    connects lost 13 to 21% to ETIMEDOUT, and at 128 none (QA measurement). A second Ctrl+C during
    `stop()`'s 2 s drain printed a `KeyboardInterrupt` traceback; it now ends the wait, and the
    requests still in flight are logged as `proxy_stop_incomplete`.
- **D54. `contexto run codex`: the key helper through a shell, no Anthropic default, no orphaned
  daemon, launch checks first.** Claude, 2026-11-05, under D0; contract change. From the 2026-11-05 QA
  run (live-codex F1, docs-audit 1 and 2, verify-codex-orphans, install 2 and 3, launcher-ux 8 and its
  nits), checked at zero spend on codex-cli 0.166.0 with a loopback Responses stub, a fake HOME, a
  fresh CONTEXTO_HOME and a synthetic key.
  - **Key helper.** Contexto split the helper into words (`shlex`) and gave Codex `command` plus `args`.
    Codex runs `auth.command` itself, with no shell (codex-rs `ModelProviderAuthInfo`, 0.166.0); Claude
    Code runs `apiKeyHelper`, which Contexto reuses, through one. So `echo $SYNTH_KEY | tr -d x` reached
    the stub as `Bearer $SYNTH_KEY | tr -d x`, and `cat ~/key.txt` failed. Rule: `command = "/bin/sh"`,
    `args = ["-c", <helper>]`. With the fix the same helper sent `Bearer key`. The helper string is one
    TOML string (JSON escapes), so quotes, backslashes, newlines and non-ASCII text round-trip exactly.
  - **No Anthropic default for Codex.** In API-key mode with no `--codex-upstream` and no
    `CONTEXTO_CODEX_UPSTREAM`, the upstream was Claude Code's plus `/v1`. With no `ANTHROPIC_BASE_URL`
    that was `https://api.anthropic.com/v1`: Codex's `/responses` would 404 there, after the user's
    OpenAI key had gone to the wrong vendor. Rule: when the derived upstream's host is `anthropic.com` or
    a subdomain (after IDNA normalization), refuse with exit 2 and name
    `--codex-upstream https://api.openai.com/v1` and the CLIProxyAPI form. A derived CLIProxyAPI root
    works as before. An explicit `--codex-upstream` or `CONTEXTO_CODEX_UPSTREAM` is never second-guessed.
    When D52 skipped an untrusted project's `env.ANTHROPIC_BASE_URL`, Claude Code's upstream falls back
    to Anthropic's and this refusal follows; D52's "ignored ... from .claude/settings.json" note is
    printed first, so the user sees why (and the launch stops there, before the key helper is looked up).
  - **`--no-daemon`.** Codex 0.166.0's TUI starts a shared background app-server pair for each
    CODEX_HOME (`codex app-server --managed-daemon` and `codex app-server daemon pid-update-loop`) and
    leaves it running. Each session has its own CODEX_HOME, so each interactive session left two
    processes and about 517 MB (QA). The updater later downloaded 0.167.0 into the home (936 MB).
    Reproduced here with main's code: 2 processes with PPID 1 and a 505 MB home after one stub turn.
    Rule: interactive launches (no subcommand, `resume`, `fork`) pass `--no-daemon` as the first
    argument, unless the user passed `--no-daemon` or `--remote`. The same holds in ChatGPT mode, where
    CODEX_HOME is the user's own and a server may already run for it. The flag goes first because 0.166.0
    accepts it before every subcommand and rejects it after `exec`. `exec` and the other
    non-interactive subcommands start no server (measured: no process, no `packages/`) and get no flag.
    `codex agents` is refused instead (exit 2, before the session exists) unless `--remote` sends it
    elsewhere (reviewer B, PR #236). Measured on 0.166.0 under Contexto: it installed and started the
    server pair in the session's CODEX_HOME and left both running (PPID 1); Codex refuses `--no-daemon`
    with it ("requires a shared server"); `codex app-server daemon stop` stopped the server but not its
    `pid-update-loop`; and the server's work would outlive the proxy it is configured to use. Other
    subcommands that manage the server (`app-server`, `remote-control`) were not measured and are not
    refused. The subcommand is the first word after the
    leading options, with Codex's value-taking options skipped (`-i`/`--image` takes every word up
    to the next option, so `codex -i a.png exec` opens the TUI; measured on 0.166.0). The flag is gated
    on the installed version, read with `codex --version` only for launches that could take it. It is
    passed from 0.156.0 on: the flag is in `codex-rs/tui/src/cli.rs` at rust-v0.156.0 and not at
    rust-v0.155.0. When the version cannot be read, `codex --help` decides (bounded, 3 s): the flag is
    passed if it is listed, else one warning line says Codex may leave a background app-server
    running; the `launch` event records `codex_version_error` (reviewer A, PR #236). Only 0.166.0 was
    run; 0.163.0 is not installed. With the fix, a turn and a `resume --last` turn on the same session
    (9 input items: the conversation resumed) left 0 processes.
  - **Cleanup.** A kept CODEX_HOME is kept for its rollouts. On exit Contexto now also removes
    `packages/` (the daemon's copy of Codex's release, 417 MB; a `--no-daemon` launch makes none) as
    well as `config.toml`. The plugin marketplace clone in `.tmp/` stays: removing it made every resumed
    launch clone about 96 MB from github.com again (reviewer A, PR #236), and plugins are left to the
    user. Measured with the fix: 98 MB after a first session (93 MB of it `.tmp/`, no `packages/`), still
    98 MB after a `resume --last` on it, with the same `.tmp/plugins` inode (no second clone). A daemon a
    user starts some other way is not stopped.
  - **TOML.** A helper is written as a JSON-escaped TOML string, but JSON leaves DEL (U+007F) raw and
    TOML forbids it, so Codex could not load the config (pre-existing, reviewer A); it is now written as
    `\u007f`. A lone surrogate, which TOML cannot hold, is refused with exit 2.
  - **Launch checks and messages.** With no `codex` on PATH, `contexto run codex` blamed a missing key
    helper (exit 2), or, given a helper, opened a session and started the proxy before failing (exit
    127). Every failed launch left an empty session that became the "most recent" one. Rule: options,
    `shutil.which("codex")` and the upstream are checked before the session and the proxy. A `codex` that
    is missing, not executable, a directory or a broken link exits 127 with D55's `cli_not_found`
    message, as `run claude` does. `codex --version` is then read on every launch, also before the
    session: an executable whose `#!` interpreter is missing passes `which` but cannot start, and it
    exits 127 (`cannot be run (FileNotFoundError)`) instead of leaving a session behind (reviewer A,
    PR #236 round 2). The same read gives the version `--no-daemon` is gated on. A failure after that and before Codex starts (no key helper, the proxy,
    Ctrl+C during the version check) removes the session directory if this launch created it; a resumed
    `--session` is never removed. The key helper is still resolved after the proxy starts, because the
    config it goes into names the proxy's URL. The Codex session line now reads `session <id> (bound N,
    upstream <source>, codex auth <mode>, engine <name>)`. The log gets `launch` (with `cli: codex`,
    `codex_auth`, `no_daemon`) and `exit` events as Claude's does. The start-time window warning now
    runs for Codex too, with an assumed fixed overhead of 19,000 tokens: measured 13,322 to 18,403 in
    the M2 Codex runs and 15,694 on 0.166.0, rounded up from the largest. At bound 40,000 and the
    default floor it now warns before Codex starts, not after the first request.
- **D55. `contexto run claude` resumes through Contexto, survives signals, and keeps out of the TUI.**
  Claude, 2026-11-05, under D0; contract change. From the QA run of 2026-11-05 (reports launcher-ux 2,
  4 to 8; live-claude 1; live-realworld F3 to F5; live-fidelity 6; security 3, 4 and 7). Measured at zero
  spend with Claude Code 2.1.314 against a loopback stub (fresh `CLAUDE_CONFIG_DIR`, fake key).
  - **Resume.** Three testers resumed a conversation (`--resume <id>`, `--continue`, or Claude Code's
    exit hint `claude --resume <id>`) and got a new Contexto session: the whole history was paged again
    into an empty store (session ac6b456b duplicated 73a20a20's 6 pages), and pins and closed
    obligations were out of reach. Measured: Claude Code's `X-Claude-Code-Session-Id` header, the
    `session_id` in `metadata.user_id` (now a JSON object), the transcript's file name and the `-p`
    result's `session_id` are one id; it survives `--resume` and `--continue` and is new with
    `--fork-session`. So the launcher wraps the engine and records each id the proxy sees in the
    session's metadata (`cli_sessions`), and without `--session` it reuses the session that served
    `--resume <id>`, or for `--continue` the one that last served Claude Code in this directory. Real
    2.1.314 through the changed launcher: the resumed and continued launches reopened the first
    session and the engine logged `epoch_resumed` (before: `epoch_not_resumed`, `no_record`). After
    exit the launcher prints the `contexto run --session S ... claude --resume <id>` command.
    Rejected: reading Claude Code's transcript directory to learn which conversation `--continue` will
    pick (it is Claude Code's private layout); instead a `--continue` that lands in a conversation the
    session never served is reported at exit. `--resume` with no id opens Claude Code's picker, which
    Contexto cannot follow, so it gets a new session and a line saying so. Codex is not covered
    (contexto.codex, D54's area); `start_proxy(cli=...)` and `record_cli_session` are shaped for it.
  - **Signals.** The launcher handled only SIGINT, SIGTERM and SIGHUP, and only while claude ran. On
    SIGQUIT (Ctrl+\\), SIGUSR1/2 or SIGALRM it died, and claude kept running against a dead proxy with
    the temporary files left; SIGTERM or SIGHUP during the version check left the files and no `exit`
    event. Now SIGQUIT is forwarded like SIGTERM, the other three end claude with SIGTERM (Claude
    Code's runtime may give SIGUSR1 a meaning), and outside claude's run all six unwind through the
    cleanup (exit 128 + N). For SIGKILL, macOS has no parent-death signal, and the CLI's code is not
    ours: a watchdog process in its own session holds a pipe from the launcher and, on end of file
    without `done`, sends claude SIGTERM and removes the temporary directory. Measured with real
    2.1.314 waiting on a slow stub: after `kill -9` of the launcher, claude was gone within 2 seconds
    and `$TMPDIR` was empty; `kill -QUIT` exited 131 with the `exit` event logged. Left by SIGKILL: no
    `exit` event; if the watchdog is killed too, the temporary directory stays (no secrets). Rejected:
    sweeping stale `contexto-run-*` directories at the next launch (it leaves claude running) and
    forwarding SIGUSR1 as is.
  - **Notices while claude runs.** The "measured window budget" warning was written while Claude
    Code's TUI owned the terminal: it landed in the input box and garbled the next redraw. The
    launcher's notices are now logged at once and printed after claude exits, in order; the start-up
    warnings still print before it starts. Codex keeps printing mid-run (its launcher does not hold).
  - **Start-up warning.** The assumed fixed overhead was 55,000; measured on 2.1.314 with tool search
    on it was 18,641 to 27,925 (live-fidelity, live-realworld), so bound 60,000 was reported as about
    -20,000 tokens of budget when the real figure was 7,000 to 14,000, and "rotations will page often"
    undersold a session whose turns ran out of recall room. The default is now 28,000 (the top of the
    measured range; OpenRig's 92,000 is unchanged), and the warning says recall will barely fit, or
    at a budget of 0 or less that it will not fit.
  - **Messages and checks.** Contexto's own options right after `claude` (Claude Code 2.1.314 said
    "unknown option '--session' (Did you mean --version?)") are refused with the command rewritten;
    `--upstream 'http://[::1'` gave a traceback and an empty session, and is now a usage error naming
    the URL's source; a `claude` that is not executable, a broken symlink or a directory is named as
    such instead of "not found on PATH". `$CONTEXTO_HOME` was forced to 0700 even when it existed (a
    regular file was chmodded before the launch failed): an existing home now keeps its mode (sessions/
    is still narrowed), and a non-directory is refused before any change. Claude Code 2.1.314 expands
    `${VAR}` and `${VAR:-x}` (not `$VAR`) in an MCP config's command, args and env, and `$${X}` gives
    `$` plus the value, so there is no escape: `${` in the home, the session id or the interpreter path
    is refused. The MCP server's `--session=<id>` is one argument (an id starting with `-` broke it).
  - **Review round 1 (reviewer A, PR #240).** Automatic reuse put two live launches on one store
    (`-c` in two terminals, or `--resume <id>` while `<id>` was open elsewhere), which the contract
    leaves out of scope and which charges both conversations to one recall room: a session picked
    automatically is now probed with a non-blocking exclusive lock on its `live.lock`, as prune does,
    and when another process holds it the launch opens a new session and says so; `--session` still
    shares on purpose. A resume through the picker, a search term or a busy session splits a
    conversation across two stores: the resume line names every other session that served the id, and
    the exit names the earlier one. `-c --fork-session` no longer gets the "another conversation"
    warning. The watchdog is told `exited` once claude is reaped, so a SIGKILL during the up to 4
    seconds of cleanup cannot signal a reused pid, and `run_child` installs its handlers before the
    spawn (a signal during it is sent once the child exists). The value of a Claude Code option that
    takes one (from 2.1.314's `--help`) is not read as `-c` or `--resume`, and the exit's metadata
    write cannot raise out of the cleanup. Left: two launches that probe the same free session within
    the same few milliseconds can still both take it (closed by the round-2 follow-ups below).
  - **Round-2 follow-ups (reviewers A and B, PR #240).** Two `contexto run claude -c` started at the
    same moment still shared one store in 6 of 10 tries: both probed before either held the session.
    The automatic choice now runs under an exclusive `flock` on `$CONTEXTO_HOME/choose.lock` until the
    chosen session's `live.lock` is held (bounded at 5 seconds); in the new test, four racers released
    together shared in 3 of 4 without the lock and never with it. Claude Code 2.1.314 and 2.1.318 parse
    options with the same Commander code (read from both binaries, with flag-and-value combining on):
    in a short cluster a flag is consumed and the rest read again, and an option that takes a value
    (`-d`, `-n`, `-r`, `-w`) takes the rest of the argument. `claude -prabc123` and `claude -rc` failed
    on 2.1.318 on the resume values "abc123" and "c". So `-pc` and `-cp` continue and `-r<id>` resumes,
    and Contexto now reads them so; it also skips the values of hidden prompt-like options
    (`--prefill`, `--append-subagent-system-prompt`, `--plan-mode-instructions`, and others found in
    both binaries). Fork: reviewer B measured that `--resume C --fork-session` reopens C's session and
    that switching back to C then logs `history_diverged` and re-rotates, with search able to return
    the other branch's turns. Decided: a fork keeps the original's session, because its history up to
    the fork point and its pins are there and a new store would page that history again without the
    pins; the launcher now says so at launch and names `--session NEW` for a separate store. Rejected:
    a new session per fork (loses the pins, the problem D55 set out to fix). The start-up warning at a
    budget of 0 or less now says "may": reviewer B measured budgets of 7,411 to 9,311 at bound 30,000
    (headroom 5,000) where the 28,000 assumption gave -3,000. Also: a signal received during a spawn
    that fails exits 128 + N instead of 127; the watchdog forgets claude's pid before the handlers are
    restored, and the temporary directory is removed before the watchdog's `done`.

- **D56. Claude Code 2.1.314 and 2.1.318 and codex-cli 0.166.0 are tested; Contexto reads the new
  subagent stop reports.** Claude, 2026-11-05, under D0; contract change. Every new user got the
  untested-version warning: the QA run of 2026-11-05 found 2.1.314 installed, 2.1.318 installed by
  Claude Code's auto-updater, which deleted 2.1.310, and Codex at 0.166.0. The D38 routine
  (`docs/cli-updates.md`, Claude steps 1 to 7, Codex steps 1 to 4) ran on each with zero spend,
  binaries run by path and `DISABLE_AUTOUPDATER=1`; the numbers are in
  `docs/research/M2-claude-code-2.1.318.md`. 2.1.310 could not be run side by side.
  - **What did not change.** Session start, every context block's kind, the hook prefixes, the
    missing-tools verdicts (True, False, True in 6 of 6 sets), the D44 refusal (5 / 0, 0 / 5, 4 / 1)
    and argstub's 8 of 8 are the same on both versions. In the four reminders matrices (52 runs),
    Contexto's code never missed a session start and found no context block without a kind (705 bodies
    with tools per matrix with tool search on, 782 with it off). Codex 0.166.0 sends the 0.163.0
    section tags and request shapes; only its built-in provider, which Contexto does not use, adds a
    role-less `configuration_update` item. The CLI changes that do not change a classification:
    claude-opus-5-5 gets no `<total_tokens>` countdown from 2.1.314 on, so an effort change on a turn
    with no other system message comes as a settings-only message (3 to 12 per opus-5-5 run; none on
    2.1.302), read as settings (contract rule 2a). And 2.1.318's `--resume` re-sends 4 old
    file-changed notes where 2.1.314 sent 80.
  - **What changed: subagent stop reports.** Since 2.1.312 a subagent run that used tools is reported
    with their names first, in `<tools-used-before-summary>Bash</tools-used-before-summary>`. It comes
    in three places. (1) It is indented above a foreground hand-back's `[Subagent hand-back]` frame,
    so the result no longer started with the frame and the call was an unknown shape. (2) While main
    is idle, it is a `<system-reminder>` text block of its own ahead of the notice's block, and only a
    user message's first text block was read, so the stop was missed. (3) It opens the `system` notice
    during main's tool loop, which was still read, since a system message is searched whole. Captured
    on 2.1.314 and 2.1.318 (the same apart from agent ids), on the fg, stop, resume, bgidle and esc
    scenarios, `agent_runs` gave 1 stop for none of fg, stop, bgidle and esc; resume gave 3.
    Unchanged: the subagent's first prompt, the launch receipt, `resumedAgentId`, and the resumed
    run's `SendMessage`-named notice.
  - **Rule.** The element and the whitespace around it are skipped at the start of an `Agent` call's
    result. A user message whose first text block is a `<system-reminder>` holding only the element
    is read at its second text block, and only when that block opens with `<system-reminder>`. A
    notice block that is not wrapped after it (`[SYSTEM NOTIFICATION ...` alone, which counts as a
    first block) does not count there, and other text before a hand-back's frame still leaves the
    call unknown. The 2.1.302 shapes are read as before. With the rule, the captures give 1 stop for
    fg, stop, bgidle and esc and 3 for resume on both versions. The engine tests run on those
    captures, scrubbed (`tests/fixtures/subagent_shapes_2_1_31x.json`).
  - **Lists.** `TESTED_VERSIONS` gains 2.1.314, 2.1.318 and 0.166.0, and `SUBAGENT_SHAPES_TESTED`
    gains 2.1.314 and 2.1.318. The session-start, context-block and hook measured-on constants become
    2.1.318, and the Codex section constant 0.166.0. 2.1.312 was only partly measured (by QA) and
    stays out.
  - **The spikes.** `carried/live_probe.sh` and `carried/probe.sh` drove `~/.local/bin/claude` with
    the real `HOME`, and `codex/probe.sh` and `codex/smoke.sh` ran Codex with the real `HOME`. Claude
    Code's auto-updater was left on in the carried scripts and `cliversion/check.sh`. Now every one
    takes a binary (an argument, or `CLAUDE=` / `CODEX=`) and defaults to the one on `PATH`, uses a
    temp `HOME`, and sets `DISABLE_AUTOUPDATER=1` for Claude Code. The reminders harness reads
    `CLAUDE=`, and `run_all.sh` passes extra arguments to every run, so the tool-search-off matrix
    needs no script edit. `subagents/run.sh` printed `rc=0` with an empty capture when its stub could
    not bind (the port is drawn from 200, and another run held it). `run.sh` and `drive.py` now take a
    free port, wait for the stub's `listening` line, fail when it does not come, and run Claude Code
    under a scrubbed environment and a temp `HOME`. `docs/cli-updates.md` says every run must set
    `DISABLE_AUTOUPDATER=1`.
  - **Not done.** No live check ran on 2.1.314, 2.1.318 or 0.166.0 (routine step 9 needs the owner's
    approval and a token cap). (Later: 2.1.318 passed the full live acceptance, run #4.) The early close was measured against the scripted stub only. In esc a
    side request asks for a background agent's progress ("Describe your most recent action in 3-5
    words ..."). Its first prompt matches no `Agent` call, so it opens no room that could close early.
- **D57. User-facing command fixes from the 2026-11-05 QA run.** Claude, 2026-11-05, under D0; contract
  change. The install, docs-audit, live-claude, store-mcp and launcher-ux reports, reproduced at zero
  spend on v0.5.8 with stub CLIs and synthetic stores; 42 new or extended tests fail on v0.5.8 and pass
  after.
  - **Finding a page id.** No command printed one: `sessions show` gave counts only, and `state` gave the
    model's instructions ending "The page index below lists the paged turns" with no index below. Now
    `sessions show` lists the pages, newest first (the 20 newest; all with `--all`; `page_list` in
    `--json`), and `contexto search WORDS` runs the store search the MCP tool uses and prints each hit's
    index line and matching lines with their offsets. `recall` takes the page id and offset both print,
    and refuses a malformed id before building a path: a 300-character `pg_` id was read as a file name,
    failed with ENAMETOOLONG, and was logged `page_unreadable`. The search prints its own lines rather
    than the tool's reply, which is shaped by the turn's recall room and phrased for the model.
  - **A readable `state`.** The default view is the pinned records, or "No pinned records.", then how
    many turns are paged out. On a session with no pages, the model text opened with "Earlier turns of
    this conversation are paged out of your context" (live-claude finding 3). `--raw` keeps the model text
    for diagnosis. The MCP tool's text is unchanged here.
  - **4xx counted once.** The proxy logs each upstream 4xx as its `request` entry and as `upstream_4xx`,
    and `sessions show` counted both: 12 for 6 `HEAD /api/hello` 401s (live-claude finding 2). `request`
    entries are no longer counted, and `upstream_4xx_model` separates the model paths, since Claude Code
    sends that keyless probe at every launch.
  - **Id prefixes.** The README writes `7f3a`; `sessions show 87df` said "no session". A unique prefix now
    names a session for the read-only commands (`recall`, `search`, `state`, `sessions show`); an exact id
    wins, and an ambiguous prefix names up to 5 matches and exits 1. `sessions prune --session` takes the
    full id only: with prefixes, `prune --session w --yes` deleted the one session starting with `w`, so
    a mistyped or already deleted id removed a session nobody named, and a cleanup run twice deleted
    `test-2` the second time (reviewer A, PR #241). A prune id that only starts existing ids exits 2 and
    names them; an unknown id still exits 1. `mcp` and `run` keep exact ids: one is written by the
    launcher, and the other creates the session when it is absent, so a prefix there would open the
    wrong store or a new one.
    `run --session 5238` made a new empty session `5238`, which the readers then preferred (exact id
    first) to `52385048...` (reviewer B, PR #241); `run` now refuses an id that is not a session but
    the start of existing ids, exit 2, naming them, for claude and codex (in `launcher.open_session`).
    A new full id still creates its session.
  - **The live session.** Inside a fresh `contexto run claude` that had not paged yet, `contexto state`
    and `contexto search` answered from an older session with content: the default skipped the live
    empty session, and nothing told the CLI its session (reviewer A, PR #241). `run` now sets
    `CONTEXTO_SESSION=<id>` for claude and codex, and the default never skips a session another process
    holds open (its `live.lock`, probed as prune probes it).
  - **`--session` edges.** `--session ''` was taken as "not given" and quietly used another session; it is
    now an invalid id, for the readers, `proxy`, and `run` with either CLI. An id over 255 bytes crashed
    `recall`, `state` and `sessions show` with a traceback (Errno 63); `paths.session_dir` now refuses it,
    so every command reports it in one line.
  - **No junk sessions.** On a fresh home, `proxy --upstream not-a-url`, `proxy --port 1`, `run --upstream
    ftp://x claude`, `run claude --version` and `mcp --session typo-id` each left a session directory, and
    the docs-audit run had 7 empty rows in `sessions list`. A failed launch's empty session then became the
    default for `state` and `recall`. Now `mcp` serves only an existing session; `proxy` and `run claude`
    remove a session they created when the proxy cannot start, through prune's lock check (the store
    releases its own lock first, `SessionStore.release`); `run claude` with only `--version`, `-v`,
    `--help` or `-h` runs claude without a session or proxy; and the default skips sessions with no pages
    and no ledger records unless another process holds them open. Not changed here: a failed `run codex`
    still leaves its session (codex.py,
    whose launch order the codex-launch area changes), and a session whose CLI never sent a request is
    kept, since its log records the launch and the exit code. (Since D52 and D54 merged, a `run codex`
    refused before Codex starts removes the session it created too; a resumed `--session` is kept.)
  - **Validation and exit codes.** `--bound 10` started with a warning of "-79,990 tokens" while `--bound 10
    --headroom-floor 25000`, the default spelled out, was refused. The default floor is now checked like a
    given one: a bound not above 25,000 without `--headroom-floor` is a usage error (the engine's headroom
    is never below the floor, rule 17). `proxy --port -1` or `70000` printed an `OverflowError` traceback;
    it is now a usage error, exit 2. `run` exited 2 when the store could not open (`CONTEXTO_HOME` a file,
    or read-only); 2 stays the usage error, including an invalid session id, and a store that cannot open
    is 1, like a proxy that cannot start, for claude and codex. (After D55 merged, `run` refuses a home
    that is a file up front as a usage error, exit 2, unchanged; the stricter check is kept, and 1 is for a
    home that exists but cannot be written.) `sessions list` on a home that is a file
    said "No sessions" and exited 0, `state` and `search` said "no sessions under <file>/sessions", and
    `prune` printed a raw `[Errno 20]`; every reader now says "<home> is not a directory (check
    $CONTEXTO_HOME)" and exits 1. `sessions show --json` marks a cut list with `page_list_truncated`.
    A pages directory that cannot be read gave `sessions show` and `search` a `PermissionError` traceback
    with this change (reviewer A); `show` and `state` now print the summary or the pins and one error line,
    `search` one line, all exit 1.
  - **Help text.** `run` and the top-level help name Codex; `mcp` says the CLI spawns it; `proxy --help`
    no longer shows "``contexto proxy``: ... (M2 contract, CLI)"; `recall`'s `PAGE_ID` and `sessions
    show`'s `ID` have help. The `--codex-auth` help still cited D35 here (codex.py, the codex-launch area); D54 (PR #236)
    removed that citation.
  - **Review round 2 (reviewers A and B).** `contexto proxy --session` goes through the same check as
    `run`: an id that only starts existing sessions is refused with exit 2, so it cannot create a session
    that hides the one meant. `sessions prune --session ''` is an invalid id (exit 1) again, checked
    before the prefix rule, which had reported it as the start of every session. `run --help` and
    `proxy --help` say `--session` takes the full id; the readers' default names "or one in use".
  - **Round 4 (re-check after D55 and its follow-ups merged).** `run codex` with a `CONTEXTO_HOME` that
    is a file exited 1, while `run claude` refuses it up front with 2 (D55): `launcher.open_session`, which
    both CLIs use, now checks the home, so codex exits 2 too. Whether the launch created its session is now
    recorded in `pick_session`, where the D55 follow-ups open the store; a test makes the proxy's bind fail in
    `run claude` and checks that a session the launch created is removed and an existing one is kept.
  - **Merge re-check after D52.** `contexto proxy --session ''` exited 1 while `run` exits 2; an invalid
    `proxy --session` id (empty, or over 255 bytes) is now a usage error, exit 2, checked before the store
    opens. D52's removal of a session a refused Codex launch created is kept as the one mechanism for
    `run codex`; `run claude` and `proxy` keep `discard_new_session`, so no launch removes a session twice.

- **D58. The store and the MCP server stay up and say what is wrong.** Claude, 2026-11-05, under D0;
  contract change. From the 2026-11-05 QA run on main 4e2c4cd (store-mcp, verify-surrogate,
  scale-engine finding 2, verify-obligations finding 3), all reproduced at zero spend first.
  - **Lone surrogates.** One `\ud83d` with no low half raised `UnicodeEncodeError` at every strict
    `encode("utf-8")` (canonical JSON, the token estimate, the preamble hash, page ids, ledger lines,
    the session log), so the engine logged `transform_error` and forwarded untransformed from then
    on, and a page could never hold the turn. In the MCP server the reply write was outside its try:
    a page id, tool name, method or request id holding one ended the process (rc 1), and the call
    failed with "Connection closed" until Claude Code respawned it. verify-surrogate showed Claude
    Code 2.1.302 to 2.1.318 replaces lone surrogates with U+FFFD in every API request, and the API
    answers 400 to a body holding one, so this was downgraded to low; MCP tool inputs reach the
    server unsanitized, so that half was reachable. Rule: such a character is encoded as its JSON
    escape (`backslashreplace`, as `proxy.serialize_request` already did), which in JSON text is the
    same string again: pages, ledger lines and log lines stay valid UTF-8 JSON that reads back
    exactly, and the client reads the same string it sent. Strings without one encode byte for byte
    as before, so no existing page id, record id or hash changes. QA's test (8 cases, 7 failing on
    main) passes: the 40-turn conversation with the surrogate in turn 0, 3 or 39 is paged and
    bounded, and the server answers the ping after each of the 4 poisoned lines. Re-encoding a
    32,000-character reply costs about 23 microseconds.
  - **Tool errors.** The tools caught only `ValueError`, so a damaged ledger (`LedgerCorruptError`)
    made pin, close and state a JSON-RPC `-32603 Internal error`, with no `mcp_call` logged. Now any
    exception inside a tool is that call's tool result (`isError: true`), logged with its error
    class: a damaged ledger says pins and closes are refused until the user repairs it and names
    `contexto sessions show <session>`; anything else says the call failed with its class and to
    try once more. A non-string tool name was `-32603` and is now `-32602`; ids that are objects,
    arrays or booleans, which JSON-RPC does not allow, are `-32600` with `id: null`; `params` of
    `[]` or `0`, which read as `{}`, are `-32602`, and `arguments` of `[]`, `0` or `false` are the
    tool error "arguments must be an object", as `"x"` already was (reviewer B, PR #235). Errors echoed whole arguments: a 10 MB page id or
    obligation id, or 100,000 rejected `supersedes` ids, came back in full, outside any recall room.
    An echoed value is now at most 100 characters and a tool error at most 1,000. A pin or close note
    of whitespace only, which passed `minLength: 1`, is refused. A pin of an `event`, `artifact` or
    `reference` that is not `verified` returned "Recorded ..." although the pinned state shows those
    kinds only when verified; its reply now says so and suggests a decision or an obligation (the
    tool description is D51's).
  - **Unknown page ids.** `get_page` took `pg_` and any number of hex digits, so a 300-digit id
    failed in the read with `ENAMETOOLONG` and was reported "cannot be read" and logged
    `page_unreadable`, as damage. Only `pg_` and exactly 16 lowercase hex digits name a page now;
    anything else is "Unknown page id" and is never read.
  - **Files that are not files.** A FIFO in place of `meta.json`, `ledger.jsonl`, `log.jsonl`,
    `retained.json`, `epoch.json` or a page hung the MCP server, `recall`, `state`, `sessions show`
    and the proxy; a directory in place of the ledger or the log gave tracebacks; a damaged
    `page.key` gave the proxy a traceback (`KeyFileError` was not a `ValueError`). Every session file
    is now opened `O_NONBLOCK | O_NOFOLLOW` and checked to be a regular file before it is read or
    written (`contexto.private.open_regular`, `open_private`), as `affinity.salt` already was; each
    reads as the damage it is (absent metadata, an unreadable page, a damaged ledger, a key file
    error), and `KeyFileError` is a `ValueError` too. A symlink in a file's place is refused the
    same way (`NotRegularFileError`): with the first head, a symlinked `ledger.jsonl` raised a plain
    `ELOOP` that gave tracebacks in `sessions list`, `show` and `state` and stopped `contexto mcp`
    and the proxy, where a FIFO or directory degraded (reviewer B, PR #235). `ledger.lock` the same:
    a symlink, FIFO or directory there gave tracebacks in `sessions list` (for every session),
    `show` and `state` and stopped `contexto mcp`; it now reads as a damaged ledger (reviewer A, PR
    #235 round 2; the FIFO and directory cases crashed on main too). In the 21 FIFO, directory and
    symlink cases every MCP tool and CLI reader returns within the test's 10-second limit; on main 11 fail (reviewer A's count on PR #235; this entry first said 8, and its causes left out the
    lock files and the symlinked ledger: each FIFO blocks the MCP server on its first call, and a directory as the
    ledger or the log raises).
  - **Symlinked session directories: one rule.** A session directory that was a symlink was used by
    `mcp`, the proxy, `state` and `sessions show`, which chmodded the link's target to 0700, but was
    left out of `sessions list` and refused by prune. Rule: a session directory must be a real
    directory, and every command refuses one that is a symlink (the error says so; a dangling link
    keeps its "symlink to a missing target" message). Following them everywhere was rejected: prune
    must not delete through a link, so it could never be one rule. A symlinked `log.jsonl` was
    appended to and chmodded; files in a session are no longer opened through a symlink.
  - **A full disk.** `write_new` removed its temporary file only after a successful write, so
    `ENOSPC` left `.pg_<id>.json.<pid>.<rand>.tmp` in `pages/` (2 after one QA full-disk run), and
    `_modify_meta` did the same for `meta.json`. Writes were a single `os.write`, so a short write
    could rename a truncated `meta.json` into place, or leave a log line without its newline that
    the next event (`proxy_start`) was appended to, losing both. Now every write continues after a
    short write, a failed write removes its temporary file, a log event after a cut line starts on a
    new line (the cut line stays unreadable and readers skip it), and opening a store for writing
    removes temporary files whose process no longer runs (1 in 25 of QA's kill -9 runs left one
    between the link and the unlink), logging `stale_temp_removed`. A file must also be more than 10
    minutes old: a live writer whose pid this process cannot see (a container sharing the home in
    another pid namespace, another machine) looks gone, and a live temporary file exists for
    milliseconds (reviewer A, PR #235).
  - **`open_existing`** did not set `clock`, so `side_rooms()`, `set_side_room()` and
    `charge_turn_room()` raised `AttributeError` on a store opened for reading. No shipped command
    reached it; it is set now.
  - **Turn numbers.** Pages number paged turns from 0 (`page_out(turns, turns_paged)`) and the
    in-view search lines showed the retained entry's count of turn starts, from 1: in one QA reply
    the 33rd turn was "pg_... (turn 32)" and the 34th "in view (turn 34)". Rotation pages whole
    turns (`k` is always a turn start), so the in-view label now shows that count less one: a test
    pages 40 turns through the engine and finds each turn's unique key under its own index, paged
    or in view. Page turn numbers are hashed into page ids and fix every page offset, so they stay;
    the logged in-view span keeps the entry's `turn`. This holds only while every page is a whole
    turn: carried-text pages from the bounded fallback take page turn numbers too, and a resync
    (local compaction) restarts the CLI's count while `turns_paged` goes on, so in-view numbers then
    run behind recent pages and can repeat one (reviewer B, PR #235: in a 40-turn store with 4
    carried pages, CLI turn 38 was "turn 43" paged and "turn 39" named both CLI turn 39 in view and
    CLI turn 36 paged). Labelling from the page numbering would need the turn starts before `k` and
    `turns_paged` with each snapshot, and the epoch record that holds `turns_paged` is written only
    at turn starts, so a label built from it would still lag a mid-turn carried page. No tool takes
    a turn number (`contexto_recall` takes a page id and offset), so the contract says when the
    numbers match instead.
  - **CLI output.** The first head stored lone surrogates, but `contexto state` and `contexto
    recall` wrote to a strict UTF-8 stdout and raised `UnicodeEncodeError`; the ledger is
    append-only, so one such pin broke `state` for that session (reviewer B, PR #235). `cli.main`
    now sets stdout and stderr to write an unencodable character as its backslash escape, as the
    MCP server does; text that encodes is unchanged.

- **D59. Review follow-ups from the 2026-11-05 fix round.** Claude, 2026-11-06, under D0; contract
  change (Stack, rule 8, the store's file safety, the MCP errors, main selection, the version check,
  the `contexto run claude` lifecycle). The low findings reviewers left on PRs #235, #236, #238, #239,
  #244 and #245, each reproduced first by a test that fails on main 2388752. Zero spend: loopback
  stubs, fake CLIs, and the 2.1.314 and 2.1.318 binaries read as files.
  - **Host check by name (#239).** D53 required the proxy's own port in `Host`. An `ssh -R 9000:...`
    forward sends `Host: localhost:9000`, a proxy on port 80 gets `Host: 127.0.0.1` from clients that
    omit the default port, and both got 403. The port added nothing: a browser cannot put another port
    in `Host` on a connection to this socket, and a DNS-rebound page fails on the name. Rule: exactly
    one `Host` naming `127.0.0.1` or `localhost`, with any port of up to five digits or none; an
    `Origin` is still refused. `localhost.`, `evil@127.0.0.1` and a non-numeric port are still
    refused. Not covered: `host.docker.internal` from a devcontainer is refused for its name, as before.
  - **No abort after the last event (#239).** A client that closes once it has `message_stop`, before
    the chunk terminator, was logged `client_aborted: true` (reviewer A: 5 of 5 runs with the
    terminator 0.3 s late). Rule: the relay records when the stream's last event (`message_stop`, or
    a Responses end event) has been written to the client, with nothing still held; a close after
    that still shuts upstream but is not an abort. Log-only: nothing in `src` reads the flag.
  - **The choose.lock wait (#245).** Ctrl+C while another launch held `choose.lock` printed a
    `KeyboardInterrupt` traceback, and the wait of up to 5 s, then the unserialized choice, were
    silent. Rule: past 1 s the launcher prints "waiting for another contexto launch to choose its
    session"; at the timeout it says it chooses without the lock and that two launches may pick the
    same session; Ctrl+C prints `contexto: interrupted` and exits 130, as the version check does,
    with the lock descriptor closed. Measured with a real `contexto run claude -c` subprocess and the
    lock held by the test: on main a traceback and death by SIGINT, now exit 130 with no traceback.
  - **Claude Code options (#245).** Read from the 2.1.314 and 2.1.318 binaries: the main command's
    chain runs from `.name("claude")` to `run_main_options_built`, and declares 79 options with a
    required value in 2.1.314 and 80 in 2.1.318 (adding `--session-began-as-copy`). 36 of them were
    missing from the skip list, among them `--thinking`, `--thinking-display`, `--task-budget`,
    `--fast-mode`, `--workload`, `--agent-name` and `--sdk-url`, so `--thinking --continue` read as a
    continue where Claude Code takes `--continue` as the value. The list is now exactly that set.
    `-h` is Commander's `helpOption`, which a cluster does not look up: `claude -hc` is an unknown
    option and `claude -ch` prints help (reviewer A, 2.1.318), so `h` is no longer a flag and ends a
    cluster; `-ch` still reads as a continue, which is harmless since Claude Code then only prints
    help. Commander's dash-like test is a `-` followed by more, so `-r -` resumes the session named
    `-`; Contexto read it as the picker.
  - **One `codex --version` (#236).** `run_codex` read the version for the `--no-daemon` gate and the
    version check read it again. With a `--version` that hangs, a `codex exec` launch through a stub
    took 6.02 s on main and takes 3.51 s now (two runs each). The check takes the version already
    read.
  - **Damaged lock files (#235).** A symlink, FIFO or directory at `meta.lock` made recall, search and
    state answer "an internal error ... Try once more", which cannot succeed, and `contexto run` named
    only a path; at `live.lock` no store opened. Rule: both raise `LockFileDamagedError`, a
    `NotRegularFileError`, whose message names the file and says to delete it; the MCP server's
    reply says which file is damaged, what is refused (recall, search and state for `meta.lock`, whose
    pins and closes still work; every tool for `live.lock`) and that trying again will not help, and
    for `meta.lock` the `mcp_call` entry names the file. With a damaged `live.lock` the MCP server
    starts without the session, so the CLI shows that reply rather than a server that failed to start
    (review of #246). `contexto run` does not open the session with either damaged, on purpose:
    without `meta.lock` the room charges are not serialized, and without `live.lock` prune would take
    a running session for an idle one. Deleting the file is the whole repair (tested). A pin still
    works with a damaged `meta.lock`, since it writes no metadata.
  - **Compaction inside a tool loop (#238).** Reviewer B of #238 saw a compacted conversation whose
    first request was the summary, a role `system` message, then a pending `tool_use` and its result:
    no turn start, so `compacted` waited for the next prompt and the loop's Contexto calls were
    charged against the old conversation's room as published after the summary request, about 0 near
    the bound. (The captures below did not produce that form: their auto-compaction, forced by a
    reported 995,000-token usage, put the result in the summary request. PR #246's review found that
    auto-compaction at Claude Code's own threshold, `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=2`, does produce it
    on 2.1.314 and 2.1.318, mostly between a Bash call and its result; through `contexto run` this
    rule took main at the first request after each of the 32 compactions in 14 runs, where main's
    code left that request side.) Engine repro (bound 4,000, real
    input 3,600 then 3,700): on main the loop's request is side and `contexto_state` returns 0
    characters, `turn_budget: spent`; now it takes main (`compacted`) and returns the pin (1,413
    characters, room left 2,258). Rule: at a request with no turn start only the `compacted` rule is
    asked, and only when main is idle, that is, main's newest request (the summary request) was
    answered with no client tool call; so no call of the old conversation is outstanding to be
    charged to the new room. When the summary's response may hold a call (unknown) or is unanswered,
    the handover waits for the next prompt as before. A subagent, another session or another
    entrypoint never qualifies, as at a turn start, and only the compacted conversation's first
    request does (review of #246: a late request of an older compaction otherwise took main back and
    pointed a later `/resume` at it).
  - **Claude Code's compaction was often not recognised (review of #246, reviewer B; dates from D50).**
    D50 matched a first message whose content is a plain string. That form is real when Claude Code
    keeps tail messages after the summary (the previous assistant message after `/compact`, a pending
    tool pair after threshold auto-compaction; the attribution reminder then moves to the next user
    message), and D50 matched it; round 2 of the review found it in raw captures with attribution on.
    The captures below kept nothing after the summary. Captured on 2.1.314 and 2.1.318
    against a loopback stub (zero spend; headless `/compact` through `--input-format stream-json`,
    headless auto-compaction from a Bash call whose usage reported 995,000 input tokens, and
    interactive `/compact` in a pty): with Claude Code's default attribution reminder, the compacted
    conversation's first message is a list, the attribution `<system-reminder>` block, then the
    summary block; after `/compact` the command's blocks and the new prompt follow in that same
    message, and a role `system` message comes next. With the attribution set empty, `/compact` (2.1.318)
    sent a list starting with the summary and auto-compaction (2.1.314) a plain string, D50's form.
    In every capture the sentence is followed by a blank line and Claude Code's
    `<tools-used-before-summary>` reminder. Replaying the 8 captures through the engine: before, the
    compacted conversation was main in 1 (the plain string), side in 7; after, main in all 8, with
    `main_changed: compacted` at its first request. Through `contexto run` with real 2.1.314 and
    2.1.318 against the stub, headless `/compact` and auto-compaction (4 runs): the compacted request
    is main with `compacted`; main's engine, same run, left it side. Rule: the summary is the first
    text after any leading `<system-reminder>` text blocks, in a string or a list, and it must start
    with the sentence and a blank line; a prompt that starts with the sentence and goes on in the same
    paragraph does not match (tested in both forms). A first prompt that reproduced Claude Code's
    whole first line and blank line, in main's own session and entrypoint, would match; a prompt in a
    new session or after `/clear` has another session id and never qualifies. The fixture is the 8
    captures' Messages requests, scrubbed (`tests/fixtures/make_compaction_shapes.py`).
  - **Text.** D50's "never makes main idle" now names the window reviewer A found (a late end that
    arrives first leaves main idle until the retry's response), and the comment in `_observe_usage`
    says the same. The test of a response without an input count (#244) made the conversation busy
    only from a fresh request, which is already busy, so a skipped busy mark went unnoticed; it now
    makes main idle by a complete response first. With the idle update moved after the early return,
    the new test fails in its True and None cases; the old one failed only in its False case.

- **D60. A PDF is never estimated low.** Claude, 2026-11-06, under D0; contract change (rule 3). It
  replaces D48's estimate for documents, which priced a PDF by its bytes as text (as v0.5.8 did). Bytes
  err high only for a PDF of more than about 4 KB a page pasted (2 KB in a Read result); a compact one
  is undercounted. At 1,583 tokens a page (the measured 3-page PDF), 25 of the 28 fixtures under
  `tests/fixtures/pdf/` (0.07 to 2.4 KB a page) were estimated pasted at 0.02 to 0.59 of their cost, and
  in a Read result at 0.05 to 1.37. A 7-page PDFlib file was estimated at 230 tokens for about 11,081. Read
  in a tool loop 9,600 tokens under a 150,000 bound, it was estimated at 140,705 for a real 151,540, and
  the request went out over the bound unflagged.
  - **The rule.** A PDF costs its pages times 4,568 tokens: 3,000 text tokens (the top of Anthropic's
    documented 1,500 to 3,000 a page) plus a page image at the older 1,568-token limit. The page count
    must be certain. When it is not, the PDF counts as 600 pages (the API's maximum), and the request
    that brings it logs `pdf_pages_uncertain` (epoch, documents, pages). This covers a Claude `document`
    (base64, URL or file id) and a Codex `input_file`. A document by URL or file id, one over 32 MB of
    base64, and one that does not decode are never certain. For the ratio a document is still media with
    floor 0 (D48), so it never raises the ratio; its estimate caps what it can be charged.
  - **Certain.** A PDF lexer (`contexto.pdfpages`) must have read:
    - every object in the file and in every object stream, with every object stream inflated within
      16 MB per PDF and 4 MB per stream;
    - every stream ending where its `/Length` says, the `/Length` being direct or an integer object
      every version of which holds the length it was read at;
    - no `N G obj` header in a byte it passes over: a stream body, a string or a comment. Whitespace,
      NUL and comments between the tokens are allowed for. A reader that follows the xref finds an
      object at such an offset, so this is how a `/Length` that lies long, or a page written inside a
      string, is caught.
    Every version of an object counts. So does every version of the object an indirect value names
    (`/Kids 900 0 R`, `/Type 902 0 R`), since the lexer does not know which one the xref names. The
    pages are the larger of two counts:
    - the object numbers typed `/Page` in any version, together with the leaves of the page tree's
      `/Kids`, each number once. A node is typed `/Pages`, or has `/Kids` and is reached from a
      catalog's `/Pages` or from another node, so a form field's or a name tree's `/Kids` is not a
      page list;
    - the tree as a renderer walks it, from each catalog's root and each node no node lists. Every
      `/Kids` entry counts each time it is listed: a kid that is not a node as one page, and a node as
      the pages under it but at least one, since the version that makes it a node may be dead. A node
      counts the pages of whichever version holds the most. So a dead copy can only add.
    Any version of any node's `/Count` may raise the count but never stands in for a part that was not
    read. The count is at most 600. Anything else is not certain: another filter or a predictor on an
    object stream; an object stream's `/N`, `/First`, `/Filter` or `/DecodeParms` with versions that
    differ; a cycle in the page tree; a token the lexer cannot read; an indirect value whose target was
    not read; `/Kids` holding anything but references; a bound passed; or no pages at all.
  - **PR #248 round 1 (reviewer A).** Two shapes counted low while certain, and both are fixed:
    - one page object listed 50 times in the root's `/Kids` counted 1 (qpdf and pypdf read 50);
    - an indirect `/Kids` or `/Type` resolved to the copy read last, so a later copy that no xref
      entry names (a plain object, one in an object stream, or a dead `/Type /Pagex`) hid 49 of 50
      pages.
    Read at 90,865 on a 150,000 bound, each was estimated at 95,472 for a real 170,074 and sent
    unflagged. Both now count 50, certain, and the same reads are flagged before the send. A
    PDF is also parsed once however many threads wait on it, since its count is cached before the
    parse lock is released.
  - **PR #248 round 2.** Reviewer A found that merging versions was quadratic and unpaid by the step
    budget, and that a dead version could still hide pages. Reviewer B found the allowance's false
    advice. All are fixed:
    - **Merging cost.** M objects naming one value of K versions of W entries cost M × K × W. A
      1.3 MB PDF (an indirect `/Kids` of 2,048 entries in 32 versions, named by 16,000 nodes) took
      66.7 s, and another session's 1-page PDF waited 63.4 s behind it, 110.9 s behind 20 smaller
      copies (main 0.003 s). Every value read in resolving versions and walking the tree now costs a
      step, and each resolution is memoised per object and version count, so a value named many times
      is read once. The walk runs over nodes and arrays, each array summed once. The eight shapes now
      take 0.16 to 0.24 s, uncertain (or certain and exact where every version agrees).
    - **A page made to look like a node.** A version of a page that makes it look like a node (a dead
      `/Type /Pages`, a dead `/Kids []`, a dead `/Pages` copy of an indirect `/Type`, or a page that also
      carries `/Kids []`) made the walk count its 50 entries at 0, so 50 pages counted 1, certain. A
      kid the walk reaches now counts at least one page; an empty real node errs high by 1.
    - **Smallest first.** Parsing is still one PDF at a time, now the smallest waiting first. Behind 20
      PDFs that spend the step budget, another session's 1-page PDF waited 4.5 s in arrival order and
      waits 0.22 to 0.25 s now (its text requests 0.010 to 0.014 s). Round 3 bounds the wait of a large
      PDF (below).
    - **Long tokens.** A regular token over 13 bytes is classified in place, not copied: 30 MB of digits
      peaked at 30 MB, now under 1 MB.
    - **The allowance is not the bound (reviewer B).** About 26 typical pages or more pasted at a turn
      start logged `bound_too_small`, and the launcher told the user to restart with a larger `--bound`,
      in 38 of 68 pastes, 35 of them falsely: 30 PyMuPDF pages were estimated at 146,623 for a real
      57,137, and even the largest bound allowed without `--allow-large-bound` cannot hold them. When
      the new turn would fit `W` with its PDFs at 1,583 a page, the engine logs `pdf_allowance`
      instead, with the same sizing, `new_turn_tokens` and `pdf_allowance_tokens`. The rotation is
      unchanged. The launcher says once that a large PDF is estimated generously. `pdf_pages_uncertain`
      is now shown too, once: a PDF could not be read fully and is counted at the maximum, 600 pages.
      Of 68 pastes in B's 230 sessions, the advice now prints for 2, both real overflows that their text
      controls share (it printed for 38).
  - **PR #248 round 3.** Both reviewers:
    - **The bound is in steps.** A timing test asserted under 0.5 s, and hosted runners took 0.52 to
      1.00 s (macOS 3.15 failed 7 of 8 shapes; the builder's machine 0.16 to 0.24 s). The tests now
      assert the steps (a shape fits the budget, or spends it and is uncertain) and keep 5 s only as a
      backstop against super-linear work. Times here are the builder's machine's unless said otherwise.
    - **An interrupted waiter (B).** A thread interrupted while waiting to parse left its place in the
      queue, and every later parse would have waited for it forever. It now takes itself out, and the
      next waiter is chosen.
    - **A large PDF's wait is bounded (A).** Smallest first let a large PDF wait while smaller ones kept
      arriving: a 20-page PDF waited 4.78 s while 2 threads fed 1-page ones. A PDF that has waited 1 s
      now goes next (the longest-waiting first): it waited 1.003 s with 2 and with 4 feeding threads. The
      next waiter is chosen once, by the parse that releases the lock, so the lock never sits free. A
      PDF over the 32 MB base64 limit is never parsed and no longer waits.
    - **`pdf_allowance` only for certain PDFs (A and B).** An uncertain PDF was taken at its floor, 0,
      so the turn always "fit", and the user read that the bound did not need to change for listings of
      100 and 300 real pages (158,300 and 474,900 at 1,583 a page). A turn with an uncertain PDF now
      logs `bound_too_small` with `pdf_uncertain`, and the launcher says a PDF could not be read fully
      rather than advising a larger bound outright. The `pdf_allowance` notice no longer promises that
      the bound need not change: dense pages within the allowance can still go over (B: a real 161,986
      on 150,000). It says the turn may fit with typical pages.
  - **The lexer (PR #234 round 9).** The round-9 parser used one token regex. Its nested quantifiers
    backtracked exponentially on comment runs (22 `%` then a newline and `)`: 0.43 s, 4 times more for
    every 2 more `%`; a 140-byte PDF held the proxy for 229 s). They backtracked quadratically on digit
    runs (40,000 digits: 7.2 s) and kept a frame per repeat (4 MB of spaces: 571 MB RSS; 16 MB of `%\n`:
    2,560 MB). The lexer is now a hand-written single pass:
    - Each regular expression is one character class or one literal, matched at a position or searched
      forward. A run of whitespace, regular characters or hex digits is passed in one C call, and none
      can backtrack.
    - A dictionary keeps only the keys the count reads. The value of any other key, and any container
      under it, is skipped to its next delimiter without making objects. A cross-reference table is
      passed in one match: it holds only digits, `f`, `n` and whitespace.
    - Every token, comment, string delimiter and skipped delimiter is a step. Past 400,000 steps per PDF
      the count is not certain.
    - Resolving versions and walking the page tree are charged to the same steps (round 2).
    - Every 4,096 steps the lexer sleeps 0.1 ms. `sleep(0)` would hand the GIL straight back and restart
      a waiting thread's switch timer: a text request then waited 1.9 s behind 20 hostile PDFs.
    - The proxy counts a request's PDFs before it takes the transform lock (`RewriteEngine.prime`), one
      PDF at a time, the smallest waiting first. Twenty parsing at once starved another session's request
      of the GIL (2.05 s).
    - Counts are cached by content: 65,536 entries, least recently used dropped first.
    - The inflate budget is checked before every object stream, so once it is spent exactly the next
      stream is not inflated (zlib reads a `max_length` of 0 as no limit).
  - **Measured (no live calls).**
    - Real producers. 48 generated files: reportlab (compressed, uncompressed, 25 links a page,
      platypus, 650 pages), PyMuPDF (plain, links, object streams), fpdf2, cupsfilter and sips, each
      re-saved by qpdf (through pikepdf) with object streams, linearized, and some in QDF or plain. 47
      count exactly and certain, the 650-page file at 600. The one PyMuPDF file with 30 of 40 pages
      deleted by an incremental save counts 40, high on purpose. The 28 committed fixtures: 25 exact and
      certain, that deleted-pages file high, and the two listings uncertain (below). The heaviest real
      file, 600 pages with 25 links a page (15,000 annotations), takes 360,116 steps and 0.19 s, the
      count included; a 300-page PyMuPDF file with 20 links a page takes 147,374 steps and 0.07 s.
    - The #234 repros from rounds 3 to 9 are each exact and certain, or 600 and uncertain. Round 8's
      hidden-syntax variants (comments, `#xx` escapes, NUL, 17 spaces, an indirect or missing `/Type`,
      an untyped tree, 3 visible pages and 47 hidden, an orphan `/Count`) read 50 of 50, certain. Its
      object streams under ASCIIHex or over 4 MB are 600. The split object streams (up to 600 pages, 100
      links a page) are exact or 600. 128-stream marker floods take under 0.05 s. The lying `/Length`
      and missing `endstream` files, and `N G obj` hidden in a body, a string or a comment, are 600. A
      redefined or missing indirect `/Length` is 600.
    - Hostile inputs, within the step budget, single or 20 at once, at most 0.27 s each on the builder's
      machine (round 2: 43 inputs, reviewer A's 29 lexer stress inputs and round-2 shapes among them;
      the slowest, 20,000 versions of the page tree, 0.255 s single and 0.272 s with 20 at once; up to
      about 1 s on CI's hosted macOS runner):
      - 2 million `/Count 1`: 0.21 s, uncertain;
      - 16 MB of `%\n`: 0.12 s;
      - 16 MB of spaces, NULs or CRLFs inside an object: 0.01 s, still certain;
      - a 16 MB string: 0.06 s;
      - 4 million escapes or nested parentheses: 0.05 s;
      - a million nested brackets: 0.000 s;
      - 1,000 zlib bombs: 0.012 s.
      Python allocations peak under 1 MB for all but the bombs (one 4 MB stream at a time, 8.5 MB peak).
    - Concurrency. 20 sessions each post a 900 KB PDF of 200,000 tokens, and a text request from another
      session follows 0.3 s later. It waited 0.005 to 0.12 s with the clients in another process (main:
      0.014 to 0.58 s, its time spent reading the same bodies), against 48 s at 323bf9e. Twenty such
      parses at once peak under 16 MB of Python allocations together.
    - The estimate. Each committed fixture is estimated at 4,568 a page, 2.89 times a 1,583-token page,
      pasted or read. The PDFlib file read near the bound is estimated at 172,415 for 151,540 and
      flagged `over_bound` before the send, where main sent it unflagged. Pasted at a turn start, the
      rotation pages enough for the request to fit (28,292 sent on a 150,000 bound).
    - Media-free and image-only sessions are byte-identical to main: 560 requests in 8 sessions (Claude
      and Codex, text only and with screenshots, bounds 60,000 and 150,000, 7 to 10 rotations each at
      60,000) give the same forwarded bodies and events.
  - **Not covered.**
    - **The allowance runs high for typical pages,** 2.9 times at 1,583 a page, and an uncertain PDF
      far more (600 pages, 2.74 million tokens). A large PDF pasted at a turn start can therefore page
      the earlier conversation and log `turn_room_low`: in reviewer B's runs, 30 PyMuPDF pages pasted at
      60,000 kept 1 message where an equal-cost text control kept 41. This is the cost of never
      estimating a PDF low; the launcher now says it is the PDF's allowance (`pdf_allowance`), not the
      bound, that fell short.
    - **Parsing is one PDF at a time.** A PDF waits for the parse in progress (one step budget), and
      for smaller PDFs for at most 1 s, after which it goes next among those that have waited that long,
      the longest-waiting first. 20 PDFs that spend the step budget take about 5 s together on the
      builder's machine.
    - **Pages that show PDF syntax are uncertain.** Uncompressed content streams that show `N G obj` as
      text are 600. Examples are a PDF tutorial, or reportlab with `pageCompression=0` listing a PDF's
      source. The lexer cannot tell such text from an object a reader finds by its offset.
    - **Deleted pages count high.** Pages an incremental save deleted still count, because every
      version counts.
    - **Some PDFs are never certain.** Encrypted object streams, and object streams under another
      filter or a predictor, are 600. So is a PDF that needs more than 400,000 steps (about 300 or more
      pages with 60 or more links a page). Anthropic refuses encrypted PDFs.
    - **A document by URL or file id is always 600 pages,** so it is over any bound. Claude Code's Read
      tool sends PDFs as base64.
    - **A dense page can cost more than 4,568:** small-font code or tables. The estimate is then low by
      the difference, as D48 said of its 3,000.

- **D61. Remaining review lows from the 2026-11-05 fix round.** Claude, 2026-11-06, under D0; contract
  change (rules 3, 4 and 16). The low findings reviewers left on PRs #234, #244 and #248, each reproduced
  first by a test that fails on main 08ec571. Zero spend: synthetic upstreams and stub tokenizers, no
  model calls. #246's D50 nit is prose and goes to the docs pass.
  - **Codex tool output at the tool-output seed (#244 reviewer B, round 2; pre-existing).** Codex
    responses that carry no input count leave the pre-send estimate as the only bound check, and that
    estimate counted `function_call_output` at the 3.5 text seed while Claude's `tool_result` counts at
    1.5. B's repro (bound 30,000, a 20k JSON-dense head, a code or non-ASCII window, the first input
    count at request 30) sent turn starts at up to 36,011 real tokens and tool-loop requests at up to
    35,958, none flagged. Rule: before a ratio is measured, a Responses item whose type ends in
    `_output` counts at 1.5 bytes per token, as a `tool_result` block does; after, every item counts at
    the measured ratio, as before. Measured with a stub tokenizer (the head at 2.0 to 3.5 bytes a token,
    the tool output at 1.8 to 3.0, the first input count at request 30 or never; 32 sessions of 36
    requests): turn starts over the bound unflagged in 26 sessions on main (up to 44,202), in 1 now (a
    2.0 head and a 1.8 window with no input count ever: 30,034 to 30,166). Tool-loop requests over it
    unflagged in 30 sessions on main, 12 now, all with a head at 2.5 bytes a token or denser. Reviewer A's
    harness (1,440 sessions at bound 30,000): requests over the bound unflagged 6,451 on main, 445 now;
    turn starts over it 89, now 0; sessions whose responses all carry an input count unchanged. The cost:
    Codex sessions with no input count rotate 9 to 20% more often, since tool output now counts higher.
  - **An older record's head (#244 reviewer A).** `resume_state` adopted `head_tokens` from the record
    unchanged, so a head v0.5.8 measured low (0 for a 25,365-token head on a resumed first request)
    stayed with the session after the upgrade, the round-1 overrun shape. Rule: the record carries
    `head_rule` (2), the head measurement it was taken under. A record without it resumes with no
    measured head and no `W`: at `k = 0` the next usage measures the head again (a head of 1 recorded,
    466 measured for a true 467 at 4 bytes a token), and at `k > 0` rule 4 estimates it, as for any
    unmeasured head. Every release through v0.5.23 writes records without the field, and a record cannot
    tell which rule it was written under, so a correct head from v0.5.9 on is dropped and measured again
    too; the bound is unaffected. The store accepts the field or its absence, so an older record still
    resumes. A record written by this version has the field, which earlier versions refuse: a downgrade
    starts from epoch 0.
  - **Crafted JPEGs (#234 reviewer A, round 10).** The JPEG walk stepped one marker at a time over up to
    192 KB, and every image in view is priced on every request under the transform lock. 100 images of
    196,000 fill bytes priced in 0.97 s, of 98,000 RST markers in 0.75 s, of 49,000 2-byte segments in
    0.81 s, against 0.03 s for normal JPEGs. Rule: a run of fill bytes is skipped in one step, and at most
    256 markers are walked; past that the image is priced as unknown (real files reach the frame header
    within a few dozen). The three shapes now take 0.032 to 0.037 s: the fill-byte image still reads as
    1920x1080, the others cost 4,787.
  - **Patches (#234 reviewer A, round 10).** D48's four count_tokens results are exactly
    `ceil(w / 28) * ceil(h / 28) + 3` (641, 1,719, 2,694, 3,243), and `w * h / 750` alone undercounts a
    thin image (2576x29: 100 against 187) and some larger sizes (reviewer A measured 6.6% to 27.7% of
    sizes above 500 pixels an edge, by up to 34 to 91, depending on how the sizes are sampled).
    Rule: an image costs the higher of the two after scaling, each edge scaled in whole pixels rounded up,
    between 64 and 4,787: the documented 4,784 is 92 x 52 patches at the 2,576-pixel edge, plus the 3.
    The floor (the ratio's lower bound) is unchanged, the pixel formula alone, which stays under the
    patch count. The patch model is inferred from four measurements; no live call checked a thin image.
  - **The parse queue is tested (#248 re-check, reviewer A).** The cross-session test was loosened to a
    5 s backstop, and with the queue removed its text request waited 0.89 to 1.65 s, under it. It now
    counts the PDFs parsing at once through `count_pages`: 1 with the queue, 19 without.
  - **`pdf_uncertain` (#248 re-check, reviewer A).** At bound 150,000 an uncertain 1-page listing pasted
    with about 250,000 tokens of text logged `bound_too_small` with `pdf_uncertain`, and the launcher
    said a larger `--bound` helps only if the PDF is long, though the text alone needed one. Rule: the
    mark is set only when the turn would fit `W` with each uncertain PDF at 0 (and each certain one at a
    typical page); otherwise the event is a plain `bound_too_small`, and the launcher advises a larger
    bound.
  - **The start warning's low end.** The launcher's warning for a bound that may leave no window budget
    said measured overheads run "from about 15,700" (a QA head of 15,689 against a stub). Measured on
    Claude Code 2.1.314 with a fresh config the overhead runs as low as 13,842; the warning, its comment
    and rule 4 now say so (left to this PR by the docs pass, since it is code). `run codex` quotes Codex's
    own measured range, 13,322 to 18,403, not Claude Code's (reviewer B).
  - **Already fixed.** The two code comments #234's reviewer A named (a document "far above what it
    costs" in `rewrite.py`, and the floor taken off real usage in `tokens.py`'s docstring) were corrected
    by D60 (#248). The stale `media_allowance` assertion in `tests/test_image_estimate.py` is removed.
  - **Not covered.** A head denser than the 3.5 text seed is still estimated low until the first usage,
    on Codex as on Claude, so tool-loop requests can go over unflagged when no response carries an input
    count (the 12 sessions above). Images in history are still priced once per request each; caching
    their cost was not needed once the walk was bounded.
- **D62. The v0.1.0 to v0.4.0 releases and tags are deleted.** Owner, 2026-11-06. The 2026-11-05 QA
  run found the owner's proxy hostname in `docs/DECISIONS.md` at all 19 tags from v0.1.0 to v0.4.0,
  so GitHub's automatic source archives on those release pages held it; no tag from v0.5.0 on does.
  The 19 releases and their tags were deleted with `gh release delete --cleanup-tag`. Nothing was
  force pushed: the tagged commits are all on `main` and stay there, and their wheels and sdists,
  which did not hold the hostname, are no longer published. Versions still come from the latest tag,
  so the next release follows v0.5.24. A backup of each release's notes, assets and tags is kept
  outside the repository. Old commits, pull request diffs and pull request comments still hold the
  hostname (D42).
- **D68. The README is rewritten so a stranger can act on it in 30 seconds.** Claude, 2026-11-12,
  under D0; docs only, no code and no contract rule changes. An audit of the README on main
  (7d59e9b) found 772 lines, the headline number (52 of 54) first at line 57, the install command at
  line 76, the statement that a subscription login is not a supported setup at line 511 (in the
  FAQ), no latency figure, and no comparison with compaction, a 1M window or Claude Code's memory.
  The README now opens with what Contexto does, three numbers with their limits, the install
  command, `contexto run claude` and a recording, and says near the top who it is not for.
  - **Three slices.** (1) PR #254 moved the reference sections into `docs/*.md` with an index
    (`docs/README.md`) and a link check (`tests/test_docs_links.py`); the README went from 772 to
    185 lines. (2) PR #259 adds the recording (`docs/assets/demo.svg`, its transcript `demo.txt`),
    the request-size chart (`request-size-light.svg`, `request-size-dark.svg`) and the scripts that
    make them (`spikes/readme/`). (3) This slice (PR #265) wrote the README (259 lines): the
    first screen, who it is for and not for, a diagram, the comparison, what the user sees when it works and when it
    fails, privacy, and a short limitations list. It moved the Results table and its cost paragraph
    to `docs/results.md` and the full limitations list to `docs/limitations.md`, word for word, and
    added the caveats below to both.
  - **Claims policy.** A number appears with its limits (one session, one seed, measured or
    modeled) and cites a record; nothing is shown that the tool did not print.
    - Recall is presented as "no worse than full context on a scripted recall test", never as
      better. In run #4 Contexto missed 2 probes and the baseline 7, none in common
      (`docs/research/M2-acceptance-run4.md`, "Misses (ids only)"). The exact McNemar test on the 9
      discordant probes gives p = 2 x (C(9,0) + C(9,1) + C(9,2)) / 2^9 = 2 x (1 + 9 + 36) / 512 =
      0.18. On claude-opus-5-5 the baseline led, 54 against 53.
    - Fewer input tokens are stated as fact (3,725,391 measured against 6,972,335, 46.6%). Cost is
      stated as depending on the model and on cache hits. Run #3's cache-class counts
      (`docs/research/M2-usage-savings-run3.md`, "By cache class": Contexto 88 uncached, 2,677,349
      cache read, 1,047,954 cache write, 22,693 output; plain 48, 6,533,929, 438,358, 12,836) at
      Haiku 5.5 base rates with no step cost $0.169 against $0.127 (34% more), and at Opus 5.5 rates
      ($4 input, $20 output, $0.20 cache read, $5 cache write; `docs/research/M0-cost-model.md`,
      "Prices and cache rules") $6.23 against $3.76 (66% more). The 49.7% saving exists because
      Haiku 5.5 charges 5 times the rate over 100,000 tokens. With the baseline arm's two measured
      cache misses, plain would cost more in both flat rows. The arithmetic is now in the record
      ("The same token counts at other prices"), so the README cites a record, not its own sum.
    - Claude Code's behaviour is quoted from its docs as fetched on 2026-11-12 (model-config,
      context-window, memory, settings-reference). The README says that a session under about
      967,000 tokens does not compact with or without Contexto, and names the one exception the
      docs give: a conversation of 200,000 tokens or more left idle for about an hour on the
      one-hour prompt cache is compacted by default (`idleCompaction`).
  - **Calls made without the owner.**
    - The `## Troubleshooting` heading stays in the README, because the program prints "See README,
      Troubleshooting." (`toolcheck.py`) and "see README, 'contexto run'" (`launcher.py`), and
      nothing under `src/` changes in a docs slice. The section points to
      `docs/troubleshooting.md` and `docs/run-reference.md#contexto-run`.
    - The link check is a pytest in the existing suite, not a new workflow.
    - The diagram is Mermaid, which GitHub renders in both themes with no image file to keep.
    - The chart is drawn from run #3's per-request CSV, the only run with a modeled full-history
      size for every request. Its full-history line is the record's plain-run shape: turns 1 to 18
      modeled, and for the six probe turns the baseline arm's measured `count_tokens` (435,590 to
      438,406). The CSV's own probe-turn column, the "same request points" series that peaks at
      493,902, is not plotted: the record says it overstates plain, because it carries the search
      tool loops that only Contexto makes. Review of PR #259 found this.
    - The recording is an animated SVG drawn by `spikes/readme/termsvg.py`, every row made only of
      `<tspan>` pieces: WebKit (Safari, every iOS browser) stretched bare text beside a `<tspan>` to
      the row's whole `textLength` and garbled the sizes. Review of PR #259 found this.
    - The Python badge is static (3.12 to 3.15, from the classifiers and the CI matrix), because the
      package is not on PyPI. The other two badges are CI on main and the licence.
    - The README no longer has a `#results` anchor; `docs/faq.md` links `docs/results.md`. The
      "What it includes" list left the README; each item is in a guide.
    - The heading "Why not compaction, a 1M window or the memory tool?" keeps the words of the
      owner's brief; the paragraph under it describes CLAUDE.md, auto memory and the API's memory
      tool, each from its own docs page.
    - The README is about 250 lines, not 200. Review asked for the subscription caveat before the
      install command, one rule for who it is for, a plainer cost line, named runs, a definition of
      a tool loop and a latency table; each added lines, and none was cut to reach a number.
    - "Window budget" stays defined in the README although the README uses it once, because
      `docs/mcp-tools.md` points to that definition.
    - `docs/limitations.md` has five entries the moved list did not have (tool loops, recall is a
      search, cost, latency, subscription logins), and the FAQ's cost answer says that cost depends
      on the model.
    - The recall bullet keeps the title "no worse than full context on a scripted recall test",
      the owner's wording. A stranger-read review asked for "close to full context", because the
      Opus run had full context one question ahead (54 against 53). The headline run had Contexto
      ahead (52 against 47), and the Opus run is within the test's pass rule (within 2 overall, no
      category more than 1 below), which the bullet now says.
  - **Repository About box**, applied with `gh repo edit` on 2026-11-14, before this slice merged,
    once both reviewers had approved the README it matches; no other repository setting changed.
    Old values, to restore from: description "Long-running AI sessions and context management",
    homepage empty, no topics. New description: "Local proxy that keeps each Claude Code and Codex
    request under a fixed token bound, so the CLI does not reach its compaction point: old turns go
    to disk word for word and the model searches them back." (Review changed "sessions" to "each
    request": only each request is bounded.) New topics: `claude-code`, `codex`, `context-window`,
    `compaction`, `context-management`, `llm-proxy`, `mcp`, `ai-agents`, `developer-tools`,
    `python`, `anthropic`, `llm`.
- **D70. `contexto serve`, gateway hint headers, and a cloud-provider guard.** Claude, 2026-11-12,
  under D0; contract change (Stack, Upstream resolution, main selection, the missing-tools and
  session-start checks, the `contexto run claude` checks). This entry is written in slices; the
  first covers the hint headers and the guard on the launcher path. Measured at zero spend on Claude
  Code 2.1.322, macOS, against loopback stubs with temporary config directories and a fake key
  (`docs/research/D70-serve-probes.md`: 34 runs, 177 requests; 36 more runs for this slice and 23
  more after its review, `spikes/m2/serve/q10_truthy.sh`).
  - **Gateway hint headers: the problem.** The engine decides what a request is from signatures that
    each Claude Code release can change: a tool list for "the main conversation or a subagent, not a
    side request" (D38, D50), the summary's first line for "Claude Code compacted main" (D50; D59
    found it missed 7 of 8 real captures), and `X-Claude-Code-Agent-Id` for "a subagent". Claude
    Code documents the same facts as headers (`llm-gateway-protocol`, "Gateway hint headers", from
    2.1.273): `x-claude-code-request-class` on every request (`main`, `subagent`, `workflow`,
    `compaction`, `auxiliary`), `x-claude-code-compaction` on the summarization request and
    `x-claude-code-context-compacted` once on the first main request after it (`auto`, `manual`,
    `reactive`), and `x-claude-code-prompt-id`, `x-claude-code-agent-type` and
    `x-claude-code-prev-tool-durations`. With a custom base URL they are off unless
    `CLAUDE_CODE_GATEWAY_HINT_HEADERS` is on. Measured in an interactive session of 39 requests with
    the variable on: every Messages and `count_tokens` request carried the class (title and
    `count_tokens` `auxiliary`, main turns `main`, the subagent `subagent`, `/compact`'s two
    summarization requests `compaction` with `x-claude-code-compaction: manual`, and the next main
    request `x-claude-code-context-compacted: manual`); without it, and with `0`, none of the six
    appeared, while the session id and agent id headers still did.
  - **Decided: ask for them, prefer them, keep the signatures.** `contexto run claude` sets
    `CLAUDE_CODE_GATEWAY_HINT_HEADERS=1` in its `--settings` file. Where a request carries a hint
    with a documented value, it decides; where it does not, the signature decides as before, request
    by request (`RewriteEngine._classify` has the table, and the contract repeats it). Class `main`
    or `compaction`: the request is judged whatever its tool list. Class `subagent`, `workflow` or
    `auxiliary`: never takes over main by any rule, the turn-start rule included, is not judged for
    the session start, and is not checked for Contexto's tools while it is main for having arrived
    first. Any other class value is read as no header. `x-claude-code-context-compacted` with any
    value marks the conversation as the compaction of main, whatever its first message says; the
    rule's other conditions (main's session id and entrypoint, main idle when there is no turn
    start) stay. The header is sent once, so the summary text still marks every other request. A
    compaction that keeps a pending tool call (D59) hands over at the request that carries the
    header, also when a side request used its key first; without the header it hands over only at
    its key's first request, as before. With
    no hint header on a request nothing changes: the suite's 3,189 other tests pass unchanged, except
    three assertions on the exact content of the `--settings` file.
  - **The summarization request (`compaction`).** It is main's history with the instructions
    appended, under main's key, with main's tools and system (D50 on 2.1.314 and 2.1.318; the same
    on 2.1.322). Decided: it is main's request and gets no path of its own. It goes through main's
    epoch and is paged under the bound like any main turn (the test sends the same request with and
    without the header and gets the same bytes, paged). Rejected: forwarding it unchanged because
    Claude Code is compacting anyway. It would carry the whole local history over the bound.
  - **Disagreements.** When a header and the signature beside it disagree the header decides, and
    the engine logs `hint_mismatch` (`header`, `value`, `signature`) once per pair and engine: the
    early warning that a signature drifted. One exception: class `main` with
    `X-Claude-Code-Agent-Id` never takes over, since the agent id is a documented header too and the
    answer that leaves main where it is cannot cost the bound. Not logged: the summary text without
    `context-compacted`, which a resumed session's compacted transcript also has.
  - **What the upstream gets.** The docs say why the hints are off for a custom base URL: "a proxy
    that rejects unknown headers would fail the request". Contexto turns them on for its own use, so
    the proxy leaves the six out of everything it sends upstream, at the one place it writes request
    headers, which covers the transformed request, requests it does not transform, and the CLI's own
    request on every fail-open path. Tested: with the hints stripped, the upstream receives the same
    headers in the same order and the same bytes as for the same requests sent without hints, over a
    first request and a paged second one, so the prompt cache sees no difference. Only the class and
    the compaction trigger are ever logged, after matching the documented sets; a trigger outside
    them is logged as `other`. `ContextoProxy(strip_hint_headers=...)` is off by default, so
    `contexto proxy` and `contexto run codex` are unchanged.
  - **Three states per launch** (`contexto.hints.hint_headers_state`, logged as `hint_headers` in
    the launch event). `stripped`: the default above. `forwarded`: the user had the hints on
    already, their gateway was getting them, and they pass through. "On" is the value Claude Code
    would run with without Contexto's `--settings`: the managed settings file's if it sets the
    variable (from Claude Code's documentation, which names managed settings as the place for it;
    not measured), else the first settings file `env` block that sets it, else the process
    environment. Measured: a settings file's value wins over the environment's in both directions
    (environment `1` with settings `0`: no hint header; environment `0` with settings `1`: sent), a
    project file's value wins over the user file's, and `1`, `true`, `yes`, `on` and `TRUE` send
    them where `0`, `false` and `2` do not. A project file follows the base URL's trust rule (D52):
    it counts as `claude -p` reads it, and not while `CLAUDE_CODE_IGNORE_PERSISTED_TRUST` is set.
    Kept as it was, knowing that interactively Claude Code applies the file only after its trust
    dialog, so a project file that turns the hints on lets them reach the upstream before the
    folder is trusted; the stricter reading would take the hints away from a gateway for a project
    that is only ever run with `-p`. `off`: the hints are not on and the user set the variable to
    `0`, `false`, `no` or `off` in the process environment or the user settings file (or the
    managed file does, which outranks `--settings`). Contexto then leaves the variable out of its
    `--settings` file, Claude Code sends no hint header, the engine reads its signatures, and the
    proxy removes nothing. The first version of this slice set `1` over the user's `0` (measured:
    the `--settings` value wins over a `0` in the environment, in the user file and in a project
    file); the review asked for a way out, and the user's own off is it.
  - **Nested launches.** Claude Code applies the `env` block of the `--settings` file to its own
    environment, so a tool command, a hook or an MCP server of the session inherits
    `CLAUDE_CODE_GATEWAY_HINT_HEADERS=1` (measured in review: a Bash command printed it). A nested
    `contexto run claude` read that as the user's setting and forwarded the six headers to its
    upstream. Decided: the launcher writes its state beside the variable as
    `CONTEXTO_HINT_HEADERS`, and a launch that finds `stripped` there does not count the process
    environment's value (with `forwarded` the user really had the hints on, so the nested launch
    forwards too; a settings file's value counts either way). Tested with the fake `claude` and
    with the real 2.1.322 run with the inherited environment. Limit, not fixable in the proxy: a
    nested plain `claude` pointed at another base URL inherits the `1` and sends the hints there,
    with no Contexto proxy in its path. A user whose gateway rejects them sets the variable to `0`
    and gets the `off` state. Rejected: a `contexto run --no-hint-headers` flag, since the
    variable Claude Code itself documents already says the same thing.
  - **Real binary, through `contexto run claude` against a fake upstream** (zero spend,
    `tests/test_hint_headers_real_client.py`, 2.1.322). `-p "say hi"`: 1 model request, class `main`
    in the session log, 0 gated headers at the upstream, the session id forwarded. The same with the
    variable `1` in the environment: the upstream got `x-claude-code-request-class` and
    `x-claude-code-prompt-id`. A headless session (a prompt that launches a subagent, `/compact`, a
    prompt): 7 model requests, classed `main`, `subagent`, `main`, `main`, `compaction`,
    `compaction`, `main`; the last carried `context_compacted: manual` and logged `main_changed:
    compacted`; the subagent's request was not main; 0 gated headers on the 7 upstream requests; no
    `hint_mismatch`, so on 2.1.322 each signature still agrees with its header. The same three
    tests pass with the 2.1.318 and 2.1.319 binaries first on PATH. Added after review, on 2.1.322:
    a launch with the environment a nested launch inherits (the variable at `1`, the marker
    `stripped`) sent the upstream 0 gated headers and logged `stripped`; with the marker `forwarded`
    the upstream got the class; and with the variable at `0` Claude Code sent no hint header, the
    launch logged `off`, and the main request was still found by its tool list.
  - **Cloud-provider guard: the problem.** With `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`,
    `CLAUDE_CODE_USE_FOUNDRY`, `CLAUDE_CODE_USE_MANTLE` or `CLAUDE_CODE_USE_ANTHROPIC_AWS` set to
    `1`, every request went to the provider's stub and 0 to `ANTHROPIC_BASE_URL`, not even
    `HEAD /api/hello` (5 runs, one per switch), also when the switch was only in the config
    directory's `settings.json`. `contexto run claude` would start its proxy, print its session
    line, and bound nothing.
  - **Which values are on (measured for this slice).** `CLAUDE_CODE_USE_BEDROCK` in the process
    environment: `1`, `true`, `yes`, `on`, `TRUE` and `" 1 "` sent 5 requests to the Bedrock stub and
    0 to the base URL; `0`, `false`, the empty string, `no`, `2` and `enabled` sent 2 to the base URL
    (`HEAD /api/hello` and the Messages request) and 0 to Bedrock. That is the rule Claude Code uses
    for `CLAUDE_CODE_IGNORE_PERSISTED_TRUST` (D52): `1`, `true`, `yes` or `on`, lower-cased and
    trimmed. In the config directory's `settings.json` `env` block: `"true"`, a JSON `true` and the
    number `1` were on, `"0"` off. Environment `1` with settings `0` was off, and environment `0`
    with settings `1` on. The other four switches: on for `true`, off for `0`. After review (15
    runs): a settings value that is not a string is read as JavaScript's `String()` of it. On:
    `1.0`, `1e0`, `["1"]`, `[1]`, `[["1"]]`, `[true]`, `" On "`, and `"1"` in a file that starts with
    a UTF-8 byte order mark. Off: `false`, `1.5`, `[]`, `["1","1"]`, `null`, an object, `"1.0"`. In a
    project file (8 runs, `claude -p`, no trust record): `"1"` in `.claude/settings.json` or
    `.claude/settings.local.json` is on, a project file's `"0"` wins over the environment's `1`,
    and the project file's `env` is dropped while `CLAUDE_CODE_IGNORE_PERSISTED_TRUST` is set.
  - **Decided: refuse.** `contexto run claude` exits 2 before it opens a session, naming the
    variable, each place it is on (the process environment or which settings file's `env` block)
    and what to do. `claude --version` and `--help` alone still run. The detection is
    `contexto.upstream.cloud_provider`, for any other caller.
  - **The check fails closed.** The first version predicted the value Claude Code would run with
    (first settings file that sets the switch, else the environment) and let the launch through
    when that was off. The review measured seven ways past it on 2.1.322: `1.0`, `1e0` and `["1"]`
    in a file, a file with a byte order mark, and the environment at `1` beside a project file at
    `"0"` that Claude Code did not apply (it also held `NaN`, or a schema error, or the arguments
    had `--setting-sources user`). Contexto cannot tell reliably which files Claude Code applies.
    Decided: refuse when a switch is on in the process environment or in any settings file
    Contexto can find, whatever the precedence between them and whatever the folder's trust state:
    the files of the base URL resolution (the main checkout's and the folder's project files and
    the user file) and the managed settings file (`/Library/Application
    Support/ClaudeCode/managed-settings.json` on macOS, `/etc/claude-code/managed-settings.json` on
    Linux; managed settings delivered another way are not seen). No variable moves that path: one
    could reach a nested launch from a project's settings `env` block, which Claude Code applies to
    every process a session starts, and hide the real file. A project file can only cause a
    refusal. A file value is on when `String()` of it, as above, is one of the four words; a number
    past the largest double reads as `Infinity`, as in JavaScript, which is off.
    Accepted costs: a refusal when the environment says on and a file Claude Code does apply says
    off (measured: Claude Code would let the file win); a refusal for a switch in a file Claude
    Code ignores (one it rejects, or a project file's under
    `CLAUDE_CODE_IGNORE_PERSISTED_TRUST` in `-p` mode); and a refusal when a settings file is there
    and Contexto could not read it (not a regular file, over 4 MiB, not valid JSON for Python),
    because such a file is not evidence that a switch is off. The hint decision reads the same
    files the same way, so a project file Claude Code ignores (one holding `NaN`, say) that turns
    the hints on makes a launch `forwarded`; a repository gains nothing by it that a valid file
    would not give it. A settings file that does not parse (a comment, a trailing comma) refuses
    only when its bytes could name a switch (they contain `CLAUDE_CODE_USE_`, a `\u` escape, or a
    NUL byte, which a wide encoding has): Claude Code does not load such a file (measured on
    2.1.322), so otherwise it turns nothing on. Claude Code does load UTF-16LE that starts with its
    byte order mark (measured; a switch on there sent every request to Bedrock), so Contexto reads
    that encoding too; UTF-16BE, UTF-16LE without the mark and UTF-32 it does not load.
    One more change to the base URL's resolution than the byte order mark and the size bound: a
    settings file nested deeper than 512 levels is unreadable for the bounded loader, so a base URL
    in it is not used and the next file's is; Claude Code would read it. No real settings file nests
    that deep. A file that does not exist, or that
    this user cannot open, passes: Claude Code cannot read it either. The message names what to
    unset or fix.
  - **Settings files are read once, with a bound.** One reader for every Claude Code settings
    file Contexto reads (the base URL, `apiKeyHelper`, the provider switches, the hint setting): a
    regular file only, at most 4 MiB, a UTF-8 byte order mark accepted as Claude Code accepts one,
    parsed with the bounded JSON loader. `contexto run claude` reads each file once per launch for
    all of them.
  - **Rejected for the guard:** a warning (the session line
    still says "bound 150,000" and the user reads no further); forcing the switch to `0` in
    Contexto's `--settings` (it would send a Bedrock user's requests to the Anthropic API with
    credentials they may not have); pointing `ANTHROPIC_BEDROCK_BASE_URL` at the proxy (the Bedrock
    and Vertex request paths and signing are not the Messages API the proxy speaks).
  - **Not confirmed.** The class `workflow`, the triggers `auto` and `reactive`, nested agents, and
    a subagent's own compaction or side requests were not provoked, so whether a workflow agent
    carries an agent id is not known; its class keeps it out either way. A drifted summary was
    tested by altering the captured text, since no Claude Code release has another one. Claude Code
    2.1.302 to 2.1.314 were not run with the hints on; the docs say 2.1.273 and later send them, and
    an older one falls back to the signatures. Sessions signed in with a claude.ai account, the
    interactive TUI through `contexto run`, and Linux were not run. No run used a managed settings
    file: that it outranks the other files and `--settings` is from the documentation, and managed
    settings that are not in that file are not seen. `CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST` was not
    measured. Whether the interactive CLI applies a project file's provider switch after its trust
    dialog was not measured (D52 measured it for the base URL); the guard refuses either way.
    Vertex, Foundry, Mantle and Claude Platform on AWS were measured with `1`, `true` and `0`
    only, and the value shapes with Bedrock in the user file only (the review measured `1.0`,
    `1e0`, `["1"]` and a byte order mark in a project file too). How Claude Code reads a settings
    file whose JSON Python rejects was not measured; the guard refuses on such a file. A
    pending-call compaction whose key a side request used first was built in a test, not provoked
    with a real client. 2.1.322 is not in
    `TESTED_VERSIONS`: the routine of `docs/cli-updates.md` was not run on it here.

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
- **O6. Upstream affinity on the owner's proxy.** Resolved by D36 (a consistent-hash session
  affinity option for CLIProxyAPI, proposed upstream and deployed on the owner's proxy 2026-10-24,
  and Contexto's `X-Session-Affinity`; verified for cache reuse, the dropped-thinking check has
  not been run); kept
  here for its evidence. (Later, 2026-10-25 UTC, PR #220: the dropped-thinking check ran; see the
  note under D36 and `docs/research/O6-thinking-check.md`.) Measured: full cache misses with
  byte-identical prefixes (4 of 14 eligible throwaway calls) and thinking dropped on every request
  (`end_user_binding_mismatch`) through the owner's proxy. Lane 1's black-box test (L1-e,
  81,319 tokens): over 10 haiku requests the keep/drop pattern falls into exactly 3 consistent
  classes, and a byte-identical series shows exactly 3 cache writes before steady reads (counts
  measured; reported by manager-1, doc pending in lane 1's L1-f PR). Inferred: the route spreads
  requests across at least 3 upstream identities, and each byte-identical miss is the first request
  to reach an identity. Opus over 5 requests kept all thinking (inconclusive). At the time the fix
  was left to the owner, since intake did not touch the proxy (D11); D36's owner request covers the
  proxy-side work.
- **O7. OpenRig context-usage clock bug.** Resolved upstream: OpenRig fixed it in its PR #124
  (commit 4a920dba), first released in v0.6.1, so no report is needed. The host was upgraded to
  OpenRig 0.7.0 on 2026-10-25, which includes the fix. Not Contexto code. Kept here for its
  evidence. Measured by lane 1: every seat's context usage reads `stale_generation` because tenure
  boot times are stored in UTC without `Z` and parsed as local time (UTC-5), blinding `rig ps`,
  `whoami`, and compaction planning for about 5 hours after boot.

