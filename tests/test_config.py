"""Tests for optional persistent configuration in ``$CONTEXTO_HOME/config.toml``."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from contexto import cli, codex, launcher, serve, upstream
from contexto.commands import sessions as sessions_cmd
from contexto.config import MAX_CONFIG_BYTES, ConfigError, FileConfig, load_config
from contexto.paths import CONFIG_FILE, config_path
from contexto.store import SessionStore


def _write_cfg(home: Path, text: str) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / CONFIG_FILE
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_config_returns_empty_defaults(tmp_path):
    cfg = load_config(home=tmp_path)
    assert cfg == FileConfig(path=tmp_path / CONFIG_FILE, exists=False)
    assert cfg.key_count() == 0
    assert cfg.serve_value("bound") is None
    assert config_path(home=tmp_path) == tmp_path / CONFIG_FILE


def test_non_directory_home_is_treated_as_absent_config(tmp_path):
    file_home = tmp_path / "not_a_dir"
    file_home.write_text("hi")
    cfg = load_config(home=file_home)
    assert cfg.exists is False


def test_full_valid_config_loads_all_sections(tmp_path):
    _write_cfg(tmp_path, """
bound = 120000
allow_large_bound = true
window_fraction = 0.65
headroom_floor = 25000
upstream = "https://proxy.example.test"
codex_upstream = "https://codex.example.test/v1"
in_turn_paging = false
statusline = false
quiet = true
project_memory = false
version_check = false
observe_thinking = true

[serve]
port = 8765
max_sessions = 12
idle_minutes = 45.5
forward_hint_headers = true
bound = 90000
allow_large_bound = false
window_fraction = 0.7
headroom_floor = 15000
upstream = "https://serve.example.test"
in_turn_paging = true
observe_thinking = false

[retention]
older_than = "14d"
keep = 20
""")
    cfg = load_config(home=tmp_path)
    assert cfg.exists is True
    assert cfg.bound == 120000
    assert cfg.allow_large_bound is True
    assert cfg.window_fraction == pytest.approx(0.65)
    assert cfg.headroom_floor == 25000
    assert cfg.upstream == "https://proxy.example.test"
    assert cfg.codex_upstream == "https://codex.example.test/v1"
    assert cfg.in_turn_paging is False
    assert cfg.statusline is False
    assert cfg.quiet is True
    assert cfg.project_memory is False
    assert cfg.version_check is False
    assert cfg.observe_thinking is True
    assert cfg.serve.port == 8765
    assert cfg.serve.max_sessions == 12
    assert cfg.serve.idle_minutes == pytest.approx(45.5)
    assert cfg.serve.forward_hint_headers is True
    assert cfg.serve_value("bound") == 90000
    assert cfg.serve_value("allow_large_bound") is False
    assert cfg.serve_value("window_fraction") == pytest.approx(0.7)
    assert cfg.serve_value("headroom_floor") == 15000
    assert cfg.serve_value("upstream") == "https://serve.example.test"
    assert cfg.serve_value("in_turn_paging") is True
    assert cfg.serve_value("observe_thinking") is False
    assert cfg.retention.older_than == 14 * 86400
    assert cfg.retention.older_than_raw == "14d"
    assert cfg.retention.keep == 20
    assert cfg.key_count() == 25


def test_serve_value_falls_back_to_top_level(tmp_path):
    _write_cfg(tmp_path, 'bound = 110000\nupstream = "https://top.example.test"\n[serve]\nport = 8888\n')
    cfg = load_config(home=tmp_path)
    assert cfg.serve_value("port") == 8888
    assert cfg.serve_value("bound") == 110000
    assert cfg.serve_value("upstream") == "https://top.example.test"
    assert cfg.serve_value("headroom_floor") is None


@pytest.mark.parametrize("body, expected", [
    ("bound = [unterminated", "TOML syntax error"),
    ("unknown_setting = 1\n", "unknown_setting"),
    ("[serve]\nunknown_serve = 1\n", "serve.unknown_serve"),
    ("[retention]\nunknown_ret = 1\n", "retention.unknown_ret"),
    ("bound = true\n", "expected an integer"),
    ("bound = 0\n", "must be 1 or more"),
    ("bound = -10\n", "must be 1 or more"),
    ("window_fraction = 0.0\n", "must be above 0"),
    ("window_fraction = 1.0\n", "must be below 1"),
    ("window_fraction = nan\n", "finite number"),
    ("window_fraction = true\n", "expected a number"),
    ("headroom_floor = 0\n", "must be 1 or more"),
    ('upstream = "   "\n', "must not be empty"),
    ("upstream = 42\n", "expected a string"),
    ("quiet = 1\n", "expected a boolean"),
    ('serve = "not-a-table"\n', "expected a table"),
    ('retention = ["not-a-table"]\n', "expected a table"),
    ("[serve]\nport = 70000\n", "between 0 and 65535"),
    ("[serve]\nmax_sessions = 0\n", "must be 1 or more"),
    ("[serve]\nidle_minutes = 0\n", "must be above 0"),
    ("[retention]\nkeep = -1\n", "must be 0 or more"),
    ('[retention]\nolder_than = "not-a-duration"\n', "retention.older_than"),
])
def test_invalid_config_raises_clear_error(tmp_path, body, expected):
    _write_cfg(tmp_path, body)
    with pytest.raises(ConfigError, match=expected):
        load_config(home=tmp_path)


def test_non_utf8_directory_fifo_and_oversized_config_are_refused(tmp_path):
    # Non-UTF-8
    h1 = tmp_path / "h1"
    h1.mkdir()
    (h1 / CONFIG_FILE).write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ConfigError, match="not valid UTF-8"):
        load_config(home=h1)

    # Directory (including Windows EACCES from os.open on a directory)
    h2 = tmp_path / "h2"
    (h2 / CONFIG_FILE).mkdir(parents=True)
    with pytest.raises(ConfigError, match="not a regular file"):
        load_config(home=h2)

    # FIFO (must not block; POSIX-only)
    if hasattr(os, "mkfifo"):
        h3 = tmp_path / "h3"
        h3.mkdir()
        os.mkfifo(h3 / CONFIG_FILE)
        with pytest.raises(ConfigError, match="not a regular file"):
            load_config(home=h3)

    # Oversized
    h4 = tmp_path / "h4"
    h4.mkdir()
    (h4 / CONFIG_FILE).write_bytes(b"#" * (MAX_CONFIG_BYTES + 1))
    with pytest.raises(ConfigError, match="larger than"):
        load_config(home=h4)


def test_launcher_precedence_cli_over_env_over_config_over_default(tmp_path):
    _write_cfg(tmp_path, """
