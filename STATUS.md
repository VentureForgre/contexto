# Contexto status

Updated 2026-11-13 by claude (D69, project memory: PR #279, the first of its two pull requests,
unreleased). Before that, 2026-11-12 (v0.5.24: the 2026-11-05 QA run and the fix round, PRs #232 to
#250, D48 to D61, live acceptance run #4, deleting the v0.1.0 to v0.4.0 releases, D62, and the README
rewrite, D68). Decisions taken and still open, with reasons:
`docs/DECISIONS.md`. Every number below is measured unless marked otherwise.

## Where the project is

Contexto keeps very long Claude Code (and Codex) sessions under a fixed context bound so the CLI
never compacts, by putting a local proxy between the CLI and the model API. The owner's bar (D2):
compaction never fires, and accuracy equals full context.

- M0 study: complete. Recommendation `docs/research/M0-recommendation.md`.
- M2 build: complete on main. Runs end to end through `contexto run claude ...` and
  `contexto run codex ...`. Contract: `docs/milestones/M2-contexto-proxy.md`.
- Acceptance: **all five criteria passed on Claude Code 2.1.318** (live run #4, v0.5.20,
  claude-haiku-5-5, bound 150,000, Claude Code's defaults with tool search on): Contexto answered 52
  of 54 probes against 47 for the full-context baseline, with zero compaction, every request under
  the bound (largest 110,465) and 38 of 38 cache reads. Earlier: live run #3 (2.1.301, 51 against
  48), claude-opus-5-5 (2.1.302, 53 against 54, two departures from the contract), and **Codex
  passed criteria 1 to 3** at bound 64,000 (D30) and at the default 150,000 (codex-cli 0.163.0;
  `docs/research/M2-codex-acceptance-150k.md`).
