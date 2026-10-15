# M0 quality versus working-context size: probe design

Status: phase 1 design (lane 2, packet L2d). Lane 1 runs it on `lab.subject`. Author:
build.builder-4. Date: 2026-10-15, regenerated for `lab.subject`'s measured head. Code:
`spikes/m0/fidelity/l2d.py`, tests in `spikes/m0/fidelity/tests/test_l2d.py`. Claims are tagged
**[measured]**, **[vendor-documented]**, or **[inferred]**. Nothing in this document has been run
against a model yet.

## Why this probe

**Owner principle** (intake, 2026-10-15 23:12Z):
- Compaction is a failure mode to avoid. Working context stays well below the compaction threshold
  at all times.
- A design succeeds only if it never compacts and its recall and task accuracy equal the
  full-context baseline on the fidelity probes.

This probe finds the working-context size where recall and instruction-following start to slip.
That size becomes the bound for the hypothesis window. The score at each checkpoint is also the
full-context baseline that phase 2 designs must match at that size.

## Design

### One growing session, two seat checkpoints

**Turn sequence:** `S0, O1, O2, G1, P1, G2, P2`.
- `S0` gives the standing rules.
- `O1` and `O2` are the opening: every deep plant for both checkpoints.
- Each `Gk` grows the context with large file reads.
- Each `Pk` probes checkpoint k.

**Checkpoint sizes.** `lab.subject`'s fixed head (system prompt, tools, OpenRig onboarding) is
81,808 tokens on its first call [measured, lane 1, 2026-10-15].
- P1 lands right after the opening, at about 91k. That is the earliest possible: the opening and
  the probe turn add about 9k over the head.
- P2 is as large as builder-1's gate allows, about 243k.
- A three-checkpoint plan (about 93k, 100k, 250k) measured 1,940,197 tokens in lane 1's read-only
  run, which is over the gate. So the plan drops to two checkpoints and keeps 24 probes per
  category. Smaller per-category counts would cost the precision the baseline needs.

### Plants

Each checkpoint has its own disjoint group of 96 plants: 24 in each of four categories.

| Category | Planted as | Probed as |
|---|---|---|
| flagged | "Please remember: the `<subject>` is `<value>`." | value |
| unflagged | a `cfg <key>=<value>` line inside a pasted log or a read file, never called out | value |
| superseded | "Decision: ..." then later "Change of plan: the `<subject>` is now `<value>`." | current value; the old value scores as **stale** |
| obligation | "Action item AI-nnn: ... Keep it open until it is done." and, for half, "Action item AI-nnn is done." | `open` or `closed` |

**Segments.** In every category, 16 of 24 plants are **deep** and 8 are **growth**.
- Deep plants sit in the shared opening.
- Growth plants sit in the segment just before their own checkpoint: user text of `Gk`, or lines in
  `Gk`'s files.
- Superseded growth plants are stated in the opening and revised in `Gk`.

**Matched positions.** The opening interleaves both groups and all categories round-robin, so each
group's deep plants sit at matched absolute positions. A test checks that the two groups' median
offsets are within 1,000 tokens of each other; measured, about 250. Each checkpoint therefore
probes equally early material at a larger context size.

**Instruction-following.**
- `S0` gives one standing rule per answers file: "Whenever you write a file named answers_k.txt, end
  every line of it with `;x9`". Each file gets a distinct seeded marker.
- The rule is never repeated, and each is used once.
- Compliance is the share of answer lines that end with the right marker.

### Contamination control

An answer written at a checkpoint re-enters context, so the design prevents leaks:

1. Each checkpoint probes only its own group.
2. Every value is unique, and every subject and config key is unique. No subject is a substring of
   another.
3. Probe ids (`q_<k>_<nnn>`) carry no content, and probe turns name only their own group's
   subjects. No probe turn contains a value.
4. The subject's replies are fixed (`OK`, `DONE`, `WRITTEN`).

**Verified** [measured, on the generated plan]:
- `contamination()` scans every seat and replay turn and every file. For each plant it removes the
  plant's own statements and question, then searches what is left for its subject, current value
  and superseded values. Obligations are checked by id only.
- It also checks that replay-only plants never appear in a seat turn, and that no seat plant
  appears in the replay's own opening and growth.
- The plan has 0 violations. `generate` exits non-zero on any violation.
- A test injects a value into a later file and confirms the scan reports it.
- Other tests check that:
  - statements occur once, in order, before their probe;
  - probe turns name only their own group;
  - rule markers appear only in their rules turn.

The scan cannot rule out the subject restating plants in its replies; the go/no-go list covers that.

### Growth

- `Gk` asks the subject to read its files with the Read tool, all in parallel in one message.
- Files are synthetic numeric logs of about 16k to 18k estimated tokens each, assumed to fit the
  Read tool's per-read limit [inferred].
