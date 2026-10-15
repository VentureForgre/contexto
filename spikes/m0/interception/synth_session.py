#!/usr/bin/env python3
"""Write a synthesized Claude Code session file (one user turn, one assistant turn).
Usage: synth_session.py <projects_dir> <cwd> <input_tokens> -> prints session id"""
import json, sys, uuid
from datetime import UTC, datetime
projects, cwd, input_tokens = sys.argv[1], sys.argv[2], int(sys.argv[3])
sid = str(uuid.uuid4())
enc = "".join(c if c.isalnum() else "-" for c in cwd)
now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
common = {"cwd": cwd, "sessionId": sid, "isSidechain": False, "userType": "external",
          "version": "2.1.301", "gitBranch": "", "timestamp": now}
u1 = str(uuid.uuid4()); a1 = str(uuid.uuid4())
recs = [
    {**common, "type": "user", "uuid": u1, "parentUuid": None,
     "message": {"role": "user", "content": "Remember the code word MARKER-H3-USER."}},
    {**common, "type": "assistant", "uuid": a1, "parentUuid": u1,
     "message": {"id": "msg_synth01", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                 "content": [{"type": "text", "text": "Noted: MARKER-H3-ASSIST."}],
                 "stop_reason": "end_turn", "stop_sequence": None,
                 "usage": {"input_tokens": input_tokens, "output_tokens": 12,
                           "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}},
]
import os
d = os.path.join(projects, enc); os.makedirs(d, exist_ok=True)
with open(os.path.join(d, sid + ".jsonl"), "w") as f:
    for r in recs: f.write(json.dumps(r) + "\n")
print(sid)
