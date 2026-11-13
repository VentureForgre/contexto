# Seeing what Contexto did: `contexto stats` and the exit summary

A `contexto run` prints one line when it starts and, since D64, a short summary when the CLI exits.
`contexto stats` prints the same numbers for any session, at any time, with more detail. Both read
the session's `log.jsonl` and its store on this machine. Nothing is written and nothing is sent
anywhere.

This page defines every number, says which are measured and which are estimates, and lists what is
counted as a fault and why.

## The exit summary

```
contexto: run of session 7f3a9c2e41d84b0c9a6f0e1d2c3b4a59 ended after 6h12m
  prompt      peak 148,210 of 150,000 tokens (measured); over the bound: 0 requests
  paged out   212 turns in 9 rotations; 215 pages on disk, about 1.9M tokens (an estimate made when each was paged)
  recall      14 recall, 31 search, 6 pin calls; about 85k tokens read back (an estimate); 2 obligations open
  cache       91% of input tokens read from cache (measured)
  compaction  none seen: Claude Code did not compact the main conversation
contexto: resume this conversation through Contexto with: contexto run --session 7f3a... claude --resume ...
```

(The numbers above are an illustration, not a recorded run.)

- It is printed on stderr after the CLI has exited and given the terminal back, after any warning
  that was held while the CLI ran, and before the line that says how to resume.
- Its first line starts `contexto: run of session`. Only the line printed at start begins
  `contexto: session`, as before, so a script that reads the session id from that line still finds
  one.
- **It covers this run only.** A run is the log from one proxy start to the next, so the summary of a
  resumed session (`contexto run --session ID ...`, `claude --resume`, `claude --continue`) counts
  what happened since this launch, and its header then ends with ``(`contexto stats ID` shows all N
  runs)``. The two store numbers, pages on disk and open obligations, are the session's as it
  stands: a store has no runs. `contexto stats ID` shows the whole session and
  `contexto stats ID --last-run` shows what the summary showed.
- A line appears only when the log holds its data. A run that paged nothing has no `paged out`
  line; a run with no usage from the API has no `cache` line. The `requests` and `bytes` lines are
  left to `contexto stats`, except that a run with failed model requests shows the `requests` line.
- `contexto run --quiet claude ...` or `CONTEXTO_QUIET=1` (any value but empty, `0`, `false` or
  `no`) drops the summary. Warnings are never dropped: the held warnings, and the warning for faults
  that no other warning described (below), are printed either way.
- Building the summary never changes the exit code. If it fails, nothing is printed.
- No summary is printed when the CLI was only asked for its version or help (`--version`, `-v`,
  `-V`, `--help`, `-h`), or when the launch ended before the CLI started (a usage error, Ctrl+C
  during the version check).

### No model request

```
contexto: run of session 7f3a... ended after 41s with no model request through Contexto (nothing reached the proxy). If you sent claude a prompt, it bypassed Contexto and nothing was bounded or paged: see docs/stats.md, 'No model request'.
```

The CLI started and exited, and the log holds no `POST` to `/v1/messages` or `/responses` for this
run. That is normal if you quit without sending a prompt, or ran a subcommand that talks to no model.
If you did send a prompt, the CLI talked to its upstream directly, and Contexto bounded and paged
nothing. The line says how many other requests reached the proxy: Claude Code's startup probe
(`HEAD /api/hello`) arriving shows that the CLI was using the proxy's address.

Known causes:

- Claude Code: organization-managed settings that set `ANTHROPIC_BASE_URL` outrank the settings file
  `contexto run` passes.
- Codex: none has been measured. In ChatGPT login mode some Codex calls (rate limits, usage) go to
  chatgpt.com directly by design; those are not model requests.

This line is part of the summary, so `--quiet` drops it.

## `contexto stats`

```
contexto stats [SESSION] [--session ID] [--last-run] [--json]
```

