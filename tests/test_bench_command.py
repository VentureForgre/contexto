"""``contexto bench``: mode discovery, the common options and the ``--script-only`` mode (D66)."""

from __future__ import annotations

import argparse
import subprocess
import sys

import pytest

from contexto import cli
from contexto.bench import modes, script
from contexto.bench.model import Script
from contexto.bench.modes import script_only
from contexto.commands import bench

SMALL = ["--bound", "4000", "--turn-kbytes", "2"]


def _mode(tmp_path, monkeypatch, name: str, source: str) -> None:
    """Install a mode module for this test, the way a builder adds one: a file in the modes package."""
    (tmp_path / f"{name}.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(modes, "__path__", [*modes.__path__, str(tmp_path)])
    monkeypatch.delitem(sys.modules, f"contexto.bench.modes.{name}", raising=False)


@pytest.fixture(autouse=True)
def _forget_test_modes():
    yield
    for name in [m for m in sys.modules if m.startswith("contexto.bench.modes.t_")]:
        del sys.modules[name]


# Without a mode


def test_bench_without_a_mode_says_which_modes_this_build_has(capsys):
    assert cli.main(["bench"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    listed = captured.err.strip().removeprefix("contexto bench: error: this build has no live run; the modes it has "
                                               "are ")
    assert listed != captured.err.strip() and "--script-only" in listed.split(", ")  # other builders add modes


def test_bench_is_listed_and_documents_its_options(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["bench", "--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    for option in ("--seed BASE", "--seeds N", "--repeats R", "--bound TOKENS", "--protocol {d27,v2}",
                   "--turn-kbytes KB", "--out DIR", "--script-only", "script-only mode"):
        assert option in text, option
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "bench" in capsys.readouterr().out


def test_common_option_defaults():
    args = cli.build_parser().parse_args(["bench"])
    assert (args.seed, args.seeds, args.repeats, args.bound, args.protocol, args.turn_kbytes, args.out) == (
        20261016, 1, 1, 150_000, "v2", 40, None)
    assert modes.seeds(args) == [20261016]
    args = cli.build_parser().parse_args(["bench", "--seed", "7", "--seeds", "3"])
    assert modes.seeds(args) == [7, 8, 9]


@pytest.mark.parametrize("argv", [
    ["--seeds", "0"], ["--seeds", "-1"], ["--repeats", "0"], ["--bound", "0"], ["--bound", "1e5"],
    ["--turn-kbytes", "0"], ["--seed", "-3"], ["--seed", "x"], ["--protocol", "v3"],
])
def test_bad_common_options_are_usage_errors(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["bench", *argv])
    assert exc.value.code == 2
    assert "usage: contexto bench" in capsys.readouterr().err


def test_turn_kbytes_below_the_floor_is_refused(tmp_path, capsys):
    # At the default bound, 1,000-byte turns would need about 652 content turns (the generator builds 688).
    assert cli.main(["bench", "--script-only", "--out", str(tmp_path), "--turn-kbytes", "1"]) == 2
    err = capsys.readouterr().err
    assert ("contexto bench: error: --turn-kbytes 1 at --bound 150,000 needs about 652 content turns, more "
            "than the 400 a script may have; use --turn-kbytes 2 or more, or a smaller bound") in err
    assert not (tmp_path / "scripts").exists()
    assert cli.main(["bench", "--script-only", "--out", str(tmp_path), "--turn-kbytes", "1", "--bound", "4000"]) == 0
    with pytest.raises(SystemExit):
        cli.main(["bench", "--help"])
    assert "more than 400 content turns" in " ".join(capsys.readouterr().out.split())


def test_contexto_does_not_import_the_generator_or_statistics_at_start():
    code = ("import sys\nfrom contexto import cli\ncli.build_parser()\n"
            "print(sorted(m for m in sys.modules if m.startswith('contexto.bench.') and m not in "
            "('contexto.bench.model', 'contexto.bench.modes', 'contexto.bench.modes.script_only')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_protocol_help_says_the_service_is_named(capsys):
    with pytest.raises(SystemExit):
        cli.main(["bench", "--help"])
    flat = " ".join(capsys.readouterr().out.split())
    assert "do not name their subject" not in flat
    assert "name the service but not the attribute" in flat


# --script-only


def test_script_only_writes_one_script_per_seed(tmp_path, capsys):
    out = tmp_path / "out"
    assert cli.main(["bench", "--script-only", "--out", str(out), "--seed", "41", "--seeds", "2", *SMALL]) == 0
    assert sorted(p.name for p in (out / "scripts").iterdir()) == ["seed-41.json", "seed-42.json"]
    for seed in (41, 42):
        written = Script.read(script_only.script_path(out, seed))
        assert written == script.build(seed, "v2", bound=4_000, turn_bytes=2_000)
    captured = capsys.readouterr()
    text = captured.out
    assert captured.err == ""
    first = Script.read(out / "scripts" / "seed-41.json")
    sizes = script.estimate(first)
    assert "seed 41: protocol v2, bound 4,000 tokens" in text and "seed 42: protocol v2" in text
    assert f"  turns: {sizes['turns']} ({sizes['content_turns']} content, 8 probe)" in text
    assert f"  bytes: {sizes['bytes']:,} (of which files the tool-loop turn reads: {sizes['file_bytes']:,})" in text
    assert f"  token estimate: {sizes['tokens']:,} (content turns {sizes['history_tokens']:,}" in text
    assert "  probes: 72" in text
    for cls in ("paged_only", "multihop", "chain", "obligations", "distractor", "paraphrase", "concluded", "toolloop"):
        assert f"    {cls}: " in text
    assert "    concluded: 6 probes, mean lexical overlap 0.00 words" in text
    for check in ("restatements", "key_free", "service_alone", "placement", "unplanted"):
        assert text.count(f"  check {check}: pass") == 2
    assert f"(sha256 {first.digest()})" in text
    assert "FAIL" not in text


def test_script_only_writes_the_d27_protocol_when_asked(tmp_path, capsys):
    assert cli.main(["bench", "--script-only", "--out", str(tmp_path), "--protocol", "d27", *SMALL]) == 0
    written = Script.read(tmp_path / "scripts" / "seed-20261016.json")
    assert written.protocol == "d27" and len(written.probes) == 54 and written.files == {}
    assert written == script.build(20261016, "d27", bound=4_000, turn_bytes=2_000)
    text = capsys.readouterr().out
    assert "probes: 54" in text and "toolloop" not in text


def test_script_only_is_reproducible(tmp_path):
    for name in ("a", "b"):
        assert cli.main(["bench", "--script-only", "--out", str(tmp_path / name), *SMALL]) == 0
    a, b = (tmp_path / name / "scripts" / "seed-20261016.json" for name in ("a", "b"))
    assert a.read_bytes() == b.read_bytes()
    assert cli.main(["bench", "--script-only", "--out", str(tmp_path / "a"), *SMALL]) == 0  # overwrites
    assert a.read_bytes() == b.read_bytes()


def test_script_only_exits_1_when_a_self_check_fails(tmp_path, capsys, monkeypatch):
    real = script.checks

    def checks(s):
        return {**real(s), "restatements": [f"q_x_{k:02d}: 'thing' restated in turn 3" for k in range(12)]}

    monkeypatch.setattr(script, "checks", checks)
    assert cli.main(["bench", "--script-only", "--out", str(tmp_path), *SMALL]) == 1
    text = capsys.readouterr().out
    assert "  check restatements: FAIL (12)" in text and "  check key_free: pass" in text
    assert "    q_x_00: 'thing' restated in turn 3" in text and "    and 2 more" in text
    assert (tmp_path / "scripts" / "seed-20261016.json").is_file()  # written, so the failure can be inspected


def test_script_only_needs_an_output_directory(capsys):
    assert cli.main(["bench", "--script-only", *SMALL]) == 2
    assert "--script-only needs --out DIR" in capsys.readouterr().err


def test_script_only_reports_a_script_it_cannot_build_or_write(tmp_path, capsys):
    # 4,000-byte turns at a 150,000-token bound still reach 2.5 times the bound, so v2 fits; the
    # generator refuses only a d27 part too short to follow the new plants, which --script-only never asks for.
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    assert cli.main(["bench", "--script-only", "--out", str(blocker), *SMALL]) == 1
    assert "cannot write" in capsys.readouterr().err

    def refuse(*args, **kwargs):
        raise ValueError("the script is too short for protocol v2")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(script, "build", refuse)
        assert cli.main(["bench", "--script-only", "--out", str(tmp_path / "x"), *SMALL]) == 2
    assert "seed 20261016: the script is too short for protocol v2" in capsys.readouterr().err


def test_python_dash_m_runs_script_only(tmp_path):
    out = subprocess.run([sys.executable, "-m", "contexto", "bench", "--script-only", "--out", str(tmp_path), *SMALL],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "check placement: pass" in out.stdout
    assert Script.read(tmp_path / "scripts" / "seed-20261016.json").seed == 20261016


# Mode discovery


FLAG_MODE = (
    "FLAG = '--t-hello'\n"
    "def add_arguments(parser):\n"
    "    parser.add_argument(FLAG, metavar='NAME', default=None, help='say hello')\n"
    "def run(args):\n"
    "    print('hello', args.t_hello, args.seed, args.protocol)\n"
    "    return 7\n"
)
DEFAULT_MODE = (
    "from contexto.bench.modes import add_option\n"
    "FLAG = None\n"
    "def add_arguments(parser):\n"
    "    add_option(parser, '--t-model', default='m-1')\n"
    "def run(args):\n"
    "    print('live', args.t_model, args.bound)\n"
    "    return 0\n"
)


def test_a_mode_is_added_by_adding_a_module(tmp_path, monkeypatch, capsys):
    _mode(tmp_path, monkeypatch, "t_hello", FLAG_MODE)
    (tmp_path / "_private.py").write_text("raise RuntimeError('never imported')\n", encoding="utf-8")
    (tmp_path / "t_notes.py").write_text("NOT_A_MODE = True\n", encoding="utf-8")  # no FLAG: skipped
    found = [bench.mode_name(m) for m in bench.discover()]
    assert found == sorted(found) and {"script-only", "t-hello"} <= set(found) and "t-notes" not in found
    assert cli.main(["bench", "--t-hello", "you", "--seed", "5"]) == 7
    assert capsys.readouterr().out == "hello you 5 v2\n"
    with pytest.raises(SystemExit):
        cli.main(["bench", "--help"])
    assert "t-hello mode" in capsys.readouterr().out
    assert cli.main(["bench"]) == 2
    assert "--script-only, --t-hello" in capsys.readouterr().err


def test_the_default_mode_runs_when_no_mode_flag_is_given(tmp_path, monkeypatch, capsys):
    _mode(tmp_path, monkeypatch, "t_live", DEFAULT_MODE)
    assert cli.main(["bench", "--bound", "9000"]) == 0
    assert capsys.readouterr().out == "live m-1 9000\n"
    # A mode flag still selects its mode, not the default.
    assert cli.main(["bench", "--script-only"]) == 2
    assert "--script-only needs --out DIR" in capsys.readouterr().err


def test_exactly_one_mode_runs(tmp_path, monkeypatch, capsys):
    _mode(tmp_path, monkeypatch, "t_hello", FLAG_MODE)
    assert cli.main(["bench", "--script-only", "--t-hello", "you", "--out", str(tmp_path / "out")]) == 2
    captured = capsys.readouterr()
    assert captured.err.strip() == "contexto bench: error: give one mode, not --script-only and --t-hello"
    assert captured.out == "" and not (tmp_path / "out").exists()


OWN_FLAG_MODE = (
    "FLAG = '--t-replay'\n"
    "OWN_OPTIONS = ('--t-dump',)\n"
    "def add_arguments(parser):\n"
    "    parser.add_argument(FLAG, action='store_true')\n"
    "    parser.add_argument('--t-dump', metavar='DIR', default=None)\n"
    "def run(args):\n"
    "    print('replay', args.t_dump)\n"
    "    return 0\n"
)
OWN_DEFAULT_MODE = (
    "from contexto.bench.modes import add_option\n"
    "FLAG = None\n"
    "OWN_OPTIONS = ('--t-key-helper',)\n"
    "def add_arguments(parser):\n"
    "    add_option(parser, '--t-model', default='m-1')\n"
    "    parser.add_argument('--t-key-helper', default=None)\n"
    "def run(args):\n"
    "    print('live', args.t_key_helper)\n"
    "    return 0\n"
)


def test_a_modes_own_option_is_refused_without_its_mode(tmp_path, monkeypatch, capsys):
    _mode(tmp_path, monkeypatch, "t_live", OWN_DEFAULT_MODE)
    (tmp_path / "t_replay.py").write_text(OWN_FLAG_MODE, encoding="utf-8")
    # The replay mode's option without --t-replay would otherwise start the live run.
    assert cli.main(["bench", "--t-dump", "out"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == ("contexto bench: error: --t-dump belongs to the t-replay mode; give "
                                    "--t-replay with it, or leave it out")
    assert cli.main(["bench", "--t-replay", "--t-dump", "out"]) == 0
    assert capsys.readouterr().out == "replay out\n"
    # The live run's own option with another mode selected.
    assert cli.main(["bench", "--t-replay", "--t-key-helper", "cmd"]) == 2
    assert capsys.readouterr().err.strip() == (
        "contexto bench: error: --t-key-helper belongs to the live run (t-live), which runs only when no mode "
        "option is given; leave it out with --t-replay")
    assert cli.main(["bench", "--t-key-helper", "cmd"]) == 0
    assert capsys.readouterr().out == "live cmd\n"
    # A shared option (add_option) belongs to no one mode, so it is allowed with either.
    assert cli.main(["bench", "--t-replay", "--t-model", "m-2"]) == 0


def test_a_mode_that_lists_an_option_it_does_not_add_is_refused(tmp_path, monkeypatch, capsys):
    _mode(tmp_path, monkeypatch, "t_odd", OWN_FLAG_MODE.replace("OWN_OPTIONS = ('--t-dump',)",
                                                               "OWN_OPTIONS = ('--t-dump', '--t-nope')"))
    assert cli.main(["bench", "--t-replay"]) == 2
    assert "mode t-odd lists --t-nope in OWN_OPTIONS but adds no such option" in capsys.readouterr().err


def test_two_default_modes_are_refused(tmp_path, monkeypatch, capsys):
    _mode(tmp_path, monkeypatch, "t_live", DEFAULT_MODE)
    (tmp_path / "t_other.py").write_text(DEFAULT_MODE, encoding="utf-8")
    assert cli.main(["bench"]) == 2
    assert "more than one default mode is installed: t-live, t-other" in capsys.readouterr().err


def test_add_option_lets_two_modes_share_an_option():
    parser = argparse.ArgumentParser()
    first, second = parser.add_argument_group("one"), parser.add_argument_group("two")
    modes.add_option(first, "--model", default="a", help="the first definition")
    modes.add_option(second, "--model", default="b", help="ignored")
    modes.add_option(second, "--prices", default=None)
    args = parser.parse_args([])
    assert args.model == "a" and args.prices is None
    with pytest.raises(argparse.ArgumentError):  # what add_option avoids: argparse refuses the second definition
        second.add_argument("--model")
