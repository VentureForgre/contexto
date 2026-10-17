"""``contexto run claude``: run Claude Code with every model request going through the Contexto proxy.

Steps (M2 contract, Architecture):

1. Resolve the upstream Claude Code would otherwise use (``contexto.upstream``), before anything
   starts.
2. Open session S in ``contexto.paths.contexto_home()`` and start ``ContextoProxy`` on
   ``127.0.0.1:<free port>`` with the rewrite engine as its transform.
3. Write two temporary files, mode 0600, in a private temporary directory (never in the user's
   Claude configuration): a ``--settings`` file whose only content is
   ``env.ANTHROPIC_BASE_URL = <proxy URL>``, and an ``--mcp-config`` file that makes Claude Code spawn
   ``contexto mcp --session S`` over stdio with the current interpreter.
4. Run ``claude --settings <file> --mcp-config <file> [user args]`` as a child with the terminal
   passed through, wait for it, stop the proxy, delete the temporary files, and return claude's
   exit code.

The user's own settings, hooks, and MCP servers still load: a flag-level ``--settings`` only adds
the base URL, and it outranks the user, project, and local settings files. Limit: organization-
managed settings outrank ``--settings``; if they set ``ANTHROPIC_BASE_URL``, Claude Code bypasses
the proxy and Contexto cannot keep the prompt bounded. ``CLAUDE_CONFIG_DIR`` is never set and no
user configuration file is edited.

Signals: Ctrl+C in a terminal reaches every process in the foreground group, so Claude Code gets
it directly; the launcher ignores SIGINT while claude runs so that an interrupt meant for claude
does not tear down the proxy under it. SIGTERM and SIGHUP sent to the launcher are forwarded to
claude. Terminal resize (SIGWINCH) goes from the kernel to claude without the launcher's help.

Window budget (contract rule 4): the engine keeps ``W = bound - headroom - F`` tokens for the
working window, where ``F`` is the fixed overhead (system, tools, carried content, pinned text, and
page index). ``F`` is only measured after the first upstream response, so at start the launcher
warns when an assumed ``F`` leaves less than 25,000 tokens: 92,000 inside an OpenRig seat (M0
measured seat heads of 82k to 84k before pins) and 55,000 otherwise (a throwaway head near 46k
plus pins and index). Once the engine measures ``F``, a ``bound_too_small`` event or a window
budget under 25,000 is shown on stderr once. stderr stays quiet otherwise.

Exit codes: claude's own code; 128 + N if claude died from signal N; 2 for a usage error such as
a bound above the guard; 1 if the proxy cannot start; 127 if ``claude`` is not on PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from contexto.paths import contexto_home
from contexto.proxy import ContextoProxy, identity_transform
from contexto.store import SessionStore
from contexto.upstream import resolve_upstream

DEFAULT_BOUND = 150_000
MIN_WINDOW_BUDGET = 25_000
DEFAULT_HEADROOM_FLOOR = 25_000
ASSUMED_OVERHEAD_OPENRIG = 92_000
ASSUMED_OVERHEAD_DEFAULT = 55_000
BOUND_GUARD = 170_000  # below Claude Code's idle compaction (200,000) with room for an over-bound turn
EXIT_USAGE = 2
EXIT_PROXY = 1
EXIT_NOT_FOUND = 127
FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


class LaunchError(Exception):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class LaunchOptions:
    claude_args: list[str] = field(default_factory=list)
    bound: int = DEFAULT_BOUND
    session_id: str | None = None
    upstream: str | None = None
    observe_thinking: bool = False
    allow_large_bound: bool = False
    window_fraction: float | None = None
    headroom_floor: int | None = None
    claude_bin: str = "claude"


def check_user_args(claude_args: list[str]) -> None:
    """Fail closed on a user ``--settings``: Claude Code 2.1.301 takes a single ``--settings``, so a
    second one could replace ours and silently send requests around the proxy. ``--mcp-config`` is
    variadic and allowed. Arguments after ``--`` are positional for claude and not checked."""
    for arg in claude_args:
        if arg == "--":
            return
        if arg == "--settings" or arg.startswith("--settings="):
            raise LaunchError(
                "--settings in the claude arguments is not supported: Claude Code accepts one --settings, "
                "and contexto uses it to route requests through its proxy, so a second one could bypass "
                "the proxy. Put those settings in your Claude settings files instead "
                "(see README, 'contexto run').", EXIT_USAGE)


def check_bound(bound: int, allow_large_bound: bool) -> None:
    if bound <= 0:
        raise LaunchError(f"--bound must be positive, got {bound}", EXIT_USAGE)
    if bound > BOUND_GUARD and not allow_large_bound:
        raise LaunchError(
            f"--bound {bound} is above {BOUND_GUARD:,}: Claude Code may compact on its own (idle compaction "
            f"starts at 200,000 by default). Pass --allow-large-bound to override.", EXIT_USAGE)


def engine_config(options: LaunchOptions) -> dict[str, int | float]:
    """Keyword arguments for ``contexto.rewrite.Config``; unset values keep the engine's defaults."""
    config: dict[str, int | float] = {"bound": options.bound}
    if options.window_fraction is not None:
        config["window_fraction"] = options.window_fraction
    if options.headroom_floor is not None:
        config["headroom_floor"] = options.headroom_floor
    return config


