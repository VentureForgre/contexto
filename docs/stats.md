# Seeing what Contexto did: `contexto stats` and the exit summary

A `contexto run` prints one line when it starts and, since D64, a short summary when the CLI exits.
`contexto stats` prints the same numbers for any session, at any time, with more detail. Both read
the session's `log.jsonl` and its store on this machine. Nothing is written or created, nothing is
waited for (a process that holds the ledger's lock does not hold a reader up), and nothing is sent
anywhere.

This page defines every number, says which are measured and which are estimates, and lists what is
counted as a fault and why.

## The exit summary

```
contexto: run of session 7f3a9c2e41d84b0c9a6f0e1d2c3b4a59 ended after 6h12m
  prompt      peak 148,210 of 150,000 tokens (measured); over the bound: 0 requests
  paged out   212 pages in 9 rotations this run; the session has 215 pages on disk, about 1.9M tokens (an estimate made when each was paged)
  recall      51 calls (14 recall, 31 search, 6 pin); about 85k tokens charged to the recall room (a budget charge that runs high, not a count of what was read); 2 obligations open
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
  stands: a store has no runs. The `paged out` line keeps the two apart: what this run added
  (`3 pages in 1 rotation this run`, or `nothing this run`), then `the session has N pages on
  disk`. `contexto stats ID` shows the whole session and `contexto stats ID --last-run` shows what
  the summary showed.
- A line appears only when the log holds its data. A session with no page and a run with no
  rotation has no `paged out` line; a run in which no response reported a cache field has no
  `cache` line. The `requests` and `bytes` lines are left to `contexto stats`, except that the
  `requests` line is shown when a model request failed, was cut short, was forwarded unread, was
  refused, or was still in flight when the proxy stopped.
- `contexto run --quiet claude ...` or `CONTEXTO_QUIET=1` (any value but empty, `0`, `false` or
  `no`) drops the summary. Warnings are never dropped: the held warnings, and the warning for faults
  that no other warning described (below), are printed either way. One line is added under
  `--quiet` when a store failure was warned about and paging then resumed, because that warning
  was written while the store was failing:
  `contexto: paging resumed before claude exited; meanwhile 2 requests went out unpaged.`
- Building the summary never changes the exit code. If it fails, nothing is printed. That holds
  for Ctrl+C while it is being built: the summary is dropped, and the exit code and the resume
  line are kept.
- No summary is printed when the CLI was only asked for its version or help (`--version`, `-v`,
  `-V`, `--help`, `-h`), or when the launch ended before the CLI started (a usage error, Ctrl+C
  during the version check).
- The summary reads only the latest run's part of the log, so it does not slow down as a resumed
  session's log grows (see Speed).

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

This line is part of the summary, so `--quiet` drops it. It is not printed when the CLI could not
be started (the launcher has said `cannot start`, exit code 127, and nothing reached the proxy) or
when `--help`, `-h` or `--version` is anywhere in the CLI's arguments (`codex exec --help`).

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
  paged out   215 pages on disk, about 1.9M tokens (an estimate made when each was paged); 11 rotations
  recall      51 calls (14 recall, 31 search, 6 pin); about 85k tokens charged to the recall room (a budget charge that runs high, not a count of what was read); 2 obligations open
  cache       91% of input tokens read from cache (measured)
  bytes       the CLI sent 1.2 GiB, Contexto forwarded 310.0 MiB (25%)
  compaction  none seen: Claude Code did not compact the main conversation
  faults      none
```

The header's time is the span of the log when the session is one run. When its runs are apart, it
shows both: `(2 runs, 10s in runs, 72h00m from the first log line to the last)`.

Rounding: a share is printed as a whole percent, and reads `0%` or `100%` only when it is exactly
none or all (`under 1%` and `over 99%` otherwise). A large token count is rounded to two or three
figures (`85k`, `1.9M`); the peak is printed exactly.

## Measured and estimated

- **Measured** means the API reported it in a response's `usage`, or it is a byte count or an event
  count the proxy made itself.
- **Estimate** means Contexto computed it from bytes. Three numbers are estimates and say so:
  the tokens kept on disk (`pages.tokens_estimate`), the peak prompt when the API reported no usage
  (`prompt.peak_source` is then `estimate`), and the largest request the API reported no usage for
  (`prompt.peak_estimate_without_usage`).
- **A charge** is neither. `tools.recall.charged_tokens` and the like are what the tools' replies
  were charged against a turn's recall room: a budget figure that is high on purpose (below). It is
  not a count of the tokens the model read, and it does not compare with the estimate of the pages
  on disk, which is made at less than half that rate.
- **What the log cannot show is `null`, and has no line.** How many pages a run added, in a log
  written before the proxy recorded the store's page count; the cache share, when no response
  reported a cache field; "Claude Code did not compact", when the requests could not have shown a
  compaction.

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
| `runs` | Proxy starts in the whole log: one per `contexto run` or `contexto proxy`. A run is the log from the lines its launch wrote while opening the session, through its proxy start, to its `exit` line (to the next proxy start for a run that was killed, and for `contexto proxy`, which writes no `exit`). |
| `cli` | `claude` or `codex`, from the latest `launch` line; `null` when the log has none. |
| `bound` | The bound in tokens: the latest `launch` line's, else one carried by an `over_bound` or `bound_too_small` event in scope, else `null`. A session resumed with another `--bound` shows the latest. |
| `started`, `ended` | The first and last timestamp in scope, as logged (UTC). A `ts` that is not a time is not used. |
| `span_seconds` | `ended` minus `started`. It is the span of the log, not time spent working: two 5-second runs three days apart span 72 hours. |
| `run_seconds` | The time in runs: each run's first line to its last, added up. `null` when the log has no proxy start. For the last run it equals `span_seconds`. |
| `log.missing` | `true` when the session has no readable `log.jsonl` (absent, or not a regular file). |
| `log.lines` | Non-empty lines read: the whole log, or for the last run its own lines (the lines before it are searched for run starts, not parsed). `log.unreadable_lines` and `log.ignored_values` cover the same lines. |
| `log.unreadable_lines` | Lines that are not a JSON object: a line cut short by a full disk, or damage. They are skipped, so the numbers may be low; the text output says so. |
| `log.ignored_values` | Values in readable lines that cannot be what their field holds: a string or a negative number where a count belongs (bytes, tokens, a status, a page count), a `ts` that is not a time, an event or tool name that is not text. Each is left out of its number and counted here; the text output says so. |

### `requests`

| Key | Meaning |
|---|---|
| `requests.model` | Model requests in scope. |
| `requests.main`, `requests.side` | Model requests of the main conversation and of any other (the engine marks each request). |
| `requests.with_usage` | Model requests whose response carried a prompt token count. |
| `requests.failed` | Model requests answered with status 400 or above, by upstream or by the proxy (a 502 when upstream is unreachable). |
| `requests.status` | Those failures by status code, for example `{"429": 2}`. |
| `requests.aborted` | Model requests the CLI gave up on before the response ended (Esc, Ctrl+C). |
| `requests.cut_short` | Model requests whose response ended before it was complete: upstream closed the stream early, or the relay failed (`error: relay: ...` on the request line). The status can still be 200. |
| `requests.not_transformed` | Model requests whose body the engine could not read (not JSON, not an object, nested too deep), forwarded as the CLI sent them (`transform_skipped` on the request line). They are counted in `requests.model`. |
| `requests.rejected_encoding` | Model requests the proxy answered 415 because it could not decode their body (`encoded_body_rejected`). They have no request line, so they are in no other number, `requests.model` included. |
| `requests.in_flight_at_stop` | Requests still in a handler when the proxy stopped (`proxy_stop_incomplete`). They may have no request line, so the numbers can miss them. |
| `requests.count_tokens` | `count_tokens` requests. |
| `requests.other` | Every other request through the proxy. |

### `prompt`

| Key | Meaning |
|---|---|
| `prompt.peak_tokens` | The largest prompt the API counted, among the model requests in scope whose response carried usage. When none did, the largest estimate the engine made before sending. |
| `prompt.peak_source` | `usage`: the API's own count. For Claude it is `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`; for Codex `input_tokens`, which already includes cached tokens. `estimate`: no response in scope carried usage. |
| `prompt.peak_estimate_without_usage` | The largest estimate among the model requests whose response carried no usage (answered with an error, or aborted before usage arrived); `null` when every request had usage. The measured peak leaves these requests out, and they are the ones most likely to be large: the API refuses a prompt that is too long. When this is above a measured peak the text says so: `peak 50,000 of 150,000 tokens (measured); a request the API reported no usage for was estimated larger, at 412,000`. |
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

The text's headline is what the API measured (`over_bound.measured`). Requests that only the
estimate flagged, and where the flagged ones were, follow as clauses of their own, because they
split the requests two different ways:

```
over the bound: 4 requests (measured); 1 more by Contexto's estimate only; of the 4 flagged before sending, 3 inside a tool loop and 1 at a turn start; 1 while paging was stopped
```

When no response carried usage, the headline counts the estimate and says so. The text says
"requests", not "turns": the log does not number turns.

### `paging` and `pages`

| Key | Meaning |
|---|---|
| `paging.rotations` | `rotation` events in scope: each starts a new epoch. A rotation after a head change can page nothing. |
| `paging.pages_added` | The pages the store gained during the runs in scope: for each run, the store's page count when its proxy stopped less the count when it started (the `pages` field of the `proxy_start` and `proxy_stop` lines), added up. A run still in progress, or killed before its stop line, is counted to the next run's start, or to the pages on disk now. `null` when a run in scope has no count: a log written before the field existed, a damaged count, or a run in progress read with `store=False`. Pages moved to quarantine during a run lower its gain, never below 0. |
| `paging.carried_items_paged` | Carried messages paged on their own by the bounded fallback (the sum of `carried_paged.count`). |
| `pages.count` | Page files in the session's `pages/` now. This is the store, so it is the same in every scope. |
| `pages.tokens_estimate` | The sum of the token estimates stored in those pages. Each was made when the page was written, at Contexto's seed ratios (3.5 bytes a token for text, 1.5 for tool output, images and PDFs at their estimate). It is not a measurement, and it errs high for tool output: 1.5 bytes a token is a cautious rate, and real tool output usually costs fewer tokens than that. `null` when the caller asked not to open the pages. |
| `pages.file_bytes` | The size of those files. |
| `pages.unreadable` | Page files that are not a readable page: the record does not carry the file's own id (a damaged start), or its token estimate is missing or not a count. They count in `pages.count` and add nothing to the estimate. |

**How much is paged out is read from the store, not from the log.** The engine's `page_out` event
counts the turns a rotation handed to the store, and a launch that cannot resume its epoch (an
edited CLAUDE.md, a CLI update, `--model`, another `--bound`) hands over the whole history again,
onto pages that already exist. Three launches of one 31-turn conversation logged 69 turns for 26
pages. The log cannot tell a new page from one found already written, so the sum of `page_out.turns`
is not shown anywhere, in the text or in the JSON. A run's own part is what the store gained while
it ran.

The noun is **pages**, not turns. A page holds one turn, except a page written by the bounded
fallback, which holds one carried message (a system reminder or a context block) and no turn. The
two cannot be told apart without parsing every page, which the count never does: page files are
counted from the directory listing alone.

### `tools` and `pins`

From the MCP server's `mcp_call` lines. Commands you type (`contexto recall`, `contexto search`) are
not tool calls and are not counted.

| Key | Meaning |
|---|---|
| `tools.recall`, `tools.search`, `tools.state`, `tools.pin`, `tools.close` | One object per tool. |
| `tools.recall.calls` | Calls that succeeded (the same three keys exist for each tool). A call that found the recall room used up and returned little or nothing succeeded, and is counted here and in `tools.out_of_room`. |
| `tools.recall.failed` | Calls that returned an error to the model. The text shows them: `2 failed (1 recall, 1 pin)`. |
| `tools.recall.charged_tokens` | What the tool's replies were charged against the turn's recall room. It is a budget charge, not a count of tokens read: the reply's characters divided by the session's measured characters per token, capped at 1.5, so that a reply can never cost more than it was charged. A page stored with an estimate of 2,285 tokens was charged 5,322 when recalled whole. Do not compare it with `pages.tokens_estimate`, which counts text at 3.5 bytes a token. Always 0 for `pin` and `close`. |
| `tools.out_of_room` | Successful calls that found the turn's recall room used up, so they returned little or nothing. The text says how many of the calls these were. |
| `pins.readable` | `false` when the ledger is damaged; the two counts are then `null`. |
| `pins.active_records` | Records in the ledger that nothing supersedes, now. |
| `pins.open_obligations` | Obligations not closed, now. |

### `cache`

Measured, from `usage`, over the model requests whose usage said how much was read from cache:
`cache_read_input_tokens` for Claude, `input_tokens_details.cached_tokens` for Codex. Anthropic's
and OpenAI's own APIs always send the field. An Anthropic-compatible gateway may leave it out; the
proxy then logs `null`, which is not a measured 0, and that request is left out of the share.

| Key | Meaning |
|---|---|
| `cache.requests` | Model requests whose usage reported a cache-read field. The share covers these. |
| `cache.unreported_requests` | Model requests with usage that reported none. With `cache.requests` it adds up to `requests.with_usage`. |
| `cache.input_tokens` | The sum of the prompt tokens of the requests in `cache.requests`, counted as for `prompt.peak_tokens`. |
| `cache.cache_read_tokens` | The part read from the prompt cache. |
| `cache.cache_write_tokens` | The part written to the cache (`cache_creation_input_tokens`; Codex reports none). |
| `cache.read_share` | `cache_read_tokens / input_tokens`, or `null` when no request reported a cache-read field; the text then has no `cache` line. This is a share of input tokens. The README's acceptance tables count something else: requests that reused the cache. |

When only some requests reported the field, the text says how many the share covers:
`70% of input tokens read from cache (measured, over the 1 of 3 requests with usage that reported a
cache field)`.

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

| Key | Meaning |
|---|---|
| `compaction.seen` | The times the engine saw Claude Code's main conversation replaced by its compacted one (`main_changed` with reason `compacted`, D50 and D59). |
| `compaction.detectable` | Whether the log could have shown a compaction, for every run in scope. `"seen": 0` means "Claude Code did not compact" only when this is `true`. |
| `compaction.reasons` | Why not, when `detectable` is `false`: any of `no_session_id`, `cli_version_unknown`, `cli_version_not_measured`, `not_read` (below). Empty when it is `true`. |
| `compaction.cli_versions` | The Claude Code versions the engine read from the requests' `User-Agent`, in the order seen. |

The engine recognises a compacted conversation by the sentence Claude Code opens its summary with,
on a request that carries the main conversation's own Claude Code session id and the `claude-cli`
User-Agent's entrypoint. So the log can show a compaction only when:

- every Messages request the engine handled carried that session id and entrypoint (the engine
  records it per request as `cli_session`); otherwise `no_session_id`. A log written before the
  engine recorded it gives this reason too.
- each run's requests named Claude Code's version (a `cli_version` event, which the engine logs
  once per run from the `claude-cli/<version>` User-Agent); otherwise `cli_version_unknown`. Any
  other client that posts to `/v1/messages` gets this.
- every such version is one the summary's wording was measured on, 2.1.314 and 2.1.318; otherwise
  `cli_version_not_measured`.
- the engine read every Messages request: a request it did not read (no engine was running, as with a
  request that reached Contexto's proxy before any engine started, or its body was forwarded unread
  after a skipped or failed transform) was never checked; otherwise `not_read`.

The text makes the claim only then:

```
compaction  none seen: Claude Code did not compact the main conversation
compaction  none seen; whether Claude Code compacted cannot be told from this log (the log does not name Claude Code's version)
compaction  Claude Code compacted the main conversation at least 1 time; others cannot be told from this log (Contexto's check was not measured on Claude Code 3.0.0)
```

Limits that remain when `detectable` is `true`:

- A subagent that compacts is not counted.
- A compaction before the first request of a run is not counted: resuming a conversation that was
  compacted earlier, with or without Contexto, shows nothing.
- A first prompt that reproduces the sentence and the blank line after it is read as a compaction.
- The log does not record Contexto's own version. A run logged by a release from before the
  engine recorded `cli_session` is never `detectable`.

For a session with no Messages API request (Codex), `compaction` is `null` and the text has no
line: no event in the log shows a Codex compaction, so nothing is claimed.

### `faults`

| Key | Meaning |
|---|---|
| `faults.total` | Fault events in scope (the list is below). |
| `faults.events` | The count of each. |
| `faults.degraded` | Degraded mode, in which paging is stopped (below). |
| `faults.degraded.entered`, `faults.degraded.left` | Times paging stopped and resumed. |
| `faults.degraded.now` | Whether paging is stopped at the end of the scope, for the main conversation (for any conversation when the scope has no main request). The `degraded_enter` and `degraded_leave` events decide it; a request's own `degraded` flag is used only for a line that holds neither. A store that fails after a request was built, and again at each later turn, is stopped at the end even though every request went out paged. A new run always starts paging. |
| `faults.degraded.since` | When it stopped, if it is stopped now. |
| `faults.degraded.unpaged_requests` | Model requests sent while paging was stopped. |
| `faults.degraded.largest_unpaged_request_bytes` | The largest body among them. |
| `faults.store_errors` | One entry per distinct store failure: `event` (`store_error` or `page_conflict`), `operation` (the store call), `error` (the exception class), `errno` (its name, such as `ENOSPC`, or `null`), `cause` (the table below), `count`, and `first` and `last` (the timestamps of the first and latest one). |
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
   A failure that is over is told as past (`the disk ... was full ..., so paging stopped ... Paging
   has resumed; meanwhile 2 requests went out unpaged, the largest 244.5 KiB.`). The warning on
   stderr is written when the failure happens, so it is in the present; the summary's `faults` line
   says whether paging resumed, and under `--quiet` one line says so (above).
3. **In `contexto state`**, for the latest run only: `Warning:` when paging is stopped now, `Note:`
   when it stopped and resumed. `contexto state --raw` is the model's text and is unchanged.

`contexto stats` also says what each fault event means, under the `faults` line:

```
  faults      3 events (retained_stale 2, transform_error 1)
    retained_stale x2: the snapshot of the conversation in view could not be written
    transform_error x1: a Contexto bug: a request was forwarded as the CLI sent it
```

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
product left its designed path") with one event taken out and the events for damage, bugs, refused
requests and lost stored data that are logged outside the engine added. Every event name the source
writes is in the table below or in the list after it; a test fails when the source gains a name
that is in neither.

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
| `encoded_body_rejected` | The proxy answered a request 415 because it could not decode its body. The request was never forwarded and has no request line, so `requests.rejected_encoding` is the only number it is in. |
| `request_refused` | The proxy answered a request 403 because it did not come from the local CLI: it carried an `Origin`, or a `Host` that is not `127.0.0.1` or `localhost`. A browser page reached the proxy's port, or a forward rewrites the `Host` header. |
| `ledger_repaired` | The ledger of pins ended in a damaged record (a write cut short). The record was moved to the session's quarantine folder and the rest of the ledger is used. |
| `pruned_while_opening` | `contexto sessions prune` deleted the session while a launch was opening it, so it started empty. The store warns on stderr when it happens. |
| `search_cache_invalid` | `CONTEXTO_SEARCH_CACHE_MB` was not a valid value, so the default was used. |
| `watchdog_unavailable` | The helper that removes a launch's temporary files if the launcher is killed could not start. |
| `affinity_salt_unavailable` | The key for the cache-affinity header could not be read, so requests went out without the header and a gateway may route them to different caches. |
| `tool_calls_hook_error` | Under `contexto serve`, the tool calls in a response could not be registered, so a Contexto tool call from that response may not find its session. |
| `project_unavailable` | Project memory could not read the project's other sessions when a session started, or for a pin, a close or the text of the pins (at a rotation or for `contexto_state`), so that step went without their records: nothing was carried, and their closures and replacements were not applied ([project memory](project-memory.md)). A failed read at a turn start is `start_text_unavailable` alone, with `project_session_skipped` once per session when the cause is another session that could not be read. |
| `project_session_skipped` | Another session of the project could not be read (damaged, too large or unreadable), so its records, closures and replacements were not counted. |
| `start_text_unavailable` | The records carried from earlier sessions could not be read for a request; the text already sent was kept. |
| `main_records_unavailable` | A pin the main conversation made could not be recorded as the main conversation's, so a later session may not carry it. |
| `trusted_conversations_unavailable` | The list of main conversations the proxy trusted could not be written, so a later launch may not trust this one. |

A launch opens its session before it starts the proxy, so `ledger_repaired`, `pruned_while_opening`
and `affinity_salt_unavailable` are logged just before a run's proxy start. They are counted in
that run.

The launcher already warns on stderr, in its own words, about `store_error`, `page_conflict` (and so
`degraded_enter`, and `page_quarantined`: the page-conflict warning says the file will be moved to
quarantine), `mcp_tools_missing`, `mcp_tools_denied` and `bound_too_small`, and the store about
`pruned_while_opening`. For a run that logged any other fault, `contexto run` ends with one more
warning, which `--quiet` does not drop:

```
contexto: warning: this run logged 2 events that mean Contexto left its designed path (retained_stale 1, transform_error 1). Details: contexto stats 7f3a...
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
- `hint_mismatch`: a signal for whoever updates Contexto for a new Claude Code (D70,
  docs/cli-updates.md): a hint header and the signature beside it disagree. Logged once per engine,
  and the request is handled as before, so it is not a fault of the session.
- `cli_version_untested`: a condition, not a failure. The launcher warns about an untested CLI
  version when the session starts.
- `window_budget_warning`: the same kind: the bound leaves little room, and the launcher says so
  when the session starts.
- `carried_text_growing`: an early notice that standing messages are being added at each turn. The
  fault is `carried_over_budget`, logged when they pass their budget.
- `sizing_not_restored`: the bytes-per-token ratio of the last run was not reused, usually because
  the first request of this run was on another model. The ratio is measured again.
- `start_text`, `start_text_changed`, `project_memory`: project memory's ordinary records: how many
  tokens a new session carried, a turn start that sent fewer carried records after one was closed or
  forgotten, and the counts at launch ([project memory](project-memory.md)). When the counts could not
  be read at launch, `project_memory` holds `error` with the error's class in their place; a project
  that still cannot be read at the first request is also `project_unavailable`.
- `main_record_unmatched`: a signal for whoever updates Contexto for a new CLI: Contexto's own pin
  or close call had a reply of a shape it does not read, so that record is not carried. It is not a
  fault of the session.
- `in_turn_batch`, `in_turn_exhausted`: in-turn paging's records (D63,
  [in-turn paging](in-turn-paging.md)). A batch is paging working as designed. `in_turn_exhausted`
  comes with an `over_bound` for the same request, which the bound line already counts, so it is
  not counted twice.
- `proxy_stop_incomplete`: the proxy stopped (Ctrl+C) with requests still in a handler. It is not
  a fault, but those requests may have no request line: `requests.in_flight_at_stop` counts them
  and the `requests` line says so.

Two things on a request line are counted under `requests` and are not fault events:
`transform_skipped` (`requests.not_transformed`; the one case that is a fault, a body nested too
deep, also logs `request_too_deep`) and `error: relay: ...` (`requests.cut_short`).

The log's ordinary records are not faults either, and most have a number of their own: `request`,
`launch`, `exit`, `proxy_start`, `proxy_stop`, `mcp_call`, `count_tokens`, `rotation`, `page_out`,
`carried_paged`, `carried_reclassified`, `over_bound`, `main_changed`, `degraded_leave`,
`epoch_resumed`, `sizing_restored`, `side_room_closed`, `mcp_tools_found`,
`cli_version`, `cli_version_check`, and `serve_open` and `serve_close`, which `contexto serve` writes
where a launch writes `proxy_start` and `proxy_stop`.

`contexto serve` writes its other events to `serve.log` in `$CONTEXTO_HOME`, not to a session's log,
so `contexto stats` never reads them: `serve_start`, `serve_stop`, `serve_error`, `session_open`,
`session_open_failed`, `session_close`, `session_discarded`, `reap_error`, `stopping_refused`,
`token_header_dropped`, `tool_use_ambiguous` and `unrouted` ([docs/serve.md](serve.md)).

## What the numbers cannot show

- Lines the log lost (a full disk) are not counted.
- Two launches that share one session (`--session ID` given to both) write one log; their runs
  interleave and `--last-run` then mixes them.
- A launcher killed with SIGKILL prints no summary. `contexto stats` still works.
- Turn counts: the log counts requests, and the store counts pages. Neither counts the turns you
  typed, and no number here is a count of turns.
- How many pages a run added, for a run logged before the proxy recorded the store's page count
  (`paging.pages_added` is `null`).
- Requests refused with 415 or still in flight when the proxy stopped have no request line. They
  are counted (`requests.rejected_encoding`, `requests.in_flight_at_stop`) and are in no other
  number.

## Speed

`session_stats` reads the whole log once, in blocks of 4 MiB, and parses each block on its own. A
log smaller than a block, which a 6-hour session's is, is parsed with one `json.loads` call (about
twice as fast as one call per line). A damaged line fails that call, and only its block is then
read line by line. Memory does not grow with the log: a call holds a few blocks' worth, where
reading the log whole held more than six times its size.

Measured on a development laptop running several other test suites at the time (load average 37
to 60), so wall-clock times vary and the CPU time is the steadier figure. The log only
(`store=False`); "before" is the reader that held the whole log.

| Log | | Before | Now |
|---|---|---|---|
| 4,000 model requests, 200 rotations, 445 tool calls (4,449 lines, 2.9 MB) | CPU, best of 25 | 37 to 40 ms | 38 to 41 ms |
| | peak resident memory, one call | 51 MB | 46 to 51 MB |
| the same with one line cut short (4,907 lines, 3.2 MB) | CPU, best of 25 | 56 to 58 ms | 56 to 59 ms |
| | peak resident memory, one call | 62 MB | 56 to 64 MB |
| 276,000 model requests (306,981 lines, 203 MB) | CPU, one call | 4.4 s | 3.0 s |
| | wall clock, one call | 17.3 s | 4.5 to 10.1 s |
| | peak resident memory | 1,282 MB | 68 to 80 MB |
| the same with one line cut short | CPU, one call | 6.8 s | 3.0 s |
| | wall clock, one call | 13.6 s | 5.1 to 9.0 s |
| | peak resident memory | 1,551 MB | 73 to 88 MB |

The interpreter alone is 27 MB of each memory figure. For the whole session the time still grows
with the log's size: every call parses all of it.

**The last run** (`--last-run`, the exit summary, `contexto state`) parses only that run's lines.
The lines before it are read once and searched for run starts, which costs about 1.4 ms of CPU for
each megabyte. On the 203 MB log above, cut into 69 runs of 4,000 requests each:

| | Before | Now |
|---|---|---|
| CPU, one call | 4.1 s | 0.34 s |
| peak resident memory | 1,524 MB | 80 MB |

The store adds to that. For the small log with a ledger and 1,200 pages a call took 122 to 133 ms
of CPU, and 51 to 52 ms with `page_tokens=False`, which counts the pages without opening them.

A page file is read at its first 64 and last 96 bytes only, where its id and its token estimate
are; a page whose ends are not a canonical page's is parsed in full. Counting pages opens no file.

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
`contexto.stats.count_pages(pages_dir)` is that count on its own.
`contexto.stats.session_faults(home, session_id)` returns the latest run's `faults` object, which is
what `contexto state` prints from.
