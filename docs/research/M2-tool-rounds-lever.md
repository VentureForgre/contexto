# M2: the tool-round cost lever (B26 proposal)

Zero-spend analysis of live acceptance run #3 (main fbb1303, session acc20261016T095818Z), for
intake's decision on whether to build a round-count lever. Proposal only: no code. Numbers come from
the run's `h1-turns.json` and Claude Code's session log, read in place. Each search was classified by
the probe and hop it serves; this document holds counts only, never query or page text.

## Where the probe-turn cost went

The H1 arm spent 3,748,084 tokens; the probe turns took 2,399,594 of that in 26 requests. Each
probe turn makes one or more search rounds and then one answer request, and every request re-sends
the whole window (65,000 to 110,000 tokens).

| Probe turn | Rounds | Searches per round | Multihop probes (hops) | Extra-round tokens | Turn tokens |
|---|---:|---|---|---:|---:|
| 1 | 4 | 6, 4, 2, 1 | 3, 3, 4 | 311,996 | 517,631 |
| 2 | 2 | 9, 1 | 3 | 110,366 | 326,795 |
| 3 | 1 | 14 | none | 0 | 141,673 |
| 4 | 4 | 9, 1, 1, 1 | 4 | 242,364 | 396,891 |
| 5 | 5 | 6, 2, 2, 1, 1 | 3, 4 | 351,672 | 527,235 |
| 6 | 4 | 14, 1, 1, 1 | 4 | 297,368 | 489,369 |

The 14 rounds after each turn's first cost 1,313,766 tokens: 54.7% of probe-turn spend and 35.1% of
H1. That is 93,840 per extra round on average. Of those tokens, 1,052,611 (80%) were cache reads.

## Why there are extra rounds

The model already batches. Round 1 of every turn held all the independent searches: chain,
paged-only, distractor, and obligation keys, and the first hop of every multihop probe. Rounds 2 and
later held only the next hops of multihop probes (hop 1, then hop 2, then hop 3). Each hop's key is
a value found by the previous hop, so it cannot be searched until that reply arrives. So:

- 13 of the 14 extra rounds are multihop dependencies. Within a turn the number of rounds equals
  the deepest multihop probe's hop count. Turn 2 is one short because a later hop was in view.
- 1 extra round is not a dependency: turn 5's fifth round, a single chain re-search, which cost
  89,514 tokens (2.4% of H1).

## The two proposed levers

**(1) Multi-key `contexto_search`** (several exact keys in one call). Claude Code already sends
parallel tool calls in one response, and that is how round 1 held 6 to 14 searches. A multi-key
call would save some tool_use and tool_result framing, but it adds no new batching: it cannot
search a key the model does not yet know.
- Best case, one round per turn: about 1,313,766 tokens. That is not reachable for multihop with
  exact search.
- Realistic case, removing only the non-dependency round: about 89,514 tokens per run.
- Risks: it is a contract change (the reply format, per-key room sharing in one reply, logging of
  per-key spans), and a single larger reply can spend more of the room at once.

**(2) Guidance to batch searches into one round** (preamble or tool description). The model
already does this for independent keys, so the realistic saving is the same 89,514 tokens, and only
if the guidance also stops re-searches.
- Risks: small. It is prompt text only, with no contract change. The guidance could push the model
  to search fewer keys, so answer quality must be watched.

**Recommendation: build neither.** Each saves about 2.4% of H1, and the round count is set by the
probe mix's dependency chains, not by unbatched searching.

## The lever that would matter: multihop dependency rounds

Collapsing the dependency hops is worth up to the 13 dependency rounds, about 1.22 million tokens
in run #3. It would need search to follow the chain itself. Example: a bounded follow option, where
each identifier named on a matched line is searched again, exact substring only, to a fixed depth
and capped in characters.
- This is still exact matching, not similarity, but it is a product and contract decision.
- It makes replies larger: four hops at about 350 tokens each is about 1,400 tokens per probe. That
  is well inside the rooms seen (20,474 at the smallest), but it adds lines a probe did not need.
- It also changes what search means: results for keys the model did not ask for.

Two ways to cost it before building:
- **Zero-spend first:** replay run #3's 54 probes through the real MCP server with a prototype
  follow option (local only). Measure the reply sizes, whether each multihop probe's hop lines are
  all returned in round 1, and how many unasked lines come back.
- **Then a small live experiment** of the 8 multihop probes in one probe turn, with and without the
  option. It must show fewer rounds (1 against up to 4), no loss in multihop or other answers, and
  the token saving.

The alternative is to accept the rounds and budget for them. That is already done:
`budget.Params.tool_rounds` is calibrated on this run (expected H1 3,880,890, 3.5% over run #3's
actual).
