"""Shared fixtures for the package tests."""

from __future__ import annotations

import signal

import pytest


@pytest.fixture
def sigint_default():
    """Start child processes with SIGINT at its default action, as a terminal does.

    A non-interactive shell starts a background job (``pytest ... &``, as parallel runs are started)
    with SIGINT ignored, and an ignored signal stays ignored across exec. A child CLI then never sees
    Ctrl+C and a test that sends it SIGINT waits out its deadline. A Python handler, unlike SIG_IGN,
    is reset to the default action on exec, so installing one here gives every child the default."""
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    yield
    if previous is not None:
        signal.signal(signal.SIGINT, previous)
