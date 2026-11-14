"""docs/dialect.md documents every member of ``contexto.rewrite.Dialect`` (D70).

The document is the contract for adding a client. These checks fail when a public attribute or method
is added to ``Dialect`` without an entry, or when the document's table of what ``ResponsesDialect``
overrides no longer matches the class. They read names only, never wording.
"""

import re
from pathlib import Path

from contexto.codex import ResponsesDialect
from contexto.rewrite import Dialect

DOC = Path(__file__).resolve().parents[1] / "docs" / "dialect.md"
HEADING = re.compile(r"^#{2,4} .*$", re.MULTILINE)
# A row of the members table: | `name` | kind | yes or no |
ROW = re.compile(r"^\|\s*`(\w+)`\s*\|[^|\n]*\|\s*(yes|no)\s*\|\s*$", re.MULTILINE)


def public_names(cls: type) -> list[str]:
    """The public names ``cls`` defines itself: class attributes, methods and properties."""
    return [name for name in vars(cls) if not name.startswith("_")]


def test_every_dialect_member_has_an_entry():
    members = public_names(Dialect)
    assert members, "Dialect defines no public names: this test no longer checks anything"
    text = DOC.read_text(encoding="utf-8")
    headings = "\n".join(HEADING.findall(text))
    undocumented = [name for name in members if f"`{name}`" not in headings]
    assert not undocumented, (f"docs/dialect.md has no entry (a heading holding the name in backticks) for "
                              f"Dialect.{', Dialect.'.join(undocumented)}")


def test_the_members_table_says_what_responses_dialect_overrides():
    members = public_names(Dialect)
    rows = ROW.findall(DOC.read_text(encoding="utf-8"))
    listed = [name for name, _ in rows]
    assert sorted(listed) == sorted(members), "the members table lists each public name of Dialect once"
    overridden = {name for name in members if name in vars(ResponsesDialect)}
    marked = {name for name, answer in rows if answer == "yes"}
    assert marked == overridden, (f"marked overridden but is not: {sorted(marked - overridden)}; "
                                  f"overridden but not marked: {sorted(overridden - marked)}")
