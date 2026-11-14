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
    # An id that does not print (a directory name can hold a newline) as its repr, so it stays one line
    # (PR #257 review B), as ``named`` shows it in the errors.
    shown = session_id if session_id.isprintable() else repr(session_id)
    print(f"session {shown} ({how})", file=sys.stderr)


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
# Zero-width characters: one prints (a ZWNJ in Persian, a ZWJ in an emoji sequence, a Mongolian variation
# selector); two or more in a row carry text in their pattern, so a run is shown as escapes (N14). Counted
# with the format characters are those drawn as nothing that Python calls printable (U+034F, the Hangul
# fillers U+115F and U+1160, the Khmer U+17B4 and U+17B5, U+180B to U+180F, the Khitan filler U+16FE4), so a
# run made only of them is escapes too (review A round 7, finding 3), and the format characters a terminal
# draws as nothing that are outside the joiners: the hieroglyph format controls U+13430 to U+1343F, the
# shorthand format controls U+1BCA0 to U+1BCA3 and the musical formatting controls U+1D173 to U+1D17A
# (review B round 8, N22). A space next to one makes it part of a run, so a space between each pair hides
# nothing (review B round 7, N19); real text never puts a joiner next to a space. The Mongolian free
# variation selectors can end a word, so one after a letter or mark is not made part of a run by a space
# (review A round 8, nit 2).
_ZERO_WIDTH = frozenset({0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF,
                         0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, *range(0x180B, 0x1810), 0x16FE4,
                         *range(0x13430, 0x13440), *range(0x1BCA0, 0x1BCA4), *range(0x1D173, 0x1D17B)})
_MONGOLIAN_SELECTORS = frozenset({0x180B, 0x180C, 0x180D, 0x180F})
# Characters drawn as a blank cell (the blank braille pattern, the Hangul fillers U+3164 and U+FFA0). On
# their own they print, as in braille art and spaced Hangul; next to a zero-width character both are
# escapes, so a run cannot be hidden by placing one of them between each pair (review A round 6, finding 4).
_BLANK_CELLS = frozenset({0x2800, 0x3164, 0xFFA0})
# Printable characters that safe_text's fast path cannot pass unchecked.
_CHECKED = _SELECTORS | _IDEOGRAPHIC_SELECTORS | _INVISIBLE | _ZERO_WIDTH


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


def _in_run(n: int, before: str, after: str) -> bool:
    """Whether the character ``n``, between ``before`` and ``after``, is part of a zero-width run."""
    if n in _ZERO_WIDTH:
        # A Mongolian free variation selector that ends a Mongolian word before a space prints; after any
        # other letter it would let each word of a line carry a hidden bit (review B on #288, N25).
        word_final = (n in _MONGOLIAN_SELECTORS and bool(before) and 0x1800 <= ord(before) <= 0x18AF
                      and unicodedata.category(before)[0] in "LM")
        return any(c and (ord(c) in _ZERO_WIDTH or ord(c) in _BLANK_CELLS
                          or (unicodedata.category(c) == "Zs" and not word_final))
                   for c in (before, after))
    return n in _BLANK_CELLS and any(c and ord(c) in _ZERO_WIDTH for c in (before, after))


def _shown(c: str, before: str, after: str) -> bool:
    n = ord(c)
    if n in _INVISIBLE or _in_run(n, before, after):
        return False
    if n in _SELECTORS or n in _IDEOGRAPHIC_SELECTORS:
        return _selector_shown(n, before, after)
    return c.isprintable() or (unicodedata.category(c) in _KEPT_CATEGORIES and n not in _HIDING)


def safe_text(text: str) -> str:
    """``text`` for one line of a terminal: every character that does not print and could move the
    cursor, end the line, reorder or hide text (a control character, a line or paragraph separator, a
    direction control, a tag or annotation character, a lone surrogate, a private-use or unassigned
    code point, an invisible operator, a variation selector anywhere but right after a character it
    varies, a zero-width character next to another, a blank cell or a space (but a Mongolian free
    variation selector after a letter or mark prints before a space), and a blank cell next to a
    zero-width character) as its backslash escape, and everything else as it is: backslashes, and the
    joiners, a variation selector after an emoji, a symbol or an ideograph or in a keycap, other format
    characters and spaces that text in many scripts and emoji need."""
    if text.isprintable() and not any(ord(c) in _CHECKED for c in text):
        return text

    def escape(c: str) -> str:
        n = ord(c)
        return _SHORT_ESCAPES.get(c) or (f"\\x{n:02x}" if n < 0x100 else f"\\u{n:04x}" if n < 0x10000
                                         else f"\\U{n:08x}")

    return "".join(c if _shown(c, text[i - 1] if i else "", text[i + 1] if i + 1 < len(text) else "")
                   else escape(c) for i, c in enumerate(text))
