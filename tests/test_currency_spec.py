"""CurrencySpec: a currency as plain data, with purely syntactic rules (design section 9.4)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from openreserve.core.currencies import FICTIONAL_PREFIX, CurrencySpec, CurrencySpecError
from openreserve.core.exact import ExactValueError
from openreserve.core.types import Currency

VECTORS = json.loads((Path(__file__).parent / "vectors" / "currency_registry_v1.json").read_text(encoding="utf-8"))


def test_vector_counts():
    assert len(VECTORS["codes_valid"]) == 7
    assert len(VECTORS["codes_invalid"]) == 11


@pytest.mark.parametrize("code", VECTORS["codes_valid"])
def test_valid_codes(code):
    spec = CurrencySpec(code, 2)
    assert spec.code == code
    assert spec.fictional is code.startswith("FICTIONAL_")


@pytest.mark.parametrize("code", VECTORS["codes_invalid"] + [None, 5, b"JPY", "FICTIONAL_", "fictional_W", "W W"])
def test_invalid_codes(code):
    with pytest.raises(CurrencySpecError):
        CurrencySpec(code, 2)


@pytest.mark.parametrize("minor_units", [-1, 31, True, False, "2", 2.0, None])
def test_invalid_minor_units(minor_units):
    with pytest.raises(CurrencySpecError):
        CurrencySpec("JPY", minor_units)


def test_minor_units_range():
    assert CurrencySpec("JPY", 0).minor_units == 0
    assert CurrencySpec("X", 30).minor_units == 30
    assert issubclass(CurrencySpecError, ExactValueError) and issubclass(CurrencySpecError, ValueError)


def test_fictional_entries_are_visible_by_their_code():
    w = CurrencySpec("FICTIONAL_W", 2)
    assert w.fictional is True and FICTIONAL_PREFIX == "FICTIONAL_"
    assert w.to_document() == {"code": "FICTIONAL_W", "fictional": True, "minor_units": "2"}
    assert CurrencySpec("JPY", 0).to_document() == {"code": "JPY", "fictional": False, "minor_units": "0"}
    # a code that merely contains the word is not fictional
    assert CurrencySpec("NOT_FICTIONAL_W", 2).fictional is False


def test_document_round_trip_and_flag_check():
    for document in VECTORS["default_registry"]["document"]["currencies"] + VECTORS["default_plus_fictional_w"]["document"]["currencies"]:
        spec = CurrencySpec.from_document(document)
        assert spec.to_document() == document
    for bad in (
        {"code": "JPY", "fictional": True, "minor_units": "0"},        # the flag without the prefix
        {"code": "FICTIONAL_W", "fictional": False, "minor_units": "2"},  # the prefix without the flag
        {"code": "JPY", "fictional": "false", "minor_units": "0"},
        {"code": "JPY", "fictional": 0, "minor_units": "0"},
        {"code": "JPY", "fictional": False, "minor_units": 0},
        {"code": "JPY", "fictional": False, "minor_units": "00"},
        {"code": "JPY", "fictional": False},
        {"code": "JPY", "fictional": False, "minor_units": "0", "name": "yen"},
        {"code": "FICTIONAL_", "fictional": True, "minor_units": "0"},
        None,
        [],
    ):
        with pytest.raises(CurrencySpecError):
            CurrencySpec.from_document(bad)


def test_every_ledger_currency_has_a_spec_and_none_is_fictional():
    specs = [CurrencySpec.of(currency) for currency in Currency]
    assert sorted((s.code, s.minor_units) for s in specs) == [
        ("EUR", 2), ("GBP", 2), ("JPY", 0), ("JPYC", 18), ("PROGMAT_JPY", 0), ("USD", 2), ("USDC", 6), ("USDT", 6),
    ]
    assert not any(s.fictional for s in specs)
    assert sorted((s.to_document() for s in specs), key=lambda d: d["code"]) == VECTORS["default_registry"]["document"]["currencies"]


def test_spec_is_immutable_and_comparable():
    a, b = CurrencySpec("JPY", 0), CurrencySpec("JPY", 0)
    assert a == b and hash(a) == hash(b) and a != CurrencySpec("JPY", 2)
    with pytest.raises(Exception):
        a.code = "USD"  # type: ignore[misc]
