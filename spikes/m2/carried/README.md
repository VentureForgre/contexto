# Carried content and system messages in Claude Code 2.1.301 (M2 A2 evidence)

Evidence behind rewrite rules 1, 10, and 12 (`docs/milestones/M2-contexto-proxy.md`, aligned in
PR #25) and the rewrite engine in `src/contexto/rewrite.py`. Measured by build.builder-2 on
2026-10-16. Everything here is zero spend except `live_check.py` (341 tokens, intake-approved).
Logs hold structure only: roles, block types, text lengths, a leading tag name, `cache_control`
positions, and the key names of extra fields. No text is logged.

## Scripts

- `shape_stub.py`: loopback stub that answers 401 and logs request structure.
- `fake_model_stub.py`: loopback fake model that answers with valid streamed replies (a `Read`
  tool call when the newest turn contains the marker `USE_TOOL`, else the text `ok`) and logs the
  same structure. No model is called.
- `probe.sh`: a fresh turn, then `--resume` of two hand-written session files (two text turns; a
  tool loop), against `shape_stub.py`.
- `live_probe.sh`: four real CLI-written sessions against `fake_model_stub.py`: six turns (turn 1
  with a tool loop, then five `--resume` turns, of which turns 4 and 6 change `--effort`); four turns
  with a UserPromptSubmit hook (`hook_context.py`) that adds per-turn context; two turns with
  UserPromptSubmit and PostToolUse hooks (turn 1 runs a tool); and four turns that add
  an MCP server (`mcp_probe_server.py`) on the second turn and drop it on the fourth.
- `live_check.py`: three placements sent to the real API through the seat's route (credential held
  in memory as in L1-e, `claude-haiku-5-5`, synthetic content). Needs the L1-e probe
  (`spikes/m0/route-identity/probe.py`; `M2_PROBE_PATH` overrides its location).

All throwaway CLI runs use an isolated `CLAUDE_CONFIG_DIR`, a dummy key that is not a credential,
`env -i`, and `--model claude-haiku-5-5`; output goes to `.contexto/spikes/M2-A2/` (gitignored).

## Findings

1. **Fresh session shape** [measured: `probe.sh`, `live_probe.sh`]: `messages[0]` is a user message
   with a `<system-reminder>` context block and then the prompt text; `messages[1]` is a role
   `system` message with one text block (about 8,470 characters, `cache_control` set) and an extra
   `output_config: {effort: medium}` field.
2. **Placement of carried system messages** [measured: `live_check.py`, 341 tokens]: a role
   `system` message as `messages[0]`, before the first user turn-start message, returns 400
   `invalid_request_error`, with and without Claude Code's beta list. The same text merged into
   the user message returns 200; a system message right after the user message (Claude Code's own
   shape) returns 200. The engine uses the latter (rule 1).
3. **Form changes along the history** [measured: `live_probe.sh`]: the CLI turns a one-text-block
   message with `cache_control` into a plain string on later requests (the system message at
   `messages[1]` from the second request of turn 1 on, earlier prompts later) and moves
   `cache_control` to the newest messages. Rule 10 therefore hashes with `cache_control` removed
   and a single text block normalised to a string.
4. **No client-side emptying** [measured: `probe.sh`, `live_probe.sh`]: with real CLI-written history
   (a tool loop and five resumes) the session-start system message is never emptied, including in
   the middle of a tool loop. A system message with empty content is a settings-only message
   `{role: system, content: [], output_config: {effort: ...}}`; it appears at `messages[1]` when a
   session file without the CLI's own system record is resumed (`probe.sh`), and as described in 5.
   No edit precedes kept thinking, so the rule 12 conformance call was not needed and was not run.
5. **Accrual without hooks** [measured: `live_probe.sh`, first session]: with no hooks, system
   messages accrue per settings change, not per turn or per resume. Turns 1 to 3 (same effort) carry
   exactly one system message. Each `--effort` change (turn 4 high, turn 5 back to the default
   medium, turn 6 low) appends one settings-only message as the request's last message, after the new
   prompt. Context blocks stay at `messages[0]`.
7. **Per-turn hook context** [measured by a single four-turn probe, `live_probe.sh` hook session;
   reviewer-2 measured the same with three turns]: with a UserPromptSubmit hook that adds
   `additionalContext`, every turn after the first gets one role `system` message right after its
   prompt (indexes 4, 7, 10), about 70 characters of text, no `output_config`. The session-start
   message at `messages[1]` keeps its text and `output_config`. So per-turn system messages exist
   whenever such a hook runs; the engine pages them with their turn (contract PR #35).
9. **PostToolUse hook context** [measured by a single two-turn probe, `live_probe.sh` PostToolUse
   session; reviewer-2 measured the same]: with PostToolUse and UserPromptSubmit hooks, turn 1's
   request after the tool call ends `assistant(tool_use), user(tool_result), system(77 characters,
   text only, no output_config)`; turn 2 adds `system(82 characters)` after its prompt. Turn 1's
   UserPromptSubmit context is folded into the session-start message itself (8,552 characters instead
   of 8,469). So text-only system messages can sit anywhere in a turn, including right after a
   `tool_result`.
8. **Tool-list changes** [measured: `live_probe.sh` MCP session]: adding an MCP server on a resume put
   `mcp__probe__contexto_probe` into `tools` (sent upfront behind a non-first-party base URL) and
   added no system message; dropping it removed the tool, again with no system message. A tool-list
   change is therefore a head change (rule 6), not carried content.
6. **Hand-written resume shape** [measured: `probe.sh`]: when the resumed session file lacks the CLI's
   own records, the context block rides in the new turn's user message and the full system text
   follows the new prompt as a trailing system message.

## Classifier (contract PR #35)

`classify_system` in `src/contexto/rewrite.py` sorts each role `system` message by shape alone
(contract PR #38 at 10f1cfa): settings-only (empty content plus settings fields such as
`output_config`), session-start (content plus settings fields), per-turn (any other text-only content,
wherever it sits; paged with its turn), and unclassified (non-text content; carried and logged as
`unclassified_system_message`).

Not measured: MCP `tools/list_changed` notifications within a single running process, and other
hook events that inject context (SessionStart, PostToolUse).
