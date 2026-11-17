"""Exact encodings: amounts, canonical JSON, instants (design sections 4.1 to 4.3)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from openreserve.core.exact import (
    AMOUNT_MAX,
    ExactValueError,
    NaiveDatetimeError,
    amount_from_text,
    amount_to_text,
    amount_to_u128,
    canonical_json,
    instant_from_stored_text,
    instant_from_text,
    instant_to_text,
    require_aware,
    sha256_hex,
)
from openreserve.core.types import Currency, Money

VECTORS = json.loads((Path(__file__).parent / "vectors" / "exact_v1.json").read_text(encoding="utf-8"))


# ---------- amounts ----------


def test_amount_max_is_ten_to_the_38_minus_one():
    assert AMOUNT_MAX == 10**38 - 1
    assert str(AMOUNT_MAX) == VECTORS["amount_max"]
    assert 2 * AMOUNT_MAX < 2**128  # one unchecked u128 addition cannot wrap


def test_vector_counts():
    assert len(VECTORS["amount_text_valid"]) == 10
    assert len(VECTORS["amount_text_invalid"]) == 22
    assert len(VECTORS["u128"]) == 7
    assert len(VECTORS["instants"]) == 6
    assert len(VECTORS["instant_text_invalid"]) == 12
    assert len(VECTORS["canonical_json"]) == 6


@pytest.mark.parametrize("text", VECTORS["amount_text_valid"])
def test_valid_amount_text_round_trips(text):
    value = amount_from_text(text)
    assert type(value) is int
    assert amount_to_text(value) == text


@pytest.mark.parametrize("text", VECTORS["amount_text_invalid"])
def test_invalid_amount_text_is_refused(text):
    with pytest.raises(ExactValueError):
        amount_from_text(text)
    with pytest.raises(ExactValueError):
        amount_from_text(text, signed=False)


@pytest.mark.parametrize("text", ["1２３", "1٢", "-1２", "1０", "12３4", "0０"])
def test_non_ascii_digits_after_an_ascii_digit_are_refused(text):
    assert text.isdigit() or text[1:].isdigit()  # Python calls these digits; the format does not
    with pytest.raises(ExactValueError):
        amount_from_text(text)
    with pytest.raises(ExactValueError):
        amount_from_text(text, signed=False)


def test_exact_values_of_some_amount_texts():
    assert amount_from_text("9223372036854775808") == 2**63
    assert amount_from_text("-9223372036854775809") == -(2**63) - 1
    assert amount_from_text("1000000000000000000000000000000") == 10**30
    assert amount_from_text("99999999999999999999999999999999999999") == 10**38 - 1
    assert amount_from_text("-99999999999999999999999999999999999999") == -(10**38 - 1)
    assert amount_from_text("0") == 0


def test_unsigned_amount_text_refuses_a_minus_sign():
    assert amount_from_text("5", signed=False) == 5
    assert amount_from_text("0", signed=False) == 0
    for text in ("-1", "-0", "-99999999999999999999999999999999999999"):
        with pytest.raises(ExactValueError):
            amount_from_text(text, signed=False)


@pytest.mark.parametrize("value", [1, b"1", None, 1.0, ["1"], Decimal("1"), True])
def test_amount_from_text_refuses_non_strings(value):
    with pytest.raises(ExactValueError):
        amount_from_text(value)


@pytest.mark.parametrize("value", [True, False, 1.0, "1", None, 10**38, -(10**38), Decimal(1), Fraction(1)])
def test_encoder_refuses_non_int_bool_and_out_of_range(value):
    with pytest.raises(ExactValueError):
        amount_to_text(value)
    with pytest.raises(ExactValueError):
        amount_to_u128(value)


def test_encoder_boundaries():
    assert amount_to_text(10**38 - 1) == "9" * 38
    assert amount_to_text(-(10**38 - 1)) == "-" + "9" * 38
    assert amount_to_text(0) == "0"
    assert amount_to_text(-0) == "0"


@pytest.mark.parametrize("row", VECTORS["u128"], ids=lambda r: r["value"])
def test_u128_encoding(row):
    encoded = amount_to_u128(int(row["value"]))
    assert encoded.hex() == row["hex"]
    assert len(encoded) == 16
    assert int.from_bytes(encoded, "big") == int(row["value"])


def test_u128_refuses_negative_and_too_large():
    for value in (-1, 10**38, 2**128 - 1, 2**128):
        with pytest.raises(ExactValueError):
            amount_to_u128(value)


def test_exact_value_error_is_a_value_error():
    assert issubclass(ExactValueError, ValueError)
    assert issubclass(NaiveDatetimeError, ExactValueError)


def test_money_refuses_bool_and_float():
    with pytest.raises(TypeError):
        Money(cents=True, currency=Currency.JPY)
    with pytest.raises(TypeError):
        Money(cents=False, currency=Currency.JPY)
    with pytest.raises(TypeError):
        Money(cents=1.0, currency=Currency.JPY)
    assert Money(cents=1, currency=Currency.JPY).cents == 1


# ---------- canonical JSON ----------


@pytest.mark.parametrize("row", VECTORS["canonical_json"], ids=lambda r: r["sha256"][:8])
def test_canonical_json_bytes_and_hash(row):
    data = canonical_json(row["object"])
    assert data.hex() == row["utf8_hex"]
    assert sha256_hex(data) == row["sha256"]
    # parsing the canonical bytes gives the same document, and canonicalising again is stable
    assert json.loads(data.decode("utf-8")) == row["object"]
    assert canonical_json(json.loads(data.decode("utf-8"))) == data


def test_canonical_json_worked_example():
    data = canonical_json({"b": "2", "a": "1"})
    assert data == b'{"a":"1","b":"2"}'
    assert sha256_hex(data) == "21f76dfbfe6dfe21f762080ef484112cf2952974cef30741fd1931e1c6d92112"


@pytest.mark.parametrize(
    "document",
    [
        {"a": 1},
        {"a": 1.5},
        {"a": float("nan")},
        {"A": "x"},
        {"a-b": "x"},
        {"": "x"},
        {"1a": "x"},
        {"é": "x"},
        {1: "x"},
        {None: "x"},
        {"a": "\ud800"},
        {"a": b"x"},
        {"a": {"x"}},
        {"a": ("x",)},
        {"a": Fraction(1, 2)},
        {"a": Decimal("1")},
        {"a": datetime(2026, 1, 1, tzinfo=timezone.utc)},
        {"a": [{"b": [1]}]},
        5,
        1.0,
        ("a",),
    ],
)
def test_canonical_json_refuses(document):
    with pytest.raises(ExactValueError):
        canonical_json(document)


def test_canonical_json_accepts_bare_values_of_the_profile():
    assert canonical_json("x") == b'"x"'
    assert canonical_json(None) == b"null"
    assert canonical_json(True) == b"true"
    assert canonical_json([]) == b"[]"


def test_canonical_json_refuses_very_deep_nesting_instead_of_overflowing_the_stack():
    deep: list = []
    for _ in range(5000):
        deep = [deep]
    with pytest.raises(ExactValueError):
        canonical_json(deep)
    cyclic: dict = {}
    cyclic["a"] = cyclic
    with pytest.raises(ExactValueError):
        canonical_json(cyclic)


def test_canonical_json_key_order_is_by_code_point():
    assert canonical_json({"b": "1", "a_b": "2", "a": "3", "a1": "4"}) == b'{"a":"3","a1":"4","a_b":"2","b":"1"}'


def test_sha256_hex_of_empty_input():
    assert sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    with pytest.raises(ExactValueError):
        sha256_hex("text")


# ---------- instants ----------


@pytest.mark.parametrize("row", VECTORS["instants"], ids=lambda r: r["label"])
def test_instant_text(row):
    value = datetime.fromisoformat(row["isoformat_input"])
    text = instant_to_text(value)
    assert text == row["text"]
    assert len(text) == 27
    parsed = instant_from_text(text)
    assert parsed == value
    assert parsed.utcoffset() == timedelta(0)
    assert instant_to_text(parsed) == text
    # the form older files hold is still read, as the same instant
    assert instant_from_stored_text(row["isoformat_input"]) == value
    assert instant_from_stored_text(text) == value


@pytest.mark.parametrize("text", VECTORS["instant_text_invalid"])
def test_invalid_instant_text_is_refused(text):
    with pytest.raises(ExactValueError):
        instant_from_text(text)


def test_instant_examples_from_the_design():
    plus_0545 = datetime(2026, 6, 30, 23, 59, 59, 999999, tzinfo=timezone(timedelta(hours=5, minutes=45)))
    assert instant_to_text(plus_0545) == "2026-06-30T18:14:59.999999Z"
    assert instant_to_text(datetime(99, 1, 1, tzinfo=timezone.utc)) == "0099-01-01T00:00:00.000000Z"
    plus9 = datetime(2026, 1, 1, 9, 0, tzinfo=timezone(timedelta(hours=9)))
    assert instant_to_text(plus9) == "2026-01-01T00:00:00.000000Z"


def test_naive_datetime_is_refused():
    naive = datetime(2026, 1, 1)
    with pytest.raises(NaiveDatetimeError):
        require_aware(naive)
    with pytest.raises(NaiveDatetimeError):
        instant_to_text(naive)
    aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert require_aware(aware) is aware


class _NoOffset(tzinfo):
    """A tzinfo that does not know its offset: ``utcoffset()`` returns ``None``."""

    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return "unknown"


def test_a_tzinfo_without_an_offset_counts_as_naive():
    value = datetime(2026, 1, 1, tzinfo=_NoOffset())
    assert value.tzinfo is not None and value.utcoffset() is None
    with pytest.raises(NaiveDatetimeError):
        require_aware(value)
    with pytest.raises(NaiveDatetimeError):
        instant_to_text(value)


@pytest.mark.parametrize("value", [None, "2026-01-01T00:00:00.000000Z", 0, 1.5])
def test_require_aware_refuses_non_datetimes(value):
    with pytest.raises(ExactValueError):
        require_aware(value)
    with pytest.raises(ExactValueError):
        instant_to_text(value)


@pytest.mark.parametrize("value", [None, 5, b"2026-01-01T00:00:00.000000Z"])
def test_instant_parsers_refuse_non_strings(value):
    with pytest.raises(ExactValueError):
        instant_from_text(value)
    with pytest.raises(ExactValueError):
        instant_from_stored_text(value)


def test_stored_text_without_offset_is_a_naive_datetime_error():
    for text in ("2026-01-01T00:00:00", "2026-01-01T00:00:00.123456"):
        with pytest.raises(NaiveDatetimeError):
            instant_from_stored_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "2026-01-01",
        "2026-01-01 00:00:00+00:00",
        "20260101T000000Z",
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00+0900",
        "2026-01-01T00:00:00+09",
        "2026-01-01T00:00:00+24:00",
        "2026-01-01T00:00:00+09:60",
        "2026-02-30T00:00:00+00:00",
        "2026-01-01T25:00:00+00:00",
        "2026-01-01T00:00:00.1+00:00",
        "2026-01-01T00:00:00+00:00\n",
        " 2026-01-01T00:00:00+00:00",
        "２０２６-01-01T00:00:00+00:00",
    ],
)
def test_stored_text_in_other_forms_is_refused(text):
    with pytest.raises(ExactValueError):
        instant_from_stored_text(text)


def test_stored_legacy_forms_are_read_as_instants():
    utc = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert instant_from_stored_text("2026-01-01T00:00:00+00:00") == utc
    assert instant_from_stored_text("2026-01-01T09:00:00+09:00") == utc
    assert instant_from_stored_text("2025-12-31T19:00:00-05:00") == utc
    assert instant_from_stored_text("2026-01-01T00:00:00.250000+00:00") == utc + timedelta(microseconds=250000)
    assert instant_from_stored_text("2026-01-01T00:00:30+00:00:30") == utc
    # whatever isoformat() writes for an aware datetime is read back as the same instant
    for value in (
        datetime(2026, 6, 30, 23, 59, 59, 999999, tzinfo=timezone(timedelta(hours=5, minutes=45))),
        datetime(1999, 12, 31, 23, 59, 59, tzinfo=timezone(-timedelta(hours=11, minutes=30))),
        datetime(2026, 1, 1, 0, 0, 30, tzinfo=timezone(timedelta(seconds=30))),
    ):
        assert instant_from_stored_text(value.isoformat()) == value
        assert instant_from_stored_text(value.isoformat()).utcoffset() == timedelta(0)


def test_instants_outside_the_range_in_utc_are_refused():
    with pytest.raises(ExactValueError):
        instant_to_text(datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=9))))
    with pytest.raises(ExactValueError):
        instant_from_stored_text("0001-01-01T00:00:00+09:00")
    with pytest.raises(ExactValueError):
        instant_from_text("0000-01-01T00:00:00.000000Z")
