# Contexto status

Updated 2026-10-15 by intake.lead.

## Current milestone

M0: infinite-session design study (2026-10-15). The owner described the problem and asked the team
to look; intake framed it as M0. Brief:
`docs/milestones/M0-infinite-session-study.md`.

- Lane 1 (interception surfaces): management.manager-1.
- Lane 2 (policy and economics): management.manager-2.

M1 (ledger, checkpoints, recovery; contract on main) is paused. Open PRs #2, #3, #4 stay as they
are until the owner decides on them after M0.

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
- Release automation REL-1 (manager-2): CI and release workflows, no real release until the owner
  agrees.
- Intake: one M0 recommendation to the owner, including the Q5 auth decision.
- Parked on gate qitem-20261015222349-14b703cb until the owner decides M1: M1 lane rows, PRs #2,
  #3, #4, and their review rows.

## Known issues

- Only intake runs in the main checkout; the other eight seats run in their own worktrees under
  `contexto-worktrees/` (pane paths checked 2026-10-15 22:22Z). Never switch branches in the main
  checkout.
- PR approvals are not required by GitHub because every seat uses one GitHub account. Exact-head
  review by a reviewer seat is a team rule.
