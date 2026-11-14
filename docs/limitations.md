# Known limitations

[Contexto](../README.md) / [Documentation](README.md)

What Contexto does not do, and where its measurements stop. The [README](../README.md#known-limitations)
has the short list; this is the full one.

- **Tool loops.** Contexto never rotates inside a tool loop. A turn that grows past the bound inside
  one very long tool loop is sent as is and logged `over_bound` with `in_tool_loop: true`. The next
  turn start rotates. A turn start can also go over: when the bound is too small for the fixed
  overhead and the new prompt together (logged `bound_too_small`, or `pdf_allowance` when a PDF
  counted high is the reason), while the store cannot be read or written (no rotation until it
  recovers), or when the estimate runs low before the first token count (below)
  ([Troubleshooting](troubleshooting.md)).
- **Recall is a search the model must run.** Once a turn is paged out, the model sees its opening in
  the page index and the rest only through a search it chooses to run. Each search is one more
  request: in acceptance run 3 the six probe turns took 26 requests through Contexto against 6 for
  the baseline ([M2-usage-savings-run3.md](research/M2-usage-savings-run3.md)).
- **Cost.** Contexto sends fewer input tokens, but each rotation rewrites the prompt cache. Whether a
  session costs less depends on the model's prices and on cache hits
  ([Results](results.md#tokens-and-cost)).
- **Latency.** The time the proxy adds to each request grows with the history: 0.056 s per turn
  start at 0.5 million tokens of history and 0.86 s at 30 million, as medians of 3 runs of synthetic
  sessions against a local fake upstream, measured on v0.2.7 code on 2026-10-17 ([M2-scale-30m.md](research/M2-scale-30m.md)).
- **Subscription logins.** Running Contexto with a claude.ai subscription login is not a supported
  setup ([FAQ](faq.md)).
- **Tested scope.** Claude Code acceptance ran on two models (claude-haiku-5-5 and
  claude-opus-5-5), one bound and one upstream, on Claude Code 2.1.301, 2.1.302 and 2.1.318 (tool
  search on by default). On 2.1.310 only a short live recall check ran; 2.1.314 passed the zero-spend
  checks only. Codex passed the long-run and recall criteria on one model at bound 64,000 and at the
  default 150,000, on codex-cli 0.163.0; 0.166.0 passed the zero-spend checks only. Its answer
  accuracy against a full-context baseline was not measured.
- **Standing rules** survive paging only if they open their message or the model pinned them. The
  model is told to pin them when stated, but that was measured on one model and 8 rules
  ([Rules you give once](standing-rules.md)).
- **Tool search** (Claude Code's default) defers Contexto's tools. When the model first loads one,
  the fixed part of the prompt changes and Contexto rotates once, so that request writes a new prompt
  cache (2 of the 6 rotations in acceptance run #4).
- **Images** are estimated from their pixel size. A screenshot in a turn that has been paged out is
  not recallable as an image: `contexto_recall` shows it as `[image omitted]`, and the model sees only
  the turn's text.
- **PDFs** are never estimated low, at the cost of running high: 4,568 tokens a page, about 2.9 times
  a typical page, and 600 pages for a PDF whose pages cannot be counted for certain. A large PDF can
  page the earlier conversation on the turn it is pasted on; a dense page (small-font code, tables)
  can still cost more than 4,568.
- **Damaged lock files.** A symlink, FIFO or directory at a session's `meta.lock` or `live.lock`
  stops `contexto run` from opening that session until you delete the file
  ([Troubleshooting](troubleshooting.md)).
- **New CLI versions** can change the text Contexto recognises in their requests. Until a version is
  tested, the launch warns ([Troubleshooting](troubleshooting.md)).
- **Cache efficiency** depends on the upstream route. A proxy that spreads one session over several
  accounts loses the cache unless it routes on the session ([Troubleshooting](troubleshooting.md)).
- **Search** counts a word once per text of a turn, so a reply that discusses a key at length weighs
  as one passing mention, and an old turn that names a key in several texts can outrank a newer
  one-line reply that states its value.
- **Search cost.** The first search of each MCP process reads every page once (1.6 s at 6,000
  synthetic pages). Each query word scans the search text once, so a long query with matches costs
  more, and the up to 53 pages one search shows are read in full with no size cap. On `main`
  (unreleased, D65), a query that no turn holds every word of also runs the approximate fallback:
  at 6,000 pages such a miss took 0.6 to 1.4 s, against 0.3 to 0.6 s for the empty answer before
  (2.2 to 3.7 times), and its reply shows 3 pages in detail and lists 10 more
  ([search.md](search.md)).
- **Before the first token count.** Until the API reports a request's real input count, Contexto
  estimates it, and a very dense start of the conversation (JSON, code, non-English text) is
  estimated low. If the upstream never reports counts, tool-loop requests can go over the bound
  without a log entry.
- **Codex ChatGPT login mode** is tested only with synthetic credentials.
- **Windows** is not supported.