`SESSION` is a session id or a unique prefix of one (`--session ID` means the same; give one of
them). Without it the session is chosen as `contexto state` chooses it: `$CONTEXTO_SESSION`, else
the most recently active session that has pages or pins or is in use. `--last-run` counts only the
most recent run. Exit code 0; 1 when no session matches; 2 for a usage error.

```
Contexto stats for session 7f3a9c2e41d84b0c9a6f0e1d2c3b4a59: the whole session (3 runs, 6h12m of log)
  requests    412 model requests (380 main, 32 side); 2 failed (429 x2)
  prompt      peak 148,210 of 150,000 tokens (measured); over the bound: 0 requests
  paged out   212 turns in 9 rotations; 215 pages on disk, about 1.9M tokens (an estimate made when each was paged)
  recall      14 recall, 31 search, 6 pin calls; about 85k tokens read back (an estimate); 2 obligations open
  cache       91% of input tokens read from cache (measured)
  bytes       the CLI sent 1.2 GiB, Contexto forwarded 310.0 MiB (25%)
  compaction  none seen: Claude Code did not compact the main conversation
  faults      none
```

## Measured and estimated

- **Measured** means the API reported it in a response's `usage`, or it is a byte count or an event
  count the proxy made itself.
- **Estimate** means Contexto computed it from bytes. Three numbers are estimates and say so:
  the tokens kept on disk (`pages.tokens_estimate`), the tokens tools read back
  (`tools.recall.charged_tokens` and the like), and the peak prompt when the API reported no usage
  (`prompt.peak_source` is then `estimate`).

## The numbers

Each heading is a key of `contexto stats --json`. The text output shows the same values.

