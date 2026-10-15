"""Conformance probe: which history edits do the APIs accept once reasoning blocks are in history?

Synthetic content only. Direct API calls through the base URL configured for this worktree; no CLI
traffic. Records status codes, error types, short error messages, usage numbers, and counts of
returned block types. Never records prompts, responses, signatures, encrypted items, or credentials.

Usage, from the repository root:
    python3 spikes/m0/conformance/probe.py --settings .claude/settings.local.json \
        --out .contexto/spikes/l2b/conformance [--cap 45000] [--skip-openai]

Credential: the settings file's apiKeyHelper is run at runtime and its output is held in memory
for the request headers only. It is never printed, logged, or written. The base URL is taken from
the settings file's env block when present (that is what the CLI uses), else from the environment.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ANTHROPIC_MODEL = "claude-haiku-5-5"
OPENAI_MODEL = "gpt-6-astra"
BINDING_BETA = "thinking-binding-controls-2026-08-01"
CONTEXT_BETA = "context-management-2025-06-27"
SYSTEM = "You are a terse calculator. Think it through, then reply with the final number only."
TURNS = (
    "Compute 37 times 43, then add the number of letters in the word preserved. Number only.",
    "Subtract 17 from that, multiply by 3, and take the result modulo 1000. Number only.",
    "Divide that by 6, rounding down, then add 41. Number only.",
)


class Budget(Exception):
    pass


class SetupFailed(Exception):
    pass


class Client:
    def __init__(self, settings: Path, cap: int) -> None:
        data = json.loads(settings.read_text(encoding="utf-8"))
        helper = data.get("apiKeyHelper")
        if not helper:
            raise SystemExit("settings file has no apiKeyHelper; stopping rather than looking for a key elsewhere")
        env_base = (data.get("env") or {}).get("ANTHROPIC_BASE_URL")
        self.base_source = "settings_env" if env_base else "process_env"
        base = env_base or os.environ.get("ANTHROPIC_BASE_URL")
        if not base:
            raise SystemExit("no base URL in the settings env block or the environment")
        self.base = base.rstrip("/")
        proc = subprocess.run(helper, shell=True, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise SystemExit(f"apiKeyHelper failed (exit {proc.returncode}); output not shown")
        self._key = proc.stdout.strip()
        self.cap = cap
        self.spent = 0
        self.records: list[dict[str, Any]] = []

    def _scrub(self, text: str) -> str:
        return text.replace(self._key, "[redacted]") if self._key else text

    def post(self, case: str, path: str, body: dict[str, Any], headers: dict[str, str], provider: str) -> dict[str, Any] | None:
        if self.spent >= self.cap:
            raise Budget(f"cap {self.cap} reached before {case}")
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode("utf-8"), method="POST")
        req.add_header("content-type", "application/json")
        # An honest client name; the default Python-urllib agent is refused upstream (error 1010).
        req.add_header("user-agent", "contexto-conformance-probe/0.1")
        for k, v in headers.items():
            req.add_header(k, v)
        # Same credential, sent the way Claude Code sends an apiKeyHelper value: both headers.
        req.add_header("authorization", f"Bearer {self._key}")
        if provider == "anthropic":
            req.add_header("x-api-key", self._key)
            req.add_header("anthropic-version", "2023-06-01")
        record: dict[str, Any] = {"case": case, "provider": provider, "betas": headers.get("anthropic-beta", "")}
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                record["status"] = resp.status
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8", errors="replace")
            record["status"] = err.code
            try:
                e = json.loads(raw).get("error", {})
                record["error_type"] = e.get("type") or e.get("code")
                record["error_message"] = self._scrub(str(e.get("message", "")))[:300]
            except (json.JSONDecodeError, AttributeError):
                record["error_type"] = "unparsed"
                record["error_chars"] = len(raw)
                if len(raw) <= 80:
                    record["error_message"] = self._scrub(raw)
            payload = None
        except urllib.error.URLError as err:
            record["status"] = None
            record["error_type"] = f"network: {type(err.reason).__name__}"
            payload = None
        record["seconds"] = round(time.monotonic() - started, 2)
        if payload is not None:
            usage = payload.get("usage") or {}
            numbers = {k: v for k, v in usage.items() if isinstance(v, int)}
            for k, v in usage.items():
                if isinstance(v, dict):
                    numbers.update({f"{k}.{kk}": vv for kk, vv in v.items() if isinstance(vv, int)})
            record["usage"] = numbers
            self.spent += sum(v for k, v in numbers.items() if k in (
                "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            if provider == "anthropic":
                record["blocks"] = [b.get("type") for b in payload.get("content", [])]
                record["input_transformations"] = [
                    {"type": t.get("type"), "reason": t.get("reason")} for t in payload.get("input_transformations") or []
                ]
                record["stop_reason"] = payload.get("stop_reason")
            else:
                record["items"] = [i.get("type") for i in payload.get("output", [])]
        record["cumulative_tokens"] = self.spent
        self.records.append(record)
        return payload


def user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def strip_thinking(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = copy.deepcopy(messages)
    for m in out:
        if m["role"] == "assistant":
            m["content"] = [b for b in m["content"] if b.get("type") not in ("thinking", "redacted_thinking")]
    return out


def anthropic_cases(c: Client) -> None:
    def body(messages: list[dict[str, Any]], system: str = SYSTEM, **extra: Any) -> dict[str, Any]:
        b = {"model": ANTHROPIC_MODEL, "max_tokens": 4000, "system": system, "messages": messages,
             "thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}
        b.update(extra)
        return b

    def binding(behavior: str) -> dict[str, Any]:
        return {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": behavior}}

    history = [user(TURNS[0])]
    for n, turn in enumerate(TURNS[1:], start=1):
        resp = c.post(f"setup_{n}", "/v1/messages", body(history), {}, "anthropic")
        if resp is None:
            raise SetupFailed(f"setup_{n} failed; see its record")
        history += [{"role": "assistant", "content": resp["content"]}, user(turn)]
    thinking_turns = sum(1 for m in history if m["role"] == "assistant" and any(b.get("type") == "thinking" for b in m["content"]))
    c.records.append({"case": "setup_summary", "assistant_turns_with_thinking": thinking_turns, "messages": len(history)})
    if thinking_turns == 0:
        c.records.append({"case": "note", "text": "no thinking blocks were produced; edit cases would not test anything"})
        return

    dropped = history[2:]  # oldest user and assistant turns removed; later thinking kept verbatim
    added_text = copy.deepcopy(history)
    added_text[2]["content"].insert(0, {"type": "text", "text": "PAGED HISTORY: [paged pg_0000000000000000 | turns 0-0]"})
    pinned_system = SYSTEM + "\nPINNED STATE: decision=x"
    epoch = strip_thinking(dropped)
    epoch[0]["content"].insert(0, {"type": "text", "text": "PAGED HISTORY: [paged pg_0000000000000000 | turns 0-0]"})
    error_hdr = {"anthropic-beta": BINDING_BETA}

    c.post("A1_append_only", "/v1/messages", body(history), {}, "anthropic")
    c.post("A2_strip_all_thinking", "/v1/messages", body(strip_thinking(history)), {}, "anthropic")
    c.post("A3_drop_oldest_keep_thinking_default", "/v1/messages", body(dropped), {}, "anthropic")
    c.post("A3e_drop_oldest_keep_thinking_error", "/v1/messages", body(dropped, thinking=binding("error")), error_hdr, "anthropic")
    c.post("A4e_change_system_keep_thinking_error", "/v1/messages",
           body(history, system=pinned_system, thinking=binding("error")), error_hdr, "anthropic")
    c.post("A5e_text_in_earlier_user_keep_thinking_error", "/v1/messages",
           body(added_text, thinking=binding("error")), error_hdr, "anthropic")
    c.post("A6e_epoch_rewrite_with_strip_error", "/v1/messages",
           body(epoch, system=pinned_system, thinking=binding("error")), error_hdr, "anthropic")
    c.post("A7_drop_oldest_drop_block", "/v1/messages", body(dropped, thinking=binding("drop_block")), error_hdr, "anthropic")
    c.post("A8_server_clear_thinking", "/v1/messages",
           body(history, context_management={"edits": [{"type": "clear_thinking_20251015"}]}),
           {"anthropic-beta": CONTEXT_BETA}, "anthropic")


def openai_cases(c: Client) -> None:
    def body(items: list[dict[str, Any]]) -> dict[str, Any]:
        return {"model": OPENAI_MODEL, "input": items, "reasoning": {"effort": "medium"}, "store": False,
                "include": ["reasoning.encrypted_content"], "max_output_tokens": 2000}

    first = [{"role": "user", "content": TURNS[0]}]
    resp = c.post("O0_setup", "/v1/responses", body(first), {}, "openai")
    if resp is None:
        return
    output = resp.get("output", [])
    if not any(i.get("type") == "reasoning" and i.get("encrypted_content") for i in output):
        c.records.append({"case": "note", "text": "no encrypted reasoning item returned; edit cases skipped"})
        return
    follow = {"role": "user", "content": TURNS[1]}
    c.post("O1_append_only", "/v1/responses", body(first + output + [follow]), {}, "openai")
    edited = [{"role": "user", "content": "PAGED HISTORY: [paged pg_0000000000000000]\n" + TURNS[0]}]
    c.post("O2_edit_user_before_reasoning", "/v1/responses", body(edited + output + [follow]), {}, "openai")
    c.post("O3_drop_user_before_reasoning", "/v1/responses", body(output + [follow]), {}, "openai")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cap", type=int, default=45_000, help="stop before a call once spend reaches this")
    parser.add_argument("--skip-openai", action="store_true")
    args = parser.parse_args(argv)
    client = Client(args.settings, args.cap)
    status = 0
    try:
        anthropic_cases(client)
        if not args.skip_openai:
            openai_cases(client)
    except (Budget, SetupFailed) as exc:
        client.records.append({"case": "stopped", "reason": str(exc)})
        status = 1
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"probe-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    summary = {"base_url_source": client.base_source, "total_tokens": client.spent, "records": client.records}
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    for r in client.records:
        print(json.dumps(r))
    print(f"total_tokens={client.spent} written={path.name}")
    return status


if __name__ == "__main__":
    sys.exit(main())