- `G1` reads 1 file and `G2` reads 9.
- With parallel reads, a growth turn is two requests. Read one file per request, each read is its
  own request.

## Budget [inferred estimates on measured inputs]

**Method**
- Log and file text are counted at 0.55 tokens per character. E1/E2 measured that chars/4
  undercounts numeric text about 2.2x [measured].
- Each Read output line adds about 8 characters for line numbers [inferred]. Prose is counted at
  chars/4.
- The meter counts every category, cache reads included, so every request costs its full context.
- The head is 81,808 [measured, lane 1]. The CLI's side calls are not modeled.

| Request | Turn | Context | Cumulative |
|---|---|---|---|
| 1 | S0 | 81,873 | 81,883 |
| 2-3 | O1, O2 | 85,053; 86,406 | 253,362 |
| 4-5 | G1 (user message; after the read) | 86,807; 88,917 | 429,206 |
| 6-7 | **P1** (probe; after the Write) | **91,133**; 92,477 | 614,120 |
| 8-9 | G2 (user message; after 9 reads) | 92,922; 240,607 | 948,909 |
| 10-11 | **P2** (probe; after the Write) | **242,822**; 244,166 | 1,436,541 |

- **Planned total:** 1,436,541 tokens, with parallel reads. Read one file per request it would be
  2,771,406, which does not fit.
- **Builder-1's pre-turn gate:** cumulative before a turn plus 1.5 times that turn's cost must stay
  under 1,900,000. The worst value is 1,680,712, before P2.
- **Compaction bound:** the largest context is about 244k, far below the block point of about 977k
  lane 1 measured for a 1M window [measured, lane 1]. No compaction is expected.
- The per-request sheet is `budget.md` in the output directory.

## Scoring

```sh
cd spikes/m0
python3 -m fidelity.l2d score --answers-dir <dir> --truth ../../.contexto/spikes/l2d/truth.jsonl [--json out.json]
```

- **Answer files read:**
  - `answers_1.txt` and `answers_2.txt` from the seat;
  - `replay_RP0_preserved.txt`, `replay_RP0_stripped.txt`, `replay_RP1_preserved.txt` and
    `replay_RP1_stripped.txt` from the replay arm, whichever exist.
- **Matching:** exact match after normalization, with the trailing marker removed. A wrong answer
  equal to a superseded value is **stale**; empty or `unknown` is **unknown**.
- **Reported per checkpoint or arm:**
  - pooled score with a Wilson 95% interval;
  - instruction compliance with its own interval;
  - per category (correct, stale, unknown);
  - per segment;
  - per depth bucket, by estimated distance from plant to probe: under 10k, 10k to 50k, 50k to
    150k, 150k and over.

**What "equal to the full-context baseline" means in numbers.** Each checkpoint has 96 probes, 24
per category, plus 96 answer lines for compliance. At 96/96 the Wilson interval is 0.962 to 1.0,
and at 24/24 it is 0.86 to 1.0. These are the decision rules [inferred]:
- **Matches the baseline at checkpoint k:** the same probes at the same context size score within 2
  of the baseline pooled, within 1 in every category, with no more stale answers, and compliance
  within 2 lines. A difference of 1 to 2 should be confirmed with a second seed.
- **Quality has slipped at checkpoint k**, relative to the smallest size measured: 5 or more fewer
  correct, or a Wilson upper bound below the reference's lower bound. A category loss of 4 or more
  of 24, or any rise in stale answers, counts as a category-level slip.
- The replay's 40k checkpoint (RP0) is the smallest size measured, so it is the reference for the
  replay arm. P1 is the reference for the seat.

## Go/no-go list for builder-1

**Before the run**
1. Files are generated with the measured head and checkpoints (P1 91,133, P2 242,822). The plan
   reported 0 contamination violations. If the head changes by more than 5k, regenerate with
   `--head N`.
2. Fresh `lab.subject`, `claude-haiku-5-5`, default settings, base URL only. Confirm the CLI's
   window is 1M; if it is not, stop and tell builder-4.
3. Copy the 10 files from `files/` into the subject's scratch directory at the `path` values in
   `turns.jsonl` (`l2d/files/...`). Make sure `l2d/` holds no `answers_*.txt`.
4. Send each turn's `text` exactly, in `turns.jsonl` order, and wait for each reply.

**During the run**
1. **Short replies.** After `S0` and `O*` the reply should be `OK`, after `G*` `DONE`, after `P*`
   `WRITTEN`. A reply over about 100 output tokens may restate plants; note it and continue.
