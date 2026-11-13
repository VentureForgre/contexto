# Tool-loop replay harness (D63, zero spend)

Scripted conversations with long tool loops, driven through the real `contexto.rewrite.RewriteEngine`
and a real `contexto.store.SessionStore` in a temporary directory, against a fake upstream in the same
process. No model is called and nothing leaves the machine. It measures what the engine does when one
turn grows past the bound, and it holds the tests D63 (in-turn paging) has to pass.

Results at v0.5.24: `docs/research/M2-in-turn-paging-baseline.md`.

```sh
uv run python spikes/m2/inturn/replay.py --scenario all --json out.json   # about 30 s of CPU
uv run python spikes/m2/inturn/replay.py --scenario small --full          # 1,500 rounds, about 3 minutes
uv run --with pytest pytest -q spikes/m2/inturn
```

## Files

- `scripts.py`: the six scenarios (`loop120`, `large`, `small`, `subagent`, `images`, `multiturn`).
  A script is what the model says and what each tool returns, with no API shape in it. Every text
  comes from SHA-256 of its position, so a script is the same bytes on every run and platform.
- `cli.py`: the two CLIs. `ClaudeCode` sends Messages requests (`/v1/messages`) in Claude Code
  2.1.318's shapes, `Codex` sends Responses requests (`/v1/responses`) in codex-cli 0.166.0's. Each
  keeps its own history and resends all of it; what the engine forwards is never fed back.
- `upstream.py`: `FakeUpstream`. It checks each forwarded request against the API's rules and counts
  violations by kind, prices it with `tokenizer.py`, models the prompt cache, and answers with the
  script's next step. A thinking block's signature is a keyed hash of the request it answered, so a
  later request that changed anything before that block is caught (`thinking_binding`).
- `tokenizer.py`: the upstream's tokenizer: tool output at 2.2 bytes a token, other text at 3.1,
  images by the contract's rule 3 pixel price, 4 tokens a message. It is not the engine's estimator.
- `harness.py`: `run(scenario, dialect)` plays a scenario and returns a `Run`; `Run.metrics()` is the
  numbers. It calls the engine in `contexto.proxy`'s order: `prime`, the transform, the upstream, then
  `observe_usage` with the CLI's own request.
- `targets.py`: `unmet(run)` lists the D63 targets a run misses.
- `stubs.py`: `find_stubs(dialect, request)`, the hook the engine pull request fills in.
- `replay.py`: the command. It prints three Markdown tables and can write the numbers as JSON.

## The tests

- `tests/test_inturn_upstream.py`: each violation kind on a hand-made request, a clean append-only
  conversation, the cache model, the re-sent count, the tokenizer and the PNG builder.
- `tests/test_inturn_scenarios.py`: the scripts' sizes, the CLI history staying append-only, no tool
  result lost, two runs giving the same numbers, and the command.
- `tests/test_inturn_targets.py`: one test per scenario and dialect, marked
  `xfail(strict=True, reason="D63: in-turn paging not built")`. They fail at v0.5.24.

## For the engine pull request (D63)

1. Replace the body of `stubs.find_stubs(dialect, request)`. It gets one forwarded request (`claude`:
   a Messages request, `codex`: a Responses request) and returns a `StubRef(page_id, call_ids)` for each
   stub in it: the store page the stub names, and the tool calls (Messages `tool_use` ids, Responses
   `call_id`s) whose results it stands for. The harness calls it on every forwarded request and checks
   that `SessionStore.get_page(page_id)` exists and holds each named call's original result.
2. Remove the `xfail` marks in `tests/test_inturn_targets.py`. They are strict: a scenario that meets
   its targets while still marked fails as XPASS.

No passing test depends on the hook being empty, so nothing else needs to change.

Independent of the hook, `Run.originals` counts every scripted tool result as in the last forwarded
request (`in_view`), in a store page (`stored`), or in neither (`lost`). Each result starts and ends
with a line of its own, and both lines must be present. `lost` must stay 0.
