# In-turn paging

Contexto pages old turns out of the request when a new turn starts. A single turn can also outgrow
the bound on its own: one prompt followed by a long tool loop (dozens of file reads, a test run that
prints megabytes), or a subagent, whose whole life is one turn. Contexto also pages inside
the running turn as well, so the bound holds there too. This page says what the model sees, how it
reads a paged result back, how to turn the feature off, and what it does not do.

It applies to Claude Code (the Messages API). Codex tool loops are still sent whole.

## When it runs

Inside a tool loop, when the next request would be larger than

    bound - headroom floor - minimum turn room - 3,000    (150,000 - 25,000 - 12,000 - 3,000 = 110,000 by default)

Past 113,000 the model would have less than 12,000 tokens of room for `contexto_recall` and
`contexto_search`, so they would be refused when the turn is longest; the last 3,000 are kept for
the model's own reply. Contexto then pages one batch, down to a quarter of the way from what it
cannot page up to that line, so the next batch is far away. In a replay of 120 tool rounds of about
6,400 tokens each, a batch ran about every 11 rounds (11 in all).

The model always gets to read a tool result once. A result that has just arrived is paged before
the model reads it only when it could not be sent at all: when the system prompt, your prompt and
the new results together would not fit even with everything older paged (a 300 KB dump, for
example). The limit is on the sum of the results that arrived together: two parallel reads of 100 KB
each can fit one at a time but not both, and then only as many are paged as needed, largest first;
the rest are read whole. Then, sized from the request with any such result already a stub, Contexto
makes room by paging older content, in this order, stopping as soon as it has freed enough:

1. Replaces older tool results with stubs, oldest first.
2. Pages the earlier turns still in the window, as a turn start would.
3. Folds the oldest tool rounds of the running turn into one page and leaves a note after your
   prompt in the turn's first message.

If the request is still over the bound after that, the results the model has not read yet are
replaced by stubs, largest first.

With a small bound, or a system prompt and tools that fill most of it, the line above leaves almost
no space: at a bound of 60,000 the default floor and turn room put it under a 20,000-token system
prompt. When less than 24,000 tokens (two minimum turn rooms) are left between the system prompt plus
your prompt and `bound - headroom floor - minimum turn room` (113,000 by default), Contexto holds
requests under the bound less the headroom floor and 3,000 instead (32,000 at a bound of 60,000), and
a new result is read once if it fits there. The room for `contexto_recall` on those requests can be under 12,000; the session log
says so once per turn (`turn_room_low` with `reason: "in_turn_small_bound"`).

What you type during a tool loop stays in the request. When you press Esc and type an instruction,
Claude Code sends it next to a tool result, after `[Request interrupted by user ...`; a screenshot you
paste there arrives the same way. A tool round holding such text, an image or a document is never
folded: it stays where it was, whole, and the folds before and after it leave it in place. Its tool
results can still be stubbed; your text and images next to them are kept as they are.

## What a stub looks like

The tool result keeps its place, its `tool_use_id` and its error flag. Its content becomes:

```
[Contexto paged out this tool result to keep the request under its bound. The original is stored in page pg_3f9c1a7b22d04e61.
tool: Read
file_path: /repo/src/contexto/rewrite.py
was: 20,480 bytes, 353 lines
page: pg_3f9c1a7b22d04e61
If you have the contexto_recall tool, call it with page_id "pg_3f9c1a7b22d04e61" to read it (pass the next offset to continue a long page); contexto_search finds lines in it. Without it, run the tool call again.]
```

The second line names the tool. The third says what the call ran or read: the command of a shell
call, the pattern of a search and where it searched (`pattern: def main (in src/)`), the URL, or
the path of a file. It is exact except that a line break becomes a space; past 200 characters its
middle is cut out and marked (`[... 114 characters ...]`), so the start and the file name stay. A
result that held images says how many; recall shows a page's text only, so the model has to run the
call again to see an image. A result that held a document (a PDF from `Read`) is stubbed once the
model has read it, and its stub says so, with the page count when it is certain and the size:
`was: 0 bytes, 0 lines, 1 document, 10 pages, 41,233 bytes (the page keeps it byte for byte, but
recall shows text only, not a document's contents; run the call again to see the document)`. A
document too large for the bound on its own, by a page count Contexto is certain of, is stubbed
before the model reads it. A PDF whose pages Contexto cannot count for certain is counted at the
API's maximum of 600 pages so that it is never estimated low, which says nothing about its real
size: it is sent whole once, as without in-turn paging (the session log records
`pdf_pages_uncertain`), nothing older is paged to make room for that estimate, and a later batch may
stub it once the model has read it. If the API's count then shows that request over the bound, the
log records `over_bound`. Tool search results (`tool_reference`) are never stubbed.

