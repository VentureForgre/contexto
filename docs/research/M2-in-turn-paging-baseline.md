# M2 in-turn paging baseline: tool loops through the engine at v0.5.24, zero spend

What the rewrite engine does today when one user turn grows past the bound, measured before D63
(in-turn paging) changes it. Six scripted conversations with long tool loops run through the real
`contexto.rewrite.RewriteEngine` and a real `contexto.store.SessionStore` against a fake upstream in
the same process (`spikes/m2/inturn/`). No model is called.

Numbers only. Every number is measured unless marked otherwise, and every run gives the same numbers:
the scripts are a pure function of their parameters.

## Run conditions

| | |
|---|---|
| Code | main `b0e2dbc244bb13d3109ee2b293575e4eacd1053f` (v0.5.24), with only `spikes/m2/inturn/` added |
| Engine | `RewriteEngine` with the default `Config`: bound 150,000, headroom floor 25,000, `min_turn_room` 12,000 |
| Store | `SessionStore` in a temporary directory: real page files, epoch record, turn room and side rooms |
| Claude Code shapes | 2.1.318, `claude-opus-5-5`, Messages API (`/v1/messages`) |
| Codex shapes | codex-cli 0.166.0, `gpt-6-astra`, Responses API (`/v1/responses`), code mode |
| Upstream | `spikes/m2/inturn/upstream.py`, in-process; its tokenizer and rules are described below |
| Run | 2026-11-12, Python 3.13.14, macOS on an Apple M5 Pro |

Reproduce, from the checkout's root:

```sh
uv run python spikes/m2/inturn/replay.py --scenario all --json out.json   # every scenario, small at 260 rounds
uv run python spikes/m2/inturn/replay.py --scenario small --full          # small at 1,500 rounds
uv run --with pytest pytest -q spikes/m2/inturn
```

The first command takes about 30 seconds of CPU. `small` at 1,500 rounds takes about 2 minutes 45
seconds of CPU for the two dialects: the engine's work for one request grows with the number of
messages in it, so a loop's cost grows with the square of its rounds. The tests run `small` at 260
rounds for that reason; every other scenario runs at full size everywhere.

## The scenarios

Each is one prompt (or several, in `multiturn`) followed by tool rounds. A round is one model
response with short thinking and one tool call (Read with a `file_path`, Bash with a `command`, or
Grep; on Codex an `exec_command` function call), then the tool's result. Sizes are in the fake
upstream's tokens. "History" is the CLI's own last request with its head and nothing paged.

| scenario | what it is | requests | rounds | tool results, tokens | largest result | history, Claude Code | history, Codex |
|---|---|---|---|---|---|---|---|
| `loop120` | one prompt, 120 rounds of 6,400 | 121 | 120 | 772,440 | 6,437 | 810,930 | 810,764 |
| `large` | 12 rounds; rounds 3, 6 and 9 return 60,000, 120,000 and 200,000, the rest 800 | 13 | 12 | 387,644 | 200,037 | 405,864 | 404,148 |
| `small` | 1,500 rounds of 400, with shorter thinking | 1,501 | 1,500 | 655,500 | 437 | 890,004 | 887,175 |
| `small`, test size | the same at 260 rounds | 261 | 260 | 113,620 | 437 | 167,466 | 165,418 |
| `subagent` | main, in a tool loop, launches a background subagent; the subagent runs 150 rounds of 5,000 in its own conversation while main sends one request for every three of its | 210 | 208 | 826,216 | 5,037 | main 97,916; subagent 793,482 | not run |
| `images` | a pasted 2,560 x 1,600 image in the prompt, then 100 rounds of 2,500; 67 of them also return a screenshot | 101 | 100 | 360,787 | 5,313 | 400,251 | 398,864 |
| `multiturn` | three turns of two 1,500 rounds, a 100-round loop of 5,000 in turn 4, two more short turns | 116 | 110 | 519,070 | 5,037 | 558,219 | 558,140 |

