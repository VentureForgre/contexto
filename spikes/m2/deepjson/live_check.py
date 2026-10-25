#!/usr/bin/env python3
"""What the Messages API does with a request nested deeper than Contexto transforms (PR #225).

Sends synthetic requests whose one ``tool_use.input`` holds a list nested ``depth`` levels deep. Each
depth goes first to ``/v1/messages/count_tokens`` (no spend, without ``max_tokens``), then to
``/v1/messages`` with ``max_tokens`` 8 on claude-haiku-5-5: when count_tokens accepted it and the count
fits under the remaining cap, or when it refused it and the body is under 20,000 bytes (a refused
request costs nothing; the check is whether ``/v1/messages`` refuses it too).

The credential comes from the seat's apiKeyHelper at call time (Contexto's own settings order) and
stays in memory; the base URL is the inherited ANTHROPIC_BASE_URL. Neither is printed or stored.
Records status, error type, the first 160 characters of an error message, and token counts only.

Usage: live_check.py [--cap 50000] [depth ...]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from contexto.codex import claude_api_key_helper

MODEL = "claude-haiku-5-5"
UA = "contexto-test/deepjson-live-check"  # D23: names this harness
DEPTHS = [64, 100, 115, 120, 121, 122, 125, 128, 1_000, 2_000, 9_999, 20_000, 120_000]


def body(depth: int, max_tokens: bool = True) -> bytes:
    """The request with ``"input": {"a": <a list nested depth levels>}``, written as text: the deep
    part is spliced in, so building it needs no recursion."""
    request = {"model": MODEL, **({"max_tokens": 8} if max_tokens else {}),
               "tools": [{"name": "probe", "input_schema": {"type": "object"}}],
               "messages": [{"role": "user", "content": "Synthetic prompt: call probe."},
                            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_deep1",
                                                               "name": "probe", "input": {"a": "DEEP"}}]},
                            {"role": "user", "content": [
                                {"type": "tool_result", "tool_use_id": "toolu_deep1", "content": "ok"},
                                {"type": "text", "text": "Reply with the word ok."}]}]}
    return json.dumps(request, separators=(",", ":")).replace('"DEEP"', "[" * depth + "]" * depth).encode()


def send(base: str, key: str, path: str, data: bytes) -> tuple[int, dict]:
    request = urllib.request.Request(base.rstrip("/") + path, data=data, method="POST", headers={
        "content-type": "application/json", "anthropic-version": "2023-06-01", "x-api-key": key,
        "authorization": f"Bearer {key}", "user-agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"error": {"type": "(not JSON)", "message": raw[:160].decode("utf-8", "replace")}}
    except urllib.error.URLError as e:
        return 0, {"error": {"type": f"network: {type(e.reason).__name__}", "message": ""}}


def summary(status: int, payload: dict) -> str:
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    usage = payload.get("usage") or {}
    if error:
        return f"{status} {error.get('type')}: {str(error.get('message', ''))[:160]}"
    if "input_tokens" in payload:
        return f"{status} input_tokens={payload['input_tokens']}"
    return f"{status} input={usage.get('input_tokens')} output={usage.get('output_tokens')}"


def main(argv: list[str]) -> int:
    cap = int(argv[argv.index("--cap") + 1]) if "--cap" in argv else 50_000
    depths = [int(a) for a in argv if a.isdigit() and (not argv.index(a) or argv[argv.index(a) - 1] != "--cap")]
    base = os.environ.get("ANTHROPIC_BASE_URL") or sys.exit("no inherited ANTHROPIC_BASE_URL")
    helper = claude_api_key_helper(Path.cwd(), Path.home(), dict(os.environ)) or sys.exit("no apiKeyHelper")
    proc = subprocess.run(helper, shell=True, capture_output=True, text=True, timeout=60)
    key = proc.stdout.strip()
    if proc.returncode != 0 or not key:
        sys.exit(f"the apiKeyHelper failed (exit {proc.returncode}); output not shown")
    spent = 0
    for depth in depths or DEPTHS:
        data = body(depth)
        status, payload = send(base, key, "/v1/messages/count_tokens", body(depth, max_tokens=False))
        print(f"depth {depth:>7} ({len(data):>7} bytes) count_tokens: {summary(status, payload)}")
        counted = payload.get("input_tokens") if status == 200 else None
        if counted is None and len(data) > 20_000:
            print(f"depth {depth:>7} messages: not sent (count_tokens refused; body over 20,000 bytes)")
            continue
        if counted is not None and spent + counted + 8 > cap:
            print(f"depth {depth:>7} messages: not sent (over the cap)")
            continue
        status, payload = send(base, key, "/v1/messages", data)
        usage = payload.get("usage") or {}
        spent += sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                                  "cache_read_input_tokens", "output_tokens"))
        print(f"depth {depth:>7} messages: {summary(status, payload)} spent={spent}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
