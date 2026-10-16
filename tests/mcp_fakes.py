"""Test doubles for the MCP server: a store with lane B's documented API, and a loader that does
not need the package installed (B1 lands the package scaffolding)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

MCP_PATH = Path(__file__).resolve().parent.parent / "src" / "contexto" / "mcp.py"


def load_mcp():
    spec = importlib.util.spec_from_file_location("contexto_mcp_under_test", MCP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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
