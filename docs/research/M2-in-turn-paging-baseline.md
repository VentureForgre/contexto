# M2 in-turn paging baseline: tool loops through the engine at v0.5.24, zero spend

What the rewrite engine does today when one user turn grows past the bound, measured before D63
(in-turn paging) changes it, and what D63 is held to. Seven scripted conversations with long tool
loops run through the real `contexto.rewrite.RewriteEngine` and a real `contexto.store.SessionStore`
against a fake upstream in the same process (`spikes/m2/inturn/`). No model is called.

Numbers only. Every number is measured unless marked otherwise, and every run gives the same numbers:
the scripts are a pure function of their parameters.

## Run conditions

| | |
|---|---|
| Code | main `b0e2dbc244bb13d3109ee2b293575e4eacd1053f` (v0.5.24), with only `spikes/m2/inturn/` added |
| Engine | `RewriteEngine` with the default `Config`: bound 150,000, headroom floor 25,000, `min_turn_room` 12,000 |
| Store | `SessionStore` in a temporary directory: real page files, epoch record, turn room and side rooms |
| MCP tools | `contexto.mcp.Server` on a second handle of the same store, called in-process |
| Claude Code shapes | 2.1.318, `claude-opus-5-5`, Messages API (`/v1/messages`, `/v1/messages/count_tokens`) |
| Codex shapes | codex-cli 0.166.0, `gpt-6-astra`, Responses API (`/v1/responses`), code mode |
| Upstream | `spikes/m2/inturn/upstream.py`, in-process; its tokenizer and rules are described below |
| Run | 2026-11-12, Python 3.13.14, macOS on an Apple M5 Pro |

Reproduce, from the checkout's root:

```sh
uv run python spikes/m2/inturn/replay.py --scenario all --json out.json   # every scenario, small at 260 rounds
uv run python spikes/m2/inturn/replay.py --scenario small --full          # small at 1,500 rounds
uv run --with pytest pytest -q spikes/m2/inturn
```

The first command takes about 30 seconds of CPU. `small` at 1,500 rounds takes about 2 minutes 40 seconds
of CPU for the two dialects: the engine's work for one request grows with the number of messages in
it, so a loop's cost grows with the square of its rounds. The tests run `small` at 260 rounds for
that reason; every other scenario runs at full size everywhere.

## The scenarios

Each is one prompt (or several, in `multiturn`) followed by tool rounds. A round is one model
response with short thinking and a tool call (Read with a `file_path`, Bash with a `command`, or
Grep; on Codex an `exec_command` function call), then the tool's result. Sizes are in the fake
upstream's tokens. "Scripted history" is the CLI's own last request with its head, nothing paged, and
without the probe rounds described next.

| scenario | what it is | requests | rounds | tool results, tokens | largest result | scripted history, Claude Code | scripted history, Codex |
|---|---|---|---|---|---|---|---|
| `loop120` | one prompt, 120 rounds of 6,400 | 125 | 120 | 772,440 | 6,437 | 810,930 | 810,764 |
| `large` | 12 rounds; rounds 3, 6 and 9 return 60,000, 120,000 and 200,000, the rest 800 | 15 | 12 | 387,644 | 200,037 | 405,864 | 404,148 |
| `small` | 1,500 rounds of 400, with shorter thinking | 1,505 | 1,500 | 655,500 | 437 | 890,004 | 887,175 |
| `small`, test size | the same at 260 rounds | 265 | 260 | 113,620 | 437 | 167,466 | 165,418 |
| `subagent` | main, in a tool loop, launches a background subagent; the subagent runs 150 rounds of 5,000 in its own conversation while main sends one request for every three of its | 214 | 208 | 826,216 | 5,037 | main 97,916; subagent 795,349 | not run |
| `images` | a pasted 2,560 x 1,600 image in the prompt, then 100 rounds of 2,500; 67 of them also return a screenshot | 105 | 100 | 360,787 | 5,313 | 400,251 | 398,864 |
| `multiturn` | three turns of two 1,500 rounds, a 100-round loop of 5,000 in turn 4, two more short turns | 120 | 110 | 519,070 | 5,037 | 558,219 | 558,140 |
| `mixed` | 45 rounds: 15 make three calls of 2,400 each, answered together; 30 make one call of 4,000 | 50 | 45 | 230,775 | 4,037 | 257,772 | 256,043 |

