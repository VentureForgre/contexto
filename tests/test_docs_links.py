"""Relative links in the tracked Markdown files resolve: the file exists and, for a Markdown target,
so does the heading the anchor names.

No network: absolute URLs and ``mailto:`` are not checked. Anchors follow GitHub's heading slugs
(lowercase, punctuation other than ``-`` and ``_`` dropped, spaces to hyphens, ``-1``, ``-2`` for a
repeated heading), plus explicit ``<a id>`` and ``<a name>`` anchors. Text inside fenced code blocks
and inline code is neither a link nor a heading.
"""

from __future__ import annotations

import html
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

import pytest

REPO = Path(__file__).resolve().parents[1]

# Links that were already broken in older records when this check was added, as (file, target)
# pairs. Fix the link and delete its line rather than adding one: a new entry needs a reason.
KNOWN_BROKEN: frozenset[tuple[str, str]] = frozenset()

FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.DOTALL)
INLINE_LINK = re.compile(
    r"(?<!\\)\]\(\s*(<[^>\n]*>|[^\s()]+(?:\([^\s()]*\)[^\s()]*)*)(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'))?\s*\)")
REFERENCE = re.compile(r"^ {0,3}\[(?!\^)[^\]\n]+\]:[ \t]*(<[^>\n]*>|\S+)", re.MULTILINE)
TAG = re.compile(r"<(img|source|a|video)\b([^>]*)>", re.IGNORECASE)
ATTRIBUTE = re.compile(r"\b(src|srcset|href|poster)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
EXPLICIT_ANCHOR = re.compile(r"<[a-zA-Z][^>]*?\b(?:id|name)\s*=\s*[\"']?([^\"'\s>]+)")
ATX = re.compile(r"^ {0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")


def tracked_markdown() -> list[str]:
    try:
        listing = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "*.md"], capture_output=True)
    except OSError:
        pytest.skip("git is not installed")
    if listing.returncode != 0:
        pytest.skip("not a git work tree (a source archive has no tracked-file list)")
    return sorted(name for name in listing.stdout.decode().split("\0") if name)


def tracked_paths() -> set[str]:
    """Every tracked file and every directory that holds one."""
    listing = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True, check=True)
    paths: set[str] = set()
    for name in listing.stdout.decode().split("\0"):
        while name and name not in paths:
            paths.add(name)
            name = os.path.dirname(name)
    return paths


def blank(text: str) -> str:
    """The text with everything but its line breaks replaced by spaces, so line numbers hold."""
    return re.sub(r"[^\n]", " ", text)


def without_fences(text: str) -> str:
    lines, fence = [], None
    for line in text.split("\n"):
        match = FENCE.match(line)
        if fence is None:
            if match:
                fence = match.group(1)
                line = ""
        else:
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) \
                    and not match.group(2).strip():
                fence = None
            line = ""
        lines.append(line)
    return "\n".join(lines)


def without_code(text: str) -> str:
    """Fenced blocks and inline code spans blanked. A code span stays within its paragraph."""
    paragraphs = re.split(r"(\n[ \t]*\n)", without_fences(text))
    return "".join(CODE_SPAN.sub(lambda span: blank(span.group(0)), part) for part in paragraphs)


def heading_text(source: str) -> str:
    """A heading as GitHub renders it: link and image text, code without its backticks, no tags."""
    spans: list[str] = []

    def keep(span: re.Match) -> str:
        spans.append(span.group(2).strip())
        return f"\0{len(spans) - 1}\0"

    text = CODE_SPAN.sub(keep, source)
    text = re.sub(r"!?\[((?:[^\[\]]|\[[^\[\]]*\])*)\](?:\([^)]*\)|\[[^\]]*\])", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"(?<!\w)_+(?=\S)(.+?)(?<=\S)_+(?!\w)", r"\1", text)  # _emphasis_, not snake_case
    text = re.sub(r"\0(\d+)\0", lambda span: spans[int(span.group(1))], text)
    return html.unescape(text).strip()


def slug(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading_text(heading).lower()).replace(" ", "-")


