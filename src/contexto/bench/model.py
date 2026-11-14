"""The data a benchmark script is made of: ``Probe``, ``Decision`` and ``Script``, and their JSON form.

A script is written to ``<out>/scripts/seed-<n>.json`` so that a reader can audit what was asked and
what the right answers are without running the generator. The JSON form is versioned (``format``)
and round-trips: ``Script.from_json(s.to_json()) == s``.

Turn indexes are 0-based positions in ``Script.turns`` everywhere in this package.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

FORMAT = 1
DEFAULT_SEED = 20261016  # the seed of live runs #3 and #4
PROTOCOLS = ("d27", "v2")
D27_CLASSES = ("paged_only", "multihop", "chain", "obligations", "distractor")
NEW_CLASSES = ("paraphrase", "concluded", "toolloop")
# What a runner does with each turn: "log" pastes log text and expects the reply OK, "decision"
# expects one DECISION line, "toolloop" needs the script's files on disk and expects OK after the
# reads, "probe" asks questions.
KINDS = ("log", "decision", "toolloop", "probe")
PROBE_KINDS = ("value", "set")
FILE_DIR = "bench-files"  # every file a script carries is under this directory, relative to the session's

_WORD = re.compile(r"[a-z0-9]+")
MIN_WORD = 3
# The words of the generator's filler lines and of its fixed turn texts (the log head and tail, the
# opening instructions, the probe-turn header, the tool-loop turn's head and the decision turn's
# fixed lines, with its label filled in), 3 or more letters long. They are on every page or every
# turn of a kind, so a word search learns nothing from them. Words that are also part of a service
# name are left out of this set ("search" is a service root and a word of the tool-loop head); other
# subject words stay ("shard", "log" and "name" are also words of attribute keys).
# tests/test_bench_script.py derives this set from the generator's texts and checks it is equal.
GENERATOR_WORDS = frozenset((
    "about", "all", "and", "answer", "any", "are", "below", "commas", "contain", "conversation", "current",
    "debug", "decision", "directory", "does", "each", "else", "end", "exactly", "file", "files", "for", "form",
    "give", "has", "have", "highest", "ids", "info", "instead", "its", "keep", "latency", "level",
    "line", "list", "listed", "log", "long", "lowest", "matters", "message", "name", "next", "not", "nothing",
    "number", "one", "only", "order", "part", "pasted", "pastes", "paths", "per", "point", "question",
    "questions", "read", "relative", "reply", "rule", "say", "separated", "session", "shard", "skip", "take",
    "the", "them", "this", "time", "tool", "unknown", "warn", "what", "when", "with", "working", "you", "your",
))
# Function words that say nothing about a subject. Most are in GENERATOR_WORDS too.
STOP_WORDS = frozenset((
    "about", "been", "does", "each", "from", "have", "into", "over", "that", "their", "them", "then",
    "there", "these", "they", "this", "those", "were", "what", "when", "where", "which", "will", "with",
    "your",
))


def words(text: str) -> frozenset[str]:
    """The word tokens of ``text`` that the lexical-overlap figure counts.

    The rule: a token is a maximal run of ASCII letters and digits, after case folding, so every
    other character separates tokens (``billing-core.retention_days=amber-4417`` gives ``billing``,
    ``core``, ``retention``, ``days``, ``amber`` and ``4417``). Tokens of 3 or more characters count,
    because Contexto's search matches a query word of any length as a substring, and the service
    suffixes ``hub``, ``api``, ``ops``, ``ctl``, ``svc`` and ``lab`` are 3 long. Tokens in
    ``GENERATOR_WORDS`` (the generator's filler vocabulary and fixed turn texts, which are on every
    page) and the function words in ``STOP_WORDS`` do not count.
    """
    return frozenset(w for w in _WORD.findall(text.casefold())
                     if len(w) >= MIN_WORD and w not in GENERATOR_WORDS and w not in STOP_WORDS)


def lexical_overlap(question: str, statements: Iterable[str]) -> int:
    """How many distinct word tokens (``words``, which states the rule) a question shares with its
    plant statements.

    It measures how much a word search is helped: Contexto's search matches a page that holds every
    word of the query, so a question that shares no word with its plant gives the model nothing to
    search for except its own guess at the plant's wording. It counts words, not how many lines a
    word finds: a ``paraphrase`` question shares its service's words with the plant, and
    ``script.service_alone`` checks that those words find the service's decoy lines as well.
    """
    planted: set[str] = set()
    for statement in statements:
        planted |= words(statement)
    return len(words(question) & planted)


@dataclass(frozen=True)
class Probe:
    """One question with its ground truth.

    ``statements`` are the plant: the exact texts that state the fact, in order. ``subjects`` are
    strings that identify the plant and so must not be restated anywhere else (``script.restatements``).
    ``cls`` is the probe class: one of ``D27_CLASSES`` (equal to ``type``) or ``NEW_CLASSES``.
    ``overlap`` is ``lexical_overlap(question, statements)``.
    """

    id: str
    group: str  # "a" for the D27 probes, "b" for the classes protocol v2 adds
    type: str
    tier: str
    question: str
    answer: str
    kind: str = "value"  # "value" or "set"
    older: tuple[str, ...] = ()  # superseded or look-alike values: a wrong answer equal to one is stale
    subjects: tuple[str, ...] = ()
    statements: tuple[str, ...] = ()
    cls: str = ""
    overlap: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {k: list(v) if isinstance(v, tuple) else v for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Probe:
        try:
            return cls(id=str(data["id"]), group=str(data["group"]), type=str(data["type"]), tier=str(data["tier"]),
                       question=str(data["question"]), answer=str(data["answer"]), kind=str(data["kind"]),
                       older=_strings(data["older"]), subjects=_strings(data["subjects"]),
                       statements=_strings(data["statements"]), cls=str(data["cls"]),
                       overlap=_whole(data["overlap"], "overlap", 0, "probe"))
        except ValueError:
            raise
        except (KeyError, TypeError) as exc:
            raise ValueError(f"not a benchmark probe: {exc!r}") from exc

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, text: str) -> Probe:
        return cls.from_dict(_loads(text))


@dataclass(frozen=True)
class Decision:
    """A ``decision`` turn: the one line the assistant must reply with, which is the plant of the
    ``concluded`` probe ``probe``. ``turn`` is the turn's index in ``Script.turns``."""

    turn: int
    probe: str
    label: str
    value: str
    reply: str  # "DECISION <label>: <value>"