The wording is the same in every conversation. A subagent given only some tools, or a session where
tool search has deferred Contexto's tools, may not have `contexto_recall`; the stub then tells the
model to run the call again, and the call is still in the request right above the stub.

Results of Contexto's own `contexto_search`, `contexto_recall` and `contexto_state` calls are copies
of stored pages, so they get no page of their own. Their stub gives the call's arguments:

```
[Contexto dropped this result of its own tool to keep the request under its bound. It was a copy of stored pages.
tool: contexto_recall
arguments: {"offset":0,"page_id":"pg_3f9c1a7b22d04e61"}
was: 31,203 bytes, 500 lines
To get it back, call the tool again with the same arguments.]
```

Folded rounds leave one note after your prompt in the turn's first message, however many folds the
turn has. Each fold rewrites it:

```
[Contexto folded 52 earlier tool rounds of this turn into page pg_8e04b6d1c57a9f30 to keep the request under its bound. They are stored in page pg_8e04b6d1c57a9f30.
tools: Grep x40, Read x12
calls: Grep pattern: TODO (in src/a.py); Grep pattern: TODO (in src/b.py); Read file_path: src/c.py; ...; and 12 more calls
call ids: toolu_01Ab... toolu_01Cd...; and 9 more, in the same page
was: 48,224 bytes
earlier folds of this turn: rounds 1 to 160 in 34 pages listed in page pg_51c0e2a9d8b74f13; rounds 161 to 198 in pg_2b7d06a1e94c3f58; rounds 199 to 212 in pg_7c19e5b0a2d48f63 (ids: toolu_01Ef... toolu_01Gh...)
If you have the contexto_recall tool, call it with a page_id named here to read them (pass the next offset to continue; a page is long); contexto_search finds lines in them. Without it, you can run again the calls listed here; the other calls of these rounds are named only in those pages.]
```

The newest fold is described in full: the `calls:` line lists its first 40 calls (at most 1,600
characters), each with what it ran or read, so a conversation without Contexto's tools can run them
again; the note says that only those can be. Earlier folds are listed by their rounds and page. The
note is never more than 6,000 bytes (about 2,000 tokens), call ids included: it lists the ids of the
newest folds only, and each page it names holds, or leads to, every result of the rounds it stands
for. When the note would pass that size, the notes of the folds since the previous folds page, each
with every call id and its page, go into a new folds page (not listed in the index; recall reaches
it) and the note names that page instead. Each fold's note is stored once. When the note names
eight folds pages, an index page lists them and the note names it instead (`listed under page`);
eight index pages get an index page of their own, and so on. The note then names a few pages
however long the loop, and every result is a few pages from it: at 2,000 folds, 950,843 bytes of
folds pages in all, about what the folds' own notes take once.

## Reading a result back

The stored page holds the tool call and its result exactly as Claude Code sent them.

- The model calls `contexto_recall` with the page id from the stub. A long page comes in chunks;
  each reply gives the offset of the next one.
- `contexto_search` finds lines inside paged results, like any other page. A hit from a page written
  inside a turn is marked with that turn, `[paged inside turn 4, tool result]` (or `folded rounds`,
  `list of folds`): it can be newer than an older copy of the same file still in view, so the model
  is told to prefer the newest by turn, then by position. The mark is the same in every later turn.
- From a terminal: `contexto recall pg_3f9c1a7b22d04e61 --session <id>` (add `--offset N` to read on).
- The page index in the prompt lists whole turns and folded rounds. It does not list single
  results, which would crowd out everything else; the stub in the request names each one.

## What it costs

On the 5.x models the API refuses a thinking block if anything before it changed. So when a batch
changes a message, Contexto removes the thinking blocks from that message to the end of the request,
on that request and every later one. Thinking before the first changed message, and in everything
the model writes afterwards, is kept. A batch also changes the cached prefix, so that request is
written to the cache again instead of read from it.

Once a turn's first batch has run, every later request of that turn is sent with some of the turn's
thinking missing: 110 of 125 requests in the 120-round replay, 136 of 214 for a subagent's loop.
That is the price of the rule and the part not yet confirmed against the live API (see "Limits").
The model's newest thinking was removed only on the batch requests themselves, 11 of 125.