def anchors(text: str) -> set[str]:
    """Every anchor a Markdown file offers: its heading slugs and its explicit ids and names."""
    lines = without_fences(text).split("\n")
    found, seen = set(), {}
    for number, line in enumerate(lines):
        match = ATX.match(line)
        if match:
            heading = match.group(1)
        elif number and SETEXT.match(line) and lines[number - 1].strip() and not ATX.match(lines[number - 1]):
            heading = lines[number - 1].strip()
        else:
            continue
        base = slug(heading)
        count = seen.get(base, 0)
        seen[base] = count + 1
        found.add(base if count == 0 else f"{base}-{count}")
    found.update(EXPLICIT_ANCHOR.findall(without_code(text)))
    return found


def targets(text: str) -> list[tuple[int, str]]:
    """(line, target) for every Markdown link, reference definition and HTML src, srcset or href."""
    clean = without_code(text)
    found = []
    for pattern in (INLINE_LINK, REFERENCE):
        for match in pattern.finditer(clean):
            found.append((match.start(1), match.group(1).strip("<>")))
    for tag in TAG.finditer(clean):
        for attribute in ATTRIBUTE.finditer(tag.group(2)):
            value = attribute.group(2).strip("\"'")
            position = tag.start(2) + attribute.start(2)
            if attribute.group(1).lower() == "srcset":  # "a.png 1x, b.png 2x"
                found.extend((position, candidate.split()[0]) for candidate in value.split(",") if candidate.strip())
            else:
                found.append((position, value))
    found.sort(key=lambda item: item[0])  # by position; a srcset's candidates keep their order
    return [(clean.count("\n", 0, position) + 1, target) for position, target in found]


def exists_exactly(root: Path, relative: str, listings: dict[Path, set[str]]) -> bool:
    """True when the path exists with this exact spelling, also on a case-insensitive file system."""
    here = root
    for part in Path(relative).parts:
        if here not in listings:
            try:
                listings[here] = set(os.listdir(here))
            except OSError:
                listings[here] = set()
        if part not in listings[here]:
            return False
        here = here / part
    return True


def broken_links(root: Path, files: list[str], tracked: set[str] | None = None) -> list[tuple[str, int, str, str]]:
    """(file, line, target, reason) for each relative link in ``files`` that does not resolve.

    With ``tracked`` (tracked files and their directories), a target that exists on disk but is not
    tracked is broken too: it would be missing from a fresh checkout.
    """
    listings: dict[Path, set[str]] = {}
    anchor_sets: dict[str, set[str]] = {}
    broken = []
    for name in files:
        try:
            text = (root / name).read_text(encoding="utf-8")
        except OSError as exc:
            broken.append((name, 0, "", f"cannot be read: {exc}"))
            continue
        for line, target in targets(text):
            if not target or SCHEME.match(target) or target.startswith("//"):
                continue
            path, _, anchor = target.partition("#")
            path = unquote(path.partition("?")[0])
            if not path:
                resolved = name
            elif path.startswith("/"):  # GitHub reads a leading slash as the repository root
                resolved = os.path.normpath(path.lstrip("/"))
            else:
                resolved = os.path.normpath(os.path.join(os.path.dirname(name), path))
            if resolved == ".." or resolved.startswith("../") or os.path.isabs(resolved):
                broken.append((name, line, target, "points outside the repository"))
                continue
            if resolved != "." and not exists_exactly(root, resolved, listings):
                broken.append((name, line, target, f"{resolved} does not exist"))
                continue
            if tracked is not None and resolved != "." and resolved not in tracked:
                broken.append((name, line, target, f"{resolved} is not tracked by git"))
                continue
            if not anchor or not resolved.lower().endswith((".md", ".markdown")):
                continue
            if resolved not in anchor_sets:
                anchor_sets[resolved] = anchors((root / resolved).read_text(encoding="utf-8"))
            if unquote(anchor) not in anchor_sets[resolved]:
                broken.append((name, line, target, f"{resolved} has no heading or anchor '{unquote(anchor)}'"))
    return broken


