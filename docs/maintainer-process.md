# How the maintainer runs the project

Contexto has one maintainer, who runs most of the work through AI coding agents. This note explains
the records that process leaves behind, such as the reviewer comments on pull requests and the
role names in older documents.

## Since 2026-10-17

One Claude Code session orchestrates the work for the maintainer. It splits work into tasks and
gives each to a builder subagent. Each builder works in its own git worktree on its own
`claude/<topic>` branch from `origin/main`, runs the full test suite, and opens a pull request.

Two independent reviewer agents then check the pull request at its exact head commit and post their
verdicts as comments ("Reviewer A verdict", "Reviewer B verdict"). A pull request merges only after
both approve that head; a new push needs new verdicts. The agents post from the maintainer's
account, so GitHub cannot count their approvals as reviews; the two verdicts are a project rule, not
a GitHub setting. Pull requests from outside contributors get the same two verdicts and the
maintainer's review.

## Before 2026-10-17

The work ran on an OpenRig rig: an intake lead, two managers, four builders and two reviewers, each
an agent in its own worktree. That rig was taken down. Older records in `docs/` and `STATUS.md`, and
some source comments, refer to its roles by name (for example "intake", "manager-1", "builder-2",
"reviewer-1"), and the decision log cites its decisions as D-numbers.

## Where decisions are recorded

- `docs/DECISIONS.md`: every decision, with its reason and measurements, numbered D0, D1, and so on.
- `docs/milestones/`: the design contracts. M2 (`M2-contexto-proxy.md`) is the proxy that ships.
  M1 (`M1-ledger-and-recovery.md`, durable handoff between sessions) is a contract that was not
  built.
- `STATUS.md`: where the project is, what is open, and the measured results.