How much a batch costs in cache depends on what the API still reads back. The cache keeps a prefix
only at a `cache_control` mark; Claude Code marks its system prompt and the newest message, so after a
batch only the system prompt and tools are read back and the messages are sent again. The 120-round
replay measures that two ways. Counted request by request, the 11 batch requests sent 259,798 tokens
uncached beyond their new results (23,618 a batch on average): 2.9% of the 9,072,693 input tokens of
the whole replay. Under the replay harness's cache model, where a prefix is kept only at a
`cache_control` mark, the loop re-sent 331,232 tokens in all, which is 0.41 of its cost ceiling: the
larger of the bound and the loop's own 803,477 tokens. Each batch pages deep (a quarter of the way up to the line, not
halfway) because that cost is the request's size after the batch: halfway re-sent about 60% more,
with two more batches. Contexto does not rewrite the prefix on two requests in a row unless the second would
leave less than 12,000 tokens of room, and not at all for less than 12,000 tokens.

A result that was paged before the model read it changes nothing before any thinking block and
costs neither.

## Turning it off

```sh
contexto run --no-in-turn-paging claude
CONTEXTO_IN_TURN_PAGING=0 contexto run claude      # also: false, off, no
```

With it off, a turn that grows past the bound inside a tool loop is sent whole, as in v0.5.24: the
session log records `over_bound` with `in_tool_loop: true` for each such request, and the next turn
start pages the turn. A session resumed with a different setting starts a new epoch (one cache
miss).

Restarting with `--no-in-turn-paging` in the middle of a tool loop that was paging sends that turn's
raw history: the stubs and folds are gone, the request can be over the bound until the next turn
start, and thinking that was produced while stubs stood before it goes out again, which an account
that enforces preserved thinking may refuse. Switch it off between turns when you can.

## In the session log

- `in_turn_batch`: one per batch, with what triggered it (`room`, `bound`, or `result` for a result
  stubbed before it was read), how many results were stubbed, rounds folded and turns paged, the
  estimate before and after, and the first changed message.
- `in_turn_exhausted`: the request is over the bound and there was nothing left to page. It comes
  with `over_bound`.
- `in_turn_thinking` on a `request` event inside a loop that has lost thinking: how many thinking
  blocks were kept before the first edit, stripped, and kept after it. Counts only.
- `turn_room_low` with `reason: "in_turn_small_bound"`: once per turn, when a small bound leaves the
  recall room under 12,000.

## Limits

- Not confirmed against the live API yet: that an account which enforces preserved thinking
  accepts a request inside a tool loop whose earlier tool result was stubbed and whose later
  thinking was removed, and that the model keeps thinking in the rest of that turn. The rule
  Contexto follows is the documented one, and a test upstream that enforces it accepts every request;
  `docs/DECISIONS.md` D63 gives the live check. If a tool loop fails with a 400 that says a
  thinking block is "bound to a different conversation", turn in-turn paging off and report it.
- Codex is not covered.
- If the session store fails while a batch writes its pages (a full disk, for example), the request
  is sent unpaged and the session enters degraded mode. Each later request of the loop that is over
  the trigger tries the store again; the first that succeeds leaves degraded mode and pages. A turn
  start tries too.
- A text result is stubbed only if it is at least 2,048 bytes; a result with an image or a document
  whenever the stub frees something. Tool search results are never stubbed.
- A tool round that brought in a nested `CLAUDE.md` is never folded, so its rules stay in the
  request.
- A prompt that is itself larger than the bound cannot be paged; that is still `bound_too_small`.
- With a small bound or a large system prompt (see "When it runs"), the recall room can be under
  12,000 tokens, and batches come more often: with a 92,000-token system prompt at a bound of
  150,000, a loop of 20 KB reads rewrites the cached prefix about every three rounds.
- A result that has just arrived is counted at 1.8 bytes per token until the API has reported its
  real size, so that a dense one never takes the request over the bound. For sparse text (plain
  prose, most source code) that is about twice its real size, so results that arrive together and
  add up to more than about 160 KB can be paged before the model reads them although they would
  have fit. Measured: two parallel reads of 100 KB of prose, 81,854 tokens by the API's count at a
  bound of 150,000, and one of them was stubbed unread. The stub says how to read it, so the cost is
  an extra round, not a wrong answer. The room published for recall counts such a result the same
  way.
- After a proxy restart a subagent's in-turn state is not restored: its next request over the
  trigger pages again and loses the loop's thinking once more. The main conversation resumes
  exactly.
- An in-turn page written by this release is read by v0.5.24 and earlier as an ordinary page:
  recall and search work, and the older release's page index also lists single results.
