"""transparency/coverage.py — declared reserve coverage as exact values.

Two artifacts:

* ``openreserve.reserve-coverage/1`` (:class:`ReserveCoverage`), per currency: the
  liabilities snapshot of ``transparency/snapshot.py``, the reserves the operator booked, and
  the coverage derived from the two totals;
* ``openreserve.declared-reserve-coverage/1`` (:class:`DeclaredReserveCoverage`): several
  currencies at one instant, plus the audit-chain commitment.

The exact value of coverage is the pair of totals, which the artifact states. What is derived
from it is computed with integers only:

* ``covered`` is ``reserves >= liabilities``;
* ``basis_points_floor`` is ``(10000 * reserves) // liabilities`` (floor division, so it also
  rounds toward minus infinity for a negative reserve total), and ``null`` when the
  liabilities are 0;
* in Python the ratio is ``Fraction(reserves, liabilities)``, or ``None`` when the
  liabilities are 0. There is no float and no sentinel value.

The reserve figure is named ``reserves_self_booked`` because that is exactly what it is: the
signed sum of the settled balances, at the snapshot instant, of the operator's own accounts of
type ``RESERVE`` in that currency. It is not evidence that reserve assets exist or are
available, and the comparison is not a solvency test: it compares two booked totals under a
1:1 reserve model. The account ids listed are those of the reserve accounts with a non-zero
settled balance at the instant, sorted; an account with balance zero adds nothing to the total,
and leaving it out means an account opened later does not change an earlier summary.
Receivables (negative customer balances) are never counted as reserves.

:func:`verify_reserve_coverage_summary` recomputes, for a published summary of either format,
what the summary derives from its own figures. It cannot check the reserve total, which is the
operator's statement.

The specification is docs/hypothetical/W_CHANGEOVER_DESIGN.md, sections 9.2 and 9.3.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Iterable

from openreserve.core.exact import (
    ExactValueError,
    amount_from_text,
    amount_to_text,
    canonical_json,
    instant_from_text,
    require_aware,
    sha256_hex,
)
from openreserve.core.ledger import Ledger
from openreserve.core.types import Currency, OwnerType
from openreserve.transparency.snapshot import LiabilitiesSnapshot, LiabilitiesSnapshotGenerator, _parse_summary
from openreserve.transparency.sumtree import (
    SumTreeError,
    Verification,
    check_identifier,
    check_salt_key,
    is_hash_text,
)

if TYPE_CHECKING:  # the module never constructs or reads a clock
    from datetime import datetime

__all__ = [
    "FORMAT_DECLARED_RESERVE_COVERAGE",
    "FORMAT_RESERVE_COVERAGE",
    "CoverageFigures",
    "DeclaredReserveCoverage",
    "DeclaredReserveCoverageGenerator",
    "ReserveCoverage",
    "ReserveCoverageGenerator",
    "coverage_figures",
    "verify_reserve_coverage_summary",
]

FORMAT_RESERVE_COVERAGE = "openreserve.reserve-coverage/1"
FORMAT_DECLARED_RESERVE_COVERAGE = "openreserve.declared-reserve-coverage/1"

_BASIS_POINTS = 10000


@dataclass(frozen=True)
class CoverageFigures:
    """What is derived from a pair of totals, exactly.

    ``covered`` is always ``reserves >= liabilities``. ``ratio`` and ``basis_points_floor``
    are ``None`` when the liabilities are 0.
    """

    reserves: int
    liabilities: int
    covered: bool
    basis_points_floor: int | None
    ratio: Fraction | None

    def to_document(self) -> dict[str, Any]:
        """``{"covered", "basis_points_floor"}``, the integer written as a string or ``null``."""
        return {
            "covered": self.covered,
            "basis_points_floor": None if self.basis_points_floor is None else str(self.basis_points_floor),
        }


def coverage_figures(reserves: int, liabilities: int) -> CoverageFigures:
    """Coverage of ``liabilities`` (at least 0) by ``reserves`` (signed), with integers only."""
    if type(reserves) is not int or type(liabilities) is not int:
        raise TypeError("reserves and liabilities must be int (minor units)")
    if liabilities < 0:
        raise ValueError("liabilities must not be negative")
    if liabilities == 0:
        return CoverageFigures(reserves, liabilities, reserves >= 0, None, None)
    return CoverageFigures(
        reserves=reserves,
        liabilities=liabilities,
        covered=reserves >= liabilities,
        basis_points_floor=(_BASIS_POINTS * reserves) // liabilities,
        ratio=Fraction(reserves, liabilities),
    )


def _self_booked_reserves(ledger: Ledger, currency: Currency, snapshot_at: "datetime") -> tuple[int, tuple[str, ...]]:
    """Signed sum of the settled balances of the reserve accounts of ``currency`` at the
    instant, and the sorted ids of those with a non-zero balance."""
    total = 0
    ids: list[str] = []
    for account in ledger.list_accounts(owner_type=OwnerType.RESERVE):
        if account.currency != currency:
            continue
        balance = ledger.balance(account.account_id, as_of=snapshot_at).cents
        if balance != 0:
            total += balance
            ids.append(account.account_id)
    return total, tuple(sorted(ids))


class ReserveCoverage:
    """Per currency: the liabilities snapshot, the reserves the operator booked, and coverage."""

    def __init__(self, *, snapshot: LiabilitiesSnapshot, reserves_total: int, reserve_account_ids: Iterable[str]) -> None:
        ids = tuple(reserve_account_ids)
        if type(reserves_total) is not int:
            raise TypeError("reserves_total must be int (minor units)")
        amount_to_text(reserves_total)  # within 10**38 - 1
        for account_id in ids:
            check_identifier(account_id, "reserve account id")
        if list(ids) != sorted(set(ids)):
            raise ValueError("reserve account ids must be sorted and unique")
        self._snapshot = snapshot
        self._reserves_total = reserves_total
        self._reserve_account_ids = ids
        self._figures = coverage_figures(reserves_total, snapshot.liabilities.total)

    @property
    def snapshot(self) -> LiabilitiesSnapshot:
        return self._snapshot

    @property
    def currency(self) -> str:
        return self._snapshot.liabilities.context.currency

    @property
    def reserves_total(self) -> int:
        """Signed sum of the settled balances of the reserve accounts (self-booked)."""
        return self._reserves_total

    @property
    def reserve_account_ids(self) -> tuple[str, ...]:
        return self._reserve_account_ids

    @property
    def liabilities_total(self) -> int:
        return self._snapshot.liabilities.total

    @property
    def is_covered(self) -> bool:
        """``reserves_total >= liabilities_total``: a comparison of two booked totals."""
        return self._figures.covered

    @property
    def coverage_ratio(self) -> Fraction | None:
        """``Fraction(reserves, liabilities)``, or ``None`` when there are no liabilities."""
        return self._figures.ratio

    @property
    def coverage_basis_points_floor(self) -> int | None:
        """``(10000 * reserves) // liabilities``, or ``None`` when there are no liabilities."""
        return self._figures.basis_points_floor

    def to_public_summary(self) -> dict[str, Any]:
        """What is published: no salt, no customer account id, no number (integers are strings)."""
        return {
            "format": FORMAT_RESERVE_COVERAGE,
            "snapshot": self._snapshot.to_public_summary(),
            "reserves_self_booked": {
                "total": amount_to_text(self._reserves_total),
                "account_ids": list(self._reserve_account_ids),
            },
            "coverage": self._figures.to_document(),
        }

    def summary_hash(self) -> str:
        """SHA-256 of the canonical JSON of the public summary."""
        return sha256_hex(canonical_json(self.to_public_summary()))


