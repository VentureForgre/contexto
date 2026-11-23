"""``contexto daemon``: manage a background ``contexto serve`` service via launchd (macOS) or systemd (Linux).

Subcommands:
- ``install``: generate the user-level launchd plist (``~/Library/LaunchAgents/io.contexto.serve.plist``)
  or systemd user unit (``~/.config/systemd/user/contexto-serve.service``) and optionally start it.
- ``uninstall``: stop and remove the user-level unit file.
- ``start`` / ``stop``: start or stop the installed user service.
- ``status``: report whether the unit is installed and whether ``contexto serve`` is running.

All unit generation is pure and uses Python stdlib only (``plistlib``, ``shlex``); service manager
commands are invoked through an injectable runner for deterministic testing.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from contexto.commands.serve import _port
from contexto.launcher import BOUND_GUARD, DEFAULT_BOUND, LaunchError, LaunchOptions, check_options
from contexto.paths import contexto_home
from contexto.private import private_dir, write_atomic
from contexto.proxy import normalize_allowed_host
from contexto.serve import DEFAULT_PORT, ServeError, ask_health, check_upstream_url, read_token, token_path
from contexto.servecontract import SERVICE_HOST, running_state, service_running
from contexto.upstream import Upstream

LAUNCHD_LABEL = "io.contexto.serve"
LAUNCHD_FILENAME = f"{LAUNCHD_LABEL}.plist"
SYSTEMD_UNIT_NAME = "contexto-serve.service"

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class DaemonError(RuntimeError):
    """Daemon unit generation or service-manager command failed."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def _default_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(cmd), capture_output=True, text=True, check=False)


def _run_cmd(runner: CommandRunner, cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return runner(cmd)
    except OSError as exc:
        raise DaemonError(f"cannot run {cmd[0]}: {exc.strerror or type(exc).__name__}") from None


def _check_unit_str(value: str, label: str) -> str:
    if any(ch in value for ch in ("\n", "\r", "\x00")):
        raise DaemonError(f"{label} must not contain newline or NUL characters", code=2)
    return value


def detect_platform(choice: str = "auto", *, platform_name: str | None = None) -> str:
    """Resolve ``auto`` to ``darwin`` or ``linux``, raising ValueError on unsupported platforms."""
    if choice in ("darwin", "linux"):
        return choice
    raw = (platform_name or sys.platform).lower()
    if raw == "darwin":
        return "darwin"
    if raw.startswith("linux"):
        return "linux"
    raise ValueError(f"unsupported platform {raw!r}: daemon management supports macOS (launchd) and Linux (systemd)")


def unit_path(platform: str, *, home_dir: Path | None = None,
              env: Mapping[str, str] | None = None) -> Path:
    """Path of the user-level service unit for ``platform`` (``darwin`` or ``linux``)."""
    user_home = home_dir or Path.home()
    environ = os.environ if env is None else env
    if platform == "darwin":
        return user_home / "Library" / "LaunchAgents" / LAUNCHD_FILENAME
    if platform == "linux":
        xdg = environ.get("XDG_CONFIG_HOME")
        base = Path(xdg).expanduser() if xdg else (user_home / ".config")
        return base / "systemd" / "user" / SYSTEMD_UNIT_NAME
    raise ValueError(f"unsupported platform {platform!r}")


def serve_argv(*, python: str | None = None, port: int = DEFAULT_PORT,
               upstream: str | None = None, anthropic_upstream: str | None = None,
               openai_upstream: str | None = None, gemini_upstream: str | None = None,
               bound: int | None = None, allow_large_bound: bool = False,
               allow_hosts: Sequence[str] = (), allow_local_origin: bool = False) -> list[str]:
    """Argument vector for the background ``contexto serve`` process."""
    argv = [python or sys.executable, "-P", "-m", "contexto", "serve", "--port", str(port)]
    if upstream:
        argv += ["--upstream", upstream]
    if anthropic_upstream:
        argv += ["--anthropic-upstream", anthropic_upstream]
    if openai_upstream:
        argv += ["--openai-upstream", openai_upstream]
    if gemini_upstream:
        argv += ["--gemini-upstream", gemini_upstream]
    if bound is not None and bound != DEFAULT_BOUND:
        argv += ["--bound", str(bound)]
    if allow_large_bound:
        argv.append("--allow-large-bound")
    for host in allow_hosts:
        argv += ["--allow-host", host]
    if allow_local_origin:
        argv.append("--allow-local-origin")
    return argv


def launchd_plist(home: Path, argv: Sequence[str], *,
                  label: str = LAUNCHD_LABEL) -> bytes:
    """Render a macOS LaunchAgent XML plist for ``contexto serve``."""
    _check_unit_str(label, "launchd label")
    _check_unit_str(str(home), "CONTEXTO_HOME")
    checked_argv = [_check_unit_str(part, "argument") for part in argv]
    data: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": checked_argv,
        "EnvironmentVariables": {"CONTEXTO_HOME": str(home)},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(home / "daemon.out.log"),
        "StandardErrorPath": str(home / "daemon.err.log"),
        "ProcessType": "Background",
    }
    return plistlib.dumps(data, fmt=plistlib.FMT_XML, sort_keys=True)


