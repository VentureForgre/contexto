# Can Claude Code hold and resend a 30M-token conversation?

Spike for M2 scale, builder-cli, 2026-10-17. Claude Code 2.1.302 (`~/.local/bin/claude`), Apple M5
Pro (18 cores, 48 GB), python 3.14. Every number below is measured unless it is marked
*inferred*.

Contexto keeps each upstream request under 150,000 tokens, but Claude Code still holds the whole
conversation and sends all of it to the proxy on every request. This spike measures what that
costs the CLI as the history grows, and whether the small usage Contexto reports keeps the CLI's
compaction off.

## Answer

- **Largest history handled: 525M tokens.** Claude Code resumed a 525,017,525-token session and sent
  it in full as one 918 MiB request, 6.6 s after launch, with a 9.7 GB peak RSS. At 30M tokens the
  same resume took 0.70 s to the first request: the request was 52.6 MiB, peak RSS was 0.96 GB, and the
  session file was 102 MB.
- **What failed first beyond that:** at 550M tokens and above (550M, 575M, 600M, 700M) the CLI
  loaded the session, then stopped before sending, with `API Error: Out of memory` and exit 1.
  Peak RSS was 7.3 to 9.2 GB, well under both the machine's free memory and the harness's 14 GB
  cap. *Inferred:* this is a per-allocation limit in the CLI's JavaScript runtime that the request
  body hits near 1 GB (the last success was 963,000,000 bytes, and the first failure would have been about 1,009,000,000
  bytes), not real memory exhaustion.
- **Small reported usage keeps compaction off at every size tested.** With the fake upstream
  reporting 100,000 input tokens, as Contexto's bounded request would, the CLI never compacted and
  never called `count_tokens`. That held from 0.5M to 525M tokens on `claude-opus-5-5`, to 30M on
  `claude-haiku-5-5` and on the 200K-window `claude-haiku-4-5`, and through a three-step tool loop at
  30M and 100M. With honest usage, the same sessions compacted before sending at 968K tokens or
  more on the two 1M-window models and at 194.5K or more on `claude-haiku-4-5`. So the CLI's
  decision follows reported usage, not the size of the history it holds.

## Results

Usage mode `contexto`: every assistant record in the session, and every reply from the fake
upstream, reports `input_tokens` 100,000 (planted usage 100,600 counts the 600 output tokens).
Mode `honest`: the planted usage is the session's true size, and the fake upstream reports body
bytes / 1.83. "Session file MB" is before and after the run; file and request sizes use MiB
(2^20 bytes), and RSS uses MB as `/usr/bin/time` bytes / 2^20. The CLI peak RSS comes from
`/usr/bin/time -l`. The load is the 1-minute `uptime` load average just before each launch (other
agents were benchmarking on the machine). One run per row.

### Contexto-like usage, `claude-opus-5-5`, `claude -p "continue" --resume <id>`

| History (tokens) | Session file MB | Launch to 1st request s | Request MiB | Requests | Compactions | Peak RSS MB | Exit | Load |
|---|---|---|---|---|---|---|---|---|
| 505,416 | 1.7 -> 1.9 | 0.20 | 0.95 | 1 | 0 | 241 | 0 | 4.4 |
| 2,005,963 | 6.8 -> 7.0 | 0.25 | 3.57 | 1 | 0 | 297 | 0 | 5.3 |
| 5,000,333 | 17.0 -> 17.2 | 0.34 | 8.81 | 1 | 0 | 404 | 0 | 5.3 |
| 10,013,928 | 34.0 -> 34.3 | 0.42 | 17.58 | 1 | 0 | 511 | 0 | 5.3 |
| 30,021,378 | 101.9 -> 129.5 | 0.70 | 52.58 | 1 | 0 | 958 | 0 | 5.3 |
| 60,004,015 | 203.6 -> 284.8 | 1.14 | 105.03 | 1 | 0 | 1,587 | 0 | 5.6 |
| 100,017,788 | 339.4 -> 492.1 | 1.62 | 175.02 | 1 | 0 | 2,488 | 0 | 5.7 |
| 200,007,442 | 678.7 -> 1010.0 | 3.06 | 349.93 | 1 | 0 | 4,364 | 0 | 9.6 |
| 300,001,144 | 1018.1 -> 1527.9 | 4.28 | 524.85 | 1 | 0 | 6,387 | 0 | 9.2 |
| 500,004,885 | 1696.8 -> 2563.9 | 6.75 | 874.71 | 1 | 0 | 9,403 | 0 | 9.0 |
| 525,017,525 | 1781.7 -> 2693.4 | 6.63 | 918.46 | 1 | 0 | 9,662 | 0 | 6.1 |
| 550,023,300 | 1866.6 -> 2822.8 | no request | | 0 | 0 | 7,256 | 1, `API Error: Out of memory` | 6.0 |
| 575,022,946 | 1951.4 -> 2952.4 | no request | | 0 | 0 | 8,143 | 1, same | 8.8 |
| 600,020,969 | 2036.3 -> 3081.8 | no request | | 0 | 0 | 8,143 | 1, same | 8.0 |
| 700,015,218 | 2375.6 -> 3599.8 | no request | | 0 | 0 | 9,204 | 1, same | 7.8 |

