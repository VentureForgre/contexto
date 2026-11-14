# D70 probes: what Claude Code 2.1.322 sends a gateway and an MCP server

Measured 2026-11-12 on macOS arm64 with the installed `claude` 2.1.322 against stub upstreams on
`127.0.0.1`, at zero model spend (every run used a temporary `CLAUDE_CONFIG_DIR`, a stub API key and
no route off the machine). The probe scripts are in `spikes/m2/serve/`; raw captures were written
under `/tmp` and are not in the repository. D70 in `docs/DECISIONS.md` relies on these results.

Two reports follow, as the two measurement agents wrote them: the HTTP side, then the MCP side. A
third, shorter one at the end was added with the hint-header slice: which values turn a switch on.

---

## D70 spike: what Claude Code 2.1.322 sends over HTTP (stub upstream, zero spend)

Client: `~/.local/bin/claude`, `2.1.322 (Claude Code)`, macOS arm64. 34 runs, 177 requests captured.
Probe: `spikes/m2/serve/http_probe.py`. Captures: `/tmp/d70-http/<run>/NNN-<METHOD>-<kind>.json` (request line, all headers, body), `<run>/index.tsv`, `<run>/setup.json`. Roll-ups: `/tmp/d70-http/aggregate.txt`, `/tmp/d70-http/headers-by-kind.txt` (full header dump per request kind).
Isolation on every run: scrubbed env (fresh HOME, `CLAUDE_CONFIG_DIR`, cwd; `ANTHROPIC_API_KEY=stub-key`; no AUTH_TOKEN / USE_* unless the run sets them) and `sandbox-exec` denying all outbound except localhost, DNS included. So traffic to any other host could not leave and was not observed.

### 1. Path prefix

Yes. Every request to the base-URL stub carried the prefix; none went to the root (`aggregate.txt`: "requests to the base stub NOT under /tok_abc123/ in prefixed runs: none").
Distinct method + path seen with `ANTHROPIC_BASE_URL=http://127.0.0.1:<port>/tok_abc123`:

| Request | When | Prefix kept |
|---|---|---|
| `HEAD /tok_abc123/api/hello` | once at startup, every run (print, pty, bare and not) | yes |
| `POST /tok_abc123/v1/messages?beta=true` | every model request (main, subagent, title, compaction) | yes |
| `POST /tok_abc123/v1/messages/count_tokens?beta=true` | 26 per interactive run, provoked by `/context` | yes |
| `GET /tok_abc123/v1/models?limit=1000` | only with `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1` | yes |