@dataclass(frozen=True)
class Script:
    """A whole session: the user turns to send, in order, and the probes with their answers.

    The content turns come first, then the probe turns. ``turn_bytes`` is the UTF-8 size of each
    turn's user text; the files a ``toolloop`` turn reads are in ``files`` and are not counted there
    (``script.load_bytes`` adds them). Hashable: equal scripts hash equally, though ``files`` is a
    dict (it is hashed by its items).
    """

    seed: int
    turns: tuple[str, ...]
    probes: tuple[Probe, ...]
    paged_only: tuple[str, ...]  # ids of the D27 probes answerable only from the first turn
    turn_bytes: tuple[int, ...]
    probe_turn_ids: tuple[tuple[str, ...], ...] = ()  # the probe ids each probe turn asks, in order
    protocol: str = "d27"
    bound: int = 150_000  # the token bound the script was sized for
    turn_size: int = 40_000  # the content turn size asked for, in bytes
    kinds: tuple[str, ...] = ()  # one of KINDS per turn
    added_turns: tuple[int, ...] = ()  # turns protocol d27 does not have; the rest are d27's, in order
    decisions: tuple[Decision, ...] = ()
    files: dict[str, str] = field(default_factory=dict)  # relative path -> content, for the toolloop turn
    reads: tuple[str, ...] = ()  # the paths the toolloop turn reads, in the order it asks for them

    def __hash__(self) -> int:
        return hash((self.protocol, self.seed, self.bound, self.turn_size, self.turns, self.probes,
                     tuple(sorted(self.files.items())), self.reads))

    @property
    def n_content(self) -> int:
        return len(self.turns) - len(self.probe_turn_ids)

    @property
    def content_turns(self) -> tuple[str, ...]:
        return self.turns[: self.n_content]

    @property
    def content_bytes(self) -> tuple[int, ...]:
        return self.turn_bytes[: self.n_content]

    @property
    def probe_texts(self) -> tuple[str, ...]:
        return self.turns[self.n_content:]

    @property
    def probe_bytes(self) -> tuple[int, ...]:
        return self.turn_bytes[self.n_content:]

    @property
    def toolloop_turn(self) -> int | None:
        """The index of the turn that reads ``files``, or None when the script has none."""
        return self.kinds.index("toolloop") if "toolloop" in self.kinds else None

    def scripted_reply(self, turn: int) -> str | None:
        """What a correct assistant replies to content turn ``turn``: the decision line for a
        ``decision`` turn, ``OK`` for a ``log`` or ``toolloop`` turn. None for a probe turn, whose
        reply is the answers. The full-context arm uses it to script the history."""
        kind = self.kinds[turn]
        if kind == "probe":
            return None
        if kind == "decision":
            return next(d.reply for d in self.decisions if d.turn == turn)
        return "OK"

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT, "protocol": self.protocol, "seed": self.seed, "bound": self.bound,
            "turn_size": self.turn_size, "turns": list(self.turns), "kinds": list(self.kinds),
            "turn_bytes": list(self.turn_bytes), "added_turns": list(self.added_turns),
            "probes": [p.to_dict() for p in self.probes], "paged_only": list(self.paged_only),
            "probe_turn_ids": [list(ids) for ids in self.probe_turn_ids],
            "decisions": [asdict(d) for d in self.decisions], "files": dict(self.files), "reads": list(self.reads),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Script:
        found = data.get("format") if isinstance(data, dict) else type(data).__name__
        if not isinstance(data, dict) or type(found) is not int or found != FORMAT:  # True is not 1
            raise ValueError(f"not a benchmark script of format {FORMAT} (found {found!r})")
        try:
            files = data["files"]
            if not isinstance(files, dict) or not all(isinstance(v, str) for v in files.values()):
                raise TypeError("files must map a path to its text")
            if not isinstance(data["decisions"], list) or not all(isinstance(d, dict) for d in data["decisions"]):
                raise TypeError("decisions must be a list of objects")
            script = cls(
                seed=_whole(data["seed"], "seed", 0), turns=_strings(data["turns"]),
                probes=tuple(Probe.from_dict(p) for p in data["probes"]), paged_only=_strings(data["paged_only"]),
                turn_bytes=tuple(_whole(n, "turn_bytes", 0) for n in _list(data["turn_bytes"], "turn_bytes")),
                probe_turn_ids=tuple(_strings(ids) for ids in _list(data["probe_turn_ids"], "probe_turn_ids")),
                protocol=str(data["protocol"]), bound=_whole(data["bound"], "bound", 1),
                turn_size=_whole(data["turn_size"], "turn_size", 1), kinds=_strings(data["kinds"]),
                added_turns=tuple(_whole(n, "added_turns", 0) for n in _list(data["added_turns"], "added_turns")),
                decisions=tuple(Decision(_whole(d["turn"], "decisions", 0), str(d["probe"]), str(d["label"]),
                                         str(d["value"]), str(d["reply"])) for d in data["decisions"]),
                files={str(k): v for k, v in files.items()}, reads=_strings(data["reads"]),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"not a benchmark script: {exc!r}") from exc
        script._check_shape()
        return script

    def _check_shape(self) -> None:
        """Refuse a script whose parts do not fit together, so a damaged file fails here and not
        later with an index error or a file written outside the session. Each problem names its
        field; turn numbers in the messages count from 1."""
        from contexto.bench.d27 import normalize  # d27 imports this module

        n = len(self.turns)
        if len(self.kinds) != n:
            raise ValueError(f"not a benchmark script: kinds: {n} turns but {len(self.kinds)} kinds")
        if len(self.turn_bytes) != n:
            raise ValueError(f"not a benchmark script: turn_bytes: {n} turns but {len(self.turn_bytes)} sizes")
        problems: list[str] = []
        problems += [f"turn_bytes: turn {i + 1} is {size} bytes but its text is {len(text.encode('utf-8'))}"
                     for i, (text, size) in enumerate(zip(self.turns, self.turn_bytes))
                     if size != len(text.encode("utf-8"))]
        if self.protocol not in PROTOCOLS:
            problems.append(f"protocol: unknown protocol {self.protocol!r}")
        problems += [f"kinds: unknown turn kind {k!r}" for k in sorted(set(self.kinds) - set(KINDS))]

        by_id: dict[str, Probe] = {}
        for p in self.probes:
            if p.id in by_id:
                problems.append(f"probes: {p.id!r} twice")
            by_id[p.id] = p
            if p.kind not in PROBE_KINDS:
                problems.append(f"probes: {p.id!r} has kind {p.kind!r}")
            if p.cls not in D27_CLASSES + NEW_CLASSES:
                problems.append(f"probes: {p.id!r} has class {p.cls!r}")
            if not normalize(p.answer):
                problems.append(f"probes: {p.id!r} has an answer that is empty after normalisation, so no reply "
                                "can score correct")
        problems += [f"paged_only: unknown probe {i!r}" for i in self.paged_only if i not in by_id]

        k = len(self.probe_turn_ids)
        if k > n:
            problems.append(f"probe_turn_ids: {k} probe turns in {n} turns")
        else:
            problems += [f"kinds: turn {i + 1} is a probe turn but has kind {self.kinds[i]!r}"
                         for i in range(n - k, n) if self.kinds[i] != "probe"]
            problems += [f"kinds: turn {i + 1} is a content turn but has kind 'probe'"
                         for i in range(n - k) if self.kinds[i] == "probe"]
            for j, ids in enumerate(self.probe_turn_ids):
                lines = set(self.turns[n - k + j].split("\n"))
                problems += [f"turns: probe turn {j + 1} does not ask {i!r}" for i in ids
                             if i in by_id and f"{i}: {by_id[i].question}" not in lines]
        asked = Counter(i for ids in self.probe_turn_ids for i in ids)
        problems += [f"probe_turn_ids: unknown probe {i!r}" for i in asked if i not in by_id]
        problems += [f"probe_turn_ids: {i!r} is asked {asked[i]} times" for i in by_id if asked[i] != 1]
        if any(not 0 <= i < n for i in self.added_turns):
            problems += [f"added_turns: {i} is not a turn index" for i in self.added_turns if not 0 <= i < n]
        elif list(self.added_turns) != sorted(set(self.added_turns)):
            problems.append("added_turns: not in order, or one twice")

        seen: set[int] = set()
        for d in self.decisions:
            where = f"the decision for turn {d.turn + 1}"
            if not (0 <= d.turn < n and self.kinds[d.turn] == "decision"):
                problems.append(f"decisions: turn {d.turn + 1} is not a decision turn")
            if d.turn in seen:
                problems.append(f"decisions: two decisions for turn {d.turn + 1}")
            seen.add(d.turn)
            if d.probe not in by_id:
                problems.append(f"decisions: unknown probe {d.probe!r}")
            elif d.value != by_id[d.probe].answer:
                problems.append(f"decisions: the value of {where} is not the answer of {d.probe!r}")
            if d.reply != f"DECISION {d.label}: {d.value}":
                problems.append(f"decisions: the reply of {where} is not 'DECISION <label>: <value>'")
            if 0 <= d.turn < n and (not d.label.strip() or f"DECISION {d.label}:" not in self.turns[d.turn]):
                problems.append(f"decisions: the label of {where} is not asked for in that turn, so no reply "
                                "can be judged right")
        problems += [f"decisions: decision turn {i + 1} has no decision" for i, kind in enumerate(self.kinds)
                     if kind == "decision" and i not in seen]

        for path in self.files:
            parts = path.split("/")
            above = next((a for a in ("/".join(parts[:i]) for i in range(1, len(parts))) if a in self.files), None)
            problem = _path_problem(path) or (f"is below the file {above!r}" if above else None)
            if problem:
                problems.append(f"files: {path!r} {problem}")
        for path in self.reads:
            problem = _path_problem(path)
            if problem:
                problems.append(f"reads: {path!r} {problem}")
            elif path not in self.files:
                problems.append(f"reads: {path!r} is not among the files")
        problems += [f"reads: {path!r} is read twice" for path, count in Counter(self.reads).items() if count > 1]
        loops = self.kinds.count("toolloop")
        if loops > 1:
            problems.append(f"kinds: {loops} toolloop turns")
        elif loops and not self.reads:
            problems.append("reads: the toolloop turn reads no files")
        elif self.reads and not loops:
            problems.append("reads: files are read but no turn is a toolloop turn")
        if problems:
            raise ValueError(f"not a benchmark script: {'; '.join(problems[:5])}")

    def to_json(self) -> str:
        """The script as JSON text. The same script always gives the same bytes."""
        return json.dumps(self.to_dict(), indent=1) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Script:
        return cls.from_dict(_loads(text))

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> Script:
        return cls.from_json(path.read_text(encoding="utf-8"))

    def digest(self) -> str:
        """SHA-256 of the JSON text: one value that pins every turn, probe, answer and file."""
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()


