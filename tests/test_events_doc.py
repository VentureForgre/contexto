"""docs/events.md is a reference for every event the package writes to a session's ``log.jsonl``.

People reading a log, and commands that read one, rely on it, so it must not go stale. These tests
read the package source with ``ast`` and the document's headings, facts lines, bullets and index, and
fail when they disagree. Every failure is a sentence that says which file to change and how.

What they guarantee:

- Names, both ways. Every event name the source can write has an entry in the document (a heading
  ``### `name```), and every entry names an event the source can write.
- The scan is not silently incomplete in the forms it reads: a dict display with an ``"event"`` key,
  any call with an ``event=`` keyword (``dict(event=...)``, ``x.update(event=...)``, a helper of the
  package's own), ``x.setdefault("event", ...)``, ``x["event"] = ...``, and a pair ``("event", ...)``
  in a list written in place as the argument of ``dict(...)`` or ``x.update(...)``. The name is read
  when it is a string literal, a conditional expression of string literals, or a local variable or
  module constant that is assigned only those. Any other value fails unless the site is listed in
  ``DYNAMIC_SITES`` with the names it can produce: a parameter, a call, an f-string, a name that
  ``+=`` adds to, and ``x["event"]`` as one of several targets or as the target of a loop, whose value
  the scan does not pair with it. A site is a function and the expression it uses for the name, so a
  second site in a listed function is not covered by the first. An entry no site needs fails too. A
  dict that never reaches a log, and a call whose ``event=`` passes a whole event and not a name, are
  exempt only by listing the site in ``NOT_SESSION_LOG``.
- Fields, source to document, for the fields the scan can read. Each of these is listed under its
  event (a bullet ``- `field`: ...``):
  - a key written as a literal next to an event name the scan read;
  - a key unpacked into the event with ``**``, from a dict display written in place (either branch of
    a conditional expression) or from a local variable that the same function assigns only dict
    displays to;
  - a key of the dict a function of the same file returns, when the event unpacks a call to it, by
    bare name or on ``self`` or ``cls``, with ``**`` (``**self._sizes()``): read from that function's
    ``return`` statements in the forms ``RETURNED_FIELDS`` reads (below). A call is resolved to the one
    function Python would run, as far as the file shows it (the same rule for every call below): on
    ``self`` or ``cls``, the method of the calling class or else of its bases, in method resolution
    order, stopping with nothing at a base the file does not define; by bare name, a function nested in
    the caller or in a function around it, else one at the top of the file, and nothing when the caller
    or a function around it binds the name itself (a parameter, a local) first;
  - a key stored later on the same local variable (``v["key"] = ...``, ``v.update(key=...)``,
    ``v.update({"key": ...})``, ``v.update(**local)``, ``v |= {"key": ...}``, ``v.setdefault("key",
    ...)``) when that variable holds one event name in its function and is bound to nothing else there;
  - for an event listed in ``CARRIED_EVENTS``, whose dict is filled in by several functions of one
    file: a key stored on that dict in any function the scan follows it into. It is followed from the
    function that assigns it to a variable, into each function of the file it is passed to by a call
    by bare name or on ``self`` or ``cls`` (under the name of the parameter that receives it), and
    into each function nested in one of those that reads the variable. So the variable can be
    renamed in one function or in all of them with no change here, and a variable of the same name
    that another function binds itself (a local, a loop variable) is not the event. A function that binds
    the variable to anything else as well (an assignment, ``+=``, a loop, ``with``, ``except ... as``, an
    import, a ``match`` capture; not a comprehension's target, which is its own, and not ``|=``, which
    updates the dict) is not followed: none of the keys stored on the variable there is taken for a
    field, nothing is followed from it, and if it stores a key on the variable or passes it on, the test
    fails and says to rename one of the two. A default for a missing dict (``entry = entry or {}``, ``if
    entry is None: entry = {}``) is such a binding, but there the fix is to drop the default and pass the
    dict: the scan does not follow the dict onto another name (``other = entry or {}``);
  - for an event listed in ``RETURNED_FIELDS``: a key of the dict that a function named there returns,
    when the returned value, or the element of the returned tuple the table names, is a dict display,
    a conditional expression of them, or a local variable assigned only dict displays (with the keys
    stored on it later), and whatever those unpack with ``**`` from such a variable. Each function named
    there (for a name with ``*``, one of the functions it matches) must have a ``return`` that gives one of
    those forms, so a change to what it returns fails rather than leaving the entry to read nothing;
  - for an event listed in ``LAST_EVENTS``: a key stored on the last element of a list (``x[-1]["key"]``,
    ``x[-1].update(...)``, ``x[-1].setdefault("key", ...)``) in the function named there.
- A key stored on a variable that holds more than one event name is in ``HELPER_FIELDS`` under at
  least one of those names.
- Fields the scan cannot read, by a list. ``HELPER_FIELDS`` names the fields that reach an event in a
  way the scan does not follow. Each is listed under its event, so such a bullet cannot be removed
  from the document unnoticed.
- Two entries that share a name are kept apart. ``request`` is both the proxy's line and the engine's
  nested event: ``SHARED_NAMES`` says which entry covers the event as each source file writes it, and
  each field is looked for in that entry alone.
- Fields, document to source, weakly. A field the document lists appears as a string literal or a
  keyword argument somewhere in a module that writes the event. This catches a field that was renamed
  or removed; it does not prove that the field is written on that event. A made-up field whose name is
  a common word passes: a bullet ``- `conversation`: ...`` under ``degraded_leave`` is not reported,
  because ``"conversation"`` is a string somewhere in rewrite.py. The check cannot be tightened to the
  fields the scan reads without listing every field it cannot read in ``HELPER_FIELDS`` first.
- Each entry states its writer, where it appears and its kind in one line, in a fixed vocabulary, and
  the document's section "Reading an entry" names every word of that vocabulary.
- The index table under ``## Index`` has exactly one row per entry, in the entries' order, and each
  row repeats its entry's heading, writer, place and kind. Every link to a place in the document
  (``](#anchor)``) names a heading that exists.
- Each ``json`` example in the document is one object with sorted keys, as a line in the file has,
  whose event and keys the document lists, nested events included, each nested event shown in a place
  its entry names.
- The tables below name things that exist: a site, an event, a file, a function or a field that the
  source no longer has fails, with the entry to remove or correct.

What they do not guarantee:

- An event built in a form the scan does not read: a computed key (``{key: name}``), a dict
  comprehension, ``dict(pairs)`` or ``x.update(pairs)`` with anything but a list written in place,
  ``dict(zip(...))``, ``dataclasses.asdict``, ``x.__setitem__``, a dict read from a file or parsed
  from a string, or a line written to the log file as text, without a dict. None exists today.
- A name through a binding the scan does not track. A local variable or module constant that is
  assigned a documented literal, and is also bound by ``global`` or ``nonlocal`` in another function,
  as the target of a comprehension, as a parameter of a lambda or by a ``match`` capture, is read as
  the literal alone, so another name written through it passes. Nothing in the package does this.
- The names a ``DYNAMIC_SITES`` entry lists. They are kept by hand; the test checks only that each is
  a string literal somewhere in the package. A helper listed there is read once, where it is: a name
  that a later caller passes to it, and every field passed with it, go unread.
- A field the source starts to write through the return value of a function ``RETURNED_FIELDS`` does
  not name, through a returned value that is not one of the forms above (a call's result, a dict
  filled in by a loop), through ``**`` of anything but the forms above, or on something that is not
  a bare variable (``self.entry["key"]``, or ``events[-1]["key"]`` in a function ``LAST_EVENTS`` does not
  name). Nothing fails until someone adds it to ``HELPER_FIELDS``. For a ``RETURNED_FIELDS`` name with
  ``*``, one function it matches with a ``return`` the scan reads is enough: another that it matches and
  whose returns the scan cannot read is not read. A field the document lists that is in neither the
  scan nor that list is checked document to source only.
- A call the scan does not resolve to one function of the file, by the rule above: a method that the
  calling class gets from a base in another file, or from a base reached after one; a method that a
  subclass in the file overrides, when the call is made in the base (``self`` may be the subclass); a
  bare name the caller binds (a parameter, a local); a function of another file; and two hops
  (``return self._inner()``). What such a function returns or stores is not read, except through the
  parameter-name rule for ``CARRIED_EVENTS`` below.
- That ``HELPER_FIELDS`` is right about the source. It is a list kept by hand. It only makes sure the
  document keeps the bullets it names. For a key stored on a variable that holds several event names,
  the test checks that the key is listed under one of them, not that it is listed under the right
  ones.
- A ``CARRIED_EVENTS`` dict under another local name (``other = entry``, ``other = entry or {}``): the
  keys stored on ``other`` are not read. And the dict through a call the scan cannot resolve: the calls
  of the item above, a call on another object (``owner.method(entry)``), a function passed as a value or
  bound with ``functools.partial``, ``getattr(self, "_f")(entry)``, a local alias (``f = self._f``, then
  ``f(entry)``), arguments unpacked with ``*`` or ``**`` (``self._f(*args)``, ``self._f(**{"rec":
  entry})``), a lambda's parameter, a method of a class defined inside a function, and the dict stored on
  an attribute (``self.current = entry``). There the scan goes by the name alone: a parameter with the
  same name as a variable that holds the dict is read as the dict. So a key stored through a parameter
  under another name, reached only by such a call, is not read; and an unrelated parameter that has the
  name is read as the event, even when every call to its function that the scan resolves passes another
  dict. The failure says so, and says to rename the parameter.
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
# it is listed. Empty today; an entry no site needs fails. The names of an entry are kept by hand. The test
# reads a listed site once, where it is: when the site is a helper that takes the name, a name a later caller
# passes and every field passed with it go unread, so each new name is added to the entry by hand, and its
# fields to ``HELPER_FIELDS``.
DYNAMIC_SITES: dict[tuple[str, str, str], frozenset[str]] = {}

# Sites the scan must ignore: (file, function, the expression used for the name), as the failure message
# prints it. Two kinds: a dict with an "event" key that never reaches a session log (say, an SSE event built
# for a stream), and a call whose ``event=`` keyword passes a whole event or some other object and not an event
# name (``store.log(event=failure)``). The scan ignores that site and no other. Empty today; an entry no site
# needs fails.
NOT_SESSION_LOG: frozenset[tuple[str, str, str]] = frozenset()

# Events whose dict is filled in by several functions of one file, so its fields are stored where the dict is
# not built: (file, event). The scan follows the dict from the function that assigns it to a variable: into
# each function of the file it is passed to (a call by bare name, or on ``self`` or ``cls``), under the name
# of the parameter that receives it; into each function nested in one of those that reads the variable; and,
# for a call the scan cannot resolve, into any function that has a parameter with the same name as a variable
# that holds the dict. Every key stored on the dict in those functions is a field of the event.
CARRIED_EVENTS: frozenset[tuple[str, str]] = frozenset({
    ("src/contexto/proxy.py", "request"),
})

# Event names with more than one entry in the document: event -> {source file: the writer named by the
# entry that covers the event as that file writes it}. The proxy's ``request`` line and the engine's nested
# ``request`` event share a name and nothing else, so each one's fields are looked for in its own entry.
SHARED_NAMES: dict[str, dict[str, str]] = {
    "request": {"src/contexto/proxy.py": "proxy", "src/contexto/rewrite.py": "engine"},
}

# Functions whose return value becomes fields of an event: (event, source file) -> ((function, which part of
# the returned value), ...). The function is named as the scan names a scope (``Class.method``); ``*`` stands
# for any run of characters but a dot, so ``Server._tool_*`` is every tool, a new one included. The part is
# None for the value itself, or the position of an element when the function returns a tuple. The scan reads
# that part of each ``return`` when it is a dict display, a conditional expression of them, or a local
# variable assigned only dict displays (with the keys stored on it later), and whatever those unpack with
# ``**`` from such a variable. Any other returned value is not read: its fields belong in ``HELPER_FIELDS``.
RETURNED_FIELDS: dict[tuple[str, str], tuple[tuple[str, int | None], ...]] = {
    # _tools_call merges the third element of what a tool returns: event.update(extra, ok=True)
    ("mcp_call", "src/contexto/mcp.py"): (("Server._tool_*", 2),),
    # ToolCheck.verdict() returns the event of its helper with one more key
    ("mcp_tools_missing", "src/contexto/toolcheck.py"): (("ToolCheck.verdict", None),),
}

# Functions that store keys on the last element of a list of events, which is known to be one event: (source
# file, function) -> that event. In the function, a key stored on ``x[-1]`` (``x[-1]["key"] = ...``,
# ``x[-1].update(...)``, ``x[-1].setdefault("key", ...)``) is a field of the event, whatever the list is called.
# An entry fails when the function is gone, stores no key on a last element any more, or stores keys on the last
# elements of two lists.
LAST_EVENTS: dict[tuple[str, str], str] = {
    # RewriteEngine._call: "the engine's request event stays last" in the list rewrite() returns
    ("src/contexto/rewrite.py", "RewriteEngine._call"): "request",
}

# Fields the scan cannot read from the source: (event, source file) -> the fields. They reach the event
# through a helper whose return value the scan cannot read, on a variable that holds more than one event
# name, or on a dict the scan does not follow. Each must be listed under the event, like a field the scan
# reads. Kept by hand: when the source gains such a field, document it and add it here; when the source drops
# one, remove its bullet and its name here. A key stored on a variable that holds more than one event name
# fails ``test_a_key_stored_on_a_variable_with_several_events_is_named_here`` until it is here.
HELPER_FIELDS: dict[tuple[str, str], frozenset[str]] = {
    # UsageReader.end_fields() returns what _keep() stored, merged with entry.update(...)
    ("request", "src/contexto/proxy.py"): frozenset({
        "stop_reason", "error_type", "end_event", "response_status", "incomplete_reason", "error_code"}),
    # _within_room returns the budget fields, which each budgeted tool unpacks into its own
    ("mcp_call", "src/contexto/mcp.py"): frozenset({
        "turn_budget", "charged_tokens", "room_left", "limited_by", "side_rooms", "limiting_age_s"}),
    # store_failure() stores these on one variable that holds both names
    ("store_error", "src/contexto/rewrite.py"): frozenset({"degraded"}),
    ("page_conflict", "src/contexto/rewrite.py"): frozenset({"degraded", "page_id"}),
}

# The words a facts line may use. Each list is repeated under "Reading an entry" in the document; a new writer,
# place or kind is added in both places.
WRITERS = frozenset({"proxy", "engine", "launcher (Claude)", "launcher (Codex)", "MCP server", "store"})
PLACES = frozenset({"own line", "`transform_events`", "`usage_events`"})
KINDS = frozenset({"normal", "notice", "problem"})
NESTED = {"transform_events": "`transform_events`", "usage_events": "`usage_events`"}  # line key -> its place
VOCABULARY = (("writer", "WRITERS", WRITERS), ("place", "PLACES", PLACES), ("kind", "KINDS", KINDS))
VOCABULARY_HEADING = "Reading an entry"


@dataclass(frozen=True)
class Tables:
    """The seven tables above. Every check takes them as an argument, so the tests of the checks themselves
    run on tables of their own and never on the package's."""

    dynamic_sites: dict[tuple[str, str, str], frozenset[str]] = field(default_factory=dict)
    not_session_log: frozenset[tuple[str, str, str]] = frozenset()
    carried_events: frozenset[tuple[str, str]] = frozenset()
    shared_names: dict[str, dict[str, str]] = field(default_factory=dict)
    helper_fields: dict[tuple[str, str], frozenset[str]] = field(default_factory=dict)
    returned_fields: dict[tuple[str, str], tuple[tuple[str, int | None], ...]] = field(default_factory=dict)
    last_events: dict[tuple[str, str], str] = field(default_factory=dict)


