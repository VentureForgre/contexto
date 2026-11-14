# Troubleshooting

[Contexto](../README.md) / [Documentation](README.md)

The warnings and errors you are most likely to see from `contexto`, each with its cause and what to
do. An entry about a printed message quotes its text, so search this page for the words you saw.

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
Until then `contexto run` does not open the session; with a damaged `meta.lock` the model's recall,
search and state tools are refused (pins and closes still work), and with a damaged `live.lock` every
tool is. The `contexto state`, `search` and `sessions show` commands still work.

**`over_bound` in the session log.** `over_bound` with `in_tool_loop: false` at a turn start is
logged next to `bound_too_small` (the bound is too small for the head and the new prompt) or when the
rotation's sizing did not hold. With `in_tool_loop: true` a tool loop grew past the bound and
in-turn paging had nothing left to page (`in_turn_exhausted` is logged with it), is turned off, or
is not available (Codex); see [In-turn paging](in-turn-paging.md). A change to
the fixed overhead (tool search loading a tool, an MCP server connecting, a model switch) is sized
like any other rotation.

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
`session_start_unrecognised` if the session start was not recognised. [docs/cli-updates.md](cli-updates.md) is how a new version gets tested.

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
[docs/cliproxyapi.md](cliproxyapi.md#troubleshooting) for the settings. Claude Code keeps its
session id on `--resume`, so routing survives a resume; a fresh launch or `--fork-session` starts a
new one. Several replicas behind one load balancer also need consistent hashing,
`routing.session-affinity-consistent-hash: true`, proposed for upstream CLIProxyAPI in
router-for-me/CLIProxyAPI#6954 and not in a CLIProxyAPI release yet.

**`count_tokens` returns 403.** Errors from the upstream pass through unchanged. Some proxies refuse
`/v1/messages/count_tokens`. Claude Code calls it around compaction, and also on resumed launches
and to draw `/context` (Claude Code 2.1.314 made 29 calls for one `/context`), so with such a proxy
those fail too.

**Where are the logs?** `$CONTEXTO_HOME/sessions/<id>/log.jsonl`, one JSON object per event, with sizes,
counts, and event names but no prompt or response text. See [What is stored and where](sessions.md#what-is-stored-and-where).
