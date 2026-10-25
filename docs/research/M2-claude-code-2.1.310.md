# Claude Code 2.1.310: the D38 routine's checks (2026-10-25)

Numbers, ids, counts and booleans only. Every check ran at zero spend: loopback stubs, temporary
homes and config dirs, fake keys. Each ran on the installed 2.1.310 and, for comparison, on 2.1.302
(`~/.local/share/claude/versions/`). Code: `origin/main` 7893ceb plus this change (D45).

## Step 1: first check (`spikes/m2/cliversion/check.sh`)

| Item | 2.1.302 | 2.1.310 |
|---|---|---|
| `User-Agent` | `claude-cli/2.1.302 (external, sdk-cli)` | `claude-cli/2.1.310 (external, sdk-cli)` |
| model, messages | claude-opus-5-5, 2 | claude-opus-5-5, 2 |
| tools | 20 | 12 (`ToolSearch`, `DeferredToolPlaceholder`, 10 others) |
| `messages[0]` block 0 | `attribution` | `attribution` |
| `messages[1]` | `session_start` | `session_start` |
| session start recognised | True | True |
| blocks of no recognised kind | 0 | 0 |
| environment message, characters | 8,524 | 8,891 (13 lines more: the deferred-tool listing, 11 names) |

codex-cli 0.163.0 gave the same result in both runs.

## Step 2: reminders spike (`run_all.sh 48`, 13 runs)

| Item | 2.1.302 | 2.1.310 default (tool search on) | 2.1.310, `ENABLE_TOOL_SEARCH=false` |
|---|---|---|---|
| `session_start_msgs_k` from turn 2, 7 runs on 5.x ids | 1 | 1 | 1 |
| `session_start_msgs_k`, 6 runs on 4.x ids | 0 | 0 | 0 |
| `carried_unclassified_k`, summed over every turn of every run | 0 | 0 | 0 |
| runs listing `# Environment` | 13 | 13 | 13 |
| main requests, opus55-resume / sonnet46-resume / probe-claude-opus-4-7 | 88 / 98 / 28 | 84 / 84 / 21 | 88 / 98 / 28 |
| context-block first lines of no kind (before D45) | 0 | 1, in `messages[0]` of the 6 runs on 4.x ids | 0 |
| nested CLAUDE.md on 5.x ids (6 runs) | role `system` message, 79 to 103 tokens | next to the Read result, 99 to 103 tokens | next to the Read result, 99 to 103 tokens |

With tool search on, the task tools and `mcp__contexto__contexto_state` are deferred, and the
scripted model calls only tools in `tools`, so it skips the turns that call them: hence fewer
requests. With tool search off, request counts match 2.1.302 in all 13 runs, and the nested
CLAUDE.md placement is the only change in `features.md` apart from sizes that moved by at most 5
tokens. On 4.x ids the nested CLAUDE.md was inside the Read result on both versions. The
analysis re-run with D45's code gave byte-identical `turns.csv`, `requests.csv`, `summary.md`,
`features.md`, `reminders.csv` and `table.md` for the 2.1.310 default runs.

## Step 3: hooks, styles, plan mode, effort, MCP (`spikes/m2/carried/live_probe.sh`)

| Item | 2.1.302 | 2.1.310 |
|---|---|---|
| scenarios, exit 0 | 22, 22 | 22, 22 |
| scenarios with the same message shapes (tool lists aside) | 18 of 22 | 18 of 22 |
| `UserPromptSubmit hook additional context: ` prefixes | 7 | 7 |
| `PostToolUse:<tool> hook additional context: ` prefixes | 2 | 2 |

The 4 others differ only by tool search: on claude-haiku-4-5, one more `messages[0]` block (the
listing); in the 3 MCP add, keep and remove turns, a role `system` listing instead of the tool in
`tools`.

## Step 4: missing-tools notices (`claude -p`, fresh, `--resume` with the server command missing, `--resume` with it back)

| Capture | role `system` texts byte-identical to 2.1.302 with tool search on | verdict before D45 | verdict with D45 |
|---|---|---|---|
| claude-opus-5-5, tool search on | 3 of 3 requests (paths, a `Monitor` name, one skill line aside) | True, False, True | True, False, True |
| claude-opus-5-5, tool search off | 3 of 3 | True, False, True | True, False, True |
| claude-sonnet-4-6, tool search on | n/a: the listing and notices are `<system-reminder>` blocks, the same on both versions | False, False, False | True, False, True |

End to end, `contexto run claude -p` on 2.1.310 with a stub upstream: on claude-sonnet-4-6,
`mcp_tools_missing` 1 before D45 and 0 with it; on claude-opus-5-5, 0 and 0. On 2.1.302 with
claude-sonnet-4-6 before D45: 0. Every request body of the 13 step 2 runs on 2.1.310 is judged
offered with D45; before it, none on the 6 runs on 4.x ids (0 of 84, 84 and 48 on the sonnet46 runs,
0 of 21 on each 4.x probe).

## Step 5: subagent shapes (`spikes/m2/subagents`)

| Scenario | captures 2.1.302 / 2.1.310 | shape strings the engine matches, the same | subagent environment message |
|---|---|---|---|
| `run.sh` fg | 4 / 4 | yes | starts with `# Environment` / with the listing |
| `run.sh` stop (`<status>killed`) | 5 / 5 | yes | same as above |
| `run.sh` resume (`resumedAgentId`, coordinator message) | 13 / 13 | yes | same as above |
| `drive.py` bgidle | 7 / 7 | yes | same as above |
| `drive.py` esc | 8 / 8 | yes | same as above |

Apart from that message and the agent ids, every captured request matched. Classified with the code
before D45, the 14 subagent environment messages on 2.1.310 were `settings_change`; with D45 they are
`session_start`, as are the 21 main ones and all 35 on 2.1.302. 2.1.302 with
`ENABLE_TOOL_SEARCH=true` also starts a subagent's environment message with the listing (fg, 1 run).
On claude-sonnet-4-6, a subagent's listing is its own block and its environment block keeps kind
`environment`. 2.1.310 refused the scenario's standalone `sleep 30` (`Blocked: standalone sleep 30`),
which ended the agent at once in stop and esc; with the wait run through perl, both versions capture
the same sequence.

## D44 permission check (`spikes/m2/permissions/permstub.py`, claude-haiku-5-5)

| Mode | 2.1.302 calls that ran / refused | 2.1.310 calls that ran / refused |
|---|---|---|
| default (`--settings` allow rules) | 5 / 0 | 5 / 0 |
| `--before` (base URL only) | 0 / 5 | 0 / 5 |
| user `ask` rule for `contexto_search` | 4 / 1 | 4 / 1 |

The refusal text was the same on both versions. With tool search on, the stub's direct calls to the
deferred tools ran without a `ToolSearch` call first.

## Step 6: the suite

`pytest -q`: see the PR. No live run (step 9): D45 says why.
