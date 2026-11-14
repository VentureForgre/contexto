# The `Dialect` contract: adapting the engine to another client

The rewrite engine (`src/contexto/rewrite.py`) is written once. It reaches each model API through a
`Dialect`: the base class `Dialect` in `src/contexto/rewrite.py`, which is the Anthropic Messages API
behaviour (instance `ANTHROPIC`, for Claude Code), and `ResponsesDialect` in `src/contexto/codex.py`,
the OpenAI Responses API (instance `RESPONSES`, for Codex). This page says what each attribute and
method must return and what the engine does with the result, for someone adding a third client.

It describes the code as of v0.5.24. Nothing was refactored for it: the contract is what the two
implementations and their call sites do today, and section 5 lists where the engine still reads the
Messages API directly. Where the code does not settle a requirement, the page says "not specified by
the code".

References: rule numbers and "Terms" are those of the "Rewrite engine (normative)" section of
`docs/milestones/M2-contexto-proxy.md`; D numbers are entries in `docs/DECISIONS.md`. A5 and A14 are
lane A packet names the code cites. They have no entry in `docs/DECISIONS.md`: A5 is the Codex
adapter (`docs/research/M2-codex-adapter.md`) and A14 the retained-window snapshot (contract PR #125,
described in the module docstring of `src/contexto/rewrite.py`).

Two words used below: an **item** is one element of the request's history list (a Messages API
message, a Responses API item), and a **block** is one element of an item's content (a Messages
content block, a Responses part).

## 1. What a dialect is and is not

**What is protocol-neutral.** The engine keeps one `EpochState` per conversation and decides
everything about it without knowing the API: history matching and resync (`_rewrite`,
`matching_prefix`), when and where to rotate and how large the window is (`send`,
`rotate_at_turn_start`, `choose_k`, `new_epoch`, `fixed_overhead`, `headroom`), degraded mode
(`guarded_rotation`), the bounded fallback for carried content (`page_carried_text`), the measured
bytes-per-token ratio (`RewriteEngine._observe_usage`, `text_usage`), the recall rooms and the
retained snapshot (`RewriteEngine._publish_room`, `_publish_side_room`, `_publish_retained`), the
epoch record and resume (`epoch_record`, `resume_state`), and which conversation is main
(`RewriteEngine.publishes`). These functions work on a list of dict items and ask the dialect about
each item. Section 5 lists the places where they are not neutral yet.

**What the dialect supplies.** The request key that holds the history; what kind of item each one is
(turn start, model output, injected context); the class of an injected-context item; the form of an
item that is hashed; what the head of the request is and how to size it; how to remove thinking; how
to put carried blocks and the preamble into the first retained item; how to estimate tokens; how to
read the input total from a usage object; which tool-call ids an item answers; which parts of an item
are searchable; and whether the client's session start was recognised.

**What a dialect is not.** It does not route endpoints, read responses or streams, launch the client,
register the MCP server, or render stored pages. Those live in `src/contexto/proxy.py`, the
launchers, `src/contexto/toolcheck.py`, `src/contexto/cliversions.py` and `src/contexto/store/`, and
most of them branch two ways on the API (section 4). There is no registry of dialects:
`dialect_for` names the two it knows.

**Where it is chosen.** `dialect_for(endpoint)` in `src/contexto/rewrite.py` returns `ANTHROPIC` for
`/v1/messages` and `/v1/messages/count_tokens`, `RESPONSES` for any endpoint ending in `/responses`
(imported lazily, because `contexto.codex` imports the engine), and `None` for anything else, which
the engine passes through unchanged. The proxy calls the engine only for the paths in
`TRANSFORMED_PATHS` (`src/contexto/proxy.py`), with the request path as the endpoint.
`RewriteEngine._call` and `RewriteEngine._observe_usage` call `dialect_for` on every request and pass
the dialect on explicitly. `rewrite`, `count_tokens_view`, `keyed` and a few engine methods also set
the `ContextVar` `_DIALECT` for the duration of the call, and the module-level helpers
(`is_turn_start`, `message_hash`, `classify_system`, `estimate_items` and the others) read it through
`_dialect()`, which falls back to `ANTHROPIC` when nothing is set. `keyed(dialect, request)` builds
the key the engine stores a conversation's state under, `"<name>:<conversation key>"`.

## 2. The contract

`Dialect` declares 3 class attributes, 18 methods and 1 property, listed here in the order the class
declares them. `ResponsesDialect` overrides every one of them.

| Member | Kind | `ResponsesDialect` overrides it |
|---|---|---|
| `name` | attribute | yes |
| `field` | attribute | yes |
| `media_floor` | attribute | yes |
| `is_context_block` | method | yes |
| `is_turn_start` | method | yes |
| `is_assistant` | method | yes |
| `is_system_like` | method | yes |
| `result_ids` | method | yes |
| `retained_blocks` | method | yes |
| `classify_system` | method | yes |
| `normalise` | method | yes |
| `head_fingerprint` | method | yes |
| `head_estimate_source` | method | yes |
| `strip_thinking` | method | yes |
| `merge_first` | method | yes |
| `countable` | method | yes |
| `estimate_items` | method | yes |
| `lead_count` | method | yes |
| `usage_tokens` | method | yes |
| `judges_session_start` | method | yes |
| `session_start_seen` | method | yes |
| `session_start_version` | property | yes |

`tests/test_dialect_doc.py` fails when a public name of `Dialect` has no entry below or no row in
this table, and when the last column disagrees with what `ResponsesDialect` defines.

Every method that takes an item assumes the item is a dict. Neither implementation checks: a
non-dict item raises `AttributeError` in the first predicate that reads it, the exception leaves the
engine, and the proxy logs `transform_error` and forwards the client's request unchanged (rule 14;
`_Handler._transform` in `src/contexto/proxy.py`).

### `name`

`name: str`. `"anthropic"` on the base class, `"responses"` on `ResponsesDialect`.

It must be unique among dialects and must not change between releases.

- `keyed` prefixes each conversation's state key with it, so two APIs on one proxy never share an
  `EpochState`.
- `epoch_record` writes it as the record's `dialect`, and the record's `conversation` is the keyed
  key. A renamed dialect resumes no existing session: `RewriteEngine._resume` finds another key
  (`epoch_not_resumed`, reason `other_conversation`), and `resume_state` also compares the record's
  `dialect` (reason `dialect_changed`, rule 16).
- It is logged as `dialect` in `session_start_unrecognised` (`ClientVersions.observe`) and in the
  tool check's events (`ToolCheck` in `src/contexto/toolcheck.py`).
