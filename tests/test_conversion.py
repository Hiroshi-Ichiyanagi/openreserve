"""Declared conversion: rate text, declaration, exact arithmetic, rounding (design 8.1, 8.2).

Expected values come from tests/vectors/conversion_v1.json, which the standalone script next
to it recomputes without importing the package.
"""

from __future__ import annotations

import copy
import json
import random
import sys
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import openreserve.conversion as conversion_mod
from openreserve.conversion import (
    FORMAT_DECLARATION,
    HALF_AWAY_FROM_ZERO,
    HALF_EVEN,
    SOURCE_PER_TARGET,
    TARGET_PER_SOURCE,
    ConversionDeclaration,
    ConversionError,
    ConvertedAmount,
    DeclarationError,
    convert,
    fraction_from_document,
    fraction_to_document,
    load_declaration,
    parse_rate_text,
    rate_significant_figures,
    round_fraction,
)
from openreserve.core.currencies import CurrencySpec
from openreserve.core.exact import AMOUNT_MAX, canonical_json, sha256_hex

VECTORS = json.loads((Path(__file__).parent / "vectors" / "conversion_v1.json").read_text(encoding="utf-8"))
CASES = {c["name"]: c for c in VECTORS["cases"]}
MODES = (HALF_AWAY_FROM_ZERO, HALF_EVEN)


def fr(document) -> Fraction:
    return Fraction(int(document["num"]), int(document["den"]))


def declaration(rate="1.95583", quotation=SOURCE_PER_TARGET, source=("DEM", 2), target=("EUR", 2),
                mode=HALF_AWAY_FROM_ZERO, figures=None, cutoff="1998-12-31T23:00:00.000000Z") -> ConversionDeclaration:
    return ConversionDeclaration(
        declaration_id="test",
        source=CurrencySpec(*source),
        target=CurrencySpec(*target),
        rate_text=rate,
        quotation=quotation,
        rounding_mode=mode,
        cutoff_at=cutoff,
        rate_significant_figures=figures,
    )


def test_vector_counts():
    assert len(VECTORS["rate_text_valid"]) == 13
    assert len(VECTORS["rate_text_invalid"]) == 20
    assert len(VECTORS["declarations_refused"]) == 7
    assert len(VECTORS["cases"]) == 9
    assert sum(len(c["conversions"]) for c in VECTORS["cases"]) == 78
    assert len(VECTORS["batch_totals"]) == 4


# ---------------------------------------------------------------------------
# 1. Rate text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("row", VECTORS["rate_text_valid"], ids=lambda r: r["text"])
def test_valid_rate_text(row):
    value = parse_rate_text(row["text"])
    assert type(value) is Fraction
    assert value == Fraction(int(row["num"]), int(row["den"]))
    assert (str(value.numerator), str(value.denominator)) == (row["num"], row["den"])
    assert rate_significant_figures(row["text"]) == int(row["significant_figures"])


def test_rate_examples_from_the_design():
    assert parse_rate_text("0.1") == Fraction(1, 10)
    assert parse_rate_text("1.95583") == Fraction(195583, 100000)
    assert parse_rate_text("340.750") == Fraction(1363, 4)
    assert [rate_significant_figures(t) for t in ("1.95583", "340.750", "0.787564", "1936.27")] == [6, 6, 6, 6]
    assert rate_significant_figures("0.0064") == 2
    assert rate_significant_figures("100") == 3
    assert rate_significant_figures("0.000001") == 1
    assert rate_significant_figures("10.0") == 3
    # exact where a binary float is not
    assert parse_rate_text("0.1") + parse_rate_text("0.2") == parse_rate_text("0.3")


@pytest.mark.parametrize("text", VECTORS["rate_text_invalid"], ids=repr)
def test_invalid_rate_text(text):
    with pytest.raises(DeclarationError):
        parse_rate_text(text)
    with pytest.raises(DeclarationError):
        rate_significant_figures(text)
    with pytest.raises(DeclarationError):
        declaration(rate=text)