The request, round and result columns are Claude Code's; Codex's results are within 0.3% (its output
item's wrapper is a few bytes longer). The head is 15,101 tokens for Claude Code's main conversation
(system 5,036, tools 10,065), 10,654 for the subagent, and 13,538 for Codex (instructions 5,484, an
empty tool list 1, the items before the first prompt 8,053), sized like measured ones; the text is
filler. A `loop120` round adds 128 tokens of thinking and signature, 43 of tool call and 6,437 of
result.

**Probes.** Late in every loop of ten rounds or more, two requests are not scripted. The model calls
`contexto_search` for a 12-character hex string that only one line, in the middle of the second or
third round's result, holds; when the reply names a page, it then calls `contexto_recall` with that
page and the offset the reply gave, and otherwise goes on with a small Read. The harness runs the
real handlers and puts the calls and their replies into the CLI's history as a tool round. A loop is
probed after three fifths of its rounds and again near its end (`large` once, at its end). On Codex
the calls go through code mode's `exec`, a `custom_tool_call`.

**Shapes.** `mixed` holds the round shapes the other loops lack: three calls answered in one user
message (Codex: three calls, then three outputs), a `<system-reminder>` text block after the results
in their message (11 rounds, Claude Code only), and Codex's `custom_tool_call` with its
`custom_tool_call_output` (6 rounds; the same command through Bash on Claude Code).

`subagent` is Claude Code only: the engine tells a subagent by `X-Claude-Code-Agent-Id`, and Codex
has no measured subagent shape. The subagent has its own `system`, its own tool list (Contexto's five
tools among them, no `Agent`) and its own first message (a `<system-reminder>` block, then the
`Agent` call's prompt unchanged). It is launched in the background, because a foreground launch
blocks main until the hand-back and nothing could interleave. The hand-back is Claude Code's task
notice, a role `system` message after main's next tool results. The subagent's loop is probed; main's
is not.

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
| loop120 | claude | 125 | 104 | 104 | 812,189 | 812,301 | 1,826,922 | 0 | 0 | 0 |
| loop120 | codex | 125 | 104 | 104 | 812,074 | 812,164 | 1,817,051 | 0 | 0 | 0 |
| large | claude | 15 | 9 | 9 | 406,441 | 406,397 | 911,443 | 0 | 0 | 0 |
| large | codex | 15 | 9 | 9 | 404,718 | 404,686 | 904,904 | 0 | 0 | 0 |
| small (1,500) | claude | 1,505 | 1,274 | 1,274 | 891,359 | 891,375 | 2,227,051 | 0 | 0 | 0 |
| small (260) | claude | 265 | 33 | 33 | 168,818 | 168,850 | 430,218 | 0 | 0 | 0 |
| small (1,500) | codex | 1,505 | 1,270 | 1,270 | 888,568 | 888,575 | 2,106,320 | 0 | 0 | 0 |
| small (260) | codex | 265 | 30 | 30 | 166,804 | 166,830 | 404,157 | 0 | 0 | 0 |
| subagent | claude | 214 | 128 | 128 | 796,575 | 796,678 | 1,794,826 | 0 | 0 | 0 |
| images | claude | 105 | 71 | 71 | 401,613 | 401,622 | 792,055 | 0 | 0 | 0 |
| images | codex | 105 | 70 | 70 | 400,204 | 400,264 | 778,400 | 0 | 0 | 0 |
| multiturn | claude | 120 | 81 | 81 | 551,525 | 551,674 | 1,252,089 | 4 | 1 | 0 |
| multiturn | codex | 120 | 81 | 81 | 551,367 | 551,486 | 1,242,916 | 5 | 1 | 0 |
| mixed | claude | 50 | 25 | 25 | 258,955 | 259,143 | 596,918 | 0 | 0 | 0 |
| mixed | codex | 50 | 24 | 25 | 257,285 | 257,443 | 587,047 | 0 | 0 | 0 |

Inside a loop the engine pages nothing: the last request of every loop is the CLI's whole history.
The only pages are `multiturn`'s, written at the turn start after its loop. No request broke a rule
of the upstream's in any run.

### Recall room

"Rooms at request" counts the rooms the engine published before the send (`set_turn_room`, main
only), "rooms at response" those it published from the response's real usage (`set_turn_room` for
main, `set_side_room` for the subagent); min, median and "at 0" are of the latter. The last four
columns count requests sent after the turn's own items passed 50,000 tokens: how many there were, and
on how many the request-time room was under `min_turn_room` (12,000), the response-time room was 0,
and the response-time room was under half of `min_turn_room` (6,000).

| scenario | dialect | conversation | requests | rooms at request | rooms at response | room min | room median | at 0 | past 50,000 | request room under 12,000 | response room 0 | response room under 6,000 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| loop120 | claude | main | 125 | 125 | 125 | 0 | 0 | 108 | 117 | 110 | 108 | 109 |
| loop120 | codex | main | 125 | 125 | 125 | 0 | 0 | 108 | 117 | 109 | 108 | 109 |
| large | claude | main | 15 | 15 | 15 | 0 | 0 | 9 | 12 | 9 | 9 | 9 |
| large | codex | main | 15 | 15 | 15 | 0 | 0 | 9 | 12 | 9 | 9 | 9 |
| small (1,500) | claude | main | 1,505 | 1,505 | 1,505 | 0 | 0 | 1,317 | 1,420 | 1,337 | 1,317 | 1,327 |
| small (260) | claude | main | 265 | 265 | 265 | 0 | 32,286 | 77 | 180 | 97 | 77 | 87 |
| small (1,500) | codex | main | 1,505 | 1,505 | 1,505 | 0 | 0 | 1,314 | 1,419 | 1,334 | 1,314 | 1,324 |
| small (260) | codex | main | 265 | 265 | 265 | 0 | 34,266 | 73 | 179 | 93 | 73 | 83 |
| subagent | claude | main | 59 | 59 | 59 | 27,088 | 68,677 | 0 | 23 | 0 | 0 | 0 |
| subagent | claude | subagent | 155 | 0 | 154 | 0 | 0 | 132 | 145 | 0 | 132 | 133 |
| images | claude | main | 105 | 105 | 105 | 0 | 0 | 77 | 93 | 80 | 77 | 79 |
| images | codex | main | 105 | 105 | 105 | 0 | 0 | 77 | 93 | 80 | 77 | 78 |
| multiturn | claude | main | 120 | 120 | 120 | 0 | 0 | 86 | 95 | 88 | 86 | 87 |
| multiturn | codex | main | 120 | 120 | 120 | 0 | 0 | 86 | 95 | 88 | 86 | 87 |
| mixed | claude | main | 50 | 50 | 50 | 0 | 0 | 29 | 40 | 31 | 29 | 31 |
| mixed | codex | main | 50 | 50 | 50 | 0 | 0 | 29 | 40 | 31 | 29 | 30 |

The room is `bound - (input + output) - 25,000`, so it reaches 0 at about 125,000 tokens, a few
requests before the first `over_bound`. The median is the lower of the two middle values. The
subagent's last response has no tool call, which closes its side room and publishes none.

In `subagent` main's own room is never under 27,088, but a tool call is charged against the smallest
live room (D40), and the subagent's side room is 0 on 132 of its 154 publishes. A recall made by main
during those requests had no room.

### Tokens sent, cache, rewrites and thinking

A prefix rewrite is a request whose forwarded head or forwarded items differ from the previous
request of the same conversation at a position both have. Two numbers describe it:

- **cache invalidated**: the previous request's tokens from the first changed item to its end, which
  can no longer be read from the cache;
- **re-sent tokens**: what the rewrite cost, the request's uncached tokens less the tokens of the
  items the CLI added since its previous request (an added item counts as it was forwarded, when that
  is smaller). The re-sent share is their share of all input tokens sent.

"Newest thinking missing" counts requests where the newest response of the turn in flight came back
without its thinking. "Thinking kept around a strip" counts requests that hold thinking both before
and after thinking that is missing (on Codex, reasoning items).

| scenario | dialect | input tokens sent | cache read | rewrites | in a loop | cache invalidated | re-sent tokens | mean re-sent | re-sent share | newest thinking missing | thinking kept around a strip |
|---|---|---|---|---|---|---|---|---|---|---|---|
| loop120 | claude | 52,567,369 | 98.5% | 0 | 0 | 0 | 0 | 0 | 0.0% | 0 | 0 |
| loop120 | codex | 52,440,285 | 98.5% | 0 | 0 | 0 | 0 | 0 | 0.0% | 0 | 0 |
| large | claude | 3,317,868 | 87.8% | 0 | 0 | 0 | 0 | 0 | 0.0% | 0 | 0 |
| large | codex | 3,290,830 | 87.7% | 0 | 0 | 0 | 0 | 0 | 0.0% | 0 | 0 |
| small (1,500) | claude | 682,821,272 | 99.9% | 0 | 0 | 0 | 0 | 0 | 0.0% | 0 | 0 |
| small (260) | claude | 24,495,254 | 99.3% | 0 | 0 | 0 | 0 | 0 | 0.0% | 0 | 0 |
| small (1,500) | codex | 679,264,448 | 99.9% | 0 | 0 | 0 | 0 | 0 | 0.0% | 0 | 0 |
| small (260) | codex | 23,971,580 | 99.3% | 0 | 0 | 0 | 0 | 0 | 0.0% | 0 | 0 |
| subagent | claude | 66,738,251 | 98.7% | 0 | 0 | 0 | 0 | 0 | 0.0% | 0 | 0 |
| images | claude | 22,642,903 | 98.2% | 0 | 0 | 0 | 0 | 0 | 0.0% | 0 | 0 |
| images | codex | 22,470,669 | 98.2% | 0 | 0 | 0 | 0 | 0 | 0.0% | 0 | 0 |
| multiturn | claude | 31,253,082 | 98.2% | 1 | 0 | 536,573 | 1,716 | 1,716 | 0.0% | 0 | 0 |
| multiturn | codex | 31,126,696 | 98.2% | 1 | 0 | 537,948 | 1,327 | 1,327 | 0.0% | 0 | 0 |
| mixed | claude | 7,173,132 | 96.4% | 0 | 0 | 0 | 0 | 0 | 0.0% | 0 | 0 |
| mixed | codex | 7,083,116 | 96.4% | 0 | 0 | 0 | 0 | 0 | 0.0% | 0 | 0 |

Today every loop request is an append, so nothing is invalidated or re-sent and no thinking is
removed in flight. The one rewrite is `multiturn`'s turn start after the loop: it invalidates the
whole loop and re-sends 1,716 tokens on Claude Code (1,327 on Codex), because the request it sends is
16,817 tokens of which 15,101, the head, are read from the cache.

### Search and recall late in a loop

What the probes got. `refused`: the reply was "Recall budget for this turn is used up". `in_view`:
the search returned the line from the request in view. `nothing`: "No paged-out turn contains every
word of that query". `not_called`: the search named no page, so the model made no recall. "Results
lost" counts scripted results that were, after the last request, neither whole in it nor byte for
byte in a store page.

| scenario | dialect | conversation | searches | recalls | stubbed on arrival | results lost | count_tokens |
|---|---|---|---|---|---|---|---|
| loop120 | claude | main | refused 2 | not_called 2 | 0 | 0 | 8 sent, 0 different |
| loop120 | codex | main | refused 2 | not_called 2 | 0 | 0 | 0 sent, 0 different |
| large | claude | main | refused 1 | not_called 1 | 0 | 0 | 1 sent, 0 different |
| large | codex | main | refused 1 | not_called 1 | 0 | 0 | 0 sent, 0 different |
| small (1,500) | claude | main | refused 2 | not_called 2 | 0 | 0 | 94 sent, 0 different |
| small (260) | claude | main | in_view 1, refused 1 | not_called 2 | 0 | 0 | 17 sent, 0 different |
| small (1,500) | codex | main | refused 2 | not_called 2 | 0 | 0 | 0 sent, 0 different |
| small (260) | codex | main | in_view 1, refused 1 | not_called 2 | 0 | 0 | 0 sent, 0 different |
| subagent | claude | subagent | nothing 2 | not_called 2 | 0 | 0 | 14 sent, 0 different |
| images | claude | main | refused 2 | not_called 2 | 0 | 0 | 7 sent, 0 different |
| images | codex | main | refused 2 | not_called 2 | 0 | 0 | 0 sent, 0 different |
| multiturn | claude | main | refused 2 | not_called 2 | 0 | 0 | 7 sent, 0 different |
| multiturn | codex | main | refused 2 | not_called 2 | 0 | 0 | 0 sent, 0 different |
| mixed | claude | main | refused 2 | not_called 2 | 0 | 0 | 3 sent, 0 different |
| mixed | codex | main | refused 2 | not_called 2 | 0 | 0 | 0 sent, 0 different |

On the baseline no probe reached a stored result: nothing is paged inside a loop. Every search in a
main conversation made after the room reached 0 was refused, although the line it asked for was in
the request in view. The `small` probe at request 157 of the 260-round run came while the room was
still open and was answered from the view.

Both of the subagent's searches found nothing, and that is not the room: search reads the retained
snapshot, which only the main conversation publishes (contract PR #125), so a subagent's search never
sees the subagent's own request.

`count_tokens` (contract rule 11): after some loop requests of a Claude Code conversation the harness
sends the same history to `/v1/messages/count_tokens`. Every one got the same `system`, `tools` and
`messages` the engine had just forwarded, and none published a room. Codex has no such endpoint.

### Where each run first passes the bound

| scenario | dialect | conversation | room first 0 | first over_bound | tokens of that request |
|---|---|---|---|---|---|
| loop120 | claude | main | request 18 | request 22 | 154,789 |
| loop120 | codex | main | request 18 | request 22 | 153,183 |
| large | claude | main | request 7 | request 7 | 200,217 |
| large | codex | main | request 7 | request 7 | 198,397 |
| subagent | claude | main | never | never | largest 97,618 |
| subagent | claude | subagent | its request 23 | its request 28 | 152,411 |
| images | claude | main | request 29 | request 35 | 150,123 |
| images | codex | main | request 29 | request 36 | 152,491 |
| multiturn | claude | main | request 29 | request 34 | 152,936 |
| multiturn | codex | main | request 29 | request 34 | 151,587 |
| mixed | claude | main | request 22 | request 26 | 152,240 |
| mixed | codex | main | request 22 | request 26 | 150,457 |

### Estimate against the upstream's count

After the first response the pre-send estimate is the last real input plus the added items at the
session's measured ratio. In the loops of even-sized results the estimate is never more than 2,178
tokens from the upstream's count (`mixed`, at request 2; 1,910 in `loop120`, 1,779 in `images`, 1,772
in `subagent`). A large result arrives while the ratio is still mostly learned from head text, and is
estimated low (`large`, Claude Code):

| request | brings | estimate | upstream's count | estimate low by |
|---|---|---|---|---|
| 4 | the 60,000 result | 60,748 | 77,943 | 17,195 |
| 7 | the 120,000 result | 185,247 | 200,217 | 14,970 |
| 10 | the 200,000 result | 389,379 | 402,492 | 13,113 |

The engine logs `estimate_error` when real usage is more than 10% over the estimate. Each `large` run
logged 2: at request 1 (15,669 against 13,897, before any ratio is measured) and at request 4.
Requests 7 and 10 are within 10% and logged none. Request 1 is the event in most other runs too
(`loop120` 1 on Claude Code and 2 on Codex, `subagent` 1, `multiturn` 1, `mixed` 2, `images` 0).

The largest relative gap of any run is the other way, at `multiturn`'s turn start after the loop:
the estimate of the rotated request was 22,712 and the upstream counted 16,817 (Codex: 20,202 and
14,865).

### The turn start after a long loop (`multiturn`)

| | Claude Code | Codex |
|---|---|---|
| Last request of turn 4 (the loop) | 551,674 | 551,486 |
| Rotation at turn 5's start | `window_minimal`, 4 pages | `window_minimal`, 5 pages |
| Items forwarded at turn 5's start | 2 of 230 | 5 of 347 |
| First request of turn 5 | 16,817 | 14,865 |
| Cache invalidated by that rewrite | 536,573 | 537,948 |
| Re-sent tokens at that rewrite | 1,716 | 1,327 |
| Largest request of turns 5 and 6 | 24,226 | 22,378 |
| Room at turn 5's start, before the send and after the response | 102,288 and 108,012 | 104,798 and 109,936 |
| Scripted results in the last request / in pages byte for byte / lost | 4 / 106 / 0 | 4 / 106 / 0 |

On Codex the items before the first prompt make one more page. Turns 1 to 3 are forwarded whole.

## Against the 2026-11-12 audit

The audit replayed one prompt and 120 tool rounds and saw the estimate reach 775,851 at bound
150,000, with 0 pages and 96 `over_bound` events. `loop120` gives 812,189, 0 pages and 104 from each
source. The difference is the script: this one has a 15,101-token head, about 170 tokens of thinking,
signature and tool call per round on top of each 6,437-token result, and four probe requests. The
audit's script is not in the repository, so its head and round sizes are not known [inferred from its
totals: about 6,465 tokens a round with no head].

