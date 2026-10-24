# M0 lane 2 synthesis: policy and cost

Status: lane 2 deliverable for intake's recommendation (brief questions 3 and 4). Date: 2026-10-15.
Author: build.builder-3, for management.manager-2. This summary states lane 2's conclusions and
links the documents that hold the evidence; it does not repeat their tables.

Tags: **measured** (observed in a run), **vendor-documented** (read on a vendor page), **inferred**
(an argument or assumption), **modeled** (computed by a script on inferred inputs). Each claim names
its source document and pull request.

Sources:

| Document | Lane, PR, state |
|---|---|
| `docs/research/M0-cost-model.md`, `spikes/m0/cost_model/` | L2a, PR #8, merged at 64ab488 |
| `docs/research/M0-paging-policy.md`, `docs/research/M0-fidelity-test.md` | L2b, PR #7, merged at 093429b |
| `docs/research/M0-quality-vs-context.md` | L2d/L2e, PR #18, merged at reviewed head 83ac7ac |
| `docs/research/M0-interception.md`, `M0-codex-measurements.md`, `M0-rollover.md` | lane 1, merged |
| `docs/research/M0-proxy-measurements.md` | lane 1 L1-a, PR #16, merged at reviewed head f560818 |
| lane 1 L1-e route identity check | lane 1, PR or doc pending; relayed by manager-2 on 2026-10-16 |
| `docs/DECISIONS.md` | intake decision log on main (D11, D12, O3, O4) |

## 1. The bar

The owner principle, binding since 2026-10-15 23:12Z: compaction is a failure mode to avoid, not a
mechanism to optimize. The success bar is no compaction ever, with recall and task accuracy equal to
the full-context baseline. Rank approaches by how small they keep working context, then by how
stable quality stays; token cost comes second.

Under that bar every lossy reset fails, whatever it is called: built-in compaction, a supervisor
rollover handoff (H4), and server-side tool-result clearing all replace content with a summary or a
placeholder.

## 2. Ranking

Ranked by the bar: inside the bound with no lossy reset first, then smaller working context, then
cost. Numbers are for the inferred 300-turn profile on Claude Opus 5.5, from the cost model (PR #8;
the summary wording is tightened in this PR). Bounds of 30k, 100k, and 250k are placeholders until a
quality-vs-size measurement finds the real one (section 3).

| Rank | Strategy | Working context | Lossy resets | Verdict |
|---|---|---|---|---|
| 1 | (iii) context virtual memory, online headroom (largest completed turn + 25%) | meets 250k on 20 of 20 seeds; 100k on 16 of 20 (the other 4 send 5 requests over in all, worst seed 2) | 0 | best under the bar; fidelity depends on recall, unscored |
| 1b | (iii) plus ingest paging of tool results over 8k | 100k on 20 of 20 seeds | 0 | valid only under the conditions in section 4; fidelity of paged results unscored |
| 2 | H4 supervisor rollover, reset inside the bound | stays inside | many (163 at a 75k reset point, one seed) | fails: each handoff is lossy |
| 3 | (ii) built-in compaction, reset inside the bound | stays inside | many (205 at 75k) | fails: compacts |
| 4 | H5 server-side tool-result clearing | grows past 250k | every clear | fails: over the bound and lossy |
| 5 | (i) plain CLI, (ii) at the CLI default | up to the 1M window, then compaction | 0, then compaction | fails: over the bound |

