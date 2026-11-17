"""Coverage as an exact value (design section 9.2) and the coverage artifacts (9.3).

Expected values come from tests/vectors/coverage_v1.json and
tests/vectors/snapshot_expected_v1.json.
"""

from __future__ import annotations

import json
import math
import random
import sys
import warnings
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))  # helpers shared with the other test modules

import pytest

import openreserve.core.ledger as ledger_mod
import openreserve.core.storage as storage_mod
import openreserve.transparency.coverage as coverage_mod
from openreserve.core.exact import NaiveDatetimeError, canonical_json, sha256_hex
from openreserve.core.ledger import Ledger, TransactionBuilder
from openreserve.core.storage import SQLiteLedgerStorage
from openreserve.core.types import Account, Currency, Money, OwnerType
from openreserve.transparency.audit_log import GENESIS_HASH
from openreserve.transparency.coverage import (
    FORMAT_DECLARED_RESERVE_COVERAGE,
    FORMAT_RESERVE_COVERAGE,
    DeclaredReserveCoverage,
    DeclaredReserveCoverageGenerator,
    ReserveCoverage,
    ReserveCoverageGenerator,
    coverage_figures,
    verify_reserve_coverage_summary,
)
from openreserve.transparency.proofs import ProofOfReservesGenerator, ProofOfSolvencyGenerator
from openreserve.transparency.snapshot import verify_snapshot_inclusion

VECTORS = Path(__file__).parent / "vectors"
COVERAGE = json.loads((VECTORS / "coverage_v1.json").read_text(encoding="utf-8"))
EXPECTED = json.loads((VECTORS / "snapshot_expected_v1.json").read_text(encoding="utf-8"))
KEY = bytes(range(32))
INSTITUTION = "example-institution"
T = datetime(2026, 1, 1, tzinfo=timezone.utc)
ABC_COVERAGE_SUMMARY_HASH = "a15cf45f2e16472155dd814c05ac535cf353bf293368fffe0d4ca3112d0eb678"


class Fixture:
    def __init__(self) -> None:
        self.storage = SQLiteLedgerStorage(":memory:")
        self.ledger = Ledger(self.storage)

    def open(self, account_id: str, owner_type: OwnerType = OwnerType.USER, currency: Currency = Currency.JPY,
             created_at: datetime = T) -> str:
        self.storage.save_account(
            Account(account_id=account_id, owner_type=owner_type, currency=currency, label=account_id, created_at=created_at)
        )
        return account_id

    def move(self, src: str, dst: str, amount: int, *, at: datetime = T, currency: Currency = Currency.JPY) -> None:
        b = TransactionBuilder("MOVE", "test", initiated_at=at)
        b.transfer(src, dst, Money(cents=amount, currency=currency))
        self.ledger.post(b.build())
        self.ledger.settle(b.transaction_id, settled_at=at)

    def coverage(self, currency: Currency = Currency.JPY, at: datetime = T) -> ReserveCoverage:
        return ReserveCoverageGenerator(self.ledger, institution_id=INSTITUTION, salt_key=KEY).generate(currency, at)

    def legacy(self, currency: Currency = Currency.JPY, at: datetime = T):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", PendingDeprecationWarning)
            return ProofOfReservesGenerator(self.ledger).generate(currency, snapshot_at=at)[0]


def abc_with_reserve() -> Fixture:
    """The abc fixture of the snapshot tests plus a RESERVE account ``reserve`` funded 1000000."""
    f = Fixture()
    f.open("platform", OwnerType.PLATFORM)
    for account_id, amount in (("alice", 300_000), ("bob", 120_000), ("carol", 55_000)):
        f.open(account_id)
        f.move("platform", account_id, amount)
    f.open("reserve", OwnerType.RESERVE)
    f.move("platform", "reserve", 1_000_000)
    return f