## The fake upstream

**Tokenizer.** For text it is deliberately not the engine's estimator, which counts text at 3.5 bytes
a token and tool output at 1.5 until the first usage and everything at one measured ratio after it.
The upstream keeps two ratios for the whole run: tool output (a Messages `tool_result` block, a
Responses `*_output` item) at 2.2 bytes a token, everything else at 3.1, on the block's JSON as the
proxy writes it with `cache_control` and images left out, each block rounded up, plus 4 tokens a
message or item. Images are the exception: an image costs the contract's rule 3 price from the pixel
size in its own header, taken from the engine's own `contexto.tokens.image_tokens`, so the image part
of the engine's estimate agrees with the upstream by construction.

**Usage.** Messages: `input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` (the
uncached part up to the last cache breakpoint) and `output_tokens`. Responses: `input_tokens` with
`input_tokens_details.cached_tokens`, and `output_tokens`.

**Cache.** `cache_read` is the longest prefix an earlier request of the same conversation began
with: the head (`tools` and `system`, or `instructions` and `tools`), then item by item, compared on
the JSON with `cache_control` ignored and a string `content` read as one text block.

**Reading a request.** Each request is read from its JSON as it arrives. The upstream remembers
nothing about an engine's object by its identity, so a message the engine edits in place between two
requests is judged as it is sent. The CLI's own history and head are read once; the harness digests
them again at the end of a run and fails the run if the engine changed any of them.