The request, round and result columns are Claude Code's; Codex's results are within 0.3% (its
`function_call_output` wrapper is a few bytes longer). The head is 15,101 tokens for Claude Code's main
conversation (system 5,036, tools 10,065), 8,787 for the subagent, and 13,537 for Codex (instructions
5,484, the items before the first prompt 8,053), sized like measured ones; the text is filler.

`subagent` is Claude Code only: the engine tells a subagent by `X-Claude-Code-Agent-Id`, and Codex
has no measured subagent shape. The subagent has its own `system`, tool list and first message (a
`<system-reminder>` block, then the `Agent` call's prompt unchanged). It is launched in the
background, because a foreground launch blocks main until the hand-back and nothing could interleave.
The hand-back is Claude Code's task notice, a role `system` message after main's next tool results.

In `images` the screenshots are 1,280 x 800, 1,920 x 1,080 and 800 x 600 PNGs, priced 1,366, 2,765
and 641 tokens; the pasted image costs the 4,787 maximum. The files are valid PNGs with those
dimensions, but all black and under 1 KB each, where a real screenshot is hundreds of kilobytes.

## Baseline at v0.5.24

### Bound

`over_bound` counts the engine's own events: `source: "estimate"` on the request event before the
send, `source: "real"` from `observe_usage`. "Largest" is the largest forwarded request: the engine's
pre-send estimate of it, the upstream's token count, and its bytes as the proxy would write them.

| scenario | dialect | requests | over_bound (estimate) | over_bound (real) | largest estimate | largest tokens | largest bytes | pages | rotations | violations |
|---|---|---|---|---|---|---|---|---|---|---|
| loop120 | claude | 121 | 100 | 100 | 810,520 | 810,632 | 1,822,484 | 0 | 0 | 0 |
| loop120 | codex | 121 | 100 | 100 | 810,356 | 810,447 | 1,812,726 | 0 | 0 | 0 |
| large | claude | 13 | 7 | 7 | 405,604 | 405,566 | 909,234 | 0 | 0 | 0 |
| large | codex | 13 | 7 | 7 | 403,857 | 403,831 | 902,752 | 0 | 0 | 0 |
| small (1,500) | claude | 1,501 | 1,270 | 1,270 | 889,690 | 889,706 | 2,222,613 | 0 | 0 | 0 |
| small (1,500) | codex | 1,501 | 1,266 | 1,266 | 886,851 | 886,858 | 2,101,995 | 0 | 0 | 0 |
| small (260) | claude | 261 | 30 | 30 | 167,135 | 167,168 | 425,752 | 0 | 0 | 0 |
| small (260) | codex | 261 | 26 | 26 | 165,075 | 165,101 | 399,804 | 0 | 0 | 0 |
| subagent | claude | 210 | 124 | 124 | 793,088 | 793,184 | 1,784,690 | 0 | 0 | 0 |
| images | claude | 101 | 67 | 67 | 399,945 | 399,953 | 787,617 | 0 | 0 | 0 |
| images | codex | 101 | 66 | 66 | 398,487 | 398,547 | 774,075 | 0 | 0 | 0 |
| multiturn | claude | 116 | 77 | 77 | 549,854 | 550,005 | 1,247,650 | 4 | 1 | 0 |
| multiturn | codex | 116 | 77 | 77 | 549,647 | 549,768 | 1,238,590 | 5 | 1 | 0 |

Inside a loop the engine pages nothing: the last request of every loop is the CLI's whole history.
The only pages are `multiturn`'s, written at the turn start after its loop.

### Recall room

The room the engine published from each response's real usage: `set_turn_room` for the main
conversation, `set_side_room` for the subagent. "At 0 past 50,000" counts requests sent after the
turn's own items passed 50,000 tokens whose room was 0, before the send or after the response.

| scenario | dialect | conversation | requests | rooms published | room min | room median | requests at 0 | at 0 past 50,000 |
|---|---|---|---|---|---|---|---|---|
| loop120 | claude | main | 121 | 121 | 0 | 0 | 104 | 104 |
| loop120 | codex | main | 121 | 121 | 0 | 0 | 104 | 104 |
| large | claude | main | 13 | 13 | 0 | 0 | 7 | 7 |
| large | codex | main | 13 | 13 | 0 | 0 | 7 | 7 |
| small (1,500) | claude | main | 1,501 | 1,501 | 0 | 0 | 1,313 | 1,313 |
| small (1,500) | codex | main | 1,501 | 1,501 | 0 | 0 | 1,310 | 1,310 |
| small (260) | claude | main | 261 | 261 | 0 | 33,441 | 73 | 73 |
| small (260) | codex | main | 261 | 261 | 0 | 35,425 | 70 | 70 |
| subagent | claude | main | 59 | 59 | 27,088 | 68,677 | 0 | 0 |
| subagent | claude | subagent | 151 | 150 | 0 | 0 | 127 | 127 |
| images | claude | main | 101 | 101 | 0 | 0 | 73 | 73 |
| images | codex | main | 101 | 101 | 0 | 0 | 73 | 73 |
| multiturn | claude | main | 116 | 116 | 0 | 0 | 82 | 82 |
| multiturn | codex | main | 116 | 116 | 0 | 0 | 82 | 82 |

The room is `bound - (input + output) - 25,000`, so it reaches 0 at about 125,000 tokens, a few
requests before the first `over_bound`. The median is the lower of the two middle values. The
subagent's last response has no tool call, which closes its side room and publishes none.

### Tokens sent, cache, rewrites and thinking

A rewrite is a request whose forwarded head or forwarded items differ from the previous request of
the same conversation at some position both have. Its re-sent tokens are the previous request's
tokens from the first changed item to its end: what was cached and had to be sent again. That is an
upper bound when the rewrite also removed content; the cache-read share shows what was paid.

| scenario | dialect | input tokens sent | cache read | rewrites | re-sent tokens | mean per rewrite | re-sent share | in-flight thinking missing |
|---|---|---|---|---|---|---|---|---|
| loop120 | claude | 49,991,101 | 98.4% | 0 | 0 | 0 | 0.0% | 0 |
| loop120 | codex | 49,864,921 | 98.4% | 0 | 0 | 0 | 0.0% | 0 |
| large | claude | 2,506,799 | 83.8% | 0 | 0 | 0 | 0.0% | 0 |
| large | codex | 2,483,199 | 83.7% | 0 | 0 | 0 | 0.0% | 0 |
| small (1,500) | claude | 679,479,973 | 99.9% | 0 | 0 | 0 | 0.0% | 0 |
| small (1,500) | codex | 675,917,389 | 99.9% | 0 | 0 | 0 | 0.0% | 0 |
| small (260) | claude | 23,859,601 | 99.3% | 0 | 0 | 0 | 0.0% | 0 |
| small (260) | codex | 23,341,247 | 99.3% | 0 | 0 | 0 | 0.0% | 0 |
| subagent | claude | 63,917,680 | 98.6% | 0 | 0 | 0 | 0.0% | 0 |
| images | claude | 21,337,116 | 98.1% | 0 | 0 | 0 | 0.0% | 0 |
| images | codex | 21,169,778 | 98.1% | 0 | 0 | 0 | 0.0% | 0 |
| multiturn | claude | 29,481,521 | 98.1% | 1 | 534,904 | 534,904 | 1.8% | 0 |
| multiturn | codex | 29,356,242 | 98.1% | 1 | 536,230 | 536,230 | 1.8% | 0 |

Today every loop request is an append, so nothing is re-sent and no in-flight thinking is removed.
All three in-flight counts are 0 in every run (`inflight_thinking_missing`, its `_latest`, and
`inflight_assistant_absent`; on Codex `inflight_reasoning_dropped`, its `_latest`, and
`inflight_calls_absent`). No tool result was lost: each of the 1,500 or fewer results per run was in
the last forwarded request or in a store page (`originals.lost` 0).

### Where each run first passes the bound

| scenario | dialect | conversation | room first 0 | first over_bound | tokens of that request |
|---|---|---|---|---|---|
| loop120 | claude | main | request 18 | request 22 | 154,789 |
| loop120 | codex | main | request 18 | request 22 | 153,183 |
| large | claude | main | request 7 | request 7 | 200,217 |
| large | codex | main | request 7 | request 7 | 198,397 |
| subagent | claude | main | never | never | largest 97,618 |
| subagent | claude | subagent | its request 24 | its request 28 | 150,544 |
| images | claude | main | request 29 | request 35 | 150,123 |
| images | codex | main | request 29 | request 36 | 152,491 |
| multiturn | claude | main | request 29 | request 34 | 152,936 |
| multiturn | codex | main | request 29 | request 34 | 151,587 |

### Estimate against the upstream's count

After the first response the pre-send estimate is the last real input plus the added items at the
session's measured ratio. In the loops of even-sized results the estimate is never more than 1,910
tokens from the upstream's count (`loop120`, at request 2; 1,779 in `images`, 1,772 in `small` and
`subagent`, each at the first request). A large result arrives while the ratio is still mostly learned
from head text, and is estimated low (`large`, Claude Code):

