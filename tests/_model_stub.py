"""Temporary stand-in for contexto.model until lane A packet A1 merges.

Field shape only, copied from the normative contract. Delete this file and the
fallback in conftest.py when rebasing onto a main that has contexto.model.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    type: str
    locator: str
    sha256: str | None = None
    span: str | None = None


@dataclass(frozen=True)
class Record:
    id: str
    kind: str
    epistemic: str
    created_at: str
    author: str
    body: str
    prev: str
    provenance: tuple[Source, ...] = ()
    session: str | None = None
    topic: str | None = None
    supersedes: tuple[str, ...] = ()
    target: str | None = None
    owner: str | None = None
