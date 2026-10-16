#!/usr/bin/env python3
"""A2 live check (intake-approved, 20,000-token cap, claude-haiku-5-5, synthetic content).

Reuses the L1-e probe's request path: the credential comes from the seat's apiKeyHelper at
runtime and stays in memory; the base URL is the inherited ANTHROPIC_BASE_URL; neither is printed,
stored, or logged. Records status, error type, usage numbers, and input_transformations shapes.

Checks:
  P-a  messages start with a carried role "system" message before the first user turn-start.
  P-b  the same system text merged into that user message as a leading text block.
  P-c  the system message placed right after that user message (Claude Code's own shape).
  R12  thinking enabled, binding beta with prefix_mismatch_behavior=error: turn 1 with a non-empty
       system message after the user prompt (Claude Code's shape), then turn 2 replaying turn 1's
       signed thinking with that system message emptied in place.
Each case is sent once, with only the feature betas it tests (R12: the thinking binding beta) and an
honest User-Agent naming this harness (intake rule D23: never another product's client marker). The
recorded A2 run (README finding 2) retried P-a once with Claude Code 2.1.301's beta list; that retry
was removed under D23.

Usage: live_check.py <out.jsonl> [--cap 20000] [--r12]
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# The L1-e probe (spikes/m0/route-identity/probe.py, PR #21); M2_PROBE_PATH overrides the location.
spec = importlib.util.spec_from_file_location("probe", os.environ.get(
    "M2_PROBE_PATH", os.path.join(HERE, "..", "..", "m0", "route-identity", "probe.py")))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

MODEL = "claude-haiku-5-5"
probe.UA = "contexto-test/live_check"  # D23: names this harness; probe.call reads it per request
SYSTEM_TEXT = "Synthetic carried system note: answer briefly."
spent = 0


def send(base, key, out, label, body, betas):
    global spent
    probe.BETAS[:] = betas
    status, headers, payload = probe.call(base, key, body)
    rec = probe.record(0, status, headers, payload, {"case": label, "betas": ",".join(betas) or "none"})
    u = payload.get("usage") or {}
    spent += sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
    rec["spent_total"] = spent
    out.write(json.dumps(rec) + "\n"); out.flush()
    print(f"{label} betas={rec['betas']}: status {status} {rec['error_type'] or ''} spent={spent}")
    return status, payload


def case(base, key, out, cap, label, body, feature_betas=()):
    """Send one case once; ``feature_betas`` are only the betas the case itself tests."""
    if spent > cap - 2000:
        print(f"{label}: skipped, cap"); return None, {}
    return send(base, key, out, label, body, list(feature_betas))


def main():
    path = sys.argv[1]
    cap = int(sys.argv[sys.argv.index("--cap") + 1]) if "--cap" in sys.argv else 20000
    base = os.environ.get("ANTHROPIC_BASE_URL") or sys.exit("no inherited ANTHROPIC_BASE_URL")
    key = probe.credential()
    with open(path, "a") as out:
        prompt = {"type": "text", "text": "Synthetic prompt: reply with the word ok."}
        case(base, key, out, cap, "P-a", {"model": MODEL, "max_tokens": 32, "messages": [
            {"role": "system", "content": SYSTEM_TEXT},
            {"role": "user", "content": [prompt]}]})
        case(base, key, out, cap, "P-b", {"model": MODEL, "max_tokens": 32, "messages": [
            {"role": "user", "content": [{"type": "text", "text": SYSTEM_TEXT}, prompt]}]})
        case(base, key, out, cap, "P-c", {"model": MODEL, "max_tokens": 32, "messages": [
            {"role": "user", "content": [prompt]},
            {"role": "system", "content": SYSTEM_TEXT}]})
        if "--r12" not in sys.argv:
            return  # rule 12 check is opt-in: zero-spend evidence showed no client-side emptying
        binding = ["thinking-binding-controls-2026-08-01"]  # the feature R12 tests
        thinking = {"type": "enabled", "budget_tokens": 1024,
                    "block_binding": {"prefix_mismatch_behavior": "error"}}
        q1 = {"role": "user", "content": "What is 37 times 43? Think it through, then answer with the number only."}
        t1_body = {"model": MODEL, "max_tokens": 1500, "thinking": thinking,
                   "messages": [q1, {"role": "system", "content": SYSTEM_TEXT}]}
        status, payload = case(base, key, out, cap, "R12-turn1", t1_body, binding)
        if status != 200:
            print("R12: turn 1 failed; stopping"); return
        types = [b.get("type") for b in payload.get("content") or []]
        if "thinking" not in types:
            print(f"R12: turn 1 produced no thinking block ({types}); stopping"); return
        q2 = {"role": "user", "content": "What is 52 times 61? Answer with the number only."}
        assistant = {"role": "assistant", "content": payload["content"]}
        for label, sys_msg in (("R12-turn2-system-kept", {"role": "system", "content": SYSTEM_TEXT}),
                               ("R12-turn2-system-emptied", {"role": "system", "content": []})):
            case(base, key, out, cap, label, {"model": MODEL, "max_tokens": 1500, "thinking": thinking,
                                              "messages": [q1, sys_msg, assistant, q2]}, binding)


if __name__ == "__main__":
    main()
