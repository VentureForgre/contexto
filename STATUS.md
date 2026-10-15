# Contexto status

Updated 2026-10-15 by intake.lead.

## Current milestone

M1: durable ledger, checkpoints, and recovery. Contract: `docs/milestones/M1-ledger-and-recovery.md`.

- Lane A (ledger, model, CLI): management.manager-1 with build.builder-1 and build.builder-2.
- Lane B (state, checkpoint, resume, recovery harness): management.manager-2 with build.builder-3 and build.builder-4.
- Reviewers per rig spec edges: builder-1 and builder-3 to review.reviewer-1, builder-2 and builder-4 to review.reviewer-2.

## Verified on main

Only the charter. No product code exists yet.

## Open obligations

- Lane A first PR: `contexto.model` plus scaffolding, which unblocks lane B integration.
- Intake: integrate reviewed PRs, run the end-to-end CLI scenario on main, record results here.

## Known issues

- Live manager seats run with cwd set to the main checkout, while the rig spec declares per-seat
  worktrees. Seats must not switch branches in the main checkout; git work happens in each seat's
  worktree under `contexto-worktrees/`.
