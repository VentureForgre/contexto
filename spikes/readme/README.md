# README assets

The scripts that make the images under `docs/assets/`. Nothing here calls a model, and nothing is a
runtime dependency of the package: every script uses the Python standard library only.

| File in `docs/assets/` | Made by | From |
|---|---|---|
| `request-size-light.svg`, `request-size-dark.svg` | `make_chart.py` | `docs/research/data/M2-usage-savings-run3.csv` and six measured counts from `docs/research/M2-usage-savings-run3.md` |
| `demo.cast.json`, `demo.txt`, `demo.svg` | `record_demo.py` | one run of this checkout's `contexto` against stand-ins |

## The chart

```sh
python spikes/readme/make_chart.py
```

It reads the 44 requests of live acceptance run 3 (Claude Code 2.1.301, claude-haiku-5-5, bound
150,000; [the record](../../docs/research/M2-usage-savings-run3.md)) and plots two lines over the 24
turns:

- **Contexto (measured)**: for each turn, the largest `contexto_input_total` of its requests, the
  input tokens Contexto sent. Largest: 109,868, in turn 20.
- **Full history without Contexto (turns 1 to 18 modeled, 19 to 24 measured)**: what plain Claude Code
  would have sent, one request a turn. This is the record's "plain-run shape", which it calls the fair
  comparison. Turns 1 to 18 are the CSV's `plain_input_modeled` (turn 18: 433,633). Turns 19 to 24 are
  the baseline arm's measured `count_tokens`, 435,590 to 438,406, kept in `make_chart.py` as
  `BASELINE_PROBE_TOKENS` with the record's line. Each measured turn has a dot. Last and largest:
  438,406.

The CSV's `plain_input_modeled` for turns 19 to 24 (up to 493,902) is not plotted. It is the history
at the points where Contexto sent a request, search tool loops included, and the record says it
overstates a plain run.

The bound, 150,000, is a horizontal line. Turns 19 to 24 are the probe turns; the chart calls them
"Recall questions". A note marks turn 4, the first turn where Contexto's request is smaller than the
turn before: "old turns moved to disk".

The two files have the same geometry and a transparent background. The light one is for GitHub's
light page (`#ffffff`), the dark one for its dark page (`#0d1117`). Embed them with `<picture>`:

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/request-size-dark.svg">
  <img src="docs/assets/request-size-light.svg" width="800" alt="...">
</picture>
```

The output is deterministic: the same CSV gives the same bytes, and a test fails if the committed
files differ from a fresh run. Another test reads the record and fails if the six measured counts are
not on the line `make_chart.py` cites.

## The recording

```sh
uv run python spikes/readme/record_demo.py
```

That runs one session (about 30 seconds) and writes the three `demo.*` files. Run it from the
checkout with `uv run`, so the `contexto` it records is this checkout's code. It needs macOS or
Linux, port 8399 free on `127.0.0.1` (or `--port N`), and no network.

The recorder starts the session only after the capture holds the fake upstream's own "listening"
line. If the upstream exits first (the port is taken) or the line does not arrive in 20 seconds, the
recorder stops with an error and sends nothing. A session still running after 120 seconds is killed
with its children, and the error holds the captured text. The stub waits at most 30 seconds for an MCP
reply or its shell command and 60 for the proxy.

### What is real and what is a stand-in

| Part | What it is |
|---|---|
| `contexto run`, the proxy, the engine, the session store | this checkout's code, started by the command the recording shows |
| `contexto sessions show` | this checkout's code, run by the stub as a shell command inside the session, piped through `grep` and `cut` as the recording shows |
| `contexto mcp` and its `contexto_search` | this checkout's code, started from the `--mcp-config` the launcher wrote |
| `claude` | `stub_claude.py`. It sends the turns of `demo_script.py`, runs the tool call it is told to, and prints lines that start with `[stub claude]` |
| the model API | `demo_upstream.py` on `127.0.0.1`. It forwards nothing and prints lines that start with `[fake upstream]` |
| the "model" | three rules in `demo_upstream.scripted_reply`: answer `OK`; on the scripted question call `contexto_search`; on a tool result repeat the name found in it |

The session has a temporary `HOME` and `CONTEXTO_HOME`, a `PATH` of two wrappers (`contexto` and the
stub as `claude`) plus `/usr/bin:/bin`, and a 90-column pseudo-terminal that all three processes write
to. The recorder keeps each chunk of output with its arrival time. It adds two things of its own: the
note line at the top, and the command line, which it then runs with `/bin/sh -c` exactly as shown.

`contexto sessions show` prints 13 summary lines and then the page list, where each page line holds
the opening 200 characters of its turn. The stub runs it through a filter and prints the whole
command, one labelled row per line:

```sh
contexto sessions show $CONTEXTO_SESSION \
    | grep -E 'Pages:|Over-bound|pg_' | cut -c1-74
