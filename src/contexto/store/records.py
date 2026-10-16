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

``state_text`` and ``pinned_text`` check file sources against the session's working directory
(``workdir()``, recorded by the launcher with ``set_workdir``). Without one they say that file
staleness was not checked, rather than presenting file-backed records as fresh.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from contexto.store.ledger import LedgerCorruptError, LedgerError
from contexto.store.model import Source, ValidationError
from contexto.store.pinned import render_pinned, render_state
from contexto.store.state import State, derive

DEFAULT_OWNER = "assistant"


class LedgerOps:
    """Mixin; the store provides ``ledger``, ``session_id``, and ``get_page``."""

    def _append(self, draft: dict[str, Any], precondition=None) -> str:
        try:
            return self.ledger.append(draft, precondition=precondition).id
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
        if supersedes:
            draft["supersedes"] = list(supersedes)
        if topic is not None:
            draft["topic"] = topic
        if kind == "obligation":
            draft["owner"] = owner or DEFAULT_OWNER
        elif owner is not None:
            raise ValueError(f"owner is only allowed on obligations, not {kind!r}")
        return self._append(draft)

    def close(self, obligation_id: str, note: str, *, author: str = "model") -> str:
        def still_open(existing):
            # Runs inside the ledger lock, so two concurrent closes cannot both pass.
            if obligation_id not in {r.id for r in derive(existing).open_obligations}:
                raise ValueError(f"not an open obligation: {obligation_id}")

        return self._append({"kind": "closure", "epistemic": "reported", "author": author, "body": note,
                             "provenance": [], "session": self.session_id, "target": obligation_id},
                            precondition=still_open)

    def state(self, root: Path | None = None) -> State:
        return derive(self.ledger.records(), root)

    def state_text(self) -> str:
        root = self.workdir()
        return render_state(self.state(root), staleness_checked=root is not None)

    def pinned_text(self, budget_tokens: int) -> str:
        root = self.workdir()
        return render_pinned(self.state(root), budget_tokens, staleness_checked=root is not None)
