# M2: Contexto at 30M tokens of history (zero spend)

Date: 2026-10-18. Main at 688b625 (v0.2.7 code). No model calls: every run used a local fake
upstream or a local store. Synthetic sessions, one machine (Apple M5 Pro, 18 cores, 48 GB), load
averages 2.6 to 9.2 because the three harnesses ran at the same time. Harnesses, commands, and raw
results: `spikes/m2/scale/cli/`, `spikes/m2/scale/proxy/`, `spikes/m2/scale/store/`.

The question: can a Claude Code session grow to about 30M tokens of history under Contexto, where
the largest live run so far was about 494K (run #3)?

## Answer

Yes for the request path, with one blocker; search is fast enough but its accuracy falls with
page count.

| Layer | At 30M | What breaks first |
|---|---|---|
| Claude Code itself | resumes and resends 30M in a 52.6 MiB request, 0.7 s to first request, about 1 GB RSS, no compaction | "API Error: Out of memory" from 550M up (525M worked, 918 MiB request) |
| Proxy request path | every upstream request under the 150,000 bound; adds 0.58 s per tool-loop request and 0.86 s per turn start | carried `<system-reminder>` blocks from later turn starts are never paged: `bound_too_small` from 4.86M and requests over the bound from 8.20M in the assumed profile |
| Store and search | 0.71 s per search at 6,000 pages (30M is about 1,150 to 2,900 pages: 1,145 in the proxy run at about 26K tokens per page) | accuracy: the planted page is in the 3 detailed results for 61 of 86 queries at 17 pages, 50 at 1,000, 44 at 3,000 |

## Claude Code (spikes/m2/scale/cli)

Claude Code 2.1.302, `claude -p "continue" --resume <id>`, temp HOME and config dir, fake key; 3,141
`lsof` samples saw only 127.0.0.1. The fake upstream reported 100,000 input tokens per request, as
Contexto's bounded request would.

| History | Request MiB | Launch to first request (s) | CLI peak RSS MB | Compactions |
|---|---|---|---|---|
| 0.5M | 0.95 | 0.20 | 241 | 0 |
| 5M | 8.81 | 0.34 | 404 | 0 |
| 30M | 52.58 | 0.70 | 958 | 0 |
| 100M | 175.02 | 1.62 | 2,488 | 0 |
| 525M | 918.46 | 6.63 | 9,662 | 0 |
| 550M to 700M | no request sent | | 7,256 to 9,204 | 0, exit 1 "API Error: Out of memory" |

- With honest usage (reported tokens equal to the real size), the 1M-window models compacted from
  968,037 tokens and `claude-haiku-4-5` from 194,502, so the CLI decides from the reported usage, as
  M0 found. With Contexto-like usage, no compaction to 525M on `claude-opus-5-5` and to 30M on both
  Haiku models, including tool loops; `count_tokens` was never called.
- Not explained: on resume at 30M and above the CLI appends about half the history back to the
  session file (27.6 MB at 30M; then 14.8 and 8.1 MB on two more resumes); at 10M and below about
  9 KB.
- Not tested: idle compaction (interactive only; read from the binary, it uses the last reported
  usage and is off unless a remote flag enables it), and one very large tool result after a small
  reported usage (D31's case).

## Proxy (spikes/m2/scale/proxy)

The real proxy class, session log, and rewrite engine wired as `launcher.start_proxy` wires them,
over HTTP, against a fake upstream that counts usage as forwarded bytes / 1.83. 0.5M ran every
request over HTTP; 2M to 30M were fast-forwarded by calling the engine at turn starts only, then
measured over HTTP in a fresh proxy. A 120-turn check against the all-HTTP path matched every page
and next-turn byte count; one rotation came a turn later (71 tokens on a 113,000 threshold) and
the paths re-converged within 3 turns.

Reminder on the first message only (the shape `spikes/m2/carried` measured); medians of 3 runs:

| History | Body MB | Turn start (s) | Tool loop (s) | Rotating turn start (s) | Max upstream tokens | Peak RSS MB | Pages |
|---|---|---|---|---|---|---|---|
| 0.5M | 0.9 | 0.056 | 0.014 | 0.110 | 134,587 | 44 | 22 |
| 2M | 3.7 | 0.099 | 0.044 | 0.156 | 134,587 | 180 | 80 |
| 5M | 9.2 | 0.103 | 0.101 | 0.230 | 135,954 | 522 | 192 |
| 10M | 18.3 | 0.278 | 0.194 | 0.358 | 135,954 | 1,337 | 382 |
| 30M | 55.0 | 0.861 | 0.580 | 0.867 | 135,954 | 1,707 | 1,145 |

No `over_bound`, `bound_too_small`, or `transform_error` at any checkpoint; `window_minimal` 1, 2,
and 5 times by 5M, 10M, and 30M (synthetic turns larger than the window). Over 79 requests in one
proxy at 30M, RSS settled at 2.1 to 3.1 GB (peak 3.45 GB) with live objects flat: allocator
retention, not a leak.

Where the time goes (cProfile at 10M): hashing every message twice per request 47 percent
(`rewrite.py:709-715` and `:973`; sorted-key JSON encoding alone 34), serializing the request twice
16 (`proxy.py:820`, `:849`), parsing the JSON twice 14 (`proxy.py:809`, `:821`), scanning history for
turn starts 11, and `index_text` on rotation 8. All of it is linear in history, about 0.019 s per
million tokens per tool-loop request.

The blocker: carried content. Every `<system-reminder>` block on a paged turn start is carried
forward (`rewrite.py:493`), and the 8,000-token fallback pages carried system messages, never
context blocks (`rewrite.py:671-701`). With an assumed profile of a todo-style reminder on 35
percent of later turn starts and a file-modified one on 10 percent (an assumption, not measured),
the fixed head grows about 215 tokens per turn: the engine logs `bound_too_small` from 4.86M (turn
181), the first request over the bound is at 8.20M (turn 308, 150,046 tokens), and at 10M 7 of 55
measured requests were over (up to 159,255). How often real sessions add reminders after the
first message is not measured here; the fix below removes the failure whatever the rate.

## Store and search (spikes/m2/scale/store)

Pages written through `SessionStore.page_out` (about 23K tokens per synthetic turn); search and
recall through a real `contexto mcp` child over stdio. Medians of 3 passes of 5 calls.

| Pages | Search (s) | Recall (s) | `index_text` (s) | MCP peak RSS MB | Store MB |
|---|---|---|---|---|---|
| 17 | 0.002 to 0.004 | 0.0005 | 0.035 | 34 | 0.8 |
| 1,000 | 0.105 to 0.110 | 0.0005 | 0.120 | 103 | 42 |
| 3,000 | 0.318 to 0.340 | 0.0006 | 0.309 | 245 | 127 |
| 6,000 | 0.677 to 0.711 | 0.0006 | 0.626 | 456 | 255 |

Search reads and parses every page on every call (about 55 percent of its time) and walks every
message block (33 percent), about 0.115 ms per page per call.

Quality: 30 planted facts, 86 queries written before any result was seen and fixed across sizes.
Distractor rates came from word frequencies in the owner's local Claude Code transcripts (counts
only; `calibration.json` holds the probe phrases and their rates, no transcript text).

| Pages | Planted page in the 3 detailed results (of 86) | Latest value first (of 24) | Facts in the detailed results for their best query (of 30) |
|---|---|---|---|
| 17 | 61 | 19 | 30 |
| 1,000 | 50 | 17 | 28 |
| 3,000 | 44 | 14 | 27 |
| 6,000 | 39 | 12 | 26 |

About 25 queries miss at every size because matching is exact and case-sensitive; the rest of the
loss grows with page count because results are newest first with no ranking, so common words
(`timeout`, `region`) fall out of the reply by 1,000 to 3,000 pages while rare identifiers still
come first at 6,000. If a session keeps returning to one topic (modelled as every query on at
least 1 page in 500, an assumption), 6,000 pages leave 15 of 86 queries in the detailed results.

## What to fix, in order

1. Page carried reminders (blocker). Apply the bounded fallback to carried context blocks, not only
   to carried system messages, so the head cannot grow without bound. Measure the real rate of
   later-turn reminders first, to size the budget.
2. Search ranking. BM25 over case-insensitive words (prototyped: 57 of 86 queries and 29 of 30 facts
   in the detailed results at 6,000 pages, against 39 and 26) breaks "latest value first" (7 of 24
   against 12), so it needs a recency term; that combination is not measured.
3. Search and rotation speed. Cache each page's searchable text in the MCP process (pages are
   immutable; prototyped: 0.007 to 0.065 s per search at 6,000 pages, identical results, after a
   1.06 s first load and 196 MB held), and give `index_text` a stub manifest instead of re-reading
   every page twice per rotation (estimated, not measured). An SQLite FTS5 trigram index matched
   today's results with rare queries under 1 ms, for 1.74 ms per page write and an index 2.5 times
   the store.
4. Proxy CPU. Hash each message once and reuse it (about 23 percent of transform time), and parse
   the body once. Not needed for 30M (0.58 s per tool-loop request), but it is the next ceiling.

Limits: synthetic sessions, one run per size for the CLI, fast-forwarded proxy checkpoints above
0.5M, and recall accuracy measured only for the search tool, not for a model using it.