- Several places compare it with `"anthropic"` or `"responses"` by value (section 5). A third name
  takes the non-Anthropic branch in the engine: no Claude Code session tracking (D50), no early
  close of side rooms and no first prompt recorded for a side room (D40). `ToolCheck.observe` returns
  no events for a name outside `toolcheck.DIALECTS`, and `cliversions.DIALECT_CLI` has no CLI for it.

### `field`

`field: str`. `"messages"` on the base class, `"input"` on `ResponsesDialect`.

The key of the request dict that holds the history list. The engine reads the history as
`request.get(dialect.field) or []` and writes the upstream request as
`{**request, field: transform_messages(...)}` (`send`), so every other key of the request passes
through unchanged and in its original order (rule 1).

- A missing key, `None` or an empty list is an empty history: no turn start, so no rotation.
- The value must be a list of dicts. The Responses API also accepts a plain string as `input`; the
  engine does not handle that (it raises, and the request is forwarded unchanged as described
  above). The Codex shapes this was measured on always send a list (module docstring of
  `src/contexto/codex.py`).

### `media_floor`

`media_floor: bool`. `True` on the base class, `False` on `ResponsesDialect`. D48.

Whether the least cost Contexto computes for an image (`MediaCost.floor`, from `tokens.media_cost`)
is at or under what this API charges for it. `sent_media` reads it: when it is `False`, the floor of
the request's media is replaced by 0.

The floor is what the ratio takes off real usage for media. Before a ratio is measured, `text_usage`
charges media their floor; once there is one, it skips a request whose usage leaves the media less
than their floor. `measured_head_tokens` counts the window's media at their floor (rule 4). A floor
above the real cost would book the difference as sparse text and raise the ratio, so text would be
under-estimated.

- Anthropic: `True`, "for Claude's pixel prices" (the comment on the attribute).
- Responses: `False`, because "OpenAI prices images by tiles (85 tokens at low detail), under
  Claude's floor".

### `is_context_block`

`is_context_block(self, block: Any) -> bool`

Must return `True` for a block the client injected into a user item and the user did not write, and
`False` for everything else, including values that are not dicts: `_carried_at` passes it every
element of an item's content as it is.

On a turn-start item, every block for which it is true is carried content (Terms; D32):

- `strip_carried` leaves it out of the turn's page;
- `carried_blocks` and `carried_content` collect those of paged turn starts, and `transform_messages`
  hands them to `merge_first`, which puts them ahead of the first retained turn start (rule 1);
- `carried_tokens` counts them in the fixed overhead `F` (rule 4) and in carried growth (rule 13);
- `fallback_candidates` may page those of every turn start after the first (D32).

Those functions then treat the block as a Messages API text block: `context_block_kind` and
`_first_line` read its string `text`, and `carried_tokens` and `_block_page` wrap blocks as
`{"role": "user", "content": [...]}` to estimate and to page them.

- Anthropic (`_anthropic_is_context_block`): a `text` block whose text, after leading whitespace,
  starts with `<system-reminder>`. `_anthropic_is_turn_start` and the base `retained_blocks` use it
  too.
- Responses: always `False`, because "Codex sends context as separate messages, not inside the
  prompt message". Those messages are system-like items instead (`is_system_like`). So the Responses
  dialect never has carried blocks, and the block paths above are exercised by the Messages API only.

### `is_turn_start`

`is_turn_start(self, item: dict) -> bool`

Must return `True` for an item that starts a user turn: a user item with content the user wrote and
no tool result (Terms). It must return `False` for tool results, model output, system-like items and
item types it does not recognise. It must depend on the item alone: `_turn_starts` caches the result
for the request's list, per dialect and list length, inside a `request_scope`.

- Rules 3 and 5: `send` considers a rotation only when the request adds items and `at_turn_start`
  holds (no `is_assistant` item after the newest turn start). An implementation that returns `True`
  for a tool-result item makes the engine rotate inside a tool loop.
- Rule 4: `choose_k` and `rotate_at_turn_start` set `k` only to a turn-start index, and `new_epoch`
  cuts the paged items at turn starts, one page per turn.
- `turn_id` is the number of turn starts, as a string. It names the turn for the per-turn recall
  budget (`set_turn_room`), and `retained_entries` numbers turns the same way.
