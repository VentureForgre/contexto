# Contexto

Contexto is a private project for managing very long-running AI sessions and their context. The repository is the source of truth for the product; the OpenRig team is the persistent development team that will design and build it.

## Mission

Make long-running sessions recoverable and useful after context-window turnover, runtime restarts, and work handoffs. A future implementation should preserve decisions, active obligations, provenance, and enough verified state for a successor to continue without treating a summary as a complete transcript. Keep raw logs, credentials, and private user data out of Git.

Initial design questions for the team:

- How should events, decisions, tasks, artifacts, and references be captured with stable identities and provenance?
- Which context should be retrieved for the next turn, and how is stale or conflicting context detected?
- How are bounded checkpoints built, validated against source material, and handed to a new session?
- How are handoff quality, retrieval precision, long-run continuity, and recovery after interruption tested?

These are product goals, not claims that the functionality is implemented yet.

## Development rig

OpenRig rig: `contexto`. One native Claude Code intake lead (`intake.lead`), two middle-management seats (`management.manager-1`, `management.manager-2`), four builders (`build.builder-1` through `build.builder-4`), and two reviewers (`review.reviewer-1`, `review.reviewer-2`). All nine seats use `claude-opus-5-5` through the existing Claude proxy. Intake, managers, and reviewers use high reasoning; builders use medium. The proxy credential is held outside this repository.

Work flows from intake to managers, from managers to builders, and from builders to independent reviewers. Intake coordinates integration and reports verified outcomes. Each specialist has a separate Git worktree. The durable OpenRig spec lives outside this repository at `~/.openrig/workspace/specs/contexto/rig.yaml`.

```sh
rig ps --nodes --rig contexto --json
rig send 'intake-lead@contexto' 'Describe the next milestone and delegate it through the managers.'
# Attach to the existing OpenRig-owned intake session:
# tmux attach -t 'intake-lead@contexto'
```

Do not commit proxy keys, tokens, local Claude settings, raw private transcripts, or generated runtime state. Contributions should go through pull requests targeting `main`. GitHub branch-rule enforcement depends on the account plan and is verified separately from this repository policy.
