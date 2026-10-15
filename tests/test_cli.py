import subprocess
import sys

import pytest

from contexto.cli import main


def test_main_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "usage: contexto" in capsys.readouterr().out


def test_module_help_subprocess():
    result = subprocess.run(
        [sys.executable, "-m", "contexto", "--help"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "usage: contexto" in result.stdout


def test_no_command_prints_help_and_signals_usage_error(capsys):
    assert main([]) == 2
    assert "usage: contexto" in capsys.readouterr().out


def test_discovers_command_modules(monkeypatch, tmp_path):
    import contexto.commands as commands

    (tmp_path / "hello.py").write_text(
        "def register(subparsers):\n"
        "    p = subparsers.add_parser('hello')\n"
        "    p.set_defaults(func=lambda args: 7)\n"
    )
    monkeypatch.setattr(commands, "__path__", [*commands.__path__, str(tmp_path)])
    monkeypatch.delitem(sys.modules, "contexto.commands.hello", raising=False)
    assert main(["hello"]) == 7