def overdraft_with_reserve() -> Fixture:
    f = Fixture()
    f.open("platform", OwnerType.PLATFORM)
    f.open("reserve", OwnerType.RESERVE)
    f.open("a")
    f.open("b")
    f.move("platform", "reserve", 50)
    f.move("platform", "a", 100)
    f.move("b", "platform", 60)  # b is overdrawn: -60
    return f


def pair(reserves: int, liabilities: int) -> Fixture:
    f = Fixture()
    f.open("platform", OwnerType.PLATFORM)
    f.open("reserve", OwnerType.RESERVE)
    f.open("holder")
    if reserves:
        f.move("platform", "reserve", reserves) if reserves > 0 else f.move("reserve", "platform", -reserves)
    if liabilities:
        f.move("platform", "holder", liabilities)
    return f


def _walk(value):
    yield value
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk(v)


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", COVERAGE["cases"], ids=lambda c: f"{c['reserves']}/{c['liabilities']}")
def test_coverage_vectors(case):
    figures = coverage_figures(int(case["reserves"]), int(case["liabilities"]))
    assert figures.covered is case["covered"]
    expected_bp = None if case["basis_points_floor"] is None else int(case["basis_points_floor"])
    assert figures.basis_points_floor == expected_bp
    expected_ratio = None if case["ratio"] is None else Fraction(int(case["ratio"]["num"]), int(case["ratio"]["den"]))
    assert figures.ratio == expected_ratio
    assert figures.to_document() == {"covered": case["covered"], "basis_points_floor": case["basis_points_floor"]}


def test_eleven_vector_cases_include_the_named_ones():
    named = {(c["reserves"], c["liabilities"]): c["basis_points_floor"] for c in COVERAGE["cases"]}
    assert len(COVERAGE["cases"]) == 11
    assert named[("1", "3")] == "3333"
    assert named[("57", "100")] == "5700"
    assert named[("5", "2")] == "25000"
    assert named[("1000000000000000000000000000001", "1000000000000000000000000000000")] == "10000"
    assert named[("-1", "3")] == "-3334"
    assert named[("5", "0")] is None and named[("0", "0")] is None


def test_basis_points_are_the_exact_floor_for_every_small_pair():
    for reserves in range(-20, 201):
        for liabilities in range(1, 201):
            figures = coverage_figures(reserves, liabilities)
            assert figures.basis_points_floor == math.floor(Fraction(10000 * reserves, liabilities))
            assert figures.covered is (reserves >= liabilities)


def test_zero_liabilities_negative_reserves_are_not_covered():
    figures = coverage_figures(-1, 0)
    assert (figures.covered, figures.basis_points_floor, figures.ratio) == (False, None, None)


@pytest.mark.parametrize("reserves, liabilities", [(1.0, 1), (1, 1.0), (True, 1), (1, False), ("1", 1)])
def test_coverage_figures_refuse_non_integers(reserves, liabilities):
    with pytest.raises(TypeError):
        coverage_figures(reserves, liabilities)


def test_coverage_figures_refuse_negative_liabilities():
    with pytest.raises(ValueError):
        coverage_figures(1, -1)


# ---------------------------------------------------------------------------
# The legacy artifact gets the same arithmetic
# ---------------------------------------------------------------------------


def test_legacy_coverage_ratio_is_exact_where_the_float_was_wrong():
    proof = pair(57, 100).legacy()
    assert proof.coverage_ratio == Fraction(57, 100)
    assert type(proof.coverage_ratio) is Fraction
    summary = proof.to_public_summary()
    assert summary["coverage_ratio_basis_points"] == 5700  # int(0.57 * 10000) was 5699
    assert summary["is_solvent"] is False
    assert int(57 / 100 * 10000) == 5699  # the float arithmetic this replaces


def test_legacy_coverage_without_liabilities_has_no_sentinel():
    proof = pair(5, 0).legacy()
    assert proof.coverage_ratio is None
    summary = proof.to_public_summary()
    assert summary["coverage_ratio_basis_points"] is None
    assert summary["is_solvent"] is True
    assert 9999900 not in summary.values()


