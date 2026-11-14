"""Helpers shared by the store-reading subcommands (not a subcommand: the leading underscore skips discovery)."""

from __future__ import annotations

import sys

EXIT_OK = 0
EXIT_ERROR = 1  # unknown session or page, bad offset, unreadable store
EXIT_USAGE = 2  # argparse's usage error, and a prune --session that is only the start of session ids

SESSION_HELP = "session id, or a unique prefix of one"


def fail(message: str) -> int:
    print(f"contexto: error: {message}", file=sys.stderr)
    return EXIT_ERROR


def add_session_option(parser) -> None:
    parser.add_argument("--session", metavar="ID",
                        help=f"{SESSION_HELP} (default: $CONTEXTO_SESSION, else the most recently active "
                             "session with pages or pins, or one in use)")


def say_session(session_id: str, how: str) -> None:
    print(f"session {session_id} ({how})", file=sys.stderr)


_SHORT_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def safe_text(text: str) -> str:
    """``text`` for one line of a terminal: every character that is not printable (a control character,
    a line or paragraph separator, a direction override, a lone surrogate, an unassigned code point)
    as its backslash escape, and everything else, backslashes included, as it is."""
    if text.isprintable():
        return text

    def escape(c: str) -> str:
        n = ord(c)
        return _SHORT_ESCAPES.get(c) or (f"\\x{n:02x}" if n < 0x100 else f"\\u{n:04x}" if n < 0x10000
                                         else f"\\U{n:08x}")

    return "".join(c if c.isprintable() else escape(c) for c in text)
