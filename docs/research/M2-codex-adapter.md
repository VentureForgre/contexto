# M2 A5 design note: the Codex adapter (`contexto run codex`)

Phase 1 of packet A5, for review by manager-1 before implementation. Author: build.builder-2.
Date: 2026-10-16. Codex CLI 0.154.0 (`rust-v0.154.0`, commit `6b9826e`), model `gpt-6-astra`.

Tags: **[measured]** zero-token probes in `spikes/m2/codex/` (loopback stub, synthetic credentials
that are not real); **[source]** Codex source at that tag, `S:<path>:<lines>` under `codex-rs/`;
**[inferred]** not verified. No model was called.

## Decisions (intake, D22, relayed by manager-1 on 2026-10-16)

- **Transport:** force HTTP through a custom provider defined with `-c`; never the built-in `openai`
  provider. The proxy URL has no `/v1`; the upstream base carries the version path (agreed with
  builder-1), and the proxy transforms any path ending in `/responses`.
- **API-key mode (default, the only mode run live, D12):** a temporary `CODEX_HOME` (directory 0700,
  files 0600, removed on exit) holding a custom provider that points at the proxy, with
  `auth.command` running the existing key helper command; upstream is the owner's proxy. The
  temporary config names the helper command only, never the key; nothing from it is committed or
  logged. The helper command is configurable (flag or environment), and its default comes from the
  same place Claude Code's `apiKeyHelper` is configured rather than a hard-coded path.
- **ChatGPT mode (`--codex-auth chatgpt`, a product feature):** the user's own `CODEX_HOME` with `-c`
  overrides only, a custom provider at the proxy with `requires_openai_auth = true`, credentials and
  `/models` passed through unchanged. Tested only with synthetic credentials, never live on the
  owner's login. Its end-user terms question stays open as O4, stated in `--help` and here.
- **Bypass:** some Codex calls go to chatgpt.com directly (rate limits, usage, a remote MCP
  connection) and bypass the proxy by design.
- **MCP:** registration with `-c mcp_servers.contexto` stays open until `tools/list` behavior for
  `gpt-6-astra`'s lite format is understood.
- **Rewrite:** a `Dialect` interface plus `ResponsesDialect`, as a separate PR after PR #30 merges.
- **Request compression:** builder-1's proxy cannot transform compressed bodies; the custom provider
  sent plain JSON in every probe, and the launcher disables request compression per process anyway.

The sections below are the phase 1 analysis that led to these decisions; section 2's recommended
auth path is kept as ChatGPT mode, and API-key mode follows the decision above.

## Summary of recommendations

1. **Transport: force HTTP with a custom provider.** A provider defined with `-c` is HTTP-only and
   sends the full input on every request (`store: false`, no `previous_response_id`) [measured]. The
   built-in `openai` provider always tries WebSockets first and cannot be switched off [measured,
   source].
2. **Auth: no temporary `CODEX_HOME`, no `auth.command`.** Launch Codex on the user's own
   `CODEX_HOME` with `-c` overrides only (nothing written to `config.toml`), and set
   `requires_openai_auth = true` on the custom provider. Codex then sends the user's existing
   credential to the proxy exactly as it would upstream, and token refresh keeps working [measured
   header names, source]. This departs from the contract's lane text (temporary `CODEX_HOME` with
   `auth.command`), so it needs intake's decision.
3. **Rewrite: share the engine, add a Responses dialect.** Epochs, rotation, matching, and window
   sizing are protocol-independent; turn detection, carried content, reasoning stripping, and
   preamble placement need a Responses-specific dialect.
4. **MCP:** register `contexto mcp --session S` with `-c mcp_servers.contexto.*`; how
   `gpt-6-astra` exposes MCP tools in the request is still open (section 4).

## 1. Transport

| Setup | Requests seen by the stub | Evidence |
|---|---|---|
| Custom provider via `-c` (`env_key` or `requires_openai_auth`) | `POST {base_url}/responses` only; full input; `store: false`; no `previous_response_id`; `prompt_cache_key` set; `include: ["reasoning.encrypted_content"]`; retried up to 6 times on 401 | measured |
| Built-in `openai` with `-c openai_base_url=...` | 7 WebSocket upgrades on `GET /responses`, then the same HTTP POSTs | measured (matches M0) |

