# Store scale: search, recall, and index at 17 to 6,000 pages

Question from the owner: can Contexto's store carry a session of about 30M tokens, roughly 1,000 to
6,000 pages? This spike measures speed and retrieval quality of the store as it is on `main`
(4525388), offline, with zero model spend. Nothing in `src/` changes. Every number below is
measured unless it is marked as an inference.

## Command

```
uv run python spikes/m2/scale/store/scale_bench.py          # about 10 minutes on an M5 Pro
uv run python spikes/m2/scale/store/scale_bench.py --sizes 17,1000,6000 --responses-sizes "" \
    --passes 0 --profile-n 0 --floor 0.002 --out spikes/m2/scale/store/results/floor_0.002
uv run python spikes/m2/scale/store/scale_fts_proto.py --sizes 1000,6000   # fix prototypes
```

Without `uv`: `python3 -m venv .venv && .venv/bin/pip install -e . pytest`, then run the same
scripts with `.venv/bin/python`. Stores are written under a fresh temporary `CONTEXTO_HOME` and
deleted at the end (`--keep` keeps them). `~/.contexto` is never touched.

`scale_calibrate.py` produced `calibration.json` once from the local Claude Code transcripts
(`~/.claude/projects`, `~/.claude_t3_proxy/projects`: 176 files, 4,974 turns). It writes numbers
only, never transcript text. Re-running it on another machine changes the distractor rates.

## Method

- Pages are written by `SessionStore.page_out`, the call the engine makes when it pages turns
  (one turn per call here; the engine batches, and each page is built the same way).
- Turns are synthetic Claude Code turns in the shape the engine pages (`strip_carried` output):
  a user prompt, then 4 to 14 tool calls (median 9; Read, Grep, Bash with pytest and git output, Edit) with
  thinking blocks, tool_use inputs, and tool_result file contents and command output, then a
  closing assistant text. Each turn is sized to about 19,000 to 30,000 tokens under contexto's
  estimator (median 22,882). The Codex variant maps the same turns to Responses items (message,
  reasoning, function_call, function_call_output).
- Speed: `contexto_search` and `contexto_recall` go through a real `python -m contexto mcp
  --session S` child over stdio with `CONTEXTO_HOME` set, the way `launcher.mcp_payload` spawns it
  (JSON-RPC `initialize`, then `tools/call`), and also in-process through `contexto.mcp.Server`.
  Before every call the harness publishes a retained-window snapshot and a turn room, as the engine
  does per request, so search charges the room and checks the conversation in view. The room
  never runs out, so the 32,000-character per-call limit binds. `index_text(4000)` runs
  in-process. Peak RSS of the MCP child comes from `wait4`. Each figure is the median of 5 calls
  per query, the median over the 3 queries of a kind, and the median over 3 passes.
- Quality: 30 facts at fixed fractions of the session (18 static, 4 renames, 8 values that change
  3 times, so only the last is right), 86 probe queries in all (2 or 3 per fact). The queries are
  in `scale_probes.py`. I wrote them from each question, as a model would type them, before I
  looked at any result, and I have not changed them since. Each query goes through
  `contexto_search`, and the reply text is parsed for the planted page: among the 3 detailed hits,
  among the 50 listed pages, or absent. An absent page can be buried (it matches, but more than 53
  newer pages also match) or the query may not be on the page at all. The harness also records
  whether the reply shows the planted value, and for changing values whether the first value page
  among the detailed hits is the latest.
- Distractors: background text never contains a probe query or a planted marker. Each probe query
  is then put on each page with probability equal to its share of real turns that contain it
  (`calibration.json`: for example `timeout` 13.5 percent, `region` 4.2, `rate limit` 1.25,
  `shard map` 0.48, most multi-word queries 0). Real against synthetic frequency per query is in
  `results/df.csv`. Renamed identifiers appear in code on 4 percent of turns, the old name before
  the rename and the new name after it (assumption). The `floor_0.002` run raises every query to
  at least 1 page in 500. That models a session that keeps returning to the same subsystem, which
  a corpus of unrelated sessions cannot show (assumption).
- Machine: Apple M5 Pro, 18 cores, 48 GB, macOS 26.6.1, Python 3.14.6, SQLite 3.53.4, warm
  OS page cache. Other agents were benchmarking at the same time. The 1-minute load average
  during each timing pass is in `latency.csv` and in the table below.

## Results

### Store size (`results/stores.csv`)

| N | pages | MB on disk | page tokens min / median / max | total page tokens | page_out ms per page |
|---:|---:|---:|---|---:|---:|
| 17 | 17 | 0.8 | 20,002 / 23,951 / 27,932 | 407,866 | 1.62 |
| 100 | 100 | 4.3 | 19,530 / 22,975 / 27,950 | 2,304,319 | 1.49 |
| 500 | 500 | 21.1 | 19,326 / 22,814 / 29,684 | 11,495,472 | 1.52 |
| 1,000 | 1,000 | 42.4 | 19,326 / 22,853 / 29,684 | 23,002,132 | 1.49 |
| 3,000 | 3,000 | 127.4 | 19,194 / 22,889 / 29,684 | 69,048,523 | 1.48 |
| 6,000 | 6,000 | 254.7 | 19,194 / 22,882 / 29,684 | 138,094,580 | 1.54 |
| 1,000 (Codex) | 1,000 | 40.3 | 150 / 408 / 652 (see note) | 409,385 | 1.37 |

A page is about 40 KB, so a store grows by about 42 MB per 1,000 pages. Note: the `tokens` field
of a Codex page is about 400 because `estimate_message_tokens` counts only `content`, and
function_call and function_call_output items keep their text elsewhere. Nothing in `src/` reads
that field, as far as `grep` shows.

How many pages is 30M tokens? (inference) At this spike's 23,000 tokens per page, about 1,300
pages. At the real mean of 10,431 tokens per turn in the calibration corpus (median 1,947), about
2,900 pages.

### Speed (`results/latency.csv`; seconds)

| N | search rare (stdio) | search common (stdio) | search multi-word (stdio) | recall one page (stdio) | search rare / common / multi (in-proc) | index_text (in-proc) | MCP startup | MCP peak RSS MB | load1 |
|---:|---:|---:|---:|---:|---|---:|---:|---:|---|
| 17 | 0.002 | 0.004 | 0.002 | 0.0005 | 0.002 / 0.004 / 0.002 | 0.035 | 0.053 | 34 | 2.69 |
| 100 | 0.010 | 0.015 | 0.011 | 0.0006 | 0.011 / 0.015 / 0.011 | 0.041 | 0.054 | 40 | 2.63, 2.69 |
| 500 | 0.054 | 0.055 | 0.055 | 0.0005 | 0.054 / 0.056 / 0.055 | 0.077 | 0.055 | 68 | 2.7, 2.8, 2.9 |
| 1,000 | 0.108 | 0.105 | 0.110 | 0.0005 | 0.108 / 0.106 / 0.111 | 0.120 | 0.057 | 103 | 3.03, 3.12, 3.27 |
| 3,000 | 0.339 | 0.318 | 0.340 | 0.0006 | 0.346 / 0.319 / 0.352 | 0.309 | 0.056 | 245 | 3.03, 3.27, 4.27 |
| 6,000 | 0.708 | 0.677 | 0.711 | 0.0006 | 0.722 / 0.673 / 0.745 | 0.626 | 0.073 | 456 | 4.88, 5.63, 5.88 |
| 1,000 (Codex) | 0.116 | 0.119 | 0.119 | 0.0006 | 0.121 / 0.121 / 0.125 | 0.116 | 0.061 | 94 | 9.15, 9.43, 10.17 |