TABLES = Tables(DYNAMIC_SITES, NOT_SESSION_LOG, CARRIED_EVENTS, SHARED_NAMES, HELPER_FIELDS, RETURNED_FIELDS,
                LAST_EVENTS)


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
    callee: str | None = None  # for ``event=`` in a call that builds no dict: what is called, as written
    built: tuple[int, int] | None = None  # where the dict assigned to ``variable`` starts: (line, column)
    spread_calls: tuple[tuple[str, bool, int], ...] = ()  # the calls unpacked into it with ``**``

    @property
    def where(self) -> str:
        return f"{self.path}:{self.line}"

    @property
    def key(self) -> tuple[str, str, str]:
        """How ``DYNAMIC_SITES`` and ``NOT_SESSION_LOG`` name this site."""
        return self.path, self.scope, self.value


@dataclass(frozen=True)
class Function:
    """One function of a module, as a call to it sees it."""

    scope: str
    positional: tuple[str, ...]  # the parameters a positional argument can fill, in order
    named: frozenset[str]  # the parameters a keyword argument can fill
    method: bool  # defined in a class and not a static method: a call on ``self`` fills ``positional[1:]``


@dataclass(frozen=True)
class Call:
    """A call the scan can resolve inside its module, with the bare names it passes."""

    scope: str  # the function that makes the call
    line: int
    name: str  # the function called
    on_self: bool  # ``self.name(...)`` or ``cls.name(...)``; otherwise ``name(...)``
    positional: tuple[str | None, ...]  # each positional argument that is a bare name, else None
    keywords: tuple[tuple[str, str], ...]  # (parameter, the bare name passed for it)


@dataclass
class Module:
    path: str
    sites: list[Site] = field(default_factory=list)
    functions: dict[str, Function] = field(default_factory=dict)  # scope -> the function
    classes: set[str] = field(default_factory=set)  # the scopes that are classes
    # class scope -> its bases as written: a bare name, or None for any other expression (``mod.Base``)
    bases: dict[str, tuple[str | None, ...]] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    returns: list[tuple[str, ast.expr]] = field(default_factory=list)  # (scope, the value a ``return`` gives)
    stores: list[tuple[str, str, str, int]] = field(default_factory=list)  # (scope, variable, key, line)
    spread_stores: list[tuple[str, str, str]] = field(default_factory=list)  # (scope, variable, unpacked variable)
    # the same two for a key stored on the last element of a list, ``variable[-1]``
    last_stores: list[tuple[str, str, str, int]] = field(default_factory=list)
    last_spread_stores: list[tuple[str, str, str]] = field(default_factory=list)
    # (scope, variable) -> each place the function binds it other than as a parameter: (line, where the dict
    # display or ``dict()`` call it is assigned starts, or None for any other value)
    bindings: dict[tuple[str, str], list[tuple[int, tuple[int, int] | None]]] = field(default_factory=dict)
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

    def stored(self, scope: str, variable: str) -> list[tuple[str, int]]:
        """The keys stored in ``scope`` on ``variable`` after it was assigned, each with its line."""
        direct = [(key, line) for where, name, key, line in self.stores if (where, name) == (scope, variable)]
        return direct + [pair for where, name, spread in self.spread_stores if (where, name) == (scope, variable)
                         for pair in self.unpacked(where, spread)]

    def stored_last(self, scope: str) -> list[tuple[str, str, int]]:
        """The keys stored in ``scope`` on the last element of a list: (the list's variable, key, line)."""
        direct = [(name, key, line) for where, name, key, line in self.last_stores if where == scope]
        return direct + [(name, *pair) for where, name, spread in self.last_spread_stores if where == scope
                         for pair in self.unpacked(where, spread)]

    def binds(self, scope: str, variable: str) -> bool:
        """Whether ``scope`` itself binds ``variable``, as a parameter or otherwise."""
        return (scope, variable) in self.local_dicts or (scope, variable) in self.bindings

    def rebound(self, scope: str, variable: str, keep: frozenset[tuple[int, int]] = frozenset()) -> int | None:
        """The first line where ``scope`` binds ``variable`` to a value other than its parameter's and other
        than the dicts that start at ``keep``; None when it binds it nowhere else."""
        lines = [line for line, built in self.bindings.get((scope, variable), []) if built is None or built not in keep]
        return min(lines, default=None)

    def strings(self, scope: str, variable: str) -> frozenset[str] | None:
        """The event names the variable can be: what its function assigns it, if only strings; for a name
        the function never binds, what the module assigns it. None when that is not known."""
        for where in (scope, "<module>"):
            if (where, variable) in self.local_strings:
                found = self.local_strings[(where, variable)]
                return frozenset(found) if found else None
        return None

    def _base(self, scope: str, base: str | None) -> str:
        """The class scope a base of the class ``scope`` names, looked up where the class is defined, then at
        the top of the module; for a base the module does not define, ``?`` and its name."""
        parts = scope.split(".")[:-1]
        for size in range(len(parts), -1, -1) if base is not None else ():
            if (found := ".".join([*parts[:size], base])) in self.classes:
                return found
        return f"?{base or ''}"

    def mro(self, scope: str, seen: frozenset[str] = frozenset()) -> list[str]:
        """The method resolution order of the class ``scope`` (C3, as Python computes it) as far as the module
        defines its classes. A base the module does not define stands for itself alone, as ``?`` and its name;
        an order Python would reject, or a loop, ends with ``?``."""
        if scope not in self.classes or scope in seen:
            return [scope if scope.startswith("?") else "?"]
        bases = [self._base(scope, base) for base in self.bases.get(scope, ()) if base != "object"]
        orders = [self.mro(base, seen | {scope}) for base in bases] + [bases]
        out = [scope]
        while orders := [order for order in orders if order]:
            head = next((order[0] for order in orders if not any(order[0] in other[1:] for other in orders)), None)
            if head is None:
                return [*out, "?"]
            out.append(head)
            orders = [order[1:] if order[0] == head else order for order in orders]
        return out


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


