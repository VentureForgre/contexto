# Provider variables and the proxy (D64, zero spend)

`contexto run claude` routes Claude Code through the proxy with `ANTHROPIC_BASE_URL`. `probe.sh`
measures what Claude Code does with that when a provider variable (`CLAUDE_CODE_USE_BEDROCK` and
the like) is set: it runs `contexto run --upstream A claude -p` against two loopback stubs, A for
Contexto's upstream and B for the provider's endpoint, and counts the requests each one got.

## Run

macOS only: the probe uses `sandbox-exec` to deny every connection that is not to localhost, so a
wrong variable name cannot reach a real provider. From the repository root:

```
spikes/m2/providers/probe.sh "$PWD/.venv/bin/contexto" /path/to/the/claude/binary
```

Each case runs under `env -i` with a temp `HOME`, `CLAUDE_CONFIG_DIR` and `CONTEXTO_HOME`, a fake
key, and the provider's own base URL variable pointing at stub B with its authentication skipped
(for example `ANTHROPIC_BEDROCK_BASE_URL` and `CLAUDE_CODE_SKIP_BEDROCK_AUTH=1`). Both stubs answer
401 and keep only each request's method and path. Claude Code retries the 401 until a 25-second
alarm stops it, so most cases exit 143. 20 cases take about 8 minutes. The temp directory is
printed at the end; remove it by hand.

## Result (Claude Code 2.1.318, 2026-11-12)

"Proxy" is the number of `request` events in Contexto's session log; "A" is `POST /v1/messages`
at Contexto's upstream; "B" is `POST` requests at the provider stub. Every case that reached B
printed the launcher's warning once and logged one `provider_bypass` event; no other case did.

| Case | Proxy | A | B | B's path |
|---|---|---|---|---|
| no variable (control) | 12 | 6 | 0 | |
| `CLAUDE_CODE_USE_BEDROCK=1` in the environment | 0 | 0 | 6 | `/model/<id>/invoke-with-response-stream` |
| `=TRUE` in the environment | 0 | 0 | 6 | the same |
| `=0` in the environment | 12 | 6 | 0 | |
| empty in the environment | 12 | 6 | 0 | |
| `=enabled` in the environment | 12 | 6 | 0 | |
| `"1"` in the project's `.claude/settings.json` `env` block | 0 | 0 | 6 | `/model/<id>/invoke-with-response-stream` |
| JSON `true` in that `env` block | 0 | 0 | 6 | the same |
| `"0"` in that `env` block, `=1` in the environment | 12 | 6 | 0 | |
| `""` in that `env` block, `=1` in the environment | 12 | 6 | 0 | |
| `CLAUDE_CODE_USE_VERTEX=1`, environment | 0 | 0 | 3 | `/projects/...` |
| the same in the `env` block | 0 | 0 | 3 | `/projects/...` |
| `CLAUDE_CODE_USE_FOUNDRY=1`, environment | 0 | 0 | 6 | `/v1/messages` |
| the same in the `env` block | 0 | 0 | 6 | `/v1/messages` |
| `CLAUDE_CODE_USE_MANTLE=1`, environment | 0 | 0 | 3 | `/v1/messages` |
| the same in the `env` block | 0 | 0 | 3 | `/v1/messages` |
| `CLAUDE_CODE_USE_ANTHROPIC_AWS=1`, environment | 0 | 0 | 3 | `/v1/messages` |
| the same in the `env` block | 0 | 0 | 3 | `/v1/messages` |
| `CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD=1`, environment | 0 | 0 | 3 | `/v1/messages` |
| the same in the `env` block | 0 | 0 | 3 | `/v1/messages` |

What it shows:

- With any of the six variables on, no model request reaches the proxy. Claude Code sends them to
  the provider's endpoint, whatever `ANTHROPIC_BASE_URL` the launcher's `--settings` file sets.
- The variable counts from the environment and from a settings file's `env` block alike.
- On is `1` or `true` in any case (and, by the same code, `yes` and `on`); `0`, an empty value and
  other text are off.
- A settings file's value replaces the environment's, also when it turns the variable off.

Not measured: interactive mode (only `-p`), a settings file other than the project's
`.claude/settings.json`, two variables on at once, organization-managed settings, and any Claude
Code version but 2.1.318. The variable list, its order and the on/off rule were also read from the
2.1.318 and 2.1.322 programs, which hold the same code for them.
