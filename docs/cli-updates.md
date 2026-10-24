# When Claude Code or Codex updates

Contexto recognises parts of each CLI's requests by text the CLI writes. A CLI update can change that
text without any error: the message falls into another class and may be paged when it should be
carried, or carried when it could be paged. This page lists what Contexto depends on, what warns, and
the routine for testing a new version (D38).

## Tested versions

The single list is `TESTED_VERSIONS` in `src/contexto/cliversions.py`, oldest to newest:

| CLI | Tested | What was tested on it |
|---|---|---|
| Claude Code | 2.1.302 | session-start signature (D33), context-block kinds (D32), missing-tools notices, live acceptance run #3 |
| Codex | 0.154.0 | Responses shapes and context sections, Codex acceptance (D30) |
| Codex | 0.163.0 | MCP server launch and missing-tools check (v0.2.8); context sections rechecked for D38 |

Hook-output signatures were measured on Claude Code 2.1.301 (`HOOK_SIGNATURES_CLAUDE_CODE_VERSION`)
and not re-measured on 2.1.302.

## What Contexto reads from the CLI's text

| Constant | File | Measured by |
|---|---|---|
| `SESSION_START_SIGNATURE` (`# Environment`), `SESSION_START_SIGNATURE_CLAUDE_CODE_VERSION` | `src/contexto/rewrite.py` | `spikes/m2/scale/reminders` |
| `CONTEXT_BLOCK_KINDS` (first lines inside `<system-reminder>`), `CONTEXT_BLOCK_KINDS_CLAUDE_CODE_VERSION` | `src/contexto/rewrite.py` | `spikes/m2/scale/reminders` |
| `HOOK_SIGNATURES`, `HOOK_SIGNATURES_CLAUDE_CODE_VERSION` | `src/contexto/rewrite.py` | `spikes/m2/carried` (`live_probe.sh`, `hook_context.py`) |
| deferred-tool listings and MCP notices | `src/contexto/toolcheck.py` | zero-spend captures described in its docstring |
| `CONTEXT_SECTION` (Codex section tags), `CONTEXT_SECTIONS_CODEX_VERSION` | `src/contexto/codex.py` | `spikes/m2/codex/probe.sh` |

If the `# Environment` heading changes, the session-start message becomes a settings change or
carried text and can be paged once superseded (D33). If a context block's first line changes, the
fallback no longer keeps its latest copy (D32). If Codex renames its section tags, its
`<environment_context>` message becomes a turn start and is paged with the first turn.

## What warns

- At launch, `contexto run claude|codex` runs `<cli> --version` (3-second timeout, stdin closed). For a
  version not in the list it prints one line on stderr, `contexto: warning: Claude Code <v> is not a
  version Contexto was tested with (tested: ...)`, with what may break and where to report it. The
  launch always goes on. A CLI that does not answer or prints no version is logged, not warned about.
  `--no-version-check` or `CONTEXTO_NO_VERSION_CHECK=1` skips the check. The session log gets a
  `cli_version_check` event (`version`, `tested`, `tested_versions`, and `error` or `skipped`).
- In the proxy, every request's `User-Agent` is read. Claude Code 2.1.302 sends
  `claude-cli/2.1.302 (external, sdk-cli)`; codex-cli 0.163.0 sends `codex_exec/0.163.0 (...)`.
  Each distinct version is logged once as `cli_version`, and the first untested one as
  `cli_version_untested`.
- On the main conversation's first request with a tool list (for Codex, its first request), the proxy
  checks that the session start was recognised. Claude Code: a role `system` message with the
  `# Environment` signature, or a context block of kind `environment`. Codex: a user message of
  context sections before the first prompt. When the request carries the CLI's injected context but
  no recognised session start, the session log gets one `session_start_unrecognised` event
  (`dialect`, `signature_version`, `cli_version`).

These runtime events go to the session log only (`contexto sessions show <id>`), since a line printed
mid-run can be drawn over by the CLI's interface.

## Routine for a new Claude Code version

Everything here is zero spend unless marked.

1. First check (about 1.5 minutes): `PATH="$PWD/.venv/bin:$PATH" spikes/m2/cliversion/check.sh`. It
   should report the new version as not tested, `session start recognised: True`, and no
   `no recognised kind` lines. A `False` or an unrecognised first line means the CLI changed the text
   Contexto reads; stop and look at the captured body before going on.
2. Reminders spike (about 1 minute): `spikes/m2/scale/reminders/run_all.sh 48`. It drives
   `~/.local/bin/claude`. Compare the new `results/` with the committed ones: in `features.md` and
   `summary.md`, the session-start environment message is still classed `session_start` on the 5.x
   ids and `# Environment` is still a context block in `messages[0]` on the 4.x ids. Every context
   block's first line still has a kind (`CONTEXT_BLOCK_KINDS`). Note new kinds and changed per-turn
   placement.
3. Hooks, output styles, plan mode, effort, MCP changes: `spikes/m2/carried/live_probe.sh` (zero
   tokens, against a fake model). Hook outputs must still start with the `HOOK_SIGNATURES` prefixes.
4. Missing-tools notices: if the CLI's MCP or tool-search text changed in step 1 or 2, recapture the
   shapes `src/contexto/toolcheck.py` describes (tool search on, a resume with the server off PATH)
   and update `tests/test_toolcheck.py`.
5. The suite: `pytest -q`.
6. If nothing changed: append the version to `TESTED_VERSIONS["claude"]` and bump
   `SESSION_START_SIGNATURE_CLAUDE_CODE_VERSION` and `CONTEXT_BLOCK_KINDS_CLAUDE_CODE_VERSION` to it,
   in the same commit. `tests/test_cliversions.py` fails if the newest tested version and those
   constants differ. Bump `HOOK_SIGNATURES_CLAUDE_CODE_VERSION` only if step 3 ran. Add the version
   to the table above.
7. If something changed: update the signatures and their tests, re-run step 2 to measure what is
   carried and paged with the new text, and record a decision in `docs/DECISIONS.md`.
8. Live acceptance (spends tokens, needs the owner's approval): `spikes/m2/acceptance`, as in live run
   #3 (`docs/research/M2-acceptance-run3.md`). Only for a change in step 7.

## Routine for a new Codex version

1. First check: `spikes/m2/cliversion/check.sh` (both CLIs). It should report
   `session start recognised: True`, and every lead item before the first prompt should show only
   `section` parts, apart from the base instructions. A user `<environment_context>` message that
   shows as a `turn` means the section tags changed.
2. Shapes: `spikes/m2/codex/probe.sh` (written for 0.154.0; it drives `~/.local/bin/codex`). Check the
   section openings `CONTEXT_SECTION` matches, the headers, and the provider behaviour listed in
   `docs/research/M2-codex-adapter.md`.
3. MCP launch: `spikes/m2/codex/smoke.sh preflight` (zero tokens: `contexto run codex` through the
   proxy to `responses_stub.py` with a dummy key helper). The session log should have no
   `mcp_tools_missing`, which shows the MCP server started and answered `tools/list`. Its `turn`
   mode spends tokens and needs the owner's approval.
4. The suite: `pytest -q`.
5. If nothing changed: append the version to `TESTED_VERSIONS["codex"]` and bump
   `CONTEXT_SECTIONS_CODEX_VERSION` to it, in the same commit, and add it to the table above.
6. If something changed: update `CONTEXT_SECTION` and `tests/test_codex.py`, and record a decision.