- Short headless run (`claude -p "say hi"`), stub answering 401 or a streamed turn: only `HEAD .../api/hello` and `POST .../v1/messages?beta=true` (`q1-env-401`, `q1-env-sse`, and the `-bare` twins). `--bare` made no difference to the paths.
- Interactive run with a streamed turn, subagent, `/context`, `/compact` (`q2-pty-hint-env`, 39 requests): the first three rows only. No telemetry, feature-flag, quota-check or other `/api/...` path reached the stub.
- Base URL given three ways, all identical result: process env (`q1-env-sse`), `--settings` file `env` (`q1-flag-sse`), `<CLAUDE_CONFIG_DIR>/settings.json` `env` (`q1-cfg-sse`); also with `--bare` (`q1-*-bare`; `--bare` still read the config dir's settings.json env).
- Precedence when set in several places (not asked, measured): `--settings` env beats config-dir settings.json env beats process env. `q1-precedence-all`: `POST /from_flag_settings/v1/messages?beta=true`; `q1-precedence-env-cfg`: `POST /from_config_settings/v1/messages?beta=true` although the process env said `/from_process_env`.
- Trailing slash (`.../tok_abc123/`, `q1-slash`): same paths, no double slash: `POST /tok_abc123/v1/messages?beta=true`.
- Query string on the base URL breaks it (`q1-query`, `q1-query2`): `HEAD /tok_abc123?beta=true/api/hello` and `POST /tok_abc123?beta=true%2Fv1%2Fmessages%3Fbeta%3Dtrue` (the stub's 404 surfaced as "There's an issue with the selected model", exit 1). The `/v1/messages` path is swallowed into the query.
- No prefix (`q1-noprefix`): `HEAD /api/hello`, `POST /v1/messages?beta=true`.

### 2. Hint headers

Docs: fetched `https://code.claude.com/docs/en/llm-gateway-protocol.md` in full (WebFetch and curl, HTTP 200, 53084 bytes, saved as `/tmp/d70-http/doc-llm-gateway-protocol.raw.md`). Nothing failed. Quotes:
- "Custom base URL: off by default, because a proxy that rejects unknown headers would fail the request. To receive them, set `CLAUDE_CODE_GATEWAY_HINT_HEADERS=1` for your developers, for example in the `env` block of managed settings". "Setting `CLAUDE_CODE_GATEWAY_HINT_HEADERS` to `0` stops the headers on every connection." "Requires Claude Code v2.1.273 or later."
- `x-claude-code-request-class`: "`main` for a turn of the main conversation, `subagent` for a turn of a subagent, `workflow` for an agent running inside a workflow, `compaction` for the summarization request that compacts a conversation, or `auxiliary` for side requests such as session titles, classifiers, and summaries. Sent on every request"
- `x-claude-code-context-compacted`: "Present once, on the first main-conversation request after a compaction, with the same values as `x-claude-code-compaction`. The conversation prefix before this request is no longer used, so a cache keyed on it can be dropped"
- `x-claude-code-compaction`: "Present on the request that summarizes the conversation during a compaction. The value says what triggered it: `auto` ..., `manual` for `/compact`, or `reactive` when the API rejected a request as too long. ... Absent on every other request, including one that writes a summary in the background before a compaction"
- `x-claude-code-prompt-id`: "Random UUID that identifies the user prompt a request serves. Requests serving one prompt share the value, including the turns of subagents that prompt started. Requests not attributed to a prompt omit it. ... Requires Claude Code v2.1.283 or later"
- Not gated by the variable (the "Request headers" table): `x-claude-code-session-id` "A unique identifier for the current Claude Code session. Use it to aggregate all requests from one session without parsing request bodies"; `x-claude-code-agent-id` "present only on requests from an agent Claude Code spawned inside the session"; `x-claude-code-parent-agent-id` "present only for nested agents".
- Also hint headers: `x-claude-code-agent-type` ("Present only on a subagent's own turns") and `x-claude-code-prev-tool-durations` (`Bash=742;Read=9`).
- Paths: "Inference requests post to `/v1/messages?beta=true`"; "receives a `HEAD /api/hello` connection-warming probe, which Claude Code skips when an HTTP proxy or client certificate is configured"; "`GET /v1/models?limit=1000`".

Measured with `CLAUDE_CODE_GATEWAY_HINT_HEADERS=1` in the process env, interactive (`q2-pty-hint-env`; all on path `/tok_abc123/v1/messages?beta=true` unless noted; session-id is the same UUID on every POST):

| Request (file) | request-class | compaction / context-compacted | prompt-id | session-id | agent-id | user-agent | body `tools` |
|---|---|---|---|---|---|---|---|
| `HEAD /tok_abc123/api/hello` (001) | absent | absent | absent | absent | absent | `Bun/1.4.4` | no body |
| session title (002, 008, 038) | `auxiliary` | absent | absent | present | absent | `claude-cli/2.1.322 (external, cli)` | key present, empty list |
| main turn (003, 005, 007, 009) | `main` | absent | present, one per prompt | present | absent | same | 13 tools |
| subagent turn (004, 006) | `subagent` | absent | present, equal to the main prompt's | present | `a800b6f5b41866e3f` | same | 8 tools |
| `.../v1/messages/count_tokens?beta=true` (010-035) | `auxiliary` | absent | absent | present | absent | same | 0, 1 or 12 tools |
| `/compact` summarization (036, 037) | `compaction` | `x-claude-code-compaction: manual` | absent | present | absent | same | 13 tools |
| first main turn after compaction (039) | `main` | `x-claude-code-context-compacted: manual` | present (new) | present | absent | same | 13 tools |

- Subagent requests also carried `x-claude-code-agent-type: general-purpose`; the request after a tool batch carried `x-claude-code-prev-tool-durations` (`Agent=4` on main 005, `Bash=30` on sub 006).
- Via the settings `env` block the result is the same: `--settings` file (`q2-print-hint-flag-agent`) and config-dir settings.json (`q2-print-hint-cfg-agent`) both give `class=main` / `class=subagent`, prompt-id, agent-type on headless runs. Headless user-agent is `claude-cli/2.1.322 (external, sdk-cli)`.
- Compaction provoked headless too: stream-json input `first turn hello`, `/compact`, `hello after compact` (`q2-streamjson-compact`): request 3 `class=compaction`, `x-claude-code-compaction: manual`; request 4 `class=main`, `x-claude-code-context-compacted: manual`.
- The Agent tool ran in the background in 2.1.322 (tool result "Async agent launched successfully"). The main turn triggered by the agent's completion notification carried a NEW prompt-id, different from the prompt that started the agent (`q2-print-hint-env-agent` 006: `233bb4d2...` against `c484ea9c...` on 002-005).
- The compaction request is otherwise shaped like a main turn (same tools, same system prompt); its last user block contains "CRITICAL: Respond with TEXT ONLY". When the stub's reply held no `<summary>`, a second `compaction` request followed 6 ms later (036, 037); with a `<summary>` reply there was one.
- `GET .../v1/models?limit=1000` with hints on (`q2-pty-models-hint` 002): no hint headers, no session id; headers are `User-Agent: claude-code/2.1.322`, `anthropic-version`, `x-api-key`.

WITHOUT the variable (`q2-pty-nohint`, `q2-print-nohint-agent`), same script:
- Absent on every request: `x-claude-code-request-class`, `x-claude-code-compaction`, `x-claude-code-context-compacted`, `x-claude-code-prompt-id`, `x-claude-code-agent-type`, `x-claude-code-prev-tool-durations`.
- Still present: `X-Claude-Code-Session-Id` on every POST, and `x-claude-code-agent-id` on subagent requests (`a9605a5961933af40`).
- `CLAUDE_CODE_GATEWAY_HINT_HEADERS=0` (`q2-print-hint-zero`): identical to unset.

### 3. Requests with no session id

- No `X-Claude-Code-Session-Id` header and no body: `HEAD /tok_abc123/api/hello` (21 of 21) and `GET /tok_abc123/v1/models?limit=1000` (2 of 2).
- Header present but no `metadata` in the body: `POST /tok_abc123/v1/messages/count_tokens?beta=true` (52 of 52; body keys are `messages`, `model`, `tools`).
- Every `POST /tok_abc123/v1/messages?beta=true` (67 of 67) had both the header and `metadata.user_id`, a JSON string `{"device_id":..., "account_uuid":"", "session_id":"<same UUID as the header>"}`.
- Evidence: `/tmp/d70-http/aggregate.txt` ("NO x-claude-code-session-id header", "NO session_id in body metadata.user_id").

### 4. Cloud providers

From the docs (`/tmp/d70-http/docs/env-vars.md`, `llm-gateway-protocol`, `amazon-bedrock.md`, `claude-platform-on-aws.md`):

| Switch | Base URL used instead | Measured path at the provider stub (run) |
|---|---|---|
| `CLAUDE_CODE_USE_BEDROCK` | `ANTHROPIC_BEDROCK_BASE_URL` | `GET /bedrock_prefix/inference-profiles?type=SYSTEM_DEFINED`, `POST /bedrock_prefix/model/{model}/invoke`, `.../invoke-with-response-stream` (`q4-bedrock`) |
| `CLAUDE_CODE_USE_MANTLE` | `ANTHROPIC_BEDROCK_MANTLE_BASE_URL` | `POST /mantle_prefix/v1/messages`, `POST /mantle_prefix/v1/messages?beta=true` (`q4-mantle`) |
| `CLAUDE_CODE_USE_VERTEX` | `ANTHROPIC_VERTEX_BASE_URL` | `POST /vertex_prefix/projects/fake-project/locations/us-east5/publishers/anthropic/models/claude-opus-5-5:streamRawPredict` (`q4-vertex`) |
| `CLAUDE_CODE_USE_FOUNDRY` | `ANTHROPIC_FOUNDRY_BASE_URL` (or `ANTHROPIC_FOUNDRY_RESOURCE`) | `POST /foundry_prefix/v1/messages?beta=true` (`q4-foundry`) |
| `CLAUDE_CODE_USE_ANTHROPIC_AWS` | `ANTHROPIC_AWS_BASE_URL` | `POST /aws_prefix/v1/messages?beta=true` (`q4-anthropic-aws`) |

- In all five runs `ANTHROPIC_BASE_URL` still pointed at the first stub, and it received zero requests, not even `HEAD /api/hello`. All runs stayed local (second stub on 127.0.0.1, sandbox).
- The switch also wins when it is set only in the config dir's settings.json `env` while `ANTHROPIC_BASE_URL` is in the process env (`q4-bedrock-in-cfg-settings`: 5 requests, all at the Bedrock stub).
- Doc precedence: "Amazon Bedrock and Microsoft Foundry take precedence in provider routing" over Claude Platform on AWS.
- `CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST` (docs): "Claude Code ignores provider-selection, endpoint, and authentication variables such as `CLAUDE_CODE_USE_BEDROCK`, `ANTHROPIC_BASE_URL`, and `ANTHROPIC_API_KEY` in settings files". Not measured.
- On Bedrock with hints on, the streamed turn had `x-claude-code-request-class: main`, but three `max_tokens: 1` probe requests (`.../invoke`, message ".") had no request-class header (`q4-bedrock` 002-004).

### 5. Origin and Host

- `Origin`: never sent. 0 of 177 requests (`aggregate.txt`).
- `Host`: the base URL's host and port verbatim. `Host: 127.0.0.1:<port>` on 175 requests; with `ANTHROPIC_BASE_URL=http://localhost:<port>/tok_abc123` it was `Host: localhost:<port>` (`q5-localhost`, both requests). The port is always included.
- Model requests do carry `anthropic-dangerous-direct-browser-access: true`, with no Origin.

### Not confirmed

- claude.ai (OAuth) login sessions: not run, they need a real credential. All runs used an API key. Which requests an OAuth session sends to the base URL is unmeasured.
- Traffic to hosts other than the stub: blocked by the sandbox and not recorded, so I cannot say what Claude Code tried to send elsewhere. Static strings in the binary show a fixed `BASE_API_URL:"https://api.anthropic.com"` used for `/api/oauth/...`, `/api/claude_code/metrics`, `/api/claude_code/settings`, `/v1/mcp_servers`, `/v1/code/sessions`; that is read from minified code, not measured. The docs say the fast-mode check and the WebFetch domain check "call `api.anthropic.com` directly".
- A feature gated on a flag fetched from another host may have stayed off because that fetch was blocked; any base-URL request it would make is unobserved.
- `request-class` values `workflow`, compaction triggers `auto` and `reactive`, `x-claude-code-parent-agent-id` (nested agents), and a subagent's own side or compaction requests: not provoked.
- `--bare` has no Agent tool (3 tools), so no subagent request in bare mode (`q2-print-hint-flag-agent-bare`).
- `HEAD /api/hello` under an HTTP proxy or client certificate (docs say it is skipped): not tested.
- No startup quota-check request appeared in any run; the only tool-less model request seen was the session title.

Side effect to know about: a lint attempt (`uv run ruff`) created a git-ignored `.venv/` in the worktree. Nothing else outside `spikes/m2/serve/http_probe.py` and `/tmp` was written; git state untouched.

---

## D70 spike: what Claude Code tells an MCP server it starts

Claude Code 2.1.322 (`~/.local/bin/claude`), macOS, measured 2026-11-12/13. Zero spend: every run used a
scrubbed environment, a temporary `HOME` and `CLAUDE_CONFIG_DIR`, `ANTHROPIC_API_KEY=stub-key` and a
stub upstream on 127.0.0.1. All 72 captured model requests carry `x-api-key: stub-key` and nothing else.

Scripts: `spikes/m2/serve/` (`recording_mcp.py`, `stub_upstream.py`, `mcp_probe.py`, `mcp_show.py`,
`hook_record.py`, `recording_http_mcp.py`, `headers_helper.py`, `interactive_probe.py`; the runs are
`runs.sh`, `q8_registration.sh`, `q9_http.sh`). Captures: `/tmp/d70-mcp/<run>/` (`mcp.jsonl` server log,
`stub/NNN-*.json` model requests with headers, `hooks.jsonl`, `http.jsonl`, `start-env.json`); a short
reading of each run is `/tmp/d70-mcp/<run>.show`. "Session id" below means the `X-Claude-Code-Session-Id`
the stub saw.

### 1. Environment of the stdio server
- The session id IS in the environment: `CLAUDE_CODE_SESSION_ID` equals the stub's header value in every
  run (bare, not bare, interactive, user scope, `--mcp-config`). No other variable holds it.
- Added by claude, `-p` not bare (`q3-turns/mcp.jsonl`): `AI_AGENT=claude-code_2-1-322_harness`,
  `CLAUDECODE=1`, `CLAUDE_CODE_ENTRYPOINT=sdk-cli` (`cli` when interactive), `CLAUDE_CODE_MESSAGING_SOCKET=/tmp/cc-socks/<claude pid>.sock`,
  `CLAUDE_CODE_MESSAGING_TOKEN`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_PROJECT_DIR`, `COREPACK_ENABLE_AUTO_PIN=0`,
  `LC_CTYPE`, `NoDefaultCurrentDirectoryInExePath=1`, `__CF_USER_TEXT_ENCODING`. With `--bare` (`q3-bare`):
  the same minus the two MESSAGING variables, plus `CLAUDE_CODE_SIMPLE=1`.
- Nothing removed or changed: every variable claude was started with reaches the server, including
  `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY` and `CLAUDE_CONFIG_DIR`.
- Working directory = the project directory; `CLAUDE_PROJECT_DIR` = the same path (resolved, `/private/tmp/...`).
- Settings `env` block: passed (`D70_SETTINGS_ENV`). The server entry's own `env`: passed (`D70_MCP_CONFIG_ENV`).
- Not in the server's environment: `CLAUDE_PID`, `CLAUDE_CODE_CHILD_SESSION` (hooks and Bash get those). The
  server's parent pid is the claude pid in every run.
- The variable is set once, at spawn. After `/clear` it is stale (question 6).

### 2. `initialize`
- First message is `server/discover` (id `server-discover-probe-1`) with `params._meta`:
  `io.modelcontextprotocol/protocolVersion: "2026-07-28"`, `.../clientInfo`, `.../clientCapabilities`. My
  server answered method-not-found and claude fell back to `initialize`.
- `initialize` params, complete: `protocolVersion: "2025-11-25"`, `capabilities: {roots: {listChanged: true},
  elicitation: {form: {}, url: {}}}`, `clientInfo: {name: "claude-code", title: "Claude Code", version:
  "2.1.322", description: "Anthropic's agentic coding tool", websiteUrl: "https://claude.com/claude-code"}`.
  No `_meta`, no session id in either message. Then `notifications/initialized`, `tools/list`.

### 3. `tools/call`
- Complete request (`q3-bare/mcp.jsonl`): `{"method":"tools/call","params":{"name":"probe_echo","arguments":
  {"note":"first of two"},"_meta":{"claudecode/toolUseId":"toolu_01D70001axQ7mZp4LkTr9VbYs","progressToken":2}},
  "jsonrpc":"2.0","id":2}`.
- `_meta` holds exactly two keys every time: `claudecode/toolUseId` and `progressToken` (equal to the
  JSON-RPC id). No session id, no agent id, nothing else.
- `claudecode/toolUseId` is exactly the `tool_use` id the stub sent, in all calls: two calls in one turn
  (`...001a...`, `...001b...`), a call in a second turn of the same process (`q3-turns`, 3 calls, one server
  pid), bare and not bare, interactive, after `/clear`, after `--resume`.
- Not bare, tool search is on: `mcp__probe__probe_echo` is not in the request's `tools` (it is named in the
  deferred-tools reminder in `messages`), and claude still executed the stub's `tool_use` for it directly.

### 4. Subagent (`q4-sub`, `q7-sub`; foreground Agent call in `-p`)
- The subagent's call reached the same server process (one `start` record, one pid). `_meta` has the same
  two keys; nothing marks the call as a subagent's.
- The subagent's model requests carry the main session's `X-Claude-Code-Session-Id` and
  `x-claude-code-agent-id: a381b760dc594316d` (main requests have no agent id header).
- A `PreToolUse` hook for that call does get `agent_id` (the same value as the header) and
  `agent_type: "general-purpose"`, with `session_id` and `tool_use_id`.

### 5. Two claude processes, one config (`q5-two`)
- Each started its own server: server pids 2 distinct, each with parent pid = its own claude pid, each
  with its own `CLAUDE_CODE_SESSION_ID` matching its own header value.

### 6. Session id control and change
- `-p --session-id 11111111-2222-4333-8444-555555555555`: header is that uuid; server env has it (`q6-sid`).
- `-p --resume <id>` from the same config dir and directory: header is the old id, the request holds the
  earlier turn, server env `CLAUDE_CODE_SESSION_ID` is the old id (`q6-resume`). `initialize` and `_meta`
  carry no hint. `--resume <id> --fork-session`: a new id in header and env (`q6-fork`).
- `/clear`, headless (`q6-clear`: stream-json input, a user message whose content is `/clear`): it works.
  stdout emits `conversation_reset` and a new `system init`; the header's session id changes; the request
  after it holds no earlier history. The MCP server process survives (same pid, one `start` record) and
  receives nothing between the two `tools/call`s: no notification, no re-initialize. Its
  `CLAUDE_CODE_SESSION_ID` therefore still holds the id from before the `/clear`.
- `/clear`, interactive in a pty (`q6-interactive`, plain `claude`, server at user scope): same result.
  One server process, header id changed from `d61fcbeb...` to `eb36a394...`, server env still `d61fcbeb...`.

### 7. Hooks
- `SessionStart` runs in `-p` (not bare) and interactively. Stdin JSON: `session_id`, `transcript_path`,
  `cwd`, `hook_event_name`, `source` (`startup`, `resume`, `clear` all observed). On `/clear` it fires again
  with the NEW `session_id` (`q7-hooks/hooks.jsonl`, `q6-interactive/hooks.jsonl`); on `--resume` with the
  resumed id (`q7-resume`).
- Its environment has `CLAUDE_ENV_FILE=$CLAUDE_CONFIG_DIR/session-env/<session id>/sessionstart-hook-0.sh`
  (`PreToolUse` and `PostToolUse` did not get it), plus `CLAUDE_CODE_SESSION_ID` (current, also after `/clear`), `CLAUDE_PID`
  (the claude pid = the MCP server's parent pid), `CLAUDE_PROJECT_DIR`, `CLAUDE_CODE_CHILD_SESSION=1`.
- A line `export D70_FROM_ENV_FILE=<id>` written to `CLAUDE_ENV_FILE` reached later Bash tool commands
  (`q7-hooks/work/bash-env.txt`, the new value after `/clear`). It did not reach the MCP server's
  environment (0 matches in `mcp.jsonl`). Bash commands also get the current `CLAUDE_CODE_SESSION_ID`.
- Order at startup: in `-p` the server's `start` record and the hook's record are within 0.01 s of each
  other, either order; interactively the hook finished 0.09 s before the server started.
- With `--bare`: no hook ran (0 records, `q7-hooks-bare`), the tool call still happened.
- `PreToolUse` for the MCP tool: `session_id`, `tool_use_id` (the stub's id), `tool_name`
  `mcp__probe__probe_echo`, `tool_input`, `prompt_id`, `permission_mode`, `mcp_server: {name: "probe", source:
  "dynamic" | "user"}`, and `agent_id`/`agent_type` in a subagent. It completes before the server gets the
  `tools/call` (same 10 ms). `PostToolUse` carries the same ids plus `tool_response`.

### 8. Registration without flags (`/tmp/d70-mcp/q8.out`; plain `claude -p`, no `--mcp-config`)
- `claude mcp add-json --scope user probe '<json>'`: wrote `$CLAUDE_CONFIG_DIR/.claude.json`, top-level
  `mcpServers.probe = {type: "stdio", command, args}` (copy: `q8-addjson/claude.json.after-add-json`). The
  next `claude -p` started the server and called the tool (`q8-user`). With tool search on the tool is named
  in the request but not in `tools`; with `ENABLE_TOOL_SEARCH=false` it is in `tools` (`q8-user-nosearch`).
- `claude mcp add --scope user`: same place. `--scope local`: `.claude.json` under
  `projects[<cwd>].mcpServers`. `--scope project`: `<cwd>/.mcp.json` (`q8-add`).
- A hand-written `$CLAUDE_CONFIG_DIR/.claude.json` holding only `mcpServers`: loaded (`q8-handwritten`).
- Project `.mcp.json`: loaded and called in `-p` with no approval setting at all (`q8-mcpjson`), and also
  with `enableAllProjectMcpServers` and with `enabledMcpjsonServers`. No approval blocked `-p`.
- A settings file cannot declare a server: `mcpServers` in `$CLAUDE_CONFIG_DIR/settings.json` and in a
  `--settings` file were both ignored (server never started; `q8-settings-user`, `q8-settings-flag`).

### 9. HTTP transport (`q9-headers`, `q9-helper`, `q9-user-helper`; `http.jsonl`, readings in `*.show`)
- Headers on `initialize`: `Accept: application/json, text/event-stream`, `Accept-Encoding: identity`,
  `Content-Type: application/json`, `User-Agent: claude-code/2.1.322 (sdk-cli)`, the configured headers,
  `Connection`, `Host`, `Content-Length`. On `tools/call`: the same plus `mcp-protocol-version: 2025-11-25`
  and `mcp-session-id` (the value my server issued on `initialize`). The first `server/discover` POST also
  has `mcp-method: server/discover` and `mcp-protocol-version: 2026-07-28`. Claude also opens `GET /mcp`.
- The Claude Code session id is in no header and no body, before or after `/clear`. After `/clear` the same
  `mcp-session-id` continues; there is no reconnect.
- `headers` expands `${VAR}` from claude's start environment and from the settings `env` block, and
  `${VAR:-default}`. `${CLAUDE_CODE_SESSION_ID}` is NOT expanded: the literal text was sent and the debug
  log says "Header values reference unset environment variables: CLAUDE_CODE_SESSION_ID".
- `headersHelper` exists and ran, both from `--mcp-config` and at user scope. It ran once per claude
  process (at connect, not per request, not after `/clear`); its headers were on all 7 requests. Its
  environment has `CLAUDE_CODE_MCP_SERVER_NAME`, `CLAUDE_CODE_MCP_SERVER_URL` and the settings `env`, but no
  `CLAUDE_CODE_SESSION_ID`, `CLAUDECODE` or `CLAUDE_PID`; its parent pid is the claude pid; stdin is left
  open and empty (a helper that reads stdin to EOF hits the 10 s timeout and is dropped).
- `tools/call` body `_meta` over HTTP: the same two keys, `claudecode/toolUseId` and `progressToken`.

### Not confirmed
- Interactive approval of a project `.mcp.json` server: only `-p` was run. The pty run used user scope.
- `/resume` inside a running interactive session and `--continue`: not run.
- Background subagents, subagents in interactive mode, a subagent calling an HTTP server: not run.
- When `headersHelper` re-runs: the binary has the text "re-running headersHelper and retrying once" and a
  trust refusal for project-declared helpers ("this workspace has no persisted trust"); neither was triggered.
- The docs page was not fetched; `headersHelper` and `CLAUDE_CODE_SESSION_ID` are reported from behaviour only,
  so I cannot say whether either is a documented, stable interface. Strings in the binary also name
  `_meta` keys `claudecode/agentId` and `claudecode/agentType`; they never appeared in a captured call.
- macOS only, one Claude Code version.

---

## D70 follow-up: which values Claude Code 2.1.322 reads as on

Measured 2026-11-12 for the hint-header and cloud-provider slice, with `spikes/m2/serve/q10_truthy.sh`
(36 runs of `http_probe.py`, each one `claude -p "say hi"`; same isolation as the HTTP report above:
scrubbed environment, temporary config directory, `stub-key`, fake AWS values, a sandbox that allows
only localhost). Captures under `/tmp/d70-truthy/<run>/`, not in the repository. "Base" is the stub
named by `ANTHROPIC_BASE_URL`, "provider" the second stub named by the provider's own base URL
variable.

### 1. `CLAUDE_CODE_USE_BEDROCK` in the process environment (12 runs)

| Value | Requests at the provider stub | Requests at the base stub | Read as |
|---|---|---|---|
| `1`, `true`, `yes`, `on`, `TRUE`, `" 1 "` (padded with spaces) | 5 each | 0 | on |
| `0`, `false`, empty string, `no`, `2`, `enabled` | 0 | 2 each (`HEAD /api/hello`, `POST /v1/messages?beta=true`) | off |

The 5 requests are the ones of the HTTP report's section 4 (`GET .../inference-profiles`, three
`.../invoke` probes, one `.../invoke-with-response-stream`). This is the rule read from the 2.1.314
and 2.1.318 binaries for `CLAUDE_CODE_IGNORE_PERSISTED_TRUST` (D52):
`["1", "true", "yes", "on"].includes(String(v).toLowerCase().trim())`.

### 2. The same switch in the config directory's `settings.json` `env` block (6 runs)

- `"true"`: 5 requests at the provider stub, 0 at base. `"0"`: 0 and 2.
- A JSON `true` and the number `1` (the base URL in the same file, so a rejected file would show as
  no request at either stub): 5 at the provider stub each. Claude Code takes them as their text.
- Environment `1` with settings `0`: 2 requests at base, 0 at the provider. Environment `0` with
  settings `1`: 5 at the provider. The settings file's value wins in both directions.

### 3. The other four switches (8 runs)

`CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`, `CLAUDE_CODE_USE_MANTLE` and
`CLAUDE_CODE_USE_ANTHROPIC_AWS`, each with `true` and with `0`, with the fake provider settings of the
HTTP report's section 4. `true`: 1, 1, 2 and 1 requests at the provider stub and 0 at base. `0`: 0 at
the provider and 2 at base, for all four.

### 4. `CLAUDE_CODE_GATEWAY_HINT_HEADERS` (10 runs)

On means the Messages request carried `x-claude-code-request-class: main`.

- Process environment: `true`, `yes`, `on`, `TRUE` on; `false`, `2` off (`1` and `0` are in the HTTP
  report, section 2).
- Environment `1` with config-directory settings `0`: off. Environment `0` with settings `1`: on.
- A `--settings` file saying `1` with the environment at `0`: on. A `--settings` file saying `1` with
  the config directory's settings at `0`: on. So the value `contexto run claude` passes wins over a
  user's `0` in either place.

### Not confirmed

- The four other provider switches with values other than `1`, `true` and `0`.
- Managed settings as the place a switch is set; `CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST`. (Project
  settings files were not measured in these 36 runs; the next section measures them.)
- Linux, and any other Claude Code version.

---

## D70 review round: value shapes and project settings files

Measured 2026-11-12 after the review of the hint-header and guard slice, with the `shapes` and
`project` groups of `spikes/m2/serve/q10_truthy.sh` (23 runs of `http_probe.py`, each one
`claude -p "say hi"` with Claude Code 2.1.322; the same isolation as above). Zero spend. "Provider"
and "base" are the two stubs as above.

### 1. `CLAUDE_CODE_USE_BEDROCK` as a JSON value that is not a plain string (15 runs)

In the config directory's `settings.json` `env` block, with the base URL in the same file, so a file
Claude Code rejected as a whole would show as no request at either stub. None did.

| JSON value | Provider | Base | Read as | JavaScript `String()` of it |
|---|---|---|---|---|
| `1.0` | 5 | 0 | on | `1` |
| `1e0` | 5 | 0 | on | `1` |
| `["1"]` | 5 | 0 | on | `1` |
| `[1]` | 5 | 0 | on | `1` |
| `[["1"]]` | 5 | 0 | on | `1` |
| `[true]` | 5 | 0 | on | `true` |
| `" On "` | 5 | 0 | on | ` On ` (lower-cased and trimmed) |
| `"1"` in a file that starts with a UTF-8 byte order mark | 5 | 0 | on | `1` |
| `false` | 0 | 2 | off | `false` |
| `1.5` | 0 | 2 | off | `1.5` |
| `[]` | 0 | 2 | off | empty |
| `["1","1"]` | 0 | 2 | off | `1,1` |
| `null` | 0 | 2 | off | `null`, or not set |
| `{"a":1}` | 0 | 2 | off | `[object Object]` |
| `"1.0"` | 0 | 2 | off | `1.0` |

Every row agrees with `["1", "true", "yes", "on"].includes(String(v).toLowerCase().trim())` applied
to the parsed JSON value. `contexto.upstream.js_string` reproduces that coercion.

### 2. Project settings files (8 runs)

The working directory had no trust record; `claude -p` asks for none.

| Run | Provider | Base | Hint class sent | Reading |
|---|---|---|---|---|
| `CLAUDE_CODE_USE_BEDROCK: "1"` in `.claude/settings.json` | 5 | 0 | | a project file's switch is applied |
| the same in `.claude/settings.local.json` | 5 | 0 | | the same |
| project file `"0"`, process environment `1` | 0 | 2 | | the project file wins over the environment |
| project file `"1"`, `CLAUDE_CODE_IGNORE_PERSISTED_TRUST=1` | 0 | 2 | | `claude -p` drops the project file's `env` (as D52 measured for the base URL) |
| `CLAUDE_CODE_GATEWAY_HINT_HEADERS: "1"` in `.claude/settings.json` | 0 | 2 | `main` | a project file turns the hints on |
| project file `"0"`, process environment `1` | 0 | 2 | none | the project file wins over the environment |
| project file `"0"`, `--settings` file `1` | 0 | 2 | `main` | the `--settings` value wins over a project file |
| project file `"1"`, config-directory settings `0` | 0 | 2 | `main` | a project file wins over the user file |

### 3. Measured by the reviewers of pull request #271, not re-run here

Reported in the review with the same kind of setup (real `claude` 2.1.322, loopback stubs, temporary
config directories, zero spend):

- With the process environment at `1` and a project file at `"0"`, Claude Code still went to the
  provider when that project file also held `"x": NaN`, when it also held `"permissions": "garbage"`
  (a schema error), and when the claude arguments had `--setting-sources user`. Claude Code did not
  apply the file in those three cases. `1.0` and `["1"]` in a project file, `1e0` in a project file,
  and a byte order mark in a project file were on.
- Claude Code applies the `env` block of a `--settings` file to its own environment: a Bash command
  of a session started with `CLAUDE_CODE_GATEWAY_HINT_HEADERS=1` in that block printed the variable
  as `1`. A nested `claude -p` pointed at another base URL then sent `x-claude-code-request-class`
  and `x-claude-code-prompt-id` to it.

### Not confirmed

- Managed settings: no run used a managed settings file, so that it outranks the other files and
  the `--settings` file is from Claude Code's documentation only.
- Whether the interactive CLI applies a project file's provider switch after its trust dialog while
  `CLAUDE_CODE_IGNORE_PERSISTED_TRUST` is set (D52 measured that for the base URL).
- Value shapes for the four other provider switches and for the hint variable; a settings file
  with JSON syntax that Python rejects (a trailing comma, a comment).
- Linux, and any other Claude Code version.

## D70 review round 4: which settings files Claude Code loads

Script: `spikes/m2/serve/q11_decoding.py`. Each run writes one settings file, the config
directory's `settings.json` ("user") or the working directory's `.claude/settings.json`
("project"). Then it runs one `claude -p` under the localhost-only sandbox profile of
`http_probe.py`, with this setup:

- a fresh `HOME`, `CLAUDE_CONFIG_DIR`, `CONTEXTO_HOME` and working directory, all outside any
  checkout;
- the key `stub-key` and fake AWS values;
- three loopback stubs: **env** (`ANTHROPIC_BASE_URL` in the process environment, and the
  launcher's `--upstream`), **file** (a base URL that only the settings file names) and
  **Bedrock** (`ANTHROPIC_BEDROCK_BASE_URL`).

Zero spend. In all, 25 runs of `claude` 2.1.322 alone and 19 of `contexto run --upstream <env stub>
claude -p` with the real `claude`, from this branch. Every case that ran more than once gave the
same counts each time. "UTF-16LE" below always starts with its byte order mark `FF FE`.

### 1. Claude Code alone (21 cases)

| Case | Env | File | Bedrock | Hint class sent | Reading |
|---|---|---|---|---|---|
| UTF-16LE, switch `"1"`, a lone high surrogate in another value | 0 | 0 | 5 | | loaded |
| the same with a lone low surrogate | 0 | 0 | 5 | | loaded |
| UTF-16LE, switch `"1"`, one odd byte at the end | 0 | 0 | 5 | | loaded |
| UTF-16LE, both, project file | 0 | 0 | 5 | | loaded |
| UTF-16LE, both, switch `"0"`, environment `1` | 2 | 0 | 0 | | loaded: its `"0"` wins |
| UTF-16LE, both, base URL | 0 | 2 | 0 | | loaded: its base URL is used |
| UTF-16LE, both, hint variable `"1"` | 2 | 0 | 0 | `main` | loaded |
| UTF-16LE with a `//` comment, base URL | 2 | 0 | 0 | | not loaded |
| base URL beside `"x": NaN` | 2 | 0 | 0 | | not loaded |
| the same with `Infinity` | 2 | 0 | 0 | | not loaded |
| the same with `[-Infinity]` | 2 | 0 | 0 | | not loaded |
| base URL beside `NaN`, project file | 2 | 0 | 0 | | not loaded |
| switch `"1"` beside `NaN` | 2 | 0 | 0 | | not loaded |
| switch `"0"` beside `NaN`, environment `1` | 0 | 0 | 5 | | not loaded |
| hint variable `"1"` beside `NaN` | 2 | 0 | 0 | none | not loaded |
| base URL beside the string `"NaN"` | 0 | 2 | 0 | | loaded |
| base URL, plain UTF-8 (control) | 0 | 2 | 0 | | loaded |
| UTF-8 with a Latin-1 `é` byte in another value, switch `"1"` | 0 | 0 | 5 | | loaded |
| the same, switch `"0"`, environment `1` | 2 | 0 | 0 | | loaded: its `"0"` wins |
| the same with a base URL | 0 | 2 | 0 | | loaded |
| switch set to the overlong bytes `C0 B1` (an overlong `1`) | 2 | 0 | 0 | | off |

Readings:

- Claude Code loads UTF-16LE with the mark even with a lone surrogate or an odd final byte.
- It does not load a file holding the bare constants `NaN`, `Infinity` or `-Infinity`. The string
  `"NaN"` is ordinary text.
- It loads UTF-8 with a byte that is not UTF-8, which it reads as U+FFFD.

### 2. `contexto run claude` with the real `claude` (17 runs)

| Case | Exit | Requests | Hint state | Stderr |
|---|---|---|---|---|
| UTF-16LE, lone high surrogate, switch `"1"` | 2 | 0 | | names `CLAUDE_CODE_USE_BEDROCK` and the user settings file |
| UTF-16LE, lone low surrogate, switch `"1"` | 2 | 0 | | the same |
| UTF-16LE, odd byte, switch `"1"` | 2 | 0 | | the same |
| UTF-16LE, both, switch `"1"`, project file | 2 | 0 | | names the switch and `.claude/settings.json` |
| UTF-16LE, both, switch `"0"` | 0 | 2 at env | `stripped` | the session line |
| UTF-16LE, both, switch `"0"`, environment `1` | 2 | 0 | | names the switch in the process environment |
| UTF-16LE, both, hint variable `"1"` | 0 | 2 at env, 1 with the class header | `forwarded` | the session line |
| UTF-16LE with a `//` comment | 2 | 0 | | could not read the user settings file (not valid JSON) |
| switch `"1"` beside `NaN` | 2 | 0 | | could not read the user settings file |
| switch `"0"` beside `NaN`, environment `1` | 2 | 0 | | names the switch in the process environment |
| hint variable `"1"` beside `NaN` | 0 | 2 at env, none with the class header | `stripped` | the session line |
| base URL beside `NaN`, `Infinity`, `[-Infinity]` (3 runs) | 0 | 2 at env each | `stripped` | the session line |
| Latin-1 byte, switch `"1"` | 2 | 0 | | names the switch and the user settings file |
| Latin-1 byte, base URL | 0 | 2 at env | `stripped` | the session line |
| plain UTF-8 base URL (control) | 0 | 2 at env | `stripped` | the session line |

No refused run created `CONTEXTO_HOME` or sent a request to any stub. A run that launched sent its
requests through the proxy to `--upstream`.

The base URL these files give was checked with `contexto.launcher.claude_launch_settings`, with no
`--upstream` and the environment's base URL set. It was the file's for the control, the malformed
UTF-16LE, the string `"NaN"` and the Latin-1 files. It was the environment's for the three files
with a constant, user and project. Each is where `claude` alone sent its requests in section 1.
The UTF-16LE file with a comment refuses instead: the NUL rule, an accepted cost.

### Not confirmed

- Python's U+FFFD replacement was compared with Claude Code's for a Latin-1 byte and an overlong
  `1` only.
- A UTF-16LE file with a lone surrogate in a key was not run.
- Linux, and any other Claude Code version.
