"""docs/events.md is a reference for every event the package writes to a session's ``log.jsonl``.

People reading a log, and commands that read one, rely on it, so it must not go stale. These tests
read the package source with ``ast`` and the document's headings, facts lines, bullets and index, and
fail when they disagree. Each failure says which file to change and how.

What they guarantee:

- Names, both ways. Every event name the source can write has an entry in the document (a heading
  ``### `name```), and every entry names an event the source can write.
- The scan is not silently incomplete in the forms it reads: a dict display with an ``"event"`` key,
  any call with an ``event=`` keyword (``dict(event=...)``, ``x.update(event=...)``, a helper of the
  package's own), ``x.setdefault("event", ...)`` and ``x["event"] = ...``. The name is read when it is a
  string literal, a conditional expression of string literals, or a local variable or module constant
  that is assigned only those. Any other value fails unless the site is listed in ``DYNAMIC_SITES``
  with the names it can produce. A site is a function and the expression it uses for the name, so a
  second site in a listed function is not covered by the first. An entry no site needs fails too. A
  dict that never reaches a log is exempt only by listing its site in ``NOT_SESSION_LOG``.
- Fields, source to document, for the fields the scan can read. Each of these is listed under its
  event (a bullet ``- `field`: ...``):
  - a key written as a literal next to an event name the scan read;
  - a key unpacked into the event with ``**``, from a dict display written in place (either branch of
    a conditional expression) or from a local variable that the same function assigns only dict
    displays to;
  - a key stored later on the same local variable (``v["key"] = ...``, ``v.update(key=...)``,
    ``v.update({"key": ...})``, ``v.update(**local)``, ``v.setdefault("key", ...)``) when that
    variable holds one event name in its function and is bound to nothing else there;
  - for an event listed in ``CARRIED_EVENTS``, whose dict is filled in by several functions of one
    file: a key stored anywhere in that file on a variable with the name the event's dict is assigned
    to. The name is read from the source, so renaming the variable throughout needs no change here.
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
- The tables below name things that exist: a site, an event, a file or a field that the source no
  longer has fails, with the entry to remove or correct.

What they do not guarantee:

- An event built in a form the scan does not read: a computed key (``{key: name}``), a dict
  comprehension, ``dict(pairs)`` or ``dict(zip(...))``, ``dataclasses.asdict``, ``x.__setitem__``, a
  loop that assigns to ``x["event"]``, or a dict read from a file or parsed from a string. None exists
  today.
- The names a ``DYNAMIC_SITES`` entry lists. They are kept by hand; the test checks only that each is
  a string literal somewhere in the package.
- A field the source starts to write through a helper's return value, through ``**`` of anything but
  the forms above, on a variable that holds more than one event name, or on an attribute
  (``self.entry["key"]``). Nothing fails until someone adds it to ``HELPER_FIELDS``. A field the
  document lists that is in neither the scan nor that list is checked document to source only.
- That ``HELPER_FIELDS`` is right about the source. It is a list kept by hand. It only makes sure the
  document keeps the bullets it names. ``mcp_call`` is the event that depends on it most: each tool
  returns its own log fields.
- A ``CARRIED_EVENTS`` dict under another name. A key stored on it through a parameter with a
  different name is not read, and an unrelated variable of the same name in that file is read as the
  event.
- An entry's writer, where it appears and its kind against the source. The index and the examples
  must agree with the entry, so a change to one of these is a change in two places, but none of them
  is read from the source.
- The prose: what an event means, what a field means, when a field is present, and which case of an
  entry with more than one kind is which. The pull request that added this file checked those against
  logs written by stub-driven sessions.
"""

from __future__ import annotations

import ast
import dataclasses
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

# Sites that write an event whose name the scan cannot read: (file, function, the expression used for the
# name) -> the names it can produce. A new one fails ``test_every_event_name_is_read_from_the_source`` until
# it is listed. Empty today; an entry no site needs fails.
DYNAMIC_SITES: dict[tuple[str, str, str], frozenset[str]] = {}

# Sites whose dict with an "event" key never reaches a session log (say, an SSE event built for a stream):
# (file, function, the expression used for the name), as the failure message prints it. The scan ignores
# that site and no other. Empty today; an entry no site needs fails.
NOT_SESSION_LOG: frozenset[tuple[str, str, str]] = frozenset()

# Events whose dict is filled in by several functions of one file, so its fields are stored where the scan
# cannot follow the dict: (file, event). The scan reads the name of the variable the event's dict is assigned
# to in that file, and takes every key stored on a variable of that name in that file as a field of the event.
CARRIED_EVENTS: frozenset[tuple[str, str]] = frozenset({
    ("src/contexto/proxy.py", "request"),
})

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
    # RewriteEngine._call adds these to the last event of the list, which is the nested request event
    ("request", "src/contexto/rewrite.py"): frozenset({"main", "conversations"}),
    # each tool returns its log fields; _within_room returns the budget fields
    ("mcp_call", "src/contexto/mcp.py"): frozenset({
        "turn_budget", "charged_tokens", "room_left", "limited_by", "side_rooms", "limiting_age_s", "returned",
        "chars", "page_id", "offset", "total_chars", "query_chars", "hits", "total_matches", "in_view_hits",
        "in_view_more", "in_view_checked", "kind", "record_id", "supersedes", "obligation_id"}),
    # ToolCheck.verdict() adds it to the event its helper returns
    ("mcp_tools_missing", "src/contexto/toolcheck.py"): frozenset({"repeated"}),
    # store_failure() stores these on one variable that holds both names
    ("store_error", "src/contexto/rewrite.py"): frozenset({"degraded"}),
    ("page_conflict", "src/contexto/rewrite.py"): frozenset({"degraded", "page_id"}),
}

