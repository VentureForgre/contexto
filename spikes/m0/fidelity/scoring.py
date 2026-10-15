"""Deterministic scoring against planted ground truth. No model judge.

An answer is correct when its normalized form equals the normalized expected answer. Normalizing
casefolds, trims whitespace, quotes, backticks, and trailing punctuation, and collapses inner
whitespace. Nothing else is forgiven: a stale (superseded) value scores wrong, and so does a value
embedded in a longer sentence.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from fidelity.session import Probe

_TRIM = " \t\r\n\"'`.,;:!"
_ANSWER_LINE = re.compile(r"^\s*(q_[a-z0-9_]+)\s*[:=]\s*(.*?)\s*$")


def normalize(text: str | None) -> str:
    if text is None:
        return ""
    return " ".join(text.strip(_TRIM).casefold().split()).strip(_TRIM)


def parse_answers(text: str) -> dict[str, str]:
    """Parse ``q_<id>: <answer>`` lines. The last line for an id wins; other lines are ignored."""
    answers: dict[str, str] = {}
    for line in text.splitlines():
        match = _ANSWER_LINE.match(line.strip().lstrip("-* "))
        if match:
            answers[match.group(1)] = match.group(2)
    return answers


@dataclass(frozen=True)
class Score:
    total: int
    correct: int
    by_category: Mapping[str, tuple[int, int]]  # category -> (correct, total)
    wrong: tuple[str, ...]  # probe ids answered wrong or not at all
    stale: tuple[str, ...]  # wrong probes whose answer equals an older planted value

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


def score(
    probes: Sequence[Probe],
    answers: Mapping[str, str | None],
    history: Mapping[str, Sequence[str]] | None = None,
) -> Score:
    """Score ``answers`` (probe id -> answer text). ``history`` maps a probe key to its older values."""
    by_category: dict[str, list[int]] = {}
    wrong: list[str] = []
    stale: list[str] = []
    for probe in probes:
        cell = by_category.setdefault(probe.category, [0, 0])
        cell[1] += 1
        given = normalize(answers.get(probe.id))
        if given and given == normalize(probe.answer):
            cell[0] += 1
            continue
        wrong.append(probe.id)
        older = {normalize(v) for v in (history or {}).get(probe.key, ())}
        if given and given in older:
            stale.append(probe.id)
    correct = sum(c for c, _ in by_category.values())
    return Score(
        total=len(probes),
        correct=correct,
        by_category={k: (v[0], v[1]) for k, v in sorted(by_category.items())},
        wrong=tuple(wrong),
        stale=tuple(stale),
    )


def older_values(planted: Mapping[str, object]) -> dict[str, list[str]]:
    """Map each planted key to the values it held before its current one."""
    out: dict[str, list[str]] = {}
    for key, p in planted.items():
        history = getattr(p, "history", ())
        out[key] = [value for _, value in history[:-1]]
    return out
