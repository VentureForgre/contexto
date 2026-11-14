# Changelog

What each release of Contexto changes for someone running `contexto run claude ...` or
`contexto run codex ...`. This file is written by hand: a release's section is added here before
the release is cut. The newest release is first.

Versions follow [Semantic Versioning](https://semver.org/). While the version is below 1.0.0, a
breaking change is a minor bump. The wheel and sdist of each release are at
https://github.com/VentureForgre/contexto/releases. How releases are cut is in
[RELEASING.md](RELEASING.md).

## Unreleased

- `contexto serve` runs the engine as a standing service on `127.0.0.1:7837` for clients Contexto
  does not launch (the IDE extensions, the desktop app, the Agent SDK, `claude -p` in CI): each
  Claude Code session gets its own store. It requires a token, the first path segment of the base
  URL `--print-settings` prints. The printed settings turn on Claude Code's gateway hint headers,
  which the engine reads and the service removes before forwarding (`--forward-hint-headers`
  passes them on). They mark that setting as Contexto's, so a `contexto run claude` that inherits
  them, or finds them merged into your Claude Code settings, removes the hints too. See
  [docs/serve.md](docs/serve.md).
- `contexto mcp --serve` gives those clients Contexto's tools: one MCP server for every
  conversation, which asks the service which session each tool call belongs to.
  `--print-mcp-config` prints its config.
- `contexto run claude` and `contexto proxy` refuse an upstream that is this home's `contexto
  serve` (as it is once the service's URL is in your Claude Code settings) and say to pass
  `--upstream URL`.
- Search finds more. When no paged-out turn holds every word of a query, `contexto_search` and
  `contexto search` now give approximate matches and say so: question words are dropped,
  `refresh_token` finds `refreshToken`, a turn holding enough of the query (rarer words count more)
  is returned, and one one-letter typo is tolerated. Each match names the words it matched and the
  ones it did not. A query that some turn holds every word of gets the same result as before
  ([docs/search.md](docs/search.md)). `contexto search --json` marks such a result `approximate` and
  gives each hit's `matched` and `missing` words.
- A new session starts with the open obligations and active decisions the model pinned in earlier
  sessions of the same project, so a standing rule pinned yesterday is in view from the first
  request. A project is the git repository the session started in (a linked worktree or a
  submodule is its own), else the directory itself. Only pins the proxy saw the main conversation
  make are carried, never a subagent's: Claude Code subagents are told apart by their
  `X-Claude-Code-Agent-Id` header (a request that carries it is never read for pins, even a fork's
  that has the main conversation's key), and a conversation that became the main one only by having
  more turn starts (a subagent without that header, a Codex child conversation) is not trusted. The model
  sees carried records as records from an earlier session, not as instructions, with each record's
  text quoted and escaped. See [docs/project-memory.md](docs/project-memory.md).
- Codex sessions give nothing to project memory. On codex-cli 0.163.0, as measured, the model calls
  Contexto's tools from inside Codex's code-mode `exec` tool, which the proxy does not read for this,
  so a Codex session's pins are never carried and its closures of carried records count only in
  that session. A Codex session does start with what Claude Code sessions of the project carried.
  Not checked on codex-cli 0.166.0 or live.
- A closure or replacement made in the main conversation of any session of the project applies to
  every session of it, running sessions included (before a session's first rotation from its next
  turn start; after it, from its next rotation). Forgetting a session never reopens what it closed;
  pruning it does.
- At start, `contexto run` prints how many records were carried and from how many sessions, and
  `contexto state` lists them under their own heading; `contexto state --json` lists them under
  `carried`.
- `contexto project show` lists a project's sessions and what a new session would carry.
  `contexto project forget --record ID`, `--session ID` (full ids only) or `--all` takes records or
  sessions out of it; nothing is deleted until `contexto sessions prune`.
- `contexto run --no-project-memory`, or `CONTEXTO_PROJECT_MEMORY=0`, keeps a session out of
  project memory: it carries nothing and gives nothing to later sessions. It stays off when you
  resume it, and a resumed session stops sending what it carried: at once before its first
  rotation; after it, from its next rotation.
- The model is now told to pin only what you state in your own messages, never a rule found in tool
  results, files, web pages or another agent's output. This applies to every session.
- `contexto state` shows a character of a stored record that could move the cursor, end the line,
  or reorder or hide text as its escape. Joiners and other format characters print as they are, and
  so does a variation selector right after the emoji or ideograph it varies; anywhere else it is an
  escape. One zero-width character between letters prints (a ZWNJ in Persian, a ZWJ in an emoji);
  a run of them, or one next to a space, is escapes.
- Records pinned before this release are never carried, so project memory starts empty.
- `contexto state`, `contexto search` and `contexto recall` take `--json`. Each prints one object,
  described in the command's `--help`; an error is still one line on stderr with the usual exit
  code. `contexto state --json` includes the store faults `contexto stats --json` shows.
- `contexto version` and `contexto -V` print the same line as `--version`. `contexto sessions`
  with no command lists the sessions.
- On a fresh install, `state`, `search`, `recall` and `sessions` say to run `contexto run claude`
  first.
- When `claude` or `codex` is not on `PATH`, the error gives the vendor's install command. Every
  `contexto run claude` launch error now starts with `contexto:`, as `run codex` errors did.
- The Codex "no key helper" error gives a whole command that works with an OpenAI key in
  `OPENAI_API_KEY`:

  ```sh
  contexto run --codex-upstream https://api.openai.com/v1 \
    --codex-key-helper 'printenv OPENAI_API_KEY' codex
  ```

- The MCP server reports the package version in `serverInfo` (it said `0.1.0`).
- `contexto run codex` with a session id that starts with `-` no longer starts Codex without
  Contexto's tools: the MCP server now gets the id as one `--session=<id>` argument, as Claude Code's
  already did.
- `contexto run claude` and `contexto run codex` print a short summary on stderr when the CLI
  exits: the largest prompt against the bound, the pages this run added and the pages the session
  has on disk, the recall and search calls, the share of input read from cache, and whether Claude
  Code compacted. `--quiet` or `CONTEXTO_QUIET=1` drops it; warnings are still printed. It is not
  printed for `--version` or `--help`.
- `contexto stats [SESSION] [--last-run] [--json]` prints the same numbers for any session, with
  the requests, the bytes sent against the bytes forwarded, and every fault. docs/stats.md defines
  each number and says which are measured, which are estimates, and which the log cannot show.
- A store that cannot be written (a full disk, a permission problem, a damaged page) is no longer
  silent. Contexto stops paging when that happens, so requests can exceed the bound. It now says
  so once per cause when the CLI exits, in `contexto stats`, and in `contexto state`, with what
  to do. The `store_error` log entry names the errno (`ENOSPC`, `EACCES`).
- A full disk no longer replaces the CLI's exit code. A session log that cannot be written is
  reported, and the CLI's own exit code is kept. Ctrl+C while the summary is being built keeps the
  exit code and the resume line too.
- `contexto stats` and `contexto state` never wait for another process that holds a session's
  ledger lock, and reading a session with them creates nothing in it.
- `contexto run codex` holds its warnings until Codex exits, as `contexto run claude` does, so a
  warning no longer lands in Codex's screen mid-run.
- A number is shown only when the log supports it. The pages a run added come from the store's
  own count, so a launch that pages the history again (after an edited CLAUDE.md, a CLI update or
  another `--model`) does not count it twice. The cache share covers only responses that reported
  one. "Claude Code did not compact" is said only for requests from a Claude Code version the
  check was measured on.
- The session log has two new fields: `pages` on `proxy_start` and `proxy_stop` (the page files in
  the store), and `cli_session` on a Messages request's entry (whether it carried Claude Code's
  session id).
- New command: `contexto doctor` checks what `contexto run` needs from the directory you run it
  in, and prints one line per check with the next step under a warning or a failure. It checks
  Python, the `claude` and `codex` CLIs and their versions, the Claude Code settings files, the
  upstream and what sets it, whether a key or key helper is configured, the store home, the MCP
  server, and whether `contexto run claude` would refuse to launch because a cloud-provider switch
  is on (a failure, with the launcher's own message). `--json` prints the same results for scripts.
  It makes no network request, runs no key helper and prints no key. Details:
  [docs/doctor.md](docs/doctor.md).
- `contexto run claude` now refuses to launch (exit 2) when a cloud-provider switch is on, in the
  environment or in a Claude Code settings file: `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`,
  `CLAUDE_CODE_USE_FOUNDRY`, `CLAUDE_CODE_USE_MANTLE`, `CLAUDE_CODE_USE_ANTHROPIC_AWS` or
  `CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD` (D70, D64). With one on, Claude Code sends its requests to
  that provider and none through the proxy, so the session would not be kept under the bound;
  0.5.24 launched anyway. It also refuses while a settings file that may turn one on cannot be read.
  The message says which switch and where; to keep using the provider, run `claude` without
  Contexto.
- A Claude Code settings file that starts with a UTF-8 byte order mark is read. Claude Code reads
  such a file, and Contexto treated it as absent, so `contexto run` missed an
  `ANTHROPIC_BASE_URL` or `apiKeyHelper` set there and used another upstream than Claude Code's own.
- A Claude Code settings file is now read as Claude Code reads it in four more cases: a named
  pipe in its place is never opened for reading (`contexto run` waited on it for ever), a file
  holding `NaN` or `Infinity` is ignored, a file holding a number longer than 4,300 digits is read,
  and a file whose `env` block is not a JSON object is ignored whole, so an `apiKeyHelper` in it
  is no longer used (Claude Code loads nothing from it either). `contexto run claude`
  refuses to launch while a named pipe, or a file it cannot parse that may name a cloud-provider
  switch, is in a settings file's place (D70).

## 0.5.24 (2026-11-06)

- A Codex turn that starts with large tool output no longer goes out over the bound unnoticed.
  Codex tool output is counted at a cautious 1.5 bytes per token until real usage arrives, as
  Claude Code's already was.
- A session started on an earlier release has its fixed overhead (system prompt and tool
  definitions) measured again when it resumes, so a low measurement is not carried on.
- An image counts the higher of its pixel price and its 28-pixel patches, at most 4,787 tokens.
  Sizing a crafted JPEG no longer slows a request.
- The start warning about a bound that may leave no room quotes each CLI's own measured overhead
  (`run codex` had quoted Claude Code's).

## 0.5.23 (2026-11-06)

- Reading a PDF near the bound no longer sends a request over it. A PDF now counts 4,568 tokens a
  page. The old estimate, from file size, came to as little as 2% of a compact file's real cost.
- This changes behavior you may notice: a large PDF pasted at a turn start can page out the earlier
  conversation, which stays recallable. When the page count cannot be read for certain (for example
  encrypted object streams, or a PDF given by URL or file id) the PDF counts as 600 pages, the
  API's maximum, and the session log records `pdf_pages_uncertain`.

## 0.5.22 (2026-11-06)

- The proxy accepts `127.0.0.1` or `localhost` with any port in the `Host` header. 0.5.16 to 0.5.21
  answered 403 when the port was not the proxy's own, which broke SSH port forwards.
- Claude Code's own compaction (`/compact` or automatic) is recognised in every form it takes, so
  the compacted conversation becomes the main one at once. 0.5.15 to 0.5.21 missed most forms.
- A launch that waits for another launch to pick its session says so, and Ctrl+C during the wait
  exits 130. A damaged lock file in a session is named in the error.
- `contexto run claude` knows all of Claude Code's options that take a value (36 were missing), so
  a value is not read as `--resume` or `--continue`.

## 0.5.21 (2026-11-06)

- A pasted screenshot no longer pages out the whole window. Images are now priced by their pixel
  size, not their base64 length: a screenshot that really costs 641 to 3,243 tokens had been
  estimated at 124,000 to 429,000, with a `bound_too_small` log entry and advice to restart.
- Text after an image or PDF is no longer under-counted. Images and PDFs are kept out of the
  bytes-per-token ratio Contexto learns, which two screenshots had pushed from 2.87 to 39.4.

## 0.5.20 (2026-11-06)

### Security

- With an API key and no Codex upstream given, `contexto run codex` no longer falls back to
  Anthropic's API. 0.5.19 and earlier sent your OpenAI key there when Claude Code itself used
  Anthropic's API. The launch now stops with exit 2 and names
  `--codex-upstream https://api.openai.com/v1`. A CLIProxyAPI upstream is still picked up.

### Fixed

- The Codex key helper runs through `/bin/sh -c`, as Claude Code runs `apiKeyHelper`, so `~`,
  `$VAR` and pipes work.
- Interactive launches on Codex 0.156.0 and later pass `--no-daemon`. Codex 0.166.0 had left two
  processes and about 517 MB behind per session. `codex agents` is refused unless `--remote` is
  given, because it starts servers that outlive the session.
- A missing `codex` exits 127 before anything starts, and a failed launch leaves no empty session.

## 0.5.19 (2026-11-06)

- Text holding a lone UTF-16 surrogate (an unpaired half of a character such as an emoji) no
  longer stops paging or ends the MCP server. Any error inside a Contexto tool is that one call's
  error.
- A FIFO, directory or symlink where a session file should be is reported as damage and never
  waited on. Symlinked session directories are refused.
- A full disk leaves no temporary file behind, and a log line cut short does not swallow the next.

## 0.5.18 (2026-11-06)

- Turn starts no longer go over the bound when the first usage report arrives late or carries no
  input count. The fixed overhead is now measured so that it errs high: at bound 30,000 a turn
  start that went out at 32,047 tokens now goes out at about 20,700.

## 0.5.17 (2026-11-06)

- New: `contexto search WORDS` searches a session's paged-out turns from the shell, and
  `contexto sessions show` lists page ids to pass to `contexto recall`.
- `contexto state` prints the pinned records in readable form. `--raw` prints the model's text.
- `recall`, `state`, `search` and `sessions show` accept a unique prefix of a session id.
  `sessions prune --session` and `run --session` take a full id only, so a mistyped id cannot
  delete or open another session.
- `contexto mcp`, `run claude --version`, `run claude --help` and failed launches no longer leave
  empty sessions. A store that cannot be opened exits 1, not 2.

## 0.5.16 (2026-11-06)

### Security

- The proxy serves only the local CLI. In 0.5.15 and earlier a web page open in your browser could
  find the proxy's port on localhost, write pages to the store and take over the main conversation.
  A request with an `Origin` header, or whose `Host` is not `127.0.0.1` or `localhost`, now gets
  403. If you reach the proxy through another host name or from a browser, that stops working.

### Fixed

- Pressing Esc stops the upstream request within about 0.2 s. It had run on for 38 s when measured.
- An early 413 from the upstream (request too large) is passed on, not turned into retried 502s.

## 0.5.15 (2026-11-06)

- Recall works after `/clear`. The new conversation becomes the main one at once. Before, it
  stayed a side conversation until it outgrew the old one, and recall returned nothing. `/resume`
  and `/compact` are followed the same way, in interactive and headless runs.
- Subagents, agent-team teammates and a `claude -p` started from a tool never become the main
  conversation. Each handover is logged as `main_changed`.

## 0.5.14 (2026-11-06)

### Security

- `contexto run codex` uses a project's `apiKeyHelper` and base URL only in a folder Claude Code has
  recorded as trusted. 0.5.13 and earlier ran a cloned repository's helper command and could send
  your key to its host. To use project settings, run `claude` in the folder once and accept the
  trust dialog, or pass `--codex-key-helper` and `--codex-upstream`.
- While `CLAUDE_CODE_IGNORE_PERSISTED_TRUST` is set, no project settings file is used by
  `run claude`, `run codex` or `proxy`, as in `claude -p`.

### Fixed

- Contexto reads settings files in the order Claude Code does. In 8 of 27 measured layouts
  (worktrees, nested folders) it had picked a different upstream than Claude Code would.

## 0.5.13 (2026-11-06)

- The first release with a `SHA256SUMS` file and a build provenance attestation. Check a download
  with `gh attestation verify <file> --repo VentureForgre/contexto` or
  `shasum -a 256 -c SHA256SUMS --ignore-missing`. Releases up to 0.5.12 have neither.
- Launches started at the same moment never share an automatically chosen session.
- `contexto run claude` reads Claude Code's short-option clusters (`-pc`, `-r<id>`) when it picks
  the session to resume, and says that a `--fork-session` fork shares the original's session.

## 0.5.12 (2026-11-06)

- `contexto run claude --resume <id>` and `--continue` reopen the Contexto session that served the
  conversation. Before, they opened a new, empty one, so nothing could be recalled. The exit
  prints the command that resumes the session.
- If the launcher is killed, Claude Code is stopped and the temporary files are removed.
- Contexto's warnings are printed after Claude Code exits and no longer land in its input box.
- Contexto options placed after `claude` are refused with the corrected command.

## 0.5.11 (2026-11-06)

- Standing instructions ("whenever X, do Y") survive paging. The model is told to pin a rule as
  soon as you state it. In a live check it pinned and applied 8 of 8 rules, against 0 of 4 before.
- Search results lead with the turn that holds the text. Contexto's own search, recall and state
  calls are no longer searched, so an old search reply cannot outrank the turn it quoted.

## 0.5.10 (2026-11-06)

- Claude Code 2.1.314 and 2.1.318 and Codex 0.166.0 are tested and no longer raise the
  untested-version warning.
- Recall room held for a finished subagent is released early again on Claude Code 2.1.314 and
  2.1.318, which report a subagent's stop in a new form.

## 0.5.9 (2026-11-06)

- A change to the fixed overhead in mid-session (tool search loading a tool, an MCP server
  connecting, a model switch) no longer leaves the next turn over its budget with empty searches.
  At bound 60,000 such a turn went out at about 51,000 tokens and now goes out at about 21,000.
- Every request, rotated or not, is checked against the bound before it is sent.

## 0.5.8 (2026-10-25)

- Files that a nested `CLAUDE.md` imports with `@path` stay in view once their turn is paged out,
  within the 4,000-token cap from 0.5.7.
- Claude Code's placeholder for a lost tool result, and a prompt queued while the model works, no
  longer confuse the check that warns when Contexto's tools are missing.

## 0.5.7 (2026-10-25)

- Claude Code 2.1.310, which turns tool search on by default, is tested.
- No false "tools are missing" warning on 4.x models (for example `claude-sonnet-4-6`) with tool
  search on.
- Memory files loaded from subfolders (`CLAUDE.md`, `CLAUDE.local.md`, `.claude/rules/*.md`) stay
  in view after their turn is paged out, within a 4,000-token cap. On Claude Code 2.1.310 with 5.x
  models their rules had been lost for the rest of the session. On 4.x models they still page out
  with their turn.

## 0.5.6 (2026-10-25)

- One damaged page file no longer stops paging for the session. Search and the index skip it,
  recalling it returns an error, and `contexto recall` exits 1. The log names it as
  `page_unreadable`. A read that fails for a passing reason (permissions, too many open files) is
  tried again on the next call.

## 0.5.5 (2026-10-25)

- Very deeply nested JSON in a tool result or tool input no longer stops paging for the session.
  A request nested more than 512 levels is forwarded unchanged and logged as `request_too_deep`.

## 0.5.4 (2026-10-25)

- `contexto run claude "prompt"`, with the prompt right after `claude`, works. On Claude Code
  2.1.302 and 2.1.310 it failed with "MCP config file not found".
- Your own `--settings` is refused wherever it appears among Claude Code's arguments, including
  after `--`, where it could route the session around the proxy. A prompt that is exactly
  `--settings` or starts with `--settings=` is refused with exit 2.

## 0.5.3 (2026-10-25)

- Recall works in `contexto run claude -p`. Claude Code 2.1.302 refused every Contexto tool call
  in `-p` mode, so recall failed silently. The launcher now allows Contexto's five tools by exact
  name. Your own `deny` and `ask` rules and `--disallowedTools` still win.
- If Claude Code refuses a Contexto tool, Contexto warns once and logs `mcp_tools_denied`.

## 0.5.2 (2026-10-25)

- The first release under the MIT license with `LICENSE` in the wheel and the sdist.
- With `--observe-thinking`, the log keeps every item of the `input_transformations` a response
  reports (up to 32). A later empty list had been able to erase a dropped-thinking report.

## 0.5.1 (2026-10-24)

- Search finds the turn that states a value's current setting. A long turn, or an old dump that
  repeats a key many times, no longer buries it: a word counts once per text that holds it, at
  most 3 times a page, and page length no longer affects the ranking.

## 0.5.0 (2026-10-24)

- A [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) instance is a supported upstream,
  next to the vendor's API with your own key. For upstreams other than the vendor's, each request
  carries a salted per-session `X-Session-Affinity` header, so a routing proxy can keep a session
  on one credential.
- `contexto sessions prune` deletes old sessions (a dry run unless `--yes`, never a session in
  use), and `sessions list` shows each session's size.
- A warning at launch when Claude Code or Codex is a version Contexto was not tested with.
- Each subagent has its own recall room, so a subagent's recall cannot overfill its own window.
- The MCP server holds at most 64 MB of page text for search, set by `CONTEXTO_SEARCH_CACHE_MB`.
  Before, its memory grew with the session.

## 0.1.0 to 0.4.0

0.1.0 was the first release, cut once the acceptance run passed. The 0.1.x releases made search
work for Codex sessions and made the proxy and every MCP server use one store. 0.2.0 let a
restarted proxy resume a session where it stopped, so the prompt cache survives a restart. 0.2.2
added the headroom rule: one large read no longer leaves the session at its smallest window. The
other 0.2.x releases fixed how cut-short and unusual streams are read, made `contexto run codex`
start its MCP server reliably, and cut the time spent on each request in long sessions. 0.3.0 added
the warning for a session that runs without Contexto's tools. 0.4.0 stopped carried context from
growing and made search match every word in any case, ranked by relevance.

Their GitHub releases and tags were removed from GitHub on 2026-11-06, so 0.5.0 is the oldest
release you can download. Their changes are part of every later release.
