# A `statusLine` in `--settings`, as Claude Code treats it (D64)

Zero-spend measurements behind `contexto statusline` and the rule in `contexto.statusline` for when
`contexto run claude` may add a status line. `probe.py <case>` runs real Claude Code in a pty (or
with `-p`) against `../subagents/stub.py`, a local stand-in for the Messages API that answers every
request with one line of text. It uses a scratch `HOME`, `CLAUDE_CONFIG_DIR` and `CONTEXTO_HOME`
under `$CX_SPIKE_DIR` (default `/tmp/cx-statusline-spike`), a fake key, nonessential traffic off and
`DISABLE_AUTOUPDATER=1`, so the user's own Claude Code and Contexto data are never read or written.
`CLAUDE=<binary>` picks the Claude Code to run.

`probe.sh` is the status line command under test: it appends its parent process, working directory,
environment and stdin to a record file, and prints `PROBE-<label>-LINE` (nothing for the label
`EMPTY`). `scenario.py` is the stub's reply.

```sh
uv run python spikes/m2/statusline/probe.py flag
CLAUDE=~/.local/share/claude/versions/2.1.318 CX_SPIKE_DIR=/tmp/cx-statusline-spike-318 \
  uv run python spikes/m2/statusline/probe.py both
```

Each case prints one JSON object. The pty stream is kept as `screen-<case>.log` in the spike
directory; replaying it through a terminal emulator shows the rows.

Measured on 2026-11-12 with Claude Code 2.1.322 (not in `TESTED_VERSIONS`), and the cases marked *
again with 2.1.318 (tested), with the same results:

| Case | Setup | Result |
|---|---|---|
| `flag` * | `statusLine` only in the `--settings` file, interactive, one prompt | The command ran twice (session start, after the assistant message); its line was on screen. |
| `print` * | the same with `-p` | The command never ran. One request reached the stub. |
| `both` * | `statusLine` in the user settings and in `--settings` | Only the `--settings` command ran (2 runs against 0) and only its line was on screen. |
| `user` | `statusLine` only in the user settings, a `--settings` file without one | The user's command ran and its line was on screen. |
| `untrusted` * | `--settings` `statusLine`, folder not trusted, dialog left open | The trust dialog was shown; the command did not run. |
| `untrusted-accept` | the same, then "Yes, I trust this folder" | No run before the answer; the command ran once after it, in the same session, and its line was on screen. |
| `empty` | the `--settings` command prints nothing | It ran; an empty row stayed between the prompt box and the footer. |
| `none` | no `statusLine` anywhere | No row: the footer sits directly under the prompt box. |
| `late` * | `--settings` `statusLine`; the user settings gain one mid-session, then a prompt | The `--settings` command kept running (2 runs); the user's never ran. |
| `contexto` * | `contexto run claude` from this checkout, empty configuration, store path with a space, quotes and `$` | The row showed `ctx bound 150k`, then `ctx 100/150k` after the reply (the stub reports 100 input tokens). The `launch` event has `statusline: added`. |
| `contexto-late` * | the same, and the user settings gain a `statusLine` mid-session | The row changed to ``contexto: restart `contexto run claude` to show your own status line``. |

What the command's first run saw, in every interactive case: the environment Claude Code was
started with (a marker variable, `HOME` and `CLAUDE_CONFIG_DIR`), the session's working directory, a
stdin that is not a terminal holding a JSON object (`session_id`, `transcript_path`, `cwd`, `model`,
`workspace`, `version`, `cost`, `context_window`, `effort`, `thinking`, `fast_mode`, `output_style`,
`exceeds_200k_tokens`), and a single-quoted `${CX_PROBE_MARK}` argument as written: Claude Code did
not expand it. `COLUMNS` and `LINES` were not set, though the documentation says they are.

Not established: which footer hints a status line hides. Claude Code's documentation says most of
the keyboard hints go; the captures here show the `← for agents` hint both with and without a status
line, so they neither confirm nor refute it.

Not measured: managed settings of any kind, Windows, and a shell other than the one Claude Code
picked here.
