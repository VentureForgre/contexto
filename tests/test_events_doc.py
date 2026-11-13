"""docs/events.md is a reference for every event the package writes to a session's ``log.jsonl``.

People reading a log, and commands that read one, rely on it, so it must not go stale. These tests
read the package source with ``ast`` and the document's headings, facts lines, bullets and index, and
fail when they disagree. Each failure says which file to change and how.

What they guarantee:

- Names, both ways. Every event name the source can write has an entry in the document (a heading
  ``### `name```), and every entry names an event the source can write.
- The scan is not silently incomplete in the forms it reads: a dict display with an ``"event"`` key,
  ``dict(event=...)``, ``x.update(event=...)``, ``x.setdefault("event", ...)`` and ``x["event"] = ...``.
  When the value is not a string literal (or a conditional expression of string literals), the test
  fails unless the function is listed in ``DYNAMIC_SITES`` with the names it can produce. Each listed
  name must be a string literal in that function, and an entry no site needs any more fails too. A
  function whose dicts never reach a log is exempt only by listing it in ``NOT_SESSION_LOG``.
- Fields, source to document, for the fields the scan can read. Each of these is listed under its
  event (a bullet ``- `field`: ...``):
  - a key written as a literal next to a literal event name;
  - a key unpacked into the event with ``**``, from a dict display written in place (either branch of
    a conditional expression) or from a local variable that the same function assigns only dict
    displays to;
  - a key stored later on the same local variable (``v["key"] = ...``, ``v.update(key=...)``,
    ``v.update({"key": ...})``, ``v.update(**local)``, ``v.setdefault("key", ...)``) when that
    variable holds one event name in its function;
  - a key stored on a variable listed in ``FIELD_CARRIERS``, which names the variables that carry one
    event across functions.
- Fields the scan cannot read, by a list. ``HELPER_FIELDS`` names the fields that reach an event
  through a helper's return value or through a variable that holds more than one event name. Each
  is listed under its event, so such a bullet cannot be removed from the document unnoticed.
- Two entries that share a name are kept apart. ``request`` is both the proxy's line and the engine's
  nested event: ``SHARED_NAMES`` says which entry covers the event as each source file writes it, and
  each field is looked for in that entry alone.
- Fields, document to source, weakly. A field the document lists appears as a string literal or a
  keyword argument somewhere in a module that writes the event. This catches a field that was renamed
  or removed; it does not prove that the field is written on that event.
- Each entry states its writer, where it appears and its kind in one line, in a fixed vocabulary.
- The index table under ``## Index`` has exactly one row per entry, in the entries' order, and each
  row repeats its entry's heading, writer, place and kind. Every link to a place in the document
  (``](#anchor)``) names a heading that exists.
- Each ``json`` example in the document is one object with sorted keys, as a line in the file has,
  whose event and keys the document lists, nested events included, each nested event shown in a place
  its entry names.

What they do not guarantee:

- An event built in a form the scan does not read: a computed key (``{key: name}``), a dict read from
  a file or built by ``dict(zip(...))``, or a keyword ``event=`` passed to a function of the package's
  own. None exists today.
- A field the source starts to write through a helper's return value, through ``**`` of anything but
  the forms above, or on a variable that holds more than one event name. Nothing fails until someone
  adds it to ``HELPER_FIELDS``. A field the document lists that is in neither the scan nor that list
  is checked document to source only.
- That ``HELPER_FIELDS`` is right about the source. It is a list kept by hand. It only makes sure the
  document keeps the bullets it names.
- An entry's writer, where it appears and its kind against the source. The index and the examples
  must agree with the entry, so a change to one of these is a change in two places, but none of them
  is read from the source.
- The prose: what an event means, what a field means, when a field is present, and which case of an
  entry with more than one kind is which. The pull request that added this file checked those against
  logs written by stub-driven sessions.
"""

from __future__ import annotations

import ast
import functools
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "contexto"
DOC = ROOT / "docs" / "events.md"
DOC_NAME = "docs/events.md"
TEST_NAME = "tests/test_events_doc.py"

# Functions that write an event whose name is not a literal at the site: (file, function) -> the names
# it can produce. A new one fails ``test_every_event_name_is_read_from_the_source`` until it is listed.
DYNAMIC_SITES: dict[tuple[str, str], frozenset[str]] = {
    ("src/contexto/rewrite.py", "store_failure"): frozenset({"page_conflict", "store_error"}),
}

# Functions whose dicts with an "event" key never reach a session log (say, an SSE event built for a
# stream): (file, function). The scan ignores them. Empty today; an entry no site needs fails.
NOT_SESSION_LOG: frozenset[tuple[str, str]] = frozenset()

# Variables that carry one event across functions, so its fields are stored where the scan cannot follow
# the dict: (file, variable name) -> the event. Every key stored on that name in that file is a field.
FIELD_CARRIERS: dict[tuple[str, str], str] = {
    ("src/contexto/proxy.py", "entry"): "request",
}

# Event names with more than one entry in the document: event -> {source file: the writer named by the
# entry that covers the event as that file writes it}. The proxy's ``request`` line and the engine's nested
# ``request`` event share a name and nothing else, so each one's fields are looked for in its own entry.
SHARED_NAMES: dict[str, dict[str, str]] = {
    "request": {"src/contexto/proxy.py": "proxy", "src/contexto/rewrite.py": "engine"},
}

