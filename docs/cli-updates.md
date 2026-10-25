# When Claude Code or Codex updates

Contexto recognises parts of each CLI's requests by text the CLI writes. A CLI update can change that
text without any error: the message falls into another class and may be paged when it should be
carried, or carried when it could be paged. This page lists what Contexto depends on, what warns, and
the routine for testing a new version (D38).

## Tested versions

The single list is `TESTED_VERSIONS` in `src/contexto/cliversions.py`, oldest to newest:

| CLI | Tested | What was tested on it |
|---|---|---|
| Claude Code | 2.1.302 | session-start signature (D33), context-block kinds (D32), missing-tools notices, the permission refusal of Contexto's tools in `-p` mode (D44), live acceptance on Claude Opus 5.5 (PR #221, [M2-acceptance-opus.md](research/M2-acceptance-opus.md); live run #3 ran on 2.1.301) |
| Claude Code | 2.1.310 | steps 1 to 6 below at zero spend, and the D44 permission check ([M2-claude-code-2.1.310.md](research/M2-claude-code-2.1.310.md)); tool search is on by default, and two of its shapes are read differently since D45; no live run |
| Codex | 0.154.0 | Responses shapes and context sections, Codex acceptance (D30) |
| Codex | 0.163.0 | MCP server launch and missing-tools check (v0.2.8); context sections and request shapes rechecked for D38 (Codex steps 1 to 4 below) |

Hook-output signatures were measured on Claude Code 2.1.301 and re-measured on 2.1.310
(`HOOK_SIGNATURES_CLAUDE_CODE_VERSION`), not on 2.1.302.

## What Contexto reads from the CLI's text

| Constant | File | Measured by |
|---|---|---|
| `SESSION_START_SIGNATURE` (`# Environment`), `SESSION_START_SIGNATURE_CLAUDE_CODE_VERSION` | `src/contexto/rewrite.py` | `spikes/m2/scale/reminders` |
| `CONTEXT_BLOCK_KINDS` (first lines inside `<system-reminder>`), `CONTEXT_BLOCK_KINDS_CLAUDE_CODE_VERSION` | `src/contexto/rewrite.py` | `spikes/m2/scale/reminders` |
| `HOOK_SIGNATURES`, `HOOK_SIGNATURES_CLAUDE_CODE_VERSION` | `src/contexto/rewrite.py` | `spikes/m2/carried` (`live_probe.sh`, `hook_context.py`) |
| deferred-tool listings and MCP notices | `src/contexto/toolcheck.py` | zero-spend captures described in its docstring |
| the refusal of a tool no permission rule allows ("... but you haven't granted it yet.", D44) | `src/contexto/toolcheck.py` | `spikes/m2/permissions/permstub.py` (D44) |
| `CONTEXT_SECTION` (Codex section tags), `CONTEXT_SECTIONS_CODEX_VERSION` | `src/contexto/codex.py` | `spikes/m2/codex/probe.sh` |
| subagent shapes (`AGENT_TOOLS`, `ASYNC_LAUNCH`, `HAND_BACK`, `NOTICE_OPENINGS`, the `<task-notification>` and `resumedAgentId` patterns), gated by `SUBAGENT_SHAPES_TESTED` (D40) | `src/contexto/rewrite.py`, `src/contexto/cliversions.py` | `spikes/m2/subagents` |

The subagent shapes are gated by version: when the latest Claude Code version seen in a request's
`User-Agent` is not in `SUBAGENT_SHAPES_TESTED`, side rooms only expire (15 minutes) and none closes
early. Step 5 of the routine below re-measures them.

If the `# Environment` heading changes, the session-start message becomes a settings change or
carried text and can be paged once superseded (D33). If a context block's first line changes, the
fallback no longer keeps its latest copy (D32). If Codex renames its section tags, its
`<environment_context>` message becomes a turn start and is paged with the first turn.

## What warns

