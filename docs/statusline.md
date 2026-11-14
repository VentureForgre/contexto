# Seeing Contexto work while Claude Code runs: the status line

Claude Code can show one line of your choosing under its prompt, the status line. Since D64,
`contexto run claude` puts Contexto's numbers there when you have no status line of your own:

```
ctx 112k/150k · 41 paged · 3 recalls · 5 searches
```

`contexto statusline` is the command that prints the line. You can also call it from a status line
script of your own. It reads the session's files on this machine. Nothing is written and nothing is
sent anywhere.

(The numbers in this page's examples are illustrations, not recorded runs, unless a section says
"measured".)

## What the line shows

| Part | Meaning |
|---|---|
| `ctx` | The line is Contexto's. |
| `112k/150k` | The current prompt against the bound. The first number is the input of the main conversation's most recent model request, as the API counted it (`prompt.last_tokens` in [docs/stats.md](stats.md)). It is the latest request, not the session's peak, so it falls after a rotation. After `/clear` it shows the last request's prompt until the next reply. The second number is `--bound`. |
| `~77k/150k` | The same, when the API reported no usage for that request: `~` marks Contexto's own estimate. |
| `bound 150k` | Shown alone before the first model request of a new session. A resumed session starts with the previous run's latest prompt instead (`61k/150k`), as `contexto stats` does. |
| `41 paged` | The page files kept on disk for the whole session (`pages.count`), from every conversation in it, subagents' included, and every run. The model can read each of them back. It is not a count of your conversation's turns: a page usually holds one turn, but a page from the bounded fallback holds one carried message, and a subagent's request that pages adds to it while your prompt stays where it was. |
| `3 recalls` | Calls of the `contexto_recall` tool that succeeded, over the whole session. |
| `5 searches` | Calls of the `contexto_search` tool that succeeded, over the whole session. |
| `PAGING STOPPED (disk full)` | The store failed and Contexto has stopped paging: requests go out unpaged and can exceed the bound. See below. |

Numbers are shortened and rounded down (`9.4k`, `112k`, `1.9M`). When the prompt and the bound differ
but would shorten to the same text, both get the decimals it takes to tell them apart: `150.9k/150k`
is over a bound of 150,000, `150.2k/150.5k` is under a bound of 150,500, and `1.09M/1M` is 1,099,999
against a bound of a million. So a prompt over the bound always reads as more than it, a prompt under
it as less, and only a prompt exactly at the bound shows the same number twice.

A part is left out when the session has no data for it. A session that has paged nothing shows
`ctx 18k/150k`. A log with no `launch` line has no bound and shows `ctx 61k`. Failed tool calls are
not counted. The prompt is the main conversation's: a subagent's request does not change it.

The line changes when Claude Code runs the command: when the session starts, after each assistant
message, after `/compact`, and on a few other events. Contexto sets no refresh timer, because its
numbers change only when a request is made. A recall the model makes in the middle of a turn shows
once the next assistant message arrives.

### When paging has stopped

```
ctx PAGING STOPPED (disk full) · 212k/150k · 41 paged · see contexto stats
```