def _is_last(index: ast.AST) -> bool:
    """Whether a subscript's index is ``-1``."""
    if isinstance(index, ast.UnaryOp) and isinstance(index.op, ast.USub):
        return isinstance(index.operand, ast.Constant) and index.operand.value == 1
    return isinstance(index, ast.Constant) and index.value == -1


def _builds_dict(value: ast.AST | None) -> bool:
    return isinstance(value, ast.Dict) or (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                                           and value.func.id == "dict")


Calls = list[tuple[str, bool, int]]  # calls unpacked with ``**``: (the function, on ``self`` or ``cls``, line)


def _display_keys(node: ast.Dict, fields: list[tuple[str, int]], spreads: list[str],
                  calls: Calls | None = None) -> None:
    """The literal keys of a dict display other than ``event``, and what it unpacks with ``**``."""
    for key, value in zip(node.keys, node.values):
        if key is None:
            _spread(value, fields, spreads, calls)
        elif (text := _string(key)) is not None and text != "event":
            fields.append((text, value.lineno))


def _spread(value: ast.AST, fields: list[tuple[str, int]], spreads: list[str], calls: Calls | None = None) -> None:
    """What ``**value`` adds: the keys of a dict display written in place (either branch of a conditional
    expression), the name of a local variable, resolved once its function has been scanned, and, when
    ``calls`` is given, a call by bare name or on ``self`` or ``cls``, resolved once the module has been."""
    if isinstance(value, ast.Dict):
        _display_keys(value, fields, spreads, calls)
    elif isinstance(value, ast.IfExp):
        _spread(value.body, fields, spreads, calls)
        _spread(value.orelse, fields, spreads, calls)
    elif isinstance(value, ast.Name):
        spreads.append(value.id)
    elif isinstance(value, ast.Call) and calls is not None:
        func = value.func
        if isinstance(func, ast.Name):
            calls.append((func.id, False, value.lineno))
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in ("self", "cls"):
            calls.append((func.attr, True, value.lineno))


def _call_keys(node: ast.Call, fields: list[tuple[str, int]], spreads: list[str], calls: Calls | None = None) -> None:
    """The keyword arguments of a call other than ``event``, and what it unpacks with ``**``."""
    for kw in node.keywords:
        if kw.arg is None:
            _spread(kw.value, fields, spreads, calls)
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

    def _site(self, value: ast.AST, fields: list[tuple[str, int]], built: ast.AST | None = None,
              spreads: list[str] | None = None, callee: str | None = None, calls: Calls | None = None) -> None:
        variable = self._assigned.get(id(built)) if built is not None else None
        self.module.sites.append(Site(self.module.path, value.lineno, self.scope, _names(value), ast.unparse(value),
                                      tuple(fields), variable, tuple(spreads or ()),
                                      value.id if isinstance(value, ast.Name) else None, callee,
                                      (built.lineno, built.col_offset) if variable is not None else None,
                                      tuple(calls or ())))

    def _bind(self, name: str, line: int, value: ast.AST | None = None) -> None:
        """``name`` is bound in this scope other than as a parameter; ``value`` is a dict it is assigned."""
        built = (value.lineno, value.col_offset) if value is not None else None
        self.module.bindings.setdefault((self.scope, name), []).append((line, built))

    def _store(self, target: ast.AST, fields: list[tuple[str, int]], spreads: list[str] | None = None) -> None:
        if isinstance(target, ast.Name):
            self.module.stores.extend((self.scope, target.id, key, line) for key, line in fields)
            self.module.spread_stores.extend((self.scope, target.id, name) for name in spreads or ())
        elif isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) and _is_last(target.slice):
            self.module.last_stores.extend((self.scope, target.value.id, key, line) for key, line in fields)
            self.module.last_spread_stores.extend((self.scope, target.value.id, name) for name in spreads or ())

    def _unknown(self, target: ast.AST | None) -> None:
        """``target`` is bound to something the scan does not read: a parameter, a loop variable, a call, one
        value of several. An ``x["event"]`` among the targets is an event name the scan cannot read, and an
        ``x["key"]`` is a key stored on ``x``."""
        if isinstance(target, (ast.Tuple, ast.List)):
            for inner in target.elts:
                self._unknown(inner)
        elif isinstance(target, ast.Starred):
            self._unknown(target.value)
        elif isinstance(target, ast.Subscript) and (key := _string(target.slice)) is not None:
            if key == "event":
                self._site(target, [])
            else:
                self._store(target.value, [(key, target.lineno)])
        elif isinstance(target, ast.arguments):
            for arg in (*target.posonlyargs, *target.args, target.vararg, *target.kwonlyargs, target.kwarg):
                self._unknown(arg)
        elif isinstance(target, (ast.Name, ast.arg)):
            name = target.id if isinstance(target, ast.Name) else target.arg
            if isinstance(target, ast.Name):
                self._bind(name, target.lineno)
            self.module.local_dicts[(self.scope, name)] = None
            self.module.local_strings[(self.scope, name)] = None

    def _scoped(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        outer = self.scope
        self._scope.append(node.name)
        if isinstance(node, ast.ClassDef):
            self.module.classes.add(self.scope)
            self.module.bases[self.scope] = tuple(base.id if isinstance(base, ast.Name) else None
                                                  for base in node.bases)
        else:
            static = any(isinstance(d, ast.Name) and d.id == "staticmethod" for d in node.decorator_list)
            positional = tuple(arg.arg for arg in (*node.args.posonlyargs, *node.args.args))
            named = frozenset(arg.arg for arg in (*node.args.args, *node.args.kwonlyargs))
            self.module.functions[self.scope] = Function(self.scope, positional, named,
                                                         outer in self.module.classes and not static)
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
            calls: Calls = []
            _display_keys(node, fields, spreads, calls)
            for value in events:
                self._site(value, fields, node, spreads, calls=calls)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        builds = _builds_dict(node)
        method = func.attr if isinstance(func, ast.Attribute) else None
        fields: list[tuple[str, int]] = []
        spreads: list[str] = []
        calls: Calls = []
        _call_keys(node, fields, spreads, calls)
        for kw in node.keywords:
            if kw.arg == "event":  # dict(event=...), x.update(event=...), or a function that takes ``event``
                self._site(kw.value, fields if builds else [], node, spreads if builds else None,
                           None if builds or method == "update" else ast.unparse(func), calls if builds else None)
        if method == "setdefault" and len(node.args) >= 2 and (key := _string(node.args[0])) is not None:
            if key == "event":
                self._site(node.args[1], [])
            else:
                self._store(func.value, [(key, node.lineno)])
        if builds or method == "update":
            for arg in node.args:  # dict([("event", name)]) and x.update([("event", name)])
                for pair in arg.elts if isinstance(arg, (ast.List, ast.Tuple)) else ():
                    if isinstance(pair, ast.Tuple) and len(pair.elts) == 2 and _string(pair.elts[0]) == "event":
                        self._site(pair.elts[1], [])
        if method == "update":
            for arg in node.args:
                if isinstance(arg, ast.Dict):
                    _display_keys(arg, fields, spreads)
            self._store(func.value, fields, spreads)
        self._call(node)
        self.generic_visit(node)

    def _call(self, node: ast.Call) -> None:
        """Record a call by bare name, or on ``self`` or ``cls``, that passes a bare name."""
        func = node.func
        on_self = (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                   and func.value.id in ("self", "cls"))
        if not on_self and not isinstance(func, ast.Name):
            return
        positional: list[str | None] = []
        for arg in node.args:
            if isinstance(arg, ast.Starred):  # the arguments after it have no known position
                break
            positional.append(arg.id if isinstance(arg, ast.Name) else None)
        keywords = tuple((kw.arg, kw.value.id) for kw in node.keywords
                         if kw.arg is not None and isinstance(kw.value, ast.Name))
        if keywords or any(positional):
            self.module.calls.append(Call(self.scope, node.lineno, func.attr if on_self else func.id, on_self,
                                          tuple(positional), keywords))

    def visit_Return(self, node: ast.Return) -> None:
        if node.value is not None:
            self.module.returns.append((self.scope, node.value))
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
                self._bind(target.id, target.lineno, value)
                self.module.local_strings[key] = None
                if dicts is not None:
                    if isinstance(value, ast.Dict):
                        _display_keys(value, dicts, [])
                    else:
                        _call_keys(value, dicts, [])
            elif (names := _names(value)) is not None:
                self._bind(target.id, target.lineno)
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
        if isinstance(node.target, ast.Subscript) and _string(node.target.slice) != "event":
            self._assign(node.target, node.value)  # x["key"] += ... stores the key
        elif isinstance(node.target, ast.Name) and isinstance(node.op, ast.BitOr):
            # on a dict, x |= {...} updates it in place, as x.update({...}) does: the keys are stored on it. On a
            # string it is an error, and a variable that held anything else is not a dict the scan reads anyway
            fields: list[tuple[str, int]] = []
            spreads: list[str] = []
            _spread(node.value, fields, spreads)
            self._store(node.target, fields, spreads)
        else:  # a name, or x["event"], is no longer the string or the dict it was assigned
            self._unknown(node.target)
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

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self._bind(node.name, node.lineno)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import | ast.ImportFrom) -> None:
        for alias in node.names:
            self._bind(alias.asname or alias.name.partition(".")[0], node.lineno)

    visit_ImportFrom = visit_Import

    def visit_MatchAs(self, node: ast.MatchAs | ast.MatchStar) -> None:
        if node.name:
            self._bind(node.name, node.lineno)
        self.generic_visit(node)

    visit_MatchStar = visit_MatchAs

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self._bind(node.rest, node.lineno)
        self.generic_visit(node)


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


@dataclass(frozen=True)
class Carry:
    """Why the scan takes a variable of one function for the dict of a ``CARRIED_EVENTS`` event."""

    event: str
    how: str  # completes "on `variable`, which holds the `event` dict there: ..."
    sure: bool  # False: only the parameter's name says so
    rebound: int | None = None  # the first line where the function binds the variable to something else

    def because(self, variable: str, scope: str) -> str:
        """Why the scan takes ``variable`` in ``scope`` for the dict, as a clause."""
        if self.sure:
            return self.how
        return (f"it is a parameter of {scope} with the name of the variable that holds the dict in {self.how}, "
                f"and the scan found no call that passes it")


