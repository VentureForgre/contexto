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
# Also shown as escapes (review B rounds 3 and 4, N7 and N11): the invisible operators, and every variation
# selector except where text needs one. Python counts the selectors as printable, and placed anywhere else
# they carry text a reader cannot see. A selector prints as it is only right after a character it varies:
# U+FE00 to U+FE0F after a non-ASCII symbol (an emoji, an arrow, a math sign), an ideograph, or in a keycap (a digit,
# # or * then U+FE0F then U+20E3); U+E0100 to U+E01EF after an ideograph.
_INVISIBLE = frozenset(range(0x2061, 0x2065))
_SELECTORS = frozenset(range(0xFE00, 0xFE10))
_IDEOGRAPHIC_SELECTORS = frozenset(range(0xE0100, 0xE01F0))
# Emoji whose base is punctuation or a letter, not a symbol: ‼ ⁉ ℹ 〰 〽 (Unicode's emoji variation sequences).
_EMOJI_BASES = frozenset({0x203C, 0x2049, 0x2139, 0x3030, 0x303D})
# Symbols drawn as blank, after which a selector would sit unseen (review B round 5, N14).
_BLANK_SYMBOLS = frozenset({0x2800, 0xFFFC})
# Zero-width characters: one prints (a ZWNJ in Persian, a ZWJ in an emoji sequence); two or more in a row
# carry text in their pattern, so a run is shown as escapes (N14).
_ZERO_WIDTH = frozenset({0x180E, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF})


def _ideograph(c: str) -> bool:
    n = ord(c)
    return 0x3400 <= n <= 0x9FFF or 0xF900 <= n <= 0xFAFF or 0x20000 <= n <= 0x3FFFF


def _selector_shown(n: int, before: str, after: str) -> bool:
    if not before:
        return False
    if n in _IDEOGRAPHIC_SELECTORS:
        return _ideograph(before)
    if before in "0123456789#*":
        return n in (0xFE0E, 0xFE0F) and after == "\u20e3"
    if ord(before) < 0x80 or ord(before) in _BLANK_SYMBOLS:  # | + < = > or a blank cell would carry one each
        return False
    return unicodedata.category(before) in ("So", "Sm") or _ideograph(before) or ord(before) in _EMOJI_BASES


def _shown(c: str, before: str, after: str) -> bool:
    n = ord(c)
    if n in _INVISIBLE:
        return False
    if n in _ZERO_WIDTH and ((before and ord(before) in _ZERO_WIDTH) or (after and ord(after) in _ZERO_WIDTH)):
        return False
    if n in _SELECTORS or n in _IDEOGRAPHIC_SELECTORS:
        return _selector_shown(n, before, after)
    return c.isprintable() or (unicodedata.category(c) in _KEPT_CATEGORIES and n not in _HIDING)


def safe_text(text: str) -> str:
    """``text`` for one line of a terminal: every character that does not print and could move the
    cursor, end the line, reorder or hide text (a control character, a line or paragraph separator, a
    direction control, a tag or annotation character, a lone surrogate, a private-use or unassigned
    code point, an invisible operator, or a variation selector anywhere but right after a character it
    varies) as its backslash escape, and everything else as it is: backslashes, and the joiners, a
    variation selector after an emoji, a symbol or an ideograph or in a keycap, other format characters and
    spaces that text in many scripts and emoji need."""
    if text.isprintable() and not any(ord(c) in _SELECTORS or ord(c) in _IDEOGRAPHIC_SELECTORS
                                      or ord(c) in _INVISIBLE or ord(c) in _ZERO_WIDTH for c in text):
        return text

    def escape(c: str) -> str:
        n = ord(c)
        return _SHORT_ESCAPES.get(c) or (f"\\x{n:02x}" if n < 0x100 else f"\\u{n:04x}" if n < 0x10000
                                         else f"\\U{n:08x}")

    return "".join(c if _shown(c, text[i - 1] if i else "", text[i + 1] if i + 1 < len(text) else "")
                   else escape(c) for i, c in enumerate(text))
