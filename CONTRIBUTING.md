# Contributing to Contexto

## Project

Contexto makes long-running sessions recoverable and useful after context-window turnover, runtime
restarts, and work handoffs. It should preserve decisions, active obligations, provenance, and
enough verified state for a successor to continue without treating a summary as a complete
transcript.

Design records live in `docs/`: milestone contracts in `docs/milestones/` (the proxy is
`docs/milestones/M2-contexto-proxy.md`), decisions in `docs/DECISIONS.md`, and measurements in
`docs/research/`. `STATUS.md` says where the project is.

## How the work runs

Since 2026-10-17 one Claude Code session orchestrates the project for the owner. It splits work
into tasks and gives each to a builder subagent. Each builder works in its own Git worktree on its
own `claude/<topic>` branch from `origin/main`, runs the full test suite, and opens a pull request.
Two independent reviewer agents then check the pull request at its exact head commit and post their
verdicts as PR comments. A pull request merges only after both reviewers approve that head; a new
push needs new verdicts. The project has one owner, and the builder and reviewer agents post from
the owner's account, so GitHub cannot count their approvals; the two verdicts are a project rule.

Contexto is MIT licensed ([LICENSE](LICENSE)). By opening a pull request you agree that your
contribution is licensed under the same terms.

Pull requests from forks go through the same review. Workflow runs on pull requests from forks
need a maintainer's approval (the repository requires approval for all external contributors).

Before 2026-10-17 the work ran on an OpenRig rig (an intake lead, two managers, four builders, and
two reviewers, each in its own worktree). That rig was taken down; older records in `docs/` and
`STATUS.md` refer to its seats by name.

## Working on the code

Contexto is a Python 3.12+ package with no runtime dependencies, built with `uv_build`.

```sh
uv run contexto --help                # run from the checkout
uv run --with pytest pytest -q        # the test suite (tests/ and spikes/)
uv build                              # the sdist and wheel, into dist/
```

Without `uv`, a plain virtual environment works:

```sh
python3.12 -m venv .venv
.venv/bin/pip install -e . pytest
.venv/bin/pytest -q
```

The suite runs on macOS and Linux. Windows is not supported (see the README's Platforms note).

CI (`.github/workflows/ci.yml`) runs on GitHub-hosted runners. The lint job runs `ruff check` with
the rules in `pyproject.toml` and the Ruff version pinned in the workflow; run
`uvx ruff@0.17.2 check .` before pushing. `ruff format` is not enforced. The test job runs
`uv run --with pytest==9.1.1 pytest` (pytest pinned like Ruff) and `uv build` on Python 3.12, 3.13,
3.14, and 3.15 on Ubuntu, and on 3.12 and 3.15 on macOS. The `ci` job is the one required check:
it passes only when lint and every test job pass.

## Pull requests

- Every change to `main` goes through a pull request; never push to `main` directly. GitHub
  branch-rule enforcement depends on the account plan and is verified separately from this policy.
- Use Conventional Commits for commit messages and pull request titles. Releases are computed from
  them: `feat:` and `fix:` are for user-facing changes in `src/`, `docs:` for documentation.
- Stage files by explicit path and check `git diff --stat origin/main...HEAD` before pushing.
- Do not commit proxy keys, tokens, local Claude or Codex settings, raw private transcripts, or
  generated runtime state such as `~/.contexto` session data.
- Report security problems privately, not in a pull request or issue: see
  [SECURITY.md](SECURITY.md).

## Releases

Releases are automated from Conventional Commits on `main`. [RELEASING.md](RELEASING.md)
describes the workflow, the version rules, and recovery.
