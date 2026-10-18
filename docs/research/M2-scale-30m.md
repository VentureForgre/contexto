# M2: Contexto at 30M tokens of history (zero spend)

Measured 2026-10-17 (local time), recorded 2026-10-18. `src/` is identical from 4525388, where the
harnesses ran, to main at 688b625 (v0.2.7 code). No model calls: every run used a local fake
upstream or a local store. Synthetic sessions, one machine (Apple M5 Pro, 18 cores, 48 GB),
1-minute load averages 2.6 to 10.2 (`load1` in the CSVs: store 2.6 to 10.2, CLI 3.0 to 9.6, proxy
3.1 to 7.4) because the three harnesses and other agents ran at the same time. Harnesses, commands,
and raw results: `spikes/m2/scale/cli/`, `spikes/m2/scale/proxy/`, `spikes/m2/scale/store/`, and
`spikes/m2/scale/reminders/` (where Claude Code 2.1.302 puts reminders over 48-turn sessions).

The question: can a Claude Code session grow to about 30M tokens of history under Contexto, where
the largest live run so far was about 494K (run #3)?

## Answer

It depends on the model id. The 30M run used the `first` profile: a context block on the first
message only and one session-start system message (`spikes/m2/scale/proxy/synth.py:176`). Claude
Code 2.1.302 places context blocks that way on the 5.x ids but not on the 4.x ids
(`spikes/m2/scale/reminders/`). The 5.x ids also send a role `system` message with every main
request (88 in 88 in a busy claude-opus-5-5 run), which the 30M run did not include. So the 30M
result covers where 5.x puts context blocks, not everything 5.x sends.

- **4.x ids (claude-sonnet-4-6; 12-turn probes on claude-opus-4-7, claude-sonnet-4-5,
  claude-haiku-4-5): the blocker is real.** The CLI puts `<system-reminder>` blocks in every later
  turn-start user message, and the proxy carries them with no cap: 45 to 425 tokens per turn,
  measured (Contexto's estimator). [Inferred, linear, a rough guide] `bound_too_small` after about
  860 turns at the bare-prompt floor, 180 in a busy session and 90 with a resume every turn (about
  23M, 4.8M and 2.4M of history), all short of 30M. Scaled by bytes per turn instead, the busy
  session fails near turn 77 (about 2.1M).
- **5.x ids (claude-opus-5-5, claude-haiku-5-5; 12-turn probes on claude-sonnet-5,
  claude-fable-5): per-turn growth is pageable.** The same reminders arrive as role `system`
  messages that the 8,000-token fallback can page. On claude-opus-5-5 and claude-haiku-5-5 one
  uncapped path is left: an effort change puts `output_config` on that turn's first system message,
  which makes it session-start class, carried forever (27 to 3,444 tokens per change, bytes /
  1.83). On the claude-sonnet-5 and claude-fable-5 probes, effort stayed in the top-level
  `output_config` and nothing unpageable grew; there the session's own environment message
  (`messages[1]`) has no `output_config`, classifies as carried text, and is first in line when the
  fallback pages.
- **[Inferred, not measured] The 5.x role `system` text probably still fits at 30M.** Carried
  content counts in the fixed head F, and the engine sizes the window as the bound minus headroom
  minus F (`rewrite.py:731`, `:735`), so extra F shrinks the window rather than pushing requests
  toward the bound. The limit is `bound_too_small`, which first fired at F = 100,810 in the `mixed`
  run; the `first` run's F at 30M was about 70,000, which leaves about 30,800 tokens of F headroom.
  In the claude-opus-5-5 resume run the pageable text held 410 to 2,143 tokens once the fallback
  engaged (turn 34) and about 3,500 just before it, a range kept small because effort content had
  used most of the shared fallback budget (with few effort changes it can approach 8,000); the fallback ran every turn there and runs only at
  rotations live, so more builds up between rotations. Effort-change content cannot be paged and
  adds to the same F with each change.
- **The owner's seats run Opus 5.5** (claude-opus-5-5), and acceptance run #3 ran on
  claude-haiku-5-5: the effort path was measured on both. [Measured] On claude-opus-5-5 nothing the
  fallback cannot page grew except at effort changes. With bare prompts and no effort change it
  stayed at 3,305 tokens over 48 turns; in the busy runs (plan mode, outside edits, MCP) its growth
  equalled the session-start growth from effort changes exactly (+6,502 tokens in one process,
  +8,867 with a resume every turn, +6,829 with task tools enabled; Contexto's estimator). The 4.x
  blocker applies only when a session runs on a 4.x id.
- **[Inferred] An Opus 5.5 session holds more pages.** The fallback writes each paged message as
  its own page (`rewrite.py:698`), so a 30M session on Opus 5.5 would hold about 3,000 pages or
  more, most of them single small messages. In the store spike's 3,000 to 6,000 rows (about 23K
  tokens per page) search reaches the planted page for 44 to 39 of 86 queries at 0.32 to 0.71 s;
  for this shape those rows are a rough guide (store section).

Search is fast enough, but its accuracy falls with page count.

| Layer | At 30M | What breaks first |
|---|---|---|
| Claude Code itself | resumes and resends 30M in a 52.6 MiB request, 0.7 s to first request, about 1 GB RSS, no compaction | "API Error: Out of memory" from 550M up (525M worked, 918 MiB request) |
| Proxy request path | every upstream request under the 150,000 bound (tokens counted as forwarded bytes / 1.83); adds 0.58 s per tool-loop request and 0.86 s per turn start | 4.x ids: later turn-start reminders are never paged, measured 45 to 425 tokens per turn of carried growth; at 215 per turn (the scale test's assumed profile), `bound_too_small` from 4.86M (turn 181) and requests over the bound from 8.20M. 5.x ids: on claude-opus-5-5 and claude-haiku-5-5 each effort change adds one never-paged system message; the per-request role `system` messages were not in the 30M run |
| Store and search | 0.71 s per search at 6,000 pages (30M is about 1,150 to 2,900 pages in the `first` profile and about 3,000 or more on 5.x ids, both inferred; see the store section) | accuracy: the planted page is in the 3 detailed results for 61 of 86 queries at 17 pages, 50 at 1,000, 44 at 3,000, 39 at 6,000 |

## Claude Code (spikes/m2/scale/cli)

Claude Code 2.1.302, `claude -p "continue" --resume <id>`, temp HOME and config dir, fake key; 3,141
`lsof` samples saw only 127.0.0.1. The fake upstream reported 100,000 input tokens per request, as
Contexto's bounded request would.

| History | Request MiB | Launch to first request (s) | CLI peak RSS MB | Compactions |
|---|---|---|---|---|
| 0.5M | 0.95 | 0.20 | 241 | 0 |
| 5M | 8.81 | 0.34 | 404 | 0 |
| 30M | 52.58 | 0.70 | 958 | 0 |
| 100M | 175.02 | 1.62 | 2,488 | 0 |
| 525M | 918.46 | 6.63 | 9,662 | 0 |
| 550M to 700M | no request sent | | 7,256 to 9,204 | 0, exit 1 "API Error: Out of memory" |

- With honest usage (reported tokens equal to the real size), `claude-opus-5-5` compacted from
  968,037 tokens and not at 950,387 (first pass), so its trigger lies between the two.
  `claude-haiku-5-5` compacted at about 1.01M and not at about 513K; nothing between was tested.
  `claude-haiku-4-5` compacted from 194,502 and not at 172,587. So the CLI decides from the reported
  usage, as M0 found.
- With Contexto-like usage, no compaction to 525M on `claude-opus-5-5` and to 30M on both Haiku
  models. Tool loops (3 Read round trips) ran on `claude-opus-5-5` at 30M and 100M and on
  `claude-haiku-4-5` at 30M, also with no compaction; `claude-haiku-5-5` had no tool-loop run.
  `count_tokens` was never called.
- Not explained: on resume at 30M and above the CLI appends about half the history back to the
  session file (27.6 MB at 30M; then 14.8 and 8.1 MB on two more resumes); at 10M and below about
  9 KB.
- Not tested: idle compaction (interactive only; read from the binary, it uses the last reported
  usage and is off unless a remote flag enables it), and one very large tool result after a small
  reported usage (D31's case).

## Proxy (spikes/m2/scale/proxy)

The real proxy class, session log, and rewrite engine wired as `launcher.start_proxy` wires them,
over HTTP, against a fake upstream that counts usage as forwarded bytes / 1.83. 0.5M ran every
request over HTTP; 2M to 30M were fast-forwarded by calling the engine at turn starts only, then
measured over HTTP in a fresh proxy. A 120-turn check (to 3.2M) against the all-HTTP path matched
every page and next-turn byte count in both profiles. In the `first` profile one rotation came a
turn later (71 tokens on a 113,000 threshold): 2 of 60 rotations differ (`rotations_equal: false`,
F or W up to 10,669 tokens apart), request sizes differ at turns 71 to 75, and the paths
re-converged after 5 turns (turn 76). The `mixed` profile matched in all 78 rotations.

Reminder on the first message only and one session-start system message (`synth.py:176`). That is
the context-block placement `spikes/m2/carried` measured and Claude Code 2.1.302 uses on 5.x model
ids, without the role `system` message those ids add to every request (next section); medians of 3
runs:

| History | Body MB | Turn start (s) | Tool loop (s) | Rotating turn start (s) | Max upstream tokens | Peak RSS MB | Pages |
|---|---|---|---|---|---|---|---|
| 0.5M | 0.9 | 0.056 | 0.014 | 0.110 | 134,587 | 44 | 22 |
| 2M | 3.7 | 0.099 | 0.044 | 0.156 | 134,587 | 180 | 80 |
| 5M | 9.2 | 0.103 | 0.101 | 0.230 | 135,954 | 522 | 192 |
| 10M | 18.3 | 0.278 | 0.194 | 0.358 | 135,954 | 1,337 | 382 |
| 30M | 55.0 | 0.861 | 0.580 | 0.867 | 135,954 | 1,707 | 1,145 |

No `over_bound`, `bound_too_small`, or `transform_error` at any checkpoint; `window_minimal` 1, 2,
and 5 times by 5M, 10M, and 30M (synthetic turns larger than the window). Over 79 requests in one
proxy at 30M, RSS settled at 2.1 to 3.1 GB (peak 3.45 GB) with live Python objects flat at about
23,000, so the proxy keeps no requests. [Inferred] The RSS growth is allocator retention, not a
leak.

Where the time goes (cProfile at 10M): hashing every message twice per request 47 percent
(`rewrite.py:709-715` and `:973`; sorted-key JSON encoding alone 34), serializing the request twice
16 (`proxy.py:820`, `:849`), parsing the JSON twice 14 (`proxy.py:809`, `:821`), scanning history for
turn starts 11, and `index_text` on rotation 8. All of it is linear in history, about 0.019 s per
million tokens per tool-loop request.

### Carried context blocks (measured, spikes/m2/scale/reminders)

Every `<system-reminder>` context block on a paged turn start is carried forward with no cap
(`rewrite.py:493`), and the 8,000-token fallback pages carried system messages, never context
blocks (`rewrite.py:671-701`). So the limit depends on where Claude Code puts reminders after the
first message. `spikes/m2/scale/reminders/README.md` measured that on Claude Code 2.1.302: 48-turn
sessions against a scripted loopback model (no model spend; isolation rests on a loopback base URL,
a proxy that refuses CONNECT and a fake key, and every `lsof` sample saw only 127.0.0.1; per-run
sample counts in the README), with plan-mode toggles, effort changes, file edits outside the CLI, task
tools, a nested CLAUDE.md and an MCP tool, run once in one process and once with a new `--resume`
process every turn. Each turn's last request went through Contexto's own `carried_content`,
`classify_system` and `page_carried_text`. Tokens are Contexto's estimator (3.5 bytes per token
before any usage is observed) unless marked bytes / 1.83. From `results/table.md`:

| Run | Model id | Turns | Later turn-start reminders, n / tokens (bytes / 1.83) | Carried at k after fallback, turn 2 to last | Not pageable, turn 2 to last | Not-pageable growth per turn |
|---|---|---|---|---|---|---|
| bare prompts, one process | claude-sonnet-4-6 | 48 | 51 / 3,210 | 3,386 to 5,452 | 3,386 to 5,452 | 45 |
| busy, one process | claude-sonnet-4-6 | 48 | 69 / 17,667 | 3,382 to 13,256 | 3,382 to 13,256 | 215 |
| busy, resume every turn | claude-sonnet-4-6 | 48 | 141 / 36,037 | 3,382 to 22,953 | 3,382 to 22,953 | 425 |
| busy, probe | claude-opus-4-7, claude-sonnet-4-5, claude-haiku-4-5 | 12 | 15 to 16 / 3,913 to 4,169 | about 3,385 at turn 2; 5,545 to 5,686 at turn 12 | same | 216 to 230 |
| bare prompts, one process | claude-opus-5-5 | 48 | 0 / 0 | 3,304 to 3,948 | 3,305 to 3,305 | 0 |
| busy, one process | claude-opus-5-5 | 48 | 0 / 0 | 3,329 to 11,254 | 3,302 to 9,804 | 141 |
| busy, resume every turn | claude-opus-5-5 | 48 | 0 / 0 | 3,329 to 12,704 | 3,302 to 12,169 | 193 |
| busy, resume every turn | claude-haiku-5-5 | 48 | 0 / 0 | 3,189 to 12,570 | 3,161 to 12,035 | 193 |
| busy, probe | claude-sonnet-5, claude-fable-5 | 12 | 0 / 0 | 3,347 to 5,564; 3,192 to 5,176 | 534 to 534 | 0 |

"At k" is Contexto's view with every earlier turn paged, the upper bound on what it carries. "Not
pageable" is context blocks plus session-start-class, settings and unclassified system messages.
The fake upstream accepts any model id, so the claude-sonnet-5 and claude-fable-5 rows show only
how the CLI places reminders for those ids.

- [Measured] **4.x ids put reminders in later turn starts.** Each is a separate text block
  starting `<system-reminder>` in the same user message as the prompt, so `carried_content` carries
  it. Sources (`results/features.md`, bytes / 1.83): the token countdown, 47 tokens on every turn
  start (on by default in 2.1.302, `CLAUDE_CODE_TOTAL_TOKENS_REMINDER`); the plan-mode entry, about
  2,720 tokens each, 62 percent of the busy run's reminder tokens (10,883 of 17,667); a
  file-changed note of 258 to 358 tokens per edit outside the CLI, sent again on every resume until
  the file is read again (80 notes with a resume every turn, 8 in one process); and a task-tools
  reminder of 250 to 330 tokens about every 10 turns. The fallback never pages any of them.
- [Measured] **5.x ids keep reminders out of turn starts.** `messages[0]` holds 2 blocks (CLAUDE.md,
  656 tokens; the attribution note, 318; bytes / 1.83) and no later user message holds a reminder.
  The same content arrives as role `system` messages after the prompt or a tool result, one per
  main request (88 in 88 requests in the busy one-process claude-opus-5-5 run, 48 in 48 with bare
  prompts), untagged on claude-opus-5-5, claude-haiku-5-5 and claude-fable-5 and wrapped in
  `<system-reminder>` on claude-sonnet-5. `classify_system` marks them carried text, which the
  fallback pages, each message as its own page (`rewrite.py:698`). In the claude-opus-5-5 resume
  run the fallback (applied every turn in the spike) first paged at turn 34, had paged 40 messages
  by turn 48, and from turn 34 on kept carried content at k between 9,618 and 12,704. Most of that
  was content the fallback cannot page (9,204 to 12,169: the 3,302 tokens at turn 2 plus 5,902 to
  8,867 from effort changes); the pageable part held 410 to 2,143 (small because effort content had already used most of the shared 8,000-token budget; with few effort changes it can approach 8,000, still inside the F headroom). The
  8,000-token budget covers growth of all carried content, unpageable included
  (`rewrite.py:677-678`). [Inferred] The live engine runs the fallback only at rotations, so between
  rotations carried content can be higher.
- [Measured on claude-opus-5-5 and claude-haiku-5-5] **Effort changes are the uncapped path.** On
  2.1.301 an effort change added a settings-only message (`spikes/m2/carried` finding 5). On
  2.1.302 `output_config` lands on that turn's first system message instead (the countdown, a file
  note, or the plan-mode text), so `classify_system` (`rewrite.py:461-462`) returns session-start
  and the message is never paged. Each change adds the size of whatever that message happens to
  be: over the 16 changes in each 48-turn busy run, 27 to 2,732 tokens in one process and 27 to
  3,444 with a resume every turn (bytes / 1.83; 14 to 1,801 in Contexto's estimator). In this
  schedule 4 of the 16 changes landed on a plan-mode entry (1,428 to 1,801 estimator tokens each);
  the other 12 added 14 to 388. The session-start class grew from 2,770 to 9,272 estimator tokens
  in one process and to 11,637 with a resume every turn, and that growth was all of the not-pageable
  growth (+6,502 and +8,867). The schedule changes effort 4 times every 12 turns, far more often
  than typical use. The classifier was written against 2.1.301
  (`HOOK_SIGNATURES_CLAUDE_CODE_VERSION`, `rewrite.py:415`).
- [Measured, 12-turn probes] **claude-sonnet-5 and claude-fable-5 keep effort out of the
  messages.** All 4 `apply_flag_settings {effortLevel}` changes succeeded, but `output_config`
  changed only at the top level, and no message carried it (`effort_check.py`). The session-start
  class stayed at 0 and the not-pageable part at 534 tokens. Their environment message
  (`messages[1]`, 9,674 and 9,205 bytes of text) has no `output_config`, so `classify_system`
  calls it carried text with no recognisable kind, and `page_carried_text` pages unrecognised
  carried text oldest first: it is the first message the fallback would page. The contract foresaw this case
  (`M2-contexto-proxy.md:89-90`, "a session start with no effort setting ... falls here").
- **Earlier evidence, now explained by the model id.** `spikes/m2/carried` (2.1.301,
  claude-haiku-5-5) found context blocks only at `messages[0]` with no hooks, and hook context as
  separate role `system` messages after the prompt or the `tool_result` (findings 5, 7, 9). That is
  the 5.x shape, which 2.1.302 keeps on 5.x ids. Live run #3 (claude-haiku-5-5, 24 turns, about
  494K) logged no `bound_too_small` (`docs/research/M2-acceptance-run3.md:39`). On 2.1.301,
  resuming a hand-written session file that lacks the CLI's own records put the context block in
  the new turn's user message (finding 6); 2.1.302 was not tested for that.
- **When it fails.** Measured in the scale test's assumed profile: the fixed head F grew about 215
  tokens per turn (62,392 at turn 3 to 100,810 at turn 181), `bound_too_small` first fired at turn
  181 (4.86M), and the first request over the bound came at turn 308 (8.20M, 150,046 tokens); at
  10M, 7 of the 22 requests measured at that checkpoint were over, all tool loops (151,779 to
  159,255). The failure turn depends on carried growth per turn, not history size. [Inferred,
  linear] Scaled to the measured 4.x rates: about turn 860 at 45 per turn, 180 at 215, and 90 at 425
  (about 23M, 4.8M and 2.4M at the scale test's 26,900 tokens per turn). This maps the spike's
  estimator tokens onto the scale test's engine, which had usage feedback, so the turn numbers are a
  rough guide: by bytes per turn the busy one-process rate (701) is 2.4 times the assumed profile
  (297), which would put that session's failure near turn 77 (about 2.1M) instead of 180.
- **When it fails on claude-opus-5-5 and claude-haiku-5-5.** [Measured] Only effort changes added
  content the fallback cannot page: each adds the size of that turn's first system message, 27 to
  3,444 tokens (bytes / 1.83), and in the busy runs that was all of the unpageable growth (+6,502,
  +8,867, +6,829). [Inferred] So the bound breaks only if effort changes keep coming. Their content
  raises F, and `bound_too_small` fires once F leaves no window for the new turn
  (`rewrite.py:774-775`): at F = 100,810 in `mixed`, against about 70,000 in `first` at 30M, so
  about 30,800 tokens of F headroom, shared with the pageable residual. How many changes fit depends
  on what each lands on (4 of the 16 here landed on a plan-mode entry) and on how often users change
  effort, enter plan mode, edit files outside the CLI and resume; none was measured on real usage.

## Store and search (spikes/m2/scale/store)

Pages written through `SessionStore.page_out` (about 23K tokens per synthetic turn). Search and
recall timed through a real `contexto mcp` child over stdio, medians of 3 passes of 5 calls;
quality scored in process through the same server code (`mcp.Server.handle_line`).

| Pages | Search (s) | Recall (s) | `index_text` (s) | MCP peak RSS MB | Store MB |
|---|---|---|---|---|---|
| 17 | 0.002 to 0.004 | 0.0005 | 0.035 | 34 | 0.8 |
| 1,000 | 0.105 to 0.110 | 0.0005 | 0.120 | 103 | 42 |
| 3,000 | 0.318 to 0.340 | 0.0006 | 0.309 | 245 | 127 |
| 6,000 | 0.677 to 0.711 | 0.0006 | 0.626 | 456 | 255 |

Search reads and parses every page on every call (about 55 percent of its time) and walks every
message block (33 percent), about 0.115 ms per page per call.

How many pages is 30M? [Inferred] About 1,150 to 2,900: the proxy run held 1,145 pages at 30M at
about 26K tokens per page, the store spike's 23K tokens per page gives about 1,300, and 30M divided
by the calibration corpus's mean turn of 10,431 tokens gives about 2,900. The 3,000-page store sits
just above that range; 6,000 pages is twice it. That range is for the `first` profile, which has no
per-request role `system` messages. [Inferred] On 5.x ids the fallback adds pages: it writes each
paged message as its own page (`rewrite.py:698`), it paged 40 messages in turns 34 to 48 of the
claude-opus-5-5 resume run (`fallback_paged_total` in `reminders/results/turns.csv`), and once it
engages it eventually pages nearly every carried role `system` message, about 1.8 per turn in that
schedule (88 in 48 turns) and more with a real model that makes more tool calls. A 30M session on
Opus 5.5 would then hold about 2 to 3 times the 1,145 turn pages, about 3,000 pages or more, most of
them single small messages, so the 3,000 to 6,000 rows below (44 to 39 of 86 queries in the
detailed results, 0.32 to 0.71 s per search) are a rough guide to this shape, not a measurement of
it.

Quality: 30 planted facts, 86 queries written before any result was seen and fixed across sizes.
Distractor rates came from word frequencies in the owner's local Claude Code transcripts (counts
only; `calibration.json` holds the probe phrases and their rates, no transcript text).

| Pages | Planted page in the 3 detailed results (of 86) | Latest value first (of 24) | Facts in the detailed results for their best query (of 30) |
|---|---|---|---|
| 17 | 61 | 19 | 30 |
| 1,000 | 50 | 17 | 28 |
| 3,000 | 44 | 14 | 27 |
| 6,000 | 39 | 12 | 26 |

About 25 queries miss at every size because matching is exact and case-sensitive; the rest of the
loss grows with page count because results are newest first with no ranking, so common words
(`timeout`, `region`) fall out of the reply by 1,000 to 3,000 pages while rare identifiers still
come first at 6,000. If a session keeps returning to one topic (modelled as every query on at
least 1 page in 500, an assumption), 6,000 pages leave 15 of 86 queries in the detailed results.

## What to fix, in order

1. Carried growth (a blocker on 4.x model ids; measured in `spikes/m2/scale/reminders/`). Two
   changes, each a contract rule change that needs a new decision. This PR changes neither. For the
   owner's seats (claude-opus-5-5) the 5.x part is the one that applies.
   - 4.x ids: page carried context blocks under the bounded fallback. Keep the first message's
     blocks (the CLAUDE.md and session context on `messages[0]`) and the latest of each
     recognisable kind (for example the token countdown, `<total_tokens>`), and page the rest once
     growth passes the 8,000-token budget. `docs/milestones/M2-contexto-proxy.md:71-72` makes the
     injected context blocks of any turn-start message carried and never paged except under the
     fallback, and :96-98 lets the fallback page only step 4 system messages; both would change.
   - 5.x ids: stop `classify_system` from classifying a system message as session-start just
     because an effort change attached `output_config` to it. Contract step 1
     (`M2-contexto-proxy.md:76-78`, content plus settings fields means session-start) and its rule
     in `_anthropic_classify_system` (`rewrite.py:461-462`) would change. The contract rules out
     position so that hand-written resumes classify correctly; how to tell the real session-start
     message apart without it is part of the decision. The effort path was measured on
     claude-opus-5-5 and claude-haiku-5-5 only. On the claude-sonnet-5 and claude-fable-5 ids the
     session-start message has no `output_config` at all, classifies as carried text, and is the
     first message the fallback would page (the case `M2-contexto-proxy.md:89-90` foresaw), so the
     decision has to identify it there too.
2. Search ranking. BM25 over case-insensitive words (prototyped: 57 of 86 queries and 29 of 30 facts
   in its own top 3 at 6,000 pages, against 39 and 26 today; the prototype ranks outside
   `_tool_search`, so the per-call reply limit is not applied, and each plant repeats its key words
   in 2 or 3 places, which may flatter BM25) breaks "latest value first" (7 of 24 against 12), so it
   needs a recency term; that combination is not measured.
3. Search and rotation speed. Cache each page's searchable text in the MCP process (pages are
   immutable; prototyped in process: 0.007 to 0.065 s per search at 6,000 pages against 0.65 to
   0.72 s in process today, identical results, after a 1.06 s first load and 196 MB held), and give
   `index_text` a stub manifest instead of re-reading every page twice per rotation (estimated, not
   measured). An SQLite FTS5 trigram index returned the same result sets as today for the 9 latency
   queries at 1,000 and 6,000 pages, with rare queries under 1 ms, for 1.74 ms per page write and
   an index 2.5 times the store.
4. Proxy CPU. Hash each message once and reuse it (expected to save about 23 percent of transform
   time, the profile share of the second hashing pass; not measured), and parse the body once. Not
   needed for 30M (0.58 s per tool-loop request), but it is the next ceiling.

Limits: synthetic sessions; one run per size for the CLI; proxy checkpoints above 0.5M
fast-forwarded. "Under the bound" is in the fake upstream's unit, forwarded bytes / 1.83 on
synthetic text, so the engine's usage feedback is self-consistent by construction and the margin
(largest measured request 135,954; largest fast-forwarded last request 144,176, 5,824 under the
bound) was not tested against real estimate error. The fast-forward check
against the all-HTTP path reaches turn 120 (3.2M), not 30M. Fast-forward skips the tool-loop
requests of fast-forwarded turns (2.2 per turn), so their room and retained-snapshot store writes
and the stream hold run only in the measured HTTP rounds. Recall accuracy is measured only for the
search tool, not for a model using it. The 30M proxy run had one session-start system message per
session (`synth.py:176`); on 5.x ids Claude Code sends a role `system` message with every main
request, so the fallback's paging at scale, its effect on F, and the resulting page count are
inferred, not measured. Reminder placement is the CLI's own behaviour against a
scripted model on one Claude Code version (2.1.302), with no hooks, output-style changes or
compaction, and with effort changes and outside edits far more frequent than typical use.