A **model request** is a log line with `event: request`, `method: POST`, and a path of `/v1/messages`,
`/responses` or `/v1/responses`. `count_tokens` requests and everything else the CLI sends through
the proxy (Claude Code's `HEAD /api/hello`) are not model requests. Requests of every conversation
count, the main one and side ones (subagents, Claude Code's title and topic requests), unless a
number says otherwise.

### Session and `log`

| Key | Meaning |
|---|---|
| `version` | The version of this JSON shape, `1`. It is raised only when a documented key changes meaning or is removed. Keys may be added without raising it. |
| `session` | The session id. |
| `scope` | `session` (the whole log) or `last_run` (`--last-run`, and the exit summary). |
| `runs` | Proxy starts in the whole log: one per `contexto run` or `contexto proxy`. |
| `cli` | `claude` or `codex`, from the latest `launch` line; `null` when the log has none. |
| `bound` | The bound in tokens: the latest `launch` line's, else one carried by an `over_bound` or `bound_too_small` event, else `null`. A session resumed with another `--bound` shows the latest. |
| `started`, `ended` | The first and last timestamp in scope, as logged (UTC). |
| `span_seconds` | `ended` minus `started`. It is the span of the log, not time spent working. |
| `log.missing` | `true` when the session has no readable `log.jsonl` (absent, or not a regular file). |
| `log.lines` | Non-empty lines in the log. |
| `log.unreadable_lines` | Lines that are not a JSON object: a line cut short by a full disk, or damage. They are skipped, so the numbers may be low; the text output says so. |

### `requests`

| Key | Meaning |
|---|---|
| `requests.model` | Model requests in scope. |
| `requests.main`, `requests.side` | Model requests of the main conversation and of any other (the engine marks each request). |
| `requests.with_usage` | Model requests whose response carried a prompt token count. |
| `requests.failed` | Model requests answered with status 400 or above, by upstream or by the proxy (a 502 when upstream is unreachable). |
| `requests.status` | Those failures by status code, for example `{"429": 2}`. |
| `requests.aborted` | Model requests the CLI gave up on before the response ended (Esc, Ctrl+C). |
| `requests.count_tokens` | `count_tokens` requests. |
| `requests.other` | Every other request through the proxy. |

### `prompt`

| Key | Meaning |
|---|---|
| `prompt.peak_tokens` | The largest prompt of any model request in scope. |
| `prompt.peak_source` | `usage`: the API's own count. For Claude it is `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`; for Codex `input_tokens`, which already includes cached tokens. `estimate`: no response in scope carried usage, so this is the largest estimate the engine made before sending. |
| `prompt.last_tokens`, `prompt.last_source` | The main conversation's most recent prompt, the same way. A status line can show it against the bound. |
| `prompt.bound` | The same value as `bound`. |

### `over_bound`

A request is over the bound when the proxy marked its log line `over_bound: true`. The engine checks
twice: before sending, against its estimate, and after the response, against the API's count.

| Key | Meaning |
|---|---|
| `over_bound.requests` | Model requests marked over the bound. |
| `over_bound.measured` | Of those, the ones whose real prompt, as the API counted it, was above the bound. |
| `over_bound.estimate_only` | Of those, the ones flagged only by the estimate before sending: the API then counted a prompt at or under the bound, or reported no usage. |
| `over_bound.in_tool_loop` | Flagged before sending, inside a tool loop. Contexto pages only at a turn start, so one turn that keeps growing through tool calls can pass the bound. This is the expected exception (contract rule 5). |
| `over_bound.at_turn_start` | Flagged before sending, at a turn start. The rotation ran and the request still did not fit: the bound is too small for the fixed overhead plus the new turn (`bound_too_small`, usually with a launcher warning), the sizing did not hold, or paging was stopped. This one is worth acting on: use a larger `--bound`. |
| `over_bound.degraded` | Marked over the bound while paging was stopped (see Faults). |

`in_tool_loop` plus `at_turn_start` can be less than `requests`: a request the estimate passed and
the API then counted over the bound has no position in the log.

The text says "requests", not "turns": the log does not number turns.

### `paging` and `pages`

| Key | Meaning |
|---|---|
| `paging.rotations` | `rotation` events: each starts a new epoch. A rotation after a head change can page nothing. |
| `paging.turns_paged` | Turns written to the store by those rotations (the sum of `page_out.turns`). A turn paged again counts again: after a resync, and after a restart that could not resume its epoch. |
| `paging.pages_written` | Pages those rotations wrote or found already written. |
| `paging.carried_items_paged` | Carried messages paged on their own by the bounded fallback. |
| `pages.count` | Page files in the session's `pages/` now. This is the store, so it is the same in every scope. |
| `pages.tokens_estimate` | The sum of the token estimates stored in those pages. Each was made when the turn was paged, at Contexto's seed ratios (3.5 bytes a token for text, 1.5 for tool output, images and PDFs at their estimate). It is not a measurement; the API would count the same turns differently. `null` when the caller asked not to open the pages. |
| `pages.file_bytes` | The size of those files. |
| `pages.unreadable` | Page files whose token estimate could not be read. They count in `pages.count` and add nothing to the estimate. |

### `tools` and `pins`

From the MCP server's `mcp_call` lines. Commands you type (`contexto recall`, `contexto search`) are
not tool calls and are not counted.

| Key | Meaning |
|---|---|
| `tools.recall`, `tools.search`, `tools.state`, `tools.pin`, `tools.close` | One object per tool. |
| `tools.recall.calls` | Calls that succeeded (the same key exists for each tool). |
| `tools.recall.failed` | Calls that returned an error to the model. |
| `tools.recall.charged_tokens` | Tokens the tool's replies were charged against the turn's recall room: an estimate, the reply's characters divided by the session's measured bytes per token, capped at 1.5, so it errs high. Always 0 for `pin` and `close`. |
| `tools.out_of_room` | Calls that found the turn's recall room used up, so they returned little or nothing. |
| `pins.readable` | `false` when the ledger is damaged; the two counts are then `null`. |
| `pins.active_records` | Records in the ledger that nothing supersedes, now. |
| `pins.open_obligations` | Obligations not closed, now. |

### `cache`

Measured, from `usage`, over the model requests that carried it.

| Key | Meaning |
|---|---|
| `cache.requests` | How many requests that is. |
| `cache.input_tokens` | The sum of their prompt tokens, counted as for `prompt.peak_tokens`. |
| `cache.cache_read_tokens` | The part read from the prompt cache (`cache_read_input_tokens`; for Codex `input_tokens_details.cached_tokens`). |
| `cache.cache_write_tokens` | The part written to the cache (`cache_creation_input_tokens`; Codex reports none). |
| `cache.read_share` | `cache_read_tokens / input_tokens`, or `null` with no usage. This is a share of input tokens. The README's acceptance tables count something else: requests that reused the cache. |

### `bytes`

Measured by the proxy, over model requests.

| Key | Meaning |
|---|---|
| `bytes.cli_sent` | The sum of the request bodies the CLI sent (decoded, if the CLI compressed them). |
| `bytes.forwarded` | The sum of the bodies Contexto sent upstream. |
| `bytes.forwarded_share` | `forwarded / cli_sent`, or `null`. |
| `bytes.largest_cli_request`, `bytes.largest_forwarded_request` | The largest single body on each side. |

The CLI resends its whole history with each request, so `cli_sent` grows with the square of the
session's length. It shows what Contexto took out of the requests; it is not what the session would
have cost without Contexto, because the CLI would have compacted.

### `compaction`

`compaction.seen` counts the times the engine saw Claude Code's main conversation replaced by its
compacted one (`main_changed` with reason `compacted`, D50 and D59). The engine recognises a
compacted conversation by the sentence Claude Code opens its summary with, in the main
conversation's own session id.

What `"seen": 0` supports is "Claude Code did not compact the main conversation in this log". Limits:

- Measured on Claude Code 2.1.314 and 2.1.318. A version that words the summary differently is not
  recognised.
- A subagent that compacts is not counted.
- A compaction before the first request of a run is not counted: resuming a conversation that was
  compacted earlier, with or without Contexto, shows nothing.
- A first prompt that reproduces the sentence and the blank line after it is read as a compaction.

For a session with no Messages API request (Codex), `compaction` is `null` and the text has no
line: no event in the log shows a Codex compaction, so nothing is claimed.

### `faults`

| Key | Meaning |
|---|---|
| `faults.total` | Fault events in scope (the list is below). |
| `faults.events` | The count of each. |
| `faults.degraded` | Degraded mode, in which paging is stopped (below). |
| `faults.degraded.entered`, `faults.degraded.left` | Times paging stopped and resumed. |
| `faults.degraded.now` | Whether paging is stopped at the end of the scope, for the main conversation (for any conversation when the scope has no main request). A new run always starts paging. |
| `faults.degraded.since` | When it stopped, if it is stopped now. |
| `faults.degraded.unpaged_requests` | Model requests sent while paging was stopped. |
| `faults.degraded.largest_unpaged_request_bytes` | The largest body among them. |
| `faults.store_errors` | One entry per distinct store failure: `event`, `operation`, `error` (the exception class), `errno` (its name, such as `ENOSPC`), `cause`, `count`, `first`, `last`. |
| `faults.messages` | What happened and what to do, in words, for the store failures. |

## Faults

### When the store fails: degraded mode

If the store cannot be read or written (`store_error`) or a stored page does not match its turn
(`page_conflict`), the engine stops paging. It keeps the last good window and appends new messages
to it, so nothing is lost and the prompt cache holds, but requests go out unpaged and can exceed the
bound. At each turn start it tries the store once and pages again when that works (contract rule
14). Before D64 this was silent: in the 2026-11-12 audit a full disk let requests grow from 67 KB
to 354 KB with nothing on stderr.

It is now said in three places:

1. **On stderr**, once per cause, when the CLI exits (held while the CLI owns the terminal, for
   Claude Code and Codex alike):

   ```
   contexto: warning: the disk that holds Contexto's store is full (ENOSPC during page_out), so paging has stopped: requests go out unpaged and can exceed the bound. Free space on the disk that holds $CONTEXTO_HOME (default ~/.contexto). Contexto retries at each turn start and pages again once the store works; turns already paged are kept.
   ```

   and a line in the summary that counts the run's faults and says whether paging resumed:

   ```
     faults      3 events (store_error 2, degraded_enter 1): 2 requests went out unpaged, and paging had not resumed at exit; details: contexto stats 7f3a...
   ```

2. **In `contexto stats`**, under `faults`, with the same advice and whether paging has resumed.
3. **In `contexto state`**, for the latest run only: `Warning:` when paging is stopped now, `Note:`
   when it stopped and resumed. `contexto state --raw` is the model's text and is unchanged.

The advice depends on the cause, which is read from the `store_error` event's `errno` (added in D64;
before it the event held only the exception class, so a full disk looked like a permission problem):

| `cause` | When | What to do |
|---|---|---|
| `disk_full` | `ENOSPC`, `EDQUOT` | Free space on the disk that holds `$CONTEXTO_HOME`. |
| `no_access` | `EACCES`, `EPERM`, `EROFS` | Check the owner and permissions of the session directory, and that the filesystem is not read-only. |
| `page_conflict` | a stored page differs from its turn | Nothing at once: at the next turn start the page is moved to the session's `quarantine/` and the turn is paged again. If it repeats, check the disk. |
| `store` | anything else (a damaged ledger, a missing key file) | Run `contexto sessions show ID` to check the ledger and pages. |

The log never holds the exception's message, which can contain paths: only the class, the errno's
name, and the store operation.

**A full disk also stops the log.** When `log.jsonl` cannot be appended to, the launcher says so
(`the session log could not be written (ENOSPC) ...`), the store warning above is still shown
because it is raised before the log write, and the CLI's exit code is kept (before D64 a failed
write of the `exit` line ended the launcher with a traceback and exit code 1). `contexto stats` reads
the log, so it then shows less than happened; the exit summary says its numbers are low.