@pytest.mark.parametrize(
    "value",
    [1.95583, 2, Decimal("1.95583"), Fraction(195583, 100000), None, True, b"1.95583", ["1.95583"]],
    ids=repr,
)
def test_rate_values_that_are_not_text_are_refused(value):
    assert len(VECTORS["rate_values_refused"]) == 5
    with pytest.raises(DeclarationError):
        parse_rate_text(value)
    with pytest.raises(DeclarationError):
        declaration(rate=value)


def test_rate_text_length_limits():
    assert parse_rate_text("9" * 30) == 10**30 - 1
    assert parse_rate_text("0." + "0" * 29 + "1") == Fraction(1, 10**30)
    for text in ("9" * 31, "0." + "0" * 30 + "1", "1." + "0" * 31):
        with pytest.raises(DeclarationError):
            parse_rate_text(text)


# ---------------------------------------------------------------------------
# 2. Refused declarations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("row", VECTORS["declarations_refused"], ids=lambda r: r["why"])
def test_refused_declarations(row):
    with pytest.raises(DeclarationError):
        load_declaration(row["declaration"])
    with pytest.raises(DeclarationError):
        ConversionDeclaration.from_document(row["declaration"])


def _valid_document() -> dict:
    return copy.deepcopy(CASES["jpy0-to-fictional-w2"]["declaration"])


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.pop("format"),
        lambda d: d.__setitem__("format", "openreserve.conversion.declaration/2"),
        lambda d: d.__setitem__("extra", "x"),
        lambda d: d.pop("reference_document_sha256"),
        lambda d: d.pop("rate_significant_figures"),
        lambda d: d.__setitem__("declaration_id", ""),
        lambda d: d.__setitem__("declaration_id", "x" * 256),
        lambda d: d.__setitem__("declaration_id", "a\nb"),
        lambda d: d.__setitem__("declaration_id", None),
        lambda d: d["source"].__setitem__("code", "jpy"),
        lambda d: d["source"].__setitem__("code", "FICTIONAL_"),
        lambda d: d["source"].__setitem__("minor_units", 0),
        lambda d: d["source"].__setitem__("minor_units", "31"),
        lambda d: d["source"].__setitem__("minor_units", "00"),
        lambda d: d["source"].__setitem__("name", "yen"),
        lambda d: d["target"].pop("minor_units"),
        lambda d: d.__setitem__("target", "FICTIONAL_W"),
        lambda d: d["target"].__setitem__("code", "JPY"),
        lambda d: d["rate"].__setitem__("text", "0"),
        lambda d: d["rate"].__setitem__("text", "-147.25"),
        lambda d: d["rate"].__setitem__("text", "147,25"),
        lambda d: d["rate"].__setitem__("text", 147.25),
        lambda d: d["rate"].__setitem__("text", 147),
        lambda d: d["rate"].__setitem__("quotation", "source_per_target"),
        lambda d: d["rate"].__setitem__("quotation", None),
        lambda d: d["rate"].pop("quotation"),
        lambda d: d["rate"].__setitem__("inverse", "0.00679"),
        lambda d: d.__setitem__("rate", "147.25"),
        lambda d: d.__setitem__("rate_significant_figures", "6"),
        lambda d: d.__setitem__("rate_significant_figures", "0"),
        lambda d: d.__setitem__("rate_significant_figures", 5),
        lambda d: d.__setitem__("rate_significant_figures", "05"),
        lambda d: d.__setitem__("rate_significant_figures", True),
        lambda d: d["rounding"].__setitem__("mode", "HALF_UP"),
        lambda d: d["rounding"].__setitem__("mode", "half_even"),
        lambda d: d["rounding"].__setitem__("mode", None),
        lambda d: d["rounding"].pop("mode"),
        lambda d: d["rounding"].__setitem__("quantum_minor_units", "5"),
        lambda d: d["rounding"].__setitem__("quantum_minor_units", 1),
        lambda d: d["rounding"].__setitem__("level", "HOLDER"),
        lambda d: d["rounding"].__setitem__("extra", "x"),
        lambda d: d.__setitem__("rounding", "HALF_EVEN"),
        lambda d: d.__setitem__("cutoff_at", "2099-12-31T15:00:00Z"),
        lambda d: d.__setitem__("cutoff_at", "2099-12-31T15:00:00.000000+00:00"),
        lambda d: d.__setitem__("cutoff_at", None),
        lambda d: d.__setitem__("reference_document_sha256", "AB" * 32),
        lambda d: d.__setitem__("reference_document_sha256", "ab" * 31),
        lambda d: d.__setitem__("reference_document_sha256", ""),
        lambda d: d.__setitem__("reference_document_sha256", False),
    ],
)
def test_other_rule_violations_are_refused(change):
    document = _valid_document()
    assert load_declaration(document).to_document() == document
    change(document)
    with pytest.raises(DeclarationError):
        load_declaration(document)


