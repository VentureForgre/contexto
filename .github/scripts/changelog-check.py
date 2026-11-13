#!/usr/bin/env python3
"""Changelog guard for the Release workflow. Standard library only.

    python3 -I .github/scripts/changelog-check.py <version or tag> [--file CHANGELOG.md]

The release job runs it after the next version is computed and before anything is built or tagged.
It fails unless CHANGELOG.md has a section for that version, headed exactly

    ## X.Y.Z (YYYY-MM-DD)

The section must be the newest one (the file is newest first, with an optional "## Unreleased" on
top) and must not be empty. The date is not compared with today.
"""

import argparse
import re
import sys
from pathlib import Path

HEADING = re.compile(r"## (\d+\.\d+\.\d+) \(\d{4}-\d{2}-\d{2}\)")
VERSION = re.compile(r"v?(\d+\.\d+\.\d+)")
FIX = (
    'Add the section "## {version} (YYYY-MM-DD)" above the older versions, with the changes since '
    "the last release in user terms, in a docs: pull request. After it merges, run the release "
    'again. See RELEASING.md, "Cutting a release".'
)


def _lines(text):
    return [line.rstrip() for line in text.splitlines()]


def _body(lines, start):
    """The non-blank lines of the section whose heading is lines[start]."""
    body = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if line.strip():
            body.append(line)
    return body


def unreleased_entries(text):
    """The non-blank lines under "## Unreleased", which a release leaves behind."""
    lines = _lines(text)
    for number, line in enumerate(lines):
        if line == "## Unreleased":
            return _body(lines, number)
    return []


def problem(text, version):
    """Why `text` cannot be released as `version`, or None when it can."""
    match = VERSION.fullmatch(version)
    if not match:
        raise ValueError(f"not a version: {version!r} (expected X.Y.Z or vX.Y.Z)")
    version = match.group(1)
    fix = FIX.format(version=version)
    lines = _lines(text)
    matches = ((number, HEADING.fullmatch(line)) for number, line in enumerate(lines))
    headings = [(number, match.group(1)) for number, match in matches if match]
    found = [number for number, heading in headings if heading == version]

    if not found:
        # A heading for this version in another form ("## v1.2.3", "## [1.2.3] - date", "## 1.2.3").
        near_miss = re.compile(rf"#+\s*\[?v?{re.escape(version)}(?!\d|\.\d)")
        hint = next(
            (
                f' The heading on line {number + 1}, "{line}", is not in the form the release reads.'
                for number, line in enumerate(lines)
                if near_miss.match(line)
            ),
            "",
        )
        return f"CHANGELOG.md has no section for {version}.{hint} {fix}"
    if len(found) > 1:
        where = " and ".join(str(number + 1) for number in found)
        return f"CHANGELOG.md has a section for {version} more than once (lines {where}). Keep one."
    newest_line, newest = headings[0]
    if newest != version:
        return (
            f"CHANGELOG.md's newest section is {newest} (line {newest_line + 1}), not {version}. "
            f"The section for {version} must come first, above the older versions."
        )
    if not _body(lines, found[0]):
        return f"The CHANGELOG.md section for {version} (line {found[0] + 1}) is empty. {fix}"
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fail unless the changelog has a section for the version.")
    parser.add_argument("version", help="the version about to be released, as X.Y.Z or vX.Y.Z")
    parser.add_argument("--file", default="CHANGELOG.md", type=Path)
    args = parser.parse_args(argv)
    if not VERSION.fullmatch(args.version):
        parser.error(f"not a version: {args.version!r} (expected X.Y.Z or vX.Y.Z)")
    version = VERSION.fullmatch(args.version).group(1)

    try:
        text = args.file.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"::error::{args.file} does not exist, so it has no section for {version}. {FIX.format(version=version)}")
        return 1
    except (OSError, UnicodeDecodeError) as error:
        print(f"::error::Cannot read {args.file}: {error}")
        return 1

    message = problem(text, version)
    if message:
        print(f"::error::{message}")
        return 1
    heading = next(line for line in _lines(text) if HEADING.fullmatch(line) and line.startswith(f"## {version} "))
    print(f'{args.file} has the section "{heading}".')
    left = unreleased_entries(text)
    if left:
        print(
            f'::warning::{args.file} still has {len(left)} line(s) under "## Unreleased". This release '
            f"ships everything on main; move what it ships into the {version} section next time."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
