# M0 quality versus working-context size: probe design

Status: phase 1 design (lane 2, packet L2d). Lane 1 runs it on `lab.subject`. Author:
build.builder-4. Date: 2026-10-15. Code: `spikes/m0/fidelity/l2d.py`, tests in
`spikes/m0/fidelity/tests/test_l2d.py`. Claims are tagged **[measured]**, **[vendor-documented]**,
or **[inferred]**. Nothing in this document has been run against a model yet.

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

### One growing session, three checkpoints

**Turn sequence:** `S0, O1..O3, G1, P1, G2, P2, G3, P3`.
- `S0` gives the standing rules.
- `O1` to `O3` are the opening: every deep plant for every checkpoint.
- Each `Gk` grows the context with large file reads.
- Each `Pk` probes checkpoint k.

**Checkpoints.** The defaults target about 30k, 100k and 250k tokens of context at the probe
request.
- If the measured fixed head plus the opening already exceeds 30k, checkpoint 1 sits right after the
  opening, relative to the head.
  - The opening plus the first probe turn add about 11k tokens: every deep plant for all three
    checkpoints, plus 96 questions.
  - With the provisional 46k head from lane 1, P1 therefore lands at 57,434, and that is the earliest
    possible. A P1 near 50k would need about 5 probes per category, too few for a precise baseline,
    so the design keeps 24.
- One session keeps the prefix cacheable. Every request costs its full context against the meter
  anyway.

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
- Growth plants sit in the segment just before their own checkpoint: user text of `Gk`, or lines
  inside `Gk`'s files.
- Superseded growth plants are stated first in the opening and revised in `Gk`. That tests whether
  a far-back stale value wins over a recent revision.

**Matched positions.** The opening interleaves all checkpoints and categories round-robin, so each
group's deep plants occupy matched absolute positions. A test checks that the median token offsets
of the three groups' deep plants are within 5% of each other. Each checkpoint therefore probes
equally early material at a larger context size.

**Instruction-following.**
- `S0` gives one standing rule per answers file: "Whenever you write a file named answers_k.txt, end
  every line of it with `;x9`". Each file gets a distinct seeded marker.
- The rule is never repeated. The probe turn only says to follow "any standing rule for that file
  given earlier".
- Each rule is used once, so checkpoint 1's answers cannot demonstrate checkpoint 2's marker.
- Compliance is the share of answer lines ending with the right marker.

### Contamination control

An answer written at checkpoint k enters context as the Write call's input, so it could hand a
later checkpoint its answers. The design prevents that in four ways:

1. Each checkpoint probes only its own group; groups are disjoint.
2. Every value is unique in the plan, and every subject and config key is unique. No subject is a
   substring of another.
3. Probe ids (`q_<k>_<nnn>`) carry no content. The probe turn names only its own group's subjects,
   and no probe turn contains any value.
4. Before a probe, nothing restates a plant. The subject's replies are fixed (`OK`, `DONE`,
   `WRITTEN`).

**How this was verified** [measured, on the generated plan]:
- `contamination()` scans every turn text and every file. For each plant it removes the plant's own
  statements and its own question, then searches what is left for the plant's subject, current
  value and superseded values, on word boundaries.
- Any hit is a violation. Obligations are checked by id only, since `open` and `closed` are ordinary
  words.
- The default plan has 0 violations. `generate` writes the result to `contamination.json` and exits
  non-zero if any are found.
- A test injects one value into a later file and confirms the scan reports it.
- Further tests check that:
  - each statement occurs exactly once, in order;
  - every statement precedes its probe;
  - no probe turn names another group's subjects;
  - the rule markers appear only in `S0`.

One leak this cannot rule out: the subject restating plants in its own replies. The go/no-go list
covers it with a reply-size check.

### Growth

- `Gk` asks the subject to read its files with the Read tool, all in parallel in one message.
- Files are synthetic numeric logs of about 16k to 18k estimated tokens each, under a size assumed
  to fit the Read tool's per-read limit [inferred].
- With parallel reads a growth turn costs two requests: the user message, and the request after all
  results. If the subject reads one file per request, each read is its own request and the cost
  roughly doubles.
- With a 46k head, `G1` reads 1 file, `G2` 3, and `G3` 9.

## Budget [inferred estimates; measured inputs where stated]

**Method**
- Log and file text are counted at 0.55 tokens per character. E1/E2 measured that chars/4
  undercounts numeric text about 2.2x [measured].
