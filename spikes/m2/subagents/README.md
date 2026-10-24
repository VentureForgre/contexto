# Subagent shapes in Claude Code 2.1.302 (D40)

Zero-spend capture for D40's side rooms: Claude Code 2.1.302 runs against `stub.py`, a scripted
local stand-in for the Messages API, and every request it sends is saved. The scripts run with an
isolated `CLAUDE_CONFIG_DIR` under `$CX_SPIKE_DIR` (default `/tmp/cx-subagent-spike`), so the
user's own Claude Code configuration and transcripts are never read or written. Captured request
bodies stay in that directory and are not committed.

- `run.sh <name> <scenario.py> <prompt>`: print mode (`claude -p`).
- `drive.py <name> esc|bgidle`: interactive mode in a pty; `esc` presses Esc while the subagent's
  tool runs and then types a new prompt.
- `esc_scenario.py` (`SCEN_MODE` = `fg`, `stop`, `esc`, `bgidle`), `bg_scenario.py` (a foreground
  and a background agent plus a background Bash at once), and `resume_scenario.py` (a background
  agent resumed twice with `SendMessage`) script the stub's replies.
- `show.py <capture dir>` prints each captured request's messages in short form.

Measured on 2026-10-24 (claude-haiku-5-5 model id, nonessential traffic off):

1. A subagent's first request carries a `<system-reminder>` text block and then the `Agent` call's
   `prompt`, unchanged, as its own text block. The engine matches a side conversation to main's
   `Agent` call by that text.
2. A background launch (`run_in_background: true`) returns at once with a tool result that starts
   `Async agent launched successfully.` and names the `agentId`. In interactive mode a call with
   `run_in_background: false` returned the same receipt. In print mode, `run_in_background: false` ran
   the subagent in the foreground and returned `[Subagent hand-back] ...` with its report.
3. When a background agent or a background Bash command stops, main gets a notification. During
   main's tool loop it arrives as a `role: "system"` message starting `[SYSTEM NOTIFICATION - NOT
   USER INPUT]` with a `<task-notification>` element (`<task-id>`, `<tool-use-id>` of the launching
   call, `<status>`: `completed`, `failed`, or `killed`, `<summary>`). While main is idle it arrives
   as a new user message holding one text block, `<system-reminder>` around the same text; when the
   CLI resends that history, the message can be a plain string instead.
4. Esc in interactive mode, pressed while the subagent's 30-second Bash ran: main was already idle
   (the agent had been launched in the background), so the Esc stopped nothing. The subagent finished
   its Bash, answered, and main got a `completed` notification.
5. A background agent stopped with `TaskStop` gives a notification with `<status>killed</status>`.
6. A human prompt typed in interactive mode arrives as a user message whose content is a plain
   string.
7. A finished background agent resumed with `SendMessage` (`resume_scenario.py`, twice): main's
   `SendMessage` result is `{"success":true,"message":"Resuming agent ...","resumedAgentId":"<agentId>",...}`.
   The subagent's next request is its whole earlier conversation plus a new user message, a plain
   string starting `The coordinator sent a message while you were working:`, so each resume adds a
   turn start to its conversation. When the resumed run stops, the notice's `<task-id>` is the same
   `agentId` but its `<tool-use-id>` is the `SendMessage` call's id, not the `Agent` call's.

Not captured: the tool result of a foreground subagent interrupted by Esc, since every interactive
launch ran in the background.
