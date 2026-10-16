# M0 recommendation: what Contexto should build

Author: intake.lead, 2026-10-16, under DECISIONS D0. Inputs: every M0 research document on `main`
(interception, Codex measurements, rollover, fidelity, paging policy, cost model, proxy
measurements, quality versus context). Numbers below carry the tag used in their source.
Not yet on main when this was written: the harder probe set (L2f, still calibrating), lane 2's
synthesis note (PR #19), and the route-identity test (L1-e, finished and reported by manager-1,
doc pending in lane 1's L1-f PR). None of them can change the choice of approach; they set
parameters (the window bound) and caveats.

## Decision

Build **H1, context virtual memory at the API boundary**, as a local proxy started by a launcher
(`contexto run claude ...`, later `contexto run codex ...`). The proxy rewrites each request into
a bounded prompt: the CLI's own system prompt and tools unchanged, a pinned state block, an index
of paged-out turns, and the recent turns verbatim. Old turns are stored locally, word for word,
and the model fetches any of them back by id through an MCP tool. Contract:
`docs/milestones/M2-contexto-proxy.md`.

## Why H1

Ranked by the owner's bar (D2): compaction never fires, accuracy equals full context; then cost.

| Approach | Working context | Accuracy risk | Modeled cost, 300 turns, Opus 5.5 | Verdict |
|---|---|---|---|---|
| Plain CLI | grows to the window; compaction at about 967K (Claude, documented) or 244.8K (Codex, documented arithmetic) | quality near the threshold is what the owner wants to avoid | $716.23 | fails D2 |
| Built-in compaction | sawtooth up to the threshold | lossy summary; loss at scale not measured; mid-turn compaction looped 3 times in one turn (measured, D3-violating run) | $382.10 | fails D2 by definition |
| H2 hooks | cannot shrink; append-only | none added | no saving | fails D2 alone; useful as a re-seed add-on |
| H3 session-file rebirth | sawtooth | thinking and encrypted reasoning cannot be synthesized, so each rebirth loses it (measured that files resume; loss inferred) | not costed separately | fails D2: lossy reset |
| H4 supervisor rollover | floor plus handoff at every rollover: 44,500 plain, 81,808 to 84,073 on an OpenRig seat (measured) | step loss at each rollover; handoff recall 3 of 3 (measured, small) | $337.07 | fails D2: lossy reset |
| H5 provider features | tool-result clearing does not hold a bound (max 757.1k, 781 lossy clears, modeled) | lossy | $308.68 | fails D2 |
| **H1 virtual memory** | **bounded without a lossy reset**; modeled: at a 250k bound 0 of 20 seeds over; at 100k with online headroom 4 of 20 seeds sent some requests over (0 of 20 with ingest paging, whose fidelity is unscored); over-bound turns are possible and logged | old turns stay word for word and recallable by id; thinking stripped at each rotation (cost unmeasured, see risks) | **$87.95** ($117.94 with an 82k OpenRig seat head) | **only candidate that can meet D2** |

H1 is cheaper than compaction from turn 14 on (modeled). Its cost rises with route-side cache
misses: at a 23% miss rate (3 of 13 eligible throwaway calls in the cost model; lane 1 counts 4
of 14 including one compaction summary call), $212.47 against $971.37 for compaction
(modeled), so its advantage widens rather than shrinks.

## Constraints the design must obey (all measured or vendor-documented)

1. **Thinking.** On Fable 5.1, Opus 5.5, Sonnet 5.5, and Haiku 5.5, editing anything before a kept
   thinking block invalidates it; changing `system` or adding text to an earlier user turn while
   keeping thinking returned 400 (measured). So: append-only between rotations, rotate only at a
   user-turn boundary (D17), and strip every earlier thinking block at rotation (measured 200).
2. **Protocol.** Keep each `tool_use` with its `tool_result`; never split a tool loop.
3. **Cache.** Order is tools, system, messages; the head stays byte-stable, the pinned block and
   index change only at rotation, and the window is append-only, so each epoch reads its prefix
   from cache.
4. **Attachment.** In a git worktree Claude Code also loads the main checkout's
   `settings.local.json`, and its base URL wins (measured). The launcher passes an explicit
   `--settings` (D16).
5. **Usage.** The `usage` the CLI sees decides whether it compacts. Forwarding the true usage of
   the bounded upstream prompt should keep the CLI from ever compacting (inferred); the acceptance
   run must prove it.

## Choices made here (new decisions D18 to D21)

- **D18. Build H1.** Reasons above.
- **D19. Recall and pinning go through an MCP server, not a hidden proxy tool.** Lane 1 judged MCP
  recall far simpler (inferred, INT section 5): no stream hold-back, no stitching hidden exchanges
  into the reply, no shadow history that must be reinserted byte for byte. Lane 2's paging policy
  chose a hidden tool; this resolves that conflict in favour of MCP. Cost: recalled pages enter the
  CLI's own local history and are resent to the proxy, so they occupy the window until the next
  rotation pages them out again; the long run must show this does not push the CLI toward
  compaction.
- **D20. Strip all thinking at each rotation.** It is the only strategy measured to return 200
  after a history edit. Its accuracy cost is unmeasured because the owner's proxy drops replayed
  haiku thinking on some requests (lane 1 found three consistent keep/drop classes; Opus kept all
  thinking in 5 requests), so M0 could not isolate it; the acceptance test measures H1 against the
  full-context baseline directly.
- **D21. M1 is folded into M2.** The ledger's record model (kinds, epistemic labels, provenance,
  supersession) becomes the pinned-state store. PRs #2 and #3 are harvested into M2 lane B and
  then closed; PR #4 (M1 contract pins) is closed as superseded. Branches are kept.

## Risks and how M2 retires them

| Risk | Retired by |
|---|---|
| H1 has never run against a real model | M2 acceptance run through `contexto run claude` |
| Accuracy cost of stripping thinking and of paging | fidelity probes through H1 against the full-context baseline at matched sizes |
| The model does not pin what matters | pinned state written through MCP tools plus every user turn kept verbatim and recallable |
| CLI compacts its own local history anyway, including from recalled pages (D19) | acceptance run continues past 2.5 times the bound with zero compaction events |
| Window bound unknown (probes hit the ceiling) | L2f harder probes; default bound set from them, configurable |
| Route-side cache misses on the owner's proxy | owner-side affinity setting (open decision O6); H1 cost model already budgets misses |
| Codex WebSocket transport sends deltas | Codex adapter forces HTTP or handles deltas (M2 lane A, second step) |
