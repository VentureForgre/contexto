"""Shared fixtures for the package tests."""

from __future__ import annotations

import signal
import threading
from http.server import ThreadingHTTPServer

import pytest

from contexto.proxy import ContextoProxy
from test_proxy_server import MemoryLog, Upstream


@pytest.fixture(autouse=True, scope="session")
def sigint_default():
    """Start child processes with SIGINT at its default action, as a terminal does.

    A non-interactive shell starts a background job (``pytest ... &``, as parallel runs are started)
    with SIGINT ignored, and an ignored signal stays ignored across exec. A child CLI then never sees
    Ctrl+C and a test that sends it SIGINT waits out its deadline. A Python handler, unlike SIG_IGN,
    is reset to the default action on exec, so installing one here gives every child the default.
    It is autouse for the whole session so that every test that sends a child SIGINT is covered."""
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    yield
    if previous is not None:
        signal.signal(signal.SIGINT, previous)


@pytest.fixture(autouse=True, scope="session")
def no_managed_settings(tmp_path_factory):
    """Keep the tests that read settings in this process off this machine's Claude Code managed
    settings file. ``contexto run claude`` reads it from a fixed system path (D70) that no variable
    moves, so the table of paths is pointed at a temporary directory where no file exists. A launcher a
    test starts as a subprocess reads the real path; the tests that do so with the real ``claude``
    skip when that file exists. A test that needs a managed file passes its own path."""
    from contexto import upstream

    saved = dict(upstream.MANAGED_SETTINGS_FILES)
    empty = str(tmp_path_factory.mktemp("managed") / "managed-settings.json")
    upstream.MANAGED_SETTINGS_FILES.update({platform: empty for platform in saved})
    yield
    upstream.MANAGED_SETTINGS_FILES.clear()
    upstream.MANAGED_SETTINGS_FILES.update(saved)


# The stub upstream and proxy factory of test_proxy_server, shared with test_upstream_affinity.


@pytest.fixture
def upstream():
    Upstream.received = []
    Upstream.mode = "sse"
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/base"
    server.shutdown()
    server.server_close()


@pytest.fixture
def make_proxy(upstream):
    started = []

    def make(**kwargs) -> tuple[ContextoProxy, MemoryLog]:
        log = MemoryLog()
        proxy = ContextoProxy(kwargs.pop("upstream", upstream), log, **kwargs)
        proxy.start()
        started.append(proxy)
        return proxy, log

    yield make
    for proxy in started:
        proxy.stop()