Queries: rare `E4127`, `SUP-48213`, `ctx-artifacts-euw3-7f2a` (1 page each); common `self`,
`return`, `import` (every page); multi-word `the staging DB port`, `failed with exit code`,
`Renamed parse_header to`.

- Search time is linear in N, about 0.115 ms per page per call (0.11 s per 1,000 pages), and it
  barely depends on the query: rare, common, and multi-word queries cost the same. stdio adds
  nothing measurable over in-process. The MCP process keeps no cache, so every call pays the full
  scan. The scan is single-threaded and the server handles calls one at a time, so 5 parallel
  searches at 6,000 pages would take about 3.5 s in all (inference).
- Recall reads one file and stays at 0.5 ms at every size.
- `index_text` is also linear, 0.63 s at 6,000 pages. It runs in the proxy's request path: a
  rotation calls it twice, in `fixed_overhead` (rewrite.py:617) and in `new_epoch` (rewrite.py:650),
  so a rotating request at 6,000 pages waits about 1.25 s for it (inference: 2 calls times the
  measured time). It reads every page, even though only the newest few dozen lines fit in 4,000
  tokens.
- Peak RSS of the MCP child grows about 70 MB per 1,000 pages (456 MB at 6,000). `_pages()` holds
  every parsed page in memory during a call.
- The Codex row ran at load 9 to 10 against 3 for the Messages row at 1,000, so the 8 percent gap
  is within noise.

### Where search time goes (`results/profile_3000.txt`, cProfile, 5 calls at 3,000 pages)

| part | rare `E4127` | common `self` |
|---|---:|---|
| total under the profiler (5 calls) | 2.60 s | 2.47 s |
| `SessionStore._pages`: glob, open, read, `json.loads` of every page | 1.36 s (52%) | 1.39 s (56%) |
|   of which `json` raw_decode | 0.90 s (35%) | 0.93 s (38%) |
|   of which `open` | 0.25 s (9%) | 0.25 s (10%) |
| `search_pages` / `page_matches` over every page | 1.17 s (45%) | 0.94 s (38%) |
|   of which `_texts` walking messages, incl. `as_message` / `is_responses_item` | 0.82 s (32%), 0.40 s (15%) | 0.84 s (34%), 0.40 s (16%) |
| re-reading and rendering up to 53 hits (`get_page`, `render_page`, `match_lines`) | about 1% | about 2% (265 `render_page` calls 0.022 s) |

The earlier static reading was right about the shape: every call reads and parses every page, then
substring-matches. The re-read and render of the 53 hits that it also flagged costs about 1 to 2
percent, so it does not matter. The time goes to parsing JSON (about 37 percent), walking every
message block through the dialect mapping (about 33 percent), and opening files (about 10
percent).

### Retrieval quality, calibrated distractors (`results/quality.csv`)

Per probe query (86 queries over 30 facts):

| N | planted page in the 3 detailed hits | listed (4 to 53) | absent: buried (matches, beyond 53) | absent: query not on the page (case-insensitive would match / all words present) | reply shows the value | changing values: latest page first (of 24) | median matching pages |
|---:|---:|---:|---:|---|---:|---:|---:|
| 17 | 61 | 0 | 0 | 25 (6 / 10) | 52 | 19 | 1 |
| 100 | 57 | 4 | 0 | 25 (5 / 9) | 50 | 19 | 1 |
| 500 | 51 | 8 | 1 | 26 (6 / 10) | 45 | 16 | 1 |
| 1,000 | 50 | 9 | 2 | 25 (6 / 10) | 44 | 17 | 2 |
| 3,000 | 44 | 12 | 4 | 26 (6 / 10) | 40 | 14 | 3 |
| 6,000 | 39 | 16 | 6 | 25 (6 / 10) | 36 | 12 | 3 |
| 1,000 (Codex) | 50 | 9 | 2 | 25 (6 / 10) | 41 | 17 | 2 |

Of the about 61 queries whose text is on the planted page, the share whose planted page is among
the detailed hits: 100 percent at 17 pages, 93 at 100, 85 at 500, 82 at 1,000, 73 at 3,000, 64 at
6,000.

Per fact, the best of its 2 or 3 queries (a model that retries):

| N | in detailed hits | listed only | absent | static (18) | rename (4) | changing (8) | changing: latest first with some query (8) |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 17 | 30 | 0 | 0 | 18 | 4 | 8 | 8 |
| 100 | 29 | 1 | 0 | 17 | 4 | 8 | 8 |
| 500 | 28 | 2 | 0 | 16 | 4 | 8 | 8 |
| 1,000 | 28 | 2 | 0 | 16 | 4 | 8 | 8 |
| 3,000 | 27 | 2 | 1 | 15 | 4 | 8 | 8 |
| 6,000 | 26 | 3 | 1 | 15 | 4 | 7 | 7 |

### Retrieval quality, topic floor of 1 page in 500 (`results/floor_0.002/`)

| N | per query: detailed / listed / buried / not on page | reply shows value | changing: latest first (of 24) | per fact: detailed / listed only / absent |
|---:|---|---:|---:|---|
| 17 | 61 / 0 / 0 / 25 | 52 | 19 | 30 / 0 / 0 |
| 1,000 | 44 / 15 / 2 / 25 | 38 | 16 | 26 / 4 / 0 |
| 6,000 | 15 / 39 / 7 / 25 | 13 | 9 | 12 / 17 / 1 |

What the quality numbers show:

- About 25 of the 86 queries miss at every size because search is an exact, case-sensitive
  substring. Examples: `flaky test` against "The flaky one is test_...", `nightly backup` against
  "Nightly backups", `renamed parse_header` against "Renamed parse_header". This loss does not
  depend on N. 6 of those queries would match case-insensitively, and 10 would match on all their
  words in any order.
- The rest of the loss grows with N. Results come newest first with no ranking, so the planted
  page is in the detailed hits only if fewer than 3 newer pages match, and it is listed only if
  fewer than 53 do. Single common words (`flag` 22 percent of real turns, `owner` 31, `timeout`
  13.5, `regression` 5.8, `rename` 4.1, `bucket` 2.9) drop out of the top 3 within a few hundred
  pages and out of the reply by 1,000 to 3,000 pages. Identifiers that are rare in real turns
  (`E4127`, `cidr_block`, `maxmemory-policy`, and the old names of renamed functions, whose rename
  page is the newest that mentions them) stay first at 6,000. Identifiers found in 0.25 to 0.65
  percent of real turns (`ttl_seconds`, `LOG_LEVEL`, `requires-python`) are only listed at 6,000,
  at positions 4 to 39.