All rows are **modeled** (PR #8, `M0-cost-model.md`, ranking and seed_sweep tables). No strategy
meets 30k with the model's inferred 42k head. Lane 1 measured 45,925 tokens for plain Claude Code and
81,808 for an OpenRig seat, about 36k of it seat context (PR #16; `docs/DECISIONS.md`
O3). A seat head leaves little of a 100k bound for anything else.

Cost, second (**modeled**, PR #8, API-key billing with a 5 minute TTL, preserved thinking): (iii)
about $88, H4 about $337, (ii) about $382, (i) about $716 over 300 turns. Three effects are larger
than the strategy choice in some settings:

- **Cache TTL.** A 1 hour TTL roughly halves (i) and (ii).
- **Route-side cache misses.** At a 23% full-miss rate both (ii) and (iii) cost about 2.5x more.
- **Measured seat head.** An 82k head raises (iii) to about $118.

(iii) costs more than (ii) only in sessions shorter than about 14 turns.

## 3. What the quality measurements show, and what they do not

- **Baseline (measured, PR #7, `M0-fidelity-test.md`).** The plain CLI scored 13/13 and 37/37 at about
  29k to 30k tokens, before and after `/compact`. Compaction was lossless at that size on these
  probes. The same document's simulated scores for paging and recall are "largely by construction"
  and are not evidence for (iii).
- **Quality vs context size (measured, PR #18, `M0-quality-vs-context.md`).** A seat
  run scored 96/96 at 98,910 and at 256,783 tokens (Wilson 0.962 to 1.00). Every category was 24/24,
  and all 64 facts planted 150k or more back were recalled. A replay scored 96/96 at 40,749 and 91,861.
- **What that does not show.** The probes are easy, explicitly stated facts, and every category sits
  at the ceiling. So the runs locate no slip between about 99k and 257k; they do not show quality is
  flat. The harder-probe run (L2f) is designed to find one.
- **Preserved vs stripped thinking: inconclusive.** Through the rig proxy route, replayed thinking
  was dropped on every request, even on pure appends (`thinking_dropped`,
  `end_user_binding_mismatch`; measured, PR #7 and PR #18). Lane 1 L1-e found the route spreads
  requests across at least three upstream end-user identities (measured pattern, inferred cause,
  pending), so the drop is a route artifact. Both arms were therefore likely the same request
  (inferred), and 96/96 is not evidence that stripping is free.
- **Route-independent results.** Only the two prefix cases that returned 400 with
  `prefix_mismatch_behavior=error` count as route-independent: a changed system prompt, and text
  added to an earlier user turn while keeping thinking (measured, PR #7 conformance probe).
- **Nothing measures (iii) itself.** Its fidelity with paging and recall waits for the phase 2
  rewriting prototype (brief, phase 2).

## 4. Design constraints learned

- **No client-side edit inside an active tool-use turn.** Within a tool-use turn, thinking blocks
  must be passed back (vendor-documented). Changing anything in front of them while keeping them is
  invalid. A mid-turn mismatch disables thinking silently (inferred). The tool-loop case is
  inconclusive on this route (PR #7).
- **Epochs at turn boundaries, with headroom.** (iii) rewrites its prefix only between turns. It
  keeps headroom H free, and H must cover the largest turn, including the recall it triggers (up to
  32,000 characters per recall call).
  - A fixed 3k H fails against measured 16k to 18k Reads (PR #7, PR #8).
  - A live proxy has to size H online. The oracle p99 row in the cost model is an upper bound on what
    one can do (modeled, PR #8).
- **Thinking is preserved and billed by default** on Opus 5.5, Haiku 5.5, and Fable 5.1
  (vendor-documented). Stripping it at each epoch is valid but costs extra output: +4.6% to +67.1%
  when dropped every turn, by effort level (vendor-documented; charged at 9.7% in the model).
- **Server-side `clear_tool_uses` is lossy.** It is a last-resort fallback that fails the bar and
  restarts caching from the first cleared block (vendor-documented, PR #7, PR #8).
- **Ingest-time paging is valid only under three conditions** (inferred design, PR #7, PR #8):
  - stubs stay byte-identical on every request, through a proxy-held mapping keyed by `tool_use_id`;
  - recall inside the turn reinserts the page byte for byte;
  - probes check answers the model gives from the stub alone.

  T = 8,000 and E = 400 are placeholders.
- **Cache layout.** A stable head goes first, with its own breakpoint. Pinned state and the stub
  index change only at epochs. The window is append-only between epochs, and a handoff or seed goes
  after the stable prefix (inferred; PR #8, lane 1 `M0-rollover.md`).
- **Route-side cache misses are intermittent.** On the rig route, 4 of 14 eligible calls were full
  misses, including byte-identical prefixes, while another seat session hit on 11 of 14 (measured,
  lane 1 PR #16). Lane 1 L1-e explains the pattern (measured pattern, inferred cause, pending).
  - The route spreads requests across at least three upstream identities.
  - A byte-identical series shows exactly three cache writes before reads settle.
  - So a miss is the first request to reach an identity that has not seen the prefix yet.
  - Cost on a pooled route: up to N full-price prefix writes per session, where N is the number of
    identities (inferred).
- **The CLI may still compact under (iii).** Both CLIs trigger compaction on reported usage, so a
  proxy that bounds the upstream prompt should stop it (inferred, lane 1 `M0-interception.md`; live
  check in phase 2). Claude Code's idle compaction on the 1 hour TTL acts on its own local history,
  which a proxy does not shrink. It would compact a subscription session about 7 times in 300 turns
  (modeled, PR #8) unless `idleCompaction` is false (vendor-documented setting).

## 5. Open questions and owner decisions

1. **The working-window bound (O3) and harder probes (L2f, budget pending).** Without probes below
   the ceiling, no slip point can be located, and so neither can the bound. The bound values above
   are placeholders.
2. **A stable-identity route for thinking tests.** Preserved vs stripped thinking stays inconclusive
   until a route preserves replayed thinking and reports `input_transformations` (PR #18).
3. **Per-session upstream affinity (owner decision; intake raising it).** Both the dropped thinking
   and the byte-identical cache misses trace to the route spreading requests across at least three
   upstream identities (lane 1 L1-e: measured pattern, inferred cause, pending). Keeping one
   upstream identity per session would remove both the extra prefix writes and the thinking drop.
   Lane 1 PR #16 calls it an owner-side setting, and the proxy is the owner's infrastructure (D11),
   so it is the owner's call.
4. **Auth (Q5, lane 1).** Both CLIs send API-key and login credentials to a custom base URL
   (`M0-interception.md`, section 6). D12 settles Q5 for development: rewriting runs only through
   the owner's proxy with the rig key, never on a claude.ai or ChatGPT login. O4, the auth guidance
   for end users, stays open for the owner.
5. **Phase 2 rewriting prototype.** It is the only way to measure (iii) itself: its cost, the CLI's
   compaction under a bounded proxy, and the fidelity of paging and recall.
6. **Smaller items for the owner:**
   - set `idleCompaction: false` for sessions behind (iii);
   - decide whether a proxy may send keepalive pings the user never typed (PR #8);
   - take PR #16's measured seat head (about 82k) as the planning head for seats.
