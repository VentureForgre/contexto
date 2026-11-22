"""Optional persistent configuration in ``$CONTEXTO_HOME/config.toml``.

Uses the standard library's ``tomllib`` (Python 3.12+). When ``config.toml`` does not exist, an
empty ``FileConfig`` is returned with zero behavioral change.

Precedence across Contexto commands:
    explicit CLI flags > environment variables > ``config.toml`` > built-in defaults.

Supported keys:
- Top-level: ``bound``, ``allow_large_bound``, ``window_fraction``, ``headroom_floor``,
  ``upstream``, ``codex_upstream``, ``in_turn_paging``, ``statusline``, ``quiet``,
  ``project_memory``, ``version_check``, ``observe_thinking``.
- ``[serve]`` table: ``port``, ``max_sessions``, ``idle_minutes``, ``forward_hint_headers``,
  plus optional overrides of ``bound``, ``allow_large_bound``, ``window_fraction``,
  ``headroom_floor``, ``upstream``, ``in_turn_paging``, ``observe_thinking``.
- ``[retention]`` table: ``older_than`` (duration string such as ``"30d"``), ``keep`` (count >= 0).

Unknown keys, wrong types, and out-of-range values raise ``ConfigError`` with the file and field
name so typos never silently fall back to defaults.
"""

from __future__ import annotations

import math
import os
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contexto.canonical import has_lone_surrogate
from contexto.paths import config_path

MAX_CONFIG_BYTES = 1 * 1024 * 1024  # 1 MiB; real config files are a few lines
MAX_PORT = 65535

TOP_KEYS = frozenset({
    "bound", "allow_large_bound", "window_fraction", "headroom_floor",
    "upstream", "codex_upstream", "in_turn_paging", "statusline", "quiet",
    "project_memory", "version_check", "observe_thinking",
    "serve", "retention",
})
SERVE_KEYS = frozenset({
    "port", "max_sessions", "idle_minutes", "forward_hint_headers",
    "bound", "allow_large_bound", "window_fraction", "headroom_floor",
    "upstream", "in_turn_paging", "observe_thinking",
})
RETENTION_KEYS = frozenset({"older_than", "keep"})


class ConfigError(ValueError):
    """``config.toml`` could not be read, parsed, or validated."""


@dataclass(frozen=True)
class ServeConfig:
    port: int | None = None
    max_sessions: int | None = None
    idle_minutes: float | None = None
    forward_hint_headers: bool | None = None
    bound: int | None = None
    allow_large_bound: bool | None = None
    window_fraction: float | None = None
    headroom_floor: int | None = None
    upstream: str | None = None
    in_turn_paging: bool | None = None
    observe_thinking: bool | None = None


@dataclass(frozen=True)
class RetentionConfig:
    older_than: int | None = None  # seconds, parsed from a duration string
    older_than_raw: str | None = None
    keep: int | None = None


@dataclass(frozen=True)
class FileConfig:
    path: Path | None = None
    exists: bool = False
    bound: int | None = None
    allow_large_bound: bool | None = None
    window_fraction: float | None = None
    headroom_floor: int | None = None
    upstream: str | None = None
    codex_upstream: str | None = None
    in_turn_paging: bool | None = None
    statusline: bool | None = None
    quiet: bool | None = None
    project_memory: bool | None = None
    version_check: bool | None = None
    observe_thinking: bool | None = None
    serve: ServeConfig = field(default_factory=ServeConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)

    def serve_value(self, name: str) -> Any:
        """``[serve].<name>`` when set, else top-level ``<name>`` when defined, else None."""
        val = getattr(self.serve, name, None)
        if val is not None:
            return val
        return getattr(self, name, None)

    def key_count(self) -> int:
        """How many settings this file explicitly configured."""
        top = sum(1 for k in (
            "bound", "allow_large_bound", "window_fraction", "headroom_floor",
            "upstream", "codex_upstream", "in_turn_paging", "statusline", "quiet",
            "project_memory", "version_check", "observe_thinking",
        ) if getattr(self, k) is not None)
        srv = sum(1 for k in SERVE_KEYS if getattr(self.serve, k) is not None)
        ret = sum(1 for k in ("older_than", "keep") if getattr(self.retention, k) is not None)
        return top + srv + ret