| request | brings | estimate | upstream's count | estimate low by |
|---|---|---|---|---|
| 4 | the 60,000 result | 60,748 | 77,943 | 17,195 |
| 7 | the 120,000 result | 185,247 | 200,217 | 14,970 |
| 10 | the 200,000 result | 389,379 | 402,492 | 13,113 |

Each `large` run logged 2 `estimate_error` events (real more than 10% over the estimate).

### The turn start after a long loop (`multiturn`)

| | Claude Code | Codex |
|---|---|---|
| Last request of turn 4 (the loop) | 550,005 | 549,768 |
| Rotation at turn 5's start | `window_minimal`, 4 pages | `window_minimal`, 5 pages |
| Items forwarded at turn 5's start | 2 of 222 | 5 of 335 |
| First request of turn 5 | 16,807 | 14,863 |
| Re-sent tokens at that rewrite | 534,904 | 536,230 |
| Largest request of turns 5 and 6 | 24,216 | 22,376 |
| Room at turn 5's start, before the send and after the response | 102,310 and 108,022 | 104,807 and 109,938 |
| Results in the last request / in pages / lost | 4 / 106 / 0 | 4 / 106 / 0 |

On Codex the items before the first prompt make one more page. Turns 1 to 3 are forwarded whole.

## Against the 2026-11-12 audit