class ReserveCoverageGenerator:
    """Builds a :class:`ReserveCoverage` from a ledger.

    ``institution_id`` and ``salt_key`` are those of :class:`LiabilitiesSnapshotGenerator`.
    Generation is a pure function of ledger state, instant, institution id and key.
    """

    def __init__(self, ledger: Ledger, *, institution_id: str, salt_key: bytes) -> None:
        self._ledger = ledger
        self._snapshots = LiabilitiesSnapshotGenerator(ledger, institution_id=institution_id, salt_key=salt_key)

    def generate(self, currency: Currency, snapshot_at: "datetime") -> ReserveCoverage:
        """Coverage of ``currency`` at ``snapshot_at`` (explicit and timezone-aware)."""
        require_aware(snapshot_at, "snapshot_at")
        if not isinstance(currency, Currency):
            raise TypeError("currency must be a ledger Currency")
        snapshot = self._snapshots.generate(currency, snapshot_at)
        total, ids = _self_booked_reserves(self._ledger, currency, snapshot_at)
        return ReserveCoverage(snapshot=snapshot, reserves_total=total, reserve_account_ids=ids)


class DeclaredReserveCoverage:
    """Several currencies at one instant, sorted by currency code, plus the audit commitment."""

    def __init__(self, *, per_currency: Iterable[ReserveCoverage], audit_log_root_hash: str) -> None:
        results = tuple(per_currency)
        if not results:
            raise ValueError("declared reserve coverage needs at least one currency")
        codes = [r.currency for r in results]
        if codes != sorted(set(codes)):
            raise ValueError("per-currency results must be sorted by currency code and unique")
        contexts = {(r.snapshot.liabilities.context.institution_id, r.snapshot.snapshot_at) for r in results}
        if len(contexts) != 1:
            raise ValueError("every currency must share one institution id and one instant")
        if not is_hash_text(audit_log_root_hash):
            raise ValueError("audit_log_root_hash must be 64 lower-case hexadecimal characters")
        self._per_currency = results
        self._audit_log_root_hash = audit_log_root_hash
        self._institution_id, self._snapshot_at = contexts.pop()

    @property
    def per_currency(self) -> tuple[ReserveCoverage, ...]:
        return self._per_currency

    @property
    def institution_id(self) -> str:
        return self._institution_id

    @property
    def snapshot_at(self) -> str:
        """The instant as canonical text."""
        return self._snapshot_at

    @property
    def audit_log_root_hash(self) -> str:
        return self._audit_log_root_hash

    @property
    def all_covered(self) -> bool:
        """True exactly when every currency is covered (there is always at least one)."""
        return all(r.is_covered for r in self._per_currency)

    def for_currency(self, code: str) -> ReserveCoverage:
        for result in self._per_currency:
            if result.currency == code:
                return result
        raise KeyError(code)

    def to_public_summary(self) -> dict[str, Any]:
        return {
            "format": FORMAT_DECLARED_RESERVE_COVERAGE,
            "institution_id": self._institution_id,
            "snapshot_at": self._snapshot_at,
            "currencies": [r.to_public_summary() for r in self._per_currency],
            "all_covered": self.all_covered,
            "audit_log_committed": self._audit_log_root_hash,
        }

    def summary_hash(self) -> str:
        return sha256_hex(canonical_json(self.to_public_summary()))


