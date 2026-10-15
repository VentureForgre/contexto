# Contexto status

Updated 2026-10-15 by intake.lead.

## Current milestone

Decisions taken and decisions still open, with reasons: `docs/DECISIONS.md`.

M0: infinite-session design study (2026-10-15). The owner described the problem and asked the team
to look; intake framed it as M0. Brief: `docs/milestones/M0-infinite-session-study.md`. Success bar
(owner): compaction never fires and accuracy equals full context.

- Lane 1 (interception surfaces, test seats): management.manager-1. PRs #9, #10 in review.
- Lane 2 (policy, cost, fidelity): management.manager-2. PRs #7, #8 in review; probe design ready.
- Phase 2 (rewriting prototype) is authorized (DECISIONS D12) and starts after phase 1 synthesis.

M1 (ledger, checkpoints, recovery; contract on main) is paused. Open PRs #2, #3, #4 stay as they
are until decision O1.

## Verified on main

- Charter and the M1 contract (PR #1, reviewed at 040318c). No product code exists yet.

## Verified repository settings (2026-10-15)

- Repository lives at VentureForgre/contexto (private). Ruleset `main: require pull request`
  (id 25202810) is active: PR required, no force-push, no deletion, no bypass. Intake verified
  enforcement with a direct push rejected (GH013, "Changes must be made through a pull request")
  against an identical temporary ruleset on a scratch branch; both were deleted afterward.

## Open obligations

- Lane 1 (manager-1): `docs/research/` interception findings and measuring-proxy data.
- Lane 2 (manager-2): `docs/research/` policy, cost model, fidelity test design and baseline.
- Release automation REL-1 (manager-2): CI and release workflows; no real release until
  decision O5.
- Intake: M0 recommendation (docs/research), then phase 2 plan. Q5 settled for development by D12.
- Parked on gate qitem-20261015222349-14b703cb until intake decides O1: M1 lane rows, PRs #2,
  #3, #4, and their review rows.

## Test seats

- `lab.subject` (Claude Code, claude-haiku-5-5, worktree `contexto-worktrees/lab-subject`) and
  `labcodex.subject` (Codex, gpt-6-astra, owner's ChatGPT login) are measured subjects owned by
  manager-1. Their agent specs live outside the repo in the OpenRig spec directory.

## Known issues

- Only intake runs in the main checkout; the other ten seats (eight team seats and two lab
  subjects) run in their own worktrees under `contexto-worktrees/`. Never switch branches in the main
  checkout.
- PR approvals are not required by GitHub because every seat uses one GitHub account. Exact-head
  review by a reviewer seat is a team rule.