**Rules.** Each violation is counted by kind, per request; none raises.

| API | kind | rule | recorded in |
|---|---|---|---|
| Messages | `tool_use_without_result` | a `tool_use` has its `tool_result` in the next message | M0-interception.md |
| Messages | `tool_result_without_use` | a `tool_result` has its `tool_use` in the assistant message right before it | M0-interception.md |
| Messages | `text_before_tool_result` | in a user message no block of another kind comes before a `tool_result`; text after the results is valid | M0-interception.md |
| Messages | `duplicate_tool_use`, `duplicate_tool_result` | no id twice | the API's request validation; not measured in this repository |
| Messages | `first_message_not_user` | the first message that is not a role `system` one is a user message | the API's request validation; not measured in this repository |
| Messages | `empty_content` | no user or assistant message without content, no text block without text | the API's request validation; not measured in this repository |
| Messages | `system_before_first_user` | no role `system` message before the first user message | contract rule 1 (measured 400) |
| Messages | `thinking_binding` | nothing before a kept thinking block changed: `system`, `tools`, earlier messages, and the blocks ahead of it in its own message | M0-interception.md, M0-paging-policy.md |
| Messages | `thinking_modified` | a kept thinking block's text and signature are as emitted | M0-interception.md |
| Responses | `call_without_output`, `call_with_several_outputs` | a client `*_call` has exactly one `*_output` with its `call_id` after it | M0-interception.md |
| Responses | `output_without_call` | an `*_output` has a call before it | M0-interception.md |
| Responses | `output_kind_mismatch` | an output's type is its call's type with `_output` | the API's request validation; not measured in this repository |
| Responses | `duplicate_call` | no call item twice | the API's request validation; not measured in this repository |
| Responses | `reasoning_without_following` | a `reasoning` item is followed by the item the model emitted after it | M0-interception.md (documented) |
| Responses | `reasoning_modified` | a `reasoning` item's content is as emitted | M0-interception.md (inferred) |
| both | `not_an_object` | every message or item is a JSON object | the API's request validation; not measured in this repository |
| both | `thinking_removed_outside_rewrite`, `reasoning_removed_outside_rewrite` | thinking is missing only where a rewrite covers it (below) | this harness's rule, not an API error |

