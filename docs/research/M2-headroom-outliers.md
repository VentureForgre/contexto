# M2: headroom that forgets outliers, offline replay (packet A33)

Proposal with contract text for intake. Zero spend: every number comes from replaying requests
through the engine offline. Numbers only. No `src` change: the candidates are prototypes in
`spikes/m2/acceptance/headroom_outliers.py` (tests:
`spikes/m2/acceptance/tests/test_headroom_outliers.py`).

## The problem (A32, confirmed by reviewer-2 on #183)

Today headroom = max(`headroom_floor`, ceil(1.25 x `max_turn_growth`)) (`rewrite.py`, `headroom`),
and `max_turn_growth` only grows. Since v0.2.0, rule 16 records and restores it, so a restart no
longer clears it. After one 149,140-token read in A32's synthetic seat session (92,000-token head),
the window budget W = bound - headroom - F stays negative for the rest of the session: 27 of 27
later rotations are `window_minimal`, each with `bound_too_small`.

## Headline

**A32 seat session with the large read** (56 turns, 92,000-token head, the read in turn 29; Opus 5.5
prices, A28's checkpoint cache model; modeled):

| Bound | Rule | Rotations (reasons) | `window_minimal` | `bound_too_small` | Smallest W | Turn-start room min / median | USD |
|---|---|---|---|---|---|---|---|
| 150,000 | today | 34 (7 room, 27 minimal) | 27 | 27 | -136,986 | 12,141 / 26,651 | 7.413 |
| | cap 25% | 15 (7 room, 1 minimal, 7 size) | 1 | 0 | 17,470 | 12,141 / 23,633 | 7.262 |
| | cap 33% | 30 (7 room, 21 minimal, 2 size) | 21 | 0 | 4,734 | 12,141 / 26,511 | 7.389 |
| | decay 0.5 | 16 (14 room, 2 minimal) | 2 | 2 | -135,313 | 12,141 / 23,031 | 7.316 |
| | decay 0.75 | 18 (13 room, 5 minimal) | 5 | 5 | -135,313 | 12,141 / 23,207 | 7.334 |
| | last 3 turns | 17 (14 room, 3 minimal) | 3 | 3 | -135,553 | 12,141 / 23,031 | 7.368 |
| | last 5 turns | 18 (13 room, 5 minimal) | 5 | 5 | -135,671 | 12,141 / 23,207 | 7.334 |
| | **decay 0.5 + cap 25%** | 15 (14 room, 1 minimal) | 1 | 0 | 18,907 | 12,141 / 23,031 | 7.313 |
| 170,000 | today | 31 (4 room, 27 minimal) | 27 | 27 | -116,988 | 12,225 / 46,209 | 7.807 |
| | cap 25% | 9 (4 room, 1 minimal, 4 size) | 1 | 0 | 32,652 | 12,225 / 28,774 | 7.753 |
| | cap 33% | 16 (4 room, 1 minimal, 11 size) | 1 | 0 | 18,128 | 12,225 / 33,357 | 8.146 |
| | decay 0.5 | 10 (8 room, 2 minimal) | 2 | 2 | -115,019 | 12,225 / 27,110 | 8.072 |
| | decay 0.75 | 12 (8 room, 4 minimal) | 4 | 4 | -115,019 | 12,225 / 27,149 | 8.030 |
| | last 3 turns | 11 (8 room, 3 minimal) | 3 | 3 | -115,555 | 12,225 / 27,149 | 8.027 |
| | last 5 turns | 13 (8 room, 5 minimal) | 5 | 5 | -115,673 | 12,164 / 27,724 | 8.052 |
| | **decay 0.5 + cap 25%** | 9 (8 room, 1 minimal) | 1 | 0 | 34,203 | 12,225 / 27,174 | 8.036 |

Every rule has the same peak input (258,586 at 150,000; 281,532 at 170,000) and 1 over-bound turn:
the large read itself (see "The 149,140-token turn"). No turn start was below `min_turn_room`, and
no `turn_room_low` was logged under any rule.

Today's higher median room at 170,000 is not a gain. It comes from `window_minimal`: the window
keeps only the new turn, so almost no recent history is in view.

**The recorded runs** (A28's replay, content rates: Codex A22 at 3.0193 and Claude run #3 at 1.8274
bytes per token). Every candidate is identical to today on both runs: Codex 19 rotations and $3.626,
Claude 6 rotations and $0.1581, with no `window_minimal`, `turn_room_low`, or `bound_too_small`. No
turn in either run grew enough for 1.25 x growth to pass the 25,000 floor, so the rules never differ
there.

## Candidates

- **Cap** (headroom at most a share of the bound). It removes `bound_too_small` at once and keeps W
  positive. It never forgets, though: after one outlier, headroom stays at the cap (37,500 at
  150,000) instead of the 25,000 floor for the rest of the session. A 33% cap is too high for a
  92,000 head at 150,000: W falls to 4,734 and 21 rotations are `window_minimal`. The cap that keeps
  W positive depends on the head, so it should be modest.
- **Decay** (at each turn start, max_turn_growth = max(floor(previous x d), this turn's growth)). It
  forgets: with d = 0.5 the outlier's weight halves at every turn start. It cannot help the first
  turns after the outlier, though (2 `window_minimal` and 2 `bound_too_small` at d = 0.5, 4 to 5 at
  0.75).
- **Recent window** (the largest growth of the last N turn starts). It behaves like decay, and is
  slightly worse at the same speed (3 and 5 `window_minimal`). It also needs the last N growths in
  the rule 16 record, a list, which would be a store contract change.
- **Decay 0.5 + cap 25%** (recommended). The cap bounds the worst case at once: no
  `bound_too_small`, and W at least 18,907 at 150,000 and 34,203 at 170,000. The decay returns
  headroom to the floor, so W regains its full size within a few turns. It has the fewest rotations
  of any forgetting rule (15 and 9). It costs within 0.7% of the cheapest candidate at 150,000
  ($7.313 against $7.262 for the cap alone) and 3.7% more at 170,000 ($8.036 against $7.753); both
  are below today ($7.413, $7.807).

## Interactions

- **Rule 5 (never rotate inside a tool loop).** Unchanged by every candidate. Headroom is read in
  the rotation decision at a turn start (`rotate_at_turn_start`) and in the window budget W, which an
  engine also sets once from the measured head after its first response (A8 (c)). The in-turn
  over-bound check never reads it. Decay is applied at turn starts only. The outlier turn itself is
  sent unrotated under every rule, as today.
- **Rule 16 (resume).**
  - Decay keeps `max_turn_growth` one integer, the decayed value. The record is unchanged, and a
    resumed engine applies the next decay at its next turn start, as the old engine would have.
  - The cap is stateless: it applies to the restored value at every use.
  - A recent window would need the last N growths recorded, a list, so it changes lane B's record
    schema. Not recommended for that reason.
  - Records written before the change: the decay factor and the cap are new `Config` settings, so a
    v0.2.0 record's config differs and it does not resume (`settings_changed`). The new engine
    starts from epoch 0 with `max_turn_growth` 0, which also clears any recorded outlier. After the
    change, a restored outlier is capped at once and decays from the next turn start.
- **`min_turn_room`.** The candidates change W and so the room target of a room rotation, but no
  turn start fell below `min_turn_room` under any rule, and `turn_room_low` stayed at 0.

## The 149,140-token turn

- **What happens to the turn itself.** Under today's rule and every candidate, the turn starts at
  108,931 tokens at 150,000 (131,878 at 170,000). Its one tool round adds the read, so the next
  request is 258,586 (281,532), past Claude Code's 200,000 idle-compaction minimum. It is one tool
  round, and rule 5 forbids rotating inside a tool loop, so the engine sends it unrotated and logs
  it over bound (1 `over_bound` under every rule). Rewriting inside the loop would conflict with
  rule 5 (the in-flight turn's thinking and tool pairing), so it is not proposed.
- **What the engine can do.** It already logs the in-turn over-bound request (rule 5). At the next
  turn start it rotates. Today that rotation and every later one are `window_minimal` because of the
  stuck headroom. With the recommended rule they return to normal within a few turns.
- **Out of Contexto's control.** The size of a tool result (the CLI decides how much a read
  returns), and whether Claude Code compacts on its own threshold while over 200,000 mid-loop.
- **The ceiling (reviewer-2's margin trade).** The largest single turn that stays under 200,000 is
  200,000 minus the size at its turn start: 91,069 at 150,000 and 68,122 at 170,000 in this session.
  In the worst case the turn starts near bound - 25,000 - 12,000, which gives reviewer-2's 87,141
  and 67,225 for a 92,000 head. **No candidate changes this ceiling.** Headroom shapes rotation
  decisions at turn starts. Before the first outlier, every rule's headroom is at the floor, so the
  outlier turn starts at the same size under all of them. Only a smaller bound, or more room kept at
  turn starts (a larger `headroom_floor` or `min_turn_room`, at the cost of window), raises it.

## The cost of the recommendation (reviewer-1, answering intake)

Forgetting returns the window to normal after an outlier. That also returns the in-turn ceiling for
a later large turn to normal. Today's stuck headroom keeps only the newest turn in view, and that
leaves extra margin as a side effect.

- **`over_bound`** is 0 under every rule on Codex A22 and Claude run #3, and 1 on the seat session
  (the read itself). Peaks are identical across rules (26,066, 109,868, 258,586 and 281,532).
- **Synthetic double reads at 150,000** (reviewer-1's replay: the seat session with a second large
  read 2, 3, 5 or 8 turns after the first):
  - With fixed sizes (149,140, 60,000, 40,000, 25,000), `over_bound` is the same for today and the
    recommended rule. The second read's turn starts 0 to 5,875 tokens larger under the recommended
    rule (92,000 head).
  - The largest second read each rule keeps under the bound differs, so there is a band. A second
    read in that band is logged over the bound inside its loop under the recommended rule, but stays
    under it today:

    | Head | Second read at | Today | Recommended | No earlier outlier |
    |---|---|---|---|---|
    | 30,000 | +3 turns | ~116,600 | ~110,700 | ~58,400 |
    | 30,000 | +8 turns | ~113,400 | ~77,200 | ~74,400 |
    | 60,000 | +3 turns | ~86,600 | ~80,700 | |
    | 60,000 | +8 turns | ~83,300 | ~47,200 | |
    | 92,000 | +3 turns | ~54,600 | ~48,600 | ~45,800 |
    | 92,000 | +8 turns | ~51,400 | ~48,600 | ~48,600 |

  - The band is about 6,000 tokens wide 3 turns after the outlier. With a small head it is up to
    about 36,000 tokens wide 8 turns after it.
- **This is the cost of the recommendation.** Today's extra margin comes only from `window_minimal`.
  The recommended rule's ceiling is never below a session with no outlier at the same turn (last
  column). It returns to normal and does not make large turns riskier than normal. In exchange, the
  model keeps more history in view, with fewer rotations.

## Proposed contract text (rule 4, headroom; for intake)

> **Headroom.** At a turn start, headroom = max(`headroom_floor`, min(ceil(`headroom_factor` x
> `max_turn_growth`), floor(`headroom_cap` x bound))), with `headroom_cap` = 0.25. At each turn
> start where the engine measures the turn's growth g (as today: a previous turn start is recorded
> and the turn count has grown), and before the rotation decision, `max_turn_growth` =
> max(floor(previous `max_turn_growth` x `headroom_decay`), g), where g is the tokens of the
> messages added since the previous turn start, per turn start, as today, and `headroom_decay` =
> 0.5. A turn start sent again with no new turn (a retry) measures no growth and does not decay.
> So one outlier turn raises headroom by at most a quarter of the bound, and its weight halves at
> every later turn start that measures growth. Both are configuration settings (`headroom_cap`,
> `headroom_decay`), included in the rule 16 configuration check. Rule 16 records
> `max_turn_growth` as today (one integer): a resumed engine applies the cap at every use and the
> decay at its next turn start that measures growth (not at the turn start that wrote the record),
> so it forgets exactly as the previous engine would have. A record written by an engine without
> these settings does not resume (`settings_changed`). Headroom enters only the turn-start
> rotation decision and the window budget W (which an engine also sets once from the measured head
> after its first response, A8 (c)); the in-turn over-bound check never reads it, so rule 5 is
> unchanged: nothing rotates inside a tool loop, and an in-turn request over the bound is logged
> as today.

## What a live check would need

- One seat-sized Claude session with a deliberate large read (more than 100,000 tokens in one tool
  round), under the recommended rule. Check that later rotations are not `window_minimal`, that W
  recovers, and that recall still answers paged-only probes. The ceiling for the large turn itself
  is unchanged and should be stated to the owner.
