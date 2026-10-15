# Milestone 0: Infinite-session design study

Status: study brief, nothing implemented. Owner: intake.lead. Date: 2026-10-15.

## Owner problem

The owner runs very long sessions in terminal agents such as Claude Code and Codex, for example a
long conversation with an OpenRig intake lead. The conversation keeps growing. When it gets too
long the CLI compacts it, which costs extra tokens and loses detail, and there is no turning back.

The owner wants a standalone tool, useful to OpenRig but not dependent on it, that sits outside the
CLI's own context handling, takes over how the conversation is sent to the model, and lets a
session run for a nearly unlimited time without context and usage tokens being eaten so quickly.
Constraints from the owner: not RAG and not a vector database; a new approach is welcome.

## Why this is possible

Each turn, the CLI resends the whole conversation to the model API. The CLI chooses the endpoint
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
   rules such as tool-use and tool-result pairing, and signed thinking blocks.
2. Compaction trigger: how does each CLI decide to compact? If it uses token counts reported by the
   API, what happens when the proxy returns counts for the bounded prompt?
3. Cost: per-turn and cumulative input tokens for a long session under (a) the plain CLI with
   caching, (b) built-in compaction, (c) the hypothesis. Rewriting the prompt prefix breaks prompt
   caching, so the design needs a cache-friendly structure, for example a head that changes only
   at checkpoint epochs.
4. Fidelity: what does the model lose with a bounded window, and does a recall tool recover it?
   Define a measurable test, for example questions about early-session facts after N turns.
5. Auth and terms: what changes between API-key auth and subscription login when requests pass
   through a local rewriting proxy? Flag anything that needs an owner decision.

## Lanes

- Lane 1 (management.manager-1): interception surfaces. Evidence for questions 1, 2, and 5 for
  Claude Code and Codex, and a pass-through measuring proxy spike that records request sizes and
  token usage per turn.
- Lane 2 (management.manager-2): policy and economics. Questions 3 and 4: a cost model with numbers,
  the paging and pinning policy, the cache-friendly prompt structure, the fidelity test design, and
  prior art (MemGPT/Letta-style paging, provider context management, each CLI's compaction).

Intake merges both lanes into one recommendation for the owner: what to build, in what order, and
which owner decisions are needed.

## Relation to M1

M1 (ledger, checkpoints, recovery) is a candidate backing store for the pinned state block. Its
open PRs (#2, #3, #4) stay paused until the owner decides, after this study, whether to adopt,
revise, or close them.

## Rules for the study

- Deliverables are documents under `docs/research/` merged through reviewed PRs, plus spike code
  only where it produced a measurement that a document cites.
- Never commit API keys, OAuth tokens, raw prompts, raw transcripts, or captured request bodies.
  Spikes record sizes, counts, hashes, and field names, not content.
- Live-model experiments use throwaway sessions on synthetic tasks and stay small. Report token
  spend for each experiment.
- Mark every claim as measured, documented by the vendor, or inferred.