class DeclaredReserveCoverageGenerator:
    """Builds a :class:`DeclaredReserveCoverage` for several currencies of one ledger."""

    def __init__(self, ledger: Ledger, *, institution_id: str, salt_key: bytes) -> None:
        check_identifier(institution_id, "institution_id")
        check_salt_key(salt_key)
        self._per_currency = ReserveCoverageGenerator(ledger, institution_id=institution_id, salt_key=salt_key)

    def generate(
        self,
        currencies: Iterable[Currency],
        audit_log_root_hash: str,
        snapshot_at: "datetime",
    ) -> DeclaredReserveCoverage:
        """Coverage of each currency at ``snapshot_at``, sorted by currency code.

        An empty list of currencies, a currency given twice and an audit hash that is not 64
        lower-case hexadecimal characters are refused (``ValueError``).
        """
        require_aware(snapshot_at, "snapshot_at")
        chosen = list(currencies)
        if not chosen:
            raise ValueError("declared reserve coverage needs at least one currency")
        if any(not isinstance(c, Currency) for c in chosen):
            raise TypeError("currencies must be ledger Currency values")
        if len({c.code for c in chosen}) != len(chosen):
            raise ValueError("a currency is given twice")
        if not is_hash_text(audit_log_root_hash):
            raise ValueError("audit_log_root_hash must be 64 lower-case hexadecimal characters")
        results = [self._per_currency.generate(c, snapshot_at) for c in sorted(chosen, key=lambda c: c.code)]
        return DeclaredReserveCoverage(per_currency=results, audit_log_root_hash=audit_log_root_hash)


# ---------------------------------------------------------------------------
# Check of a published summary
# ---------------------------------------------------------------------------

