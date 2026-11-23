"""Tests for `contexto daemon` (launchd + systemd unit generation & lifecycle) and
`contexto setup <claude-ide|opencode|codex|cursor> [--undo]`."""

from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import sys

import pytest

from contexto import cli
from contexto.commands import daemon, setup
from contexto.serve import ensure_token


def _fake_runner(log: list[list[str]], *, rc: int = 0):
    def _run(cmd):
        log.append(list(cmd))
        return subprocess.CompletedProcess(list(cmd), rc, "", "")

    return _run


# 1. contexto daemon ------------------------------------------------------------------------------


def test_launchd_plist_generation_and_install_uninstall(tmp_path):
    home = tmp_path / "contexto-home"
    user_home = tmp_path / "user-home"
    argv = daemon.serve_argv(
        python="/usr/bin/python3", port=7840,
        upstream="https://api.anthropic.com",
        openai_upstream="https://api.openai.com",
        gemini_upstream="https://generativelanguage.googleapis.com",
        bound=120_000, allow_hosts=["host.docker.internal"], allow_local_origin=True,
    )
    raw = daemon.launchd_plist(home, argv)
    parsed = plistlib.loads(raw)
    assert parsed["Label"] == daemon.LAUNCHD_LABEL
    assert parsed["ProgramArguments"] == argv
    assert parsed["EnvironmentVariables"] == {"CONTEXTO_HOME": str(home)}
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] == {"SuccessfulExit": False}
    assert "--openai-upstream" in parsed["ProgramArguments"]
    assert "--allow-host" in parsed["ProgramArguments"]
    assert "--allow-local-origin" in parsed["ProgramArguments"]

    ran: list[list[str]] = []
    target, cmds = daemon.install_daemon(
        home=home, platform="darwin", argv=argv, home_dir=user_home,
        start_now=True, runner=_fake_runner(ran), uid=501,
    )
    assert target == user_home / "Library" / "LaunchAgents" / daemon.LAUNCHD_FILENAME
    assert target.exists()
    if sys.platform != "win32":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert any(c[:2] == ["launchctl", "bootstrap"] for c in cmds)

    code, msg = daemon.start_daemon(platform="darwin", home_dir=user_home, runner=_fake_runner(ran), uid=501)
    assert code == 0 and "started" in msg
    code, msg = daemon.stop_daemon(platform="darwin", home_dir=user_home, runner=_fake_runner(ran), uid=501)
    assert code == 0 and msg == "stopped"

    code, status_msg = daemon.daemon_status(home=home, platform="darwin", home_dir=user_home)
    assert code == 1 and "not running" in status_msg and "installed" in status_msg

    _, removed, ucmds = daemon.uninstall_daemon(platform="darwin", home_dir=user_home,
                                                runner=_fake_runner(ran), uid=501)
    assert removed is True and not target.exists()
    assert any(c[:2] == ["launchctl", "bootout"] for c in ucmds)


def test_systemd_unit_generation_and_install_uninstall(tmp_path):
    home = tmp_path / "my home" / "contexto"
    user_home = tmp_path / "user-home"
    xdg = tmp_path / "xdg"
    argv = daemon.serve_argv(python="/opt/py 3/python", port=7837, upstream="https://api.anthropic.com")
    unit = daemon.systemd_unit(home, argv)
    assert "[Unit]" in unit and "[Service]" in unit and "[Install]" in unit
    assert '"/opt/py 3/python"' in unit
    escaped_home = str(home).replace("\\", "\\\\")
    assert f'Environment="CONTEXTO_HOME={escaped_home}"' in unit
    assert "Restart=on-failure" in unit

    ran: list[list[str]] = []
    env = {"XDG_CONFIG_HOME": str(xdg)}
    target, cmds = daemon.install_daemon(
        home=home, platform="linux", argv=argv, home_dir=user_home, env=env,
        start_now=True, runner=_fake_runner(ran),
    )
    assert target == xdg / "systemd" / "user" / daemon.SYSTEMD_UNIT_NAME
    assert target.exists()
    if sys.platform != "win32":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert ["systemctl", "--user", "daemon-reload"] in cmds
    assert ["systemctl", "--user", "enable", "--now", daemon.SYSTEMD_UNIT_NAME] in cmds

    code, _ = daemon.start_daemon(platform="linux", home_dir=user_home, env=env, runner=_fake_runner(ran))
    assert code == 0
    code, _ = daemon.stop_daemon(platform="linux", home_dir=user_home, env=env, runner=_fake_runner(ran))
    assert code == 0

    _, removed, ucmds = daemon.uninstall_daemon(platform="linux", home_dir=user_home, env=env,
                                                runner=_fake_runner(ran))
    assert removed is True and not target.exists()
    assert ["systemctl", "--user", "disable", "--now", daemon.SYSTEMD_UNIT_NAME] in ucmds