@pytest.mark.parametrize("document", [None, 5, "declaration", [], {}])
def test_documents_that_are_not_declarations(document):
    with pytest.raises(DeclarationError):
        load_declaration(document)


def test_declaration_keyword_form_checks_the_same_rules():
    assert declaration(figures=6).rate_significant_figures == 6
    with pytest.raises(DeclarationError):
        declaration(rate="1.9558", figures=6)
    with pytest.raises(DeclarationError):
        declaration(quotation="EUR_PER_DEM")
    with pytest.raises(DeclarationError):
        declaration(mode="HALF_UP")
    with pytest.raises(DeclarationError):
        declaration(mode=None)
    with pytest.raises(TypeError):  # the mode has no default
        ConversionDeclaration(declaration_id="x", source=CurrencySpec("DEM", 2), target=CurrencySpec("EUR", 2),
                              rate_text="1.95583", quotation=SOURCE_PER_TARGET, cutoff_at="1998-12-31T23:00:00.000000Z")
    with pytest.raises(DeclarationError):
        declaration(source=("EUR", 2))
    with pytest.raises(DeclarationError):
        declaration(source=("EUR", 0))  # the same code with other minor units is still the same currency
    with pytest.raises(DeclarationError):
        declaration(cutoff="1998-12-31")
    with pytest.raises(DeclarationError):
        declaration(figures=True)
    with pytest.raises(DeclarationError):
        ConversionDeclaration(declaration_id="x", source="DEM", target=CurrencySpec("EUR", 2), rate_text="2",
                              quotation=SOURCE_PER_TARGET, rounding_mode=HALF_EVEN, cutoff_at="1998-12-31T23:00:00.000000Z")
    assert issubclass(DeclarationError, ConversionError) and issubclass(ConversionError, ValueError)


def test_reference_document_hash_is_part_of_the_declaration_hash():
    document = _valid_document()
    base = load_declaration(document)
    document["reference_document_sha256"] = "ab" * 32
    with_reference = load_declaration(document)
    assert with_reference.reference_document_sha256 == "ab" * 32
    assert with_reference.declaration_hash != base.declaration_hash


# ---------------------------------------------------------------------------
# 3. The nine declarations and their 78 conversions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["name"])
def test_declaration_bytes_hash_factor_and_conversions(case):
    d = load_declaration(case["declaration"])
    assert d.to_document() == case["declaration"]
    assert d.canonical_bytes().hex() == case["declaration_canonical_utf8_hex"]
    assert d.declaration_hash == case["declaration_hash"] == sha256_hex(canonical_json(case["declaration"]))
    assert d.declaration_hash_bytes.hex() == case["declaration_hash"]
    assert d.factor == fr(case["factor"])
    assert fraction_to_document(d.factor) == case["factor"]
    assert d.cutoff_at == case["declaration"]["cutoff_at"] and d.cutoff.utcoffset().total_seconds() == 0

    for row in case["conversions"]:
        x = int(row["x"])
        result = convert(d, x)
        assert isinstance(result, ConvertedAmount)
        assert result.source_amount == x
        assert result.exact == fr(row["z"]) and fraction_to_document(result.exact) == row["z"]
        assert result.converted_amount == int(row["y"]) and type(result.converted_amount) is int
        assert result.residue == fr(row["e"]) and fraction_to_document(result.residue) == row["e"]
        assert abs(result.residue) <= Fraction(1, 2)
        assert result.converted_amount - result.exact == result.residue
        # symmetric in sign
        mirrored = convert(d, -x)
        assert (mirrored.converted_amount, mirrored.exact, mirrored.residue) == (
            -result.converted_amount, -result.exact, -result.residue,
        )
        assert d.convert(x) == result
        assert convert(case["declaration"], x) == result  # a document is accepted as well