WRITERS = frozenset({"proxy", "engine", "launcher (Claude)", "launcher (Codex)", "MCP server", "store"})
PLACES = frozenset({"own line", "`transform_events`", "`usage_events`"})
KINDS = frozenset({"normal", "notice", "problem"})
NESTED = {"transform_events": "`transform_events`", "usage_events": "`usage_events`"}  # line key -> its place


@dataclass(frozen=True)
class Tables:
    """The five tables above. Every check takes them as an argument, so the tests of the checks themselves
    run on tables of their own and never on the package's."""

    dynamic_sites: dict[tuple[str, str, str], frozenset[str]] = field(default_factory=dict)
    not_session_log: frozenset[tuple[str, str, str]] = frozenset()
    carried_events: frozenset[tuple[str, str]] = frozenset()
    shared_names: dict[str, dict[str, str]] = field(default_factory=dict)
    helper_fields: dict[tuple[str, str], frozenset[str]] = field(default_factory=dict)


TABLES = Tables(DYNAMIC_SITES, NOT_SESSION_LOG, CARRIED_EVENTS, SHARED_NAMES, HELPER_FIELDS)


@dataclass(frozen=True)
class Site:
    """One place in the source that sets a dict's ``event`` value."""

    path: str
    line: int
    scope: str  # the enclosing function, as ``Class.method`` or ``function``; ``<module>`` outside one
    names: frozenset[str] | None  # None: the scan cannot read the value
    value: str  # the value's source text, for messages and for the tables' keys
    fields: tuple[tuple[str, int], ...] = ()  # the literal keys written with it, each with its line
    variable: str | None = None  # the local name the dict is assigned to, if any
    spreads: tuple[str, ...] = ()  # the local variables unpacked into it with ``**``
    reference: str | None = None  # the variable the value is, when it is a bare name

    @property
    def where(self) -> str:
        return f"{self.path}:{self.line}"

    @property
    def key(self) -> tuple[str, str, str]:
        """How ``DYNAMIC_SITES`` and ``NOT_SESSION_LOG`` name this site."""
        return self.path, self.scope, self.value


@dataclass
class Module:
    path: str
    sites: list[Site] = field(default_factory=list)
    stores: list[tuple[str, str, str, int]] = field(default_factory=list)  # (scope, variable, key, line)
    spread_stores: list[tuple[str, str, str]] = field(default_factory=list)  # (scope, variable, unpacked variable)
    # (scope, variable) -> the literal keys of the dicts assigned to it; None when the function also binds
    # it to something that is not a dict display, so neither its keys nor what it holds is known
    local_dicts: dict[tuple[str, str], list[tuple[str, int]] | None] = field(default_factory=dict)
    # (scope, variable) -> the strings assigned to it; None when it is also bound to anything else
    local_strings: dict[tuple[str, str], set[str] | None] = field(default_factory=dict)
    literals: set[str] = field(default_factory=set)  # every string literal
    vocabulary: set[str] = field(default_factory=set)  # every string literal and keyword argument name

    def unpacked(self, scope: str, variable: str) -> list[tuple[str, int]]:
        """The keys ``**variable`` adds in ``scope``, as far as the scan knows them."""
        return self.local_dicts.get((scope, variable)) or []

    def strings(self, scope: str, variable: str) -> frozenset[str] | None:
        """The event names the variable can be: what its function assigns it, if only strings; for a name
        the function never binds, what the module assigns it. None when that is not known."""
        for where in (scope, "<module>"):
            if (where, variable) in self.local_strings:
                found = self.local_strings[(where, variable)]
                return frozenset(found) if found else None
        return None


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


def _builds_dict(value: ast.AST | None) -> bool:
    return isinstance(value, ast.Dict) or (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                                           and value.func.id == "dict")


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


def _call_keys(node: ast.Call, fields: list[tuple[str, int]], spreads: list[str]) -> None:
    """The keyword arguments of a call other than ``event``, and what it unpacks with ``**``."""
    for kw in node.keywords:
        if kw.arg is None:
            _spread(kw.value, fields, spreads)
        elif kw.arg != "event":
            fields.append((kw.arg, kw.value.lineno))