def _callees(module: Module, call: Call) -> list[Function]:
    """The function of the module that ``call`` reaches, if the scan knows it. ``self.name`` and ``cls.name`` are
    looked up in the calling class and then its bases, in method resolution order, as far as the module defines
    them: when the order reaches a base the module does not define, that base may define the method, so the call
    reaches nothing the scan knows. A bare name is looked up in the calling function and those around it, then
    at the top of the module, and reaches nothing when one of those functions binds the name itself (a
    parameter, a local) before a function of that name is found."""
    parts = call.scope.split(".") if call.scope != "<module>" else []
    outer = [".".join(parts[:size]) for size in range(len(parts), 0, -1)]  # the enclosing scopes, innermost first
    if call.on_self:
        own = next((scope for scope in outer if scope in module.classes), None)
        for scope in module.mro(own) if own is not None else ():
            if scope not in module.classes:
                return []
            if f"{scope}.{call.name}" in module.functions:
                return [module.functions[f"{scope}.{call.name}"]]
        return []
    for scope in outer:
        if scope in module.classes:  # a class body's names are not seen from the functions in it
            continue
        if f"{scope}.{call.name}" in module.functions:
            return [module.functions[f"{scope}.{call.name}"]]
        if module.binds(scope, call.name):
            return []
    return [module.functions[call.name]] if call.name in module.functions else []


def _passes(module: Module, scope: str, variable: str) -> list[tuple[tuple[str, str], str]]:
    """Where ``variable`` of ``scope`` goes: each (function, parameter) a call passes it to, and each function
    nested in ``scope`` that reads it, with how it gets there."""
    found: list[tuple[tuple[str, str], str]] = []
    for call in module.calls:
        for function in _callees(module, call) if call.scope == scope else ():
            skip = 1 if call.on_self and function.method else 0
            params = [function.positional[at + skip] for at, name in enumerate(call.positional)
                      if name == variable and at + skip < len(function.positional)]
            params += [param for param, name in call.keywords if name == variable and param in function.named]
            found += [((function.scope, param), f"{scope} passes it at {module.path}:{call.line}") for param in params]
    for inner in module.functions:
        if inner.startswith(scope + "."):
            depth = scope.count(".") + 1
            between = [".".join(inner.split(".")[:size]) for size in range(depth + 1, inner.count(".") + 2)]
            if not any(module.binds(part, variable) for part in between):
                found.append(((inner, variable), f"it is read from the enclosing {scope}"))
    return found


def _follow(module: Module, out: dict[tuple[str, str], Carry], pending: list[tuple[str, str]]) -> None:
    """Follow each (function, variable) in ``pending`` into the functions it is passed to and the functions
    nested in its own that read it. A function that also binds the variable to something else is not followed
    further: what it stores on the variable, and passes on, may be the other value."""
    while pending:
        scope, variable = pending.pop()
        carry = out[(scope, variable)]
        for key, how in _passes(module, scope, variable) if carry.rebound is None else ():
            if key not in out:
                out[key] = Carry(carry.event, how, carry.sure, module.rebound(*key))
                pending.append(key)


def carried(module: Module, tables: Tables) -> dict[tuple[str, str], Carry]:
    """(function, variable) -> the ``CARRIED_EVENTS`` event whose dict the variable holds in that function.

    The dict is followed from each function that assigns it to a variable, through the calls the scan can
    resolve and into nested functions. A parameter that only has the name of a variable that holds the dict is
    taken for it too, and marked as not sure: the call that passes it may be one the scan cannot resolve."""
    out: dict[tuple[str, str], Carry] = {}
    built: dict[tuple[str, str], set[tuple[int, int]]] = {}  # where the carried dicts assigned there start
    for site in log_sites(module, tables):
        for name in sorted(site_names(site, tables)) if site.variable is not None and site.built else ():
            if (module.path, name) in tables.carried_events:
                built.setdefault((site.scope, site.variable), set()).add(site.built)
                out.setdefault((site.scope, site.variable), Carry(name, "it is assigned in that function", True))
    for key, carry in out.items():
        out[key] = dataclasses.replace(carry, rebound=module.rebound(*key, frozenset(built[key])))
    _follow(module, out, list(out))
    guessed = []
    for (scope, variable), carry in list(out.items()):
        for function in module.functions.values() if carry.rebound is None else ():
            key = (function.scope, variable)
            if key not in out and variable in (*function.positional, *function.named):
                out[key] = Carry(carry.event, scope, False, module.rebound(*key))
                guessed.append(key)
    _follow(module, out, guessed)
    return out


def held_names(module: Module, tables: Tables) -> dict[tuple[str, str], set[str]]:
    """(function, variable) -> the event names of the dicts assigned to it there; "" for a name not known."""
    held: dict[tuple[str, str], set[str]] = {}
    for site in log_sites(module, tables):
        if site.variable is not None:
            held.setdefault((site.scope, site.variable), set()).update(site_names(site, tables) or {""})
    return held


def _matches(pattern: str, scope: str) -> bool:
    """Whether a ``RETURNED_FIELDS`` function names ``scope``: ``*`` is any run of characters but a dot."""
    return re.fullmatch(re.escape(pattern).replace(r"\*", "[^.]*"), scope) is not None


RETURNED_FORMS = "a dict display, a conditional expression of them, or a local variable assigned only dict displays"


def _readable(module: Module, scope: str, value: ast.expr, index: int | None) -> bool:
    """Whether a ``return`` in ``scope`` gives, as the value or at ``index`` of a tuple, a form ``returned_keys``
    reads."""
    if index is not None:
        if not isinstance(value, ast.Tuple) or index >= len(value.elts):
            return False
        value = value.elts[index]
    if isinstance(value, ast.IfExp):
        return _readable(module, scope, value.body, None) or _readable(module, scope, value.orelse, None)
    return isinstance(value, ast.Dict) or (isinstance(value, ast.Name)
                                           and module.local_dicts.get((scope, value.id)) is not None)


def returned_keys(module: Module, scope: str, value: ast.expr, index: int | None) -> list[tuple[str, int]]:
    """The keys of the dict a ``return`` in ``scope`` gives, at ``index`` of a returned tuple or as the value
    itself, as far as the scan reads them."""
    if index is not None:
        if not isinstance(value, ast.Tuple) or index >= len(value.elts):
            return []
        value = value.elts[index]
    fields: list[tuple[str, int]] = []
    spreads: list[str] = []
    _spread(value, fields, spreads)
    for name in spreads:
        if module.local_dicts.get((scope, name)) is not None:
            fields += [*module.unpacked(scope, name), *module.stored(scope, name)]
    return fields


def _spread_call_keys(module: Module, site: Site) -> list[tuple[str, str]]:
    """The keys a function of the module returns, for each call to it that ``site`` unpacks with ``**``, as
    far as ``returned_keys`` reads them; each with how the test knows of it."""
    out = []
    for name, on_self, line in site.spread_calls:
        for function in _callees(module, Call(site.scope, line, name, on_self, (), ())):
            for scope, value in module.returns:
                for key, at in returned_keys(module, scope, value, None) if scope == function.scope else ():
                    out.append((key, f"returned at {module.path}:{at} by {scope}, whose result {site.scope} "
                                     f"unpacks into the event with ** at {module.path}:{line}"))
    return out


@dataclass(frozen=True)
class Known:
    """How the test knows of a field. ``how`` completes the sentence "the entry does not list the field `x`,
    ..."; ``otherwise`` is a sentence for the case where a bullet is not the fix."""

    how: str
    otherwise: str = ""


def written_fields(modules: list[Module], tables: Tables) -> dict[tuple[str, str], dict[str, Known]]:
    """(event name, source file) -> {field: how the test knows of it}: what the scan reads at each site, on
    each variable and from each ``RETURNED_FIELDS`` function, then ``HELPER_FIELDS``."""
    out: dict[tuple[str, str], dict[str, Known]] = {}

    def add(name: str, key: str, path: str, known: Known) -> None:
        out.setdefault((name, path), {}).setdefault(key, known)

    for module in modules:
        for site in log_sites(module, tables):
            if site.names is not None:  # a registered site's keys belong to only some of its names
                unpacked = [pair for spread in site.spreads for pair in module.unpacked(site.scope, spread)]
                for name in site.names:
                    for key, line in [*site.fields, *unpacked]:
                        add(name, key, module.path, Known(f"written at {module.path}:{line}"))
                    for key, how in _spread_call_keys(module, site):
                        add(name, key, module.path, Known(how))
        held = held_names(module, tables)
        carrying = carried(module, tables)
        stores = [*module.stores, *((scope, variable, key, line) for scope, variable, spread in module.spread_stores
                                    for key, line in module.unpacked(scope, spread))]
        guessed = []
        for scope, variable, key, line in stores:
            carry = carrying.get((scope, variable))
            names = held.get((scope, variable), set())
            at = f"written at {module.path}:{line}"
            if carry is not None and carry.rebound is not None:
                continue  # which value the key is stored on is not known: rebound_carriers reports it
            if carry is not None and carry.sure:
                add(carry.event, key, module.path, Known(
                    f"{at} (on `{variable}`, which holds the `{carry.event}` dict there: {carry.how}; see "
                    f"CARRIED_EVENTS in {TEST_NAME})"))
            elif carry is not None:
                guessed.append((carry.event, key, Known(
                    f"{at} (on `{variable}`, a parameter of {scope}. The scan takes it for the `{carry.event}` "
                    f"dict because {carry.how} holds that dict in a variable of the same name; it found no call "
                    f"that passes it: see CARRIED_EVENTS in {TEST_NAME})",
                    f". If `{variable}` in {scope} is not the `{carry.event}` dict, the field is not in the log: "
                    f"add no bullet, and rename that parameter instead")))
            elif len(names) == 1 and "" not in names and module.local_dicts.get((scope, variable)) is not None:
                add(next(iter(names)), key, module.path, Known(at))
        for event, key, known in guessed:  # after the stores the scan is sure of, which say more
            add(event, key, module.path, known)
        for (name, path), parts in tables.returned_fields.items():
            for scope, value in module.returns if path == module.path else ():
                for pattern, index in parts:
                    for key, line in returned_keys(module, scope, value, index) if _matches(pattern, scope) else ():
                        add(name, key, path, Known(
                            f"returned at {path}:{line} by {scope}, which RETURNED_FIELDS in {TEST_NAME} names as "
                            f"a function whose return value becomes fields of `{name}`"))
        for (path, scope), name in tables.last_events.items():
            for variable, key, line in module.stored_last(scope) if path == module.path else ():
                add(name, key, path, Known(
                    f"written at {path}:{line} (on `{variable}[-1]`, which LAST_EVENTS in {TEST_NAME} names as the "
                    f"`{name}` event in {scope})"))
    for (name, path), keys in tables.helper_fields.items():
        for key in sorted(keys):
            add(name, key, path, Known(
                f"which HELPER_FIELDS in {TEST_NAME} names as a field {path} writes in a way the scan cannot read",
                ". If the source no longer writes it, remove it from HELPER_FIELDS instead"))
    return out


