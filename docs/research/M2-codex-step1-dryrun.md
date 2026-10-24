# M2 A21: Codex acceptance step 1, zero-spend dry run

Packet A21 (lane A). It covers intake's conditions for considering a 5,385,600-token line for step 1
of the Codex acceptance plan (`docs/research/M2-codex-acceptance-plan.md`): the H1 arm alone, for
acceptance 1 and 3.

Everything ran against `spikes/m2/acceptance/codex_stub.py` on 127.0.0.1:
- scratch HOME, CODEX_HOME and CONTEXTO_HOME;
- codex-cli 0.154.0, `gpt-6-astra` pinned, bound 64,000, 20 KB turns, seed 20261016;
- never a live ChatGPT login or the user's `~/.codex`.

Numbers only; no transcripts.

## Reviewer-1's notes on PR #154

- **(a) Startup window for a missing log** (reworked after reviewer-2 on PR #165).
  - Only on the session's very first turn (turn index 0), and only until the log has been seen once, may a missing session log be the session still starting, for at most 10 seconds (`SpendWatch.begin_turn(fresh=True)`).
  - **Why 10 seconds is enough:** the launcher logs `proxy_start` before it starts Codex, and every dry-run turn here started within a second.
  - After the log has been seen, on any later turn, or past the window, a missing log fails closed at once.
  - `run_session` also stops the run in two cases, and never reads a missing log as nothing spent:
    - when the log is missing after a turn returns;
    - when the log is missing or unreadable before any turn but the session's first. That includes a resume's first turn: a resume that finds no log sends no turn, since its spend so far, including what a failed attempt was billed, is unknown (reviewer-2's residual on PR #165).
  - The first version restarted the window every turn, so a log that never appeared went uncaught when turns ran under the window (reviewer-2's repro).
  - Tests:
    - `test_only_the_first_turn_may_start_without_a_log_and_only_inside_the_window` (both sides);
    - `test_a_log_deleted_mid_run_fails_closed_at_once`;
    - `test_a_log_that_never_appears_stops_the_run_after_the_first_turn` (reviewer-2's case: it stops after turn 1, and a resume that finds no log sends no turn).
- **(b) Joining an attempt and its resume.**
  - Each record keeps its probe-turn answers by turn index (`answers_by_turn`) and the turn it failed on (`failed_turn`).
  - `join_records` merges the attempts by turn index; a later attempt replaces an earlier one's answer for the same turn.
  - It lists every attempt with its failed turn, so the turn that ran twice is visible, and it reports whether every probe turn has an answer.
  - Test: `test_records_of_an_attempt_and_its_resume_join_into_one_scored_record`.

## Condition 1: retries enforced (harness only)

`codex_driver.RETRY_PIN` puts `-c model_providers.contexto.request_max_retries=0` and `-c model_providers.contexto.stream_max_retries=0` on every acceptance turn's command line. `run_codex` and the launcher are unchanged, so users keep Codex's own retries.

- **(a)** Test: `test_every_acceptance_turn_carries_both_retry_pins`. Every turn's final argv carries exactly these two pins.
- **(b)** The dry run with the stub failing the third POST:
  `uv run python spikes/m2/acceptance/codex_dryrun.py "$PWD/.venv/bin/contexto" /tmp/a21-fail --fail-post 3`.

  | | |
  |---|---|
  | Proxy log, POST /responses | 3: statuses 200, 200, 500 |
  | Turn 3 (the failed one) | exactly 1 POST, Codex exit 1 |
  | Record | `turns_completed` 2, `failed_turn` 2 (0-based), stopped "turn 3: codex exited 1" |

  Codex's default retries sent 30 POSTs for one failing turn (A16).

## Condition 2: the spend watcher trips

The dry run with an approval line of 200,000:
`uv run python spikes/m2/acceptance/codex_dryrun.py "$PWD/.venv/bin/contexto" /tmp/a21-trip --approved 200000`.

Turn 1's pre-turn check (spent + the turn's estimate + reserve = 0 + 96,000 + 0) is within 200,000, so the turn starts. While its request is in flight, the in-turn condition is spent + 2 × 96,000 = 192,000, which is within the line. Once the request is logged (25,448), spent + 2 × 96,000 = 217,448 passes it, and the watcher stops the turn.

| | |
|---|---|
| Stop | turn 1, `stopped_by_watcher` [0]; Codex exit 143 (SIGTERM to the turn's process group) |
| Processes left for the session | 0 |
| Spent at the stop | 25,448 of 200,000; within the one-request in-flight allowance (96,000) and under the line |
| Record | stopped "turn 1: stopped by the spend watcher (approved 200,000)", `turns_completed` 0 |
| POSTs | 1 |

## Condition 3: acceptance 1 and 3 on a full step-1 dry run

On main **e07a963** (with A20, `codex_recall_credit`, PR #159) merged into this PR's branch:
`uv run python spikes/m2/acceptance/codex_dryrun.py "$PWD/.venv/bin/contexto" /tmp/a21-full`.

The run uses `CONTEXTO_HOME`, Codex retries pinned, and the spend watcher at the H1 worst case.

**Acceptance 1:**

| | |
|---|---|
| History | 164,844 tokens of content at least 2.5 times the bound (160,000); 15 content and 6 probe turns, all 21 completed |
| Requests | 27 POST `/v1/responses`, all 200: 0 4xx |
| Bound | largest real input 27,015 (stub tokens) against 64,000; 0 `over_bound` of either source; no failure events |
| Rotations | 18 for size and 2 for room |
| Compaction | none: Codex's rollout holds no `compacted` record, and Codex compacts only at 244,800 tokens |
| Stream hold | each of the 6 tool-call rounds held 3 events; the 15 `OK` replies and the 6 answers held none |
| Verdict | `acceptance_1` true |

**Acceptance 3** (`codex_recall_credit.credit(probes, answers, session_log, pages, probe_turn_ids=..., expected_turns=21)`, called from `codex_dryrun.acceptance_3`):

| | |
|---|---|
| Verdict | `acceptance_3` true |
| Credited | 4 of the 4 paged-only probes; their plants are in turn 1, paged out by the first rotation |
| Per probe turn | turn 1: `q_p_03` (via `contexto_search`); turn 2: `q_p_02` (search); turn 4: `q_p_01` (`contexto_recall`, 4 calls); turn 5: `q_p_04` (search); turns 3 and 6: none |
| Expected turns | 21, content plus probe turns (B29) |
| In-view spans | 14 returned by search from the retained window (A14), logged content-free |
| Answered correctly, not credited | 50 (the stub is given every answer; credit needs a returned span covering the plant before the answer) |
| Recall calls | 6 `contexto_search` and 6 `contexto_recall` through Codex's `exec`, 0 failed |

**Conditions 1 and 2 re-run on the same tree** (main e07a963 merged, branch head 2ad66fe):
- `--fail-post 3`: 3 POSTs (2 `OK`, 1 failed). The failing turn made exactly 1 POST, Codex exited 1, and the run stopped at turn 3 with `failed_turn` 2.
- `--approved 200000`: the watcher stopped turn 1 with exit 143 and 25,448 spent. Both unchanged.

## Tests

`spikes/m2/acceptance/tests/test_codex_acceptance.py` has 27 tests. The full suites results are in the PR.
