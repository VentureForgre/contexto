# M0 L1-a: pass-through measuring proxy and per-request measurements

Status: phase 1 evidence, pass-through only (no request rewriting). Packet M0 L1-a (lane 1, build.builder-1).
Brief: `docs/milestones/M0-infinite-session-study.md`. Date: 2026-10-15.

Every finding is tagged **measured** (with the run that produced it), **documented** (vendor
source with version), or **inferred**. Proxy numbers come from `docs/research/M0-proxy-summary.csv`,
which `spikes/m0/proxy/summarize.py` builds from the local proxy logs. Seat numbers come from
`docs/research/M0-l2d-seat-usage.csv`, built from the seat's session JSONL with
`spikes/m0/proxy/sessionlog.py`. Both files hold numbers and labels only.

## What was built

`spikes/m0/proxy/` (standard library only, Python 3.12+; zstd request decoding needs 3.14):

- `proxy.py`: listens on `127.0.0.1` only and forwards each request to one upstream base URL.
  Bodies and headers pass through unchanged; only hop-by-hop headers and `Host` are replaced, as
  any proxy must. Responses, including SSE, are relayed chunk by chunk as they arrive (a test
  checks that the first event reaches the client before the upstream finishes). WebSocket
  upgrades are tunneled byte for byte. A side copy of each request and response is parsed for
  metrics; the relayed bytes are never touched.
- `extract.py`: content-free metrics. Per request it records the endpoint, model, message count,
  decoded and wire bytes, block types and per-class bytes per role (text, tool_use, tool_result,
  thinking, image), system and tool definition sizes, `cache_control` positions, header names,
  the auth scheme word, `anthropic-beta` flags, and usage from the response (JSON, SSE, or
  WebSocket events). Prefix stability uses HMAC-SHA256 with a random per-run key held in memory
  only. No header values, prompt text, tool output, or credentials are written.
- Safety: a token cap per experiment that refuses new calls with a local error once reached
  (refusal, never modification), and a local control endpoint (`/__m0proxy/turn`, `/status`) so
  a driver can label turns.
- `run_session.py`: throwaway Claude Code or Codex sessions on a seeded synthetic project.
  `drive_seat.py`: the same through a test seat over `rig send`. `summarize.py`: the CSV.
- Optional per-block keyed hashes (`block_hashes=True`): one truncated HMAC per system block, tool
  definition, and message content block, in wire order, plus where `cache_control` sits.
  `first_changed_block()` names the first block that differs between two requests, by type and
  position.
- `sessionlog.py`: the same usage fields from a Claude Code session JSONL, for seats the proxy
  cannot see. Calls are deduplicated by `message.id`; turns start at each user prompt. It has a
  pre-turn gate and post-turn checks (cost over budget, serial reads, compaction markers), and
  exits 3 on a stop. `request_bytes` and `message_count` stay empty for session-file rows: the
  file does not record the wire request.
- `drive_l2d.py`: sends one planned turn to a test seat, waits for it in the session JSONL, and
  applies the stop rules. `stub401.py`: a zero-token routing stub. `cache_test.py`: the cache-miss
  experiment below.
- `test_proxy.py`, `test_sessionlog.py`, and `test_run_session.py`: 25 tests on synthetic
  fixtures and temporary git repos. They cover end-to-end
  transparency, SSE streaming, WebSocket tunneling with masked, fragmented, permessage-deflate
  frames, the cap, block hashing, the session meter, worktree base-URL resolution, and checks that
  a planted secret and planted content never reach the logs.

Output goes to the gitignored `.contexto/spikes/L1-a/`. Only the numbers-only CSV is committed.

## Safety recipe used for live runs