- Newest first is what makes "latest value wins" work at small N (19 of 24). The same order costs
  it at large N, because newer distractors push the latest value page out of the detailed hits: 12
  of 24 at 6,000.
- With the topic floor, 6,000 pages put only 15 of 86 queries, and 12 of 30 facts, in the
  detailed hits. How often a session's own topic words come back decides most of the result.

## Where search stops being reliable

- Speed is not the limit up to 6,000 pages: 0.71 s per search, 0.5 ms per recall. At the
  measured 0.115 ms per page, a search reaches 1 s at about 8,500 pages (inference). The costs in
  the request path are `index_text` (about 1.25 s per rotation at 6,000 pages, inference from 2
  calls) and memory (456 MB peak in the MCP process).
- Accuracy is the limit. By 1,000 pages, 82 percent of the queries that are on the planted page
  find it in the detailed hits. By 3,000 pages that falls to 73 percent, and the latest value
  comes first for only 14 of 24 queries. By 6,000 pages it is 64 percent and 12 of 24. With a
  0.2 percent topic floor, 6,000 pages leave 15 of 86 queries in the top 3.
- Inference: a 30M-token session is about 1,300 to 2,900 pages, which is where one-shot queries
  stop being dependable. At that size, search is reliable for exact, distinctive identifiers
  typed in the page's case, and unreliable for common words and for "what is the current value"
  questions.

## Fixes (measurement only; nothing in src/ changed)

Prototype figures are in `results/fts_proto.csv`, `results/fts_quality.csv`, and
`results/fts_summary.md` (`scale_fts_proto.py`, same stores, in-process, median of 5).

| N | approach | rare | common | multi-word | same result sets as today | one-time cost |
|---:|---|---:|---:|---:|---|---|
| 1,000 | current search | 0.1120 | 0.1028 | 0.1094 | n/a | |
| 1,000 | text cache in the MCP process | 0.0107 | 0.0012 | 0.0078 | yes | load 0.18 s, 33 MB held |
| 1,000 | FTS5 trigram, case_sensitive 1 | 0.0001 | 0.0061 | 0.0003 | yes | 1.73 ms per page insert+commit, index 106 MB |
| 6,000 | current search | 0.7009 | 0.6453 | 0.7242 | n/a | |
| 6,000 | text cache in the MCP process | 0.0645 | 0.0069 | 0.0480 | yes | load 1.06 s, 196 MB held |
| 6,000 | FTS5 trigram, case_sensitive 1 | 0.0001 | 0.0390 | 0.0006 | yes | 1.74 ms per page insert+commit, index 632 MB |

| N | ordering (prototype) | planted page in top 3 (of 86) | in top 53 | changing: latest first (of 24) | facts with a query in the top 3 (of 30) |
|---:|---|---:|---:|---:|---:|
| 1,000 | today: exact case-sensitive substring, newest first | 50 | 59 | 17 | 28 |
| 1,000 | all words, case-insensitive, newest first | 55 | 68 | n/a | 29 |
| 1,000 | all words, case-insensitive, BM25, no recency | 63 | 69 | 2 | 30 |
| 6,000 | today | 39 | 55 | 12 | 26 |
| 6,000 | all words, case-insensitive, newest first | 43 | 63 | n/a | 27 |
| 6,000 | all words, case-insensitive, BM25, no recency | 57 | 67 | 7 | 29 |

1. **Cache each page's searchable text in the MCP process.** Pages are immutable, so the cache
   cannot go stale. Each call lists `pages/` to pick up new pages; the glob costs 2 ms at 3,000
   pages. Measured at 6,000 pages: 0.007 to 0.065 s per search against 0.65 to 0.72 s today, with
   identical result sets, after a 1.06 s first load. The cache holds 196 MB, against today's
   456 MB peak per call. This is the smallest change and gives 10 to 100 times faster search. It
   changes neither semantics nor quality.
2. **Stop `index_text` reading every page.** It needs only the newest pages that fit in 4,000
   tokens, but it parses all of them, twice per rotation. A small append-only manifest of
   `(turn, id, stub)` written by `page_out` (or the same cache in the proxy process) would read
   about 1 KB per page instead of about 40 KB (inference: 30 to 40 times less I/O and parsing).
   That removes about 1.25 s from a rotating request at 6,000 pages.
3. **SQLite FTS5 written at page time.** The trigram tokenizer with `case_sensitive 1` keeps
   today's exact semantics: all 9 latency queries returned the same result sets as today at both sizes. It
   answers rare and multi-word queries in under 1 ms, and common words in 39 ms at 6,000 pages.
   It costs 1.74 ms per page insert and commit (today's `page_out` is about 1.5 ms per page) and
   an index of 632 MB at 6,000 pages, 2.5 times the store. On speed alone, fix 1 gets most of the
   gain without the disk cost. FTS5 is worth it mainly as the base for fix 4.
4. **Better matching and ranking, which is what moves quality.** Case-insensitive all-words
   matching, newest first, raises the planted page in the top 53 from 55 to 63 of 86 at 6,000
   pages. BM25 ranking raises the top 3 from 39 to 57 of 86, and the facts found from 26 to 29 of
   30. BM25 alone breaks "latest value wins", though: 7 of 24 changing-value queries show the
   latest page first at 6,000, against 12 today, and 2 against 17 at 1,000. A ranking needs both:
   BM25 to choose the pages that are about the query, and recency among them so the newest
   statement of a value comes first (not measured; a design suggestion). Caveat: each plant
   repeats its key words in 2 or 3 places (assistant text, tool output, edit), which may flatter
   BM25 here.

## After D34: word matching, BM25 with a recency prior, and page caches (2026-10-18)

D34 (`docs/DECISIONS.md`) changed `contexto_search` and the store; the probes, plants, and
distractors above are unchanged. What changed, in short (details in `src/contexto/store/search.py`
and `src/contexto/store/catalog.py`):

- Matching: every whitespace-separated word of the query, case-insensitively (one character at a
  time), in any order, each as a substring, so for any query with a word every page the old rule
  matched still matches. A whitespace-only query finds nothing.
- Ranking: turn pages holding the whole query, then turn pages holding only its words, then pages
  holding only paged context blocks; within each group BM25 (k1 1.2, b 0.75) over the words and
  the whole query, times `1 + 0.5 r`, `r` the page's position among the matching pages (0 oldest,
  1 newest). The newest matching turn page always takes one of the 3 detailed places. The 3 best
  pages are shown newest first, and so are the next 50, so among the pages shown the newest
  statement comes first.