- Each Read output line adds about 8 characters for line numbers [inferred]. Prose is counted at
  chars/4.
- The meter counts every category, cache reads included, so each request costs its full context.
  Outputs are small: answers are about 14 tokens per line.
- The CLI's own side calls are not modeled; the margin covers them.
- The fixed head is an input. Lane 1 is measuring it on `lab.subject`, and the plan is regenerated
  with `--head N`.

| Fixed head | P1 | P2 | P3 | Requests | Total, parallel reads | Total, sequential reads |
|---|---|---|---|---|---|---|
| 25,000 | 36,434 | 97,321 | 243,300 | 16 | 1,418,923 (71% of 2M) | 2,980,510 (149%) |
| 40,000 | 51,434 | 98,049 | 243,114 | 16 | 1,556,212 (78%) | 3,069,014 (153%) |
| 46,000 (provisional, lane 1; current generated plan) | 57,434 | 98,305 | 243,070 | 16 | 1,611,104 (81%) | 3,130,995 (157%) |

- The full per-request sheet is `budget.md` in the output directory.
- **Compaction bound:** the largest context is about 245k, far below the block point of about 977k
  that lane 1 measured for a 1M window. No compaction is expected.
- Parallel reads fit with a 19% to 29% margin. Sequential reads do not fit, which is why the
  go/no-go list has a stop rule for them.

## Scoring

```sh
cd spikes/m0
python3 -m fidelity.l2d score --answers-dir <dir with answers_1.txt ...> \
    --truth ../../.contexto/spikes/l2d/truth.jsonl [--json out.json]
```

**Exact match after normalization**, as in L2b:
- The trailing marker is removed before matching.
- A wrong answer equal to a superseded value is **stale**.
- An empty answer or `unknown` is **unknown**.

**Reports per checkpoint**
- pooled score with a Wilson 95% interval;
- instruction compliance with its own interval;
- per category: correct, stale, unknown;
- per segment (deep, growth);
- per depth bucket, by estimated token distance from plant to probe: under 10k, 10k to 50k, 50k
  to 150k, 150k and over.

**What "equal to the full-context baseline" means in numbers**
- Each checkpoint has 96 probes: 24 per category, plus 96 answer lines for compliance.
- At 96/96 the Wilson interval is 0.962 to 1.0, and at 24/24 it is 0.86 to 1.0 [measured, by the
  scorer's formula].
- One run cannot separate differences of one or two probes from noise. These are the decision
  rules [inferred]:
  - **Matches the baseline at checkpoint k**: the same 96 probes at the same context size score
    within 2 of the baseline pooled, within 1 of it in every category, no more stale answers than
    the baseline, and compliance within 2 lines. A difference of 1 to 2 should be confirmed with a
    second seed.
  - **Quality has slipped at checkpoint k**, relative to checkpoint 1 as the smallest-size
    reference: 5 or more fewer correct (about 5 points), or a Wilson upper bound below checkpoint
    1's lower bound. Either a category loss of 4 or more of 24, or any rise in stale answers,
    counts as a category-level slip.

## Go/no-go list for builder-1

**Before the run**
1. Regenerate with the measured head:
   `python3 -m fidelity.l2d generate --out .../.contexto/spikes/l2d --head <first request context>`.
   It must report 0 contamination violations.
2. Fresh `lab.subject`, `claude-haiku-5-5`, default settings, base URL only. Note the context
   window the CLI shows. If it is not 1M, stop and tell builder-4: the compaction bound changes.
3. Copy `files/*` into the subject's scratch directory at the `path` values in `turns.jsonl`
   (`l2d/files/...`). Make sure `l2d/` holds no `answers_*.txt`.
4. Send each turn's `text` exactly, in `turns.jsonl` order, and wait for each reply.

**During the run**
1. **Short replies.** After `S0` and `O*` the reply should be `OK`, after `G*` `DONE`, and after
   `P*` `WRITTEN`. If a reply has more than about 100 output tokens, the subject may have restated
   plants. Note it and continue; I treat that checkpoint as possibly contaminated.
2. **Parallel reads.** `G2` should take about two requests.
   - With parallel reads, `G3` plus `P3` cost about 830,000. Send `G3` only if the meter plus
     830,000 stays under 1,900,000.
   - Read one file per request, `G3` alone would cost about 2,190,000, which never fits. So if `G3`'s
     reads arrive one per request, stop sending further turns once the meter plus twice the current
     context reaches 1,900,000. Then send `P3` at the size reached, and report the actual P3 context.
3. **Probe isolation.** Before each `Pk`, move that checkpoint's `Gk` files out of the scratch
   directory, since they are no longer needed. During `Pk` the subject must make exactly one Write
   call and no Read, Grep, Glob, or Bash call. Any other tool call marks checkpoint k contaminated;
   keep the file anyway.
4. **Compaction.** If any compaction notice appears, or a request's input drops sharply from the
   previous one, stop. The run is invalid for this probe.
5. **Cap.** Stop growth if the meter reaches 1,900,000. If the next probe turn costs about twice the
   current context and that still fits under 2,000,000, send it, because a probe at the reached
   size is still a result.

**After the run**
1. Copy each `l2d/answers_k.txt` to `.contexto/spikes/l2d/answers/` in builder-4's worktree (local
   and gitignored), and send me the path.
