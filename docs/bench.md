# The benchmark: `contexto bench`

[Contexto](../README.md) / [Documentation](README.md)

`contexto bench` runs one generated session three ways and scores each way the same: through
Contexto, as full context sent straight to the model, and as plain Claude Code with its auto-compact
window set so that compaction fires near the bound (the bound plus 33,000 tokens). At its defaults,
Claude Code on these models, whose windows are 1,000,000 tokens, would compact near 967,000 tokens
and would not compact at all in this session. The question it answers: on a session whose history
grows several times past the bound, does Contexto answer questions about early details as well as
full context does, and how does it compare with plain Claude Code compacting at the same bound? It
also reports input tokens and cost for each arm. The session is generated log text with facts
planted in it, not real coding work, so the benchmark says nothing about real sessions, and nothing
about plain Claude Code at its default compaction threshold; its limits are under
[What the benchmark does not show](#what-the-benchmark-does-not-show).

The benchmark exists because the earlier evidence could not settle that question. Live run #4
([research/M2-acceptance-run4.md](research/M2-acceptance-run4.md)) answered 52 of 54 probes through
Contexto against 47 for full context. That is within noise: the exact McNemar p value is 0.18, the
misses are disjoint (2 and 7), and every run used one seed. It supports "no worse than full
context", not "better". Plain Claude Code with its own compaction was never run, and the scoring
could not be repeated by anyone outside. The decision record is D66 in
[DECISIONS.md](DECISIONS.md).

## Quick start

The benchmark ships in the package, with no runtime dependency beyond Python's standard library. It
is on `main` and not yet in a release: v0.5.24, the latest release, does not have it, and releases
are cut by hand and batched ([RELEASING.md](../RELEASING.md), D67). From a release that has it, run
the commands as shown. From a checkout, put `uv run` in front of each one (`uv run contexto bench
--replay`). A live run refuses an `--out` inside a git work tree, so that results are not committed
before anyone has looked at them: from a checkout, give it a directory outside, such as `--out
../bench-haiku`, or pass `--allow-git-out` to keep it inside on purpose.

With no key, no `claude` and no network, see what can be shown without a model:

```sh
contexto bench --replay --out bench-replay
```

It checks that every request of a pinned synthetic session stays under the bound, by sizes
estimated from bytes, and which planted facts search can reach. It cannot show answer accuracy,
cache reads or real token counts
([what the replay shows](#what-the-replay-shows-and-what-it-cannot)).

With no key, see what a live run would cost before you start one:

```sh
contexto bench --dry-run --model claude-haiku-5-5 --seeds 10 --repeats 3
```

A live run calls the model with your own key and is paid by whoever runs it, at their account's
prices. The run below is estimated at about $14 expected and about $456 in the plan's worst case
(about $602 with `--cache-ttl 1h`), from the dry run's plan priced at the shipped price table ([the
minimum run](#the-minimum-run-that-makes-the-claim-testable) says how). Those are estimates, not a
ceiling: the ceiling is `--budget-tokens`, and the run refuses to start without one at or above the
dry run's worst case in tokens:

```sh
export ANTHROPIC_API_KEY=...        # or --key-helper COMMAND, or --settings FILE
contexto bench --model claude-haiku-5-5 --seeds 10 --repeats 3 \
  --budget-tokens 598290000 --out bench-haiku
```

The run writes `results.md` and two charts into `bench-haiku/` when it ends. `contexto bench
--report bench-haiku` makes them again from the records.

## Modes

`contexto bench` with no mode option is the live run. Each mode option does something else
instead, and at most one may be given. A mode's own options are refused without that mode, so a
replay option can never start a live run by mistake.

| Command | Calls a model | Needs `claude` | What it does |
|---|---|---|---|
| `contexto bench --script-only --out DIR` | no | no | writes each seed's generated script and checks it |
| `contexto bench --replay [--out DIR]` | no | no | replays a pinned synthetic session through the real proxy |
| `contexto bench --dry-run --model M ...` | no | no | prints the live run's plan, token estimate and budget figure |
| `contexto bench --model M --budget-tokens T --out DIR ...` | yes | yes | the live three-arm run |
| `contexto bench --report DIR [DIR ...]` | no | no | rebuilds `results.json`, `results.md` and the charts from records |
| `contexto bench --chart RESULTS` | no | no | draws the charts from one `results.json` |

Options every mode shares: `--seed BASE` (default 20261016, the seed of the published runs),
`--seeds N` (BASE, BASE + 1, and so on; default 1), `--repeats R` (run the first seed R times, for
run-to-run noise; default 1), `--bound TOKENS` (default 150,000), `--protocol d27|v2` (default
`v2`), `--turn-kbytes KB` (the size of a content turn, default 40) and `--out DIR`.

### `--script-only`

```sh
contexto bench --script-only --out scripts-check
contexto bench --script-only --protocol d27 --seeds 3 --out scripts-check
```

Writes `scripts/seed-<n>.json` for each seed and prints its turns, bytes, token estimate, probes by
class with their mean lexical overlap, and the generator's five self-checks (`restatements`,
`key_free`, `service_alone`, `placement`, `unplanted`). It exits 1 if a check fails. Measured for
seed 20261016 at bound 150,000: protocol `v2` has 34 turns (26 content, 8 probe), 844,966 bytes and
467,012 estimated tokens (3.09 times the bound), SHA-256 `469a5465...`; protocol `d27` has 24 turns
(18 content, 6 probe), 720,082 bytes and 398,376 estimated tokens (2.64 times the bound), SHA-256
`81f359d5...`.

### `--replay`

```sh
contexto bench --replay
contexto bench --replay --out bench-replay
```

Keyless. It regenerates a pinned synthetic session from its seed, checks every turn against the
SHA-256 pinned in the shipped trace, and sends the session through the real proxy (started as
`contexto run` starts it) to a fake upstream on 127.0.0.1 that replies from a script. It exits 0
when the bound held, the session log has no failure event and every measurement equals the trace;
1 otherwise, with the differences. What it shows and cannot show is under
[What the replay shows](#what-the-replay-shows-and-what-it-cannot). It took 3.0 seconds on a
loaded Mac.

Replay options: `--replay-turns N` replays a session of another length (it then says it is not the
pinned trace), `--dump-trace DIR` writes the regenerated session's texts without replaying, and
`--record-trace FILE` (with `--force`) is for maintainers who re-record the shipped trace after a
proxy change.

### `--dry-run`

```sh
contexto bench --dry-run --model claude-haiku-5-5 --seeds 10 --repeats 3
contexto bench --dry-run --model claude-opus-5-5 --seeds 5
```

Reads no key and makes no call. It prints, per arm and in total, the units, turns, requests and
tokens by class in an expected case and a worst case, the plain arm's compaction setting, the model
behind the figures with what in it is assumed, and the `--budget-tokens` figure to pass (the worst
case rounded up to 10,000). Given `--out` of an earlier run, it says what that directory holds and
plans only the units still to run; it changes no file.

### The live run

```sh
contexto bench --model claude-haiku-5-5 --seeds 10 --repeats 3 \
  --budget-tokens 598290000 --out bench-haiku
```

Needs `claude` on `PATH`, `--model`, `--budget-tokens` and `--out`. The key comes from one of three
places, never from the command line: `ANTHROPIC_API_KEY`; `--key-helper COMMAND`, run with
`/bin/sh -c` when a key is needed, as Claude Code runs an `apiKeyHelper`; or `--settings FILE`, a
Claude Code settings file whose `apiKeyHelper` prints the key. `--upstream URL` sets the model API
(default `$ANTHROPIC_BASE_URL` when set, else `https://api.anthropic.com`).

Other options: `--arms LIST` (default `contexto,full,plain`), `--cache-ttl 5m|1h` (default `5m`),
`--turn-timeout SECONDS` (default 1,800), `--max-turn-requests N` (default 60),
`--max-turn-failures N` (default 200), `--max-unit-tokens T` (default twice the unit's planned worst
case), `--context-window TOKENS` (for the full arm's fit check), `--work DIR` and `--keep-work`
(where each unit's Claude Code and Contexto session files go; removed after each unit by default,
and never inside a git work tree).

`--out` is refused inside a git work tree unless `--allow-git-out` is given, so results are not
committed by accident; from a checkout, give an `--out` path outside it. One run at a time holds a
lock on the output directory. A run is resumable: the same command with the same `--out` keeps the
units the manifest records `complete` and runs the rest again; a different model, bound, protocol,
turn size or first seed for that directory is refused. Every attempt's requests stay in the
records, because they were billed. A run killed in the middle of writing a record can leave a last
line with no newline. On resume, a whole record only gets its newline back; a torn one is set aside
in `<file>.torn-<n>` and cut from the file, so the next record starts a line of its own, and the
manifest's `torn_lines` says what was done. A request whose record was torn is not counted in the
spend, since its usage cannot be read.

A unit ends `complete`, `not_run` (the full arm's request does not fit the model's window) or
`stopped` with the reason, among them: `desync` (Claude Code printed a `result` that answers no
turn), `runaway` (a cap under [Spend guards](#spend-guards) was passed), `api_error` (the upstream
answered none of a turn's requests, or failed more often than `--max-turn-failures` allows),
`unmetered` (a turn sent no request through the meter), `timeout`, `budget` and `auth`. A stopped
unit writes no probe lines; the same command with the same `--out` runs it again.

### `--report`

```sh
contexto bench --report bench-haiku
contexto bench --report bench-haiku bench-haiku-more --out bench-combined
contexto bench --report bench-haiku --prices my-prices.json
```

Reads the records strictly (a damaged line is an error naming the file, line and field) and writes
`results.json`, `results.md` and `charts/` into the directory, or into `--out`; several directories
need `--out`. Either every file is written or the earlier report is left as it was. `--prices FILE`
replaces the shipped price table.

### `--chart`

```sh
contexto bench --chart bench-haiku/results.json
contexto bench --chart bench-replay/results.json --out charts-only
```

Draws `charts/accuracy.svg` (accuracy by arm with Wilson 95% intervals) and
`charts/request-size.svg` (request size against history length) beside the results file, or in
`DIR/charts/`. The SVG is written by the standard library, with no plotting dependency, and is the
same bytes for the same input. For a replay it draws only the request-size chart and says why there
is no accuracy chart.

## The three arms

Each seed gives one generated script, and every arm runs that same script. A unit is one arm on one
seed (and, for the first seed, one repeat). For each seed the arm order is rotated by one.

- **`contexto`**: `contexto run --bound B claude -p --input-format stream-json --output-format
  stream-json --verbose --model M`, one long-lived Claude Code process for the whole session.
- **`full`**: direct Messages API requests built from the script alone, never from another arm's
  transcript, so a reader can rebuild each request from `scripts/seed-<n>.json`. Content turns carry
  scripted assistant replies (`OK`, or the right `DECISION` line); the tool-loop turn carries
  scripted file reads. One request per probe turn: request k carries probe turns 1 to k-1 with this
  arm's own answers to them, then probe turn k (D27's symmetric form). It answers with
  `tool_choice: none`. Its exact input size is asked from `count_tokens` first; a request that does
  not fit the model's window makes the unit `not_run` with the reason.
- **`plain`**: the same headless Claude Code as the `contexto` arm with no Contexto, and its
  auto-compact window set so that compaction fires near the bound, at the bound plus 33,000 tokens
  ([below](#the-plain-arms-compaction)). At its defaults, Claude Code on these models
  (1,000,000-token windows) would compact near 967,000 tokens and would not compact at all in this
  session; the arm moves the trigger to the bound so that both Claude Code arms work under the same
  limit.

Every way the arms differ is written into the manifest's `arm_differences`. In short:

- `contexto` and `plain` run the same `claude` with the same arguments, a fresh isolated
  configuration directory with the same settings file, the same environment, turn texts and files.
- Only `contexto`: `contexto run` adds its `--settings` file (the proxy's base URL, permission rules
  for Contexto's five tools, the gateway hint headers) and its `--mcp-config` file, so its requests
  carry Contexto's tool definitions and server instructions. It also runs `claude --version` once.
  Its auto-compaction is left at Claude Code's default.
- Only `plain`: `CLAUDE_CODE_AUTO_COMPACT_WINDOW` is set, and its base URL is the meter.
- Both Claude Code arms have a base URL on 127.0.0.1. Claude Code behaves differently when its base
  URL is not the vendor's host (read in the 2.1.322 bundle), in both arms alike; a plain session
  sent straight to the vendor would not.
- Both Claude Code arms run with `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`. Without it, a subagent
  the model started in the background reported back with a run and a `result` of its own, and every
  later reply was scored against the wrong turn: in one stub test, 0 of 72 answers counted while the
  unit said `complete`. On Claude Code 2.1.322 the variable removes the `run_in_background`
  parameter from the Agent and Bash tool definitions, and with it about 771 characters of their
  descriptions (405 of Agent's, 366 of Bash's), in both arms alike; so both differ from plain Claude
  Code's default, in which subagents run in the background. It does not cover the Monitor tool or
  MCP background tasks. These sessions are not given the Monitor tool, and Contexto's MCP server
  declares no tasks, but a `result` from either, or any other `result` that answers no turn, stops
  the unit as `desync`.
- Every arm runs under the same runaway caps.
- `full` is not Claude Code: a one-line system prompt, one declared tool with `tool_choice: none`
  when the script has a tool-loop turn, no thinking parameter, no beta header, `max_tokens` 8,192.
  It answers each probe turn in one request with no tools, where the Claude Code arms may search.
  Its decision replies and file reads are scripted and always right, so its `concluded` and
  `toolloop` probes test recall only.

Every billed request of every arm goes through one pass-through meter on 127.0.0.1, which records
counts only: tokens by class from the response, the status, the turn, and main or side request.

## What the benchmark does not show

- Real coding tasks. The session is generated log text with planted facts, not recorded work.
- A real user's session shape: its turn sizes, tool use and the way facts recur.
- Plain Claude Code at its default compaction threshold. On the 1,000,000-token windows of the
  models priced here, Claude Code compacts near 967,000 tokens and would not compact at all in a
  session of this length (467,012 estimated tokens at the default settings). The `plain` arm is
  set to compact near the bound (the bound plus 33,000) so that both Claude Code arms work under the
  same bound; that is a choice, not Claude Code's default.
- Other CLIs: Codex is not an arm.
- Models other than the ones run, and Claude Code versions other than the one run.
- Answers judged by a model. Scoring is exact match, which can count a right answer in another form
  as wrong, in every arm alike.
- With fewer than 6 seeds, which way two arms differ.
- From a replay: answer accuracy, cache reads and real token counts
  ([what the replay shows](#what-the-replay-shows-and-what-it-cannot)).

## Protocols and probe classes

**`d27`** is the 54-probe protocol of live runs #3 and #4. Its turn texts and probe fields are
byte-identical to `spikes/m2/acceptance/run.py::build_script` for any seed, bound and turn size
(compared on 216 generated scripts in one check and 280 in another while #255 was built and
reviewed). Use it for continuity with those runs:
`--protocol d27`. Every one of its questions names its subject's key, which suits a word search.
Its classes: `paged_only` (4), `multihop` (8), `chain` (30), `obligations` (6) and `distractor` (6).

**`v2`** (the default) keeps every `d27` turn and probe unchanged and adds 18 probes in three
classes whose questions contain none of their plant's subject strings, 72 probes in all. Every new
plant has at least `bound` tokens of later content after it, so it is paged out before it is asked.

- **`paraphrase`** (6): facts planted as `cfg <service>.<attribute>=<value>` lines. The question
  names the service in words ("the billing core service") and describes the attribute in other
  words. Each asked service has 3 decoy `cfg` lines for other attributes, so a search by the
  service name shows 4 candidate lines and only reading the paraphrase picks the right one. A decoy
  answer counts as stale.
- **`concluded`** (6): the fact exists only as the assistant's own reply `DECISION <label>:
  <value>` to a short decision turn (three candidates, a number each, and a rule). The question
  describes what was decided in other words and names neither the label nor a candidate.
- **`toolloop`** (6): facts in 10 generated files (about 8,000 bytes each) that one turn reads one
  at a time with the file-read tool. Same decoy rule as `paraphrase`.

Each probe records its class and its lexical overlap: the number of distinct words the question
shares with its plant. Measured mean overlap for seed 20261016, in words: `paged_only` 4.00,
`multihop` 6.00, `chain` 4.00, `obligations` 4.33, `distractor` 4.00, `paraphrase` 2.00 (the
service's own words), `concluded` 0.00 and `toolloop` 2.00.

What the harder classes test, measured with the store's real search on generated pages (4,560
probes per class, measured during the review of #255):

| Query | Class | Plant page first | In the top 3 |
|---|---|---|---|
| service name, hyphenated (`relay-mesh`) | paraphrase | 50.0% | 100% |
| service name as two words (`relay mesh`) | paraphrase | 16.4% | 52.1% |
| service name, hyphenated | toolloop | 96.3% | 100% |
| service name as two words | toolloop | 79.5% | 99.5% |
| every word of the paraphrase | both | 0 | 0 |
| the key, for comparison | paged_only | 100% | 100% |

With the hyphenated name, the plant's page also showed at least 2 decoy lines every time. So
`paraphrase` and `toolloop` measure whether the model picks the right line among the 3 or 4 it is
shown, not whether search finds the page; `paged_only` is the keyed control of the same age. A
`concluded` question's words found no decision turn as one query (0 of 4,560).

## Scoring

Exact match after normalisation against the generator's answers, with no model judge. A `value`
answer is right when it equals the expected answer after normalisation (case folded; outer
whitespace, quotes, backticks, markdown emphasis `*` and `_`, and the punctuation `.` `,` `;` `:`
`!` trimmed; inner whitespace collapsed); a `set`
answer when its ids are the expected ids in any order. A wrong `value` answer that equals an older
value of the same fact, or a planted decoy, is also counted stale.

Three deliberate differences from the spike's scoring (`spikes/m0/fidelity/l2f.py::score`, with
`normalize` and `parse_answers` from `spikes/m0/fidelity/scoring.py`), applied to every arm alike:

1. A probe with no answer line, or an empty answer, is never correct. The spike read a missing
   answer to a `set` probe as the empty set, so a `set` probe whose answer is `none` scored correct
   when the arm said nothing.
2. More answer lines written in markdown are read: a numbered item or a `+` item, and `**`, `__` or
   backticks around the id, the separator or the answer (`**q_a_01**: x`, `1. q_a_01: x`,
   `` - `q_a_01`: x ``). The spike read a plain `-` or `*` item (`- q_a_01: x`) but dropped these,
   so an answer Claude Code wrote in them counted as no answer.
3. A line in a fenced code block is read, but an answer for the same id on a line outside any fence
   wins over it wherever the two stand, so a note or file the reply quotes in a fence after its
   answers cannot override them. A reply that writes all its answers in a fence is still scored.
   The spike read fenced lines like any other.

Every other line the spike read is read the same, and a line the spike did not read stays unread:
as in the spike, a line in a blockquote (`> q_a_01: x`) is never read. Table rows and headings are
not read.

For `concluded` probes the report also counts how many decision replies were right in each arm, so
a failed recall can be told apart from a wrong conclusion.

## Statistics and the verdict rule

Probes inside one session are not independent, so the report gives two views.

**Pooled across seeds.** Per-arm accuracy with a Wilson 95% interval. For each pair of arms on the
same probes: the 2x2 table, the difference in accuracy with a 95% interval by Tango's score method,
and the exact two-sided McNemar p value. A pooled difference is `significant` only when the interval
excludes zero and p < 0.05; the two can disagree (4 probes right only in one arm and 0 only in the
other, of 72, give an interval that excludes zero and p = 0.125).

**Seeds as the unit.** Each seed's difference between the two arms is one observation. The p value
is an exact sign-flip permutation test over every sign pattern (Monte Carlo above 20 seeds). The
interval is Student's t on the per-seed differences, given only from 3 seeds and when the seeds do
not all have the same difference.

**The verdict rule.** A direction between two arms is named only when three things hold: the pooled
difference is significant, the seed-level sign-flip p is below 0.05, and both point the same way.
Otherwise the report says no difference was shown, with each reason that applies.

- **Six seeds is the floor.** The smallest two-sided sign-flip p value with n seeds is 2 / 2^n:
  0.0625 at 5 seeds. With fewer than 6 seeds no direction can be named. A seed whose difference is
  exactly 0 carries no sign, so it is the seeds with a non-zero difference that must number 6 or
  more; the report says when this applies.
- **The interval over seeds does not decide.** Its coverage was simulated over 4,000 benchmarks of
  72 probes from two discordance rates (the share of probes right only in the first arm, and only
  in the second). With the rates fixed across seeds at 0.10 and 0.04, 0.13 and 0.04, or 0.06
  and 0.06, it covered 94.7% to 95.7% at 3 to 20 seeds; where one arm almost never wins alone (0.03
  and 0), 92.1% at 3 seeds. When the rates vary between seeds and one arm rarely wins alone, it
  covers less: at 0.03 and 0, 75.6%, 82.9%, 86.6% and 89.7% at 3, 5, 10 and 20 seeds. It assumes
  roughly symmetric per-seed differences.
- **Run-to-run noise** comes from repeats of the first seed: each arm's accuracy range over the
  repeats, the probes that flipped, and the repeated seed's own difference between two arms in each
  repeat. It is reported beside each pair and is not part of the rule.

## Tokens and cost

The meter records each request's input in three classes (uncached, cache write, cache read) and its
output. The report gives them per arm, with the largest request, requests over the bound,
compactions and failed responses, and prices every arm at every model in a published price table,
request by request.

The table is `src/contexto/bench/data/prices.json`, from
https://platform.claude.com/docs/en/about-claude/pricing, read 2026-11-12. USD per million tokens:

| Model | Input | 5-minute cache write | 1-hour cache write | Cache read | Output |
|---|---|---|---|---|---|
| claude-haiku-5-5, request up to 100,000 input tokens | 0.10 | 0.125 | 0.20 | 0.01 | 0.50 |
| claude-haiku-5-5, request over 100,000 input tokens | 0.50 | 0.625 | 1.00 | 0.05 | 2.50 |
| claude-sonnet-5-5 | 2.00 | 2.50 | 4.00 | 0.10 | 10.00 |
| claude-opus-5-5 | 4.00 | 5.00 | 8.00 | 0.20 | 20.00 |
| claude-fable-5-2 | 8.00 | 10.00 | 16.00 | 0.20 | 40.00 |

The Haiku size step counts all input tokens, cache reads and writes included, per request. That the
higher row then applies to the whole request, output included, is a reading of the page, not its
wording; the table's `size_rule` holds that reading as data, so another reading is a JSON edit.
Cache writes are priced at the run's cache lifetime (`--cache-ttl`). A response whose usage could
not be read gives a cost range: its upper bound prices the tokens reserved for that request at the
model's highest rate. When a unit was retried, the tokens and cost of every attempt count, because
every attempt was billed; accuracy uses the completed attempt. No row is a bill.

## Spend guards

A live run spends your money, so it stops itself:

- `--budget-tokens` is required and must be at least the dry run's worst case. It covers the whole
  output directory, earlier attempts included. Before each request leaves, the meter reserves its
  request bytes / 1.5 plus its `max_tokens`, and refuses a request that could pass the budget. A
  response whose usage cannot be read counts as its reservation. A request sent that got no response
  (the connection failed or closed before a status line) may still have been billed, so its
  reservation is kept as spent; each unit counts these in `requests_unanswered` and
  `unanswered_tokens`. One whose connection was never made counts 0.
- The first 401 or 403 stops the run.
- Per unit: more than 60 answered requests in one script turn (`--max-turn-requests`; the recorded
  runs needed at most 6, the tool-loop turn needs 11), more than 200 failed responses in one turn
  (`--max-turn-failures`), or more than twice the unit's planned worst case (`--max-unit-tokens`)
  stops that unit, and the run goes on with the next. A turn with no metered request stops the unit
  as `unmetered`.
- If the runner dies, a watchdog stops the unit's processes (all gone within 1.7 seconds of a
  SIGKILL in the test).
- Managed Claude Code settings that would change routing, compaction, permissions, the model, MCP
  servers, hooks or the system prompt refuse the run, since they would make the arms differ.

## The output directory

```
<out>/
  manifest.json                 what was run: versions, model, bound, seeds, arms, every unit's status
  scripts/seed-<seed>.json      the generated script of each seed
  probes.jsonl                  live: one line per arm, seed, repeat and probe, for complete units
  requests.jsonl                live and replay: one line per request the upstream answered
  *.torn-<n>                    live, after a resume: a torn last line set aside
  reach.jsonl                   replay: the search reachability of each planted fact
  results.json, results.md      the report
  charts/accuracy.svg           live: accuracy by arm with Wilson 95% intervals
  charts/request-size.svg       request size against history length
  store/                        replay with --out: the replayed session's store, synthetic pages only
```

The directory is meant to be published as it is. It holds the synthetic script, the model's parsed
probe answers (at most 200 characters each), token counts, statuses and versions. Never in it: a
key, a key-helper command, a settings path, an upstream's hostname (the upstream is recorded only as
`anthropic`, `custom` or `local`), a home directory or user name, or any request or response body
other than the synthetic script and the parsed answers. Tests plant secrets and check that none
reaches an output. Claude Code's and Contexto's own session files go into a separate working
directory that is removed after each unit. A replay's `store/` has its random `page.key` and
`affinity.salt` removed; opening it as a session writes a new `page.key`, so remove that again
before publishing.

## The minimum run that makes the claim testable

The audit behind D66 suggested 10 seeds on claude-haiku-5-5, 5 seeds on claude-opus-5-5, and 3
repeats of one seed. With `v2`'s 72 probes, the Haiku run gives 720 pooled paired probes per pair of
arms over 10 seeds, and its three runs of the first seed give 216 probes per arm for run-to-run
noise (repeats are not pooled). The Opus run gives 360. The project's maintainer will start these
runs; anyone can run the same commands with their own key, at their own cost.

```sh
contexto bench --model claude-haiku-5-5 --seeds 10 --repeats 3 --budget-tokens 598290000 --out <dir>
contexto bench --model claude-opus-5-5 --seeds 5 --budget-tokens 250500000 --out <dir>
```

From the dry run (tokens, input and output of every arm together), with an estimated cost in USD:

| Run | Units | Expected | Worst case | `--budget-tokens` | Expected, USD | Worst case, USD (5-minute / 1-hour cache) |
|---|---|---|---|---|---|---|
| claude-haiku-5-5, 10 seeds, first seed 3 times | 36 | 180,251,043 | 598,285,396 | 598,290,000 | about $14 | about $456 / $602 |
| claude-opus-5-5, 5 seeds | 15 | 97,122,711 | 250,490,244 | 250,500,000 | about $90 | about $2,025 / $2,622 |

How the dollar figures were computed: every request the dry run's plan simulates
(`simulate_contexto`, `simulate_plain` and `simulate_full` in `contexto.bench.plan`) was split into
uncached input, cache write, cache read and output as the plan splits it, and priced on its own
with the shipped table (`contexto.bench.prices.request_cost`). `--dry-run` prints those classes
only as totals, and claude-haiku-5-5's size step needs each request's size: in the Haiku plan's
expected case, 524 of 1,606 requests are over 100,000 input tokens and pay the higher row on the
whole request, as the report applies it. The token totals of that computation equal the dry run's.
The Haiku expected cost splits as about $2.71 for `contexto`, $6.08 for `full` and $5.19 for
`plain`; the Opus one as $36.37, $16.97 and $36.57. These are estimates from a plan, not a quote:
the run is paid by whoever runs it, at their account's prices, and `--budget-tokens` is the
ceiling.

The expected case is a rough size. Its tool rounds are calibrated on the two recorded live runs,
and the plain arm's figures are assumed: no plain arm has been run live, and two of its figures come
from Claude Code 2.1.322 against a stub. The plan assumes a 3,000-token summary and 25,000 tokens of
re-attached files per compaction, which errs toward more spend; the replay's modelled plain line
uses a 7,920-token summary and nothing re-attached, which errs toward plain Claude Code (see
[below](#what-the-replay-shows-and-what-it-cannot)). The two serve different ends, a budget and a
lower bound on plain Claude Code's request size, so they are kept apart. The Opus expected figure scales the Haiku calibration by
1.381, the ratio of the recorded Opus run's Contexto arm to run #4's (4,726,388 against 3,422,988
tokens), and the plain arm by the same factor [assumed]. The worst case counts every input token as
a cache write with a 10% margin and reserves 32,000 output tokens per Claude Code request and 8,192
per full-arm request. It is a planning figure, not a ceiling: Claude Code may ask for up to 128,000
output tokens a request. The ceiling is `--budget-tokens`, which the guard enforces.

With 5 seeds the Opus run alone cannot name a direction (see the six-seed floor above); it adds a
second model's accuracy and cost, not a verdict.

## What the replay shows, and what it cannot

The pinned trace (`src/contexto/bench/data/trace-20261016.json`, 11,175 bytes) is seed 20261016,
protocol `v2`, bound 150,000, with 40 `d27` content turns so the history runs well past the bound.
Measured on the replay, every size estimated from bytes (1.8 bytes per token for log text, 3.0 for
everything else):

- 56 turns (48 content, 8 probe); 952,971 estimated tokens of content history, 6.35 times the bound
  (956,104 with the probe turns); a synthetic head of 21,306.
- 138 requests, the first 44,302 estimated tokens and the largest 116,306; 0 over the bound by the
  fake upstream's count and 0 by the proxy's own log; 14 rotations; 47 pages stored; no failure
  event; no request carried a compaction summary or marker.
- Search reachability, measured when the probe turns start. Under one fixed query per fact, the
  store's strict search returns 48 of 72 planted facts, all 48 in the top 3, and 6 more are still in
  view (54). The best query made from each question's own words (single words and adjacent pairs,
  counted only on whole-word matches) puts 60 in the top 3.

| Class | Probes | Paged | By the fixed query | Best query from the question's words |
|---|---|---|---|---|
| paged_only | 4 | 4 | 4 | 4 |
| multihop | 8 | 7 | 7 | 7 |
| chain | 30 | 27 | 27 | 27 |
| obligations | 6 | 4 | 4 | 4 |
| distractor | 6 | 6 | 6 | 6 |
| paraphrase | 6 | 6 | 0 | 6 |
| concluded | 6 | 6 | 0 | 0 |
| toolloop | 6 | 6 | 0 | 6 |
| all | 72 | 66 | 48 | 60 |

These are a floor under mechanical queries, not what a model reaches. For the three new classes the
fixed query is every content word of the question, and no page holds all of them, so that 0 says
nothing about the plants. Which query a model makes is for the live run.

Reach is counted with the store's strict search (`SessionStore.search`), which counts a page only
when it holds every word of the query, not with the reply of the `contexto_search` tool. Since D65
the tool falls back to approximate matches when no page holds every word. On the pinned session
that fallback answers 1 of the 18 queries the strict search finds nothing for: for `q_r_02`
(`paraphrase`) its reply shows the plant's line on the second of the three pages it shows in
detail, beside two of the service's decoy lines. The table does not count it.

Modelled from the script, not run: full context's last request would be 977,410 estimated tokens,
and plain Claude Code compacting at the bound would compact 8 times with a largest request of
140,441. The plain model takes the summary as 7,920 tokens (the mean of three measured summaries in
[research/M0-proxy-measurements.md](research/M0-proxy-measurements.md)) and assumes that only the
head, the summary and the current turn survive a compaction; anything re-attached would make plain
Claude Code's requests larger, so the assumption errs toward plain Claude Code. The dry run's plan
models the same arm differently on purpose, with a 3,000-token summary and 25,000 tokens of
re-attached files, to err toward more spend
([the minimum run](#the-minimum-run-that-makes-the-claim-testable)).

A replay cannot show answer accuracy, cache reads, real token counts, what a real CLI or a real
model does, or what the proxy does with what only Claude Code sends (its session start, the role
`system` messages of 5.x model ids, thinking blocks, side requests). Its output says so.

## Request size against history length

<img src="bench/request-size-synthetic.svg" width="800" alt="Line chart of request size against history length for the pinned replay, a synthetic session with no model. The x axis is the history so far, 0 to about 956,000 estimated tokens; the y axis is the request's input, 0 to 1 million tokens, with a dotted line at the bound of 150,000. The Contexto line (estimated) rises and drops 14 times and peaks at 116,306. The plain Claude Code line (modelled) rises and drops 8 times and peaks at 140,441. The full-context line (modelled) grows straight to 977,410.">

Synthetic session, no model: the pinned replay (seed 20261016, protocol `v2`, bound 150,000) sent
through the real proxy to a fake upstream on 127.0.0.1. The Contexto line is estimated from the
bytes of each request the proxy sent, not counted by a tokenizer. The full-context and plain Claude
Code lines are modelled from the script, not run. Contexto's drops are its 14 rotations: old turns
moved to the store, word for word and still recallable through search. Plain Claude Code's drops
are its 8 modelled compactions: old turns replaced by a summary. Peaks: Contexto 116,306 estimated
tokens, plain Claude Code 140,441 and full context 977,410. The full-context line sets the y axis
near 1M tokens, so the other two lines look flatter than they are; the bound is the dotted line.
Each request of a turn, tool-loop rounds included, is plotted at that turn's history length.

Made with `contexto bench --replay --out D` and `contexto bench --chart D/results.json` from
`main` at c476834f, before any release had the command; the notes under the chart are the ones
`--chart` writes. Its subtitle's `0.0.0+checkout` is the placeholder version every build from a
checkout carries (`pyproject.toml`), not a release. The chart's own notes do not yet say that
Contexto's drops are rotations and plain Claude Code's are compactions; a planned follow-up to the
code adds that, and until then this caption says it.

## The plain arm's compaction

Claude Code has no setting for the token count at which it compacts. Every documented control
(`CLAUDE_CODE_AUTO_COMPACT_WINDOW`, `--autocompact`, the `autoCompactWindow` setting, and per-model
`modelSettings`) sets the auto-compact window, which must be 100,000 to 1,000,000 tokens and is
capped at the model's window. Claude Code 2.1.322 compacts 33,000 tokens below that window. The
33,000 was read in the installed 2.1.322 bundle, not in the documentation, and has not been
confirmed by a live run; the documentation's one figure, compaction "at about 967K" on a 1,000,000
window, agrees with it. At that default, plain Claude Code on these models would compact near
967,000 tokens. The live run's session at the default settings is 467,012 estimated tokens, so
plain Claude Code at its defaults would not compact at all in it.

So the plain arm sets `CLAUDE_CODE_AUTO_COMPACT_WINDOW` to the bound plus 33,000 (183,000 for the
default 150,000), which outranks the flag and the settings. That works for bounds from 67,000 to
967,000; outside that range the nearest window is set and the manifest says the trigger could not
be placed at the bound.

The trigger is near the bound, not at it. Claude Code compares the last response's usage plus its
own estimate of what was added since, so a large turn can carry the session past the trigger before
the check runs. In stub runs with the real 2.1.322 at bound 150,000, compaction started at
`pre_tokens` 148,159, 167,202, 168,178 and 147,140, and the largest plain request was 158,168
tokens (stub-modelled usage). The manifest's `plain_compaction` records the variable, the value, the
intended trigger and whether it could be placed; each unit records every compaction's `pre_tokens`;
the report shows how far past the intended start each one was.

## Public benchmarks as a later arm

Not built. Read on 2026-11-12. The ids and titles, and the figures marked "arXiv", were re-checked
at arXiv on 2026-11-14; the rest is from the D66 research note:

- **Compact-Bench** (arXiv 2610.31710, "Measuring What Context Compaction Costs Long-Horizon Coding
  Agents"; v2 2026-10-28, a preprint). It scores a compactor that turns a history into a summary.
  Contexto writes no summary, and the task histories are 13 to 53 KB (research note), so nothing
  would rotate at a 150,000-token bound: it does not fit. Background worth knowing (arXiv): LLM
  summaries keep at most 16.0% of recurring details, and with one summarizer Claude Code's prompt
  kept 15.0%.
- **LongMemEval** (arXiv 2410.10813, ICLR 2025; code and data MIT): 500 questions (arXiv), each
  with its own history. The S variant is about 115,000 tokens per history, below the bound, and
  scoring needs an LLM judge. It fits only with a custom history length and a subset.
- **BEAM** (arXiv 2510.27246, "Beyond a Million Tokens"): 100 conversations in tiers from about
  100,000 to 10 million tokens, 20 probing questions each, scored by an LLM against rubric nuggets
  (arXiv). Its 500,000-token tier (3.3 times the bound) is the closest fit to this design. The
  research note read the data's licence as CC BY-SA 4.0, to be downloaded at run time and not
  copied here.

Any conversation benchmark meets one problem: a headless CLI writes its own replies, so recorded
assistant turns can only be fed as quoted text inside user turns. That decides whether a result is a
run of that benchmark or only probes taken from it.

## Where the code is

- `src/contexto/bench/`: `script.py`, `d27.py` and `model.py` (generator and scoring), `stats.py`,
  `arms/`, `runner.py`, `meter.py`, `plan.py`, `credentials.py`, `records.py`, `prices.py`,
  `report.py`, `charts.py`, `replay/`, and `modes/` (one module per mode).
- `spikes/m2/acceptance/`: the harness of runs #1 to #4, kept as their frozen record. The package
  imports nothing from it; the generator was ported and tests prove the port identical.
- `spikes/m2/scale/proxy/`: the scale harness, repaired under D66, whose 200-turn version runs in CI
  ([README](../spikes/m2/scale/proxy/README.md)).
