"""Helpers shared by the store-reading subcommands (not a subcommand: the leading underscore skips discovery)."""

from __future__ import annotations

import sys
import unicodedata

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
# Format characters and spaces a terminal shows as they are (ZWNJ in Persian, ZWJ in Devanagari and emoji
# sequences, the soft hyphen, the word joiner, no-break and ideographic spaces), except these, which
# reorder text (the bidi embedding, override, isolate and mark controls, and the deprecated U+206A to
# U+206F) or hide it (the interlinear annotation characters and the tag characters).
_KEPT_CATEGORIES = ("Cf", "Zs")
_HIDING = frozenset({0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x2070),
                     *range(0xFFF9, 0xFFFC), 0xE0001, *range(0xE0020, 0xE0080)})
# Also shown as escapes (review B round 3, N7): the invisible operators, and a variation selector that
# follows another selector, an ASCII character or nothing. Python counts the selectors as printable, and a
# run of them, or one after each letter of an English line, carries text a reader cannot see; one after an
# emoji or an ideograph is how those are written, and prints as it is.
_INVISIBLE = frozenset(range(0x2061, 0x2065))
_SELECTORS = frozenset(range(0xFE00, 0xFE10)) | frozenset(range(0xE0100, 0xE01F0))


def _shown(c: str, before: str) -> bool:
    n = ord(c)
    if n in _INVISIBLE or (n in _SELECTORS and (not before or ord(before) < 0x80 or ord(before) in _SELECTORS)):
        return False
    return c.isprintable() or (unicodedata.category(c) in _KEPT_CATEGORIES and n not in _HIDING)


def safe_text(text: str) -> str:
    """``text`` for one line of a terminal: every character that does not print and could move the
    cursor, end the line, reorder or hide text (a control character, a line or paragraph separator, a
    direction control, a tag or annotation character, a lone surrogate, a private-use or unassigned
    code point, an invisible operator, or a variation selector after another one, after an ASCII
    character or at the start) as its backslash escape, and everything else as it is: backslashes, and
    the joiners, a variation selector after an emoji or an ideograph, other format characters and spaces
    that text in many scripts and emoji need."""
    if text.isprintable() and not any(ord(c) in _SELECTORS or ord(c) in _INVISIBLE for c in text):
        return text

    def escape(c: str) -> str:
        n = ord(c)
        return _SHORT_ESCAPES.get(c) or (f"\\x{n:02x}" if n < 0x100 else f"\\u{n:04x}" if n < 0x10000
                                         else f"\\U{n:08x}")

    return "".join(c if _shown(c, text[i - 1] if i else "") else escape(c) for i, c in enumerate(text))
