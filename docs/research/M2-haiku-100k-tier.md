# Haiku 5.5's 100K price tier: a proposal (B27, 2026-10-16)

**Proposal only, made at zero spend; no code is changed until intake decides.** It gives numbers only.

On Haiku 5.5 every rate is ×5 once a prompt exceeds 100,000 tokens, cache reads and writes included
([M0-cost-model.md:289](M0-cost-model.md), the Haiku 5.5 row, and [:305](M0-cost-model.md), the
long-prompt rule). In live run #3 (main `fbb1303`, bound 150,000), 7 of the 44 requests crossed that
line, from 101,390 to 109,868 tokens: the tool-loop requests of probe turns 1 and 2
([M2-usage-savings-run3.md](M2-usage-savings-run3.md)). This asks which settings would have kept them
under it, and what that would have cost or saved.

## Recommendation

**For Haiku 5.5, run with bound 125,000** (headroom floor 25,000 and `min_turn_room` 12,000
unchanged), as a Haiku-only setting.

In run #3's replay it removes every request over 100,000: 0 against 7, with a peak of 87,605 against
109,868. It cuts input tokens 21.0% and **modeled** cost 45.7%. Each probe turn keeps about as much
recall room as run #3 had.

The cost is 3 more rotations (9 against 6) and a retained window of 2 content turns instead of 3.
That is an accuracy risk, and only a live run can measure it (see Accuracy). The setting needs no
code: `contexto run --bound 125000` already exists. Making it the automatic default for Haiku is a
small lane A change (see Per model).

## Method (zero spend)

1. **Replay.** All 44 of run #3's original Claude Code requests are decoded from its session JSONL,
   with tool results merged as they were sent. They go in order through main's engine
   (`RewriteEngine`) under each candidate config, on a private scratch store with the run's page key.
2. **Request size.** Each request is priced at run #3's measured tokens, moved by the candidate's byte
   difference from the baseline replay at the same request, at 1.8274 bytes per token (B25's
   calibration). The engine is fed these as usage.
3. **Validation.** With run #3's own config, the replay reproduces the run exactly:
   - 3,725,391 input tokens;
   - rotations at the same requests (4, 7, 10, 13, 16, 27, all `min_turn_room`);
   - the same 7 requests over 100,000, peak 109,868.

   The request-27 rotation fired on a margin of 119 tokens (estimate 113,119 against a threshold of
   113,000), so the replay has to start from measured figures, not raw bytes.
4. **Cost.** Each request reads the same share of the previous prompt from cache as run #3 did at that
   request; a rotation in the candidate reads only the cached head (24,601 tokens, as measured). Prices
   are M0's Haiku 5.5 rates, with ×5 on every rate for a prompt over 100,000. This reproduces run #3's
   measured cost to within 1.0%: 0.2887 USD modeled against 0.2915.
5. **Held fixed** (a limit, see below): the model's behavior. That means the same turns, the same tool
   calls and the same tool results as run #3.

## Candidates

(1) Settings that keep requests at or under 100,000, with their effect on run #3 (**modeled** cost;
the tokens are run #3's measured tokens moved by the replayed bytes):

| Setting (others at default) | Requests over 100K | Peak | Rotations | Input tokens | USD | vs run #3 |
|---|---|---|---|---|---|---|
| Run #3: bound 150,000 | 7 | 109,868 | 6 | 3,725,391 | 0.2887 | |
| bound 140,000 | 8 | 106,400 | 6 | 3,684,225 | 0.2562 | −11.3% |
| bound 135,000 | 4 | 103,160 | 7 | 3,282,927 | 0.1942 | −32.7% |
| bound 130,000 | 2 | 109,771 | 8 | 3,036,045 | 0.2161 | −25.1% |
| bound 128,000 | 2 | 109,771 | 9 | 2,968,082 | 0.2189 | −24.2% |
| **bound 125,000** | **0** | **87,605** | **9** | **2,941,930** | **0.1569** | **−45.7%** |
| `min_turn_room` 25,000 | 8 | 106,400 | 6 | 3,684,225 | 0.2562 | −11.3% |
| `min_turn_room` 30,000 | 4 | 103,160 | 7 | 3,248,442 | 0.1928 | −33.2% |
| `min_turn_room` 35,000 | 0 | 87,605 | 9 | 2,941,930 | 0.1569 | −45.7% |
| headroom floor 35,000 | 8 | 106,400 | 6 | 3,684,225 | 0.2562 | −11.3% |
| headroom floor 40,000 | 4 | 103,160 | 7 | 3,282,927 | 0.1942 | −32.7% |

- **Rotation threshold.** A turn rotates when bound − estimate − headroom floor would be under
  `min_turn_room`. So the largest turn-start request is about bound − floor − room: 113,000 in run #3
  and 90,000 for both bound 125,000 and `min_turn_room` 35,000. A probe turn's tool loop then grows it
  by up to about 8,800 tokens in run #3 (largest per-turn growth), which keeps it under 100,000.
- **Partial steps can stay over.** At bounds of 130,000 and 128,000, the shifted rotation timing leaves
  probe turn 3's tool loop starting high, so 2 requests stay at 109,771. Only a threshold near 90,000
  removed every crossing.
- **Bound 125,000 against `min_turn_room` 35,000.** Their requests are identical in this replay, but
  their recall rooms differ:
  - **bound 125,000:** the room it publishes is 125,000 − input − 25,000 = 100,000 − input, so search
    output charged against the room also ends near 100,000.
  - **`min_turn_room` 35,000 at bound 150,000:** the room is 125,000 − input, so a probe turn's tool
    loop can grow past 100,000 if the model reads a lot.

  That is why bound 125,000 is the recommendation: it caps the loop too, not only the turn's first
  request.

