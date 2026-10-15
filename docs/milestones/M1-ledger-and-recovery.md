# Milestone 1: Durable ledger, checkpoints, and recovery

Status: contract, not implemented. Owner: intake.lead. Date: 2026-10-15.

## Outcome

A long-running session can record what happened, what was decided, and what is still owed, with
provenance. A successor can rebuild a bounded handoff from that record, see which items are
verified, reported, summary, hypothesis, or stale, and verify the handoff against its sources. Every
claim in this list must be backed by a test that would fail if the behavior broke.

## Stack decisions

- Python 3.12 or newer, `src/` layout, package `contexto`, console script `contexto`.
- Runtime uses the standard library only. Tests use `pytest`, run with `uv run --with pytest pytest`.
- Local data lives under `<root>/.contexto/` (already gitignored). Nothing in that directory is committed.
- No network calls, no model calls, no transcript ingestion in M1.

## Data model (normative)

A **record** is an immutable JSON object. Records are never edited; change is expressed by appending
new records.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | string | yes | `rec_` + first 24 hex chars of SHA-256 over the canonical JSON of every field except `id` and `prev` |
| `kind` | enum | yes | `event`, `decision`, `obligation`, `artifact`, `reference`, `closure` |
| `epistemic` | enum | yes | `verified`, `reported`, `summary`, `hypothesis` |
| `created_at` | string | yes | ISO 8601 UTC with `Z` suffix |
| `author` | string | yes | seat or person that wrote the record |
| `session` | string | no | session identifier the record came from |
| `topic` | string | no | stable key; two active `decision` records with the same topic are a conflict |
| `body` | string | yes | the claim itself, plain text |
| `provenance` | list | yes, may be empty | list of source objects, see below |
| `supersedes` | list of ids | no | records this one replaces; they become inactive |
| `target` | id | only for `closure` | the obligation this closes |
| `owner` | string | only for `obligation` | who owes the work |
| `prev` | string | yes | SHA-256 hex of the previous ledger line, or 64 zeros for the first record |

Source object: `{"type": "file" | "url" | "record" | "command" | "transcript", "locator": str,
"sha256": str | null, "span": str | null}`. For `file` sources the writer records the file's
SHA-256 at capture time so staleness can be detected later.

Canonical JSON: `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.

Serialization rules (pinned 2026-10-15 from lane A, so ids are reproducible across implementations):

- `Record.to_dict()` omits optional fields whose value is `None` (`session`, `topic`, `target`,
  `owner`) and omits `supersedes` when it is empty. `provenance` is always present, even when empty.
- In Python, `Record.provenance` and `Record.supersedes` are tuples so the frozen dataclass stays
  hashable; they serialize as JSON lists.
- Every source object is serialized with all four keys; absent `sha256` or `span` is `null`.
- `id` is computed over the UTF-8 bytes of the canonical JSON of that `to_dict()` output minus
  `id` and `prev`. The ledger line is the
  canonical JSON of the full `to_dict()` output, including `id` and `prev`.
- `prev` is the SHA-256 hex of the previous ledger line's UTF-8 bytes, excluding its trailing newline.
- The ledger **head** is the SHA-256 hex of the last intact line, computed as for `prev`, or 64
  zeros for an empty ledger. A truncated final line is not part of the head.
- `created_at` is written as `YYYY-MM-DDTHH:MM:SS.ffffffZ`. Readers accept any ISO 8601 UTC
  timestamp with a `Z` suffix, and the stored string is hashed as written. `append` fills
  `created_at` with the current UTC time when the draft omits it; a draft may supply its own.

Derived state rules:

- A record is **inactive** if any later record lists it in `supersedes`.
- An `obligation` is **open** when it is active and no active `closure` targets it. A superseded
  obligation is not open. Superseding a closure reopens its obligation if the obligation is still
  active.
- A `file` source's `locator` is a POSIX path relative to `<root>` (the directory that contains
  `.contexto/`), and its `sha256` is required. A record is **stale** if any `file` source's current
  SHA-256 differs from the recorded one, or the file is missing. Staleness is computed at read time, never written back.
- `epistemic` is never upgraded implicitly. Only a new record can claim `verified`, and a
  `verified` record must have at least one provenance source.

## Ledger (lane A, manager-1)

Append-only JSONL at `<root>/.contexto/ledger.jsonl`, one canonical record per line, hash-chained by
`prev`. Required behavior:

- `append` validates the record, computes `id` and `prev`, writes one line with `fsync`, and rejects
  invalid kinds, missing required fields, a record whose `id` already exists, closures whose
  target is not an existing active obligation, `supersedes` ids that do not exist, `file` sources
  without `sha256` or with an absolute locator, and `verified` records with empty provenance.
- `records()` returns the intact records in order and stops before a truncated final line.
  `verify` reports the first broken link, bad id, or truncated last line. A truncated final line
  (crash mid-write) is recoverable by `repair`, which moves the bad tail to `.contexto/quarantine/`
  and never deletes data. `repair` handles only a truncated tail; corruption mid-chain is reported
  by `verify` and left for a human.
- Concurrent appends from two processes do not interleave or break the chain (`fcntl` file lock).
  M1 supports macOS and Linux only.
- CLI: `contexto init`, `contexto add <kind> ...`, `contexto list [--kind] [--open] [--active]`,
  `contexto show <id>`, `contexto verify`, `contexto repair`. `add` with `--file PATH` captures
  that file's SHA-256 into provenance.
- CLI layout: `contexto.cli` builds the parser and discovers subcommand modules in the
  `contexto.commands` package via `pkgutil`; each module exposes `register(subparsers)`. Lane B adds
  its own modules there and does not edit `cli.py`.

Public Python API that lane B depends on, in `contexto.model` and `contexto.ledger`:

```python
@dataclass(frozen=True)
class Source: type: str; locator: str; sha256: str | None = None; span: str | None = None

