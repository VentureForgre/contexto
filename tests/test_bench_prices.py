"""``contexto.bench.prices``: the published price table and the cost of measured tokens (D66)."""

from __future__ import annotations

import copy
import json
from importlib import resources

import pytest

from contexto.bench import prices

SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
# The "Model pricing" table of the source page as read on 2026-11-12, USD per million tokens:
# input, 5-minute cache write, 1-hour cache write, cache read, output.
PUBLISHED = {
    "claude-haiku-5-5": [(0, 0.10, 0.125, 0.20, 0.01, 0.50), (100_000, 0.50, 0.625, 1.00, 0.05, 2.50)],
    "claude-sonnet-5-5": [(0, 2.00, 2.50, 4.00, 0.10, 10.00)],
    "claude-opus-5-5": [(0, 4.00, 5.00, 8.00, 0.20, 20.00)],
    "claude-fable-5-2": [(0, 8.00, 10.00, 16.00, 0.20, 40.00)],
}


def request(uncached=0, cache_write=0, cache_read=0, output=0) -> dict:
    return {"uncached": uncached, "cache_write": cache_write, "cache_read": cache_read, "output": output}


# The shipped table


def test_the_shipped_table_has_the_published_prices_with_source_and_date():
    table = prices.load()
    assert prices.models(table) == list(PUBLISHED)
    assert (table["currency"], table["unit"], table["source"], table["accessed"]) == (
        "USD", "per million tokens", SOURCE, "2026-11-12")
    for model, rows in PUBLISHED.items():
        entry = table["models"][model]
        assert (entry["source"], entry["accessed"]) == (SOURCE, "2026-11-12")
        assert [(r["over_input_tokens"], r["input"], r["cache_write_5m"], r["cache_write_1h"], r["cache_read"],
                 r["output"]) for r in entry["rows"]] == rows


def test_the_size_rule_of_haiku_is_data_and_its_note_says_what_is_a_reading():
    entry = prices.load()["models"]["claude-haiku-5-5"]
    assert entry["size_rule"] == {"counts": ["uncached", "cache_write", "cache_read"],
                                  "applies_to": "whole_request", "output": "request_row"}
    assert "is a reading of the page" in entry["note"] and "whole request" in entry["note"]
    for model in ("claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-2"):
        assert "size_rule" not in prices.load()["models"][model]


def test_the_table_is_package_data_read_through_importlib_resources():
    packaged = resources.files("contexto.bench").joinpath("data", "prices.json")
    assert packaged.is_file()
    assert json.loads(packaged.read_text(encoding="utf-8")) == prices.load()


# Cost of one request


def test_a_request_is_priced_by_class():
    # 1,000 uncached at $4, 2,000 written at $5, 30,000 read at $0.20 and 500 output at $20, per million.
    r = request(uncached=1_000, cache_write=2_000, cache_read=30_000, output=500)
    assert prices.request_cost(r, "claude-opus-5-5") == pytest.approx(
        (1_000 * 4 + 2_000 * 5 + 30_000 * 0.20 + 500 * 20) / 1e6)
    assert prices.request_cost(r, "claude-opus-5-5") == pytest.approx(0.03)


def test_the_cache_lifetime_selects_the_write_price():
    r = request(cache_write=1_000_000)
    assert prices.request_cost(r, "claude-fable-5-2", "5m") == pytest.approx(10.0)
    assert prices.request_cost(r, "claude-fable-5-2", "1h") == pytest.approx(16.0)
    assert prices.request_cost(r, "claude-haiku-5-5", "1h") == pytest.approx(1.0)  # over 100,000: the higher row
    with pytest.raises(prices.PriceError, match="cache lifetime of '24h'.*5m and 1h"):
        prices.request_cost(r, "claude-fable-5-2", "24h")


def test_haiku_pays_the_lower_row_up_to_100000_input_tokens_and_the_higher_row_above():
    at = request(uncached=100_000, output=1_000)
    over = request(uncached=100_001, output=1_000)
    assert prices.request_cost(at, "claude-haiku-5-5") == pytest.approx((100_000 * 0.10 + 1_000 * 0.50) / 1e6)
    assert prices.request_cost(over, "claude-haiku-5-5") == pytest.approx((100_001 * 0.50 + 1_000 * 2.50) / 1e6)


