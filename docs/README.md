# Contexto documentation

Everything under `docs/`, in three groups. The [README](../README.md) says what Contexto is, how to
install it and how to start it.

## Guides

- [install.md](install.md): other ways to install, checking the download, installing from a
  checkout, uninstalling, and the supported platforms.
- [claude-code.md](claude-code.md): resuming a Claude Code conversation through Contexto, and
  `/clear`, `/resume` and `/compact`.
- [codex.md](codex.md): `contexto run codex` in API-key mode: the key helper, the Codex upstream,
  and the `CODEX_HOME` Contexto sets up.
- [run-reference.md](run-reference.md): the options of `contexto run`, what the launcher does,
  `contexto proxy`, the environment variables, and the supported upstreams.
- [serve.md](serve.md): `contexto serve`, the engine as a local service for clients Contexto does
  not launch (IDE extensions, the desktop app, the Agent SDK, CI): the token, client settings,
  routing, the files it writes, and its limits.
- [cliproxyapi.md](cliproxyapi.md): running Contexto with a CLIProxyAPI instance as the upstream:
  setup, checks, and what was tested.
- [sessions.md](sessions.md): how to tell that Contexto is working, what a session stores and
  where, and the commands that inspect and remove sessions.
- [stats.md](stats.md): the summary `contexto run` prints at exit and `contexto stats`: every
  number defined, which are measured and which estimated, and what counts as a fault.
- [standing-rules.md](standing-rules.md): how a rule you give once stays in view after its turn is
  paged out.
- [project-memory.md](project-memory.md): what a new session carries from the earlier sessions of
  its project, what the model is shown, and how to see, remove or turn it off.
- [mcp-tools.md](mcp-tools.md): the five MCP tools the model gets, how search ranks, and the recall
  budget.
- [search.md](search.md): what a search query matches, the approximate matches search falls back to
  when no turn holds every word, and its limits.
- [faq.md](faq.md): subscription logins, API keys, cost, what is sent where, and continuing a
  session later.
- [results.md](results.md): what the acceptance runs measured (recall against full context, tokens
  and cost), with the limits of each number.
- [limitations.md](limitations.md): the full list of known limitations.
- [doctor.md](doctor.md): `contexto doctor`, a check of what `contexto run` needs: the CLIs, the
  upstream, a key, the store home, the MCP server, and the cloud-provider switches that make
  `contexto run claude` refuse to launch.
- [troubleshooting.md](troubleshooting.md): the warnings and errors you are most likely to see, each
  with its cause and what to do.
- [exit-codes.md](exit-codes.md): the exit codes of every command.
- [bench.md](bench.md): `contexto bench`, the benchmark that runs Contexto, full context and plain
  Claude Code set to compact at the same bound on the same generated session: how to run it, what
  it measures, and what it cannot show.
- [cli-updates.md](cli-updates.md): how a new Claude Code or Codex version is tested.
- [dialect.md](dialect.md): the `Dialect` contract, for adapting the engine to another client's API.

## Design

- [milestones/M0-infinite-session-study.md](milestones/M0-infinite-session-study.md): the brief for
  the first study: the questions and hypotheses about sessions that never compact.
- [milestones/M1-ledger-and-recovery.md](milestones/M1-ledger-and-recovery.md): a contract for a
  durable ledger and handoff between sessions. It was not built as a milestone; its record model
  became the pinned state of the proxy that ships.
- [milestones/M2-contexto-proxy.md](milestones/M2-contexto-proxy.md): the design contract for the
  proxy that ships.
- [DECISIONS.md](DECISIONS.md): every decision, numbered D0, D1 and so on, with its reason and
  measurements.
- [maintainer-process.md](maintainer-process.md): how the maintainer runs the project with AI
  agents, and what the reviewer comments on pull requests are.
- [pypi-publishing.md](pypi-publishing.md): the steps that turn on publishing to PyPI as
  `contexto-proxy`: the `pypi` environment, the trusted publisher, the switch, and what is
  permanent on PyPI.

## Research

Measurements and proposals from the design work. They record numbers, not conversation text. The
code behind them is in `spikes/`.

The M0 study:

