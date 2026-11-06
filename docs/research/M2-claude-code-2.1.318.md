# Claude Code 2.1.314 and 2.1.318, codex-cli 0.166.0: the D38 routine's checks (2026-11-05)

Numbers, ids, counts and booleans only. Every check ran at zero spend: loopback stubs, temporary
homes and config dirs, fake keys, `DISABLE_AUTOUPDATER=1`. The binaries ran by path from
`~/.local/share/claude/versions/` (`~/.local/bin/claude` untouched); Codex was the installed 0.166.0.
2.1.310 could not be run again: an auto-update during a QA session deleted it. So the comparison is
with the committed 2.1.302 results and [M2-claude-code-2.1.310.md](M2-claude-code-2.1.310.md). Code:
`origin/main` 4e2c4cd plus this change (D56). No live check ran (routine step 9 stays with the owner).

## Step 1: first check (`spikes/m2/cliversion/check.sh`)

| Item | 2.1.314 | 2.1.318 |
|---|---|---|
| `User-Agent` | `claude-cli/2.1.314` | `claude-cli/2.1.318` |
| model, messages | claude-opus-5-5, 2 | claude-opus-5-5, 2 |
| tools | 12 (`ToolSearch`, `DeferredToolPlaceholder`, 10 others) | 12 (the same) |
| `messages[0]` block 0 | `attribution` | `attribution` |
| `messages[1]` | `session_start` | `session_start` |
| session start recognised | True | True |
| blocks of no recognised kind | 0 | 0 |
| environment message, characters | 9,034 (8,891 on 2.1.310) | 9,034 |

codex-cli 0.166.0, in both runs: `codex_exec/0.166.0`, session start recognised, 7 input items with
the 0.163.0 section tags (`skills_instructions`, `permissions instructions`, `collaboration_mode`,
`multi_agent_role`, `multi_agent_mode`, `environment_context`).

## Step 2: reminders spike (`run_all.sh 48`, 13 runs per matrix, 52 runs)

| Item | 2.1.314 on | 2.1.314 off | 2.1.318 on | 2.1.318 off |
|---|---|---|---|---|
| runs exit 0, non-loopback lines | 13, 0 | 13, 0 | 13, 0 | 13, 0 |
| `session_start_msgs_k` from turn 2, 7 runs on 5.x ids | 1 | 1 | 1 | 1 |
| `session_start_msgs_k`, 6 runs on 4.x ids | 0 | 0 | 0 | 0 |
| `carried_unclassified_k`, summed | 0 | 0 | 0 | 0 |
| runs listing `# Environment` | 13 | 13 | 13 | 13 |
| main requests, opus55-resume / sonnet46-resume / probe-claude-opus-4-7 | 84 / 84 / 21 | 88 / 98 / 28 | 84 / 84 / 21 | 88 / 98 / 28 |

"on" is Claude Code's default (tool search on); "off" adds `--env ENABLE_TOOL_SEARCH=false` to every
run. The request counts match 2.1.310's with tool search on and 2.1.302's with it off.

Every request with a tool list, re-read with Contexto's own code (705 per matrix with tool search
on, 782 with it off): `_anthropic_session_start_seen` False 0 times, `context_block_kind` None 0
times (10,882 to 13,811 context blocks per matrix), `toolcheck.anthropic_tools_offered` True on all.

What changed in the CLI, none of it a misclassification:

| Item | 2.1.302 (committed) | 2.1.314 | 2.1.318 |
|---|---|---|---|
| bodies with a `<total_tokens>` countdown, opus55-resume / haiku55-resume / probe-claude-sonnet-5 | present in all three runs (bodies not kept) | 0 of 84 / 84 of 84 / 21 of 21 | 0 of 84 / 84 of 84 / 21 of 21 |
| settings-only effort messages (`{"role": "system", "content": [], "output_config": ...}`), opus55-resume / opus55-stream / opus55-stream-todo | 0 | 3 / 8 / 7 | 12 / 8 / 7 |
| "changed on disk" notes in the last request of opus55-, haiku55- and sonnet46-resume | not kept | 80 each | 4 each |

claude-opus-5-5 gets no countdown from 2.1.314 on, so an `--effort` change on a turn with no other
system message arrives as a settings-only message. It is read as `settings`, and the newest copy is
carried. 2.1.318's `--resume` no longer re-sends old file-changed notes, so less text is carried.

## Step 3: hooks, styles, plan mode, effort, MCP (`spikes/m2/carried/live_probe.sh`), and the launcher's command line

