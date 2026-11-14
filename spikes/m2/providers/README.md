# Provider variables and the proxy (D64, zero spend)

`contexto run claude` routes Claude Code through the proxy with `ANTHROPIC_BASE_URL`. `probe.sh`
measures what Claude Code does with that when a provider variable (`CLAUDE_CODE_USE_BEDROCK` and
the like) is set: it runs `contexto run --upstream A claude -p` against two loopback stubs, A for
Contexto's upstream and B for the provider's endpoint, and counts the requests each one got.

Since PR #258 was reconciled with #271 (D70), `contexto run claude` no longer warns and launches:
it refuses a launch with any of the six variables on (exit 2), before Claude Code starts, and logs
no `provider_bypass` event. The launcher's warning and `provider_bypass` below describe the launcher
as it was when these runs were made; the request counts are Claude Code's and still hold. Run
against today's launcher, every case with a variable on exits 2 with no request at A or B, so the
bypass itself can now be measured only with the `plain-...` cases' plain `claude -p`.

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
alarm stops it, so most cases exit 143. The first 20 cases take about 8 minutes. The runs are kept
in a private temp directory (`mktemp -d`), printed at the end; remove it by hand.

`ONLY="case case"` limits the run to the named cases. The last four cases (`plain-...`) run plain
`claude -p`, without Contexto, in the same sandbox: they ask whether Claude Code reads a settings
file that starts with a byte order mark for the two settings `contexto.upstream` reads.

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

## Result (Claude Code 2.1.322, 2026-11-12)

Ten cases, run with `ONLY=`, for the review of PR #258. The columns are as above. The first two
repeat cases from the table above; the others are new.

| Case | Proxy | A | B | The launcher's warning |
|---|---|---|---|---|
| no variable (control) | 12 | 6 | 0 | none |
| `CLAUDE_CODE_USE_BEDROCK=1` in the environment | 0 | 0 | 6 | printed, `provider_bypass` logged |
| `"1"` in a project `settings.json` that starts with a UTF-8 byte order mark | 0 | 0 | 6 | none before the fix; printed and logged after it |
| `CLAUDE_CODE_USE_GATEWAY=1` in the environment, with `ANTHROPIC_AUTH_TOKEN` and the environment's `ANTHROPIC_BASE_URL` set to B | 12 | 6 | 0 | none |
| `CLAUDE_CODE_USE_GATEWAY` `"1"` and the token in the project's `env` block, the environment's `ANTHROPIC_BASE_URL` set to B | 12 | 6 | 0 | none |
| `CLAUDE_CODE_IGNORE_PERSISTED_TRUST=1`, `CLAUDE_CODE_USE_BEDROCK` `"1"` only in the project's `env` block, Bedrock's own variables in the environment | 12 | 6 | 0 | the "may apply once its trust dialog is accepted" form, logged with `after_trust_dialog` |

Plain `claude -p`, without Contexto. The environment's `ANTHROPIC_BASE_URL` is stub A, the
project file's is stub B, and the helper creates a marker file and prints a fake key:

| Project `.claude/settings.json` | `POST /v1/messages` at A | at B | Helper ran |
|---|---|---|---|
| `env.ANTHROPIC_BASE_URL` = B | 0 | 6 | |
| the same, after a byte order mark | 0 | 6 | |
| `apiKeyHelper` | 6 | 0 | yes |
| the same, after a byte order mark | 6 | 0 | yes |

What it adds:

- A settings file that starts with a UTF-8 byte order mark is read like any other: for a provider
  variable, for `env.ANTHROPIC_BASE_URL` and for `apiKeyHelper`. Contexto read such a file as not
  JSON, so as absent; `contexto.upstream` now reads through the mark (one reader since D70).
- `CLAUDE_CODE_USE_GATEWAY`, with the base URL and token it needs, is not a bypass in `-p` mode:
  every model request went through the proxy, from the environment and from a project file.
- With `CLAUDE_CODE_IGNORE_PERSISTED_TRUST` set, `-p` mode drops a project file's
  `CLAUDE_CODE_USE_BEDROCK`, as D52 measured for a project file's `env` block in general.

