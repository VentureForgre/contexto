#!/usr/bin/env python3
"""L1-e black-box identity probe of the seat's model route (Anthropic Messages API).

Sends controlled synthetic requests through the base URL this seat already uses and records only
HTTP status, usage numbers, input_transformations / thinking_dropped entries (type and reason),
response header NAMES, and HMAC-SHA256 hashes of header values under a per-run random key that is
never written down. The credential comes from the seat's existing apiKeyHelper at runtime and is
held in memory only; the base URL comes from the inherited ANTHROPIC_BASE_URL. Neither is printed,
stored, or logged.

Usage: probe.py <series A|B> <out.jsonl> [--cap TOKENS] [--model M] [--binding none|drop_block|error]
                [--user-id] [--lines N] [--n REQUESTS] [--hard]
--binding adds the thinking-binding-controls-2026-08-01 beta and
thinking.block_binding.prefix_mismatch_behavior; --user-id sends a fixed synthetic metadata.user_id.
"""
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

MODEL = "claude-haiku-5-5"
UA = "contexto-m0-l1e-probe/0.1 (python-urllib; black-box route identity test)"
RUN_KEY = secrets.token_bytes(32)  # per-run HMAC key, never written down
SKIP_HASH = {"date", "content-length", "content-type", "connection", "transfer-encoding",
             "content-encoding", "vary", "server"}


def credential() -> str:
    settings = json.load(open(os.path.join(os.getcwd(), ".claude", "settings.local.json")))
    out = subprocess.run(settings["apiKeyHelper"], shell=True, capture_output=True, text=True, timeout=30)
    key = out.stdout.strip()
    if out.returncode != 0 or not key:
        sys.exit("apiKeyHelper did not return a credential")
    return key


def keyed(value: str) -> str:
    return hmac.new(RUN_KEY, value.encode(), hashlib.sha256).hexdigest()[:16]


BETAS = []


def call(base: str, key: str, body: dict) -> tuple[int, dict, dict]:
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01",
               "x-api-key": key, "authorization": f"Bearer {key}", "user-agent": UA}
    if BETAS:
        headers["anthropic-beta"] = ",".join(BETAS)
    req = urllib.request.Request(
        base.rstrip("/") + "/v1/messages", data=json.dumps(body).encode(), method="POST",
        headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, dict(r.headers.items()), json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read())
        except Exception:
            payload = {}
        return e.code, dict(e.headers.items()), payload
    except urllib.error.URLError as e:
        sys.exit(f"network error: {type(e.reason).__name__}")


ENUM = __import__("re").compile(r"[a-z0-9_.:-]{1,48}")