# Fields the scan cannot read from the source, because they reach the event through a helper's return
# value or on a variable that holds more than one event name: (event, source file) -> the fields. Each must
# be listed under the event, like a field the scan reads. Kept by hand: when the source gains such a field,
# document it and add it here; when the source drops one, remove its bullet and its name here.
HELPER_FIELDS: dict[tuple[str, str], frozenset[str]] = {
    # UsageReader.end_fields(), merged with entry.update(...)
    ("request", "src/contexto/proxy.py"): frozenset({
        "stop_reason", "error_type", "end_event", "response_status", "incomplete_reason", "error_code"}),
    # each tool returns its log fields; _within_room returns the budget fields
    ("mcp_call", "src/contexto/mcp.py"): frozenset({
        "turn_budget", "charged_tokens", "room_left", "limited_by", "side_rooms", "limiting_age_s", "returned",
        "chars", "page_id", "offset", "total_chars", "query_chars", "hits", "total_matches", "in_view_hits",
        "in_view_more", "in_view_checked", "kind", "record_id", "supersedes", "obligation_id"}),
    # RewriteEngine._call adds these to the last event of the list, which is the nested request event
    ("request", "src/contexto/rewrite.py"): frozenset({"main", "conversations"}),
    # ToolCheck.verdict() adds it to the event its helper returns
    ("mcp_tools_missing", "src/contexto/toolcheck.py"): frozenset({"repeated"}),
    # store_failure() builds one dict for both names
    ("store_error", "src/contexto/rewrite.py"): frozenset({"epoch", "operation", "error", "degraded"}),
    ("page_conflict", "src/contexto/rewrite.py"): frozenset({"epoch", "operation", "error", "degraded", "page_id"}),
}

WRITERS = frozenset({"proxy", "engine", "launcher (Claude)", "launcher (Codex)", "MCP server", "store"})
PLACES = frozenset({"own line", "`transform_events`", "`usage_events`"})
KINDS = frozenset({"normal", "notice", "problem"})
NESTED = {"transform_events": "`transform_events`", "usage_events": "`usage_events`"}  # line key -> its place


@dataclass(frozen=True)
class Site:
    """One place in the source that sets a dict's ``event`` value."""

    path: str
    line: int
    scope: str  # the enclosing function, as ``Class.method`` or ``function``; ``<module>`` outside one
    names: frozenset[str] | None  # None: the value is not a string literal
    value: str  # the value's source text, for messages
    fields: tuple[tuple[str, int], ...] = ()  # the literal keys written with it, each with its line
    variable: str | None = None  # the local name the dict is assigned to, if any
    spreads: tuple[str, ...] = ()  # the local variables unpacked into it with ``**``

    @property
    def where(self) -> str:
        return f"{self.path}:{self.line}"


@dataclass
class Module:
    path: str
    sites: list[Site] = field(default_factory=list)
    stores: list[tuple[str, str, str, int]] = field(default_factory=list)  # (scope, variable, key, line)
    spread_stores: list[tuple[str, str, str]] = field(default_factory=list)  # (scope, variable, unpacked variable)
    # (scope, variable) -> the literal keys of the dict displays assigned to it; None when the function also
    # assigns it something that is not a dict display, so its keys are not known
    local_dicts: dict[tuple[str, str], list[tuple[str, int]] | None] = field(default_factory=dict)
    literals: dict[str, set[str]] = field(default_factory=dict)  # scope -> its string literals
    vocabulary: set[str] = field(default_factory=set)  # every string literal and keyword argument name

    def unpacked(self, scope: str, variable: str) -> list[tuple[str, int]]:
        """The keys ``**variable`` adds in ``scope``, as far as the scan knows them."""
        return self.local_dicts.get((scope, variable)) or []