- `conversation_key` uses the first turn start when `lead_count` is not 0.
- `RewriteEngine._main_after` compares turn-start counts to decide which conversation is main
  (contract PR #125, D50). `RewriteEngine._publish_side_room` continues a side room only for a
  request that adds no turn start, and records the count as the room's `runs` (D40).
- `send` measures per-turn growth for the headroom between consecutive turn starts (rule 17).
- `carried_blocks`, `strip_carried` and `fallback_candidates` look for context blocks in turn starts.

Implementations:

- Anthropic (`_anthropic_is_turn_start`): role `user`, and either a string content that is not blank,
  or a content list with no `tool_result` block and at least one dict block that is not a context
  block (text, an image, a document).
- Responses: a `message` item (its `type` is `message` or absent) with role `user` and at least one
  dict part that is not a context section. A context section is a text part matching
  `CONTEXT_SECTION` in `src/contexto/codex.py` (six tags and the `# AGENTS.md instructions for`
  header, measured on codex-cli). A user message made only of context sections is system-like, and a
  prompt that merely starts with another tag stays a turn start.

### `is_assistant`

`is_assistant(self, item: dict) -> bool`

Must return `True` for an item the model produced. Only `at_turn_start` and
`RewriteEngine._main_after` call it, for one question: does the newest turn hold model output yet
(rule 3)? An item it does not recognise counts as not model output, so such an item after a new
prompt does not stop a rotation.

- Anthropic: role `assistant`.
- Responses: an item whose `type` is in `MODEL_OUTPUT_TYPES` (`reasoning`, `function_call`,
  `custom_tool_call`, `local_shell_call`, `web_search_call`, `image_generation_call`,
  `tool_search_call`), or a `message` with role `assistant`. The Responses API returns model output
  as items of several types, where the Messages API has one assistant message.

### `is_system_like`

`is_system_like(self, item: dict) -> bool`

Must return `True` for an item that is injected context standing as its own item: not a prompt, not
model output, not a tool result. An item it returns `False` for, and that is no turn start, belongs
to the turn it sits in: it is paged with that turn and counted in the window.

- `_system_classes` calls `classify_system` only for system-like items, and `carried_systems` decides
  from the class whether the item is carried.
- `strip_carried` leaves every system-like item out of a page unless its class is `hook_output`. So
  `window_items` and `window_tokens` do not count them in the window; they count in `F` through
  `carried_tokens`.
- `retained_entries` skips them (A14), and `send` counts the `carried_text` ones for the
  `carried_text_growing` warning.
- `as_message` in `src/contexto/store/render.py` calls `RESPONSES.is_system_like` to leave them out
  of rendered pages.

Whether an item may be both a turn start and system-like is not specified by the code. Both
implementations make the two exclusive.

- Anthropic: role `system`.
- Responses: an `additional_tools` item; a `message` with role `developer` or `system`; or a user
  `message` with at least one part, all of them context sections.

### `result_ids`

`result_ids(self, item: dict) -> list[str]`. D40.

Must return the ids of the tool calls this item carries results for, as strings, and an empty list
for any other item.

Only `RewriteEngine._publish_side_room` calls it. A side room recorded with tool-call ids is
continued only by a request in which an item after that room's request returns a result for one of
those ids. The recorded ids come from the response, through `UsageReader._take_call_id` in
`src/contexto/proxy.py`, so the two must name the same id.

- Anthropic: on a role `user` message, the `tool_use_id` of each `tool_result` block. The response
  side records each `tool_use` block's `id`.
- Responses: on an item whose `type` ends in `_output`, its `call_id`, else its `id`. The response
  side records a client call item's `call_id`, else its `id`.

### `retained_blocks`

`retained_blocks(self, item: dict) -> list[tuple[str, dict]]`. A14.

Must return the item's searchable content as `(role, block)` pairs in order, where each block is a
Messages API block of type `text`, `tool_use` (`name`, `input`) or `tool_result` (`content`: a string
or a list of text blocks). It returns an empty list for an item with nothing searchable (thinking,
images, item types it does not recognise).

- `retained_entries` renders each block with `render_page` (`src/contexto/store/render.py`, the
  renderer recall uses) into one `{turn, message_index, role, text}` entry, and
  `RewriteEngine._publish_retained` writes the entries with `set_retained` (contract PR #125) so that
  `contexto_search` can list in-view lines. It keeps a block only when its role is one of
  `RETAINED_ROLES` (`user`, `assistant`, `tool_result`; a `tool_result` block's role is replaced by
  `tool_result`) and the rendered text is not empty. It skips system-like items and Contexto's own
  search, recall and state calls (`is_own_item` per item, `is_own_block` per block; D51).
- For the Responses API there is a second caller: `as_message` in `src/contexto/store/render.py` maps
  each stored item through `RESPONSES.retained_blocks`, so a page is rendered, searched and stubbed
  through the same mapping as the in-view snapshot.

Implementations:

- Anthropic: a string content gives one `text` block (none when the string is empty); a content list
  gives its dict blocks of type `text`, `tool_use` or `tool_result` that are not context blocks. The
  role is the item's `role`, `"unknown"` when absent.
- Responses: a `message` gives its text parts (`input_text`, `output_text`, `text`) that are not
  context sections, as `text` blocks; a `function_call` or `custom_tool_call` gives
  `("assistant", tool_use)` with the call's name and its arguments (`arguments` for a
  `function_call`, `input` for a `custom_tool_call`) parsed from JSON (kept as the string when they
  do not parse); a `function_call_output` or `custom_tool_call_output` gives
  `("user", tool_result)`; other types give nothing. It maps to Messages API blocks "so an in-view
  line reads like a paged one". Because it also reads page files a person may have edited, a part or
  output of the wrong type becomes `{"type": "text", "text": None}`, which `render_page` shows as
  `[malformed text omitted]`. The blocks it builds carry no ids, so Contexto's own calls are filtered
  at the item level (`is_own_item`).

### `classify_system`

`classify_system(self, items: list[dict], i: int) -> str`

Must return the class of the system-like item `items[i]`: one of the constants `SESSION_START`,
`SETTINGS`, `SETTINGS_CHANGE`, `HOOK_OUTPUT`, `CARRIED_TEXT`, `UNCLASSIFIED` in
`src/contexto/rewrite.py`. The engine calls it only for an `i` whose item is system-like. The result
must be deterministic and must not change as later items are appended: the carried set is recomputed
on every request (`transform_messages`, through `carried_content`), and the prefix must stay
byte-identical within an epoch. For an item it cannot place, the contract's choice is to carry:
"Carrying is the safe failure, since paging a message that must persist would lose state" (Terms).

What each class means to the engine (`carried_systems`, `protected_systems`, `fallback_candidates`,
`strip_carried`, `new_epoch`):

| Class | Effect |
|---|---|
| `session_start` | carried, never paged |
| `settings` | carried; only the latest per `settings_key` is kept (rules 12 and 13) |
| `settings_change` | carried; the fallback may page it unless it is the latest per `settings_fields` (D33) |
| `hook_output` | not carried: it stays in its turn's page and goes when the turn is paged |
| `carried_text` | carried; the fallback may page it unless it is the latest of a `mode_kind` (D32, D33) |
| `unclassified` | carried, never paged; `new_epoch` logs `unclassified_system_message` with the count |

`settings_key` and `settings_fields` read Messages API fields (role `system`, and any field other
than `role` and `content`). For an item of another shape both return `None`, so all `settings` items
share one key and only the last is carried, and one `settings_change` item is protected.

- Anthropic (`_anthropic_classify_system`): the contract's order (Terms, amended by D33): the
  session-start signature, settings-only, settings change, a hook-output signature, other text-only
  content, then anything else. The position `i` is not used.
- Responses: `session_start` for every system-like item of the lead (`i < lead_count(items)`),
  `carried_text` for a later text-only item, `unclassified` otherwise. It never returns `settings`,
  `settings_change` or `hook_output`. The module docstring gives the reason: "no per-turn Codex
  context shape is measured yet, and carrying is the contract's safe failure".

### `normalise`

`normalise(self, item: dict) -> dict`

Must return the form of the item that rule 10 hashes. `_message_hash` computes
`sha256(canonical_bytes(normalise(item)))`. The result must be serialisable by `canonical_bytes`, two
forms the client sends for the same item at different times must normalise to equal values, and the
method must not change the item. The result is hashed and nothing else: what is sent upstream is
built from the original items.

The hash (`message_hash`) is used for:

- history matching and resync (`matching_prefix`, `_rewrite`; rule 10) and `EpochState.hashes`;
- `usage_identity`, which pairs a response's usage with the request it measured (rule 3);
- `conversation_key`, so the state key;
- `anchor_hash` in `epoch_record` and `resume_state` (rule 16) and in a side room's lineage (D40);
- the identity of a paged system-like item in `paged_carried` (`carried_systems`,
  `fallback_candidates`);
- `appends_to`, which decides whether a request only appended to the last measured one (D48).

Anchor hashes and `paged_carried` are written to the epoch record. A change to a dialect's
`normalise` changes the conversation key, which hashes the first item, so a record written before it
usually fails to resume as `other_conversation`; when the key still matches, the anchor check fails
as `history_diverged` (`resume_state`). `paged_carried` is not compared on resume: items paged before
the change are carried again rather than blocking the resume.

- Anthropic (`_anthropic_normalise`): removes every `cache_control` key at any depth, and turns a
  content list of exactly one `{type: "text", text}` block into the plain string. Claude Code moves
  `cache_control` along the tail and converts one text form to the other along the history (rule 10).
  `_message_hash` has a fast path that applies only when the dialect is the `ANTHROPIC` instance.
- Responses: returns the item unchanged (the same object). The code gives no reason. The effect is
  that any change in an earlier item's JSON is a history mismatch.

### `head_fingerprint`

`head_fingerprint(self, request: dict) -> str`

Must return a string that changes exactly when the head changes: the part of the request that goes
ahead of the retained window and that the engine never rewrites.

`_rewrite` computes it on every request. `send` compares it with `EpochState.head_fingerprint`; a
difference logs `head_changed` and starts a new epoch at the next turn start (rule 6). `new_epoch`
stores it, it is part of the scope `appends_to` compares (D48), and `epoch_record` and `resume_state`
use it to refuse a resume after a head change (rule 16, reason `head_changed`).

- Anthropic (`_anthropic_head_fingerprint`): SHA-256 of the canonical JSON of the request's `system`
  and `tools`.
- Responses: SHA-256 of the canonical JSON of `instructions`, `tools` and the lead items
  (`items[:lead_count(items)]`). The lead stays first in every upstream request, so the adapter
  counts it as head (module docstring: "Head (rule 6): top-level `instructions` and `tools` plus the
  lead items").

Missing keys hash as `None`; both implementations accept any request dict.

### `head_estimate_source`

`head_estimate_source(self, request: dict) -> dict`

Must return the head as a JSON value, for any request dict (both implementations give `None` for a
missing key). The engine sizes its canonical JSON:

- `estimate_upstream` and `fixed_overhead` estimate the head's tokens from it while no head is
  measured (rules 3 and 4);
- `head_size` is its length in bytes, and `measured_head` adjusts a measured head by the change in
  that length after a head change (D49);
- `measured_head_tokens` uses its estimate as the lower limit of a head measured from a request with
  media in its window (D48).

Implementations:

- Anthropic: `{"system": ..., "tools": ...}`.
- Responses: `{"instructions": ..., "tools": ...}`, without the lead that `head_fingerprint`
  includes. The lead's tokens are counted elsewhere: its system-like items are class `session_start`,
  so `carried_tokens` counts them as carried content, and `estimate_upstream` counts them in the
  transformed item list. A change in the lead therefore changes the fingerprint and not `head_size`.
  Why the two methods differ is not specified by the code.

### `strip_thinking`

`strip_thinking(self, item: dict) -> dict | None`

Must return the item without the model's thinking: the same item when there is nothing to remove, a
new item (never the input changed in place) when there is, or `None` to drop the whole item. The
result must depend on the item alone, so the stripped prefix is byte-identical on every request
(rule 2).

- `transform_messages` applies it to each retained item that existed when the epoch was created
  (index below `EpochState.strip_upto`), on every request, and leaves out an item it returns `None`
  for.
- `window_items` applies it to every item before estimating the window (rule 4).

Implementations:

- Anthropic (`_anthropic_strip_thinking`): removes `thinking` and `redacted_thinking` blocks; a
  message left with no blocks gets one text block `[thinking omitted]`. It never returns `None`.
- Responses: `None` for a `reasoning` item, any other item as it is. The module docstring: "Dropping
  a reasoning item alone keeps call pairing valid; turns are paged whole, so a `function_call` and
  its output stay together."

### `merge_first`

`merge_first(self, first: dict, blocks: list, text: str) -> dict`

Must return a new item: `first` with `blocks`, and then one text block holding `text` when `text` is
not empty, ahead of its own content (rule 1). It must not change `first`, and must accept an empty
`blocks`, an empty `text` and a string content.

Only `transform_messages` calls it, and only once turns are paged (`k > 0`). `first` is the first
retained item, the turn start at `k`. `blocks` are the carried context blocks of paged turn starts,
in their original order and as the client sent them. `text` is the preamble (`compose_preamble`: the
recall knobs, then the store's pinned and index text). With `Config.preamble_first`, the default, it
is called twice, `merge_first(merge_first(first, blocks, ""), [], preamble)`, so the preamble ends up
ahead of the carried blocks. The method must therefore work on its own output.

- Anthropic: the text block is `{"type": "text", "text": ...}`; a string content becomes one text
  block. It raises `TypeError` when `first` has no `content`.
- Responses: the text part is `{"type": "input_text", "text": ...}` (module docstring: "an
  `input_text` part prepended to the first retained turn start"); a missing content counts as empty.
  `blocks` is always empty on this dialect.

### `countable`

`countable(self, item: dict) -> bool`

Whether `window_items` includes the item when it sizes the non-carried window (`window_tokens`,
rule 4, and the head measurement in `send`). It is called after `strip_carried` and before
`strip_thinking`. No other code calls it.

- Anthropic: `bool(item.get("content"))`, so a message with empty or missing content is left out.
- Responses: always `True`.

Why they differ is not specified by the code. What the code shows: the Anthropic estimate counts
content only (`tokens.measure_messages`), so an empty message adds nothing either way, and the
Responses estimate counts the whole item's JSON (`tokens.estimate_value_tokens`).

### `estimate_items`

`estimate_items(self, estimator: TokenEstimator, items: list[dict]) -> int`

Must return the estimated input tokens of `items`, and 0 for an empty list.

The module-level `estimate_items` forwards to it from `estimate_upstream` (rule 3), `window_tokens`
(rule 4), `carried_tokens` (rules 4 and 13), `page_carried_text` and `_nested_over_budget` (the
fallback), and `send` (per-turn growth, and the window of the first measured request). What those
call sites rely on:

- The estimate goes through the estimator it is given. `send` passes `BYTE_COUNT`, an estimator
  whose ratio is fixed at one byte a token, to get the window's bytes "as the estimator counts them".
- The list may be empty (`messages[previous_n:]` in `estimate_upstream`).
- An item's content may hold `None` where an image or document was: `send` passes
  `split_media(window)[0]`.
- Media are priced as media, not by their bytes (D48, D60). Both implementations get this from
  `src/contexto/tokens.py`.
- The items may be Messages API messages whatever the dialect: `carried_tokens` and `_block_page`
  build `{"role": "user", "content": [...]}` around carried context blocks. That happens only when
  `is_context_block` returned `True` for a block, so never on the Responses dialect.

Implementations:

- Anthropic: `estimator.estimate_messages(items)`. It counts content only; until a ratio is measured,
  `tool_result` content counts 1.5 bytes a token and other text 3.5 (rule 3).
- Responses: `estimator.estimate_items(items)`, or 0 for an empty list (on its own that method
  returns 1 for `[]`, the two bytes of its JSON). It counts each item's whole canonical JSON as text,
  and a tool output item (`type` ending in `_output`) at the tool-output seed until a ratio is
  measured (D61).

### `lead_count`

`lead_count(self, items: list[dict]) -> int`

Must return how many leading items stay first in every upstream request and are never paged; 0 for
an API that has none, and 0 for an empty list.

- `transform_messages` puts `items[:lead_count(items)]` ahead of the first retained item, and does
  not repeat carried system-like items that are in the lead.
- `conversation_key` hashes the first turn-start item when the count is not 0, and item 0 when it
  is. With a lead and no turn start the key is the empty string.

The count must not exceed the index of the first turn start. The engine sets `k` only to a turn-start
index and sends `[*lead, first retained item, carried system-like items, the rest]`; a lead reaching
past `k` would send those items twice.

- Anthropic: 0.
- Responses: the index of the first turn start, or the length of the list when there is none. The
  lead is what Codex sends before the user's first prompt: the `additional_tools` item, the base
  instructions, tagged developer context and the `<environment_context>` user message (module
  docstring). It is "the analogue of `system` and of the session-start message".

### `usage_tokens`

`usage_tokens(self, usage: dict) -> int`

Must return the total input tokens of the request the usage measured, cached tokens included, and 0
when the usage has no input count.

`RewriteEngine._observe_usage` calls it (through the module-level `usage_tokens`) on the usage dict
the proxy read from the response. A result of 0 or less is a response without usage: nothing is
measured or published, and the next request logs `usage_missing` (rule 3; D49 as amended). Otherwise
the value teaches the ratio (`estimator.observe`), becomes `last_input_tokens` for rule 3's next
estimate, measures the head once (rule 4), is checked against the bound (`over_bound`, source `real`)
and against the estimate (`estimate_error`), and sizes the room, `bound - (input + output) -
headroom_floor`. The engine reads the output count itself, as `usage.get("output_tokens")`.

The dict is what `UsageReader` in `src/contexto/proxy.py` collected: for the Messages API the four
keys of `USAGE_KEYS` with integer values, for the Responses API `response.usage` reduced to numbers
and nested dicts of numbers (`_numeric`).

- Anthropic: `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`, a missing or
  null count as 0.
- Responses: `input_tokens` alone, because "cached_tokens is a subset of input_tokens".

### `judges_session_start`

`judges_session_start(self, request: dict) -> bool`. D38.

Must return whether this request is one a session start can be judged on: a request of a real
conversation, not one of the client's side requests.

- `ClientVersions.observe` (`src/contexto/cliversions.py`) runs the session-start check on the first
  request for which this is true and `session_start_seen` is not `None`.
- `RewriteEngine._call` records the conversation's key as judged. Only a judged conversation takes
  over main by D50's rules (`RewriteEngine._takes_over`, `_compacts`), and a main that was never
  judged gives way to a judged conversation at its turn start (reason `after_side_request`).

Implementations:

- Anthropic: `bool(request.get("tools"))`. "Claude Code's title, topic, and quota side requests have
  no tool list and no session start."
- Responses: always `True`. "Codex's main requests carry no top-level tools in code mode; every
  request has the lead."

### `session_start_seen`

`session_start_seen(self, items: list[dict]) -> bool | None`. D38.

Must return `True` when the history holds a session start the dialect recognises; `False` when it
holds the client's injected context but no session start the dialect recognises, which is what a
client that changed its text would send; and `None` when it holds no injected context, so the
request cannot tell (another client, or an empty history).

Only `ClientVersions.observe` calls it, on each request the dialect judges until one gives an
answer. `False` logs one
`session_start_unrecognised` event (`dialect`, `signature_version`, `cli_version`,
`tested_versions`) to the session log; nothing is printed. `None` leaves the check open for at most
`MAX_UNJUDGED` (8) such requests. An exception here is caught in `RewriteEngine._call` and logged as
`cli_version_error`.

- Anthropic (`_anthropic_session_start_seen`): `True` for a role `system` message with
  `SESSION_START_SIGNATURE` (the `# Environment` first line, D33) or a context block of kind
  `environment`. It reads only the first 4,096 characters (`SESSION_START_SCAN_CHARS`) of each text
  block; a role
  `system` message is read in full. It skips items that are not dicts.
- Responses: `True` when the lead holds a user message made only of context sections
  (`<environment_context>` on the tested versions); `False` when the lead holds a developer or
  system message but no such user message ("a Codex that changed its section tags makes that
  message a turn start, and it would be paged"); `None` otherwise.

### `session_start_version`

`session_start_version` (read-only property) `-> str`

The client version the session-start recognition was measured on. `ClientVersions.observe` puts it in
`session_start_unrecognised` as `signature_version`.

- Anthropic: `SESSION_START_SIGNATURE_CLAUDE_CODE_VERSION` in `src/contexto/rewrite.py`.
- Responses: `CONTEXT_SECTIONS_CODEX_VERSION` in `src/contexto/codex.py`.

`tests/test_cliversions.py` requires both constants to equal the newest version of their CLI in
`cliversions.TESTED_VERSIONS`.

## 3. Invariants no single method states

- **Never change the request.** `RewriteEngine.preserves_input` is `True`, so the proxy skips its
  defensive copy and gives `observe_usage` the same parsed request the engine transformed
  (`_Handler._transform` in `src/contexto/proxy.py`). The engine also caches each item's hash by
  object identity for the length of one call (`request_scope`, `_RequestMemo`): "A message dict
  hashed inside a scope must not be changed afterwards in that scope". A dialect method that changes
  an item or the request in place breaks both. `strip_thinking` and `merge_first` return new items;
  both implementations build them with `{**item, ...}`.
- **Deterministic, from the arguments alone.** The module docstring of `src/contexto/rewrite.py`:
  `rewrite` "is deterministic, so the proxy's deterministic serializer sends byte-identical prefixes
  within an epoch". The prompt cache reads that prefix (acceptance 4). `strip_thinking`,
  `merge_first`, `is_context_block`, `is_system_like`, `classify_system` and `lead_count` all shape
  the prefix, so none may depend on time, on randomness, on earlier calls, or on anything but its
  arguments.
- **Stable as the history grows.** The engine re-derives turn starts, the lead and the carried set
  from the full history on every request (`transform_messages`). An item's answers must not change
  when items are appended after it: per-item predicates see only the item, and `classify_system` and
  `lead_count`, which see the list, must not let later items change an earlier result.
  `ResponsesDialect.lead_count` is fixed once the first turn start exists.
- **No per-request state on the instance.** `dialect_for` returns module-level instances
  (`ANTHROPIC`, `RESPONSES`) shared by every conversation, and `keyed` and `rewrite` can be called
  with the same instance for different conversations in turn. `_RequestMemo` and `_message_hash`
  compare dialects by identity (`is`), so use one instance per dialect.
- **Hashes outlive the process.** `normalise` output is only hashed, but the hashes are stored: the
  epoch record holds an anchor over the first `n` item hashes and the `paged_carried` set
  (`epoch_record`), and `name` is part of the conversation key. Changing `name`, `normalise` or what
  `conversation_key` hashes (`lead_count`, `is_turn_start`) in a release means sessions written by
  the previous release do not resume their epoch; they start again from epoch 0 (`resume_state`,
  `RewriteEngine._resume`).
- **Search speaks Messages API blocks.** `retained_blocks` is the only place a dialect's items become
  searchable text, and its output is rendered by `render_page`, which reads Messages API blocks. A
  line found in view and the same line in a page read alike only if the page renderer maps stored
  items the same way (section 4, "Stored pages").
- **Failures.** An exception from a dialect method inside the transform is not caught by the engine
  (the module docstring: "Any other exception propagates to the proxy as `transform_error`"). The
  proxy then forwards the client's own request, which is not bounded, and acceptance counts the event
  as a failure. In degraded mode `_rewrite` does the same itself and logs `fallback_failed`. In
  `observe_usage` the proxy logs `observe_usage_error` and drops the usage. The two diagnostics
  catch their own (`toolcheck_error`, `cli_version_error`).
- **The transform runs under a lock, so a dialect's work must be bounded.** The proxy serializes
  calls to the engine (`ContextoProxy.transform_lock`), and every dialect method runs inside it, some
  of them more than once per item on a request, while every other request waits. Keep each method
  linear in the size of the request, scan text only as far as needed (`SESSION_START_SCAN_CHARS`),
  use simple regular expressions, and do no file or network I/O. A slow step belongs in
  `RewriteEngine.prime`, which the proxy calls before taking the lock (it counts PDF pages there,
  D60). The request reaching a dialect is already parsed with `loads_bounded`, which refuses nesting
  deeper than `MAX_JSON_DEPTH` (512 levels, rule 18), but a dialect that recurses must still bound its
  own depth. Raising an exception to skip input the dialect does not understand is not a way out: it
  is a `transform_error`, which forwards the request unbounded and counts as a failure.
- **The session log is content-free.** Events a dialect method or the engine returns are written to
  `log.jsonl`, which SECURITY.md promises holds sizes, counts, hashes, event names and reasons, never
  prompts, replies, tool input or output, or header values. The proxy reduces events to scalars and
  caps their size (`_scalar_summary`) but does not filter their content, so an implementation must
  put no client text in an event. Log an exception by its type name only, as the engine does.
- **Credentials never reach a dialect.** The client's auth headers are forwarded upstream unchanged
  and never stored or logged (SECURITY.md). A dialect receives only the request body; engine code
  that reads headers names each header it reads (`claude_code_session`).
- **Tool-call ids come from the client and the upstream.** They are compared as opaque strings,
  never logged, stored in a file name or used in a path, and the proxy keeps at most 256 per response
  (`UsageReader._take_call_id`).

## 4. What else a new client needs

A dialect alone does nothing: `dialect_for` must return it, the proxy must read the API's responses,
and something must start the client behind the proxy with the MCP server registered. Each item says
whether Codex needed it.

The client must also fit the engine's model. It sends its whole history on every request (Terms:
"The CLI sends its full local history `M[0..n)` on every request"; for Codex, `store: false` and no
`previous_response_id`, module docstring of `src/contexto/codex.py`), over HTTP, to a base URL that
can be pointed at `127.0.0.1`, with a loopback `Host` and no `Origin` header (`_Handler._refusal`,
D53).

- [ ] **Endpoint routing in the proxy.** Add the API's paths to `TRANSFORMED_PATHS` and
  `USAGE_FEEDBACK_PATHS`, and give the log entry's `api` a third value (`_Handler._handle_request`
  sets it to `"responses"` or `"messages"`), in `src/contexto/proxy.py`. Add the dialect to
  `dialect_for` in `src/contexto/rewrite.py`. Codex: yes (`RESPONSES_PATHS`, the
  `endswith("/responses")` branch).
- [ ] **A token-count endpoint, if the client calls one.** Rule 11 gives `count_tokens` the current
  epoch's transform with no side effects. The engine knows one such endpoint, `COUNT_TOKENS`, and
  calls `count_tokens_view` without a dialect. Codex: no, it has none.
- [ ] **Usage from responses.** `UsageReader` in `src/contexto/proxy.py` has two `api` forms:
  `"messages"` reads `message_start` and `message_delta` usage, limited to `USAGE_KEYS`;
  `"responses"` reads `response.usage` from the end event or a JSON body. The dialect's
  `usage_tokens` must match what the reader keeps. The entry's logged `usage` has the same two-way
  branch (`_Handler._relay`). Codex: yes (`_take_responses_usage`).
- [ ] **Tool calls from responses (D40).** The proxy tells the engine whether the response holds a
  call the client runs, and the calls' ids: `UsageReader.tool_calls`, `UsageReader._take_call_id`,
  `StreamHold.client_calls`. The ids must be the ones `result_ids` returns later. Codex: yes
  (`is_client_call`, `HOSTED_CALL_TYPES` for calls that run on OpenAI's servers).
- [ ] **How a response ended.** `UsageReader` records end fields from allowlists
  (`END_ALLOWLISTS`), and `ended` when the stream's last event was read, which decides whether a
  client that closes early counts as aborted (D59). Codex: yes (`RESPONSE_END_EVENTS`,
  `_take_responses_end`).
- [ ] **The stream hold.** `StreamHold` in `src/contexto/proxy.py` holds every event from the first
  tool call until the event that carries the usage, so the room is published before the client can
  run a tool (contract PR #74). It needs three things per API: which event starts a tool call
  (`_call`), which event triggers the publish (`_is_trigger`), and which is an error (`_is_error`).
  Codex: yes (`response.output_item.added` with a `*_call` item; the events in
  `RESPONSE_END_EVENTS`).
- [ ] **Local error bodies.** `_Handler._reject_encoded` answers 415 in the API's own error format,
  with a branch for `"responses"`. `_Handler._send_local_error` always uses the Messages API format.
  Codex: yes, for the 415 only.
- [ ] **Transport the proxy can read.** The proxy refuses a WebSocket upgrade and decodes or refuses
  a compressed request body (rule 15). The launcher must keep the client on plain HTTP with
  compression off. Codex: yes (`supports_websockets = false` and
  `enable_request_compression = false` in `api_key_config` and `chatgpt_overrides`).
- [ ] **A launcher.** `contexto run <cli>` is the only path that wires the engine in:
  `src/contexto/commands/run.py` lists the CLIs in `choices` and dispatches, and the launcher shares
  `options_from_args`, `check_options`, `open_session`, `engine_config`, `start_proxy`,
  `check_cli_version`, `start_warning`, `run_child` and `report_missing_tools` from
  `src/contexto/launcher.py`. `start_proxy` builds the `RewriteEngine`, passes its `observe_usage` to
  the proxy and clears the previous launch's MCP record. Files a launcher writes for the client
  follow SECURITY.md: mode 0600 from the first byte, in a private directory, removed on exit
  (`write_private_json`, `Watchdog`). Codex: yes (`run_codex`, `plan_codex`,
  `add_arguments` in `src/contexto/codex.py`). `contexto proxy` (`src/contexto/commands/proxy.py`)
  starts the proxy alone with the identity transform, so no dialect runs under it.
- [ ] **Upstream and credentials.** Where the client's requests go and how it authenticates, without
  Contexto storing a credential. Codex: yes (`codex_upstream_with_source`, the key helper run through
  `/bin/sh -c`, D22, D52, D54). `VENDOR_HOSTS` in `src/contexto/proxy.py` lists the vendor API hosts
  that get no affinity header (D36).
- [ ] **MCP server registration.** The client must start the MCP server with the absolute path of
  the interpreter Contexto runs on (`sys.executable`) as `<python> -P -m contexto mcp --session=<id>`,
  with `CONTEXTO_HOME` set (`paths.mcp_env`). `-P` keeps the project directory, where the client
  starts its servers, off `sys.path`, so a project's own `contexto` package cannot run in its place;
  `--session=<id>` is one token, so an id starting with `-` is not read as an option. Every value
  written into the client's config must be quoted for that config's format, and refused when the
  client would expand it (Claude Code expands `${`: `check_mcp_values`, D55). Where the client runs
  unattended it must be allowed to call the tools without a prompt, and only Contexto's own tools,
  each by its exact name, never a client-wide approval mode; the user's own deny and ask rules must
  still win (`contexto_tool_rules`, D44). Claude Code: `mcp_payload`, `settings_payload` and
  `claude_argv` in
  `src/contexto/launcher.py` (D44). Codex: yes (`_mcp_server`, the `[mcp_servers.contexto]` table and
  `MCP_APPROVAL_MODE` in `src/contexto/codex.py`). The child also gets `CONTEXTO_SESSION` (D57).
- [ ] **The tool check.** `ToolCheck` in `src/contexto/toolcheck.py` reports a main conversation that
  runs without Contexto's tools. It returns nothing for a dialect name outside `DIALECTS`, and
  `_offered` has two branches: the request names the tools (Anthropic), or the MCP server recorded
  that it answered `tools/list` (`SessionStore.set_mcp_listed`, written by `_tools_list` in
  `src/contexto/mcp.py`). `missing_tools_message` words the warning per dialect. The refusal check
  (`_denied`, D44) is Messages API only. Codex: yes (`responses_tools_offered`, the `listed` record).
- [ ] **Session-start recognition and the version table.** Implement `judges_session_start`,
  `session_start_seen` and `session_start_version`; add the client to `TESTED_VERSIONS`, `CLI_NAMES`,
  `AT_RISK` and `DIALECT_CLI`, and its `User-Agent` to `client_version`, in
  `src/contexto/cliversions.py`; keep a measured-on constant next to each text signature and add it
  to `tests/test_cliversions.py`; add a routine to `docs/cli-updates.md`. Codex: yes
  (`CONTEXT_SECTION`, `CONTEXT_SECTIONS_CODEX_VERSION`, D38).
- [ ] **Stored pages.** Pages hold the client's items exactly as sent (`strip_carried`, `page_out`).
  `src/contexto/store/render.py` must render them: `is_responses_item` tells a Responses item from a
  Messages API message by its top-level `type` or its part types, and `as_message` maps it through
  `RESPONSES.retained_blocks`. `own_call_ids` and `is_own_item` find Contexto's own search, recall
  and state calls in each shape (D51). `src/contexto/store/pages.py` builds the stub and the index
  line through the same mapping. A third item shape needs its own branch there; one with a top-level
  `type` would be read as a Responses item today. Codex: yes.
- [ ] **Token and media estimates.** `src/contexto/tokens.py` knows both APIs' media blocks
  (`IMAGE_TYPES`, `_media_kind`, `_base64_payload`, `MEDIA_KEYS`) and prices every image by
  Anthropic's formula (D48), and has a Responses estimate (`estimate_item_tokens`, `is_tool_output`,
  D61). `RewriteEngine.prime` lists the request keys it scans for PDFs. Codex: yes (`input_image`,
  `input_file`, `media_floor = False`).
- [ ] **Launch-time overhead.** `start_warning` in `src/contexto/launcher.py` warns when the bound
  leaves a small window for an assumed fixed overhead (rule 4, D55). Codex: yes (`ASSUMED_OVERHEAD`,
  `MEASURED_OVERHEADS` in `src/contexto/codex.py`).
- [ ] **Subagents and side conversations.** `result_ids` is enough for side rooms to open, continue
  and expire (D40). Closing a room early on evidence that an agent stopped (`agent_runs`,
  `RewriteEngine._close_finished_side_rooms`) reads Claude Code's shapes and is gated by
  `SUBAGENT_SHAPES_TESTED`. Codex: `result_ids` only; its rooms keep the 15-minute expiry.
- [ ] **Which conversation is main.** The turn-start count rule works for any dialect. The rules for
  `/clear`, `/resume` and compaction (D50, D59) read Claude Code's session id, `User-Agent` and
  compaction summary (`claude_code_session`, `compaction_summary`). `SessionRecorder` in
  `src/contexto/launcher.py` records Claude Code's session ids for `--continue` (D55). Codex: no
  ("Codex requests carry no Claude Code session id, so only the turn-start rule applies to them").
- [ ] **Tests and fixtures to copy.**
  - Dialect and engine: the first part of `tests/test_codex.py` (the item builders `lead`, `prompt`,
    `reasoning`, `call`, `output`, `history`, and the tests from
    `test_turn_starts_lead_and_system_like_items` to
    `test_engine_dispatches_responses_paths_with_separate_state`, plus
    `test_retained_snapshot_on_the_responses_dialect` and
    `test_tag_leading_user_prompts_stay_turn_starts`, the turn-start boundary case).
  - Launcher: the rest of `tests/test_codex.py`.
  - Proxy: the Responses cases in `tests/test_proxy_server.py`, `tests/test_stream_hold.py` and
    `tests/test_subagent_room.py`.
  - Store: `tests/test_render_responses.py`, `tests/test_codex_search.py`,
    `tests/test_codex_stub_refresh.py`.
  - Checks: `tests/test_toolcheck.py` and `tests/test_cliversions.py`
    (`test_responses_session_start_seen`).
  - Measurement: `spikes/m2/codex/` (`probe.sh` for request shapes, `responses_stub.py` and
    `smoke.sh` for a zero-spend launch), `spikes/m2/cliversion/check.sh`, and the write-up
    `docs/research/M2-codex-adapter.md`. Item shapes in tests are synthetic, structure only.
  - The JSON files under `tests/fixtures/` are Claude Code shapes (subagents, compaction).
  - Codex: yes, all of the above.

A change to what the proxy does to requests is a change to the design contract
(`CONTRIBUTING.md`): say so in the pull request, and the maintainers record the decision.

## 5. Known rough edges

The contract above is descriptive. These are the places where it is not a clean interface. Line
numbers are for `src/contexto/rewrite.py` as of v0.5.24 and will go stale.

**The base class is the Anthropic implementation.** `Dialect` is not abstract: every method has the
Messages API behaviour as its default, and `ANTHROPIC = Dialect()`. A subclass that forgets a method
inherits the Messages API reading of it without an error. The class docstring names an
`AnthropicDialect`; no class of that name exists.

**Module-level `_anthropic_*` functions.** Seven base-class methods forward to functions defined at
module level, some of them before the class: `_anthropic_is_context_block`,
`_anthropic_is_turn_start`, `_anthropic_normalise`, `_anthropic_head_fingerprint`,
`_anthropic_strip_thinking`, `_anthropic_classify_system`, `_anthropic_session_start_seen`.
`_anthropic_is_turn_start` calls the module-level `is_context_block`, which asks the current
dialect, not `_anthropic_is_context_block`. `first_prompt` calls `_anthropic_is_context_block`
directly (line 2079).

**Sites that read `dialect.name`.**

| Line | Function | What it does |
|---|---|---|
| 1878 | `keyed` | prefixes the conversation's state key with the name |
| 2188 | `epoch_record` | writes the name as the record's `dialect` |
| 2205 | `resume_state` | refuses a record whose `dialect` differs (`dialect_changed`) |
| 2311 | `RewriteEngine._call` | `== "anthropic"`: records Claude Code's session (`claude_code_session`) and compaction summary (`compaction_summary`) for D50 |
| 2336 | `RewriteEngine._call` | passes the name to `ToolCheck.observe` |
| 2643 | `RewriteEngine._close_finished_side_rooms` | `!= "anthropic"`: returns, so no side room closes early (D40) |
| 2722 | `RewriteEngine._publish_side_room` | `== "anthropic"`: records the conversation's `first_prompt`, else `None` |

The first three use the name as data. The last four branch on which client it is.

Outside the engine: `ClientVersions.observe` in `src/contexto/cliversions.py` (lines 268 and 269)
maps the name to a CLI through `DIALECT_CLI` and logs it; `src/contexto/toolcheck.py` compares the
name string at lines 345, 358, 384 and 409 and lists the two names in `DIALECTS` (line 73).

**Sites that compare the dialect instance with `ANTHROPIC`.**

| Line | Function | What it does |
|---|---|---|
| 385 | `_message_hash` | the fast path that skips `normalise` when the canonical JSON holds no `cache_control` |
| 1152 | `transform_messages` | `Config.turn_reminder` is appended to retained turn starts on the Messages API only ("other dialects ignore it") |

**Messages API shapes the engine reads without asking the dialect.**

- `_carried_at`, `_is_tool_round` and `_result_missing` (nested memory next to tool results, D46 and
  D47) look for role `user` items with `tool_result` blocks, on every dialect.
- `settings_fields`, `settings_key`, `mode_kind`, `message_text` and `context_block_kind` read
  `role`, `content` and `text` as the Messages API has them. They run on whatever `classify_system`
  and `is_context_block` let through.
- `carried_tokens` and `_block_page` build Messages API user messages around carried blocks, and the
  fallback pages them in that shape.
- The `paged_changed` resync in `_rewrite` (line 1774) looks for in-flight thinking by the Messages
  API block types, so `inflight_thinking_kept` is always false on another dialect.
- `RewriteEngine._observe_usage` reads `usage.get("output_tokens")` itself (line 2857).
- `RewriteEngine.prime` scans the keys `("messages", "input")` by name (line 2732), not
  `dialect.field`.
- `RewriteEngine._call` calls `count_tokens_view(request, state)` without the dialect (line 2300),
  and `COUNT_TOKENS` is the only count endpoint the engine knows.
- `agent_runs`, `first_prompt`, `compaction_summary` and `claude_code_session` are Claude Code
  readings that live in the engine module.
- `retained_entries` expects `retained_blocks` to return Messages API blocks, and the renderer it
  calls lives in the store.

**Outside the engine.**

- `dialect_for` hard-codes two dialects and imports `contexto.codex` lazily to avoid an import cycle.
- The proxy's `api` is two-valued. `UsageReader` and `StreamHold` branch on `api == "responses"` and
  treat anything else as the Messages API. The proxy's log fields `original_message_count` and
  `message_count` count `request["messages"]` only, so they are 0 for a Responses request.
- `src/contexto/store/render.py` imports `RESPONSES` and treats any item with a top-level `type` as
  a Responses item.
- `Dialect.estimate_items` has the same name as `TokenEstimator.estimate_items`, which the Responses
  implementation calls; the base class calls `TokenEstimator.estimate_messages`.
