"""The live path after the aborted 07:08Z run (intake, 2026-10-16 07:11Z): one spend estimate per turn,
the H1 record on any exception, and one H1 function shared by live and the stub dry run.

Fixtures are synthetic: no content from a real session is committed.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from acceptance import budget, driver, run, script, stubrun  # noqa: E402

INPUT = 10_000  # each fake request's input tokens; output 5


class FakeTransport:
    """Stands in for StreamJsonTransport: answers each turn and logs it as the proxy would."""

    made: list[FakeTransport] = []

    def __init__(self, command, cwd, env, session_log=None, approved=None, reserve=0):
        self.command, self.env, self.session_log = command, env, session_log
        self.approved, self.reserve = approved, reserve
        self.watcher = object()
        self.compactions = 0
        self.sent = 0
        FakeTransport.made.append(self)

    def send(self, text):
        self.sent += 1
        self.session_log.parent.mkdir(parents=True, exist_ok=True)
        with self.session_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"event": "request", "method": "POST", "path": "/v1/messages", "status": 200,
                                "usage": {"input_tokens": INPUT, "output_tokens": 5}}) + "\n")
        return {"type": "result", "result": f"answer {self.sent}", "is_error": False,
                "usage": {"input_tokens": INPUT, "output_tokens": 5}}

    def close(self):
        return 0


@pytest.fixture
def live(tmp_path, monkeypatch):
    FakeTransport.made = []
    monkeypatch.setattr(driver, "StreamJsonTransport", FakeTransport)
    monkeypatch.setattr(driver, "isolated_config", lambda scratch, seat: scratch / "config")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://owner-proxy.invalid")
    seat = tmp_path / "settings.json"
    seat.write_text("{}", encoding="utf-8")
    p = budget.Params()
    s, b = run.plan(params=p)
    home = tmp_path / "home"

    def go(inject=None, approved=None):
        return run.run_h1(s, b, p, approved or b.h1_worst + b.baseline_replay_worst, tmp_path / "scratch", home,
                          "sess", seat, "http://127.0.0.1:9", "/venv/bin/contexto", inject=inject)

    def record():
        return json.loads((home / "acceptance-sess-h1.json").read_text(encoding="utf-8"))

    def answers(partial=False):
        full, part = home / "acceptance-sess-answers.txt", home / "acceptance-sess-answers.partial.txt"
        assert (part if partial else full).exists() and not (full if partial else part).exists()
        return (part if partial else full).read_text(encoding="utf-8").splitlines()

    return s, b, p, go, record, answers


def test_turn_estimates_give_every_d27_turn_its_largest_request():
    for kb in (40, 70):
        s, b = run.plan(turn_bytes=kb * 1_000)
        est = budget.turn_estimates(b)
        assert len(est) == len(s.turns) == s.n_content + script.PROBE_TURNS
        for k in range(1, len(s.turns) + 1):
            assert est[k - 1] == max(r.input_tokens for r in b.rows if r.turn == k)
        # The aborted run's list: content turns plus ONE probe entry, short by five.
        old = [r.input_tokens for r in b.rows if r.kind == "turn"] + [max(r.input_tokens for r in b.rows if r.kind != "turn")]
        assert len(old) == len(s.turns) - (script.PROBE_TURNS - 1)


def test_a_clean_run_sends_every_turn_and_keeps_all_probe_answers(live):
    s, b, p, go, record, answers = live
    assert go() == 0
    t = FakeTransport.made[0]
    assert t.sent == len(s.turns) and t.approved == b.h1_worst  # H1's cap: the approval minus the baseline's worst
    assert "ANTHROPIC_BASE_URL" not in t.env  # the launcher's proxy is the only way out
    assert t.command[0] == "/venv/bin/contexto" and "http://127.0.0.1:9" in t.command
    r = record()
    assert r["turns_completed"] == len(s.turns) and r["probe_turns_completed"] == script.PROBE_TURNS
    assert r["stopped"] is None and r["spent_logged"] == len(s.turns) * (INPUT + 5) and r["in_flight_allowance"] == 0
    assert len(answers()) == script.PROBE_TURNS
    assert set(r["paths"]) == set(run.LIVE_PATHS) - {"stop_handled", "error_handled"}


def test_a_harness_error_at_a_probe_turn_writes_the_record_then_reraises(live):
    s, b, p, go, record, answers = live
    at = s.n_content + 1  # after probe turn 2, the turn the 07:08Z run never reached

    def boom(i):
        if i == at:
            raise KeyError("injected")

    with pytest.raises(KeyError):
        go(inject=boom)
    r = record()
    assert r["turns_completed"] == at + 1 and r["probe_turns_completed"] == 2
    assert r["stopped"].startswith("KeyError") and "error_handled" in r["paths"] and "record_written" in r["paths"]
    assert r["spent_logged"] == (at + 1) * (INPUT + 5)
    assert r["in_flight_allowance"] == INPUT + p.max_tokens_reserve  # abnormal: one more of the last size
    assert r["spent"] == r["spent_logged"] + r["in_flight_allowance"]
    # The finished probe turns are kept, under the partial name only (reviewer-2 on PR #123)
    assert answers(partial=True) == [f"answer {s.n_content + 1}", f"answer {s.n_content + 2}"]
    assert r["answers_file"] == "acceptance-sess-answers.partial.txt"


def test_a_guard_stop_is_handled_and_recorded(live):
    s, b, p, go, record, answers = live

    def stop(i):
        if i == s.n_content:
            raise driver.BudgetExceeded("injected")

    assert go(inject=stop) == 1
    r = record()
    assert "stop_handled" in r["paths"] and "error_handled" not in r["paths"] and r["probe_turns_completed"] == 1
    assert len(answers(partial=True)) == 1


def test_the_inject_hook_runs_only_against_a_loopback_upstream(live, tmp_path):
    s, b, p, go, record, answers = live
    seat = tmp_path / "settings.json"
    for upstream in ("https://owner-proxy.invalid", "http://10.0.0.5:8080"):
        assert run.run_h1(s, b, p, b.h1_worst + b.baseline_replay_worst, tmp_path / "scratch", tmp_path / "home",
                          "sess", seat, upstream, inject=lambda i: None) == 2
    assert FakeTransport.made == []
    assert go(inject=lambda i: None) == 0  # 127.0.0.1 is allowed


def _score(tmp_path, record, *extra):
    h1, base, rec = tmp_path / "h1.txt", tmp_path / "base.txt", tmp_path / "h1.json"
    h1.write_text("q_c_01: unknown\n", encoding="utf-8")
    base.write_text("q_c_01: unknown\n", encoding="utf-8")
    rec.write_text(json.dumps(record), encoding="utf-8")
    return run.main(["score", "--h1", str(h1), "--h1-record", str(rec), "--baseline", str(base), *extra])


def test_score_refuses_a_stopped_run_and_labels_a_partial_score(tmp_path, capsys):
    stopped = {"stopped": "IndexError: list index out of range", "probe_turns_completed": 1}
    assert _score(tmp_path, stopped) == 2
    assert "partial run" in capsys.readouterr().err
    short = {"stopped": None, "probe_turns_completed": script.PROBE_TURNS - 1}
    assert _score(tmp_path, short) == 2
    capsys.readouterr()
    assert _score(tmp_path, stopped, "--partial") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["label"].startswith("PARTIAL") and "compare" not in out  # never an acceptance 2 verdict
    assert out["h1_record"]["probe_turns_completed"] == 1


def test_score_compares_only_a_full_run(tmp_path, capsys):
    full = {"stopped": None, "probe_turns_completed": script.PROBE_TURNS}
    assert _score(tmp_path, full) == 0
    out = json.loads(capsys.readouterr().out)
    assert "label" not in out and "pass" in out["compare"]
    (tmp_path / "bad.json").write_text("not json", encoding="utf-8")
    assert run.main(["score", "--h1", str(tmp_path / "h1.txt"), "--h1-record", str(tmp_path / "bad.json"),
                     "--baseline", str(tmp_path / "base.txt")]) == 2


def test_short_estimates_refuse_before_anything_starts(live, monkeypatch):
    s, b, p, go, record, answers = live
    monkeypatch.setattr(budget, "turn_estimates", lambda b: [1] * (len(s.turns) - 5))
    assert go() == 2 and FakeTransport.made == []


def test_the_three_dry_runs_together_must_take_every_live_path(live):
    s, b, p, go, record, answers = live
    go()
    clean = {"h1_record": record()}
    go(inject=lambda i: (_ for _ in ()).throw(driver.AuthFailure("x")) if i == 3 else None)
    auth = {"h1_record": record()}
    with pytest.raises(ValueError):
        go(inject=lambda i: (_ for _ in ()).throw(ValueError("x")) if i == 3 else None)
    error = {"h1_record": record()}
    assert stubrun.unexercised([clean, auth, error]) == []
    assert stubrun.unexercised([clean, auth]) == ["error_handled"]
    assert stubrun.unexercised([clean]) == ["stop_handled", "error_handled"]
    assert stubrun.unexercised([]) == list(run.LIVE_PATHS)


def test_stub_answers_the_chosen_messages_request_with_401():
    stub = stubrun.Stub("k", auth_fail_at=1)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), stub.handler())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_port}/v1/messages"
    body = json.dumps({"messages": [{"role": "user", "content": "hi"}], "max_tokens": 10}).encode()
    try:
        first = urllib.request.urlopen(urllib.request.Request(url, data=body, method="POST"), timeout=10)
        assert first.status == 200
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(urllib.request.Request(url, data=body, method="POST"), timeout=10)
        assert err.value.code == 401
    finally:
        httpd.shutdown()