def assumed_overhead(env: dict[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    return ASSUMED_OVERHEAD_OPENRIG if env.get("OPENRIG_SESSION_NAME") else ASSUMED_OVERHEAD_DEFAULT


def start_warning(bound: int, headroom_floor: int | None, overhead: int) -> str | None:
    """A one-line warning when the assumed window budget is below MIN_WINDOW_BUDGET, else None."""
    headroom = headroom_floor if headroom_floor is not None else DEFAULT_HEADROOM_FLOOR
    budget = bound - headroom - overhead
    if budget >= MIN_WINDOW_BUDGET:
        return None
    suggested = headroom + overhead + 2 * MIN_WINDOW_BUDGET
    return (f"window budget about {budget:,} tokens (bound {bound:,} - headroom {headroom:,} - assumed fixed "
            f"overhead {overhead:,}) is below {MIN_WINDOW_BUDGET:,}; rotations will page often. "
            f"Consider --bound {suggested:,} or more.")


class NoticeLog:
    """Session log wrapper: everything goes to the store; the engine's first bound_too_small event
    and first window budget under MIN_WINDOW_BUDGET are also shown on stderr, once each."""

    def __init__(self, store: Any, stderr) -> None:
        self.store = store
        self.stderr = stderr
        self.shown: set[str] = set()

    def log(self, event: dict[str, Any]) -> None:
        self.store.log(event)
        for inner in event.get("transform_events") or []:
            if not isinstance(inner, dict):
                continue
            budget, overhead = inner.get("window_budget"), inner.get("fixed_overhead")
            if inner.get("event") == "bound_too_small":
                # Rule 4 window_minimal (contract PR #50): the engine still rotates, to a window of
                # only the new turn, which can still put the request over the bound.
                bound = inner.get("bound")
                self._once("bound_too_small", f"bound {bound:,} is too small: fixed overhead {overhead:,} leaves a "
                           f"window budget of {budget:,}; the engine keeps only the new turn, and requests may "
                           f"exceed the bound. Restart with a larger --bound."
                           if all(isinstance(v, int) for v in (bound, overhead, budget))
                           else "bound is too small for the measured fixed overhead; the engine keeps only the new "
                           "turn, and requests may exceed the bound.")
            elif isinstance(budget, int) and budget < MIN_WINDOW_BUDGET:
                self._once("small_window", f"measured window budget {budget:,} is below {MIN_WINDOW_BUDGET:,} "
                           f"(fixed overhead {overhead:,}); consider a larger --bound."
                           if isinstance(overhead, int) else f"measured window budget {budget:,} is small.")

    def _once(self, key: str, message: str) -> None:
        if key not in self.shown:
            self.shown.add(key)
            print(f"contexto: {message}", file=self.stderr, flush=True)


def open_session(home: Path, session_id: str, workdir: Path) -> Any:
    """Create the session and record the directory claude runs in, before claude starts (lane B
    checks file sources against it for staleness)."""
    store = SessionStore(home, session_id)
    store.set_workdir(workdir)
    return store


def build_engine(store: Any, config: dict[str, int | float]) -> tuple[Callable[..., Any], Callable[..., Any] | None, str]:
    """The A2 rewrite engine as (transform, observe_usage, name); identity when it is not installed."""
    try:
        from contexto.rewrite import Config, RewriteEngine
    except ImportError:
        return identity_transform, None, "identity"
    engine = RewriteEngine(store, config=Config(**config))
    return engine, getattr(engine, "observe_usage", None), "rewrite"


def write_private_json(directory: Path, name: str, payload: dict[str, Any]) -> Path:
    """Create ``directory/name`` with mode 0600 from the first byte, holding ``payload``."""
    path = directory / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return path


def settings_payload(proxy_url: str) -> dict[str, Any]:
    return {"env": {"ANTHROPIC_BASE_URL": proxy_url}}


def mcp_payload(session_id: str, home: Path) -> dict[str, Any]:
    return {"mcpServers": {"contexto": {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "contexto", "mcp", "--session", session_id],
        "env": {"CONTEXTO_HOME": str(home)},
    }}}


def exit_code(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def run_claude(options: LaunchOptions, *, stderr=None) -> int:
    stderr = stderr or sys.stderr
    try:
        return _run(options, stderr)
    except LaunchError as exc:
        print(f"contexto run: {exc}", file=stderr)
        return exc.code


def options_from_args(args: Any) -> LaunchOptions:
    """LaunchOptions from the ``contexto run`` argparse namespace (also used by contexto.codex)."""
    return LaunchOptions(
        claude_args=list(getattr(args, "cli_args", None) or []), bound=args.bound, session_id=args.session,
        upstream=args.upstream, observe_thinking=args.observe_thinking, allow_large_bound=args.allow_large_bound,
        window_fraction=args.window_fraction, headroom_floor=args.headroom_floor)


def check_options(options: LaunchOptions) -> None:
    check_bound(options.bound, options.allow_large_bound)
    if options.window_fraction is not None and not 0 < options.window_fraction < 1:
        raise LaunchError(f"--window-fraction must be between 0 and 1, got {options.window_fraction}", EXIT_USAGE)
    if options.headroom_floor is not None and not 0 < options.headroom_floor < options.bound:
        raise LaunchError(f"--headroom-floor must be between 0 and the bound, got {options.headroom_floor}",
                          EXIT_USAGE)


def start_proxy(store: Any, upstream_url: str, upstream_source: str, config: dict[str, int | float],
                observe_thinking: bool, *, stderr=None) -> tuple[ContextoProxy, str]:
    """Build the rewrite engine for ``store`` and start the proxy on 127.0.0.1 with a free port.
    Returns ``(proxy, engine_name)``; the caller stops the proxy. Shared by claude and codex."""
    transform, observe_usage, engine_name = build_engine(store, config)
    try:
        proxy = ContextoProxy(upstream_url, NoticeLog(store, stderr or sys.stderr), transform,
                              observe_usage=observe_usage, observe_thinking=observe_thinking,
                              upstream_source=upstream_source)
    except ValueError as exc:
        raise LaunchError(f"invalid upstream: {exc}", EXIT_USAGE) from exc
    try:
        proxy.start(0)
    except OSError as exc:
        raise LaunchError(f"cannot start the proxy: {exc}", EXIT_PROXY) from exc
    if engine_name == "identity":
        store.log({"event": "engine_unavailable", "detail": "contexto.rewrite not installed; identity transform"})
    return proxy, engine_name


def run_child(argv: list[str], env: dict[str, str] | None = None, *, stderr=None) -> int:
    """Run a CLI with the terminal passed through and return its exit code (128 + N if it died from
    signal N, 127 if it cannot be started). Ctrl+C reaches it through the terminal while the
    launcher ignores SIGINT; SIGTERM and SIGHUP are forwarded. Shared by claude and codex."""
    try:
        child = subprocess.Popen(argv, env=env)
    except (FileNotFoundError, PermissionError) as exc:
        print(f"contexto: cannot start {argv[0]!r}: {exc.strerror}", file=stderr or sys.stderr)
        return EXIT_NOT_FOUND
    previous = _install_signal_handlers(child)
    try:
        return exit_code(child.wait())
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()


def _run(options: LaunchOptions, stderr) -> int:
    check_user_args(options.claude_args)
    check_options(options)
    claude = shutil.which(options.claude_bin)
    if claude is None:
        raise LaunchError(f"'{options.claude_bin}' not found on PATH", EXIT_NOT_FOUND)
    upstream = resolve_upstream(options.upstream, cwd=Path.cwd())
    home = contexto_home()
    session_id = options.session_id or uuid.uuid4().hex
    try:
        store = open_session(home, session_id, Path.cwd())
    except (OSError, ValueError) as exc:
        raise LaunchError(f"cannot open session {session_id!r}: {exc}", EXIT_USAGE) from exc
    config = engine_config(options)
    proxy, engine_name = start_proxy(store, upstream.url, upstream.source, config, options.observe_thinking,
                                     stderr=stderr)
    tmp = Path(tempfile.mkdtemp(prefix="contexto-run-"))  # mode 0700, removed on exit
    code = EXIT_PROXY
    try:
        settings = write_private_json(tmp, "settings.json", settings_payload(proxy.url))
        mcp = write_private_json(tmp, "mcp.json", mcp_payload(session_id, home))
        store.log({"event": "launch", "cli": "claude", "session": session_id, "config": config,
                   "engine": engine_name, "upstream_source": upstream.source,
                   "observe_thinking": options.observe_thinking, "user_arg_count": len(options.claude_args)})
        print(f"contexto: session {session_id} (bound {options.bound:,}, upstream {upstream.source})",
              file=stderr, flush=True)
        warning = start_warning(options.bound, options.headroom_floor, assumed_overhead())
        if warning:
            print(f"contexto: {warning}", file=stderr, flush=True)
            store.log({"event": "window_budget_warning", "bound": options.bound,
                       "assumed_overhead": assumed_overhead()})
        code = run_child([claude, "--settings", str(settings), "--mcp-config", str(mcp), *options.claude_args],
                         stderr=stderr)
        return code
    finally:
        proxy.stop()
        shutil.rmtree(tmp, ignore_errors=True)
        store.log({"event": "exit", "code": code})


def _install_signal_handlers(child: subprocess.Popen) -> dict[int, Any]:
    """Keep the launcher alive through the user's Ctrl+C and forward termination to claude.
    Python handlers (not SIG_IGN) are used, so nothing is inherited by the child."""
    previous: dict[int, Any] = {}

    def ignore(signum, frame) -> None:
        pass

    def forward(signum, frame) -> None:
        if child.poll() is None:
            child.send_signal(signum)

    previous[signal.SIGINT] = signal.signal(signal.SIGINT, ignore)
    for signum in FORWARDED_SIGNALS:
        previous[signum] = signal.signal(signum, forward)
    return previous
