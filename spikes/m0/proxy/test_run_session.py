"""Tests for the effective base URL resolution (no network, temporary git repos only)."""

from __future__ import annotations

import json
import subprocess

from run_session import claude_settings_order, inherited_claude_base, main_checkout


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@e", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})


def settings(directory, url):
    (directory / ".claude").mkdir(parents=True, exist_ok=True)
    (directory / ".claude" / "settings.local.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": url}}))


def test_worktree_resolves_to_main_checkout_settings_first(tmp_path):
    main, tree, home = tmp_path / "main", tmp_path / "tree", tmp_path / "home"
    main.mkdir()
    home.mkdir()
    git("init", "-q", cwd=main)
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=main)
    git("worktree", "add", "-q", str(tree), cwd=main)
    settings(main, "http://main.invalid")
    settings(tree, "http://tree.invalid")

    assert main_checkout(tree).resolve() == main.resolve()
    assert main_checkout(main) is None
    assert claude_settings_order(tree, home)[0].resolve() == (main / ".claude" / "settings.local.json").resolve()
    assert inherited_claude_base(tree, home) == "http://main.invalid"
    assert inherited_claude_base(main, home) == "http://main.invalid"


def test_plain_directory_uses_its_own_settings_then_env(tmp_path, monkeypatch):
    plain, home = tmp_path / "plain", tmp_path / "home"
    plain.mkdir()
    home.mkdir()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://env.invalid")
    assert main_checkout(plain) is None
    assert inherited_claude_base(plain, home) == "http://env.invalid"
    settings(plain, "http://plain.invalid")
    assert inherited_claude_base(plain, home) == "http://plain.invalid"
