"""Cost of measured tokens at published prices.

The price table is data: ``data/prices.json`` ships in the package, and ``load(path)`` reads a
replacement (``contexto bench --prices FILE``). Each model has one or more price rows in USD per
million tokens, its source URL and the date the source was read.

A model with more than one row is priced by request size, and the rule is in the table too
(``size_rule``), so a corrected reading of the vendor's page is an edit to the JSON file:

``counts``
    The fields of a request record whose sum is the size compared with a row's
    ``over_input_tokens``. A row applies to requests whose size is over that number, so a request
    of exactly the threshold pays the lower row.
``applies_to``
    ``"whole_request"``: every input token of a request pays the row its size selects.
    ``"excess"``: only the input above a threshold pays the higher row; the split is spread over
    the input classes in proportion to their sizes, because a request record does not say which
    tokens came last.
``output``
    ``"request_row"``: output tokens pay the row the request's size selects. ``"first_row"``:
    output always pays the first row.

``cost`` prices each request record on its own and sums, because a size rule applies per request.

``reservation_cost`` gives an upper bound for a request whose usage could not be read: a number of
tokens with no split into classes, each priced at the model's highest rate in any row.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from importlib import resources
from pathlib import Path
from typing import Any

from contexto.bench import records

FORMAT = 1
RATES = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")
CACHE_TTLS = ("5m", "1h")
INPUT_CLASSES = ("uncached", "cache_write", "cache_read")
APPLIES_TO = ("whole_request", "excess")
OUTPUT_RULES = ("request_row", "first_row")
SHIPPED = "the price table shipped with contexto"
_DATED = re.compile(r"-\d{8}$")


class PriceError(ValueError):
    """A price table that cannot be used, or a model or cache lifetime it has no price for."""


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _check_model(name: str, entry: Any, table: Mapping[str, Any], where: str) -> None:
    def bad(field: str, problem: str) -> PriceError:
        return PriceError(f"{where}: field 'models.{name}{field}': {problem}")

    if not isinstance(entry, dict):
        raise bad("", "expected an object")
    for field in ("source", "accessed"):
        if not isinstance(entry.get(field, table.get(field)), str):
            raise bad(f".{field}", "missing: every price needs its source URL and the date it was read")
    rows = entry.get("rows")
    if not isinstance(rows, list) or not rows:
        raise bad(".rows", "expected a list of one or more price rows")
    previous = None
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise bad(f".rows[{i}]", "expected an object")
        over = row.get("over_input_tokens")
        if not isinstance(over, int) or isinstance(over, bool) or over < 0:
            raise bad(f".rows[{i}].over_input_tokens", "expected a whole number, 0 or more")
        if (previous is None and over != 0) or (previous is not None and over <= previous):
            raise bad(f".rows[{i}].over_input_tokens", "the first row starts at 0 and each later row at a higher "
                                                       "number")
        previous = over
        for rate in RATES:
            if not _number(row.get(rate)):
                raise bad(f".rows[{i}].{rate}", "expected a price in USD per million tokens, 0 or more")
    if len(rows) == 1:
        return
    rule = entry.get("size_rule")
    if not isinstance(rule, dict):
        raise bad(".size_rule", "a model with more than one price row needs a size_rule")
    counts = rule.get("counts")
    if not isinstance(counts, list) or not counts or any(c not in INPUT_CLASSES for c in counts):
        raise bad(".size_rule.counts", f"expected a list of one or more of {', '.join(INPUT_CLASSES)}")
    if rule.get("applies_to") not in APPLIES_TO:
        raise bad(".size_rule.applies_to", f"expected one of {', '.join(APPLIES_TO)}")
    if rule.get("output") not in OUTPUT_RULES:
        raise bad(".size_rule.output", f"expected one of {', '.join(OUTPUT_RULES)}")


def check(table: Any, where: str = "the price table") -> dict[str, Any]:
    """Check a price table and return it. ``PriceError`` names ``where`` and the field at fault."""
    if not isinstance(table, dict):
        raise PriceError(f"{where}: not a JSON object")
    if table.get("format") != FORMAT:
        raise PriceError(f"{where}: field 'format': this version reads format {FORMAT}, got {table.get('format')!r}")
    models = table.get("models")
    if not isinstance(models, dict) or not models:
        raise PriceError(f"{where}: field 'models': expected an object with one or more models")
    for name, entry in models.items():
        _check_model(name, entry, table, where)
    return table


def load(path: Path | str | None = None) -> dict[str, Any]:
    """The price table: the shipped one, or the file at ``path`` in its place."""
    if path is None:
        where = SHIPPED
        try:
            text = resources.files("contexto.bench").joinpath("data", "prices.json").read_text(encoding="utf-8")
        except OSError as exc:
            raise PriceError(f"{where}: cannot read it from the installed package: {exc}") from exc
    else:
        where = str(path)
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise PriceError(f"{where}: cannot read the file: {exc}") from exc
    try:
        table = records.strict(text)
    except json.JSONDecodeError as exc:
        raise PriceError(f"{where}: not JSON ({exc})") from exc
    except ValueError as exc:  # a repeated key, NaN or Infinity, nesting too deep: see records.strict
        raise PriceError(f"{where}: {exc}") from exc
    return check(table, where)


def models(table: Mapping[str, Any]) -> list[str]:
    """The models the table prices, in the table's order."""
    return list(table["models"])