@dataclass(frozen=True)
class Record:  # fields exactly as in the table above; list fields are tuples
    ...
    @classmethod
    def from_dict(cls, d: dict) -> "Record": ...
    def to_dict(self) -> dict: ...

class Ledger:
    def __init__(self, root: Path): ...
    def append(self, draft: dict) -> Record: ...
    def records(self) -> list[Record]: ...  # intact prefix only
    def head(self) -> str: ...  # line hash of last intact line, 64 zeros when empty
    def verify(self) -> list[str]: ...  # empty list means intact
```

Lane A lands `contexto.model` plus project scaffolding (`pyproject.toml`, test config) as its first,
small PR so lane B can build on it.

## Checkpoint and recovery (lane B, manager-2)

Lane B consumes `list[Record]` and `Ledger.head()` and never writes the ledger file directly in
product code. The recovery-harness fixture is exempt: it may truncate or corrupt the file to
simulate a crash.

- `contexto.state`: derive active records, open obligations, conflicts (same `topic`, both active
  decisions), and staleness against the working tree.
- `contexto.checkpoint`: build a bounded checkpoint under a character budget. The budget bounds
  the full rendered handoff markdown, including headings and omission lines. Priority order: open
  obligations, conflicts, active verified decisions, other active decisions, recent verified
  events, everything else. If the budget forces omission, the checkpoint states how many records
  of each kind were omitted. It never silently drops an open obligation; if obligations alone exceed
  the budget, it fails with a clear error.
- Checkpoint files: `.contexto/checkpoints/<ckpt id>.json` plus the rendered
  `.contexto/checkpoints/<ckpt id>.handoff.md`. The checkpoint id is `ckpt_` + the first 24 hex chars of
  SHA-256 over the canonical JSON of the checkpoint object minus its `id`. The JSON holds its
  `created_at`, the ledger head hash, the included record ids, and the omission counts. The markdown groups
  content under separate headings for verified, reported, summary, hypothesis, and stale items, and
  lists every record id it cites. A stale record appears only under the stale heading, with its
  epistemic label shown.
- `contexto checkpoint [--budget N]` writes one. `contexto resume [<ckpt id>]` (default: the checkpoint with the latest
  `created_at`, ties broken by id) prints the handoff and
  checks it: ledger chain intact, checkpoint head is an ancestor of the current head, records added
  since the checkpoint are listed, and any cited record now stale or superseded is flagged.
- Recovery test harness: a scripted fixture that simulates a long session (at least 500 records,
  supersessions, closures, a modified source file, a crash-truncated final line) and asserts the
  successor recovers every open obligation and every active decision, flags the stale item, and
  reports the truncated line.

## Done means

- Both lanes merged to `main` through reviewed PRs, each reviewed at an exact head SHA.
- `uv run --with pytest pytest` passes on `main` after integration.
- An end-to-end CLI scenario (init, add, checkpoint, modify a file, resume) is run by intake on
  integrated `main` and its output is recorded in `STATUS.md`.

## Out of scope for M1

Transcript ingestion, retrieval ranking for the next turn, embeddings, model-written summaries,
multi-host sync, and any UI.