Every request carried the whole history: for example, 5,023 messages at 30M and 16,731 at 100M
(`requests.csv`). A first pass on the same harness (`results-run1.csv`) agreed: at 30M it measured
0.66 s and 52.55 MiB, at 100M 1.57 s and 175.02 MiB, and at 300M 4.03 s and 524.86 MiB.

### Other models, tool loop, honest usage

| Run | Model | Usage | History (tokens) | Planted usage | Requests | Compactions | 1st request MiB | Peak RSS MB |
|---|---|---|---|---|---|---|---|---|
| D | haiku-5-5 | contexto | 505,438 / 2,006,064 / 30,001,233 | 100,600 | 1 each | 0 | 0.95 / 3.57 / 52.54 | 242 / 299 / 913 |
| G2 | haiku-4-5 (200K window) | contexto | 2,006,064 / 30,001,233 | 100,600 | 1 each | 0 | 3.62 / 52.59 | 302 / 966 |
| E | opus-5-5, 3 Read round trips | contexto | 30,021,378 | 100,600 | 4 | 0 | 52.58 (all 4) | 1,137 |
| E | opus-5-5, 3 Read round trips | contexto | 100,017,788 | 100,600 | 4 | 0 | 175.02 (all 4) | 3,303 |
| E2 | haiku-4-5, 3 Read round trips | contexto | 30,001,233 | 100,600 | 4 | 0 | 52.59 (all 4) | 1,136 |
| B | opus-5-5 | honest | 505,370 / 920,977 | same as size | 1 each | 0 | | 242 / 262 |
| B | opus-5-5 | honest | 968,156 / 1.01M / 2.0M / 5.0M / 30.0M | same as size | 2 each | 1 each | 1.75 / 1.83 / 3.57 / 8.85 / 52.57 | 286 to 898 |
| C | haiku-5-5 | honest | 120,854 to 505,395 (5 sizes) | same | 1 each | 0 | | 222 to 242 |
| C | haiku-5-5 | honest | 1,014,218 / 2,005,881 | same | 2 each | 1 each | 1.83 / 3.57 | 289 / 323 |
| G | haiku-4-5 | honest | 171,008 | 170,896 | 1 | 0 | 0.41 | 227 |
| G | haiku-4-5 | honest | 194,615 / 215,597 / 505,395 | same | 2 each | 1 each | 0.43 / 0.46 / 0.97 | 232 to 257 |
| F | opus-5-5, fake refuses > 1,000,000 | honest | 2,005,785 | 2,005,714 | 8 | 6 | 3.57 | 353 |

The first pass narrows the 1M-window bracket: a 950,387-token honest session did not compact on
either 1M-window model (`results-run1.csv`). The measured pre-send trigger therefore lies between
950,387 and 968,037 planted tokens, which agrees with M0's 970,012 / 975,012 bracket within the
step size. For `claude-haiku-4-5` it lies between 172,587 (no compaction) and 194,502 (compaction).
`claude-haiku-5-5` behaves as a 1M-window model in this CLI: 505K honest did not compact.

The full table of all 44 runs is generated by `make_table.py results.csv`. The isolation check,
across all runs: 3,141 lsof samples; 40 inet sockets seen, every one from the CLI to the fake
upstream on 127.0.0.1 (the four failed runs never opened one); 0 non-loopback sockets; 0 proxy
attempts refused.

## Findings

