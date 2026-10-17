# M2 A32: the working-window bound for an OpenRig seat (modeled)

This is a proposal for decision O3 (the working-window bound) in the owner's own use case: Contexto
under Claude Code on an OpenRig seat, with Opus 5.5. It is **modeled** throughout. Nothing was sent to
a model; the engine ran offline on a synthetic seat session. Numbers only.

Reproduce: `uv run python spikes/m2/acceptance/seat_bound.py --json <file> --csv <file>`. The per-request
rows of the six base sessions are in [`data/M2-seat-bound.csv`](data/M2-seat-bound.csv).

## Recommendation (modeled)

**Keep 150,000 for seats**, the current default. The owner's bar comes first (D2: compaction never
fires; accuracy equals full context; then cost), and on that bar the margin to idle compaction
decides.

- **The trade.** A turn adds its growth on top of its turn-start input, and rotation keeps that input
  under the bound.
  - The largest single turn that stays under Claude Code's 200,000 idle-compaction minimum is 200,000
    less the largest turn-start input.
  - At a 92,000 head that is 87,141 tokens at 150,000, 78,133 at 160,000 and 67,225 at 170,000.
  - At an 82,000 head: 88,133, 77,225 and 71,350.
  - So 170,000 lowers the read size that triggers idle compaction by about 20,000 tokens. A turn
    growing 67,000 to 87,000 tokens crosses 200,000 only at 170,000.
  - M0 measured a 149,140-token read on this very seat (`lab.subject`, G2), which crosses at every bound.
- **What 170,000 would buy.** A larger window for up to 13.8% more cost at a 92,000 head:
  - W of 50,331 to 52,958 against 30,030 to 32,958, and 10 rotations against 15 over 56 turns.
  - At an 82,000 head it costs 0.8% less.
  - No accuracy figure says the larger window is needed. Recall room never fell below 12,000 at any
    bound. M0 found no slip point by context size (96/96 at 98,910 and at 256,783, O3).
- **When to revisit.** 170,000 is justified only if a live seat check shows that 150,000's window
  costs recall accuracy.
- **What no bound fixes.** A large read passes 200,000 at every bound.
  - If Claude Code's idle compaction counts the bounded usage it reports (O3's premise, unverified),
    no bound guarantees D2 on a seat that reads large files.
  - If it counts its local history instead, that passes 200,000 early at any bound.
  - Either way, Claude Code's `idleCompaction: false` setting is what removes the risk. That is the
    seat owner's setting, not Contexto's.

## Headline (base session: 56 turns, no restarts)

| Head | Bound | Window budget W | Window target | Rotations | Turn-start room: min / median | Largest turn-start input | Largest one turn under 200,000 | Peak input | Margin to 200,000 | Cached share | USD |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 82,000 | 150,000 | 40,091 to 42,958 | 20,045 to 21,479 | 14 | 13,133 / 23,791 | 111,867 | 88,133 | 116,889 | 83,111 | 94.7% | 7.07 |
| 82,000 | 160,000 | 50,331 to 52,958 | 25,165 to 26,479 | 10 | 12,225 / 26,939 | 122,775 | 77,225 | 128,597 | 71,403 | 95.0% | 7.26 |
| 82,000 | 170,000 | 60,570 to 62,958 | 30,285 to 31,479 | 7 | 16,350 / 37,000 | 128,650 | 71,350 | 132,390 | 67,610 | 95.6% | 7.01 |
| 92,000 | 150,000 | 30,030 to 32,958 | 15,015 to 16,479 | 15 | 12,141 / 22,650 | 112,859 | 87,141 | 116,161 | 83,839 | 95.8% | 6.59 |
| 92,000 | 160,000 | 40,091 to 42,958 | 20,045 to 21,479 | 14 | 13,133 / 23,791 | 121,867 | 78,133 | 126,889 | 73,111 | 95.1% | 7.31 |
| 92,000 | 170,000 | 50,331 to 52,958 | 25,165 to 26,479 | 10 | 12,225 / 26,939 | 132,775 | 67,225 | 138,597 | 61,403 | 95.3% | 7.50 |

- Every rotation was for the room (`min_turn_room`, 12,000).
- In all six sessions: 0 turn starts below 12,000, 0 `window_minimal`, 0 `turn_room_low`, 0
  `bound_too_small`, and 0 `over_bound`.
- The largest growth inside one turn is 5,823 tokens (a write with a long reply).
- "Margin to 200,000" is the peak of this session's measured turn sizes. "Largest one turn under
  200,000" is the decisive figure: the biggest single-turn growth that still stays under idle
  compaction (200,000 less the largest turn-start input).