# Legacy summaries carry JSON numbers; values from 2**63 on are covered by the arithmetic test.
LEDGER_SIZED = [c for c in COVERAGE["cases"] if max(abs(int(c["reserves"])), int(c["liabilities"])) < 2**63]


@pytest.mark.parametrize("case", LEDGER_SIZED, ids=lambda c: f"{c['reserves']}/{c['liabilities']}")
def test_legacy_summary_matches_the_vectors_for_ledger_sized_values(case):
    reserves, liabilities = int(case["reserves"]), int(case["liabilities"])
    assert len(LEDGER_SIZED) == 9
    summary = pair(reserves, liabilities).legacy().to_public_summary()
    expected = None if case["basis_points_floor"] is None else int(case["basis_points_floor"])
    assert summary["coverage_ratio_basis_points"] == expected
    assert summary["is_solvent"] is case["covered"]


def test_legacy_summary_has_no_float():
    for f in (pair(57, 100), pair(5, 0), overdraft_with_reserve(), abc_with_reserve()):
        summary = f.legacy().to_public_summary()
        assert not any(isinstance(v, float) for v in _walk(summary))


# ---------------------------------------------------------------------------
# The reserve coverage artifact
# ---------------------------------------------------------------------------


def test_abc_reserve_coverage_matches_the_pinned_summary():
    coverage = abc_with_reserve().coverage()
    summary = coverage.to_public_summary()
    assert summary == EXPECTED["abc_reserve_coverage_summary"]["summary"]
    assert coverage.summary_hash() == ABC_COVERAGE_SUMMARY_HASH == EXPECTED["abc_reserve_coverage_summary"]["summary_hash"]
    assert sha256_hex(canonical_json(summary)) == ABC_COVERAGE_SUMMARY_HASH
    assert summary["snapshot"] == EXPECTED["abc_snapshot_summary"]["summary"]
    assert (coverage.reserves_total, coverage.liabilities_total) == (1_000_000, 475_000)
    assert coverage.reserve_account_ids == ("reserve",)
    assert coverage.is_covered is True
    assert coverage.coverage_ratio == Fraction(1_000_000, 475_000)
    assert coverage.coverage_basis_points_floor == 21052
    assert coverage.currency == "JPY"


def test_overdraft_ledger_is_not_covered_by_50():
    coverage = overdraft_with_reserve().coverage()
    expected = EXPECTED["overdraft_snapshot_summary"]
    summary = coverage.to_public_summary()
    assert summary["snapshot"] == expected["summary"]
    assert summary["reserves_self_booked"] == {"total": expected["expected_coverage"]["reserves"], "account_ids": ["reserve"]}
    assert summary["coverage"] == {
        "covered": expected["expected_coverage"]["covered"],
        "basis_points_floor": expected["expected_coverage"]["basis_points_floor"],
    }
    assert (coverage.is_covered, coverage.coverage_basis_points_floor) == (False, 5000)
    # The debtor's 60 is a receivable: it is neither netted nor counted as a reserve.
    assert coverage.snapshot.receivables.total == 60
    assert coverage.reserves_total == 50


def test_no_liabilities_gives_null_and_covered():
    f = Fixture()
    f.open("platform", OwnerType.PLATFORM)
    f.open("reserve", OwnerType.RESERVE)
    f.move("platform", "reserve", 5)
    coverage = f.coverage()
    assert coverage.to_public_summary()["coverage"] == {"covered": True, "basis_points_floor": None}
    assert coverage.coverage_ratio is None and coverage.coverage_basis_points_floor is None


def test_summary_has_no_number_and_survives_a_float_parser():
    for f in (abc_with_reserve(), overdraft_with_reserve(), pair(10**30 + 1, 10**30)):
        coverage = f.coverage(currency=Currency.JPY)
        summary = coverage.to_public_summary()
        for value in _walk(summary):
            assert not isinstance(value, (int, float)) or isinstance(value, bool), value
        text = canonical_json(summary).decode("utf-8")
        reparsed = json.loads(text, parse_int=float, parse_float=float)
        assert reparsed == summary
        assert sha256_hex(canonical_json(reparsed)) == coverage.summary_hash()


