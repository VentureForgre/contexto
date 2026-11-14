# Session log events

Every Contexto session has a log, `log.jsonl`. This is a reference for every event that can appear in
it: who writes it, where in the log it appears, what it means, its fields, and whether it is normal
operation, a notice, or a sign that something went wrong.

`tests/test_events_doc.py` reads the package source and keeps this document in step with it. What it
checks:

- Event names, both ways: every name the source can write has an entry here, and every entry names
  an event the source can write.
- Fields, as far as the test can read them from the source: a key written next to an event's name,
  stored later on the same variable, or unpacked into the event from a dict built in the same
  function must be listed under that event. The fields that reach an event some other way (through
  a helper's return value, for example) are named in a list in the test, and each of those must be
  listed here too. A field the source newly writes through a helper is not noticed until someone
  adds it to that list.
- Every field listed here appears in a module that writes its event. That catches a renamed or
  removed field. It does not prove the field is written on that event.
- The [index](#index) and the entries agree, and each `json` example shows only events and fields
  that are listed.

It does not check an entry's writer, where it appears or its kind against the source, and it does
not check the prose: what an event or a field means, and when a field is present.

## Index

Every event, in the order of its entry below. Writer, Where and Kind are explained under
[Reading an entry](#reading-an-entry). An event whose Where is `transform_events` or `usage_events`
is never a line of its own: look for it inside a `request` line.

| Event | Writer | Where | Kind |
| --- | --- | --- | --- |
| [`request`](#request) | proxy | own line | normal |
| [`proxy_start`](#proxy_start) | proxy | own line | normal |
| [`proxy_stop`](#proxy_stop) | proxy | own line | normal |
| [`proxy_stop_incomplete`](#proxy_stop_incomplete) | proxy | own line | notice |
| [`request_refused`](#request_refused) | proxy | own line | notice |
| [`websocket_rejected`](#websocket_rejected) | proxy | own line | problem |
| [`encoded_body_rejected`](#encoded_body_rejected) | proxy | own line | problem |
| [`request_too_deep`](#request_too_deep) | proxy | own line | problem |
| [`transform_error`](#transform_error) | proxy | own line | problem |
| [`proxy_error`](#proxy_error) | proxy | own line | problem |
| [`upstream_retry`](#upstream_retry) | proxy | own line | notice |
| [`upstream_4xx`](#upstream_4xx) | proxy | own line | notice |
| [`hold_skipped`](#hold_skipped) | proxy | own line | notice |
| [`observe_usage_error`](#observe_usage_error) | proxy | own line | problem |
| [`tool_calls_hook_error`](#tool_calls_hook_error) | proxy | own line | problem |
| [`request` (the engine's nested event)](#request-the-engines-nested-event) | engine | `transform_events` | normal |
| [`count_tokens`](#count_tokens) | engine | `transform_events` | normal |
| [`rotation`](#rotation) | engine | `transform_events` | normal |
| [`page_out`](#page_out) | engine | `transform_events` | normal |
| [`rotation_noop`](#rotation_noop) | engine | `transform_events` | normal |
| [`window_minimal`](#window_minimal) | engine | `transform_events` | notice |
| [`bound_too_small`](#bound_too_small) | engine | `transform_events` | problem |
| [`pdf_allowance`](#pdf_allowance) | engine | `transform_events` | notice |
| [`pdf_pages_uncertain`](#pdf_pages_uncertain) | engine | `transform_events` | notice |
| [`turn_room_low`](#turn_room_low) | engine | `transform_events` | problem |
| [`head_changed`](#head_changed) | engine | `transform_events` | normal |
| [`usage_missing`](#usage_missing) | engine | `transform_events` | notice |
| [`carried_text_growing`](#carried_text_growing) | engine | `transform_events` | notice |
| [`carried_paged`](#carried_paged) | engine | `transform_events` | notice |
| [`carried_over_budget`](#carried_over_budget) | engine | `transform_events` | problem |
| [`unclassified_system_message`](#unclassified_system_message) | engine | `transform_events` | problem |
| [`resync`](#resync) | engine | `transform_events` | notice |
| [`over_bound`](#over_bound) | engine | `transform_events`, `usage_events` | notice, problem |
| [`estimate_error`](#estimate_error) | engine | `usage_events` | notice |
| [`usage_unattributed`](#usage_unattributed) | engine | `usage_events` | notice |
| [`store_error`](#store_error) | engine | `transform_events`, `usage_events` | problem |
| [`page_conflict`](#page_conflict) | engine | `transform_events` | problem |
| [`degraded_enter`](#degraded_enter) | engine | `transform_events`, `usage_events` | problem |
| [`degraded_leave`](#degraded_leave) | engine | `transform_events` | notice |
| [`fallback_failed`](#fallback_failed) | engine | `transform_events` | problem |
| [`sizing_restored`](#sizing_restored) | engine | `transform_events` | normal |
| [`sizing_not_restored`](#sizing_not_restored) | engine | `transform_events` | notice |
| [`epoch_resumed`](#epoch_resumed) | engine | `transform_events` | normal |
| [`epoch_not_resumed`](#epoch_not_resumed) | engine | `transform_events` | normal, notice, problem |
| [`carried_reclassified`](#carried_reclassified) | engine | `transform_events` | notice |
| [`epoch_record_stale`](#epoch_record_stale) | engine | `transform_events` | problem |
| [`main_changed`](#main_changed) | engine | `transform_events` | normal |
| [`retained_stale`](#retained_stale) | engine | `transform_events` | problem |
| [`side_room_closed`](#side_room_closed) | engine | `transform_events`, `usage_events` | normal |
| [`side_room_close_failed`](#side_room_close_failed) | engine | `transform_events` | problem |
| [`mcp_tools_missing`](#mcp_tools_missing) | engine | `transform_events`, own line | problem |
| [`mcp_tools_found`](#mcp_tools_found) | engine | `transform_events` | notice |
| [`mcp_tools_denied`](#mcp_tools_denied) | engine | `transform_events` | problem |
| [`toolcheck_error`](#toolcheck_error) | engine | `transform_events` | problem |
| [`cli_version`](#cli_version) | engine | `transform_events` | normal |
| [`cli_version_untested`](#cli_version_untested) | engine | `transform_events` | notice |
| [`session_start_unrecognised`](#session_start_unrecognised) | engine | `transform_events` | notice |
| [`cli_version_error`](#cli_version_error) | engine | `transform_events` | problem |
| [`hint_mismatch`](#hint_mismatch) | engine | `transform_events` | notice |
| [`in_turn_batch`](#in_turn_batch) | engine | `transform_events` | normal |
| [`in_turn_exhausted`](#in_turn_exhausted) | engine | `transform_events` | problem |
| [`start_text`](#start_text) | engine | `transform_events` | normal |
| [`start_text_changed`](#start_text_changed) | engine | `transform_events` | normal |
| [`start_text_unavailable`](#start_text_unavailable) | engine | `transform_events` | problem |
| [`trusted_conversations_unavailable`](#trusted_conversations_unavailable) | engine | `transform_events` | problem |
| [`main_records_unavailable`](#main_records_unavailable) | engine | `transform_events` | problem |
| [`main_record_unmatched`](#main_record_unmatched) | engine | `transform_events` | notice |
| [`launch`](#launch) | launcher (Claude), launcher (Codex) | own line | normal |
| [`project_memory`](#project_memory) | launcher (Claude), launcher (Codex) | own line | normal, problem |
| [`cli_version_check`](#cli_version_check) | launcher (Claude), launcher (Codex) | own line | normal |
| [`window_budget_warning`](#window_budget_warning) | launcher (Claude), launcher (Codex) | own line | notice |
| [`exit`](#exit) | launcher (Claude), launcher (Codex) | own line | normal |
| [`engine_unavailable`](#engine_unavailable) | launcher (Claude), launcher (Codex) | own line | problem |
| [`affinity_salt_unavailable`](#affinity_salt_unavailable) | launcher (Claude), launcher (Codex) | own line | notice |
| [`watchdog_unavailable`](#watchdog_unavailable) | launcher (Claude) | own line | notice |
| [`serve_open`](#serve_open) | service | own line | normal |
| [`serve_close`](#serve_close) | service | own line | normal |
| [`mcp_call`](#mcp_call) | MCP server | own line | normal |
| [`turn_room_missing`](#turn_room_missing) | MCP server | own line | problem |
| [`search_cache_invalid`](#search_cache_invalid) | MCP server | own line | notice |
| [`stale_temp_removed`](#stale_temp_removed) | store | own line | notice |
| [`pruned_while_opening`](#pruned_while_opening) | store | own line | notice |
| [`ledger_damaged`](#ledger_damaged) | store | own line | problem |
| [`ledger_repaired`](#ledger_repaired) | store | own line | notice |
| [`page_quarantined`](#page_quarantined) | store | own line | problem |
| [`page_unreadable`](#page_unreadable) | store | own line | problem |
| [`side_room_expired`](#side_room_expired) | store | own line | normal |
| [`project_session_skipped`](#project_session_skipped) | store | own line | problem |
| [`project_unavailable`](#project_unavailable) | store | own line | problem |

## The log

**Where it is.** `$CONTEXTO_HOME/sessions/<session id>/log.jsonl`, where `CONTEXTO_HOME` defaults to
`~/.contexto`. The file has mode 0600. `contexto run` prints the session id when it starts, and
`contexto sessions list` shows the sessions.

**Format.** One JSON object per line, appended, never rewritten. Keys are sorted, with no whitespace
between them. Every line has an `event` name and a `ts`.

**Who writes.** The proxy (with the paging engine inside it), the `contexto run` launcher, the
`contexto serve` service, the MCP server, and the session store. The MCP server is a separate process
that the CLI starts, so its lines interleave with the proxy's in the order they were written. The
service keeps a log of its own as well, `$CONTEXTO_HOME/serve.log` (see [serve.md](serve.md#files)).
That file is not a session log, and its events are not in this reference.

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

**Runs.** A log holds every run of its session, one after another: a session that is resumed
(`contexto run --session`) appends to the same file. A run of `contexto run` writes, in this order:

1. Notices from opening the session ([`stale_temp_removed`](#stale_temp_removed),
   [`ledger_repaired`](#ledger_repaired), [`ledger_damaged`](#ledger_damaged),
   [`pruned_while_opening`](#pruned_while_opening)) and
   [`affinity_salt_unavailable`](#affinity_salt_unavailable), when there are any.
2. [`proxy_start`](#proxy_start). Every run whose proxy started has one, and no line of the kinds
   below comes before it, so find a run by its `proxy_start`, not by its `launch`. The lines of
   step 1 come before it: a line between one run's `exit` and the next `proxy_start` belongs to the
   next run, or to no run (the end of this section).
3. [`engine_unavailable`](#engine_unavailable) and [`watchdog_unavailable`](#watchdog_unavailable),
   when they apply, then [`launch`](#launch), [`project_memory`](#project_memory),
   [`cli_version_check`](#cli_version_check) and [`window_budget_warning`](#window_budget_warning)
   when it applies.
4. The run's `request` lines and the other lines of the proxy, the MCP server and the store.
5. [`proxy_stop_incomplete`](#proxy_stop_incomplete) when it applies, [`proxy_stop`](#proxy_stop),
   then [`mcp_tools_missing`](#mcp_tools_missing) when it applies, then [`exit`](#exit), the last
   line.

A launch that fails after the proxy started writes `proxy_start`, `proxy_stop` and `exit` with no
`launch` between them. `contexto run codex` does so when it cannot build Codex's configuration (no
key helper, for example); it then removes a session that this launch created, log included, so
those three lines are found only in a session that already existed. A launcher that is killed
(SIGKILL) writes no `proxy_stop` and no `exit`, so a run can also end where the next run's lines
begin (its step 1 lines, then its `proxy_start`) or at the end of the file. `contexto proxy`, which
runs the proxy alone, writes neither `launch` nor `exit`: stopped with Ctrl+C it writes
`proxy_start` and `proxy_stop`, and ended by SIGTERM it writes `proxy_start` alone. A store line can
also be written outside any run, by a `contexto` command that opens the session (`contexto recall`,
for example).

A session that `contexto serve` serves has no `proxy_start`, `launch` or `exit`: the service's proxy
for a session is never started on a port of its own. Each time the service opens the session it
writes, in this order: the notices from opening it and
[`affinity_salt_unavailable`](#affinity_salt_unavailable), when there are any, then
[`serve_open`](#serve_open), the session's `request` lines and the other lines of the proxy, the MCP
server and the store, then [`mcp_tools_missing`](#mcp_tools_missing) when it applies, and
[`serve_close`](#serve_close) when the service closes the session. A service that is killed writes no
`serve_close`. One session's log can hold runs of `contexto run claude` and opens by the service, one
after another.

**Credentials.** The value of any key named `authorization`, `x-api-key`, `cookie`, `set-cookie` or
`proxy-authorization` (in any letter case, at any depth) is replaced by `[redacted]` before the line
is written. Redaction goes by key name only. No event has such a key: request headers and bodies are
never put in an event.

**No conversation content.** Events are built from counts, sizes, ids, fixed words and exception class
names, and the few strings from outside that the table below lists. They hold no prompt, response,
tool output or file content, and no text from a tool argument: of a tool call's arguments,
[`mcp_call`](#mcp_call) logs only a page id, an offset, a record kind and an obligation id, and only
when the call succeeded. The proxy adds a
second guard to nested engine events, to a request's `model` and to `input_transformations`: a string
longer than 80 characters is replaced by its length (`<123 chars>`), a list is cut to its first 50
items, and anything nested deeper than 16 levels becomes `<dict>` or `<list>`. The guard does not
apply to the other fields of the proxy's own lines, or to the launcher's, the MCP server's and the
store's lines.

The log is not anonymous. It holds the session id, page ids and pinned-record ids, the model name,
request paths and the CLI version. These fields hold a string that comes from outside Contexto.
None is a credential or conversation text:

| Field | What it holds | Where the string comes from |
| --- | --- | --- |
| [`page_unreadable`](#page_unreadable) `error` | An exception's message. For a failed read it names the page file with its full path, which includes the home directory. | The machine. |
| [`search_cache_invalid`](#search_cache_invalid) `value` | The first 100 characters of `CONTEXTO_SEARCH_CACHE_MB`. | The environment. |
| [`launch`](#launch) `session` | The session id. With `--session` it is the name the user chose. | The command line. |
| `model` on a `request` line and in [`upstream_4xx`](#upstream_4xx) | The model the request names, at most 80 characters. | The request body. |
| `method`, `path` and `endpoint`, on every line that has one | The request's HTTP method and its path without the query string, at any length. On a [`request_refused`](#request_refused) line they are those of a request that did not come from the CLI. | The request line. |
| [`encoded_body_rejected`](#encoded_body_rejected) `encoding` | The request's `Content-Encoding` header, lower-cased, at any length: the 80-character guard does not apply to it. | A request header. |
| [`hold_skipped`](#hold_skipped) `encoding`, and `usage_unavailable` on a `request` line | The response's `Content-Encoding` header, lower-cased, at any length. | Upstream's response header. |
| `usage` on a Responses API `request` line | Upstream's usage object: its key names as sent, with their numbers. Other values are dropped. | Upstream's response. |
| `input_transformations` on a `request` line | What upstream reported, each string at most 80 characters. | Upstream's response. |
| `client` and `version` in [`cli_version`](#cli_version), `version` in [`cli_version_untested`](#cli_version_untested), `cli_version` in [`session_start_unrecognised`](#session_start_unrecognised) | The client name and version from `User-Agent`, taken only in the form `name/1.2.3`. | A request header. |

Read a log before you share it, and look at these fields first.

**A line cut short.** A write that a full disk stops partway leaves a line without its end. The next
event starts on a new line, so the cut line stays as one line that is not valid JSON. Skip lines that
do not parse, as `contexto sessions show` does.

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
(`rotation`), and sent 24 of the CLI's 61 messages: the 23 from `k` on and one carried from a paged
turn (its `request` event). The conversation was in
epoch 1 with 9 turns paged already, so `estimate`, 126,480 tokens, is the size of what epoch 1 would
have sent, not of the CLI's 492,310 bytes. Most `request` lines carry only that last nested event:

```json
{"api":"messages","duration_ms":8412,"epoch":2,"event":"request","message_count":24,
 "method":"POST","model":"claude-sonnet-5-5","original_message_count":61,
 "original_request_bytes":492310,"over_bound":false,"path":"/v1/messages","request_bytes":199940,
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
- **Conversation**: one message history. The engine tells conversations apart by their first
  prompt. The **main conversation** is the one the user is in; subagents and the CLI's side requests
  (a title, for example) are other conversations in the same session. No line names its conversation.
  The nested `request` event's `main` says whether the engine took the line's conversation for the
  main one when it sent the request.
- **Turn**: a user prompt and everything up to the next one. A **turn start** is a request whose
  newest turn has no assistant message yet. The engine pages at turn starts. The one exception is a
  [`resync`](#resync) with `paged_changed`, which pages at whatever request finds the change.
- **Tool loop**: the requests of a turn after its turn start. The model called a tool, and the CLI
  sends the result back with the turn so far. For Claude Code the engine pages inside a tool loop
  too ([in-turn paging](in-turn-paging.md), D63; see [`in_turn_batch`](#in_turn_batch)). A Codex
  tool loop, and any tool loop with `--no-in-turn-paging`, is sent whole. A turn that grows there
  can pass the bound.
- **Epoch**: the stretch between two rotations of one conversation. Within an epoch the prompt
  prefix does not change, so the prompt cache can be read. `epoch` counts from 0.
- **`k`**: the index, in the CLI's message list, of the first message still sent. Messages before `k`
  are paged out. `k` is 0 in the first epoch.
- **Carried content**: what is sent on every request and not paged with its turn: the context
  blocks the CLI puts into a prompt message beside the user's own text, and the `role: "system"`
  messages the engine classifies as carried (a standing message from a hook or a mode, for
  example). Only the fallback in [`carried_paged`](#carried_paged) pages any of it.
- **Fixed overhead** (`F`): system prompt, tools, carried content and the preamble (pinned state and
  page index), in tokens.
- **Headroom**: the tokens kept free under the bound for the growth of one turn: 1.25 times the
  largest recent growth of a turn (an older turn's growth counts for half at each later turn start),
  at least the headroom floor (`--headroom-floor`, 25,000 by default) and, above that floor, at most
  a quarter of the bound.
- **Window budget** (`W`): `bound - headroom - F`, what is left for turns.
- **Window target**: the size a rotation cuts the retained turns down to: `W` times
  `--window-fraction` (0.5 by default). The other half is room to grow before the next rotation. A
  rotation made for the room (reason `min_turn_room`) uses `W - min_turn_room` in place of `W`.
- **Room**: the tokens `contexto_recall`, `contexto_search` and `contexto_state` may return in the
  current turn. A **side room** is a subagent's own.
- **`min_turn_room`**: the least room a turn should start with, 12,000 tokens. A turn that would
  start with less makes the engine rotate if a rotation would restore it.
- **Degraded mode**: after a store failure in one conversation, the engine stops paging that
  conversation and sends its last good transform, extended with the new messages, until the store
  works again at one of its turn starts or at a `paged_changed` resync of it, or the run ends. It
  belongs to that conversation alone: the others keep paging until a store call of their own fails.

The design contract, [milestones/M2-contexto-proxy.md](milestones/M2-contexto-proxy.md), defines
these and the rules the entries cite.

### What the counters count

`epoch`, `k`, `turns_paged`, `first_turn` and degraded mode (the nested `request` event's
`degraded`) belong to one conversation in one run. The engine keeps a separate state for each
conversation, and each run starts a new engine. So:

- Every conversation starts at epoch 0. A line with `epoch` 3 can be followed by another
  conversation's line with `epoch` 0.
- A new run continues the main conversation's counters only when that conversation's first request
  in the run has [`epoch_resumed`](#epoch_resumed). With [`epoch_not_resumed`](#epoch_not_resumed)
  the conversation starts again at epoch 0 with nothing counted as paged. Its next rotation pages it
  from its first turn again (`first_turn` 0) and reports those turns again.
- Every other conversation starts again at epoch 0 in a new run: the record holds the main
  conversation alone. Its first request has `epoch_not_resumed` only until the run has used the
  record or found none. After that it has neither event, and the `page_out` of its first rotation
  still has `first_turn` 0. So the absence of `epoch_not_resumed` does not mean a conversation carried its
  counters over.
- A [`resync`](#resync) with `paged_changed` pages everything before the current turn again within
  one run, and counts those turns again in `turns_paged`.
- A [`degraded_enter`](#degraded_enter) is the conversation of its line entering degraded mode, and a
  [`degraded_leave`](#degraded_leave) is one conversation leaving it, at that conversation's next turn
  start that finds the store working, or at a `paged_changed` [`resync`](#resync) of it that finds the
  store working, which can come inside a tool loop. A conversation that sends no further turn start
  and no such resync, such as a subagent that has finished, never logs a leave. So a `degraded_enter`
  with no `degraded_leave` after it does not mean the session stopped paging: other conversations, the
  main one included, can go on paging and rotating on the lines that follow.
- A new run starts every conversation out of degraded mode, and no `degraded_leave` is logged for
  one that was degraded when the previous run ended. `epoch_resumed` does not carry degraded mode
  over: the engine writes the epoch record only while the main conversation is not degraded, so a
  resumed conversation continues the counters of a record written before it entered degraded mode,
  and is paged as usual from its first request in the new run.
- The store keeps one page per page id, and a turn paged again with the same turn number and the
  same messages gets the same id. So a run that pages a conversation from turn 0 again normally
  does not store those turns twice. A `paged_changed` resync gives its pages new turn numbers, so they are
  stored beside the earlier ones.

A tool that reads a log must not:

- take the highest `epoch` as the number of rotations. Count the [`rotation`](#rotation) events.
- take the last `turns_paged` as the number of turns the session has paged, or add up
  `page_out.turns` or `page_out.pages` over a whole log. The number of pages the session holds is
  `pages` in `contexto sessions show --json`, which counts the store.
- compare `epoch` or `k` between two lines unless they are the same conversation in the same run.
- pair a `degraded_enter` with the next `degraded_leave` in the log. Pair them per conversation,
  which in practice means for the main conversation, followed as below, with its `degraded`.

No total of turns or pages paged can be derived from the log alone, because no line names its
conversation: a turn that is paged again after a restart or a resync cannot be matched to its
earlier paging, and the lines of two conversations with `main: false` cannot be told apart. Only
the main conversation can be followed, as the last point below says.

It can rely on this:

- Each `rotation` event is one rotation, and each [`page_out`](#page_out) belongs to the `rotation`
  that follows it in the same `transform_events`.
- A `page_out`'s `first_turn + turns` is the `turns_paged` of its `rotation`.
- Within one run (from a [`proxy_start`](#proxy_start) to the next), the lines whose nested
  `request` event has `main: true` belong to one conversation until a line with a
  [`main_changed`](#main_changed) or an [`epoch_resumed`](#epoch_resumed) event. That line is the
  first on which the new conversation is main. With `main_changed` the conversation can have
  earlier lines in the run, with `main: false`. The `epoch_resumed` case: after a restart a side
  request often arrives before the main conversation's next prompt. Its line has `main: true` and
  `epoch` 0, and the line with `epoch_resumed` that follows is the recorded main conversation at
  its recorded epoch, with no `main_changed`. On the lines of one conversation in one run, `epoch`
  rises by one at each `rotation`. A line with `transform_error` set to a class name (the engine
  raised) breaks the rule: it has no nested events, so a `main_changed` or `epoch_resumed` it would
  have had is lost. After such a line the main conversation is unknown until the next of these
  events. A resume on that request is undone, not only unlogged: the engine used the record up on it
  and kept nothing, so the conversation starts again at epoch 0, and its next line has neither
  `epoch_resumed` nor `epoch_not_resumed`. The `page_out` of its first rotation then has `first_turn` 0, with an
  `epoch` that can equal the one before the restart although the counters were not continued.

### Reading an entry

Each entry has one line of facts, then the meaning, then one bullet per field. `event` and `ts` are
not repeated in the field lists. In the facts line:

- **Writer** is `proxy`, `engine`, `launcher (Claude)`, `launcher (Codex)`, `service` (`contexto
  serve`), `MCP server` or `store`.
- **Where** is `own line`, `transform_events` or `usage_events`.
- **Kind** is `normal` (expected in a healthy session), `notice` (worth knowing, the session carries
  on as designed) or `problem` (Contexto left its designed path, or cannot do part of its job). Three
  entries, [`over_bound`](#over_bound), [`epoch_not_resumed`](#epoch_not_resumed) and
  [`project_memory`](#project_memory), name more than one kind: which one applies depends on the
  event's fields, and the entry says how.

## The request line

### `request`

Writer: proxy. Where: own line. Kind: normal.

One line for each request the proxy sent upstream or tried to send, written when the response has
ended or the attempt has failed. It is the line to read for sizes, token usage and timing. The line
itself is normal. The fields `error`, `transform_error`, `transform_skipped`, `fallback`,
`client_aborted` and a true `over_bound` mark a request that did not go as designed.

Some requests have no `request` line:

- A request the proxy answered itself and did not forward: [`request_refused`](#request_refused)
  (403), [`websocket_rejected`](#websocket_rejected) (501) and
  [`encoded_body_rejected`](#encoded_body_rejected) (415). That line is the only record of it.
- A request that ended in a [`proxy_error`](#proxy_error) with `fallback` `local_502` or `none`.
- A request still in flight when the proxy stopped
  ([`proxy_stop_incomplete`](#proxy_stop_incomplete)).
- A request with a method the proxy does not handle (`TRACE` or `CONNECT`; it handles `GET`, `POST`,
  `PUT`, `PATCH`, `DELETE`, `HEAD` and `OPTIONS`). The HTTP server answers 501 and nothing is
  logged.

A request with malformed framing (a bad `Content-Length` or chunk size) is not forwarded either. Its
`request` line has only `method`, `path`, `status` 400 and `error`.

**Which fields a line has.** A field that does not apply is absent, not null, so read every field
with a default. Four of the line's own fields can be null when present: `usage`, any value inside a
Messages API `usage`, `model` (a request that names none) and `errno`. Every line except the 400
line above has `method`, `path`, `original_request_bytes`, `request_bytes` and `duration_ms`. The
rest depends on two things.

What the proxy did with the request before sending it:

| The request | `api` | `model`, `stream`, `original_message_count`, `message_count` | `transform_events`, `over_bound`, `epoch` |
| --- | --- | --- | --- |
| A model request the engine transformed: `POST` to `/v1/messages`, `/responses` or `/v1/responses` | yes | yes | yes |
| `POST /v1/messages/count_tokens` | yes | yes | yes; `transform_events` holds only [`count_tokens`](#count_tokens) |
| With `transform_skipped` | yes | no | no |
| With `transform_error` set to a class name (the engine raised) | yes | yes | no, except that a line with `usage_events` also has `over_bound`, true or false |
| With `transform_error` set to `serialize: <class>` | yes | yes | yes |
| With `fallback: true` | no | no | no |
| Any other method or path, passed through (Claude Code's `HEAD /api/hello`, for example) | no | no | no |

How the exchange ended:

| The end | `status` | `response_bytes`, `usage` | `stop_reason` and the other end fields |
| --- | --- | --- | --- |
| Upstream answered, with any status | upstream's | yes | those the response held |
| Upstream answered and the response then broke off (`error` starts `relay:`), or the CLI left during it (`client_aborted` with a `status`) | upstream's | yes: the bytes read and the usage read up to that point | those read up to that point |
| No answer from upstream (`error` starts `upstream unreachable:`) | 502 | no | no |
| The CLI left before the response started (`client_aborted` with `upstream_phase`) | no | no | no |

`usage_events` and `stream_hold` can appear only on a model request that reached the engine (the
first row of the first table and its two `transform_error` rows) and was answered with a 2xx status.
A `count_tokens` line never has them, and its `usage` is null: that response holds a count and no
usage object. A line with `fallback: true` has no `api`, so its response is read as a Messages API
response. For a Codex stream its `usage` is null and it has no end fields. A Codex response with a
JSON body (an error, for example) can have `error_type`, and a `usage` with the four Messages API
keys when the body holds a usage object.

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
- `response_bytes`: the size of the response body read from upstream, in bytes. Absent with status
  502 and when the CLI left before a response started.
- `duration_ms`: milliseconds from the start of handling the request to the end of the response, or
  to the failure. On a `fallback` line it counts from the start of the second attempt.
- `usage`: the token usage upstream reported, or null when none was read. Absent where
  `response_bytes` is absent. On the Messages API (Claude Code) it always has the four keys
  `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` and `output_tokens`; a
  key upstream did not send is null, so count a null as 0. The request's real input is the sum of
  the first three. On the Responses API (Codex) it is the usage object as upstream gave it, numbers
  only: there `input_tokens` already includes the cached tokens, which
  `input_tokens_details.cached_tokens` counts.

Requests to a model endpoint (`POST` to `/v1/messages`, `/v1/messages/count_tokens`, `/responses`
or `/v1/responses`). The tables above say which of these a line has:

- `api`: `messages` or `responses`.
- `model`: the model the request names, or null when it names none.
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
- `epoch`: the epoch of this request's conversation when it was sent, taken from the last nested
  event that has one. It is one conversation's counter, not the session's (see
  [What the counters count](#what-the-counters-count)). Absent when no nested event has an epoch,
  as under `contexto proxy`, which runs without the engine.
- `stream_hold`: present when the response was a stream the proxy could hold. `held_events` is how
  many stream events were held back, from the first tool call until the usage was read, so the room
  is published before the CLI starts the call. `triggered` is whether the stream reached its usage.
- `stop_reason`: Messages API: why the response ended (`end_turn`, `tool_use`, `max_tokens` and the
  API's other values). Absent when the response held none, as for `count_tokens` and most errors.
- `error_type`: Messages API: the type of an error the response carried.
- `end_event`: Responses API streams: the event the stream ended on: `completed`, `incomplete`,
  `failed`, or `none` when it ended without one.
- `response_status`: Responses API: the response's own `status`.
- `incomplete_reason`: Responses API: why the response is incomplete.
- `error_code`: Responses API: the code of an error the response carried.

The six end fields hold only values on a fixed list; any other value is logged as `other`. Each is
present only when the response held it and the proxy could read the response (no
`usage_unavailable`).

Only when something unusual happened:

- `request_content_encoding`: the request body arrived compressed with this encoding. The proxy
  decoded it and sent it on uncompressed.
- `wire_request_bytes`: the compressed body's size as received.
- `transform_skipped`: the request was forwarded without a transform, and why: its body is not JSON,
  is not an object, or nests deeper than 512 levels (then with a
  [`request_too_deep`](#request_too_deep) line). The code has a fourth value,
  `cannot serialize request: <class>`, for a transform that may change its input. Neither transform
  Contexto ships is one, so it does not occur; if it did, the line would also have `model`, `stream`
  and `original_message_count`.
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
- `usage_unavailable`: the response was encoded in a way the proxy does not read, so `usage` is null
  and there are no end fields. The value is `response content-encoding ` followed by upstream's
  `Content-Encoding`.
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
- `pages`: the page files in the session's store when the proxy started. Absent when the pages
  directory could not be listed. `contexto stats` takes a run's pages added from it and the `pages`
  of the run's `proxy_stop`.

### `proxy_stop`

Writer: proxy. Where: own line. Kind: normal.

The proxy stopped accepting requests and finished waiting for those in flight.

- `pages`: the page files in the session's store when the proxy stopped. Absent when the pages
  directory could not be listed.

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

- `encoding`: the request's `Content-Encoding` header, lower-cased and otherwise as the client sent
  it, at any length.
- `endpoint`: the request path.
- `reason`: why it could not be read, one of a fixed set of sentences.

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
- `encoding`: the response's `Content-Encoding` header, lower-cased and otherwise as upstream sent
  it, or `identity`.
- `chunked`: whether the response was chunked.

### `observe_usage_error`

Writer: proxy. Where: own line. Kind: problem.

The engine raised while reading a response's usage. The response was relayed, but its usage did not
reach the engine. This is a Contexto defect worth reporting.

- `error`: the exception's class name.

### `tool_calls_hook_error`

Writer: proxy. Where: own line. Kind: problem.

Under `contexto serve` only: the service could not record which session the client tool calls in a
response belong to. The response was relayed as usual. A `contexto_*` tool call from that response
may then find no session, and the model gets a tool error ([serve.md](serve.md#the-mcp-server)).
The proxy hands the service the new tool-call ids each time it reads part of a response, so one
response can log this more than once.

- `error`: the exception's class name.

## Engine events (nested in request lines)

The paging engine does not write to the log itself. It returns these events to the proxy, which puts
them inside the `request` line, in `transform_events` or `usage_events`. The one exception,
[`mcp_tools_missing`](#mcp_tools_missing) at exit, is described in its entry.

### `request` (the engine's nested event)

Writer: engine. Where: `transform_events`. Kind: normal.

The last item of `transform_events` on every request sent to a model endpoint: what the engine sent.
A `count_tokens` request has a [`count_tokens`](#count_tokens) event in its place.

- `epoch`: the epoch of this request's conversation when it was sent. It is that conversation's
  counter in this run (see [What the counters count](#what-the-counters-count)).
- `k`: the first message still sent; messages before it are paged out. Per conversation, like `epoch`.
- `messages`: how many messages the CLI's request had.
- `upstream_messages`: how many messages were sent upstream.
- `estimate`: the estimated input, in tokens, of this request as the epoch in force when it arrived
  would send it: the conversation's last real input plus the messages added since, or, when there
  is no usable last input (a conversation's first request in a run, after
  [`usage_missing`](#usage_missing) or a [`resync`](#resync)), an estimate of the system prompt,
  the tools and the messages that epoch sends. The rotation decision uses it. It is not the size of
  the CLI's own request: from the first rotation on, the paged turns are not in it. For that size
  use `original_request_bytes` on the `request` line, which is in bytes. Absent with
  `fallback_failed`.
- `sent_estimate`: only when this request rotated: the estimated input of what was sent after the
  rotation. The estimate of what was sent is `sent_estimate` when present, else `estimate`.
- `sent_bytes`: the size of the request sent upstream, in bytes of canonical JSON.
- `over_bound`: true when the estimate of what was sent exceeds the bound. An
  [`over_bound`](#over_bound) event precedes it.
- `degraded`: true when the request was sent in degraded mode.
- `in_turn_thinking`: only on a request inside a tool loop after in-turn paging has removed thinking
  in the turn (D63): counts of the thinking blocks in the messages from `k` on, with no content.
  `kept_before` (before the first stretch whose thinking is removed), `stripped` (removed),
  `kept_between` (between two such stretches) and `kept_after` (after the last one). Messages folded
  into a page are not counted.
- `fallback_failed`: true when the CLI's request was sent unchanged (see
  [`fallback_failed`](#fallback_failed)).
- `main`: whether this request belongs to the main conversation.
- `conversations`: how many conversations the engine is tracking in this session.
- `cli_session`: Messages requests only: whether the request carried a Claude Code session id with
  its entrypoint. `contexto stats` says "Claude Code did not compact" only for a log in which every
  such request did.
- `request_class`: the request's `x-claude-code-request-class` hint header (D70), when it carried
  one Contexto knows: `main`, `compaction`, `subagent`, `workflow` or one of the side classes.
  Absent otherwise.
- `context_compacted`: the hint that Claude Code compacted this conversation: `auto`, `manual`,
  `reactive`, or `other` for a value Contexto does not know. Absent when the request carried no
  such hint.
- `compaction`: the same vocabulary, for the summarization request itself: the trigger its
  compaction hint header named. Absent when the request carried no such hint.

### `count_tokens`

Writer: engine. Where: `transform_events`. Kind: normal.

The only item on a `/v1/messages/count_tokens` request. The engine applies the current epoch's
transform to it, so the CLI's own counts match what is sent. It never rotates and has no side effects.

- `epoch`: the epoch whose transform was applied.

### `rotation`

Writer: engine. Where: `transform_events`. Kind: normal.

A new epoch started at this request: older turns went to the store and the retained window was
rebuilt. The first request after a rotation rewrites the cache from the window on.

`epoch`, `k` and `turns_paged` are this conversation's counters in this run, not the session's (see
[What the counters count](#what-the-counters-count)).

- `epoch`: the new epoch's number: the conversation's previous epoch plus one.
- `reason`: `bound` (the estimate passed `bound - headroom`), `min_turn_room` (the recall room for
  the new turn would have been under `min_turn_room`), `window_minimal` (see
  [`window_minimal`](#window_minimal)), `head_change` (the system prompt or tools changed),
  `paged_changed` (see [`resync`](#resync)), or `in_turn` (in-turn paging paged the earlier turns
  of the window inside a tool loop; an [`in_turn_batch`](#in_turn_batch) follows).
- `k`: the first message still sent in the new epoch.
- `retained_messages`: the number of the CLI's messages from `k` on. Messages from before `k` that
  are carried (a `role: "system"` message from a paged turn, or a Codex `developer` item ahead of
  the first turn) are sent too, and are counted only in the nested `request` event's
  `upstream_messages`.
- `turns_paged`: how many turns this conversation's engine has paged out in this run, this
  rotation's included, on top of the count it resumed with, if it resumed. Carried items that
  earlier rotations paged ([`carried_paged`](#carried_paged)) are counted too. It is not the number
  of pages in the store: after a restart that did not resume, or a `paged_changed` resync, turns
  paged before are paged and counted again.
- `fixed_overhead`: `F` in tokens, as used for this decision. Absent with reason `paged_changed` or
  `in_turn`.
- `window_budget`: `W` in tokens. Absent with reason `paged_changed` or `in_turn`.

### `page_out`

Writer: engine. Where: `transform_events`. Kind: normal.

Turns were written to the store, one page per turn. It comes just before the `rotation` it belongs
to. A rotation that pages nothing (a head change with nothing to page) has no `page_out`.

- `epoch`: the epoch that is starting.
- `turns`: how many turns this rotation paged.
- `pages`: how many pages that made: one per turn. A page already in the store with the same turn
  number and messages is kept as it is and still counted here.
- `first_turn`: the turn number the first of these pages gets: the conversation's `turns_paged`
  before this rotation. It is 0 again after a restart that did not resume, so do not add up `turns`
  or `pages` over a log (see [What the counters count](#what-the-counters-count)).

### `rotation_noop`

Writer: engine. Where: `transform_events`. Kind: normal.

A turn start met the rotation trigger, but there was nothing to page: the retained turns already fit
the window target, or no turn older than the newest complete one is still retained. Nothing changed
and the epoch continues.

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

The recall room is under `min_turn_room`, so recall and search may run out of room in this turn.
Three cases, told apart by `reason`:

- No `reason`: a turn started with less room than `min_turn_room`, and no rotation could restore it.
  Logged once per epoch. It means the bound is small for this session: every epoch logging it is a
  reason to raise `--bound`.
- `reason: "in_turn_small_bound"`: inside a tool loop, the bound is small next to the system prompt,
  the tools and the turn's prompt, so in-turn paging holds requests closer to the bound and the room
  of this request falls under `min_turn_room` (see [in-turn-paging.md](in-turn-paging.md#when-it-runs)).
  Logged once per turn.
- `reason: "usage_missing"`: inside a tool loop, the previous request's usage has not arrived, so
  the estimate is not corrected, and the room published for this request falls under
  `min_turn_room`. Logged once per turn, and not in degraded mode.

A turn logs at most one of the two in-turn cases.

- `epoch`: the current epoch.
- `reason`: `in_turn_small_bound` or `usage_missing`. Absent at a turn start.
- `room`: the room, in tokens: at a turn start, the room the turn starts with; in a tool loop, the
  room of this request, never below 0.
- `best_room`: at a turn start only: the room a rotation for the room would have given.
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
  everything before it paged (a `rotation` with reason `paged_changed` follows). A conversation in
  degraded mode leaves it here when the store works ([`degraded_leave`](#degraded_leave) follows).
- `matched`: how many leading messages still match.
- `previous_messages`: how many messages the previous request had.
- `messages`: how many this request has.
- `inflight_thinking_kept`: `paged_changed` only: whether the current turn holds thinking blocks,
  which are kept.

### `over_bound`

Writer: engine. Where: `transform_events`, `usage_events`. Kind: notice, problem.

The request was over the bound. With `source: "estimate"` (in `transform_events`) the estimate of
what is sent exceeded the bound before sending. With `source: "real"` (in `usage_events`) the real
input upstream reported did. One request can have both. Either sets `over_bound` on the `request`
line.

The kind depends on the case. An `estimate` event says which case it is. A `real` event does not:
it has neither `in_tool_loop` nor `degraded`. Classify a `real` event by the `estimate` event in
the `transform_events` of the same `request` line.

| The event | Kind | What it means |
| --- | --- | --- |
| `estimate` with `in_tool_loop: true` and no `degraded` | notice | The allowed case (contract rule 5). The turn grew past the bound inside a tool loop, and in-turn paging could not bring it back under or did not run (Codex, or `--no-in-turn-paging`). The next turn start rotates. |
| `estimate` with `degraded: true` | problem | A consequence of degraded mode, in which the conversation pages nothing. The fault to look at is the [`store_error`](#store_error) or [`page_conflict`](#page_conflict) that started it. |
| `estimate` with `in_tool_loop: false` and no `degraded` | problem | Over the bound at a turn start. A [`bound_too_small`](#bound_too_small) or [`pdf_allowance`](#pdf_allowance) on the same line says why. With neither, the rotation's sizing did not hold. |
| `real`, with an `estimate` event on the same line | that event's kind | The real input confirms the estimate. |
| `real`, with no `estimate` event on the same line | problem | The real input was over the bound and no estimate said so. Either the estimate was at or under the bound, so it was low, or there was none: `estimate` is null when the usage was not paired with a request, as after a `transform_error`. The log does not say whether the request was inside a tool loop. If the nested `request` event on the line has `degraded: true`, it is a consequence of degraded mode. |

The acceptance harness is stricter than these kinds: it fails a run for any `real` event and for
any `estimate` event without `in_tool_loop: true`.

- `source`: `estimate` or `real`.
- `epoch`: the current epoch.
- `bound`: the bound.
- `estimate`: the estimate made before sending, in tokens. Null on a `real` event whose usage was
  not paired with a request (see [`usage_unattributed`](#usage_unattributed)).
- `in_tool_loop`: `estimate` only: whether the request is inside a tool loop. Absent on a `real`
  event.
- `degraded`: `estimate` only: true in degraded mode. Absent otherwise, and on a `real` event.
- `real_tokens`: `real` only: the real input. On the Messages API it is the sum of `input_tokens`,
  `cache_read_input_tokens` and `cache_creation_input_tokens`. On the Responses API it is
  `input_tokens` alone, which already includes the cached tokens.
- `over_bound`: `real` only: true.

### `estimate_error`

Writer: engine. Where: `usage_events`. Kind: notice.

The real input was more than 10% over the estimate made before sending. The engine corrects its
bytes-per-token ratio from every response, so one such event is not a fault. Repeated ones mean the
engine keeps estimating low, which makes the bound less reliable.

- `epoch`: the current epoch.
- `real_tokens`: the real input, counted as in [`over_bound`](#over_bound).
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

The session store could not be read or written. The conversation on the line enters degraded mode
(or stays in it). Other conversations are not affected until a store call of theirs fails.

- `epoch`: the current epoch.
- `operation`: the store operation that failed, for example `page_out` or `set_turn_room`.
- `error`: the exception's class name.
- `errno`: the error's name when it has an operating-system error number, for example `ENOSPC` (a
  full disk) or `EACCES` (a permission problem); never its message. Absent otherwise.
- `degraded`: true when the conversation was already in degraded mode.

### `page_conflict`

Writer: engine. Where: `transform_events`. Kind: problem.

A page already in the store differs from the turn being paged under the same id, or its file is not
a page record. Either means a corrupted page. The conversation on the line enters degraded mode; at
its next turn start the engine has the store move that page to quarantine and pages the turn again.

- `epoch`: the current epoch.
- `operation`: the store operation that failed.
- `error`: the exception's class name.
- `degraded`: true when the conversation was already in degraded mode.
- `page_id`: the page in conflict.

### `degraded_enter`

Writer: engine. Where: `transform_events`, `usage_events`. Kind: problem.

The conversation on this line entered degraded mode. Until it leaves, the engine pages nothing of it
and never rotates it, so its requests can exceed the bound; each is marked `degraded` and, when
over, `over_bound`. Nothing is lost, because no turn of it is paged while the store is broken. The
store is tried again at each of its turn starts and at a `paged_changed` [`resync`](#resync) of it.
Other conversations keep paging, and each enters degraded mode on a line of its own when a store call
of its own fails. Degraded mode ends with the run, with no `degraded_leave` (see
[What the counters count](#what-the-counters-count)).

- `epoch`: the current epoch.
- `reason`: `store_error` or `page_conflict`: the event just before it.

### `degraded_leave`

Writer: engine. Where: `transform_events`. Kind: notice.

The store works again for the conversation on this line, which left degraded mode at this request:
a turn start, or a `paged_changed` [`resync`](#resync), which can come inside a tool loop and is
logged just before this event. Its rotation resumes at this request. Each conversation leaves with
its own `degraded_leave`, so pair this event with the `degraded_enter` of the same conversation. A
conversation that sends no further turn start or such resync never logs one, and a new run ends
degraded mode without one (see [What the counters count](#what-the-counters-count)).

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
the cache still holds. The resumed conversation is the main one from this line on. When another
conversation was main before it in the run (a side request that arrived first), no
[`main_changed`](#main_changed) says so: this event is the only sign. The resumed conversation is not
in degraded mode, whatever it was when the previous run ended.

- `epoch`: the epoch resumed.
- `k`: its first message still sent.

### `epoch_not_resumed`

Writer: engine. Where: `transform_events`. Kind: normal, notice, problem.

The engine started a conversation from epoch 0 instead of continuing a recorded epoch. It is logged
at a conversation's first request in a run, until the run's epoch record has been used or found
missing. After a restart the cost is one rotation, and that rotation pages the conversation from
its first turn again (see [What the counters count](#what-the-counters-count)).

The kind depends on `reason`:

| `reason` | Kind | What it means |
| --- | --- | --- |
| `no_record` | normal | The session has no epoch record. Expected on a new session's first request. The store also reports a record file it cannot open or parse, or one of an unknown version or with a field missing or of the wrong type, as no record. So a damaged `epoch.json` normally reads as `no_record`. Suspect that when `no_record` comes in a run that follows one whose main conversation had a turn start, where a record is normally written. |
| `other_conversation` | normal | The record belongs to another conversation, as at a subagent's or a side request's first request after a restart. The record stays available for its own conversation. |
| `dialect_changed`, `model_changed`, `settings_changed`, `head_changed` | notice | The session was resumed with the other CLI, another model, other engine settings (`--bound`, for example), or a changed system prompt or tool list. |
| `preamble_changed` | notice | The recorded pinned state and page index no longer give the recorded preamble, as after an upgrade that changed how the preamble is written. |
| `history_shorter`, `history_diverged` | notice | The CLI's history is not the recorded one: it is shorter, or differs before the recorded point (a rewind, a compaction, another conversation with the same first prompt). |
| `unreadable` | problem | The store raised an error it does not handle while reading the record (a file nested too deeply to parse, for example), or it passed the engine a record the engine cannot use. Rare with the session store, which reports almost every damaged record as `no_record`. |
| `pages_missing` | problem | The store holds fewer pages than the record says were paged, or cannot list them. Page files were removed, lost or quarantined, so the recorded epoch's page index could not be served. |

- `epoch`: 0.
- `reason`: one of the values above.

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

Another conversation became the session's main one. A conversation that becomes main by resuming
its recorded epoch has [`epoch_resumed`](#epoch_resumed) and not this event.

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

### `hint_mismatch`

Writer: engine. Where: `transform_events`. Kind: notice.

A request's hint header disagrees with what the engine sees in the request itself (D70). Logged
once per run for each distinct header, value and signature. The engine follows the header; this says
that a Claude Code update may have changed what a class of request looks like.

- `header`: `request_class` or `context_compacted`.
- `value`: the header's value, from its fixed vocabulary.
- `signature`: what disagreed: `agent_id` (a main-conversation class with an agent id), `no_tool_list`
  (a main-conversation class with no tool list), `no_agent_id` (a subagent class with no agent id and
  no parent session), `tool_list` (an auxiliary request with a tool list), or `compaction_summary` (a
  compaction hint with no compaction summary in the messages).

### `in_turn_batch`

Writer: engine. Where: `transform_events`. Kind: normal.

In-turn paging ([in-turn-paging.md](in-turn-paging.md), D63) made room inside a tool loop: one batch
of stubbed tool results, folded tool rounds and paged earlier turns, on this request. The request
is rewritten from its first changed message on. When the batch paged the earlier turns of the
window, their [`page_out`](#page_out) and [`rotation`](#rotation) (reason `in_turn`) come before it
in the same `transform_events`. Claude Code (the Messages API) only.

- `epoch`: the current epoch, after this batch's rotation if it had one.
- `trigger`: `result` (the batch only stubbed results the model had not read yet, because the
  request could not be sent with them), `bound` (the estimate, with the fresh results' `margin`, was
  over the bound), or `room` (it was past the line in-turn paging holds requests under, and not
  over the bound; see [in-turn-paging.md](in-turn-paging.md#when-it-runs)).
- `results_stubbed`: how many tool results were replaced by stubs, the two kinds below included.
- `own_results_stubbed`: of those, results of Contexto's own `contexto_search`, `contexto_recall`
  and `contexto_state`, which are stubbed without a page.
- `unseen_results_stubbed`: of those, results the model had not read yet: ones the request could
  not be sent with, and, when the request was still over the bound after every other step, the
  unread results, largest first.
- `documents_over_bound`: of the results stubbed unread because the request could not be sent with
  them, how many hold a document. A document is stubbed unread only when it is over the bound on its
  own.
- `rounds_folded`: how many tool rounds of the running turn were folded into a page.
- `turns_paged`: how many earlier turns were paged: the `turns` of this batch's `page_out`.
- `pages`: one for each stubbed result that is not Contexto's own, one for the folded rounds, and
  the `pages` of this batch's `page_out`.
- `estimate_before`: the request's estimate before the batch, in tokens, without `margin`.
- `estimate_after`: the estimate of what is sent after the batch, in tokens, without `margin`.
- `margin`: what the batch added to the estimate for the tool results that arrived with this
  request, in tokens: until usage has measured them, they count at 1.8 bytes per token.
- `first_changed`: the index, in the CLI's message list, of the first message the batch changed.
- `resent_tokens`: the estimated tokens of what is sent from `first_changed` on, or of everything
  sent when the batch changed the first message still sent.
- `messages`: how many messages the CLI's request had.

### `in_turn_exhausted`

Writer: engine. Where: `transform_events`. Kind: problem.

Inside a tool loop, the request is over the bound by the estimate, and in-turn paging found nothing
it could page, or the batch on this request left it over the bound. The request is sent as it
stands. The count the estimate gives a PDF whose pages are not certain (D60), until usage has
measured it, is left out of this comparison.

- `epoch`: the current epoch.
- `estimate`: the request's estimate, in tokens, after this request's batch if one ran.
- `bound`: the bound.

### `start_text`

Writer: engine. Where: `transform_events`. Kind: normal.

Project memory ([project-memory.md](project-memory.md), D69): the first request of a new main
conversation, at what the engine took for the start of a session, sends records carried from
earlier sessions of its project. They go with the first turn-start message until the
conversation's first rotation; after it, the preamble's pinned state holds the same records. Absent
when the session carries nothing.

- `epoch`: 0.
- `tokens`: the estimated size of the carried records sent, within the pin budget, in tokens.

### `start_text_changed`

Writer: engine. Where: `transform_events`. Kind: normal.

The carried records the main conversation sends changed, and this request sends the new text, which
costs one cache miss. The engine reads them again at each turn start of the main conversation while
it carries records and no turn has been paged (`k` is 0), and at the request that resumed it from an
epoch record. A record carried was closed, replaced or forgotten, project memory was turned off, or
a conflict mark changed. A record is never added this way except by a late confirmation.

- `epoch`: the current epoch.
- `tokens`: the estimated size of the new text, in tokens; 0 when nothing is carried any more.

### `start_text_unavailable`

Writer: engine. Where: `transform_events`. Kind: problem.

The carried records could not be read. At the start of a session it is the session's own ledger
that failed (a failure in the project is a [`project_unavailable`](#project_unavailable) line
instead, with nothing carried), and the conversation carries nothing. At a later read (see
[`start_text_changed`](#start_text_changed)) it is the session's own ledger or the project: a sessions
directory that cannot be listed, anything reading the project raised, or a session of the project
whose files may read next time. The request then keeps sending what it sent before. Neither fails
the request or enters degraded mode.

- `epoch`: 0 at the start of a session; the current epoch at a later read.
- `error`: the exception's class name.

### `trusted_conversations_unavailable`

Writer: engine. Where: `transform_events`. Kind: problem.

The engine could not add the conversation it trusts as main to the session's `trusted_conversations`
in `meta.json` (D69). A later engine on this session (after a restart, for example) then trusts
that conversation only by its other rules, so its pins and closures may not be taken as the main
conversation's.

- `error`: the exception's class name.

### `main_records_unavailable`

Writer: engine. Where: `transform_events`. Kind: problem.

The engine could not read the main conversation's new messages for its own pins and closures, or
could not record the ids it found in the session's `main_records` (D69). Only records listed there
can be carried to later sessions. The messages are read again at the conversation's next request.

- `error`: the exception's class name.

### `main_record_unmatched`

Writer: engine. Where: `transform_events`. Kind: notice.

In the main conversation's history, a `contexto_pin` or `contexto_close` call has a result that is
not the MCP server's success reply, so no record id is taken from it: whatever it recorded is not
carried to later sessions. Each engine logs a call once.

- `tool`: `contexto_pin` or `contexto_close`.
- `reason`: `is_error` (the result is marked as an error), `reply` (its text is not a success
  reply), `shape` (its content is neither a string nor a list), `parts` (a list without exactly one
  part) or `part_type` (its one part is not text).

## Launcher events

`contexto run claude` and `contexto run codex` write these as their own lines.

### `launch`

Writer: launcher (Claude), launcher (Codex). Where: own line. Kind: normal.

The proxy is up and the CLI is about to start.

- `cli`: `claude` or `codex`.
- `session`: the session id. A new session gets 32 random hex characters; with `--session` it is
  the name the user gave.
- `config`: the engine settings given on the command line: `bound`, `window_fraction` and
  `headroom_floor` when set, and `in_turn_paging: false` when in-turn paging is off.
- `engine`: `rewrite`, or `identity` when the paging engine could not be loaded.
- `upstream_source`: as in [`proxy_start`](#proxy_start).
- `observe_thinking`: whether `--observe-thinking` is on.
- `user_arg_count`: how many arguments were passed through to the CLI. The arguments are not logged.
- `affinity_header`: whether the proxy adds a session-affinity header to requests.
- `hint_headers`: Claude Code only: what the launch does with Claude Code's hint headers (D70):
  `stripped` (turned on for the engine and removed before forwarding), `forwarded` (you had them on,
  so they are sent upstream), or `off` (you turned them off).
- `statusline`: Claude Code only: whether the launch added Contexto's status line (D64,
  [statusline.md](statusline.md)): `added`, or why not: `off:flag` (`--no-statusline`), `off:env`
  (`CONTEXTO_NO_STATUSLINE`), `own:<kind>` (a settings file has a `statusLine`),
  `unreadable:<kind>` (a settings file could not be read as a JSON object), `hooks_off:<kind>` (a
  settings file sets `disableAllHooks`), `managed_hooks_only`, `unsafe_path` (the command would hold
  `${`) or `error` (deciding failed). `<kind>` is the highest-ranking such file: `managed`,
  `main-checkout-local`, `local`, `project` or `user`.
- `codex_auth`: Codex only: the `--codex-auth` mode.
- `no_daemon`: Codex only: whether Codex was started with `--no-daemon`, so it leaves no background
  server running.
- `codex_version_error`: Codex only, when the Codex version could not be read: why, in the values of
  [`cli_version_check`](#cli_version_check)'s `error`.

### `project_memory`

Writer: launcher (Claude), launcher (Codex). Where: own line. Kind: normal, problem.

What project memory ([project-memory.md](project-memory.md), D69) gives this session at launch, in
counts. The project's directory is never logged; when the project has an earlier session, the
launcher prints a line that names it. The kind is `normal` with the counts, and `problem` when the
line has `error` in their place: the counts could not be read, and no line is printed. The launch
goes on either way.

- `on`: whether project memory is on for this session: false when the session records
  `project_memory: false`.
- `earlier`: how many earlier sessions its project has.
- `carried`: how many records it carries from them. The first request sends those that fit the pin
  budget, so it can send fewer.
- `carried_sessions`: how many sessions those records come from.
- `skipped`: how many of the project's other sessions, earlier or later, were skipped because their
  ledger could not be read.
- `unreadable`: how many session directories, of any project, were left out because their
  `meta.json` could not be read.
- `error`: in place of every field above: the exception's class name.

The counts are 0 when project memory is off or the session is in no project.

### `cli_version_check`

Writer: launcher (Claude), launcher (Codex). Where: own line. Kind: normal.

The launcher ran `<cli> --version` before starting the CLI. When the version is not a tested one it
prints a warning.

The line has one of three shapes: `cli` and `skipped` when the check was turned off; `cli` and
`error: "interrupted"` when Ctrl+C ended the check; otherwise `cli`, `version`, `tested` and
`tested_versions`, with `error` when the version could not be read.

- `cli`: `claude` or `codex`.
- `version`: the installed version, or null when it could not be read. Absent, not null, on a
  skipped or interrupted check.
- `tested`: whether this Contexto release was tested with it; null without a version.
- `tested_versions`: the tested versions, as one comma-separated string.
- `error`: why the version could not be read (`not_found`, `timeout`, `unparsed`, `exit_<code>`
  or an exception's class name), or `interrupted` when Ctrl+C ended the check; the CLI is then not
  started, and the run's `exit` has code 130.
- `skipped`: true when the check was turned off.

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

- `code`: the exit code `contexto run` returns. Normally the CLI's own, which is 128 plus the signal
  number when a signal killed the CLI. 127 when the CLI could not be started (its `#!` interpreter
  is missing, for example). 130 for Ctrl+C before the CLI started, 128 plus the signal number when the
  launcher was terminated, and 1 or 2 when the launch failed before the CLI ran.

### `engine_unavailable`

Writer: launcher (Claude), launcher (Codex). Where: own line. Kind: problem.

The paging engine could not be loaded, so the proxy forwards every request unchanged and nothing is
bounded. It means a broken install.

- `detail`: a fixed sentence saying so.

### `affinity_salt_unavailable`

Writer: launcher (Claude), launcher (Codex). Where: own line. Kind: notice.

The install's affinity salt could not be read or created, so requests go out without the
`X-Session-Affinity` header. Only an upstream that routes by that header is affected (see
[cliproxyapi.md](cliproxyapi.md)). `contexto proxy` writes this event too, and so does `contexto
serve`, before a session's [`serve_open`](#serve_open).

- `error`: the exception's class name.

### `watchdog_unavailable`

Writer: launcher (Claude). Where: own line. Kind: notice.

The helper process that cleans up if the launcher is killed could not be started. The session runs
normally; only a killed launcher would leave Claude Code running and its temporary files behind.

- `error`: the exception's class name.

## Service events

`contexto serve` ([serve.md](serve.md)) writes these two in the log of each session it serves, as
their own lines. Everything else it logs goes to its own `serve.log`, not to a session's log.

### `serve_open`

Writer: service. Where: own line. Kind: normal.

The service opened the session for a Claude Code session: its first request since the service
started, or since the service last closed it. The service's proxy and engine for the session are
ready. This is the line to find an open by, as `proxy_start` is for a run.

- `cli`: `claude`.
- `session`: the Contexto session id.
- `created`: true when the service made the session now. A session it makes is kept out of project
  memory (it records `project_memory: false`). False when the session existed: one the service or
  `contexto run claude` made.
- `config`: the engine settings, as in [`launch`](#launch), from `contexto serve`'s command line.
- `engine`: `rewrite`, or `identity` when the paging engine could not be loaded.
- `upstream_source`: as in [`proxy_start`](#proxy_start).
- `observe_thinking`: whether `--observe-thinking` is on.
- `affinity_header`: whether the proxy adds a session-affinity header to requests.

### `serve_close`

Writer: service. Where: own line. Kind: normal.

The service closed the session. Nothing is lost: the session's next request opens it again, with a
new [`serve_open`](#serve_open).

- `reason`: `idle` (no request for `--idle-minutes`), `cap` (one more session was needed than
  `--max-sessions` allows, and this one was the least recently used without a request in flight),
  or `shutdown` (the service stopped).
- `requests`: how many requests the service routed to the session since it opened it.

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
- `ok`: whether the call succeeded. A failed call has only `tool`, `ok` and `error`, and `file` with
  `LockFileDamagedError`. Every field below is on successful calls only.
- `error`: on a failed call: the exception's class name. `ToolError` is a call the server refused
  with a message to the model.
- `file`: with `LockFileDamagedError`: the damaged lock file's name.

`contexto_recall`, `contexto_search` and `contexto_state`, which are charged against the turn's room.
`turn_budget` is on every successful call of the three. The next five fields are there only when a
room was open for the call, that is when `turn_budget` is not `unbudgeted`:

- `turn_budget`: `charged` (the reply was charged), `spent` (no room left, so the reply only says
  so), `nearly_spent` (recall: some room is left but too little for a useful chunk), `control` (a
  fixed reply such as "no match", never charged), or `unbudgeted` (no room is published, so only
  the per-call limit applied; with a running engine a
  [`turn_room_missing`](#turn_room_missing) line comes just before this one).
- `charged_tokens`: the tokens charged for this reply: 0 unless `turn_budget` is `charged`. Absent
  when `unbudgeted`.
- `room_left`: the room left after it, in tokens. Absent when `unbudgeted`.
- `limited_by`: `main` or `side`: whether the main conversation's room or a subagent's was the
  smaller one, which is the one that bounds the call. Absent when `unbudgeted`.
- `side_rooms`: how many subagent rooms are open. Absent when `unbudgeted`.
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
- `approximate`: whether the reply showed approximate matches (D65): no page held every word.
- `approximate_steps`: with `approximate`: which steps of the fallback the pages shown in detail
  needed, in this order: `stop_words`, `long_query`, `fold`, `typo`, `any_word`; `punctuation` alone
  when only the punctuation at the words' ends was dropped. Never the words.

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

### `project_session_skipped`

Writer: store. Where: own line. Kind: problem.

Project memory (D69): another session of this session's project was skipped whole because its
ledger could not be read: it is unreadable, damaged, or over the size limit. None of its records
count for this session, carried or not. Logged once per process for each skipped session. The
skipped session is not named.

- `error`: the exception's class name.

### `project_unavailable`

Writer: store. Where: own line. Kind: problem.

Project memory (D69): reading the session's project failed, so the store went on as if the session
had no project: nothing is carried from the earlier sessions, and their closures and replacements
do not apply. The session's own records are read as usual. A store opened read-only does not log
it.

- `error`: the exception's class name.
