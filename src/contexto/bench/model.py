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
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

FORMAT = 1
PROTOCOLS = ("d27", "v2")
D27_CLASSES = ("paged_only", "multihop", "chain", "obligations", "distractor")
NEW_CLASSES = ("paraphrase", "concluded", "toolloop")
# What a runner does with each turn: "log" pastes log text and expects the reply OK, "decision"
# expects one DECISION line, "toolloop" needs the script's files on disk and expects OK after the
# reads, "probe" asks questions.
KINDS = ("log", "decision", "toolloop", "probe")

_WORD = re.compile(r"[a-z0-9]+")
MIN_WORD = 4
# Function words of four or more letters. They say nothing about a subject, so a question and a plant
# that share one are not lexically closer for it.
STOP_WORDS = frozenset((
    "about", "been", "does", "each", "from", "have", "into", "over", "that", "their", "them", "then",
    "there", "these", "they", "this", "those", "were", "what", "when", "where", "which", "will", "with",
    "your",
))


def words(text: str) -> frozenset[str]:
    """The word tokens of ``text`` that the lexical-overlap figure counts.

    A token is a maximal run of ASCII letters and digits, after case folding, so every other
    character separates tokens: ``billing-core.retention_days=amber-4417`` gives ``billing``,
    ``core``, ``retention``, ``days``, ``amber`` and ``4417``. Tokens shorter than four characters
    and the function words in ``STOP_WORDS`` are dropped.
    """
    return frozenset(w for w in _WORD.findall(text.casefold()) if len(w) >= MIN_WORD and w not in STOP_WORDS)


def lexical_overlap(question: str, statements: Iterable[str]) -> int:
    """How many distinct word tokens (``words``) a question shares with its plant statements.

    It measures how much a word search is helped: Contexto's search matches a page that holds every
    word of the query, so a question that shares no word with its plant gives the model nothing to
    search for except its own guess at the plant's wording.
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
                       statements=_strings(data["statements"]), cls=str(data["cls"]), overlap=int(data["overlap"]))
        except (KeyError, TypeError, ValueError) as exc:
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
    (``script.load_bytes`` adds them).
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
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            found = data.get("format") if isinstance(data, dict) else type(data).__name__
            raise ValueError(f"not a benchmark script of format {FORMAT} (found {found!r})")
        try:
            files = data["files"]
            if not isinstance(files, dict) or not all(isinstance(v, str) for v in files.values()):
                raise TypeError("files must map a path to its text")
            return cls(
                seed=int(data["seed"]), turns=_strings(data["turns"]),
                probes=tuple(Probe.from_dict(p) for p in data["probes"]), paged_only=_strings(data["paged_only"]),
                turn_bytes=tuple(int(n) for n in data["turn_bytes"]),
                probe_turn_ids=tuple(_strings(ids) for ids in data["probe_turn_ids"]),
                protocol=str(data["protocol"]), bound=int(data["bound"]), turn_size=int(data["turn_size"]),
                kinds=_strings(data["kinds"]), added_turns=tuple(int(n) for n in data["added_turns"]),
                decisions=tuple(Decision(int(d["turn"]), str(d["probe"]), str(d["label"]), str(d["value"]),
                                         str(d["reply"])) for d in data["decisions"]),
                files={str(k): v for k, v in files.items()}, reads=_strings(data["reads"]),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"not a benchmark script: {exc!r}") from exc

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
