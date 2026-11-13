"""Upstream resolution (M2 contract, PR #20 head bf1c7ad, D52): flag, settings, env, default."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from contexto.upstream import (
    DEFAULT_UPSTREAM,
    claude_key_helper,
    claude_settings_order,
    main_checkout,
    resolve_upstream,
)

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
    assert main_checkout(main).resolve() == main.resolve()  # a repository is its own main checkout
    kinds = [kind for kind, _ in claude_settings_order(tree, home)]
    assert kinds == ["main-checkout-local", "local", "project", "user"]
    up = resolve_upstream(env={"ANTHROPIC_BASE_URL": "http://env.invalid"}, cwd=tree, home=home)
    assert (up.url, up.source) == ("http://main.invalid", "settings:main-checkout-local")


def test_worktree_subdirectory_reads_the_main_checkout_and_its_own_files_not_the_worktree_root(
        repo_with_worktree, home):
    # D52, measured with Claude Code 2.1.314: from <worktree>/a/b neither of the worktree root's
    # files is read; the main checkout's local file and a/b's own files are.
    main, tree = repo_with_worktree
    sub = tree / "a" / "b"
    sub.mkdir(parents=True)
    write_settings(tree / ".claude" / "settings.local.json", "http://tree.invalid")
    assert resolve_upstream(env={}, cwd=sub, home=home).source == "default"
    write_settings(sub / ".claude" / "settings.json", "http://sub.invalid")
    assert resolve_upstream(env={}, cwd=sub, home=home).url == "http://sub.invalid"
    write_settings(main / ".claude" / "settings.local.json", "http://main.invalid")
    up = resolve_upstream(env={}, cwd=sub, home=home)
    assert (up.url, up.source) == ("http://main.invalid", "settings:main-checkout-local")


def test_main_checkout_itself_has_no_extra_layer(repo_with_worktree, home):
    main, _ = repo_with_worktree
    assert [kind for kind, _ in claude_settings_order(main, home)] == ["local", "project", "user"]
    sub = main / "sub"
    sub.mkdir()
    assert claude_settings_order(sub, home)[0] == ("main-checkout-local", main / ".claude" / "settings.local.json")


def test_home_directory_settings_are_listed_once_as_user(home):
    # From the home directory, the project settings file is the user settings file; listed as the
    # user's so the Codex trust gate (D52) never drops the user's own apiKeyHelper.
    assert [kind for kind, _ in claude_settings_order(home, home)] == ["local", "user"]


# D52: real Claude Code 2.1.314, `claude -p` with a fresh CLAUDE_CONFIG_DIR against stub upstreams
# (one port per file, the process environment on another), 27 layouts; the two not listed here
# repeat two below with the root's settings.local.json committed, same result. Each entry: the
# working directory, the files that set env.ANTHROPIC_BASE_URL, and the file whose URL Claude Code
# used ("env" when none). Before D52 Contexto read the git toplevel's files instead of the working
# directory's and got 8 of these wrong (marked *).
LAYOUTS = [
    ("repo", ["repo/.claude/settings.json"], "repo/.claude/settings.json"),
    ("repo/sub", ["repo/.claude/settings.json"], "env"),  # *
    ("repo/sub", ["repo/sub/.claude/settings.json", "repo/.claude/settings.json"],
     "repo/sub/.claude/settings.json"),  # *
    ("repo/sub/deeper", ["repo/.claude/settings.json"], "env"),  # *
    ("repo/sub", ["repo/.claude/settings.local.json"], "repo/.claude/settings.local.json"),
    ("repo/sub", ["repo/sub/.claude/settings.json", "repo/.claude/settings.local.json"],
     "repo/.claude/settings.local.json"),
    ("repo/sub", ["repo/sub/.claude/settings.local.json", "repo/.claude/settings.json"],
     "repo/sub/.claude/settings.local.json"),  # *
    ("repo/sub", ["repo/sub/.claude/settings.local.json", "repo/.claude/settings.local.json"],
     "repo/.claude/settings.local.json"),
    ("repo/sub", ["repo/sub/.claude/settings.json", "repo/sub/.claude/settings.local.json"],
     "repo/sub/.claude/settings.local.json"),  # *
    ("repo/sub", ["user"], "user"),
    ("repo/sub", ["user", "repo/sub/.claude/settings.json"], "repo/sub/.claude/settings.json"),  # *
    ("repo/sub/deeper", ["repo/sub/.claude/settings.local.json"], "env"),
    ("repo/sub/deeper", ["repo/.claude/settings.local.json"], "repo/.claude/settings.local.json"),
    ("repo", ["repo/.claude/settings.json", "repo/.claude/settings.local.json"], "repo/.claude/settings.local.json"),
    ("plain", ["plain/.claude/settings.json"], "plain/.claude/settings.json"),
    ("plain/child", ["plain/.claude/settings.json"], "env"),
    ("plain/child", ["plain/.claude/settings.local.json"], "env"),
    ("wt", ["repo/.claude/settings.local.json"], "repo/.claude/settings.local.json"),
    ("wt", ["repo/.claude/settings.local.json", "wt/.claude/settings.local.json"], "repo/.claude/settings.local.json"),
    ("wt/sub", ["wt/.claude/settings.json"], "env"),  # *
    ("wt/sub", ["wt/.claude/settings.local.json"], "env"),  # *
    ("wt", ["wt/.claude/settings.json", "repo/.claude/settings.json"], "wt/.claude/settings.json"),
    ("wt/sub", ["repo/.claude/settings.local.json", "wt/sub/.claude/settings.json"],
     "repo/.claude/settings.local.json"),
    ("repo/sm", ["repo/.claude/settings.local.json"], "env"),
    ("repo/sm/sub", ["repo/sm/.claude/settings.local.json"], "repo/sm/.claude/settings.local.json"),
]


@pytest.fixture
def layouts(tmp_path: Path) -> Path:
    """repo (git, with sub/deeper and a submodule sm with sub), wt (a linked worktree of repo, with
    sub), and plain/child (no git), under one root."""
    root = tmp_path / "layouts"
    repo, lib = root / "repo", root / "lib"
    (repo / "sub" / "deeper").mkdir(parents=True)
    (lib / "sub").mkdir(parents=True)
    (lib / "sub" / ".keep").write_text("")
    git("init", "-q", cwd=lib)
    git("add", ".", cwd=lib)
    git("commit", "-q", "-m", "lib", cwd=lib)
    git("init", "-q", cwd=repo)
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), "sm", cwd=repo)
    git("worktree", "add", "-q", str(root / "wt"), cwd=repo)
    (root / "wt" / "sub").mkdir()
    (root / "plain" / "child").mkdir(parents=True)
    return root


@pytest.mark.parametrize(("cwd", "files", "used"), LAYOUTS, ids=[f"{c}:{','.join(f)}" for c, f, _ in LAYOUTS])
def test_settings_files_match_claude_code_per_layout(layouts, home, cwd, files, used):
    config_dir = layouts.parent / "cfg"
    for name in files:
        path = config_dir / "settings.json" if name == "user" else layouts / name
        write_settings(path, f"http://{name.replace('/', '-').replace('.', '-')}.invalid")
    env = {"ANTHROPIC_BASE_URL": "http://env.invalid", "CLAUDE_CONFIG_DIR": str(config_dir)}
    up = resolve_upstream(env=env, cwd=layouts / cwd, home=home)
    assert up.url == f"http://{used.replace('/', '-').replace('.', '-')}.invalid"


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


BOM = b"\xef\xbb\xbf"


@pytest.mark.parametrize("where", ["project", "user"])
def test_a_settings_file_that_starts_with_a_byte_order_mark_is_read(tmp_path, home, where):
    """Claude Code reads such a file (measured on 2.1.322 with plain ``claude -p`` and the project's
    file, spikes/m2/providers: every request went to the file's base URL, and the file's
    ``apiKeyHelper`` ran; the user's file goes through the same reader here). Read as plain UTF-8
    it was not JSON and counted as absent, so the proxy forwarded to another upstream than Claude Code's
    own, and ``contexto run codex`` did not find the helper."""
    path = (tmp_path if where == "project" else home) / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(BOM + json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://marked.invalid"},
                                       "apiKeyHelper": "print-a-key"}).encode())
    up = resolve_upstream(env={"ANTHROPIC_BASE_URL": "http://env.invalid"}, cwd=tmp_path, home=home)
    assert (up.url, up.source) == ("http://marked.invalid", f"settings:{where}")
    helper = claude_key_helper(tmp_path, home, {}, trusted_only=False)
    assert (helper.value, helper.source) == ("print-a-key", where)