### Which events count as a fault

A fault is an event that means Contexto left its designed path. The set is the contract's
acceptance list (docs/milestones/M2-contexto-proxy.md, acceptance check 1: "any event that means the
product left its designed path") with one event taken out and the events for damage and bugs that
are logged outside the engine added.

| Event | What it means |
|---|---|
| `degraded_enter` | Paging stopped after a store failure. |
| `store_error` | The store could not be read or written. |
| `page_conflict` | A stored page did not match the turn it should hold. |
| `fallback_failed` | A request was forwarded as the CLI sent it, unbounded, because a failure happened while paging was already stopped. |
| `transform_error` | A Contexto bug: the engine raised, and the request was forwarded as the CLI sent it. Please report it. |
| `proxy_error` | The proxy failed while handling a request. |
| `request_too_deep` | A request nested deeper than 512 levels was forwarded as the CLI sent it. |
| `engine_unavailable` | The rewrite engine was missing, so nothing was bounded. |
| `websocket_rejected` | The CLI tried a WebSocket, which would bypass the bound; Contexto refused it. |
| `bound_too_small` | The bound could not hold one turn, so requests exceeded it. Use a larger `--bound`. |
| `carried_over_budget` | Content Contexto cannot page grew more than 8,000 tokens past its size at the first request. |
| `unclassified_system_message` | The CLI sent a system message of a kind Contexto does not know. It usually means the CLI changed; see docs/cli-updates.md. |
| `session_start_unrecognised` | The CLI's session-start message was not recognised, so it may be paged when it should be carried. Logged at most once per run, and only for a request that holds the CLI's injected context; it also means the CLI changed. |
| `page_quarantined` | A damaged page was moved to quarantine. |
| `page_unreadable` | A page file could not be read. |
| `ledger_damaged` | The ledger of pins is damaged. |
| `retained_stale` | The snapshot of the conversation in view could not be written, so search cannot say what is in view. |
| `epoch_record_stale` | The record a restart resumes from could not be written; the next launch rotates once more. |
| `side_room_close_failed` | A subagent's recall room could not be closed; it expires instead. |
| `mcp_tools_missing` | The model had none of Contexto's tools, so it could not recall. The exit-time repeat of a warning already counted is not counted again. |
| `mcp_tools_denied` | The CLI refused a Contexto tool. |
| `turn_room_missing` | A Contexto tool found no recall room for its turn. |
| `toolcheck_error`, `cli_version_error`, `observe_usage_error` | One of Contexto's own checks failed. |

The launcher already warns on stderr, in its own words, about `store_error`, `page_conflict` (and so
`degraded_enter`), `mcp_tools_missing`, `mcp_tools_denied` and `bound_too_small`. For a run that
logged any other fault, `contexto run` ends with one more warning, which `--quiet` does not drop:

```
contexto: warning: this run logged 2 events (retained_stale 1, transform_error 1) that Contexto left its designed path. Details: contexto stats 7f3a...
```

Events that are **not** counted, because they happen in normal use:

- `turn_room_low`: the one event taken out of the contract's list. A turn started with little
  recall room. At a small bound every epoch logs it (QA 2026-11-05 at bound 60,000), and the
  launcher warns about the bound when the session starts.
- `upstream_4xx`: Claude Code's keyless `HEAD /api/hello` gets a 401 at every launch, and a 429 is
  the upstream's rate limit. Failed model requests are shown under `requests`.
- `usage_missing`, `usage_unattributed`: a response without usage, or two requests of one
  conversation in flight.
- `resync`: `/rewind`, or an edit to an earlier message.
- `rotation_noop`, `epoch_not_resumed`, `head_changed`, `window_minimal`, `side_room_expired`,
  `hold_skipped`, `upstream_retry`, `stale_temp_removed`: the engine's and the proxy's ordinary
  bookkeeping.
- `estimate_error`: the estimate was more than 10% under the real count; the next request is sized
  from the real one.
- `pdf_allowance`, `pdf_pages_uncertain`: sizing notes about a PDF, which the launcher prints.
- `cli_version_untested`: a condition, not a failure. The launcher warns about an untested CLI
  version when the session starts.

## What the numbers cannot show

- Lines the log lost (a full disk) are not counted.
- Two launches that share one session (`--session ID` given to both) write one log; their runs
  interleave and `--last-run` then mixes them.
- A launcher killed with SIGKILL prints no summary. `contexto stats` still works.
- Turn counts: the log counts requests and paged turns, not the turns you typed.

## Speed

`session_stats` reads the whole log once. On a synthetic log shaped like a 6-hour session (4,000
model requests, 200 rotations, 445 tool calls; 4,449 lines, 2.9 MB) it took 46 ms for the log alone
and 55 ms with the ledger and pages (best of 15 runs each), of which parsing the JSON was 24 ms.
That was on a development laptop running several other test suites at the time (load average 23 to
58), so the figures are high. The time grows with the log's size.

A healthy log is parsed with one `json.loads` call (27 ms on that log, against 42 ms for one call
per line). A damaged line fails that parse, and the log is then read line by line. A page file is
read at its last 96 bytes only, where the token estimate is.

## Calling it from code

```python
from contexto.paths import contexto_home
from contexto.stats import session_stats

stats = session_stats(contexto_home(), session_id)                  # the dict `--json` prints
stats = session_stats(contexto_home(), session_id, last_run=True)   # this run only
stats = session_stats(contexto_home(), session_id, store=False)     # the log only: no ledger, no pages
```

It returns a plain dict of JSON values, raises `SessionNotFoundError` for a session that does not
exist and nothing else, and reads only files under the session's directory.
`session_stats(..., page_tokens=False)` counts the page files without opening them.
`contexto.stats.session_faults(home, session_id)` returns the latest run's `faults` object, which is
what `contexto state` prints from.