def _ys(name: str, xs) -> list[int]:
    d = load_declaration(CASES[name]["declaration"])
    return [convert(d, x).converted_amount for x in xs]


def test_worked_examples_of_the_design():
    dem = load_declaration(CASES["precedent-direction-six-figures"]["declaration"])
    assert dem.factor == Fraction(100000, 195583)
    r = convert(dem, 10000)
    assert (r.exact, r.converted_amount, r.residue) == (Fraction(1000000000, 195583), 5113, Fraction(15879, 195583))
    r = convert(dem, -10000)
    assert (r.exact, r.converted_amount, r.residue) == (Fraction(-1000000000, 195583), -5113, Fraction(-15879, 195583))
    r = convert(dem, 195583)
    assert (r.exact, r.converted_amount, r.residue) == (100000, 100000, 0)
    r = convert(dem, 100000000000)
    assert (r.converted_amount, r.residue) == (51129188120, Fraction(73960, 195583))

    ties = [1, 3, 5, 7, -1, -3, -5, -7]
    assert _ys("ties-rate-2-half_away_from_zero", ties) == [1, 2, 3, 4, -1, -2, -3, -4]
    assert _ys("ties-rate-2-half_even", ties) == [0, 2, 2, 4, 0, -2, -2, -4]
    assert _ys("ties-rate-0.5-target-per-source-half_away_from_zero", ties) == [1, 2, 3, 4, -1, -2, -3, -4]
    assert _ys("ties-rate-0.5-target-per-source-half_even", ties) == [0, 2, 2, 4, 0, -2, -2, -4]

    jpy = load_declaration(CASES["jpy0-to-fictional-w2"]["declaration"])
    assert jpy.factor == Fraction(400, 589)
    assert [(convert(jpy, x).converted_amount, convert(jpy, x).residue) for x in (300000, 120000, 55000, -60)] == [
        (203735, Fraction(-85, 589)), (81494, Fraction(-34, 589)), (37351, Fraction(-261, 589)), (-41, Fraction(-149, 589)),
    ]

    jpyc = load_declaration(CASES["jpyc18-to-fictional-w2"]["declaration"])
    assert jpyc.factor == Fraction(17, 25000000000000000000)
    r = convert(jpyc, 10000000000000000000)
    assert (r.exact, r.converted_amount, r.residue) == (Fraction(34, 5), 7, Fraction(1, 5))
    assert convert(jpyc, 735294117647058823).converted_amount == 0  # just below one half
    assert convert(jpyc, 735294117647058824).converted_amount == 1  # just above one half
    assert convert(jpyc, 735294117647058823).exact < Fraction(1, 2) < convert(jpyc, 735294117647058824).exact

    w0 = load_declaration(CASES["jpy0-to-fictional-w0-zero-results"]["declaration"])
    assert w0.factor == Fraction(4, 589)
    assert (convert(w0, 73).converted_amount, convert(w0, 73).residue) == (0, Fraction(-292, 589))
    assert (convert(w0, 74).converted_amount, convert(w0, 74).residue) == (1, Fraction(293, 589))

    w18 = load_declaration(CASES["usd2-to-fictional-w18"]["declaration"])
    assert w18.factor == 8000000000000000
    r = convert(w18, 12345)
    assert (r.exact, r.converted_amount, r.residue) == (98760000000000000000, 98760000000000000000, 0)


