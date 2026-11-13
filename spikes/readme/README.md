# README assets

The scripts that make the images under `docs/assets/`. Nothing here calls a model, and nothing is a
runtime dependency of the package: every script uses the Python standard library only.

| File in `docs/assets/` | Made by | From |
|---|---|---|
| `request-size-light.svg`, `request-size-dark.svg` | `make_chart.py` | `docs/research/data/M2-usage-savings-run3.csv` |
| `demo.cast.json`, `demo.txt`, `demo.svg` | `record_demo.py` | one run of this checkout's `contexto` against stand-ins |

## The chart

```sh
python spikes/readme/make_chart.py
```

It reads the 44 requests of live acceptance run #3 (Claude Code 2.1.301, claude-haiku-5-5, bound
150,000; [the record](../../docs/research/M2-usage-savings-run3.md)) and plots, for each of the 24
turns, the largest request:

- **Contexto (measured)**: `contexto_input_total`, the input tokens Contexto sent. Largest: 109,868.
- **Full history without Contexto (modeled)**: `plain_input_modeled`, what plain Claude Code would
  have sent at the same request. Largest: 493,902.

The bound, 150,000, is a horizontal line. Turns 19 to 24 are the probe turns.

The two files have the same geometry and a transparent background. The light one is for GitHub's
light page (`#ffffff`), the dark one for its dark page (`#0d1117`). Embed them with `<picture>`:

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/request-size-dark.svg">
  <img src="docs/assets/request-size-light.svg" width="800" alt="...">
</picture>
```

The output is deterministic: the same CSV gives the same bytes, and a test fails if the committed
files differ from a fresh run.

## The recording

```sh
uv run python spikes/readme/record_demo.py
```

That runs one session (about 25 seconds) and writes the three `demo.*` files. Run it from the
checkout with `uv run`, so the `contexto` it records is this checkout's code. It needs macOS or
Linux, port 8399 free on `127.0.0.1` (or `--port N`), and no network.

### What is real and what is a stand-in

| Part | What it is |
|---|---|
| `contexto run`, the proxy, the engine, the session store | this checkout's code, started by the command the recording shows |
| `contexto sessions show` | this checkout's code, run by the stub as a shell command inside the session |
| `contexto mcp` and its `contexto_search` | this checkout's code, started from the `--mcp-config` the launcher wrote |
| `claude` | `stub_claude.py`. It sends the turns of `demo_script.py`, runs the tool call it is told to, and prints lines that start with `[stub claude]` |
| the model API | `demo_upstream.py` on `127.0.0.1`. It forwards nothing and prints lines that start with `[fake upstream]` |
| the "model" | three rules in `demo_upstream.scripted_reply`: answer `OK`; on the scripted question call `contexto_search`; on a tool result repeat the name found in it |

The session has a temporary `HOME` and `CONTEXTO_HOME`, a `PATH` of two wrappers (`contexto` and the
stub as `claude`) plus `/usr/bin:/bin`, and a 90-column pseudo-terminal that all three processes write
to. The recorder keeps each chunk of output with its arrival time. It adds two things of its own: the
note line at the top, and the command line, which it then runs with `/bin/sh -c` exactly as shown.

### What the numbers mean

There is no tokenizer without a model. The stub and the fake upstream both count one token per 3.5
bytes of request body, the rate Contexto's estimator assumes for text before it has seen real usage.

- `history N tokens` (stub): the size of the request the stub sent to the proxy, which holds the whole
  conversation, as Claude Code's requests do.
- `received N tokens` (fake upstream): the size of the request that reached it after the proxy. It
  returns the same number as `usage.input_tokens`, so the engine measures against it.

The bound is 60,000 with `--headroom-floor 5000`, so the launcher prints no small-budget warning and
the session still rotates at its fourth turn. Each turn is about 12,000 tokens of made-up build log.
Turn 2 ends with a made-up fact, `amber-kestrel-17`. It is placed after the log, so it is not in the
opening 200 characters that the page index keeps in view. Contexto numbers turns from 0, so the stub's
turn 2 is the tool's `turn 1`.

`--no-version-check` is in the command because the stub is not a Claude Code version. The stub sends
no `X-Claude-Code-Session-Id`, so `contexto run` prints no resume hint at exit.

### What is and is not deterministic

Same on every run: the turns, every `history` and `received` figure, the page count, the search
result's text, and the order of the lines. A test records a fresh session without pauses and compares
it with the committed transcript, with the values below masked.

Different on every run: the session id, the page ids (the search result quotes one), the two
timestamps `sessions show` prints, the log event count if the launcher's logging changes, and the
arrival times. `--port` changes the port in two lines.

### Rendering

`termsvg.py` turns the capture into the SVG: one `<text>` element per terminal row, shown at its time
with a CSS animation, no scripts. It never changes text. A line wider than 90 columns wraps. A pause
longer than 4 seconds is cut to 4, and the last frame is held for 8 seconds, then the loop restarts.
Colour marks who printed a line: stub and fake-upstream lines are dim after their coloured label, the
tool's own output is bright, and the planted fact is amber. The top bar says "synthetic session" for
the whole loop. A viewer that does not animate, or one with reduced motion set, shows the last frame.

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

`tests/test_readme_assets.py` runs in the main suite (about 3 seconds on a quiet machine, most of it
the fresh recording). It needs neither `claude` nor `codex`.