def _fail(path: Path, field_name: str | None, message: str) -> ConfigError:
    prefix = f"invalid config file {path}"
    if field_name:
        return ConfigError(f"{prefix}: field '{field_name}': {message}")
    return ConfigError(f"{prefix}: {message}")


def _check_unknown(data: Mapping[str, Any], allowed: frozenset[str], path: Path, section: str = "") -> None:
    for key in data:
        if key not in allowed:
            full = f"{section}.{key}" if section else key
            names = ", ".join(sorted(allowed))
            raise _fail(path, full, f"unknown setting (supported: {names})")


def _bool(data: Mapping[str, Any], key: str, path: Path, section: str = "") -> bool | None:
    if key not in data:
        return None
    val = data[key]
    if type(val) is not bool:
        full = f"{section}.{key}" if section else key
        raise _fail(path, full, f"expected a boolean (true or false), got {type(val).__name__}")
    return val


def _pos_int(data: Mapping[str, Any], key: str, path: Path, section: str = "", *, minimum: int = 1,
             maximum: int | None = None) -> int | None:
    if key not in data:
        return None
    val = data[key]
    full = f"{section}.{key}" if section else key
    if type(val) is not int:
        raise _fail(path, full, f"expected an integer, got {type(val).__name__}")
    if val < minimum or (maximum is not None and val > maximum):
        if maximum is not None:
            raise _fail(path, full, f"must be between {minimum} and {maximum}, got {val}")
        raise _fail(path, full, f"must be {minimum} or more, got {val}")
    return val


def _float_range(data: Mapping[str, Any], key: str, path: Path, section: str = "", *,
                 gt: float | None = None, lt: float | None = None) -> float | None:
    if key not in data:
        return None
    val = data[key]
    full = f"{section}.{key}" if section else key
    if type(val) not in (int, float):
        raise _fail(path, full, f"expected a number, got {type(val).__name__}")
    fval = float(val)
    if not math.isfinite(fval):
        raise _fail(path, full, "must be a finite number")
    if gt is not None and not fval > gt:
        raise _fail(path, full, f"must be above {gt:g}, got {val}")
    if lt is not None and not fval < lt:
        raise _fail(path, full, f"must be below {lt:g}, got {val}")
    return fval


def _nonempty_str(data: Mapping[str, Any], key: str, path: Path, section: str = "") -> str | None:
    if key not in data:
        return None
    val = data[key]
    full = f"{section}.{key}" if section else key
    if not isinstance(val, str):
        raise _fail(path, full, f"expected a string, got {type(val).__name__}")
    stripped = val.strip()
    if not stripped:
        raise _fail(path, full, "must not be empty")
    if has_lone_surrogate(stripped):
        raise _fail(path, full, "contains an invalid Unicode surrogate")
    return stripped


