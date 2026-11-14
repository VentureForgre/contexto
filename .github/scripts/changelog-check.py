#!/usr/bin/env python3
"""Changelog guard for the Release workflow. Standard library only.

    python3 -I .github/scripts/changelog-check.py <version or tag> [--last-released <tag>] [--file CHANGELOG.md]

The release job runs it after the next version is computed and before anything is built or tagged.
It fails unless CHANGELOG.md has a section for that version, headed exactly

    ## X.Y.Z (YYYY-MM-DD)

The section must be the newest one (the file is newest first, with an optional "## Unreleased" on
top) and must hold at least one entry: a line that is not a heading, a placeholder such as
"Nothing yet." or an HTML comment. The date must be a real date; it is not compared with today.
A heading inside a fenced code block is an example, not a section. Fences are read as CommonMark
reads them: a line of three or more backticks or tildes opens a block, unless it is a backtick
fence whose remainder holds a backtick (that is inline code), and only a fence of the same
character, at least as long and with nothing after it, closes it.

The file must not contain the line "<!-- version list -->". semantic-release's `changelog`
command, which the reattach job runs, inserts its own text at that line; without it, the command
leaves a hand-written file as it is.
"""

import argparse
import datetime
import re
import sys
from pathlib import Path

HEADING = re.compile(r"## ([0-9]+\.[0-9]+\.[0-9]+) \(([0-9]{4}-[0-9]{2}-[0-9]{2})\)")
VERSION = re.compile(r"v?([0-9]+\.[0-9]+\.[0-9]+)")
MARKER = re.compile(r"<!--\s*version list\s*-->", re.IGNORECASE)
FENCE = re.compile(r"\s{0,3}(`{3,}|~{3,})")
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
SUBHEADING = re.compile(r"\s{0,3}#{1,6}(\s|$)")
PLACEHOLDERS = {"nothing yet", "nothing", "none", "no changes yet"}
ADD = (
    'Add the section "## {version} (YYYY-MM-DD)" above the older versions, with the changes since '
    "the last release in user terms, in a docs: pull request. After it merges, run the release "
    'again. See RELEASING.md, "Cutting a release".'
)
RENAME = (
    'change its heading to "## {version} (YYYY-MM-DD)" in a docs: pull request and run the release '
    "again. {why}"
)
GREW = "The version grew because a feat: merged after the section was written."
LOWER = (
    "The computed version, {version}, is lower than the section written, {newest}: the version comes "
    "from the commits since the last release, not from the changelog."
)


def _lines(text):
    """The lines of `text`, with HTML comments blanked out and the lines inside code fences marked.

    Returns (lines, fenced, unclosed): `unclosed` is the index of the line that opens a code block
    that is never closed, or None.
    """
    # Blank a comment without moving the lines after it.
    text = COMMENT.sub(lambda match: re.sub(r"[^\n]", " ", match.group(0)), text)
    lines, fenced, fence, opened = [], [], None, None
    for number, line in enumerate(text.splitlines()):
        line = line.rstrip()
        opening = FENCE.match(line)
        if fence is None and opening and not (opening.group(1)[0] == "`" and "`" in line[opening.end():]):
            fence, opened = opening.group(1), number
            fenced.append(True)
        elif fence is not None:
            fenced.append(True)
            if opening and opening.group(1)[0] == fence[0] and len(opening.group(1)) >= len(fence) \
                    and line.strip() == opening.group(1):
                fence = None
        else:
            fenced.append(False)
        lines.append(line)
    return lines, fenced, (opened if fence is not None else None)


def _headings(lines, fenced):
    """(line index, version, date) for every version heading outside a code block."""
    matches = ((number, HEADING.fullmatch(line)) for number, line in enumerate(lines) if not fenced[number])
    return [(number, match.group(1), match.group(2)) for number, match in matches if match]


def _entries(lines, fenced, start):
    """The lines of the section headed at lines[start] that say something."""
    entries = []
    for number in range(start + 1, len(lines)):
        line = lines[number]
        if not fenced[number]:
            if line.startswith("## "):
                break
            if SUBHEADING.match(line) or line.strip(" *_.-").lower() in PLACEHOLDERS:
                continue
        if line.strip():
            entries.append(line)
    return entries


def unreleased_entries(text):
    """The entries under "## Unreleased", which a release leaves behind. A "Nothing yet." is not one."""
    lines, fenced, _ = _lines(text)
    for number, line in enumerate(lines):
        if line == "## Unreleased" and not fenced[number]:
            return _entries(lines, fenced, number)
    return []


def _version(value):
    match = VERSION.fullmatch(value)
    if not match:
        raise ValueError(f"not a version: {value!r} (expected X.Y.Z or vX.Y.Z)")
    return match.group(1)


def _key(version):
    return tuple(int(part) for part in version.split("."))