def test_values_beyond_2_63_stay_exact():
    coverage = pair(10**30 + 1, 10**30).coverage()
    summary = coverage.to_public_summary()
    assert summary["reserves_self_booked"]["total"] == str(10**30 + 1)
    assert summary["snapshot"]["liabilities"]["total"] == str(10**30)
    assert summary["coverage"] == {"covered": True, "basis_points_floor": "10000"}


def test_new_summaries_never_say_solvent_or_solvency():
    f = abc_with_reserve()
    declared = DeclaredReserveCoverageGenerator(f.ledger, institution_id=INSTITUTION, salt_key=KEY).generate(
        (Currency.JPY,), GENESIS_HASH, T
    )
    for summary in (f.coverage().to_public_summary(), declared.to_public_summary()):
        for value in _walk(summary):
            if isinstance(value, str):
                assert "solven" not in value.lower(), value


def test_which_reserve_accounts_count():
    f = abc_with_reserve()
    f.open("reserve-empty", OwnerType.RESERVE)  # balance zero: adds nothing, not listed
    f.open("reserve-usd", OwnerType.RESERVE, Currency.USD)
    f.open("platform-usd", OwnerType.PLATFORM, Currency.USD)
    f.move("platform-usd", "reserve-usd", 999, currency=Currency.USD)  # another currency
    f.open("reserve-pending", OwnerType.RESERVE)
    b = TransactionBuilder("MOVE", "test", initiated_at=T)
    b.transfer("platform", "reserve-pending", Money(cents=7, currency=Currency.JPY))
    f.ledger.post(b.build())  # pending: not settled, not counted
    f.open("fee", OwnerType.FEE)
    f.move("platform", "fee", 11)  # not a reserve account
    coverage = f.coverage()
    assert coverage.summary_hash() == ABC_COVERAGE_SUMMARY_HASH
    # A reserve account that is overdrawn lowers the self-booked figure (signed sum).
    f.open("reserve-2", OwnerType.RESERVE)
    f.move("reserve-2", "platform", 25_000)
    coverage = f.coverage()
    assert coverage.reserves_total == 975_000
    assert coverage.reserve_account_ids == ("reserve", "reserve-2")


def test_reserve_account_ids_are_sorted_whatever_the_storage_order():
    f = Fixture()
    f.open("platform", OwnerType.PLATFORM)
    for account_id in ("reserve-z", "reserve-m", "reserve-a"):  # opened in reverse order
        f.open(account_id, OwnerType.RESERVE)
        f.move("platform", account_id, 10)
    assert [a.account_id for a in f.ledger.list_accounts(owner_type=OwnerType.RESERVE)] == ["reserve-z", "reserve-m", "reserve-a"]
    coverage = f.coverage()
    assert coverage.reserve_account_ids == ("reserve-a", "reserve-m", "reserve-z")
    assert coverage.to_public_summary()["reserves_self_booked"] == {
        "total": "30", "account_ids": ["reserve-a", "reserve-m", "reserve-z"],
    }


def test_reserve_coverage_of_the_past_does_not_change_when_things_happen_later():
    f = abc_with_reserve()
    before = f.coverage().summary_hash()
    later = T + timedelta(days=30)
    f.open("reserve-late", OwnerType.RESERVE, created_at=later)
    f.move("platform", "reserve-late", 5_000, at=later)
    f.move("reserve", "platform", 1, at=later)
    f.open("late-holder", created_at=later)
    f.move("platform", "late-holder", 5, at=later)
    assert f.coverage().summary_hash() == before == ABC_COVERAGE_SUMMARY_HASH
    assert f.coverage(at=later).reserves_total == 1_000_000 - 1 + 5_000