A thinking block's signature is a keyed hash of the request it answered (`system`, `tools` and the
messages, with `cache_control` and thinking blocks left out) and of the blocks ahead of it in its own
message. On every later request the upstream takes the same hash over what precedes the block and
counts `thinking_binding` when it differs. Removing thinking blocks and appending therefore pass, and
an edit to `system`, to `tools` or to any earlier message fails for every thinking block after it, as
the vendor documents for 5.x models. So does text inserted ahead of a thinking block in its message,
and a thinking block moved behind its `tool_use`. Hosted Responses calls (`web_search_call` and the
like) need no output. A reasoning item left behind when its call and output were removed is orphaned
even when the next round's reasoning item follows it.

**Thinking that goes missing.** A request rewrites content at `p` when its head differs from the
conversation's previous request, or its item at `p` is the first that differs from that request's in
more than its thinking (on Codex the two requests are compared with their reasoning items left out).
Such a request covers every response emitted so far whose message is at `p` or later: that thinking
may be missing on that request and on every later one. Thinking before `p`, and thinking the model
emits afterwards, has to come back, and removing thinking alone covers nothing. Missing thinking that
no rewrite covers is counted as `thinking_removed_outside_rewrite`. The API would not reject such a
request (removing thinking from the start, from the end, or all of it is documented as valid); the
documented consequence is that it stops thinking for the rest of the turn.