def test_relative_links_in_tracked_markdown_resolve():
    files = tracked_markdown()  # skips outside a git work tree
    broken = broken_links(REPO, files, tracked_paths())
    new = [f"{name}:{line}: ({target}): {reason}" for name, line, target, reason in broken
           if (name, target) not in KNOWN_BROKEN]
    assert not new, "broken relative links:\n" + "\n".join(new)
    stale = KNOWN_BROKEN - {(name, target) for name, _line, target, _reason in broken}
    assert not stale, f"these links resolve now, so remove them from KNOWN_BROKEN: {sorted(stale)}"


def test_heading_slugs_match_github():
    text = "\n".join([
        "# `contexto run`", "## Is it working?", "## Codex (API-key mode)", "## What CI runs", "## FAQ", "## FAQ",
        "### A & B, c_d: [link text](x.md) and _stressed_", "```", "# not a heading", "```",
        "Setext title", "============", '<a id="pinned"></a> and <a name="named">text</a>',
        "`<a id=\"in-code\">`", "####### seven hashes are a paragraph",
    ])
    assert anchors(text) == {
        "contexto-run", "is-it-working", "codex-api-key-mode", "what-ci-runs", "faq", "faq-1",
        "a--b-c_d-link-text-and-stressed", "setext-title", "pinned", "named",
    }


def test_links_are_found_outside_code_only():
    text = "\n".join([
        "[a](one.md) and ![b](two.png \"title\") and [c](<three four.md#part>)",
        "`[d](in-code.md)` and ``[e](`also`.md)``", "```sh", "[f](fenced.md)", "```",
        "~~~", "[g](tilde.md)", "```", "[h](still-fenced.md)", "~~~",
        "[ref]: five.md#x", "[^note]: not a link", "<img alt=\"i\" src=\"six.png\">",
        "<picture><source srcset=\"seven.png 1x, eight.png 2x\"><img src='nine.png'></picture>",
        "<a href=\"ten.md\">j</a> [k](https://example.com/x.md) [l](mailto:someone@example.com) [m](a_(b).md)",
    ])
    assert [target for _line, target in targets(text)] == [
        "one.md", "two.png", "three four.md#part", "five.md#x", "six.png", "seven.png", "eight.png", "nine.png",
        "ten.md", "https://example.com/x.md", "mailto:someone@example.com", "a_(b).md",
    ]
    assert targets("one\n\n[a](b.md)\n")[0][0] == 3  # the line number survives the blanking


def test_a_broken_link_is_reported(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# Guide\n\n## First part\n\n## First part\n")
    (tmp_path / "docs" / "shot.png").write_bytes(b"")
    (tmp_path / "README.md").write_text("\n".join([
        "# Top", "[ok](docs/guide.md#first-part-1) [ok](docs/guide.md) [ok](#top) [ok](docs/) [ok](/docs/guide.md)",
        "<img src=\"docs/shot.png\"> [ok](https://example.com/missing.md) [ok](docs/shot.png#L3)",
        "[gone](docs/missing.md)", "[anchor](docs/guide.md#second-part)", "[case](docs/Guide.md)",
        "[self](#bottom)", "<img src=\"docs/missing.png\">", "[out](../elsewhere.md)", "`[code](docs/nope.md)`",
    ]) + "\n")
    (tmp_path / "docs" / "other.md").write_text(
        "[up](../README.md#top) [bad](../README.md#nope) [bad](guide.md#Guide)\n")
    assert [(name, line, target) for name, line, target, _reason
            in broken_links(tmp_path, ["README.md", "docs/other.md"])] == [
        ("README.md", 4, "docs/missing.md"), ("README.md", 5, "docs/guide.md#second-part"),
        ("README.md", 6, "docs/Guide.md"), ("README.md", 7, "#bottom"), ("README.md", 8, "docs/missing.png"),
        ("README.md", 9, "../elsewhere.md"),
        ("docs/other.md", 1, "../README.md#nope"), ("docs/other.md", 1, "guide.md#Guide"),
    ]
    tracked = {"README.md", "docs", "docs/guide.md", "docs/other.md"}  # docs/shot.png is on disk only
    assert [(line, target, reason) for name, line, target, reason
            in broken_links(tmp_path, ["README.md"], tracked) if "tracked" in reason] == [
        (3, "docs/shot.png", "docs/shot.png is not tracked by git"),
        (3, "docs/shot.png#L3", "docs/shot.png is not tracked by git"),
    ]