def test_holder_proofs_verify_against_the_snapshot_inside_the_coverage_summary():
    coverage = overdraft_with_reserve().coverage()
    summary = coverage.to_public_summary()
    for account_id, kind in (("a", "liabilities"), ("b", "receivables")):
        proof = coverage.snapshot.proof_for(account_id)
        assert verify_snapshot_inclusion(proof, summary["snapshot"], expected_slot_id=account_id, expected_kind=kind).ok


def test_generator_refuses_bad_input():
    f = abc_with_reserve()
    with pytest.raises(ValueError):
        ReserveCoverageGenerator(f.ledger, institution_id=INSTITUTION, salt_key=b"short")
    with pytest.raises(ValueError):
        ReserveCoverageGenerator(f.ledger, institution_id="", salt_key=KEY)
    generator = ReserveCoverageGenerator(f.ledger, institution_id=INSTITUTION, salt_key=KEY)
    with pytest.raises(NaiveDatetimeError):
        generator.generate(Currency.JPY, datetime(2026, 1, 1))
    with pytest.raises(TypeError):
        generator.generate("JPY", T)  # type: ignore[arg-type]


def test_reserve_coverage_object_refuses_inconsistent_parts():
    snapshot = abc_with_reserve().coverage().snapshot
    with pytest.raises(ValueError):
        ReserveCoverage(snapshot=snapshot, reserves_total=1, reserve_account_ids=("b", "a"))
    with pytest.raises(ValueError):
        ReserveCoverage(snapshot=snapshot, reserves_total=1, reserve_account_ids=("a", "a"))
    with pytest.raises(TypeError):
        ReserveCoverage(snapshot=snapshot, reserves_total=1.0, reserve_account_ids=())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ReserveCoverage(snapshot=snapshot, reserves_total=10**38, reserve_account_ids=())


class _NowForbidden:
    @staticmethod
    def now(*args, **kwargs):
        raise AssertionError("datetime.now() called during coverage generation (wall-clock leak)")

    fromisoformat = staticmethod(datetime.fromisoformat)


def test_generation_reads_no_clock_and_is_reproducible():
    f = abc_with_reserve()
    assert not hasattr(coverage_mod, "datetime")  # the module does not import it at run time
    original = (ledger_mod.datetime, storage_mod.datetime)
    ledger_mod.datetime = _NowForbidden  # type: ignore[assignment]
    storage_mod.datetime = _NowForbidden  # type: ignore[assignment]
    try:
        first = f.coverage()
        declared = DeclaredReserveCoverageGenerator(f.ledger, institution_id=INSTITUTION, salt_key=KEY).generate(
            (Currency.JPY,), GENESIS_HASH, T
        )
    finally:
        ledger_mod.datetime, storage_mod.datetime = original
    assert first.summary_hash() == abc_with_reserve().coverage().summary_hash() == ABC_COVERAGE_SUMMARY_HASH
    assert declared.per_currency[0].summary_hash() == ABC_COVERAGE_SUMMARY_HASH


# ---------------------------------------------------------------------------
# Several currencies
# ---------------------------------------------------------------------------


def two_currency_fixture(usd_reserve: int = 2_000) -> Fixture:
    f = abc_with_reserve()
    f.open("platform-usd", OwnerType.PLATFORM, Currency.USD)
    f.open("reserve-usd", OwnerType.RESERVE, Currency.USD)
    f.open("alice-usd", OwnerType.USER, Currency.USD)
    f.move("platform-usd", "reserve-usd", usd_reserve, currency=Currency.USD)
    f.move("platform-usd", "alice-usd", 1_500, currency=Currency.USD)
    return f


