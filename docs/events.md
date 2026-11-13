# Session log events

Every Contexto session has a log, `log.jsonl`. This is a reference for every event that can appear in
it: who writes it, where in the log it appears, what it means, its fields, and whether it is normal
operation, a notice, or a sign that something went wrong.

`tests/test_events_doc.py` reads the package source and fails when it can write an event name or a
field this document does not list, or when the document lists one the source cannot write. The test
checks names and fields. It does not check the prose.

## The log

**Where it is.** `$CONTEXTO_HOME/sessions/<session id>/log.jsonl`, where `CONTEXTO_HOME` defaults to
`~/.contexto`. The file has mode 0600. `contexto run` prints the session id when it starts, and
`contexto sessions list` shows the sessions.

**Format.** One JSON object per line, appended, never rewritten. Keys are sorted, with no whitespace
between them. Every line has an `event` name and a `ts`.

**Who writes.** The proxy (with the paging engine inside it), the `contexto run` launcher, the MCP
server, and the session store. The MCP server is a separate process that the CLI starts, so its lines
interleave with the proxy's in the order they were written.

**`ts`.** The time the line was written, in UTC. The proxy's lines carry microseconds
(`2026-11-12T09:41:17.482915Z`); every other writer's lines carry whole seconds
(`2026-11-12T09:41:19Z`). A `request` line is written when its response has ended, so the request
started `duration_ms` before its `ts`.

**Lines and nested events.** The engine's events are not lines of their own. The engine returns them
to the proxy, which puts them on the `request` line of the request they belong to:

