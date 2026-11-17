"""CurrencyRegistry: an immutable, hash-identified list of currency specs (design section 9.4).

Expected values come from tests/vectors/currency_registry_v1.json. The code rules of a single
spec are tested in tests/test_currency_spec.py.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from openreserve.core.currencies import (
    DEFAULT_REGISTRY,
    FORMAT_REGISTRY,
    CurrencyRegistry,
    CurrencyRegistryError,
    CurrencySpec,
    CurrencySpecError,
)
from openreserve.core.exact import ExactValueError, canonical_json, sha256_hex
from openreserve.core.types import Currency

VECTORS = json.loads((Path(__file__).parent / "vectors" / "currency_registry_v1.json").read_text(encoding="utf-8"))
DEFAULT_HASH = "a6c8eb8df071e723925179a222c3341d5a0b687020b3c79746d20fc580bf823f"
WITH_W_HASH = "2ab8443ffc04d3f6e651c6e7c5a6cd22f8a5b2d6293c482344219ba353dc87c2"
W = CurrencySpec("FICTIONAL_W", 2)  # the scenario's fictional entry; it exists only in tests


def test_default_registry_document_bytes_and_hash():
    expected = VECTORS["default_registry"]
    assert DEFAULT_REGISTRY.to_document() == expected["document"]
    assert canonical_json(DEFAULT_REGISTRY.to_document()).decode("utf-8") == expected["canonical_utf8"]
    assert DEFAULT_REGISTRY.registry_hash == expected["registry_hash"] == DEFAULT_HASH
    assert sha256_hex(canonical_json(DEFAULT_REGISTRY.to_document())) == DEFAULT_HASH
    assert DEFAULT_REGISTRY.to_document()["format"] == FORMAT_REGISTRY == "openreserve.currency-registry/1"


def test_default_registry_equals_the_ledger_enumeration_entry_by_entry():
    assert len(DEFAULT_REGISTRY) == len(Currency) == 8
    for currency in Currency:
        spec = DEFAULT_REGISTRY.get(currency.code)
        assert spec == CurrencySpec(currency.code, currency.minor_units) == CurrencySpec.of(currency)
        assert spec.fictional is False
    assert set(DEFAULT_REGISTRY.codes) == {c.code for c in Currency}
    assert not any(spec.fictional for spec in DEFAULT_REGISTRY)
    assert "FICTIONAL_W" not in DEFAULT_REGISTRY


def test_extension_by_the_fictional_entry():
    extended = DEFAULT_REGISTRY.extended(W)
    expected = VECTORS["default_plus_fictional_w"]
    assert extended.to_document() == expected["document"]
    assert extended.registry_hash == expected["registry_hash"] == WITH_W_HASH
    assert extended is not DEFAULT_REGISTRY
    assert DEFAULT_REGISTRY.registry_hash == DEFAULT_HASH  # the old registry is unchanged
    assert "FICTIONAL_W" not in DEFAULT_REGISTRY and "FICTIONAL_W" in extended
    assert extended.get("FICTIONAL_W").fictional is True
    assert extended.codes == tuple(sorted(extended.codes))
    assert len(extended) == 9


def test_redefinition_is_refused_also_when_identical():
    with pytest.raises(CurrencyRegistryError, match="already"):
        DEFAULT_REGISTRY.extended(CurrencySpec("JPY", 0))
    with pytest.raises(CurrencyRegistryError, match="already"):
        DEFAULT_REGISTRY.extended(CurrencySpec("JPY", 2))
    with pytest.raises(CurrencyRegistryError, match="already"):
        DEFAULT_REGISTRY.extended(W).extended(W)
    with pytest.raises(CurrencyRegistryError, match="twice"):
        DEFAULT_REGISTRY.extended(W, W)
    with pytest.raises(CurrencyRegistryError, match="twice"):
        CurrencyRegistry([W, CurrencySpec("FICTIONAL_W", 3)])
    assert DEFAULT_REGISTRY.registry_hash == DEFAULT_HASH


def test_fictional_flag_and_prefix_rules():
    with pytest.raises(CurrencySpecError):
        CurrencySpec("FICTIONAL_", 2)  # the bare prefix
    document = copy.deepcopy(VECTORS["default_registry"]["document"])
    document["currencies"][0]["fictional"] = True  # EUR flagged fictional without the prefix
    with pytest.raises(CurrencySpecError):
        CurrencyRegistry.from_document(document)
    document = copy.deepcopy(VECTORS["default_plus_fictional_w"]["document"])
    document["currencies"][1]["fictional"] = False  # FICTIONAL_W not flagged
    with pytest.raises(CurrencySpecError):
        CurrencyRegistry.from_document(document)


def test_from_document_round_trip_and_pinned_hash():
    for key in ("default_registry", "default_plus_fictional_w"):
        expected = VECTORS[key]
        registry = CurrencyRegistry.from_document(expected["document"], expected_hash=expected["registry_hash"])
        assert registry.to_document() == expected["document"]
        assert registry.registry_hash == expected["registry_hash"]
    assert CurrencyRegistry.from_document(DEFAULT_REGISTRY.to_document()) == DEFAULT_REGISTRY
    with pytest.raises(CurrencyRegistryError, match="expected"):
        CurrencyRegistry.from_document(VECTORS["default_registry"]["document"], expected_hash=WITH_W_HASH)


def _with(change):
    document = copy.deepcopy(VECTORS["default_registry"]["document"])
    change(document)
    return document


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        "registry",
        {"format": FORMAT_REGISTRY},
        {"currencies": []},
        _with(lambda d: d.__setitem__("format", "openreserve.currency-registry/2")),
        _with(lambda d: d.__setitem__("extra", "x")),
        _with(lambda d: d.__setitem__("currencies", {})),
        _with(lambda d: d["currencies"].reverse()),  # not in ascending order
        _with(lambda d: d["currencies"].append(copy.deepcopy(d["currencies"][-1]))),  # a code twice
        _with(lambda d: d["currencies"][0].__setitem__("minor_units", 2)),  # a number, not a string
        _with(lambda d: d["currencies"][0].__setitem__("minor_units", "02")),
        _with(lambda d: d["currencies"][0].__setitem__("minor_units", "31")),
        _with(lambda d: d["currencies"][0].__setitem__("code", "eur")),
        _with(lambda d: d["currencies"][0].pop("fictional")),
        _with(lambda d: d["currencies"][0].__setitem__("name", "x")),
    ],
)
def test_malformed_documents_are_refused(document):
    with pytest.raises(ExactValueError):  # CurrencyRegistryError and CurrencySpecError both are
        CurrencyRegistry.from_document(document)


def test_registry_is_immutable():
    with pytest.raises(AttributeError):
        DEFAULT_REGISTRY._specs = ()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        DEFAULT_REGISTRY.extra = 1  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        del DEFAULT_REGISTRY._hash
    document = DEFAULT_REGISTRY.to_document()
    document["currencies"].clear()  # a copy: the registry does not change
    assert DEFAULT_REGISTRY.registry_hash == DEFAULT_HASH and len(DEFAULT_REGISTRY) == 8


def test_lookup_and_construction():
    assert DEFAULT_REGISTRY.get("JPYC").minor_units == 18
    with pytest.raises(KeyError):
        DEFAULT_REGISTRY.get("FICTIONAL_W")
    with pytest.raises(KeyError):
        DEFAULT_REGISTRY.get(None)  # type: ignore[arg-type]
    assert CurrencyRegistry([CurrencySpec("USD", 2), CurrencySpec("EUR", 2)]).codes == ("EUR", "USD")
    assert CurrencyRegistry().to_document() == {"format": FORMAT_REGISTRY, "currencies": []}
    with pytest.raises(CurrencyRegistryError):
        CurrencyRegistry([("JPY", 0)])  # type: ignore[list-item]
    with pytest.raises(CurrencyRegistryError):
        DEFAULT_REGISTRY.extended("FICTIONAL_W")  # type: ignore[arg-type]
    assert hash(DEFAULT_REGISTRY) == hash(CurrencyRegistry(list(DEFAULT_REGISTRY)))
    assert "a6c8eb8df071" in repr(DEFAULT_REGISTRY)