- `supports_websockets` defaults to false for custom providers [source: `model-provider-info/src/lib.rs:419, 454`]. The built-in id `openai` is reserved, and `openai_base_url` changes only its URL [source: `config/src/config_toml.rs:65-71`].
- **Recommendation: force HTTP.** WebSocket mode sends deltas against server-side state (`previous_response_id`, measured by L1-a), which the proxy cannot bound. Handling deltas would require the proxy to keep its own copy of the history and rebuild full requests, which is the hidden-state complexity D19 avoided.
- **What breaks or changes with a custom provider** [source]:
  - no WebSocket transport, so each request is a full HTTP POST (bounded by the proxy anyway);
  - remote compaction (`/responses/compact`) and the guardian endpoint are used only when the provider is named like OpenAI or Azure, so the provider must not use those names; Codex falls back to local compaction, which a bounded prompt should never trigger;
  - backend-only routes are off under a custom URL (`supports_codex_backend_routes`).
- **Upstream URL:** the custom provider's `base_url` is the proxy. The proxy's upstream depends on the auth mode: `https://api.openai.com/v1` for API keys, `https://chatgpt.com/backend-api/codex` for ChatGPT login [source: `model-provider-info/src/lib.rs:40, 293-312`].

## 2. Auth

- **Recommended:** `-c 'model_providers.contexto={name="Contexto", base_url="http://127.0.0.1:<port>/v1", wire_api="responses", requires_openai_auth=true}' -c model_provider=contexto`, on the user's normal `CODEX_HOME`.
  - `-c` overrides form their own layer above `config.toml` and may define `model_providers` and `mcp_servers`; the project-local deny list does not apply to them [source: `config/src/loader/mod.rs:277-292`, `config/src/overrides.rs:9-15`]. Nothing is written to the user's config [measured: probes used an empty temporary home only to isolate the test].
  - Headers the proxy receives [measured with synthetic credentials, names only]:
    - API-key mode: `authorization`, `originator`, `session-id`, `thread-id`.
    - ChatGPT-login mode: the same plus `chatgpt-account-id`; Codex also sends `GET {base_url}/models` through the proxy at startup.
  - Refresh: in ChatGPT mode Codex refreshes against `auth.openai.com` directly and writes its own auth store; a 401 from upstream triggers the refresh, so the proxy must forward 401s unchanged [source].
- **Why not a temporary `CODEX_HOME` with `auth.command`** [source]:
  - the command prints a raw bearer token (cached 300 s, rerun on 401, 5 s timeout), but it cannot supply `ChatGPT-Account-ID`; adding that header through `http_headers` or `env_http_headers` disables Codex's own ChatGPT token refresh;
  - `auth.command` cannot be combined with `requires_openai_auth`;
  - a temporary `CODEX_HOME` has no `auth.json`, and keyring entries are keyed by a hash of the `CODEX_HOME` path, so the command would have to read the user's credential from their store at runtime, which is close to copying it.
- **Owner or terms questions:**
  1. ChatGPT-plan credentials pass through the local proxy (as with Claude subscription OAuth in M0, section 6 of `M0-interception.md`). The owner already runs `labcodex.subject` on the ChatGPT login; whether a product may route that token through a local proxy is the same Q5 decision.
  2. With ChatGPT login, the `/models` reply fetched through the proxy is cached in the user's `models_cache.json`, which is not keyed by provider [source]. The proxy must pass `/models` through unchanged. This writes Codex's own cache, not the user's config.
  3. The synthetic ChatGPT-mode probe also made Codex open a remote MCP connection that was not sent to the proxy and failed with 401 [measured; target not logged]. Rate-limit and usage calls go to chatgpt.com directly [source]. These bypass the proxy by design and carry the user's credential as they normally would.

## 3. Mapping rules 1 to 13 onto Responses input items

Measured request (custom provider, first turn): `input` = `additional_tools` (developer item with
three tool namespaces), a developer message with the base instructions (about 21 KB), developer
messages tagged `skills_instructions`, `permissions`, `collaboration_mode`, `multi_agent_role`,
`multi_agent_mode`, a user message tagged `environment_context`, then the user's prompt. Top-level
`instructions` and `tools` are empty for `gpt-6-astra` (its "lite" format puts them in `input`)
[measured; source: `core/src/client.rs:891-996`].

