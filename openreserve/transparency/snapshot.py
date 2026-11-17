"""transparency/snapshot.py — the liabilities snapshot, format ``openreserve.liabilities-snapshot/1``.

For one currency and one instant, every customer account (account type ``USER``) with a
**positive** settled balance is a leaf of the ``liabilities`` tree, every one with a
**negative** balance is a leaf of the ``receivables`` tree with the magnitude, and an account
with balance zero has no leaf. The two trees are never netted: one customer's debt does not
reduce what is owed to another.

What a snapshot contains: exactly the entries of transactions that are SETTLED with a
settlement instant at or before the snapshot instant (inclusive, compared as instants). A
PENDING transaction is never in it. Because only accounts with a non-zero balance have
leaves, an account opened later does not change an earlier snapshot.

Both trees are Merkle-sum trees (``transparency/sumtree.py``): each root binds its context,
its leaf count and its total. A holder's check shows that their amount is inside the total
bound in the published root. It does not show that every liability was recorded, that every
holder was given the same summary, or anything about reserves. The receivables tree lets a
debtor check the amount recorded against them; it does not count as reserves, and nothing
stops an operator from listing debtors who do not exist.

The specification is docs/hypothetical/W_CHANGEOVER_DESIGN.md, section 7.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable

from openreserve.core.exact import (
    ExactValueError,
    amount_from_text,
    amount_to_text,
    canonical_json,
    instant_from_text,
    instant_to_text,
    require_aware,
    sha256_hex,
)
from openreserve.core.ledger import Ledger
from openreserve.core.types import Currency, OwnerType
from openreserve.transparency.sumtree import (
    FORMAT_INCLUSION,
    SNAPSHOT_KINDS,
    SumTree,
    SumTreeContext,
    SumTreeError,
    Verification,
    audit_sum_tree_export,
    check_identifier,
    check_salt_key,
    is_hash_text,
    parse_head,
    verify_sum_tree_inclusion,
)

if TYPE_CHECKING:  # the module never constructs or reads a clock
    from datetime import datetime

__all__ = [
    "FORMAT_SNAPSHOT",
    "FORMAT_SNAPSHOT_EXPORT",
    "INCLUSION_RULE",
    "POPULATION_RULE",
    "LiabilitiesSnapshot",
    "LiabilitiesSnapshotGenerator",
    "audit_snapshot_export",
    "collect_user_balances",
    "verify_snapshot_inclusion",
]

FORMAT_SNAPSHOT = "openreserve.liabilities-snapshot/1"
FORMAT_SNAPSHOT_EXPORT = "openreserve.liabilities-snapshot.export/1"
INCLUSION_RULE = "SETTLED_AT_OR_BEFORE_SNAPSHOT"
POPULATION_RULE = "USER_ACCOUNTS_WITH_NONZERO_BALANCE"

_SUMMARY_KEYS = frozenset(
    {
        "format",
        "institution_id",
        "currency",
        "minor_units",
        "snapshot_at",
        "inclusion_rule",
        "population_rule",
        "liabilities",
        "receivables",
    }
)
_EXPORT_KEYS = frozenset({"format", "summary", "liabilities", "receivables"})


def _fail(reason: str, message: str = "") -> Verification:
    return Verification(ok=False, reason=reason, details={"message": message} if message else {})


def collect_user_balances(ledger: Ledger, currency: Currency, snapshot_at: "datetime") -> list[tuple[str, int]]:
    """Every customer account of ``currency`` with its signed settled balance at the instant.

    The list is in storage order and includes accounts with a zero or negative balance. Only
    transactions settled at or before ``snapshot_at`` count; pending ones never do.
    """
    require_aware(snapshot_at, "snapshot_at")
    balances: list[tuple[str, int]] = []
    for account in ledger.list_accounts(owner_type=OwnerType.USER):
        if account.currency != currency:
            continue
        balances.append((account.account_id, ledger.balance(account.account_id, as_of=snapshot_at).cents))
    return balances


class LiabilitiesSnapshot:
    """The two trees of one currency at one instant, and what is derived from them."""

    def __init__(self, *, liabilities: SumTree, receivables: SumTree) -> None:
        a, b = liabilities.context, receivables.context
        if a.kind != "liabilities" or b.kind != "receivables":
            raise SumTreeError("a snapshot needs one tree of kind liabilities and one of kind receivables")
        if (a.institution_id, a.currency, a.minor_units, a.as_of) != (b.institution_id, b.currency, b.minor_units, b.as_of):
            raise SumTreeError("the two trees of a snapshot must share institution, currency and instant")
        if a.binding or b.binding:
            raise SumTreeError("the trees of a snapshot have no binding")
        both = set(leaf.slot_id for leaf in liabilities.leaves) & set(leaf.slot_id for leaf in receivables.leaves)
        if both:
            raise SumTreeError(f"slot id in both trees: {sorted(both)[0]!r}")
        self._liabilities = liabilities
        self._receivables = receivables

    @classmethod
    def from_balances(
        cls,
        *,
        institution_id: str,
        currency_code: str,
        minor_units: int,
        snapshot_at: "datetime",
        balances: Iterable[tuple[str, int]],
        salt_key: bytes,
    ) -> LiabilitiesSnapshot:
        """Build both trees from ``(slot id, signed balance)`` pairs. No ledger is needed.

        Positive balances become liabilities leaves, negative ones receivables leaves with
        the magnitude, zero balances no leaf. A slot id may appear only once.
        """
        as_of = instant_to_text(snapshot_at)
        positive: list[tuple[str, int]] = []
        negative: list[tuple[str, int]] = []
        seen: set[str] = set()
        for pair in balances:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise SumTreeError("each balance must be a (slot id, signed amount) pair")
            slot_id, balance = pair
            if type(balance) is not int:
                raise SumTreeError("a balance must be an int in minor units")
            if type(slot_id) is not str:
                raise SumTreeError("a slot id must be str")
            if slot_id in seen:
                raise SumTreeError(f"duplicate slot id: {slot_id!r}")
            seen.add(slot_id)
            if balance > 0:
                positive.append((slot_id, balance))
            elif balance < 0:
                negative.append((slot_id, -balance))

        def context(kind: str) -> SumTreeContext:
            return SumTreeContext(
                kind=kind, institution_id=institution_id, currency=currency_code, minor_units=minor_units, as_of=as_of
            )

        return cls(
            liabilities=SumTree.build(context("liabilities"), positive, salt_key=salt_key),
            receivables=SumTree.build(context("receivables"), negative, salt_key=salt_key),
        )

    @property
    def liabilities(self) -> SumTree:
        return self._liabilities

    @property
    def receivables(self) -> SumTree:
        return self._receivables

    @property
    def snapshot_at(self) -> str:
        """The snapshot instant as canonical text."""
        return self._liabilities.context.as_of

    def to_public_summary(self) -> dict[str, Any]:
        """What is published. It contains no salt, no account id and no number: every
        integer is a string."""
        context = self._liabilities.context
        return {
            "format": FORMAT_SNAPSHOT,
            "institution_id": context.institution_id,
            "currency": context.currency,
            "minor_units": amount_to_text(context.minor_units),
            "snapshot_at": context.as_of,
            "inclusion_rule": INCLUSION_RULE,
            "population_rule": POPULATION_RULE,
            "liabilities": self._liabilities.head(),
            "receivables": self._receivables.head(),
        }

    def summary_hash(self) -> str:
        """SHA-256 of the canonical JSON of the public summary."""
        return sha256_hex(canonical_json(self.to_public_summary()))

    def proof_for(self, account_id: str) -> dict[str, Any]:
        """The inclusion proof from whichever tree holds the account.

        Raises ``KeyError`` when the account has no leaf (its balance was zero, or it is not
        a customer account of this currency). The proof contains the account's salt.
        """
        if account_id in self._liabilities:
            return self._liabilities.proof_for(account_id)
        if account_id in self._receivables:
            return self._receivables.proof_for(account_id)
        raise KeyError(account_id)

    def auditor_export(self) -> dict[str, Any]:
        """Summary plus every leaf of both trees. For auditors, not for publication."""
        return {
            "format": FORMAT_SNAPSHOT_EXPORT,
            "summary": self.to_public_summary(),
            "liabilities": self._liabilities.export(),
            "receivables": self._receivables.export(),
        }


class LiabilitiesSnapshotGenerator:
    """Builds a :class:`LiabilitiesSnapshot` from a ledger.

    ``institution_id`` is a label chosen by the operator. ``salt_key`` is an operator secret
    of at least 32 bytes; the library never creates, stores or logs it. Generation is a pure
    function of ledger state, instant, institution id and key: it reads no clock.
    """

    def __init__(self, ledger: Ledger, *, institution_id: str, salt_key: bytes) -> None:
        # Validate now, so a bad key or label fails before any ledger is read.
        check_identifier(institution_id, "institution_id")
        check_salt_key(salt_key)
        self._ledger = ledger
        self._institution_id = institution_id
        self._salt_key = salt_key

    def generate(self, currency: Currency, snapshot_at: "datetime") -> LiabilitiesSnapshot:
        """Snapshot of the customer balances of ``currency`` at ``snapshot_at`` (explicit, aware)."""
        return LiabilitiesSnapshot.from_balances(
            institution_id=self._institution_id,
            currency_code=currency.code,
            minor_units=currency.minor_units,
            snapshot_at=snapshot_at,
            balances=collect_user_balances(self._ledger, currency, snapshot_at),
            salt_key=self._salt_key,
        )


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _parse_summary(summary: object) -> dict[str, Any]:
    """Validate a public summary. Returns its parsed parts, or raises ``SumTreeError``."""
    if type(summary) is not dict:
        raise SumTreeError("the summary must be an object")
    try:
        canonical_json(summary)
    except ExactValueError as exc:
        raise SumTreeError(f"the summary does not fit the canonical value profile: {exc}") from None
    if set(summary) != _SUMMARY_KEYS:
        raise SumTreeError("the summary does not have exactly its nine keys")
    if summary["format"] != FORMAT_SNAPSHOT:
        raise SumTreeError(f"the summary is not of format {FORMAT_SNAPSHOT}")
    if summary["inclusion_rule"] != INCLUSION_RULE or summary["population_rule"] != POPULATION_RULE:
        raise SumTreeError("unknown inclusion or population rule")
    try:
        minor_units = amount_from_text(summary["minor_units"], signed=False)
        instant_from_text(summary["snapshot_at"])
    except ExactValueError as exc:
        raise SumTreeError(str(exc)) from None
    contexts = {
        kind: SumTreeContext(  # applies the field rules of the tree context
            kind=kind,
            institution_id=summary["institution_id"],
            currency=summary["currency"],
            minor_units=minor_units,
            as_of=summary["snapshot_at"],
        )
        for kind in SNAPSHOT_KINDS
    }
    return {
        "contexts": contexts,
        "heads": {kind: parse_head(summary[kind]) for kind in SNAPSHOT_KINDS},
    }


def verify_snapshot_inclusion(
    proof: object, summary: object, *, expected_slot_id: str, expected_kind: str
) -> Verification:
    """Check a holder's proof against a published snapshot summary.

    ``expected_kind`` is the tree the holder expects to be in, from their own record:
    ``"liabilities"`` when the institution owes them the amount (a positive balance),
    ``"receivables"`` when they owe it (a negative balance). A proof of kind
    ``receivables`` says that the holder owes the amount, and an amount in that tree is not
    part of the published liabilities total.

    In this order: the summary has exactly its keys and valid values
    (``MALFORMED_SUMMARY``); the proof's kind is ``liabilities`` or ``receivables`` and its
    context equals the summary's institution, currency, minor units and instant, with an
    empty binding (``CONTEXT_MISMATCH``); the proof verifies against the root of that kind
    in the summary (the reason of ``verify_sum_tree_inclusion`` is passed on); the total and
    the leaf count authenticated by the root equal those stated in the summary
    (``TOTAL_MISMATCH``, ``COUNT_MISMATCH``); and the proof's kind is ``expected_kind``
    (``KIND_MISMATCH``). This last check comes after the others, so ``KIND_MISMATCH`` means
    that the proof is a genuine proof for the summary's tree of the other kind; ``details``
    then names that ``kind``.

    On success ``details`` holds the details of the tree check plus ``kind`` and
    ``summary_hash``. The holder then compares ``amount`` with their own record of their
    balance, and ``summary_hash`` with the published one; the library cannot do that for them.

    A malformed ``expected_slot_id``, or an ``expected_kind`` that is not one of the two
    kinds, raises ``ValueError``; no content of ``proof`` or ``summary`` makes the function
    raise.
    """
    try:
        check_identifier(expected_slot_id, "expected_slot_id")
    except SumTreeError as exc:
        raise ValueError(str(exc)) from None
    if type(expected_kind) is not str or expected_kind not in SNAPSHOT_KINDS:
        raise ValueError(f"expected_kind must be one of {SNAPSHOT_KINDS}")
    try:
        return _verify_snapshot_inclusion(proof, summary, expected_slot_id, expected_kind)
    except Exception as exc:  # last line of defence: fail closed, never raise on content
        return _fail("MALFORMED_PROOF", f"unexpected {type(exc).__name__}")


def _verify_snapshot_inclusion(
    proof: object, summary: object, expected_slot_id: str, expected_kind: str
) -> Verification:
    try:
        parsed = _parse_summary(summary)
    except SumTreeError as exc:
        return _fail("MALFORMED_SUMMARY", str(exc))

    context_document = proof.get("context") if type(proof) is dict else None
    if type(proof) is not dict or proof.get("format") != FORMAT_INCLUSION or type(context_document) is not dict:
        # Not a proof document at all: let the tree check name the reason.
        result = verify_sum_tree_inclusion(
            proof, expected_root=parsed["heads"]["liabilities"][0], expected_slot_id=expected_slot_id
        )
        return result if not result.ok else _fail("MALFORMED_PROOF", "the proof has no context")

    kind = context_document.get("kind")
    if type(kind) is not str or kind not in SNAPSHOT_KINDS:
        return _fail("CONTEXT_MISMATCH", "the proof is not of kind liabilities or receivables")
    if context_document != parsed["contexts"][kind].to_document():
        return _fail("CONTEXT_MISMATCH", "the proof was made for another institution, currency or instant")

    root, total, leaf_count = parsed["heads"][kind]
    result = verify_sum_tree_inclusion(proof, expected_root=root, expected_slot_id=expected_slot_id)
    if not result.ok:
        return result
    if result.details["total"] != total:
        return _fail("TOTAL_MISMATCH", "the total stated in the summary is not the total bound in the root")
    if result.details["leaf_count"] != leaf_count:
        return _fail("COUNT_MISMATCH", "the leaf count stated in the summary is not the one bound in the root")
    if kind != expected_kind:
        return Verification(
            ok=False,
            reason="KIND_MISMATCH",
            details={
                "message": f"the proof is genuine for the {kind} tree of the summary, not for the {expected_kind} tree",
                "kind": kind,
            },
        )

    details = dict(result.details)
    details["kind"] = kind
    details["summary_hash"] = sha256_hex(canonical_json(summary))
    return Verification(ok=True, reason=None, details=details)


def audit_snapshot_export(export: object, *, expected_summary_hash: str) -> Verification:
    """Recompute a snapshot from its complete leaf lists.

    Checks the key sets (``UNSUPPORTED_FORMAT``, ``MALFORMED_EXPORT``), the summary
    (``MALFORMED_SUMMARY``) and its hash (``SUMMARY_HASH_MISMATCH``); then, for each tree,
    that the export has exactly the context the summary implies
    (``LIABILITIES_CONTEXT_MISMATCH``, ``RECEIVABLES_CONTEXT_MISMATCH``), that it passes the
    tree audit against the summary's root (the tree reason with the prefix ``LIABILITIES_``
    or ``RECEIVABLES_``) and that total and count are those stated in the summary
    (``..._HEAD_MISMATCH``); and finally that no slot id appears in both trees
    (``SLOT_IN_BOTH_TREES``).

    A malformed ``expected_summary_hash`` raises ``ValueError``; no content of ``export``
    makes the function raise.
    """
    if not is_hash_text(expected_summary_hash):
        raise ValueError("expected_summary_hash must be 64 lower-case hexadecimal characters")
    try:
        return _audit_snapshot_export(export, expected_summary_hash)
    except Exception as exc:  # last line of defence: fail closed, never raise on content
        return _fail("MALFORMED_EXPORT", f"unexpected {type(exc).__name__}")


def _audit_snapshot_export(export: object, expected_summary_hash: str) -> Verification:
    if type(export) is not dict or export.get("format") != FORMAT_SNAPSHOT_EXPORT:
        return _fail("UNSUPPORTED_FORMAT", f"not a document of format {FORMAT_SNAPSHOT_EXPORT}")
    try:
        canonical_json(export)
    except ExactValueError:
        return _fail("MALFORMED_EXPORT", "the document does not fit the canonical value profile")
    if set(export) != _EXPORT_KEYS:
        return _fail("MALFORMED_EXPORT", "the export does not have exactly its four keys")
    summary = export["summary"]
    try:
        parsed = _parse_summary(summary)
    except SumTreeError as exc:
        return _fail("MALFORMED_SUMMARY", str(exc))
    summary_hash = sha256_hex(canonical_json(summary))
    if summary_hash != expected_summary_hash:
        return _fail("SUMMARY_HASH_MISMATCH", "the summary in the export is not the published one")

    slots: dict[str, set[str]] = {}
    totals: dict[str, int] = {}
    counts: dict[str, int] = {}
    for kind in SNAPSHOT_KINDS:
        prefix = kind.upper() + "_"
        tree_export = export[kind]
        root, total, leaf_count = parsed["heads"][kind]
        if type(tree_export) is dict and type(tree_export.get("context")) is dict:
            if tree_export["context"] != parsed["contexts"][kind].to_document():
                return _fail(prefix + "CONTEXT_MISMATCH", "the tree export has another context than the summary")
        result = audit_sum_tree_export(tree_export, expected_root=root)
        if not result.ok:
            return Verification(ok=False, reason=prefix + str(result.reason), details=result.details)
        if (result.details["total"], result.details["leaf_count"]) != (total, leaf_count):
            return _fail(prefix + "HEAD_MISMATCH", "total or leaf count differ from the summary")
        slots[kind] = {leaf["slot_id"] for leaf in tree_export["leaves"]}
        totals[kind], counts[kind] = total, leaf_count
    if slots["liabilities"] & slots["receivables"]:
        return _fail("SLOT_IN_BOTH_TREES", "a slot id appears in the liabilities and in the receivables tree")
    return Verification(
        ok=True,
        reason=None,
        details={
            "summary_hash": summary_hash,
            "liabilities_total": totals["liabilities"],
            "liabilities_leaf_count": counts["liabilities"],
            "receivables_total": totals["receivables"],
            "receivables_leaf_count": counts["receivables"],
        },
    )