def test_declared_reserve_coverage_over_two_currencies():
    f = two_currency_fixture()
    generator = DeclaredReserveCoverageGenerator(f.ledger, institution_id=INSTITUTION, salt_key=KEY)
    a = generator.generate((Currency.USD, Currency.JPY), GENESIS_HASH, T)
    b = generator.generate([Currency.JPY, Currency.USD], GENESIS_HASH, T)
    assert a.summary_hash() == b.summary_hash()
    summary = a.to_public_summary()
    assert set(summary) == {"format", "institution_id", "snapshot_at", "currencies", "all_covered", "audit_log_committed"}
    assert summary["format"] == FORMAT_DECLARED_RESERVE_COVERAGE
    assert (summary["institution_id"], summary["snapshot_at"]) == (INSTITUTION, "2026-01-01T00:00:00.000000Z")
    assert [c["snapshot"]["currency"] for c in summary["currencies"]] == ["JPY", "USD"]
    assert all(c["format"] == FORMAT_RESERVE_COVERAGE for c in summary["currencies"])
    assert summary["currencies"][0] == EXPECTED["abc_reserve_coverage_summary"]["summary"]
    assert summary["all_covered"] is True and a.all_covered is True
    assert summary["audit_log_committed"] == GENESIS_HASH
    assert a.for_currency("USD").coverage_basis_points_floor == 13333
    with pytest.raises(KeyError):
        a.for_currency("EUR")
    assert sha256_hex(canonical_json(summary)) == a.summary_hash()

    short = two_currency_fixture(usd_reserve=1_000)
    c = DeclaredReserveCoverageGenerator(short.ledger, institution_id=INSTITUTION, salt_key=KEY).generate(
        (Currency.JPY, Currency.USD), GENESIS_HASH, T
    )
    assert c.all_covered is False
    assert [r.is_covered for r in c.per_currency] == [True, False]


@pytest.mark.parametrize(
    "currencies, audit_hash, error",
    [
        ((), GENESIS_HASH, ValueError),  # the legacy aggregate reports "all solvent" for none
        ((Currency.JPY, Currency.JPY), GENESIS_HASH, ValueError),
        ((Currency.JPY,), "0" * 63, ValueError),
        ((Currency.JPY,), "A" * 64, ValueError),
        (("JPY",), GENESIS_HASH, TypeError),
    ],
)
def test_declared_generator_refuses(currencies, audit_hash, error):
    f = abc_with_reserve()
    generator = DeclaredReserveCoverageGenerator(f.ledger, institution_id=INSTITUTION, salt_key=KEY)
    with pytest.raises(error):
        generator.generate(currencies, audit_hash, T)


def test_declared_object_refuses_inconsistent_parts():
    f = two_currency_fixture()
    jpy = f.coverage(Currency.JPY)
    usd = f.coverage(Currency.USD)
    usd_later = f.coverage(Currency.USD, at=T + timedelta(days=1))
    with pytest.raises(ValueError):
        DeclaredReserveCoverage(per_currency=(), audit_log_root_hash=GENESIS_HASH)
    with pytest.raises(ValueError):
        DeclaredReserveCoverage(per_currency=(usd, jpy), audit_log_root_hash=GENESIS_HASH)  # not sorted
    with pytest.raises(ValueError):
        DeclaredReserveCoverage(per_currency=(jpy, usd_later), audit_log_root_hash=GENESIS_HASH)
    other = ReserveCoverageGenerator(f.ledger, institution_id="other-institution", salt_key=KEY).generate(Currency.USD, T)
    with pytest.raises(ValueError):
        DeclaredReserveCoverage(per_currency=(jpy, other), audit_log_root_hash=GENESIS_HASH)
    assert DeclaredReserveCoverage(per_currency=(jpy, usd), audit_log_root_hash=GENESIS_HASH).all_covered


def test_legacy_aggregate_still_accepts_an_empty_list():
    f = abc_with_reserve()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PendingDeprecationWarning)
        proof, trees = ProofOfSolvencyGenerator(f.ledger).generate((), GENESIS_HASH, T)
    assert proof.overall_solvent is True and trees == {}


# ---------------------------------------------------------------------------
# Check of a published coverage summary
# ---------------------------------------------------------------------------