- Plain Claude Code on the same requests, with a best-case cache, costs 8.69 (82,000 head) and 8.93
  (92,000 head). Contexto is 16.1% to 26.3% cheaper in every case above.
- Plain's peaks, 372,232 and 382,232, pass 200,000. Its cost therefore assumes no idle compaction.

## The seat session (synthetic, labeled)

The M0 seat measurements (`docs/research/M0-l2d-seat-usage.csv`, `lab.subject`, session `93eadb5d`) have
per-call usage only: no request bodies or byte sizes. They cannot drive the engine, so the session is
synthetic, built to match them.
- **Fixed head:** 82,000 and 92,000 tokens.
  - Measured first calls were 81,808 and 84,073, and the context was 92,197 after 4 turns.
  - Decision O3 gives a seat's fixed overhead as near 92,000.
- **Turns:** the 7 measured small turns (S0, O1, O2, G1, P1, the first call of G2, P2), cycled 8 times
  for 56 turns.
  - Each turn keeps its measured user-message size, tool rounds (0 or 1) and per-call outputs (5 to
    4,777 tokens).
  - That gives 96 requests.
- **Large read (a variant):** at turn 28, one tool round of 149,140 tokens, which is G2's measured
  growth from nine parallel reads.
- **Tokens:** every text is filler at 4 bytes per token, and every count is bytes over 4. The engine's
  usage feedback therefore sees exactly what it sent.
- **Not modeled:**
  - Claude Code's own system reminders and attachments.
  - Thinking blocks.
  - The seat path's intermittent cache misses: M0 measured 2 full misses in 14 calls on
    `93eadb5d`, cause inferred to be route-side.

## Method

- **Engine.** The real `RewriteEngine` at each bound with the defaults: `headroom_floor` 25,000,
  `window_fraction` 0.5, `min_turn_room` 12,000.
  - W = bound − headroom − F, where F is the fixed overhead (head, preamble, pinned text and index).
  - Headroom = max(25,000, 1.25 × the largest turn growth seen).
  - The window target is 0.5 × W.
  - W and F come from the engine's own rotation events.
- **Turn-start room** = bound − input − 25,000, at each turn's first request.
- **Cache model** (A28's checkpoints, an assumption):
  - A request reads from cache the longest earlier whole prompt that prefixes it.
  - Otherwise it reads the head alone when the head is unchanged.
  - A rotation therefore reads the head, and the request after it reads the rotated prompt whole.
- **Cost:** Opus 5.5 at the M0 cost model's prices (`docs/research/M0-cost-model.md`, prices table,
  vendor-documented 2026-10-15), in USD per million tokens. Every uncached input token is counted as a
  cache write.

  | Input | Output | Cache read | Cache write (5 minute TTL) | Window |
  |---|---|---|---|---|
  | 4 | 20 | 0.2 | 5 | 1.00M, no long-context step |

- **Compaction thresholds:**
  - Claude Code's idle compaction has a 200,000 minimum (`docs/DECISIONS.md` O3;
    `docs/research/M0-cost-model.md`). The launcher's 170,000 cap exists to stay under it.
  - Claude Code compacts on its own at about 967,000 on 1M-window models such as Opus 5.5
    (`docs/research/M0-interception.md`). No request here comes near it.

## Proxy restarts (rule 16)

**Assumption:** a seat's proxy restarts when its Claude Code process does (a rig restart, restore or
crash). The rate is unmeasured. Here it is once every 14 turns: 3 restarts in 56 turns.

| Head | Bound | Restarts with resume | Restarts without resume |
|---|---|---|---|
| 82,000 | 150,000 | identical to no restarts: 14 rotations, 7.07 USD | 13 rotations (10 room, 3 bound), 6.94 USD (−1.8%) |
| 82,000 | 160,000 | identical: 10 rotations, 7.26 USD | 11 (8 room, 3 bound), 7.28 USD (+0.2%) |
| 82,000 | 170,000 | identical: 7 rotations, 7.01 USD | 7 (4 room, 3 bound), 6.97 USD (−0.5%) |
| 92,000 | 150,000 | identical: 15 rotations, 6.59 USD | 17 (14 room, 3 bound), 6.66 USD (+1.2%) |
| 92,000 | 160,000 | identical: 14 rotations, 7.31 USD | 13 (10 room, 3 bound), 7.18 USD (−1.7%) |
| 92,000 | 170,000 | identical: 10 rotations, 7.50 USD | 11 (8 room, 3 bound), 7.52 USD (+0.2%) |

- **With rule 16's epoch record:** a new engine resumes, and every request is byte-identical to the
  session without restarts.
- **Without it:** each restart rotates once for size. On a seat that changes cost by −1.8% to +1.2%,
  because a seat restarts rarely. A Codex run that starts a proxy per turn is a different case (A28,
  A29).

