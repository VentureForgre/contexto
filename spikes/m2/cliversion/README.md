# First check of a new CLI version (D38, zero spend)

`check.sh` sends `claude -p` and `codex exec` requests to a loopback stub that answers 401, then
`classify.py` reads the first main request of each with Contexto's own recognisers. It reports
which version each CLI's `User-Agent` names, whether Contexto was tested with it, and whether the
session start is recognised. For Claude Code it lists the class of every role `system` message
and the kind of every context block. For Codex it lists every input item, with the tag of each
recognised context-section part. Anything of no recognised kind is printed with its first line.

## Setup and run

From the repository root, with Python 3.12 or newer, and both CLIs on `PATH` (or pass their paths):

```
python3.12 -m venv .venv
.venv/bin/pip install -q -e . pytest
PATH="$PWD/.venv/bin:$PATH" spikes/m2/cliversion/check.sh [claude-binary] [codex-binary]
```

It takes about a minute. Codex retries the 401 five times (6 requests) and gives up by itself.
Claude Code keeps retrying until a 40-second alarm (`perl -e 'alarm 40'`) stops it, so the shell
prints a line like `check.sh: line 25: ... Alarm clock: 14 ...`. That line is expected and is not a
failure. Each CLI runs under `env -i`, with a temp `HOME`, `CLAUDE_CONFIG_DIR` or `CODEX_HOME`, fake
keys, and a base URL on 127.0.0.1. The stub keeps each body and its `User-Agent`, and no other
header. Bodies stay in the printed temp directory; remove it by hand.

This covers one fresh request per CLI. It does not cover later turns, resumes, effort changes, plan
mode, or hooks; `docs/cli-updates.md` lists the spikes that do.

## Result on 2026-10-24 (Claude Code 2.1.302, codex-cli 0.163.0)

Both versions are tested, and both session starts are recognised.

Claude Code (default model claude-opus-5-5) sent 2 messages and 20 tools:

| Item | Content | Class |
|---|---|---|
| `messages[0]` user | one `<system-reminder>` block (`Attribution for git commits`), then the prompt | context block of kind `attribution` |
| `messages[1]` system | `# Environment` | `session_start` |

Codex sent 7 input items. The first 6 are the lead (`RESPONSES.lead_count`):

| Item | Role | Parts |
|---|---|---|
| `input[0]` `additional_tools` | developer | none (tool namespaces) |
| `input[1]` message | developer | base instructions (`You are Codex, ...`), no tag |
| `input[2]` message | developer | `<skills_instructions>`, `<permissions instructions>`, `<collaboration_mode>` |
| `input[3]` message | developer | `<multi_agent_role>` |
| `input[4]` message | developer | `<multi_agent_mode>` |
| `input[5]` message | user | `<environment_context>` |
| `input[6]` message | user | the prompt (the first turn start) |

These are the same six openings `CONTEXT_SECTION` was written for on 0.154.0. Only the user-role
ones affect classification: developer messages are in the lead whatever their tags. If
`<environment_context>` were renamed, `input[5]` would become the first turn start and be paged
with it. The AGENTS.md header (`# AGENTS.md instructions for <dir>`) was not exercised: the temp
project has no `AGENTS.md`.
