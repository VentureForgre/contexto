"""Test double for the MCP server's store: lane B's agreed API, including B2's pin, close, and
state_text, which the real MemoryStore gains when B2 (PR #28) merges. Recall and search are also
tested against the real MemoryStore in test_mcp.py."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from contexto import mcp

MCP_PATH = Path(mcp.__file__)


def load_mcp():
    return mcp


class FakeStore:
    """Mirrors SessionStore/MemoryStore as agreed with builder-3: get_page, page_ids, search, pin,
    close, state_text, log."""

    def __init__(self, pages: dict[str, dict[str, Any]] | None = None) -> None:
        self.pages = pages or {}
        self.records: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []

    def get_page(self, page_id: str) -> dict[str, Any] | None:
        return self.pages.get(page_id)

    def page_ids(self) -> list[str]:
        return sorted(self.pages, key=lambda p: -self.pages[p]["turn"])

    def search(self, query: str) -> list[str]:
        if not query:
            return []
        hits = []
        for pid in self.page_ids():
            page = self.pages[pid]
            texts = [str(v) for v in page["stub"].values() if isinstance(v, str)]
            texts += [x for v in page["stub"].values() if isinstance(v, list) for x in v]
            for m in page["messages"]:
                for b in m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]:
                    if b.get("type") == "text":
                        texts.append(b["text"])
                    elif b.get("type") == "tool_use":
                        texts.append(json.dumps(b.get("input", {})))
                    elif b.get("type") == "tool_result":
                        c = b.get("content")
                        texts.append(c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or []))
            if any(query in t for t in texts):
                hits.append(pid)
        return hits

    def pin(self, kind, text, *, supersedes=(), epistemic="reported", provenance=()):
        if epistemic == "verified" and not provenance:
            raise ValueError("a verified record needs provenance")
        rid = f"rec_{len(self.records):024x}"
        self.records.append({"id": rid, "kind": kind, "text": text, "supersedes": list(supersedes), "open": kind == "obligation"})
        return rid

    def close(self, obligation_id, note):
        for r in self.records:
            if r["id"] == obligation_id and r["kind"] == "obligation" and r["open"]:
                r["open"] = False
                rid = f"rec_{len(self.records):024x}"
                self.records.append({"id": rid, "kind": "closure", "text": note})
                return rid
        raise ValueError(f"{obligation_id} is not an open obligation")

    def state_text(self) -> str:
        lines = ["PINNED STATE"]
        lines += [f"- {r['kind']} {r['id']}: {r['text']}" for r in self.records if r["kind"] != "closure" and r.get("open", True)]
        return "\n".join(lines)

    def log(self, event: dict[str, Any]) -> None:
        self.events.append(dict(event))


def page(page_id: str, turn: int, messages: list[dict[str, Any]], user_text: str = "", tools=(), paths=()):
    return {"id": page_id, "turn": turn, "messages": messages, "bytes": 0, "tokens": 0,
            "stub": {"user_text": user_text, "tools": list(tools), "paths": list(paths)}}