2. **Parallel reads.** `G1` has one file. `G2` should take two requests.
   - With parallel reads, `G2` plus `P2` cost about 822,000. Send `G2` only if the meter plus
     822,000 stays under 1,900,000.
   - Read one file per request, `G2` plus `P2` would cost about 2,157,000, which never fits. So if
     `G2`'s reads arrive one per request, stop sending once the meter plus twice the current
     context reaches 1,900,000. Then send `P2` at the size reached, and report its actual context.
3. **Probe isolation.** Before each `Pk`, move that checkpoint's `Gk` files out of the scratch
   directory. During `Pk` the subject must make exactly one Write call and no Read, Grep, Glob, or
   Bash call; any other tool call marks checkpoint k contaminated.
4. **Compaction.** If any compaction notice appears, or a request's input drops sharply, stop. The
   run is invalid for this probe.
5. **Cap.** Apply your pre-turn gate, cumulative plus 1.5 times the next turn's budget under
   1,900,000. If the gate blocks `G2`, send `P2` without it only if that probe fits; it then
   measures P1's size twice, so report it as such.

**After the run**
1. Copy each `l2d/answers_k.txt` to `.contexto/spikes/l2d/answers/` in builder-4's worktree (local,
   gitignored), and send me the path.
2. Send the per-request input sizes from the proxy summary, sizes only.

## Strip-thinking arm: direct-API replay (separate approval; not part of the lab run)

**What it tests.** PR #7's chosen strategy strips all earlier thinking at each epoch (policy doc,
section 7). This arm measures what that costs in accuracy, by comparing preserved and stripped
thinking on identical content.

**Why a replay.** It cannot run on `lab.subject`, which is pass-through only.

**Design** (`replay_turns.jsonl`):
- `claude-haiku-5-5` with adaptive thinking, called through the rig proxy with the credential
  helper at runtime.
- One request per turn, files inline as user text, and the model replies `OK` or `DONE`.
- Each probe point forks into separate requests that never enter the shared history, so no answer
  can reach a later probe.
- **Preserved** keeps every earlier thinking block byte for byte. **Stripped** removes all of them.

| Turn | What it holds | Context (est.) |
|---|---|---|
| RS | a standing rule with the replay's own marker, plus the opening for a third disjoint group (group 0: 96 plants, same categories and segments) | 3,790 |
| RG | group 0's growth statements and files, inline | 38,381 |
| **RP0** | group 0 probes, forked: preserved and stripped | **40,749** each |
| RF | plant-free filler standing in for the seat's 81,808-token head, then the seat's `S0`, `O1`, `O2`, and `G1` content verbatim (reply and Read instructions removed, files inline) | 89,484 |
| **RP1** | the seat's P1 questions, verbatim, answered as the lines that would go in `answers_1.txt`; forked preserved and stripped | **91,861** each |

**Geometry.** RP1 sits at the seat's P1 size, 91.9k against 91.1k, with group 1's plants at about the
same distance from the probe. The seat's head is system prompt and tools, which the replay replaces
with conversation filler. Group 2's opening plants ride along unprobed, as on the seat.

**Budget** [inferred]:

| Scope | Tokens |
|---|---|
| (1) RP0 in both arms + (2) RP1 preserved | 314,146 |
| (1) + (2) + (3) RP1 stripped | 408,851 |

- Both fit the 450,000 ceiling.
- (1) and (2) together are 4,639 tokens over the 309,507 approval. Shrinking RP0 to about 38k would
  bring them under. Manager-2 decides.
- Thinking output is assumed at about 1,500 tokens per probe and 200 per build turn; these are not
  yet measured.

**Gates.**
- The conformance gate is met: a history with all thinking stripped returned 200 [measured,
  2026-10-15].
- Run only after manager-2 says go.

## Codex variant (`labcodex.subject`)

Same files and plants. The turn texts and two limits differ [inferred unless tagged]:
- **Reading files.** Codex has no Read tool, so `Gk` must ask it to print each file with a shell
  command.
- **Tool-output cap.** Codex caps stored tool output (`tool_output_token_limit`,
  vendor-documented). A file over the cap is truncated, and any plant past the cut would score as a
  recall loss that is really truncation. Files must be smaller, which means more requests.
- **Compaction.** Codex auto-compacts at about 90% of a 272k window, about 245k [measured, lane 1,
  PR #9]. P2 at about 243k is at that edge, so a Codex run should keep P2 near 200k.
- **Writing answers.** Codex writes the answer file with its own file-editing tool or a shell
  command.

## Open questions

1. The Read tool's per-read size limit in this CLI version. If it is under 18k tokens, regenerate
   with smaller files.
2. The replay arm's scope: within 309,507 by shrinking RP0, or up to 408,851 with RP1 stripped.
3. A second seed is worth running only where a result lands within 1 to 2 probes of a decision rule.