def test_daemon_cli_install_no_start_and_status(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    user_home = tmp_path / "u"
    user_home.mkdir()
    monkeypatch.setenv("CONTEXTO_HOME", str(home))
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("USERPROFILE", str(user_home))
    assert cli.main(["daemon", "install", "--platform", "darwin", "--no-start", "--port", "7845"]) == 0
    out = capsys.readouterr().out
    assert "installed" in out
    assert (user_home / "Library" / "LaunchAgents" / daemon.LAUNCHD_FILENAME).exists()
    assert cli.main(["daemon", "status", "--platform", "darwin"]) == 1
    assert "not running" in capsys.readouterr().out


# 2. contexto setup <target> [--undo] -------------------------------------------------------------


def test_setup_claude_ide_preserves_existing_and_undo_restores_exact_bytes(tmp_path):
    home = tmp_path / "contexto"
    user_home = tmp_path / "user"
    claude_dir = user_home / ".claude"
    claude_dir.mkdir(parents=True)
    settings_path = claude_dir / "settings.json"
    original_settings = '{\n  "theme": "dark",\n  "env": {"KEEP": "yes"}\n}\n'
    settings_path.write_text(original_settings)
    os.chmod(settings_path, 0o644)
    claude_json_path = user_home / ".claude.json"
    assert not claude_json_path.exists()

    env = {"HOME": str(user_home)}
    notes = setup.setup_target("claude-ide", home=home, port=7837, user_home=user_home, env=env)
    assert len(notes) == 2
    token = ensure_token(home)
    updated = json.loads(settings_path.read_text())
    assert updated["theme"] == "dark" and updated["env"]["KEEP"] == "yes"
    assert updated["env"]["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:7837/{token}"
    assert "mcp__contexto__contexto_search" in updated["permissions"]["allow"]
    if sys.platform != "win32":
        assert stat.S_IMODE(settings_path.stat().st_mode) == 0o600
    assert "contexto" in json.loads(claude_json_path.read_text())["mcpServers"]

    # Idempotent re-run at a different port preserves the original pre-Contexto backup!
    setup.setup_target("claude-ide", home=home, port=7900, user_home=user_home, env=env)
    assert json.loads(settings_path.read_text())["env"]["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:7900/{token}"

    # Undo restores original settings bytes & mode, and deletes .claude.json that did not exist before
    undo_notes = setup.undo_target("claude-ide", home=home)
    assert len(undo_notes) == 2
    assert settings_path.read_text() == original_settings
    if sys.platform != "win32":
        assert stat.S_IMODE(settings_path.stat().st_mode) == 0o644
    assert not claude_json_path.exists()
    with pytest.raises(setup.SetupError, match="no setup backup"):
        setup.undo_target("claude-ide", home=home)


@pytest.mark.parametrize("target", ["opencode", "codex", "cursor"])
def test_setup_and_undo_for_opencode_codex_and_cursor(tmp_path, target):
    home = tmp_path / "contexto"
    user_home = tmp_path / "user"
    user_home.mkdir()
    env = {"HOME": str(user_home), "XDG_CONFIG_HOME": str(user_home / ".config"),
           "CODEX_HOME": str(user_home / ".codex")}

    if target == "opencode":
        cfg = user_home / ".config" / "opencode" / "opencode.json"
        cfg.parent.mkdir(parents=True)
        orig = '{"model": "claude-sonnet-4"}\n'
        cfg.write_text(orig)
    elif target == "codex":
        cfg = user_home / ".codex" / "config.toml"
        cfg.parent.mkdir(parents=True)
        orig = 'model = "gpt-5"\n'
        cfg.write_text(orig)
    else:
        cfg = user_home / ".cursor" / "mcp.json"
        cfg.parent.mkdir(parents=True)
        orig = '{"mcpServers": {"other": {"url": "http://localhost:9999"}}}\n'
        cfg.write_text(orig)

    # Dry-run modifies nothing
    setup.setup_target(target, home=home, port=7837, user_home=user_home, env=env, dry_run=True)
    assert cfg.read_text() == orig

    # Real setup writes config with token at mode 0600
    setup.setup_target(target, home=home, port=7837, user_home=user_home, env=env)
    token = ensure_token(home)
    after = cfg.read_text()
    assert token in after
    if sys.platform != "win32":
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    if target == "opencode":
        parsed = json.loads(after)
        assert parsed["model"] == "claude-sonnet-4"
        assert parsed["provider"]["openai"]["options"]["baseURL"].endswith("/openai/v1")
        assert parsed["provider"]["google"]["options"]["baseURL"].endswith("/gemini/v1beta")
        assert parsed["mcp"]["contexto"]["url"].endswith("/mcp")
    elif target == "codex":
        import tomllib

        parsed_toml = tomllib.loads(after)
        assert parsed_toml["model"] == "gpt-5"
        assert parsed_toml["model_providers"]["contexto"]["base_url"].endswith("/openai/v1")
        assert parsed_toml["features"]["enable_request_compression"] is False
        assert parsed_toml["mcp_servers"]["contexto"]["url"].endswith("/mcp")
        # Second run replaces managed block instead of duplicating it
        setup.setup_target(target, home=home, port=7838, user_home=user_home, env=env)
        re_read = cfg.read_text()
        assert re_read.count(setup.CODEX_BEGIN) == 1
        assert ":7838/" in re_read
        assert tomllib.loads(re_read)["model_providers"]["contexto"]["base_url"].endswith("/openai/v1")
    else:
        parsed = json.loads(after)
        assert "other" in parsed["mcpServers"]
        assert parsed["mcpServers"]["contexto"]["url"].endswith("/mcp")

    # Undo restores exact original file
    setup.undo_target(target, home=home)
    assert cfg.read_text() == orig


def test_setup_refuses_symlinked_config(tmp_path):
    home = tmp_path / "contexto"
    user_home = tmp_path / "user"
    cursor_dir = user_home / ".cursor"
    cursor_dir.mkdir(parents=True)
    real = tmp_path / "real.json"
    real.write_text("{}")
    (cursor_dir / "mcp.json").symlink_to(real)
    with pytest.raises(setup.SetupError, match="symlink"):
        setup.setup_target("cursor", home=home, port=7837, user_home=user_home, env={"HOME": str(user_home)})


def test_setup_cli_roundtrip(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    user_home = tmp_path / "u"
    user_home.mkdir()
    monkeypatch.setenv("CONTEXTO_HOME", str(home))
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("USERPROFILE", str(user_home))
    assert cli.main(["setup", "cursor", "--port", "7855"]) == 0
    assert (user_home / ".cursor" / "mcp.json").exists()
    assert cli.main(["setup", "cursor", "--undo"]) == 0
    assert not (user_home / ".cursor" / "mcp.json").exists()
    assert "removed" in capsys.readouterr().out


def test_daemon_rejects_control_chars_escapes_dollar_and_propagates_errors(tmp_path):
    home = tmp_path / "home$val"
    argv = ["/usr/bin/python3", "-m", "contexto", "serve", "--upstream", "http://127.0.0.1:4000/$var"]
    unit = daemon.systemd_unit(home, argv)
    # ExecStart doubles $, Environment keeps $ literal
    assert '"http://127.0.0.1:4000/$$var"' in unit
    escaped_home = str(home).replace("\\", "\\\\")
    assert f"Environment=CONTEXTO_HOME={escaped_home}" in unit or f'Environment="CONTEXTO_HOME={escaped_home}"' in unit
    assert "$$val" not in unit

    # Directive injection via newline is rejected
    with pytest.raises(daemon.DaemonError, match="newline"):
        daemon.systemd_unit(home, ["/usr/bin/python3", "bad\nExecStartPre=/bin/evil"])
    with pytest.raises(daemon.DaemonError, match="newline"):
        daemon.launchd_plist(home, ["/usr/bin/python3", "bad\x00arg"])

    # Non-zero systemctl exit raises DaemonError instead of silently succeeding
    with pytest.raises(daemon.DaemonError, match="daemon-reload failed"):
        daemon.install_daemon(home=home, platform="linux", argv=["/usr/bin/python3"],
                              home_dir=tmp_path / "u", env={"XDG_CONFIG_HOME": str(tmp_path / "xdg")},
                              start_now=True, runner=_fake_runner([], rc=1))

    # Missing launchctl/systemctl binary returns exit 1 cleanly
    def _missing(cmd):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    code, msg = daemon.start_daemon(platform="linux", home_dir=tmp_path / "u",
                                    env={"XDG_CONFIG_HOME": str(tmp_path / "xdg")}, runner=_missing)
    assert code == 1 and "cannot run systemctl" in msg


def test_setup_codex_conflicts_broken_symlink_and_multi_path_backup(tmp_path):
    home = tmp_path / "contexto"
    user_home = tmp_path / "user"
    user_home.mkdir()

    # Broken symlink is refused even on --dry-run
    cursor_dir = user_home / ".cursor"
    cursor_dir.mkdir(parents=True)
    (cursor_dir / "mcp.json").symlink_to(tmp_path / "does-not-exist.json")
    with pytest.raises(setup.SetupError, match="symlink"):
        setup.setup_target("cursor", home=home, port=7837, user_home=user_home,
                           env={"HOME": str(user_home)}, dry_run=True)

    # Codex refuses unmanaged [model_providers.contexto] instead of creating duplicate tables
    codex1 = tmp_path / "codex1"
    codex1.mkdir()
    (codex1 / "config.toml").write_text('[model_providers.contexto]\nname = "Mine"\n')
    with pytest.raises(setup.SetupError, match="already defines"):
        setup.setup_target("codex", home=home, port=7837, user_home=user_home,
                           env={"HOME": str(user_home), "CODEX_HOME": str(codex1)})

    # Codex preserves existing [features] table without duplicating it
    codex2 = tmp_path / "codex2"
    codex2.mkdir()
    orig2 = '[features]\nfoo = true\n'
    (codex2 / "config.toml").write_text(orig2)
    notes = setup.setup_target("codex", home=home, port=7837, user_home=user_home,
                               env={"HOME": str(user_home), "CODEX_HOME": str(codex2)})
    import tomllib

    parsed = tomllib.loads((codex2 / "config.toml").read_text())
    assert parsed["features"]["foo"] is True
    assert any("already has [features]" in n for n in notes)

    # Changing CODEX_HOME on a second run backs up the new file too, and --undo restores both
    codex3 = tmp_path / "codex3"
    codex3.mkdir()
    orig3 = 'model = "o3"\n'
    (codex3 / "config.toml").write_text(orig3)
    setup.setup_target("codex", home=home, port=7837, user_home=user_home,
                       env={"HOME": str(user_home), "CODEX_HOME": str(codex3)})
    setup.undo_target("codex", home=home)
    assert (codex2 / "config.toml").read_text() == orig2
    assert (codex3 / "config.toml").read_text() == orig3
