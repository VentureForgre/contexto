# Changelog

What each release of Contexto changes for someone running `contexto run claude ...` or
`contexto run codex ...`. This file is written by hand: a release's section is added here before
the release is cut. The newest release is first.

Versions follow [Semantic Versioning](https://semver.org/). While the version is below 1.0.0, a
breaking change is a minor bump. The wheel and sdist of each release are at
https://github.com/VentureForgre/contexto/releases. How releases are cut is in
[RELEASING.md](RELEASING.md).

## Unreleased

### Changed

- The package is now named `contexto-proxy`. The command and the import name are still `contexto`.
  If you installed 0.5.24 or earlier, uninstall `contexto` before you install the new one, or use
  `install.sh`, which does it for you: both provide the same `contexto` command. The wheel and
  sdist are named `contexto_proxy-<version>`.
- Releases are no longer cut on every fix. They are batched, and each has a section in this file.

### Added

- `install.sh`: one line installs the latest release, after checking the wheel against the
  release's `SHA256SUMS`, and upgrades it when run again. See the README's Install section.
- `contexto --version` names the revision of an install from a git URL, for example
  `contexto 0.0.0+checkout (git v0.6.0, commit 1a2b3c4d)`.

## 0.5.24 (2026-11-06)

- A Codex turn that starts with large tool output rarely goes out over the bound unnoticed now
  (1 of 32 test sessions, down from 26). Codex tool output is counted at a cautious 1.5 bytes per
  token until real usage arrives, as Claude Code's already was. Codex sessions whose responses
  carry no input count page out 9 to 20% more often as a result.
- A session started on an earlier release has its fixed overhead (system prompt and tool
  definitions) measured again when it resumes, so a low measurement is not carried on. A session
  used on 0.5.24 starts its window over if you go back to 0.5.23 or earlier; its pages stay.
- An image counts the higher of two estimates, by pixel count and by 28-pixel tile, at most 4,787
  tokens. Sizing a crafted JPEG no longer slows a request.
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
- Claude Code's own compaction (`/compact` or automatic) is recognised in the three forms Claude
  Code 2.1.314 and 2.1.318 send, so the compacted conversation becomes the main one at once.
  0.5.15 to 0.5.21 recognised one of them.
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

- Recall works after `/clear`. The new conversation becomes the main one at once. Before, it was
  treated like a subagent's conversation until it outgrew the old one, and recall returned
  nothing. `/resume` is followed the same way, in interactive and headless runs, and `/compact` in
  one of its forms (the rest in 0.5.22).
- Subagents, agent-team teammates and a `claude -p` started from a tool never become the main
  conversation. Each handover is logged as `main_changed`.

## 0.5.14 (2026-11-06)

### Security

- `contexto run codex` uses a project's `apiKeyHelper` and base URL only in a folder Claude Code has
  recorded as trusted. 0.5.13 and earlier ran a cloned repository's helper command and could send
  your key to its host. To use project settings, run `claude` in the folder once and accept the
  trust dialog, or pass `--codex-key-helper` and `--codex-upstream`.
- While `CLAUDE_CODE_IGNORE_PERSISTED_TRUST` is `1`, `true`, `yes` or `on`, no project settings
  file is used by `run claude`, `run codex` or `proxy`, as in `claude -p`. With it set, 0.5.13 and
  earlier still sent `run claude -p` and `proxy` requests, with your credential, to a base URL
  set in the project.

### Fixed

- Contexto reads settings files in the order Claude Code does. In 8 of 27 measured layouts
  (worktrees, nested folders) it had picked a different upstream than Claude Code would.

## 0.5.13 (2026-11-06)

- The first release with a `SHA256SUMS` file and a build provenance attestation. Check a download
  with `gh attestation verify <file> --repo VentureForgre/contexto` or
  `shasum -a 256 -c SHA256SUMS --ignore-missing`. Releases up to 0.5.12 have neither.
- Launches started at the same moment no longer share an automatically chosen session. A launch
  that has waited 5 s for another to choose goes on without waiting.
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
- The token budget held for a finished subagent's recalls is released early again on Claude Code
  2.1.314 and 2.1.318, which report a subagent's stop in a new form.

## 0.5.9 (2026-11-06)

- A change to the fixed overhead in mid-session (tool search loading a tool, an MCP server
  connecting, a model switch) no longer leaves the next turn over its budget with empty searches.
  At bound 60,000 such a turn went out at about 51,000 tokens and now goes out at about 21,000.
- A request estimated over the bound is logged as `over_bound` (and still sent), whether or not
  Contexto paged anything out of it. Before, a request it had just paged was never checked.

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

- A page file that is valid JSON with damaged fields no longer stops paging for the session. It
  is handled like any unreadable page (since 0.5.5): search and the index skip it, recalling it
  returns an error, and the log names it as `page_unreadable`. A read that fails for a passing
  reason (permissions, too many open files) is tried again on the next call.

## 0.5.5 (2026-10-25)

- Very deeply nested JSON in a tool result or tool input no longer stops paging for the session.
  A request nested more than 512 levels is forwarded unchanged and logged as `request_too_deep`.
- A page file that is not valid JSON, or is nested too deep, is a one-line error from
  `contexto recall` (exit 1), not a traceback. Search skips it.

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
- With `--observe-thinking`, which logs when the API reports that it dropped thinking from a
  request, the log keeps every such report of a response (up to 32). A later empty report had
  been able to erase an earlier one.

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
- Each subagent has its own token budget for recall, so a subagent's recall cannot overfill its
  own window.
- The MCP server holds at most 64 MB of page text for search, set by `CONTEXTO_SEARCH_CACHE_MB`.
  Before, its memory grew with the session.

## 0.1.0 to 0.4.0

0.1.0 was the first release. The 0.1.x releases made search
work for Codex sessions and made the proxy and every MCP server use one store. 0.2.0 let a
restarted proxy resume a session where it stopped, so the prompt cache survives a restart. 0.2.2
capped the space held back after a large read, so one large read no longer leaves the session at
its smallest window. The
other 0.2.x releases fixed how cut-short and unusual streams are read, made `contexto run codex`
start its MCP server reliably, and cut the time spent on each request in long sessions. 0.3.0 added
the warning for a session that runs without Contexto's tools. 0.4.0 stopped the context kept in
every request from growing and made search match every word in any case, ranked by relevance.

Their GitHub releases and tags were removed from GitHub on 2026-11-06, so 0.5.0 is the oldest
release you can download. Their changes are part of every later release.