def test_the_haiku_size_counts_all_three_input_classes_and_not_output():
    # 1 + 50,000 + 50,000 = 100,001 input tokens: over, though no single class is.
    over = request(uncached=1, cache_write=50_000, cache_read=50_000, output=10)
    assert prices.request_cost(over, "claude-haiku-5-5") == pytest.approx(
        (1 * 0.50 + 50_000 * 0.625 + 50_000 * 0.05 + 10 * 2.50) / 1e6)
    # 60,000 input and 60,000 output: output does not count toward the size.
    under = request(uncached=60_000, output=60_000)
    assert prices.request_cost(under, "claude-haiku-5-5") == pytest.approx((60_000 * 0.10 + 60_000 * 0.50) / 1e6)


def test_the_other_models_have_no_step():
    big = request(uncached=900_000)
    for model, usd in (("claude-sonnet-5-5", 1.8), ("claude-opus-5-5", 3.6), ("claude-fable-5-2", 7.2)):
        assert prices.request_cost(big, model) == pytest.approx(usd)


# Cost of many requests


def test_cost_prices_each_request_on_its_own_and_sums():
    # Two requests of 60,000 tokens each are both under the threshold; their total of 120,000 is not.
    two = [request(cache_read=60_000, output=100), request(cache_read=60_000, output=100)]
    assert prices.cost(two, "claude-haiku-5-5") == pytest.approx(2 * (60_000 * 0.01 + 100 * 0.50) / 1e6)
    mixed = [request(cache_read=60_000), request(cache_read=120_000)]
    assert prices.cost(mixed, "claude-haiku-5-5") == pytest.approx((60_000 * 0.01 + 120_000 * 0.05) / 1e6)
    assert prices.cost([], "claude-haiku-5-5") == 0.0


def test_cost_ignores_fields_it_does_not_price():
    record = {**request(uncached=1_000_000), "arm": "full", "status": 200, "history_tokens": 5, "main": True}
    assert prices.cost([record], "claude-sonnet-5-5") == pytest.approx(2.0)


def test_an_unknown_model_is_an_error_that_lists_the_known_ones():
    with pytest.raises(prices.PriceError) as exc:
        prices.cost([request(uncached=1)], "claude-opus-4-1")
    text = str(exc.value)
    assert "no price for model 'claude-opus-4-1'" in text and "--prices FILE" in text
    assert "claude-haiku-5-5, claude-sonnet-5-5, claude-opus-5-5, claude-fable-5-2" in text
    with pytest.raises(prices.PriceError, match="no price for model 'x'"):
        prices.cost([], "x")  # an error even when there is nothing to price


def test_a_dated_snapshot_id_is_priced_as_its_model():
    table = prices.load()
    assert prices.resolve("claude-haiku-5-5-20261001", table) == "claude-haiku-5-5"
    assert prices.cost([request(uncached=1_000_000)], "claude-opus-5-5-20260801") == pytest.approx(4.0)
    for other in ("claude-haiku-5-5-fast", "claude-haiku-5-5-2026", "claude-haiku"):
        with pytest.raises(prices.PriceError):
            prices.resolve(other, table)


# A replacement table


def test_a_file_replaces_the_table(tmp_path):
    table = {"format": 1, "source": "https://example.test/prices", "accessed": "2027-01-02", "models": {
        "house-model": {"rows": [{"over_input_tokens": 0, "input": 1, "cache_write_5m": 2, "cache_write_1h": 3,
                                  "cache_read": 0.5, "output": 4}]}}}
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(table), encoding="utf-8")
    loaded = prices.load(path)
    assert prices.models(loaded) == ["house-model"]
    r = request(uncached=1_000_000, cache_write=1_000_000, cache_read=1_000_000, output=1_000_000)
    assert prices.cost([r], "house-model", "1h", loaded) == pytest.approx(1 + 3 + 0.5 + 4)
    with pytest.raises(prices.PriceError, match="the price table has house-model"):
        prices.cost([r], "claude-haiku-5-5", "5m", loaded)


