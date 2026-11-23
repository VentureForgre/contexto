"""``contexto setup``: safe, atomic, reversible configuration for IDEs and coding harnesses.

Supported targets:
- ``claude-ide``: Claude Code VS Code / JetBrains / Desktop settings (``~/.claude/settings.json`` and
  user MCP configuration in ``~/.claude.json``)
- ``opencode``: OpenCode configuration (``~/.config/opencode/opencode.json``)
- ``codex``: OpenAI Codex configuration (``~/.codex/config.toml``)
- ``cursor``: Cursor MCP configuration (``~/.cursor/mcp.json``)

Safety guarantees:
- Every target backs up every file it will modify under ``$CONTEXTO_HOME/setup-backups/<target>/``
  (directory mode 0700, files mode 0600) BEFORE editing, preserving original bytes and permissions.
- Re-running ``setup`` updates settings idempotently without overwriting the original pre-Contexto
  backup, so ``contexto setup <target> --undo`` always restores the exact pre-Contexto state (and
  removes files Contexto itself created).
- Every file written containing the service token is written atomically at mode 0600.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from contexto.commands.serve import _port
from contexto.launcher import check_mcp_values
from contexto.paths import contexto_home
from contexto.private import private_dir, write_atomic
from contexto.serve import DEFAULT_PORT, ensure_token, mcp_config_payload, settings_payload
from contexto.servecontract import base_url, mcp_url, running_state

TARGETS = ("claude-ide", "opencode", "codex", "cursor")
BACKUP_DIR_NAME = "setup-backups"
MANIFEST_NAME = "manifest.json"
CODEX_BEGIN = "# BEGIN contexto setup (managed by `contexto setup codex`)"
CODEX_END = "# END contexto setup"
_CODEX_BLOCK_RE = re.compile(
    re.escape(CODEX_BEGIN) + r".*?" + re.escape(CODEX_END) + r"\n?",
    re.DOTALL,
)


class SetupError(RuntimeError):
    """Configuration cannot be safely read or modified."""


def _resolve_port(port: int | None, home: Path) -> int:
    if port is not None:
        return port
    state = running_state(home)
    return state["port"] if state is not None else DEFAULT_PORT


def _backup_root(home: Path, target: str) -> Path:
    return Path(home) / BACKUP_DIR_NAME / target


def _load_json_object(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise SetupError(f"refusing to edit symlink {path}")
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SetupError(f"cannot read {path}: {exc.strerror or type(exc).__name__}") from None
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SetupError(f"{path} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise SetupError(f"{path} must hold a JSON object")
    return data


def _record_backup(home: Path, target: str, paths: list[Path]) -> Path:
    """Save original bytes and mode of each file in ``paths`` before modifying it. Preserves the
    original pre-Contexto backup across repeated runs while also backing up any newly targeted
    path (e.g. if ``CLAUDE_CONFIG_DIR`` or ``CODEX_HOME`` changed between runs)."""
    for file_path in paths:
        if file_path.is_symlink():
            raise SetupError(f"refusing to back up or edit symlink {file_path}")
    bdir = _backup_root(home, target)
    manifest_path = bdir / MANIFEST_NAME
    entries: list[dict[str, Any]] = []
    recorded: set[str] = set()
    if manifest_path.exists():
        existing_manifest = _load_json_object(manifest_path)
        existing_files = existing_manifest.get("files")
        if not isinstance(existing_files, list):
            raise SetupError(f"corrupt backup manifest {manifest_path}")
        entries = list(existing_files)
        recorded = {e["path"] for e in entries if isinstance(e, dict) and isinstance(e.get("path"), str)}
    missing_paths = [p for p in paths if str(p) not in recorded]
    if not missing_paths and manifest_path.exists():
        return manifest_path
    private_dir(Path(home), narrow=False)
    private_dir(bdir.parent)
    private_dir(bdir)
    next_idx = len(entries)
    for file_path in missing_paths:
        if file_path.exists():
            st = file_path.stat()
            backup_name = f"file-{next_idx}.bak"
            write_atomic(bdir / backup_name, file_path.read_bytes())
            entries.append({
                "path": str(file_path),
                "existed": True,
                "backup": backup_name,
                "mode": stat.S_IMODE(st.st_mode),
            })
        else:
            entries.append({
                "path": str(file_path),
                "existed": False,
                "backup": None,
                "mode": None,
            })
        next_idx += 1
    write_atomic(manifest_path, (json.dumps({"target": target, "files": entries}, indent=2) + "\n").encode("utf-8"))
    return manifest_path


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, text.encode("utf-8"))


def _claude_paths(user_home: Path, env: Mapping[str, str]) -> tuple[Path, Path]:
    raw = env.get("CLAUDE_CONFIG_DIR")
    config_dir = Path(raw).expanduser() if raw else (user_home / ".claude")
    return config_dir / "settings.json", user_home / ".claude.json"


def _opencode_path(user_home: Path, env: Mapping[str, str]) -> Path:
    xdg = env.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else (user_home / ".config")
    return base / "opencode" / "opencode.json"


def _codex_path(user_home: Path, env: Mapping[str, str]) -> Path:
    codex_home = Path(env["CODEX_HOME"]).expanduser() if env.get("CODEX_HOME") else (user_home / ".codex")
    return codex_home / "config.toml"


def _cursor_path(user_home: Path) -> Path:
    return user_home / ".cursor" / "mcp.json"


def _apply_claude_ide(home: Path, port: int, token: str, *, user_home: Path, env: Mapping[str, str],
                      mcp_transport: str, dry_run: bool) -> list[str]:
    settings_path, claude_json_path = _claude_paths(user_home, env)
    add_settings = settings_payload(port, token)
    current = _load_json_object(settings_path)
    env_block = dict(current.get("env") if isinstance(current.get("env"), dict) else {})
    env_block.update(add_settings["env"])
    perms = dict(current.get("permissions") if isinstance(current.get("permissions"), dict) else {})
    allow = list(perms.get("allow") if isinstance(perms.get("allow"), list) else [])
    for rule in add_settings["permissions"]["allow"]:
        if rule not in allow:
            allow.append(rule)
    perms["allow"] = allow
    updated_settings = {**current, "env": env_block, "permissions": perms}

    claude_json = _load_json_object(claude_json_path)
    mcp_servers = dict(claude_json.get("mcpServers") if isinstance(claude_json.get("mcpServers"), dict) else {})
    if mcp_transport == "http":
        mcp_servers["contexto"] = {"type": "http", "url": mcp_url(port, token)}
    else:
        check_mcp_values("serve", home, sys.executable)
        mcp_servers["contexto"] = mcp_config_payload(home)["mcpServers"]["contexto"]
    updated_claude_json = {**claude_json, "mcpServers": mcp_servers}

    if not dry_run:
        _record_backup(home, "claude-ide", [settings_path, claude_json_path])
        _write_json_atomic(settings_path, updated_settings)
        _write_json_atomic(claude_json_path, updated_claude_json)
    return [
        f"updated {settings_path} (mode 0600; ANTHROPIC_BASE_URL + Contexto tool allow rules)",
        f"updated {claude_json_path} (mcpServers.contexto via {mcp_transport})",
    ]


def _apply_opencode(home: Path, port: int, token: str, *, user_home: Path, env: Mapping[str, str],
                    mcp_transport: str, dry_run: bool) -> list[str]:
    config_path = _opencode_path(user_home, env)
    current = _load_json_object(config_path)
    providers = dict(current.get("provider") if isinstance(current.get("provider"), dict) else {})
    root = base_url(port, token)
    for name, suffix in (("anthropic", "/anthropic"), ("openai", "/openai/v1"), ("google", "/gemini/v1beta")):
        entry = dict(providers.get(name) if isinstance(providers.get(name), dict) else {})
        opts = dict(entry.get("options") if isinstance(entry.get("options"), dict) else {})
        opts["baseURL"] = root + suffix
        entry["options"] = opts
        providers[name] = entry
    mcp_block = dict(current.get("mcp") if isinstance(current.get("mcp"), dict) else {})
    if mcp_transport == "stdio":
        mcp_block["contexto"] = {
            "type": "local",
            "command": [sys.executable, "-P", "-m", "contexto", "mcp", "--serve"],
            "environment": {"CONTEXTO_HOME": str(home)},
            "enabled": True,
        }
    else:
        mcp_block["contexto"] = {
            "type": "remote",
            "url": mcp_url(port, token),
            "enabled": True,
        }
    updated = {**current, "provider": providers, "mcp": mcp_block}
    if not dry_run:
        _record_backup(home, "opencode", [config_path])
        _write_json_atomic(config_path, updated)
    return [f"updated {config_path} (mode 0600; anthropic/openai/google baseURLs + mcp.contexto via {mcp_transport})"]


def _toml_str(value: str) -> str:
    """Encode ``value`` as a TOML basic string, rejecting lone surrogates and escaping DEL."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SetupError(f"cannot encode non-UTF-8 string for TOML: {exc}") from None
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _apply_codex(home: Path, port: int, token: str, *, user_home: Path, env: Mapping[str, str],
                 mcp_transport: str, dry_run: bool) -> list[str]:
    config_path = _codex_path(user_home, env)
    if config_path.is_symlink():
        raise SetupError(f"refusing to edit symlink {config_path}")
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    if existing.strip():
        try:
            tomllib.loads(existing)
        except tomllib.TOMLDecodeError as exc:
            raise SetupError(f"{config_path} is not valid TOML: {exc}") from None
    stripped = _CODEX_BLOCK_RE.sub("", existing).rstrip()
    parsed_stripped = tomllib.loads(stripped) if stripped.strip() else {}
    existing_providers = parsed_stripped.get("model_providers")
    if isinstance(existing_providers, dict) and "contexto" in existing_providers:
        raise SetupError(f"{config_path} already defines [model_providers.contexto] outside Contexto's managed block")
    existing_mcp = parsed_stripped.get("mcp_servers")
    if isinstance(existing_mcp, dict) and "contexto" in existing_mcp:
        raise SetupError(f"{config_path} already defines [mcp_servers.contexto] outside Contexto's managed block")
    openai_base = base_url(port, token) + "/openai/v1"
    if mcp_transport == "http":
        mcp_lines = [
            "[mcp_servers.contexto]",
            f"url = {_toml_str(mcp_url(port, token))}",
            'default_tools_approval_mode = "approve"',
        ]
    else:
        mcp_lines = [
            "[mcp_servers.contexto]",
            f"command = {_toml_str(sys.executable)}",
            f"args = [{', '.join(_toml_str(a) for a in ('-P', '-m', 'contexto', 'mcp', '--serve'))}]",
            'default_tools_approval_mode = "approve"',
            "",
            "[mcp_servers.contexto.env]",
            f"CONTEXTO_HOME = {_toml_str(str(home))}",
        ]
    features_lines: list[str] = []
    extra_notes: list[str] = []
    existing_features = parsed_stripped.get("features")
    if not isinstance(existing_features, dict):
        features_lines = [
            "",
            "[features]",
            "enable_request_compression = false",
        ]
    elif existing_features.get("enable_request_compression") is not False:
        extra_notes.append(f"note: {config_path} already has [features]; set enable_request_compression = false there")
    block = "\n".join([
        CODEX_BEGIN,
        "[model_providers.contexto]",
        'name = "Contexto"',
        f"base_url = {_toml_str(openai_base)}",
        'wire_api = "responses"',
        "supports_websockets = false",
        *features_lines,
        "",
        *mcp_lines,
        CODEX_END,
        "",
    ])
    updated = f"{stripped}\n\n{block}" if stripped else block
    try:
        tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise SetupError(f"generated invalid TOML for {config_path}: {exc}") from None
    if not dry_run:
        _record_backup(home, "codex", [config_path])
        _write_text_atomic(config_path, updated)
    return [f"updated {config_path} (mode 0600; [model_providers.contexto] + [mcp_servers.contexto])", *extra_notes]