def _systemd_exec_quote(arg: str) -> str:
    """Quote one ``ExecStart=`` argument for systemd unit syntax. Escapes ``$`` -> ``$$`` and
    ``%`` -> ``%%`` so systemd does not expand environment variables or specifiers."""
    _check_unit_str(arg, "ExecStart argument")
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    if not arg or escaped != arg or any(c.isspace() or c in "\"';" for c in arg):
        return f'"{escaped}"'
    return arg


def _systemd_env_quote(arg: str) -> str:
    """Quote one ``Environment=`` assignment for systemd unit syntax (``%`` -> ``%%``, keeping
    ``$`` literal per ``systemd.exec(5)``)."""
    _check_unit_str(arg, "Environment value")
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if not arg or escaped != arg or any(c.isspace() or c in "\"';" for c in arg):
        return f'"{escaped}"'
    return arg


def systemd_unit(home: Path, argv: Sequence[str]) -> str:
    """Render a Linux ``systemd --user`` unit file for ``contexto serve``."""
    exec_start = " ".join(_systemd_exec_quote(part) for part in argv)
    env_val = _systemd_env_quote(f"CONTEXTO_HOME={home}")
    return (
        "[Unit]\n"
        "Description=Contexto local proxy daemon\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        f"Environment={env_val}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _launchd_domain(uid: int | None = None) -> str:
    return f"gui/{os.getuid() if uid is None else uid}"


def _cmd_detail(res: subprocess.CompletedProcess[str]) -> str:
    return (res.stderr or res.stdout or f"exit {res.returncode}").strip()


def install_daemon(*, home: Path, platform: str, argv: Sequence[str],
                   home_dir: Path | None = None, env: Mapping[str, str] | None = None,
                   start_now: bool = True, runner: CommandRunner = _default_runner,
                   uid: int | None = None) -> tuple[Path, list[list[str]]]:
    """Write the daemon unit atomically (0600) and optionally load/start it.
    Returns ``(unit_file_path, commands_executed)``."""
    private_dir(home, narrow=False)
    target = unit_path(platform, home_dir=home_dir, env=env)
    target.parent.mkdir(parents=True, exist_ok=True)
    if platform == "darwin":
        content = launchd_plist(home, argv)
    else:
        content = systemd_unit(home, argv).encode("utf-8")
    write_atomic(target, content)
    ran: list[list[str]] = []
    if platform == "darwin":
        if start_now:
            domain = _launchd_domain(uid)
            # Bootout any previous copy first (ignore errors when not loaded).
            bootout = ["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"]
            ran.append(bootout)
            _run_cmd(runner, bootout)
            bootstrap = ["launchctl", "bootstrap", domain, str(target)]
            ran.append(bootstrap)
            res = _run_cmd(runner, bootstrap)
            if res.returncode != 0:
                load = ["launchctl", "load", "-w", str(target)]
                ran.append(load)
                res2 = _run_cmd(runner, load)
                if res2.returncode != 0:
                    raise DaemonError(f"wrote {target}, but launchctl failed to start it: {_cmd_detail(res2)}")
    else:
        reload_cmd = ["systemctl", "--user", "daemon-reload"]
        ran.append(reload_cmd)
        res = _run_cmd(runner, reload_cmd)
        if res.returncode != 0:
            raise DaemonError(f"wrote {target}, but systemctl daemon-reload failed: {_cmd_detail(res)}")
        if start_now:
            enable_cmd = ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME]
            ran.append(enable_cmd)
            res2 = _run_cmd(runner, enable_cmd)
            if res2.returncode != 0:
                raise DaemonError(f"wrote {target}, but systemctl enable --now failed: {_cmd_detail(res2)}")
    return target, ran


