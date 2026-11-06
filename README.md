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
- `contexto sessions list`, `show` and `prune` to see sessions and delete old ones, and
  `contexto search`, `recall` and `state` to read what a session stored.
- Resuming a Claude Code conversation (`--resume`, `--continue`) through the Contexto session that
  served it, so its pages and pins carry over.
- A per-session `X-Session-Affinity` header for proxies that spread requests over several accounts,
  so a session's prompt cache stays on one ([Troubleshooting](#troubleshooting)).
- A warning when Claude Code or Codex is a version Contexto was not tested with. New CLI versions
  come out every week, so expect to see it; it does not stop the launch.

## Results

Each acceptance run is a scripted session whose history grows well past the bound. Its
probe turns ask 54 questions about details spread through that history. The same questions then go
to the same model with the whole history in its context (the baseline).

| Run | Result | Record |
|---|---|---|
| Claude Code 2.1.318, claude-haiku-5-5, bound 150,000 | 52 of 54 answered correctly, against 47 for the baseline. No compaction. Largest request 110,465 tokens. Cache reused on 38 of 38 eligible requests. Claude Code's defaults, tool search on, so Contexto's tools load through it. | [M2-acceptance-run4.md](docs/research/M2-acceptance-run4.md) |
| Claude Code 2.1.301, claude-haiku-5-5, bound 150,000 | 51 of 54 answered correctly, against 48 for the baseline. No compaction. Largest request 109,868 tokens. Prompt cache reused on 36 of 37 eligible requests. | [M2-acceptance-run3.md](docs/research/M2-acceptance-run3.md) |
| Claude Code 2.1.302, claude-opus-5-5, bound 150,000 | 53 of 54, against 54 for the baseline. No compaction. Cache reused on 54 of 55 eligible requests. The run added the permission rule that `contexto run` now sets itself. | [M2-acceptance-opus.md](docs/research/M2-acceptance-opus.md) |
| Codex 0.163.0, gpt-6-astra, bound 150,000 | History of about 233,000 tokens (about 1.56 times the bound; 375,922 as the test harness prices them) with no compaction. Largest request 108,565 tokens. All 61 requests succeeded. No baseline was run. | [M2-codex-acceptance-150k.md](docs/research/M2-codex-acceptance-150k.md) |

In the claude-haiku-5-5 run, Contexto used 46.6% fewer input tokens than plain Claude Code would have
for the same session, and cost 49.7% less. The plain Claude Code figures are modeled from measured
requests, not run end to end ([M2-usage-savings-run3.md](docs/research/M2-usage-savings-run3.md)).

## Install

