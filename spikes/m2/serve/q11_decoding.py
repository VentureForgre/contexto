"""D70 review round 4: which settings files Claude Code 2.1.322 loads, and what Contexto does with them.

    python3 -I q11_decoding.py <out dir outside any checkout> claude|launcher [case ...]

Zero spend and fully local. Each case writes one settings file (the config directory's
``settings.json``, or the working directory's ``.claude/settings.json``) and runs one ``claude -p``
under the sandbox profile of ``http_probe.py`` (localhost only), with a fresh HOME, CLAUDE_CONFIG_DIR,
CONTEXTO_HOME and working directory under <out dir>, the key ``stub-key`` and fake AWS values.
Three loopback stubs: ``env`` (ANTHROPIC_BASE_URL in the process environment, and the launcher's
``--upstream``), ``file`` (a base URL only the settings file names) and ``bedrock``
(ANTHROPIC_BEDROCK_BASE_URL). ``claude`` runs the real Claude Code alone; ``launcher`` runs
``python -m contexto run --upstream <env stub> claude -p`` with the interpreter in $CONTEXTO_PYTHON
(default: the checkout's .venv), and ``claude`` is $CLAUDE (default: ~/.local/bin/claude). Prints one JSON line per case: requests per stub, requests carrying
x-claude-code-request-class, the exit code, whether CONTEXTO_HOME was created, the launch's hint state.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
import http_probe as hp  # noqa: E402  (the stubs and the sandbox profile)

B, H, U = "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_GATEWAY_HINT_HEADERS", "ANTHROPIC_BASE_URL"


def w16(text: str, odd: bool = False) -> bytes:
    """UTF-16LE with its byte order mark; lone surrogates written as they are; ``odd`` adds one byte."""
    return b"\xff\xfe" + text.encode("utf-16-le", "surrogatepass") + (b" " if odd else b"")


def doc(name: str, value: str, tail: str = "") -> str:
    """A settings file whose env block sets ``name`` to the string ``value``, with ``tail`` (more members)."""
    return '{"env": {"' + name + '": "' + value + '"}' + tail + "}"


def bad_utf8(text: str, raw: bytes) -> bytes:
    """``text`` in UTF-8 with each U+0001 replaced by ``raw`` (bytes that are not UTF-8)."""
    return text.encode().replace(b"\x01", raw)


# name -> (the file's bytes, given the file stub's URL; extra process environment; user or project file)
CASES = {
    "u16-hi-on": (lambda f: w16(doc(B, "1", ', "note": "a\ud800b"')), {}, "user"),
    "u16-lo-on": (lambda f: w16(doc(B, "1", ', "note": "\udc00"')), {}, "user"),
    "u16-odd-on": (lambda f: w16(doc(B, "1"), odd=True), {}, "user"),
    "u16-both-on-project": (lambda f: w16(doc(B, "1", ', "note": "\ud83d"'), odd=True), {}, "project"),
    "u16-both-off": (lambda f: w16(doc(B, "0", ', "note": "\ud83d"'), odd=True), {}, "user"),
    "u16-both-off-env1": (lambda f: w16(doc(B, "0", ', "note": "\ud83d"'), odd=True), {B: "1"}, "user"),
    "u16-both-base": (lambda f: w16(doc(U, f, ', "note": "\ud800"'), odd=True), {}, "user"),
    "u16-both-hint": (lambda f: w16(doc(H, "1", ', "note": "\ud800"'), odd=True), {}, "user"),
    "u16-comment-base": (lambda f: w16(doc(U, f) + " // c"), {}, "user"),
    "nan-base": (lambda f: doc(U, f, ', "x": NaN').encode(), {}, "user"),
    "inf-base": (lambda f: doc(U, f, ', "x": Infinity').encode(), {}, "user"),
    "neginf-base": (lambda f: doc(U, f, ', "x": [-Infinity]').encode(), {}, "user"),
    "nan-base-project": (lambda f: doc(U, f, ', "x": NaN').encode(), {}, "project"),
    "nan-on": (lambda f: doc(B, "1", ', "x": NaN').encode(), {}, "user"),
    "nan-off-env1": (lambda f: doc(B, "0", ', "x": NaN').encode(), {B: "1"}, "user"),
    "nan-hint": (lambda f: doc(H, "1", ', "x": NaN').encode(), {}, "user"),
    "nanstr-base": (lambda f: doc(U, f, ', "x": "NaN"').encode(), {}, "user"),
    "control-base": (lambda f: doc(U, f).encode(), {}, "user"),
    "latin1-on": (lambda f: bad_utf8(doc(B, "1", ', "x": "caf\x01"'), b"\xe9"), {}, "user"),
    "latin1-off-env1": (lambda f: bad_utf8(doc(B, "0", ', "x": "caf\x01"'), b"\xe9"), {B: "1"}, "user"),
    "overlong-on": (lambda f: bad_utf8(doc(B, "\x01"), b"\xc0\xb1"), {}, "user"),
    "latin1-base": (lambda f: bad_utf8(doc(U, f, ', "x": "caf\x01"'), b"\xe9"), {}, "user"),
    "u16-keysur-on": (lambda f: w16(doc(B, "1", ', "\ud800": 1')), {}, "user"),
    "esc-surrogate-base": (lambda f: doc(U, f + "/a\\ud800").encode(), {}, "user"),
    "u16-surrogate-base": (lambda f: w16(doc(U, f + "/a\ud800")), {}, "user"),
}


def run_case(out: Path, mode: str, name: str) -> dict:
    build, extra, where = CASES[name]
    run_dir = out / mode / name
    run_dir.mkdir(parents=True, exist_ok=True)
    recorder = hp.Recorder(run_dir)
    stubs = {stub: hp.start_stub(recorder, stub, "sse") for stub in ("env", "file", "bedrock")}
    port = {stub: server.server_port for stub, server in stubs.items()}
    scratch = Path(tempfile.mkdtemp(prefix=f"q11-{name}-", dir=out))
    home, cfg, work, chome = scratch / "home", scratch / "config", scratch / "work", scratch / "contexto-home"
    for d in (home, cfg, work):
        d.mkdir()
    target = cfg / "settings.json" if where == "user" else work / ".claude" / "settings.json"
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(build(f"http://127.0.0.1:{port['file']}/file"))
    claude = os.environ.get("CLAUDE") or str(Path.home() / ".local/bin/claude")
    claude_dir = str(Path(claude).parent)
    env = {"HOME": str(home), "PATH": f"{claude_dir}:/usr/bin:/bin", "TERM": "xterm-256color",
           "CLAUDE_CONFIG_DIR": str(cfg), "CONTEXTO_HOME": str(chome), "ANTHROPIC_API_KEY": hp.KEY,
           "DISABLE_AUTOUPDATER": "1", U: f"http://127.0.0.1:{port['env']}/env",
           "AWS_ACCESS_KEY_ID": "fake", "AWS_SECRET_ACCESS_KEY": "fake", "AWS_REGION": "us-east-1",
           "ANTHROPIC_BEDROCK_BASE_URL": f"http://127.0.0.1:{port['bedrock']}/bedrock", **extra}
    prompt = ["-p", "--output-format", "json", "say hi"]
    if mode == "claude":
        argv = [claude, *prompt]
    else:
        python = os.environ.get("CONTEXTO_PYTHON", str(REPO / ".venv/bin/python"))
        argv = [python, "-m", "contexto", "run", "--upstream", f"http://127.0.0.1:{port['env']}/env", "claude", *prompt]
    profile = out / "localonly.sb"
    profile.write_text(hp.SANDBOX_PROFILE)
    proc = subprocess.run(["/usr/bin/sandbox-exec", "-f", str(profile), *argv], cwd=work, env=env,
                          stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    time.sleep(1.0)
    for server in stubs.values():
        server.shutdown()
    hint_state = None
    for log in chome.glob("**/*.jsonl") if chome.exists() else []:
        for line in log.read_text(errors="replace").splitlines():
            event = json.loads(line) if line.startswith("{") else {}
            if event.get("event") == "launch":
                hint_state = event.get("hint_headers")
    stderr = proc.stderr.decode(errors="replace").splitlines()
    result = {"case": name, "where": where, "exit": proc.returncode,
              **{stub: sum(1 for r in recorder.rows if r["stub"] == stub) for stub in stubs},
              "class_header": sum(1 for r in recorder.rows if r["x-claude-code-request-class"] != "-"),
              "contexto_home": chome.exists(), "hint_state": hint_state,
              "stderr": next((line for line in stderr if line.startswith("contexto")), "")[:300]}
    (run_dir / "result.json").write_text(json.dumps(result, indent=1))
    return result


def main() -> None:
    out, mode, names = Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3:] or list(CASES)
    if out.is_relative_to(REPO) or mode not in ("claude", "launcher"):
        sys.exit("usage: q11_decoding.py <out dir outside the checkout> claude|launcher [case ...]")
    out.mkdir(parents=True, exist_ok=True)
    hp.OUT = out
    with ThreadPoolExecutor(4) as pool:
        for result in pool.map(lambda name: run_case(out, mode, name), names):
            print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
