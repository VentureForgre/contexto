# Contexto status

Updated 2026-10-15 by intake.lead.

## Current milestone

M0: infinite-session design study, set by the owner on 2026-10-15. Brief:
`docs/milestones/M0-infinite-session-study.md`.

- Lane 1 (interception surfaces): management.manager-1.
- Lane 2 (policy and economics): management.manager-2.

M1 (ledger, checkpoints, recovery; contract on main) is paused. Open PRs #2, #3, #4 stay as they
are until the owner decides on them after M0.

## Verified on main

- Charter and the M1 contract (PR #1, reviewed at 040318c). No product code exists yet.
- Repository lives at VentureForgre/contexto (private). Ruleset `main: require pull request` is
  active: PR required, no force-push, no deletion, no bypass. Enforcement was verified with a
  rejected direct push against an identical temporary ruleset on a scratch branch.

## Known issues

- Only intake runs in the main checkout; the other eight seats run in their own worktrees under
  `contexto-worktrees/` (pane paths checked 2026-10-15 22:22Z). Never switch branches in the main
  checkout.
- PR approvals are not required by GitHub because every seat uses one GitHub account. Exact-head
  review by a reviewer seat is a team rule.