def problem(text, version, last_released=None):
    """Why `text` cannot be released as `version`, or None when it can.

    `last_released` is the last release's version or tag, when the caller knows it. It tells a
    section that was written for this release under a smaller version from the last release's own.
    """
    version = _version(version)
    last_released = _version(last_released) if last_released else None

    for number, line in enumerate(text.splitlines()):
        if MARKER.search(line):
            return (
                f'CHANGELOG.md line {number + 1} is the marker "<!-- version list -->". Remove that line '
                "in a docs: pull request. semantic-release's changelog command, which the reattach job "
                "runs, inserts its own text there; without the marker it leaves this hand-written file "
                "as it is."
            )

    lines, fenced, unclosed = _lines(text)
    headings = _headings(lines, fenced)
    found = [(number, date) for number, heading, date in headings if heading == version]

    if not found and unclosed is not None:
        # The heading is there, but a fence above it that is never closed made it code.
        for number in range(unclosed + 1, len(lines)):
            match = HEADING.fullmatch(lines[number])
            if match and match.group(1) == version:
                return (
                    f'CHANGELOG.md has the heading "{lines[number]}" on line {number + 1}, but it is inside '
                    f"the code block opened on line {unclosed + 1}, which is never closed. Close that block, "
                    "or remove the line that opens it, in a docs: pull request and run the release again."
                )
    if not found:
        # A heading for this version in another form ("## v1.2.3", "## [1.2.3] - date", "## 1.2.3").
        near_miss = re.compile(rf"#+\s*\[?v?{re.escape(version)}(?![0-9]|\.[0-9])")
        for number, line in enumerate(lines):
            if not fenced[number] and near_miss.match(line):
                return (
                    f'CHANGELOG.md has no section for {version}. The heading on line {number + 1}, "{line}", '
                    f'is not in the form the release reads: "## {version} (YYYY-MM-DD)". Correct it in a '
                    "docs: pull request and run the release again."
                )
        add = ADD.format(version=version)
        if not headings:
            return f"CHANGELOG.md has no section for {version}. {add}"
        newest_line, newest, _ = headings[0]
        if last_released and _key(newest) <= _key(last_released):
            return f"CHANGELOG.md has no section for {version}. {add}"
        why = GREW if _key(version) > _key(newest) else LOWER.format(version=version, newest=newest)
        rename = RENAME.format(version=version, why=why)
        if last_released:
            # The newest section is for a version above the last release: it was never released.
            return (
                f"CHANGELOG.md has no section for {version}. Its newest section is {newest} (line "
                f"{newest_line + 1}), and the last release is {last_released}, so that section was written "
                f"for this release: {rename}"
            )
        return (
            f"CHANGELOG.md has no section for {version}. Its newest section is {newest} (line "
            f"{newest_line + 1}). If that section was written for this release, {rename} If {newest} is "
            f"already released: {add}"
        )
    if len(found) > 1:
        where = " and ".join(str(number + 1) for number, _ in found)
        return f"CHANGELOG.md has a section for {version} more than once (lines {where}). Keep one."
    number, date = found[0]
    newest_line, newest, _ = headings[0]
    if newest != version:
        return (
            f"CHANGELOG.md's newest section is {newest} (line {newest_line + 1}), not {version}. "
            f"The section for {version} must come first, above the older versions."
        )
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        return (
            f'The heading for {version} on CHANGELOG.md line {number + 1} has "{date}", which is not a date. '
            "Write the day of the release as YYYY-MM-DD."
        )
    if not _entries(lines, fenced, number):
        return (
            f"The CHANGELOG.md section for {version} (line {number + 1}) is empty: it has no line besides "
            f"headings, placeholders and comments. Write what the release changes for users, in a docs: "
            f"pull request, and run the release again."
        )
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fail unless the changelog has a section for the version.")
    parser.add_argument("version", help="the version about to be released, as X.Y.Z or vX.Y.Z")
    parser.add_argument("--last-released", default="", help="the last released version or tag, if there is one")
    parser.add_argument("--file", default="CHANGELOG.md", type=Path)
    args = parser.parse_args(argv)
    try:
        version = _version(args.version)
        last_released = _version(args.last_released) if args.last_released else None
    except ValueError as error:
        parser.error(str(error))

    try:
        text = args.file.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"::error::{args.file} does not exist, so it has no section for {version}. {ADD.format(version=version)}")
        return 1
    except (OSError, UnicodeDecodeError) as error:
        print(f"::error::Cannot read {args.file}: {error}")
        return 1

    message = problem(text, version, last_released)
    if message:
        print(f"::error::{message}")
        return 1
    lines, fenced, _ = _lines(text)
    number = next(number for number, heading, _ in _headings(lines, fenced) if heading == version)
    print(f'{args.file} has the section "{lines[number]}".')
    left = unreleased_entries(text)
    if left:
        print(
            f'::warning::{args.file} still has {len(left)} line(s) under "## Unreleased". This release '
            f"ships everything on main; move what it ships into the {version} section next time."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
