# Subagent shapes in Claude Code 2.1.302 (D40)

Zero-spend capture for D40's side rooms: Claude Code 2.1.302 runs against `stub.py`, a scripted
local stand-in for the Messages API, and every request it sends is saved. The scripts run Claude Code
under a scrubbed environment with an isolated `CLAUDE_CONFIG_DIR` and `HOME` under `$CX_SPIKE_DIR`
(default `/tmp/cx-subagent-spike`) and `DISABLE_AUTOUPDATER=1`, so the user's own Claude Code
configuration and transcripts are never read or written and the CLI cannot update itself. Captured
request bodies stay in that directory and are not committed. `CLAUDE=<binary>` picks the Claude Code
to run (default: `claude` on `PATH`), for example `~/.local/share/claude/versions/2.1.318`. Each run
binds a free port and waits for the stub's `listening` line; `run.sh` exits 1 (and `drive.py` stops)
when the stub could not listen, instead of letting Claude Code reach whatever holds the port.

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

Measured again on 2026-10-25 with 2.1.310 (D46, `docs/research/M2-claude-code-2.1.310.md`): the same shapes. With
tool search on, its default, a subagent's environment message starts with the deferred-tool listing
and `# Environment` follows. 2.1.310 refuses a standalone `sleep 30`, so `esc_scenario.py` waits
through perl.

Measured again on 2026-11-05 with 2.1.314 and 2.1.318 (D56, `docs/research/M2-claude-code-2.1.318.md`):
the same shapes, except that since 2.1.312 a run that used tools is reported with their names first,
in `<tools-used-before-summary>Bash</tools-used-before-summary>`:

- A foreground hand-back's result starts with the element, indented, then a line holding only two
  spaces, then `[Subagent hand-back] ...`.
- An idle notice is a user message of two `<system-reminder>` text blocks: the first holds only the
  element, the second the notice.
- The `system` notice during main's tool loop starts with the element, then `[SYSTEM NOTIFICATION -
  NOT USER INPUT]`.

A resumed run that used no tools gets no element. Captures per scenario: fg 4, stop 5, resume 13, bgidle
6, esc 7 (2.1.310: 7 and 8 for the last two); 2.1.314 and 2.1.318 differ only in agent ids. In esc, a
side request with the subagent's prompt plus "Describe your most recent action in 3-5 words ..." asks
for a background agent's progress; its first prompt matches no `Agent` call.
