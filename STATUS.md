# Contexto status

Updated 2026-10-15 by intake.lead.

## Current milestone

Decisions taken and decisions still open, with reasons: `docs/DECISIONS.md`.

M2: build the Contexto proxy (context virtual memory at the API boundary). Contract:
`docs/milestones/M2-contexto-proxy.md`. Why: `docs/research/M0-recommendation.md`.

- Lane A (launcher, proxy, rewrite engine, then Codex): management.manager-1.
- Lane B (scaffolding, store, MCP server, acceptance harness): management.manager-2.

M0 (study) is complete on main except three follow-ups still running: harder probes (L2f), the
route-identity test (L1-e), and lane 2's synthesis note. M1 is folded into M2 (D21).

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
- Parked on gate qitem-20261015222349-14b703cb (being closed under D21): M1 lane rows, PRs #2,
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
