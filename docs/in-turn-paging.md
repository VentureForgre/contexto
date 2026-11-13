# In-turn paging

Contexto pages old turns out of the request when a new turn starts. A single turn can also outgrow
the bound on its own: one prompt followed by a long tool loop (dozens of file reads, a test run that
prints megabytes), or a subagent, whose whole life is one turn. Since v0.6.0 Contexto pages inside
the running turn as well, so the bound holds there too. This page says what the model sees, how it
reads a paged result back, how to turn the feature off, and what it does not do.

It applies to Claude Code (the Messages API). Codex tool loops are still sent whole.

## When it runs

Inside a tool loop, when the next request would be larger than

    bound - headroom floor - minimum turn room    (150,000 - 25,000 - 12,000 = 113,000 by default)

Past that point the model would have less than 12,000 tokens of room for `contexto_recall` and
`contexto_search`, so they would be refused when the turn is longest. Contexto then pages one batch,
down to about halfway between what it cannot page and that line, so the next batch is far away. In
a replay of 120 tool rounds of about 6,400 tokens each, a batch ran every 7 rounds.

A batch does these in order and stops as soon as it has freed enough:

1. Replaces older tool results with stubs, oldest first. Results the model has not read yet are
   left alone.
2. Pages the earlier turns still in the window, as a turn start would.
3. Folds the oldest tool rounds of the running turn into one page and leaves a note in the prompt
   message.
4. If the request is still over the bound, replaces results the model has not read yet with stubs,
   largest first. A single result too large to fit at all (a 500 KB file) is stubbed first, before
   anything else is touched.

## What a stub looks like

The tool result keeps its place, its `tool_use_id` and its error flag. Its content becomes:

```
[Contexto paged out this tool result to keep the request under its bound. Nothing was lost.
tool: Read
file_path: /repo/src/contexto/rewrite.py
was: 20,480 bytes, 353 lines
page: pg_3f9c1a7b22d04e61
To read it, call contexto_recall with page_id "pg_3f9c1a7b22d04e61" (pass the next offset to continue a long page). contexto_search finds lines in it. You can also run the tool call again.]
```

The second line names the tool. The third is the call's path, command, pattern, URL or query, cut to
200 characters. A result that held images says how many; recall shows a page's text only, so the
model has to run the call again to see an image.

Results of Contexto's own `contexto_search`, `contexto_recall` and `contexto_state` calls are copies
of stored pages, so they get no page of their own. Their stub gives the call's arguments:

```
[Contexto dropped this result of its own tool to keep the request under its bound. It was a copy of stored pages.
tool: contexto_recall
arguments: {"offset":0,"page_id":"pg_3f9c1a7b22d04e61"}
was: 31,203 bytes, 500 lines
To get it back, call the tool again with the same arguments.]
```

Folded rounds leave a note after your prompt in the turn's first message:

```
[Contexto folded 212 earlier tool rounds of this turn into page pg_8e04b6d1c57a9f30 to keep the request under its bound. Nothing was lost.
tools: Grep x180, Read x32
files: src/a.py, src/b.py, and 30 more
was: 148,224 bytes
To read them, call contexto_recall with page_id "pg_8e04b6d1c57a9f30" (pass the next offset to continue; the page is long). contexto_search finds lines in it.]
```

## Reading a result back

The stored page holds the tool call and its result exactly as Claude Code sent them.

- The model calls `contexto_recall` with the page id from the stub. A long page comes in chunks;
  each reply gives the offset of the next one.
- `contexto_search` finds lines inside paged results, like any other page.
- From a terminal: `contexto recall --session <id> pg_3f9c1a7b22d04e61`.
- The page index in the prompt lists whole turns and folded rounds. It does not list single
  results, which would crowd out everything else; the stub in the request names each one.

## What it costs

On the 5.x models the API refuses a thinking block if anything before it changed. So when a batch
changes a message, Contexto removes the thinking blocks from that message to the end of the request,
on that request and every later one. Thinking before the first changed message, and in everything
the model writes afterwards, is kept. A batch also changes the cached prefix, so that request is
written to the cache again instead of read from it.

In the 120-round replay the 16 batches re-sent between 2.2% and 9.6% of all tokens sent (the lower
figure if the cache still holds the prefix before the first changed message, the higher if only the
system prompt and tools are read from cache).

A result that was stubbed before the model read it changes nothing before any thinking block and
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

## In the session log

- `in_turn_batch`: one per batch, with what triggered it (`room`, `bound`, or `result` for a result
  stubbed before it was read), how many results were stubbed, rounds folded and turns paged, the
  estimate before and after, and the first changed message.
- `in_turn_exhausted`: the request is over the bound and there was nothing left to page. It comes
  with `over_bound`.

## Limits

- Not confirmed against the live API yet: that an account which enforces preserved thinking
  accepts a request inside a tool loop whose earlier tool result was stubbed and whose later
  thinking was removed, and that the model keeps thinking in the rest of that turn. The rule
  Contexto follows is the documented one, and a test upstream that enforces it accepts every request;
  `docs/DECISIONS.md` D63 gives the live check. If a tool loop fails with a 400 that says a
  thinking block is "bound to a different conversation", turn in-turn paging off and report it.
- Codex is not covered.
- A result is stubbed only if it is at least 2,048 bytes and its content is text or images. Tool
  search results and documents are never stubbed.
- A tool round that brought in a nested `CLAUDE.md` is never folded, so its rules stay in the
  request.
- A prompt that is itself larger than the bound cannot be paged; that is still `bound_too_small`.
- When the system prompt and tools alone come close to the trigger (a bound under about 70,000 with
  Claude Code's default tools), a batch runs only once it can free 12,000 tokens, so the recall room
  can be under 12,000 between batches.
- After a proxy restart a subagent's in-turn state is not restored: its next request over the
  trigger pages again and loses the loop's thinking once more. The main conversation resumes
  exactly.
- An in-turn page written by this release is read by v0.5.24 and earlier as an ordinary page:
  recall and search work, and the older release's page index also lists single results.