def resolve(model: str, table: Mapping[str, Any]) -> str:
    """The table's name for ``model``: the name itself, or the name a dated snapshot id extends
    (``claude-haiku-5-5-20261001`` is priced as ``claude-haiku-5-5``). ``PriceError`` lists the
    known models when there is none."""
    known = table["models"]
    if model in known:
        return model
    if isinstance(model, str) and _DATED.search(model) and model[:-9] in known:
        return model[:-9]
    raise PriceError(f"no price for model {model!r}: the price table has {', '.join(known)}. "
                     "Use --prices FILE to give a table that includes it.")


def _row_for(rows: list[dict[str, Any]], size: int) -> dict[str, Any]:
    chosen = rows[0]
    for row in rows[1:]:
        if size > row["over_input_tokens"]:
            chosen = row
    return chosen


def _input_shares(rows: list[dict[str, Any]], size: int, applies_to: str) -> list[tuple[dict[str, Any], float]]:
    """The price rows the input of a request of ``size`` pays, each with the share of the input
    that pays it."""
    if len(rows) == 1 or applies_to == "whole_request" or size <= 0:
        return [(_row_for(rows, size), 1.0)]
    shares = []
    for i, row in enumerate(rows):
        low = row["over_input_tokens"]
        high = rows[i + 1]["over_input_tokens"] if i + 1 < len(rows) else size
        part = min(size, high) - low
        if part > 0:
            shares.append((row, part / size))
    return shares


def request_cost(request: Mapping[str, Any], model: str, cache_ttl: str = "5m",
                 table: Mapping[str, Any] | None = None) -> float:
    """The cost in USD of one request record (``uncached``, ``cache_write``, ``cache_read`` and
    ``output`` tokens) at ``model``'s prices. ``cache_ttl`` says which cache-write price applies:
    ``"5m"`` or ``"1h"``."""
    table = load() if table is None else table
    if cache_ttl not in CACHE_TTLS:
        raise PriceError(f"no cache-write price for a cache lifetime of {cache_ttl!r}: the table has "
                         f"{' and '.join(CACHE_TTLS)}")
    entry = table["models"][resolve(model, table)]
    rows = entry["rows"]
    rule = entry.get("size_rule") or {}
    size = sum(request[name] for name in rule.get("counts", INPUT_CLASSES))
    write_rate = f"cache_write_{cache_ttl}"
    total = 0.0
    for row, share in _input_shares(rows, size, rule.get("applies_to", "whole_request")):
        total += share * (request["uncached"] * row["input"] + request["cache_write"] * row[write_rate]
                          + request["cache_read"] * row["cache_read"])
    output_row = rows[0] if rule.get("output") == "first_row" else _row_for(rows, size)
    total += request["output"] * output_row["output"]
    return total / 1_000_000


def cost(requests: Iterable[Mapping[str, Any]], model: str, cache_ttl: str = "5m",
         table: Mapping[str, Any] | None = None) -> float:
    """The cost in USD of the request records at ``model``'s prices: each record priced on its own
    (``request_cost``), then summed. An unknown model is a ``PriceError`` that lists the known ones."""
    table = load() if table is None else table
    resolve(model, table)  # so that an unknown model is an error even with no requests
    if cache_ttl not in CACHE_TTLS:
        raise PriceError(f"no cache-write price for a cache lifetime of {cache_ttl!r}: the table has "
                         f"{' and '.join(CACHE_TTLS)}")
    return math.fsum(request_cost(r, model, cache_ttl, table) for r in requests)


def reservation_cost(tokens: int, model: str, table: Mapping[str, Any] | None = None) -> float:
    """An upper bound in USD on a request that counted at most ``tokens`` tokens of any class: each
    token at ``model``'s highest price in any row and any class (in practice the output price of
    its last row). Used for a request whose usage could not be read, priced at the reservation the
    live run's spend guard kept for it, which says how many tokens it can have used but not of
    which class."""
    table = load() if table is None else table
    rows = table["models"][resolve(model, table)]["rows"]
    return tokens * max(row[rate] for row in rows for rate in RATES) / 1_000_000