(2) Cost in both directions (bound 125,000 against run #3, **modeled**):

| | Run #3 | bound 125,000 |
|---|---|---|
| Requests over 100K (priced ×5) | 7 | 0 |
| Rotations | 6 | 9 |
| Content-turn input tokens | 1,347,282 | 1,142,263 |
| Probe-turn input tokens | 2,378,109 | 1,799,667 |
| Cache read | 2,701,307 | 1,931,721 |
| Cache write | 1,023,996 | 1,010,121 |
| USD | 0.2887 | 0.1569 |

- **Against the setting:** the 3 extra rotations each re-write the window. Cache writes stay about
  the same (1.01M against 1.02M), because each rotation's window is smaller.
- **For it:** smaller windows send fewer tokens per request, and the 5× tier is gone.
- **Not modeled:** extra search rounds. The replay holds the model's tool calls fixed. If smaller
  windows make the model search more, each extra round re-sends about 50,000 to 90,000 tokens. At the
  observed rounds this does not change the sign: 3 extra rounds of about 90,000 tokens, read mostly
  from cache, cost about 0.003 USD at the base rate. Only a live run can count them.

## Accuracy risk

(3) Smaller windows mean more paging:

- **Retained content.** Run #3 held 3 content turns at its first probe turn (rotation at turn 16).
  Bound 125,000 holds 2 (rotation at turn 17). More of the planted evidence is paged, so more probes
  depend on search.
- **Recall room per probe turn**, from each turn's first request:

  | | pt1 | pt2 | pt3 | pt4 | pt5 | pt6 |
  |---|---|---|---|---|---|---|
  | Run #3 | 25,696 | 20,474 | 59,836 | 51,229 | 41,380 | 34,030 |
  | bound 125,000 | 22,959 | 17,737 | 57,077 | 48,470 | 38,621 | 31,271 |

  These are about 2,700 lower each, still above 12,000, so turn_room_low would not fire. Search
  replies have nearly the same room.
- **What M0 found.** Deep supersession chains are the probe type that fails at small contexts. Across
  M0's pilots at 12k to 15k context, all 4 misses in 18 depth-6 and depth-8 probes were chains, and
  every other type stayed at ceiling ([M0-harder-probes.md](M0-harder-probes.md), pilots 1 and 3).
  Chain depth 6 scored 5/8, with 3 stale answers. Bound 125,000's requests (42,723 to 87,605 tokens in
  the replay) are far larger than M0's 12k to 15k contexts. But they hold fewer turns, so a chain's newest link is more
  often paged and must come back through search.
- **Run #3's own evidence** points the same way. H1's misses were all stale chain or obligation
  answers (q_a_010, q_a_033, q_a_042). B19's in-view-first search and B21's latest lines are the
  defence: run #3 answered 51/54 with 6 rotations.
- **Only a live run can tell** whether 9 rotations and 2-turn windows keep that score: chain evidence
  in view against paged, the in-view hit count, the rounds per probe turn, and acceptance 2 against
  the baseline. A live run #4 on Haiku at bound 125,000 would measure all four. At the calibrated
  tool_rounds the approval table's worst case falls from 9,433,809 at bound 150,000 to 7,881,354 at
  bound 125,000 (H1 6,561,088 to 5,408,497), with no turn_room_low.

## Per model

(4) This can be Haiku-only without touching Sonnet or Opus:

- **Now, with no code.** `contexto run --bound 125000 … claude --model claude-haiku-5-5`. The bound is
  a launch option, and the launch event logs it (`config`). The harness's `run.py live` takes
  `--bound`.
- **Automatic, lane A** (named here, not edited). The launcher builds the engine's `Config` from
  the launch config (`launcher.py:180` `build_engine`, `Config(**config)` at :186). The request
  carries `model`, which the proxy records per request. Two places could choose a Haiku default:
  - **In the launcher:** when the user passes no `--bound` and Claude's `--model` names a Haiku model,
    default the bound to 125,000 and log the choice. This is the smallest change.
  - **In the engine:** `RewriteEngine._call` (`rewrite.py:1154`) could apply a per-model override
    (a `Config` map keyed by model prefix) at turn start, so a mid-session `/model` switch also
    switches the bound.

  Either way, Sonnet and Opus keep bound 150,000. Neither has the price step: Fable 5.1, Opus 5.5
  and Sonnet 5.5 bill a 900K prompt at the same rate as a 9K one
  ([M0-cost-model.md:305](M0-cost-model.md)).
- **How the acceptance harness would test it.**
  - `run.py plan` and `approval-table` take `--bound 125000`.
  - The stub dry run (`stubrun.py`) asserts that no request exceeds 100,000 at that bound.
  - A report-only detector would count requests over 100,000 on Haiku.
  - A live H1 plus baseline at bound 125,000 gives the accuracy answer.

## Limits

- **Measured vs modeled.** Measured: run #3's tokens, cache pattern and decisions at bound 150,000.
  Modeled: every candidate's request sizes (from replayed bytes at 1.8274 bytes per token), their
  cache split, and all USD.
- **Behavior held fixed.** The model's turns, tool calls and tool results are run #3's. With other
  windows the model might search differently, and search replies sized by a different room would
  differ. Request sizes inside tool loops are the least certain figures.
- **Rotation sensitivity.** Run #3's request-27 rotation fired on a 119-token margin. Small changes in
  estimates can move a rotation by a turn, as bounds 130,000 and 128,000 show.
- **One synthetic run, one model:** 40 KB pasted log per turn and Haiku 5.5's ×5 step. Other models
  and typical turn sizes will differ.
