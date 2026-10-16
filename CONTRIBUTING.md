# Contributing to Contexto

## Project

Contexto makes long-running sessions recoverable and useful after context-window turnover, runtime
restarts, and work handoffs. It should preserve decisions, active obligations, provenance, and
enough verified state for a successor to continue without treating a summary as a complete
transcript.

Design records live in `docs/`: milestone contracts in `docs/milestones/` (the proxy is
`docs/milestones/M2-contexto-proxy.md`), decisions in `docs/DECISIONS.md`, and measurements in
`docs/research/`. `STATUS.md` says where the project is.

## Development rig

OpenRig rig: `contexto`. One native Claude Code intake lead (`intake.lead`), two middle-management
seats (`management.manager-1`, `management.manager-2`), four builders (`build.builder-1` through
`build.builder-4`), and two reviewers (`review.reviewer-1`, `review.reviewer-2`). All nine seats use
`claude-opus-5-5` through the existing Claude proxy. Intake, managers, and reviewers use high
reasoning; builders use medium. The proxy credential is held outside this repository.

Work flows from intake to managers, from managers to builders, and from builders to independent
reviewers. Intake coordinates integration and reports verified outcomes. Each specialist has a
separate Git worktree. The durable OpenRig spec lives outside this repository at
`~/.openrig/workspace/specs/contexto/rig.yaml`.

```sh
rig ps --nodes --rig contexto --json
rig send 'intake-lead@contexto' 'Describe the next milestone and delegate it through the managers.'
# Attach to the existing OpenRig-owned intake session:
# tmux attach -t 'intake-lead@contexto'
```

## Working on the code

Contexto is a Python 3.12+ package with no runtime dependencies, built with `uv_build`.

```sh
uv run contexto --help                # run from the checkout
uv run --with pytest pytest -q        # the test suite (tests/ and spikes/)
uv build                              # the sdist and wheel, into dist/
```

CI (`.github/workflows/ci.yml`) runs on Python 3.12: Ruff when the repository has a Ruff
configuration, `uv run --with pytest pytest`, and `uv build`. The `ci` job requires both to pass.

## Pull requests

- Every change to `main` goes through a pull request; never push to `main` directly. GitHub
  branch-rule enforcement depends on the account plan and is verified separately from this policy.
- Use Conventional Commits for commit messages and pull request titles. Releases are computed from
  them: `feat:` and `fix:` are for user-facing changes in `src/`, `docs:` for documentation.
- Stage files by explicit path and check `git diff --stat origin/main...HEAD` before pushing.
- Do not commit proxy keys, tokens, local Claude or Codex settings, raw private transcripts, or
  generated runtime state such as `~/.contexto` session data.

## Releases

Releases are automated from Conventional Commits on `main`. [RELEASING.md](RELEASING.md) describes the
workflow, the version rules, and recovery.
