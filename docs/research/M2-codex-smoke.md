# M2 A6: live Codex smoke through `contexto run codex`

Packet A6 (intake approval 2026-10-16 02:41Z; plan approved by manager-1 at 02:47Z). One synthetic
Codex conversation in API-key mode, behind the Contexto proxy, within a 250,000-token cap. Tags:
**measured** means read from the session log, the stub log, or Codex's own output in this run;
**inferred** means reasoned from those. No prompt or response content and no credentials are
recorded here.

## Run conditions

- Code: `main` at `c1b60f6c15def2b7d837ad8c5ad97ea840acb72e` (contains the PR #43 merge `59987eb`;
  `src/` unchanged from it). Codex `codex-cli 0.154.0`, model `gpt-6-astra` pinned with
  `-c model=gpt-6-astra`, `-c model_reasoning_effort=low` (run condition; it changes output size,
  not the proxy path).
- Auth: API-key mode only (D22, D12). The session-dir `CODEX_HOME` provider's `auth.command` runs
  the rig's key helper command; the key itself is in no file. Upstream: the owner's proxy plus `/v1`.
  Never the ChatGPT login, never `~/.codex`.
- Process: `env -i` with only `HOME`, `PATH`, `TERM`, and `CONTEXTO_HOME`, so no OpenRig or Claude
  Code session variables reach Codex; a non-Git scratch directory with no `AGENTS.md`.
- Engine: `--bound 42000 --headroom-floor 6000`. One turn per process: turn 1 is `codex exec`, later
  turns are `codex exec resume --last` on the same Contexto session, so each turn starts a new proxy
  and a new engine instance.
- Turns: T1 asks the model to run a script that prints 40 KB of synthetic text; T2 and T3 carry that
  40 KB in the prompt (T1 showed the model pipes the tool output away, so turn growth was about 100
  tokens); T4 asks the model to call `contexto_state`; T5 repeats T4 with the contexto MCP server's
  tools pre-approved.
- Stop rules (watchdog in `spikes/m2/codex/smoke.sh`, reading the session log every 0.5 s): kill on any
  request above 46,000 real input tokens, cumulative real tokens above 225,000, any upstream 4xx,
  WebSocket attempt, `over_bound` with source real, `fallback_failed`, or `transform_error`; before
  each turn, stop if cumulative plus 75,000 would pass 225,000. None fired.

Commands (from the repository root; the two variables name the key helper command and the owner's
proxy base URL):

```
spikes/m2/codex/smoke.sh preflight                      # zero tokens, against responses_stub.py
A6_KEY_HELPER=... A6_UPSTREAM=... spikes/m2/codex/smoke.sh turn <n> <session>
python3 spikes/m2/codex/smoke_summary.py <CONTEXTO_HOME>/sessions/<session>/log.jsonl
```

## Results

Per request (measured; real input includes cached tokens, as the Responses API reports it):

| Turn (process) | Req | Endpoint | Status | Real input | Cached | Output (reasoning) | Items in -> sent | Estimate | Sent estimate | Engine events |
|---|---|---|---|---|---|---|---|---|---|---|
| T1 | 1 | POST /responses | 200 | 15,309 | 0 | 52 (0) | 7 -> 7 | 21,290 | | |
| T1 | 2 | POST /responses | 200 | 15,420 | 15,104 | 10 (0) | 10 -> 10 | 15,487 | | |
| T2 | 3 | POST /responses | 200 | 24,812 | 15,232 | 44 (32) | 12 -> 12 | 33,437 | | |
| T3 | 4 | POST /responses | 200 | 24,757 | 15,104 | 65 (56) | 15 -> 7 | 45,904 | 33,160 | page_out (3 turns), rotation window_minimal k=14 F=21,283 W=14,717, carried_over_budget, window_minimal |
| T4 | 5 | POST /responses | 200 | 24,816 | 24,576 | 57 (0) | 18 -> 9 | 46,702 | 33,306 | page_out (3 turns), rotation bound k=14 F=21,308 W=14,692, carried_over_budget |
| T4 | 6 | POST /responses | 200 | 24,897 | 24,576 | 5 (0) | 20 -> 11 | 24,942 | | |
| T5 | 7 | POST /responses | 200 | 15,582 | 15,104 | 56 (0) | 22 -> 11 | 47,017 | 21,737 | page_out (4 turns), rotation bound k=17 F=21,308 W=14,692, carried_over_budget |
| T5 | 8 | POST /responses | 200 | 15,662 | 15,360 | 5 (0) | 24 -> 13 | 15,702 | | |

**Spend (measured):** 161,549 tokens: 161,255 input (125,056 of them cached) and 294 output. The
zero-token pre-flight, baseline, and config checks ran against the local stub.

1. **HTTP through the proxy (goal 1), measured.** 8 `POST /responses`, all 200; 0 WebSocket attempts
   (no 501); 0 4xx on model requests. Non-model calls that reached the proxy: `GET /models` 6 times
   (2 in the first process, 1 in each later one), all 200. Calls Codex makes outside the configured
   provider are not visible to the proxy and were not measured here.