2. Send the per-request input sizes from the proxy summary, sizes only, so I can check the budget
   estimate against measurement.

## Strip-thinking arm (design only; not part of the lab run)

**What it tests.** PR #7's B3 finding is that on 5.x models, editing history before preserved
thinking blocks invalidates them. One valid strategy is to strip earlier thinking at each epoch.
This arm measures what that strategy costs in accuracy.

**Why it cannot run on `lab.subject`.** The lab seat is pass-through only, so it can neither strip
nor keep thinking on demand.

**Design: a direct-API replay of the same session content**
- `claude-haiku-5-5` with extended thinking on, called through the rig proxy, using the credential
  helper at runtime.
- One request per turn, with the same turn texts. File contents go inline as user text, because the
  replay makes no tool calls, and the model replies `OK` or `DONE`.
- At each checkpoint the shared history forks into two probe requests with identical content:
  - **preserved**: every earlier thinking block kept byte-for-byte;
  - **stripped**: every earlier thinking block removed, as the policy would do at an epoch.
- Answers come back in the response text in the same `q_id: answer` format and are scored by the
  same scorer.
- Accuracy is compared against the replay's own preserved arm and against the lab baseline at that
  checkpoint.

**Budget**, computed by `replay_requests()` from the same plan [inferred]:

| Scope | Requests | Tokens, plan for a 46k head | Tokens, plan for a 25k head |
|---|---|---|---|
| all three checkpoints | 13 | 808,527 | 932,265 |
| checkpoints 1 and 2 only | 10 | 226,700 | 286,748 |

The replay has its own short head, so its cost depends only on the plan's content and file sizes,
which shift slightly with the lab head the plan was generated for.

- Neither fits in the 2,000,000 lab budget alongside the 1.42M to 1.61M lab run, so the arm needs
  its own approval. Intake approved checkpoints 1 and 2 at up to 309,507 tokens.
- The B3 conformance probe's gate is met: a history with all thinking stripped returned 200
  [measured, 2026-10-15].

## Codex variant (`labcodex.subject`)

The same files and plants work. The turn texts and two limits differ [inferred unless tagged]:

- **Reading files.** Codex has no Read tool, so `Gk` must ask it to print each file with a shell
  command.
- **Tool-output cap.** Codex caps each stored tool output (`tool_output_token_limit`;
  vendor-documented, default not verified). A file larger than the cap is truncated in context, and
  any unflagged plant past the cut would score as a recall loss that is really truncation. Files
  must be sized under Codex's cap: more, smaller files, which means more requests.
- **Compaction.** Codex auto-compacts at about 90% of a 272k window, about 245k (lane 1, PR #9).
  `P3` at about 243k is at that edge, so the Codex run should target about 30k, 100k and 200k.
- **Writing answers.** Answers are written with Codex's own file-editing tool or a shell command;
  the probe turn's tool rule changes accordingly.

## Open questions

1. The measured fixed head and window of `lab.subject` (pending from lane 1). Then regenerate.
2. The Read tool's per-read size limit in this CLI version. If it is under 18k tokens, regenerate
   with smaller files, which raises the request count.
3. Whether one run per checkpoint is enough. A second seed would cost about 1.5M more, so it needs
   approval; consider it only where the first run lands within 1 to 2 probes of a decision rule.
4. The strip-thinking arm's approval and the B3 conformance result.
