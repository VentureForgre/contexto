# Running Contexto with CLIProxyAPI

[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) is a local or hosted proxy that serves
Claude Code, Codex, and other CLIs through subscription logins and API keys. Contexto can use it as
its upstream for both Claude Code and Codex. This guide covers the setup, what to check when it
fails, and what was tested.

## Supported upstreams (D35)

If you run Contexto yourself, it supports two kinds of upstream:

- the vendor's API with your own API key (Anthropic for Claude Code, OpenAI for Codex);
- a CLIProxyAPI instance, reached with one of its client keys.

A subscription login (claude.ai, ChatGPT) belongs in CLIProxyAPI or in the vendor's own CLI, never
in Contexto. Contexto forwards the credential the CLI sends and never stores or logs it. To use a
ChatGPT login with Contexto, log in to it in CLIProxyAPI (`cli-proxy-api -codex-login`) and run
Codex in the default `apikey` mode against CLIProxyAPI. `contexto run --codex-auth chatgpt`, in
which Codex keeps its own login and Contexto passes it to the ChatGPT backend, is tested only with
synthetic credentials.

You are responsible for following your providers' terms, including any terms on using a
subscription through a proxy. Contexto does not decide that for you.

## How the pieces connect

```
Claude Code --> Contexto (127.0.0.1:<port>) --> CLIProxyAPI <root>/v1/messages    --> provider
Codex       --> Contexto (127.0.0.1:<port>) --> CLIProxyAPI <root>/v1/responses   --> provider
```

`<root>` is CLIProxyAPI's base URL without a path, for example `http://127.0.0.1:8317` (its default
port) or `https://cpa.example.com`. CLIProxyAPI serves `/v1/messages`, `/v1/messages/count_tokens`,
`/v1/responses`, and `/v1/models` under that root.

## Claude Code

1. In CLIProxyAPI's `config.yaml`, list a client key under `api-keys:`, and log in to the providers
   you want it to serve (for example `cli-proxy-api -claude-login`), or add provider API keys.