- QA run, 2026-11-05 (v0.5.8): 22 tester agents found that three common actions broke the promise in
  normal use (a pasted screenshot, a head change from tool search or a model switch, `/clear`), that
  standing rules were lost once paged, and two security holes (a project's `apiKeyHelper` run by
  `contexto run codex` with no trust check, and no Host or Origin check on the proxy). The fix round
  (PRs #232 to #250, D48 to D61, v0.5.9 to v0.5.24) fixed all of them, each PR with two reviewer
  agents; details under "QA run and fix round" below.
- Project memory (D69, PR #279, unreleased, the first of two pull requests): a new session starts
  with the open obligations and active decisions the main conversation pinned in earlier sessions of
  the same project (the git repository it started in, a linked worktree or submodule being its own,
  else the directory). A record counts as the main conversation's when the proxy sees Contexto's pin
  call and the server's success reply in that conversation's own history. The model sees the
  records as escaped literals under a heading that calls them records from an earlier session, not
  instructions, inside the 6,000-token pin budget. Closures and replacements made in the main
  conversation of any session of the project apply to all of them, running ones included.
  `contexto run` prints how many were carried, `contexto state` and `contexto project show` list
  them, `contexto project forget` removes them, and `--no-project-memory` or
  `CONTEXTO_PROJECT_MEMORY=0` turns it off. The pin guidance changed for every user (only the user's
  own words are pinned). Contract rule 19. Three review rounds; tested at zero spend only, no live
  model call. Guide: `docs/project-memory.md`.
- Latest release: v0.5.24 (main d38e87d, 2026-11-06). Releases from v0.5.13 on carry `SHA256SUMS`
  and a build provenance attestation (PR #233).
- Tests: 3185 passed, 1 skipped, 1 xfailed on macOS, Python 3.13 (local run on PR #250's head before
  its review fixes, 2026-11-06; main d38e87d collects the same 3,187 tests). PR #250's final head
  passed CI on Linux and macOS, Python 3.12 to 3.15. D69 adds the `tests/test_project*.py` files;
  they are not in that count.
- Tested CLI versions (`TESTED_VERSIONS`): Claude Code 2.1.302, 2.1.310, 2.1.314 and 2.1.318;
  codex-cli 0.154.0, 0.163.0 and 0.166.0 (D56). 2.1.318 has the full live acceptance; 2.1.314 and
  0.166.0 the zero-spend routine only.
- The repository is public (D42) and MIT licensed (D43). GitHub Releases stay the only distribution;
  there is no package registry release. Tracked files were scrubbed at `HEAD` of the owner's proxy
  hostname, another private repository's name, statements about the owner's own accounts and plans,
  and the self-hosted runner's machine name; history was not rewritten. On 2026-11-06, with the
  owner's approval, 197 old branches were deleted (QA had found the hostname on 197 of 232 public
  branches), with no force push. The same day, with the owner's approval, the releases and tags
  v0.1.0 to v0.4.0 were deleted (D62), because GitHub's source archives of those 19 releases held the
  hostname; their commits stay on `main`. Old commits, pull request diffs and pull request comments
  still hold it (D42).
- CI and releases run on GitHub-hosted runners (PR #213): Python 3.12 to 3.15 on Ubuntu, 3.12 and
  3.15 on macOS, and Ruff lint (`uvx ruff@0.17.2 check .`; `ruff format` is not enforced). `ci` is a
  required check in the main ruleset, and workflow runs on fork pull requests need approval for all
  external contributors (D42). GitHub private vulnerability reporting, which `SECURITY.md` points
  to, is on.
- CLIProxyAPI compatibility (D35, PR #210) and upstream affinity (D36, PR #212, deployed on the
  owner's proxy and verified for cache reuse). Guide: `docs/cliproxyapi.md`.
- README and docs (2026-11-12, D68): the README was 772 lines and is 263. Its reference sections are
  in `docs/*.md` (index `docs/README.md`), the Results table in `docs/results.md` and the full
  limitations list in `docs/limitations.md`. `tests/test_docs_links.py` checks every relative link
  and heading anchor in the tracked Markdown files. The README states recall as no worse than full
  context on a scripted test, and cost as depending on the model and on cache hits.
  The repository's About box (description and twelve topics) matches it since 2026-11-14; the old
  values are in D68.
- Search falls back to approximate matches when no paged-out turn holds every word (D65, PRs #266
  and #267): identifier styles fold, question words drop, a turn holding enough of the query's
  weight counts, and one typo is tolerated. A strict hit's result is unchanged. On 192 probes at
  6,000 pages, recall at 3 on each class that used to miss rose from 0 to between 73 percent
  (typos) and 92 percent (one extra word), and exact queries stayed at 15 of 15. False hits stayed
  at 0 of 16. A miss now takes 0.6 to 1.4 s instead of an empty answer at 0.3 to 0.6 s; strict
  hits are unchanged (D65 gives the latency in full). Guide: `docs/search.md`.

## QA run and fix round (2026-11-05 to 2026-11-06, v0.5.9 to v0.5.24)

The QA run (main 4e2c4cd, v0.5.8) had 22 tester agents in three waves and spent about 4.94M live
tokens on claude-haiku-5-5 and gpt-6-astra low. What held: installs on Python 3.12 to 3.15 by pip,
pipx and uv; 2,284 tests passed on every Python, 0 failures in 18 runs; the bound held in every live
session; about 3,800 synthetic requests passed an API-rule validator with byte-exact storage; the
proxy took 2,400 requests at 100 concurrent with no crash or leak, and keys were never logged. The
fix round then ran one builder per area, two reviewer agents per PR, and re-checks after each merge.

What each decision changed, with the key numbers (full text in `docs/DECISIONS.md`):

- **D48 (PR #234, v0.5.21): images priced by their pixels.** Images and PDFs were priced
  by base64 size: a screenshot measured at 641 to 3,243 tokens was estimated at 124,000 to 429,000,
  so one pasted screenshot paged the whole window and logged `bound_too_small` with advice to restart.
  Now `width * height / 750` after scaling to a 2,576-pixel long edge, 64 to 4,784 (estimates 640 to
  3,334 for the four measured sizes); since D61 the higher of that and the image's 28-pixel patches
  plus 3, at most 4,787. Images and documents are kept out of the bytes-per-token
  ratio, which two screenshots had pushed from 2.87 to 39.4; after the fix it stays 3.00.
- **D49 (PR #232, v0.5.9; amended by PR #244, v0.5.18): a head change is sized like any rotation.**
  Tool search loading a tool, an MCP server connecting, or a model switch took a branch that skipped
  the minimal window, `bound_too_small` and the room rotation. Live at bound 60,000 a turn started
  with recall room -4,243 and every search came back empty. Now sized like any rotation, and every
  forwarded request is checked against the bound before it is sent (at bound 60,000 with the live
  shape: sent 51,050 to 51,406 before, 20,681 to 21,037 after). The amendment measures the head from
  the first usage that has an input count, at that request's own ratio (a late first usage had left
  the head 0 and turn starts over the bound: 32,047 real at bound 30,000, now about 20,700).
- **D50 (PR #238, v0.5.15): main follows Claude Code's session id.** After `/clear` the new
  conversation stayed a side conversation until it outgrew the old one, so recall drew on a spent
  room and returned nothing. Main now follows `X-Claude-Code-Session-Id` across `/clear` and
  `/resume`, and the compaction summary across `/compact`, interactive and headless, measured on 2.1.302 to 2.1.318; subagents, teammates and
  nested `claude -p` never take it. Logged as `main_changed`.
- **D51 (PR #242, v0.5.11): standing rules pinned when stated.** The model had pinned 1 of 16
  standing rules. The MCP instructions and the preamble now tell it to pin a rule as an obligation
  when stated and keep it open: live, 4 of 4 and 4 of 4 rules pinned and applied, against 0 of 4 with
  the shipped wording (865,443 tokens). Contexto's own search, recall and state calls are no longer
  searched, so an old search reply no longer outranks the page it quoted.
- **D52 (PR #237, v0.5.14): settings as Claude Code reads them, and project settings for Codex only
  from a trusted folder.** Contexto read different project files than Claude Code in 8 of 27
  layouts; it now matches all 27. A project's `apiKeyHelper` and base URL count for Codex only when
  Claude Code has recorded the folder as trusted; `CLAUDE_CODE_IGNORE_PERSISTED_TRUST` turns project
  files off for every CLI, as `claude -p` does.
- **D53 (PR #239, v0.5.16): the proxy serves only the local CLI.** A page on localhost could scan
  every ephemeral port in about a second, write pages and take main. Now a request with an `Origin`
  header, or whose `Host` is not `127.0.0.1` or `localhost`, gets 403. Esc during a held tool call
  let upstream generate for 38 s more; now the proxy shuts upstream within 0.21 to 0.23 s. An early
  413 is forwarded instead of becoming retried 502s.
- **D54 (PR #236, v0.5.20): `contexto run codex` launch.** The key helper runs through `/bin/sh -c`
  (`$VAR`, `~` and pipes failed before); a derived upstream on Anthropic's API is refused instead of
  sending the OpenAI key there; interactive launches pass `--no-daemon` (Codex 0.166.0 had left 2
  processes and about 517 MB per session); a missing `codex` exits 127 before anything starts.
- **D55 (PR #240, v0.5.12; follow-ups PR #245, v0.5.13): `contexto run claude` resumes through
  Contexto.** `--resume <id>` and `--continue` had opened a new, empty Contexto session; they now
  reuse the one that served the conversation, and the exit prints the resume command. Signals: a
  watchdog ends Claude Code and removes the temporary files when the launcher is killed with SIGKILL
  (Claude Code gone within 2 s). Mid-run notices are printed after Claude Code exits. The automatic
  session choice is serialized by `choose.lock` (four racers shared a store in 3 of 4 tries without
  it, never with it).
- **D56 (PR #243, v0.5.10): Claude Code 2.1.314 and 2.1.318 and codex-cli 0.166.0 tested** at zero
  spend; Contexto reads the new subagent stop reports (`<tools-used-before-summary>`), so side rooms
  close early on those versions too. Most spikes run a CLI by path with a temp `HOME`, and all
  set `DISABLE_AUTOUPDATER=1`. Numbers: `docs/research/M2-claude-code-2.1.318.md`.
- **D57 (PR #241, v0.5.17): user-facing commands.** `contexto sessions show` lists page ids,
  `contexto search` finds them, `contexto state` prints the pinned records readably (`--raw` for
  the model's text), session ids take unique prefixes for read-only commands, failed launches leave
  no empty session, and exit codes are consistent (42 new or extended tests fail on v0.5.8).
- **D58 (PR #235, v0.5.19): the store and the MCP server stay up.** Lone surrogates no longer stop
  paging or kill the MCP server; any tool exception is that call's tool error; a FIFO, directory or
  symlink in place of a session file is damage, never waited on; a full disk leaves no temporary
  file; error echoes are capped at 100 characters.
- **D59 (PR #246, v0.5.22): review follow-ups.** The Host check accepts any port (SSH forwards, port
  80); a close after the last event is not an abort; the `choose.lock` wait says so and Ctrl+C exits
  130; Claude Code's full value-option list (36 were missing); one `codex --version` read (6.02 s to
  3.51 s with a hanging one); damaged lock files are named; and Claude Code's compaction summary is
  recognised in all its forms (7 of 8 captures had been missed).
- **D60 (PR #248, v0.5.23): a PDF is never estimated low.** Priced by bytes, 25 of 28 real-producer
  fixtures were estimated at 0.02 to 0.59 of their cost, and one read near the bound went out over
  it unflagged. A PDF now costs 4,568 tokens a page with its pages counted only when certain, else
  600 pages (`pdf_pages_uncertain`). The page lexer is a single pass with a 400,000-step budget:
  hostile inputs take at most 0.27 s, and the exponential regex of the earlier parser (branch `claude/pdf-pages`, found in review round 9)
  is gone.
- **D61 (PR #250, v0.5.24): the fix round's remaining review lows.** Codex tool output counts at
  the 1.5 tool-output seed until usage arrives (unflagged over-bound requests in reviewer A's harness
  6,451 to 445, turn starts 89 to 0); records from every release through v0.5.23 resume with their
  head measured again; the JPEG walk is bounded (0.75 to 0.97 s for 100 crafted images, now 0.032 to
  0.037 s) and an image costs the higher of its pixels and its 28-pixel patches, at most 4,787; the
  start warning quotes each CLI's own measured overheads; `pdf_uncertain` only when the PDF is why
  the turn does not fit.
- **Docs and packaging (PR #233):** README sections for first-time users (what is stored, how to
  tell it works, uninstall), SECURITY.md's full store list, release checksums and provenance.
- **Harness (PR #247):** the baseline replay turns `tool_reference` blocks for tools it removes
  into a text note, which run #4's baseline needed.

## Acceptance, live run #4 (v0.5.20, Claude Code 2.1.318, claude-haiku-5-5, bound 150,000, owner's proxy)

Numbers-only record: `docs/research/M2-acceptance-run4.md`. D27 protocol, seed 20261016 (run #3's
content), Claude Code's default settings (tool search on, no `--allow-contexto-tools`). No departures
from the contract. The owner approved the run on 2026-11-06.

| Criterion | Result |
|---|---|
| 1. Long run | PASS: 45 Messages requests, all 200, 0 compaction, max upstream input 110,465 of 150,000, 0 real over-bound, 0 model 4xx, 0 local 5xx, 0 failure events, usage on 45 of 45; 6 rotations (4 `bound`, 2 `head_change`, inferred from the request shapes to be tool search loading Contexto's tools); CLI exit 0 |
| 2. Fidelity | PASS: Contexto 52 of 54, baseline 47; chain 28 / 26, obligations 6 / 3, distractor, multihop and paged-only level |
| 3. Recall | PASS: paged-only 4 of 4 correct and credited, each through one `contexto_search` call that returned the plant's paged span before the answer |
| 4. Cache | PASS: 38 of 38 eligible requests (1.0) |
| 5. Tests | PASS: 2880 passed, 1 skipped, 1 xfailed at 2388752; CI and Release green |

The baseline's first attempt stopped at request 1's `count_tokens` (400, no tokens spent): the
replay kept a `tool_reference` to a Contexto tool it removes. That was the harness, not the
product; with PR #247 the baseline re-ran from the same transcript, 6 of 6 requests 200. Tokens:
6,001,818 in all (H1 3,422,988, baseline 2,578,830) of a 9,865,000 approval. Against run #3: one
more probe right for Contexto, one fewer for the baseline, and 380,631 fewer tokens. Product, client
and model behaviour changed together between the runs, so the record does not attribute the
difference to any one of them.

## Fixed and added on 2026-10-25 (v0.5.2 to v0.5.8)

- D44 (PR #224, v0.5.3): `contexto run claude` allows Contexto's five MCP tools by exact name in
  its `--settings` (`permissions.allow`). Claude Code 2.1.302 in `-p` mode had refused every
  Contexto tool call, so recall silently failed (the Opus run's attempt 1: 18 of 54). User `deny`
  and `ask` rules and `--disallowedTools` still win. A refused Contexto tool now logs
  `mcp_tools_denied` and warns once; it fails acceptance 1. PR #227 (v0.5.7) pairs refusals across
  all assistant messages and all trailing user messages, and a non-string `tool_use_id` no longer
  raises `toolcheck_error`.
- PR #226 (v0.5.4): `contexto run claude "prompt"` (a prompt right after `claude`) failed on Claude
  Code 2.1.302 and 2.1.310 with "MCP config file not found", because the variadic `--mcp-config`
  took the prompt as a file. The launcher now passes `--settings=<file> --mcp-config=<file>`. The
  user `--settings` guard now checks every claude argument, including those after `--`: a
  `--settings` after a `--` that was another option's value could bypass the proxy. A prompt that is
  exactly `--settings` or starts with `--settings=` is now refused (exit 2), even after `--`.
  `spikes/m2/argv/argstub.py` checks the launcher's command line in eight scenarios at zero spend,
  for each new Claude Code version (`docs/cli-updates.md` step 3). argstub and permstub keep Claude
  Code on loopback, with no plugin-marketplace SSH clone, and delete only a `--work` they made or an
  existing empty directory.
- D45 (PR #225, v0.5.5; contract rule 18): the JSON sources rule 18 lists (request and response
  bodies, SSE events, MCP lines, the tool output the index lists keys from, Codex call arguments,
  and page files) are read through one 512-level nesting limit. The user's own settings files are
  still read with plain `json.loads`. A request past the limit is forwarded untransformed and logged
  `request_too_deep`, so a deep tool result or input no longer stops paging for the session. The
  Messages API itself refuses bodies nested more than 127 levels (measured 2026-10-25, 3,840
  tokens).
- PR #228 (v0.5.6): every page file is read through one check (`load_page`, `check_page`), so a
  hand-damaged page is logged `page_unreadable` and handled the same way everywhere (rule 18):
  search and the index skip it, `contexto_recall` and a `contexto_pin` that cites it return a tool
  error, and `contexto recall` exits 1. Before, a bad stub or `messages` failed every index build
  and paging stopped. Inside `messages`, a value of the wrong type is shown as `[malformed ...
  omitted]` and is not searched. `contexto_search` leaves out a page that went bad after it was
  listed. Re-paging onto a page file that is not a page record, or names another id, is a
  `page_conflict` that quarantines and re-pages it. Search and the index log a page file whose
  content is unreadable once per process until it changes, and each recall or pin of it logs it
  again; a failed read (permissions, too many open files) is retried on the next call.
- PR #222 (v0.5.2, defensive): with `--observe-thinking`, a response's `input_transformations` are
  merged across `message_start` and `message_delta` (top level and under `usage`) by count, in
  first-seen order, capped at 32 items with `input_transformations_truncated: true` when cut. A
  later empty list can no longer erase a `thinking_dropped` report. A field seen only as empty
  lists still logs `[]`. The O6 check's streams carried the field only in `message_start`, so its
  `[]` readings stand. Summaries of upstream values are depth-bounded (16 levels).
- D46 (PR #227, v0.5.7): with tool search on, the missing-tools check also reads 4.x ids'
  `<system-reminder>` listings (no more false warning on claude-sonnet-4-6); a subagent environment
  message led by the deferred-tool listing is session start; new context-block kinds
  `deferred_tools` and `nested_instructions`. Nested memory (`CLAUDE.md`, `CLAUDE.local.md`,
  `.claude/rules/*.md` below the root) on 5.x ids arrives next to the Read result on 2.1.310 and is
  carried again once its turn is paged, with the newest protected only up to 4,000 tokens. Carried
  growth after the fallback, one ~1,000-token CLAUDE.md per directory: on #227 reviewer A's fixture,
  7,421 tokens at 40 directories with the cap against 49,549 without it; on the PR's own fixture,
  7,769 at 10 directories and 7,891 at 40. On 4.x ids it is still paged with its turn (see "Needs
  the owner"). Numbers: `docs/research/M2-claude-code-2.1.310.md`.
- D47 (PR #230, v0.5.8), follow-ups to D46 from #227's round-3 review: a tool-result message with
  Claude Code's synthetic missing result (`is_error` true, `[Tool result missing due to internal
  error]` as its whole text) carries nothing and is not read by the missing-tools check. Files that
  nested memory imports with `@path` are carried within the 4,000-token cap. The cap counts each
  block as its page, the way carried growth does: with 500 and 2,000 tiny files the protected
  blocks add 4,000 and 3,907 tokens, against 5,312 and 5,360 before; the ~1,000-token fixture is
  unchanged. A queued prompt in a role `system` text (5.x ids) is no longer read by the
  missing-tools check. Recall says "reading its stored file failed" for an OSError and "is damaged"
  otherwise, and a directory named `pg_*.json` is not listed as a page.
- Harness (PR #223): the Claude Code 2.1.302 scoring fixes from the Opus run are in
  `spikes/m2/acceptance`: `--model` on `live`, `check-credential` and `stubrun` (default
  claude-haiku-5-5); an opt-in `--allow-contexto-tools`, redundant since D44 and best left off,
  since it would hide a D44 regression; the title side request is no longer a turn end; replay
  decodes parallel tool results. On the Opus session, `run.py check` credits q_p_03 with 24 turn
  ends, without out-of-repo scripts.
- D43 (PR #218): MIT license. Releases from v0.5.2 on ship `LICENSE` in the wheel and sdist.

## Acceptance, live run #3 (main fbb1303, claude-haiku-5-5, bound 150,000, owner's proxy)

Numbers-only record: `docs/research/M2-acceptance-run3.md`.

| Criterion | Result |
|---|---|
| 1. Long run | PASS: 24 of 24 turns, 0 compaction events, max upstream input 109,868 of 150,000, 0 model 4xx, 0 local 5xx, 0 failure events (`turn_room_low` 0, `retained_stale` 0), usage on 44 of 44 requests, 6 rotations; the spend watcher stopped the CLI after the last response was fully logged and stored (exit 143, nothing lost) |
| 2. Fidelity (D27: 54 probes over 6 turns of 9) | PASS: Contexto 51, baseline 48; no category below the baseline |
| 3. Recall | PASS: the 4 probes answerable only from paged-out turns were all correct and credited through search hits from those pages (lines from the conversation in view are never credited) |
| 4. Cache | 36 of 37 eligible requests read the cached prefix (97.3 percent); the one miss is route-side (O6) |
| 5. Tests | 1071 passed, 1 skipped on main (75105f5); CI green |

Per category (Contexto / baseline): chain 28 / 25, obligations 5 / 5 of 6, multihop 8 / 8,
distractor 6 / 6, paged-only 4 / 4. Contexto missed 3 (2 chain, 1 obligations); the baseline
missed 6, every one a stale value, which is consistent with the owner's premise that very long
contexts hurt accuracy. One run on one model, so the 3-probe lead is a measurement, not a claim of
superiority. The baseline is a no-replayed-thinking baseline (O6).

How it got here, all on record:

| Run | Protocol | Contexto | Baseline | Why the next change |
|---|---|---|---|---|
| H1 #1 | single turn | 16 | 44 | model made 0 tool calls: recall preamble V2 (D25) |
| H1 #2 | single turn | 18 | 44 | one recall used 89 percent of the turn's room: line hits, half-room cap (B15, B16, B17); 54 questions in one turn cannot fit the room even for an ideal searcher, so 6 turns of 9 (D27) |
| Live #2 (c8f0b08) | D27 | 44 | 45 | criteria 1, 3, 4 passed; chain 22 vs 24 failed by one probe: search showed old values first (D28) |
| Live #3 (fbb1303) | D27, new content | 51 | 48 | passed |

D28's targeted experiment (532,157 tokens) replayed one of live run #2's failing turns on the
fixed search before the full rerun: 5 of 9 became 8 of 9, 0 regressions.

## Acceptance on Claude Opus 5.5 (main 9d2949a, claude-opus-5-5, bound 150,000, owner's proxy)

Numbers-only record: `docs/research/M2-acceptance-opus.md`. On Claude Code 2.1.302, seed 20261016
(run #3's content), all five criteria pass with two departures from the contract: its pinned
model (`claude-opus-5-5`, which the owner approved) and its default settings (a `mcp__contexto`
permission allow rule). On defaults, 2.1.302 refused every Contexto tool call (attempt 1: 18 of
54, acceptance 1 still clean); D44 (PR #224) fixed that in the launcher.

| Criterion | Result |
|---|---|
| 1. Long run | PASS: 24 of 24 turns, 0 compaction, max upstream input 112,669 of 150,000, 62 requests all 200, 0 model 4xx, 0 local 5xx, 0 failure events, 6 rotations (all `min_turn_room`), CLI exit 0 |
| 2. Fidelity | PASS: H1 53 of 54, baseline 54 of 54; chain 29 against 30, every other category level |
| 3. Recall | PASS: paged-only 4 of 4 correct. q_p_03 had a paged search span returned before its answer; the other 3 were correct but not credited, consistent with a pin the model made in content turn 1 (inferred path). That pin held all four values, q_p_03's included, so none of the four needed paged-out content |
| 4. Cache | PASS: 54 of 55 (98.2 percent). The miss is the main conversation's cold first write, after Claude Code's title side request opened the epoch, not a route miss |
| 5. Tests | 2065 passed, 1 skipped, 1 xfailed at 9d2949a; CI green |

Tokens: 7,476,203 for the verdict run (H1 4,726,388, baseline 2,749,815) of a 9,440,000 approval,
plus 2,621,358 for attempt 1. The product (fbb1303 to 9d2949a), the client (2.1.301 to 2.1.302)
and the baseline's replayed transcript changed along with the model, so the record does not
attribute the differences from run #3 to the model. Its two 2.1.302 scoring corrections (the title
side request as a turn end, parallel tool results in the replay decoder) were applied out of the
repository at the time and are now in the harness (PR #223).

## Acceptance on Codex (main ca05b29, gpt-6-astra reasoning low, bound 64,000, owner's proxy)

Record: `docs/research/M2-codex-step1-live.md`; decision D30.

| Criterion | Result |
|---|---|
| 1. Long run | PASS: 21 of 21 turns, history 164,844 (at least 160,000 needed), 0 compaction, 44 requests all under the bound (max input 26,066), all 200 and completed, 0 failure events |
| 2. Fidelity | PASS by arithmetic: Contexto answered 54 of 54 (rescored independently by reviewer-1), so no baseline can be above it; the baseline was not run (D30) |
| 3. Recall | PASS: 4 of 4 paged-only probes reached the model through `contexto_search` (as scored after B30, #169). The run's harness scored 1 of 4 because it located 3 plants on later pages that also held them; B30 fixed that, and the Claude runs were unaffected |

Spend 922,201 of a 5,385,600 line (74.0 percent of input cached). Criterion 4 (cache): the
record's cache hit rate is 1.0. The model searched 84 times,
pinned 9 records, and never called recall. Network samples saw no connection outside the proxy
with analytics and plugin sync off.

## Codex acceptance at the default bound (main 9d2949a, gpt-6-astra low, bound 150,000, owner's proxy)

Numbers-only record: `docs/research/M2-codex-acceptance-150k.md`. Step 1 only, as in D30, on
codex-cli 0.163.0.

| Criterion | Result |
|---|---|
| 1. Long run | PASS: 40 of 40 turns (34 content, 6 probe), history 375,922 harness-priced tokens (about 233,000 real, derived as 34 × 6,866) of 375,000 needed, 0 compaction, max input 108,565 of 150,000, 61 of 61 requests 200, 0 4xx, 0 real over-bound, 0 failure events, 3 rotations (all `min_turn_room`) |
| 2. Fidelity | PASS by D30's arithmetic: 54 of 54; no baseline run. The replay does not fit as the harness prices it (421,448 harness-priced tokens); at the measured byte rate its largest request would be about 247,000 real tokens (modeled), and fails the 272,000 window only with the 32,000 output allowance |
| 3. Recall | PASS: 4 of 4 paged-only probes credited (22 credited overall) |
| Spend | 4,988,562 of 14,014,001 approved (35.6 percent); 92.0 percent of input cached |

Against the 64,000 run: 40 turns against 21, largest input 108,565 against 26,066, 3 rotations
(all room) against 19 (16 bound, 3 room). Main moved 295 commits between the runs, the affinity
fix was deployed, and Codex went from 0.154.0 to 0.163.0, so the record attributes only the
window, rotation and spend differences to the bound.

## Usage against plain Claude Code (run #3's session, zero-spend analysis)

Record: `docs/research/M2-usage-savings-run3.md` (per-request CSV alongside). Contexto's side is
measured; plain Claude Code's side is modeled from the CLI's own request bytes for the 18 content
turns and measured from the baseline arm for the 6 probe turns.

| | Input tokens | Cost at Haiku 5.5 prices |
|---|---|---|
| Contexto (measured; 2,677,349 cache read, 1,047,954 cache write, 88 uncached) | 3,725,391 | about $0.29 |
| Plain Claude Code, plain-run shape (18 content turns modeled, 6 probe turns measured; best-case caching) | 6,972,335 | about $0.58 |
| Saving | 46.6 percent | 49.7 percent (73.0 percent with the baseline's measured cache misses) |

By phase: content turns 69.0 percent fewer tokens; probe turns only 9.3 percent fewer, because
multihop probes add dependent search rounds that each resend the window
(`docs/research/M2-tool-rounds-lever.md`: 13 of 14 extra rounds are dependency hops; batching would
save about 2 percent, so neither multi-key search nor batching guidance is built). Plain Claude Code
would not have compacted in this session (peak 493,902, modeled, against about 967K). 7 Contexto
requests went just over Haiku 5.5's 100,000-token price tier (5x above it, M0 cost model), all in
tool loops. B27 (`docs/research/M2-haiku-100k-tier.md`): replaying run #3 at bound 125,000 with the
model's behavior held fixed kept every request under the tier, for a modeled 45.7 percent lower
cost, with 9 rotations instead of 6; accuracy at that bound is unmeasured and long tool loops can
still cross it. Decision: no default change and no live run (the owner's seats run Opus 5.5,
which has no price step); `docs/run-reference.md` gives `--bound 125000` as a Haiku tip with those caveats. Limits: one synthetic session, one
run, one model. The saving in money is Haiku 5.5's price step: the same token counts at flat rates
cost more through Contexto (modeled: 34 percent more at Haiku 5.5 base rates, 66 percent more at
Opus 5.5 rates) when every plain request hits the cache; with the baseline arm's two measured
misses plain costs more in both (section added to the record 2026-11-12, D68).

## Usage against plain Codex (Codex acceptance run, zero-spend analysis)

Record: `docs/research/M2-codex-usage-vs-plain.md` (with the cause correction, #177). Contexto's
side is measured; plain Codex's side is modeled from the request sizes in the proxy log. One run,
bound 64,000, 20 KB turns.

| | Input tokens | Cost at gpt-6-astra prices |
|---|---|---|
| Contexto (measured; 680,832 cached) | 919,998 | about $3.78 |
| Plain Codex, plain-run shape (modeled, best-case caching) | 1,672,525 | about $3.05 |
| Difference | 45.0 percent fewer | **24.0 percent more** (15.5 percent more pricing uncached input at the input rate) |

Content turns: 71.0 percent fewer tokens and 12.5 percent lower cost. Probe turns: 8.1 percent
fewer tokens but 2.3 times the cost. The main cause is Contexto's handling of restarts, not the
bound: the acceptance harness started a new Contexto proxy for each of the 21 `codex exec` turns,
and a new proxy kept no epoch state, so every one of the 19 rotations was the first request after
a restart (verified from the run's log). Each rotation changes the text right after Codex's
9,088-token head, so 17 of those 19 requests read only the head from cache. A zero-spend replay (A28,
`docs/research/M2-cache-aware-rotation.md`) models resuming the epoch after a restart at 9 rotations
instead of 19 and 9.8 to 16.2 percent lower cost, depending on the bytes-per-token rate; that is
rule 16, released in v0.2.0 (A30, B31; replaying the run through the released engine gives
the same number of rotations, 9). Codex's own compaction would not have fired (plain peak 116,731,
modeled). Users who keep one proxy for the whole session do not hit these restarts.

## Token spend on live model calls (owner's proxy unless noted)

- M0 study, lane 1: about 4.9M as reported by manager-1 (documented parts in
  `docs/research/M0-proxy-measurements.md`, `M0-codex-measurements.md`, `M0-rollover.md`, and
  81,319 for the route-identity test). Lane 2: about 1.0M, summed from its research docs.
- Codex smoke: 161,549 (API-key mode). Earlier Codex seat measurement: 410,827 on a ChatGPT
  login.
- Acceptance before D27: H1 #1 1,783,815; aborted attempt up to about 65,000; recall experiment
  415,413; H1 #2 2,015,165; baseline 447,659.
- Live run #2: 5,897,784 (Contexto 3,260,912, baseline 2,636,872).
- D28 experiment: 532,157 of a 740,000 line.
- Live run #3: 6,382,449 of a 6,880,000 line (Contexto 3,748,084, baseline 2,634,365). Contexto
  used more than the modelled 2,952,139 because the model made more searches and tool-loop
  requests per probe turn than the model assumed; still under its 4,007,279 cap.
- 2026-10-25: Opus acceptance 7,476,203 of a 9,440,000 line, plus 2,621,358 for attempt 1
  (10,097,561 in all, PR #221). Codex at 150,000: 4,988,562 of 14,014,001 (PR #219). O6
  dropped-thinking check: 1,159,358 (PR #220). Messages API depth check: 3,840 (PR #225). Claude
  Code 2.1.310 live recall check: 305,223 over two attempts (PR #227).
- 2026-11-05 QA run: about 4.94M (live-claude 509k, live-codex 316k, live-fidelity 1.41M,
  verify-images 103k, verify-obligations 1.13M, live-realworld 1.47M).
- 2026-11-05 fix round: D51's live standing-rules check 865,443 of a 1,200,000 budget (PR #242).
- 2026-11-06: live acceptance run #4 6,001,818 of a 9,865,000 line (H1 3,422,988, baseline
  2,578,830; the baseline's first attempt spent 0).

## Scale to 30M tokens of history (zero-spend measurement, measured 2026-10-17, recorded 2026-10-18)

Record: `docs/research/M2-scale-30m.md`; harnesses in `spikes/m2/scale/`. Synthetic sessions, local
fake upstream, no model calls. The carried-content findings in this section were measured before
D32 and D33 (#203); what remains after them is under "Known limitations".

- Claude Code 2.1.302 resumed and resent a 30M-token history (52.6 MiB request, 0.7 s, about 1 GB)
  and up to 525M; from 550M it stops with "API Error: Out of memory". With Contexto-like reported
  usage it never compacted.
- The proxy kept every upstream request under 150,000 (counted by the fake upstream as bytes /
  1.83) at 30M in a synthetic session with reminders on the first message only and one
  session-start system message, and added 0.58 s per tool-loop request and 0.86 s per turn start.
  Claude Code 2.1.302 places context blocks that way on 5.x model ids but not on 4.x ids (next
  bullets), and on 5.x ids it also sends a role `system` message with every main request, which the
  30M run did not model.
- Where Claude Code 2.1.302 puts reminders (`spikes/m2/scale/reminders/`, 48-turn sessions against
  a scripted local model). On 4.x model ids (claude-sonnet-4-6; 12-turn probes on claude-opus-4-7,
  claude-sonnet-4-5, claude-haiku-4-5) it puts `<system-reminder>` blocks in every later turn-start
  user message, which Contexto carries and never pages: 45 tokens per turn with bare prompts, 215
  in a busy one-process session, 425 with a resume every turn (Contexto's estimator). So the
  blocker is real on 4.x ids. [Inferred, linear from the scale test's failure at turn 181 at 215
  per turn; a rough guide] `bound_too_small` from about turn 860, 180, and 90. By bytes per turn
  the busy rate is 2.4 times the scale test's profile, which would put the busy session's failure
  near turn 77 (about 2.1M). Also measured: the token countdown is on by default in 2.1.302, a
  plan-mode entry adds about 2,720 tokens, and on resume a file-changed note is sent again on every
  launch until the file is read again.
- On 5.x ids (claude-opus-5-5, claude-haiku-5-5; probes on claude-sonnet-5, claude-fable-5) the
  same content arrives as role `system` messages, one per main request, which the 8,000-token
  fallback can page. On claude-opus-5-5 and claude-haiku-5-5 one uncapped path is left: an effort
  change puts `output_config` on that turn's first system message, and `classify_system` (written
  against 2.1.301) then classifies it as session-start, carried forever. 16 changes grew that
  class from 2,770 to 9,272 tokens (one process) and 11,637 (resume every turn), Contexto's
  estimator, and that was all of the unpageable growth. With bare prompts and no effort change
  (claude-opus-5-5), nothing unpageable grew over 48 turns. On the claude-sonnet-5 and
  claude-fable-5 probes effort stayed top-level and nothing unpageable grew; there the session's
  environment message has no `output_config`, classifies as carried text, and is the first message
  the fallback would page.
- What that means for the owner's seats (Opus 5.5, claude-opus-5-5), measured: per-turn reminders
  are pageable, and only effort changes add carried content that is never paged, 27 to 3,444
  tokens each (bytes / 1.83). The 4.x blocker applies only to sessions on a 4.x id.
- What that means for the owner's seats, [inferred, not measured]: the per-request role `system`
  text probably still fits at 30M. Carried content counts in the fixed head F, and more F shrinks
  the window rather than pushing requests toward the bound. F was about 70,000 at 30M against the
  100,810 at which `bound_too_small` first fired in the `mixed` run, about 30,800 tokens of room;
  the pageable text held 410 to 2,143 once the fallback engaged in the 48-turn resume run (at each
  rotation the fallback pages it back to about 8,000 or less), and
  effort-change content adds to the same F. The fallback writes each paged message as its own
  page, so such a session would reach about 3,000 pages or more by 30M, most of them single small
  messages; the store spike's 3,000 to 6,000 rows (after D34: planted page in the 3 detailed
  results for 60 and 56 of 86 queries, 0.04 to 0.22 s per search with every page's text held) are a
  rough guide for that shape. Under D39 search time depends on the store's text bytes, not its page
  count: many small pages stay within the 64 MB default.
- Search (D34: every word in any case, BM25 with a recency prior, cached page text) takes 0.10 to
  0.25 s at 6,000 pages when the MCP server holds every page's text uncompressed, and the first
  search of an MCP process 0.8 s. With D39's default 64 MB (2,085 such pages uncompressed, about
  48M tokens; the whole 6,000-page store compressed), it takes 0.26 to 0.41 s, the first search
  1.6 s, and the server's peak RSS is 134 MB instead of 290. The planted page reaches
  the 3 detailed results for 71 of 86 queries at 17 pages, 63 at 1,000, 60 at 3,000, and 58 at
  6,000 (D41; 56 with D34's ranking; before D34: 61, 50, 44, 39), and the latest value comes first
  for 20 of 24 at 6,000 (before D34: 12). On pages sized like real turns at 6,000, D41 puts the
  planted page in the top 3 for 58 of 86 queries (53 with D34), the latest value first for 20 of
  24 (17), and no query shows only an old value (3).

## Decisions taken in M2 and why (full text in DECISIONS.md)

- D18: build H1, context virtual memory at the API boundary; the only approach that bounds the
  context without a lossy reset.
- D19: recall through MCP tools, not a hidden proxy tool; far simpler and no shadow history.
- D20: strip all thinking at each rotation; measured to return 200 after a history edit (its
  accuracy cost could not be isolated in M0).
- D25: recall preamble V2, after the model made no tool calls in H1 #1.
- D26: acceptance fails on any off-path event, not only over-bound or 4xx.
- D27: probes over 6 turns of 9 for both arms; one turn cannot hold 54 deep questions' evidence.
- D28: fix stale chain answers in search (matches in view first, latest lines per page), then
  rerun both arms. Rejected: accept as a limitation, widen the tolerance, rerun one arm only.
- D29: cut v0.1.0 after acceptance, on the owner's instruction to come back to a finished
  project; GitHub Release only, no package registry.
- D32: the bounded fallback also pages context blocks of later turn starts (4.x ids), so carried
  content stops growing with them.
- D33: session-start is recognised by its content, not by settings fields, so an effort change no
  longer makes a reminder unpageable (5.x ids).
- D34: search matches every word in any case, ranks by BM25 with a recency prior (paged context
  blocks after every turn, in rank and in the reply), and both processes cache page reads.
- D35 (resolves O4): end users run Contexto against the vendor API with an API key, or a
  CLIProxyAPI instance; logins live in CLIProxyAPI or the vendor's own CLI, never in Contexto;
  users follow their providers' terms. Guide: `docs/cliproxyapi.md`.
- D36 (resolves O6): upstream affinity. Contexto sends a salted per-session `X-Session-Affinity` to
  non-vendor upstreams; the CLI's own session ids rank above it. On the proxy side, CLIProxyAPI's
  `routing.session-affinity-consistent-hash` (proposed upstream as router-for-me/CLIProxyAPI#6954)
  pins each session to one credential on every replica; deployed on the owner's proxy 2026-10-24.
- D37: `contexto sessions prune` deletes whole sessions by `--older-than`, `--keep`, `--session`;
  dry run unless `--yes`; `--json` available. Writable stores hold a shared flock on `live.lock`,
  and prune never deletes a session whose lock is held or that was active in the last 10 minutes.
  It deletes through open directory descriptors, so a swapped-in symlink cannot redirect it.
  `sessions list` shows size on disk and a total.
- D38: `contexto run` warns once on stderr when Claude Code or Codex is a version Contexto was not
  tested with, and still launches; the proxy logs each distinct CLI version once (`cli_version`,
  at most 8) and `session_start_unrecognised`.
- D39: the MCP server holds search text up to `CONTEXTO_SEARCH_CACHE_MB` (default 64), the newest
  pages uncompressed and older ones compressed, and reads the rest from the store at each search;
  results do not change.
- D40: each subagent and side request in a tool loop has its own recall room, and a tool call is
  charged against every live room, because the MCP server cannot tell which conversation made it.
- D41: search counts a term once per text that holds it, at most 3 per page, and drops BM25's length
  term (b 0), so neither a long turn nor an old dump that repeats a key buries the turn that states
  its value; chosen on a measured grid (probes, 13-turn cases, five dump sets).
- D42: the repository goes public (2026-10-24); GitHub Releases stay the only distribution (no
  package registry). D0, D13, and D14 are marked superseded in place.
- D43: Contexto is MIT licensed (owner, 2026-10-25). The `LICENSE` file and the license metadata
  ship in the wheel and sdist from v0.5.2 on; v0.5.1 and earlier assets do not hold them.
- D44: `contexto run claude` allows Contexto's five MCP tools by exact name in its `--settings`,
  because Claude Code 2.1.302 in `-p` mode refuses any MCP tool no rule allows; a refusal that
  still happens logs `mcp_tools_denied` and fails acceptance 1.
- D45: one 512-level nesting limit for the JSON sources contract rule 18 lists; a request past it
  goes upstream untransformed and logs `request_too_deep`, an acceptance 1 failure event because the
  Responses API's own limit is unmeasured.
- D46: Claude Code 2.1.310 is tested. Contexto reads its tool-search shapes (listings in 4.x
  reminders, a subagent environment message led by the listing) and carries 5.x nested memory
  that arrives next to a tool result, the newest up to 4,000 tokens. The full live acceptance
  (step 9 of `docs/cli-updates.md`) did not run; a 305,223-token live recall check stands in.
- D47: follow-ups to D46. A tool-result message with Claude Code's synthetic missing result carries
  nothing and is not read by the missing-tools check (it still goes to the model unchanged); files
  nested memory imports with `@path` are carried within the cap; the cap counts each block as its
  page; a queued prompt in a role `system` text is not read by the missing-tools check.
- D48: images are priced by their pixels (`width * height / 750`, 64 to 4,784; D61: or the 28-pixel
  patches plus 3 when higher, at most 4,787), and images and
  documents are kept out of the bytes-per-token ratio, so a screenshot no longer pages the window.
- D49: a head change is sized like any rotation, and every forwarded request is checked against the
  bound; amended (PR #244) to measure the head from the first usage that has an input count.
- D50: main follows Claude Code's session id across `/clear` and `/resume`, and its compaction
  summary across `/compact`; a side request that arrives first stays main only until the first
  prompt.
- D51: the model is told to pin standing rules when stated and keep them open; Contexto's own
  search, recall and state calls are not searched or indexed.
- D52: settings files are read in Claude Code's order; project settings count for Codex only in a
  folder Claude Code recorded as trusted; `CLAUDE_CODE_IGNORE_PERSISTED_TRUST` turns them off for
  every CLI.
- D53: the proxy answers only requests with a loopback `Host` and no `Origin`, shuts upstream when
  the client leaves, and forwards an early upstream answer instead of retrying.
- D54: `contexto run codex` runs the key helper through `/bin/sh -c`, refuses an Anthropic upstream
  for Codex, passes `--no-daemon`, and checks the launch before opening a session.
- D55: `contexto run claude` resumes through the Contexto session that served the conversation,
  survives signals (a watchdog for SIGKILL), and prints its notices after Claude Code exits.
- D56: Claude Code 2.1.314 and 2.1.318 and codex-cli 0.166.0 are tested; the new subagent stop
  reports are read.
- D57: page ids are findable (`sessions show`, `contexto search`), `state` is readable, read-only
  commands take id prefixes, failed launches leave no session, exit codes are consistent.
- D58: the store and the MCP server survive lone surrogates, non-regular files and full disks, and
  report damage by name.
- D59: the review follow-ups of the fix round (Host check by name, abort after the last event, the
  `choose.lock` wait, Claude Code's option list, one `codex --version`, damaged locks, compaction
  shapes).
- D60: a PDF is never estimated low: 4,568 tokens a page when its pages are counted for certain,
  else 600 pages.
- D61: the fix round's remaining review lows (Codex tool output at the 1.5 seed before usage, older
  epoch records measure their head again, a bounded JPEG walk and patch pricing, each CLI's own
  overheads in the start warning, `pdf_uncertain` only when the PDF is the cause).
- D62: the v0.1.0 to v0.4.0 releases and tags are deleted, because their source archives held the
  owner's proxy hostname; their commits stay on `main`.
- D65: when strict search finds nothing, search returns approximate matches, labelled as such with
  the words each page matched; deterministic, no embeddings or model call. Its one-letter typo
  step is the one exception to the owner's ruling against fuzzy matching that D34 records.
- D68: the README is rewritten for a first-time reader (what it does, three numbers with their
  limits, install and start on the first screen; who it is not for; a comparison with compaction, a
  1M window and Claude Code's memory), with recall stated as no worse than full context and cost as
  model-dependent; the reference, results and limitations moved under `docs/`.
- D69: project memory. A new session carries the open obligations and active decisions the main
  conversation pinned in its project's earlier sessions, as records and not instructions, with a
  start line, `contexto project show|forget` and an off switch shipped in the same pull request.
  Main-conversation records are confirmed from the main conversation's own history; removals are
  project-wide; forgetting never reopens a record, pruning does.

## Open follow-ups

PR #250 (D61, v0.5.24) fixed the low findings the fix round's reviews left:
- **Codex tool output** counts at the 1.5 bytes-a-token tool-output seed until real usage arrives,
  as Claude's tool results do. Reviewer A's harness (1,440 sessions at bound 30,000): requests over
  the bound unflagged 6,451 on main, 445 now; turn starts over it 89, now 0. Codex sessions that
  never report an input count rotate 9 to 20% more often.
- **Older epoch records** (every release through v0.5.23) resume with their head and window budget
  dropped, so the head is measured again; v0.5.8's low heads no longer carry over. A downgrade
  starts from epoch 0.
- **Images:** a crafted JPEG no longer slows pricing (100 images 0.75 to 0.97 s, now 0.032 to
  0.037 s); thin images are priced at their 28-pixel patches when that is higher.
- **Start warning:** quotes 13,842 (Claude Code 2.1.314, a fresh config) to about 28,000, and for
  Codex its own 13,322 to 18,403.
- **Uncertain PDFs:** the `pdf_uncertain` advice appears only when the PDF is why the turn does not
  fit; otherwise the launcher advises a larger `--bound`.

Still open:

- A head denser than the 3.5 text seed is estimated low until the first usage, on Codex and Claude
  alike, so tool-loop requests can still go over the bound unflagged when no response carries an
  input count (12 of 32 synthetic Codex sessions in D61's measurement, 445 requests in reviewer A's).
- High-resolution screenshots in a conversation's first request make the head run high (log-only
  `turn_room_low` and `window_minimal` at bound 60,000); Codex pages show no `[image omitted]` marker
  for a screenshot (as on v0.5.8).
- Not started: a byte budget per search on the pages `contexto_search` reads (D41 follow-up): it
  reads and renders up to 53 pages per search with no cap on their size (18.5 MB of page JSON for
  one multi-word query at 3,000 pages sized like real turns).
- Open from D49: a head too large for `min_turn_room` (bound 60,000, a 28,000-token head) leaves the
  minimum-room rotation (A12) nothing to restore, so later turn starts keep their window (about 34,200 real, room 806,
  `turn_room_low` logged once); PR #232 review B's finding 1.

## What is left (as of 2026-11-13)

Definition of done for this project: M2 acceptance passed on both CLIs (done), every release
from v0.5.0 on published with its wheel and sdist, and STATUS, README, and the contract accurate to `main`.

Earlier work, all merged and released: the 30M plan (D32 to D34, #202 to #205, by v0.4.0), the
2026-10-24 work (D35 to D41, v0.5.0 and v0.5.1), and the 2026-10-25 work (D43 to D47, v0.5.2 to
v0.5.8); see "Releases".

The 2026-11-05 QA run's fix round is merged and released: D49 (#232, v0.5.9; amended #244,
v0.5.18), D56 (#243, v0.5.10), D51 (#242, v0.5.11), D55 (#240, v0.5.12; #245, v0.5.13), the docs for
first-time users and release checksums (#233, v0.5.13), D52 (#237, v0.5.14), D50 (#238, v0.5.15),
D53 (#239, v0.5.16), D57 (#241, v0.5.17), D58 (#235, v0.5.19), D54 (#236, v0.5.20), D48 (#234) and
the replay fix (#247) in v0.5.21, D59 (#246, v0.5.22), D60 (#248, v0.5.23), the docs pass (#249) and
D61 (#250, v0.5.24). Live acceptance run #4 passed on v0.5.20. Left: the open follow-ups above.

D69 (project memory): the first pull request, #279 (carry-over, rule 19, the off switch, the start
line, `contexto project show|forget`), is under review, unreleased. Left: the second pull request, search
and recall across a project's earlier sessions (`contexto_search` and `contexto_recall`) with the
measurements of what a later session can reach and what it costs.

Measured candidates deliberately not pursued (each would need a live run or new evidence):
- Keeping less of the recent conversation in view (`window_fraction` 0.25): 3 to 13 percent
  cheaper in replays, but it shows the model less, so it needs an accuracy run first (A28).
- A Haiku default bound of 125,000 (B27): only a tip in `docs/run-reference.md`; accuracy at that bound unmeasured.
- A search "follow" option for multihop chains (B26): the hops come from synthetic probes.
- A live check of 150,000 on an OpenRig seat (D31 says revisit only if a seat shows a recall
  cost).
- Answering Claude Code's `HEAD /api/hello` locally while the upstream comes from an untrusted
  project file (D52): for now `docs/troubleshooting.md` says to pin `--upstream` in such a repository.

Needs the owner:
- D69's live check: no live model call was made, so whether a model follows, questions or closes
  carried records as their heading asks is unmeasured, and so is project memory on real Claude Code
  and Codex binaries. The check is two `contexto run claude` sessions in one real project directory
  (in the first, state a standing rule, let the model pin it and send one more message so the pin's
  reply reaches a main request; in the second, check the start line, `contexto state`, and that the
  first reply honours the rule; then `contexto project forget`). A run from an empty temporary
  directory with a stub home is not a live check. It costs live tokens and needs the owner's
  approval. The same run would be the first live look at
  the new pin guidance (only the user's own words are pinned) and at the main-conversation evidence
  on a real CLI; a Codex session would check the structured reply Codex is believed to pass back.
- Live checks the fix round could not run at zero spend: D51's "keep it open until the user
  withdraws it" wording (added after the live run, not measured live), standing-rule pinning on
  claude-opus-5-5 and on Codex, and the full acceptance on Claude Code 2.1.314 (run #4 covered
  2.1.318). Step 9 of `docs/cli-updates.md` costs millions of tokens and needs the owner's approval.
- Nested memory on Claude 4.x model ids (D46): a nested `CLAUDE.md`, `CLAUDE.local.md` or
  `.claude/rules/*.md` arrives inside the Read tool result there, so it is paged with its turn. On
  5.x ids it is carried. Whether to carry it on 4.x ids too is the owner's call.
- Search ranking (D41): search ships with text cap 1, page cap 3. Text cap 3, page cap 3 restores
  main's two D34 scenarios at 41 more stale-only results, all in the quiet dump set (162 against
  121), with no other measured change. The owner asked to discuss it; the default stands until the
  owner chooses.
- The Responses API's nesting depth limit is unmeasured (D45). The Messages API refuses more than
  127 levels; if the Responses API accepts a request past Contexto's 512, that request goes
  upstream untransformed, which is why `request_too_deep` fails acceptance 1. A Responses check
  needs a live call.
- Old commits, pull request diffs and pull request comments still hold the owner's proxy hostname
  (the v0.1.0 to v0.4.0 releases and tags that carried it in their source archives were deleted,
  D62). Removing it from commits would mean rewriting history, which needs force pushes, and pull request diffs would still
  keep it unless GitHub Support purges them. The hostname has been public since 2026-10-24, so what
  protects the proxy is its own access control.

## Open decisions (details in DECISIONS.md)

- O3: settled by D31: the default bound stays 150,000, including for OpenRig seats, because a
  larger bound lowers the largest single turn that stays under Claude Code's 200,000 idle-compaction
  minimum (about 87k at 150,000 against 67k at 170,000 on a 92k head, modeled).
- O4: settled by D35 (owner, 2026-10-24; CLIProxyAPI support shipped in v0.5.0, #210). People who
  run Contexto themselves use either a vendor API key or a CLIProxyAPI instance. Subscription
  logins live in CLIProxyAPI or the vendor's own CLI, never in Contexto, and users follow their
  providers' terms.
- O5: settled by D29. v0.1.0 was cut from f990703 (tag and GitHub Release on this
  repository, sdist and wheel attached, no package registry; the release and tag were deleted with
  v0.1.1 to v0.4.0 on 2026-11-06, D62).
- O6: resolved by D36, deployed on the owner's proxy 2026-10-24 and verified for cache reuse. The
  dropped-thinking check ran on 2026-10-25 UTC (`docs/research/O6-thinking-check.md`). A 7-turn
  claude-opus-5-5 session with `--observe-thinking` made 18 requests; 14 of 17 main requests
  replayed signed thinking, and 0 reported `thinking_dropped`. By cache evidence the session stayed
  on one credential (16 of 16 exact prefix reads). A claude-haiku-5-5 control shows the API still
  reports drops when a replay lands on another credential (8 of 8; 0 of 8 on the same credential).
  On Opus, cross-credential replays reported no drop, so the signal cannot show Opus drops. Cache
  misses and dropped thinking were traced to the proxy spreading one session's requests across
  credentials.
- O7: done. OpenRig fixed the context-usage clock bug (UTC parsed as local time) in its PR #124
  (commit 4a920dba), first released in v0.6.1, and the host was upgraded to OpenRig 0.7.0 on
  2026-10-25. No report is needed.

## O6 (upstream affinity), resolved by D36: deployed 2026-10-24

- Deployed on the owner's CLIProxyAPI deployment on 2026-10-24, with `session-affinity` and the
  consistent-hash option on. The option hashes the session id so every replica picks the same
  credential; nested subagents rank with their root session. Upstream proposal:
  router-for-me/CLIProxyAPI#6954. Contexto (#212) sends a salted per-session
  `X-Session-Affinity` to non-vendor upstreams; the CLI's own session ids rank above it.
- Verified for cache reuse with a 10-request byte-identical claude-haiku-5-5 series using one
  affinity key: 1 cache write, then 9 reads, all served by one credential across several replicas
  (before: 3 writes). Measured on the owner's proxy on 2026-10-24 by the deployer and recorded in
  D36; no record of the run is in this repository.
- Dropped-thinking check, 2026-10-25 UTC (`docs/research/O6-thinking-check.md`): 0
  `thinking_dropped` reported in 18 responses of a claude-opus-5-5 session, with 14 of 17 main
  requests replaying signed thinking, and 16 of 16 exact prefix reads. On claude-haiku-5-5, replays
  onto another credential are still reported dropped (JSON and streaming, in `message_start`). On
  claude-opus-5-5 they were not reported here; the one Opus series on 2026-10-15 also reported none,
  with crossing unknown. 1,159,358 tokens.

## Known limitations (v0.5.24)

- Claude acceptance ran on two models, claude-haiku-5-5 (Claude Code 2.1.301 and 2.1.318) and
  claude-opus-5-5 (2.1.302, with a `mcp__contexto` allow rule before D44), at one bound (150,000)
  and through the owner's proxy. Run #4 on 2.1.318 used Claude Code's defaults, tool search on, so
  Contexto's tools were deferred. 2.1.310 had only a one-fact live recall check (D46), and 2.1.314
  the zero-spend routine only. Codex acceptance ran on gpt-6-astra at bounds 64,000 (D30) and
  150,000 on codex-cli 0.163.0, through the owner's proxy; 0.166.0 had the zero-spend routine only.
- Images (D48, D61) cost the higher of `width * height / 750` and the 28-pixel patches plus 3, 64 to
  4,787, the high-resolution tier's formulas; the patch model is inferred from four measurements and no
  live call checked a thin image.
  On models before that tier images cost less (at most about 1,568), so the estimate errs high by up
  to 3 times per large image. Codex uses the same formula and OpenAI prices images by tiles, lower,
  so a Codex estimate errs high: two turns an equal-cost text control keeps were paged in reviewer
  B's run. A screenshot in a paged turn is not recallable as an image: recall shows
  `[image omitted]`, and the model sees only the turn's text.
- PDFs (D60) are never estimated low, and pay for it: 4,568 tokens a page, about 2.9 times a typical
  1,583-token page, so a large PDF pasted at a turn start can page the earlier conversation (30
  PyMuPDF pages at bound 60,000 kept 1 message where an equal-cost text control kept 41); the
  launcher says it is the PDF's allowance that fell short (`pdf_allowance`). A PDF whose pages cannot
  be counted for certain counts as 600 pages (2.74 million tokens) and logs `pdf_pages_uncertain`:
  encrypted object streams, object streams under another filter or a predictor, pages that show PDF
  source as text, a PDF that needs more than 400,000 lexer steps (about 300 or more pages with 60 or
  more links a page), and every PDF by URL or file id, which is then over any bound. Pages an
  incremental save deleted still count. A dense page (small-font code, tables) can cost more than
  4,568. PDFs are parsed one at a time, the smallest waiting first; one that has waited 1 s goes
  next. 20 PDFs that spend the step budget take about 5 s together.
- Rule 4's measured head (D49 as amended) errs high except when the window has more bytes per token
  than the head (prose in the window, a JSON-dense head): then it is low by that difference, up to
  1,816 tokens (7.3%) in PR #244's reviews, with the bound held. If no usage ever carries an input
  count, the head is never measured and the estimator stands in for the whole session; the
  pre-send check is then the only bound check, and a head denser than the 3.5 text seed is estimated
  low, so tool-loop requests can go over the bound unflagged (D61: 12 of 32 synthetic Codex sessions;
  445 requests in reviewer A's 1,440).
- Project memory (D69, PR #279, unreleased): only records the proxy saw the main conversation make
  are carried. A pin whose reply never reaches a later main request (the session ends right after
  it), a Codex code-mode pin, and every pin of v0.5.24 and earlier are not, so project memory starts
  empty. Codex sessions give nothing: on codex-cli 0.163.0 the model calls Contexto's tools through
  code mode's `exec`, which the confirmation does not read, so a Codex session's pins are never
  carried and its closures of carried records count only in it (not checked on 0.166.0 or live). A
  conversation that took main by `more_turn_starts` (a subagent without `X-Claude-Code-Agent-Id`, a
  Codex child) is not trusted. `main_records` is a user-editable list in `meta.json`, a record of
  what the proxy saw, not an integrity guarantee. Text injected into the main
  conversation (a web page, a file, a tool result) that persuades the model to pin something is
  carried like any other main pin; the pin guidance and the heading that calls them records, not
  instructions, are the defence there, and no live model has been measured against either. Pruning
  the session that closed a record brings the record back. Each git worktree and submodule is its
  own project, and a symlinked directory is its target's. A session never carries from one started
  after it. Verified facts are not carried. A session of the project whose ledger does not verify,
  cannot be read or is over 64 MB gives nothing until it is repaired or pruned. The carried records
  share the 6,000-token pin budget, the session's own first. Reading the project lists every session
  in the home: with 200 sessions, about 4 to 6 ms warm per rotation's `pinned_text` call and per turn
  start of a session that carries records before its first rotation, 15.8 ms cold; 0.14 ms with
  memory off.
- Standing rules (D51): the model is told to pin a rule as an obligation when it is stated and keep
  it open. Measured on one model (claude-haiku-5-5) with 8 rules stated in their own turn; the
  "keep it open" words were added after that run and are unmeasured. A rule the model does not pin
  leaves view once its turn is paged; the index carries only its turn's first 200 characters, and
  its words stay searchable. A rule turn costs a second request for the pin (a mean of 45,000
  tokens against 21,426 at bound 60,000).
- Search does not read Contexto's own search, recall and state calls (D51). For Codex's code-mode
  `exec` this is a text check: a call is left out only when every mention of the word `tools` is a
  literal call of one of those tools and the code holds no `//` or `/*`. Code that reaches a tool
  without the word `tools` (`eval`, `globalThis[...]`, Codex's `load()`) beside such a call is not
  seen, so a shell call made that way is hidden from search (recall keeps it); strings holding `//`,
  `/*` or `tools` leave the echo searchable.
- Search has no synonyms, and no stemming beyond D65's one-letter typo step (D34, D65): `database`
  does not find `DB`. The fallback runs only when no turn holds every word, so when another turn
  holds every word of the query, search returns those turns and never looks further: 8 of 192 probes
  at 6,000 pages, 6 paraphrases and 2 with an extra word. Limits are listed in `docs/search.md`.
- Main conversation (D50, D59): main follows Claude Code's session id, measured on 2.1.302 to
  2.1.318. Without a session id (another client) only the turn-start rule applies. A headless
  `/clear` right after an SDK `interrupt()`, or after a late end of an older request that may hold a
  call, falls back to the turn-start rule. A compaction inside a tool loop takes main at its first
  request only when the summary request was answered with no client call; otherwise at the next
  prompt. A first prompt that reproduces Claude Code's compaction sentence and the blank line after
  it, in main's own session, is read as a compaction. Two interactive Claude Codes on one proxy are
  not a supported setup. After `/rewind` of the first prompt the new key stays side until it
  outgrows the old one (inferred).
- Resume (D55): `contexto run claude --resume` without an id opens Claude Code's picker, which
  Contexto cannot follow, so that launch gets a new session. A `--fork-session` fork shares the
  original's session (pages, pins, recall room); switching between branches re-rotates and search
  can return the other branch's turns. Pass `--session NEW` for a separate store. Codex resumes only
  with `contexto run --session <id> codex resume --last`. Two launches that wait more than 5 s for
  `choose.lock` choose without it and may pick the same session (the launcher says so).
- Signals (D55): SIGKILL of the launcher leaves no `exit` event; a watchdog ends Claude Code and
  removes the temporary files, and if the watchdog is killed too, the temporary directory (no
  secrets) stays.
- Untrusted repositories (D52): `contexto run claude` uses project settings as Claude Code does, so
  in `-p` mode, which has no trust check, a repository's own `ANTHROPIC_BASE_URL` wins; pin it with
  `--upstream`. Interactively, while Claude Code shows its trust dialog, its keyless `HEAD /api/hello`
  goes to that repository's upstream even if the user declines. For Codex, project settings count
  only in a folder Claude Code recorded as trusted; Claude Code may change where it records trust,
  and the rule errs toward untrusted (a dangling `.git` symlink, a `.git` FIFO, a bare repository's
  worktree).
- The proxy (D53, D59) refuses a request whose `Host` is not `127.0.0.1` or `localhost`, so a CLI in a
  container that names `host.docker.internal` gets 403; use a port forward to `localhost`. A client
  that half-closes its sending side counts as gone and gets no response. An early upstream answer
  (a 413) over TLS is read the same way, but a TLS upstream was not tested.
- Deeply nested JSON (D45): the Messages API refuses a request nested more than 127 levels
  (400 `invalid_request_error`), so once a message that deep is in what goes upstream, the session
  cannot continue, with or without Contexto. Past 512 levels Contexto forwards the request
  untransformed (`request_too_deep`). The Responses API's limit is unmeasured.
- Codex pages written by v0.1.0 to v0.1.2 were stored with empty index stubs. Since v0.2.3
  the store rebuilds such a stub from the page's messages each time it reads the page; the page
  file is not rewritten and its id is unchanged.
- Codex: HTTP is forced through a custom provider (no WebSocket). Contexto's tools sit behind Codex
  tool search and need `default_tools_approval_mode = approve`, which `contexto run codex` sets.
  Interactive launches pass `--no-daemon` from Codex 0.156.0 on, so `codex agents`, which needs the
  shared server, is refused unless `--remote` sends it elsewhere; `app-server` and `remote-control`
  were not measured and are not refused. Only API-key mode is live-tested; ChatGPT login mode is
  tested with synthetic credentials. In ChatGPT login mode some Codex calls (rate limits, usage, a
  remote MCP connection) go to chatgpt.com directly and bypass the proxy by design. By default Codex
  also connects to chatgpt.com (analytics) and github.com (plugin sync) outside the proxy;
  `-c analytics.enabled=false` and `-c features.plugins=false` stop them in network samples, and
  `contexto run codex` leaves both to the user. `preamble_first` is on but unmeasured there.
- One turn that needs more evidence than its recall room holds cannot get it all (D27); the room
  per turn is the bound minus the request's usage (input plus output) minus a 25,000-token
  headroom floor; the engine rotates at a turn start when that would leave less than 12,000
  (`min_turn_room`). A head change (tool search, an MCP server connecting, a model switch) is sized
  like any other rotation (D49), so it no longer leaves a turn without recall room.
- Subagents and side requests have their own recall room while in a tool loop (D40): a tool call
  gets the smallest live room and is charged against every live room, because the MCP server
  cannot tell which conversation made it. A side room expires 15 minutes after its last publish,
  or earlier when main's history shows its agent stopped. That early close works only on the Claude
  Code versions whose subagent shapes were measured (`SUBAGENT_SHAPES_TESTED`: 2.1.302, 2.1.310,
  2.1.314 and 2.1.318, which report the tools a subagent used before its hand-back, D56); on any
  other version side rooms only expire. A subagent that stopped without one can hold main's recall
  down until the expiry (`limited_by: side` with `limiting_age_s` in the log). Search's "in view"
  for a subagent still means the main conversation's window.
- Usage refines the estimate only for the request it measured (rule 3): with two requests of one
  conversation in flight, only the newer one's usage counts (`usage_unattributed` for the other),
  and after a request without usage (including usage whose input counts are absent or 0) the next
  estimate is a full one (`usage_missing`).
- Small bounds with large fixed heads leave little window: the launcher warns when the window
  budget is under 25,000, assuming a fixed overhead of 28,000 for Claude Code (measured 18,641 to
  27,925 on 2.1.314 with tool search on) and 19,000 for Codex. Use a larger bound for OpenRig
  seat-sized heads (O3).
- Very large single turns can still exceed the bound inside a tool loop. Every forwarded request is
  checked before it is sent (D49): `over_bound` with `in_tool_loop: true` is that expected overrun;
  at a turn start it goes with `bound_too_small` or means the sizing did not hold.
- Search (D34, D39, D41) holds page text in the MCP server up to `CONTEXTO_SEARCH_CACHE_MB` (default
  64 MB). The first search of each MCP process reads every page once (1.6 s at 6,000 synthetic
  pages). Single common words can still bury a fact at 6,000 pages (5 of 86 probe queries). A reply
  or file that discusses a key at length in one text weighs as one passing mention, and an old turn
  that names a key in 2 or more texts usually outranks a newer one-line reply that states its value.
  The pages one search shows are read in full with no cap (18.5 MB of page JSON for one multi-word
  query at 3,000 pages sized like real turns); a per-search budget is under "Open follow-ups".
- Turn numbers in search replies (D58) match the page numbering only while every page is a whole
  turn; carried-text pages from the bounded fallback and a resync make in-view numbers run behind
  recent pages and can repeat one. No tool takes a turn number.
- Carried content after D32 and D33 (#203, measured on Claude Code 2.1.302's captured bodies,
  Contexto's estimator): at turn 48 the content the fallback cannot page is 3,557 to 3,794 tokens on
  sonnet-4-6 and 4,897 to 5,643 on claude-opus-5-5 busy sessions; on 4.x ids, and on 5.x ids in one
  process, it stops growing from turn 12; on claude-opus-5-5 and claude-haiku-5-5 with a resume every
  turn it still grows about 21 tokens per turn. Session-start recognition depends on Claude Code's
  own `# Environment` text: re-run the routine in `docs/cli-updates.md` when the CLI changes.
- Nested memory (D46, D47): on 4.x ids it is paged with its turn (see "Needs the owner"). A round
  where a completed Read's nested memory sits next to another call's synthetic result is
  unmeasured, and `@~/x.md` imports sent nothing in the `-p` capture, so their form is unknown.
- Missing-tools check (D46, D47): still readable are an exact notice typed as an earlier block of a
  several-block prompt, a queued prompt if Claude Code changes its two sentences, and messages under
  Claude Code's other mid-turn openers. The check only logs and warns; it does not change what is
  paged.
- CLI versions (D38, D56): Contexto is tested with Claude Code 2.1.302, 2.1.310, 2.1.314 and 2.1.318
  and codex-cli 0.154.0, 0.163.0 and 0.166.0 (`TESTED_VERSIONS`, `src/contexto/cliversions.py`).
  `contexto run` warns once on stderr for any other version and still launches
  (`--no-version-check` or `CONTEXTO_NO_VERSION_CHECK=1` silences it). A CLI that changes its text
  without changing its version, or changes text only on later turns, is not caught.
- Session folders are never removed on their own. `contexto sessions prune` (D37) deletes whole
  sessions only, by full id with `--session`, skips any session in use or active in the last 10
  minutes, and cannot remove pages inside a live session. A launch that fails before the CLI starts
  removes the session it created (D52, D54, D57); a session whose CLI never sent a request is kept.
- Damaged files (D58, D59): a FIFO, directory or symlink in place of a session file is damage, read
  as such and never waited on. A damaged `meta.lock` or `live.lock` stops `contexto run` from opening
  the session until the file is deleted; with a damaged `meta.lock` recall, search and state are
  refused (pins and closes work), and with a damaged `live.lock` every tool is. A symlinked session
  directory is refused by every command.
- Windows is not supported: the store locks files with `fcntl`, the launcher forwards POSIX signals,
  and the store relies on POSIX file modes (0700 and 0600), so neither imports there. CI tests
  Python 3.12 to 3.15 on Linux and 3.12 and 3.15 on macOS; the live runs did not record their Python version.
- Cache efficiency depends on the upstream route: 38 of 38 in run #4 and 54 of 55 in the Opus run
  after D36. A proxy with several upstream credentials has to pin each session to one (D36).
  Subagents and side requests share the session's key, so parallel subagents load one credential's
  rate limit, and a 429 there moves the whole session to the next credential, with one cache miss
  and dropped thinking at each move. A fresh launch or `--fork-session` gets a new Claude Code
  session id and can miss the cache once.
- Organization-managed Claude Code settings outrank the launcher's `--settings`, and were not
  tested with the D44 allow rules. A user-supplied `--settings` is refused (exit 2) anywhere in the
  claude arguments, after `--` too. A prompt right after the user's own `--mcp-config <file>` is read
  as another config file (`--mcp-config=<file>` avoids it), and after any other list-taking flag
  such as `--allowedTools` it is read as one of its values.
- The proxy never reuses upstream connections, and retries once, on a new connection, only for
  connect, TLS, or send failures (rule 8). A failure after the body is sent and before the response
  head returns 502 and relies on the CLI's own retry; a failure after the response started ends the
  stream without its terminator (logged as `relay: <class>`).
- Compressed request bodies: gzip and deflate are decoded (zstd only on Python 3.14+); other
  encodings get 415 (rule 15). `count_tokens` errors from upstream pass through unchanged.
- On restart, sizing restores the measured token ratio at the first request on the recorded model,
  unless the ratio is outside 1.0 to 6.0 bytes per token (then the seed ratios apply). Mixed-model
  sessions still blend it.

## Releases

| Version | Commit | What changed |
|---|---|---|
| v0.1.0 | f990703 | first release, after M2 acceptance (D29) |
| v0.1.1 | 0769fdf | B22: in-view search skips the current turn's own question |
| v0.1.2 | def6e8c | A18: `CONTEXTO_HOME` resolved to one absolute path for the proxy and every MCP server (Codex's MCP server ignored it before) |
| v0.1.3 | 031ca59 | B23: search and index lines read Codex message parts, tool calls, and tool outputs (Codex search found nothing before; recall already worked, B20 in v0.1.0) |
| v0.1.4 | 314d575 | B24: standalone `contexto mcp` resolves `CONTEXTO_HOME` and `--home` to an expanded absolute path |
| v0.1.5 | 5ef9370 | A23: the proxy logs how each response ended (status, incomplete reason, stop reason), content-free |
| v0.1.6 | db472a9 | A26: the proxy's end-status log fields accept only documented values; anything else logs as `other` |
| v0.2.0 | 703d7c3 | Rule 16 (B31, A30): a restarted proxy resumes the main conversation's epoch instead of rotating again from epoch 0, so the prompt and its cache survive a restart |
| v0.2.1 | 798a7ce | A31: a resume requires the store to hold at least the recorded number of pages (`pages_missing` otherwise) |
| v0.2.2 | 2da3cb0 | Rule 17 (A34, #187): headroom capped at a quarter of the bound and halving at each turn start that measures growth, so one large read no longer leaves the session at the minimal window. Wheel and sdist attached by the reattach run after #189 |
| v0.2.3 | 2312caa | #192: Codex pages written by v0.1.0 to v0.1.2 get their index stub rebuilt from the page's messages when the page is read; nothing is rewritten |
| v0.2.4 | 0914850 | #193: a stream cut short upstream still writes the request's log entry, and its usage still reaches the engine; a malformed response head is logged as a 502 that is not retried; negative body lengths get 400. #194: only the main conversation publishes the window, and a restore waits for a request on the recorded model |
| v0.2.5 | 77961d1 | #195: a stream hold ended by an error event, end of stream, or an upstream failure publishes the room from the usage already read, before it releases the held tool calls; a `Content-Length` body cut short is detected; acceptance never counts a truncated 2xx as a turn end |
| v0.2.6 | 56e4551 | #197: one SSE grammar (CRLF, LF, CR) for the hold splitter and the usage reader, so usage is read on CR-terminated streams, and a final `message_delta` without its blank line is read before the hold publishes, so its output tokens reach the engine |
| v0.2.7 | 4525388 | #196: after a request whose usage never arrived, the next estimate covers the whole request, so a needed rotation is no longer skipped (`usage_missing`); usage for a request other than the one the engine last sent (after a transform error, or with two in flight) no longer feeds the estimate or the bytes-per-token ratio (`usage_unattributed`); a conversation is recorded as main before its turn start's rotation publishes the window, so a failed request cannot leave the old main recorded |
| v0.2.8 | 0a8e4c3 | #200: `contexto run codex` starts its MCP server as this Python by absolute path (`-P -m contexto`) instead of a bare `contexto` on `PATH`, so Codex no longer runs silently without the Contexto tools when `contexto` is not on `PATH`; both launchers pass `-P`, so a project's own `contexto` package cannot run in place of the server (the Claude path was exposed from v0.1.0) |
| v0.2.9 | 62b02a1 | #205: the proxy hashes each message once and drops the request copy and reference serialization, to cut per-request time on long histories (the fast hash falls back to normalise-then-hash when a message cannot be encoded). #199: the 30M scale harnesses and record (`spikes/m2/scale/`, `docs/research/M2-scale-30m.md`) |
| v0.3.0 | c023c87 | #202: the proxy warns on stderr and logs `mcp_tools_missing` when the main conversation runs without Contexto's MCP tools, and `mcp_tools_found` when they appear later |
| v0.4.0 | 4b87ec2 | #203 (D32, D33): the bounded fallback pages later turn starts' context blocks, and session-start is recognised by its `# Environment` first line, so carried content stops growing; `carried_reclassified` on resuming an older record. #204 (D34): search matches every word in any case, ranks by BM25 with a recency prior (paged context blocks last), and both processes cache page reads |
| v0.5.0 | cb1986f | #210 (D35): CLIProxyAPI as an upstream; SSE keep-alive comments pass the stream hold, the settings file follows `CLAUDE_CONFIG_DIR`. #212 (D36): a salted per-session `X-Session-Affinity` for non-vendor upstreams. #208 (D37): `contexto sessions prune` and session sizes in `sessions list`. #211 (D38): a warning for untested Claude Code or Codex versions. #215 (D39): the MCP server holds search text up to 64 MB by default. #209 (D40): each subagent gets its own recall room, and a tool call is charged against every live room. #213: the proxy starts without a reverse hostname lookup; quarantined pages are named from one clock reading |
| v0.5.1 | e864896 | #216 (D41): search counts a term once per text that holds it, at most 3 per page, and drops BM25's length term, so neither a long turn nor an old dump that repeats a key buries the turn that states its value |
| v0.5.2 | 42552d3 | The first release with `LICENSE` in the wheel and sdist (D43, #218). #222: `input_transformations` are merged across a response's events by count, capped at 32 with `input_transformations_truncated`, and upstream value summaries are depth-bounded. Docs and harness: #217, #219 (Codex at 150,000), #220 (O6 check), #221 (Opus acceptance), #223 (harness fixes for 2.1.302 and other models) |
| v0.5.3 | 7893ceb | #224 (D44): `contexto run claude` allows Contexto's five MCP tools by exact name in its `--settings`; a refused Contexto tool logs `mcp_tools_denied` and warns once |
| v0.5.4 | 0ff4ca5 | #226: the launcher passes `--settings=<file> --mcp-config=<file>`, so a prompt right after `claude` is no longer read as an MCP config file; the user `--settings` guard checks arguments after `--` too |
| v0.5.5 | 9a773ec | #225 (D45, rule 18): the JSON sources rule 18 lists are read through one 512-level nesting limit; a request past it goes upstream untransformed and logs `request_too_deep`; an unreadable page is a clean recall error |
| v0.5.6 | 302f7d2 | #228: hand-damaged page files are handled the same way everywhere (rule 18): search and the index skip them, recall returns an error; re-paging onto one is a `page_conflict`; a failed read is retried on the next call |
| v0.5.7 | 3a48fcc | #227 (D46): Claude Code 2.1.310 tested; the missing-tools check reads 4.x tool-search listings, a subagent environment message led by the listing is session start, and 5.x nested memory next to a tool result is carried up to 4,000 tokens; D44 refusals paired across the request |
| v0.5.8 | 52a96b5 | #230 (D47): a tool-result message with Claude Code's synthetic missing result carries nothing and is not read by the missing-tools check (it still goes to the model unchanged); nested memory's `@path` imports are carried within the cap; the cap counts each block as its page; a queued prompt in a role `system` text is not read by the missing-tools check; recall tells a failed read from a damaged file. Docs: #229 (STATUS, README, contract and decisions accurate to v0.5.7) |
| v0.5.9 | 0ed37b5 | #232 (D49): a head change is sized like any rotation; every forwarded request is checked against the bound before it is sent |
| v0.5.10 | 1d5f0d1 | #243 (D56): Claude Code 2.1.314 and 2.1.318 and codex-cli 0.166.0 tested; the new subagent stop reports are read; spikes use a temp `HOME` and `DISABLE_AUTOUPDATER=1` |
| v0.5.11 | 0339038 | #242 (D51): the model is told to pin standing rules when stated; Contexto's own search, recall and state calls are not searched |
| v0.5.12 | e2ae0c3 | #240 (D55): `contexto run claude` resumes through the session that served the conversation, survives signals with a watchdog, and prints its notices after Claude Code exits |
| v0.5.13 | 8557d6d | #245 (D55 follow-ups): the automatic session choice is serialized by `choose.lock`; Claude Code's short-option clusters are read; a fork keeps the original's session. Docs: #233 (README, SECURITY and CONTRIBUTING for first-time users; release checksums and provenance), so this is the first release with `SHA256SUMS` and an attestation |
| v0.5.14 | 844df06 | #237 (D52): settings files in Claude Code's order; project settings count for Codex only in a trusted folder |
| v0.5.15 | 26bacd3 | #238 (D50): main follows Claude Code's session id across `/clear`, `/resume` and `/compact` |
| v0.5.16 | 3950a21 | #239 (D53): Host and Origin check; upstream shut when the client leaves; an early 413 forwarded |
| v0.5.17 | 9865f5d | #241 (D57): `sessions show` lists pages, `contexto search`, readable `state`, id prefixes, no junk sessions, consistent exit codes |
| v0.5.18 | ded5b67 | #244 (D49 amended): the head is measured from the first usage with an input count, at that request's own ratio |
| v0.5.19 | c702657 | #235 (D58): lone surrogates, non-regular files and full disks no longer stop the store or the MCP server |
| v0.5.20 | 2388752 | #236 (D54): the Codex key helper through `/bin/sh -c`, no Anthropic default upstream for Codex, `--no-daemon`, launch checks first. Live acceptance run #4 ran on this release |
| v0.5.21 | cbc9683 | #234 (D48): images priced by their pixels; images and documents kept out of the ratio. Harness: #247 (replay turns `tool_reference` blocks for removed tools into text) |
| v0.5.22 | 33b54e6 | #246 (D59): the fix round's review follow-ups |
| v0.5.23 | 08ec571 | #248 (D60): a PDF is never estimated low: 4,568 tokens a page when counted for certain, else 600 pages |
| v0.5.24 | d38e87d | #250 (D61): Codex tool output at the 1.5 seed before usage, older epoch records re-measure their head, bounded JPEG walk and patch pricing, Codex's own overhead range in its start warning, `pdf_uncertain` only when the PDF is the cause |

From v0.5.0 on, each is a tag and a GitHub Release on this repository with the wheel and sdist
attached; there is no package registry release. The v0.1.0 to v0.4.0 releases and tags were deleted
on 2026-11-06 (D62); their rows stay as the record of what changed, and their commits are on `main`.

## Process notes

- 2026-10-16 18:15Z: hosted GitHub Actions stopped starting jobs. Until the move to
  the self-hosted runner (PR #188, main 2da3cb0), merges were checked by full local test runs
  from the author, the reviewer, and intake. 19:31Z to about 00:40Z the rig was down; the owner
  restored it, and several seats' git worktrees were recreated.
- 2026-10-16, about 10:25Z: builder-2 deleted `~/.contexto` on the owner's machine (8 session
  folders) without asking. 6 were matched to session ids its own probes recorded; 2 only by name
  pattern and time (builder-2's account, relayed verbatim by manager-1 at 10:29Z and noted on
  A18's queue row). Logs were not read and no other user of the folder was checked for. Agents now
  ask before deleting anything under the owner's home directory. The cause, `contexto run codex`
  ignoring `CONTEXTO_HOME` for its MCP server, was fixed in v0.1.2 (A18).
- 2026-10-17: the OpenRig rig was taken down (snapshot kept). Claude runs the project directly:
  builder subagents work in their own worktrees, and two independent reviewer agents post
  exact-head verdicts as PR comments before a merge. Before that, only intake ran in the main
  checkout and every other seat worked in its own worktree.
- Direct API requests identify honestly and carry no other product's client marker (D23).
- The project has one owner, and the builder and reviewer agents post from the owner's account,
  so GitHub cannot count their approvals; two exact-head reviewer verdicts per PR are a project
  rule. Ruleset `main: require pull request` (id 25202810) is active, and since 2026-10-24 it
  requires the `ci` check.
- 2026-10-24: CI and releases moved back to GitHub-hosted runners (PR #213), and the repository
  went public (D42).
- 2026-11-05: a 22-agent QA run on v0.5.8 (reports kept outside the repository), then a fix round
  with one builder per area in its own worktree, two reviewer agents per PR, and a re-check after
  each merge. During the QA run Claude Code's auto-updater installed 2.1.318 and deleted the 2.1.310
  binary, so 2.1.310 can no longer be run side by side; every spike and run now sets
  `DISABLE_AUTOUPDATER=1` (D56).
- 2026-11-06: the owner approved the branch cleanup and live acceptance run #4 ("just do it"),
  keeping the two-reviewer gate. Later that day the owner approved deleting the v0.1.0 to v0.4.0
  releases and tags (D62); a backup of their notes, assets and tags is kept outside the repository.
