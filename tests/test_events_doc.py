"""docs/events.md is a reference for every event the package writes to a session's ``log.jsonl``.

People reading a log, and commands that read one, rely on it, so it must not go stale. These tests
read the package source with ``ast`` and the document's headings and bullets, and fail when the two
disagree. Each failure says which file to change and how.

What they guarantee:

- Names, both ways. Every event name the source can write has an entry in the document (a heading
  ``### `name```), and every entry names an event the source can write.
- The scan is not silently incomplete in the forms it reads: a dict display with an ``"event"`` key,
  ``dict(event=...)``, ``x.update(event=...)``, ``x.setdefault("event", ...)`` and ``x["event"] = ...``.
  When the value is not a string literal (or a conditional expression of string literals), the test
  fails unless the function is listed in ``DYNAMIC_SITES`` with the names it can produce. Each listed
  name must be a string literal in that function, and an entry no site needs any more fails too. A
  function whose dicts never reach a log is exempt only by listing it in ``NOT_SESSION_LOG``.
- Fields, source to document. A key written as a literal next to a literal event name is listed under
  that event (a bullet ``- `field`: ...``). So is a key stored later on the same local variable
  (``v["key"] = ...``, ``v.update(key=...)``, ``v.update({"key": ...})``, ``v.setdefault("key", ...)``)
  when that variable holds one event name in its function, and a key stored on a variable listed in
  ``FIELD_CARRIERS``, which names the variables that carry one event across functions.
- Fields, document to source, weakly. A field the document lists appears as a string literal or a
  keyword argument somewhere in a module that writes the event. This catches a field that was renamed
  or removed; it does not prove that the field is written on that event.
- Each entry states its writer, where it appears and its kind in one line, in a fixed vocabulary.
- Each ``json`` example in the document is one object with sorted keys, as a line in the file has,
  whose event and keys the document lists, nested events included.

What they do not guarantee:

- An event built in a form the scan does not read: a computed key (``{key: name}``), a dict read from
  a file or built by ``dict(zip(...))``, or a keyword ``event=`` passed to a function of the package's
  own. None exists today.
- Fields that reach an event through ``**`` unpacking, through a helper's return value, or through a
  variable that holds more than one event name are checked document to source only.
- The prose: what an event means, what a field means, and whether "where it appears" is right. The
  pull request that added this file checked those against logs written by stub-driven sessions.
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

WRITERS = frozenset({"proxy", "engine", "launcher (Claude)", "launcher (Codex)", "MCP server", "store"})
PLACES = frozenset({"own line", "`transform_events`", "`usage_events`"})
KINDS = frozenset({"normal", "notice", "problem"})


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

    @property
    def where(self) -> str:
        return f"{self.path}:{self.line}"


@dataclass
class Module:
    path: str
    sites: list[Site] = field(default_factory=list)
    stores: list[tuple[str, str, str, int]] = field(default_factory=list)  # (scope, variable, key, line)
    literals: dict[str, set[str]] = field(default_factory=dict)  # scope -> its string literals
    vocabulary: set[str] = field(default_factory=set)  # every string literal and keyword argument name


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


class _Scan(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.module = Module(path)
        self._scope: list[str] = []
        self._assigned: dict[int, str] = {}  # id(dict display) -> the local name it is assigned to

    @property
    def scope(self) -> str:
        return ".".join(self._scope) or "<module>"

    def _site(self, value: ast.AST, fields: list[tuple[str, int]], variable: str | None = None) -> None:
        self.module.sites.append(Site(self.module.path, value.lineno, self.scope, _names(value), ast.unparse(value),
                                      tuple(fields), variable))

    def _store(self, target: ast.AST, key: str, line: int) -> None:
        if isinstance(target, ast.Name):
            self.module.stores.append((self.scope, target.id, key, line))

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
        keys = [(_string(key), value) for key, value in zip(node.keys, node.values)]
        for key, value in keys:
            if key == "event":
                others = [(k, v.lineno) for k, v in keys if k is not None and k != "event"]
                self._site(value, others, self._assigned.get(id(node)))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        builds = isinstance(func, ast.Name) and func.id == "dict"
        method = func.attr if isinstance(func, ast.Attribute) else None
        keywords = [(kw.arg, kw.value) for kw in node.keywords if kw.arg is not None]
        if builds or method == "update":
            for name, value in keywords:
                if name == "event":
                    self._site(value, [(k, v.lineno) for k, v in keywords if k != "event"] if builds else [])
        if method == "setdefault" and len(node.args) >= 2 and (key := _string(node.args[0])) is not None:
            if key == "event":
                self._site(node.args[1], [])
            else:
                self._store(func.value, key, node.lineno)
        if method == "update":
            for name, value in keywords:
                if name != "event":
                    self._store(func.value, name, value.lineno)
            for arg in node.args:
                if isinstance(arg, ast.Dict):
                    for key in arg.keys:
                        if (text := _string(key)) is not None and text != "event":
                            self._store(func.value, text, key.lineno)
        self.generic_visit(node)

    def _assign(self, target: ast.AST, value: ast.AST | None) -> None:
        if isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple) and len(target.elts) == len(value.elts):
            for inner, part in zip(target.elts, value.elts):
                self._assign(inner, part)
        elif isinstance(target, ast.Name) and isinstance(value, ast.Dict):
            self._assigned[id(value)] = target.id
        elif isinstance(target, ast.Subscript) and (key := _string(target.slice)) is not None:
            if key == "event" and value is not None:
                self._site(value, [])
            elif key != "event":
                self._store(target.value, key, target.lineno)

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


def written_fields(modules: list[Module]) -> dict[str, dict[str, str]]:
    """event name -> {field: ``file:line`` of the first place that writes it}, as far as the scan can tell."""
    out: dict[str, dict[str, str]] = {}

    def add(name: str, key: str, path: str, line: int, note: str = "") -> None:
        out.setdefault(name, {}).setdefault(key, f"{path}:{line}{note}")

    for module in modules:
        held: dict[tuple[str, str], set[str]] = {}  # (scope, variable) -> the event names it holds
        for site in log_sites(module):
            names = site_names(site)
            if site.names is not None:  # a registered site's keys belong to only some of its names
                for name in names:
                    for key, line in site.fields:
                        add(name, key, module.path, line)
            if site.variable is not None:
                held.setdefault((site.scope, site.variable), set()).update(names or {""})
        for scope, variable, key, line in module.stores:
            carried = FIELD_CARRIERS.get((module.path, variable))
            names = held.get((scope, variable), set())
            if carried is not None:
                add(carried, key, module.path, line, f" (on `{variable}`, which FIELD_CARRIERS in {TEST_NAME} says "
                                                     f"carries `{carried}`)")
            elif len(names) == 1 and "" not in names:
                add(next(iter(names)), key, module.path, line)
    return out


# The document ------------------------------------------------------------------------------------------------

HEADING = re.compile(r"^### `([a-z][a-z0-9_]*)`")
FIELD = re.compile(r"^- `([a-z][a-z0-9_]*)`")
FACTS = re.compile(r"^Writer: (?P<writers>[^.]+)\. Where: (?P<places>[^.]+)\. Kind: (?P<kind>[a-z]+)\.$")


@dataclass
class Entry:
    name: str
    line: int
    fields: dict[str, int] = field(default_factory=dict)  # field -> the line that lists it
    facts: list[str] = field(default_factory=list)  # its "Writer: ... Where: ... Kind: ..." lines


def read_document(text: str) -> tuple[list[Entry], list[tuple[int, str]]]:
    """The document's entries, in order (a name can have more than one), and its ``json`` examples.

    An entry runs from its ``### `name``` heading to the next heading of any level. Its fields are its
    bullets that start at the margin with a name in backticks; indented bullets are not fields."""
    entries: list[Entry] = []
    examples: list[tuple[int, str]] = []
    current: Entry | None = None
    fence: tuple[int, str, list[str]] | None = None
    for number, line in enumerate(text.splitlines(), 1):
        if fence is not None:
            if line.startswith("```"):
                if fence[1] == "json":
                    examples.append((fence[0], "\n".join(fence[2])))
                fence = None
            else:
                fence[2].append(line)
        elif line.startswith("```"):
            fence = (number, line[3:].strip(), [])
        elif line.startswith("#"):
            match = HEADING.match(line)
            current = Entry(match.group(1), number) if match else None
            if current is not None:
                entries.append(current)
        elif current is not None:
            if match := FIELD.match(line):
                current.fields.setdefault(match.group(1), number)
            elif line.startswith("Writer:"):
                current.facts.append(line)
    return entries, examples


def documented(entries: list[Entry]) -> dict[str, dict[str, int]]:
    """event name -> {field: line}, joining the entries that share a name."""
    out: dict[str, dict[str, int]] = {}
    for entry in entries:
        fields = out.setdefault(entry.name, {})
        for name, line in entry.fields.items():
            fields.setdefault(name, line)
    return out


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


def undocumented_events(written: dict[str, list[Site]], entries: list[Entry]) -> list[str]:
    listed = {entry.name for entry in entries}
    return [f"{DOC_NAME} does not document the event `{name}` written at {', '.join(s.where for s in sites)}; "
            f"add an entry for it: a heading ### `{name}` in the section for its writer, then its writer, where it "
            f"appears, its kind, what it means, and one bullet per field. If that dict never reaches a session "
            f"log, add its function to NOT_SESSION_LOG in {TEST_NAME} instead"
            for name, sites in sorted(written.items()) if name not in listed]


def unwritten_events(written: dict[str, list[Site]], entries: list[Entry]) -> list[str]:
    return [f"{DOC_NAME}:{entry.line} documents the event `{entry.name}`, but nothing under src/contexto writes it; "
            f"remove the entry, or rename it to the name the source writes"
            for entry in entries if entry.name not in written]


def undocumented_fields(fields: dict[str, dict[str, str]], entries: list[Entry]) -> list[str]:
    listed = documented(entries)
    return [f"{DOC_NAME}'s entry for `{name}` does not list the field `{key}` written at {where}; add a bullet "
            f"- `{key}`: ... under ### `{name}`"
            for name, keys in sorted(fields.items()) if name in listed
            for key, where in sorted(keys.items()) if key not in listed[name]]


def unwritten_fields(modules: list[Module], written: dict[str, list[Site]], entries: list[Entry]) -> list[str]:
    vocabulary = {module.path: module.vocabulary for module in modules}
    problems = []
    for entry in entries:
        paths = sorted({site.path for site in written.get(entry.name, [])})
        for key, line in entry.fields.items():
            if paths and not any(key in vocabulary[path] for path in paths):
                problems.append(
                    f"{DOC_NAME}:{line} lists the field `{key}` under `{entry.name}`, but `{key}` does not appear in "
                    f"{', '.join(paths)}, which {'writes' if len(paths) == 1 else 'write'} that event; remove the "
                    f"bullet, or rename it to the key the source writes")
    return problems


def _listed(text: str) -> list[str]:
    return [part.strip() for part in re.split(r",| and ", text) if part.strip()]


def missing_facts(entries: list[Entry]) -> list[str]:
    form = "Writer: <who>. Where: <where>. Kind: <kind>."
    problems = []
    for entry in entries:
        if len(entry.facts) != 1 or not (match := FACTS.match(entry.facts[0])):
            problems.append(f"{DOC_NAME}:{entry.line} the entry for `{entry.name}` needs exactly one line of the form "
                            f"`{form}` right after its heading")
            continue
        for label, allowed, given in (("writer", WRITERS, _listed(match["writers"])),
                                      ("place", PLACES, _listed(match["places"])),
                                      ("kind", KINDS, [match["kind"]])):
            for value in given:
                if value not in allowed:
                    problems.append(f"{DOC_NAME}:{entry.line} the entry for `{entry.name}` names the {label} "
                                    f"{value!r}; use one of: {', '.join(sorted(allowed))}")
    return problems


def bad_examples(examples: list[tuple[int, str]], entries: list[Entry]) -> list[str]:
    listed = documented(entries)
    problems = []

    def check(event: object, line: int) -> None:
        if not isinstance(event, dict) or not isinstance(event.get("event"), str):
            problems.append(f"{DOC_NAME}:{line} the json example is not an object with an `event` name")
            return
        name = event["event"]
        if list(event) != sorted(event):
            problems.append(f"{DOC_NAME}:{line} the json example of `{name}` does not have its keys sorted, as "
                            f"every line in the file has")
        if name not in listed:
            problems.append(f"{DOC_NAME}:{line} the json example shows the event `{name}`, which has no entry")
            return
        for key in sorted(set(event) - set(listed[name]) - {"event", "ts"}):
            problems.append(f"{DOC_NAME}:{line} the json example of `{name}` has the key `{key}`, which the entry "
                            f"for `{name}` does not list; list it, or take it out of the example")
        for nested in ("transform_events", "usage_events"):
            for inner in event.get(nested) or []:
                check(inner, line)

    for line, text in examples:
        try:
            check(json.loads(text), line)
        except ValueError as error:
            problems.append(f"{DOC_NAME}:{line} the json example is not valid JSON ({error})")
    return problems


def _report(problems: list[str]) -> str:
    return "\n" + "\n".join(f"- {problem}" for problem in problems)


def _document() -> tuple[list[Entry], list[tuple[int, str]]]:
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
'''


def test_the_scan_reads_every_form_it_claims_to():
    module = scan_source(FORMS, "forms.py")
    by_name = {name: site for site in module.sites for name in site.names or ()}
    assert set(by_name) == {"a_literal", "a_continued", "a_conflict", "an_error", "a_dict_call", "an_update",
                            "a_setdefault", "a_subscript", "a_variable_holder", "a_tuple_target"}
    assert by_name["a_continued"].line == 8  # the line of the name, not of the dict's opening brace
    assert by_name["a_conflict"] is by_name["an_error"]
    unread = [site for site in module.sites if site.names is None]
    assert [(site.scope, site.value, site.line) for site in unread] == [("Helper.dynamic", "kind", 33)]
    fields = written_fields([module])
    assert set(fields["a_literal"]) == {"count", "more"}
    assert set(fields["a_dict_call"]) == {"size"}
    assert set(fields["a_variable_holder"]) == {"at", "late", "later", "latest", "default"}  # not "unpacked"
    assert set(fields["a_tuple_target"]) == {"error"}
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
    entries, _ = _document()
    problems = undocumented_events(written_names(scan_package()), entries)
    assert not problems, _report(problems)


def test_every_documented_event_is_written_by_the_source():
    entries, _ = _document()
    problems = unwritten_events(written_names(scan_package()), entries)
    assert not problems, _report(problems)


def test_every_field_the_scan_finds_is_documented():
    entries, _ = _document()
    problems = undocumented_fields(written_fields(scan_package()), entries)
    assert not problems, _report(problems)


def test_every_documented_field_appears_in_a_module_that_writes_the_event():
    entries, _ = _document()
    modules = scan_package()
    problems = unwritten_fields(modules, written_names(modules), entries)
    assert not problems, _report(problems)


def test_every_entry_states_its_writer_place_and_kind():
    entries, _ = _document()
    problems = missing_facts(entries)
    assert not problems, _report(problems)


def test_every_json_example_shows_documented_events_and_fields():
    entries, examples = _document()
    assert examples, f"{DOC_NAME} has no json example"
    problems = bad_examples(examples, entries)
    assert not problems, _report(problems)


# The checks have teeth: the same functions, on the real document with one change -----------------------------

def test_removing_an_entry_or_inventing_one_is_reported():
    text = DOC.read_text(encoding="utf-8")
    modules = scan_package()
    written = written_names(modules)
    assert "\n### `rotation`\n" in text
    without, _ = read_document(text.replace("\n### `rotation`\n", "\n### rotation\n"))
    assert any(problem.startswith(f"{DOC_NAME} does not document the event `rotation` written at "
                                  "src/contexto/rewrite.py:") for problem in undocumented_events(written, without))
    invented, _ = read_document(text + "\n### `made_up_event`\n\nWriter: proxy. Where: own line. Kind: normal.\n")
    line = len(text.splitlines()) + 2
    assert (f"{DOC_NAME}:{line} documents the event `made_up_event`, but nothing under src/contexto writes it; "
            "remove the entry, or rename it to the name the source writes") in unwritten_events(written, invented)


def test_removing_a_field_or_inventing_one_is_reported():
    text = DOC.read_text(encoding="utf-8")
    modules = scan_package()
    assert "\n- `retained_messages`:" in text
    without, _ = read_document(text.replace("\n- `retained_messages`:", "\n- retained_messages:"))
    assert any(problem.startswith(f"{DOC_NAME}'s entry for `rotation` does not list the field `retained_messages` "
                                  "written at src/contexto/rewrite.py:")
               for problem in undocumented_fields(written_fields(modules), without))
    invented, _ = read_document(text.replace("\n- `retained_messages`:",
                                             "\n- `made_up_field`: x\n- `retained_messages`:"))
    assert any("lists the field `made_up_field` under `rotation`" in problem
               for problem in unwritten_fields(modules, written_names(modules), invented))
