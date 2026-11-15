# FAQ

[Contexto](../README.md) / [Documentation](README.md)

Short answers about logins, API keys, cost, what is sent where, and continuing a session.

**Does it work with a Claude Pro or Max login?** The supported upstreams are the vendor's API with
an API key, and CLIProxyAPI; the live acceptance runs went through CLIProxyAPI. Contexto forwards
whatever credential Claude Code sends and never stores it, but running it with a claude.ai
subscription login is not a supported setup. To use a subscription, log in to it in CLIProxyAPI and
point Contexto at CLIProxyAPI ([docs/cliproxyapi.md](cliproxyapi.md)), and
check your plan's terms on using it through a proxy.

**Does it work with my own API key?** Yes. With `ANTHROPIC_API_KEY` or an `apiKeyHelper` set for
Claude Code, `contexto run claude` uses them as Claude Code would. For Codex, give a key helper and,
for an OpenAI key, `--codex-upstream https://api.openai.com/v1` ([Codex](codex.md)).

**What does it cost?** Contexto is free (MIT). It makes no model calls of its own. Because every
request stays under the bound, a long session sends fewer input tokens than it would without
Contexto: 46.6% fewer in the measured run ([Results](results.md#tokens-and-cost)). Each rotation changes the start
of the prompt, so the request after it writes a new prompt cache instead of reading the old one.
Between rotations the cache is reused as usual: each acceptance run reused it on all but at
most one eligible request. Whether a session costs less in money depends on the model's prices and
on cache hits: the measured session cost less at Haiku 5.5's prices. At flat prices such as Opus
5.5's it would cost more if plain Claude Code hit the prompt cache on every request, and less with
the two cache misses the full-context baseline had ([Results](results.md#tokens-and-cost)).

**Does it send anything anywhere?** No. The proxy listens on `127.0.0.1` only and forwards your
CLI's own requests to the upstream you configured. There is no telemetry and no update check. The
MCP server opens no network socket under `contexto run`; under `contexto serve` it connects only
to the service on `127.0.0.1`. Besides paging the prompt, and putting the records a new session
carries from earlier sessions of its project ahead of its first message
([Project memory](project-memory.md)), the one thing Contexto adds to requests is an
`X-Session-Affinity` header, a salted hash of the session id, and only for upstreams
that are not a vendor API ([Troubleshooting](troubleshooting.md)). The version check runs `claude --version` or
`codex --version` locally.

**Can I continue a session later?** Yes. `contexto run claude --resume <id>` and
`contexto run claude --continue` reuse the Contexto session that served the conversation
([Resuming](claude-code.md)), and Contexto prints the exact resume command when Claude Code exits.
`contexto run --session <full id> claude --resume <id>` names the session yourself. For Codex:
`contexto run --session <id> codex resume --last`. `--session` takes a full id: a prefix of existing
ids exits 2, and an unknown full id starts a new, empty session with that id, so check it with
`contexto sessions list`. A new session in the same repository or directory does not see the earlier
session's turns, but it starts with the open obligations and decisions the model pinned there
([Project memory](project-memory.md)).
