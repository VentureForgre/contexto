# Contexto

When a Claude Code or Codex conversation grows too large for the model's context window, the CLI
compacts it: it replaces the history with a summary, and details are lost. Contexto stops that from
happening. It runs a small proxy on your machine between the CLI and the model API, keeps every
request under a fixed size, moves the oldest turns out of the prompt into a store on disk word for
word, and gives the model tools to search and read them back. A session can then go on for as long
as you like without compacting, and the model can still find what was said hours ago.

## How it works

`contexto run claude` starts the proxy on `127.0.0.1` and points Claude Code at it. Every model
request passes through the proxy, which keeps the prompt sent upstream under the **bound**: the most
tokens Contexto lets a request's prompt reach, 150,000 by default.

The proxy keeps some **headroom** free below the bound for the conversation to grow into: 25,000
tokens by default, raised after large turns, up to a quarter of the bound. When a request gets
within the headroom of the bound, the proxy **rotates**: it moves the oldest turns out of the prompt
and writes them, word for word, to a local store under `~/.contexto`. Those turns are now **paged
out**. In their place the model sees a short index of the paged-out turns and the **pinned state**:
the decisions, obligations and facts the model recorded with the `contexto_pin` tool.

The room left for conversation turns is the **window budget**: the bound, minus the headroom floor,
minus the fixed overhead of every request (the CLI's system prompt and tool definitions).

The model reads paged-out turns back with MCP tools: `contexto_search` finds turns by the words they
contain, and `contexto_recall` returns one in full. The bound stays below the point where Claude Code
would compact the conversation itself, so nothing is summarized away.

Contexto makes no model calls of its own. It reads and rewrites the requests your CLI already makes.

What it includes:

- Claude Code and Codex (API-key mode) through `contexto run`.
- Upstreams: the vendor's API with your own key, or a CLIProxyAPI instance
  ([below](#upstreams-and-cliproxyapi)).
- Word search over paged-out turns ([MCP tools](#mcp-tools-the-model-gets)).
- A separate recall budget for each subagent, so subagents cannot use up the main conversation's
  room.
- `contexto sessions list`, `show` and `prune` to see sessions and delete old ones.
- A per-session `X-Session-Affinity` header for proxies that spread requests over several accounts,
  so a session's prompt cache stays on one ([Troubleshooting](#troubleshooting)).
- A warning when Claude Code or Codex is a version Contexto was not tested with. New CLI versions
  come out every week, so expect to see it; it does not stop the launch.

## Results

Each acceptance run is a scripted session whose history reaches at least 2.5 times the bound. Its
probe turns ask 54 questions about details spread through that history. The same questions then go
to the same model with the whole history in its context (the baseline).

| Run | Result | Record |
|---|---|---|
| Claude Code 2.1.301, claude-haiku-5-5, bound 150,000 | 51 of 54 answered correctly, against 48 for the baseline. No compaction. Largest request 109,868 tokens. Prompt cache reused on 36 of 37 eligible requests. | [M2-acceptance-run3.md](docs/research/M2-acceptance-run3.md) |
| Claude Code 2.1.302, claude-opus-5-5, bound 150,000 | 53 of 54, against 54 for the baseline. No compaction. Cache reused on 54 of 55 eligible requests. The run added the permission rule that `contexto run` now sets itself. | [M2-acceptance-opus.md](docs/research/M2-acceptance-opus.md) |
| Codex 0.163.0, gpt-6-astra, bound 150,000 | History of 375,922 tokens (2.5 times the bound) with no compaction. Largest request 108,565 tokens. All 61 requests succeeded. No baseline was run. | [M2-codex-acceptance-150k.md](docs/research/M2-codex-acceptance-150k.md) |

In the claude-haiku-5-5 run, Contexto used 46.6% fewer input tokens than plain Claude Code would have
for the same session, and cost 49.7% less. The plain Claude Code figures are modeled from measured
requests, not run end to end ([M2-usage-savings-run3.md](docs/research/M2-usage-savings-run3.md)).

## Install

Contexto needs Python 3.12 or later, macOS or Linux, and has no runtime dependencies. Install the
wheel from the latest release, [v0.5.8](https://github.com/VentureForgre/contexto/releases/tag/v0.5.8).
It is not on PyPI: the PyPI package named `contexto` is an unrelated project, so
`pip install contexto` installs the wrong thing. Use the release URL or the downloaded wheel.

Straight from the release URL, with no GitHub login:

```sh
pipx install https://github.com/VentureForgre/contexto/releases/download/v0.5.8/contexto-0.5.8-py3-none-any.whl
contexto --version                                    # contexto 0.5.8
```

`pip install <the same URL>` works too, inside an active Python 3.12+ virtual environment.

Or download it first with the GitHub CLI:

```sh
gh release download v0.5.8 --repo VentureForgre/contexto --pattern 'contexto-0.5.8-py3-none-any.whl'
uv tool install ./contexto-0.5.8-py3-none-any.whl     # or: pipx install ./contexto-0.5.8-py3-none-any.whl
```

`pipx` installs with its default Python. If that is older than 3.12, add `--python python3.12`.

You also need `claude` (Claude Code) or `codex` (Codex CLI) on `PATH`.

### Checking the download

Contexto sees every prompt and every auth header your CLI sends, so check that the wheel came from
this repository's release workflow. Releases after v0.5.8 carry a `SHA256SUMS` asset and a GitHub
build provenance attestation:

```sh
gh release download <tag> --repo VentureForgre/contexto --pattern 'SHA256SUMS'
shasum -a 256 -c SHA256SUMS --ignore-missing          # Linux: sha256sum -c SHA256SUMS --ignore-missing
gh attestation verify contexto-<version>-py3-none-any.whl --repo VentureForgre/contexto
```

For v0.5.8 and earlier, compare `shasum -a 256 contexto-0.5.8-py3-none-any.whl` with the digest
GitHub records for the asset:
`gh release view v0.5.8 --repo VentureForgre/contexto --json assets --jq '.assets[] | .name + " " + .digest'`.

The build is reproducible: rebuilding the wheel from the release tag with the same uv version gives
the same bytes. [RELEASING.md](RELEASING.md#verifying-a-release) has the commands.

### From a checkout

`uv tool install .` in the repository root installs the checkout. It reports its version as
`0.0.0+checkout`, because only release builds carry a real version; include the commit
(`git rev-parse --short HEAD`) when you report a problem.

### Uninstall

`pipx uninstall contexto`, `uv tool uninstall contexto`, or `pip uninstall contexto`, matching how
you installed it. Your session data stays in `~/.contexto` (or `$CONTEXTO_HOME`). Delete that
directory to remove every stored conversation; see [What is stored and where](#what-is-stored-and-where).

### Platforms

CI tests Python 3.12 to 3.15 on Linux, and 3.12 and 3.15 on macOS. Windows is not supported: the
store locks files with `fcntl`, the launcher forwards POSIX signals (`SIGHUP`), and the store relies
on POSIX file modes (0700 directories, 0600 files).

## Quickstart

### Claude Code

```sh
contexto run claude
contexto run --bound 120000 claude --model claude-haiku-5-5
```

Contexto options go before `claude`. Everything after `claude` is passed to Claude Code unchanged.
Your own Claude Code settings, hooks and MCP servers still load, and nothing in your Claude
configuration is edited. Ctrl+C reaches Claude Code as usual. What the launcher does is under
[`contexto run`](#contexto-run).

### Codex (API-key mode)

```sh
contexto run codex
contexto run --codex-key-helper 'my-key-command' codex
```

Codex gets its key from a helper command that prints it: `--codex-key-helper`, else
`$CONTEXTO_CODEX_KEY_HELPER`, else the `apiKeyHelper` from your Claude Code settings. Codex runs the
helper itself; Contexto never reads or stores the key. The command is split into words the way a
shell would split it, then run without a shell, so `~`, `$VAR`, pipes and redirects are not
expanded. Wrap such a command as `sh -c '...'`. This applies to a reused `apiKeyHelper` too.

Codex requests go to `--codex-upstream`, else `$CONTEXTO_CODEX_UPSTREAM`, else (API-key mode) the
upstream Claude Code would use plus `/v1`, or (ChatGPT mode) the ChatGPT Codex backend. The API-key
default is right only for a CLIProxyAPI root. **With an OpenAI API key, pass
`--codex-upstream https://api.openai.com/v1`** (or set `CONTEXTO_CODEX_UPSTREAM`); otherwise Codex's
requests, and its key, go to `https://api.anthropic.com/v1`.

`contexto run codex` sets `CODEX_HOME` to `$CONTEXTO_HOME/sessions/<id>/codex-home` (mode 0700) and
writes a `config.toml` there (mode 0600) that:

- adds a model provider `contexto` whose `base_url` is the proxy, with `wire_api = "responses"`,
  `supports_websockets = false`, and the key helper as its auth command;
- turns request compression off (`features.enable_request_compression = false`);
- adds the MCP server `contexto` (`python -P -m contexto mcp --session <id>` with the same Python)
  with `default_tools_approval_mode = "approve"`, so recall works in `codex exec` too, and
  `CONTEXTO_HOME` (plus `CONTEXTO_SEARCH_CACHE_MB` when set) in its `env` table, because Codex starts
  MCP servers with a restricted environment.

On exit the `config.toml` is deleted and the directory is kept, so Codex can resume its rollouts.
Because `CODEX_HOME` is new, your own `~/.codex` configuration is not loaded in this mode. Install
Contexto (an editable install is fine) rather than running it from a checkout with
`PYTHONPATH=src`, because Codex starts the MCP server with a restricted environment.

ChatGPT login mode (`--codex-auth chatgpt`) uses your own `CODEX_HOME` with `-c` overrides instead. It
is tested only with synthetic credentials, never live. Follow your provider's terms for whatever
credentials you use with Contexto.

### Upstreams and CLIProxyAPI

If you run Contexto yourself, the supported upstreams are the vendor's API with your own API key, or
a [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) instance reached with one of its client
keys. Subscription logins live in CLIProxyAPI or the vendor's own CLI, never in Contexto, and you
must follow your providers' terms. For CLIProxyAPI, set Claude Code's `ANTHROPIC_BASE_URL` to its
root (for example `http://127.0.0.1:8317`, no `/v1`) and its `apiKeyHelper` to a command that prints
the client key; `contexto run claude` and `contexto run codex` then both go through it. With the key
in `ANTHROPIC_AUTH_TOKEN` instead, Codex also needs `--codex-key-helper`. Setup, checks, and what was
tested: [docs/cliproxyapi.md](docs/cliproxyapi.md).

## Is it working?

- **At start**, Contexto prints a session line on stderr, for example
  `contexto: session 7f3a... (bound 150,000, upstream default)` for Claude Code, or
  `contexto: session 7f3a..., engine rewrite, codex auth apikey, upstream ...` for Codex. The
  upstream is named by where it came from (`flag`, `env`, `default`, or a settings file), never by
  its URL.
- **In Claude Code**, `/mcp` lists the `contexto` server as connected.
- **Nothing else is visible until the prompt nears the bound**, about 125,000 tokens with the
  defaults. Then the proxy rotates for the first time.
- **After that**, `contexto sessions list` shows a PAGES count above 0 for the session, and
  `contexto sessions show <id>` gives the counts: pages, pinned records, over-bound turns and
  upstream 4xx errors. Over-bound turns and upstream 4xx should stay at 0.
- **If something is wrong**, Contexto prints a `warning:` line, for example when the model has no
  Contexto tools or Claude Code refused them. See [Troubleshooting](#troubleshooting).

## Rules you give once

When turns are paged out, the model keeps seeing the pinned state and the page index, which holds
the opening of each paged-out turn (about its first 200 characters). A standing rule ("from now on,
write dates in ISO format") stated at the start of a message stays in view through that index. A
rule given in the middle or at the end of a longer message is gone from view once its turn is paged
out, unless the model pinned it with `contexto_pin`. The model is told to pin decisions and obligations,
but it often does not do so on its own, and it rarely searches for a rule it does not know about.

If a rule matters:

- state it at the start of its own message, or
- ask the model to pin it ("pin that as an obligation"), and
- check with `contexto state`, which prints the pinned state.

## What is stored and where

Everything Contexto stores is under `~/.contexto`, or `$CONTEXTO_HOME` when set, one directory per
session in `sessions/<id>/`. Directories are mode 0700 and files 0600, so other users on the machine
cannot read them.

The store holds **raw conversation text**: your prompts, the model's replies, tool calls and
outputs, and any file contents or secrets that passed through the conversation. The paged-out turns
are in `pages/`. The recent turns still in view are in `retained.json`, the page index and pinned
state in `epoch.json`, and the pinned records in `ledger.jsonl`. For Codex sessions in API-key mode,
`codex-home/` holds Codex's own transcripts. `meta.json` records the working directory the session
started in. The session log, `log.jsonl`, holds event names, sizes and counts, never prompt or
response text. Auth headers and keys are never stored.

To delete one session's text, delete its whole directory or run
`contexto sessions prune --session <id> --yes`. Deleting only `pages/` leaves copies in the other
files. Deleting `~/.contexto` removes everything. Contexto deletes nothing on its own; old sessions
stay until you prune them ([Removing old sessions](#removing-old-sessions)).

The full file list is in [SECURITY.md](SECURITY.md#what-the-session-store-holds).

## `contexto run`

```
contexto run [options] claude [claude arguments]
contexto run [options] codex [codex arguments]
```

| Option | Default | Meaning |
|---|---|---|
| `--bound N` | 150,000 | The bound, in tokens. Above 170,000 needs `--allow-large-bound`. |
| `--allow-large-bound` | off | Allow `--bound` above 170,000 (Claude Code may then compact on its own). |
| `--window-fraction F` | 0.5 | Share of the window budget kept as recent turns after a rotation. |
| `--headroom-floor N` | 25,000 | The smallest headroom below the bound; a request within it triggers a rotation. |
| `--session ID` | a new id | Session id; reuse one to continue a session's store. |
| `--upstream URL` | resolved | Upstream base URL for Claude Code requests. |
| `--observe-thinking` | off | Adds the `thinking-binding-controls-2026-08-01` beta so dropped thinking is logged. This changes the request by one beta flag. |
| `--codex-auth MODE` | `apikey` | `apikey` or `chatgpt` (see [Codex](#codex-api-key-mode)). |
| `--codex-key-helper CMD` | see [Codex](#codex-api-key-mode) | Command that prints the API key (Codex, API-key mode). |
| `--codex-upstream URL` | see [Codex](#codex-api-key-mode) | Upstream for Codex requests, including its version path. |
| `--no-version-check` | off | Skip the check of `claude --version` or `codex --version` against the versions Contexto was tested with. |

For Claude Code, the launcher:

1. resolves the upstream Claude Code would otherwise use (see [Troubleshooting](#troubleshooting));
2. opens the session under `$CONTEXTO_HOME/sessions/<id>/` and starts the proxy on
   `127.0.0.1:<free port>`;
3. writes two temporary files (mode 0600, deleted on exit): a `--settings` file that sets only
   `env.ANTHROPIC_BASE_URL` to the proxy and allows Contexto's five MCP tools by name
   (`permissions.allow`; see [Troubleshooting](#troubleshooting)), and an `--mcp-config` file that
   starts Contexto's MCP server (`python -P -m contexto mcp --session <id>` with the same Python, and
   `CONTEXTO_HOME`, plus `CONTEXTO_SEARCH_CACHE_MB` when set, in its environment; `-P` keeps your
   project directory off the import path);
4. runs `claude --settings=<file> --mcp-config=<file> [your args]` in your terminal and returns its
   exit code. Each flag and its file are one argument, so a prompt right after `claude`
   (`contexto run claude "fix the tests"`) stays a prompt.

SIGTERM and SIGHUP are forwarded to the CLI.

**Your own `--settings` is refused** (exit 2), because Claude Code accepts only one and the launcher
needs it to route requests through the proxy. Put those settings in your Claude settings files
instead. Details are under [Troubleshooting](#troubleshooting).

At start, `contexto run` warns on stderr when the bound leaves a window budget under 25,000 tokens
and suggests a larger `--bound`. It warns again, once, if the measured overhead does the same.

**Tip for Haiku 5.5:** on Haiku 5.5, `--bound 125000` keeps requests at turn start below the 100K price
tier, above which every rate is 5 times higher. Replaying a recorded acceptance session at that bound,
with the model's behavior held fixed, every request stayed under it, for a modeled 45.7% lower
cost. Long tool loops can still cross it, and accuracy at that bound is not yet measured. See
[docs/research/M2-haiku-100k-tier.md](docs/research/M2-haiku-100k-tier.md).
The Quickstart's `--bound 120000` is also below the tier at turn start: with the default headroom floor
(25,000) and minimum turn room (12,000), any bound up to 125000 is.

### `contexto proxy`

`contexto proxy [--session ID] [--port N] [--upstream URL] [--observe-thinking]` runs the proxy alone,
mainly for tests. `--port` defaults to 0 (a free port). It prints
`listening on 127.0.0.1:<port> session <id> upstream <source>` and forwards requests unchanged, with
no paging.

### Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `CONTEXTO_HOME` | all commands | Data directory (default `~/.contexto`), resolved to an absolute path. `contexto run` passes it to the MCP server for both Claude Code and Codex. `contexto mcp --home` overrides it. |
| `CONTEXTO_SESSION` | `recall`, `state` | Session to read when `--session` is not given. |
| `CONTEXTO_CODEX_KEY_HELPER` | `run codex` | Key helper command when `--codex-key-helper` is not given. |
| `CONTEXTO_CODEX_UPSTREAM` | `run codex` | Codex upstream when `--codex-upstream` is not given. |
| `CONTEXTO_SEARCH_CACHE_MB` | `mcp` | Page text the MCP server holds in memory for `contexto_search`, in MB (default 64; `0` holds none). See [MCP tools](#mcp-tools-the-model-gets). |
| `CONTEXTO_RECALL_VARIANT` | new sessions | `default` or `legacy` recall guidance text. `legacy` is kept for diagnosis. A session keeps the variant it started with. |
| `ANTHROPIC_BASE_URL` | `run`, `proxy` | Upstream, when no settings file sets one (see Troubleshooting). |
| `CONTEXTO_NO_VERSION_CHECK` | `run` | `1` skips the CLI version check, like `--no-version-check`. |

## Inspecting sessions

These commands read a session's store under `$CONTEXTO_HOME/sessions/<id>/` and never change it
(except `sessions prune`, below).
`recall` and `state` pick the session from `--session ID`, else `CONTEXTO_SESSION`, else the session
with the most recent activity, and print the choice to stderr (for example
`session 7f3a (most recent)`), so stdout carries only the text.

- `contexto sessions list [--json]`: one row per session, most recent activity first: id, created
  time, page count, pinned record count (`?` if the ledger cannot be read), last activity in UTC,
  and size on disk, then the total size. `--json` gives the size as `size_bytes`.
- `contexto sessions show ID [--json]`: created and last activity times; page, record, active
  record and open obligation counts; session log event, over-bound turn and upstream 4xx counts; a
  counter that goes up at each rotation (`Last epoch`); and whether the ledger verifies. `--json`
  adds event counts by type.
- `contexto recall PAGE_ID [--session ID] [--offset N]`: a paged-out turn as plain text, thinking
  omitted, exactly as the MCP `contexto_recall` tool renders it. Page ids are the file names in
  `sessions/<id>/pages/` without `.json` (for example `pg_1cb04faa431985c5`). Each chunk is at most
  32,000 characters and about a quarter of the session's window budget, or 12,000 characters until
  the proxy has recorded the window. When more remains, stderr says
  `[N more characters: rerun with --offset M; <how the chunk size was chosen>]`.
- `contexto state [--session ID]`: the full pinned state, the same text as the MCP `contexto_state`
  tool but never cut by a recall budget. It is written for the model, so it starts with the
  model's instructions; with nothing pinned, that is all it shows. It hashes each file a record
  cites as provenance, inside the session's recorded working directory, to mark changed files
  `STALE`. It never writes there.

### Removing old sessions

Session directories hold the raw conversation and are never removed on their own.
`contexto sessions prune` deletes whole sessions. It is the one `sessions` command that changes the
store.

```
contexto sessions prune --older-than 30d          # list sessions idle for 30 days, with sizes
contexto sessions prune --older-than 30d --yes    # delete them
contexto sessions prune --keep 10 --yes           # keep the 10 most recently active, delete the rest
contexto sessions prune --session 7f3a --yes      # delete one session
```

- Without `--yes` nothing is deleted: it prints each selected session, its size, and the total.
- `--older-than` takes a positive whole number and a unit: `s`, `m`, `h`, `d`, or `w`. Age is the
  time since the last activity that `sessions list` shows. When several options are given, a session
  must match all of them.
- A session in use by a running `contexto run`, `contexto proxy`, or `contexto mcp` is skipped, and
  so is any session active in the last 10 minutes. Stop the session first. If you name it with
  `--session`, prune exits 1 with the reason on stderr.
- `sessions/` and each session must be real directories. Prune deletes through open directory
  handles and never follows a symlink, so it does not delete or create anything outside the session
  it checked, even if a path is swapped for a link while it runs.
- If one session cannot be deleted, prune reports it on stderr, goes on with the rest, and exits 1.
- `--json` prints `dry_run`, `total_bytes`, and each selected session with its `status`
  (`would_remove`, `removed`, `skipped`, or `failed`) and `reason`. If prune fails before selecting
  anything (for example an unknown `--session`), it prints `{"error": ...}` instead and exits 1.
- If you resume a session with `contexto run --session ID` while prune is deleting it, the resume
  waits, then starts the session empty and prints a warning.
- A launch that fails before the CLI starts, and `contexto mcp` given an unknown id, can leave an
  empty session behind. `prune --older-than` removes it later.

## MCP tools the model gets

`contexto run` registers `contexto mcp --session <id>` with the CLI, which starts it over stdio. You do
not start it by hand.

| Tool | What it does |
|---|---|
| `contexto_search(query)` | Finds turns that contain every word of the query (details below) |
| `contexto_recall(page_id, offset?)` | A paged-out turn word for word, thinking omitted. Each call returns at most 32,000 characters and about a quarter of the window budget (12,000 characters until the proxy has recorded the window), and at most half of the turn's remaining recall budget; continue with `offset` |
| `contexto_pin(kind, text, supersedes?, epistemic?, provenance?)` | Record an event, decision, obligation, artifact, or reference in the session ledger. `epistemic` is `verified`, `reported`, `summary`, or `hypothesis`. A verified event, artifact, or reference shows as a fact in the pinned state; decisions and obligations keep their own groups |
| `contexto_close(obligation_id, note)` | Close an open obligation |
| `contexto_state()` | The full pinned state, with no pin-budget cap. It is charged against the turn's recall budget and cut at a whole line, with a trailer, when the budget runs out |

**Search.** `contexto_search` finds turns holding every word of the query, in any case and any
order. Words match as substrings, so identifiers and paths work; there is no stemming and no fuzzy
or similarity matching. A query with no words finds nothing.

- Turns holding the whole query rank first, then by relevance (BM25), with newer turns preferred
  among similar ones. A word counts once per text of a turn (a reply, a tool input, a tool output),
  at most 3 times per turn, and a turn's length does not count, so a long turn or a repeated tool
  dump weighs less. Paged context blocks (`<system-reminder>` text paged on its own) rank after
  every turn.
- The reply starts with matching lines from the conversation still in view (at most 3, newest
  first), skipping the current turn's own user message.
- Then the 3 best matching paged-out turns, newest first (the newest matching turn is always one of
  them), each with its last 3 matching lines in page order and their offsets.
- Then up to 50 more matching turns, newest first, as page id and the offsets of their first and
  last matches (`@first..last`, one offset when they are the same).
- In both parts, paged context blocks come after the turns, under their own line, because a block's
  turn number is when it was paged, not when it was sent. Each reply ends with a note on what was
  searched.

The MCP server holds search text in memory up to `CONTEXTO_SEARCH_CACHE_MB` (64 MB by default): the
newest pages' text as it is and older pages' text compressed. Pages that do not fit are read from the
store at each search, so a larger value makes search in a large store faster and a smaller one uses
less memory. Results do not change.

**Recall budget.** Each turn has a recall budget: the bound minus the tokens the turn already uses
minus the headroom floor. At a turn start the proxy rotates when needed to leave at least 12,000
tokens of it. Search, recall and state replies are charged against it. A subagent or side request
(a request the CLI makes outside the main conversation, such as a title or summary request) in a
tool loop has its own budget, measured from its own request. Because the MCP server cannot tell
which conversation made a call, each call gets the smallest live budget and is charged against all
of them. A subagent's budget closes early when the main conversation's history shows the subagent
stopped, on tested Claude Code versions only; otherwise it expires 15 minutes after its last
response. Tool calls are logged with names, ids, sizes and character ranges, never the text.

## FAQ

**Does it work with a Claude Pro or Max login?** The supported upstreams are the vendor's API with
an API key, and CLIProxyAPI; the live acceptance runs went through CLIProxyAPI. Contexto forwards whatever credential Claude Code sends and never stores it, but running
it with a claude.ai subscription login is not a supported setup. To use a subscription, log in to it
in CLIProxyAPI and point Contexto at CLIProxyAPI ([docs/cliproxyapi.md](docs/cliproxyapi.md)), and
check your plan's terms on using it through a proxy.

**Does it work with my own API key?** Yes. With `ANTHROPIC_API_KEY` or an `apiKeyHelper` set for
Claude Code, `contexto run claude` uses them as Claude Code would. For Codex, give a key helper and,
for an OpenAI key, `--codex-upstream https://api.openai.com/v1` ([Codex](#codex-api-key-mode)).

**What does it cost?** Contexto is free (MIT). It makes no model calls of its own. Because every
request stays under the bound, a long session sends fewer input tokens than it would without
Contexto: 46.6% fewer in the measured run ([Results](#results)). Each rotation changes the start
of the prompt, so the request after it writes a new prompt cache instead of reading the old one.
Between rotations the cache is reused as usual: the acceptance runs reused it on all but one
eligible request.

**Does it send anything anywhere?** No. The proxy listens on `127.0.0.1` only and forwards your
CLI's own requests to the upstream you configured. There is no telemetry and no update check. The
MCP server opens no network socket. Besides paging the prompt, the one thing Contexto adds to
requests is an `X-Session-Affinity` header, a salted hash of the session id, and only for upstreams
that are not a vendor API ([Troubleshooting](#troubleshooting)). The version check runs `claude --version` or
`codex --version` locally.

**Can I continue a session later?** Yes: `contexto run --session <id> claude --resume` reuses the
session's store. A mistyped id starts a new, empty session with that id; check the id with
`contexto sessions list`.

## Troubleshooting

**Which upstream does `contexto run` use?** The first of: `--upstream`; `env.ANTHROPIC_BASE_URL` from
the first Claude Code settings file that sets it (in a git worktree, the main checkout's
`.claude/settings.local.json` first, then the project's `.claude/settings.local.json`, then
`.claude/settings.json`, then `~/.claude/settings.json`, or `$CLAUDE_CONFIG_DIR/settings.json` when
`CLAUDE_CONFIG_DIR` is set); `ANTHROPIC_BASE_URL` in the environment;
`https://api.anthropic.com`. The session log records which source won, never the URL.

**Requests bypass the proxy.** Organization-managed Claude Code settings outrank the launcher's
`--settings`. If they set `ANTHROPIC_BASE_URL`, Claude Code talks to that URL directly.

**`--settings` is refused (exit 2).** Claude Code accepts one `--settings`, and the launcher uses it to
route requests through the proxy. Put those settings in your Claude settings files instead. Every
argument after `claude` is checked, including those after `--`, so a prompt that is exactly `--settings`
or starts with `--settings=` is refused too. Your own
`--mcp-config` is allowed and loads alongside Contexto's. It takes several files, so Claude Code reads
a prompt right after `--mcp-config <file>` as another config file ("MCP config file not found"). Write
`--mcp-config=<file>`, or put the prompt before it or after a flag such as `-p`. After another flag
that takes a list, such as `--allowedTools`, the prompt is read as one of its values.

**"warning: ... running without Contexto's tools".** The model has no `contexto_search` or
`contexto_recall`, so it cannot get paged-out turns back. Neither Claude Code nor Codex reports an MCP
server that fails to start, so the proxy checks the main conversation (subagents and side requests do
not count). The launcher prints the warning when it first finds the tools missing, during the run or
when the CLI exits. If the session ends still without them, it prints the warning again after the CLI
exits ("warning (repeated from the session)"), because an interactive session can draw over a line
printed mid-run. The session log records it as `mcp_tools_missing`. Likely causes:

- *The MCP server did not start.* Run the command the warning names (`<python> -P -m contexto mcp
  --help`); it should print the `contexto mcp` usage. In Claude Code, `/mcp` shows the server's
  status.
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

**"warning: Claude Code refused Contexto's contexto_search ..." ("you haven't granted it yet").**
The tools are there, but Claude Code would not run them. Claude Code 2.1.302 and 2.1.310 in `-p`
mode refuse any MCP tool that no permission rule allows, so without a rule `contexto run claude -p`
loses recall. The launcher's `--settings` allows exactly `mcp__contexto__contexto_recall`, `_pin`,
`_close`, `_state`, and `_search`, and nothing else. These tools only touch your own local data:
they read the session's store under `$CONTEXTO_HOME` and log their calls there, pin and close add
ledger records, and `contexto_state` reads and hashes the project files that ledger records cite, to
mark stale records. They run no commands, write no project files, and use no network. Your own
permission rules still apply on top. A `deny` rule or `--disallowedTools` for one of them takes it
away. An `ask` rule for one of them outranks the allow, so in `-p` mode Claude Code refuses it and
this warning appears; remove that rule to let it run. Organization-managed settings may also
override the allow (not tested). The session log records the refusal as `mcp_tools_denied`, with the
tool names only.

**"warning: Claude Code X is not a version Contexto was tested with".** Contexto recognises parts of
Claude Code's and Codex's requests by text the CLI writes, and was tested with the versions the line
names. A newer CLI usually works, but if it changed that text, Contexto can page context it should
keep, and nothing else says so. The launch goes on. Look for a newer Contexto, or report the version
at https://github.com/VentureForgre/contexto/issues. `contexto run --no-version-check claude` (the
flag goes before `claude` or `codex`) or `CONTEXTO_NO_VERSION_CHECK=1` silences it. During the run
the session log records each distinct version the requests name, once (`cli_version`), and
`session_start_unrecognised` if the session start was not recognised. [docs/cli-updates.md](docs/cli-updates.md) is how a new version gets tested.

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

**Cache misses or dropped thinking through a proxy with several accounts.** A proxy that spreads one
session's requests over several upstream accounts loses the prompt cache and the thinking blocks on
each switch. Contexto forwards the CLI's own session signals unchanged (Claude Code's
`X-Claude-Code-Session-Id` and `metadata.user_id`, Codex's `session-id` and `prompt_cache_key`) and
adds `X-Session-Affinity: contexto-<salted hash of the session id>` to requests for any upstream
other than Anthropic, OpenAI (including Azure OpenAI), Azure AI Foundry, and ChatGPT hosts. The
proxy has to route on one of them. CLIProxyAPI does with `routing.session-affinity: true`; see
[docs/cliproxyapi.md](docs/cliproxyapi.md#troubleshooting) for the settings. Claude Code keeps its
session id on `--resume`, so routing survives a resume; a fresh launch or `--fork-session` starts a
new one. Several replicas behind one load balancer also need consistent hashing,
`routing.session-affinity-consistent-hash: true`, proposed for upstream CLIProxyAPI in
router-for-me/CLIProxyAPI#6954 and not in a CLIProxyAPI release yet.

**`count_tokens` returns 403.** Errors from the upstream pass through unchanged. Some proxies refuse
`/v1/messages/count_tokens`. Claude Code calls it around compaction, and also on resumed launches
and to draw `/context` (Claude Code 2.1.314 made 29 calls for one `/context`), so with such a proxy
those fail too.

**Where are the logs?** `$CONTEXTO_HOME/sessions/<id>/log.jsonl`, one JSON object per event, with sizes,
counts, and event names but no prompt or response text. See [What is stored and where](#what-is-stored-and-where).

## Known limitations

- **Tested scope.** Claude Code acceptance ran on two models (claude-haiku-5-5 and
  claude-opus-5-5), one bound and one upstream, on Claude Code 2.1.301 and 2.1.302. On 2.1.310,
  where tool search is on by default, only a short live recall check ran. Codex passed the long-run
  and recall criteria on one model at bound 64,000 and at the default 150,000; its answer accuracy
  against a full-context baseline was not measured.
- **Standing rules** survive paging only if they open their message or the model pinned them
  ([Rules you give once](#rules-you-give-once)).
- **New CLI versions** can change the text Contexto recognises in their requests. Until a version is
  tested, the launch warns ([Troubleshooting](#troubleshooting)).
- **Cache efficiency** depends on the upstream route. A proxy that spreads one session over several
  accounts loses the cache unless it routes on the session ([Troubleshooting](#troubleshooting)).
- **Search** counts a word once per text of a turn, so a reply that discusses a key at length weighs
  as one passing mention, and an old turn that names a key in several texts can outrank a newer
  one-line reply that states its value.
- **Search cost.** The first search of each MCP process reads every page once (1.6 s at 6,000
  synthetic pages). Each query word scans the search text once, so a long query with matches costs
  more, and the up to 53 pages one search shows are read in full with no size cap.
- **Codex ChatGPT login mode** is tested only with synthetic credentials.
- **Windows** is not supported.

## Exit codes

| Command | Code | Meaning |
|---|---|---|
| `run` | the CLI's | Claude Code's or Codex's own exit code |
| `run` | 128 + N | The CLI died from signal N |
| `run` | 1 | The proxy could not start |
| `run` | 2 | Usage error, for example `--bound` above 170,000, a `--settings` after `claude`, or no Codex key helper or upstream |
| `run` | 127 | `claude` or `codex` is not on `PATH` |
| `run` | 130 | Ctrl+C during the version check, before the CLI started |
| `proxy` | 1 | The proxy could not start: an invalid session id, a port in use or not allowed, or an invalid upstream |
| `mcp` | 0 | The CLI closed the server's input (the normal end) |
| `mcp` | 1 | Invalid session id |
| `mcp` | 2 | Invalid `--search-cache-mb` |
| `sessions`, `recall`, `state` | 0 | Success (an empty session list is a success) |
| `sessions`, `recall`, `state` | 1 | Unknown or invalid session id, unknown page id, an offset past the end of the page, or an unreadable ledger |
| `sessions prune` | 1 | A session could not be deleted, or the session named by `--session` was skipped (in use, active in the last 10 minutes, or unsafe to delete) |
| any | 2 | Unknown command or option, or a missing argument (`contexto` with no command prints help and exits 2) |

## Contributing and security

Development, tests and releases: [CONTRIBUTING.md](CONTRIBUTING.md). Reporting a vulnerability:
[SECURITY.md](SECURITY.md). Bugs and new CLI versions:
[issues](https://github.com/VentureForgre/contexto/issues).

## License

MIT. See [LICENSE](LICENSE).