Claude Code (agreed with manager-1 and intake; recipe proof by builder-2): the throwaway child runs
with its working directory under this worktree, so it inherits the seat credential as configured,
and gets a flag-level override `--settings '{"disableAllHooks": true, "env": {"ANTHROPIC_BASE_URL":
"http://127.0.0.1:<port>"}}'` plus `--strict-mcp-config`. Its environment drops this seat's
`CLAUDE*`, `OPENRIG_*`, and `RIGGED_*` variables. The proxy upstream is the base URL the child
would otherwise inherit, read at run time and never written. `inherited_claude_base()` resolves it
the way Claude Code was measured to (finding 1a; `M0-interception.md` section 7). In a linked git
worktree it reads the main checkout's `.claude/settings.local.json` first, found through
`git rev-parse --git-common-dir`; then the worktree's local and project settings, the user
settings, and finally the process env. Runs use `--no-session-persistence`,
so no transcript is written; a throwaway that persists sessions would write one under
`~/.claude/projects`, which stays local and is never committed.

Seat checks after the first recipe turn (**measured**, `.contexto/spikes/L1-a/seat-checks/`): the
seat's resume token (compared by keyed fingerprint), resume provenance, and queue rows were
unchanged. `lastActivity` advanced, but this seat was active itself, so that field cannot
separate the two.

Codex: `codex exec --ignore-user-config --disable hooks -c openai_base_url="http://127.0.0.1:<port>"`,
the inherited ChatGPT sign-in unchanged, and the proxy upstream set to the ChatGPT backend default.

## Findings: Claude Code 2.1.301 (claude-haiku-5-5)

1. **Settings env beats process env** (**measured**, first bare run): with `ANTHROPIC_BASE_URL`
   set only in the child's environment, a settings-file env block won. Model calls bypassed the
   proxy (only an unauthenticated `GET /api/hello`, status 401, reached it). A flag-level
   `--settings` env override is required.
1a. **In a git worktree, the main checkout's local settings also load, and their base URL wins**
   (**measured**). These are zero-token tests against `stub401.py` on `127.0.0.1:18451`. The child
   env was stripped of `CLAUDE*`, `OPENRIG_*`, `RIGGED_*`, and `ANTHROPIC_*`, with
   `CLAUDE_CODE_MAX_RETRIES=0`, and every run used
   `claude -p hi --settings '{"disableAllHooks": true}' --strict-mcp-config --no-session-persistence < /dev/null`.

   | Test | cwd | Base URL source | Stub hits | Outcome |
   |---|---|---|---|---|
   | T1 | `contexto-worktrees/lab-subject` (a worktree whose own `settings.local.json` names the stub) | settings files only | 0 | a real model answered |
   | T2 | a non-git `mktemp` dir whose `.claude/settings.local.json` holds only the stub URL | that file | 3 `POST /v1/messages` | 401 from the stub, 0 tokens |
   | T3 | as T1, plus `--debug --output-format json` | settings files only | 0 | a real model answered |

   T3's debug log watched these settings files:
   - `~/.claude/settings.json`
   - `lab-subject/.claude/settings.json` (missing)
   - `lab-subject/.claude/settings.local.json`
   - `contexto/.claude/settings.local.json`, the **main checkout's** local settings

   The main checkout's file names the rig proxy. So a worktree's own fragment cannot reroute a
   seat while the main checkout's local settings set a base URL. That explains both failed
   attempts to route the `lab.subject` seat through this proxy. Which file wins on precedence is
   inferred from the outcome. T1 and T3 cost real tokens, because without `--model` the child used
   the user default, `claude-fable-5-1`. T3 measured 183,275 (101 input, 43,106 cache write,
   139,281 cache read, 787 output); T1 is unmeasured, estimated the same.
2. **Fixed head** (**measured**, recipe probe run): system 9,584 bytes, 25 tool definitions
   70,196 bytes, first message 17,230 bytes of injected context (user and project instructions,
   environment), 131,097 request bytes in total. The first call's context is **45,925 tokens**
   before any work. A `--bare` child sends 3 tools (3,352 bytes) and a 426-byte system prompt,
   so bare mode does not represent real sessions. An OpenRig seat's head is about 82k to 84k
   (see the `lab.subject` findings).