def _apply_cursor(home: Path, port: int, token: str, *, user_home: Path, env: Mapping[str, str],
                  mcp_transport: str, dry_run: bool) -> list[str]:
    del env
    mcp_path = _cursor_path(user_home)
    current = _load_json_object(mcp_path)
    mcp_servers = dict(current.get("mcpServers") if isinstance(current.get("mcpServers"), dict) else {})
    if mcp_transport == "stdio":
        mcp_servers["contexto"] = mcp_config_payload(home)["mcpServers"]["contexto"]
    else:
        mcp_servers["contexto"] = {"url": mcp_url(port, token)}
    updated = {**current, "mcpServers": mcp_servers}
    if not dry_run:
        _record_backup(home, "cursor", [mcp_path])
        _write_json_atomic(mcp_path, updated)
    root = base_url(port, token)
    return [
        f"updated {mcp_path} (mode 0600; mcpServers.contexto via {mcp_transport})",
        f"in Cursor Settings -> Models -> Override OpenAI Base URL, set: {root}/openai/v1",
    ]


def setup_target(target: str, *, home: Path, port: int | None = None,
                 user_home: Path | None = None, env: Mapping[str, str] | None = None,
                 mcp_transport: str | None = None, dry_run: bool = False) -> list[str]:
    """Configure ``target`` to use ``contexto serve``, backing up existing files first."""
    if target not in TARGETS:
        raise SetupError(f"unknown setup target {target!r} (choose from {', '.join(TARGETS)})")
    user_home = user_home or Path.home()
    environ = os.environ if env is None else env
    token = ensure_token(home)
    chosen_port = _resolve_port(port, home)
    default_transport = "stdio" if target == "claude-ide" else "http"
    transport = mcp_transport or default_transport
    if target == "claude-ide":
        return _apply_claude_ide(home, chosen_port, token, user_home=user_home, env=environ,
                                 mcp_transport=transport, dry_run=dry_run)
    if target == "opencode":
        return _apply_opencode(home, chosen_port, token, user_home=user_home, env=environ,
                               mcp_transport=transport, dry_run=dry_run)
    if target == "codex":
        return _apply_codex(home, chosen_port, token, user_home=user_home, env=environ,
                            mcp_transport=transport, dry_run=dry_run)
    return _apply_cursor(home, chosen_port, token, user_home=user_home, env=environ,
                         mcp_transport=transport, dry_run=dry_run)