bound = 111000
allow_large_bound = true
window_fraction = 0.62
headroom_floor = 18000
in_turn_paging = false
statusline = false
quiet = true
project_memory = false
version_check = false
observe_thinking = true
""")
    cfg = load_config(home=tmp_path)
    parser = cli.build_parser()

    # 1. Config values apply when neither env nor CLI sets them.
    args = parser.parse_args(["run", "claude"])
    opts = launcher.options_from_args(args, config=cfg, env={})
    assert opts.bound == 111000
    assert opts.allow_large_bound is True
    assert opts.window_fraction == pytest.approx(0.62)
    assert opts.headroom_floor == 18000
    assert opts.in_turn_paging is False
    assert opts.statusline is False
    assert opts.statusline_source == "config"
    assert opts.quiet is True
    assert opts.project_memory is False
    assert opts.version_check is False
    assert opts.observe_thinking is True

    # 2. Env overrides config (e.g., CONTEXTO_QUIET=0 overrides config quiet=true,
    #    CONTEXTO_NO_STATUSLINE=0 overrides config statusline=false).
    env = {
        "CONTEXTO_QUIET": "0",
        "CONTEXTO_NO_STATUSLINE": "0",
        "CONTEXTO_IN_TURN_PAGING": "1",
        "CONTEXTO_PROJECT_MEMORY": "1",
        "CONTEXTO_NO_VERSION_CHECK": "0",
    }
    opts_env = launcher.options_from_args(args, config=cfg, env=env)
    assert opts_env.quiet is False
    assert opts_env.statusline is True
    assert opts_env.statusline_source is None
    assert opts_env.in_turn_paging is True
    assert opts_env.project_memory is True
    assert opts_env.version_check is True

    # 3. Explicit CLI flags override both env and config.
    args_cli = parser.parse_args([
        "run", "--bound", "130000", "--window-fraction", "0.75", "--headroom-floor", "22000",
        "--no-quiet", "--statusline", "--in-turn-paging", "--project-memory", "--version-check",
        "claude",
    ])
    env_off = {
        "CONTEXTO_QUIET": "1",
        "CONTEXTO_NO_STATUSLINE": "1",
        "CONTEXTO_IN_TURN_PAGING": "0",
        "CONTEXTO_PROJECT_MEMORY": "0",
        "CONTEXTO_NO_VERSION_CHECK": "1",
    }
    opts_cli = launcher.options_from_args(args_cli, config=cfg, env=env_off)
    assert opts_cli.bound == 130000
    assert opts_cli.window_fraction == pytest.approx(0.75)
    assert opts_cli.headroom_floor == 22000
    assert opts_cli.quiet is False
    assert opts_cli.statusline is True
    assert opts_cli.in_turn_paging is True
    assert opts_cli.project_memory is True
    assert launcher.project_memory_wanted(opts_cli, env=env_off) is True
    assert opts_cli.version_check is True

    # 4. Unrecognized CONTEXTO_PROJECT_MEMORY is ignored without flipping config.toml project_memory=false
    opts_bad_pm = launcher.options_from_args(args, config=cfg, env={"CONTEXTO_PROJECT_MEMORY": "bogus"})
    assert opts_bad_pm.project_memory is False
    assert launcher.project_memory_wanted(opts_bad_pm, env={"CONTEXTO_PROJECT_MEMORY": "bogus"}) is False

    # 5. CONTEXTO_PROJECT_MEMORY=0 resolves into LaunchOptions.project_memory=False, while direct
    #    LaunchOptions(project_memory=False) still overrides CONTEXTO_PROJECT_MEMORY=1 (legacy API).
    opts_pm_off = launcher.options_from_args(args, config=FileConfig(), env={"CONTEXTO_PROJECT_MEMORY": "0"})
    assert opts_pm_off.project_memory is False
    assert launcher.project_memory_wanted(launcher.LaunchOptions(project_memory=False),
                                          env={"CONTEXTO_PROJECT_MEMORY": "1"}) is False
    #    Meanwhile config.toml project_memory=false IS overridden by CONTEXTO_PROJECT_MEMORY=1.
    assert launcher.project_memory_wanted(opts, env={"CONTEXTO_PROJECT_MEMORY": "1"}) is True

    # 6. Contradictory CLI boolean pairs are refused by argparse
    for bad_argv in (["run", "--quiet", "--no-quiet", "claude"],
                     ["run", "--no-in-turn-paging", "--in-turn-paging", "claude"],
                     ["serve", "--no-in-turn-paging", "--in-turn-paging"]):
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(bad_argv)
        assert exc_info.value.code == 2


def test_upstream_and_codex_upstream_precedence(tmp_path):
    # Claude upstream: flag > settings > env > config > default
    res_cfg = upstream.resolve_upstream(
        None, cwd=tmp_path, home=tmp_path, env={}, config_upstream="https://cfg.example.test",
    )
    assert res_cfg.url == "https://cfg.example.test" and res_cfg.source == "config"

    res_env = upstream.resolve_upstream(
        None, cwd=tmp_path, home=tmp_path,
        env={"ANTHROPIC_BASE_URL": "https://env.example.test"},
        config_upstream="https://cfg.example.test",
    )
    assert res_env.url == "https://env.example.test" and res_env.source == "env"

    res_flag = upstream.resolve_upstream(
        "https://flag.example.test", cwd=tmp_path, home=tmp_path,
        env={"ANTHROPIC_BASE_URL": "https://env.example.test"},
        config_upstream="https://cfg.example.test",
    )
    assert res_flag.url == "https://flag.example.test" and res_flag.source == "flag"

    # Codex upstream: flag > env > config > default
    url, src = codex.codex_upstream_with_source(
        codex.API_KEY_MODE, None, {}, None, config_upstream="https://codex-cfg.example.test/v1",
    )
    assert (url, src) == ("https://codex-cfg.example.test/v1", "codex-config")

    url, src = codex.codex_upstream_with_source(
        codex.API_KEY_MODE, None, {"CONTEXTO_CODEX_UPSTREAM": "https://codex-env.example.test/v1"}, None,
        config_upstream="https://codex-cfg.example.test/v1",
    )
    assert (url, src) == ("https://codex-env.example.test/v1", "codex-env")

    url, src = codex.codex_upstream_with_source(
        codex.API_KEY_MODE, "https://codex-flag.example.test/v1",
        {"CONTEXTO_CODEX_UPSTREAM": "https://codex-env.example.test/v1"}, None,
        config_upstream="https://codex-cfg.example.test/v1",
    )
    assert (url, src) == ("https://codex-flag.example.test/v1", "codex-flag")


def test_serve_options_precedence(tmp_path):
    _write_cfg(tmp_path, """