Counted as information, not as violations: requests inside a tool loop where a response of the turn
in flight arrived without its thinking (`inflight_thinking_missing`), where the newest one did
(`..._latest`), where one did not arrive at all (`inflight_assistant_absent`), and requests that hold
thinking both before and after thinking that is missing (`thinking_kept_around_strip`; on Codex
`inflight_reasoning_dropped`, `inflight_calls_absent`, `reasoning_kept_around_drop`). All are 0 in
every baseline run.

## Limits of the model

- The tokenizer is two fixed ratios, not a real tokenizer. Absolute counts are not Claude's or
  OpenAI's, and the gap between the engine's estimate and the upstream's count is this model's gap.
- Images are priced by the engine's own rule, so the harness cannot show an error in it. On Codex
  OpenAI prices by tiles (85 tokens at low detail, `contexto.codex`), so the Codex image counts err
  high [inferred].
- The cache has no minimum prefix length, no expiry and no limit on breakpoints, and it ignores the
  20-block lookback `docs/research/M0-interception.md` records: a prefix is read wherever it matches.
  After a rewrite more than 20 blocks before a request's last breakpoint, the real Messages cache
  reads back only to an explicit breakpoint, so the harness cannot tell an engine that places one at
  the last unchanged block from one that does not. The cache-read share is an upper bound.
