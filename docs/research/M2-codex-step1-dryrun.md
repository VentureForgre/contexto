# M2 A21: Codex acceptance step 1, zero-spend dry run

Packet A21 (lane A). It covers intake's conditions for considering a 5,385,600-token line for step 1
of the Codex acceptance plan (`docs/research/M2-codex-acceptance-plan.md`): the H1 arm alone, for
acceptance 1 and 3.

Everything ran against `spikes/m2/acceptance/codex_stub.py` on 127.0.0.1:
- scratch HOME, CODEX_HOME and CONTEXTO_HOME;
- codex-cli 0.154.0, `gpt-6-astra` pinned, bound 64,000, 20 KB turns, seed 20261016;
- never the owner's ChatGPT login or `~/.codex`.

Numbers only; no transcripts.

## Reviewer-1's notes on PR #154

- **(a) Startup window for a missing log** (reworked after reviewer-2 on PR #165).
  - Only on an attempt's first turn, and only until the log has been seen once, may a missing session log be the session still starting, for at most 10 seconds (`SpendWatch.begin_turn`).
  - **Why 10 seconds is enough:** the launcher logs `proxy_start` before it starts Codex, and every dry-run turn here started within a second.
  - After the log has been seen, on any later turn, or past the window, a missing log fails closed at once.
  - `run_session` also stops the run in two cases, and never reads a missing log as nothing spent:
    - when the log is missing after a turn returns;
    - when the log is missing or unreadable before any turn but the attempt's first.
  - The first version restarted the window every turn, so a log that never appeared went uncaught when turns ran under the window (reviewer-2's repro).
  - Tests:
    - `test_only_the_first_turn_may_start_without_a_log_and_only_inside_the_window` (both sides);
    - `test_a_log_deleted_mid_run_fails_closed_at_once`;
    - `test_a_log_that_never_appears_stops_the_run_after_the_first_turn` (reviewer-2's case: it stops after turn 1, and a resume stops after its first turn).
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

Pending A20 (PR #159, builder-1: `codex_recall_credit.py`). Once it is on main, the full step-1 dry run runs on that main SHA, and acceptance 3 is credited through `codex_recall_credit` from the session log.

## Tests

`spikes/m2/acceptance/tests/test_codex_acceptance.py` has 27 tests. The full suites results are in the PR.
