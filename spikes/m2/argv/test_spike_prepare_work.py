"""prepare_work in the zero-spend spikes (argstub, permstub) deletes only a scratch directory it made."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SPIKES = Path(__file__).resolve().parents[1]


def load(path: Path):
    spec = importlib.util.spec_from_file_location(f"spike_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULES = [load(SPIKES / "argv" / "argstub.py"), load(SPIKES / "permissions" / "permstub.py")]


@pytest.fixture(params=MODULES, ids=lambda m: m.__name__)
def spike(request):
    return request.param


def test_new_directory_is_made_with_the_marker(spike, tmp_path):
    work = spike.prepare_work(tmp_path / "a" / "work")
    assert work.is_dir() and (work / spike.WORK_MARKER).is_file()
    assert [p.name for p in work.iterdir()] == [spike.WORK_MARKER]


def test_empty_directory_is_accepted(spike, tmp_path):
    (tmp_path / "work").mkdir()
    assert (spike.prepare_work(tmp_path / "work") / spike.WORK_MARKER).is_file()


def test_marked_directory_is_emptied(spike, tmp_path):
    work = spike.prepare_work(tmp_path / "work")
    (work / "cfg").mkdir()
    (work / "cfg" / "old.json").write_text("{}")
    again = spike.prepare_work(tmp_path / "work")
    assert again == work and [p.name for p in again.iterdir()] == [spike.WORK_MARKER]


def test_relative_path_is_made_absolute(spike, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    work = spike.prepare_work(Path("rel"))
    assert work.is_absolute() and work == (tmp_path / "rel").resolve()


def test_unmarked_directory_with_files_is_refused(spike, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "keep.txt").write_text("mine")
    with pytest.raises(ValueError, match="not made by this spike"):
        spike.prepare_work(work)
    assert (work / "keep.txt").read_text() == "mine"


def test_file_is_refused(spike, tmp_path):
    path = tmp_path / "work"
    path.write_text("mine")
    with pytest.raises(ValueError):
        spike.prepare_work(path)
    assert path.read_text() == "mine"


@pytest.mark.parametrize("target_kind", ["unmarked_dir", "marked_dir", "empty_dir", "file"])
def test_symlink_is_refused(spike, tmp_path, target_kind):
    target = tmp_path / "target"
    if target_kind == "file":
        target.write_text("mine")
    else:
        target.mkdir()
        if target_kind == "unmarked_dir":
            (target / "keep.txt").write_text("mine")
        elif target_kind == "marked_dir":
            (target / spike.WORK_MARKER).write_text("")
    link = tmp_path / "work"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        spike.prepare_work(link)
    assert link.is_symlink() and target.exists()
    if target_kind == "unmarked_dir":
        assert (target / "keep.txt").read_text() == "mine"
