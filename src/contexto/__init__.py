"""Contexto: durable, recoverable context for very long-running AI sessions."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Installed metadata carries the version the release workflow stamped.
    __version__ = version("contexto")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0.0.0+unknown"