- [research/M0-recommendation.md](research/M0-recommendation.md): what the M0 study recommended
  building. The other M0 notes:
  [interception](research/M0-interception.md),
  [proxy measurements](research/M0-proxy-measurements.md),
  [Codex measurements](research/M0-codex-measurements.md),
  [rollover](research/M0-rollover.md),
  [paging policy](research/M0-paging-policy.md),
  [fidelity test](research/M0-fidelity-test.md),
  [harder probes](research/M0-harder-probes.md),
  [quality against context size](research/M0-quality-vs-context.md),
  [cost model](research/M0-cost-model.md) and
  [policy and cost](research/M0-policy-and-cost.md). Their data is in the `research/M0-*.csv`
  files.

Claude Code acceptance:

- [research/M2-acceptance-harness.md](research/M2-acceptance-harness.md): the design of the
  acceptance harness, its detectors, and its dry-run budget.
- [research/M2-acceptance-run3.md](research/M2-acceptance-run3.md): live run #3, claude-haiku-5-5 on
  Claude Code 2.1.301.
- [research/M2-acceptance-opus.md](research/M2-acceptance-opus.md): the acceptance run on
  claude-opus-5-5.
- [research/M2-acceptance-run4.md](research/M2-acceptance-run4.md): live run #4, claude-haiku-5-5 on
  Claude Code 2.1.318.
- [research/M2-recall-experiment.md](research/M2-recall-experiment.md): an experiment on the
  recall guidance text, after an acceptance run made no recall call.
- [research/M2-usage-savings-run3.md](research/M2-usage-savings-run3.md): input tokens and cost in
  live run #3 against plain Claude Code, modeled from the run's own records.

Codex:

- [research/M2-codex-adapter.md](research/M2-codex-adapter.md): the design note for
  `contexto run codex`.
- [research/M2-codex-smoke.md](research/M2-codex-smoke.md): the first live Codex conversation
  through `contexto run codex`.
- [research/M2-codex-acceptance-plan.md](research/M2-codex-acceptance-plan.md): the Codex acceptance
  plan and its zero-spend dry run.
- [research/M2-codex-step1-dryrun.md](research/M2-codex-step1-dryrun.md): the zero-spend dry run of
  step 1 of that plan.
- [research/M2-codex-step1-live.md](research/M2-codex-step1-live.md): step 1 of that plan, run live
  at bound 64,000.
- [research/M2-codex-acceptance-150k.md](research/M2-codex-acceptance-150k.md): the same step run
  live at the default bound of 150,000.
- [research/M2-codex-usage-vs-plain.md](research/M2-codex-usage-vs-plain.md): one Codex session
  through Contexto against what plain Codex would have used.

New CLI versions and upstream checks:

- [research/M2-claude-code-2.1.310.md](research/M2-claude-code-2.1.310.md): the version checks for
  Claude Code 2.1.310.
- [research/M2-claude-code-2.1.318.md](research/M2-claude-code-2.1.318.md): the version checks for
  Claude Code 2.1.314 and 2.1.318, and codex-cli 0.166.0.
- [research/D70-serve-probes.md](research/D70-serve-probes.md): what Claude Code 2.1.322 sends a
  gateway and an MCP server, measured against stubs for `contexto serve`.
- [research/O6-thinking-check.md](research/O6-thinking-check.md): a live check for dropped
  thinking after each session was pinned to one credential.

Scale and cost proposals:

- [research/M2-scale-30m.md](research/M2-scale-30m.md): Contexto at 30 million tokens of history,
  measured against a local fake upstream.
- [research/M2-haiku-100k-tier.md](research/M2-haiku-100k-tier.md): a proposal on Haiku 5.5's
  100K price tier and the bound that stays under it.
- [research/M2-cache-aware-rotation.md](research/M2-cache-aware-rotation.md): a proposal for
  cache-aware rotation, from an offline replay.
- [research/M2-headroom-outliers.md](research/M2-headroom-outliers.md): a proposal for headroom
  that forgets outlier turns, from an offline replay.
- [research/M2-tool-rounds-lever.md](research/M2-tool-rounds-lever.md): a proposal on limiting
  the search rounds in a turn, from an analysis of live run #3.
- [research/M2-seat-bound.md](research/M2-seat-bound.md): a modeled choice of bound for Claude Code
  with Opus 5.5 when the fixed part of every request is large (82,000 or 92,000 tokens).
- `research/data/`: the CSV files behind the usage and seat-bound notes.
