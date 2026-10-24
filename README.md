# Contexto

Contexto keeps a Claude Code or Codex session going indefinitely without compaction.

`contexto run claude` starts a small proxy on `127.0.0.1` and points Claude Code at it. Every model
request passes through the proxy, which keeps the prompt sent upstream under a token bound (150,000
by default). When a request gets within the headroom of the bound (25,000 tokens by default, raised
after large turns up to a quarter of the bound), the proxy moves the oldest turns out of the prompt and writes them, word for word, to a local store under `~/.contexto`. In their place the model sees a
short index of those turns and a pinned state of the session's decisions and open obligations.

The model reads old turns back with MCP tools: `contexto_search` finds turns by the words they contain and
`contexto_recall` returns it in full. The bound stays below the point where Claude Code would
compact the conversation itself, so nothing is summarized away. The full history stays on disk and
can be inspected with `contexto sessions`, `contexto recall`, and `contexto state`.

Contexto sends no extra model calls. It reads and rewrites the requests your CLI already makes.

## Install

Contexto needs Python 3.12 or later and has no runtime dependencies. Install the wheel from the
latest release, [v0.2.8](https://github.com/VentureForgre/contexto/releases/tag/v0.2.8). It has two
assets: `contexto-0.2.8-py3-none-any.whl` and `contexto-0.2.8.tar.gz`. The repository is private, so
download with a `gh` login that can read it:

```sh
gh release download v0.2.8 --repo VentureForgre/contexto --pattern 'contexto-0.2.8-py3-none-any.whl'
uv tool install ./contexto-0.2.8-py3-none-any.whl     # or: pipx install ./contexto-0.2.8-py3-none-any.whl
contexto --version                                    # contexto 0.2.8
```

`pipx` installs with its default Python. If that is older than 3.12, add `--python python3.12`. To
install from a checkout instead, run `uv tool install .` in the repository root.

You also need `claude` (Claude Code) or `codex` (Codex CLI) on `PATH`.

## Quickstart

### Claude Code

```sh
contexto run claude
contexto run --bound 120000 claude --model claude-haiku-5-5
```

Contexto options go before `claude`. Everything after `claude` is passed to Claude Code unchanged.
The session id is printed on stderr at start. The launcher:

1. resolves the upstream Claude Code would otherwise use (see [Troubleshooting](#troubleshooting));
2. opens the session under `$CONTEXTO_HOME/sessions/<id>/` and starts the proxy on
   `127.0.0.1:<free port>`;
3. writes two temporary files (mode 0600, deleted on exit): a `--settings` file that sets only
   `env.ANTHROPIC_BASE_URL` to the proxy, and an `--mcp-config` file that starts Contexto's MCP
   server (`python -P -m contexto mcp --session <id>` with the same Python, and `CONTEXTO_HOME` in
   its environment; `-P` keeps your project directory off the import path);
4. runs `claude --settings <file> --mcp-config <file> [your args]` in your terminal and returns its
   exit code.

Your own Claude Code settings, hooks, and MCP servers still load, and nothing in your Claude
configuration is edited. Ctrl+C reaches Claude Code as usual; SIGTERM and SIGHUP are forwarded to it.

### Codex (API-key mode)

```sh
contexto run codex
contexto run --codex-key-helper 'my-key-command' codex
```

Codex gets its key from a helper command that prints it: `--codex-key-helper`, else
`$CONTEXTO_CODEX_KEY_HELPER`, else the `apiKeyHelper` from your Claude Code settings. Codex runs the
helper itself; Contexto never reads or stores the key.

`contexto run codex` sets `CODEX_HOME` to `$CONTEXTO_HOME/sessions/<id>/codex-home` (mode 0700) and
writes a `config.toml` there (mode 0600) that:

- adds a model provider `contexto` whose `base_url` is the proxy, with `wire_api = "responses"`,
  `supports_websockets = false`, and the key helper as its auth command;
- turns request compression off (`features.enable_request_compression = false`);
- adds the MCP server `contexto` (`python -P -m contexto mcp --session <id>` with the same Python)
  with `default_tools_approval_mode = "approve"`, so recall works in `codex exec` too, and `CONTEXTO_HOME`
  in its `env` table, because Codex starts MCP servers with a restricted environment.

On exit the `config.toml` is deleted and the directory is kept, so Codex can resume its rollouts.
Because `CODEX_HOME` is new, your own `~/.codex` configuration is not loaded in this mode.

Before v0.1.2, the Codex MCP server was not given `CONTEXTO_HOME`. With a non-default home, the proxy
wrote the session to `$CONTEXTO_HOME/sessions/<id>/` while the MCP tools looked in
`~/.contexto/sessions/<id>/` and did not find it. Upgrade if you use a non-default home with Codex.

Before v0.2.8, the Codex MCP server was started as a bare `contexto` looked up on `PATH`. When
`contexto` was not on `PATH` (for example `.venv/bin/contexto run codex` without activating the
venv), Codex ran with no Contexto tools and reported no error, so recall and search were missing.
Because Codex starts MCP servers with a restricted environment, install Contexto (an editable
install is fine) rather than running it from a checkout with `PYTHONPATH=src`.

Codex requests go to `--codex-upstream`, else `$CONTEXTO_CODEX_UPSTREAM`, else (API-key mode) the
upstream Claude Code would use plus `/v1`, or (ChatGPT mode) the ChatGPT Codex backend.

ChatGPT login mode (`--codex-auth chatgpt`) uses your own `CODEX_HOME` with `-c` overrides instead. It
is tested only with synthetic credentials, never live, and whether end users may route ChatGPT
credentials this way under OpenAI's terms is an open question.

### Upstreams and CLIProxyAPI

If you run Contexto yourself, the supported upstreams are the vendor's API with your own API key, or
a [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) instance reached with one of its client
keys (D35). Subscription logins live in CLIProxyAPI or the vendor's own CLI, never in Contexto, and
you must follow your providers' terms. For CLIProxyAPI, set Claude Code's `ANTHROPIC_BASE_URL` to its
root (for example `http://127.0.0.1:8317`, no `/v1`) and its `apiKeyHelper` or
`ANTHROPIC_AUTH_TOKEN` to the client key; `contexto run claude` and `contexto run codex` then both go
through it. Setup, checks, and what was tested: [docs/cliproxyapi.md](docs/cliproxyapi.md).

## Bound and configuration

`contexto run` options (all go before `claude` or `codex`):

| Option | Default | Meaning |
|---|---|---|
| `--bound N` | 150,000 | Upstream prompt bound in tokens. Above 170,000 needs `--allow-large-bound`. |
| `--allow-large-bound` | off | Allow `--bound` above 170,000 (Claude Code may then compact on its own). |
| `--window-fraction F` | 0.5 | Share of the window budget kept as recent turns after a rotation. |
| `--headroom-floor N` | 25,000 | Minimum headroom below the bound that triggers a rotation. |
| `--session ID` | a new id | Session id; reuse one to continue a session's store. |
| `--upstream URL` | resolved | Upstream base URL for Claude Code requests. |
| `--observe-thinking` | off | Adds the `thinking-binding-controls-2026-08-01` beta so dropped thinking is logged. This changes the request by one beta flag. |
| `--codex-auth MODE` | `apikey` | `apikey` or `chatgpt` (see Quickstart). |
| `--codex-key-helper CMD` | see Quickstart | Command that prints the API key (Codex, API-key mode). |
| `--codex-upstream URL` | see Quickstart | Upstream for Codex requests, including its version path. |

**Tip for Haiku 5.5:** on Haiku 5.5, `--bound 125000` keeps requests at turn start below the 100K price
tier, above which every rate is 5x. Replaying live run #3 at that bound with the model's behavior held
fixed, every request stayed under it, for a modeled 45.7% lower cost. Long tool loops can still cross
it, and accuracy at that bound is not yet measured. See
[docs/research/M2-haiku-100k-tier.md](docs/research/M2-haiku-100k-tier.md).
The Quickstart's `--bound 120000` is also below the tier at turn start: with the default headroom floor
(25,000) and minimum turn room (12,000), any bound up to 125000 is.

`contexto proxy [--session ID] [--port N] [--upstream URL] [--observe-thinking]` runs the proxy alone,
mainly for tests. `--port` defaults to 0 (a free port). It prints
`listening on 127.0.0.1:<port> session <id> upstream <source>` and forwards requests unchanged.

Environment variables:

| Variable | Used by | Meaning |
|---|---|---|
| `CONTEXTO_HOME` | all commands | Data directory (default `~/.contexto`), resolved to an absolute path. `contexto run` passes it to the MCP server for both Claude Code and Codex. `contexto mcp --home` overrides it. |
| `CONTEXTO_SESSION` | `recall`, `state` | Session to read when `--session` is not given. |
| `CONTEXTO_CODEX_KEY_HELPER` | `run codex` | Key helper command when `--codex-key-helper` is not given. |
| `CONTEXTO_CODEX_UPSTREAM` | `run codex` | Codex upstream when `--codex-upstream` is not given. |
| `CONTEXTO_RECALL_VARIANT` | new sessions | `default` or `legacy` recall guidance text. `legacy` is kept for diagnosis. A session keeps the variant it started with. |
| `ANTHROPIC_BASE_URL` | `run`, `proxy` | Upstream, when no settings file sets one (see Troubleshooting). |
| `OPENRIG_SESSION_NAME` | `run` | When set, the start-up window check assumes 92,000 tokens of fixed overhead instead of 55,000. |

At start, `contexto run` warns on stderr when the bound leaves a window budget under 25,000 tokens
(bound minus headroom floor minus the assumed fixed overhead) and suggests a larger `--bound`. It
warns again, once, if the measured overhead does the same.

## Inspecting sessions

These commands read a session's store under `$CONTEXTO_HOME/sessions/<id>/` and never change it.
`recall` and `state` pick the session from `--session ID`, else `CONTEXTO_SESSION`, else the session
with the most recent activity, and print the choice to stderr (for example
`session 7f3a (most recent)`), so stdout carries only the text.

- `contexto sessions list [--json]`: one row per session, most recent activity first: id, created
  time, page count, ledger record count (`?` if the ledger cannot be read), and last activity, in UTC.
- `contexto sessions show ID [--json]`: created and last activity times; page, record, active
  record, and open obligation counts; session-log event, over-bound turn, and upstream 4xx counts;
  the last epoch the proxy logged; and whether the ledger verifies. `--json` adds event counts by type.
- `contexto recall PAGE_ID [--session ID] [--offset N]`: a paged-out turn as plain text, thinking
  omitted, exactly as the MCP `contexto_recall` tool renders it. Each chunk is at most 32,000
  characters and about a quarter of the session's window budget, or 12,000 characters until the proxy
  has recorded the window. When more remains, stderr says
  `[N more characters: rerun with --offset M; <how the chunk size was chosen>]`.
- `contexto state [--session ID]`: the full pinned state, the same text as the MCP `contexto_state`
  tool but never cut by a recall budget. It hashes each file a record cites as provenance, inside the session's
  recorded working directory, to mark changed files `STALE`. It never writes there.

## MCP tools the model gets

`contexto run` registers `contexto mcp --session <id>` with the CLI, which starts it over stdio. You do
not start it by hand.

| Tool | What it does |
|---|---|
| `contexto_search(query)` | Finds turns holding every word of the query, in any case and any order (words match as substrings, so identifiers and paths work; no stemming, no fuzzy or similarity matching). Turns holding the whole query rank first, then by relevance (BM25) with newer turns preferred among similar ones; paged context blocks (`<system-reminder>` text paged on its own) rank after every turn. A query with no words finds nothing. Matching lines from the conversation still in view come first (at most 3, newest first), skipping the current turn's own user message. Then the 3 best matching paged-out turns (the newest matching turn is always one of them), newest first, each with its last 3 matching lines in page order and their offsets, then up to 50 more matching turns, newest first, as page id and the offsets of their first and last matches (`@first..last`, one offset when they are the same). In both parts, paged context blocks are shown after the turns, under their own line, because a block's turn number is when it was paged, not when it was sent. Each reply ends with a note on what was searched |
| `contexto_recall(page_id, offset?)` | A paged-out turn word for word, thinking omitted. Each call returns at most 32,000 characters and about a quarter of the window budget (12,000 characters until the proxy has recorded the window), and at most half of the turn's remaining recall budget; continue with `offset` |
| `contexto_pin(kind, text, supersedes?, epistemic?, provenance?)` | Record an event, decision, obligation, artifact, or reference in the session ledger. `epistemic` is `verified`, `reported`, `summary`, or `hypothesis`. A verified event, artifact, or reference shows as a fact in the pinned state; decisions and obligations keep their own groups |
| `contexto_close(obligation_id, note)` | Close an open obligation |
| `contexto_state()` | The full pinned state, with no pin-budget cap. It is charged against the turn's recall budget and cut at a whole line, with a trailer, when the budget runs out |

Each turn has a recall budget: the bound minus the tokens the turn already uses minus the headroom
floor. At a turn start the proxy rotates when needed to leave at least 12,000 tokens of it. Search,
recall, and state replies are charged against it. Tool calls are logged with names, ids, sizes, and
character ranges, never the text.

## Troubleshooting

**Which upstream does `contexto run` use?** The first of: `--upstream`; `env.ANTHROPIC_BASE_URL` from
the first Claude Code settings file that sets it (in a git worktree, the main checkout's
`.claude/settings.local.json` first, then the project's `.claude/settings.local.json`, then
`.claude/settings.json`, then `~/.claude/settings.json`); `ANTHROPIC_BASE_URL` in the environment;
`https://api.anthropic.com`. The session log records which source won, never the URL.

**Requests bypass the proxy.** Organization-managed Claude Code settings outrank the launcher's
`--settings`. If they set `ANTHROPIC_BASE_URL`, Claude Code talks to that URL directly.

**`--settings` is refused (exit 2).** Claude Code accepts one `--settings`, and the launcher uses it to
route requests through the proxy. Put those settings in your Claude settings files instead. Your own
`--mcp-config` is allowed and loads alongside Contexto's.

**"warning: ... running without Contexto's tools".** The model has no `contexto_search` or
`contexto_recall`, so it cannot get paged-out turns back. Neither Claude Code nor Codex reports an MCP
server that fails to start, so the proxy checks the main conversation (subagents and side requests do
not count). The launcher prints the warning when it first finds the tools missing, during the run or
when the CLI exits. If the session ends still without them, it prints the warning again after the CLI
exits ("warning (repeated from the session)"), because an interactive session can draw over a line
printed mid-run. The session log records it as `mcp_tools_missing`. Likely causes:

- *The MCP server did not start.* Run the command the warning names (`<python> -P -m contexto mcp
  --help`); it should print the `contexto mcp` usage. Before v0.2.8, `contexto run codex` registered
  the server as a bare `contexto` command, and Codex ran without the tools whenever `contexto` was not
  on its PATH; upgrade. In Claude Code, `/mcp` shows the server's status.
- *Claude Code started the server, but its tools are gone.* The server may have stopped after
  starting: `/mcp` in Claude Code shows whether it is still connected. Otherwise a permission deny rule
  or `--disallowedTools` that matches `mcp__contexto__` tools removes them.
- *The server started too slowly.* Codex 0.163.0 does not wait long for MCP servers: in a
  zero-spend test, `codex exec` sent its only request about 1 second in, before a server that took 3
  seconds was up, and that turn ran without the tools.

How it is checked: in Claude Code's requests the tools appear by name, in the tool list or, with tool
search on, in the deferred-tools list. When the server goes away later, Claude Code keeps the old list
in the conversation and adds a notice that the tools are no longer available or that the server
disconnected, so the proxy reads every system message in order and the latest notice wins. Codex
never names MCP tools in its requests, so for Codex the MCP server records in the session when it
answers Codex's tool list request, and the proxy checks for that record. For Codex, a server that
crashes after it answered cannot be detected: the record stays.

**"window budget about N tokens ... is below 25,000".** The bound leaves little room for turns after
the fixed overhead, so rotations page often. Start again with the suggested `--bound`.

**502 "contexto proxy: upstream unreachable (...)".** The proxy could not reach the upstream. It opens
a new connection per request and retries once when the connection, TLS handshake, or request send
fails. A failure after the request was fully sent and before the response head is returned as 502
without a retry, and the CLI's own retry takes over; a failure after the response started ends the
stream without its terminator. The session log records which step failed. "contexto proxy: internal error"
is a Contexto bug; please report it.

**415 "cannot read a request body with Content-Encoding ...".** The proxy decodes gzip and deflate
(and zstd on Python 3.14+) and refuses other encodings. Run the CLI through `contexto run`, which
turns Codex's request compression off.

**`count_tokens` returns 403.** Errors from the upstream pass through unchanged. Some proxies refuse
`/v1/messages/count_tokens`; Claude Code calls it only around compaction.

**Where are the logs?** `$CONTEXTO_HOME/sessions/<id>/log.jsonl`, one JSON object per event, with sizes,
counts, and event names but no prompt or response text. Paged-out turns are in `pages/` and the
pinned records in `ledger.jsonl` in the same directory.

## Known limitations

Claude Code acceptance ran on one model, one bound, and one upstream. Codex passed acceptance
criteria 1 to 3 on one model at bound 64,000; the default bound 150,000 is untested on Codex. Cache
efficiency depends on the upstream route. Search keeps each page's searchable text in the MCP
server's memory for the life of the process (about the UTF-8 size of the page text; peak RSS 293 MB
at 6,000 synthetic pages), and the first search of each MCP process reads every page once (0.74 s at
6,000 pages). Each query word scans that text once, so a long query with matches costs more. The
full list is in
[STATUS.md, "Known limitations (v0.2.8)"](STATUS.md#known-limitations-v028).

## Exit codes

| Command | Code | Meaning |
|---|---|---|
| `run` | the CLI's | Claude Code's or Codex's own exit code |
| `run` | 128 + N | The CLI died from signal N |
| `run` | 1 | The proxy could not start |
| `run` | 2 | Usage error, for example `--bound` above 170,000, a `--settings` after `claude`, or no Codex key helper or upstream |
| `run` | 127 | `claude` or `codex` is not on `PATH` |
| `sessions`, `recall`, `state` | 0 | Success (an empty session list is a success) |
| `sessions`, `recall`, `state` | 1 | Unknown or invalid session id, unknown page id, an offset past the end of the page, or an unreadable ledger |
| any | 2 | Unknown command or option, or a missing argument (`contexto` with no command prints help and exits 2) |

Development, tests, and releases: see [CONTRIBUTING.md](CONTRIBUTING.md).