def _with(document: dict, fn) -> dict:
    changed = json.loads(json.dumps(document))
    fn(changed)
    return changed


def test_genuine_coverage_summaries_pass_the_check():
    for f, covered, floor in ((abc_with_reserve(), True, 21052), (overdraft_with_reserve(), False, 5000),
                              (pair(5, 0), True, None), (pair(-1, 0), False, None), (pair(-1, 3), False, -3334)):
        coverage = f.coverage()
        result = verify_reserve_coverage_summary(coverage.to_public_summary())
        assert result.ok is True and result.reason is None, result
        d = result.details
        assert (d["covered"], d["basis_points_floor"]) == (covered, floor)
        assert (d["reserves_total"], d["liabilities_total"]) == (coverage.reserves_total, coverage.liabilities_total)
        assert d["summary_hash"] == coverage.summary_hash() and d["currency"] == "JPY"


def test_a_verdict_that_contradicts_the_totals_is_reported():
    # reserves 50, liabilities 160: not covered, 3125 basis points
    summary = pair(50, 160).coverage().to_public_summary()
    assert summary["coverage"] == {"covered": False, "basis_points_floor": "3125"}
    flipped = _with(summary, lambda s: s["coverage"].__setitem__("covered", True))
    result = verify_reserve_coverage_summary(flipped)
    assert (result.ok, result.reason) == (False, "COVERAGE_MISMATCH")
    assert result.details["recomputed"] == {"covered": False, "basis_points_floor": "3125"}
    assert result.details["stated"] == {"covered": True, "basis_points_floor": "3125"}
    for figures in ({"covered": False, "basis_points_floor": "3126"}, {"covered": False, "basis_points_floor": None},
                    {"covered": True, "basis_points_floor": "31250"}):
        assert verify_reserve_coverage_summary(_with(summary, lambda s: s.__setitem__("coverage", figures))).reason == "COVERAGE_MISMATCH"
    # a larger reserve total with the verdict to match is consistent: the reserve total is the
    # operator's statement, which no check of the summary can test
    restated = _with(summary, lambda s: (s["reserves_self_booked"].__setitem__("total", "500"),
                                         s.__setitem__("coverage", {"covered": True, "basis_points_floor": "31250"})))
    assert verify_reserve_coverage_summary(restated).ok
    # without liabilities there are no basis points: a stated number is a mismatch
    no_liabilities = pair(5, 0).coverage().to_public_summary()
    stated_floor = _with(no_liabilities, lambda s: s["coverage"].__setitem__("basis_points_floor", "0"))
    assert verify_reserve_coverage_summary(stated_floor).reason == "COVERAGE_MISMATCH"


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s.pop("coverage"),
        lambda s: s.__setitem__("extra", "x"),
        lambda s: s["snapshot"].__setitem__("format", "x"),
        lambda s: s["snapshot"]["liabilities"].__setitem__("total", "-5"),
        lambda s: s["reserves_self_booked"].__setitem__("total", "050"),
        lambda s: s["reserves_self_booked"].__setitem__("total", "1" + "0" * 38),
        lambda s: s["reserves_self_booked"].__setitem__("account_ids", ["reserve", "reserve"]),
        lambda s: s["reserves_self_booked"].__setitem__("account_ids", ["z", "a"]),
        lambda s: s["reserves_self_booked"].__setitem__("account_ids", [""]),
        lambda s: s["reserves_self_booked"].__setitem__("account_ids", "reserve"),
        lambda s: s["reserves_self_booked"].pop("account_ids"),
        lambda s: s["coverage"].__setitem__("covered", "false"),
        lambda s: s["coverage"].__setitem__("covered", None),
        lambda s: s["coverage"].__setitem__("basis_points_floor", "03125"),
        lambda s: s["coverage"].__setitem__("basis_points_floor", "3125.0"),
        lambda s: s["coverage"].__setitem__("basis_points_floor", " 3125"),
        lambda s: s["coverage"].__setitem__("basis_points_floor", "３１２５"),
        lambda s: s["coverage"].__setitem__("basis_points_floor", 3125),
        lambda s: s["coverage"].__setitem__("note", "x"),
    ],
)
def test_malformed_coverage_summary(change):
    summary = _with(pair(50, 160).coverage().to_public_summary(), change)
    assert verify_reserve_coverage_summary(summary).reason == "MALFORMED_SUMMARY"