_COVERAGE_KEYS = frozenset({"format", "snapshot", "reserves_self_booked", "coverage"})
_RESERVES_KEYS = frozenset({"total", "account_ids"})
_FIGURES_KEYS = frozenset({"covered", "basis_points_floor"})
_DECLARED_KEYS = frozenset(
    {"format", "institution_id", "snapshot_at", "currencies", "all_covered", "audit_log_committed"}
)


class _Malformed(Exception):
    """A summary that breaks a rule of its format (reported as ``MALFORMED_SUMMARY``)."""


def _fail(reason: str, message: str = "", **extra: Any) -> Verification:
    details: dict[str, Any] = dict(extra)
    if message:
        details["message"] = message
    return Verification(ok=False, reason=reason, details=details)


def _integer_text(value: object) -> int:
    """Canonical signed decimal text without a bound (basis points can exceed 10**38)."""
    if type(value) is not str:
        raise _Malformed("basis_points_floor must be text or null")
    try:
        number = int(value)
    except ValueError:
        raise _Malformed("basis_points_floor is not canonical decimal text") from None
    if str(number) != value:
        raise _Malformed("basis_points_floor is not canonical decimal text")
    return number


def verify_reserve_coverage_summary(summary: object) -> Verification:
    """Check a published coverage summary against its own figures.

    For ``openreserve.reserve-coverage/1``: the document fits the canonical value profile and
    has exactly its keys; the embedded snapshot summary is valid (the rules of
    ``verify_snapshot_inclusion``); ``reserves_self_booked`` has exactly a signed total within
    10**38 - 1 and a sorted list of unique account ids; ``coverage`` has exactly a boolean
    ``covered`` and ``basis_points_floor`` as canonical decimal text or ``null``. Any breach is
    ``MALFORMED_SUMMARY``. Then ``covered`` and ``basis_points_floor`` must be what
    :func:`coverage_figures` gives for the reserve total and the liabilities total of the
    snapshot (``COVERAGE_MISMATCH``).

    For ``openreserve.declared-reserve-coverage/1``: exactly its keys, an institution id, a
    canonical instant, at least one per-currency summary, each of which passes the check above
    (its reason is passed on) and names the same institution and instant, sorted by currency
    code without repetition, and a 64-character audit hash (``MALFORMED_SUMMARY`` otherwise);
    then ``all_covered`` must be true exactly when every currency is covered
    (``ALL_COVERED_MISMATCH``). Any other document is ``UNSUPPORTED_FORMAT``.

    A successful check establishes only that the summary is well formed and that its verdicts
    follow from the totals it states. The reserve total is the operator's statement of its own
    booked balances, not evidence that reserve assets exist or are available. The liabilities
    total is bound to the snapshot root; a holder checks their part of it with
    ``verify_snapshot_inclusion`` against the embedded snapshot summary.

    On success ``details`` holds ``format`` and ``summary_hash`` (to compare with the published
    hash), and for one currency ``currency``, ``reserves_total``, ``liabilities_total``,
    ``covered`` and ``basis_points_floor``; for several currencies ``currencies`` (the codes)
    and ``all_covered``. No content of ``summary`` makes the function raise.
    """
    try:
        return _verify_coverage_summary(summary)
    except Exception as exc:  # last line of defence: fail closed, never raise on content
        return _fail("MALFORMED_SUMMARY", f"unexpected {type(exc).__name__}")


def _verify_coverage_summary(summary: object) -> Verification:
    if type(summary) is not dict or summary.get("format") not in (
        FORMAT_RESERVE_COVERAGE,
        FORMAT_DECLARED_RESERVE_COVERAGE,
    ):
        return _fail("UNSUPPORTED_FORMAT", "not a document of a reserve coverage format")
    try:
        canonical_json(summary)
    except ExactValueError:
        return _fail("MALFORMED_SUMMARY", "the summary does not fit the canonical value profile")
    try:
        if summary["format"] == FORMAT_RESERVE_COVERAGE:
            result, _covered = _check_one(summary)
            return result
        return _check_declared(summary)
    except (_Malformed, SumTreeError, ExactValueError) as exc:
        return _fail("MALFORMED_SUMMARY", str(exc))


