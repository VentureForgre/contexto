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
