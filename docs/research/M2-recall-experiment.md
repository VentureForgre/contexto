# M2 recall experiment after H1 (2026-10-16)

The H1 acceptance rerun (main `c1b60f6`) made no `contexto_recall` call; acceptance 3 failed. The
zero-spend diagnosis found the preamble present but missing the contract's instruction to recall
before relying on a paged-out detail, and page index lines that all read alike. This experiment
tested fixed preambles on H1's own final probe request before changing the product.

## Method

- **Requests.** H1's final probe-turn request was rebuilt offline from the recorded run (Claude
  Code's session JSONL and the stored pages) with lane A's variant builder (PR #70, `03335ad`,
  `--strip-thinking`) and lane B's store variants (PR #71). The engine's decisions matched the
  recorded run on all 19 requests. One signed thinking block was stripped from each, uniformly.
- **Variants.** V1: store `v1` (conformant opening lines, salient identifiers in index lines),
  engine default. V2: store `v2` (V1 plus a worked search-then-recall example) and the preamble placed
  first. V3: V2 plus an end-of-window reminder on each retained turn. Control: the best variant with
  H1's recorded probe wording. V1 to V3 used the product-realistic probe wording, which names no tool.
- **Sends.** One send each, first response only (no tool was run), `max_tokens` 8,000, through the
  owner's proxy. Each was gated on the builder's token estimate plus 6 percent, at most 110,000,
  under a 500,000 cap.
- **Access.** The owner's proxy refused urllib's default user agent (403) and the key in `x-api-key`
  alone (401). Intake approved, and the proxy accepted, an honest user agent naming the harness with
  the seat key in `x-api-key` and `Authorization: Bearer`.

## Results

H1's probe turn made no tool call. Every variant called Contexto's tools in its first response:

| Send | Tool calls in the first response | Calls that reach the paged-only page | Spend |
|---|---|---|---|
| V1 | state; search `retention_days`, `map_version`, `Ownership note`, `AI-78048` | 1 | 102,898 |
| V2 | 15 searches, including all four paged-only keys exactly | 4 | 103,311 |
| V3 | search `retention_days`, `map_version`, `ledger-svc`, `AI-78048`; state | 1 | 103,407 |
| Control (V2, recorded wording) | search `checkout-api.retention_days`, `map_version`, `ledger-svc` | 1 | 105,797 |

Total spend 415,413 tokens. Intake chose V2 (store `v2` and the preamble placed first, no reminder).

Limits [inferred]: one sample per variant; the first response only, so recall calls, the tool loop,
and the answers were not run.

## Caveat on record (intake, 2026-10-16 03:39Z)

All four result sends carried `claude-code-20250219` among the 11 betas copied from H1's recorded
request. Access did not depend on it: the diagnostic call the proxy accepted (2026-10-16 03:24Z,
`count_tokens` with a minimal body, the honest user agent, and the key in both headers) carried no
`anthropic-beta` header at all. The
comparison with H1 is like-for-like, since H1's own requests carried the same 11. Its effect on tool
use was not measured. The result was accepted without a repeat.

Standing rule D23 followed: every request the team sends directly, not through the real CLI, uses an
honest user agent naming the harness and never carries another product's client marker. In the
harness, `replay.harness_betas` drops every `claude-code-*` beta from direct requests, with a test.

## Code

`spikes/m2/acceptance/experiment.py` is the sender. It sends each rebuilt body once and reads the
first response. Before each send it checks the input size: count_tokens, or the builder's estimate,
which these runs used because the proxy refused count_tokens before the headers were fixed. It
refuses a send above the per-send limit or past the approval. It records tool calls, with their
input, whether each reaches the turn-0 page, usage, and the paged-only answer lines.
`diagnose_count_tokens` makes one call that records only the status, error type, and redacted
message. The numbers above came from this code at `eaf95a3`, whose sends still carried the copied
betas. This version differs only in passing every header through `replay.beta_headers` (D23).

Evidence (local, not in Git): `.contexto/spikes/recall-experiment/`.