The audit replayed one prompt and 120 tool rounds and saw the estimate reach 775,851 at bound
150,000, with 0 pages and 96 `over_bound` events. `loop120` gives 810,520, 0 pages and 100 from each
source. The difference is the script: this one has a 15,101-token head and about 180 tokens of
thinking, signature and tool call per round on top of each 6,437-token result. The audit's script is
not in the repository, so its head and round sizes are not known [inferred from its totals: about
6,465 tokens a round with no head].

## The fake upstream

**Tokenizer.** It is deliberately not the engine's estimator, which counts text at 3.5 bytes a token
and tool output at 1.5 until the first usage and everything at one measured ratio after it. The
upstream keeps two ratios for the whole run: tool output (a Messages `tool_result` block, a
Responses `*_output` item) at 2.2 bytes a token, everything else at 3.1, on the block's JSON as the
proxy writes it with `cache_control` and images left out, each block rounded up, plus 4 tokens a
message or item. An image costs the contract's rule 3 price from the pixel size in its own header
(`contexto.tokens.image_tokens`), and 4,787 when the header cannot be read.

**Usage.** Messages: `input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` (the
uncached part up to the last cache breakpoint) and `output_tokens`. Responses: `input_tokens` with
`input_tokens_details.cached_tokens`, and `output_tokens`.

**Cache.** `cache_read` is the longest prefix an earlier request of the same conversation began
with: the head (`tools` and `system`, or `instructions` and `tools`), then item by item, compared on
the JSON with `cache_control` ignored and a string `content` read as one text block.

**Rules.** Each violation is counted by kind, per request; none raises.