def _string(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _names(value: ast.AST) -> frozenset[str] | None:
    """The event names ``value`` can be: a string literal, or a conditional expression of them."""
    if (text := _string(value)) is not None:
        return frozenset({text})
    if isinstance(value, ast.IfExp):
        body, orelse = _names(value.body), _names(value.orelse)
        return body | orelse if body is not None and orelse is not None else None
    return None


def _display_keys(node: ast.Dict, fields: list[tuple[str, int]], spreads: list[str]) -> None:
    """The literal keys of a dict display other than ``event``, and what it unpacks with ``**``."""
    for key, value in zip(node.keys, node.values):
        if key is None:
            _spread(value, fields, spreads)
        elif (text := _string(key)) is not None and text != "event":
            fields.append((text, value.lineno))


def _spread(value: ast.AST, fields: list[tuple[str, int]], spreads: list[str]) -> None:
    """What ``**value`` adds: the keys of a dict display written in place (either branch of a conditional
    expression), or the name of a local variable, resolved once its function has been scanned."""
    if isinstance(value, ast.Dict):
        _display_keys(value, fields, spreads)
    elif isinstance(value, ast.IfExp):
        _spread(value.body, fields, spreads)
        _spread(value.orelse, fields, spreads)
    elif isinstance(value, ast.Name):
        spreads.append(value.id)


class _Scan(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.module = Module(path)
        self._scope: list[str] = []
        self._assigned: dict[int, str] = {}  # id(dict display) -> the local name it is assigned to

    @property
    def scope(self) -> str:
        return ".".join(self._scope) or "<module>"

    def _site(self, value: ast.AST, fields: list[tuple[str, int]], variable: str | None = None,
              spreads: list[str] | None = None) -> None:
        self.module.sites.append(Site(self.module.path, value.lineno, self.scope, _names(value), ast.unparse(value),
                                      tuple(fields), variable, tuple(spreads or ())))

    def _store(self, target: ast.AST, fields: list[tuple[str, int]], spreads: list[str] | None = None) -> None:
        if isinstance(target, ast.Name):
            self.module.stores.extend((self.scope, target.id, key, line) for key, line in fields)
            self.module.spread_stores.extend((self.scope, target.id, name) for name in spreads or ())

    def _scoped(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self.module.vocabulary.add(node.value)
            self.module.literals.setdefault(self.scope, set()).add(node.value)

    def visit_keyword(self, node: ast.keyword) -> None:
        if node.arg is not None:
            self.module.vocabulary.add(node.arg)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        events = [value for key, value in zip(node.keys, node.values) if _string(key) == "event"]
        if events:
            fields: list[tuple[str, int]] = []
            spreads: list[str] = []
            _display_keys(node, fields, spreads)
            for value in events:
                self._site(value, fields, self._assigned.get(id(node)), spreads)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        builds = isinstance(func, ast.Name) and func.id == "dict"
        method = func.attr if isinstance(func, ast.Attribute) else None
        fields = [(kw.arg, kw.value.lineno) for kw in node.keywords if kw.arg not in (None, "event")]
        spreads: list[str] = []
        for kw in node.keywords:
            if kw.arg is None:
                _spread(kw.value, fields, spreads)
        if builds or method == "update":
            for kw in node.keywords:
                if kw.arg == "event":
                    self._site(kw.value, fields if builds else [], spreads=spreads if builds else None)
        if method == "setdefault" and len(node.args) >= 2 and (key := _string(node.args[0])) is not None:
            if key == "event":
                self._site(node.args[1], [])
            else:
                self._store(func.value, [(key, node.lineno)])
        if method == "update":
            for arg in node.args:
                if isinstance(arg, ast.Dict):
                    _display_keys(arg, fields, spreads)
            self._store(func.value, fields, spreads)
        self.generic_visit(node)

    def _assign(self, target: ast.AST, value: ast.AST | None) -> None:
        if isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple) and len(target.elts) == len(value.elts):
            for inner, part in zip(target.elts, value.elts):
                self._assign(inner, part)
        elif isinstance(target, ast.Name):
            known = self.module.local_dicts.setdefault((self.scope, target.id), [])
            if isinstance(value, ast.Dict):
                self._assigned[id(value)] = target.id
                if known is not None:
                    _display_keys(value, known, [])
            elif value is not None:  # a bare annotation assigns nothing
                self.module.local_dicts[(self.scope, target.id)] = None
        elif isinstance(target, ast.Subscript) and (key := _string(target.slice)) is not None:
            if key == "event" and value is not None:
                self._site(value, [])
            elif key != "event":
                self._store(target.value, [(key, target.lineno)])

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._assign(target, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._assign(node.target, node.value)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._assign(node.target, node.value)
        self.generic_visit(node)


def scan_source(text: str, path: str) -> Module:
    scan = _Scan(path)
    scan.visit(ast.parse(text, filename=path))
    return scan.module


def log_sites(module: Module) -> list[Site]:
    """The module's sites, without those in functions listed in ``NOT_SESSION_LOG``."""
    return [site for site in module.sites if (site.path, site.scope) not in NOT_SESSION_LOG]


@functools.cache
def scan_package() -> list[Module]:
    """Every module of the package, scanned once per test run. Callers do not change the result."""
    return [scan_source(file.read_text(encoding="utf-8"), file.relative_to(ROOT).as_posix())
            for file in sorted(PACKAGE.rglob("*.py"))]


def site_names(site: Site) -> frozenset[str]:
    """The names ``site`` can write: read from the source, else from ``DYNAMIC_SITES``, else none."""
    if site.names is not None:
        return site.names
    return DYNAMIC_SITES.get((site.path, site.scope), frozenset())


def written_names(modules: list[Module]) -> dict[str, list[Site]]:
    """Every event name the source can write, with the sites that write it."""
    out: dict[str, list[Site]] = {}
    for module in modules:
        for site in log_sites(module):
            for name in site_names(site):
                out.setdefault(name, []).append(site)
    return out


def written_fields(modules: list[Module]) -> dict[tuple[str, str], dict[str, str]]:
    """(event name, source file) -> {field: how the test knows of it}: what the scan reads, then
    ``HELPER_FIELDS``. Each value completes the sentence "the entry does not list the field `x`, ..."."""
    out: dict[tuple[str, str], dict[str, str]] = {}

    def add(name: str, key: str, path: str, where: str) -> None:
        out.setdefault((name, path), {}).setdefault(key, where)

    for module in modules:
        held: dict[tuple[str, str], set[str]] = {}  # (scope, variable) -> the event names it holds
        for site in log_sites(module):
            names = site_names(site)
            if site.names is not None:  # a registered site's keys belong to only some of its names
                unpacked = [pair for spread in site.spreads for pair in module.unpacked(site.scope, spread)]
                for name in names:
                    for key, line in [*site.fields, *unpacked]:
                        add(name, key, module.path, f"written at {module.path}:{line}")
            if site.variable is not None:
                held.setdefault((site.scope, site.variable), set()).update(names or {""})
        stores = [*module.stores, *((scope, variable, key, line) for scope, variable, spread in module.spread_stores
                                    for key, line in module.unpacked(scope, spread))]
        for scope, variable, key, line in stores:
            carried = FIELD_CARRIERS.get((module.path, variable))
            names = held.get((scope, variable), set())
            if carried is not None:
                add(carried, key, module.path, f"written at {module.path}:{line} (on `{variable}`, which "
                                               f"FIELD_CARRIERS in {TEST_NAME} says carries `{carried}`)")
            elif len(names) == 1 and "" not in names:
                add(next(iter(names)), key, module.path, f"written at {module.path}:{line}")
    for (name, path), keys in HELPER_FIELDS.items():
        for key in sorted(keys):
            add(name, key, path, f"which HELPER_FIELDS in {TEST_NAME} names as a field {path} writes in a way "
                                 f"the scan cannot read; if the source no longer writes it, remove it from "
                                 f"HELPER_FIELDS instead")
    return out


# The document ------------------------------------------------------------------------------------------------

HEADING = re.compile(r"^### (?P<title>`(?P<name>[a-z][a-z0-9_]*)`.*)$")
FIELD = re.compile(r"^- `([a-z][a-z0-9_]*)`")
FACTS = re.compile(r"^Writer: (?P<writers>[^.]+)\. Where: (?P<places>[^.]+)\. Kind: (?P<kinds>[^.]+)\.$")
INDEX_HEADING = "## Index"
INDEX_ROW = re.compile(r"^\| \[(?P<title>.+)\]\(#(?P<anchor>[^)]+)\) \|")
LINK = re.compile(r"\]\(#([^)\s]+)\)")


def _listed(text: str) -> list[str]:
    return [part.strip() for part in re.split(r",| and ", text) if part.strip()]


def anchor(heading: str) -> str:
    """The anchor GitHub gives a heading: lower case, punctuation dropped, spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


@dataclass
class Entry:
    name: str
    title: str  # the heading's text, as the index repeats it
    line: int
    fields: dict[str, int] = field(default_factory=dict)  # field -> the line that lists it
    facts: list[str] = field(default_factory=list)  # its "Writer: ... Where: ... Kind: ..." lines

    def _fact(self, group: str) -> str:
        match = FACTS.match(self.facts[0]) if len(self.facts) == 1 else None
        return match[group] if match else ""

    @property
    def writers(self) -> list[str]:
        return _listed(self._fact("writers"))

    @property
    def places(self) -> list[str]:
        return _listed(self._fact("places"))

    @property
    def index_row(self) -> str:
        """The row the index must have for this entry."""
        return (f"| [{self.title}](#{anchor(self.title)}) | {self._fact('writers')} | {self._fact('places')} | "
                f"{self._fact('kinds')} |")


@dataclass
class Document:
    entries: list[Entry] = field(default_factory=list)  # in order; a name can have more than one
    examples: list[tuple[int, str]] = field(default_factory=list)  # each ``json`` block: its line, its text
    headings: list[tuple[int, str]] = field(default_factory=list)  # every heading: its line, its text
    index: list[tuple[int, str]] = field(default_factory=list)  # the index table's rows: line, text
    links: list[tuple[int, str]] = field(default_factory=list)  # every ``](#anchor)``: line, anchor

    def named(self, name: str) -> list[Entry]:
        return [entry for entry in self.entries if entry.name == name]


def read_document(text: str) -> Document:
    """The document's entries, ``json`` examples, headings, index rows and internal links.

    An entry runs from its ``### `name``` heading to the next heading of any level. Its fields are its
    bullets that start at the margin with a name in backticks; indented bullets are not fields. The index
    is the table rows that start with a link, between the ``## Index`` heading and the next heading."""
    doc = Document()
    current: Entry | None = None
    in_index = False
    fence: tuple[int, str, list[str]] | None = None
    for number, line in enumerate(text.splitlines(), 1):
        if fence is not None:
            if line.startswith("```"):
                if fence[1] == "json":
                    doc.examples.append((fence[0], "\n".join(fence[2])))
                fence = None
            else:
                fence[2].append(line)
            continue
        if line.startswith("```"):
            fence = (number, line[3:].strip(), [])
            continue
        doc.links.extend((number, target) for target in LINK.findall(line))
        if line.startswith("#"):
            doc.headings.append((number, line.lstrip("#").strip()))
            in_index = line.strip() == INDEX_HEADING
            match = HEADING.match(line)
            current = Entry(match["name"], match["title"], number) if match else None
            if current is not None:
                doc.entries.append(current)
        elif in_index and line.startswith("| ["):
            doc.index.append((number, line.rstrip()))
        elif current is not None:
            if match := FIELD.match(line):
                current.fields.setdefault(match.group(1), number)
            elif line.startswith("Writer:"):
                current.facts.append(line)
    return doc


def entry_for(doc: Document, name: str, path: str) -> Entry | None:
    """The entry that covers the event ``name`` as the source file ``path`` writes it, if the document and
    ``SHARED_NAMES`` settle that; ``shared_name_problems`` reports the cases where they do not."""
    entries = doc.named(name)
    if len(entries) == 1:
        return entries[0]
    writer = SHARED_NAMES.get(name, {}).get(path)
    covering = [entry for entry in entries if writer in entry.writers]
    return covering[0] if len(covering) == 1 else None


def entry_files(doc: Document, entry: Entry, written: dict[str, list[Site]]) -> list[str]:
    """The source files that write the event ``entry`` covers."""
    paths = sorted({site.path for site in written.get(entry.name, [])})
    if len(doc.named(entry.name)) == 1:
        return paths
    return [path for path in paths if SHARED_NAMES.get(entry.name, {}).get(path) in entry.writers]


# The checks: each returns the problems it found, as sentences a contributor can act on -----------------------

def unread_sites(modules: list[Module]) -> list[str]:
    problems = []
    needed = set()
    scopes = {(site.path, site.scope) for module in modules for site in module.sites}
    for path, scope in sorted(NOT_SESSION_LOG - scopes):
        problems.append(f"NOT_SESSION_LOG in {TEST_NAME} lists {scope} in {path}, but no dict there has an "
                        f"\"event\" key; remove the entry")
    for module in modules:
        for site in log_sites(module):
            if site.names is not None:
                continue
            key = (site.path, site.scope)
            needed.add(key)
            if key not in DYNAMIC_SITES:
                problems.append(
                    f"{site.where} (in {site.scope}) writes an event whose name is not a string literal "
                    f"(`{site.value}`), so the scan cannot read it; add ({site.path!r}, {site.scope!r}) to "
                    f"DYNAMIC_SITES in {TEST_NAME} with the event names it can produce, and document each in "
                    f"{DOC_NAME}")
                continue
            literals = module.literals.get(site.scope, set())
            for name in sorted(DYNAMIC_SITES[key] - literals):
                problems.append(
                    f"DYNAMIC_SITES in {TEST_NAME} says {site.scope} in {site.path} can write the event `{name}`, but "
                    f"`{name}` is not a string literal in that function; correct the entry")
    for path, scope in sorted(set(DYNAMIC_SITES) - needed):
        problems.append(
            f"DYNAMIC_SITES in {TEST_NAME} lists {scope} in {path}, but no event there has a name the scan cannot "
            f"read; remove the entry")
    return problems


def undocumented_events(written: dict[str, list[Site]], doc: Document) -> list[str]:
    listed = {entry.name for entry in doc.entries}
    return [f"{DOC_NAME} does not document the event `{name}` written at {', '.join(s.where for s in sites)}; "
            f"add an entry for it: a heading ### `{name}` in the section for its writer, then its writer, where it "
            f"appears, its kind, what it means, and one bullet per field, and add its row to the index. If that "
            f"dict never reaches a session log, add its function to NOT_SESSION_LOG in {TEST_NAME} instead"
            for name, sites in sorted(written.items()) if name not in listed]


def unwritten_events(written: dict[str, list[Site]], doc: Document) -> list[str]:
    return [f"{DOC_NAME}:{entry.line} documents the event `{entry.name}`, but nothing under src/contexto writes it; "
            f"remove the entry, or rename it to the name the source writes"
            for entry in doc.entries if entry.name not in written]


def shared_name_problems(written: dict[str, list[Site]], doc: Document) -> list[str]:
    """Where the document has more than one entry for a name, ``SHARED_NAMES`` must give each source file
    that writes the name to exactly one of them, and leave none of them without a file."""
    problems = []
    counts: dict[str, int] = {}
    for entry in doc.entries:
        counts[entry.name] = counts.get(entry.name, 0) + 1
    for name in sorted(set(SHARED_NAMES) - {name for name, count in counts.items() if count > 1}):
        problems.append(f"SHARED_NAMES in {TEST_NAME} lists `{name}`, but {DOC_NAME} does not have more than one "
                        f"entry for it; remove it from SHARED_NAMES")
    for name, count in sorted(counts.items()):
        if count < 2:
            continue
        entries = doc.named(name)
        where = ", ".join(f"line {entry.line}" for entry in entries)
        files = SHARED_NAMES.get(name)
        if files is None:
            problems.append(f"{DOC_NAME} has {count} entries for `{name}` ({where}); give them one entry, or add "
                            f"`{name}` to SHARED_NAMES in {TEST_NAME} with the writer whose entry covers each "
                            f"source file that writes it")
            continue
        paths = sorted({site.path for site in written.get(name, [])})
        for path in paths:
            if path not in files:
                problems.append(f"{path} writes the event `{name}`, which has {count} entries in {DOC_NAME} "
                                f"({where}); add {path!r} to SHARED_NAMES[{name!r}] in {TEST_NAME} with the writer "
                                f"of the entry that covers it")
            elif len([entry for entry in entries if files[path] in entry.writers]) != 1:
                problems.append(f"SHARED_NAMES in {TEST_NAME} gives `{name}` as {path} writes it to the entry whose "
                                f"writer is {files[path]!r}, but the entries at {where} do not include exactly one "
                                f"with that writer; correct the writer, or the entry's facts line")
        for path in sorted(set(files) - set(paths)):
            problems.append(f"SHARED_NAMES in {TEST_NAME} lists {path} for `{name}`, but no `{name}` event is written "
                            f"there; remove it, or name the file that writes it now")
        for entry in entries:
            if not entry_files(doc, entry, written):
                problems.append(f"{DOC_NAME}:{entry.line} no source file in SHARED_NAMES[{name!r}] in {TEST_NAME} "
                                f"is given to this entry (writer {', '.join(entry.writers) or 'unknown'}); remove "
                                f"the entry, or correct SHARED_NAMES")
    return problems


def undocumented_fields(fields: dict[tuple[str, str], dict[str, str]], doc: Document) -> list[str]:
    problems = []
    for (name, path), keys in sorted(fields.items()):
        entry = entry_for(doc, name, path)
        if entry is None:  # no entry, or more than one and SHARED_NAMES does not choose: reported elsewhere
            continue
        for key, where in sorted(keys.items()):
            if key not in entry.fields:
                problems.append(f"{DOC_NAME}:{entry.line} the entry {entry.title} does not list the field `{key}`, "
                                f"{where}; add a bullet - `{key}`: ... under that heading")
    return problems


def unwritten_fields(modules: list[Module], written: dict[str, list[Site]], doc: Document) -> list[str]:
    vocabulary = {module.path: module.vocabulary for module in modules}
    problems = []
    for entry in doc.entries:
        paths = entry_files(doc, entry, written)
        for key, line in entry.fields.items():
            if paths and not any(key in vocabulary[path] for path in paths):
                problems.append(
                    f"{DOC_NAME}:{line} lists the field `{key}` under `{entry.name}`, but `{key}` does not appear in "
                    f"{', '.join(paths)}, which {'writes' if len(paths) == 1 else 'write'} that event; remove the "
                    f"bullet, or rename it to the key the source writes")
    return problems


def missing_facts(doc: Document) -> list[str]:
    form = "Writer: <who>. Where: <where>. Kind: <kind>."
    problems = []
    for entry in doc.entries:
        if len(entry.facts) != 1 or not (match := FACTS.match(entry.facts[0])):
            problems.append(f"{DOC_NAME}:{entry.line} the entry for `{entry.name}` needs exactly one line of the form "
                            f"`{form}` right after its heading")
            continue
        for label, allowed in (("writer", WRITERS), ("place", PLACES), ("kind", KINDS)):
            given = _listed(match[label + "s"])
            for value in given:
                if value not in allowed:
                    problems.append(f"{DOC_NAME}:{entry.line} the entry for `{entry.name}` names the {label} "
                                    f"{value!r}; use one of: {', '.join(sorted(allowed))}")
            if len(set(given)) != len(given):
                problems.append(f"{DOC_NAME}:{entry.line} the entry for `{entry.name}` names a {label} twice")
    return problems


def _row_anchor(row: str) -> str:
    """The anchor an index row links to, or the row itself when it does not start with a link."""
    match = INDEX_ROW.match(row)
    return match["anchor"] if match else row


def index_problems(doc: Document) -> list[str]:
    """The index has one row per entry, in the entries' order, each repeating its entry's heading and facts."""
    if not doc.entries:
        return [f"{DOC_NAME} has no entries"]
    if not doc.index:
        return [f"{DOC_NAME} has no index: add a heading `{INDEX_HEADING}` and under it a table with one row per "
                f"entry, the first being: {doc.entries[0].index_row}"]
    problems = []
    wanted = {anchor(entry.title): entry for entry in doc.entries}
    rows: dict[str, tuple[int, str]] = {}
    for line, text in doc.index:
        target = _row_anchor(text)
        if target in rows:
            problems.append(f"{DOC_NAME}:{line} the index has a second row for #{target}; remove it")
        elif target not in wanted:
            problems.append(f"{DOC_NAME}:{line} the index has a row that matches no entry's heading ({text}); remove "
                            f"it, or correct its link to the entry's heading")
        rows.setdefault(target, (line, text))
    previous = None
    for entry in doc.entries:
        target = anchor(entry.title)
        if target not in rows:
            after = f"after the row for {previous.title}" if previous else "as the first row"
            problems.append(f"{DOC_NAME}: the index has no row for the entry {entry.title} at line {entry.line}; add "
                            f"this row to the table under `{INDEX_HEADING}`, {after}: {entry.index_row}")
        elif rows[target][1] != entry.index_row:
            problems.append(f"{DOC_NAME}:{rows[target][0]} the index row for {entry.title} does not repeat its entry "
                            f"at line {entry.line}; make the two agree. From the entry as it is now the row would "
                            f"be: {entry.index_row}")
        previous = entry
    if not problems:  # the same rows as the entries: are they in the entries' order?
        for (line, text), entry in zip(doc.index, doc.entries):
            if _row_anchor(text) != anchor(entry.title):
                problems.append(f"{DOC_NAME}:{line} the index rows are not in the order of the entries; the row here "
                                f"should be the one for {entry.title}")
                break
    return problems


def broken_links(doc: Document) -> list[str]:
    anchors: dict[str, int] = {}
    problems = []
    for line, heading in doc.headings:
        target = anchor(heading)
        if target in anchors:
            problems.append(f"{DOC_NAME}:{line} the heading `{heading}` has the same anchor (#{target}) as the "
                            f"heading at line {anchors[target]}, so links cannot tell them apart; reword one")
        anchors.setdefault(target, line)
    for line, target in doc.links:
        if target not in anchors:
            problems.append(f"{DOC_NAME}:{line} links to #{target}, but no heading has that anchor; correct the "
                            f"link, or restore the heading it names")
    return problems


def bad_examples(doc: Document) -> list[str]:
    problems = []

    def check(event: object, line: int, place: str) -> None:
        if not isinstance(event, dict) or not isinstance(event.get("event"), str):
            problems.append(f"{DOC_NAME}:{line} the json example is not an object with an `event` name")
            return
        name = event["event"]
        if list(event) != sorted(event):
            problems.append(f"{DOC_NAME}:{line} the json example of `{name}` does not have its keys sorted, as "
                            f"every line in the file has")
        entries = doc.named(name)
        if not entries:
            problems.append(f"{DOC_NAME}:{line} the json example shows the event `{name}`, which has no entry")
            return
        shown = [entry for entry in entries if place in entry.places]
        if len(shown) != 1:
            problems.append(f"{DOC_NAME}:{line} the json example shows `{name}` as {place}, but "
                            f"{'no entry' if not shown else 'more than one entry'} for `{name}` says Where: {place}; "
                            f"correct the example, or the entry's facts line")
            return
        for key in sorted(set(event) - set(shown[0].fields) - {"event", "ts"}):
            problems.append(f"{DOC_NAME}:{line} the json example of `{name}` has the key `{key}`, which the entry "
                            f"{shown[0].title} at line {shown[0].line} does not list; list it, or take it out of the "
                            f"example")
        for key, nested in NESTED.items():
            for inner in event.get(key) or []:
                check(inner, line, nested)

    for line, text in doc.examples:
        try:
            check(json.loads(text), line, "own line")
        except ValueError as error:
            problems.append(f"{DOC_NAME}:{line} the json example is not valid JSON ({error})")
    return problems


def _report(problems: list[str]) -> str:
    return "\n" + "\n".join(f"- {problem}" for problem in problems)


def _document() -> Document:
    assert DOC.is_file(), f"{DOC_NAME} is missing; it is the reference for every event in a session's log.jsonl"
    return read_document(DOC.read_text(encoding="utf-8"))


# The scan itself ---------------------------------------------------------------------------------------------

FORMS = '''
def literal(log):
    log({"event": "a_literal", "count": 1,
         "more": 2})

def continued(log):
    log({"path": "p", "event":
         "a_continued"})

def conditional(error):
    return {"event": "a_conflict" if error else "an_error"}

def built(extra):
    first = dict(event="a_dict_call", size=3)
    second = {}
    second.update(event="an_update")
    third = {}
    third.setdefault("event", "a_setdefault")
    fourth = {}
    fourth["event"] = "a_subscript"
    return first, second, third, fourth

class Helper:
    def made(self, kind):
        event = {"event": "a_variable_holder", "at": kind}
        event["late"] = True
        event.update(later=1)
        event.update({"latest": 2})
        event.setdefault("default", 3)
        return {**event, "unpacked": True}

    def dynamic(self, kind):
        return {"event": kind, "epoch": 1}

def tupled():
    event, warning = {"event": "a_tuple_target"}, None
    event["error"] = "x"
    return event, warning

def spread(log, flag, helper):
    sizing = {"budget": 1, "overhead": 2}
    opaque = {"hidden": 1}
    opaque = helper()
    log({"event": "a_spread", **sizing, **({"inline": 1} if flag else {}), **opaque, **helper()})
    later = {"event": "a_spread_store"}
    later.update(status=1, **sizing)
    return dict(event="a_spread_call", **sizing)
'''


def test_the_scan_reads_every_form_it_claims_to():
    module = scan_source(FORMS, "forms.py")
    by_name = {name: site for site in module.sites for name in site.names or ()}
    assert set(by_name) == {"a_literal", "a_continued", "a_conflict", "an_error", "a_dict_call", "an_update",
                            "a_setdefault", "a_subscript", "a_variable_holder", "a_tuple_target", "a_spread",
                            "a_spread_store", "a_spread_call"}
    assert by_name["a_continued"].line == 8  # the line of the name, not of the dict's opening brace
    assert by_name["a_conflict"] is by_name["an_error"]
    unread = [site for site in module.sites if site.names is None]
    assert [(site.scope, site.value, site.line) for site in unread] == [("Helper.dynamic", "kind", 33)]
    fields = {name: set(keys) for (name, path), keys in written_fields([module]).items() if path == "forms.py"}
    assert fields["a_literal"] == {"count", "more"}
    assert fields["a_dict_call"] == {"size"}
    assert fields["a_variable_holder"] == {"at", "late", "later", "latest", "default"}  # not "unpacked"
    assert fields["a_tuple_target"] == {"error"}
    # ** of a local dict display and of a display written in place; not of a variable that is also assigned
    # something else, nor of a call
    assert fields["a_spread"] == {"budget", "overhead", "inline"}
    assert fields["a_spread_store"] == {"status", "budget", "overhead"}
    assert fields["a_spread_call"] == {"budget", "overhead"}
    assert "kind" not in str(fields)  # an unread site contributes no fields


def test_an_unread_site_must_be_registered():
    module = scan_source(FORMS, "forms.py")
    assert unread_sites([module])[0].startswith(
        "forms.py:33 (in Helper.dynamic) writes an event whose name is not a string literal (`kind`), so the scan "
        "cannot read it; add ('forms.py', 'Helper.dynamic') to DYNAMIC_SITES in tests/test_events_doc.py")


# The package against the document ----------------------------------------------------------------------------

def test_every_event_name_is_read_from_the_source():
    problems = unread_sites(scan_package())
    assert not problems, _report(problems)


def test_every_event_the_source_writes_is_documented():
    problems = undocumented_events(written_names(scan_package()), _document())
    assert not problems, _report(problems)


def test_every_documented_event_is_written_by_the_source():
    problems = unwritten_events(written_names(scan_package()), _document())
    assert not problems, _report(problems)


def test_entries_that_share_a_name_are_told_apart():
    problems = shared_name_problems(written_names(scan_package()), _document())
    assert not problems, _report(problems)


def test_every_field_the_scan_finds_or_the_test_names_is_documented():
    problems = undocumented_fields(written_fields(scan_package()), _document())
    assert not problems, _report(problems)


def test_every_documented_field_appears_in_a_module_that_writes_the_event():
    modules = scan_package()
    problems = unwritten_fields(modules, written_names(modules), _document())
    assert not problems, _report(problems)


def test_every_entry_states_its_writer_place_and_kind():
    problems = missing_facts(_document())
    assert not problems, _report(problems)


def test_the_index_has_one_row_per_entry_that_repeats_its_facts():
    problems = index_problems(_document())
    assert not problems, _report(problems)


def test_every_link_inside_the_document_names_a_heading():
    problems = broken_links(_document())
    assert not problems, _report(problems)


def test_every_json_example_shows_documented_events_and_fields():
    doc = _document()
    assert doc.examples, f"{DOC_NAME} has no json example"
    problems = bad_examples(doc)
    assert not problems, _report(problems)


# The checks have teeth: the same functions, on the real document with one change -----------------------------

def _changed(old: str, new: str, *, after: str = "") -> Document:
    """The document with the first ``old`` after the text ``after`` replaced by ``new``."""
    text = DOC.read_text(encoding="utf-8")
    start = text.index(after)
    assert old in text[start:], f"{DOC_NAME} no longer has {old!r} after {after!r}; update this test's mutation"
    return read_document(text[:start] + text[start:].replace(old, new, 1))


def test_removing_an_entry_or_inventing_one_is_reported():
    text = DOC.read_text(encoding="utf-8")
    written = written_names(scan_package())
    without = _changed("\n### `rotation`\n", "\n### rotation\n")
    assert any(problem.startswith(f"{DOC_NAME} does not document the event `rotation` written at "
                                  "src/contexto/rewrite.py:") for problem in undocumented_events(written, without))
    invented = read_document(text + "\n### `made_up_event`\n\nWriter: proxy. Where: own line. Kind: normal.\n")
    line = len(text.splitlines()) + 2
    assert (f"{DOC_NAME}:{line} documents the event `made_up_event`, but nothing under src/contexto writes it; "
            "remove the entry, or rename it to the name the source writes") in unwritten_events(written, invented)


def test_removing_a_field_or_inventing_one_is_reported():
    modules = scan_package()
    fields = written_fields(modules)
    without = _changed("\n- `retained_messages`:", "\n- retained_messages:")
    assert any("the entry `rotation` does not list the field `retained_messages`, written at src/contexto/rewrite.py:"
               in problem for problem in undocumented_fields(fields, without))
    invented = _changed("\n- `retained_messages`:", "\n- `made_up_field`: x\n- `retained_messages`:")
    assert any("lists the field `made_up_field` under `rotation`" in problem
               for problem in unwritten_fields(modules, written_names(modules), invented))


def test_a_field_missing_from_one_of_two_entries_with_the_same_name_is_reported():
    # `epoch` and `over_bound` are fields of the proxy's request line and of the engine's nested request
    # event. Each entry must list them itself: the other entry's bullet does not count.
    fields = written_fields(scan_package())
    for key in ("epoch", "over_bound"):
        line_entry = _changed(f"\n- `{key}`:", f"\n- {key}:", after="\n### `request`\n")
        assert any(f"the entry `request` does not list the field `{key}`, written at src/contexto/proxy.py:"
                   in problem for problem in undocumented_fields(fields, line_entry)), key
        nested = _changed(f"\n- `{key}`:", f"\n- {key}:", after="\n### `request` (")
        assert any(f"the entry `request` (the engine's nested event) does not list the field `{key}`, written at "
                   "src/contexto/rewrite.py:" in problem for problem in undocumented_fields(fields, nested)), key


def test_a_field_that_arrives_by_unpacking_or_through_a_helper_is_reported_when_removed():
    fields = written_fields(scan_package())
    for name, key, how in (("bound_too_small", "fixed_overhead", "written at src/contexto/rewrite.py:"),  # **sizing
                           ("request", "stop_reason", "which HELPER_FIELDS in"),  # UsageReader.end_fields()
                           ("mcp_call", "charged_tokens", "which HELPER_FIELDS in")):  # _within_room's budget
        without = _changed(f"\n- `{key}`:", f"\n- {key}:", after=f"\n### `{name}`\n")
        assert any(f"the entry `{name}` does not list the field `{key}`, {how}" in problem
                   for problem in undocumented_fields(fields, without)), (name, key)


def test_an_index_row_that_is_missing_or_disagrees_with_its_entry_is_reported():
    row = "| [`rotation`](#rotation) | engine | `transform_events` | normal |"
    missing = index_problems(_changed(row + "\n", ""))
    assert len(missing) == 1 and missing[0].endswith(f"after the row for `count_tokens`: {row}"), missing
    # an entry whose kind (or writer, or place) changes without its row, and a row that changes without its entry
    for changed in (_changed("Kind: normal.", "Kind: problem.", after="\n### `rotation`\n"),
                    _changed(row, row.replace("normal", "notice"))):
        problems = index_problems(changed)
        assert len(problems) == 1 and "the index row for `rotation` does not repeat its entry" in problems[0], problems
    first = "| [`request`](#request) |"
    moved = read_document(DOC.read_text(encoding="utf-8").replace(row + "\n", "").replace(first, f"{row}\n{first}", 1))
    problems = index_problems(moved)
    assert len(problems) == 1 and "the index rows are not in the order of the entries" in problems[0], problems


def test_a_link_to_a_heading_that_does_not_exist_is_reported():
    broken = broken_links(_changed("](#rotation)", "](#rotatoin)"))
    assert len(broken) == 1 and "links to #rotatoin, but no heading has that anchor" in broken[0], broken


def test_an_example_that_shows_an_event_where_its_entry_does_not_say_is_reported():
    moved = bad_examples(_changed('{"epoch":2,"event":"rotation"', '{"epoch":2,"event":"estimate_error"'))
    assert any("shows `estimate_error` as `transform_events`, but no entry" in problem for problem in moved), moved
