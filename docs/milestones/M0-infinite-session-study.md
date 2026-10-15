# Milestone 0: Infinite-session design study

Status: study brief, nothing implemented. Owner: intake.lead. Date: 2026-10-15. The owner described
the problem and asked the team to look at it; framing it as milestone M0 is intake's choice.

## Owner problem

The owner runs very long sessions in terminal agents such as Claude Code and Codex, for example a
long conversation with an OpenRig intake lead. The conversation keeps growing. When it gets too
long the CLI compacts it, which costs extra tokens, and there is no turning back. (Intake adds:
compaction also loses detail.)

The owner wants a standalone tool, useful to OpenRig but not dependent on it, that sits outside the
CLI's own context handling, takes over how the conversation is sent to the model, and lets a
session run for a nearly unlimited time without context and usage tokens being eaten so quickly.
Constraints from the owner: not RAG and not a vector database; a new approach is welcome.

## Why this is possible

Each turn, Claude Code resends the whole conversation to the Messages API (vendor-documented).
Whether Codex does the same, or relies on server-side state in the Responses API
(`previous_response_id`), is inferred until Q1 measures it. The CLI chooses the endpoint
from configuration (`ANTHROPIC_BASE_URL` for Claude Code; a configurable provider base URL for
Codex). This rig's own seats already reach the model through such a proxy. Whatever sits at that
boundary decides what the model actually sees on every turn, without changing the CLI.

## Starting hypothesis (to be tested, not assumed)

**Context virtual memory at the API boundary.** Treat the model's context window as RAM and a local
store as disk. A local proxy rewrites each request into a bounded prompt:

1. A stable head: system prompt, tool definitions, and a pinned state block (open obligations,
   active decisions, verified facts with provenance) built from a durable ledger.
2. A rolling window of recent turns, kept verbatim.
3. Older turns and large tool outputs paged out to the store and replaced by short stubs with
   stable ids. The model can fetch a page back by id (a recall tool) when it needs one.

Retrieval is deterministic and addressable: pinned state, recency, and explicit ids. No embeddings
and no similarity search. The CLI's own history may keep growing locally, but the upstream prompt
stays bounded, so the CLI's compaction should never be needed.

## Alternatives to compare against

- H2. CLI hooks that inject state (for example Claude Code `SessionStart`, `PreCompact`,
  `UserPromptSubmit`).
- H3. Session-file rebirth: write a fresh session file containing curated state and resume it
  (Claude Code and Codex both persist sessions locally).
- H4. Supervisor rollover: wrap the CLI in a terminal supervisor, detect a full context, and start
  a fresh session seeded with a handoff. This is roughly what OpenRig does today.
- H5. Provider-native features: prompt caching, server-side context editing, memory tools, and each
  CLI's built-in compaction.

## Questions the study must answer with evidence

1. Interception: for each CLI, what can be intercepted or rewritten (requests, responses, reported
   token usage, session files, hooks), and what breaks when we do it? Include protocol validity
   rules such as tool-use and tool-result pairing, and signed thinking blocks. If the proxy adds a
   recall tool the CLI never declared, the proxy must answer that call itself with extra upstream
   round trips the CLI does not see; document what that requires.
2. Compaction trigger: how does each CLI decide to compact? If it uses token counts reported by the
   API, what happens when the proxy returns counts for the bounded prompt?
3. Cost: per-turn and cumulative input tokens for a long session under (a) the plain CLI with
   caching, (b) built-in compaction, (c) the hypothesis. In M0, (a) and (b) are measured where
   possible and (c) is modeled; measuring (c) needs the rewriting prototype in phase 2. Rewriting the prompt prefix breaks prompt
   caching, so the design needs a cache-friendly structure, for example a head that changes only
   at checkpoint epochs.
4. Fidelity: what does the model lose with a bounded window, and does a recall tool recover it?
   In M0, design a measurable test (for example probes about early-session facts after N turns)
   and run its baseline on the plain CLI. Running it against the hypothesis needs phase 2.
5. Auth and terms: what changes between API-key auth and subscription login when requests pass
   through a local rewriting proxy? Flag anything that needs an owner decision.

## Lanes

Phase 1 (now) produces evidence, models, and designs. It does not rewrite requests.

- Lane 1 (management.manager-1): interception surfaces. Questions 1, 2, and 5 for Claude Code and
  Codex; a pass-through measuring proxy that records request sizes and token usage per turn; and
  the evidence for H2 (hooks), H3 (session-file rebirth), and H4 (supervisor rollover).
- Lane 2 (management.manager-2): policy and economics. Questions 3 and 4: a cost model with
  numbers for the plain CLI, built-in compaction, H4, and the hypothesis; the paging and pinning
  policy; the cache-friendly prompt structure; the fidelity test design and plain-CLI baseline;
  and prior art plus H5 (MemGPT/Letta-style paging, provider context management, each CLI's
  compaction).
- Dependency: lane 2's measured numbers for Q3 (a) and (b) come from lane 1's measuring proxy.
  Lane 2 does not build a second one; until lane 1's data lands it models with stated assumptions.

Intake merges both lanes into one recommendation for the owner: what to build, in what order, and
which owner decisions are needed, including Q5.

Phase 2 (only after the owner decides Q5): a rewriting prototype of the hypothesis, used to
measure Q3 (c) and run the Q4 fidelity test against it.

## Relation to M1

M1 (ledger, checkpoints, recovery) is a candidate backing store for the pinned state block. Its
open PRs (#2, #3, #4) stay paused until the owner decides, after this study, whether to adopt,
revise, or close them.

## Rules for the study

- Deliverables are documents under `docs/research/` merged through reviewed PRs, plus spike code
  only where it produced a measurement that a document cites.
- Never commit API keys, OAuth tokens, raw prompts, raw transcripts, or captured request bodies.
  Spikes record sizes, counts, and field names. Avoid plain hashes of short or guessable content;
  use keyed hashes if a hash is needed.
- Spike proxies listen on `127.0.0.1` only and are pass-through only in phase 1: they forward
  requests and responses unchanged.
- Never edit shared model routing: per-worktree `.claude/settings.local.json`,
  `~/.claude/settings.json`, Codex `config.toml`, or the rig's existing proxy. Seats reach the model
  through that routing, so editing it cuts a live seat off. A spike sets its base URL only in the
  environment, or a temporary config directory, of its own throwaway CLI process.
- Credentials: the throwaway CLI process uses the credential it already inherits. The spike proxy
  forwards auth headers upstream unchanged and never stores them; the rig's proxy key is never
  copied into spike config, files, or logs. Redact `Authorization`, `x-api-key`, cookies, and OAuth
  tokens before anything is logged, including local logs. Spike output goes under the gitignored
  `.contexto/` directory. Do not log in, log out, or change any CLI's auth mode.
- Token caps for live-model experiments: 500,000 tokens per experiment, 3,000,000 per builder
  packet, 6,000,000 per lane. Use throwaway sessions on synthetic tasks and report token spend for
  each experiment. Exceeding a cap needs intake's approval first.
- Mark every claim as measured, documented by the vendor, or inferred.
