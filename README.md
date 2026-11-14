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
  ([Upstreams and CLIProxyAPI](docs/run-reference.md#upstreams-and-cliproxyapi)).
- Word search over paged-out turns ([MCP tools](docs/mcp-tools.md)).
- A separate recall budget for each subagent, so subagents cannot use up the main conversation's
  room.
- `contexto sessions list`, `show` and `prune` to see sessions and delete old ones, and
  `contexto search`, `recall` and `state` to read what a session stored.
- Resuming a Claude Code conversation (`--resume`, `--continue`) through the Contexto session that
  served it, so its pages and pins carry over.
- A per-session `X-Session-Affinity` header for proxies that spread requests over several accounts,
  so a session's prompt cache stays on one ([Troubleshooting](docs/troubleshooting.md)).
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
wheel from the latest release, [v0.5.24](https://github.com/VentureForgre/contexto/releases/tag/v0.5.24).
It is not on PyPI: the PyPI package named `contexto` is an unrelated project, so
`pip install contexto` installs the wrong thing. Use the release URL or the downloaded wheel.

Straight from the release URL, with no GitHub login:

```sh
pipx install https://github.com/VentureForgre/contexto/releases/download/v0.5.24/contexto-0.5.24-py3-none-any.whl
contexto --version                                    # contexto 0.5.24
```

You also need `claude` (Claude Code) or `codex` (Codex CLI) on `PATH`.

Checking the download before you install, other ways to install, installing from a checkout,
uninstalling, and the supported platforms: [docs/install.md](docs/install.md).

## Quickstart

```sh
contexto run claude
contexto run --bound 120000 claude --model claude-haiku-5-5
```

Contexto options go before `claude`. Everything after `claude` is passed to Claude Code unchanged.
Your own Claude Code settings, hooks and MCP servers still load, and nothing in your Claude
configuration is edited. Ctrl+C reaches Claude Code as usual. What the launcher does is under
[`contexto run`](docs/run-reference.md#contexto-run).

Resuming a conversation, and `/clear`, `/resume` and `/compact`:
[docs/claude-code.md](docs/claude-code.md). Every option of `contexto run`:
[docs/run-reference.md](docs/run-reference.md).

Codex runs through `contexto run codex` in API-key mode. It needs a key helper command and, with an
OpenAI key, `--codex-upstream https://api.openai.com/v1`: [docs/codex.md](docs/codex.md).

## Is it working?

At start, Contexto prints a session line on stderr, and in Claude Code `/mcp` lists the `contexto`
server as connected. Nothing else is visible until the prompt nears the bound. What to check after
that: [docs/sessions.md](docs/sessions.md#is-it-working).

## Documentation

The index of everything under `docs/` is [docs/README.md](docs/README.md). The guides:

- [Install](docs/install.md): checking the download, other ways to install, uninstalling, platforms.
- [Claude Code](docs/claude-code.md): resuming a conversation, and `/clear`, `/resume` and `/compact`.
- [Codex](docs/codex.md): API-key mode, the key helper, and the Codex upstream.
- [`contexto run` reference](docs/run-reference.md): the options, what the launcher does,
  `contexto proxy`, environment variables, and upstreams.
- [CLIProxyAPI](docs/cliproxyapi.md): running Contexto with a CLIProxyAPI instance as the upstream.
- [`contexto serve`](docs/serve.md): the engine as a local service for Claude Code clients that
  Contexto does not launch (the IDE extensions, the Agent SDK, `claude -p` in CI).
- [Sessions](docs/sessions.md): how to tell it is working, what is stored and where, and the
  commands that inspect and remove sessions.
- [Rules you give once](docs/standing-rules.md): how a standing rule stays in view after paging.
- [MCP tools](docs/mcp-tools.md): the tools the model gets, how search ranks, and the recall budget.
- [FAQ](docs/faq.md): subscriptions, API keys, cost, what is sent where, and continuing a session.
- [Troubleshooting](docs/troubleshooting.md): the warnings and errors you are most likely to see,
  each with its cause and what to do.
- [Exit codes](docs/exit-codes.md): the exit code of every command.
- [CLI updates](docs/cli-updates.md): how a new Claude Code or Codex version is tested.

## Troubleshooting

The warnings and errors you are most likely to see, each with its cause and what to do, are in
[docs/troubleshooting.md](docs/troubleshooting.md). A message that points here for `contexto run`
means [docs/run-reference.md](docs/run-reference.md#contexto-run).

## Known limitations

- **Tested scope.** Claude Code acceptance ran on two models (claude-haiku-5-5 and
  claude-opus-5-5), one bound and one upstream, on Claude Code 2.1.301, 2.1.302 and 2.1.318 (tool
  search on by default). On 2.1.310 only a short live recall check ran; 2.1.314 passed the zero-spend
  checks only. Codex passed the long-run and recall criteria on one model at bound 64,000 and at the
  default 150,000, on codex-cli 0.163.0; 0.166.0 passed the zero-spend checks only. Its answer
  accuracy against a full-context baseline was not measured.
- **Standing rules** survive paging only if they open their message or the model pinned them. The
  model is told to pin them when stated, but that was measured on one model and 8 rules
  ([Rules you give once](docs/standing-rules.md)).
- **Tool search** (Claude Code's default) defers Contexto's tools. When the model first loads one,
  the fixed part of the prompt changes and Contexto rotates once, so that request writes a new prompt
  cache (2 of the 6 rotations in acceptance run #4).
- **Images** are estimated from their pixel size. A screenshot in a turn that has been paged out is
  not recallable as an image: `contexto_recall` shows it as `[image omitted]`, and the model sees only
  the turn's text.
- **PDFs** are never estimated low, at the cost of running high: 4,568 tokens a page, about 2.9 times
  a typical page, and 600 pages for a PDF whose pages cannot be counted for certain. A large PDF can
  page the earlier conversation on the turn it is pasted on; a dense page (small-font code, tables)
  can still cost more than 4,568.
- **Damaged lock files.** A symlink, FIFO or directory at a session's `meta.lock` or `live.lock`
  stops `contexto run` from opening that session until you delete the file
  ([Troubleshooting](docs/troubleshooting.md)).
- **New CLI versions** can change the text Contexto recognises in their requests. Until a version is
  tested, the launch warns ([Troubleshooting](docs/troubleshooting.md)).
- **Cache efficiency** depends on the upstream route. A proxy that spreads one session over several
  accounts loses the cache unless it routes on the session ([Troubleshooting](docs/troubleshooting.md)).
- **Search** counts a word once per text of a turn, so a reply that discusses a key at length weighs
  as one passing mention, and an old turn that names a key in several texts can outrank a newer
  one-line reply that states its value.
- **Search cost.** The first search of each MCP process reads every page once (1.6 s at 6,000
  synthetic pages). Each query word scans the search text once, so a long query with matches costs
  more, and the up to 53 pages one search shows are read in full with no size cap.
- **Before the first token count.** Until the API reports a request's real input count, Contexto
  estimates it, and a very dense start of the conversation (JSON, code, non-English text) is
  estimated low. If the upstream never reports counts, tool-loop requests can go over the bound
  without a log entry.
- **Codex ChatGPT login mode** is tested only with synthetic credentials.
- **Windows** is not supported.

## Contributing and security

Development, tests and releases: [CONTRIBUTING.md](CONTRIBUTING.md). Reporting a vulnerability:
[SECURITY.md](SECURITY.md). Bugs and new CLI versions:
[issues](https://github.com/VentureForgre/contexto/issues).

## License

MIT. See [LICENSE](LICENSE).
