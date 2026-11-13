"""Shared fixtures for the package tests."""

from __future__ import annotations

import os
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
    """Keep the suite off this machine's Claude Code managed settings file.

    ``contexto run claude`` reads that file from a fixed system path (D70). The variable below names
    another path for it, here one in a temporary directory where no file exists, for the tests that
    call the reader in this process and, through the inherited environment, for every launcher a test
    starts. A test that needs a managed file passes its own path."""
    from contexto.upstream import MANAGED_SETTINGS_KEY

    previous = os.environ.get(MANAGED_SETTINGS_KEY)
    os.environ[MANAGED_SETTINGS_KEY] = str(tmp_path_factory.mktemp("managed") / "managed-settings.json")
    yield
    if previous is None:
        os.environ.pop(MANAGED_SETTINGS_KEY, None)
    else:
        os.environ[MANAGED_SETTINGS_KEY] = previous


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