class _Scan(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.module = Module(path)
        self._scope: list[str] = []
        self._assigned: dict[int, str] = {}  # id(a dict display or dict() call) -> the local name it is assigned to

    @property
    def scope(self) -> str:
        return ".".join(self._scope) or "<module>"

    def _site(self, value: ast.AST, fields: list[tuple[str, int]], variable: str | None = None,
              spreads: list[str] | None = None) -> None:
        self.module.sites.append(Site(self.module.path, value.lineno, self.scope, _names(value), ast.unparse(value),
                                      tuple(fields), variable, tuple(spreads or ()),
                                      value.id if isinstance(value, ast.Name) else None))

    def _store(self, target: ast.AST, fields: list[tuple[str, int]], spreads: list[str] | None = None) -> None:
        if isinstance(target, ast.Name):
            self.module.stores.extend((self.scope, target.id, key, line) for key, line in fields)
            self.module.spread_stores.extend((self.scope, target.id, name) for name in spreads or ())

    def _unknown(self, target: ast.AST | None) -> None:
        """``target`` is bound to something the scan does not read: a parameter, a loop variable, a call."""
        for node in ast.walk(target) if target is not None else ():
            if isinstance(node, (ast.Name, ast.arg)):
                name = node.id if isinstance(node, ast.Name) else node.arg
                self.module.local_dicts[(self.scope, name)] = None
                self.module.local_strings[(self.scope, name)] = None

    def _scoped(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self._scope.append(node.name)
        if not isinstance(node, ast.ClassDef):
            self._unknown(node.args)
        self.generic_visit(node)
        self._scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self.module.vocabulary.add(node.value)
            self.module.literals.add(node.value)

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
        builds = _builds_dict(node)
        method = func.attr if isinstance(func, ast.Attribute) else None
        fields: list[tuple[str, int]] = []
        spreads: list[str] = []
        _call_keys(node, fields, spreads)
        for kw in node.keywords:
            if kw.arg == "event":  # dict(event=...), x.update(event=...), or a helper that takes the name
                self._site(kw.value, fields if builds else [], self._assigned.get(id(node)),
                           spreads if builds else None)
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
            if value is None:  # a bare annotation binds nothing
                return
            key = (self.scope, target.id)
            dicts = self.module.local_dicts.setdefault(key, [])
            strings = self.module.local_strings.setdefault(key, set())
            if _builds_dict(value):
                self._assigned[id(value)] = target.id
                self.module.local_strings[key] = None
                if dicts is not None:
                    if isinstance(value, ast.Dict):
                        _display_keys(value, dicts, [])
                    else:
                        _call_keys(value, dicts, [])
            elif (names := _names(value)) is not None:
                self.module.local_dicts[key] = None
                if strings is not None:
                    strings.update(names)
            else:
                self._unknown(target)
        elif isinstance(target, ast.Subscript) and (key := _string(target.slice)) is not None:
            if key == "event" and value is not None:
                self._site(value, [])
            elif key != "event":
                self._store(target.value, [(key, target.lineno)])
        elif isinstance(target, (ast.Tuple, ast.List, ast.Starred)):
            self._unknown(target)

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

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._assign(node.target, node.value)
        self.generic_visit(node)

    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
        self._unknown(node.target)
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            self._unknown(item.optional_vars)
        self.generic_visit(node)

    visit_AsyncWith = visit_With


def scan_source(text: str, path: str) -> Module:
    """Scan one module. A site whose name is a bare variable is read through that variable's assignments,
    once the whole module has been seen."""
    scan = _Scan(path)
    scan.visit(ast.parse(text, filename=path))
    module = scan.module
    module.sites = [dataclasses.replace(site, names=module.strings(site.scope, site.reference))
                    if site.names is None and site.reference is not None else site for site in module.sites]
    return module


@functools.cache
def scan_package() -> list[Module]:
    """Every module of the package, scanned once per test run. Callers do not change the result."""
    modules = [scan_source(file.read_text(encoding="utf-8"), file.relative_to(ROOT).as_posix())
               for file in sorted(PACKAGE.rglob("*.py"))]
    assert any(module.sites for module in modules), (
        f"the scan found no event under {PACKAGE}: these tests read the package source and must run from a "
        f"checkout that has it at src/contexto, beside tests/")
    return modules


def log_sites(module: Module, tables: Tables) -> list[Site]:
    """The module's sites, without those listed in ``NOT_SESSION_LOG``."""
    return [site for site in module.sites if site.key not in tables.not_session_log]


def site_names(site: Site, tables: Tables) -> frozenset[str]:
    """The names ``site`` can write: read from the source, else from ``DYNAMIC_SITES``, else none."""
    if site.names is not None:
        return site.names
    return tables.dynamic_sites.get(site.key, frozenset())


def written_names(modules: list[Module], tables: Tables) -> dict[str, list[Site]]:
    """Every event name the source can write, with the sites that write it."""
    out: dict[str, list[Site]] = {}
    for module in modules:
        for site in log_sites(module, tables):
            for name in site_names(site, tables):
                out.setdefault(name, []).append(site)
    return out


def carriers(module: Module, tables: Tables) -> dict[str, set[str]]:
    """variable name -> the ``CARRIED_EVENTS`` events of this file whose dict is assigned to that name."""
    out: dict[str, set[str]] = {}
    for site in log_sites(module, tables):
        for name in site_names(site, tables) if site.variable is not None else ():
            if (module.path, name) in tables.carried_events:
                out.setdefault(site.variable, set()).add(name)
    return out


def written_fields(modules: list[Module], tables: Tables) -> dict[tuple[str, str], dict[str, str]]:
    """(event name, source file) -> {field: how the test knows of it}: what the scan reads, then
    ``HELPER_FIELDS``. Each value completes the sentence "the entry does not list the field `x`, ..."."""
    out: dict[tuple[str, str], dict[str, str]] = {}

    def add(name: str, key: str, path: str, where: str) -> None:
        out.setdefault((name, path), {}).setdefault(key, where)

    for module in modules:
        held: dict[tuple[str, str], set[str]] = {}  # (scope, variable) -> the event names it holds
        for site in log_sites(module, tables):
            names = site_names(site, tables)
            if site.names is not None:  # a registered site's keys belong to only some of its names
                unpacked = [pair for spread in site.spreads for pair in module.unpacked(site.scope, spread)]
                for name in names:
                    for key, line in [*site.fields, *unpacked]:
                        add(name, key, module.path, f"written at {module.path}:{line}")
            if site.variable is not None:
                held.setdefault((site.scope, site.variable), set()).update(names or {""})
        stores = [*module.stores, *((scope, variable, key, line) for scope, variable, spread in module.spread_stores
                                    for key, line in module.unpacked(scope, spread))]
        carrying = carriers(module, tables)
        for scope, variable, key, line in stores:
            carried = carrying.get(variable, set())
            names = held.get((scope, variable), set())
            if len(carried) == 1:
                event = next(iter(carried))
                add(event, key, module.path, f"written at {module.path}:{line} (on `{variable}`, the variable that "
                                             f"holds `{event}` there: see CARRIED_EVENTS in {TEST_NAME})")
            elif len(names) == 1 and "" not in names and module.local_dicts.get((scope, variable)) is not None:
                add(next(iter(names)), key, module.path, f"written at {module.path}:{line}")
    for (name, path), keys in tables.helper_fields.items():
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
    lines: list[str] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)  # in order; a name can have more than one
    examples: list[tuple[int, str]] = field(default_factory=list)  # each ``json`` block: its line, its text
    headings: list[tuple[int, str]] = field(default_factory=list)  # every heading: its line, its text
    index: list[tuple[int, str]] = field(default_factory=list)  # the index table's rows: line, text
    links: list[tuple[int, str]] = field(default_factory=list)  # every ``](#anchor)``: line, anchor

    def named(self, name: str) -> list[Entry]:
        return [entry for entry in self.entries if entry.name == name]

    def with_line(self, number: int, *replacement: str) -> Document:
        """This document with line ``number`` replaced by ``replacement`` (no lines: the line removed)."""
        return read_document("\n".join([*self.lines[:number - 1], *replacement, *self.lines[number:]]))


def read_document(text: str) -> Document:
    """The document's entries, ``json`` examples, headings, index rows and internal links.

    An entry runs from its ``### `name``` heading to the next heading of any level. Its fields are its
    bullets that start at the margin with a name in backticks; indented bullets are not fields. The index
    is the table rows that start with a link, between the ``## Index`` heading and the next heading."""
    doc = Document(text.splitlines())
    current: Entry | None = None
    in_index = False
    fence: tuple[int, str, list[str]] | None = None
    for number, line in enumerate(doc.lines, 1):
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


def entry_for(doc: Document, name: str, path: str, tables: Tables) -> Entry | None:
    """The entry that covers the event ``name`` as the source file ``path`` writes it, if the document and
    ``SHARED_NAMES`` settle that; ``shared_name_problems`` reports the cases where they do not."""
    entries = doc.named(name)
    if len(entries) == 1:
        return entries[0]
    writer = tables.shared_names.get(name, {}).get(path)
    covering = [entry for entry in entries if writer in entry.writers]
    return covering[0] if len(covering) == 1 else None


def entry_files(doc: Document, entry: Entry, written: dict[str, list[Site]], tables: Tables) -> list[str]:
    """The source files that write the event ``entry`` covers."""
    paths = sorted({site.path for site in written.get(entry.name, [])})
    if len(doc.named(entry.name)) == 1:
        return paths
    return [path for path in paths if tables.shared_names.get(entry.name, {}).get(path) in entry.writers]


# The checks: each returns the problems it found, as sentences a contributor can act on -----------------------

READ_FORMS = ("a string literal, a conditional expression of string literals, or a local variable or module "
              "constant that is assigned only those")


def unread_sites(modules: list[Module], tables: Tables) -> list[str]:
    problems = []
    needed = set()
    literals = set().union(*(module.literals for module in modules)) if modules else set()
    keys = {site.key for module in modules for site in module.sites}
    for path, scope, value in sorted(tables.not_session_log - keys):
        problems.append(f"NOT_SESSION_LOG in {TEST_NAME} lists ({path!r}, {scope!r}, {value!r}), but {scope} in "
                        f"{path} has no dict whose \"event\" value is written `{value}`; remove the entry, or "
                        f"correct it to the site as it is now")
    for module in modules:
        for site in log_sites(module, tables):
            if site.names is not None:
                continue
            needed.add(site.key)
            if site.key not in tables.dynamic_sites:
                problems.append(
                    f"{site.where} (in {site.scope}) writes an event whose name the scan cannot read "
                    f"(`{site.value}`). It reads {READ_FORMS}. Either write the name in one of those forms, or "
                    f"add {site.key!r} to DYNAMIC_SITES in {TEST_NAME} with the event names it can produce (each "
                    f"must be a string literal somewhere under src/contexto) and document each in {DOC_NAME}. If "
                    f"that dict never reaches a session log, add {site.key!r} to NOT_SESSION_LOG instead")
                continue
            for name in sorted(tables.dynamic_sites[site.key] - literals):
                problems.append(
                    f"DYNAMIC_SITES in {TEST_NAME} says {site.scope} in {site.path} can write the event `{name}`, but "
                    f"`{name}` is not a string literal anywhere under src/contexto, so nothing can pass it there; "
                    f"correct the entry. A name that is put together from parts must be written out in full "
                    f"where it is chosen for the scan to find it")
    for path, scope, value in sorted(set(tables.dynamic_sites) - needed):
        problems.append(
            f"DYNAMIC_SITES in {TEST_NAME} lists ({path!r}, {scope!r}, {value!r}), but {scope} in {path} has no "
            f"event whose name is written `{value}` and that the scan cannot read; remove the entry, or correct "
            f"it to the site as it is now")
    return problems


def table_problems(modules: list[Module], tables: Tables) -> list[str]:
    """``CARRIED_EVENTS`` and ``HELPER_FIELDS`` name things the source has. (``DYNAMIC_SITES`` and
    ``NOT_SESSION_LOG`` are checked by ``unread_sites``, ``SHARED_NAMES`` by ``shared_name_problems``.)"""
    problems = []
    written = written_names(modules, tables)
    words = set().union(*(module.vocabulary for module in modules)) if modules else set()
    for module in modules:
        held = carriers(module, tables)
        for path, event in sorted(tables.carried_events):
            if path == module.path and not any(event in events for events in held.values()):
                problems.append(f"CARRIED_EVENTS in {TEST_NAME} lists the event `{event}` in {path}, but no variable "
                                f"there is assigned a dict with that event any more; remove the entry, or name the "
                                f"file that builds `{event}` now")
        for variable, events in sorted(held.items()):
            if len(events) > 1:
                problems.append(f"{module.path} assigns the events {', '.join(sorted(events))} to variables named "
                                f"`{variable}`, and CARRIED_EVENTS in {TEST_NAME} lists more than one of them, so a "
                                f"key stored on `{variable}` cannot be given to one event; give the dicts different "
                                f"names")
    for path, event in sorted(tables.carried_events):
        if path not in {module.path for module in modules}:
            problems.append(f"CARRIED_EVENTS in {TEST_NAME} lists {path}, which does not exist; remove the entry, or "
                            f"name the file that builds `{event}` now")
    for (event, _path), keys in sorted(tables.helper_fields.items()):
        if event not in written:
            problems.append(f"HELPER_FIELDS in {TEST_NAME} names fields of the event `{event}`, which nothing under "
                            f"src/contexto writes; remove the entry, or rename it to the name the source writes")
            continue
        for key in sorted(keys - words):
            problems.append(f"HELPER_FIELDS in {TEST_NAME} names the field `{key}` of `{event}`, but `{key}` does not "
                            f"appear anywhere under src/contexto; remove it from HELPER_FIELDS, or rename it to the "
                            f"key the source writes")
    return problems


def undocumented_events(written: dict[str, list[Site]], doc: Document) -> list[str]:
    listed = {entry.name for entry in doc.entries}
    return [f"{DOC_NAME} does not document the event `{name}` written at {', '.join(s.where for s in sites)}; "
            f"add an entry for it: a heading ### `{name}` in the section for its writer, then its writer, where it "
            f"appears, its kind, what it means, and one bullet per field, and add its row to the index. If that "
            f"dict never reaches a session log, add {sites[0].key!r} to NOT_SESSION_LOG in {TEST_NAME} instead"
            for name, sites in sorted(written.items()) if name not in listed]


def unwritten_events(written: dict[str, list[Site]], doc: Document) -> list[str]:
    return [f"{DOC_NAME}:{entry.line} documents the event `{entry.name}`, but nothing under src/contexto writes it; "
            f"remove the entry, or rename it to the name the source writes"
            for entry in doc.entries if entry.name not in written]


def shared_name_problems(written: dict[str, list[Site]], doc: Document, tables: Tables) -> list[str]:
    """Where the document has more than one entry for a name, ``SHARED_NAMES`` must give each source file
    that writes the name to exactly one of them, and leave none of them without a file."""
    problems = []
    counts: dict[str, int] = {}
    for entry in doc.entries:
        counts[entry.name] = counts.get(entry.name, 0) + 1
    for name in sorted(set(tables.shared_names) - {name for name, count in counts.items() if count > 1}):
        problems.append(f"SHARED_NAMES in {TEST_NAME} lists `{name}`, but {DOC_NAME} does not have more than one "
                        f"entry for it; remove it from SHARED_NAMES")
    for name, count in sorted(counts.items()):
        if count < 2:
            continue
        entries = doc.named(name)
        where = ", ".join(f"line {entry.line}" for entry in entries)
        files = tables.shared_names.get(name)
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
            if not entry_files(doc, entry, written, tables):
                problems.append(f"{DOC_NAME}:{entry.line} no source file in SHARED_NAMES[{name!r}] in {TEST_NAME} "
                                f"is given to this entry (writer {', '.join(entry.writers) or 'unknown'}); remove "
                                f"the entry, or correct SHARED_NAMES")
    return problems


def undocumented_fields(fields: dict[tuple[str, str], dict[str, str]], doc: Document, tables: Tables) -> list[str]:
    problems = []
    for (name, path), keys in sorted(fields.items()):
        entry = entry_for(doc, name, path, tables)
        if entry is None:  # no entry, or more than one and SHARED_NAMES does not choose: reported elsewhere
            continue
        for key, where in sorted(keys.items()):
            if key not in entry.fields:
                problems.append(f"{DOC_NAME}:{entry.line} the entry {entry.title} does not list the field `{key}`, "
                                f"{where}; add a bullet - `{key}`: ... under that heading")
    return problems


def unwritten_fields(modules: list[Module], written: dict[str, list[Site]], doc: Document,
                     tables: Tables) -> list[str]:
    vocabulary = {module.path: module.vocabulary for module in modules}
    problems = []
    for entry in doc.entries:
        paths = entry_files(doc, entry, written, tables)
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


# The checks themselves, on a source and a document of their own ----------------------------------------------
# These tests never read the tables above, the package or docs/events.md, so an entry added to a table, or an
# event or a field renamed in the source and the document together, cannot fail them.

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

A_CONSTANT = "a_constant"

def named(log, error):
    kind = "a_first" if error else "a_second"
    failure = {"event": kind, "at": 1}
    failure["late"] = 2
    log(failure)
    log({"event": A_CONSTANT})
    log({"event": f"a_{kind}"})

def keyword(emit, log):
    emit(log, event="a_keyword_call", size=1)

def rebound(rows):
    event = {"event": "a_rebound", "code": 0}
    for event in rows:
        event["unrelated"] = 1
    held = dict(event="a_dict_holder", code=0)
    held["late"] = 1

def frame(text):
    return {"event": "error", "data": text}
'''
ALL_FORMS = {"a_literal", "a_continued", "a_conflict", "an_error", "a_dict_call", "an_update", "a_setdefault",
             "a_subscript", "a_variable_holder", "a_tuple_target", "a_spread", "a_spread_store", "a_spread_call",
             "a_first", "a_second", "a_constant", "a_keyword_call", "a_rebound", "a_dict_holder", "error"}
DYNAMIC_FORM = ("forms.py", "Helper.dynamic", "kind")
FSTRING_FORM = ("forms.py", "named", "f'a_{kind}'")
FRAME_FORM = ("forms.py", "frame", "'error'")
NONE = Tables()


def test_the_scan_reads_every_form_it_claims_to():
    module = scan_source(FORMS, "forms.py")
    by_name = {name: site for site in module.sites for name in site.names or ()}
    assert set(by_name) == ALL_FORMS
    assert by_name["a_continued"].line == 8  # the line of the name, not of the dict's opening brace
    assert by_name["a_conflict"] is by_name["an_error"]
    assert by_name["a_first"] is by_name["a_second"]  # a local variable, read through its one assignment
    unread = [site for site in module.sites if site.names is None]
    assert [site.key for site in unread] == [DYNAMIC_FORM, FSTRING_FORM]  # a parameter; a computed name
    fields = {name: set(keys) for (name, path), keys in written_fields([module], NONE).items()}
    assert fields["a_literal"] == {"count", "more"}
    assert fields["a_dict_call"] == {"size"}
    assert fields["a_variable_holder"] == {"at", "late", "later", "latest", "default"}  # not "unpacked"
    assert fields["a_tuple_target"] == {"error"}
    # ** of a local dict display and of a display written in place; not of a variable that is also assigned
    # something else, nor of a call
    assert fields["a_spread"] == {"budget", "overhead", "inline"}
    assert fields["a_spread_store"] == {"status", "budget", "overhead"}
    assert fields["a_spread_call"] == {"budget", "overhead"}
    # a key next to a name read through a variable belongs to each name; a key stored later on a variable
    # that holds two names belongs to neither
    assert fields["a_first"] == fields["a_second"] == {"at"}
    assert "a_keyword_call" not in fields  # a helper's other keyword arguments are not known to be fields
    assert fields["a_rebound"] == {"code"}  # the loop binds `event` to something else: its stores are not read
    assert fields["a_dict_holder"] == {"code", "late"}
    assert "kind" not in str(fields)  # an unread site contributes no fields


def test_an_unread_site_must_be_listed_and_the_message_says_how():
    module = scan_source(FORMS, "forms.py")
    first, second = unread_sites([module], NONE)
    assert first.startswith(
        "forms.py:33 (in Helper.dynamic) writes an event whose name the scan cannot read (`kind`). It reads a string "
        "literal, a conditional expression of string literals, or a local variable or module constant that is "
        "assigned only those. Either write the name in one of those forms, or add ('forms.py', 'Helper.dynamic', "
        "'kind') to DYNAMIC_SITES in tests/test_events_doc.py with the event names it can produce")
    assert first.endswith("add ('forms.py', 'Helper.dynamic', 'kind') to NOT_SESSION_LOG instead")
    assert second.startswith("forms.py:57 (in named) writes an event whose name the scan cannot read (`f'a_{kind}'`)")


def test_a_listed_site_can_name_what_a_caller_passes_and_only_that_site_is_covered():
    module = scan_source(FORMS, "forms.py")
    # the names need only be string literals somewhere in the source, as a caller's argument is
    listed = Tables(dynamic_sites={DYNAMIC_FORM: frozenset({"a_literal"}), FSTRING_FORM: frozenset({"a_first"})})
    assert unread_sites([module], listed) == []
    assert "a_literal" in written_names([module], listed)
    # an entry covers its own site, not another in the same function, and its names must exist somewhere
    one = Tables(dynamic_sites={DYNAMIC_FORM: frozenset({"never_written"})})
    problems = unread_sites([module], one)
    assert len(problems) == 2 and problems[1].startswith("forms.py:57 (in named) writes an event whose name")
    assert "`never_written` is not a string literal anywhere under src/contexto" in problems[0]
    stale = Tables(dynamic_sites={**listed.dynamic_sites, ("forms.py", "literal", "name"): frozenset({"a_literal"})})
    assert unread_sites([module], stale) == [
        "DYNAMIC_SITES in tests/test_events_doc.py lists ('forms.py', 'literal', 'name'), but literal in forms.py "
        "has no event whose name is written `name` and that the scan cannot read; remove the entry, or correct it "
        "to the site as it is now"]


def test_not_session_log_exempts_one_site_and_a_stale_entry_is_reported():
    module = scan_source(FORMS, "forms.py")
    exempt = Tables(not_session_log=frozenset({FRAME_FORM, DYNAMIC_FORM, FSTRING_FORM}))
    assert unread_sites([module], exempt) == []
    assert set(written_names([module], exempt)) == ALL_FORMS - {"error"}
    assert ("error", "forms.py") not in written_fields([module], exempt)
    stale = Tables(not_session_log=exempt.not_session_log | {("forms.py", "frame", "'gone'")})
    assert unread_sites([module], stale) == [
        "NOT_SESSION_LOG in tests/test_events_doc.py lists ('forms.py', 'frame', \"'gone'\"), but frame in forms.py "
        "has no dict whose \"event\" value is written `'gone'`; remove the entry, or correct it to the site as it "
        "is now"]


LINE_SOURCE = '''
def line(log):
    entry = {"event": "shared", "size": 1, "epoch": 0}
    fill(entry)
    log(entry)

def fill(entry):
    entry["status"] = 200
    entry["transform_events"] = []
    entry.update(ending())

def ending():
    return {"ended": "x"}
'''
INNER_SOURCE = '''
def nested(events):
    sizing = {"budget": 1}
    events.append({"event": "shared", "epoch": 0, **sizing})
    events.append({"event": "plain", "count": 1})
'''
SAMPLE_DOC = '''# Sample

## Index

| Event | Writer | Where | Kind |
| --- | --- | --- | --- |
| [`shared`](#shared) | proxy | own line | normal |
| [`shared` (nested)](#shared-nested) | engine | `transform_events` | notice, problem |
| [`plain`](#plain) | engine | `transform_events` | normal |

## Entries

### `shared`

Writer: proxy. Where: own line. Kind: normal.

See [`plain`](#plain).

- `size`: a size.
- `epoch`: an epoch.
- `status`: a status.
- `ended`: how it ended.
- `transform_events`: the nested events.

### `shared` (nested)

Writer: engine. Where: `transform_events`. Kind: notice, problem.

- `epoch`: an epoch.
- `budget`: a budget.

### `plain`

Writer: engine. Where: `transform_events`. Kind: normal.

- `count`: a count.

```json
{"epoch":0,"event":"shared","size":1,"transform_events":[{"budget":1,"epoch":0,"event":"shared"}]}
```
'''
SAMPLE = Tables(carried_events=frozenset({("line.py", "shared")}),
                shared_names={"shared": {"line.py": "proxy", "inner.py": "engine"}},
                helper_fields={("shared", "line.py"): frozenset({"ended"})})


def _sample(text: str = SAMPLE_DOC, tables: Tables = SAMPLE) -> dict[str, list[str]]:
    """Every check's problems for the sample source (two files, as the proxy and the engine are) and ``text``."""
    modules = [scan_source(LINE_SOURCE, "line.py"), scan_source(INNER_SOURCE, "inner.py")]
    doc = read_document(text)
    written = written_names(modules, tables)
    return {"sites": unread_sites(modules, tables), "tables": table_problems(modules, tables),
            "undocumented": undocumented_events(written, doc), "unwritten": unwritten_events(written, doc),
            "shared": shared_name_problems(written, doc, tables),
            "fields": undocumented_fields(written_fields(modules, tables), doc, tables),
            "words": unwritten_fields(modules, written, doc, tables), "facts": missing_facts(doc),
            "index": index_problems(doc), "links": broken_links(doc), "examples": bad_examples(doc)}


def _only(problems: dict[str, list[str]], check: str) -> str:
    """The one problem, found by ``check`` and by no other."""
    assert {name for name, found in problems.items() if found} == {check}, problems
    assert len(problems[check]) == 1, problems[check]
    return problems[check][0]


def test_the_sample_document_passes_every_check():
    assert not any(_sample().values()), _sample()


def test_an_entry_that_is_removed_or_made_up_is_reported():
    removed = _sample(SAMPLE_DOC.replace("### `plain`", "### plain"))
    assert removed["undocumented"][0].startswith("docs/events.md does not document the event `plain` written at "
                                                 "inner.py:5; add an entry for it")
    made_up = _sample(SAMPLE_DOC + "\n### `made_up`\n\nWriter: proxy. Where: own line. Kind: normal.\n")
    assert made_up["unwritten"] == ["docs/events.md:42 documents the event `made_up`, but nothing under "
                                    "src/contexto writes it; remove the entry, or rename it to the name the "
                                    "source writes"]


def test_each_of_two_entries_with_one_name_must_list_its_own_fields():
    # both entries list `epoch`, which both files write: each entry must list it itself, and the other
    # entry's bullet must not stand in for it
    for bullet, title, how in (("- `epoch`: an epoch.\n- `status`", "`shared`", "written at line.py:3"),
                               ("- `epoch`: an epoch.\n- `budget`", "`shared` (nested)", "written at inner.py:4"),
                               ("- `budget`: a budget.", "`shared` (nested)", "written at inner.py:3")):  # by **
        key = bullet.split("`")[1]
        assert bullet in SAMPLE_DOC
        found = _sample(SAMPLE_DOC.replace(bullet, bullet.replace(f"- `{key}`", f"- {key}", 1)))["fields"]
        assert len(found) == 1 and f"the entry {title} does not list the field `{key}`, {how}" in found[0], found
    unlisted = _only(_sample(tables=dataclasses.replace(SAMPLE, shared_names={})), "shared")
    assert unlisted.startswith("docs/events.md has 2 entries for `shared` (line 13, line 25); give them one entry, "
                               "or add `shared` to SHARED_NAMES")


def test_a_field_on_a_carried_event_or_named_in_helper_fields_is_reported_when_its_bullet_goes():
    carried = _only(_sample(SAMPLE_DOC.replace("- `status`", "- status")), "fields")
    assert ("does not list the field `status`, written at line.py:8 (on `entry`, the variable that holds `shared` "
            "there: see CARRIED_EVENTS in tests/test_events_doc.py); add a bullet - `status`: ...") in carried
    helper = _only(_sample(SAMPLE_DOC.replace("- `ended`", "- ended")), "fields")
    assert ("does not list the field `ended`, which HELPER_FIELDS in tests/test_events_doc.py names as a field "
            "line.py writes in a way the scan cannot read; if the source no longer writes it, remove it from "
            "HELPER_FIELDS instead; add a bullet - `ended`: ...") in helper


def test_a_carried_event_is_followed_under_whatever_name_its_variable_has():
    renamed = scan_source(LINE_SOURCE.replace("entry", "rec"), "line.py")  # the variable renamed throughout
    assert set(written_fields([renamed], SAMPLE)[("shared", "line.py")]) == {
        "size", "epoch", "status", "transform_events", "ended"}
    assert table_problems([renamed], SAMPLE) == []


def test_a_table_entry_the_source_no_longer_has_is_reported():
    gone = dataclasses.replace(SAMPLE, carried_events=frozenset({("line.py", "plain")}))
    assert _sample(tables=gone)["tables"] == [
        "CARRIED_EVENTS in tests/test_events_doc.py lists the event `plain` in line.py, but no variable there is "
        "assigned a dict with that event any more; remove the entry, or name the file that builds `plain` now"]
    stale = dataclasses.replace(SAMPLE, helper_fields={("shared", "line.py"): frozenset({"ended", "dropped"}),
                                                       ("removed", "line.py"): frozenset({"size"})})
    assert _sample(tables=stale)["tables"] == [
        "HELPER_FIELDS in tests/test_events_doc.py names fields of the event `removed`, which nothing under "
        "src/contexto writes; remove the entry, or rename it to the name the source writes",
        "HELPER_FIELDS in tests/test_events_doc.py names the field `dropped` of `shared`, but `dropped` does not "
        "appear anywhere under src/contexto; remove it from HELPER_FIELDS, or rename it to the key the source writes"]


def test_a_documented_field_no_writing_module_mentions_is_reported():
    made_up = _sample(SAMPLE_DOC.replace("- `count`: a count.", "- `count`: a count.\n- `made_up`: x"))
    assert _only(made_up, "words") == ("docs/events.md:37 lists the field `made_up` under `plain`, but `made_up` "
                                       "does not appear in inner.py, which writes that event; remove the bullet, or "
                                       "rename it to the key the source writes")
    # `status` is a word in line.py only, so it cannot be listed under the entry that inner.py writes
    other = _sample(SAMPLE_DOC.replace("- `budget`: a budget.", "- `budget`: a budget.\n- `status`: x"))
    assert _only(other, "words").startswith("docs/events.md:31 lists the field `status` under `shared`, but `status` "
                                            "does not appear in inner.py, which writes that event")


def test_an_index_row_that_is_missing_moved_or_unlike_its_entry_is_reported():
    row = "| [`plain`](#plain) | engine | `transform_events` | normal |\n"
    assert _only(_sample(SAMPLE_DOC.replace(row, "")), "index").endswith(
        "add this row to the table under `## Index`, after the row for `shared` (nested): " + row.strip())
    first = "| [`shared`](#shared) |"
    moved = _only(_sample(SAMPLE_DOC.replace(row, "").replace(first, row + first)), "index")
    assert moved == ("docs/events.md:7 the index rows are not in the order of the entries; the row here should be "
                     "the one for `shared`")
    for changed in (SAMPLE_DOC.replace(row, row.replace("normal", "notice")),  # the row alone
                    SAMPLE_DOC.replace("`transform_events`. Kind: normal.", "`transform_events`. Kind: notice.")):
        assert "the index row for `plain` does not repeat its entry at line 32" in _only(_sample(changed), "index")


def test_a_link_to_no_heading_and_an_example_in_the_wrong_place_are_reported():
    assert _only(_sample(SAMPLE_DOC.replace("](#plain).", "](#plian).")), "links") == (
        "docs/events.md:17 links to #plian, but no heading has that anchor; correct the link, or restore the "
        "heading it names")
    moved = _only(_sample(SAMPLE_DOC.replace('{"epoch":0,"event":"shared","size":1,', '{"event":"plain",')), "examples")
    assert moved == ("docs/events.md:38 the json example shows `plain` as own line, but no entry for `plain` says "
                     "Where: own line; correct the example, or the entry's facts line")
    extra = _only(_sample(SAMPLE_DOC.replace('"event":"shared"}]', '"event":"shared","status":1}]')), "examples")
    assert "has the key `status`, which the entry `shared` (nested) at line 25 does not list" in extra


# The package against the document ----------------------------------------------------------------------------

def test_every_event_name_is_read_from_the_source():
    problems = unread_sites(scan_package(), TABLES)
    assert not problems, _report(problems)


def test_the_tables_name_things_that_exist():
    problems = table_problems(scan_package(), TABLES)
    assert not problems, _report(problems)


def test_every_event_the_source_writes_is_documented():
    problems = undocumented_events(written_names(scan_package(), TABLES), _document())
    assert not problems, _report(problems)


def test_every_documented_event_is_written_by_the_source():
    problems = unwritten_events(written_names(scan_package(), TABLES), _document())
    assert not problems, _report(problems)


def test_entries_that_share_a_name_are_told_apart():
    problems = shared_name_problems(written_names(scan_package(), TABLES), _document(), TABLES)
    assert not problems, _report(problems)


def test_every_field_the_scan_finds_or_the_test_names_is_documented():
    problems = undocumented_fields(written_fields(scan_package(), TABLES), _document(), TABLES)
    assert not problems, _report(problems)


def test_every_documented_field_appears_in_a_module_that_writes_the_event():
    modules = scan_package()
    problems = unwritten_fields(modules, written_names(modules, TABLES), _document(), TABLES)
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


# The same checks on the real document with one line changed. The test picks what to change from the document
# as it is, so an event or a field renamed in the source and the document together does not fail it.

def test_the_real_document_fails_when_a_field_an_index_row_or_an_entry_goes():
    doc = _document()
    modules = scan_package()
    fields = written_fields(modules, TABLES)
    written = written_names(modules, TABLES)
    for (name, path), keys in sorted(fields.items()):  # every field the test knows of, in its own entry
        entry = entry_for(doc, name, path, TABLES)
        assert entry is not None, (name, path)
        for key in sorted(keys):
            bullet = doc.lines[entry.fields[key] - 1]
            without = doc.with_line(entry.fields[key], bullet.replace(f"- `{key}`", f"- {key}", 1))
            assert any(f"the entry {entry.title} does not list the field `{key}`, " in problem
                       for problem in undocumented_fields(fields, without, TABLES)), (name, path, key)
    for entry in doc.entries:
        row = next(number for number, text in doc.index if text == entry.index_row)
        assert any(f"the index has no row for the entry {entry.title} " in problem
                   for problem in index_problems(doc.with_line(row))), entry.title
        if len(doc.named(entry.name)) == 1:
            gone = doc.with_line(entry.line, entry.title)  # no longer a heading
            assert any(problem.startswith(f"{DOC_NAME} does not document the event `{entry.name}` written at ")
                       for problem in undocumented_events(written, gone)), entry.title