| Contract concept | Responses mapping |
|---|---|
| Head (`system` + `tools`, rule 6 fingerprint) | the `additional_tools` item and the base-instructions developer message (rebuilt every request with deterministic ids), plus top-level `instructions`, `tools`, `reasoning`, `text` when present |
| Turn start | a `user` message with authored text that is not a context message |
| Turn | the turn start plus every following item (assistant `message`, `reasoning`, `function_call`, `function_call_output`, `custom_tool_call`, `custom_tool_call_output`, later context messages) up to the next turn start |
| Carried | the session-start context: the combined developer context message and the user context message (AGENTS.md as `# AGENTS.md instructions for <dir>` with `<INSTRUCTIONS>`, and `<environment_context>`) [source]; later context updates that Codex appends when a section changes are also carried (contract step 4: no per-turn Codex context shape is measured yet, and carrying is the safe failure; the 8,000-token fallback bounds growth). Context sections are recognised only by the exact openings measured on codex-cli 0.154.0, so a user prompt starting with another tag stays a turn start |
| Thinking (rule 2) | drop `reasoning` items of retained turns that existed when the epoch began; never inside the in-flight span after the last user message. Dropping a reasoning item alone keeps call pairing valid; Codex itself inserts `"aborted"` outputs for orphan calls [source]. Lane 2 measured that edits before encrypted reasoning returned 200 on `gpt-6-astra`, with use not observable |
| Preamble (rule 1) | an `input_text` part prepended to the first retained turn-start user message; carried context messages placed right after it, as for Claude. Placement still needs a small conformance check (budget from intake) |
| Usage (rules 3, 7) | `response.completed` carries `response.usage`: `input_tokens` (includes cached), `input_tokens_details.cached_tokens` and `cache_write_tokens`, `output_tokens`, `output_tokens_details.reasoning_tokens`. The estimate uses `input_tokens` alone (cached tokens are a subset, unlike Anthropic) |
| Cache | Codex sends `prompt_cache_key` per session; pass it through. A bounded prefix that is stable within an epoch keeps prefix caching |
| Compaction | Codex compacts from the `total_tokens` the proxy returns; with a bounded upstream, `gpt-6-astra`'s 244,800-token trigger (272,000 window) is never reached [source]. `-c model_auto_compact_token_limit` stays at its default (D3) |
| count_tokens (rule 11) | no Responses equivalent was seen; nothing to transform |

**Engine reuse.** Epoch state, rotation, rule 10 matching, F and W sizing, events, and
per-conversation state are protocol-independent. The Anthropic-specific parts are turn-start
detection, context blocks, the system-message classifier, thinking stripping, preamble merge, and
the head fingerprint. Proposal: extract a small `Dialect` interface from `rewrite.py` (Anthropic as
the existing implementation) and add `ResponsesDialect` in `codex.py`, with the request field
(`messages` or `input`) and the usage reader per dialect. That touches `rewrite.py` only after PR #30
merges.

## 4. MCP registration

- `-c mcp_servers.contexto.command="contexto" -c 'mcp_servers.contexto.args=["mcp","--session","<S>"]'` registers the server for this process only [source: `config/src/mcp_types.rs:323-368`].
- Measured: a probe MCP server registered this way was started (the request grew by about 3.6 KB), but its tool name did not appear in the `additional_tools` namespaces (`functions`, `clock`, `collaboration`). With `gpt-6-astra`'s lite format MCP tools may be exposed elsewhere (a developer message or deferred tool search) [inferred]. **Open:** confirm where the tool lands, and that the model can call it, with lane B's MCP server once it serves `tools/list` correctly.

## 5. Plan for phase 2 (after approval)

1. `codex.py`: build the `codex` argv (`-c` provider and MCP overrides), pick the upstream from the auth mode (header `chatgpt-account-id` present means ChatGPT), and the `ResponsesDialect`.
2. Proxy: route `POST /responses` through the transform and `GET /models` unchanged; a small hook agreed with builder-1 (proxy.py and run.py are his).
3. Tests from the measured item shapes (no content).
4. One budgeted live check (intake): placement of the preamble and carried context, and a two-turn run on `labcodex.subject` or a throwaway.

## Evidence

`spikes/m2/codex/probe.sh` (five scenarios), `responses_stub.py` (structure-only logger),
`mcp_probe_server.py`. Output stays in `.contexto/spikes/M2-A5/`.
