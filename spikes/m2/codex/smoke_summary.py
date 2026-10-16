"""Per-request numbers from a Contexto session log (M2 A6 Codex smoke): status, real usage, the engine's
request and rotation events. Numbers and event names only. Usage: smoke_summary.py <log.jsonl>"""
import json, sys
path = sys.argv[1]
rows = [json.loads(l) for l in open(path) if l.strip()]
total = 0
for r in rows:
    ev = r.get("event")
    if ev != "request":
        if ev not in ("proxy_start", "proxy_stop"):
            print("EVENT", {k: v for k, v in r.items() if k not in ("ts",)})
        continue
    u = r.get("usage") or {}
    inp, out = u.get("input_tokens") or 0, u.get("output_tokens") or 0
    cached = (u.get("input_tokens_details") or {}).get("cached_tokens")
    total += inp + out
    tev = [e for e in r.get("transform_events") or []]
    names = [e.get("event") for e in tev]
    req = next((e for e in tev if e.get("event") == "request"), {})
    print(r.get("method"), r.get("path"), r.get("status"), "in", inp, "cached", cached, "out", out,
          "reasoning", (u.get("output_tokens_details") or {}).get("reasoning_tokens"),
          "| msgs", req.get("messages"), "->", req.get("upstream_messages"), "est", req.get("estimate"),
          "sent_est", req.get("sent_estimate"), "epoch", req.get("epoch"), "k", req.get("k"),
          "| events", [n for n in names if n != "request"], "| cum", total)
