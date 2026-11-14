# Tool-loop replay harness (D63, zero spend)

Scripted conversations with long tool loops, driven through the real `contexto.rewrite.RewriteEngine`
and a real `contexto.store.SessionStore` in a temporary directory, against a fake upstream in the same
process. No model is called and nothing leaves the machine. It measures what the engine does when one
turn grows past the bound, and it holds the tests D63 (in-turn paging) has to pass.

Results at v0.5.24: `docs/research/M2-in-turn-paging-baseline.md`.

```sh
uv run python spikes/m2/inturn/replay.py --scenario all --json out.json   # every scenario, small at 260 rounds
uv run python spikes/m2/inturn/replay.py --scenario small --full          # small at 1,500 rounds
uv run python spikes/m2/inturn/replay.py --engine toy                     # the toy pager, held to the targets
uv run --with pytest pytest -q spikes/m2/inturn
```

The research note gives the measured run times and the load they were measured at.

## Files

- `scripts.py`: the seven scenarios (`loop120`, `large`, `small`, `subagent`, `images`, `multiturn`,
  `mixed`). A script is what the model says and what each tool returns, with no API shape in it.
  Every text comes from SHA-256 of its position, so a script is the same bytes on every run and
  platform. Late in each long loop two steps are a probe: a search for a line of an early result, then
  a recall of the page the search named.
- `cli.py`: the two CLIs. `ClaudeCode` sends Messages requests (`/v1/messages`) in Claude Code
  2.1.318's shapes, with its breakpoints on both system blocks and the newest message; `Codex` sends
  Responses requests (`/v1/responses`) in codex-cli 0.166.0's. Each keeps its own history and resends
  all of it; what the engine forwards is never fed back.
- `upstream.py`: `FakeUpstream`. It checks each forwarded request against the API's rules and counts
  violations by kind, prices it with `tokenizer.py`, models the prompt cache (Messages by its
  `cache_control` breakpoints with the 20-block lookback, Responses by automatic prefix), and answers
  with the script's next step. A thinking block's signature is a keyed hash of the request it answered and of
  the blocks ahead of it in its message, so a later request that changed anything before that block is
  caught (`thinking_binding`).
- `tokenizer.py`: the upstream's tokenizer: tool output at 2.2 bytes a token, other text at 3.1,
  4 tokens a message, images by the contract's rule 3 pixel price. For text it is not the engine's
  estimator; for images it is the engine's own rule.
- `harness.py`: `run(scenario, dialect)` plays a scenario and returns a `Run`; `Run.sent` is one
  record per request and `Run.metrics()` the numbers. It calls the engine in `contexto.proxy`'s order
  (`prime`, the transform, the upstream, then `observe_usage` with the CLI's own request), runs the
  probes through the real MCP handlers (`contexto.mcp.Server`) on a second handle of the store, and
  sends a `count_tokens` request after some loop requests. With `check_wire` (the target tests run
  with it) it also proves each request's byte count and digests the CLI's own history right after the
  engine's call.
- `targets.py`: the D63 rules, one function each, and `unmet(run)`.
- `stubs.py`: `find_stubs(dialect, request)`, the hook the engine pull request fills in.
- `toy.py`: a small in-turn pager for the harness's own tests, and its defects (the wrong engines). It
  is not the D63 design.
- `replay.py`: the command. It prints four Markdown tables and can write the numbers as JSON; with
  `--engine toy` it runs the toy pager, prints the targets each run misses, and exits 1 on any.

## The tests

- `tests/test_inturn_upstream.py`: each violation kind on a hand-made request, a clean append-only
  conversation, the cache model, the rewrite numbers, the tokenizer and the PNG builder.
- `tests/test_inturn_scenarios.py`: the scripts' sizes and shapes, the CLI history staying
  append-only, no tool result lost, two runs giving the same numbers, and the command.
- `tests/test_inturn_engines.py`: `toy.ToyPager` meets every target on every scenario (quick builds;
  `replay.py --engine toy` runs the target tests' sizes); each wrong engine fails the rule written for
  it; each rule's line, on a run edited by hand.
- `tests/test_inturn_targets.py`: one test per scenario and dialect (13), marked
  `xfail(strict=True, raises=AssertionError, reason="D63: in-turn paging not built")`. They fail at
  v0.5.24.

## For the engine pull request (D63)

1. Replace the body of `stubs.find_stubs(dialect, request)`. It returns a `StubRef(page_id, call_ids)`
   for each stub and each fold note in `request`:
   - `call_ids`: the tool calls whose results the stub stands for (Messages `tool_use` ids, Responses
     `call_id`s). A reference that names a page as a whole, such as a fold note naming a rounds or
     folds page, may carry empty `call_ids`: it then covers every call reachable from that page.
   - A call is reachable from a page when the page holds the call's original byte for byte, or holds
     a stub or fold reference whose page leads to it, within 8 pages.
   - On every request, each result the CLI has delivered so far in that conversation that is not
     whole in the request has to be covered by one of these references in that request. The one
     exception is a result of an earlier turn whose call is gone from the request too: it is accounted
     for only when the text the model reads (text parts of messages, not tool results or other request
     fields) names where it went: a page id there reaches it, or the whole turn left (no 40-character
     run of its prompt or of any of its answers that is found in no other turn's text, with whitespace,
     format characters and combining marks removed, is anywhere in that text; a shorter text is set
     aside only when another turn has that same whole text, and counts only as a whole text part), that
     text carries the index's
     "(N older pages omitted; use contexto_search" line with N at least 1 and at most the store pages it
     does not name, and the result is stored byte for byte.
   - `page_id`: the store page that holds them. `None` only when every call is one of Contexto's own
     search, recall or state calls (`contexto.store.render.own_call_ids`), which may be stubbed
     without a stored copy.
   - `request` is a forwarded request, or `{"messages": page["messages"]}` (`codex`:
     `{"input": page["messages"]}`) for a store page, when the harness follows a stub it found inside
     a page. Read only that one field.
2. Publish, in the engine's `request` event, `estimate`: its pre-send estimate of the request without
   that request's rewrite (the previous forwarded request and the items added since), and
   `sent_estimate`: of what it forwarded. The second-rewrite-in-a-row exception reads `estimate`,
   counted at most 1.02 x the upstream's count of the same request.
3. Remove the `xfail` marks in `tests/test_inturn_targets.py` (the `NOT_BUILT` mark). They are strict:
   a scenario that meets its targets while still marked fails as XPASS.

No passing test depends on the hook being empty, so nothing else needs to change.

What a run is held to is in `targets.py`, one docstring per rule, and in the research note under
"What D63 has to change". `targets.unmet_by_rule(run)` says which rules a run misses and why.
`uv run python spikes/m2/inturn/replay.py --scenario loop120` prints one scenario's numbers.