| API | kind | rule | recorded in |
|---|---|---|---|
| Messages | `tool_use_without_result` | a `tool_use` has its `tool_result` in the next message | M0-interception.md |
| Messages | `tool_result_without_use` | a `tool_result` has its `tool_use` in the assistant message right before it | M0-interception.md |
| Messages | `empty_content` | no user or assistant message without content, no text block without text | the API's request validation; not measured in this repository |
| Messages | `system_before_first_user` | no role `system` message before the first user message | contract rule 1 (measured 400) |
| Messages | `thinking_binding` | nothing before a kept thinking block changed: `system`, `tools`, earlier messages | M0-interception.md, M0-paging-policy.md |
| Messages | `thinking_modified` | a kept thinking block's text and signature are as emitted | M0-interception.md |
| Responses | `call_without_output`, `call_with_several_outputs` | a client `*_call` has exactly one `*_output` with its `call_id` after it | M0-interception.md |
| Responses | `output_without_call` | an `*_output` has a call before it | M0-interception.md |
| Responses | `reasoning_without_following` | a `reasoning` item is followed by another model item | M0-interception.md (documented) |
| Responses | `reasoning_modified` | a `reasoning` item's content is as emitted | M0-interception.md (inferred) |
| both | `not_an_object` | every message or item is a JSON object | the API's request validation; not measured in this repository |

A thinking block's signature is a keyed hash of the request it answered: `system`, `tools` and the
messages, with `cache_control` and thinking blocks left out. On every later request the upstream
takes the same hash over what precedes the block's message and counts `thinking_binding` when it
differs. Removing thinking blocks and appending therefore pass, and an edit to `system`, to `tools`
or to any earlier message fails for every thinking block after it, as the vendor documents for 5.x
models. Hosted Responses calls (`web_search_call` and the like) need no output.

The in-flight counts are information, not violations: requests inside a tool loop where an assistant
message of the turn in flight arrived without the thinking it was emitted with, where the newest one
did, and where one did not arrive at all. The documented consequence of the first is that the API
disables thinking for the rest of the turn without an error.

## Limits of the model

- The tokenizer is two fixed ratios, not a real tokenizer. Absolute counts are not Claude's or
  OpenAI's, and the gap between the engine's estimate and the upstream's count is this model's gap.
- On Codex, images are priced by the same pixel rule as on Claude. OpenAI prices by tiles (85 tokens
  at low detail, `contexto.codex`), so the Codex image counts err high [inferred].
- The cache has no minimum prefix length, no expiry and no limit on breakpoints, so the cache-read
  share is an upper bound on a real one.
- The thinking check is the documented prefix rule alone. It does not model the per-identity
  binding the M0 probe saw on one route, `prefix_mismatch_behavior`, or `drop_block`.
- `inflight_thinking_missing` counts requests; whether the API then disables thinking is documented,
  not measured [vendor-documented; untested in a tool loop, `docs/research/M0-paging-policy.md`].
- Two documented Messages rules are not checked: text placed before a `tool_result` in the same
  message, and duplicate `tool_use` ids.
- The model never calls `contexto_recall`, so the published rooms are never spent, and nothing reads
  a page back through the MCP server. "Reachable" means `SessionStore.get_page` returns the page and
  it holds the original text.
- The scripts are synthetic: every result is new text, no file is read twice, and thinking is short
  (about 70 tokens; `small` about 30).
- One engine, one store and one upstream per run, in one process and one thread. The proxy's HTTP
  layer, stream hold and locks are not in the path.

## What D63 has to change

`spikes/m2/inturn/tests/test_inturn_targets.py` holds one test per scenario and dialect (11), each
marked `xfail(strict=True)` and failing at v0.5.24. `targets.unmet(run)` passes when a run has:

- no `over_bound` event from either source;
- the largest forwarded request at or under the bound, by the estimate and by the upstream's count;
- no violation of any kind;
- every stub's original reachable in the store (`stubs.find_stubs`, which the engine pull request
  fills in), with at least one stub found, and no tool result lost;
- no request with a published room of 0 once the turn's own items have passed 50,000 tokens.
