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
| Claude Code | 2.1.310 | steps 1 to 6 below at zero spend, and the D44 permission check ([M2-claude-code-2.1.310.md](research/M2-claude-code-2.1.310.md)); tool search is on by default, and three of its shapes are read differently since D46; a live recall check with Contexto's tools deferred (credited at 145,080 tokens; 305,223 with the first attempt), not the full live acceptance |
| Claude Code | 2.1.314, 2.1.318 | steps 1 to 7 below at zero spend, with the reminders matrix run with tool search on and off, and the D44 permission check ([M2-claude-code-2.1.318.md](research/M2-claude-code-2.1.318.md)); the request shapes are 2.1.310's, and the subagent stop reports put the tools the agent used first, which the engine reads since D56; no live check (step 9 is the owner's) |
| Codex | 0.154.0 | Responses shapes and context sections, Codex acceptance (D30) |
| Codex | 0.163.0 | MCP server launch and missing-tools check (v0.2.8); context sections and request shapes rechecked for D38 (Codex steps 1 to 4 below) |
| Codex | 0.166.0 | Codex steps 1 to 4 below at zero spend for D56: the same context sections and request shapes, and the MCP launch; no live check |

Hook-output signatures were measured on Claude Code 2.1.301 and re-measured on 2.1.310, 2.1.314 and
2.1.318 (`HOOK_SIGNATURES_CLAUDE_CODE_VERSION`), not on 2.1.302.

## What Contexto reads from the CLI's text

| Constant | File | Measured by |
|---|---|---|
| `SESSION_START_SIGNATURE` (`# Environment`), `SESSION_START_SIGNATURE_CLAUDE_CODE_VERSION` | `src/contexto/rewrite.py` | `spikes/m2/scale/reminders` |
| `CONTEXT_BLOCK_KINDS` (first lines inside `<system-reminder>`), `CONTEXT_BLOCK_KINDS_CLAUDE_CODE_VERSION` | `src/contexto/rewrite.py` | `spikes/m2/scale/reminders` |
| `HOOK_SIGNATURES`, `HOOK_SIGNATURES_CLAUDE_CODE_VERSION` | `src/contexto/rewrite.py` | `spikes/m2/carried` (`live_probe.sh`, `hook_context.py`) |
| deferred-tool listings and MCP notices | `src/contexto/toolcheck.py` | zero-spend captures described in its docstring |
| the refusal of a tool no permission rule allows ("... but you haven't granted it yet.", D44) | `src/contexto/toolcheck.py` | `spikes/m2/permissions/permstub.py` (D44) |
| `CONTEXT_SECTION` (Codex section tags), `CONTEXT_SECTIONS_CODEX_VERSION` | `src/contexto/codex.py` | `spikes/m2/codex/probe.sh` |
| subagent shapes (`AGENT_TOOLS`, `ASYNC_LAUNCH`, `HAND_BACK`, `NOTICE_OPENINGS`, the `<task-notification>`, `resumedAgentId` and `<tools-used-before-summary>` patterns), gated by `SUBAGENT_SHAPES_TESTED` (D40, D56) | `src/contexto/rewrite.py`, `src/contexto/cliversions.py` | `spikes/m2/subagents` |

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
  `claude-cli/2.1.302 (external, sdk-cli)` (2.1.310, 2.1.314 and 2.1.318 the same with their version);
  codex-cli 0.163.0 sends `codex_exec/0.163.0 (...)` (0.166.0 the same with its version).
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

Every spike runs the `claude` and `codex` first on `PATH`, falling back to `~/.local/bin`, unless you
name a binary. To test a version that is not your default, run it by path and leave your own install
alone (Claude Code keeps each version under `~/.local/share/claude/versions/<version>`):

- `check.sh [claude] [codex]`, `carried/live_probe.sh [claude]`, `carried/probe.sh [claude]` and
  `codex/probe.sh [codex]` take the binary as an argument.
- The reminders spike (`run_all.sh`), `subagents/run.sh` and `subagents/drive.py` read `CLAUDE=<binary>`,
  and `codex/smoke.sh` reads `CODEX=<binary>`.
- `argv/argstub.py` and `permissions/permstub.py` take `--claude-version` and run
  `~/.local/share/claude/versions/<version>`.

They use temp homes and config dirs, fake keys, and loopback stubs, so they read no credential, write
nothing in your home, and reach no model. Each run of Claude Code must have `DISABLE_AUTOUPDATER=1`:
otherwise the CLI may update itself during the run, repoint `~/.local/bin/claude` and delete older
versions (a QA run lost 2.1.310 this way). The spikes set it; set it yourself for any other run,
interactive ones included. The shell may print `Alarm clock` lines: the spikes stop a CLI that keeps
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
     88 on `opus55-resume`). Run the matrix a second time with tool search off
     (`run_all.sh 48 --env ENABLE_TOOL_SEARCH=false`: arguments after the turn count go to every run),
     and compare that run with the previous version's.