2. **Forced rotation, no real over-bound (goal 2), measured.** T3's first request rotated by
   `window_minimal` (T2 did not fit W with the new turn): 15 items in, 7 sent, 24,757 real input,
   where the unrotated history would have been about 34,300 (inferred: T2's 24,812 plus T3's prompt of
   about 9,500). The largest real request was 24,897 against the 42,000 bound; 0 `over_bound` events of
   either source.
3. **MCP tools (goal 3).**
   - Measured (pre-flight): no Contexto tool appears in the top-level `tools` list or in the
     `additional_tools` namespaces (`functions`, `clock`, `collaboration`, unchanged in name and
     count). The only mention of a Contexto tool name in the request is the test prompt's own text,
     which names `contexto_state` (input item 6 in all 12 pre-flight requests); no other item names
     any of the five tools. Registering the server grows the code-mode `exec` tool's description from
     10,824 to 14,426 bytes and adds a "deferred" mention: Codex offers MCP tools as deferred tools
     behind tool search, not by name.
   - Measured (T4): the model found and called `contexto_state`, and Codex refused the call:
     "MCP tool call requires approval, but approval policy is never" (exec mode's default).
   - Measured (T5): with `-c 'mcp_servers.contexto.default_tools_approval_mode="approve"'` the call
     completed. Codex 0.154.0 accepts `auto`, `prompt`, `writes`, and `approve` for that key (an
     invalid value fails at config load, measured against the stub).
   - Implication (inferred): `run_codex` should register the contexto server with
     `default_tools_approval_mode = "approve"` (its tools only read the session store or append to the
     ledger), or exec-mode users can never recall. Interactive Codex would prompt instead.
4. **Spend from the session log (goal 4):** above. Measured F (T1's first request) is 15,309 tokens.
   The seed estimate was 21,290 (1.39 times high).

## Proxy restart on the same session (manager-1, 02:53Z)

Each `codex exec resume` started a new proxy and engine on the same Contexto session; the same
happens for `contexto run claude --session S`. Measured from the session log:

1. **Epoch and k are re-derived from the history, not read from the store.** Every new engine starts
   with no state, so its first request is a rotation decision on the full history Codex resends. All
   three rotating processes logged `epoch: 1`. T4's new engine chose k=14, the k the T3 process had
   ended with; T5's chose k=17 because a new complete turn existed. Equal k across processes comes
   from equal sizing decisions, not from persisted state.
2. **Re-paging is idempotent.** T4 re-paged the same 3 turns from `first_turn` 0; the session's page
   files stayed at 3, so the ids matched, and there was no `page_conflict`. T5 paged 4 turns from
   `first_turn` 0: 4 page files (the 3 earlier ones plus 1 new).
3. **Same transformed prefix when k is unchanged.** T4's first request read 24,576 of 24,816 input
   tokens from the cache, so its prefix was byte-identical to what the T3 process sent. T5's first
   request read 15,104 of 15,582 (about the fixed head) because k moved from 14 to 17, which a
   continuing engine would also have done at that turn start (inferred).
4. **Each new engine starts from the seed ratio.** First requests per process overestimated by 1.34 to
   1.40 times (33,437 vs 24,812; 33,160 vs 24,757; 33,306 vs 24,816; 21,737 vs 15,582), and F was
   taken from the estimator (21,283 to 21,308 against the measured 15,309), so W was 14,692 instead of
   about 20,700. Second requests in the same process were within 0.5% (15,487 vs 15,420; 24,942 vs
   24,897; 15,702 vs 15,662). The launcher's "window budget below 25,000" warning used the seed F as
   well.
5. **`carried_over_budget` fires falsely on a new engine's first rotation.** `carried_growth` 21,220
   equals `carried_tokens` 21,220 in all three, because `initial_carried` is still unset when the
   first request rotates, so growth is measured from zero. This is a rule 13 bug for any first
   request that rotates (a resumed session, in Codex or Claude Code), not a real growth.

**Implication for resume:** correctness held across restarts (no conflict, no lost turn, no over-bound,
and an unchanged k kept the cache), but a restarted engine sizes its first request with the seed
ratio and an estimated F, so it rotates earlier and keeps a smaller window than the previous process
would have. When that changes k, the cache prefix is lost. It also reports a false
`carried_over_budget`. Persisting the measured bytes-per-token ratio and head tokens in session
metadata (B9's `meta.json` already has `bytes_per_token`), and setting `initial_carried` before the
first rotation, would remove both effects (inferred).

## Files

- `spikes/m2/codex/smoke.sh`: pre-flight and live turns with the watchdog.
- `spikes/m2/codex/responses_stub.py`: also records where the Contexto tool names occur, and per-tool
  sizes and MCP-related words in `additional_tools` (no text).
- `spikes/m2/codex/smoke_summary.py`: the per-request table above, from a session log.
- Raw logs stay in `.contexto/spikes/M2-A6/` (ignored by Git).