def _whole(value: Any, field: str, minimum: int, what: str = "script") -> int:
    """``value`` if it is a whole number of at least ``minimum`` (not a bool or a float), else a
    ``ValueError`` that names ``field``."""
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"not a benchmark {what}: {field}: must be a whole number, {minimum} or more, "
                         f"found {value!r}")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError(f"{field} must be a list, found {type(value).__name__}")
    return value


def _path_problem(path: str) -> str | None:
    """What is wrong with a file path a script carries, or None. A path must be relative, use ``/``,
    hold no control character (U+0000 to U+001F), have no empty, ``.`` or ``..`` part, and lie under
    ``FILE_DIR``: the runner writes these files into the session's working directory. A path below
    another of the script's files is refused in ``Script._check_shape``."""
    if not path:
        return "is empty"
    if any(ord(c) < 0x20 for c in path):  # NUL fails the write; a newline breaks the tool-loop turn's list
        return "holds a control character"
    if "\\" in path:
        return "holds a backslash"
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return "is absolute"
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return "has an empty, '.' or '..' part"
    if len(parts) < 2 or parts[0] != FILE_DIR:
        return f"is not under {FILE_DIR}/"
    return None


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise TypeError(f"expected a list of strings, found {type(value).__name__}")
    if not all(isinstance(x, str) for x in value):
        raise TypeError("expected a list of strings")
    return tuple(value)


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not JSON: {exc}") from exc
    except RecursionError as exc:
        raise ValueError("not JSON: nested too deeply to read") from exc
