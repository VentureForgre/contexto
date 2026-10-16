import subprocess
import sys

import pytest

import contexto
from contexto import cli, commands


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"contexto {contexto.__version__}"


def test_no_subcommand_prints_help_and_exits_2(capsys):
    assert cli.main([]) == 2
    assert "usage: contexto" in capsys.readouterr().out


def test_subcommands_are_discovered_from_the_commands_package(tmp_path, monkeypatch):
    (tmp_path / "hello.py").write_text(
        "def register(sub):\n"
        "    p = sub.add_parser('hello')\n"
        "    p.add_argument('name')\n"
        "    p.set_defaults(func=lambda a: 7 if a.name == 'x' else 0)\n"
    )
    (tmp_path / "_private.py").write_text("raise RuntimeError('never imported')\n")
    monkeypatch.setattr(commands, "__path__", [*commands.__path__, str(tmp_path)])
    assert cli.main(["hello", "x"]) == 7
    sys.modules.pop("contexto.commands.hello", None)


def test_python_dash_m_runs_the_cli():
    out = subprocess.run([sys.executable, "-m", "contexto", "--version"], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.startswith("contexto ")