- Speed: the MCP server keeps each page's folded searchable text (UTF-8) for its lifetime, the
  proxy each page's id, turn, stub, and index line; both list `pages/` per call and read only new
  or changed files. Nothing is written to the store. Pages are narrowed word by word, and the scan
  stops when no page holds every word so far.

Command (both runs on this machine, M5 Pro, load 3.3 to 4.2, back to back):

```
# before: origin/claude/scale-30m a15770f, from a `git archive` copy so the MCP children import that code
.venv/bin/python spikes/m2/scale/store/scale_bench.py --sizes 17,1000,3000,6000 --out results/d34_before
# after: this branch at 958e597 (D34 with the PR #204 review fixes; results/d34_after/env.json)
.venv/bin/python spikes/m2/scale/store/scale_bench.py --sizes 17,1000,3000,6000 --out results/d34_after
.venv/bin/python spikes/m2/scale/store/scale_bench.py --sizes 17,1000,6000 --responses-sizes "" --passes 0 \
    --profile-n 0 --floor 0.002 --out results/d34_after/floor_0.002
.venv/bin/python spikes/m2/scale/store/scale_compare.py results/d34_before results/d34_after
```

The before run reproduces the quality figures above exactly (quality is deterministic; the
`floor_0.002` before run is byte-identical to `results/floor_0.002/quality.csv`), and its latency
is within about 6 percent of them.

### Before and after (`results/d34_before`, `results/d34_after`; `scale_compare.py`)

Top 3: the planted page among the 3 detailed hits, of 86 probe queries. Latest first: changing
values whose first value page among the detailed hits is the latest, of 24. Facts: facts with
some query in the detailed hits, of 30. Search: stdio, seconds, median as in the tables above.
First search: the first call of each MCP child, which after D34 loads the page cache. index_text:
in-process, warm is the median of 5 calls on one store (the proxy's case: it wrote the pages, or
read them once), cold is the first call on a fresh store (a proxy restart).

| N | dialect | run | top 3 (of 86) | latest first (of 24) | facts (of 30) | value shown | search rare / common / multi, s | first search, s | index_text warm / cold, s | MCP peak RSS MB |
|---:|---|---|---:|---:|---:|---:|---|---:|---|---:|
| 17 | messages | before | 61 | 19 | 30 | 52 | 0.002 / 0.004 / 0.002 | 0.003 | 0.035 / 0.035 | 34 |
| 17 | messages | after | 71 | 21 | 30 | 61 | 0.001 / 0.005 / 0.001 | 0.003 | 0.000 / 0.035 | 35 |
| 1,000 | messages | before | 50 | 17 | 28 | 44 | 0.107 / 0.105 / 0.109 | 0.119 | 0.119 / 0.122 | 103 |
| 1,000 | messages | after | 63 | 21 | 29 | 57 | 0.014 / 0.035 / 0.035 | 0.119 | 0.003 / 0.112 | 79 |
| 3,000 | messages | before | 44 | 14 | 27 | 40 | 0.349 / 0.323 / 0.348 | 0.378 | 0.319 / 0.319 | 245 |
| 3,000 | messages | after | 60 | 21 | 29 | 54 | 0.043 / 0.079 / 0.107 | 0.367 | 0.011 / 0.300 | 165 |
| 6,000 | messages | before | 39 | 12 | 26 | 36 | 0.694 / 0.659 / 0.715 | 0.761 | 0.590 / 0.616 | 456 |
| 6,000 | messages | after | 56 | 20 | 29 | 50 | 0.087 / 0.152 / 0.217 | 0.742 | 0.025 / 0.550 | 293 |
| 1,000 | responses | before | 50 | 17 | 28 | 41 | 0.119 / 0.119 / 0.123 | 0.127 | 0.118 / 0.116 | 94 |
| 1,000 | responses | after | 63 | 21 | 29 | 54 | 0.014 / 0.041 / 0.036 | 0.138 | 0.003 / 0.114 | 79 |

With the topic floor of 1 page in 500 (`results/floor_0.002` before, `results/d34_after/floor_0.002` after):

| N | run | top 3 (of 86) | latest first (of 24) | facts (of 30) | value shown |
|---:|---|---:|---:|---:|---:|
| 17 | before / after | 61 / 71 | 19 / 21 | 30 / 30 | 52 / 61 |
| 1,000 | before / after | 44 / 59 | 16 / 20 | 26 / 27 | 38 / 53 |
| 6,000 | before / after | 15 / 41 | 9 / 17 | 12 / 24 | 13 / 37 |

What the numbers show:

- Quality. At 6,000 pages the planted page is in the top 3 for 56 of 86 queries (39 before), the
  latest value comes first for 20 of 24 (12 before), and 29 of 30 facts are found (26). The
  prototype's BM25 without recency reached 57 and 29 but only 7 of 24 latest first; the recency
  prior and the newest-first display keep "latest value wins" at every size (21, 21, 21, 20 of
  24, against 19, 17, 14, 12). With the topic floor, 6,000 pages go from 15 to 41 of 86 and from
  12 to 24 facts.
- The PR #204 review fixes (newest matching turn always among the 3 detailed, context-block pages
  last, per-character case folding, UTF-8 text in memory) left every quality figure of the main
  run unchanged; the synthetic store has no context-block pages and no non-ASCII text. With the
  topic floor two queries moved from listed to detailed because the planted page was the newest
  match (`VPC CIDR` at 1,000, `webhook secret` at 6,000): top 3 58 to 59 and 40 to 41 of 86. Where
  the newest match now takes a detailed place, the planted page's `rank_shown` in `quality.csv`
  moved down by one without changing its outcome.
- The 10 queries per size that missed only because of case or word order now match (the "not on
  page" count falls from 25 to 15, and none of the 15 would match case-insensitively or by all
  words). The 15 left are words the plant does not contain (`hashing algorithm`, `Python
  version`), which no lexical search finds.
- Still buried at 6,000 (5 queries): single words on 3 to 31 percent of the pages (`flaky`,
  `owner`, `regression`, `rename`; `df.csv`), where the plant does not stand out from the
  hundreds of other pages that mention the word, and `flaky test`, whose plant holds both words but
  not the phrase while about 50 distractor pages hold the phrase, which ranks them first by design.
  A more specific query finds each of these facts (29 of 30 facts are in the top 3).