## Settings files Claude Code reads or skips whole (Claude Code 2.1.322)

Measured by the two reviewers of PR #258 (rounds 2 to 4), with this probe's sandbox and stubs:
plain `claude -p` and `contexto run claude -p` from a `mktemp -d` directory, `env -i`, a temp
`HOME` and `CLAUDE_CONFIG_DIR`, a fake key, `sandbox-exec` denying everything but localhost, and
loopback stubs that answer 401. Stub A is the environment's `ANTHROPIC_BASE_URL`, stub B the
settings file's; "proxy requests" are `request` events in a `contexto run claude -p` session log.
Nothing was spent. These are the
measurements behind two of the rules `contexto.upstream.read_settings_file` follows; the third,
`NaN` and `Infinity`, is also in [D70-serve-probes.md](../../../docs/research/D70-serve-probes.md) §3.

| Settings file | What Claude Code did |
|---|---|
| An integer of 400 or of 5,000 digits (past Python's 4,300-digit limit), at the top level or inside `env`, beside `env.ANTHROPIC_BASE_URL` = B | read the file: 6 requests at B each, 0 at A |
| A 5,000-digit integer beside `"CLAUDE_CODE_USE_BEDROCK": "1"` | read the file: 0 requests at Contexto's upstream, 6 at the provider stub |
| `NaN`, `Infinity` or `-Infinity` beside a base URL B | "Invalid or malformed JSON", the file not loaded: 6 requests at A |
| `NaN` beside `CLAUDE_CODE_USE_BEDROCK` | the file not loaded: no bypass, 6 requests through the proxy |
| `{"env": null, "apiKeyHelper": <helper>}`, no key in the environment (project file, and the user file) | the file not loaded: "These settings files did not load, so nothing in them is in effect … `(env): Expected record, but received null`"; the helper did not run, and plain `claude -p` and `contexto run claude -p` both ended `Not logged in · Please run /login`, exit 1 |
| The same with `"env": []` or `"env": "x"` | the same |
| `{"env": null}` with `ANTHROPIC_API_KEY` in the environment | the environment's key was used: 12 proxy requests |
| `{"env": {}, "apiKeyHelper": <helper>}` (control) | the file loaded: the helper ran, 12 proxy requests |
| `{"env": {"FOO": {"a": 1}}, "apiKeyHelper": <helper>}`, and `{"env": {"FOO": null}, …}` | the file loaded: the helper ran, 12 proxy requests |
| `{"permissions": "bad", "env": {"CLAUDE_CODE_USE_BEDROCK": "1", …}}` | the file not loaded: 6 of 6 requests through the proxy, 0 at the provider stub |

What it shows:

- Claude Code parses a settings file as JavaScript's `JSON.parse` does: an integer longer than
  Python's 4,300-digit limit is a number (Infinity past the largest double), so the file is read,
  and `NaN` or `Infinity`, which `JSON.parse` rejects, make the file unreadable. Contexto's reader
  follows both (`_json_int`, `_json_constant`).
- Claude Code checks the type of each settings key it knows. An `env` that is not an object makes
  it load nothing from the file, `apiKeyHelper` included; a value of any type inside an `env`
  object does not. Contexto's reader treats a file whose `env` is not an object as setting
  nothing. Other wrongly typed keys (`"permissions": "bad"`) also make Claude Code skip the file;
  Contexto does not check those, which docs/doctor.md lists as a limit.

Not measured: interactive mode (only `-p`), so not what interactive Claude Code does with a project
file's provider variable once its trust dialog is accepted; a settings file other than the
project's `.claude/settings.json`, except the `env`-not-an-object case above, which was also run on
the user file; two variables on at once; organization-managed settings; the
other 18 cases of the first table on 2.1.322; and any Claude Code version but 2.1.318 and 2.1.322.
The variable list, its order and the on/off rule were also read from the 2.1.318 and 2.1.322
programs, which hold the same code for them.