- At launch, `contexto run claude|codex` runs `<cli> --version` in its own process group, with stdin
  closed. It reads at most 4 KB of output, until a line with a version, end of file, or 3 seconds,
  then kills the process group. For a version not in the list it prints one line on stderr:
  `contexto: warning: Claude Code <v> is not a version Contexto was tested with (tested: ...)`, with
  what may break and where to report it. The launch always goes on. A CLI that does not answer or
  prints no version is logged, not warned about. `contexto run --no-version-check <cli>` (the flag goes
  before the CLI's name) or `CONTEXTO_NO_VERSION_CHECK=1` skips the check. Ctrl+C during the check
  stops the launch with exit code 130, before the CLI starts. The session log gets a
  `cli_version_check` event (`version`, `tested`, `tested_versions`, and `error` or `skipped`).
- In the proxy, every request's `User-Agent` is read. Claude Code 2.1.302 sends
  `claude-cli/2.1.302 (external, sdk-cli)` (2.1.310 the same with its version); codex-cli 0.163.0 sends `codex_exec/0.163.0 (...)`.
  Each distinct version is logged once as `cli_version`, and the first untested one as
  `cli_version_untested`.
- Once per proxy, the proxy checks that the main conversation's session start was recognised. It
  judges the first request that carries a tool list (any Codex request) and the CLI's injected
  context. For Claude Code that is a role `system` message or a `<system-reminder>` block; for Codex,
  a developer item before the first prompt. This is the main conversation's first request: Claude
  Code's title, topic, and quota side requests have no tool list, and subagents start from a main
  turn's tool call, so they come later. Recognised means: for Claude Code, a role `system` message
  with the `# Environment` signature or a context block of kind `environment`; for Codex, a user
  message of context sections before the first prompt. If neither is there, the session log gets one
  `session_start_unrecognised` event (`dialect`, `signature_version`, `cli_version`). A request with
  tools but no injected context leaves the check open, for up to 8 such requests.

These runtime events go to the session log only (`contexto sessions show <id>`), since a line printed
mid-run can be drawn over by the CLI's interface.

## Before you start

Run everything from the repository root, with a virtual environment at `.venv` (the spikes look for
`.venv/bin/python` and `.venv/bin/contexto` there):

```
python3.12 -m venv .venv
.venv/bin/pip install -q -e . pytest
export PATH="$PWD/.venv/bin:$PATH"
```

`check.sh` runs the `claude` and `codex` first on `PATH`, falling back to `~/.local/bin`, or the paths
you pass as arguments. `probe.sh` always drives `~/.local/bin/codex` and the reminders spike
`~/.local/bin/claude`; `smoke.sh` finds `codex` on a `PATH` of `.venv/bin`, `~/.local/bin`, `/usr/bin`
and `/bin`. They use temp homes, fake keys, and loopback stubs, so they read no credential
and reach no model. The shell may print `Alarm clock` lines: the spikes stop a CLI that keeps
retrying the stub's 401 with a timer, and that line is the timer firing, not a failure.

## Routine for a new Claude Code version

Everything here is zero spend unless marked.

1. First check (about a minute): `spikes/m2/cliversion/check.sh`. It should report the new version as
   not tested, `session start recognised: True`, and no `no recognised kind` lines. A `False` or an
   unrecognised first line means the CLI changed the text Contexto reads; stop and look at the
   captured body before going on.
2. Reminders spike (about a minute): `spikes/m2/scale/reminders/run_all.sh 48`. It ends by running
   `analyze_all.sh`, which rewrites the committed files in `spikes/m2/scale/reminders/results/`
   (`turns.csv` now holds the current classification); compare with `git diff`, and restore them
   (`git checkout -- spikes/m2/scale/reminders/results`) unless you are recording a new measurement.
   What to check:
   - `results/turns.csv` and `results/requests.csv`, column `session_start_msgs_k`: on the 5.x runs
     (`opus55-*`, `haiku55-resume`, `probe-claude-sonnet-5`, `probe-claude-fable-5`) it is 1 from
     turn 2 on, as in the committed `results/turns-d32-d33.csv` (the D33 classification).
     On the 4.x runs it is 0, because the environment is a context block there.
   - `results/summary.md`: every run still lists `# Environment`, under `system_untagged` or
     `system_role` on 5.x ids and under `m0` (a context block in `messages[0]`) on 4.x ids.
   - `results/reminders.csv` and `results/features.md`: every context block's first line still has a
     kind (`CONTEXT_BLOCK_KINDS`). Note new kinds and changed per-turn placement.
   - With tool search on (Claude Code's default from 2.1.310), the task tools and Contexto's MCP tools
     are deferred, so the scripted model skips the turns that call them (84 main requests instead of
     88 on `opus55-resume`). Run the matrix a second time with `--env ENABLE_TOOL_SEARCH=false` added
     to each `run_session.py` call, and compare that run with the previous version's.
3. Hooks, output styles, plan mode, effort, MCP changes: `spikes/m2/carried/live_probe.sh` (zero
   tokens, against a fake model). Hook outputs must still start with the `HOOK_SIGNATURES` prefixes.
4. Missing-tools notices: if the CLI's MCP or tool-search text changed in step 1 or 2, recapture the
   shapes `src/contexto/toolcheck.py` describes (tool search on, a resume with the server off PATH)
   and update `tests/test_toolcheck.py`.
5. Subagent shapes (zero tokens, against the stub; D40): `spikes/m2/subagents/run.sh` with
   `esc_scenario.py` (`SCEN_MODE=fg` and `SCEN_MODE=stop`) and with `resume_scenario.py`, then
   `spikes/m2/subagents/drive.py` in interactive mode (`bgidle` and `esc`). Compare the captures with
   the spike's README: the subagent's first prompt, the launch receipt and the hand-back, the
   `<task-notification>` notices in a `system` message and in a `<system-reminder>` user message, and
   a resume's `resumedAgentId`, new turn start, and `SendMessage`-named notice. Check also that the
   subagent's environment message is still `session_start` (D45: with tool search on it starts with
   the deferred-tool listing). Run the scenarios one at a time: `run.sh` and `drive.py` pick a random
   port from a small range, and two runs at once can reach each other's stub. Append the version to
   `SUBAGENT_SHAPES_TESTED` only if they are unchanged; otherwise update the shapes in
   `src/contexto/rewrite.py` and `tests/test_subagent_room.py` and record a decision. Leaving the
   version out is safe: side rooms then only expire.
6. The suite: `pytest -q`.
7. If nothing changed: append the version to `TESTED_VERSIONS["claude"]` and bump
   `SESSION_START_SIGNATURE_CLAUDE_CODE_VERSION` and `CONTEXT_BLOCK_KINDS_CLAUDE_CODE_VERSION` to it,
   in the same commit. Both constants need steps 1, 2 and 6. `tests/test_cliversions.py` fails if the
   newest tested version and those constants differ. Bump `HOOK_SIGNATURES_CLAUDE_CODE_VERSION` only
   if step 3 ran. Add the version to the table above. Also run
   `spikes/m2/permissions/permstub.py --claude-version <v>` (default, `--before`, and an `ask` rule
   for one tool, D44): all five calls should run, all five be refused, and only the asked one be
   refused.
8. If something changed: update the signatures and their tests, re-run step 2 to measure what is
   carried and paged with the new text, and record a decision in `docs/DECISIONS.md`.
9. Live acceptance (spends tokens, needs the owner's approval): `spikes/m2/acceptance`, as in live run
   #3 (`docs/research/M2-acceptance-run3.md`). Only for a change in step 8.

## Routine for a new Codex version

1. First check: `spikes/m2/cliversion/check.sh` (both CLIs; pass the Codex binary as the second
   argument if it is not on `PATH`). It should report `session start recognised: True`. Every lead
   item before the first prompt should show only `section <tag>` parts, apart from the base
   instructions, with the tags listed in `spikes/m2/cliversion/README.md`. If the user
   `<environment_context>` message shows as `turn`, the section tags changed.
2. Shapes: `spikes/m2/codex/probe.sh` (written for 0.154.0; five auth and provider scenarios). It writes
   `.contexto/spikes/M2-A5/codex-shapes.jsonl`: one structure-only line per request, with the header
   names, the top-level keys, and each input item's role and part tags. Check it against the table in
   `docs/research/M2-codex-adapter.md`: `POST /v1/responses` only with the full input, `store: false`,
   no `previous_response_id`, `prompt_cache_key` set; on a 401, 6 POSTs with the custom providers in
   API-key mode, and 2 POSTs plus 2 `GET /v1/models` in ChatGPT mode (0.163.0); 7 WebSocket upgrades
   and then 6 POSTs with the built-in provider; the same section tags in every scenario.
3. MCP launch: `spikes/m2/codex/smoke.sh preflight` (zero tokens: `contexto run codex` through the
   proxy to `responses_stub.py` with a dummy key helper). The newest session log under
   `.contexto/spikes/M2-A6/preflight-home/sessions/` should have no `mcp_tools_missing`, which shows the
   MCP server started and answered `tools/list`. The run ends with `watchdog stop status_401`, which
   is the stub's answer. Its `turn` mode spends tokens and needs the owner's approval.
4. The suite: `pytest -q`.
5. If nothing changed: append the version to `TESTED_VERSIONS["codex"]` and bump
   `CONTEXT_SECTIONS_CODEX_VERSION` to it, in the same commit, and add it to the table above. The
   constant needs steps 1, 2 and 4; step 3 is for the MCP launch, which the constant does not cover.
6. If something changed: update `CONTEXT_SECTION` and `tests/test_codex.py`, and record a decision.
