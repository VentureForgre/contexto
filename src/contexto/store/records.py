"""Ledger operations shared by ``SessionStore`` and ``MemoryStore``: the store API the MCP server calls.

- ``pin(kind, text, *, supersedes=(), epistemic="reported", provenance=(), owner=None, topic=None,
  author="model") -> str``: append a record and return its id. Raises ``ValueError`` when the
  record is invalid (unknown kind or epistemic, verified without provenance, a file source without
  sha256, a page source citing a page this store does not have, unknown supersedes ids).
  Obligations default to owner ``"assistant"``.
- ``close(obligation_id, note, *, author="model") -> str``: append a closure; ``ValueError`` unless
  ``obligation_id`` is an open obligation.
- ``state(root=None) -> State``: derived state; staleness is checked against ``root`` when given.
- ``state_text() -> str``: the full pinned state with no budget cap.
- ``pinned_text(budget_tokens) -> str``: the capped pinned text for the preamble.
- ``project() -> Project | None``, ``carried() -> tuple[Carried, ...]`` and ``carried_text(budget_tokens)
  -> str`` (D69, ``contexto.store.project``): the session's view of its project, the records it carries
  from the project's earlier sessions, and the carried section alone (``""`` when nothing is carried).
  ``state_text`` and ``pinned_text`` show the carried records as a section after the session's own;
  ``close`` and ``pin(supersedes=...)`` accept a carried record's id, and write to this session's
  ledger only. Removals are project-wide: a record another session of the project closed or replaced,
  earlier or later, is closed or replaced in this session's texts too, its own records included, and a
  conflict with a carried decision is marked. ``pinned_state(root)`` is the state and the carried
  records those texts render. A failure anywhere in the project part (never the session's own ledger)
  reads as nothing carried and logs the content-free ``project_unavailable``: another session can never
  break this one's texts, pins or closes. With nothing carried every method returns what it did before
  D69.

``state_text`` and ``pinned_text`` check file sources against the session's working directory
(``workdir()``, recorded by the launcher with ``set_workdir``). Without one they say that file
staleness was not checked, rather than presenting file-backed records as fresh.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from contexto.store.ledger import LedgerCorruptError, LedgerError
from contexto.store.model import Record, Source, ValidationError
from contexto.store.pinned import render_carried, render_pinned, render_state
from contexto.store.recall import VARIANTS, check_variant, env_variant, opening_lines
from contexto.store.state import State, derive

if TYPE_CHECKING:
    from contexto.store.project import Carried, Project

DEFAULT_OWNER = "assistant"


class LedgerOps:
    """Mixin; the store provides ``ledger``, ``session_id``, and ``get_page``."""

    def project(self) -> Project | None:
        """This session's view of its project (D69); None for a store that has none."""
        return None

    def carried(self) -> tuple[Carried, ...]:
        """The records this session carries from the earlier sessions of its project; ``()`` without one."""
        records = self.ledger.records()
        return self._project_part(lambda project: project.carried(records), ())

    def _project_part(self, compute: Callable[[Project], Any], fallback: Any) -> Any:
        """``compute(project)``, or ``fallback`` when this session has no project. Other sessions can
        never break this one: any failure in the project part (the session's own ledger is read before
        it) reads as nothing carried and logs the content-free ``project_unavailable`` event."""
        try:
            project = self.project()
            return fallback if project is None else compute(project)
        except Exception as error:
            try:
                if not getattr(self, "_read_only", False):  # a store opened read-only writes nothing
                    self.log({"event": "project_unavailable", "error": type(error).__name__})
            except Exception:  # a log write must never fail a render or a pin
                pass
            return fallback

    def pinned_state(self, root: Path | None = None) -> tuple[State, tuple[Carried, ...]]:
        """The session's own state and what it carries, from one read of its ledger. With a project the
        state takes the project's removals and conflicts (``Project.session_state``)."""
        records = self.ledger.records()
        return self._project_part(lambda project: project.session_state(records, root), None) or (
            derive(records, root), ())

    def _append(self, draft: dict[str, Any], precondition=None, known: Sequence[Record] = ()) -> str:
        try:
            return self.ledger.append(draft, precondition=precondition, known=known).id
        except LedgerCorruptError:
            raise
        except (ValidationError, LedgerError) as e:
            raise ValueError(str(e)) from e

    def pin(self, kind: str, text: str, *, supersedes=(), epistemic: str = "reported", provenance=(),
            owner: str | None = None, topic: str | None = None, author: str = "model") -> str:
        sources = []
        for item in provenance or ():
            try:
                source = item if isinstance(item, Source) else Source.from_dict(item)
            except ValidationError as e:
                raise ValueError(str(e)) from e
            if source.type == "page" and self.get_page(source.locator) is None:
                raise ValueError(f"page source cites an unknown page: {source.locator}")
            sources.append(source.to_dict())
        draft: dict[str, Any] = {"kind": kind, "epistemic": epistemic, "author": author, "body": text,
                                 "provenance": sources, "session": self.session_id}
        known: Sequence[Record] = ()
        if supersedes:
            draft["supersedes"] = list(supersedes)
            known = [c.record for c in self.carried()]  # a carried record can be replaced (D69)
        if topic is not None:
            draft["topic"] = topic
        if kind == "obligation":
            draft["owner"] = owner or DEFAULT_OWNER
        elif owner is not None:
            raise ValueError(f"owner is only allowed on obligations, not {kind!r}")
        return self._append(draft, known=known)

    def close(self, obligation_id: str, note: str, *, author: str = "model") -> str:
        # A carried obligation can be closed (D69). The closure goes to this session's ledger only; a
        # later session of the project then finds the obligation closed.
        known = [c.record for c in self.carried()]

        def still_open(existing):
            # Runs inside the ledger lock, so two concurrent closes in this session cannot both pass.
            # Two sessions closing one carried obligation at the same moment hold different ledger
            # locks, so both closures can be written, which is harmless. With a project it asks the
            # project-wide state, in which every other session's closure counts too (earlier or later,
            # memory on or off), through a view made now: the one above may have read the other
            # ledgers before that closure.
            state = self._project_part(lambda project: project.state(existing), None) or derive(existing)
            if obligation_id not in {r.id for r in state.open_obligations}:
                raise ValueError(f"not an open obligation: {obligation_id}")

        return self._append({"kind": "closure", "epistemic": "reported", "author": author, "body": note,
                             "provenance": [], "session": self.session_id, "target": obligation_id},
                            precondition=still_open, known=known)

    def state(self, root: Path | None = None) -> State:
        return derive(self.ledger.records(), root)

    def state_text(self, variant: str | None = None) -> str:
        root = self.workdir()
        state, carried = self.pinned_state(root)
        return render_state(state, staleness_checked=root is not None,
                            opening=opening_lines(self.recall_variant(variant)), carried=carried)

    def pinned_text(self, budget_tokens: int, variant: str | None = None) -> str:
        root = self.workdir()
        state, carried = self.pinned_state(root)
        return render_pinned(state, budget_tokens, staleness_checked=root is not None,
                             opening=opening_lines(self.recall_variant(variant)), carried=carried)

    def carried_text(self, budget_tokens: int) -> str:
        """The carried section alone, within ``budget_tokens``; ``""`` when nothing is carried (D69)."""
        return render_carried(self.carried(), budget_tokens)

    def recall_variant(self, variant: str | None = None) -> str:
        """The recall-guidance variant: ``variant`` if given, else the one recorded for this session,
        else ``CONTEXTO_RECALL_VARIANT`` (default ``default``), recorded on first use when writable."""
        if variant is not None:
            return check_variant(variant)
        recorded = self._recorded_variant()
        if recorded in VARIANTS:
            return recorded
        chosen = env_variant()
        self._record_variant(chosen)
        return chosen