## A large read (one turn, 149,140 tokens)

This table describes the engine before rule 17. With rule 17 (headroom capped at a quarter of
the bound and decaying by half per turn, v0.2.2), the 92,000-head session at bound 150,000 has 15
rotations, 1 `window_minimal`, 0 `bound_too_small`, and a window budget never below 18,907
(reviewer-2's real-engine reproduction for A34, PR #187; `docs/research/M2-headroom-outliers.md`).
The large read itself still passes 200,000 at every bound.

| Head | Bound | Peak input | Margin to 200,000 | `window_minimal` / `bound_too_small` | Rotations | USD |
|---|---|---|---|---|---|---|
| 82,000 | 150,000 | 259,873 | −59,873 | 27 / 27 | 33 | 7.51 |
| 82,000 | 160,000 | 271,532 | −71,532 | 27 / 27 | 31 | 7.57 |
| 82,000 | 170,000 | 248,586 | −48,586 | 27 / 27 | 31 | 7.58 |
| 92,000 | 150,000 | 258,586 | −58,586 | 27 / 27 | 34 | 7.41 |
| 92,000 | 160,000 | 269,873 | −69,873 | 27 / 27 | 33 | 7.75 |
| 92,000 | 170,000 | 281,532 | −81,532 | 27 / 27 | 31 | 7.81 |

- **The read itself.** The request after the read passes 200,000 at every bound, by 48,586 to 81,532.
  No bound up to 170,000 prevents that; only a smaller read does.
- **After the read (an engine finding).** Headroom is 1.25 × the largest turn growth seen, and that
  maximum persists.
  - After one 149,655-token growth, headroom is about 187,000, larger than the bound.
  - W stays negative for the rest of the session: as low as −136,986.
  - So each of the 27 remaining turns rotates to the minimal window (only the new turn) and logs
    `bound_too_small`.
  - The retained context for the rest of the session is one turn.
  - **A restart does not clear it.** Since v0.2.0, rule 16 records `max_turn_growth` in the epoch record
    and restores it, so the degradation survives proxy restarts too.
  - A cap or decay on the growth that sets headroom would avoid this. It is a follow-up packet (A33,
    builder-1).

## Accuracy risk of each bound (modeled)

| Bound | Window budget W (82,000 to 92,000 head) | Turn-start room: min / median (92,000 head) | Largest one turn under 200,000 (92,000 / 82,000 head) | Risk |
|---|---|---|---|---|
| 150,000 | 30,030 to 42,958 | 12,141 / 22,650 | 87,141 / 88,133 | The smallest window: at a 92,000 head about 15,000 to 16,500 tokens of recent turns are kept (the target), so more is paged and recall carries more. The most margin to idle compaction. |
| 160,000 | 40,091 to 52,958 | 13,133 / 23,791 | 78,133 / 77,225 | Between the two. |
| 170,000 | 50,331 to 62,958 | 12,225 / 26,939 | 67,225 / 71,350 | The largest window. The least margin: a single turn growing past 67,225 tokens reaches idle compaction. |

- Recall room never fell below `min_turn_room` at any bound.
- No accuracy figure is modeled here. M0 found no slip point by context size: 96/96 at 98,910 and at
  256,783 on `lab.subject` (O3). A larger window therefore carries no measured accuracy cost.

## What a live check would need

- **One seat session at 150,000 through the launcher,** Opus 5.5, with the real seat head. A
  170,000 arm is worth adding only to test whether the larger window changes recall accuracy.
- **It would measure:**
  - F, W and the rotations;
  - the cache read after each rotation, given the seat path's intermittent misses;
  - recall accuracy on planted probes;
  - and the idle-compaction premise: whether Claude Code counts the bounded usage it reports. That is
    inferred in O3 and still unverified.
- **Size:** at this model's figures, 56 turns would be about 10 million input tokens and about 6.59 USD
  at 150,000, or about 11.6 million and 7.50 USD at 170,000.
  The approval would be priced at the bound, as for A22.
- **A large-read turn** should be run only with the headroom finding above understood.

## Limits

- **Synthetic session.** It is shaped on one measured seat session of 8 turns, on Haiku 5.5. Real seat
  turns vary more: hooks, attachments, thinking, and long tool outputs.
- **Head range.** The head is taken as 82,000 to 92,000. A heavier seat head shrinks W one for one.
- **Cache.** A28's checkpoint model, which was checked on two recorded runs, not on a seat. Seat-path
  misses are not modeled, and each miss bills the whole context as a write.
- **Restart rate.** Assumed (1 in 14 turns), not measured.
- **Prices.** Every uncached token is counted as a 5-minute write. The 1-hour TTL and its idle
  compaction are not modeled.