1. **The CLI resends the whole history on every request, and its cost grows linearly** (measured,
   contexto mode, opus-5-5). Over 0.5M to 525M tokens:
   - Launch to first request: about 0.2 s plus 13 ms per million tokens.
   - Peak RSS: about 240 MB plus 18 to 24 bytes per token.
   - Request body: 1.75 MiB per million tokens, plus about 0.06 MiB of system prompt and tools.
   - Synthetic session file: about 3.4 bytes per token, because Read results are stored twice, as
     the real CLI stores them (`toolUseResult`).

   At the owner's 30M target, the CLI's per-request work is small: 0.7 s to the first request on
   resume, about 0.25 s per tool round trip in the loop (inter-request gaps of 1.75 s, minus the
   fake upstream's 1.5 s hold), and 1.1 GB peak RSS through four sends. At 100M the round trip is
   about 0.8 s and peak RSS is 3.3 GB.
2. **Compaction follows reported usage only** (measured). Every honest session at or above the
   trigger sent one compaction request before the new prompt. That compaction request carried the
   whole history (for example, 52.57 MiB at 30M). The follow-up request was 66 to 69 KB with 5
   messages on the 1M-window models and 117 KB with 3 messages on `claude-haiku-4-5`.
   No contexto-mode run ever sent one. Neither mode ever called `/v1/messages/count_tokens`.
   *Inferred:* the local estimate covers only messages after the last assistant record with
   usage. Here that is the short "continue" prompt or a small Read result, so a large single tool
   result arriving after a small reported usage (D31's case) is not covered by this spike.
3. **When the upstream refuses a too-long compaction, the CLI trims and retries** (measured, run F,
   fake upstream returning 400 "prompt is too long" above 1,000,000 tokens). The request sequence
   (`requests.csv`):
   - The 2M-token compaction request was refused twice.
   - The CLI retried with about half the messages (167; refused twice), then with 155 (accepted).
   - The new prompt went out with 183 messages, or 1.12M tokens (refused).
   - A second compaction of 134 messages was accepted.
   - The final request held 51 messages (319,504 tokens) and was answered; exit 0, 8 requests in
     5 s.

   This path loses history, and Contexto avoids it only by keeping reported usage small.
4. **Resuming a large session rewrites part of it into the session file** (measured).
   - At 30M tokens and above, the first resume appended `api-request-blob` records holding about
     half the history's messages: 27.6 MB at 30M, 152.7 MB at 100M, 867 MB at 500M. At 10M and
     below, it appended about 9 KB.
   - Two more resumes of the same 30M session appended 14.8 MB and then 8.1 MB
     (`resume-again.jsonl`), so the file was 152 MB after three resumes.
   - The failed runs at 550M and above wrote these records before they failed.
   - *Inferred:* this is the CLI's request journal (`api-request` records list every message hash
     in `tail`). Why it stores blobs only above about 10M was not determined.
5. **Idle compaction is not covered by this method** (*inferred* from the 2.1.302 binary, not
   measured).
   - The changelog string reads "Added idle compaction in the terminal: a 200K+ token conversation
     left idle for about 54 minutes is compacted". That makes it a timer in the interactive TUI;
     `-p` resume did not trigger it. Honest sessions of 505K that had been idle two hours did not
     compact.
   - The timer reads `requestJournal.lastMainThreadContextTokens()`. That supports D31's premise
     that it counts the usage the proxy reported, but the premise is still unmeasured.
   - Its mode comes from the feature value `tengu_sunny_locket` and defaults to `"off"` when that
     value is absent. With nonessential traffic disabled, as here, the value is never fetched.

## Limits

- `-p` resume only. The interactive TUI (idle compaction, the context indicator, rendering a huge
  scrollback) was not driven.
- Synthetic content: unique Python-like files and prose, about 24K tokens per turn, Read tool only,
  no thinking blocks or images. "Tokens" means JSON bytes / 1.83, the ratio the brief set. The fake
  upstream answers instantly, apart from a 1.5 s hold per reply so that lsof sees the socket.
- Contexto's real reported usage varies (about 100K to 150K); here it is a constant 100,000.
- One run per size, on a shared machine (1-minute load 4.4 to 9.6 during the final pass).
- The harness kills any run whose process tree passes 14 GB RSS (15 GB for the runs at 550M and
  above). No run reached the cap.

## Safety

Zero model spend, no real API contact:

- Each launch got a scrubbed environment (`env -i` style) with `HOME`, `CLAUDE_CONFIG_DIR` and
  `TMPDIR` in a per-run temp dir under `/tmp/scale-cli/work`, and a fake `ANTHROPIC_API_KEY`.
- `ANTHROPIC_BASE_URL` pointed at `http://127.0.0.1:<port>`. `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`,
  `DISABLE_TELEMETRY=1`, `DISABLE_ERROR_REPORTING=1` and `DISABLE_AUTOUPDATER=1` were set.
- `HTTPS_PROXY`/`HTTP_PROXY` pointed at the fake upstream, which refuses and logs any `CONNECT`
  (none arrived).
- A monitor thread ran `lsof -a -i -P -n` on the process tree back to back for each whole run and
  kills the run on any non-loopback socket (none was seen).
- The fake upstream logs header names only, never values, and no message text.
- Nothing under `~/.claude` was used; the CLI wrote only to the temp config dir. Large session
  files were deleted after each run.

## Files and commands

| File | Role |
|---|---|
| `fake_upstream.py` | Loopback Messages API: SSE or JSON replies; `contexto` / `honest` usage; `--tool-loop N` Read round trips; `--window W` refusal; `--delay S` hold; logs one JSON line per request; flags compaction prompts |
| `harness.py` | Isolated launch, RSS sampling (tree, 50 ms, plus `/usr/bin/time -l`), lsof socket watch, RSS cap |
| `probe.py` | Step 2: one tiny real session (one Read round trip) to learn the JSONL format |
| `gen_session.py` | Synthetic session of a target size in the observed 2.1.302 format |
| `run_scale.py` | One resume per size; writes a CSV row and `<run>/result.json` |
| `run_all.sh` | Every sweep in this README, by phase |
| `resume_again.py` | Resume a kept session N more times (finding 4) |
| `make_table.py` | Markdown table from the CSV |
| `results.csv` | Final pass, 44 runs (`run_all.sh probe A B C C2 D G G2 F E E2 A2`, then the 500M to 700M runs below) |
| `results-run1.csv` | First pass of the same phases (no `planted_usage` column) |
| `requests.csv` | Every request the fake upstream saw in the final pass |
| `resume-again.jsonl` | Output of `resume_again.py` on a kept 30M session |

```
spikes/m2/scale/cli/run_all.sh probe A B C C2 D G G2 F E E2 A2   # writes /tmp/scale-cli/results.csv
D=spikes/m2/scale/cli
python3 -I $D/run_scale.py /tmp/scale-cli/work /tmp/scale-cli/results.csv --mode contexto --sizes 500000000 --timeout 1200 --label A
SCALE_MAX_RSS_GB=15 python3 -I $D/run_scale.py /tmp/scale-cli/work /tmp/scale-cli/results.csv --mode contexto --sizes 700000000 --timeout 1200 --label A
SCALE_MAX_RSS_GB=15 python3 -I $D/run_scale.py /tmp/scale-cli/work /tmp/scale-cli/results.csv --mode contexto --sizes 575000000,600000000 --timeout 1200 --label A
SCALE_MAX_RSS_GB=15 python3 -I $D/run_scale.py /tmp/scale-cli/work /tmp/scale-cli/results.csv --mode contexto --sizes 525000000,550000000 --timeout 1200 --label A
python3 -I $D/run_scale.py /tmp/scale-cli/work /tmp/scale-cli/k.csv --mode contexto --sizes 10000000,30000000 --label K --keep
python3 -I $D/resume_again.py /tmp/scale-cli/work/K-contexto-opus-5-5-tl0-30000000 2
python3 -I $D/make_table.py /tmp/scale-cli/results.csv
```

The session format observed by `probe.py` (2.1.302): `projects/<cwd with non-alphanumerics as
"-">/<session id>.jsonl`. It holds `user` and `assistant` records chained by `parentUuid`, and a
tool result is a `user` record with a `tool_result` block plus a `toolUseResult` sidecar. The CLI
also writes `attachment`, `api-request`, `api-request-shape` (system prompt and tools),
`api-request-blob`, `queue-operation`, `last-prompt`, `atis-latch` and `cost-state` records. The
synthetic sessions contain only `user`, `assistant` and `last-prompt` records, and the CLI accepted
them.