def test_the_same_factor_declared_both_ways_is_two_different_declarations():
    a = declaration(rate="2", quotation=SOURCE_PER_TARGET)
    b = declaration(rate="0.5", quotation=TARGET_PER_SOURCE)
    assert a.factor == b.factor == Fraction(1, 2)
    assert a.declaration_hash != b.declaration_hash
    assert a.to_document()["rate"] == {"text": "2", "quotation": "SOURCE_PER_TARGET"}
    assert b.to_document()["rate"] == {"text": "0.5", "quotation": "TARGET_PER_SOURCE"}
    # trailing zeros are kept verbatim and change the hash, not the value
    c = declaration(rate="2.0", quotation=SOURCE_PER_TARGET)
    assert c.factor == a.factor and c.rate_text == "2.0" and c.declaration_hash != a.declaration_hash


# ---------------------------------------------------------------------------
# 4. Rounding against a second implementation
# ---------------------------------------------------------------------------


def _round_by_sign_and_magnitude(n: int, d: int, mode: str) -> int:
    """Second implementation: integers only, working on the magnitude."""
    assert d > 0
    sign = -1 if n < 0 else 1
    magnitude = -n if n < 0 else n
    whole = magnitude // d
    remainder = magnitude - whole * d
    if 2 * remainder > d:
        whole += 1
    elif 2 * remainder == d:
        if mode == HALF_AWAY_FROM_ZERO or whole % 2 == 1:
            whole += 1
    return sign * whole