- Tuning history. k1 1.2 and b 0.75 are the usual BM25 defaults. The recency prior and the phrase
  group were added after the BM25-only prototype put the latest value first for only 7 of 24 on
  these same probes (`docs/research/M2-scale-30m.md`), so the probes informed the design and there
  is no held-out probe set. The recency weight 0.5 and the display order were not swept on the
  probes. The newest-match place, the context-block group, and per-character case folding came
  from the PR #204 review (unit cases of their own in `tests/test_search_rank.py`), and the after
  run was repeated with them. The caveat above still holds: each plant repeats its key words in 2
  or 3 places, which flatters BM25, and every synthetic page is 19,000 to 30,000 tokens, so BM25's
  length discount is not exercised here (reviewer-2's repro of it is a unit test).
- Speed. Search at 6,000 pages takes 0.09 to 0.22 s (0.66 to 0.72 before): it no longer reads or
  parses pages, but it still scans the cached text (about 240 MB at 6,000) once per query word
  while some page holds every word so far, so multi-word queries with matches cost more (`results/d34_after/profile_3000.txt`: `rank` is most of a warm
  call; listing `pages/` with a `stat` per file is about 10 ms at 3,000). The first search of
  each MCP process loads the cache, 0.74 s at 6,000, about one search before D34. `index_text`
  takes 0.025 s warm (0.59 before), so a rotation no longer spends about 1.2 s on it; after a proxy
  restart its first call still reads every page once (0.55 s). Peak RSS of the MCP child is lower
  (293 MB at 6,000 against 456): the cache holds folded text as UTF-8 bytes, not the parsed pages.
  The synthetic text is ASCII; held as `str`, a page with one em dash would take 2 bytes per
  character and one with an emoji 4 (measured on 924 KB of text: 1,848 and 3,696 KB as `str`,
  924 KB as UTF-8 in every case), which is why the cache holds bytes.
- Long queries (`scale_long_query.py`, `results/d34_after/long_query.txt`; 6,000 synthetic pages
  of a Zipf vocabulary, 208 MB): with one word absent, 120 words took 7.5 s before and take 0.05
  to 0.19 s, 40 words 2.2 s and 0.05 to 0.27 s, since pages are narrowed word by word and the
  scan stops when none is left. A query whose every word is on nearly every page still costs one
  scan per word (3 such words: 0.75 s before and after).

## After D39: the MCP server holds search text up to a budget (2026-10-24)

D39 caps the page text the MCP server holds for search (`CONTEXTO_SEARCH_CACHE_MB`, default 64).
It holds the newest pages' text as it is and older pages' text compressed (zlib level 1), counting
compressed bytes against the same budget; at each search it reads pages that are not held from
their files and lets them go. The 6,000-page store (`stores.csv`: 254.7 MB on disk, 184 MiB of
folded text, 32.1 KB per page) was measured with the server's budget at 64 MB (the default),
unlimited (100,000 MB), and 0:

```
# before: origin/main 18b9955, from a `git archive` copy so the MCP children import that code
python spikes/m2/scale/store/scale_bench.py --sizes 6000 --responses-sizes= --profile-n 0 --out results/d39/before
# after: this branch, once per budget
python spikes/m2/scale/store/scale_bench.py --sizes 6000 --responses-sizes= --profile-n 0 --out results/d39/after_64
python spikes/m2/scale/store/scale_bench.py --sizes 6000 --responses-sizes= --profile-n 0 \
    --search-cache-mb 100000 --out results/d39/unlimited
python spikes/m2/scale/store/scale_bench.py --sizes 6000 --responses-sizes= --profile-n 0 \
    --search-cache-mb 0 --out results/d39/zero
python spikes/m2/scale/store/scale_compare.py results/d39/before results/d39/after_64
```

`--search-cache-mb` sets the budget of the MCP child (through the environment variable) and of the
in-process server, which the quality pass uses. The four runs went one after another on the same
machine, with other builders' test suites running (load1 6.5 to 11.7, in each `summary.md`).
`after_64_uncompressed` is the same run at this PR's first head (8a96500), which held text only
uncompressed, measured earlier the same day.

| Budget | search rare / common / multi, s | first search, s | index_text warm / cold, s | MCP peak RSS MB |
|---|---|---:|---|---:|
| before D39 (no cap) | 0.100 / 0.174 / 0.243 | 0.88 | 0.037 / 0.652 | 290 |
| unlimited | 0.100 / 0.171 / 0.246 | 0.81 | 0.035 / 0.618 | 278 |
| 64 MB (default) | 0.263 / 0.334 / 0.405 | 1.62 | 0.034 / 0.604 | 134 |
| 64 MB, uncompressed only (8a96500) | 0.548 / 0.633 / 0.693 | 0.85 | 0.035 / 0.613 | 129 |
| 0 | 0.799 / 0.880 / 0.908 | 0.78 | 0.031 / 0.561 | 47 |

- Results are the same. `quality.csv` is byte-identical in all runs (86 probe queries: the outcome,
  the rank shown, the matching page count, and whether the value is shown), and all 270 search
  replies over stdio have the same length as before. The 30 recall rows differ only because each
  run's store has its own key, so its own page ids. `tests/test_search_cache.py` checks the same on
  random stores at several budgets, against a reference ranking with no narrowing.
- What 64 MB holds here: every page, 292 as they are and 5,708 compressed (zlib level 1 makes this
  text 3.2 times smaller; the whole store is 57.8 MiB compressed). A search decompresses 5,708
  pages, 27 us each, instead of reading, parsing, and folding about 3,900 pages, about 110 us each,
  so the budget's added search time falls by about 64 percent against the uncompressed-only head.
  The first search compresses those pages once (137 us each), 1.6 s instead of 0.8.
- Memory. Peak RSS falls from 290 MB to 134 MB at the default and to 47 MB at 0. Text held
  uncompressed and compressed later leaves its memory with the allocator, which keeps it: with no
  limit on the uncompressed share, in-process peak RSS at 64 MB was 185 MB; with half the budget,
  145; with a quarter, 124. So when the store's text does not fit as it is, at most a quarter of the
  budget stays uncompressed (the pages left uncompressed at the end are the same 292 either way).
  Besides held text the server keeps 695 bytes per page (id, turn, kind) and about 210 more per
  held page (`tracemalloc`: 4.2 MB at 6,000 pages with budget 0; 5.4 MB beyond the 67.1 MB counted
  at 64 MB). The page stub (2.4 KB per page, 14 MB at 6,000) is no longer kept by the MCP server,
  which never builds the index.
- Where the default stops mattering. 64 MiB holds 2,085 of these pages as they are (about 48M
  tokens of history) and about 3.2 times that compressed. A 30M-token session
  (`docs/research/M2-scale-30m.md`: 1,616 pages) has at most 30M tokens paged, about 40 MiB of
  folded text at this store's 1.4 bytes per token, so it keeps every page as it is and searches as
  fast as before. The 3.2 ratio is from synthetic text; real text is unmeasured.
- Long queries (`results/d39/long_query.txt`, `scale_long_query.py --before 4b87ec2`, every page
  held): the same results and the same times as main, 0.05 to 0.33 s with one word absent and up to
  120 words. With pages outside the budget a query reads each of them once, and a second time only
  if some page matches, the query has more than 6 words, and the page came before the first match
  in the listing (a page just read is tested for every word at once otherwise).

## D41: BM25's length term and repeated keys, on pages sized like real turns (2026-10-24)

The D34 runs above could not test BM25's length discount: every synthetic page is 19,000 to 30,000
tokens. Reviewer-2's 13-turn unit cases showed it can bury the long turn that states the current
value under short older pages (`tests/test_search_rank.py`). D41 (`docs/DECISIONS.md`) drops the
length term (BM25 with b 0) and counts a term once per text of the page that holds it, at most 3
per page. A text is one stub field (the prompt's first 200 characters, each tool name, each path) or
one text of the turn: each text block, each string field of a tool input, each tool output. PR #216
went through three versions: b 0 alone; then one count per text, after reviewer A measured the case
D34 rejected b 0 for (old tool output that repeats a key); then the page cap of 3, chosen on a grid
after A's second review built dumps that defeat per-text counting. What was added to measure it:

- `scale_bench.py --page-sizes calibration` (and `scale_synth.calibrated_goal`) draws each turn's
  token goal from a lognormal fitted to `calibration.json`'s real turn sizes: mu is the log of the
  median (1,947), sigma 1.98 is the mean of the fits through the 90th and 95th percentiles (24,649
  and 50,840), capped at the largest real turn (566,461). The rest of each turn's random draws are
  unchanged. The goals match the real turns (6,000 drawn: median 1,940, p90 23,918, p95 47,484),
  but only the upper half of the pages does: one synthetic tool step is about 2,770 tokens and every
  plant and distractor gets its own, so the 37 percent of goals under 1,000 tokens come out at a
  median of 2,772. At 6,000 pages the pages are 112 to 571,844 tokens, median 5,509 (real 1,947),
  mean 15,178 (10,431), p90 25,755 (24,649), p95 50,557 (50,840); 54 have no tool call (a goal the
  prompt already meets, with no plant or distractor). Call these "real-size" pages below with that
  caveat; a real store has more tiny pages, which makes b 0.75's bias toward short pages larger.
- `quality.csv` has a `stale_only` column: a changing value whose older page is among the 3
  detailed hits while its latest page is not.
- `scale_length.py` builds one store per page-size model and size, then runs every probe query
  in-process under each ranking: D34's `rank` read from git at `4b87ec2` with b 0.75, 0.5, 0.3, 0
  (`page b=0` is the first version of D41: no length term, every occurrence counted); this tree's
  `rank` (`d41`); and, for the record only (D41 rejected them, and Contexto ranks only as `d41`),
  `cap3` (each page's count capped at 3, reviewer A's option) and three prototypes that take length
  text by text (a text is one stub field or one text, tool input, or tool output): `texts` (length
  of the texts holding a query word, against its mean over the matches), `fields` (BM25F against
  the store's mean text length), and `floored` (the same BM25F with each text at least the mean).
  It also runs reviewer-2's 13-turn cases and main's two original D34 scenarios ("a page about the
  query", "the newest of equally relevant statements") under each. `--grid` runs each text's count
  up to 1, 2, or 3 and the page's up to 2, 3, or no cap with the stub's repeats of the prompt and
  paths dropped (the change reviewer A suggested; D41 measured it and kept the repeats), `--grid-full`
  the same cells with them kept. It holds every page's text (a search budget above any store here,
  D39), so the prototypes can read each page's text ends.
- `scale_dumps.py` and `scale_dumps_gen.sh` are reviewer A's simulation from PR #216: sessions
  built only from this repository's own text (Reads of its files, `git log --stat`, and real
  `grep -rn KEY`, `grep -rn -C3 KEY`, and pytest tracebacks after changing the key or making it a
  string), where 1, 2, or 4 old dumps of a key come before the turn that states its new value and 0
  to 3 passing mentions follow it. "Loud": the dump turn's prompt and reply also name the key;
  "quiet": only its command and output do. `--adversarial` gives A's three sets built to defeat
  per-text counting: `many_calls` (5, 10, or 20 `grep -n KEY file` calls in one turn),
  `split_output` (one tool result in blocks of 5, 20, or 50 lines), `edits` (3, 6, or 12 Edits whose
  strings name the key). `--variants-full` ranks the grid cells with the stub's repeats kept. Its own
  docstring has the details. `scale_grid.py` prints the grid table from both outputs.

Commands (this machine, M5 Pro; quality is deterministic, so load does not matter for it):

```
.venv/bin/python spikes/m2/scale/store/scale_length.py --sizes 17,1000,3000,6000 --out results/d41/length
.venv/bin/python spikes/m2/scale/store/scale_length.py --sizes 1000,6000 --floor 0.002 \
    --out results/d41/length/floor_0.002
git worktree add /tmp/cx-dumps-repo 4d30c34      # the tree the dump sessions are made of
spikes/m2/scale/store/scale_dumps_gen.sh /tmp/cx-dumps-repo /tmp/cx-dumps .venv/bin/python
D="--repo /tmp/cx-dumps-repo --dumps /tmp/cx-dumps"
.venv/bin/python spikes/m2/scale/store/scale_dumps.py $D --trials 500 --seed 7 > results/d41/grid/dumps_loud.md
.venv/bin/python spikes/m2/scale/store/scale_dumps.py $D --trials 500 --seed 11 --quiet > results/d41/grid/dumps_quiet.md
.venv/bin/python spikes/m2/scale/store/scale_dumps.py $D --trials 300 --seed 21 --quiet --adversarial many_calls \
    > results/d41/grid/dumps_many_calls.md    # seed 22 split_output, seed 23 edits
# each of the five again with --variants-full, into dumps_*_full.md
.venv/bin/python spikes/m2/scale/store/scale_length.py --grid --sizes 1000,3000,6000 --out results/d41/grid/probes
.venv/bin/python spikes/m2/scale/store/scale_length.py --grid-full --sizes 1000,3000,6000 \
    --out results/d41/grid/probes_full
.venv/bin/python spikes/m2/scale/store/scale_grid.py results/d41/grid --full   # table_full.md; without --full, table.md
# before: a `git archive` of main at 01cf3fe (D34's ranking with D39's 64 MB search budget), with
# this branch's spikes/m2/scale/store/*.py copied in; after: this branch. Default budget in both.
<before>/spikes/m2/scale/store/scale_bench.py --sizes 17,1000,3000,6000 --responses-sizes 1000 --passes 1 \
    --profile-n 0 --page-sizes spike --out results/d41/before_spike         # and --page-sizes calibration
.venv/bin/python spikes/m2/scale/store/scale_bench.py --sizes 17,1000,3000,6000 --responses-sizes 1000 \
    --passes 1 --profile-n 0 --page-sizes spike --out results/d41/after_spike  # and --page-sizes calibration
```

### Ranking variants (`results/d41/length/summary.md`)

Top 3: planted page among the 3 detailed hits (of 86). Latest: changing values whose first value page
shown is the latest (of 24). Stale: an older value shown in detail and the latest not (of 24).
`page b=0.75` is D34. `page b=0`, `d41`, and `cap3` give the same figures in every row.

| pages | N | D34 (page b=0.75) | page b=0.5 | page b=0.3 | D41 | texts b=0.75 | fields b=0.75 | fields b=0.3 | floored b=0.75 |
|---|---:|---|---|---|---|---|---|---|---|
| spike | 1,000 | 63 / 21 / 0 | 63 / 21 / 0 | 63 / 21 / 0 | 63 / 21 / 0 | 60 / 20 / 0 | 63 / 21 / 0 | 63 / 21 / 0 | 63 / 21 / 0 |
| spike | 3,000 | 60 / 21 / 0 | 60 / 21 / 0 | 60 / 21 / 0 | 60 / 21 / 0 | 55 / 19 / 0 | 57 / 21 / 0 | 59 / 21 / 0 | 59 / 21 / 0 |
| spike | 6,000 | 56 / 20 / 0 | 58 / 21 / 0 | 57 / 20 / 0 | 58 / 20 / 0 | 51 / 17 / 0 | 53 / 20 / 0 | 54 / 20 / 0 | 56 / 20 / 0 |
| real | 1,000 | 61 / 20 / 0 | 62 / 21 / 0 | 63 / 21 / 0 | 63 / 21 / 0 | 60 / 20 / 0 | 63 / 21 / 0 | 63 / 21 / 0 | 63 / 21 / 0 |
| real | 3,000 | 57 / 20 / 0 | 58 / 21 / 0 | 59 / 21 / 0 | 60 / 21 / 0 | 55 / 19 / 0 | 57 / 21 / 0 | 59 / 21 / 0 | 59 / 21 / 0 |
| real | 6,000 | 53 / 17 / 3 | 55 / 19 / 2 | 54 / 19 / 2 | 58 / 20 / 0 | 48 / 13 / 3 | 53 / 20 / 0 | 54 / 20 / 0 | 56 / 20 / 0 |
| 13-turn cases fixed (of 3) | | 0 | 0 | 0 | 3 | 3 | 3 | 3 | 3 |

Each cell is top 3 / latest / stale. Facts: 29 of 30 for every variant at 1,000 to 6,000 except D34,
`page b=0.5` and `page b=0.3` (28) and `texts` (27) at 6,000 real-size pages; 30 of 30 for every
variant at 17 pages, where every variant scores 71 / 21 / 0. With the 0.2 percent topic floor
(`results/d41/length/floor_0.002/`), top 3 / latest / stale / facts:

| pages | N | D34 | D41 (and `page b=0`, `cap3`) | fields b=0.75 | floored b=0.75 |
|---|---:|---|---|---|---|
| spike | 1,000 | 59 / 20 / 0 / 27 | 60 / 21 / 0 / 27 | 57 / 21 / 0 / 27 | 60 / 21 / 0 / 27 |
| spike | 6,000 | 41 / 17 / 0 / 24 | 44 / 19 / 0 / 24 | 34 / 19 / 0 / 20 | 44 / 19 / 0 / 24 |
| real | 1,000 | 56 / 20 / 0 / 27 | 60 / 21 / 0 / 27 | 57 / 21 / 0 / 27 | 60 / 21 / 0 / 27 |
| real | 6,000 | 29 / 15 / 2 / 17 | 44 / 19 / 0 / 24 | 34 / 19 / 0 / 20 | 44 / 19 / 0 / 24 |

### The grid (`results/d41/grid/`)

Stub repeats kept (`table_full.md`). The probes at 6,000 pages are top 3 / latest first / stale
only; "probe rows below" counts the rows at 1,000, 3,000, and 6,000 on both models where a cell is
lower than D41 at 0ff1bfe (text cap 1, no page cap) in any of the three. Stale only is per dump set:
loud / quiet / many calls / split output / refactor, of 500, 500, 300, 300, 300.

| text cap, page cap | spike 6,000 | real 6,000 | probe rows below | 13-turn (of 4) | page about the query | newest of equal statements | stale only | sum |
|---|---|---|---:|---:|---|---|---|---:|
| 1, 2 | 58 / 20 / 0 | 58 / 20 / 0 | 2 | 4 | no | no | 184 / 120 / 103 / 98 / 109 | 614 |
| **1, 3 (D41)** | **58 / 20 / 0** | **58 / 20 / 0** | **0** | **4** | **no** | **no** | **184 / 121 / 103 / 98 / 109** | **615** |
| 1, none (0ff1bfe) | 58 / 20 / 0 | 58 / 20 / 0 | 0 | 4 | no | no | 184 / 121 / 113 / 99 / 116 | 633 |
| 2, 2 | 57 / 19 / 0 | 57 / 19 / 0 | 6 | 4 | no | yes | 184 / 161 / 103 / 98 / 109 | 655 |
| 2, 3 | 58 / 20 / 0 | 58 / 20 / 0 | 0 | 4 | no | yes | 184 / 162 / 103 / 98 / 109 | 656 |
| 2, none | 58 / 20 / 0 | 58 / 20 / 0 | 0 | 4 | no | yes | 184 / 162 / 119 / 99 / 116 | 680 |
| 3, 2 | 57 / 19 / 0 | 57 / 19 / 0 | 6 | 4 | no | yes | 184 / 161 / 103 / 98 / 109 | 655 |
| 3, 3 | 58 / 20 / 0 | 58 / 20 / 0 | 0 | 4 | yes | yes | 184 / 162 / 103 / 98 / 109 | 656 |
| 3, none | 58 / 20 / 0 | 58 / 20 / 0 | 0 | 4 | yes | yes | 184 / 162 / 119 / 103 / 116 | 684 |
| D34 (b 0.75) | 56 / 20 / 0 | 53 / 17 / 3 | 4 | 1 | yes | yes | 171 / 158 / 127 / 125 / 143 | 724 |

With the stub's repeats of the prompt and paths dropped (`table.md`), every cell is lower on 4 to 6
probe rows (55 or 56 of 86 at 6,000 on both models, against 58), so D41 keeps them. Main's
scenarios come out the same as
with the repeats kept, and stale only sums to 614 to 716.

Statement in detail / an old dump ranked above it / stale only, D34 then D41 (text cap 1, page cap
3): loud 300 / 272 / 171 and 316 / 260 / 184; quiet 308 / 235 / 158 and 363 / 131 / 121; many calls
173 / 195 / 127 and 197 / 153 / 103; split output 171 / 190 / 125 and 202 / 140 / 98; refactor
157 / 205 / 143 and 191 / 151 / 109. One-line replies shown, D34 then D41: 64 and 38 of 122 (loud),
65 and 61 of 132 (quiet), 37 and 28 of 85 (many calls), 27 and 20 of 74 (split output), 22 and 18
of 68 (refactor). Edits and Edit-then-pytest statements are shown in every session under D41.

What the numbers show:

- On real-size pages, D34's length discount shows the stale-value bug in the probes: at 6,000
  pages 3 changing-value queries show an older value in detail and not the latest. D41 shows none,
  has 5 more queries in the top 3 (58 against 53) and 3 more latest-first (20 against 17). No b
  above 0 beat b 0 on top 3 at any size (b 0.5 tied it at 6,000 spike-size pages, with latest first
  21 against 20). Taking length text by text fixes the 13-turn cases but costs elsewhere (BM25F
  boosts short texts, so saying the word in 2 or 3 places stops counting).
- Counting every occurrence with b 0 (the first D41) showed only a stale value more often than D34
  in the dump sessions (195 against 171 loud, 170 against 158 quiet). Counting per text removes most
  of that; the page cap of 3 then holds the many-calls, split-output, and refactor dumps to 3 texts
  (stale only 113 to 103, 99 to 98, 116 to 109) and costs nothing on the probes.
- What is left: a reply that discusses a key at length in one text counts as one mention, so main's
  two D34 scenarios now fail (tests pin the new outcome), and keeping them (text cap 3) costs 41
  stale only in the quiet set. An old turn with the key in 3 or more texts still outranks a newer
  one-line reply (1.57 against 1.5); once a passing mention follows that reply, the reply is shown
  less often than under D34 in every set. Many separate tool calls are the shape that costs most.

### Before and after on the main harness (`results/d41/before_*`, `results/d41/after_*`)

`scale_compare.py results/d41/before_spike results/d41/after_spike`, and the same for
`calibration`. Before is main at cb1986f (D34's ranking, with D39's search budget and #209 and #213)
from a `git archive` with this branch's `spikes/m2/scale/store/*.py` copied in, so its `env.json`
says commit `unknown`; after is this branch at e87f148, the code commit of the final rule (text cap
1, page cap 3); the commits after it change docs, tests, and this harness, not `src/`. Both use
D39's default 64 MB budget. One timing pass each, run back to back, so latency is a rough guide.

| pages | N | dialect | run | top 3 | latest | facts | value shown | search rare / common / multi, s | first search, s | MCP peak RSS MB |
|---|---:|---|---|---:|---:|---:|---:|---|---:|---:|
| spike | 1,000 | messages | before / after | 63 / 63 | 21 / 21 | 29 / 29 | 57 / 57 | 0.014 / 0.040 / 0.037, 0.014 / 0.024 / 0.037 | 0.117, 0.123 | 74, 74 |
| spike | 3,000 | messages | before / after | 60 / 60 | 21 / 21 | 29 / 29 | 54 / 54 | 0.110 / 0.149 / 0.177, 0.110 / 0.108 / 0.176 | 0.678, 0.699 | 91, 90 |
| spike | 6,000 | messages | before / after | 56 / 58 | 20 / 20 | 29 / 29 | 50 / 52 | 0.240 / 0.303 / 0.372, 0.240 / 0.222 / 0.372 | 1.447, 1.494 | 134, 134 |
| spike | 1,000 | responses | before / after | 63 / 63 | 21 / 21 | 29 / 29 | 54 / 54 | 0.015 / 0.040 / 0.037, 0.015 / 0.027 / 0.037 | 0.130, 0.136 | 74, 74 |
| real | 1,000 | messages | before / after | 61 / 63 | 20 / 21 | 29 / 29 | 55 / 57 | 0.011 / 0.037 / 0.022, 0.011 / 0.034 / 0.023 | 0.094, 0.096 | 65, 80 |
| real | 3,000 | messages | before / after | 57 / 60 | 20 / 21 | 29 / 29 | 51 / 54 | 0.033 / 0.072 / 0.068, 0.034 / 0.042 / 0.070 | 0.268, 0.284 | 112, 142 |
| real | 6,000 | messages | before / after | 53 / 58 | 17 / 20 | 28 / 29 | 47 / 52 | 0.159 / 0.231 / 0.230, 0.159 / 0.158 / 0.229 | 1.048, 1.100 | 112, 136 |
| real | 1,000 | responses | before / after | 61 / 63 | 20 / 21 | 29 / 29 | 52 / 54 | 0.011 / 0.042 / 0.023, 0.011 / 0.039 / 0.023 | 0.102, 0.112 | 64, 79 |

Common-word search is faster than before: the page cap ties every page that holds a word on every
page, so recency orders them and the 53 pages shown are the newest, not the longest (at 3,000
real-size pages, in-process scratch checks, 2.0 to 3.3 MB of page JSON for `self`, `return`, and
`import`, against 2.1 to 3.1 under D34). The higher peak RSS on real-size pages matches one
multi-word query's reads: `failed with exit code` shows 18.5 MB of page JSON with a 28.1 MB allocation peak
against 0.5 and 1.5 MB under D34, because with no length term the long pages that hold its common
words in 3 or more texts are no longer pushed down. The other 8 latency queries allocate at most
0.2 MB more than under D34, except `import` (5.8 against 5.2 MB). `contexto_search` reads and
renders every page it shows (`get_page` for the 3 detailed and the 50 listed), with no cap on their size; D39's budget
bounds the text held between searches, not these reads. `index_text` does not rank and is unchanged.

## Files

- `scale_bench.py`: the harness (store build, stdio and in-process timing, quality, profile, summary).
- `scale_synth.py`: synthetic turns, the Codex mapping, and distractor and rename injection.
- `scale_probes.py`: the 30 facts, 86 probe queries, and 9 latency queries (fixed).
- `scale_calibrate.py`, `calibration.json`: real-turn query frequencies and turn sizes (numbers only).
- `scale_fts_proto.py`: the text-cache, FTS5 trigram, and BM25 prototypes.
- `scale_compare.py`: the before and after table of D34 (and D39) from two result directories.
- `scale_long_query.py`: `rank` before and after word-by-word narrowing, on queries of 1 to 120 words.
- `scale_length.py`: BM25 length and term-count variants on spike-size and real-size pages, and
  reviewer-2's 13-turn cases (D41).
- `scale_dumps.py`, `scale_dumps_gen.sh`: reviewer A's sessions of old tool-output dumps that repeat
  a key, built from this repository's own text (D41).
- `scale_grid.py`: the D41 grid table from `scale_length.py --grid` and `scale_dumps.py` outputs.
- `test_scale_store.py`: fast checks of the helpers (about 0.5 s, part of the default suite).
- `results/`: `stores.csv`, `latency.csv` (every call), `quality.csv` (every probe query), `df.csv`,
  `profile_3000.txt`, `env.json`, `summary.md`, `fts_*`, and `floor_0.002/`; `d34_before/` and
  `d34_after/` (with `d34_after/floor_0.002/` and `long_query.txt`) are the same outputs from the D34 runs;
  `d39/` holds the D39 runs at 6,000 pages (`before`, `after_64`, `after_64_uncompressed`, `unlimited`,
  `zero`, `long_query.txt`); `d41/` holds `length/` (with `floor_0.002/`) from `scale_length.py`,
  `grid/` from `scale_dumps.py`, `scale_length.py --grid` and `--grid-full`, and `scale_grid.py`, and the D41
  before and after runs of `scale_bench.py` for each page-size model.