3. Hooks, output styles, plan mode, effort, MCP changes: `spikes/m2/carried/live_probe.sh [claude]` (zero
   tokens, against a fake model). Hook outputs must still start with the `HOOK_SIGNATURES` prefixes.
   The launcher's command line (about 10 s per version, against a stub):
   `python spikes/m2/argv/argstub.py --claude-version <version> --work /tmp/argstub`. The spike deletes
   only a `--work` it made (one holding `.argstub-work`) or an empty one. If a run from before the
   marker existed left an unmarked `/tmp/argstub`, remove it first. Every line must say
   `"pass": true` and `"blocked": []`: a prompt right after `claude`, after flags, after `--`, with
   `-p`, with `-c -p`, and no prompt.
4. Missing-tools notices: if the CLI's MCP or tool-search text changed in step 1 or 2, recapture the
   shapes `src/contexto/toolcheck.py` describes (tool search on, a resume with the server off PATH)
   and update `tests/test_toolcheck.py`.
5. Subagent shapes (zero tokens, against the stub; D40): `spikes/m2/subagents/run.sh` with
   `esc_scenario.py` (`SCEN_MODE=fg` and `SCEN_MODE=stop`) and with `resume_scenario.py`, then
   `spikes/m2/subagents/drive.py` in interactive mode (`bgidle` and `esc`). Compare the captures with
   the spike's README: the subagent's first prompt, the launch receipt and the hand-back, the
   `<task-notification>` notices in a `system` message and in a `<system-reminder>` user message, and
   a resume's `resumedAgentId`, new turn start, and `SendMessage`-named notice. Check also that the
   subagent's environment message is still `session_start` (D46: with tool search on it starts with
   the deferred-tool listing). Since 2.1.312 a run that used tools reports their names first, in a
   `<tools-used-before-summary>` element (D56): indented above the hand-back's frame, as a
   `<system-reminder>` block of its own ahead of an idle notice's block, and at the start of a `system`
   notice. Check that it still comes in those places only. Each run takes a free port and
   waits for its stub to hold it; `run.sh` exits 1 without starting Claude Code when the stub could not
   listen. `CX_SPIKE_DIR` sets where the captures go (one directory per version). Append the version to
   `SUBAGENT_SHAPES_TESTED` only if they are unchanged; otherwise update the shapes in
   `src/contexto/rewrite.py` and `tests/test_subagent_room.py`, test them on the captures (as
   `tests/fixtures/subagent_shapes_2_1_31x.json`, written and scrubbed by
   `tests/fixtures/make_subagent_shapes.py`), and record a decision. Leaving the version out is safe:
   side rooms then only expire.
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
   #3 (`docs/research/M2-acceptance-run3.md`). Only for a change in step 8. When the full run is not
   approved, a short live recall check at a small cost can stand in for the question a change in how
   Contexto's tools are offered raises (2.1.310: a planted fact, paged out, asked for, through
   deferred tools; D46). The stand-in spends tokens too (305,223 for 2.1.310's two attempts) and needs
   the owner's approval of its token cap. Say in the version's decision which of the two ran, or that
   neither did (2.1.314 and 2.1.318: neither yet, D56).

## Routine for a new Codex version

1. First check: `spikes/m2/cliversion/check.sh` (both CLIs; pass the Codex binary as the second
   argument if it is not on `PATH`). It should report `session start recognised: True`. Every lead
   item before the first prompt should show only `section <tag>` parts, apart from the base
   instructions, with the tags listed in `spikes/m2/cliversion/README.md`. If the user
   `<environment_context>` message shows as `turn`, the section tags changed.
2. Shapes: `spikes/m2/codex/probe.sh [codex]` (written for 0.154.0; five auth and provider scenarios). It writes
   `.contexto/spikes/M2-A5/codex-shapes.jsonl`: one structure-only line per request, with the header
   names, the top-level keys, and each input item's role and part tags. Check it against the table in
   `docs/research/M2-codex-adapter.md`: `POST /v1/responses` only with the full input, `store: false`,
   no `previous_response_id`, `prompt_cache_key` set; on a 401, 6 POSTs with the custom providers in
   API-key mode, and 2 POSTs plus 2 `GET /v1/models` in ChatGPT mode (0.163.0); 7 WebSocket upgrades
   and then 6 POSTs with the built-in provider; the same section tags in every scenario. On 0.166.0
   the built-in provider's input also ends with a role-less `configuration_update` item; Contexto's own
   provider never gets one.
3. MCP launch: `[CODEX=<codex>] spikes/m2/codex/smoke.sh preflight` (zero tokens: `contexto run codex` through the
   proxy to `responses_stub.py` with a dummy key helper). The newest session log under
   `.contexto/spikes/M2-A6/preflight-home/sessions/` should have no `mcp_tools_missing`, which shows the
   MCP server started and answered `tools/list`. The run ends with `watchdog stop status_401`, which
   is the stub's answer. Its `turn` mode spends tokens and needs the owner's approval.
4. The suite: `pytest -q`.
5. If nothing changed: append the version to `TESTED_VERSIONS["codex"]` and bump
   `CONTEXT_SECTIONS_CODEX_VERSION` to it, in the same commit, and add it to the table above. The
   constant needs steps 1, 2 and 4; step 3 is for the MCP launch, which the constant does not cover.
6. If something changed: update `CONTEXT_SECTION` and `tests/test_codex.py`, and record a decision.
