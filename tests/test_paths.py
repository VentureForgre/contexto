from pathlib import Path

import pytest

from contexto.paths import contexto_home, session_dir


def test_home_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTEXTO_HOME", str(tmp_path / "h"))
    assert contexto_home() == tmp_path / "h"


def test_home_default(monkeypatch):
    monkeypatch.delenv("CONTEXTO_HOME", raising=False)
    assert contexto_home() == Path.home() / ".contexto"


def test_session_dir(tmp_path):
    assert session_dir(tmp_path, "abc-1") == tmp_path / "sessions" / "abc-1"


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a\\b", "x\0"])
def test_session_dir_rejects_unsafe_ids(tmp_path, bad):
    with pytest.raises(ValueError):
        session_dir(tmp_path, bad)
