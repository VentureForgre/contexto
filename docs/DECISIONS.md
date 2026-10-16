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

- **O2. History-edit strategy under preserved thinking.** Vendor-documented: on Fable 5.1, Opus 5.5,
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
- **O3. Working-window bound.** Provisional default 150,000 (M2 contract), with the launcher
  refusing more than 170,000 to stay under Claude Code's 200,000 idle-compaction minimum (the premise
  that idle compaction counts the bounded reported usage is inferred; acceptance run 1 verifies it). Evidence:
  every M0 probe hit its ceiling on stated facts (96/96 at 98,910 and 256,783 on `lab.subject`;
  96/96 in the replay at 40,749 and 91,861), while deep supersession chains failed even at small
  contexts (5/8 at about 12k; one depth-8 miss in 29/30 at 15,002; PR #22). So no slip point by
  context size was located, and the failures that do occur argue for an explicit current-state
  block rather than for a smaller window. M2 acceptance measures H1 against the full-context
  baseline; the deferred 852,610-token run can be revived if the bound needs more evidence.
  Heads: `lab.subject` 81,808 to 84,073 (plain Claude Code 45,925), Codex 32,331 after onboarding.
  Large heads leave little room: an OpenRig seat (fixed overhead near 92k) gets a window budget
  of about 33k at 150,000, so the bound for seat use should be weighed upward, toward the 170,000
  launcher cap, once acceptance shows how the idle-compaction premise holds.
- **O4. End-user authentication guidance.** API key only, or also subscription logins, for people
  who run Contexto themselves. Needs the terms question answered in the product docs. Owner
  decision, because it bears on what may be shared beyond this repository.
- **O5. First real release.** The release workflow runs dry until a release is cut. A GitHub Release
  on this private repository stays inside the repository, so it is within D0. Lean: cut v0.1.0
  when the first working Contexto build merges with passing CI.
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