def shape(obj, path=""):
    """Key paths of an API metadata object, with values kept only when they are numbers, booleans,
    or short lowercase enum-like strings. Free text is replaced by its type name."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(shape(v, f"{path}.{k}" if path else k))
    elif isinstance(obj, list):
        out[f"{path}[]"] = len(obj)
        for i, v in enumerate(obj[:5]):
            out.update(shape(v, f"{path}[{i}]"))
    elif isinstance(obj, (int, float, bool)) or obj is None:
        out[path] = obj
    elif isinstance(obj, str):
        out[path] = obj if ENUM.fullmatch(obj) else f"<str:{len(obj)}>"
    return out


def transformations(payload: dict) -> list:
    found = []
    for k in ("input_transformations", "thinking_dropped"):
        v = payload.get(k)
        if v:
            for item in (v if isinstance(v, list) else [v]):
                if isinstance(item, dict):
                    found.append({"field": k, "type": item.get("type"), "reason": item.get("reason"),
                                  "shape": shape(item)})
                else:
                    found.append({"field": k, "value_type": type(item).__name__})
    return found


def record(i: int, status: int, headers: dict, payload: dict, extra: dict) -> dict:
    lower = {k.lower(): v for k, v in headers.items()}
    u = payload.get("usage") or {}
    err = payload.get("error") or {}
    return {
        "i": i, "status": status, "model": payload.get("model"),
        "input": u.get("input_tokens"), "cache_creation": u.get("cache_creation_input_tokens"),
        "cache_read": u.get("cache_read_input_tokens"), "output": u.get("output_tokens"),
        "transformations": transformations(payload), "payload_keys": sorted(payload),
        "content_types": [b.get("type") for b in payload.get("content") or []],
        "error_type": err.get("type"),
        "diagnostics": shape(payload.get("diagnostics")),
        "context_management": shape(payload.get("context_management")),
        "stop_details": shape(payload.get("stop_details")),
        "header_names": sorted(lower),
        "header_hashes": {k: keyed(v) for k, v in sorted(lower.items()) if k not in SKIP_HASH},
        **extra,
    }


OPTS = {"binding": "none", "user_id": False}


def series_a(base, key, out, cap):
    questions = [f"What is {a} times {b}? Think it through, then answer with the number only."
                 for a, b in [(17, 23), (34, 19), (56, 27), (41, 38), (63, 29),
                              (72, 44), (85, 31), (93, 47), (28, 67), (59, 76)]]
    if OPTS.get("hard"):  # larger products so stronger models still produce thinking blocks
        questions = [f"What is {a} times {b}? Work it out carefully, then answer with the number only."
                     for a, b in [(4831, 2973), (7719, 6058), (3947, 8216), (9182, 4475), (6653, 7391)]]
    questions = questions[:OPTS.get("n", 10)]
    messages, spent = [], 0
    thinking = {"type": "enabled", "budget_tokens": 1024}
    if OPTS["binding"] != "none":
        thinking["block_binding"] = {"prefix_mismatch_behavior": OPTS["binding"]}
    for i, q in enumerate(questions, 1):
        messages.append({"role": "user", "content": q})
        body = {"model": MODEL, "max_tokens": 1500, "thinking": thinking, "messages": messages}
        if OPTS["user_id"]:
            body["metadata"] = {"user_id": "contexto-l1e-synthetic-user"}
        status, headers, payload = call(base, key, body)
        if i == 1 and status == 400 and thinking["type"] == "enabled":
            thinking = {"type": "adaptive", **({"block_binding": thinking["block_binding"]}
                                                if "block_binding" in thinking else {})}
            body["thinking"] = thinking
            status, headers, payload = call(base, key, body)
        replayed = sum(1 for m in messages if m["role"] == "assistant"
                       for b in m["content"] if b.get("type") in ("thinking", "redacted_thinking"))
        rec = record(i, status, headers, payload, {"series": "A", "thinking_mode": thinking["type"],
                                                   "binding": OPTS["binding"], "user_id_set": OPTS["user_id"],
                                                   "replayed_thinking_blocks": replayed,
                                                   "turns_in_request": len(messages)})
        out.write(json.dumps(rec) + "\n"); out.flush()
        if status != 200:
            print(f"A{i}: status {status} {rec['error_type']}; stopping series"); return
        messages.append({"role": "assistant", "content": payload["content"]})  # replay verbatim
        spent += sum(x or 0 for x in (rec["input"], rec["cache_creation"], rec["cache_read"], rec["output"]))
        print(f"A{i}: status 200 spent={spent}")
        if spent > cap:
            print("cap reached; stopping"); return
        time.sleep(5)


def series_b(base, key, out, cap):
    prefix = "\n".join(f"Reference line {i:04d}: synthetic cache test text alpha beta gamma delta."
                       for i in range(1, OPTS.get("lines", 250) + 1))  # above the 512-token minimum
    body = {"model": MODEL, "max_tokens": 16,
            "system": [{"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": "Reply with only OK."}]}
    spent = 0
    for i in range(1, 11):
        status, headers, payload = call(base, key, body)
        rec = record(i, status, headers, payload, {"series": "B", "model_requested": MODEL})
        out.write(json.dumps(rec) + "\n"); out.flush()
        if status != 200:
            print(f"B{i}: status {status} {rec['error_type']}; stopping series"); return
        spent += sum(x or 0 for x in (rec["input"], rec["cache_creation"], rec["cache_read"], rec["output"]))
        print(f"B{i}: status 200 spent={spent}")
        if spent > cap:
            print("cap reached; stopping"); return
        time.sleep(5)


def main():
    global MODEL
    series, path = sys.argv[1], sys.argv[2]
    argv = sys.argv
    cap = int(argv[argv.index("--cap") + 1]) if "--cap" in argv else 45000
    if "--model" in argv:
        MODEL = argv[argv.index("--model") + 1]
    if "--binding" in argv:
        OPTS["binding"] = argv[argv.index("--binding") + 1]
        if OPTS["binding"] != "none":
            BETAS.append("thinking-binding-controls-2026-08-01")
    OPTS["user_id"] = "--user-id" in argv
    OPTS["hard"] = "--hard" in argv
    if "--n" in argv:
        OPTS["n"] = int(argv[argv.index("--n") + 1])
    if "--lines" in argv:
        OPTS["lines"] = int(argv[argv.index("--lines") + 1])
    base = os.environ.get("ANTHROPIC_BASE_URL")
    if not base:
        sys.exit("no inherited ANTHROPIC_BASE_URL")
    key = credential()
    with open(path, "a") as out:
        (series_a if series == "A" else series_b)(base, key, out, cap)


if __name__ == "__main__":
    main()
