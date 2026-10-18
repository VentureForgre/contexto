#!/usr/bin/env python3
"""Loopback scripted fake model for the reminder-placement spike. Zero model spend.

Answers POST /v1/messages (SSE or JSON) and /v1/messages/count_tokens on 127.0.0.1, saves every
request body to <bodies>/NNNNN.json (bodies stay local, under a temp or gitignored dir), and logs
one JSON line per request (no header values, no auth material).

The script lives in the prompt. The current turn's prompt (the newest user message with non-
reminder text and no tool_result) may contain ``STEP[a1|a2|...]``. Each action becomes one
tool_use, in order; the model counts the tool_use blocks it already sent after that prompt to know
which comes next, then ends the turn with text. An action whose tool is not in the request's tool
list is skipped, the way a model only calls tools it is offered. Actions:

  todo=ip,p,c        TodoWrite with one item per status (p pending, ip in_progress, c completed)
  taskcreate=<n>     TaskCreate "Synthetic task <n>"
  taskupdate=<id>:<status>  TaskUpdate (pending, in_progress, completed)
  tasklist           TaskList
  write=<rel>        Write <project>/<rel> (content ends with the line END_OF_FILE)
  edit=<rel>         Edit <project>/<rel>: replace END_OF_FILE with a new line plus END_OF_FILE
  read=<rel>         Read <project>/<rel>
  bash=<word>        Bash with a harmless fixed command (ls, pwd, date)
  enterplan          EnterPlanMode
  exitplan           ExitPlanMode with a short plan
  tool=<name>        a tool by exact name with empty input (for MCP tools)

Requests without tools (side queries) and prompts without STEP get the text "ok".

Usage: scripted_model.py <port> <log.jsonl> <bodies_dir> <project_dir>
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, LOG, BODIES, PROJECT = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
os.makedirs(BODIES, exist_ok=True)
LOCK = threading.Lock()
N = {"n": 0}
TAG = "<system-reminder>"
STATUS = {"p": "pending", "ip": "in_progress", "c": "completed"}
BASH = {"ls": "ls", "pwd": "pwd", "date": "date +%Y"}


def blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in (content or []) if isinstance(b, dict)]


def is_prompt(m):
    if m.get("role") != "user":
        return False
    bs = blocks(m.get("content"))
    if any(b.get("type") == "tool_result" for b in bs):
        return False
    return any(b.get("type") == "text" and not b.get("text", "").lstrip().startswith(TAG) for b in bs)


def current_turn(messages):
    for i in range(len(messages) - 1, -1, -1):
        if is_prompt(messages[i]):
            return i
    return None


def actions_of(text):
    a = text.find("STEP[")
    if a < 0:
        return []
    b = text.find("]", a)
    return [x for x in text[a + 5:b].split("|") if x]


def tool_use_for(action, n):
    name, _, arg = action.partition("=")
    p = os.path.join(PROJECT, arg) if arg else ""
    if name == "todo":
        sts = [STATUS[s] for s in arg.split(",")]
        todos = [{"content": f"Synthetic task {i + 1}", "status": s, "activeForm": f"Doing synthetic task {i + 1}"}
                 for i, s in enumerate(sts)]
        return "TodoWrite", {"todos": todos}
    if name == "taskcreate":
        return "TaskCreate", {"subject": f"Synthetic task {arg}", "description": f"Synthetic task number {arg}",
                              "activeForm": f"Doing synthetic task {arg}"}
    if name == "taskupdate":
        tid, _, status = arg.partition(":")
        return "TaskUpdate", {"taskId": tid, "status": status}
    if name == "tasklist":
        return "TaskList", {}
    if name == "write":
        return "Write", {"file_path": p, "content": f"written by model at request {n}\nEND_OF_FILE\n"}
    if name == "edit":
        return "Edit", {"file_path": p, "old_string": "END_OF_FILE",
                        "new_string": f"model edit at request {n}\nEND_OF_FILE"}
    if name == "read":
        return "Read", {"file_path": p}
    if name == "bash":
        return "Bash", {"command": BASH.get(arg, "pwd"), "description": "Synthetic harmless command"}
    if name == "enterplan":
        return "EnterPlanMode", {}
    if name == "exitplan":
        return "ExitPlanMode", {"plan": "Synthetic plan: edit notes.md, then read data.txt."}
    if name == "tool":
        return arg, {}
    return "Read", {"file_path": os.path.join(PROJECT, "data.txt")}


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _read(self):
        if self.headers.get("transfer-encoding", "").lower() == "chunked":
            out = bytearray()
            while True:
                size = int(self.rfile.readline().strip().split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    break
                out += self.rfile.read(size)
                self.rfile.readline()
            return bytes(out)
        n = int(self.headers.get("content-length") or 0)
        return self.rfile.read(n) if n else b""

    def _send(self, code, ctype, data):
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass

    def _log(self, rec):
        with LOCK, open(LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")

    def do_CONNECT(self):
        self._log({"t": time.time(), "method": "CONNECT", "refused": True})
        self._send(403, "text/plain", b"blocked")
        self.close_connection = True

    def do_GET(self):
        self._log({"t": time.time(), "method": "GET", "path": self.path.split("?")[0]})
        self._send(200, "application/json", b"{}")

    def do_HEAD(self):
        self._send(200, "application/json", b"")

    def do_POST(self):
        raw = self._read()
        path = self.path.split("?")[0]
        if path.startswith("http"):  # absolute-form request via the proxy setting: refuse
            self._log({"t": time.time(), "method": "POST", "refused": True})
            self._send(403, "text/plain", b"blocked")
            return
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        with LOCK:
            N["n"] += 1
            n = N["n"]
        with open(os.path.join(BODIES, f"{n:05d}.json"), "wb") as f:
            f.write(raw)
        msgs = body.get("messages") or []
        tools = [t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)]
        rec = {"n": n, "t": time.time(), "path": path, "bytes": len(raw), "messages": len(msgs),
               "tools": len(tools), "stream": bool(body.get("stream"))}
        if path.endswith("/count_tokens"):
            self._log(rec)
            self._send(200, "application/json", json.dumps({"input_tokens": int(len(raw) / 1.83)}).encode())
            return
        reply, inp = None, None
        if tools:
            i = current_turn(msgs)
            if i is not None:
                acts = [x for x in actions_of(" ".join(b.get("text", "") for b in blocks(msgs[i].get("content"))
                                                       if b.get("type") == "text"))
                        if tool_use_for(x, 0)[0] in tools]
                done = sum(1 for m in msgs[i + 1:] if m.get("role") == "assistant"
                           for b in blocks(m.get("content")) if b.get("type") == "tool_use")
                if done < len(acts):
                    reply, inp = tool_use_for(acts[done], n)
                    rec["action"] = acts[done]
        rec["reply"] = reply or "text"
        self._log(rec)
        usage = {"input_tokens": int(len(raw) / 1.83), "output_tokens": 5,
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        mid = f"msg_fake{n:06d}"
        if reply:
            block = {"type": "tool_use", "id": f"toolu_fake{n:06d}", "name": reply, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(inp)}
            stop, full = "tool_use", {**block, "input": inp}
        else:
            block = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": "ok"}
            stop, full = "end_turn", {"type": "text", "text": "ok"}
        if not body.get("stream"):
            msg = {"id": mid, "type": "message", "role": "assistant", "model": body.get("model"),
                   "content": [full], "stop_reason": stop, "stop_sequence": None, "usage": usage}
            self._send(200, "application/json", json.dumps(msg).encode())
            return
        events = [
            {"type": "message_start", "message": {"id": mid, "type": "message", "role": "assistant",
                                                  "model": body.get("model"), "content": [], "stop_reason": None,
                                                  "stop_sequence": None, "usage": usage}},
            {"type": "content_block_start", "index": 0, "content_block": block},
            {"type": "content_block_delta", "index": 0, "delta": delta},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
             "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        ]
        self._send(200, "text/event-stream", sse(events))


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