def rebound_carriers(modules: list[Module], tables: Tables) -> list[str]:
    """A variable that holds a ``CARRIED_EVENTS`` dict in a function that also binds it to something else: the
    scan cannot tell which keys stored on it there, and which functions it is passed to from there, are the
    event's, so it reads none of them and the variable must be renamed."""
    problems = []
    for module in modules:
        for (scope, variable), carry in sorted(carried(module, tables).items()):
            if carry.rebound is None or not (module.stored(scope, variable) or _passes(module, scope, variable)):
                continue
            fix = "Give the other value" if carry.sure else "Rename the parameter, or give the other value"
            problems.append(
                f"{module.path}:{carry.rebound} binds `{variable}` in {scope} to another value, but `{variable}` "
                f"also holds the `{carry.event}` dict there ({carry.because(variable, scope)}), so the scan cannot "
                f"tell which keys stored on `{variable}` in {scope}, or passed on with it from there, are fields of "
                f"`{carry.event}`. {fix} another name in {scope}; add no bullet to {DOC_NAME} for a key stored on "
                f"the other value. If the other value is a default for a missing dict (`{variable} = {variable} or "
                f"{{}}`, or `if {variable} is None: {variable} = {{}}`), drop the default and pass the dict instead: "
                f"the scan does not follow the dict onto another name (`other = {variable} or {{}}`), so a key stored "
                f"there would not be read. See CARRIED_EVENTS in {TEST_NAME}")
    return problems


def ambiguous_stores(modules: list[Module], tables: Tables) -> list[str]:
    """A key stored on a variable that holds more than one event name is a field of one or more of them, and
    the scan cannot tell which: ``HELPER_FIELDS`` must name it under at least one."""
    problems = []
    for module in modules:
        for (scope, variable), names in sorted(held_names(module, tables).items()):
            if len(names) < 2 or "" in names or module.local_dicts.get((scope, variable)) is None:
                continue
            listed = set().union(*(tables.helper_fields.get((name, module.path), frozenset()) for name in names))
            keys = [f"({name!r}, {module.path!r})" for name in sorted(names)]
            for key, line in sorted(set(module.stored(scope, variable))):
                if key not in listed:
                    problems.append(
                        f"{module.path}:{line} stores the key `{key}` on `{variable}`, which holds more than one "
                        f"event in {scope} ({', '.join(sorted(names))}), so the scan cannot tell which of them "
                        f"gets it. Add `{key}` to HELPER_FIELDS in {TEST_NAME} under each of those events that "
                        f"can carry it (the keys are {' and '.join(keys)}), and a bullet - `{key}`: ... under "
                        f"each such entry in {DOC_NAME}")
    return problems


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


def _not_logged(site: Site) -> str:
    """The start of the sentence that says when a site belongs in ``NOT_SESSION_LOG``."""
    if site.callee is None:
        return "If that dict never reaches a session log"
    return (f"If `event=` in that call to `{site.callee}` passes a whole event or another object and not an event "
            f"name (the name of an event is read where its dict is built), or what the call builds never reaches "
            f"a session log")


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
                    f"must be a string literal somewhere under src/contexto) and document each in {DOC_NAME}. The "
                    f"test reads a listed site once: the names in its entry are kept by hand from then on, and so "
                    f"are the fields of a name a later caller passes. {_not_logged(site)}, add {site.key!r} to "
                    f"NOT_SESSION_LOG instead")
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
    """``CARRIED_EVENTS``, ``HELPER_FIELDS`` and ``RETURNED_FIELDS`` name things the source has.
    (``DYNAMIC_SITES`` and ``NOT_SESSION_LOG`` are checked by ``unread_sites``, ``SHARED_NAMES`` by
    ``shared_name_problems``.)"""
    problems = []
    written = written_names(modules, tables)
    words = set().union(*(module.vocabulary for module in modules)) if modules else set()
    by_path = {module.path: module for module in modules}
    for path, event in sorted(tables.carried_events):
        if path not in by_path:
            problems.append(f"CARRIED_EVENTS in {TEST_NAME} lists {path}, which does not exist; remove the entry, or "
                            f"name the file that builds `{event}` now")
        elif not any(carry.event == event for carry in carried(by_path[path], tables).values()):
            problems.append(f"CARRIED_EVENTS in {TEST_NAME} lists the event `{event}` in {path}, but no variable "
                            f"there is assigned a dict with that event any more; remove the entry, or name the "
                            f"file that builds `{event}` now")
    for module in modules:
        for (scope, variable), names in sorted(held_names(module, tables).items()):
            events = sorted(name for name in names if (module.path, name) in tables.carried_events)
            if len(events) > 1:
                problems.append(f"{module.path} assigns the events {', '.join(events)} to `{variable}` in {scope}, "
                                f"and CARRIED_EVENTS in {TEST_NAME} lists more than one of them, so a key stored on "
                                f"`{variable}` cannot be given to one event; give the dicts different names")

    def writes(table: str, event: str, path: str) -> bool:
        """Whether ``path`` writes ``event``; if not, the problem is added."""
        paths = sorted({site.path for site in written.get(event, [])})
        if not paths:
            problems.append(f"{table} in {TEST_NAME} names fields of the event `{event}`, which nothing under "
                            f"src/contexto writes; remove the entry, or rename it to the name the source writes")
        elif path not in paths:
            problems.append(f"{table} in {TEST_NAME} names fields of the event `{event}` as {path} writes it, but "
                            f"{path} does not write `{event}` ({', '.join(paths)} "
                            f"{'does' if len(paths) == 1 else 'do'}); correct the file in that entry's key")
        return path in paths

    for (event, path), keys in sorted(tables.helper_fields.items()):
        if writes("HELPER_FIELDS", event, path):
            for key in sorted(keys - words):
                problems.append(f"HELPER_FIELDS in {TEST_NAME} names the field `{key}` of `{event}`, but `{key}` does "
                                f"not appear anywhere under src/contexto; remove it from HELPER_FIELDS, or rename it "
                                f"to the key the source writes")
    for (event, path), parts in sorted(tables.returned_fields.items()):
        if writes("RETURNED_FIELDS", event, path):
            module = by_path[path]
            for pattern, index in parts:
                if not any(_matches(pattern, scope) for scope in module.functions):
                    problems.append(f"RETURNED_FIELDS in {TEST_NAME} names the function `{pattern}` in {path} for the "
                                    f"event `{event}`, but {path} has no function of that name; correct it to the "
                                    f"function whose return value becomes fields of `{event}` now, or remove it "
                                    f"and list those fields in HELPER_FIELDS")
                elif not any(_readable(module, scope, value, index) for scope, value in module.returns
                             if _matches(pattern, scope)):
                    part = "the returned value" if index is None else f"element {index} of a returned tuple"
                    problems.append(f"RETURNED_FIELDS in {TEST_NAME} names `{pattern}` in {path} for the event "
                                    f"`{event}`, but no `return` there gives, as {part}, a form the scan "
                                    f"reads ({RETURNED_FORMS}), so it reads no field of `{event}` from it. Update the "
                                    f"table: name the function that returns that dict now, or correct the part, or "
                                    f"remove the entry and list the fields it returns in HELPER_FIELDS")
    for (path, scope), event in sorted(tables.last_events.items()):
        if not writes("LAST_EVENTS", event, path):
            continue
        module = by_path[path]
        lists = sorted({variable for variable, _key, _line in module.stored_last(scope)})
        if scope not in module.functions:
            problems.append(f"LAST_EVENTS in {TEST_NAME} names the function {scope} in {path} for the event `{event}`, "
                            f"but {path} has no function of that name; correct it to the function that stores keys "
                            f"on the last element of a list of events now, or remove it and list those keys in "
                            f"HELPER_FIELDS")
        elif not lists:
            problems.append(f"LAST_EVENTS in {TEST_NAME} names {scope} in {path} for the event `{event}`, but {scope} "
                            f"stores no key on the last element of a list (`x[-1][\"key\"] = ...`, "
                            f"`x[-1].update(...)`) any more; remove the entry, or correct it to the function that "
                            f"does now")
        elif len(lists) > 1:
            problems.append(f"{scope} in {path} stores keys on the last elements of {' and '.join(lists)}, and "
                            f"LAST_EVENTS in {TEST_NAME} says the last element there is the `{event}` event, so the "
                            f"scan cannot tell which list holds it; store the other list's keys in another function, "
                            f"or name the element in a variable first")
    return problems