Contexto needs Python 3.12 or later, macOS or Linux, and has no runtime dependencies. Install the
wheel from the latest release, [v0.5.23](https://github.com/VentureForgre/contexto/releases/tag/v0.5.23).
It is not on PyPI: the PyPI package named `contexto` is an unrelated project, so
`pip install contexto` installs the wrong thing. Use the release URL or the downloaded wheel.

Straight from the release URL, with no GitHub login:

```sh
pipx install https://github.com/VentureForgre/contexto/releases/download/v0.5.23/contexto-0.5.23-py3-none-any.whl
contexto --version                                    # contexto 0.5.23
```

`pip install <the same URL>` works too, inside an active Python 3.12+ virtual environment.

Or download it first with the GitHub CLI:

```sh
gh release download v0.5.23 --repo VentureForgre/contexto --pattern 'contexto-0.5.23-py3-none-any.whl'
uv tool install ./contexto-0.5.23-py3-none-any.whl   # or: pipx install ./contexto-0.5.23-py3-none-any.whl
```

`pipx` installs with its default Python. If that is older than 3.12, add `--python python3.12`.

You also need `claude` (Claude Code) or `codex` (Codex CLI) on `PATH`.

### Checking the download

Contexto sees every prompt and every auth header your CLI sends, so check that the wheel came from
this repository's release workflow. Releases from v0.5.13 on carry a `SHA256SUMS` asset and a GitHub
build provenance attestation:

```sh
gh release download <tag> --repo VentureForgre/contexto --pattern 'SHA256SUMS'
shasum -a 256 -c SHA256SUMS --ignore-missing          # Linux: sha256sum -c SHA256SUMS --ignore-missing
gh attestation verify contexto-<version>-py3-none-any.whl --repo VentureForgre/contexto
```

For v0.5.12 and earlier, compare `shasum -a 256 contexto-<version>-py3-none-any.whl` with the digest
GitHub records for the asset:
`gh release view v<version> --repo VentureForgre/contexto --json assets --jq '.assets[] | .name + " " + .digest'`.

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

**Resuming.** `contexto run claude --resume <id>` (also `-r <id>` or `-r<id>`) and
`contexto run claude --continue` (also `-c`, `-pc`) reuse the Contexto session that served that
conversation (for `--continue`, the last one used in this directory), so pages, pins and
obligations carry over. `--session ID` always wins. When Claude Code exits, Contexto prints the
command that resumes through it; Claude Code's own hint (`claude --resume <id>`) bypasses Contexto.
`--resume` without an id opens Claude Code's picker, which Contexto cannot follow, so that launch
gets a new session. A session another launch has open is never picked automatically. A
`--fork-session` fork keeps the original's session: both branches share its pages, pins and recall
room, and switching between them re-rotates. Pass `--session NEW` to give the fork its own store.

`/clear`, `/resume` and `/compact` (and auto-compaction) work inside `contexto run`, interactive
and headless. After `/clear` the new conversation is the main one from its first prompt, so pins,
search and recall work there, and `/resume` of the earlier conversation makes that one main again.
This relies on Claude Code's session id, measured on 2.1.302 to 2.1.318.

### Codex (API-key mode)

```sh
contexto run codex
contexto run --codex-key-helper 'my-key-command' codex
```

Codex gets its key from a helper command that prints it: `--codex-key-helper`, else
`$CONTEXTO_CODEX_KEY_HELPER`, else the `apiKeyHelper` from your Claude Code settings. Codex runs the
helper itself; Contexto never reads or stores the key. Codex runs it with `/bin/sh -c`, as Claude
Code runs `apiKeyHelper`, so `~`, `$VAR` and pipes work.

A helper or base URL from a project's `.claude/` files counts only when Claude Code has recorded the
folder as trusted (run `claude` there once and accept the trust dialog). Otherwise Contexto ignores
it and says which file; pass `--codex-key-helper` (or set `CONTEXTO_CODEX_KEY_HELPER`) and
`--codex-upstream` instead. With no helper left the launch stops (exit 2) and leaves no session
behind. While `CLAUDE_CODE_IGNORE_PERSISTED_TRUST` is set (for example on a runner that checks out
content it does not trust), no folder counts as trusted, and no project `.claude/` settings file is
used for any CLI ([Troubleshooting](#troubleshooting)).

Codex requests go to `--codex-upstream`, else `$CONTEXTO_CODEX_UPSTREAM`, else (API-key mode) the
upstream Claude Code would use plus `/v1`, which suits a CLIProxyAPI root, or (ChatGPT mode) the
ChatGPT Codex backend. **With an OpenAI API key, pass `--codex-upstream https://api.openai.com/v1`**
(or set `CONTEXTO_CODEX_UPSTREAM`). If Claude Code's upstream is Anthropic's API, Contexto refuses
(exit 2) rather than send the key there. The session line names where the upstream came from, for
example `contexto: session <id> (bound 150,000, upstream env, codex auth apikey, engine rewrite)`.

`contexto run codex` sets `CODEX_HOME` to `$CONTEXTO_HOME/sessions/<id>/codex-home` (mode 0700) and
writes a `config.toml` there (mode 0600) that:

- adds a model provider `contexto` whose `base_url` is the proxy, with `wire_api = "responses"`,
  `supports_websockets = false`, and the key helper as its auth command;
- turns request compression off (`features.enable_request_compression = false`);
- adds the MCP server `contexto` (`python -P -m contexto mcp --session <id>` with the same Python)
  with `default_tools_approval_mode = "approve"`, so recall works in `codex exec` too, and
  `CONTEXTO_HOME` (plus `CONTEXTO_SEARCH_CACHE_MB` when set) in its `env` table, because Codex starts
  MCP servers with a restricted environment.

Interactive Codex launches pass `--no-daemon` (Codex 0.156.0 and later), so Codex's background
app-server is not left running per session; `codex agents`, which needs that server, is refused
unless `--remote` sends it elsewhere. On exit Contexto deletes `config.toml` and `packages/` and keeps
the rest of the directory, so Codex can resume its rollouts: `contexto run --session <id> codex
resume --last`.
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
  `contexto: session 7f3a... (bound 150,000, upstream flag, codex auth apikey, engine rewrite)` for
  Codex. The
  upstream is named by where it came from (`flag`, `env`, `default`, or a settings file), never by
  its URL.
- **In Claude Code**, `/mcp` lists the `contexto` server as connected.
- **Nothing else is visible until the prompt nears the bound**, about 125,000 tokens with the
  defaults. Then the proxy rotates for the first time.
- **After that**, `contexto sessions list` shows a PAGES count above 0 for the session, and
  `contexto sessions show <id>` gives the counts (pages, pinned records, over-bound turns, upstream
  4xx errors) and lists the paged-out turns. Over-bound turns should stay at 0. The upstream 4xx
  count says how many were on model requests: Claude Code's keyless `HEAD /api/hello` probe gets a
  401 at every launch, and that one is expected.
- **From inside the session**, `contexto state` and `contexto search WORDS` read the running
  session: `contexto run` sets `CONTEXTO_SESSION` for the CLI and its shell commands.
- **If something is wrong**, Contexto prints a `warning:` line, for example when the model has no
  Contexto tools or Claude Code refused them. See [Troubleshooting](#troubleshooting).

## Rules you give once

When turns are paged out, the model keeps seeing the pinned state and the page index, which holds
the opening of each paged-out turn (about its first 200 characters). A standing rule ("from now on,
write dates in ISO format") stated at the start of a message stays in view through that index. A
rule given in the middle or at the end of a longer message is gone from view once its turn is paged
out, unless the model pinned it with `contexto_pin`. The model is told to pin a standing rule or
preference as an obligation as soon as you state it, and to keep it open until you withdraw it. In
a live check with that wording the model pinned and applied 8 of 8 rules (it pinned 0 of 4 before
it), but that was one model and 8 rules. A rule the model did not pin leaves view once its turn is
paged out; its words stay searchable, but the model rarely searches for a rule it does not know
about.

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
started in and the Claude Code session ids it served. The session log, `log.jsonl`, holds event
names, sizes and counts, never prompt or response text. Auth headers and keys are never stored.

`$CONTEXTO_HOME` must be a directory and must not contain `${`. An existing home keeps its
permissions; Contexto creates `sessions/` as 0700. A session directory must be a real directory:
Contexto refuses a symlinked `sessions/<id>`. A FIFO or directory in place of a session file is
treated as damage, never waited on. Temporary files left by a crashed process are removed the next
time the session opens.

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
| `--session ID` | see [Resuming](#claude-code) | Full session id to resume or create. Without it, `--resume <id>` and `--continue` reuse the session that served that conversation; anything else gets a new id. A prefix of existing ids is refused (exit 2). |
| `--upstream URL` | resolved | Upstream base URL for Claude Code requests. |
| `--observe-thinking` | off | Adds the `thinking-binding-controls-2026-08-01` beta so dropped thinking is logged. This changes the request by one beta flag. |
| `--codex-auth MODE` | `apikey` | `apikey` or `chatgpt` (see [Codex](#codex-api-key-mode)). |
| `--codex-key-helper CMD` | see [Codex](#codex-api-key-mode) | Command that prints the API key (Codex, API-key mode). |
| `--codex-upstream URL` | see [Codex](#codex-api-key-mode) | Upstream for Codex requests, including its version path. |
| `--no-version-check` | off | Skip the check of `claude --version` or `codex --version` against the versions Contexto was tested with. |

For Claude Code, the launcher:

1. resolves the upstream Claude Code would otherwise use (see [Troubleshooting](#troubleshooting));
2. picks the session (`--session`, else the one a `--resume <id>` or `--continue` names, else a new
   one), opens it under `$CONTEXTO_HOME/sessions/<id>/` and starts the proxy on
   `127.0.0.1:<free port>`;
3. writes two temporary files (mode 0600, deleted on exit): a `--settings` file that sets only
   `env.ANTHROPIC_BASE_URL` to the proxy and allows Contexto's five MCP tools by name
   (`permissions.allow`; see [Troubleshooting](#troubleshooting)), and an `--mcp-config` file that
   starts Contexto's MCP server (`python -P -m contexto mcp --session <id>` with the same Python, and
   `CONTEXTO_HOME`, plus `CONTEXTO_SEARCH_CACHE_MB` when set, in its environment; `-P` keeps your
   project directory off the import path);
4. runs `claude --settings=<file> --mcp-config=<file> [your args]` in your terminal, with
   `CONTEXTO_SESSION=<id>` in its environment, and returns its exit code. Each flag and its file are
   one argument, so a prompt right after `claude` (`contexto run claude "fix the tests"`) stays a
   prompt.
5. after Claude Code exits, prints any warnings raised while it ran (printing them mid-run would land
   in Claude Code's input box), then the command that resumes the conversation through Contexto.

`contexto run claude --version` (or `-v`, `--help`, `-h` alone) runs Claude Code without a session or
proxy. SIGTERM, SIGHUP and SIGQUIT are forwarded to Claude Code; SIGUSR1, SIGUSR2 and SIGALRM end it
with SIGTERM. If Contexto itself is killed with SIGKILL, a watchdog process ends Claude Code and
removes the temporary files; the session log then has no `exit` event. A launch that cannot start
(for example the proxy fails) removes the session it created; a resumed `--session` is kept.

**Your own `--settings` is refused** (exit 2), because Claude Code accepts only one and the launcher
needs it to route requests through the proxy. Put those settings in your Claude settings files
instead. Details are under [Troubleshooting](#troubleshooting).

At start, `contexto run` warns on stderr when the bound leaves a window budget under 25,000 tokens
and suggests a larger `--bound`. It assumes a fixed overhead of 28,000 tokens for Claude Code
(measured 18,641 to 27,925 on 2.1.314 with tool search on) and 19,000 for Codex. It warns again,
once, if the measured overhead does the same.

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
no paging. `--session` takes a full id; a prefix of existing ids exits 2. A proxy that cannot start
removes the session it created.

The proxy, here and under `contexto run`, answers only the CLI on this machine: a request whose
`Host` does not name `127.0.0.1` or `localhost` (any port, or none), or that carries an `Origin`
header (any web page), gets 403 `permission_error` and is logged as `request_refused`. If you point
another local client at the proxy, use one of those two names. When the CLI goes away while a
request is in flight (Esc), the proxy stops the upstream response at once and logs the request with
`client_aborted: true`.

### Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `CONTEXTO_HOME` | all commands | Data directory (default `~/.contexto`), resolved to an absolute path. `contexto run` passes it to the MCP server for both Claude Code and Codex. `contexto mcp --home` overrides it. |
| `CONTEXTO_SESSION` | `recall`, `search`, `state` | Session to read when `--session` is not given. `contexto run` sets it for the CLI it runs. |
| `CONTEXTO_CODEX_KEY_HELPER` | `run codex` | Key helper command when `--codex-key-helper` is not given. |
| `CONTEXTO_CODEX_UPSTREAM` | `run codex` | Codex upstream when `--codex-upstream` is not given. |
| `CONTEXTO_SEARCH_CACHE_MB` | `mcp` | Page text the MCP server holds in memory for `contexto_search`, in MB (default 64; `0` holds none). See [MCP tools](#mcp-tools-the-model-gets). |
| `CONTEXTO_RECALL_VARIANT` | new sessions | `default` or `legacy` recall guidance text. `legacy` is kept for diagnosis. A session keeps the variant it started with. |
| `ANTHROPIC_BASE_URL` | `run`, `proxy` | Upstream, when no settings file sets one (see Troubleshooting). |
| `CLAUDE_CODE_IGNORE_PERSISTED_TRUST` | `run`, `proxy` | Read, never set, by Contexto: when truthy, no project `.claude/` settings file is used, as Claude Code does in `-p` mode (see Troubleshooting). |
| `CONTEXTO_NO_VERSION_CHECK` | `run` | `1` skips the CLI version check, like `--no-version-check`. |

## Inspecting sessions

These commands read a session's store under `$CONTEXTO_HOME/sessions/<id>/` and never change it
(except `sessions prune`, below).
`recall`, `search`, and `state` pick the session from `--session ID`, else `CONTEXTO_SESSION`, else
the most recently active session that has pages or pinned records or that another process holds
open (else the most recent session). A session can be named by its full id (32 hex characters) or
any unique prefix, such as `7f3a`; an ambiguous prefix is an error that lists the matches. They print
the choice to stderr (for example `session 7f3a91c2... (most recent)`), so stdout carries only the
text. A mistyped `--session` is an error, never a new session.

- `contexto sessions list [--json]`: one row per session, most recent activity first: id, created
  time, page count, pinned record count (`?` if the ledger cannot be read), last activity in UTC,
  and size on disk, then the total size. `--json` gives the size as `size_bytes`.
- `contexto sessions show ID [--all] [--json]`: created and last activity times; page, record,
  active record and open obligation counts; session log event, over-bound turn and upstream 4xx
  counts; a counter that goes up at each rotation (`Last epoch`); whether the ledger verifies; then
  the paged-out turns, newest first: page id, turn number, and the opening user text (the 20 newest;
  `--all` for every page). The upstream 4xx count shows how many were on model requests: Claude
  Code's keyless `HEAD /api/hello` probe gets a 401 at every launch. `--json` adds event counts by
  type and `page_list`.
- `contexto search WORDS [--session ID] [--limit N]`: the paged-out turns holding every word, best
  match first (at most 10 unless `--limit`), with the same search the MCP `contexto_search` tool
  uses. Each hit shows its page id, turn and opening text, then its matching lines, each with the
  offset to pass to `contexto recall PAGE_ID --offset N`. It searches paged-out turns only. No match
  is a success (exit 0).
- `contexto recall PAGE_ID [--session ID] [--offset N]`: a paged-out turn as plain text, thinking
  omitted, exactly as the MCP `contexto_recall` tool renders it. Page ids (`pg_` and 16 hex digits,
  for example `pg_1cb04faa431985c5`) come from `contexto sessions show` or `contexto search`. Each chunk is at most
  32,000 characters and about a quarter of the session's window budget, or 12,000 characters until
  the proxy has recorded the window. When more remains, stderr says
  `[N more characters: rerun with --offset M; <how the chunk size was chosen>]`.
- `contexto state [--session ID] [--raw]`: the decisions, open obligations, and verified facts the
  model pinned, uncapped (or "No pinned records."), then how many turns are paged out. `--raw`
  prints the exact text the MCP `contexto_state` tool gives the model, including its instructions to
  the model. It hashes each file a record cites as provenance, inside the session's recorded working
  directory, to mark changed files `STALE`. It never writes there.

### Removing old sessions

Session directories hold the raw conversation and are never removed on their own.
`contexto sessions prune` deletes whole sessions. It is the one `sessions` command that changes the
store.

```
contexto sessions prune --older-than 30d          # list sessions idle for 30 days, with sizes
contexto sessions prune --older-than 30d --yes    # delete them
contexto sessions prune --keep 10 --yes           # keep the 10 most recently active, delete the rest
contexto sessions prune --session <full id> --yes # delete one session
```

- Without `--yes` nothing is deleted: it prints each selected session, its size, and the total.
- `--session` needs the full id. A prefix of existing ids exits 2 and names them, so a mistyped id
  never deletes another session.
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
- A launch that fails before the CLI starts removes the session it created, and `contexto mcp`
  serves existing sessions only, so neither leaves an empty session behind. A session whose CLI
  started but never sent a request is kept, since its log records the launch.

## MCP tools the model gets

`contexto run` registers `contexto mcp --session <id>` with the CLI, which starts it over stdio. You do
not start it by hand.

| Tool | What it does |
|---|---|
| `contexto_search(query)` | Finds turns that contain every word of the query (details below) |
| `contexto_recall(page_id, offset?)` | A paged-out turn word for word, thinking omitted. Each call returns at most 32,000 characters and about a quarter of the window budget (12,000 characters until the proxy has recorded the window), and at most half of the turn's remaining recall budget; continue with `offset` |
| `contexto_pin(kind, text, supersedes?, epistemic?, provenance?)` | Record an event, decision, obligation, artifact, or reference in the session ledger. `epistemic` is `verified`, `reported`, `summary`, or `hypothesis`. Decisions and obligations always show in the pinned state; an event, artifact, or reference shows there only when verified with a provenance source, and the reply says so when it will not |
| `contexto_close(obligation_id, note)` | Close an open obligation |
| `contexto_state()` | The full pinned state, with no pin-budget cap. It is charged against the turn's recall budget and cut at a whole line, with a trailer, when the budget runs out |

**Search.** `contexto_search` finds turns holding every word of the query, in any case and any
order. Words match as substrings, so identifiers and paths work; there is no stemming and no fuzzy
or similarity matching. A query with no words finds nothing. Contexto's own search, recall and
state calls (a turn's earlier search replies, for example) are kept for recall but not searched,
since they copy older pages and would outrank the turns they quote.

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

**Can I continue a session later?** Yes. `contexto run claude --resume <id>` and
`contexto run claude --continue` reuse the Contexto session that served the conversation
([Resuming](#claude-code)), and Contexto prints the exact resume command when Claude Code exits.
`contexto run --session <full id> claude --resume <id>` names the session yourself. For Codex:
`contexto run --session <id> codex resume --last`. `--session` takes a full id: a prefix of existing
ids exits 2, and an unknown full id starts a new, empty session with that id, so check it with
`contexto sessions list`.

## Troubleshooting

**Which upstream does `contexto run` use?** The first of: `--upstream`; `env.ANTHROPIC_BASE_URL` from
the first Claude Code settings file that sets it: the main checkout's `.claude/settings.local.json`
(the repository root, also from a subdirectory; the main checkout from a linked worktree), then the
current directory's own `.claude/settings.local.json`, then `.claude/settings.json`, then
`~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`); `ANTHROPIC_BASE_URL` in the
environment; `https://api.anthropic.com`. From a subdirectory the repository root's `settings.json`
is not read, as in Claude Code. The session log records which source won, never the URL.

`contexto run claude` uses project settings as Claude Code does. In `-p` mode Claude Code has no
trust check, so pass `--upstream` to pin the upstream in a repository you do not trust. While
`CLAUDE_CODE_IGNORE_PERSISTED_TRUST` is set, Contexto uses no project `.claude/` settings file for any
CLI, as `claude -p` does then: the base URL comes from your user settings, the environment or
`--upstream`, and Codex's key from `--codex-key-helper`, `CONTEXTO_CODEX_KEY_HELPER` or your user
settings. Contexto prints which file it ignored.

**Opening an untrusted repository with `contexto run claude`.** While Claude Code shows its "do you
trust this folder" dialog it sends `HEAD /api/hello` (no key, no content). Through Contexto that ping
goes to the repository's own `ANTHROPIC_BASE_URL` even if you decline, so that host learns your
address, Claude Code version and the time; plain `claude` sends it to your own upstream. In a
repository you do not trust, run `contexto run --upstream <your upstream> claude` to pin the
upstream, or set `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`.

**"ignored ... from .claude/settings.json" (Codex).** A project's helper or base URL counts for
Codex only in a folder Claude Code has recorded as trusted. Pass `--codex-key-helper` (or set
`CONTEXTO_CODEX_KEY_HELPER`) and `--codex-upstream`, or run `claude` there once and accept the trust
dialog.

**"`--session` after 'claude' is a Contexto option".** Contexto's options go before `claude`; the
message shows the command rewritten.

**403 from the proxy, or a request you sent by hand is refused.** The proxy accepts `Host`
`127.0.0.1` or `localhost` with any port, so an SSH port forward or port 80 works. A request naming
another host (for example `host.docker.internal` from a container) or carrying an `Origin` header
gets 403 `permission_error`. Point the CLI at `localhost` or `127.0.0.1` through a forward instead.

**"`<session>/meta.lock` is damaged" or "`live.lock` is damaged".** A symlink, FIFO or directory
stands where the lock file should be. Delete it, and Contexto makes a new one at the next start.
Until then `contexto run` does not open the session; with a damaged `meta.lock` recall, search and
state are refused (pins and closes still work), and with a damaged `live.lock` every tool is.

**`over_bound` in the session log.** `over_bound` with `in_tool_loop: false` at a turn start is
logged next to `bound_too_small` (the bound is too small for the head and the new prompt) or when the
rotation's sizing did not hold; only `in_tool_loop: true` is the expected tool-loop overrun. A head
change (tool search loading a tool, an MCP server connecting, a model switch) is sized like any
other rotation.

**"a large PDF is estimated generously ..." or "a PDF could not be read fully ...".** A PDF is
estimated at 4,568 tokens a page, more than most pages cost, so a large one pasted at a turn start
can page the earlier conversation (it stays recallable); the first notice says that only the PDF's
allowance fell short, not the bound. When Contexto cannot count a PDF's pages for certain (encrypted
object streams, pages that show PDF source as text, a PDF by URL or file id) it counts 600 pages,
the API's maximum, and logs `pdf_pages_uncertain`. Such a PDF is over any bound, so the turn keeps
only its own messages.

**Warnings while Claude Code runs** (window budget, missing or refused tools) are printed after it
exits, so they do not land in its input box.

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
The tools are there, but Claude Code would not run them. Claude Code 2.1.302 to 2.1.318 in `-p`
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
the fixed overhead, so turns page out almost at once and recalled pages barely fit. Start again with
the suggested `--bound`. At a budget of 0 or less the warning says the bound "may leave no window
budget": the start-up figure assumes the top of the measured overheads.

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
  claude-opus-5-5), one bound and one upstream, on Claude Code 2.1.301, 2.1.302 and 2.1.318 (tool
  search on by default). On 2.1.310 only a short live recall check ran; 2.1.314 passed the zero-spend
  checks only. Codex passed the long-run and recall criteria on one model at bound 64,000 and at the
  default 150,000, on codex-cli 0.163.0; 0.166.0 passed the zero-spend checks only. Its answer
  accuracy against a full-context baseline was not measured.
- **Standing rules** survive paging only if they open their message or the model pinned them. The
  model is told to pin them when stated, but that was measured on one model and 8 rules
  ([Rules you give once](#rules-you-give-once)).
- **Images** are estimated from their pixel size. A screenshot in a turn that has been paged out is
  not recallable as an image: `contexto_recall` shows it as `[image omitted]`, and the model sees only
  the turn's text.
- **PDFs** are never estimated low, at the cost of running high: 4,568 tokens a page, about 2.9 times
  a typical page, and 600 pages for a PDF whose pages cannot be counted for certain. A large PDF can
  page the earlier conversation on the turn it is pasted on; a dense page (small-font code, tables)
  can still cost more than 4,568.
- **Damaged lock files.** A symlink, FIFO or directory at a session's `meta.lock` or `live.lock`
  stops `contexto run` from opening that session until you delete the file
  ([Troubleshooting](#troubleshooting)).
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
| `run` | 1 | Contexto could not start: the proxy, or the session store (for example a `CONTEXTO_HOME` that exists but cannot be written) |
| `run` | 2 | Usage error, for example `--bound` above 170,000 or not above the headroom floor (25,000 by default), an invalid `--session` id or one that only starts existing ids, a `CONTEXTO_HOME` that is a file, a `--settings` after `claude`, no Codex key helper, or a Codex upstream that would be Anthropic's API |
| `run` | 127 | `claude` or `codex` is not on `PATH` or cannot be run, checked before anything starts |
| `run` | 130 | Ctrl+C during the version check or while waiting for another launch to choose its session, before the CLI started |
| `proxy` | 1 | Could not start: a port in use, or an invalid upstream |
| `proxy` | 2 | `--port` outside 0 to 65535, an invalid `--session` id (`''` included), an id that only starts existing session ids, or a `CONTEXTO_HOME` that is a file |
| `mcp` | 0 | The CLI closed the server's input (the normal end) |
| `mcp` | 1 | An invalid or unknown session id (it serves existing sessions only) |
| `mcp` | 2 | Invalid `--search-cache-mb` |
| `sessions`, `recall`, `search`, `state` | 0 | Success (an empty session list, or a search with no match, is a success) |
| `sessions`, `recall`, `search`, `state` | 1 | Unknown, ambiguous or invalid session id, pages that cannot be read, an unknown or malformed page id, an offset past the end of the page, an unreadable ledger, or a `CONTEXTO_HOME` that is not a directory |
| `sessions prune` | 1 | A session could not be deleted, or the session named by `--session` was skipped (in use, active in the last 10 minutes, or unsafe to delete) |
| `sessions prune` | 2 | `--session` is not a session but the start of existing ids (prune needs the full id) |
| any | 2 | Unknown command or option, an invalid option value (for example `--bound abc`), or a missing argument (`contexto` with no command prints help and exits 2) |

## Contributing and security

Development, tests and releases: [CONTRIBUTING.md](CONTRIBUTING.md). Reporting a vulnerability:
[SECURITY.md](SECURITY.md). Bugs and new CLI versions:
[issues](https://github.com/VentureForgre/contexto/issues).

## License

MIT. See [LICENSE](LICENSE).