def undo_target(target: str, *, home: Path, dry_run: bool = False) -> list[str]:
    """Restore every file backed up by ``contexto setup <target>`` and remove the backup."""
    if target not in TARGETS:
        raise SetupError(f"unknown setup target {target!r} (choose from {', '.join(TARGETS)})")
    bdir = _backup_root(home, target)
    manifest_path = bdir / MANIFEST_NAME
    if not manifest_path.exists():
        raise SetupError(f"no setup backup found for {target!r} under {bdir}")
    manifest = _load_json_object(manifest_path)
    files = manifest.get("files")
    if not isinstance(files, list):
        raise SetupError(f"corrupt backup manifest {manifest_path}")
    notes: list[str] = []
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise SetupError(f"corrupt entry in backup manifest {manifest_path}")
        dest = Path(entry["path"])
        if entry.get("existed"):
            backup_name = entry.get("backup")
            if not isinstance(backup_name, str) or not (bdir / backup_name).exists():
                raise SetupError(f"missing backup file for {dest}")
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                write_atomic(dest, (bdir / backup_name).read_bytes())
                mode = entry.get("mode")
                if isinstance(mode, int) and not isinstance(mode, bool):
                    try:
                        os.chmod(dest, mode)
                    except OSError:
                        pass
            notes.append(f"restored {dest}")
        else:
            if not dry_run and dest.exists() and not dest.is_symlink():
                dest.unlink()
            notes.append(f"removed {dest} (did not exist before setup)")
    if not dry_run:
        shutil.rmtree(bdir, ignore_errors=True)
    return notes


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "setup",
        help="safely configure an IDE or coding harness to use `contexto serve` (reversible with --undo)",
        description="Atomically configure Claude IDE, OpenCode, Codex, or Cursor to route through "
                    "`contexto serve` and register Contexto's MCP server. Existing files are backed up "
                    "under $CONTEXTO_HOME/setup-backups/<target>/ and restored cleanly with --undo.",
    )
    parser.add_argument("target", choices=TARGETS, help="client to configure or restore")
    parser.add_argument("--undo", action="store_true", help="restore the pre-Contexto configuration from backup")
    parser.add_argument("--port", type=_port, default=None,
                        help=f"service port (default: running service's port, else {DEFAULT_PORT})")
    parser.add_argument("--mcp-transport", choices=("stdio", "http"), default=None,
                        help="MCP transport to configure (default: stdio for claude-ide, http for others)")
    parser.add_argument("--dry-run", action="store_true", help="show what would change without modifying files")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace, *, stdout=None, stderr=None) -> int:
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    home = contexto_home()
    try:
        if args.undo:
            notes = undo_target(args.target, home=home, dry_run=args.dry_run)
        else:
            notes = setup_target(args.target, home=home, port=args.port,
                                 mcp_transport=args.mcp_transport, dry_run=args.dry_run)
    except (SetupError, Exception) as exc:
        print(f"contexto setup: {exc}", file=stderr)
        return 1
    prefix = "contexto setup (dry-run):" if args.dry_run else "contexto setup:"
    for line in notes:
        print(f"{prefix} {line}", file=stdout)
    return 0