- The thinking check is the documented prefix rule alone. It does not model the per-identity binding
  the M0 probe saw on one route, `prefix_mismatch_behavior`, or `drop_block`.
- Thinking kept both before and after a stripped range is what a rewrite in the middle of a loop
  leaves behind. The research reads it as valid: when the strip is made it is a removal from the end
  of the history, and the later blocks are appends bound to the prefix as it was sent
  (`docs/research/M0-interception.md`). Whether an enforcing account accepts that shape inside a tool
  loop, and whether the model keeps thinking afterwards, only a live run can confirm. The harness
  counts how often it occurs.
- `inflight_thinking_missing` counts requests; whether the API then disables thinking is documented,
  not measured [vendor-documented; untested in a tool loop, `docs/research/M0-paging-policy.md`].
- The probes are scripted: the model asks for one known line at fixed points. Nothing models a model
  that decides for itself what to recall, and the rooms are spent only by the probes.
- A subagent's search cannot return a line that is still in the subagent's own request (above), so
  the recall rule lets a subagent be told "nothing" while its result is still whole in its request.
- No scenario has a rotation that keeps a window (`multiturn`'s is `window_minimal`), a resumed
  session, a proxy restart or a store written by an older release. The package's own tests cover
  those (`tests/test_rewrite.py`, `tests/test_epoch_resume.py`, `tests/test_store.py`).
- The scripts are synthetic: every result is new text, no file is read twice, and thinking is short
  (about 70 tokens; `small` about 30).
- One engine, one store and one upstream per run, in one process and one thread. The proxy's HTTP
  layer, stream hold and locks are not in the path.

## What D63 has to change

`spikes/m2/inturn/tests/test_inturn_targets.py` holds one test per scenario and dialect (13), each
marked `xfail(strict=True, raises=AssertionError)` and failing at v0.5.24. `targets.unmet(run)` is
empty when a run keeps every rule below; `spikes/m2/inturn/targets.py` has one function per rule.

A loop is one turn of one conversation after its first request. The turn's own items are the CLI's
items since the turn's prompt, in the upstream's tokens.

1. **Bound.** No request is over the bound: no `over_bound` event from the estimate or from real
   usage, and the largest forwarded request is at or under 150,000 by the estimate and by the
   upstream's count.
2. **Violations.** The upstream counts no violation of any kind.
3. **Byte-exact storage.** On every request, each tool result delivered so far is either whole in the
   forwarded request or in a store page byte for byte: a `tool_result` block (Codex: an `*_output`
   item) with that call's id whose content is the original's, compared as canonical JSON.
4. **Stubs.** Every stub names the calls it stands for, and for each of them the page it names holds
   the original byte for byte, or holds a stub for that call which leads, through at most 8 pages, to
   a page that does. A stub may name no page only for the result of one of Contexto's own search,
   recall or state calls. At least one stub has to be found.
5. **Thinking.** In a loop, the newest response comes back without its thinking on no more requests
   than the loop has prefix rewrites; thinking missing anywhere a rewrite does not cover is a
   violation (rule 2). Stripping all thinking on every request fails both.
6. **Seen before stubbed.** A result is whole in the request that first carries it, unless the head,
   the turn's prompt and that request's new results together come to at least 90,400 tokens, which is
   0.8 of `bound - headroom_floor - min_turn_room` (113,000); the 0.8 allows for the engine's estimate
   differing from the upstream's count. In `large` on Claude Code that sum is 75,347 for the request
   that brings the 60,000 result, so the model has to be shown it once; it is 135,347 and 215,347 for
   the 120,000 and the 200,000 results, which may be stubbed on arrival.
7. **Cache cost.** A loop has at most 2 prefix rewrites plus one for each 25,000 tokens (`bound / 6`)
   of its own items, and no request in a loop is a rewrite right after another rewrite unless the
   previous forwarded request plus the items added since is over the bound. The re-sent share is
   reported per scenario and has no ceiling: in a short run such as `large` it is dominated by the
   number of requests.
8. **Room.** Every main request in a loop publishes a room before the send and after the response,
   and every subagent response with a tool call publishes a side room. Once the turn's own items pass
   50,000 tokens the request-time room is never under 12,000 (`min_turn_room`), and the
   response-time room is never 0 and is at least 6,000 on 95% of those requests.
9. **Search and recall.** Late in a loop, the search for a line of an early result returns that line,
   from the request in view or from a page; when the result is no longer whole in the request the
   search names a page, and recalling that page at the offset the search gave returns the line and
   the two lines after it as the original has them. A subagent whose result is still whole in its own
   request may be told that no page holds the line (see Limits).
10. **`count_tokens`.** A `count_tokens` request with the same history gets the transform that was
    just forwarded and publishes no room (contract rule 11).

Rule 8 follows from the numbers: a request-time room of 12,000 means the estimate of every loop
request past 50,000 tokens is at most 113,000.

Each rule has an engine that breaks it and fails (`spikes/m2/inturn/tests/test_inturn_engines.py`):
one whose stubs name a page that holds none of the results, one that strips all thinking on every
request, one that stubs every result on arrival, one that rewrites the prefix on every request, one
that publishes no room, one whose rooms are 1 token, and one that answers `count_tokens` with the
CLI's own request.

### The rules can be kept together

`spikes/m2/inturn/toy.py` is a pager written for the harness's tests. It is not the D63 design: it
sizes requests with the upstream's own tokenizer, never removes a round, and keeps no state across a
restart. It stubs a result on arrival when rule 6 allows it, and otherwise stubs the oldest results
in a batch when a request would pass 100,000 tokens, down to 38,000. It meets every rule on every
scenario and dialect at the sizes the target tests run:

| scenario | dialect | largest request | pages | rewrites in a loop | re-sent share | cache read |
|---|---|---|---|---|---|---|
| loop120 | claude | 98,501 | 118 | 11 | 0.5% | 89.3% |
| loop120 | codex | 99,583 | 118 | 11 | 0.7% | 89.1% |
| large | claude | 86,527 | 2 | 0 | 0.0% | 91.7% |
| large | codex | 84,816 | 2 | 0 | 0.0% | 91.6% |
| small (260) | claude | 99,672 | 250 | 2 | 0.2% | 98.7% |
| small (260) | codex | 99,906 | 254 | 2 | 0.3% | 98.7% |
| subagent | claude | 99,471 | 149 | 11 | 0.6% | 92.6% |
| images | claude | 99,278 | 92 | 5 | 0.4% | 93.6% |
| images | codex | 99,216 | 94 | 5 | 0.4% | 93.4% |
| multiturn | claude | 99,952 | 99 | 7 | 0.6% | 92.0% |
| multiturn | codex | 99,203 | 99 | 7 | 0.7% | 91.8% |
| mixed | claude | 97,795 | 42 | 3 | 0.7% | 90.5% |
| mixed | codex | 99,876 | 68 | 3 | 1.1% | 90.3% |

Every probe in these runs was answered: the searches returned the line from a page (`large`: from the
view) and the recalls returned the original lines.