| Item | 2.1.314 | 2.1.318 |
|---|---|---|
| live_probe scenarios, exit 0 | 22, 22 | 22, 22 |
| normalised request shapes (sizes aside) the same as the other version | 46 of 46 lines | 46 of 46 |
| `UserPromptSubmit hook additional context: ` prefixes | 7 | 7 |
| `PostToolUse:<tool> hook additional context: ` prefixes | 2 | 2 |
| `carried/probe.sh` (3 scenarios), shapes the same as the other version | 9 of 9 lines | 9 of 9 |
| `argv/argstub.py`: `"pass": true`, `"blocked": []` | 8 of 8 | 8 of 8 |

## Step 4: missing-tools notices

`claude -p` three times per set with the real `contexto mcp` server: fresh, `-c` with `contexto` off
`PATH`, `-c` with it back (the subagent spike's stub, a temp `HOME` and config dir). Verdict of
`toolcheck.anthropic_tools_offered` on each request:

| Set | 2.1.314 | 2.1.318 |
|---|---|---|
| claude-opus-5-5, tool search on | True, False, True | True, False, True |
| claude-opus-5-5, tool search off | True, False, True | True, False, True |
| claude-sonnet-4-6, tool search on | True, False, True | True, False, True |

## Step 5: subagent shapes (`spikes/m2/subagents`, claude-haiku-5-5)

| Scenario | captures 2.1.310 / 2.1.314 / 2.1.318 | `agent_runs` stops before D56 | with D56 |
|---|---|---|---|
| `run.sh` fg (foreground hand-back) | 4 / 4 / 4 | unknown shape (`[None]`) | 1 |
| `run.sh` stop (`TaskStop`, idle notice) | 5 / 5 / 5 | 0 | 1 |
| `run.sh` resume (two `SendMessage` resumes) | 13 / 13 / 13 | 3 | 3 |
| `drive.py` bgidle (idle notice after a human prompt) | 7 / 6 / 6 | 0 | 1 |
| `drive.py` esc | 8 / 7 / 7 | 0 | 1 |

Unchanged: the subagent's first prompt (a `<system-reminder>` block, then the call's `prompt` as its
own block), `Async agent launched successfully.` with `agentId: `, `"resumedAgentId"`, the resumed
run's notice naming the `SendMessage` call, and the subagent's environment message (`session_start`,
starting with the deferred-tool listing). Changed, the same on both versions: a run that used tools
is reported with their names first, in a `<tools-used-before-summary>` element.

- Foreground hand-back: `"  <tools-used-before-summary>Bash</tools-used-before-summary>\n  \n[Subagent
  hand-back] ..."`. The frame says notes may stand above it.
- Idle notice: two `<system-reminder>` blocks in one user message: the first holds only the element,
  the second the `[SYSTEM NOTIFICATION - NOT USER INPUT]` notice.
- Notice during main's tool loop: a `system` message whose text starts with the element. A resumed
  run that used no tools gets no element.

After normalising agent ids, the 2.1.314 and 2.1.318 captures are the same in every scenario. In esc,
a side request asks for a background agent's progress ("Describe your most recent action in 3-5
words ... Do not use tools.") as a third text block of the subagent's first message. Its first prompt
matches no `Agent` call, so it opens no room that could close early. Main's last request of each
scenario on each version, scrubbed, is `tests/fixtures/subagent_shapes_2_1_31x.json`.

## D44 permission check (`spikes/m2/permissions/permstub.py`, claude-haiku-5-5)

| Mode | 2.1.314 calls that ran / refused | 2.1.318 calls that ran / refused |
|---|---|---|
| default (`--settings` allow rules) | 5 / 0 | 5 / 0 |
| `--before` (base URL only) | 0 / 5 | 0 / 5 |
| user `ask` rule for `contexto_search` | 4 / 1 | 4 / 1 |

## Codex 0.166.0

| Step | Result |
|---|---|
| 2, `spikes/m2/codex/probe.sh` | custom provider, env key: 6 POSTs; custom provider, API-key auth: 6 POSTs; ChatGPT mode: 2 POSTs and 2 `GET /v1/models`; built-in provider: 7 WebSocket upgrades, then 6 POSTs; MCP registered: 6 POSTs. Every POST: `store: false`, no `previous_response_id`, `prompt_cache_key` set, the same section tags. |
| 3, `spikes/m2/codex/smoke.sh preflight` | no `mcp_tools_missing`; ends with `watchdog stop status_401`; no Codex process left after the run |

One difference, only on the built-in provider path: the input gains an 8th item,
`{"type": "configuration_update"}` with no role, after the prompt. Contexto always runs Codex on its
own custom provider, where the item never appears.

## Step 6: the suite

`pytest -q`: see the PR.
