- **D64. Let the user see Contexto working.** Claude, 2026-11-14, under D0 and the owner's brief for
  the item. An audit on 2026-11-12 found that a run printed one line at start and then nothing until
  the prompt neared the bound, that the numbers proving Contexto's value were in each session's
  `log.jsonl` and never shown, and that a full disk put the engine in degraded mode (requests grew
  from 67 KB to 354 KB) with nothing on stderr. Six slices, each built test first by a builder agent
  and reviewed by two reviewer agents at its exact head:
  - **Command polish (#257, MERGE_257).** `--json` on `state`, `search` and `recall`; `contexto
    version` and `-V`, which print `version_line()` like `--version` (#256); `contexto sessions`
    alone lists sessions; a first-run hint on an empty store; the vendor's install command in the
    "not found on PATH" errors and one `contexto:` prefix; the Codex "no key helper" error gives a
    whole working command (`--codex-upstream https://api.openai.com/v1 --codex-key-helper 'printenv
    OPENAI_API_KEY'`): without the upstream, the example as first written sent the OpenAI key to
    whatever host `ANTHROPIC_BASE_URL` named (review round 1). The MCP `serverInfo` reports the
    package version. Session ids: read-only commands take a unique prefix (D57); `run`, `proxy`,
    `mcp` and `sessions prune` keep the full id, because they create, serve or delete.
  - **`contexto stats` and the exit summary (#263, MERGE_263).** `contexto.stats.session_stats`
    reads only the log and the store. Decisions, each from a review finding:
    - The store is the truth for what is paged out. Summing `page_out.turns` counted a launch that
      could not resume its epoch again (69 shown for 26 pages); the proxy now records the store's
      page count on `proxy_start` and `proxy_stop`, and a run's gain is the difference.
    - The peak is the API's count, and the largest estimate among requests without usage is shown
      beside it when larger. The cache share covers only responses that reported a cache field.
    - "Claude Code did not compact" is said only when the log could have shown a compaction: every
      Messages request read by the engine, with Claude Code's session id, from a CLI version the
      summary's wording was measured on.
    - The summary covers the latest run; `contexto stats` the whole session, `--last-run` the run.
      `--quiet` or `CONTEXTO_QUIET=1` drops the summary and never a warning. Building it never
      changes the exit code, never waits on a lock, and Ctrl+C keeps the exit code.
    - Codex's mid-run notices are now held until Codex exits, as Claude Code's are (D55). This
      reverses D55's "Codex keeps printing mid-run".
    - Store faults (degraded mode, `store_error` with the errno's name, `page_conflict`) are shown
      once per cause at exit, in `contexto stats`, and in `contexto state` (text and `--json`
      `faults`), with what to do. A store still failing at exit is never called recovered.
    - The README demo (#259) is recorded with `CONTEXTO_QUIET=1`: the summary's run time differs at
      every recording, so the committed recording could not match a fresh one.
  - **`contexto doctor` (#258, MERGE_258).** Eleven checks, the provider variables included; no
    network request, no key helper run, no key printed. `contexto run claude` warns, and still
    launches, when one of six variables (`CLAUDE_CODE_USE_BEDROCK`, `_VERTEX`, `_FOUNDRY`, `_MANTLE`,
    `_ANTHROPIC_AWS`, `_ANTHROPIC_GOOGLE_CLOUD`) sends Claude Code's requests to another provider;
    measured on Claude Code 2.1.318 and 2.1.322 in `-p` mode at zero spend: 0 requests at the proxy
    with any of them on. Settings files are read as Claude Code 2.1.322 reads them, for every
    `contexto run`: a UTF-8 byte order mark is skipped, a named pipe is skipped, `NaN` and
    `Infinity` reject the file, an integer past 4,300 digits is read, and a file whose `env` is not
    an object is ignored whole. Doctor judges a `sessions` directory by asking the running Python
    the launcher's first question, because Python 3.14 changed `Path.exists()`.
  - **The status line (#276, MERGE_276).** `contexto statusline` prints one line such as
    `ctx 112k/150k · 41 paged · 3 recalls · 5 searches`, or `PAGING STOPPED`. Claude Code's
    documentation says the `--settings` file outranks the user's and `statusLine` is replaced, not
    merged, so `contexto run claude` adds one only when no settings file Claude Code reads has a
    `statusLine`, none sets `disableAllHooks`, and every file could be read; `--no-statusline` or
    `CONTEXTO_NO_STATUSLINE=1` turns it off. A prompt over the bound never reads as equal to it
    (`1.09M/1M`). A user's own script that calls it through `$CONTEXTO_SESSION` shows nothing
    until a conversation's first request, at start and after `/clear`, because only that request
    names the Claude Code session; the line the launcher installs passes `--session` and shows from
    the start.
  - **`docs/events.md` (#261, MERGE_261).** A reference for every event in `log.jsonl`, with a test
    that scans the source with `ast` and fails, with a message that says what to do, when the source
    can write an event or field the document does not list, or the document lists one the source
    cannot write. Six review rounds; the last two were held to a rule: a finding blocks only when
    code that exists or an ordinary next edit would hit it, or when the test would ask for a bullet
    for a field the log never carries.
  Not built: a status line for Codex, which has no such setting. Unconfirmed: interactive Claude
  Code for the provider variables and for the status line beyond the cases measured in a pty, any
  Claude Code version other than 2.1.318 and 2.1.322, and Linux except through CI.