```

`contexto run` sets `CONTEXTO_SESSION` for the CLI and its shell commands. The `grep` keeps the page
count, the over-bound count and the page lines. The `cut` ends each page line after the turn's
first line. What the recording shows under the command is that pipe's output, unchanged.

### What the numbers mean

There is no tokenizer without a model. The stub and the fake upstream both count one token per 3.5
bytes of request body, the rate Contexto's estimator assumes for text before it has seen real usage.

- `history N tokens` (stub): the size of the request the stub sent to the proxy, which holds the whole
  conversation, as Claude Code's requests do.
- `received N tokens` (fake upstream): the size of the request that reached it after the proxy. It
  returns the same number as `usage.input_tokens`, so the engine measures against it.

The bound is 60,000 with `--headroom-floor 5000`, so the launcher prints no small-budget warning and
the session still rotates at turn 3. Each turn is about 12,000 tokens of made-up build log. Turn 1
ends with a made-up fact: "The release codename is amber-kestrel-17." It is placed after the log, so
it is not in the opening 200 characters that the page index keeps in view. The stub numbers turns from
0, as Contexto does, so the stub's "turn 1", the page list's `turn 1` and the search result's
`(turn 1)` are the same turn. The scripted text names a build step with the same number and does not
say "turn".

The stub pauses before each step. It pauses about 2 seconds after the first turn where the upstream
received less than the stub sent (it reads that from the reply's usage), because that line is the
point of the recording.

`--no-version-check` is in the command because the stub is not a Claude Code version. The stub sends
no `X-Claude-Code-Session-Id`, so `contexto run` prints no resume hint at exit. The session runs with
`CONTEXTO_QUIET=1`, so it prints no run summary at exit either: the summary's run time differs at
every recording, so a committed recording that held it could not match a fresh one.

### What is and is not deterministic

Same on every run: the turns, every `history` and `received` figure, the page count, the search
result's text, and the order of the lines. A test records a fresh session without pauses and compares
it with the committed transcript, with the session id, the page ids and the port masked.

Different on every run: the session id, the page ids (the search result quotes one), and the arrival
times. `--port` changes the port in two lines. The filter drops the lines of `sessions show` that
hold timestamps and the log event count, so the recording shows neither.

### Rendering

`termsvg.py` turns the capture into the SVG: one `<text>` element per terminal row, shown at its time
with a CSS animation, no scripts. It never changes text. A line wider than 90 columns wraps. A pause
longer than 4 seconds is cut to 4, and the last frame is held for 8 seconds, then the loop restarts.
Colour marks who printed a line: stub and fake-upstream lines are dim after their coloured label, the
tool's own output is bright, and the planted fact is amber. In a turn line the two sizes, `history`
and `received`, are bright too. The top bar says "synthetic session" for the whole loop.

The note line is there from the start of the loop, so a viewer that stops at time 0 shows it and not
an empty terminal. The terminal is 35 rows and the recording is 39, so the last frame runs from the
`contexto: session` line to the last reply. A viewer that does not animate, or one with reduced motion
set, shows that last frame.

To change the look without a new capture:

```sh
uv run python spikes/readme/record_demo.py --render-only
```

It rebuilds `demo.txt` and `demo.svg` from `demo.cast.json`. A test fails if the committed files are
not what the capture renders to. `--still SECONDS FILE` also writes one frame without animation, for
checking the layout as a PNG.

Before writing, the recorder refuses a capture that holds the user name, the home directory, the host
name or its temporary directory.

## Tests

`spikes/readme/tests/test_readme_assets.py` runs in the main suite (about 5 seconds on a quiet
machine, most of it the fresh recording). It needs neither `claude` nor `codex`.

```sh
uv run --with pytest pytest -q spikes/readme
```
