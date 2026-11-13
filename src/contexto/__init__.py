"""Contexto: durable, recoverable context for very long-running AI sessions."""



def __getattr__(name: str) -> str:
    """``__version__``, read when it is first asked for (D64). Reading the installed metadata loads
    ``importlib.metadata``, which takes longer than everything else ``contexto statusline`` does at
    start, and Claude Code runs that command after every assistant message."""
    if name != "__version__":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib.metadata import PackageNotFoundError, version

    try:
        found = version("contexto")  # installed metadata carries the version the release workflow stamped
    except PackageNotFoundError:  # a source tree that was never installed
        found = "0.0.0+unknown"
    globals()["__version__"] = found
    return found