def test_a_corrected_reading_of_the_size_rule_is_an_edit_to_the_table():
    r = request(uncached=40_000, cache_write=40_000, cache_read=120_000, output=1_000)  # 200,000 input
    table = copy.deepcopy(prices.load())
    rule = table["models"]["claude-haiku-5-5"]["size_rule"]
    whole = prices.request_cost(r, "claude-haiku-5-5", table=table)
    assert whole == pytest.approx((40_000 * 0.50 + 40_000 * 0.625 + 120_000 * 0.05 + 1_000 * 2.50) / 1e6)

    rule["output"] = "first_row"  # output stays at the lower row
    assert prices.request_cost(r, "claude-haiku-5-5", table=table) == pytest.approx(
        (40_000 * 0.50 + 40_000 * 0.625 + 120_000 * 0.05 + 1_000 * 0.50) / 1e6)

    rule["applies_to"] = "excess"  # half of the input is over the threshold, so half of each class pays 5 times
    lower = 40_000 * 0.10 + 40_000 * 0.125 + 120_000 * 0.01
    assert prices.request_cost(r, "claude-haiku-5-5", table=table) == pytest.approx(
        (0.5 * lower + 0.5 * 5 * lower + 1_000 * 0.50) / 1e6)

    rule["counts"] = ["uncached", "cache_write"]  # cache reads no longer count: 80,000 is under the threshold
    rule["applies_to"], rule["output"] = "whole_request", "request_row"
    assert prices.request_cost(r, "claude-haiku-5-5", table=table) == pytest.approx((lower + 1_000 * 0.50) / 1e6)


@pytest.mark.parametrize("edit, field", [
    (lambda t: t.update(format=2), "'format'"),
    (lambda t: t.update(models={}), "'models'"),
    (lambda t: t["models"]["claude-opus-5-5"]["rows"][0].pop("cache_read"),
     "'models.claude-opus-5-5.rows[0].cache_read'"),
    (lambda t: t["models"]["claude-opus-5-5"]["rows"][0].update(output="20"),
     "'models.claude-opus-5-5.rows[0].output'"),
    (lambda t: t["models"]["claude-opus-5-5"].update(rows=[]), "'models.claude-opus-5-5.rows'"),
    (lambda t: t["models"]["claude-haiku-5-5"].pop("size_rule"), "'models.claude-haiku-5-5.size_rule'"),
    (lambda t: t["models"]["claude-haiku-5-5"]["size_rule"].update(applies_to="marginal"),
     "'models.claude-haiku-5-5.size_rule.applies_to'"),
    (lambda t: t["models"]["claude-haiku-5-5"]["size_rule"].update(counts=["output"]),
     "'models.claude-haiku-5-5.size_rule.counts'"),
    (lambda t: t["models"]["claude-haiku-5-5"]["rows"][1].update(over_input_tokens=0),
     "'models.claude-haiku-5-5.rows[1].over_input_tokens'"),
    (lambda t: (t.pop("source"), t["models"]["claude-opus-5-5"].pop("source")), "'models.claude-opus-5-5.source'"),
])
def test_a_bad_table_is_refused_with_the_file_and_the_field(tmp_path, edit, field):
    table = copy.deepcopy(prices.load())
    edit(table)
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(table), encoding="utf-8")
    with pytest.raises(prices.PriceError) as exc:
        prices.load(path)
    assert str(exc.value).startswith(f"{path}: field {field}: ")


def test_a_table_that_cannot_be_read_is_refused(tmp_path):
    with pytest.raises(prices.PriceError, match="cannot read the file"):
        prices.load(tmp_path / "absent.json")
    path = tmp_path / "prices.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(prices.PriceError, match="not JSON"):
        prices.load(path)
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(prices.PriceError, match="not a JSON object"):
        prices.load(path)


@pytest.mark.parametrize("text, words", [
    ('"extra": NaN}', "field 'extra': not a finite number"),
    ('"extra": {"deeper": [Infinity]}}', r"field 'extra.deeper\[0\]': not a finite number"),
    ('"format": 1}', "the key 'format' appears twice"),
    ('"extra": ' + "[" * 200_000, "nested too deeply"),
])
def test_a_price_file_is_read_strictly(tmp_path, text, words):
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(prices.load())[:-1] + ", " + text, encoding="utf-8")
    with pytest.raises(prices.PriceError, match=words) as exc:
        prices.load(path)
    assert str(exc.value).startswith(str(path)) and len(str(exc.value)) < 400
