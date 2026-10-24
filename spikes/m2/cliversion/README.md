# First check of a new CLI version (D38, zero spend)

`check.sh` sends one `claude -p` request and one `codex exec` request to a loopback stub that answers
401, then `classify.py` reads them with Contexto's own recognisers. It answers, in about a minute and
a half (each CLI retries the 401 until a 40-second alarm stops it): which version each CLI's
`User-Agent` names, whether Contexto was tested with it, and whether the session start is recognised.
For Claude Code it lists the class of every role `system` message and the kind of every context
block; for Codex, every input item and whether its parts are recognised context sections. Anything
of no recognised kind is printed with its first line.

```
python3.12 -m venv .venv && .venv/bin/pip install -q -e . pytest
PATH="$PWD/.venv/bin:$PATH" spikes/m2/cliversion/check.sh [claude-binary] [codex-binary]
```

Isolation: `env -i`, a temp `HOME`, `CLAUDE_CONFIG_DIR` and `CODEX_HOME`, fake keys, and a base URL on
127.0.0.1. The stub keeps each body and its `User-Agent`, and no other header. Bodies stay in the
printed temp directory; remove it by hand.

This covers one fresh request. It does not cover later turns, resumes, effort changes, plan mode, or
hooks; `docs/cli-updates.md` lists the spikes that do.

Result on 2026-10-24 (Claude Code 2.1.302, codex-cli 0.163.0): both tested, both session starts
recognised. Claude Code (default model claude-opus-5-5) sent `messages[0]` with an `attribution`
context block and `messages[1]` as the session-start system message. Codex's lead was 6 items:
`additional_tools`, the base instructions, three developer messages of sections, and the user
`<environment_context>` message.