def uninstall_daemon(*, platform: str, home_dir: Path | None = None,
                     env: Mapping[str, str] | None = None,
                     runner: CommandRunner = _default_runner,
                     uid: int | None = None) -> tuple[Path, bool, list[list[str]]]:
    """Stop and remove the daemon unit file. Returns ``(path, removed, commands_executed)``."""
    target = unit_path(platform, home_dir=home_dir, env=env)
    ran: list[list[str]] = []
    if platform == "darwin":
        domain = _launchd_domain(uid)
        cmd = ["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"]
        ran.append(cmd)
        res = _run_cmd(runner, cmd)
        if res.returncode != 0 and target.exists():
            unload = ["launchctl", "unload", "-w", str(target)]
            ran.append(unload)
            _run_cmd(runner, unload)
    else:
        disable = ["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME]
        ran.append(disable)
        _run_cmd(runner, disable)
    removed = False
    if target.exists() or target.is_symlink():
        target.unlink()
        removed = True
    if platform == "linux":
        reload_cmd = ["systemctl", "--user", "daemon-reload"]
        ran.append(reload_cmd)
        _run_cmd(runner, reload_cmd)
    return target, removed, ran


def start_daemon(*, platform: str, home_dir: Path | None = None,
                 env: Mapping[str, str] | None = None,
                 runner: CommandRunner = _default_runner,
                 uid: int | None = None) -> tuple[int, str]:
    target = unit_path(platform, home_dir=home_dir, env=env)
    if not target.exists():
        return 1, f"unit file {target} is not installed; run `contexto daemon install` first"
    try:
        if platform == "darwin":
            domain = _launchd_domain(uid)
            res = _run_cmd(runner, ["launchctl", "kickstart", "-k", f"{domain}/{LAUNCHD_LABEL}"])
            if res.returncode != 0:
                res = _run_cmd(runner, ["launchctl", "bootstrap", domain, str(target)])
            if res.returncode != 0:
                res = _run_cmd(runner, ["launchctl", "load", "-w", str(target)])
        else:
            res = _run_cmd(runner, ["systemctl", "--user", "start", SYSTEMD_UNIT_NAME])
    except DaemonError as exc:
        return 1, str(exc)
    if res.returncode != 0:
        return 1, f"failed to start daemon: {_cmd_detail(res)}"
    return 0, f"started ({target})"


def stop_daemon(*, platform: str, home_dir: Path | None = None,
                env: Mapping[str, str] | None = None,
                runner: CommandRunner = _default_runner,
                uid: int | None = None) -> tuple[int, str]:
    target = unit_path(platform, home_dir=home_dir, env=env)
    try:
        if platform == "darwin":
            domain = _launchd_domain(uid)
            res = _run_cmd(runner, ["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"])
            if res.returncode != 0 and target.exists():
                res = _run_cmd(runner, ["launchctl", "unload", str(target)])
        else:
            res = _run_cmd(runner, ["systemctl", "--user", "stop", SYSTEMD_UNIT_NAME])
    except DaemonError as exc:
        return 1, str(exc)
    if res.returncode != 0:
        return 1, f"failed to stop daemon: {_cmd_detail(res)}"
    return 0, "stopped"


def daemon_status(*, home: Path, platform: str, home_dir: Path | None = None,
                  env: Mapping[str, str] | None = None) -> tuple[int, str]:
    target = unit_path(platform, home_dir=home_dir, env=env)
    installed = target.exists()
    running = service_running(home)
    state = running_state(home) if running else None
    health = None
    if state is not None and os.path.lexists(token_path(home)):
        try:
            health = ask_health(state["port"], read_token(home))
        except Exception:
            health = None
    unit_note = f"installed ({target})" if installed else f"not installed ({target})"
    if running and state is not None and health is not None:
        count = health.get("sessions", 0)
        return 0, (f"running on {SERVICE_HOST}:{state['port']} (pid {state['pid']}, {count} open "
                   f"session{'' if count == 1 else 's'}); unit {unit_note}")
    if running and state is not None:
        return 0, f"running on {SERVICE_HOST}:{state['port']} (pid {state['pid']}); unit {unit_note}"
    return 1, f"not running; unit {unit_note}"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "daemon",
        help="install and manage `contexto serve` as a background launchd/systemd user daemon",
        description="Generate and manage a macOS launchd LaunchAgent or Linux systemd --user service "
                    "so `contexto serve` runs reliably in the background.",
    )
    sub = parser.add_subparsers(dest="daemon_action", metavar="<action>")

    inst = sub.add_parser("install", help="write the launchd/systemd unit and start the daemon")
    inst.add_argument("--port", type=_port, default=DEFAULT_PORT,
                      help=f"port on 127.0.0.1 (default {DEFAULT_PORT})")
    inst.add_argument("--upstream", default=None, help="default upstream base URL")
    inst.add_argument("--anthropic-upstream", default=None, help="explicit Anthropic upstream base URL")
    inst.add_argument("--openai-upstream", default=None, help="explicit OpenAI upstream base URL")
    inst.add_argument("--gemini-upstream", default=None, help="explicit Gemini upstream base URL")
    inst.add_argument("--bound", type=int, default=DEFAULT_BOUND,
                      help=f"token bound per session (default {DEFAULT_BOUND:,}; above {BOUND_GUARD:,} needs "
                           "--allow-large-bound)")
    inst.add_argument("--allow-large-bound", action="store_true", help=f"allow --bound above {BOUND_GUARD:,}")
    inst.add_argument("--allow-host", action="append", default=[], metavar="HOST",
                      help="allow an extra Host header value (repeatable)")
    inst.add_argument("--allow-local-origin", action="store_true",
                      help="allow token-authenticated local/webview Origin headers")
    inst.add_argument("--platform", choices=("auto", "darwin", "linux"), default="auto",
                      help="target service manager (default: auto-detect)")
    inst.add_argument("--no-start", action="store_true", help="write the unit file without starting it")
    inst.set_defaults(func=run)

    for action, help_text in (
        ("uninstall", "stop and remove the installed launchd/systemd unit"),
        ("start", "start the installed daemon"),
        ("stop", "stop the running daemon"),
        ("status", "report daemon installation and running status"),
    ):
        p = sub.add_parser(action, help=help_text)
        p.add_argument("--platform", choices=("auto", "darwin", "linux"), default="auto",
                       help="target service manager (default: auto-detect)")
        p.set_defaults(func=run)

    parser.set_defaults(func=run)