3. **Per-turn growth**: each agent call resends the full history. In the 1-turn default run,
   messages went 2, 4, 6, 8 and request bytes 118,536 to 125,629 (**measured**). Tool output is
   the dominant growth term: four ~50 KB file reads added 153,195 bytes of tool_result and took
   the context from 45,951 to 136,866 tokens in one call (**measured**, compact run seq 7).
4. **Prompt caching**: `cache_control` sits on `system[1]`, `system[2]`, and the last one or two
   message blocks (**measured**). Steady-state calls read the previous call's prefix from cache
   and write only the new tail.
   - Full miss right after a write (**measured**, default run seq 3): 42,384 tokens written,
     0 read, about 2.5 s after seq 2 had written 42,027. Seq 4 then read 42,027.
     Cause **inferred**: the first write was not yet readable when the next call arrived.
   - Cache reuse across processes (**measured**, compact run seq 2): a new process read 25,982
     head tokens that the previous run had written minutes earlier.
   - **A full miss with a byte-identical prefix** (**measured**, `cache_test.py`). Two conditions
     ran 3 tiny turns each, about 62 s apart, with keyed hashes of every system block, tool
     definition, and message block. Condition (a) ran the lab seat's 4 hook commands with hooks
     enabled; they ran, giving 4 `hook_success` attachments, but no-op without the seat identity.
     Condition (b) disabled all hooks.

     | Condition | Turn | Cache write | Cache read | First changed block vs previous |
     |---|---|---|---|---|
     | a | 1 | 44,694 | 0 | (first) |
     | a | 2 | 11,508 | 33,209 | none |
     | a | 3 | 24 | 44,717 | none |
     | b | 1 | 11,485 | 33,209 | (first; read (a)'s head across processes) |
     | b | 2 | **44,717** | **0** | **none** |
     | b | 3 | 75 | 44,694 | none |

     In (b) turn 2, every block hash equals turn 1's, inside the 5 minute TTL, and nothing was read.
     So misses happen without any prompt change: hook text, timestamps, and git status are ruled out
     for this one. Across all throwaway runs today, 4 of 14 cache-eligible Anthropic calls were full
     misses, one of them a compaction summary call with a different request shape. L1-c (builder-2)
     measured the same on 4 back-to-back seeded sessions: 3 read the cached prefix, and one read 0
     despite an identical shape hash and cache markers within the TTL (doc pending). Route-side
     causes are **inferred** and were not probed. For example, the cache is per organization, so
     requests spread across upstream accounts or replicas would miss. The rig proxy is the owner's
     infrastructure (D11); per-session upstream affinity would be an owner-side setting.
   - Claude Code 2.1.301 puts a `cache_control` breakpoint on `messages[1]`, a message with role
     `system`, on the first turn (**measured**, block hashes).
5. **Auth** (**measured**): the inherited credential arrives as `x-api-key`. Claude Code also calls
   `/v1/messages/count_tokens` (about 35 to 50 KB requests) around compaction.
6. **Compaction (Q2)** (**measured**, compact run, `--autocompact 100000`, `-p` stream-json).
   **This run violated owner decision D3** (`docs/DECISIONS.md`: experiments use default compaction
   settings, with no threshold overrides). It ran before D3 was issued, and it also exceeded the
   500,000-token experiment cap. Its results are kept as evidence: mid-turn compaction can loop,
   and the first compaction call is a full cache miss. No further override runs will be made.
   Compaction fired mid-turn. Its summary call is recognizable by shape: same message count as the
   call before it, no `claude-code-20250219` beta, `end_turn`, thousands of output tokens.

   | seq | call | context tokens | cache write | cache read | output |
   |---|---|---|---|---|---|
   | 7 | agent call with the 4 reads | 136,866 | 90,915 | 45,949 | 976 |
   | 8 | compaction summary 1 | 139,320 | 139,318 | 0 | 5,510 |
   | 9 | after compaction, re-reads | 75,873 | 47,103 | 28,768 | 3,500 |
   | 12 | compaction summary 2 | 78,469 | 52,131 | 26,336 | 8,908 |
   | 13 | agent call | 84,406 | 84,404 | 0 | 4,503 |
   | 14 | compaction summary 3 | 86,968 | 60,630 | 26,336 | 9,342 |
   | 15 | final answer | 38,887 | 10,117 | 28,768 | 353 |

   The CLI emitted three `system:compact_boundary` events in that one user turn. The model
   re-read files after each summary, which refilled the window, so compaction looped. Two
   summaries fired below 100k reported context. The trigger is therefore not reported usage
   alone; it likely also counts pending tool results against a margin below the window
   (**inferred**; builder-2's L1-b covers the mechanism). Each summary call rewrote its prompt
   to cache (seq 8: 139,318 written, 0 read). The turn cost **720,446 tokens**, 5.6 times what
   the same reads cost before the first compaction.

## Findings: OpenRig test seat `lab.subject` (claude-haiku-5-5, interactive)

The seat could not be routed through the proxy (finding 1a above), so it was measured from its own
session JSONL with `sessionlog.py`. Those rows have usage, but no request bytes or message counts.

1. **Fixed head** (**measured**): the first call's context was 81,808 tokens (session `0d475c46`)
   and 84,073 (session `93eadb5d`). A throwaway default-mode child measured 45,925. A real OpenRig
   seat therefore carries about 36k to 38k more, from its onboarding, hooks, and seat context. The
   seat's startup turn alone took 2 calls and 170,162 tokens. A lighter setup is not the owner's
   real case, so a probe point below about 85k is not reachable on a seat.
2. **Cache on the seat path** (**measured**):
   - Session `0d475c46`: 3 of 3 calls were full writes with 0 reads, including a call 70 s after
     the previous write.
   - Fresh session `93eadb5d`: 11 of 14 calls read cache. The full misses were the startup turn's
     second call, 4 s after the first write, and O2. P2's first call read only 98,906 tokens, about
     a minute after G2 had written a 252k prefix; its second call then read 252,074.

   Together with the byte-identical throwaway miss, this points to an intermittent route-side cause
   (**inferred**). Seat misses are expensive: each one bills the full context, 82k tokens or more,
   at cache-write price.
3. **Replies through `rig send` cost a call per turn** (**measured**): delivered with the normal
   message envelope, the seat answered with a Bash `rig send` before ending its turn, so 2 calls per
   short turn. Delivered with `rig send --raw` (no envelope, no reply footer), it answered in its
   terminal in 1 call.
4. **L2d quality-versus-context run** (builder-4's design, lane 2 scores it), session `93eadb5d`,
   default compaction settings (D3), no compaction observed. Per-call numbers are in
   `docs/research/M0-l2d-seat-usage.csv`.

   | Turn | Calls | Tokens | Budget | Largest context |
   |---|---|---|---|---|
   | startup (OpenRig) | 2 | 170,162 | n/a | 85,245 |
   | S0 (with envelope) | 2 | 172,700 | 81,883 | 86,316 |
   | O1 | 1 | 90,820 | 85,063 | 90,357 |
   | O2 | 1 | 92,367 | 86,416 | 92,197 |
   | G1 (1 read) | 2 | 188,814 | 175,794 | 95,158 |
   | P1 (1 Write) | 2 | 205,907 | 184,964 | 99,923 (probe call 98,910) |
   | G2 (9 parallel reads) | 2 | 355,935 | 334,079 | 252,076 |
   | P2 (1 Write) | 2 | 520,244 | 488,342 | 256,961 (probe call 256,783) |
   | Total | 14 | 1,796,949 | | |

   Totals: input 44, cache write 601,942, cache read 1,178,130, output 16,833.

   Run conditions:
   - The logs sat untracked at `lab-subject/l2d/files/`, so the turn texts' relative paths
     resolved from the seat's cwd; answers were written to `lab-subject/l2d/`.
   - S0 went with the rig envelope; O1 onward went with `--raw`.
   - Each checkpoint's growth files were moved out before its probe.
   - Each turn was gated before sending: cumulative plus 1.5 times its budget had to stay under
     1.9M, G2 also had to satisfy builder-4's rule (meter plus 822,000 under 1.9M), and P2 was
     gated on measured context times measured probe calls times 1.1.

   A local check found no planted value in any visible reply. Thinking text is not saved in the
   session file, so restatement inside thinking cannot be checked.

## Findings: Codex CLI 0.154.0 (ChatGPT sign-in, gpt-6-astra)

1. **Base URL** (**measured**): under ChatGPT sign-in, the `OPENAI_BASE_URL` environment variable
   is ignored and requests go straight to the ChatGPT backend. The `openai_base_url` config key,
   passed per process with `-c`, is honored (**documented**: codex-rs `core/src/config/mod.rs`
   at rust-v0.154.0 maps it onto the built-in provider). `gpt-6-luna` is rejected for ChatGPT
   accounts (HTTP 400 from the backend).
2. **Transport** (**measured**): `GET /models`, then a WebSocket upgrade on `/responses` with
   `permessage-deflate`. When the upgrade fails, Codex retries 5 times, then falls back to HTTP
   `POST /responses` with a zstd-compressed body (90,854 bytes decoded, 31,547 on the wire,
   7 input items, the full history).
3. **Server-side state** (**measured**, WebSocket probe): the first `response.create` sends the
   head (2 items, 59,142 bytes) and produces no output, so it looks like a prewarm. The second
   carries `previous_response_id` and only 5 items (32,957 bytes), not the whole conversation.
   `store` is false. Reported usage 11,750 and then 18,521 input tokens, 0 cached. So Codex over
   WebSocket sends deltas against server-side state, while the HTTP fallback resends everything.
4. **Request shape** (**measured**): no top-level `instructions` or `tools`. Instructions travel as
   developer-role messages and tool definitions as one `additional_tools` input item (35,952
   bytes) that also carries a developer role. In the captured log, and so in the CSV, the first
   WebSocket message's `system_bytes` of 57,833 includes that tool item. The developer text alone
   is about 21,900 bytes. The extractor was corrected after capture. Header names include
   `chatgpt-account-id`, `session-id`, `thread-id`, and several `x-codex-*`; values are not
   recorded.
5. **Auth (Q5 flag)** (**measured**): in ChatGPT-login mode the proxy passes the bearer token
   through: inherited, forwarded unchanged, never logged. Flagged to intake for Q5 because it is
   login traffic through a local proxy.

Codex measurement continues on builder-2's `labcodex.subject` seat through session files.

## Usage normalization (lane 2 summary fields)

The summary uses Anthropic field names. Claude Code usage passes through. For OpenAI Responses usage:
`cache_read_input_tokens = input_tokens_details.cached_tokens`;
`input_tokens = input_tokens - cached_tokens`, because OpenAI counts cached tokens inside
`input_tokens` while Anthropic does not; `cache_creation_input_tokens` stays empty, because
Responses does not report cache writes; `output_tokens` passes through and includes reasoning.
The detailed log keeps the raw numbers. Token caps count all four categories, cache reads included.

The CSV adds per call: head sizes, tool_result bytes, context tokens, `cache_full_miss` (0 read
after the first call), `cache_partial` (read below 90% of the previous call's cached prefix),
and `compaction_shape`.

## Token spend

| Experiment | Model | Input | Cache write | Cache read | Output | Total |
|---|---|---|---|---|---|---|
| Bare smoke, 2 turns (proxy bypassed) | haiku-5-5 | not measured | | | | about 13,000 (estimate) |
| Bare smoke, 2 turns | haiku-5-5 | 14 | 2,403 | 9,522 | 1,000 | 12,939 |
| Default, 1 turn (pre-recipe) | haiku-5-5 | 8 | 86,187 | 84,411 | 989 | 171,595 |
| Recipe probe, 1 turn | haiku-5-5 | 2 | 45,923 | 0 | 41 | 45,966 |
| Autocompact 100k, 1 turn | haiku-5-5 | 16 | 504,585 | 182,139 | 33,706 | 720,446 |
| Codex probes (2 rejected, 1 HTTP, 1 WS) | gpt-6-astra | 48,792 | n/a | 0 | 10 | 48,802 |
| Seat seed turn (session `c07642ba`, turn 2; not routed, from the JSONL) | haiku-5-5 | | | | | 364,343 |
| Seat routing check (session `0d475c46`, turn 2; from the JSONL) | haiku-5-5 | 4 | 83,874 | 0 | 4 | 83,882 |
| Routing T1 (unpinned model) | fable-5-1 | not measured | | | | about 183,000 (estimate) |
| Routing T3 (unpinned model) | fable-5-1 | 101 | 43,106 | 139,281 | 787 | 183,275 |
| Cache test (a) | haiku-5-5 | 10 | 56,226 | 77,926 | 12 | 134,174 |
| Cache test (b) | haiku-5-5 | 10 | 56,277 | 77,903 | 37 | 134,227 |
| Packet total | | | | | | about 2,100,000 |
| L2d run (its own approved line, under lane 1) | haiku-5-5 | 44 | 601,942 | 1,178,130 | 16,833 | 1,796,949 |

Two overruns, both reported to manager-1. The autocompact experiment exceeded the 500,000
per-experiment cap inside a single user turn, because the between-turn guard could not stop calls
within that turn; the driver now also gives its budget to the proxy as a hard per-call cap. Routing
tests T1 and T3 ran without `--model` and used the user default model; every throwaway now pins
`--model claude-haiku-5-5`.

## Not done in this packet, and why

- 30+ turn sessions per CLI: a realistic Claude Code session costs about 42k to 80k tokens per
  call (a seat about 85k or more), so 30 turns is roughly 3 to 5M tokens, above the 500k
  experiment cap. The owner replaced the growth run with the L2d quality-versus-context probe
  (compaction is a failure mode to avoid), whose 8 seat turns from 85k to 257k context are above.
- Seat measurement through this proxy: not possible while the main checkout's local settings set a
  base URL (finding 1a). The seat was measured from its session JSONL instead.
- Codex long session: moved to builder-2 (`labcodex.subject`, session files, no proxy).

## Reproduce

```sh
uv run --python 3.14 --with pytest pytest spikes/m0/proxy
cd spikes/m0/proxy
uv run --python 3.14 python run_session.py claude --probe --budget 100000
uv run --python 3.14 python run_session.py codex --probe --budget 60000
uv run --python 3.14 python cache_test.py a && uv run --python 3.14 python cache_test.py b  # M0_LAB_WORKTREE overrides the lab seat path
uv run --python 3.14 python summarize.py ../../../.contexto/spikes/L1-a/<run>... --csv ../../../docs/research/M0-proxy-summary.csv
# Seat runs (session JSONL meter); drive_l2d.py sends one planned turn per call
python3 sessionlog.py status ~/.claude/projects/<escaped seat cwd>/<session>.jsonl
python3 drive_l2d.py <TURN> --session-file <session.jsonl> --plan <l2d plan dir> --log <run>/turns.jsonl
```

Routing tests T1 to T3 use `stub401.py --port 18451 --log <run>/stub.jsonl` and the `claude -p`
command in finding 1a. The `--autocompact` experiment is not reproducible from these scripts on
purpose: decision D3 rules out threshold overrides, so the driver option was removed.
