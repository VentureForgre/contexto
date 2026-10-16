# M2 A22: Codex acceptance step 1, live

Packet A22 (lane A). Step 1 of the Codex acceptance plan (`docs/research/M2-codex-acceptance-plan.md`):
the H1 arm alone, for acceptance 1 and 3, run live under intake's go of 2026-10-16 14:27Z. The
zero-spend dry run of the same step is `docs/research/M2-codex-step1-dryrun.md`.

Numbers and event counts only; no transcripts, prompts, tool output or answers.

## Run conditions

| | |
|---|---|
| Code | main `ca05b292b425a8f80944fea0433d723a13d0ba30`, tree `c654fbc73672e16b7a62994fffa081593668d688`, a clean detached checkout |
| Entry point | `spikes/m2/acceptance/codex_live.py` (PR #166) |
| Codex | codex-cli 0.154.0, `gpt-6-astra`, reasoning effort low |
| Engine | bound 64,000, 20 KB turns, seed 20261016 |
| Auth | API-key mode through the owner's proxy (D12, D22); the key from the seat's own key helper, read at call time, in no file |
| Homes | temporary `CONTEXTO_HOME` and `CODEX_HOME` outside the repository |
| Codex pins | `RETRY_PIN` (request and stream retries 0); `QUIET_PIN` (`analytics.enabled=false`, `features.plugins=false`) |
| Approval | 5,385,600 tokens, equal to codex_budget's step-1 worst case |

Command, from the checkout's root (the seat settings path shortened):

```
uv run python spikes/m2/acceptance/codex_live.py "$PWD/.venv/bin/contexto" <out dir> \
    --upstream <owner's proxy base URL> \
    --key-helper "python3 <checkout>/spikes/m2/acceptance/keyhelper.py <seat>/.claude/settings.local.json" \
    --approved 5385600
```

Stop rules armed: any 4xx or 5xx, a request whose output passes 32,000 tokens, a Responses request
that ended other than `completed` (A23), a turn with no assistant message (how Codex 0.154.0 shows a
refusal), Codex's `turn.failed` or `error`, and the fail-closed spend watcher. None fired.

## Result

The run completed: exit 0, 21 of 21 turns (15 content, 6 probe), every Codex exit 0, no failed turn,
no watcher stop.

**Acceptance 1: true.**

| | |
|---|---|
| History | 164,844 tokens against the 160,000 needed (2.5 times the bound) |
| Compaction | none: Codex's rollout holds no `compacted` record |
| Requests | 44 POST `/v1/responses`, all 200, all with `end_event` completed; 0 4xx |
| Bound | largest input 26,066 against 64,000; 0 `over_bound` real; no failure events |
| Rotations | 16 for size (`bound`) and 3 for room (`min_turn_room`) |

**Acceptance 3: true** (`codex_recall_credit`).

| | |
|---|---|
| Credited | 11, with expected_turns 21 |
| Credited per probe turn | 3, 1, 0, 0, 3, 4 |
| Paged-only plants | 1 of 4 credited (`q_p_04`, via `contexto_search`); the other 3 were answered correctly without their span being returned, so not credited |
| Answered correctly | 54 of 54; 43 of them not credited |
| In-view spans | 23 |

**Spend** (real usage from the proxy's log):

| | Tokens |
|---|---|
| Input | 919,998 |
| Cached | 680,832 (74.0% of input) |
| Output | 2,203 |
| Total | 922,201 of 5,385,600 (17.1%) |

**Requests and tools:**

| | |
|---|---|
| Requests | 44: 21 that ended a turn and 23 that held a tool call |
| Max input / max output | 26,066 / 136 |
| MCP calls through Codex's `exec` | `contexto_search` 84, `contexto_recall` 0, `contexto_pin` 9, `contexto_state` 1; 0 failed |
| Stream hold | 23 requests held (largest hold 128 events), 21 unheld |

The model also called `contexto_pin` and `contexto_state`, which the stub never did, and never
`contexto_recall`. Neither is a stop rule; both are noted as observed.

## Calls outside the proxy

Two views, neither complete:
- **Codex's local logs:** no host named, with 0 log files and 237 SQLite rows scanned. This is not a
  network observation.
- **Network sample:** 168 samples, every 1 s, of each turn's process group. Short connections can be
  missed.
  - `codex` reached only 127.0.0.1.
  - The proxy process reached 127.0.0.1 and two addresses on port 443. Both are the AAAA records of
    the owner's proxy host (a DNS lookup only), so they are its upstream.
  - No connection outside the proxy path was seen. Without `QUIET_PIN`, a stub run's samples showed
    Codex reaching chatgpt.com and github.com (PR #166).
