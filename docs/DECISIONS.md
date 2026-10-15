# Decision log

Every decision that shapes Contexto, with who made it, when, and why. Newest last within each
section. "Owner" is the human who owns the project; "intake" is the intake lead seat acting under
the owner's delegation. Open decisions are listed at the end with the options and the current lean.

## Standing authority

- **D0. Intake has free rein to finish the project.** Owner, 2026-10-15. The owner prefers to return
  to a finished project and asked that STATUS show decisions taken, why, and decisions still open.
  Hard limits from the owner: nothing is published outside this repository (no PyPI, npm,
  Homebrew, public repos, or public posts); the owner's personal Claude and Codex configuration is
  not changed; heavy spend on the owner's ChatGPT plan needs the owner.

## Product direction

- **D1. Contexto targets near-unlimited sessions for terminal agents.** Owner, 2026-10-15. Claude
  Code and Codex resend the whole conversation every turn, so long sessions burn usage, and
  compaction adds cost and loses detail. Wanted: a standalone tool that takes over how the session
  is sent to the model. Not RAG, not a vector database. See
  `docs/milestones/M0-infinite-session-study.md`.
- **D2. Success bar: compaction never fires, and accuracy equals full context.** Owner, 2026-10-15.
  The compaction threshold exists because quality is already degrading near it. Designs are ranked
  first by how small and stable they keep the working context and whether recall and task accuracy
  match the full-context baseline, then by token cost. Built-in compaction counts as a failure.
- **D3. Experiments use default CLI compaction settings.** Owner, 2026-10-15. An autocompact
  threshold override was proposed to save tokens and revoked: results must reflect real sessions.
- **D4. Measurements guide decisions.** Owner, 2026-10-15. Every number in a decision carries its
  source and is marked measured, vendor-documented, or modeled.

## Study (M0)

- **D5. Run M0 as a study before building.** Intake, 2026-10-15. Interception surfaces, economics,
  and fidelity were unknown; a wrong guess would build the wrong proxy correctly.
- **D6. Phase 1 is pass-through only, with numeric token caps.** Intake after reviewer-1, 2026-10-15.
  500k per experiment, 3M per builder packet, 6M per lane; spike proxies on 127.0.0.1; shared
  routing never edited. Raised once: a 2M lab budget for the quality-versus-context-size probe.
- **D7. Dedicated test seats instead of throwaway processes.** Owner, 2026-10-15. `lab.subject`
  (Claude Code, claude-haiku-5-5) and `labcodex.subject` (Codex, gpt-6-astra) are real long-running
  OpenRig seats, the owner's actual use case, and avoid builder-seat hook and resume-token
  collisions.
- **D8. Seats stay on `builtin:yolo`; the Codex test seat uses the owner's ChatGPT login.** Owner,
  2026-10-15. Under yolo Codex drops `-p` profiles and project-local config cannot set
  `model_provider`, so routing the seat to the rig proxy would change the owner's personal default.
- **D9. The 2M compaction-loss run was replaced by a quality-versus-context-size probe.** Intake under
  D2, 2026-10-15. Forcing a session into compaction measures the wrong thing; the probe finds the
  context size where recall starts to slip, which bounds the working window.
- **D10. Strip-thinking arm limited to two checkpoints (309,507 tokens).** Intake, 2026-10-15. The
  third checkpoint (954,439 total) runs only if the first two show a gap of at least 2 probes.
- **D11. The rig proxy is the owner's infrastructure.** Owner, 2026-10-15. Auth headers reaching
  `cliproxy.sullylab.de` are not an exposure.
- **D12. Phase 2 rewriting is authorized under D0, on owner infrastructure only.** Intake,
  2026-10-15. This settles brief question Q5 for development: rewriting experiments run through the
  owner's proxy with the rig key (Claude) or the same proxy through a throwaway `CODEX_HOME` whose
  provider uses `auth.command` (Codex). Rewritten traffic never uses the owner's ChatGPT or
  claude.ai subscription login, and the owner's own config files are never edited. What end users
  should use is a product documentation question, listed under open decisions.

## Repository and process

- **D13. Repository lives at VentureForgre/contexto, private, PR-only `main`.** Owner, 2026-10-15.
  The personal account is on GitHub Free, where branch rules are unavailable for private repos; the
  org is on Team. Ruleset 25202810: PR required, no force-push, no deletion, no bypass, 0 required
  approvals because all seats share one GitHub account. Exact-head review by a reviewer seat is a
  team rule.
- **D14. Releases follow discord-cli's Conventional Commit versioning, adapted.** Owner request,
  intake design, 2026-10-15. Versions come from tags, so CI never commits to `main` (which the
  ruleset would reject); artifacts attach to GitHub Releases on this private repo only. No package
  registry publishing (D0).
- **D15. M1 (ledger, checkpoints, recovery) paused when M0 started.** Operator and owner,
  2026-10-15. The owner had not chosen M1; an earlier intake occupant drafted it. Its PRs #2, #3,
  #4 stay open and untouched until decision O1.

## Open decisions

- **O1. Fate of M1 and PRs #2, #3, #4.** Options: adopt the ledger as the backing store for the
  pinned state block; revise it to fit the phase 2 design; close it. Lean: decide from the M0
  recommendation; the hash-chained ledger with provenance and epistemic labels is a natural fit for
  pinned state, so revising beats closing. Owner: intake under D0.
- **O2. History-edit strategy under preserved thinking.** Current Claude models reject edits to
  history before a thinking block. Options: strip all thinking at window rotation; keep
  thinking-bearing turns in an untouched recent suffix; byte-for-byte reinsertion; server-side
  context management. Pending: builder-4's comparison, the conformance probe, and the strip-thinking
  arm's accuracy numbers.
- **O3. Working-window bound.** Pending the quality-versus-context-size probe at about 40k, 97k, and
  243k. Constraint already measured or modeled: Claude Code's fixed head is about 42k (modeled from
  lane 2) and Codex's was measured at 32.3k.
- **O4. End-user authentication guidance.** API key only, or also subscription logins, for people
  who run Contexto themselves. Needs the terms question answered in the product docs; the owner
  decides before anything is shared beyond this repo.
