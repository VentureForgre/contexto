"""Upstream resolution (M2 contract, PR #20 head bf1c7ad): flag, settings, env, default."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from contexto.upstream import DEFAULT_UPSTREAM, claude_settings_order, main_checkout, resolve_upstream

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": "/dev/null",
           "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=GIT_ENV)


def write_settings(path: Path, url: str | None, **extra) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dict(extra)
    if url is not None:
        data["env"] = {"ANTHROPIC_BASE_URL": url}
    path.write_text(json.dumps(data))


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    main, tree = tmp_path / "main", tmp_path / "tree"
    main.mkdir()
    git("init", "-q", cwd=main)
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=main)
    git("worktree", "add", "-q", str(tree), cwd=main)
    return main, tree


def test_explicit_flag_wins_over_everything(tmp_path, home):
    write_settings(tmp_path / ".claude" / "settings.local.json", "http://local.invalid")
    up = resolve_upstream("http://flag.invalid", env={"ANTHROPIC_BASE_URL": "http://env.invalid"}, cwd=tmp_path,
                          home=home)
    assert (up.url, up.source) == ("http://flag.invalid", "flag")


def test_settings_beat_process_env(tmp_path, home):
    # Measured in M0 (PR #16 finding 1): a settings-file value overrides the process environment.
    write_settings(home / ".claude" / "settings.json", "http://user.invalid")
    up = resolve_upstream(env={"ANTHROPIC_BASE_URL": "http://env.invalid"}, cwd=tmp_path, home=home)
    assert (up.url, up.source) == ("http://user.invalid", "settings:user")


def test_settings_precedence_local_then_project_then_user(tmp_path, home):
    write_settings(home / ".claude" / "settings.json", "http://user.invalid")
    write_settings(tmp_path / ".claude" / "settings.json", "http://project.invalid")
    assert resolve_upstream(env={}, cwd=tmp_path, home=home).source == "settings:project"
    write_settings(tmp_path / ".claude" / "settings.local.json", "http://local.invalid")
    up = resolve_upstream(env={}, cwd=tmp_path, home=home)
    assert (up.url, up.source) == ("http://local.invalid", "settings:local")


def test_settings_without_base_url_or_unreadable_are_skipped(tmp_path, home):
    write_settings(tmp_path / ".claude" / "settings.local.json", None, model="x")
    (tmp_path / ".claude" / "settings.json").write_text("{not json")
    write_settings(home / ".claude" / "settings.json", "  ")
    up = resolve_upstream(env={"ANTHROPIC_BASE_URL": "http://env.invalid"}, cwd=tmp_path, home=home)
    assert (up.url, up.source) == ("http://env.invalid", "env")


def test_default_when_nothing_is_set(tmp_path, home):
    up = resolve_upstream(env={}, cwd=tmp_path, home=home)
    assert (up.url, up.source) == (DEFAULT_UPSTREAM, "default")


def test_linked_worktree_reads_main_checkout_local_settings_first(repo_with_worktree, home):
    main, tree = repo_with_worktree
    write_settings(main / ".claude" / "settings.local.json", "http://main.invalid")
    write_settings(tree / ".claude" / "settings.local.json", "http://tree.invalid")
    assert main_checkout(tree).resolve() == main.resolve()
    assert main_checkout(main) is None
    kinds = [kind for kind, _ in claude_settings_order(tree, home)]
    assert kinds == ["main-checkout-local", "local", "project", "user"]
    up = resolve_upstream(env={"ANTHROPIC_BASE_URL": "http://env.invalid"}, cwd=tree, home=home)
    assert (up.url, up.source) == ("http://main.invalid", "settings:main-checkout-local")


def test_worktree_subdirectory_uses_its_toplevel(repo_with_worktree, home):
    main, tree = repo_with_worktree
    sub = tree / "a" / "b"
    sub.mkdir(parents=True)
    write_settings(tree / ".claude" / "settings.local.json", "http://tree.invalid")
    up = resolve_upstream(env={}, cwd=sub, home=home)
    assert (up.url, up.source) == ("http://tree.invalid", "settings:local")


def test_main_checkout_itself_has_no_extra_layer(repo_with_worktree, home):
    main, _ = repo_with_worktree
    assert [kind for kind, _ in claude_settings_order(main, home)] == ["local", "project", "user"]


def test_source_label_never_contains_url_or_path(repo_with_worktree, home):
    main, tree = repo_with_worktree
    write_settings(main / ".claude" / "settings.local.json", "http://secret-host.invalid")
    up = resolve_upstream(env={}, cwd=tree, home=home)
    assert "secret-host" not in up.source and str(main) not in up.source


def test_claude_config_dir_replaces_the_user_settings_file(tmp_path, home):
    # Claude Code reads its user settings from $CLAUDE_CONFIG_DIR when that is set (D35, reviewer-B).
    config_dir = tmp_path / "claude-config"
    write_settings(config_dir / "settings.json", "http://configdir.invalid", apiKeyHelper="/from/config/dir")
    write_settings(home / ".claude" / "settings.json", "http://home.invalid")
    env = {"CLAUDE_CONFIG_DIR": str(config_dir)}
    up = resolve_upstream(env=env, cwd=tmp_path, home=home)
    assert (up.url, up.source) == ("http://configdir.invalid", "settings:user")
    assert resolve_upstream(env={}, cwd=tmp_path, home=home).url == "http://home.invalid"
    assert resolve_upstream(env={"CLAUDE_CONFIG_DIR": "  "}, cwd=tmp_path, home=home).url == "http://home.invalid"
    from contexto.codex import claude_api_key_helper

    assert claude_api_key_helper(tmp_path, home, env) == "/from/config/dir"
    assert claude_api_key_helper(tmp_path, home) is None  # no env: no override, and home has no helper


def test_claude_config_dir_ranks_below_project_settings(tmp_path, home):
    config_dir = tmp_path / "claude-config"
    write_settings(config_dir / "settings.json", "http://configdir.invalid")
    write_settings(tmp_path / ".claude" / "settings.json", "http://project.invalid")
    up = resolve_upstream(env={"CLAUDE_CONFIG_DIR": str(config_dir)}, cwd=tmp_path, home=home)
    assert (up.url, up.source) == ("http://project.invalid", "settings:project")
    order = claude_settings_order(tmp_path, home, {"CLAUDE_CONFIG_DIR": str(config_dir)})
    assert order[-1] == ("user", config_dir / "settings.json")


@pytest.mark.parametrize("value", ["~/cfg", "~nosuchuser-contexto-test/cfg"])
def test_claude_config_dir_is_used_as_given_without_tilde_expansion(tmp_path, home, monkeypatch, value):
    # Claude Code does not expand ~ in CLAUDE_CONFIG_DIR (reviewer-A, round 2); expanding it read a
    # different file and made an unknown ~user raise out of `contexto run`.
    monkeypatch.chdir(tmp_path)
    env = {"CLAUDE_CONFIG_DIR": value}
    assert claude_settings_order(tmp_path, home, env)[-1] == ("user", Path(value) / "settings.json")
    write_settings(home / ".claude" / "settings.json", "http://home.invalid")
    assert resolve_upstream(env=env, cwd=tmp_path, home=home).source == "default"  # ./~... has no file
    write_settings(tmp_path / value / "settings.json", "http://literal.invalid", apiKeyHelper="/literal")
    up = resolve_upstream(env=env, cwd=tmp_path, home=home)
    assert (up.url, up.source) == ("http://literal.invalid", "settings:user")
    from contexto.codex import claude_api_key_helper

    assert claude_api_key_helper(tmp_path, home, env) == "/literal"


@pytest.mark.parametrize("make", ["directory", "bad-json", "deep-json", "not-utf8"])
def test_an_unreadable_user_settings_file_is_skipped(tmp_path, home, make):
    config_dir = tmp_path / "cfg"
    target = config_dir / "settings.json"
    config_dir.mkdir()
    if make == "directory":
        target.mkdir()
    elif make == "bad-json":
        target.write_text("{not json")
    elif make == "deep-json":
        target.write_text("[" * 100_000 + "]" * 100_000)
    else:
        target.write_bytes(b"\xff\xfe{")
    env = {"CLAUDE_CONFIG_DIR": str(config_dir), "ANTHROPIC_BASE_URL": "http://env.invalid"}
    assert resolve_upstream(env=env, cwd=tmp_path, home=home).source == "env"
    from contexto.codex import claude_api_key_helper

    assert claude_api_key_helper(tmp_path, home, env) is None