2. Point Claude Code at CLIProxyAPI's root and give it the client key. In a Claude Code settings
   file (`~/.claude/settings.json`, or a project's `.claude/settings.local.json`):

   ```json
   {
     "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317"},
     "apiKeyHelper": "/bin/cat /path/to/cliproxyapi-client-key"
   }
   ```

   `apiKeyHelper` is any command that prints the key. Instead of a helper you can set
   `ANTHROPIC_AUTH_TOKEN` to the key. Both send it as `Authorization: Bearer`, which CLIProxyAPI
   accepts. `ANTHROPIC_API_KEY` sends it as `X-Api-Key` only: stock CLIProxyAPI accepts that too,
   but a deployment behind another gateway may not (one tested deployment answered `X-Api-Key`
   with 401), so prefer the bearer forms. Do not rely on a claude.ai login in Claude Code itself:
   CLIProxyAPI rejects that token with 401 `Invalid API key`.
3. Run Claude Code through Contexto:

   ```sh
   contexto run claude
   ```

   Contexto finds the upstream the way Claude Code would (README, Troubleshooting), so the
   settings above are enough. To name it explicitly, put `--upstream` before `claude`:

   ```sh
   contexto run --upstream http://127.0.0.1:8317 claude --model claude-haiku-5-5
   ```

   The session line on stderr names where the upstream came from (`upstream flag`, or
   `upstream settings:user`, and so on), never the URL.

Do not add `/v1` to `ANTHROPIC_BASE_URL` or `--upstream`: Claude Code adds it, and
`<root>/v1/v1/messages` is a 404.

## Codex

```sh
contexto run --codex-key-helper '/bin/cat /path/to/cliproxyapi-client-key' codex
```

- Upstream: with no `--codex-upstream`, Contexto uses Claude Code's upstream plus `/v1`, so with the
  settings above Codex goes to `http://127.0.0.1:8317/v1`. To set it on its own, pass
  `--codex-upstream http://127.0.0.1:8317/v1` or set `CONTEXTO_CODEX_UPSTREAM`. Keep the `/v1`:
  Codex sends `POST /responses`, and CLIProxyAPI answers `<root>/responses` with
  `404 page not found`.
- Key: Codex runs the helper and sends the key as `Authorization: Bearer`. Without
  `--codex-key-helper` or `CONTEXTO_CODEX_KEY_HELPER`, Contexto uses the `apiKeyHelper` from your
  Claude Code settings, so the setup above serves both CLIs with one key. If Claude Code gets its
  key from `ANTHROPIC_AUTH_TOKEN` instead, pass a helper.
- Leave `--codex-auth` at its default, `apikey`.
- Contexto keeps Codex on HTTP (D22). CLIProxyAPI's WebSocket route for Responses is not used.

## Models

Contexto does not rename models. Claude Code and Codex send the model name they are given, and
CLIProxyAPI resolves it with its own aliases and prefixes. `GET <root>/v1/models` with your client
key lists what it serves. A model no configured provider serves comes back as a 400
`unknown provider for model`, passed to the CLI unchanged.

## What Contexto passes through

- Every request header except hop-by-hop headers, including `Authorization`, `X-Api-Key`, and
  `anthropic-beta`, and the query string (CLIProxyAPI also accepts `?key=`). For the rewritten
  endpoints Contexto asks for an uncompressed response (`Accept-Encoding: identity`).
- Every response header except hop-by-hop headers, including `X-Cpa-Trace-Id` and `Retry-After`.
- Error bodies and status codes, unchanged and never retried. CLIProxyAPI's own auth errors are not
  in Anthropic's error shape (`{"error":"Missing API key"}` or `{"error":"Invalid API key"}`, 401);
  Claude Code shows them as an API error.
- Streams, as they arrive. CLIProxyAPI sends `: keep-alive` comment events when
  `streaming.keepalive-seconds` is set; Contexto relays them as they come, also while it holds a
  tool call back for its recall budget (contract rule on the stream hold, D35). A failure after a
  stream started arrives as `event: error` (Messages) or `event: response.failed` (Responses) and
  is relayed and logged like any other ending.
- `count_tokens` requests, rewritten the same way as the messages they count.

## Troubleshooting

**401 `Missing API key`.** The CLI sent no key. Check `apiKeyHelper` or `ANTHROPIC_AUTH_TOKEN` for
Claude Code, and the key helper for Codex.

**401 `Invalid API key`.** The key is not in CLIProxyAPI's `api-keys`, or a gateway in front of it
does not accept the header the CLI used. Switch to `apiKeyHelper` or `ANTHROPIC_AUTH_TOKEN`.

**404 `page not found` from Codex.** The Codex upstream lacks `/v1`.

**502 `contexto proxy: upstream unreachable (...)`.** Contexto could not connect. Check that
CLIProxyAPI is running (`lsof -iTCP:8317 -sTCP:LISTEN`) and the URL scheme: `http://` for a plain
local instance, `https://` when its `tls.enable` is on. Contexto verifies certificates against
Python's default trust store. For a self-signed certificate, point `SSL_CERT_FILE` at it (Python's
`ssl` reads that variable), or run CLIProxyAPI on plain HTTP bound to `127.0.0.1`.

**Answers lose earlier thinking, or the prompt cache misses.** CLIProxyAPI can spread requests over
several upstream credentials (`routing.strategy`). Each credential has its own cache, and thinking
signed for one may be dropped by another (O6 in `docs/DECISIONS.md`). That costs tokens, not
correctness of the bound.

**Cloaking.** CLIProxyAPI rewrites requests from clients it does not recognise as Claude Code when
its cloak mode says so. Contexto forwards Claude Code's own headers, so the default `auto` mode
treats it as Claude Code. Modes that strip or move prompt content (`always`, `strict-mode`) were not
tested with Contexto.

## What was tested

- `tests/test_cliproxyapi.py` runs Contexto's real rewrite engine against a fake upstream built
  from CLIProxyAPI v7.3.12's source: its routes, client-key checks (all four ways), error bodies,
  response headers, keep-alive comments, and failure framing for both APIs.
- Live, 2026-10-24, outcomes only: through a CLIProxyAPI deployment, `claude-haiku-5-5`
  `count_tokens`, a non-streaming message, and a streaming message each returned 200 (both messages
  ended `end_turn`, with usage read), and one Codex Responses request on `gpt-5.6-luna` returned 200
  with `response.completed` and usage. A local CLIProxyAPI 7.3.12, sent no key, returned its 401
  through Contexto unchanged. Claude Code and Codex themselves were not run in this check; the
  requests had the shapes they send.