def undocumented_events(written: dict[str, list[Site]], doc: Document) -> list[str]:
    listed = {entry.name for entry in doc.entries}
    return [f"{DOC_NAME} does not document the event `{name}` written at {', '.join(s.where for s in sites)}; "
            f"add an entry for it: a heading ### `{name}` in the section for its writer, then its writer, where it "
            f"appears, its kind, what it means, and one bullet per field, and add its row to the index. A writer "
            f"the document does not have yet goes into WRITERS in {TEST_NAME} and into the list under "
            f"\"{VOCABULARY_HEADING}\" first. {_not_logged(sites[0])}, add {sites[0].key!r} to NOT_SESSION_LOG in "
            f"{TEST_NAME} instead"
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


def undocumented_fields(fields: dict[tuple[str, str], dict[str, Known]], doc: Document,
                        tables: Tables) -> list[str]:
    problems = []
    for (name, path), keys in sorted(fields.items()):
        entry = entry_for(doc, name, path, tables)
        if entry is None:  # no entry, or more than one and SHARED_NAMES does not choose: reported elsewhere
            continue
        for key, known in sorted(keys.items()):
            if key not in entry.fields:
                problems.append(f"{DOC_NAME}:{entry.line} the entry {entry.title} does not list the field `{key}`, "
                                f"{known.how}; add a bullet - `{key}`: ... under that heading{known.otherwise}")
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
        for label, table, allowed in VOCABULARY:
            given = _listed(match[label + "s"])
            for value in given:
                if value not in allowed:
                    problems.append(f"{DOC_NAME}:{entry.line} the entry for `{entry.name}` names the {label} "
                                    f"{value!r}; use one of: {', '.join(sorted(allowed))}. For a new {label}, add "
                                    f"it to {table} in {TEST_NAME} and to the list under \"{VOCABULARY_HEADING}\" "
                                    f"in {DOC_NAME}, then use it")
            if len(set(given)) != len(given):
                problems.append(f"{DOC_NAME}:{entry.line} the entry for `{entry.name}` names a {label} twice")
    return problems


def vocabulary_problems(doc: Document) -> list[str]:
    """The section that explains a facts line names every writer, place and kind an entry may use."""
    start = next((line for line, heading in doc.headings if heading == VOCABULARY_HEADING), None)
    if start is None:
        return [f"{DOC_NAME} has no heading \"{VOCABULARY_HEADING}\"; it is the section that lists the writers, "
                f"places and kinds a facts line may use. Restore it"]
    end = next((line for line, _heading in doc.headings if line > start), len(doc.lines) + 1)
    section = "\n".join(doc.lines[start:end - 1])
    words = [(label, table, word.strip("`")) for label, table, allowed in VOCABULARY for word in sorted(allowed)]
    return [f"{DOC_NAME}:{start} the section \"{VOCABULARY_HEADING}\" does not name the {label} `{word}`, which "
            f"{table} in {TEST_NAME} allows in a facts line; add it to that section's list, or remove it from {table}"
            for label, table, word in words if f"`{word}`" not in section]


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

def named(log, error, constant=A_CONSTANT):
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

def paired(pair, log):
    first = {}
    first.update([("event", "a_pair")])
    second = dict([("event", "a_pair_call"), ("size", 1)])
    third = {"size": 1}
    third["event"], third["count"] = pair
    count = third["count"]
    kind = "a_start"
    kind += "_more"
    log({"event": kind})
    return first, second, third, count

def passed(store, failure):
    store.log(event=failure)
'''
ALL_FORMS = {"a_literal", "a_continued", "a_conflict", "an_error", "a_dict_call", "an_update", "a_setdefault",
             "a_subscript", "a_variable_holder", "a_tuple_target", "a_spread", "a_spread_store", "a_spread_call",
             "a_first", "a_second", "a_constant", "a_keyword_call", "a_rebound", "a_dict_holder", "error", "a_pair",
             "a_pair_call"}
DYNAMIC_FORM = ("forms.py", "Helper.dynamic", "kind")
FSTRING_FORM = ("forms.py", "named", "f'a_{kind}'")
FRAME_FORM = ("forms.py", "frame", "'error'")
TARGET_FORM = ("forms.py", "paired", "third['event']")  # one of several targets: its value is not read
AUGMENTED_FORM = ("forms.py", "paired", "kind")  # a string that was added to is no longer the literal
OBJECT_FORM = ("forms.py", "passed", "failure")  # ``event=`` that passes a whole event
UNREAD_FORMS = [DYNAMIC_FORM, FSTRING_FORM, TARGET_FORM, AUGMENTED_FORM, OBJECT_FORM]
NONE = Tables()


def test_the_scan_reads_every_form_it_claims_to():
    module = scan_source(FORMS, "forms.py")
    by_name = {name: site for site in module.sites for name in site.names or ()}
    assert set(by_name) == ALL_FORMS
    assert by_name["a_continued"].line == 8  # the line of the name, not of the dict's opening brace
    assert by_name["a_conflict"] is by_name["an_error"]
    assert by_name["a_first"] is by_name["a_second"]  # a local variable, read through its one assignment
    unread = [site for site in module.sites if site.names is None]
    assert [site.key for site in unread] == UNREAD_FORMS
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
    first, second, target, augmented, passed = unread_sites([module], NONE)
    assert first.startswith(
        "forms.py:33 (in Helper.dynamic) writes an event whose name the scan cannot read (`kind`). It reads a string "
        "literal, a conditional expression of string literals, or a local variable or module constant that is "
        "assigned only those. Either write the name in one of those forms, or add ('forms.py', 'Helper.dynamic', "
        "'kind') to DYNAMIC_SITES in tests/test_events_doc.py with the event names it can produce")
    assert first.endswith(
        "The test reads a listed site once: the names in its entry are kept by hand from then on, and so are the "
        "fields of a name a later caller passes. If that dict never reaches a session log, add ('forms.py', "
        "'Helper.dynamic', 'kind') to NOT_SESSION_LOG instead")
    assert second.startswith("forms.py:57 (in named) writes an event whose name the scan cannot read (`f'a_{kind}'`)")
    assert target.startswith("forms.py:77 (in paired) writes an event whose name the scan cannot read "
                             "(`third['event']`)")
    # `kind = "a_start"` then `kind += "_more"`: not an event named `a_start`, nor one named `_more`
    assert augmented.startswith("forms.py:81 (in paired) writes an event whose name the scan cannot read (`kind`)")
    assert passed.endswith(
        "If `event=` in that call to `store.log` passes a whole event or another object and not an event name (the "
        "name of an event is read where its dict is built), or what the call builds never reaches a session log, "
        "add ('forms.py', 'passed', 'failure') to NOT_SESSION_LOG instead")


def test_a_listed_site_can_name_what_a_caller_passes_and_only_that_site_is_covered():
    module = scan_source(FORMS, "forms.py")
    # the names need only be string literals somewhere in the source, as a caller's argument is
    rest = frozenset(UNREAD_FORMS[2:])
    listed = Tables(dynamic_sites={DYNAMIC_FORM: frozenset({"a_literal"}), FSTRING_FORM: frozenset({"a_first"})},
                    not_session_log=rest)
    assert unread_sites([module], listed) == []
    assert "a_literal" in written_names([module], listed)
    # an entry covers its own site, not another in the same function, and its names must exist somewhere
    one = Tables(dynamic_sites={DYNAMIC_FORM: frozenset({"never_written"})}, not_session_log=rest)
    problems = unread_sites([module], one)
    assert len(problems) == 2 and problems[1].startswith("forms.py:57 (in named) writes an event whose name")
    assert "`never_written` is not a string literal anywhere under src/contexto" in problems[0]
    stale = dataclasses.replace(listed, dynamic_sites={**listed.dynamic_sites,
                                                       ("forms.py", "literal", "name"): frozenset({"a_literal"})})
    assert unread_sites([module], stale) == [
        "DYNAMIC_SITES in tests/test_events_doc.py lists ('forms.py', 'literal', 'name'), but literal in forms.py "
        "has no event whose name is written `name` and that the scan cannot read; remove the entry, or correct it "
        "to the site as it is now"]


def test_not_session_log_exempts_one_site_and_a_stale_entry_is_reported():
    module = scan_source(FORMS, "forms.py")
    exempt = Tables(not_session_log=frozenset({FRAME_FORM, *UNREAD_FORMS}))
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
            "ambiguous": ambiguous_stores(modules, tables), "rebound": rebound_carriers(modules, tables),
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
    assert removed["undocumented"][0] == (
        "docs/events.md does not document the event `plain` written at inner.py:5; add an entry for it: a heading "
        "### `plain` in the section for its writer, then its writer, where it appears, its kind, what it means, and "
        "one bullet per field, and add its row to the index. A writer the document does not have yet goes into "
        "WRITERS in tests/test_events_doc.py and into the list under \"Reading an entry\" first. If that dict never "
        "reaches a session log, add ('inner.py', 'nested', \"'plain'\") to NOT_SESSION_LOG in "
        "tests/test_events_doc.py instead")
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
    assert carried.endswith(
        "does not list the field `status`, written at line.py:8 (on `entry`, which holds the `shared` dict there: "
        "line passes it at line.py:4; see CARRIED_EVENTS in tests/test_events_doc.py); add a bullet - `status`: ... "
        "under that heading")
    helper = _only(_sample(SAMPLE_DOC.replace("- `ended`", "- ended")), "fields")
    assert helper.endswith(
        "does not list the field `ended`, which HELPER_FIELDS in tests/test_events_doc.py names as a field line.py "
        "writes in a way the scan cannot read; add a bullet - `ended`: ... under that heading. If the source no "
        "longer writes it, remove it from HELPER_FIELDS instead")


LINE_FIELDS = {"size", "epoch", "status", "transform_events", "ended"}


def _line_fields(source: str) -> dict[str, Known]:
    module = scan_source(source, "line.py")
    assert table_problems([module], SAMPLE) == []
    return written_fields([module], SAMPLE)[("shared", "line.py")]


def test_a_carried_event_is_followed_under_whatever_name_its_variable_has():
    assert set(_line_fields(LINE_SOURCE)) == LINE_FIELDS
    assert set(_line_fields(LINE_SOURCE.replace("entry", "rec"))) == LINE_FIELDS  # renamed throughout
    # renamed in the function that receives it alone, and in the function that creates it alone: the call
    # says which parameter gets the dict, whatever its name
    received = LINE_SOURCE.replace("def fill(entry):", "def fill(rec):").replace("    entry[", "    rec[")
    assert "    rec.update(ending())" in (received := received.replace("    entry.update(", "    rec.update("))
    assert set(_line_fields(received)) == LINE_FIELDS
    created = LINE_SOURCE.replace("    entry = {", "    rec = {").replace("fill(entry)\n    log(entry)",
                                                                         "fill(rec)\n    log(rec)")
    assert "rec = {" in created and "fill(rec)" in created
    assert set(_line_fields(created)) == LINE_FIELDS
    # through a method called on self, by keyword, and into a function nested in the one that receives it
    methods = '''
class Handler:
    def line(self, log):
        entry = {"event": "shared", "size": 1}
        self.fill(0, target=entry)
        log(entry)

    def fill(self, code, *, target):
        target["status"] = code

        def late():
            target["ended"] = "x"
        late()
'''
    fields = _line_fields(methods)
    assert set(fields) == {"size", "status", "ended"}
    assert "(on `target`, which holds the `shared` dict there: it is read from the enclosing Handler.fill;" in (
        fields["ended"].how)
    # x |= {...} updates the dict in place, as x.update({...}) does
    merged = LINE_SOURCE.replace("    entry.update(ending())\n",
                                 "    entry.update(ending())\n    entry |= {'merged': 1}\n")
    assert set(_line_fields(merged)) == LINE_FIELDS | {"merged"}
    assert rebound_carriers([scan_source(merged, "line.py")], SAMPLE) == []


CARRIED = Tables(carried_events=frozenset({("line.py", "shared")}))
INHERITED_SOURCE = '''
class Base:
    def _note(self, rec):
        rec["noted"] = 1

class Handler(Base):
    def line(self, log):
        entry = {"event": "shared", "size": 1}
        self._note(entry)
        log(entry)

class Other(Base):
    def _note(self, row):
        row["other_only"] = 1
'''


def test_a_call_on_self_reaches_the_calling_class_s_method_in_resolution_order_and_no_other():
    def _inherited(source: str) -> dict[str, Known]:
        return written_fields([scan_source(source, "line.py")], CARRIED)[("shared", "line.py")]

    # Handler inherits Base._note; Other's override is never what Handler runs
    fields = _inherited(INHERITED_SOURCE)
    assert set(fields) == {"size", "noted"}
    assert "Handler.line passes it at line.py:9" in fields["noted"].how
    # with the parameter name of a function that holds the dict, the override is taken for it by that name alone,
    # and the message says so (see CARRIED_EVENTS)
    guessed = _inherited(INHERITED_SOURCE.replace("row", "rec"))
    assert set(guessed) == {"size", "noted", "other_only"}
    assert "(on `rec`, a parameter of Other._note. The scan takes it for the `shared` dict because" in (
        guessed["other_only"].how)
    # a class of the file between Handler and Base that overrides it is what Handler runs
    middle = INHERITED_SOURCE.replace("class Handler(Base):", '''class Middle(Base):
    def _note(self, item):
        item["middle"] = 1

class Handler(Middle):''')
    assert set(_inherited(middle)) == {"size", "middle"}
    # the order is Python's: Handler(Left, Right) with both on Base runs Right's before Base's
    diamond = INHERITED_SOURCE.replace("class Handler(Base):", '''class Left(Base):
    pass

class Right(Base):
    def _note(self, item):
        item["right"] = 1

class Handler(Left, Right):''')
    assert set(_inherited(diamond)) == {"size", "right"}
    # a base the file does not define comes first in the order and may define it: nothing is read
    for bases in ("Mixin, Base", "mixins.Mixin, Base"):
        external = INHERITED_SOURCE.replace("class Handler(Base):", f"class Handler({bases}):")
        assert set(_inherited(external)) == {"size"}, bases
    # after the method is found, a base the file does not define is not looked at
    assert set(_inherited(INHERITED_SOURCE.replace("class Handler(Base):", "class Handler(Base, Mixin):"))) == {
        "size", "noted"}


def test_an_unrelated_variable_with_the_carrier_name_is_not_the_event():
    # a function that binds the name itself, to anything, holds another variable: none of its keys is a field
    for unrelated in ('''
def remember(cache, key):
    entry = cache.setdefault(key, {})
    entry["hits"] = entry.get("hits", 0) + 1
''', '''
def mark(rows):
    for entry in rows:
        entry["seen"] = True
''', '''
def pair(n, v):
    entry = {"name": n}
    entry["value"] = v
    entry.update(more=1)
    return entry
'''):
        assert set(_line_fields(LINE_SOURCE + unrelated)) == LINE_FIELDS, unrelated
    # a parameter with that name in a function no call passes the dict to cannot be told apart from a call
    # the scan does not resolve (`handler.note(entry)`): its keys are read, and the message names both fixes
    guessed = _line_fields(LINE_SOURCE + '''
def note(entry, code):
    entry["noted"] = code
''')["noted"]
    assert guessed.how == (
        "written at line.py:16 (on `entry`, a parameter of note. The scan takes it for the `shared` dict because "
        "line holds that dict in a variable of the same name; it found no call that passes it: see CARRIED_EVENTS "
        "in tests/test_events_doc.py)")
    assert guessed.otherwise == (". If `entry` in note is not the `shared` dict, the field is not in the log: add no "
                                 "bullet, and rename that parameter instead")


def test_a_function_that_binds_the_carrier_to_another_value_is_not_followed():
    calling = LINE_SOURCE.replace("    fill(entry)\n", "    fill(entry)\n    copy(entry)\n")
    for name, line, helper in (("rec", 17, '''
def copy(rec):
    rec = dict(rec)
    rec["copied"] = 1
'''), ("entry", 17, '''
def copy(entry):
    entry = {"seen": len(entry)}
    entry["copied"] = 1
'''), ("rec", 18, '''
def copy(rec, rows=()):
    rec["copied"] = 1
    for rec in rows:
        rec["looped"] = True
'''), ("rec", 17, '''
def copy(rec):
    with open(rec["path"]) as rec:
        mark(rec)

def mark(rec):
    rec["copied"] = 1
'''), ("entry", 17, '''
def copy(entry=None):
    entry = entry or {}
    entry["copied"] = 1
'''), ("entry", 18, '''
def copy(entry=None):
    if entry is None:
        entry = {}
    entry["copied"] = 1
''')):
        module = scan_source(calling + helper, "line.py")
        # none of what is stored on the variable in that function, or passed on from it, is taken for a field
        assert set(written_fields([module], SAMPLE)[("shared", "line.py")]) == LINE_FIELDS, helper
        assert rebound_carriers([module], SAMPLE) == [
            f"line.py:{line} binds `{name}` in copy to another value, but `{name}` also holds the `shared` dict there "
            f"(line passes it at line.py:5), so the scan cannot tell which keys stored on `{name}` in copy, or passed "
            f"on with it from there, are fields of `shared`. Give the other value another name in copy; add no "
            f"bullet to docs/events.md for a key stored on the other value. If the other value is a default for a "
            f"missing dict (`{name} = {name} or {{}}`, or `if {name} is None: {name} = {{}}`), drop the default and "
            f"pass the dict instead: the scan does not follow the dict onto another name (`other = {name} or {{}}`), "
            f"so a key stored there would not be read. See CARRIED_EVENTS in tests/test_events_doc.py"], helper
    # a rebinding that neither stores a key on the variable nor passes it on is harmless
    assert rebound_carriers([scan_source(calling + "\ndef copy(rec):\n    rec = len(rec)\n", "line.py")], SAMPLE) == []
    # the function that builds the dict is held to the same rule
    looped = LINE_SOURCE.replace("    log(entry)\n", "    log(entry)\n    for entry in []:\n        entry['x'] = 1\n")
    module = scan_source(looped, "line.py")
    assert set(written_fields([module], SAMPLE)[("shared", "line.py")]) == {"size", "epoch", "ended"}
    assert rebound_carriers([module], SAMPLE)[0].startswith(
        "line.py:6 binds `entry` in line to another value, but `entry` also holds the `shared` dict there (it is "
        "assigned in that function)")
    # a parameter taken for the dict by its name alone: renaming the parameter is a fix too
    guessed = rebound_carriers([scan_source(LINE_SOURCE + "\ndef note(entry):\n    entry = {}\n    entry['k'] = 1\n",
                                            "line.py")], SAMPLE)
    assert len(guessed) == 1 and "(it is a parameter of note with the name of the variable that holds the dict in " \
        "line, and the scan found no call that passes it), so" in guessed[0] and ". Rename the parameter, or give " \
        "the other value another name in note;" in guessed[0], guessed


LAST_SOURCE = '''
def rewrite():
    return [{"event": "nested", "epoch": 0}]

def engine():
    events = rewrite()
    events[-1].update(main=True)
    events[-1]["side"] = 1
    events[-1].setdefault("count", 0)
    return events

def other(spans):
    spans[-1]["end"] = 2
'''
LAST = Tables(last_events={("last.py", "engine"): "nested"})


def test_a_key_stored_on_the_last_element_of_a_named_function_s_list_is_a_field():
    module = scan_source(LAST_SOURCE, "last.py")
    assert table_problems([module], LAST) == []
    fields = written_fields([module], LAST)[("nested", "last.py")]
    assert set(fields) == {"epoch", "main", "side", "count"}  # not `end`, stored in a function the table does not name
    assert fields["side"].how == ("written at last.py:8 (on `events[-1]`, which LAST_EVENTS in "
                                  "tests/test_events_doc.py names as the `nested` event in engine)")
    assert set(written_fields([module], NONE)[("nested", "last.py")]) == {"epoch"}
    renamed = scan_source(LAST_SOURCE.replace("events", "found"), "last.py")
    assert set(written_fields([renamed], LAST)[("nested", "last.py")]) == set(fields)
    gone = scan_source(LAST_SOURCE.replace("def engine", "def engine2"), "last.py")
    assert table_problems([gone], LAST) == [
        "LAST_EVENTS in tests/test_events_doc.py names the function engine in last.py for the event `nested`, but "
        "last.py has no function of that name; correct it to the function that stores keys on the last element of "
        "a list of events now, or remove it and list those keys in HELPER_FIELDS"]
    none = scan_source(LAST_SOURCE.replace("    events[-1]", "    last = events[-1]\n    last"), "last.py")
    assert table_problems([none], LAST)[0].startswith(
        "LAST_EVENTS in tests/test_events_doc.py names engine in last.py for the event `nested`, but engine stores no "
        "key on the last element of a list")
    two = scan_source(LAST_SOURCE.replace("    return events", "    spans = []\n    spans[-1]['end'] = 1\n"
                                          "    return events"), "last.py")
    assert table_problems([two], LAST) == [
        "engine in last.py stores keys on the last elements of events and spans, and LAST_EVENTS in "
        "tests/test_events_doc.py says the last element there is the `nested` event, so the scan cannot tell which "
        "list holds it; store the other list's keys in another function, or name the element in a variable first"]


RETURNING_SOURCE = '''
class Tools:
    def call(self, name, args, log):
        event = {"event": "called", "tool": name}
        content, structured, extra = getattr(self, "_tool_" + name)(args)
        event.update(extra, ok=True)
        log(event)

    def _tool_read(self, args):
        budget = self._room()
        return [args], {"shown": 1}, {"chars": 1, **({"more": 2} if args else {}), **budget}

    def _tool_write(self, args):
        fields = {"kind": args}
        fields["size"] = 1

        def inner():
            return 1, 2, {"not_a_field": 3}
        if not args:
            return self._tool_read(args)
        return [inner()], None, fields

    def _room(self):
        return {"room_left": 1}

def failure(error, log):
    kind = "conflict" if error else "broken"
    event = {"event": kind, "at": 1}
    if error:
        event["page"] = error
    log(event)
'''
RETURNING = Tables(returned_fields={("called", "tools.py"): (("Tools._tool_*", 2),)},
                   helper_fields={("called", "tools.py"): frozenset({"room_left"}),
                                  ("conflict", "tools.py"): frozenset({"page"})})


def test_fields_a_named_function_returns_are_read_and_a_stale_name_is_reported():
    module = scan_source(RETURNING_SOURCE, "tools.py")
    fields = written_fields([module], RETURNING)[("called", "tools.py")]
    # the third element of each return: a display, both branches of a conditional, a local dict and the keys
    # stored on it; not the second element, not a nested function's return, not what a call returns
    assert set(fields) == {"tool", "ok", "chars", "more", "kind", "size", "room_left"}
    assert fields["more"].how == (
        "returned at tools.py:11 by Tools._tool_read, which RETURNED_FIELDS in tests/test_events_doc.py names as a "
        "function whose return value becomes fields of `called`")
    assert table_problems([module], RETURNING) == []
    # a named function that no longer returns a form the scan reads would read nothing: that fails
    unread = dataclasses.replace(RETURNING, returned_fields={("called", "tools.py"): (("Tools._room", 2),)})
    assert table_problems([module], unread) == [
        "RETURNED_FIELDS in tests/test_events_doc.py names `Tools._room` in tools.py for the event `called`, but no "
        "`return` there gives, as element 2 of a returned tuple, a form the scan reads (a dict display, a conditional "
        "expression of them, or a local variable assigned only dict displays), so it reads no field of `called` "
        "from it. Update the table: name the function that returns that dict now, or correct the part, or remove "
        "the entry and list the fields it returns in HELPER_FIELDS"]
    assert table_problems([module], dataclasses.replace(RETURNING, returned_fields={
        ("called", "tools.py"): (("Tools._room", None),)})) == []
    stale = dataclasses.replace(RETURNING, returned_fields={
        ("called", "tools.py"): (("Tools._tool_*", 2), ("Tools.gone", None)), ("called", "other.py"): (("x", None),)})
    assert table_problems([module], stale) == [
        "RETURNED_FIELDS in tests/test_events_doc.py names fields of the event `called` as other.py writes it, but "
        "other.py does not write `called` (tools.py does); correct the file in that entry's key",
        "RETURNED_FIELDS in tests/test_events_doc.py names the function `Tools.gone` in tools.py for the event "
        "`called`, but tools.py has no function of that name; correct it to the function whose return value becomes "
        "fields of `called` now, or remove it and list those fields in HELPER_FIELDS"]


def test_a_call_unpacked_into_an_event_gives_the_keys_its_function_returns():
    source = '''
class Server:
    def start(self, log, other):
        log({"event": "started", "port": 1, **self._pages(), **sizes(), **other.pages()})

    def _pages(self):
        count = 1
        return {} if count is None else {"pages": count}

def sizes():
    found = {"budget": 1}
    found["room"] = 2
    return found
'''
    fields = written_fields([scan_source(source, "server.py")], NONE)[("started", "server.py")]
    assert set(fields) == {"port", "pages", "budget", "room"}  # not what a call on another object returns
    assert fields["pages"].how == ("returned at server.py:8 by Server._pages, whose result Server.start unpacks "
                                   "into the event with ** at server.py:4")


def test_an_unpacked_call_reaches_the_function_the_caller_runs_and_no_other():
    def fields(source: str) -> set[str]:
        return set(written_fields([scan_source(source, "m.py")], NONE)[("started", "m.py")])

    sibling = '''
class Base:
    def _pages(self):
        return {"pages": 1}

class Proxy(Base):
    def start(self, log):
        log({"event": "started", "port": 1, **self._pages()})

class Other(Base):
    def _pages(self):
        return {"other_only": 1}
'''
    assert fields(sibling) == {"port", "pages"}  # Proxy runs Base._pages, never Other's
    override = sibling.replace("class Proxy(Base):",
                               "class Proxy(Base):\n    def _pages(self):\n        return {'own': 1}\n")
    assert fields(override) == {"port", "own"}
    # a base in another file may define it: nothing is read, whatever the file's other classes define
    assert fields(sibling.replace("class Proxy(Base):", "class Proxy(Imported):")) == {"port"}
    shadowed = '''
def start(log, sizes):
    log({"event": "started", "port": 1, **sizes()})

def sizes():
    return {"budget": 1}
'''
    assert fields(shadowed) == {"port"}  # the parameter, not the function of the module
    local = shadowed.replace("def start(log, sizes):\n", "def start(log, table):\n    sizes = table.get\n")
    assert fields(local) == {"port"}
    assert fields(shadowed.replace("def start(log, sizes):", "def start(log):")) == {"port", "budget"}
    nested = shadowed.replace("def start(log, sizes):\n", "def start(log, sizes):\n    def sizes():\n"
                                                          "        return {'inner': 1}\n")
    assert fields(nested) == {"port", "inner"}  # a function nested in the caller comes first


def test_a_key_stored_on_a_variable_that_holds_two_events_must_be_named_in_helper_fields():
    module = scan_source(RETURNING_SOURCE, "tools.py")
    assert ambiguous_stores([module], RETURNING) == []
    assert "page" not in written_fields([module], NONE).get(("broken", "tools.py"), {})
    assert ambiguous_stores([module], NONE) == [
        "tools.py:30 stores the key `page` on `event`, which holds more than one event in failure (broken, "
        "conflict), so the scan cannot tell which of them gets it. Add `page` to HELPER_FIELDS in "
        "tests/test_events_doc.py under each of those events that can carry it (the keys are ('broken', 'tools.py') "
        "and ('conflict', 'tools.py')), and a bullet - `page`: ... under each such entry in docs/events.md"]


def test_a_table_entry_the_source_no_longer_has_is_reported():
    gone = dataclasses.replace(SAMPLE, carried_events=frozenset({("line.py", "plain")}))
    assert _sample(tables=gone)["tables"] == [
        "CARRIED_EVENTS in tests/test_events_doc.py lists the event `plain` in line.py, but no variable there is "
        "assigned a dict with that event any more; remove the entry, or name the file that builds `plain` now"]
    stale = dataclasses.replace(SAMPLE, helper_fields={("shared", "line.py"): frozenset({"ended", "dropped"}),
                                                       ("removed", "line.py"): frozenset({"size"}),
                                                       ("plain", "lime.py"): frozenset({"count"})})
    assert _sample(tables=stale)["tables"] == [
        "HELPER_FIELDS in tests/test_events_doc.py names fields of the event `plain` as lime.py writes it, but "
        "lime.py does not write `plain` (inner.py does); correct the file in that entry's key",
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


def test_a_new_writer_place_or_kind_is_added_in_the_test_and_in_the_document():
    renamed = SAMPLE_DOC.replace("Writer: engine. Where: `transform_events`. Kind: normal.",
                                 "Writer: router. Where: `transform_events`. Kind: normal.")
    facts = _only(_sample(renamed.replace("| engine | `transform_events` | normal |",
                                          "| router | `transform_events` | normal |")), "facts")
    assert facts == (
        "docs/events.md:32 the entry for `plain` names the writer 'router'; use one of: MCP server, engine, launcher "
        "(Claude), launcher (Codex), proxy, store. For a new writer, add it to WRITERS in tests/test_events_doc.py "
        "and to the list under \"Reading an entry\" in docs/events.md, then use it")
    section = ("### Reading an entry\n\nWriter is `proxy`, `engine`, `launcher (Claude)`, `launcher (Codex)` or "
               "`store`.\nWhere is `own line`, `transform_events` or `usage_events`. Kind is `normal`, `notice` or "
               "`problem`.\n\n### Next\n\n`MCP server`\n")
    assert vocabulary_problems(read_document(section)) == [
        "docs/events.md:1 the section \"Reading an entry\" does not name the writer `MCP server`, which WRITERS in "
        "tests/test_events_doc.py allows in a facts line; add it to that section's list, or remove it from WRITERS"]
    assert vocabulary_problems(read_document(section.replace("`launcher (Codex)` or", "`launcher (Codex)`, `MCP "
                                                             "server` or"))) == []
    assert vocabulary_problems(read_document(SAMPLE_DOC))[0].startswith(
        "docs/events.md has no heading \"Reading an entry\"")


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


def test_a_variable_that_holds_a_carried_event_holds_nothing_else():
    problems = rebound_carriers(scan_package(), TABLES)
    assert not problems, _report(problems)


def test_a_key_stored_on_a_variable_with_several_events_is_named_here():
    problems = ambiguous_stores(scan_package(), TABLES)
    assert not problems, _report(problems)


def test_every_documented_field_appears_in_a_module_that_writes_the_event():
    modules = scan_package()
    problems = unwritten_fields(modules, written_names(modules, TABLES), _document(), TABLES)
    assert not problems, _report(problems)


def test_every_entry_states_its_writer_place_and_kind():
    doc = _document()
    problems = missing_facts(doc) + vocabulary_problems(doc)
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
# as it is, so an event or a field renamed in the source and the document together does not fail it. Where
# the document is behind the source (an event with no entry, a field with no bullet, an entry with no index
# row), there is no line to change and the tests above say what to add: this one leaves that case to them.

def test_the_real_document_fails_when_a_field_an_index_row_or_an_entry_goes():
    doc = _document()
    modules = scan_package()
    fields = written_fields(modules, TABLES)
    written = written_names(modules, TABLES)
    broken = (f"is a fault in the check, not in {DOC_NAME} or the source: correct {{check}} in {TEST_NAME}, or "
              f"report it")
    for (name, path), keys in sorted(fields.items()):  # every field the test knows of, in its own entry
        entry = entry_for(doc, name, path, TABLES)
        for key in sorted(keys) if entry is not None else ():
            if key not in entry.fields:
                continue
            bullet = doc.lines[entry.fields[key] - 1]
            without = doc.with_line(entry.fields[key], bullet.replace(f"- `{key}`", f"- {key}", 1))
            assert any(f"the entry {entry.title} does not list the field `{key}`, " in problem
                       for problem in undocumented_fields(fields, without, TABLES)), (
                f"with the bullet for `{key}` at {DOC_NAME}:{entry.fields[key]} taken out of the entry {entry.title}, "
                f"the check for an undocumented field of `{name}` as {path} writes it reports nothing. That "
                f"{broken.format(check='undocumented_fields')}")
    for entry in doc.entries:
        row = next((number for number, text in doc.index if text == entry.index_row), None)
        if row is not None:
            assert any(f"the index has no row for the entry {entry.title} " in problem
                       for problem in index_problems(doc.with_line(row))), (
                f"with the index row at {DOC_NAME}:{row} removed, the check of the index does not report the entry "
                f"{entry.title} as having no row. That {broken.format(check='index_problems')}")
        if len(doc.named(entry.name)) == 1 and entry.name in written:
            gone = doc.with_line(entry.line, entry.title)  # no longer a heading
            assert any(problem.startswith(f"{DOC_NAME} does not document the event `{entry.name}` written at ")
                       for problem in undocumented_events(written, gone)), (
                f"with the heading at {DOC_NAME}:{entry.line} no longer a heading, the check for an undocumented "
                f"event does not report `{entry.name}`. That {broken.format(check='undocumented_events')}")