- `transform_events`: what the engine did before the request was sent, in order. The engine's own
  [`request` event](#request-the-engines-nested-event) is the last one.
- `usage_events`: what the engine found when the response's real usage arrived.

Nested events have no `ts`. Each entry below says where its event appears: `own line`,
`transform_events` or `usage_events`. To find every `rotation` in a log, look inside the
`transform_events` of its `request` lines; a search for lines whose `event` is `rotation` finds
nothing. `contexto sessions show --json` counts lines only (`events_by_type`), so nested events are
not in its counts.

**Credentials.** The value of any key named `authorization`, `x-api-key`, `cookie`, `set-cookie` or
`proxy-authorization` (in any letter case, at any depth) is replaced by `[redacted]` before the line
is written. Redaction goes by key name only. No event has such a key: request headers and bodies are
never put in an event.

**No conversation content.** Events are built from counts, sizes, ids, fixed words and exception class
names. They hold no prompt, response, tool argument, tool output or file content. The proxy adds a
second guard to nested engine events, to a request's `model` and to `input_transformations`: a string
longer than 80 characters is replaced by its length (`<123 chars>`), a list is cut to its first 50
items, and anything nested deeper than 16 levels becomes `<dict>` or `<list>`. The guard does not
apply to the other fields of the proxy's own lines, or to the launcher's, the MCP server's and the
store's lines.

The log is not anonymous. It holds the session id, page ids and pinned-record ids, the model name,
request paths, the CLI version, and in two places text from the machine:
[`page_unreadable`](#page_unreadable) carries an exception's message, which for a failed read names
the page file with its full path, and [`search_cache_invalid`](#search_cache_invalid) carries the
first 100 characters of an environment variable. Read a log before you share it.

**A line cut short.** A write that a full disk stops partway leaves a line without its end. The next
event starts on a new line, so the cut line stays as one line that is not valid JSON. Skip lines that
do not parse, as `contexto sessions show` does.

**A missing `exit`.** A launcher that is killed (SIGKILL) writes no [`exit`](#exit) line.

### Examples

These lines are built from the code with made-up values, not taken from a session. They are wrapped
here; in the file each is one line.

The launcher starts a session:

```json
{"affinity_header":false,"cli":"claude","config":{"bound":150000},"engine":"rewrite",
 "event":"launch","observe_thinking":false,"session":"3f6c0f0e5d9a4a0e9a1f2b7c8d4e5f60",
 "ts":"2026-11-12T09:30:00Z","upstream_source":"default","user_arg_count":0}
```

A request at a turn start that rotated. The engine paged 7 turns out (`page_out`), started epoch 2
(`rotation`), and sent 24 of the CLI's 61 messages (its `request` event). Most `request` lines carry
only that last nested event:

```json
{"api":"messages","duration_ms":8412,"epoch":2,"event":"request","message_count":24,
 "method":"POST","model":"claude-sonnet-5-5","original_message_count":61,
 "original_request_bytes":328850,"over_bound":false,"path":"/v1/messages","request_bytes":199940,
 "response_bytes":18306,"status":200,"stop_reason":"tool_use","stream":true,
 "stream_hold":{"held_events":4,"triggered":true},
 "transform_events":[
  {"epoch":2,"event":"page_out","first_turn":9,"pages":7,"turns":7},
  {"epoch":2,"event":"rotation","fixed_overhead":31250,"k":38,"reason":"bound",
   "retained_messages":23,"turns_paged":16,"window_budget":93750},
  {"conversations":1,"epoch":2,"estimate":126480,"event":"request","k":38,"main":true,
   "messages":61,"sent_bytes":199940,"sent_estimate":76900,"upstream_messages":24}],
 "ts":"2026-11-12T09:41:17.482915Z",
 "usage":{"cache_creation_input_tokens":47215,"cache_read_input_tokens":29870,"input_tokens":3,
  "output_tokens":1204}}
```

The model reads a paged turn back:

```json
{"charged_tokens":2800,"chars":4200,"event":"mcp_call","limited_by":"main","offset":0,"ok":true,
 "page_id":"pg_0f3a9c5d7e1b2a46",
 "returned":[{"end":4200,"page_id":"pg_0f3a9c5d7e1b2a46","source":"paged","start":0}],
 "room_left":44288,"side_rooms":0,"tool":"contexto_recall","total_chars":4200,
 "ts":"2026-11-12T09:41:19Z","turn_budget":"charged"}
```

### Words used below

- **Bound**: the token limit the session is kept under (`--bound`, 150,000 by default).
- **Turn**: a user prompt and everything up to the next one. A **turn start** is a request whose
  newest turn has no assistant message yet. The engine pages only at turn starts.
- **Epoch**: the stretch between two rotations. Within an epoch the prompt prefix does not change,
  so the prompt cache can be read. `epoch` counts from 0.
- **`k`**: the index, in the CLI's message list, of the first message still sent. Messages before `k`
  are paged out. `k` is 0 in the first epoch.
- **Fixed overhead** (`F`): system prompt, tools, carried content and the preamble (pinned state and
  page index), in tokens. **Window budget** (`W`): `bound - headroom - F`, what is left for turns.
- **Main conversation**: the one the user is in. Subagents and the CLI's side requests are other
  conversations in the same session.
- **Room**: the tokens `contexto_recall`, `contexto_search` and `contexto_state` may return in the
  current turn. A **side room** is a subagent's own.
- **Degraded mode**: after a store failure the engine stops paging and sends the last good transform,
  extended with the new messages, until the store works again.

The design contract, [milestones/M2-contexto-proxy.md](milestones/M2-contexto-proxy.md), defines
these and the rules the entries cite.

### Reading an entry

Each entry has one line of facts, then the meaning, then one bullet per field. `event` and `ts` are
not repeated in the field lists. In the facts line:

- **Writer** is `proxy`, `engine`, `launcher (Claude)`, `launcher (Codex)`, `MCP server` or `store`.
- **Where** is `own line`, `transform_events` or `usage_events`.
- **Kind** is `normal` (expected in a healthy session), `notice` (worth knowing, the session carries
  on as designed) or `problem` (Contexto left its designed path, or cannot do part of its job).

## The request line

### `request`

Writer: proxy. Where: own line. Kind: normal.

One line for every HTTP request the proxy handled, written when the response has ended. It is the
line to read for sizes, token usage and timing. A request the proxy only passes through (for example
Claude Code's `HEAD /api/hello`) usually has the first group of fields and no others. The line itself
is normal. The fields `error`, `transform_error`, `transform_skipped`, `client_aborted` and a true
`over_bound` mark a request that did not go as designed.

Every request:

- `method`: the HTTP method.
- `path`: the request path, without the query string.
- `original_request_bytes`: the size of the body the CLI sent, in bytes. For a compressed body, its
  size after decoding.
- `request_bytes`: the size of the body sent upstream, in bytes. The difference from
  `original_request_bytes` is the net effect of paging on this request: paged turns taken out, the
  pinned state and page index put in. The two are equal when the request was not changed.
- `status`: the HTTP status the CLI got. It is upstream's, except 502 (the proxy could not reach
  upstream; see `error`) and 400 (malformed request framing). Absent when the CLI left before a
  response started.
- `response_bytes`: the size of the response body read from upstream, in bytes.
- `duration_ms`: milliseconds from the start of handling the request to the end of the response.
- `usage`: the token usage upstream reported, or null when none was read. On the Messages API (Claude
  Code) it has `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` and
  `output_tokens`; the request's real input is the sum of the first three. On the Responses API
  (Codex) it is the usage object as upstream gave it, numbers only: there `input_tokens` already
  includes the cached tokens, which `input_tokens_details.cached_tokens` counts.

Requests the engine transforms (`POST` to `/v1/messages`, `/v1/messages/count_tokens`, `/responses`
or `/v1/responses`):

- `api`: `messages` or `responses`.
- `model`: the model the request names.
- `stream`: whether the request asked for a streamed response.
- `original_message_count`: how many `messages` the CLI's request had. A Responses API request keeps
  its history in `input`, so this and `message_count` are 0 for Codex; the nested `request` event's
  `messages` and `upstream_messages` count both APIs.
- `message_count`: how many `messages` the request sent upstream had.
- `transform_events`: the engine's events for this request, in order (see
  [Engine events](#engine-events-nested-in-request-lines)).
- `usage_events`: the engine's events from the response's real usage. Absent when there are none.
- `over_bound`: true when the request was over the bound, by the estimate made before sending or by
  the real usage. See [`over_bound`](#over_bound) for which, and whether it was the allowed case.
- `epoch`: the epoch the request was sent in.
- `stream_hold`: present when the response was a stream the proxy could hold. `held_events` is how
  many stream events were held back, from the first tool call until the usage was read, so the room
  is published before the CLI starts the call. `triggered` is whether the stream reached its usage.
- `stop_reason`: Messages API: why the response ended (`end_turn`, `tool_use`, `max_tokens` and the
  API's other values).
- `error_type`: Messages API: the type of an error the response carried.
- `end_event`: Responses API streams: the event the stream ended on: `completed`, `incomplete`,
  `failed`, or `none` when it ended without one.
- `response_status`: Responses API: the response's own `status`.
- `incomplete_reason`: Responses API: why the response is incomplete.
- `error_code`: Responses API: the code of an error the response carried.

The six end fields hold only values on a fixed list; any other value is logged as `other`.

Only when something unusual happened:

- `request_content_encoding`: the request body arrived compressed with this encoding. The proxy
  decoded it and sent it on uncompressed.
- `wire_request_bytes`: the compressed body's size as received.
- `transform_skipped`: the request was forwarded without a transform, and why: its body is not JSON,
  is not an object, nests deeper than 512 levels (then with a
  [`request_too_deep`](#request_too_deep) line), or cannot be serialized.
- `transform_error`: the engine raised, or produced a request that cannot be serialized
  (`serialize: <class>`). The CLI's own request was forwarded. A
  [`transform_error`](#transform_error) line is written too.
- `fallback`: true on the line of a request that was forwarded unchanged after a
  [`proxy_error`](#proxy_error).
- `error`: `upstream unreachable: <class>` with status 502; `relay: <class>` when upstream ended the
  response early (the status and any usage already read are still on the line);
  `malformed request framing` with status 400, on a line that has only `method`, `path`, `status`
  and `error`.
- `upstream_phase`: on a 502, or a client that left before the response started: the step that
  failed or was in progress: `connect`, `tls`, `send` or `wait_headers`.
- `phase_ms`: on a 502: milliseconds spent in that step.
- `errno`: on a 502: the operating system's error number for the failure, or null.
- `upstream_retried`: true when the request was sent a second time. That happens once, and only when
  the first attempt failed before the body was fully written (see [`upstream_retry`](#upstream_retry)).
- `upstream_answered_early`: upstream answered before it read the whole body (a body limit's 413,
  for example). The value is the class of the send failure; the answer was forwarded unchanged.
- `client_aborted`: true when the CLI closed its connection before it had the whole response (Esc).
  The proxy then closes the upstream connection.
- `usage_unavailable`: the response was encoded in a way the proxy does not read, so no usage.
- `input_transformations`: with `--observe-thinking`: what the API reported it changed in the input
  (dropped thinking blocks), at most 32 items.
- `input_transformations_truncated`: true when more than 32 were reported.

## Proxy events

### `proxy_start`

Writer: proxy. Where: own line. Kind: normal.

The proxy is listening.

- `port`: the port on 127.0.0.1.
- `upstream_source`: where the upstream URL came from, never the URL: `flag`, `settings:<file kind>`,
  `env` or `default` for Claude Code; `codex-flag`, `codex-env`, `chatgpt-default` or
  `claude-<source>+/v1` for Codex.
- `observe_thinking`: whether `--observe-thinking` is on.

### `proxy_stop`

Writer: proxy. Where: own line. Kind: normal.

The proxy stopped accepting requests and finished waiting for those in flight. No fields.

### `proxy_stop_incomplete`

Writer: proxy. Where: own line. Kind: notice.

The proxy stopped while requests were still in flight: they did not finish within 2 seconds, or a
second Ctrl+C ended the wait. Their `request` lines may be missing.

- `in_flight`: how many requests were still running.

### `request_refused`

Writer: proxy. Where: own line. Kind: notice.

A request that did not come from the local CLI was answered 403 and not forwarded: a browser page or
a DNS-rebound name tried to reach the proxy. The session is unaffected.

- `method`: the HTTP method.
- `path`: the request path.
- `reason`: `origin` (the request had an `Origin` header) or `host` (its `Host` was not the proxy's
  own address).

### `websocket_rejected`

Writer: proxy. Where: own line. Kind: problem.

A client asked for a WebSocket, which would bypass the bound. The proxy answered 501. The launcher
keeps Codex on HTTP, so this means a client was not started through `contexto run`.

- `path`: the request path.

### `encoded_body_rejected`

Writer: proxy. Where: own line. Kind: problem.

A request body arrived with a `Content-Encoding` the proxy cannot decode, or failed to decode. It
was answered 415 and not forwarded, since an unread request cannot be kept under the bound. The
launcher turns request compression off, so this means a misconfigured client.

- `encoding`: the `Content-Encoding` value.
- `endpoint`: the request path.
- `reason`: why it could not be read.

### `request_too_deep`

Writer: proxy. Where: own line. Kind: problem.

A request body nests objects and arrays deeper than 512 levels. It was forwarded without a transform
(its `request` line has `transform_skipped`), so it was not bounded.

- `level`: `warning`.
- `endpoint`: the request path.
- `limit`: 512.
- `epoch`: the epoch of the last transformed request, or null.

### `transform_error`

Writer: proxy. Where: own line. Kind: problem.

The engine raised an unexpected exception, or returned a request that cannot be serialized. The
proxy forwarded the CLI's own request, so the user is not blocked, but that request was not bounded.
This is a Contexto defect worth reporting.

- `level`: `error`.
- `error`: the exception's class name.
- `stage`: `transform` or `serialize`.
- `endpoint`: the request path.
- `epoch`: the epoch of the last transformed request, or null.

### `proxy_error`

Writer: proxy. Where: own line. Kind: problem.

The proxy itself failed while handling a request. This is a Contexto defect worth reporting.

- `method`: the HTTP method. Absent on the second `proxy_error` of one request.
- `path`: the request path.
- `error`: the exception's class name.
- `fallback`: what the client got. `forward_original`: the CLI's own request was sent upstream
  unchanged (its `request` line has `fallback`). `local_502`: a 502 from the proxy. `none`: the
  response had already started, so the connection was closed.

### `upstream_retry`

Writer: proxy. Where: own line. Kind: notice.

An attempt to reach upstream failed before the request body was fully written, so upstream cannot
have acted on it, and the proxy is sending the same bytes once more on a new connection. The
`request` line that follows has `upstream_retried`.

- `path`: the request path.
- `error`: the exception's class name.
- `elapsed_ms`: milliseconds since the proxy started handling the request.
- `upstream_phase`: the step that failed: `connect`, `tls` or `send`.
- `phase_ms`: milliseconds spent in that step.
- `errno`: the operating system's error number, or null.

### `upstream_4xx`

Writer: proxy. Where: own line. Kind: notice.

Upstream answered with a 4xx status, which the proxy forwarded unchanged. Written right after that
request's `request` line, with the engine's state for diagnosis. Claude Code's `HEAD /api/hello`
probe at launch gets one from some upstreams, which is harmless. A 4xx on a model path
(`/v1/messages`, `/responses`) is worth reading.

- `status`: the HTTP status.
- `path`: the request path.
- `model`: the model the request named, or null.
- `epoch`: the request's epoch, or null.
- `transform_events`: the same list as on the `request` line, or null.

### `hold_skipped`

Writer: proxy. Where: own line. Kind: notice.

A streamed response could not be held: upstream encoded it or did not send it chunked. Tool calls in
that response reach the CLI before the room is published from real usage, so a recall in that moment
is budgeted from the estimate made before sending.

- `path`: the request path.
- `encoding`: the response's `Content-Encoding`, or `identity`.
- `chunked`: whether the response was chunked.

### `observe_usage_error`

Writer: proxy. Where: own line. Kind: problem.

The engine raised while reading a response's usage. The response was relayed, but its usage did not
reach the engine. This is a Contexto defect worth reporting.

- `error`: the exception's class name.

## Engine events (nested in request lines)

The paging engine does not write to the log itself. It returns these events to the proxy, which puts
them inside the `request` line, in `transform_events` or `usage_events`. The one exception,
[`mcp_tools_missing`](#mcp_tools_missing) at exit, is described in its entry.

### `request` (the engine's nested event)

Writer: engine. Where: `transform_events`. Kind: normal.

The last item of `transform_events` on every request sent to a model endpoint: what the engine sent.

- `epoch`: the epoch the request was sent in.
- `k`: the first message still sent; messages before it are paged out.
- `messages`: how many messages the CLI's request had.
- `upstream_messages`: how many messages were sent upstream.
- `estimate`: the estimated input, in tokens, of the request as it arrived. The rotation decision
  uses it. Absent with `fallback_failed`.
- `sent_estimate`: only when this request rotated: the estimated input of what was sent after the
  rotation.
- `sent_bytes`: the size of the request sent upstream, in bytes of canonical JSON.
- `over_bound`: true when the estimate of what was sent exceeds the bound. An
  [`over_bound`](#over_bound) event precedes it.
- `degraded`: true when the request was sent in degraded mode.
- `fallback_failed`: true when the CLI's request was sent unchanged (see
  [`fallback_failed`](#fallback_failed)).
- `main`: whether this request belongs to the main conversation.
- `conversations`: how many conversations the engine is tracking in this session.

### `count_tokens`

Writer: engine. Where: `transform_events`. Kind: normal.

The only item on a `/v1/messages/count_tokens` request. The engine applies the current epoch's
transform to it, so the CLI's own counts match what is sent. It never rotates and has no side effects.

- `epoch`: the epoch whose transform was applied.

### `rotation`

Writer: engine. Where: `transform_events`. Kind: normal.

A new epoch started at this request: older turns went to the store and the retained window was
rebuilt. The first request after a rotation rewrites the cache from the window on.

- `epoch`: the new epoch's number.
- `reason`: `bound` (the estimate passed `bound - headroom`), `min_turn_room` (the recall room for
  the new turn would have been too small), `window_minimal` (see
  [`window_minimal`](#window_minimal)), `head_change` (the system prompt or tools changed), or
  `paged_changed` (see [`resync`](#resync)).
- `k`: the first message still sent in the new epoch.
- `retained_messages`: how many of the CLI's messages are still sent.
- `turns_paged`: how many turns have been paged out so far, this rotation's included. Carried items
  that earlier rotations paged ([`carried_paged`](#carried_paged)) are counted too.
- `fixed_overhead`: `F` in tokens, as used for this decision. Absent with reason `paged_changed`.
- `window_budget`: `W` in tokens. Absent with reason `paged_changed`.

### `page_out`

Writer: engine. Where: `transform_events`. Kind: normal.

Turns were written to the store, one page per turn. It comes just before the `rotation` it belongs
to. A rotation that pages nothing (a head change with nothing to page) has no `page_out`.

- `epoch`: the epoch that is starting.
- `turns`: how many turns were paged.
- `pages`: how many pages were written.
- `first_turn`: the turn number the first of these pages gets.

### `rotation_noop`

Writer: engine. Where: `transform_events`. Kind: normal.

A turn start met the rotation trigger, but no more turns could be paged within the window target, so
nothing changed and the epoch continues.

- `epoch`: the current epoch.
- `fixed_overhead`: `F` in tokens.
- `window_budget`: `W` in tokens.
- `k`: the first message still sent, unchanged.

### `window_minimal`

Writer: engine. Where: `transform_events`. Kind: notice.

The window budget could not hold the newest complete turn together with the new one, so the rotation
kept only the new turn. The previous turn is paged and can be recalled, but the model no longer has
it verbatim. Frequent `window_minimal` means the bound is small for this session's fixed overhead.

- `epoch`: the new epoch.
- `window_budget`: `W` in tokens.
- `new_turn_tokens`: the new turn's estimated size.
- `previous_turn_tokens`: the previous turn's estimated size.
- `k`: the first message still sent: the new turn's start.
- `count`: how many times this has happened in the conversation.

### `bound_too_small`

Writer: engine. Where: `transform_events`. Kind: problem.

Even the new turn alone does not fit the window budget (or the budget is not positive). The engine
still rotates to the new turn alone, and the request may exceed the bound. The launcher prints a
warning once. A larger `--bound` is the fix, unless `pdf_uncertain` is set.

- `epoch`: the current epoch, after the rotation if there was one.
- `fixed_overhead`: `F` in tokens.
- `window_budget`: `W` in tokens.
- `bound`: the bound.
- `new_turn_tokens`: the new turn's estimated size.
- `pdf_uncertain`: present when the turn holds this many PDFs whose page count could not be read,
  each counted at the maximum of 600 pages, and the turn would fit without them. The PDF's real size,
  and so the bound it needs, is unknown.

### `pdf_allowance`

Writer: engine. Where: `transform_events`. Kind: notice.

Written in place of `bound_too_small` when the new turn does not fit only because its PDFs are
estimated generously (4,568 tokens a page). With typical pages the turn may fit as it is. The engine
rotates to the new turn alone. The launcher says so once.

- `epoch`: the current epoch, after the rotation if there was one.
- `fixed_overhead`: `F` in tokens.
- `window_budget`: `W` in tokens.
- `bound`: the bound.
- `new_turn_tokens`: the new turn's estimated size, PDFs included.
- `pdf_allowance_tokens`: the part of it that is the PDFs' allowance.

### `pdf_pages_uncertain`

Writer: engine. Where: `transform_events`. Kind: notice.

A request brought a PDF whose pages could not be counted with certainty (a URL or file id, a file
too large, or one the reader could not follow). It is counted at the API's maximum so it is never
estimated low, which can page earlier messages out sooner. The launcher says so once.

- `epoch`: the current epoch.
- `documents`: how many such PDFs the request added.
- `pages`: the page count assumed for each: 600.

### `turn_room_low`

Writer: engine. Where: `transform_events`. Kind: problem.

A turn started with less recall room than `min_turn_room`, and no rotation could restore it. Recall
and search may run out of room in this turn. Logged once per epoch. It means the bound is small for
this session: every epoch logging it is a reason to raise `--bound`.

- `epoch`: the current epoch.
- `room`: the room the turn starts with, in tokens.
- `best_room`: the room a rotation for the room would have given.
- `min_turn_room`: the configured minimum.

### `head_changed`

Writer: engine. Where: `transform_events`. Kind: normal.

The CLI's system prompt or tools changed (an MCP server connected, a model switch). The cached prefix
is lost either way, so the engine starts a new epoch at the next turn start; that `rotation` has
reason `head_change`.

- `epoch`: the current epoch.

### `usage_missing`

Writer: engine. Where: `transform_events`. Kind: notice.

The previous request in this conversation got no usage (an error status, a dropped stream, a
response without usage, or a request still in flight), so this request is sized from a full estimate
instead of the last real input.

- `epoch`: the current epoch.

### `carried_text_growing`

Writer: engine. Where: `transform_events`. Kind: notice.

The number of carried system messages grew at two turn starts in a row. A hook or mode is probably
adding a standing message every turn. Such messages are sent on every request until the fallback
([`carried_paged`](#carried_paged)) pages them.

- `epoch`: the current epoch.
- `messages`: how many carried text messages there are now.

### `carried_paged`

Writer: engine. Where: `transform_events`. Kind: notice.

Carried content had grown more than its budget (8,000 tokens by default) over its size at the
conversation's first request, so this rotation paged some of it: superseded items first, then
unrecognised ones, oldest first. Each is its own page and can be recalled.

- `epoch`: the new epoch.
- `count`: how many items were paged.
- `tokens`: the tokens freed.
- `context_blocks`: how many of them were context blocks of a user message (the rest are system
  messages).
- `unrecognised`: how many were of no kind the engine recognises. A CLI version that adds a kind
  shows up here.

### `carried_over_budget`

Writer: engine. Where: `transform_events`. Kind: problem.

After a rotation and its fallback, carried content is still more than the budget over its size at
the first request: the fallback could not bound it. Fixed overhead stays high and the window small.

- `epoch`: the new epoch.
- `carried_tokens`: the carried content's estimated size.
- `carried_growth`: its growth since the conversation's first request.
- `system_messages`: how many carried system messages there are.
- `budget`: the growth allowed.

### `unclassified_system_message`

Writer: engine. Where: `transform_events`. Kind: problem.

A `role: "system"` message with non-text content is among the paged turns. The engine has never
measured such a message, so it carries it and never pages it, which is the safe choice. It probably
means a CLI version sends something new.

- `epoch`: the new epoch.
- `count`: how many such messages.

### `resync`

Writer: engine. Where: `transform_events`. Kind: notice.

The CLI's history no longer matches what the engine saw before: the CLI edited or dropped earlier
messages (a local compaction, a rewind). The engine continues from the longest matching prefix.

- `epoch`: the current epoch.
- `kind`: `window_changed`: only retained messages changed, and the epoch continues.
  `paged_changed`: messages already paged changed, so a new epoch starts at the current turn with
  everything before it paged (a `rotation` with reason `paged_changed` follows).
- `matched`: how many leading messages still match.
- `previous_messages`: how many messages the previous request had.
- `messages`: how many this request has.
- `inflight_thinking_kept`: `paged_changed` only: whether the current turn holds thinking blocks,
  which are kept.

### `over_bound`

Writer: engine. Where: `transform_events`, `usage_events`. Kind: notice.

The request was over the bound. With `source: "estimate"` (in `transform_events`) the estimate of
what is sent exceeded the bound before sending. With `source: "real"` (in `usage_events`) the real
input upstream reported did. Either sets `over_bound` on the `request` line. Inside a tool loop
(`in_tool_loop: true`) this is the allowed case: the engine never rotates mid-turn, and the next turn
start rotates. At a turn start it comes with [`bound_too_small`](#bound_too_small), or means the
rotation's sizing did not hold. With `degraded` it is a consequence of degraded mode.

- `source`: `estimate` or `real`.
- `epoch`: the current epoch.
- `bound`: the bound.
- `estimate`: the estimate made before sending, in tokens. Null on a `real` event whose usage was
  not paired with a request (see [`usage_unattributed`](#usage_unattributed)).
- `in_tool_loop`: `estimate` only: whether the request is inside a tool loop.
- `degraded`: `estimate` only: true in degraded mode.
- `real_tokens`: `real` only: the real input: input, cache read and cache write tokens.
- `over_bound`: `real` only: true.

### `estimate_error`

Writer: engine. Where: `usage_events`. Kind: notice.

The real input was more than 10% over the estimate made before sending. The engine corrects its
bytes-per-token ratio from every response, so one such event is not a fault. Repeated ones mean the
engine keeps estimating low, which makes the bound less reliable.

- `epoch`: the current epoch.
- `real_tokens`: the real input.
- `estimate`: the estimate.
- `ratio`: estimate divided by real, for this request.
- `mean_ratio`: the mean of that ratio over the conversation.

### `usage_unattributed`

Writer: engine. Where: `usage_events`. Kind: notice.

A response's usage is not for the newest request the engine sent in its conversation: it answers an
older request still in flight, or one the engine did not transform. The usage is not used to learn
the ratio or to check the estimate. It still counts for `over_bound` and the room.

- `epoch`: the current epoch.
- `messages`: the message count of the request the usage is for.
- `expected_messages`: the message count of the request the engine was waiting on, or null.

### `store_error`

Writer: engine. Where: `transform_events`, `usage_events`. Kind: problem.

The session store could not be read or written. The engine enters degraded mode (or stays in it).

- `epoch`: the current epoch.
- `operation`: the store operation that failed, for example `page_out` or `set_turn_room`.
- `error`: the exception's class name.
- `degraded`: true when the engine was already in degraded mode.

### `page_conflict`

Writer: engine. Where: `transform_events`. Kind: problem.

A page already in the store differs from the turn being paged under the same id, or its file is not
a page record. Either means a corrupted page. The engine enters degraded mode; at the next turn
start it has the store move that page to quarantine and pages the turn again.

- `epoch`: the current epoch.
- `operation`: the store operation that failed.
- `error`: the exception's class name.
- `degraded`: true when the engine was already in degraded mode.
- `page_id`: the page in conflict.

### `degraded_enter`

Writer: engine. Where: `transform_events`, `usage_events`. Kind: problem.

The engine entered degraded mode. Until it leaves, it pages nothing and never rotates, so requests
can exceed the bound; each is marked `degraded` and, when over, `over_bound`. Nothing is lost,
because no turn is paged while the store is broken. The store is tried again at each turn start.

- `epoch`: the current epoch.
- `reason`: `store_error` or `page_conflict`: the event just before it.

### `degraded_leave`

Writer: engine. Where: `transform_events`. Kind: notice.

The store works again and the engine left degraded mode. Rotation resumes at this request.

- `epoch`: the current epoch.
- `quarantined`: present when the engine left by quarantining a corrupted page: its id.

### `fallback_failed`

Writer: engine. Where: `transform_events`. Kind: problem.

A failure inside degraded mode, or a resync that needed paging while the store was broken. The
engine sent the CLI's own request unchanged, so it was not bounded. The next request tries again.

- `epoch`: the current epoch.
- `reason`: `resync_paged_changed` or `degraded_transform:<class>`.
- `messages`: how many messages the request had.

### `sizing_restored`

Writer: engine. Where: `transform_events`. Kind: normal.

At the first request after a restart or a resume, the engine took over the bytes-per-token ratio the
previous run measured, instead of starting from its built-in guess. It is the first item of
`transform_events`.

- `bytes_per_token`: the ratio restored.
- `weight_tokens`: how much weight it gets against new measurements, in tokens.

### `sizing_not_restored`

Writer: engine. Where: `transform_events`. Kind: notice.

The recorded ratio was not used. The engine starts from its built-in guess, which errs high, until
real usage arrives.

- `reason`: `model_changed` (the request is for another model than the recorded one; logged once,
  and a later request on the recorded model still restores it) or `ratio_out_of_range` (the recorded
  value is not one a real session produces).
- `bytes_per_token`: the recorded ratio.

### `epoch_resumed`

Writer: engine. Where: `transform_events`. Kind: normal.

A new engine on an existing session (a proxy restart, `contexto run --session`) continued the main
conversation's recorded epoch, so the prompt stays as the previous process would have sent it and
the cache still holds.

- `epoch`: the epoch resumed.
- `k`: its first message still sent.

### `epoch_not_resumed`

Writer: engine. Where: `transform_events`. Kind: normal.

The engine started a conversation from epoch 0. On a new session's first request this is expected
(`reason: "no_record"`). After a restart, the reason says why the recorded epoch was not continued;
the cost is one rotation.

- `epoch`: 0.
- `reason`: `no_record`, `unreadable`, `other_conversation` (the record belongs to another
  conversation, as for a subagent's or a side request's first request), `dialect_changed`,
  `model_changed`, `settings_changed`, `preamble_changed`, `head_changed`, `history_shorter`,
  `history_diverged`, or `pages_missing` (the store holds fewer pages than the record needs).

### `carried_reclassified`

Writer: engine. Where: `transform_events`. Kind: notice.

After `epoch_resumed`: the record, written by an older version, had paged system messages that this
version always carries. They are carried again from this request on, at the cost of one cache write.

- `epoch`: the epoch resumed.
- `count`: how many messages, at most.

### `epoch_record_stale`

Writer: engine. Where: `transform_events`. Kind: problem.

The epoch record could not be written. The request is unaffected. A restart before the next
successful write starts from epoch 0, which costs one rotation.

- `epoch`: the current epoch.
- `error`: the exception's class name.

### `main_changed`

Writer: engine. Where: `transform_events`. Kind: normal.

Another conversation became the session's main one.

- `reason`: `more_turn_starts` (it has more turn starts than the previous main), `new_cli_session`
  (a new Claude Code session id, as after `/clear`), `resumed_cli_session` (`/resume` of a
  conversation that had lost main to another session), `compacted` (Claude Code compacted the main
  conversation) or `after_side_request` (a side request was the session's first request, and this is
  the user's conversation).
- `turn_starts`: how many turn starts the new main conversation has.

### `retained_stale`

Writer: engine. Where: `transform_events`. Kind: problem.

The snapshot of the messages in view could not be written. `contexto_search` then cannot check the
conversation in view and says so in its reply; paged turns are searched as usual.

- `epoch`: the current epoch.
- `error`: the exception's class name.

### `side_room_closed`

Writer: engine. Where: `transform_events`, `usage_events`. Kind: normal.

A subagent's recall room was closed before its 15-minute expiry.

- `reason`: `no_tool_call` (in `usage_events`: the subagent's response held no tool call, so it is
  done) or `agent_stopped` (in `transform_events`: the main conversation's history shows the agent
  stopped).
- `age_s`: seconds since the room was last published.

### `side_room_close_failed`

Writer: engine. Where: `transform_events`. Kind: problem.

The store could not close a finished subagent's room. The room expires on its own after 15 minutes;
until then it can hold the main conversation's recall room down.

- `error`: the exception's class name.

### `mcp_tools_missing`

Writer: engine. Where: `transform_events`, own line. Kind: problem.

The main conversation's request did not offer Contexto's MCP tools, so the model cannot recall or
search paged turns. Nested in `transform_events` when first noticed, 30 seconds or more into the run.
Written once more as its own line at exit, after `proxy_stop`, if the last main request still lacked
them. The launcher prints a warning naming the likely cause.

- `dialect`: `anthropic` (Claude Code) or `responses` (Codex).
- `at`: `request` or `exit`.
- `server_listed`: whether the MCP server answered `tools/list` in this run. False means it probably
  did not start.
- `requests`: how many main requests in a row lacked the tools.
- `elapsed_s`: seconds since the engine started.
- `repeated`: `exit` only: whether it had already been reported during the run.

### `mcp_tools_found`

Writer: engine. Where: `transform_events`. Kind: notice.

Contexto's tools appeared in a main request after `mcp_tools_missing` had been reported.

- `dialect`: `anthropic` or `responses`.
- `at`: `request`.

### `mcp_tools_denied`

Writer: engine. Where: `transform_events`. Kind: problem.

Claude Code refused a Contexto tool call for lack of permission, usually because of an `ask` rule or
organization-managed settings. Logged for the first refusal in a session; the launcher prints a
warning naming the tools.

- `dialect`: `anthropic`.
- `main`: whether the request belongs to the main conversation.
- `tools`: the refused tools' short names, for example `contexto_search`.
- `elapsed_s`: seconds since the engine started.

### `toolcheck_error`

Writer: engine. Where: `transform_events`. Kind: problem.

The check behind the three events above raised. The request is unaffected; the check's answer for
this request is missing.

- `error`: the exception's class name.

### `cli_version`

Writer: engine. Where: `transform_events`. Kind: normal.

The first request from each distinct CLI version (read from `User-Agent`).

- `cli`: `claude` or `codex`.
- `client`: the client name in `User-Agent`, for example `claude-cli` or `codex_exec`.
- `version`: its version.
- `tested`: whether this Contexto release was tested with it.
- `tested_versions`: the tested versions, as one comma-separated string.

### `cli_version_untested`

Writer: engine. Where: `transform_events`. Kind: notice.

The CLI sending requests is a version this Contexto release was not tested with. Logged once. It is
usually fine; if something else in the log looks wrong, this is the first thing to rule out.

- `cli`: `claude` or `codex`.
- `version`: its version.
- `tested_versions`: the tested versions.

### `session_start_unrecognised`

Writer: engine. Where: `transform_events`. Kind: notice.

The main conversation's first request does not carry the CLI's session-start content in a form the
engine recognises. Logged at most once per proxy. A newer CLI version may have changed that form;
[cli-updates.md](cli-updates.md) says how a new version is tested.

- `dialect`: `anthropic` or `responses`.
- `signature_version`: the CLI version the recognised form was measured on.
- `cli_version`: the CLI version seen, or null.
- `tested_versions`: the tested versions.

### `cli_version_error`

Writer: engine. Where: `transform_events`. Kind: problem.

The version check behind the three events above raised. The request is unaffected.

- `error`: the exception's class name.

## Launcher events

`contexto run claude` and `contexto run codex` write these as their own lines.

### `launch`

Writer: launcher (Claude), launcher (Codex). Where: own line. Kind: normal.

The proxy is up and the CLI is about to start.

- `cli`: `claude` or `codex`.
- `session`: the session id.
- `config`: the engine settings given on the command line: `bound`, and `window_fraction` and
  `headroom_floor` when set.
- `engine`: `rewrite`, or `identity` when the paging engine could not be loaded.
- `upstream_source`: as in [`proxy_start`](#proxy_start).
- `observe_thinking`: whether `--observe-thinking` is on.
- `user_arg_count`: how many arguments were passed through to the CLI. The arguments are not logged.
- `affinity_header`: whether the proxy adds a session-affinity header to requests.
- `codex_auth`: Codex only: the `--codex-auth` mode.
- `no_daemon`: Codex only: whether Codex was started with `--no-daemon`, so it leaves no background
  server running.
- `codex_version_error`: Codex only, when the Codex version could not be read: why, in the values of
  [`cli_version_check`](#cli_version_check)'s `error`.

### `cli_version_check`

Writer: launcher (Claude), launcher (Codex). Where: own line. Kind: normal.

The launcher ran `<cli> --version` before starting the CLI. When the version is not a tested one it
prints a warning.

- `cli`: `claude` or `codex`.
- `version`: the installed version, or null when it could not be read.
- `tested`: whether this Contexto release was tested with it; null without a version.
- `tested_versions`: the tested versions, as one comma-separated string.
- `error`: why the version could not be read (`not_found`, `timeout`, `unparsed`, `exit_<code>`
  or an exception's class name), or `interrupted` when Ctrl+C ended the check; the CLI is then not
  started.
- `skipped`: true when the check was turned off; the other fields except `cli` are then absent.

### `window_budget_warning`

Writer: launcher (Claude), launcher (Codex). Where: own line. Kind: notice.

At launch, the bound leaves less than 25,000 tokens of window budget under a typical fixed overhead.
The launcher printed a warning suggesting a larger `--bound`.

- `bound`: the bound.
- `assumed_overhead`: the fixed overhead assumed, in tokens.

### `exit`

Writer: launcher (Claude), launcher (Codex). Where: own line. Kind: normal.

The CLI exited and the launcher cleaned up. It is the launcher's last line. A launcher that was
killed writes none.

- `code`: the exit code `contexto run` returns. Normally the CLI's own. 130 for Ctrl+C before the
  CLI started, 128 plus the signal number when the launcher was terminated, and 1 or 2 when the
  launch failed before the CLI ran.

### `engine_unavailable`

Writer: launcher (Claude), launcher (Codex). Where: own line. Kind: problem.

The paging engine could not be loaded, so the proxy forwards every request unchanged and nothing is
bounded. It means a broken install.

- `detail`: a fixed sentence saying so.

### `affinity_salt_unavailable`

Writer: launcher (Claude), launcher (Codex). Where: own line. Kind: notice.

The install's affinity salt could not be read or created, so requests go out without the
`X-Session-Affinity` header. Only an upstream that routes by that header is affected (see
[cliproxyapi.md](cliproxyapi.md)). `contexto proxy` writes this event too.

- `error`: the exception's class name.

### `watchdog_unavailable`

Writer: launcher (Claude). Where: own line. Kind: notice.

The helper process that cleans up if the launcher is killed could not be started. The session runs
normally; only a killed launcher would leave Claude Code running and its temporary files behind.

- `error`: the exception's class name.

## MCP server events

The MCP server serves the model's `contexto_*` tools. It is a separate process.

### `mcp_call`

Writer: MCP server. Where: own line. Kind: normal.

One line for every tool call. No text is logged: not the query, the pinned text, or what was
returned. A failed call (`ok: false`) is usually the model's mistake, such as an unknown page id,
and the model gets a readable error; `LedgerCorruptError` and `LockFileDamagedError` mean damage in
the session directory.

Every call:

- `tool`: `contexto_recall`, `contexto_search`, `contexto_state`, `contexto_pin` or `contexto_close`.
- `ok`: whether the call succeeded. The fields after `file` in this entry are on successful calls only.
- `error`: on a failed call: the exception's class name. `ToolError` is a call the server refused
  with a message to the model.
- `file`: with `LockFileDamagedError`: the damaged lock file's name.

`contexto_recall`, `contexto_search` and `contexto_state`, which are charged against the turn's room:

- `turn_budget`: `charged` (the reply was charged), `spent` (no room left, so the reply only says
  so), `nearly_spent` (recall: some room is left but too little for a useful chunk), `control` (a
  fixed reply such as "no match", never charged), or `unbudgeted` (no room is published; see
  [`turn_room_missing`](#turn_room_missing)).
- `charged_tokens`: the tokens charged for this reply.
- `room_left`: the room left after it, in tokens.
- `limited_by`: `main` or `side`: whether the main conversation's room or a subagent's was the
  smaller one, which is the one that bounds the call.
- `side_rooms`: how many subagent rooms are open.
- `limiting_age_s`: with `limited_by: "side"`: seconds since that room was last published. A large
  value is a stale room, not a live subagent.
- `returned`: recall and search: the spans of text the reply showed, as positions, not text. Each
  item has `source` (`paged` or `in_view`), `start` and `end`, and either `page_id` or, for text in
  view, `turn` and `message_index`.
- `chars`: recall and state: how many characters of content the reply returned.

`contexto_recall`:

- `page_id`: the page read.
- `offset`: the character offset the read started at.
- `total_chars`: the page's full length in characters.

`contexto_search`:

- `query_chars`: the query's length. The query itself is not logged.
- `hits`: how many pages the reply showed.
- `total_matches`: how many pages matched.
- `in_view_hits`: how many matching lines of the conversation in view the reply showed.
- `in_view_more`: how many more matched in view and were not shown.
- `in_view_checked`: whether the conversation in view could be checked.

`contexto_pin` and `contexto_close` (`chars` is then the length of the pinned text or the note):

- `kind`: pin: the record's kind.
- `record_id`: the id of the record written.
- `supersedes`: pin: how many records it supersedes.
- `obligation_id`: close: the obligation closed.

### `turn_room_missing`

Writer: MCP server. Where: own line. Kind: problem.

A tool call found that the engine is running (it has recorded the window) but has published no room
for the turn. The call was limited only by the per-call limit, not charged against a turn budget.

- `tool`: the tool called.

### `search_cache_invalid`

Writer: MCP server. Where: own line. Kind: notice.

`CONTEXTO_SEARCH_CACHE_MB` is not a number of megabytes. The server used the default and started.

- `value`: the first 100 characters of the variable's value.

## Store events

The session store writes these as their own lines, from whichever process holds the store: the proxy
and launcher, the MCP server, or a `contexto` command.

### `stale_temp_removed`

Writer: store. Where: own line. Kind: notice.

When the session was opened, temporary files left by a process that died mid-write were removed.
Nothing was lost: a write only counts once its temporary file is renamed into place.

- `files`: how many were removed.

### `pruned_while_opening`

Writer: store. Where: own line. Kind: notice.

`contexto sessions prune` deleted the session at the moment it was being opened to resume. It starts
empty. A warning is printed too.

No fields.

### `ledger_damaged`

Writer: store. Where: own line. Kind: problem.

The ledger of pinned records (`ledger.jsonl`) is damaged in some way other than a cut-short last
line, so opening the session did not repair it. `contexto_pin`, `contexto_close` and
`contexto_state` return an error until it is fixed; `contexto sessions show` lists the problems.

- `error`: the exception's class name.
- `line`: the ledger line where the damage is, or null.

### `ledger_repaired`

Writer: store. Where: own line. Kind: notice.

The ledger ended in a line cut short (a crash or a full disk mid-write). When the session was
opened, that tail was copied to a quarantine file and cut off the ledger, which is usable again.

- `quarantine`: the name of the file the tail was moved to.

### `page_quarantined`

Writer: store. Where: own line. Kind: problem.

A corrupted page file was moved to the session's `quarantine/` directory, never deleted. The engine
asks for this to resolve a persistent [`page_conflict`](#page_conflict), then pages the turn again.

- `page_id`: the page.
- `path`: where it went, relative to the session directory.

### `page_unreadable`

Writer: store. Where: own line. Kind: problem.

A page file could not be read, or does not hold a page record. Search and the index skip it, and a
recall of it returns an error to the model. Search and the index log it once per process until the
file changes; each recall of it logs it again.

- `path`: the page file's name.
- `error`: the exception's message. For a failed read it includes the file's full path.
- `will_retry`: true when the read itself failed (permissions, too many open files, an I/O error),
  so the page is skipped for this call only and read again next time.

### `side_room_expired`

Writer: store. Where: own line. Kind: normal.

A subagent's room was dropped because it was not published again for 15 minutes. That is how a room
ends when the engine saw no evidence that its subagent finished.

- `age_s`: seconds since it was last published.
- `room_tokens`: the room's size, in tokens.
- `spent_tokens`: how much of it was used.