def _read_bytes(path: Path) -> bytes | None:
    """Read ``path`` safely: follow symlinks (dotfiles), never block on a FIFO, reject directories and
    files larger than ``MAX_CONFIG_BYTES``. Returns None when the file does not exist."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise _fail(path, None, f"cannot be opened ({exc.strerror or type(exc).__name__})") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _fail(path, None, "is not a regular file")
        if st.st_size > MAX_CONFIG_BYTES:
            raise _fail(path, None, f"is larger than {MAX_CONFIG_BYTES // (1024 * 1024)} MiB")
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_CONFIG_BYTES:
            chunk = os.read(fd, min(65536, MAX_CONFIG_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    except ConfigError:
        raise
    except OSError as exc:
        raise _fail(path, None, f"cannot be read ({exc.strerror or type(exc).__name__})") from None
    finally:
        os.close(fd)
    if total > MAX_CONFIG_BYTES:
        raise _fail(path, None, f"is larger than {MAX_CONFIG_BYTES // (1024 * 1024)} MiB")
    return b"".join(chunks)


def _parse_serve(raw: Any, path: Path) -> ServeConfig:
    if raw is None:
        return ServeConfig()
    if not isinstance(raw, dict):
        raise _fail(path, "serve", f"expected a table ([serve]), got {type(raw).__name__}")
    _check_unknown(raw, SERVE_KEYS, path, "serve")
    return ServeConfig(
        port=_pos_int(raw, "port", path, "serve", minimum=0, maximum=MAX_PORT),
        max_sessions=_pos_int(raw, "max_sessions", path, "serve", minimum=1),
        idle_minutes=_float_range(raw, "idle_minutes", path, "serve", gt=0.0),
        forward_hint_headers=_bool(raw, "forward_hint_headers", path, "serve"),
        bound=_pos_int(raw, "bound", path, "serve", minimum=1),
        allow_large_bound=_bool(raw, "allow_large_bound", path, "serve"),
        window_fraction=_float_range(raw, "window_fraction", path, "serve", gt=0.0, lt=1.0),
        headroom_floor=_pos_int(raw, "headroom_floor", path, "serve", minimum=1),
        upstream=_nonempty_str(raw, "upstream", path, "serve"),
        in_turn_paging=_bool(raw, "in_turn_paging", path, "serve"),
        observe_thinking=_bool(raw, "observe_thinking", path, "serve"),
    )


def _parse_retention(raw: Any, path: Path) -> RetentionConfig:
    if raw is None:
        return RetentionConfig()
    if not isinstance(raw, dict):
        raise _fail(path, "retention", f"expected a table ([retention]), got {type(raw).__name__}")
    _check_unknown(raw, RETENTION_KEYS, path, "retention")
    older_raw = _nonempty_str(raw, "older_than", path, "retention")
    older_secs: int | None = None
    if older_raw is not None:
        from contexto.store.prune import parse_duration

        try:
            older_secs = parse_duration(older_raw)
        except ValueError as exc:
            raise _fail(path, "retention.older_than", str(exc)) from None
    keep = _pos_int(raw, "keep", path, "retention", minimum=0)
    return RetentionConfig(older_than=older_secs, older_than_raw=older_raw, keep=keep)


def load_config(home: Path | None = None, env: Mapping[str, str] | None = None, *,
                path: Path | None = None) -> FileConfig:
    """Load and validate ``$CONTEXTO_HOME/config.toml`` (or ``path``). Returns an empty ``FileConfig``
    when the file does not exist; raises ``ConfigError`` when it exists and is invalid."""
    target = config_path(home, env) if path is None else Path(path)
    raw = _read_bytes(target)
    if raw is None:
        return FileConfig(path=target, exists=False)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _fail(target, None, f"is not valid UTF-8 ({exc.reason})") from None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise _fail(target, None, f"TOML syntax error: {exc}") from None
    _check_unknown(data, TOP_KEYS, target)
    return FileConfig(
        path=target,
        exists=True,
        bound=_pos_int(data, "bound", target, minimum=1),
        allow_large_bound=_bool(data, "allow_large_bound", target),
        window_fraction=_float_range(data, "window_fraction", target, gt=0.0, lt=1.0),
        headroom_floor=_pos_int(data, "headroom_floor", target, minimum=1),
        upstream=_nonempty_str(data, "upstream", target),
        codex_upstream=_nonempty_str(data, "codex_upstream", target),
        in_turn_paging=_bool(data, "in_turn_paging", target),
        statusline=_bool(data, "statusline", target),
        quiet=_bool(data, "quiet", target),
        project_memory=_bool(data, "project_memory", target),
        version_check=_bool(data, "version_check", target),
        observe_thinking=_bool(data, "observe_thinking", target),
        serve=_parse_serve(data.get("serve"), target),
        retention=_parse_retention(data.get("retention"), target),
    )