def test_round_agrees_with_a_second_implementation_on_random_fractions_and_ties():
    rng = random.Random(8_2)
    checked = ties = 0
    for _ in range(60_000):
        if rng.random() < 0.4:  # a constructed tie: an odd numerator over 2, scaled
            scale = rng.randrange(1, 10**6)
            n, d = (2 * rng.randrange(-10**12, 10**12) + 1) * scale, 2 * scale
        elif rng.random() < 0.5:  # next to a tie
            d = 2 * rng.randrange(1, 10**9)
            n = (2 * rng.randrange(-10**9, 10**9) + 1) * (d // 2) + rng.choice((-1, 1))
        else:
            d = rng.randrange(1, 10**18)
            n = rng.randrange(-10**30, 10**30)
        z = Fraction(n, d)
        ties += (2 * z).denominator == 1 and z.denominator == 2
        for mode in MODES:
            got = round_fraction(z, mode)
            assert type(got) is int
            assert got == _round_by_sign_and_magnitude(n, d, mode)
            assert round_fraction(-z, mode) == -got            # symmetric in sign
            assert abs(got - z) <= Fraction(1, 2)                # nearest
            if z.denominator != 2:
                assert abs(got - z) < Fraction(1, 2)
            checked += 1
    assert checked == 120_000
    assert ties >= 20_000


def test_round_exact_table():
    half = Fraction(1, 2)
    table = {
        # z: (HALF_AWAY_FROM_ZERO, HALF_EVEN)
        Fraction(0): (0, 0),
        half: (1, 0), -half: (-1, 0),
        3 * half: (2, 2), -3 * half: (-2, -2),
        5 * half: (3, 2), -5 * half: (-3, -2),
        7 * half: (4, 4), -7 * half: (-4, -4),
        Fraction(1, 3): (0, 0), Fraction(2, 3): (1, 1), Fraction(-1, 3): (0, 0), Fraction(-2, 3): (-1, -1),
        Fraction(49, 100): (0, 0), Fraction(51, 100): (1, 1), Fraction(-49, 100): (0, 0), Fraction(-51, 100): (-1, -1),
        Fraction(10**40 + 1, 2): ((10**40) // 2 + 1, (10**40) // 2),
        Fraction(-(10**40) - 1, 2): (-((10**40) // 2) - 1, -((10**40) // 2)),
        Fraction(7): (7, 7), Fraction(-7): (-7, -7),
    }
    for z, (away, even) in table.items():
        assert round_fraction(z, HALF_AWAY_FROM_ZERO) == away, z
        assert round_fraction(z, HALF_EVEN) == even, z
    assert round_fraction(5, HALF_EVEN) == 5 and round_fraction(-5, HALF_AWAY_FROM_ZERO) == -5


@pytest.mark.parametrize("mode", ["HALF_UP", "ROUND_HALF_EVEN", "half_even", "", None, 0])
def test_round_refuses_unknown_modes(mode):
    with pytest.raises(ConversionError):
        round_fraction(Fraction(1, 2), mode)


@pytest.mark.parametrize("value", [0.5, Decimal("0.5"), "1/2", None, True])
def test_round_refuses_values_that_are_not_exact(value):
    with pytest.raises(ConversionError):
        round_fraction(value, HALF_EVEN)


# ---------------------------------------------------------------------------
# 5. A rate must not be inverted
# ---------------------------------------------------------------------------


def test_inverse_rate_case():
    illustration = VECTORS["inverse_rate_illustration"]
    x = int(illustration["x"])
    assert x == 100_000_000_000
    declared = declaration(rate="1.95583", quotation=SOURCE_PER_TARGET, figures=6)
    exact_division = convert(declared, x).converted_amount
    assert exact_division == 51_129_188_120 == int(illustration["y_exact_division"])

    # what a decimal reciprocal rounded to six significant figures would give
    inverse = Fraction(1) / parse_rate_text("1.95583")
    rounded_inverse_text = illustration["inverse_rounded_to_six_significant_figures"]
    assert rounded_inverse_text == "0.511292"
    assert abs(parse_rate_text(rounded_inverse_text) - inverse) < Fraction(1, 2 * 10**6)
    through_inverse = convert(declaration(rate=rounded_inverse_text, quotation=TARGET_PER_SOURCE, figures=6), x).converted_amount
    assert through_inverse == 51_129_200_000 == int(illustration["y_via_rounded_inverse"])
    assert through_inverse - exact_division == 11_880 == int(illustration["difference_minor_units"])


def test_the_module_offers_no_way_to_invert_a_rate():
    public = {name.lower() for name in conversion_mod.__all__}
    for word in ("invert", "inverse", "reciprocal"):
        assert not any(word in name for name in public)
        assert not any(word in name.lower() for name in dir(ConversionDeclaration))


# ---------------------------------------------------------------------------
# 6. Totals of a batch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("batch", VECTORS["batch_totals"], ids=lambda b: b["declaration_case"] + "/" + b["totals"]["count"])
def test_batch_totals(batch):
    d = load_declaration(CASES[batch["declaration_case"]]["declaration"])
    xs = [int(m) for m in batch["magnitudes"]]
    results = [convert(d, x) for x in xs]
    t = batch["totals"]
    exact_total = d.factor * sum(xs)
    rounded_exact_total = round_fraction(exact_total, d.rounding_mode)
    converted_total = sum(r.converted_amount for r in results)
    assert str(len(xs)) == t["count"]
    assert str(sum(xs)) == t["source_total"]
    assert str(converted_total) == t["converted_total"]
    assert fraction_to_document(exact_total) == t["exact_total"]
    assert str(rounded_exact_total) == t["rounded_exact_total"]
    assert str(converted_total - rounded_exact_total) == t["reconciliation_difference"]
    assert fraction_to_document(converted_total - exact_total) == t["sum_of_residues"]
    assert sum(r.residue for r in results) == fr(t["sum_of_residues"])


def test_design_example_of_batch_totals():
    d = load_declaration(CASES["jpy0-to-fictional-w2"]["declaration"])
    xs = [300000, 120000, 55000]
    ys = [convert(d, x).converted_amount for x in xs]
    assert sum(ys) == 322580
    assert d.factor * sum(xs) == Fraction(10000000, 31)
    assert round_fraction(d.factor * sum(xs), d.rounding_mode) == 322581
    assert sum(ys) - 322581 == -1
    assert sum(ys) - d.factor * sum(xs) == Fraction(-20, 31)


def test_random_batches_reconcile_exactly():
    rng = random.Random(8_4)
    for case in VECTORS["cases"]:
        d = load_declaration(case["declaration"])
        for _ in range(40):
            xs = [rng.randrange(-10**15, 10**15) for _ in range(rng.randrange(1, 60))]
            results = [convert(d, x) for x in xs]
            sum_y = sum(r.converted_amount for r in results)
            sum_z = sum(r.exact for r in results)
            sum_e = sum(r.residue for r in results)
            assert sum_y - sum_z == sum_e
            assert sum_z == d.factor * sum(xs)
            assert abs(sum_e) <= Fraction(len(xs), 2)
            assert all(abs(r.residue) <= Fraction(1, 2) for r in results)


# ---------------------------------------------------------------------------
# Bounds and input types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [1.0, "1", None, True, Decimal(1), Fraction(1)])
def test_convert_refuses_amounts_that_are_not_int(value):
    with pytest.raises(ConversionError):
        convert(declaration(), value)


def test_convert_bounds():
    d = declaration(rate="2", quotation=SOURCE_PER_TARGET)
    assert convert(d, AMOUNT_MAX).converted_amount == (AMOUNT_MAX + 1) // 2
    assert convert(d, -AMOUNT_MAX).converted_amount == -(AMOUNT_MAX + 1) // 2
    with pytest.raises(ConversionError):
        convert(d, AMOUNT_MAX + 1)
    with pytest.raises(ConversionError):
        convert(d, -AMOUNT_MAX - 1)
    doubling = declaration(rate="2", quotation=TARGET_PER_SOURCE)
    assert convert(doubling, AMOUNT_MAX // 2).converted_amount == AMOUNT_MAX - 1
    with pytest.raises(ConversionError):
        convert(doubling, AMOUNT_MAX // 2 + 1)  # the converted amount would exceed the bound
    w18 = load_declaration(CASES["usd2-to-fictional-w18"]["declaration"])
    with pytest.raises(ConversionError):
        convert(w18, 10**23)
    with pytest.raises(DeclarationError):
        convert({"format": FORMAT_DECLARATION}, 1)


def test_zero_converts_to_zero_with_no_residue():
    for case in VECTORS["cases"]:
        r = convert(case["declaration"], 0)
        assert (r.exact, r.converted_amount, r.residue) == (0, 0, 0)


# ---------------------------------------------------------------------------
# Fractions in documents
# ---------------------------------------------------------------------------


def test_fraction_documents():
    assert fraction_to_document(Fraction(-20, 31)) == {"num": "-20", "den": "31"}
    assert fraction_to_document(Fraction(4, 8)) == {"num": "1", "den": "2"}
    assert fraction_to_document(Fraction(0)) == {"num": "0", "den": "1"}
    assert fraction_to_document(7) == {"num": "7", "den": "1"}
    assert fraction_from_document({"num": "-20", "den": "31"}) == Fraction(-20, 31)
    big = Fraction(10**120 + 1, 10**80 + 7)
    assert fraction_from_document(fraction_to_document(big)) == big
    for bad in (
        {"num": "2", "den": "4"},          # not in lowest terms
        {"num": "1", "den": "0"},
        {"num": "1", "den": "-2"},
        {"num": "-0", "den": "1"},
        {"num": "01", "den": "2"},
        {"num": "1", "den": "02"},
        {"num": 1, "den": "2"},
        {"num": "1", "den": 2},
        {"num": "1.0", "den": "2"},
        {"num": "1"},
        {"num": "1", "den": "2", "x": "3"},
        {"num": "0", "den": "5"},          # zero is written 0/1
        {"num": "1" + "0" * 200, "den": "1"},
        None,
        [1, 2],
        "1/2",
    ):
        with pytest.raises(ConversionError):
            fraction_from_document(bad)
    for bad in (0.5, "1/2", None, True):
        with pytest.raises(ConversionError):
            fraction_to_document(bad)