If the store cannot be written, Contexto keeps the conversation going but stops paging (degraded
mode, [docs/stats.md](stats.md#when-the-store-fails-degraded-mode)). The line then starts with
`PAGING STOPPED` and the cause, when the log names one for this stop: `disk full`, `store not
writable`, `page conflict` or `store error`.
The recall counts give way to `see contexto stats`, which prints what happened and what to do.
`contexto run claude` asks for colour, so the marker is red there; everything else on the line is
plain text. With `NO_COLOR` set to a non-empty value when you start `contexto run claude`, it does
not ask, and the marker is plain. The marker goes when paging resumes, and a new `contexto run` always
starts paging.

## When `contexto run claude` adds it

Claude Code takes `statusLine` from the highest-ranking settings file that has one, and replaces the
value rather than merging it. `contexto run claude` passes its own temporary settings file with
`--settings`, which ranks above your project and user settings. A status line put there would hide
yours. So Contexto adds its line only when that cannot happen. All of these must hold:

1. No settings file Claude Code would read from this directory has a `statusLine` key, whatever its
   value. The files are your user settings (`~/.claude/settings.json`, or
   `$CLAUDE_CONFIG_DIR/settings.json`), the project's `.claude/settings.json` and
   `.claude/settings.local.json` (and the main checkout's `settings.local.json`, where Claude Code
   reads it), and the managed settings file and its drop-ins
   (`/Library/Application Support/ClaudeCode/` on macOS, `/etc/claude-code/` on Linux:
   `managed-settings.json` and `managed-settings.d/*.json`).
2. Every one of those files that exists can be read as a JSON object. A file with a syntax error, or
   one Contexto has no permission to read, may hold your status line, so nothing is added. (An empty
   file holds none and does not count.)
3. None of them sets `disableAllHooks` to `true`, which also turns a custom status line off, and the
   managed files do not set `allowManagedHooksOnly`.
4. You did not turn it off (next section).

Managed settings that reach Claude Code another way (an MDM profile, or your organization's
claude.ai console) are not read. They rank above `--settings`, so a status line they set is shown
and Contexto's is not.

Your settings files are only read. Contexto never writes them.

The session log says what was decided, in the `launch` event's `statusline` field:

| Value | Meaning |
|---|---|
| `added` | Contexto's status line is in the settings it passed. |
| `off:flag`, `off:env` | You turned it off with `--no-statusline` or `CONTEXTO_NO_STATUSLINE`. |
| `own:<kind>` | A settings file has a `statusLine`. `<kind>` names the highest-ranking one: `managed`, `main-checkout-local`, `local`, `project` or `user`. |
| `unreadable:<kind>` | A settings file exists and could not be read as a JSON object. |
| `hooks_off:<kind>` | A settings file sets `disableAllHooks`. |
| `managed_hooks_only` | Managed settings allow only managed hooks. |
| `unsafe_path` | A path in the command holds `${`. Claude Code expands that in an MCP config (D55). It did not in a status line command on 2.1.318 and 2.1.322, but a path cannot be escaped if a later version does. |
| `error` | Deciding failed. Nothing is added, and the launch goes on. |

To see it: `grep '"event":"launch"' ~/.contexto/sessions/ID/log.jsonl`.

What adding it can cost you: Claude Code's documentation says that with a custom status line
configured it stops showing most of its footer's keyboard hints, among them `esc to interrupt` and
`? for shortcuts`. Contexto's measurements neither confirm nor refute that. If you miss the hints,
turn the line off.

The line is shown in interactive sessions only. `claude -p` runs no status line (measured), so
`contexto run claude -p` shows nothing; the summary printed at exit covers that case. In a folder you
have not told Claude Code to trust, the line appears once you accept its trust dialog (measured).

`contexto run codex` is unchanged. Codex has no such setting.

## Turning it off

```sh
contexto run --no-statusline claude        # this launch
export CONTEXTO_NO_STATUSLINE=1            # every launch (any value but empty, 0, false or no)
```

Or set up a status line of your own: Contexto then never adds its line.

## Calling it from your own status line

```
contexto statusline [--session ID] [--home DIR] [--color]
```

It prints one line and exits 0. **With no session, or on any error, it prints nothing and still
exits 0**, so your script decides what to show. It never prints a traceback. The session is, in this
order:

1. `--session ID`: a session id or a unique prefix of one.
2. `$CONTEXTO_SESSION`. `contexto run` sets it for the CLI it runs, and Claude Code passes its
   environment to the status line command (measured), so inside `contexto run claude` no option is
   needed. When the JSON on stdin names a Claude Code session (`session_id`), the variable's session
   counts only while a `contexto run` has it open and has served that Claude Code session in that
   run (since its proxy started); otherwise nothing is printed. The variable can outlive the launch that set it, if you exported it in a shell
   for `contexto stats`, and a plain `claude` started there would otherwise show that old session's
   numbers.
3. The Contexto session that is serving the Claude Code session named by `session_id` in the JSON
   on stdin. It counts only while a `contexto run` has that session open and has served that Claude
   Code session in that run: a session reopened for another conversation does not count for the
   one it served before. If two open launches serve
   the same Claude Code session, either could be the one asking, so nothing is printed.

The status line covers `contexto run` launches only. When the JSON on stdin names a Claude Code
session, a session that [`contexto serve`](serve.md) has opened since its last `contexto run` launch
prints nothing in steps 2 and 3. Named by `--session`, or by `$CONTEXTO_SESSION` with no session on
stdin (run by hand), it prints that session's numbers with the last launch's bound, not the
service's `--bound`, or with no bound when no launch made the session.

There is no fourth step. If none of the three names a session, nothing is printed. In a plain
`claude` session that Contexto is not serving, that is the right answer: a line with another
session's numbers would be wrong. A session named by `--session` or `$CONTEXTO_SESSION` that does not
exist also prints nothing; it does not fall through to stdin. `--session` is taken as given: you
named it.

Contexto learns which Claude Code session it serves from that session's model requests. So in steps
2 and 3 a conversation shows nothing until its first request goes through Contexto in the current
launch: at the start of a new session, after `/clear`, and when a conversation is resumed in a new
launch. The command `contexto run claude` installs names its session with
`--session`, so it shows from the start: `bound 150k` in a new session, and the previous run's
latest prompt (`61k/150k`) in a resumed one.

Stdin is read for steps 2 and 3. A terminal on stdin is not read, and a pipe that stays silent is
given up on after half a second, so running the command by hand does not hang; with no Claude Code
session on stdin, step 2 takes the variable's session as it is.

A working script, which appends Contexto's part to its own:

```sh
#!/bin/sh
# ~/.claude/statusline.sh: the folder's name, then Contexto's numbers when there are any.
input=$(cat)                      # Claude Code's JSON; stdin can be read once
dir=$(basename "$PWD")
ctx=$(printf '%s' "$input" | contexto statusline 2>/dev/null)
if [ -n "$ctx" ]; then
  printf '%s | %s\n' "$dir" "$ctx"
else
  printf '%s\n' "$dir"
fi
```

```json
{ "statusLine": { "type": "command", "command": "~/.claude/statusline.sh" } }
```

The script needs `contexto` on `PATH`. If you start Contexto another way (`uv run contexto`, a
virtual environment), give the full path of the `contexto` program in the script.

`--color` marks stopped paging in red with ANSI codes; without it the output has no escape codes.
When the output's encoding has no `·`, the parts are separated by ` | ` instead. A `C` locale is not
enough for that: Python still writes UTF-8 there (measured). `PYTHONIOENCODING=ascii` is.

## If you set up a status line during a session

`/statusline` writes a status line to your user settings. In a session started by `contexto run
claude`, Contexto's `--settings` still ranks above that file until you restart, so your new line
would not appear and Contexto's would stay (measured: Claude Code kept running the `--settings`
command). So the command Contexto installs checks your settings files each time it runs. Once one of
them has a `statusLine`, it prints this in place of the numbers until you restart `contexto run
claude`:

```
ctx: your status line needs a restart
```

If paging has stopped, the marker stays first, because it matters more and a narrow terminal cuts the
line from the end:

```
ctx PAGING STOPPED (disk full) · your status line needs a restart
```

After the restart, Contexto finds your status line and adds none. It never runs your command itself.
(The installed command gets the files to check as `--own-settings FILE` arguments. A script of your
own does not need them.)

## What is not shown, and why

- The share of input read from cache, bytes sent and forwarded, tokens kept on disk, pins and open
  obligations, faults other than stopped paging. `contexto stats` shows all of them. The line is kept
  short because Claude Code shows its own notifications on the same row and cuts the line on a narrow
  terminal, and because reading the pins would mean opening the ledger after every message.
- How many requests went over the bound. A current prompt over the bound shows as it is
  (`212k/150k`); the count is in `contexto stats`.
- The session's peak prompt. The line shows where the prompt is now.
- Subagents' prompts.
- Anything in a `claude -p` run or under `contexto run codex`: neither has a status line.

## Speed

Claude Code runs the command after every assistant message, and each run is a new process. Measured
on a development laptop, on a synthetic log shaped like a 6-hour session (4,000 model requests, 445
tool calls; 4,449 lines, 2.9 MB) with 1,200 page files, process start included, 20 runs:

| | Wall time, median | Worst | CPU time, median |
|---|---|---|---|
| `python -c pass` (the floor) | 32 ms | 48 ms | 23 ms |
| the command as `contexto run claude` installs it | 68 ms | 91 ms | 56 ms |
| the same, reading the whole log | 108 ms | 232 ms | 90 ms |
| `contexto stats`, for comparison | 516 ms | 1,335 ms | 288 ms |

The machine was running other test suites at the time (load average 32), so wall times are high; a
second batch at load average 50 gave a median of 165 ms and a worst of 663 ms for the installed
command, at 73 ms of CPU time. On a log ten times as long (28.8 MB) the installed command took 162 ms
of CPU time, against 555 ms for a read of the whole log.

While paging is stopped, each run reads the whole log, twice, to name the cause, so it is slower
then: measured in review on a 73 MB log, 0.77 s a run against 0.16 s for the same log while paging
works (0.05 s on the 6-hour log).

Three things keep it short:

- `contexto statusline` skips the rest of the command line: it loads neither the launcher, the
  proxy, the engine nor the store. The package's version is looked up only when something asks for
  it; that lookup alone took longer than everything the status line does.
- It does not parse the whole log. It reads the file once and parses only the lines that hold a
  launch, a proxy start or a tool call (found by searching the bytes), and from the end backwards
  the last requests: enough for the current prompt and for whether paging is stopped. The lines it
  parses are counted by the same code `contexto stats` uses, and the tests hold the two to the same
  answers on every log shape in `LOG_SHAPES`. When paging is stopped, or the log has no `launch` line,
  it reads the whole log as `contexto stats` does. When the session comes from stdin or
  `$CONTEXTO_SESSION` rather than `--session`, the log is read once more to find when
  the open launch started, by a search of the bytes for its `proxy_start` line (measured: 6.5 ms on a
  17 MB log).
- Pages are counted from the directory listing; no page file is opened.

A damaged log is read as far as it goes: a line cut short by a full disk is skipped. With no log at
all, the line shows the page count if there are pages, and nothing otherwise.

## What Claude Code does with a status line (measured)

Measured on 2026-11-12 at zero spend with real Claude Code 2.1.322 (not one of Contexto's tested
versions) and again with 2.1.318 (a tested version), in a pty against a local stub of the Messages
API, with a scratch `HOME`, `CLAUDE_CONFIG_DIR` and `CONTEXTO_HOME`. The results were the same on
both versions. `spikes/m2/statusline/` has the probe.

- A `statusLine` in the `--settings` file is run in an interactive session: once at start and again
  after the assistant message.
- It is not run in `-p` mode.
- With a `statusLine` in the user settings and another in the `--settings` file, only the
  `--settings` one ran and only its line was on screen. This is why Contexto adds none when you have
  one.
- A `statusLine` written to the user settings after the session started did not take over: the
  `--settings` command kept running.
- The command inherits the environment Claude Code was started with (a marker variable, `HOME` and
  `CLAUDE_CONFIG_DIR` all arrived), runs in the session's working directory, and gets the JSON on a
  stdin that is not a terminal. The command Contexto installs does not rely on any of that: it names
  the interpreter, the session and the store home in its arguments.
- `${VAR}` in the command was passed to the shell as written; Claude Code did not expand it.
- In a folder that is not trusted, the trust dialog was shown and the command did not run. Once
  the dialog was accepted it ran, in the same session (2.1.322 only).
- A command that prints nothing leaves an empty row where the line would be; with no `statusLine`
  at all there is no row (2.1.322 only).
- End to end, `contexto run claude` with an empty configuration showed `ctx bound 150k` at start and
  `ctx 100/150k` after the stub's first reply (the stub reports 100 input tokens), with a store path
  holding a space, quotes and a `$`. With a `statusLine` written to the user settings mid-session,
  the row changed to the restart notice.

Not measured: managed settings of any kind, Windows, and a login shell other than the one Claude
Code picked here.