bound = 140000
in_turn_paging = false

[serve]
port = 9191
bound = 95000
max_sessions = 7
idle_minutes = 15
forward_hint_headers = true
upstream = "https://serve-cfg.example.test"
""")
    cfg = load_config(home=tmp_path)
    parser = cli.build_parser()

    args = parser.parse_args(["serve"])
    opts = serve.serve_options(args, config=cfg, env={})
    assert opts.bound == 95000
    assert opts.config_upstream == "https://serve-cfg.example.test"
    assert opts.in_turn_paging is False
    assert serve._chosen_port(args, tmp_path, cfg) == 9191

    # CLI overrides config
    args2 = parser.parse_args(["serve", "--port", "9292", "--bound", "105000", "--max-sessions", "3",
                               "--idle-minutes", "5", "--in-turn-paging"])
    opts2 = serve.serve_options(args2, config=cfg, env={})
    assert opts2.bound == 105000
    assert opts2.in_turn_paging is True
    assert serve._chosen_port(args2, tmp_path, cfg) == 9292


def test_sessions_prune_applies_retention_config_defaults(tmp_path, monkeypatch, capsys):
    import time

    home = tmp_path / "home"
    old = time.time() - 3600
    for i in range(4):
        s = SessionStore(home, f"s{i}")
        s.release()
        t = old + i * 60
        for p in [s.dir, *s.dir.rglob("*")]:
            os.utime(p, (t, t))
    _write_cfg(home, '[retention]\nolder_than = "365d"\nkeep = 2\n')
    monkeypatch.setenv("CONTEXTO_HOME", str(home))
    parser = cli.build_parser()
    # Dry-run with no CLI selection flags applies [retention] (both older_than=365d and keep=2 -> none older than 365d)
    args = parser.parse_args(["sessions", "prune"])
    assert sessions_cmd.run_prune(args) == 0
    assert "No sessions to remove" in capsys.readouterr().out
    # Explicit CLI --keep 3 does NOT silently AND config.toml's older_than=365d!
    args2 = parser.parse_args(["sessions", "prune", "--keep", "3"])
    assert sessions_cmd.run_prune(args2) == 0
    out2 = capsys.readouterr().out
    assert "1 session" in out2
    # Config with only keep = 2 selects 2 sessions when no CLI flag is passed
    _write_cfg(home, "[retention]\nkeep = 2\n")
    args3 = parser.parse_args(["sessions", "prune"])
    assert sessions_cmd.run_prune(args3) == 0
    assert "2 session" in capsys.readouterr().out


def test_symlinked_config_and_huge_int_overflow(tmp_path):
    # Dotfile symlink to a regular config file is followed (skipped if Windows host disallows symlinks)
    target = tmp_path / "dotfiles_config.toml"
    target.write_text("bound = 123456\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    try:
        (home / CONFIG_FILE).symlink_to(target)
    except OSError:
        pass
    else:
        cfg = load_config(home=home)
        assert cfg.exists is True and cfg.bound == 123456

    # Huge integer overflowing float raises ConfigError cleanly
    h2 = tmp_path / "h2"
    _write_cfg(h2, f"[serve]\nidle_minutes = {'9' * 400}\n")
    with pytest.raises(ConfigError, match="finite number"):
        load_config(home=h2)


def test_cli_exits_usage_on_invalid_config(tmp_path):
    home = tmp_path / "home"
    _write_cfg(home, "bogus_key = 123\n")
    env = {**os.environ, "CONTEXTO_HOME": str(home)}
    for cmd in (["run", "claude"], ["serve"]):
        proc = subprocess.run([sys.executable, "-m", "contexto", *cmd], env=env, capture_output=True, text=True)
        assert proc.returncode == 2
        assert "bogus_key" in proc.stderr