def run(args: argparse.Namespace, *, stdout=None, stderr=None,
        runner: CommandRunner = _default_runner) -> int:
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    action = getattr(args, "daemon_action", None)
    if not action:
        print("contexto daemon: choose one of install, uninstall, start, stop, status", file=stderr)
        return 2
    try:
        platform = detect_platform(getattr(args, "platform", "auto"))
    except ValueError as exc:
        print(f"contexto daemon: {exc}", file=stderr)
        return 1
    home = contexto_home()
    try:
        if action == "install":
            if args.port < 1:
                raise DaemonError("--port must be between 1 and 65535 for a background daemon", code=2)
            check_options(LaunchOptions(bound=args.bound, allow_large_bound=args.allow_large_bound))
            for attr, src in (("upstream", "flag"), ("anthropic_upstream", "flag:anthropic"),
                              ("openai_upstream", "flag:openai"), ("gemini_upstream", "flag:gemini")):
                raw_url = getattr(args, attr, None)
                if raw_url:
                    check_upstream_url(Upstream(raw_url, src))
            for host in args.allow_host:
                try:
                    normalize_allowed_host(host)
                except ValueError as exc:
                    raise DaemonError(f"invalid --allow-host {host!r}: {exc}", code=2) from None
            argv = serve_argv(
                port=args.port, upstream=args.upstream,
                anthropic_upstream=args.anthropic_upstream,
                openai_upstream=args.openai_upstream,
                gemini_upstream=args.gemini_upstream,
                bound=args.bound, allow_large_bound=args.allow_large_bound,
                allow_hosts=args.allow_host, allow_local_origin=args.allow_local_origin,
            )
            target, _ = install_daemon(home=home, platform=platform, argv=argv,
                                       start_now=not args.no_start, runner=runner)
            print(f"contexto daemon: installed {target} ({shlex.join(argv)})", file=stdout)
            return 0
        if action == "uninstall":
            target, removed, _ = uninstall_daemon(platform=platform, runner=runner)
            msg = f"removed {target}" if removed else f"{target} was not installed"
            print(f"contexto daemon: {msg}", file=stdout)
            return 0
        if action == "start":
            code, msg = start_daemon(platform=platform, runner=runner)
            print(f"contexto daemon: {msg}", file=stdout if code == 0 else stderr)
            return code
        if action == "stop":
            code, msg = stop_daemon(platform=platform, runner=runner)
            print(f"contexto daemon: {msg}", file=stdout if code == 0 else stderr)
            return code
        if action == "status":
            code, msg = daemon_status(home=home, platform=platform)
            print(f"contexto daemon: {msg}", file=stdout)
            return code
    except (DaemonError, ServeError, LaunchError) as exc:
        print(f"contexto daemon: {exc}", file=stderr)
        return exc.code
    return 2
