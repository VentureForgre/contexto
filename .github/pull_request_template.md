<!--
Title: a Conventional Commit, for example `fix: recall reads a page written by an older release`.
The title becomes the squash commit on main and decides the release: `feat:` is a minor release,
`fix:` and `perf:` a patch, and `docs:`, `test:`, `build:`, `ci:`, `chore:` and `refactor:` none.
-->

## What was wrong or missing

## What changed

## How it was tested

- [ ] `uv run --with pytest pytest -q` passes
- [ ] `uvx ruff@0.17.2 check .` passes
- [ ] A test that fails without this change, for a bug fix

## Notes

<!-- Anything left open, and docs that need to change. Never paste session data, keys or tokens. -->