@pytest.mark.parametrize("document", [None, 5, "summary", [], {}, {"format": "openreserve.reserve-coverage/2"}])
def test_document_that_is_not_a_coverage_summary(document):
    assert verify_reserve_coverage_summary(document).reason == "UNSUPPORTED_FORMAT"


def test_declared_coverage_summary_check():
    f = two_currency_fixture(usd_reserve=1_000)
    declared = DeclaredReserveCoverageGenerator(f.ledger, institution_id=INSTITUTION, salt_key=KEY).generate(
        (Currency.JPY, Currency.USD), GENESIS_HASH, T
    )
    summary = declared.to_public_summary()
    result = verify_reserve_coverage_summary(summary)
    assert result.ok is True, result
    assert result.details == {"format": FORMAT_DECLARED_RESERVE_COVERAGE, "summary_hash": declared.summary_hash(),
                              "currencies": ("JPY", "USD"), "all_covered": False}

    flag = _with(summary, lambda s: s.__setitem__("all_covered", True))
    assert verify_reserve_coverage_summary(flag).reason == "ALL_COVERED_MISMATCH"
    # one currency's verdict changed together with the aggregate flag: the currency is caught
    both = _with(summary, lambda s: (s.__setitem__("all_covered", True), s["currencies"][1]["coverage"].__setitem__("covered", True)))
    result = verify_reserve_coverage_summary(both)
    assert (result.reason, result.details["index"]) == ("COVERAGE_MISMATCH", 1)
    for change in (
        lambda s: s["currencies"].reverse(),
        lambda s: s["currencies"].append(s["currencies"][0]),
        lambda s: s.__setitem__("currencies", []),
        lambda s: s.__setitem__("currencies", [None]),
        lambda s: s.__setitem__("snapshot_at", "2026-01-01T00:00:00Z"),
        lambda s: s.__setitem__("institution_id", "other-institution"),
        lambda s: s.__setitem__("audit_log_committed", "A" * 64),
        lambda s: s.__setitem__("all_covered", "false"),
        lambda s: s.pop("audit_log_committed"),
        lambda s: s.__setitem__("extra", "x"),
    ):
        assert verify_reserve_coverage_summary(_with(summary, change)).reason == "MALFORMED_SUMMARY"
    # a per-currency summary is checked as such; an aggregate is not a per-currency summary
    assert verify_reserve_coverage_summary(summary["currencies"][1]).ok
    assert verify_reserve_coverage_summary(_with(summary, lambda s: s["currencies"].__setitem__(0, summary))).reason == "MALFORMED_SUMMARY"


def test_seeded_mutations_of_a_coverage_summary_never_raise():
    from test_sumtree import _same, mutate  # the mutation helper of the tree tests

    rng = random.Random(92)
    f = two_currency_fixture(usd_reserve=1_000)
    documents = [
        DeclaredReserveCoverageGenerator(f.ledger, institution_id=INSTITUTION, salt_key=KEY)
        .generate((Currency.JPY, Currency.USD), GENESIS_HASH, T).to_public_summary(),
        pair(50, 160).coverage().to_public_summary(),
    ]
    done = 0
    while done < 2_000:
        genuine = rng.choice(documents)
        candidate = mutate(genuine, rng)
        if _same(candidate, genuine):
            continue
        done += 1
        result = verify_reserve_coverage_summary(candidate)  # never raises
        assert result.ok is False or result.details["summary_hash"] != sha256_hex(canonical_json(genuine))