def _check_one(summary: dict[str, Any]) -> tuple[Verification, bool]:
    """The check of one currency. Raises ``_Malformed`` (or a field error) for a breach."""
    if set(summary) != _COVERAGE_KEYS or summary["format"] != FORMAT_RESERVE_COVERAGE:
        raise _Malformed(f"a summary of format {FORMAT_RESERVE_COVERAGE} must have exactly its four keys")
    parsed = _parse_summary(summary["snapshot"])
    reserves = summary["reserves_self_booked"]
    if type(reserves) is not dict or set(reserves) != _RESERVES_KEYS:
        raise _Malformed("reserves_self_booked must have exactly total and account_ids")
    reserves_total = amount_from_text(reserves["total"])
    account_ids = reserves["account_ids"]
    if type(account_ids) is not list:
        raise _Malformed("account_ids must be a list")
    for account_id in account_ids:
        check_identifier(account_id, "reserve account id")
    if account_ids != sorted(set(account_ids)):
        raise _Malformed("account_ids must be sorted and unique")
    stated = summary["coverage"]
    if type(stated) is not dict or set(stated) != _FIGURES_KEYS:
        raise _Malformed("coverage must have exactly covered and basis_points_floor")
    if type(stated["covered"]) is not bool:
        raise _Malformed("covered must be true or false")
    if stated["basis_points_floor"] is not None:
        _integer_text(stated["basis_points_floor"])

    liabilities_total = parsed["heads"]["liabilities"][1]
    figures = coverage_figures(reserves_total, liabilities_total)
    recomputed = figures.to_document()
    if stated != recomputed:
        return (
            _fail(
                "COVERAGE_MISMATCH",
                "covered or basis_points_floor do not follow from the two totals",
                stated=dict(stated),
                recomputed=recomputed,
            ),
            figures.covered,
        )
    return (
        Verification(
            ok=True,
            reason=None,
            details={
                "format": FORMAT_RESERVE_COVERAGE,
                "summary_hash": sha256_hex(canonical_json(summary)),
                "currency": summary["snapshot"]["currency"],
                "reserves_total": reserves_total,
                "liabilities_total": liabilities_total,
                "covered": figures.covered,
                "basis_points_floor": figures.basis_points_floor,
            },
        ),
        figures.covered,
    )


def _check_declared(summary: dict[str, Any]) -> Verification:
    if set(summary) != _DECLARED_KEYS:
        raise _Malformed(f"a summary of format {FORMAT_DECLARED_RESERVE_COVERAGE} must have exactly its six keys")
    check_identifier(summary["institution_id"], "institution_id")
    instant_from_text(summary["snapshot_at"])
    if not is_hash_text(summary["audit_log_committed"]):
        raise _Malformed("audit_log_committed must be 64 lower-case hexadecimal characters")
    if type(summary["all_covered"]) is not bool:
        raise _Malformed("all_covered must be true or false")
    currencies = summary["currencies"]
    if type(currencies) is not list or not currencies:
        raise _Malformed("currencies must be a non-empty list")
    codes: list[str] = []
    verdicts: list[bool] = []
    for item in currencies:
        if type(item) is not dict:
            raise _Malformed("each currency must be a summary of format " + FORMAT_RESERVE_COVERAGE)
        result, covered = _check_one(item)
        if not result.ok:
            return Verification(ok=False, reason=result.reason, details=dict(result.details, index=len(codes)))
        snapshot = item["snapshot"]
        if snapshot["institution_id"] != summary["institution_id"] or snapshot["snapshot_at"] != summary["snapshot_at"]:
            raise _Malformed("every currency must have the institution id and the instant of the summary")
        codes.append(snapshot["currency"])
        verdicts.append(covered)
    if codes != sorted(set(codes)):
        raise _Malformed("currencies must be sorted by currency code and unique")
    if summary["all_covered"] != all(verdicts):
        return _fail(
            "ALL_COVERED_MISMATCH",
            "all_covered is not true exactly when every currency is covered",
            stated=summary["all_covered"],
            recomputed=all(verdicts),
        )
    return Verification(
        ok=True,
        reason=None,
        details={
            "format": FORMAT_DECLARED_RESERVE_COVERAGE,
            "summary_hash": sha256_hex(canonical_json(summary)),
            "currencies": tuple(codes),
            "all_covered": all(verdicts),
        },
    )
