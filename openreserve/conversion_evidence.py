"""conversion_evidence.py — evidence for a declared conversion, and its three checks.

For one declaration (``openreserve/conversion.py``) the evidence consists of six Merkle-sum
trees and three documents:

==================== ========================== ======== ============ ================
tree                 kind                       currency instant      binding
==================== ========================== ======== ============ ================
pre, liabilities     ``liabilities``            source   cut-off      none
pre, receivables     ``receivables``            source   cut-off      none
records, liabilities ``conversion-liabilities`` target   effective    declaration hash
records, receivables ``conversion-receivables`` target   effective    declaration hash
post, liabilities    ``liabilities``            target   effective    none
post, receivables    ``receivables``            target   effective    none
==================== ========================== ======== ============ ================

The pre and post trees are ordinary liabilities snapshots. The records tree has one leaf per
pre leaf: its slot is the source account, its amount the converted amount, and its payload
the canonical JSON text ``{"source_amount": ..., "target_slot_id": ...}``. It binds the
mapping from each source to its target and amount to a published root, and its total binds
the sum of the converted amounts.

* The **manifest** (``openreserve.conversion.manifest/1``) holds the declaration, the six
  heads and the reconciliation figures. Anyone can check its arithmetic:
  :func:`verify_conversion_manifest`.
* A **holder bundle** (``openreserve.conversion.holder-bundle/1``) holds three inclusion
  proofs. A holder checks that their balance at the cut-off, the declared rate and rounding
  rule, and their converted balance fit together: :func:`verify_conversion_bundle`.
* The **auditor export** (``openreserve.conversion.export/1``) holds every leaf. An auditor
  recomputes every tree and figure of the export: :func:`audit_conversion_export`.

What these checks do not show: that every balance was recorded, that every holder was given
the same manifest, that an identifier belongs to one holder only, that the clearing and
residue accounts and the closing amount named in the manifest are the ones of the ledger
(only rebuilding the evidence from the ledger shows that), or anything about reserves. There
is no signature in these formats. The checks never raise on the content of what they verify;
they return a :class:`~openreserve.transparency.sumtree.Verification`.

The specification is docs/hypothetical/W_CHANGEOVER_DESIGN.md, sections 8.5 and 8.6.
"""

from __future__ import annotations

import json
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from openreserve.conversion import (
    ConversionDeclaration,
    ConversionError,
    ConversionIncompleteError,
    DeclarationError,
    _as_declaration,
    _posted_conversions,
    convert,
    fraction_from_document,
    fraction_to_document,
    read_closing,
    round_fraction,
)
from openreserve.core.exact import (
    AMOUNT_MAX,
    ExactValueError,
    amount_from_text,
    amount_to_text,
    canonical_json,
    instant_from_text,
    instant_to_text,
    sha256_hex,
)
from openreserve.core.ledger import Ledger
from openreserve.core.types import IntegrityError, OwnerType
from openreserve.transparency.sumtree import (
    FORMAT_EXPORT,
    FORMAT_INCLUSION,
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
    "BEARER_INSTITUTION",
    "FORMAT_BUNDLE",
    "FORMAT_CONVERSION_EXPORT",
    "FORMAT_MANIFEST",
    "SCOPE_CONVERSION_ONLY",
    "SCOPE_MIXED",
    "ConversionEvidence",
    "assemble_conversion_evidence",
    "audit_conversion_export",
    "build_conversion_evidence",
    "verify_conversion_bundle",
    "verify_conversion_manifest",
]

FORMAT_MANIFEST = "openreserve.conversion.manifest/1"
FORMAT_BUNDLE = "openreserve.conversion.holder-bundle/1"
FORMAT_CONVERSION_EXPORT = "openreserve.conversion.export/1"

SCOPE_CONVERSION_ONLY = "CONVERSION_ONLY"
SCOPE_MIXED = "MIXED"
BEARER_INSTITUTION = "INSTITUTION"

SIDES = ("liabilities", "receivables")
_STAGES = ("pre", "conversion", "post")

_MANIFEST_KEYS = frozenset(
    {
        "format",
        "institution_id",
        "declaration",
        "declaration_hash",
        "effective_at",
        "accounts",
        "liabilities",
        "receivables",
        "post",
        "ledger_closing",
    }
)
_ACCOUNT_KEYS = frozenset({"clearing_source", "clearing_target", "residue"})
_SIDE_KEYS = frozenset(
    {
        "pre",
        "conversion",
        "converted_nonzero_count",
        "exact_total",
        "rounded_exact_total",
        "reconciliation_difference",
        "sum_of_residues",
    }
)
_POST_KEYS = frozenset({"scope", "liabilities", "receivables"})
_CLOSING_KEYS = frozenset({"bearer", "residue_account_change"})
_BUNDLE_KEYS = frozenset({"format", "manifest_hash", "side", "pre_proof", "record_proof", "post_proof"})
_EXPORT_KEYS = frozenset({"format", "manifest", "liabilities", "receivables"})
_EXPORT_SIDE_KEYS = frozenset(_STAGES)
_RECORD_KEYS = frozenset({"source_amount", "target_slot_id"})


def _fail(reason: str, message: str = "", **extra: Any) -> Verification:
    details: dict[str, Any] = dict(extra)
    if message:
        details["message"] = message
    return Verification(ok=False, reason=reason, details=details)


def _prefixed(prefix: str, result: Verification, **extra: Any) -> Verification:
    details = dict(result.details)
    details.update(extra)
    return Verification(ok=False, reason=prefix + str(result.reason), details=details)


def _record_payload(source_magnitude: int, target_slot_id: str) -> str:
    """The canonical JSON text of one conversion record."""
    return canonical_json(
        {"source_amount": amount_to_text(source_magnitude), "target_slot_id": target_slot_id}
    ).decode("utf-8")


def _parse_record_payload(payload: object) -> tuple[int, str] | None:
    """``(source magnitude, target slot id)`` of a canonical record payload, else ``None``."""
    if type(payload) is not str:
        return None
    try:
        parsed = json.loads(payload)
        if type(parsed) is not dict or set(parsed) != _RECORD_KEYS:
            return None
        if canonical_json(parsed).decode("utf-8") != payload:
            return None
        source_amount = amount_from_text(parsed["source_amount"], signed=False)
        check_identifier(parsed["target_slot_id"], "target_slot_id")
    except (ValueError, TypeError, RecursionError):
        return None
    return source_amount, parsed["target_slot_id"]


def _contexts(declaration: ConversionDeclaration, institution_id: str, effective_at: str) -> dict[str, dict[str, SumTreeContext]]:
    """The six tree contexts of a conversion, by side and stage."""
    binding = declaration.declaration_hash_bytes
    result: dict[str, dict[str, SumTreeContext]] = {}
    for side in SIDES:
        result[side] = {
            "pre": SumTreeContext(side, institution_id, declaration.source.code, declaration.source.minor_units, declaration.cutoff_at),
            "conversion": SumTreeContext(
                "conversion-" + side, institution_id, declaration.target.code, declaration.target.minor_units, effective_at, binding
            ),
            "post": SumTreeContext(side, institution_id, declaration.target.code, declaration.target.minor_units, effective_at),
        }
    return result


# ---------------------------------------------------------------------------
# The evidence object
# ---------------------------------------------------------------------------


class ConversionEvidence:
    """The evidence of one conversion: manifest, holder bundles and the auditor export."""

    def __init__(
        self,
        *,
        manifest: dict[str, Any],
        trees: Mapping[str, Mapping[str, SumTree]],
        targets: Mapping[str, tuple[str, str, int]],
    ) -> None:
        self._manifest = manifest
        self._manifest_hash = sha256_hex(canonical_json(manifest))
        self._trees = trees
        self._targets = dict(targets)  # source slot -> (side, target slot, converted magnitude)

    @property
    def manifest(self) -> dict[str, Any]:
        """The manifest document. Publish it; its hash identifies the conversion evidence."""
        return json.loads(canonical_json(self._manifest).decode("utf-8"))

    @property
    def manifest_hash(self) -> str:
        """SHA-256 of the canonical JSON of the manifest."""
        return self._manifest_hash

    def tree(self, side: str, stage: str) -> SumTree:
        """One of the six trees: ``side`` is liabilities or receivables, ``stage`` is pre,
        conversion or post."""
        return self._trees[side][stage]

    def source_slot_ids(self) -> tuple[str, ...]:
        """The source accounts that have a record, in ascending order."""
        return tuple(sorted(self._targets))

    def bundle_for(self, source_account_id: str) -> dict[str, Any]:
        """The holder bundle of one source account: three inclusion proofs.

        Raises ``KeyError`` when the account has no record (its balance at the cut-off was
        zero, or it is not a customer account of the source currency). The proofs contain
        the account's salts: deliver the bundle privately.
        """
        side, target_slot_id, converted = self._targets[source_account_id]
        trees = self._trees[side]
        return {
            "format": FORMAT_BUNDLE,
            "manifest_hash": self._manifest_hash,
            "side": side,
            "pre_proof": trees["pre"].proof_for(source_account_id),
            "record_proof": trees["conversion"].proof_for(source_account_id),
            "post_proof": trees["post"].proof_for(target_slot_id) if converted != 0 else None,
        }

    def auditor_export(self) -> dict[str, Any]:
        """Manifest plus every leaf of the six trees. For auditors, not for publication."""
        export: dict[str, Any] = {"format": FORMAT_CONVERSION_EXPORT, "manifest": self.manifest}
        for side in SIDES:
            export[side] = {stage: self._trees[side][stage].export() for stage in _STAGES}
        return export


def _evidence_from_parts(
    declaration: ConversionDeclaration,
    *,
    institution_id: str,
    salt_key: bytes,
    effective_at: str,
    accounts: Mapping[str, str],
    records: Iterable[tuple[str, int, str, int]],
    post_balances: Iterable[tuple[str, int]],
    scope: str,
    residue_account_change: int,
) -> ConversionEvidence:
    """Build the trees and the manifest from given records ``(source, x, target, y)``.

    The converted amounts are taken as given here. The public functions compute them from
    the declaration before they call this one; it exists separately so that tests can build
    evidence for a conversion that was carried out wrongly.
    """
    contexts = _contexts(declaration, institution_id, effective_at)
    leaves: dict[str, dict[str, list[tuple]]] = {side: {stage: [] for stage in _STAGES} for side in SIDES}
    targets: dict[str, tuple[str, str, int]] = {}
    nonzero: dict[str, int] = {side: 0 for side in SIDES}
    for source_slot_id, x, target_slot_id, y in records:
        if x == 0:
            continue
        side = "liabilities" if x > 0 else "receivables"
        if (y > 0 and x < 0) or (y < 0 and x > 0):
            raise ConversionError("a converted amount cannot have the opposite sign of its source amount")
        magnitude_x, magnitude_y = abs(x), abs(y)
        leaves[side]["pre"].append((source_slot_id, magnitude_x))
        leaves[side]["conversion"].append((source_slot_id, magnitude_y, _record_payload(magnitude_x, target_slot_id)))
        targets[source_slot_id] = (side, target_slot_id, magnitude_y)
        nonzero[side] += magnitude_y != 0
    for slot_id, balance in post_balances:
        if balance > 0:
            leaves["liabilities"]["post"].append((slot_id, balance))
        elif balance < 0:
            leaves["receivables"]["post"].append((slot_id, -balance))

    trees = {
        side: {stage: SumTree.build(contexts[side][stage], leaves[side][stage], salt_key=salt_key) for stage in _STAGES}
        for side in SIDES
    }

    manifest: dict[str, Any] = {
        "format": FORMAT_MANIFEST,
        "institution_id": institution_id,
        "declaration": declaration.to_document(),
        "declaration_hash": declaration.declaration_hash,
        "effective_at": effective_at,
        "accounts": {key: accounts[key] for key in sorted(_ACCOUNT_KEYS)},
        "post": {"scope": scope},
        "ledger_closing": {"bearer": BEARER_INSTITUTION, "residue_account_change": amount_to_text(residue_account_change)},
    }
    factor = declaration.factor
    for side in SIDES:
        pre, conversion, post = (trees[side][stage] for stage in _STAGES)
        exact_total = factor * pre.total
        rounded_exact_total = round_fraction(exact_total, declaration.rounding_mode)
        manifest[side] = {
            "pre": pre.head(),
            "conversion": conversion.head(),
            "converted_nonzero_count": amount_to_text(nonzero[side]),
            "exact_total": fraction_to_document(exact_total),
            "rounded_exact_total": amount_to_text(rounded_exact_total),
            "reconciliation_difference": amount_to_text(conversion.total - rounded_exact_total),
            "sum_of_residues": fraction_to_document(conversion.total - exact_total),
        }
        manifest["post"][side] = post.head()
    return ConversionEvidence(manifest=manifest, trees=trees, targets=targets)


def _check_evidence_inputs(
    declaration: ConversionDeclaration, institution_id: object, salt_key: object, effective_at: "datetime", accounts: object
) -> str:
    try:
        check_identifier(institution_id, "institution_id")
        check_salt_key(salt_key)
        effective_text = instant_to_text(effective_at)
    except ExactValueError as exc:
        raise ConversionError(str(exc)) from None
    if not instant_from_text(effective_text) > declaration.cutoff:
        raise ConversionError("effective_at must be later than the cut-off of the declaration")
    if not isinstance(accounts, Mapping) or set(accounts) != _ACCOUNT_KEYS:
        raise ConversionError("accounts must name exactly clearing_source, clearing_target and residue")
    try:
        for key in _ACCOUNT_KEYS:
            check_identifier(accounts[key], f"accounts[{key!r}]")
    except SumTreeError as exc:
        raise ConversionError(str(exc)) from None
    if len(set(accounts.values())) != 3:
        raise ConversionError("the clearing accounts and the residue account must be three different accounts")
    return effective_text


def assemble_conversion_evidence(
    declaration: ConversionDeclaration | dict,
    *,
    institution_id: str,
    salt_key: bytes,
    effective_at: "datetime",
    accounts: Mapping[str, str],
    conversions: Iterable[tuple[str, int, str]],
    other_target_balances: Iterable[tuple[str, int]] = (),
    residue_account_change: int = 0,
) -> ConversionEvidence:
    """Build the evidence of a conversion from plain data. No ledger is needed.

    ``conversions`` lists ``(source slot id, signed source amount, target slot id)`` for
    each customer account; an amount of 0 has no record. Every converted amount is computed
    here from the declaration: the function never takes one as input.
    ``other_target_balances`` lists ``(slot id, signed amount)`` of customer accounts in the
    target currency that are not targets of this conversion; if any is non-zero the scope of
    the post trees is ``MIXED``, otherwise ``CONVERSION_ONLY``. ``accounts`` names the
    ``clearing_source``, ``clearing_target`` and ``residue`` accounts used in the ledger, and
    ``residue_account_change`` is the amount the closing booked to the residue account.
    """
    declaration = _as_declaration(declaration)
    effective_text = _check_evidence_inputs(declaration, institution_id, salt_key, effective_at, accounts)
    if type(residue_account_change) is not int or abs(residue_account_change) > AMOUNT_MAX:
        raise ConversionError("residue_account_change must be an int within 10**38 - 1 in magnitude")

    records: list[tuple[str, int, str, int]] = []
    post_balances: list[tuple[str, int]] = []
    sources: set[str] = set()
    target_slots: set[str] = set()
    try:
        for item in conversions:
            if not isinstance(item, (tuple, list)) or len(item) != 3:
                raise ConversionError("each conversion must be (source slot id, signed source amount, target slot id)")
            source_slot_id, x, target_slot_id = item
            check_identifier(source_slot_id, "source slot id")
            check_identifier(target_slot_id, "target slot id")
            if source_slot_id in sources:
                raise ConversionError(f"source {source_slot_id!r} is listed twice")
            if target_slot_id in target_slots:
                raise ConversionError(f"target {target_slot_id!r} is listed twice")
            sources.add(source_slot_id)
            target_slots.add(target_slot_id)
            y = convert(declaration, x).converted_amount
            records.append((source_slot_id, x, target_slot_id, y))
            if y != 0:
                post_balances.append((target_slot_id, y))
        mixed = False
        for item in other_target_balances:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ConversionError("each other balance must be (slot id, signed amount)")
            slot_id, balance = item
            check_identifier(slot_id, "slot id")
            if type(balance) is not int or abs(balance) > AMOUNT_MAX:
                raise ConversionError("a balance must be an int within 10**38 - 1 in magnitude")
            if slot_id in target_slots:
                raise ConversionError(f"{slot_id!r} is a target of this conversion or is listed twice")
            target_slots.add(slot_id)
            post_balances.append((slot_id, balance))
            mixed = mixed or balance != 0
        return _evidence_from_parts(
            declaration,
            institution_id=institution_id,
            salt_key=salt_key,
            effective_at=effective_text,
            accounts=accounts,
            records=records,
            post_balances=post_balances,
            scope=SCOPE_MIXED if mixed else SCOPE_CONVERSION_ONLY,
            residue_account_change=residue_account_change,
        )
    except ConversionError:
        raise
    except ExactValueError as exc:  # a tree or an encoding refused a value
        raise ConversionError(str(exc)) from None


def build_conversion_evidence(
    ledger: Ledger,
    declaration: ConversionDeclaration | dict,
    *,
    institution_id: str,
    salt_key: bytes,
    clearing_source_account_id: str,
    clearing_target_account_id: str,
    residue_account_id: str,
    effective_at: "datetime",
) -> ConversionEvidence:
    """Build the evidence of a closed conversion from the ledger.

    Requires a closed conversion (``close_conversion``), and that every customer account
    with a non-zero balance at the cut-off has its conversion transaction with the right
    amounts (:class:`ConversionIncompleteError` otherwise). It also requires that each target
    account holds exactly its converted amount at ``effective_at``. Generation is
    deterministic: running it again on the same ledger gives the same manifest hash.

    ``effective_at`` is the instant of the post snapshot, and the manifest states it. It is
    not tied to the instant at which the conversions were posted: the evidence builds for any
    instant after the cut-off at which every account of the source currency is at zero and
    every target holds exactly its converted amount, so a later instant can be used as long
    as nothing has changed those balances.
    """
    declaration = _as_declaration(declaration)
    accounts = {
        "clearing_source": clearing_source_account_id,
        "clearing_target": clearing_target_account_id,
        "residue": residue_account_id,
    }
    _check_evidence_inputs(declaration, institution_id, salt_key, effective_at, accounts)
    residue_account_change, closed = read_closing(
        ledger,
        declaration,
        clearing_source_account_id=clearing_source_account_id,
        clearing_target_account_id=clearing_target_account_id,
        residue_account_id=residue_account_id,
        effective_at=effective_at,
    )
    if not closed:
        raise ConversionIncompleteError("the conversion is not closed: call close_conversion first")

    posted = _posted_conversions(ledger, declaration, clearing_source_account_id, clearing_target_account_id)
    conversions: list[tuple[str, int, str]] = []
    targets: set[str] = set()
    for account in ledger.list_accounts(owner_type=OwnerType.USER):
        if account.currency.code != declaration.source.code:
            continue
        x = ledger.balance(account.account_id, as_of=declaration.cutoff).cents
        done = posted.get(account.account_id)
        if x == 0:
            if done is not None:
                raise ConversionError(f"account {account.account_id!r} has a conversion but no balance at the cut-off")
            continue
        if done is None or done.source_amount != x:
            raise ConversionIncompleteError(
                f"account {account.account_id!r} has no conversion transaction for its balance at the cut-off"
            )
        try:
            target = ledger.get_account(done.target_account_id)
        except IntegrityError:
            raise ConversionError(f"the target account of {account.account_id!r} does not exist") from None
        if target.owner_type is not OwnerType.USER or target.currency.code != declaration.target.code:
            raise ConversionError(f"the target of {account.account_id!r} is not a customer account of the target currency")
        if ledger.balance(done.target_account_id, as_of=effective_at).cents != done.target_amount:
            raise ConversionError(
                f"target account {done.target_account_id!r} does not hold exactly its converted amount at the effective instant"
            )
        if done.target_account_id in targets:
            raise ConversionError(f"target account {done.target_account_id!r} is used by two conversions")
        targets.add(done.target_account_id)
        conversions.append((account.account_id, x, done.target_account_id))

    others: list[tuple[str, int]] = []
    for account in ledger.list_accounts(owner_type=OwnerType.USER):
        if account.currency.code != declaration.target.code or account.account_id in targets:
            continue
        balance = ledger.balance(account.account_id, as_of=effective_at).cents
        if balance != 0:
            others.append((account.account_id, balance))

    return assemble_conversion_evidence(
        declaration,
        institution_id=institution_id,
        salt_key=salt_key,
        effective_at=effective_at,
        accounts=accounts,
        conversions=conversions,
        other_target_balances=others,
        residue_account_change=residue_account_change,
    )


# ---------------------------------------------------------------------------
# Check 1: anyone, from the manifest alone
# ---------------------------------------------------------------------------


class _Malformed(Exception):
    """Private to this module: a document breaks a shape or field rule."""


def _signed(value: object) -> int:
    try:
        return amount_from_text(value)  # type: ignore[arg-type]
    except ExactValueError:
        raise _Malformed("expected canonical signed decimal text") from None


def _unsigned(value: object) -> int:
    try:
        return amount_from_text(value, signed=False)  # type: ignore[arg-type]
    except ExactValueError:
        raise _Malformed("expected canonical unsigned decimal text") from None


def _fraction(value: object) -> Fraction:
    try:
        return fraction_from_document(value)
    except ConversionError as exc:
        raise _Malformed(str(exc)) from None


def _head(value: object) -> tuple[str, int, int]:
    try:
        return parse_head(value)
    except SumTreeError as exc:
        raise _Malformed(str(exc)) from None


def _parse_manifest(manifest: object) -> dict[str, Any]:
    """Shape and field rules of a manifest. Raises ``_Malformed``; returns the parsed parts."""
    if type(manifest) is not dict:
        raise _Malformed("a manifest must be an object")
    try:
        canonical_json(manifest)
    except ExactValueError as exc:
        raise _Malformed(f"the manifest does not fit the canonical value profile: {exc}") from None
    if set(manifest) != _MANIFEST_KEYS:
        raise _Malformed("a manifest must have exactly its ten keys")
    if manifest["format"] != FORMAT_MANIFEST:
        raise _Malformed(f"a manifest must have the format {FORMAT_MANIFEST}")
    try:
        declaration = ConversionDeclaration.from_document(manifest["declaration"])
    except DeclarationError as exc:
        raise _Malformed(f"declaration: {exc}") from None
    if not is_hash_text(manifest["declaration_hash"]):
        raise _Malformed("declaration_hash must be 64 lower-case hexadecimal characters")
    try:
        check_identifier(manifest["institution_id"], "institution_id")
        effective = instant_from_text(manifest["effective_at"])
        accounts = manifest["accounts"]
        if type(accounts) is not dict or set(accounts) != _ACCOUNT_KEYS:
            raise _Malformed("accounts must have exactly clearing_source, clearing_target and residue")
        for key in _ACCOUNT_KEYS:
            check_identifier(accounts[key], key)
        if len(set(accounts.values())) != 3:
            raise _Malformed("the clearing accounts and the residue account must be three different accounts")
    except ExactValueError as exc:
        raise _Malformed(str(exc)) from None

    sides: dict[str, dict[str, Any]] = {}
    for side in SIDES:
        block = manifest[side]
        if type(block) is not dict or set(block) != _SIDE_KEYS:
            raise _Malformed(f"{side} must have exactly its seven keys")
        exact_total = _fraction(block["exact_total"])
        if exact_total < 0:
            raise _Malformed("exact_total cannot be negative")
        sides[side] = {
            "pre": _head(block["pre"]),
            "conversion": _head(block["conversion"]),
            "converted_nonzero_count": _unsigned(block["converted_nonzero_count"]),
            "exact_total": exact_total,
            "rounded_exact_total": _unsigned(block["rounded_exact_total"]),
            "reconciliation_difference": _signed(block["reconciliation_difference"]),
            "sum_of_residues": _fraction(block["sum_of_residues"]),
        }
    post = manifest["post"]
    if type(post) is not dict or set(post) != _POST_KEYS:
        raise _Malformed("post must have exactly scope, liabilities and receivables")
    if type(post["scope"]) is not str or post["scope"] not in (SCOPE_CONVERSION_ONLY, SCOPE_MIXED):
        raise _Malformed("post.scope must be CONVERSION_ONLY or MIXED")
    for side in SIDES:
        sides[side]["post"] = _head(post[side])
    closing = manifest["ledger_closing"]
    if type(closing) is not dict or set(closing) != _CLOSING_KEYS:
        raise _Malformed("ledger_closing must have exactly bearer and residue_account_change")
    if closing["bearer"] != BEARER_INSTITUTION:
        raise _Malformed("ledger_closing.bearer must be INSTITUTION in this format version")
    return {
        "declaration": declaration,
        "effective": effective,
        "sides": sides,
        "scope": post["scope"],
        "residue_account_change": _signed(closing["residue_account_change"]),
        "contexts": _contexts(declaration, manifest["institution_id"], manifest["effective_at"]),
    }


def _check_manifest(manifest: object) -> tuple[Verification, dict[str, Any] | None]:
    """The manifest check, returning the parsed manifest as well for the other two checks."""
    try:
        parsed = _parse_manifest(manifest)
    except _Malformed as exc:
        return _fail("MALFORMED_MANIFEST", str(exc)), None
    declaration: ConversionDeclaration = parsed["declaration"]
    assert isinstance(manifest, dict)
    if declaration.declaration_hash != manifest["declaration_hash"]:
        return _fail("DECLARATION_HASH_MISMATCH", "declaration_hash is not the hash of the declaration"), None
    if not parsed["effective"] > declaration.cutoff:
        return _fail("EFFECTIVE_NOT_AFTER_CUTOFF", "effective_at must be later than the cut-off"), None

    factor, mode = declaration.factor, declaration.rounding_mode
    for side in SIDES:
        block = parsed["sides"][side]
        _, pre_total, pre_count = block["pre"]
        _, conversion_total, conversion_count = block["conversion"]
        if conversion_count != pre_count or block["converted_nonzero_count"] > pre_count:
            return _fail("COUNT_MISMATCH", "the records tree and the pre tree must have the same leaf count", side=side), None
        exact_total = factor * pre_total
        if block["exact_total"] != exact_total or block["rounded_exact_total"] != round_fraction(exact_total, mode):
            return _fail("EXACT_TOTAL_MISMATCH", "exact_total is not factor times the pre total", side=side), None
        if (
            block["reconciliation_difference"] != conversion_total - block["rounded_exact_total"]
            or block["sum_of_residues"] != conversion_total - exact_total
        ):
            return _fail("RESIDUE_MISMATCH", "the residue figures do not follow from the records total", side=side), None
        if 2 * abs(block["sum_of_residues"]) > pre_count:
            return (
                _fail("RESIDUE_OUT_OF_BOUND", "each residue is at most one half, so their sum is at most half the leaf count", side=side),
                None,
            )
    for side in SIDES:
        block = parsed["sides"][side]
        _, post_total, post_count = block["post"]
        conversion_total = block["conversion"][1]
        nonzero = block["converted_nonzero_count"]
        if parsed["scope"] == SCOPE_CONVERSION_ONLY:
            consistent = post_total == conversion_total and post_count == nonzero
        else:
            consistent = post_total >= conversion_total and post_count >= nonzero
        if not consistent:
            return _fail("POST_TOTAL_MISMATCH", "the post tree does not account for the converted amounts", side=side), None

    details = {
        "manifest_hash": sha256_hex(canonical_json(manifest)),
        "declaration_hash": declaration.declaration_hash,
        "scope": parsed["scope"],
        "residue_account_change": parsed["residue_account_change"],
    }
    for side in SIDES:
        block = parsed["sides"][side]
        details[side] = {
            "pre_total": block["pre"][1],
            "converted_total": block["conversion"][1],
            "post_total": block["post"][1],
            "leaf_count": block["pre"][2],
            "exact_total": block["exact_total"],
            "reconciliation_difference": block["reconciliation_difference"],
            "sum_of_residues": block["sum_of_residues"],
        }
    return Verification(ok=True, reason=None, details=details), parsed


def verify_conversion_manifest(manifest: object) -> Verification:
    """Check a conversion manifest from the manifest alone.

    1. Shape, formats and field rules; the declaration is valid (``MALFORMED_MANIFEST``);
       its hash equals ``declaration_hash`` (``DECLARATION_HASH_MISMATCH``); ``effective_at``
       is later than the cut-off (``EFFECTIVE_NOT_AFTER_CUTOFF``).
    2. For each side: the pre and records trees have the same leaf count and
       ``converted_nonzero_count`` does not exceed it (``COUNT_MISMATCH``); ``exact_total``
       is ``factor * pre.total`` and ``rounded_exact_total`` its rounding
       (``EXACT_TOTAL_MISMATCH``); ``reconciliation_difference`` and ``sum_of_residues``
       follow from the records total (``RESIDUE_MISMATCH``); twice the magnitude of
       ``sum_of_residues`` does not exceed the leaf count (``RESIDUE_OUT_OF_BOUND``).
    3. Under ``CONVERSION_ONLY`` the post total equals the records total and the post leaf
       count equals ``converted_nonzero_count``; under ``MIXED`` neither is smaller
       (``POST_TOTAL_MISMATCH``).

    The roots themselves, the totals bound in them, and ``residue_account_change`` cannot be
    checked from the manifest alone: holders and auditors check the roots. Four fields are
    bound in no root, so this function checks only their form and neither the holder check
    nor the auditor's check covers them: ``accounts.clearing_source``,
    ``accounts.clearing_target``, ``accounts.residue`` and
    ``ledger_closing.residue_account_change``. They are checked only by rebuilding the
    evidence from the ledger with :func:`build_conversion_evidence` and comparing manifest
    hashes. No content of ``manifest`` makes the function raise.
    """
    try:
        return _check_manifest(manifest)[0]
    except Exception as exc:  # last line of defence: fail closed, never raise on content
        return _fail("MALFORMED_MANIFEST", f"unexpected {type(exc).__name__}")


# ---------------------------------------------------------------------------
# Check 2: a holder
# ---------------------------------------------------------------------------


def _verify_proof_in_tree(
    prefix: str,
    proof: object,
    context: SumTreeContext,
    head: tuple[str, int, int],
    expected_slot_id: str,
) -> Verification:
    """Context, inclusion and head of one proof; failure reasons carry ``prefix``."""
    if type(proof) is dict and proof.get("format") == FORMAT_INCLUSION and type(proof.get("context")) is dict:
        if proof["context"] != context.to_document():
            return _fail(prefix + "CONTEXT_MISMATCH", "the proof was made for another tree")
    root, total, leaf_count = head
    result = verify_sum_tree_inclusion(proof, expected_root=root, expected_slot_id=expected_slot_id)
    if not result.ok:
        return _prefixed(prefix, result)
    if result.details["context"] != context.to_document():
        return _fail(prefix + "CONTEXT_MISMATCH", "the proof was made for another tree")
    if (result.details["total"], result.details["leaf_count"]) != (total, leaf_count):
        return _fail(prefix + "HEAD_MISMATCH", "the manifest states another total or leaf count than the root binds")
    return result


def verify_conversion_bundle(
    bundle: object,
    manifest: object,
    *,
    expected_manifest_hash: str,
    expected_source_slot_id: str,
    expected_side: str,
) -> Verification:
    """Check a holder bundle against a manifest whose hash was published.

    ``expected_side`` is the side the holder expects to be on, from their own record:
    ``"liabilities"`` when the institution owed them their balance at the cut-off,
    ``"receivables"`` when they owed it. A bundle of side ``receivables`` says that the
    holder owes the converted amount, and its amounts are not part of the liabilities totals
    of the manifest.

    The manifest passes :func:`verify_conversion_manifest` (its reason is passed on) and its
    hash is the published one (``MANIFEST_HASH_MISMATCH``); the bundle has its keys
    (``MALFORMED_BUNDLE``) and names the same hash (``MANIFEST_HASH_MISMATCH``). Then, for
    the bundle's side:

    1. ``pre_proof`` has exactly the pre context (``PRE_CONTEXT_MISMATCH``) and verifies
       against the pre root for the holder's slot (tree reason with the prefix ``PRE_``).
       Its amount is ``x``.
    2. ``record_proof`` has exactly the records context, declaration hash included
       (``RECORD_CONTEXT_MISMATCH``), and verifies against the records root for the same
       slot (``RECORD_``). Its payload is a canonical record (``RECORD_PAYLOAD_MALFORMED``)
       whose ``source_amount`` equals ``x`` (``SOURCE_AMOUNT_MISMATCH``). Its amount equals
       ``Round(x * factor)`` recomputed from the declaration (``CONVERTED_AMOUNT_MISMATCH``).
    3. If that amount is 0, ``post_proof`` must be ``null`` (``POST_PROOF_UNEXPECTED``).
       Otherwise it must be present (``POST_PROOF_MISSING``), have exactly the post context
       (``POST_CONTEXT_MISMATCH``), verify against the post root for the record's
       ``target_slot_id`` (``POST_``) and state the same amount (``POST_AMOUNT_MISMATCH``).

    Each proof must also lead to a root whose total and leaf count are the ones the manifest
    states (``PRE_HEAD_MISMATCH``, ``RECORD_HEAD_MISMATCH``, ``POST_HEAD_MISMATCH``), so the
    figures of the manifest check are the figures bound in the roots.

    4. Last, the bundle's side is ``expected_side`` (``SIDE_MISMATCH``). Because this comes
       after the other checks, ``SIDE_MISMATCH`` means that the bundle is genuine for the
       other side of the published manifest; ``details`` then names that ``side``.

    On success ``details`` holds ``side``, ``source_slot_id``, ``source_amount``,
    ``converted_amount``, the exact fractions ``exact`` and ``residue``, ``target_slot_id``
    and ``manifest_hash``. Every figure is a magnitude: on the side ``receivables`` they are
    the magnitudes of the negative values that :func:`openreserve.conversion.convert` gives
    for ``x``, and the sign is the side. The holder still has to recognise the source and
    target identifiers as their own, and to compare ``source_amount`` with their own record
    of their balance.

    A malformed ``expected_manifest_hash`` or ``expected_source_slot_id``, or an
    ``expected_side`` that is not one of the two sides, raises ``ValueError``; no content of
    ``bundle`` or ``manifest`` makes the function raise.
    """
    if not is_hash_text(expected_manifest_hash):
        raise ValueError("expected_manifest_hash must be 64 lower-case hexadecimal characters")
    try:
        check_identifier(expected_source_slot_id, "expected_source_slot_id")
    except SumTreeError as exc:
        raise ValueError(str(exc)) from None
    if type(expected_side) is not str or expected_side not in SIDES:
        raise ValueError(f"expected_side must be one of {SIDES}")
    try:
        return _verify_bundle(bundle, manifest, expected_manifest_hash, expected_source_slot_id, expected_side)
    except Exception as exc:  # last line of defence: fail closed, never raise on content
        return _fail("MALFORMED_BUNDLE", f"unexpected {type(exc).__name__}")


def _verify_bundle(
    bundle: object, manifest: object, expected_manifest_hash: str, expected_source_slot_id: str, expected_side: str
) -> Verification:
    result, parsed = _check_manifest(manifest)
    if not result.ok or parsed is None:
        return result
    manifest_hash = result.details["manifest_hash"]
    if manifest_hash != expected_manifest_hash:
        return _fail("MANIFEST_HASH_MISMATCH", "the manifest is not the published one")

    if type(bundle) is not dict:
        return _fail("MALFORMED_BUNDLE", "a bundle must be an object")
    try:
        canonical_json(bundle)
    except ExactValueError:
        return _fail("MALFORMED_BUNDLE", "the bundle does not fit the canonical value profile")
    if set(bundle) != _BUNDLE_KEYS or bundle["format"] != FORMAT_BUNDLE:
        return _fail("MALFORMED_BUNDLE", f"a bundle must have exactly its six keys and the format {FORMAT_BUNDLE}")
    side = bundle["side"]
    if type(side) is not str or side not in SIDES:
        return _fail("MALFORMED_BUNDLE", "side must be liabilities or receivables")
    if bundle["manifest_hash"] != manifest_hash:
        return _fail("MANIFEST_HASH_MISMATCH", "the bundle was made for another manifest")

    declaration: ConversionDeclaration = parsed["declaration"]
    contexts = parsed["contexts"][side]
    heads = parsed["sides"][side]

    # 1. the balance at the cut-off
    pre = _verify_proof_in_tree("PRE_", bundle["pre_proof"], contexts["pre"], heads["pre"], expected_source_slot_id)
    if not pre.ok:
        return pre
    x = pre.details["amount"]

    # 2. the conversion record
    record = _verify_proof_in_tree("RECORD_", bundle["record_proof"], contexts["conversion"], heads["conversion"], expected_source_slot_id)
    if not record.ok:
        return record
    payload = _parse_record_payload(record.details["payload"])
    if payload is None:
        return _fail("RECORD_PAYLOAD_MALFORMED", "the record payload is not a canonical conversion record")
    recorded_source_amount, target_slot_id = payload
    if recorded_source_amount != x:
        return _fail("SOURCE_AMOUNT_MISMATCH", "the record does not repeat the balance at the cut-off")
    converted = convert(declaration, x)
    y = record.details["amount"]
    if y != converted.converted_amount:
        return _fail(
            "CONVERTED_AMOUNT_MISMATCH",
            "the recorded amount is not the declared rate and rounding rule applied to the balance",
            expected=converted.converted_amount,
            recorded=y,
        )

    # 3. the balance after the conversion
    post_proof = bundle["post_proof"]
    if y == 0:
        if post_proof is not None:
            return _fail("POST_PROOF_UNEXPECTED", "an amount of 0 has no leaf in the post tree")
    else:
        if post_proof is None:
            return _fail("POST_PROOF_MISSING", "a converted amount needs its post proof")
        post = _verify_proof_in_tree("POST_", post_proof, contexts["post"], heads["post"], target_slot_id)
        if not post.ok:
            return post
        if post.details["amount"] != y:
            return _fail("POST_AMOUNT_MISMATCH", "the post balance is not the converted amount")

    # 4. the side the holder expects
    if side != expected_side:
        return _fail(
            "SIDE_MISMATCH",
            f"the bundle is genuine for the {side} side of the manifest, not for the {expected_side} side",
            side=side,
        )

    return Verification(
        ok=True,
        reason=None,
        details={
            "side": side,
            "source_slot_id": expected_source_slot_id,
            "source_amount": x,
            "converted_amount": y,
            "exact": converted.exact,
            "residue": converted.residue,
            "target_slot_id": target_slot_id,
            "manifest_hash": manifest_hash,
        },
    )


# ---------------------------------------------------------------------------
# Check 3: an auditor
# ---------------------------------------------------------------------------


def audit_conversion_export(export: object, *, expected_manifest_hash: str) -> Verification:
    """Recompute a conversion from its complete leaf lists.

    The export has its keys (``MALFORMED_EXPORT``); the manifest passes
    :func:`verify_conversion_manifest` (its reason is passed on) and has the published hash
    (``MANIFEST_HASH_MISMATCH``). Each of the six tree exports has exactly its context
    (``PRE_``, ``CONVERSION_`` or ``POST_`` + ``CONTEXT_MISMATCH``), passes the tree audit
    (the tree reason with the same prefix) and gives the head stated in the manifest
    (``..._HEAD_MISMATCH``). No slot is in both pre trees or in both post trees
    (``PRE_SLOT_IN_BOTH_TREES``, ``POST_SLOT_IN_BOTH_TREES``). The records and the pre
    leaves of a side have the same slots (``RECORDS_DO_NOT_MATCH_PRE_SNAPSHOT``); each record
    payload is canonical (``RECORD_PAYLOAD_MALFORMED``), repeats the pre amount
    (``SOURCE_AMOUNT_MISMATCH``) and its leaf carries ``Round(x * factor)``
    (``CONVERTED_AMOUNT_MISMATCH``); no target appears twice across both sides
    (``TARGET_NOT_UNIQUE``); each record with a non-zero amount has its post leaf with that
    amount (``POST_AMOUNT_MISMATCH``); under ``CONVERSION_ONLY`` the post trees have no other
    leaf (``POST_HAS_UNCONVERTED_LEAVES``); and ``converted_nonzero_count`` is the number of
    records with a non-zero amount (``COUNT_MISMATCH``).

    A malformed ``expected_manifest_hash`` raises ``ValueError``; no content of ``export``
    makes the function raise.
    """
    if not is_hash_text(expected_manifest_hash):
        raise ValueError("expected_manifest_hash must be 64 lower-case hexadecimal characters")
    try:
        return _audit_export(export, expected_manifest_hash)
    except Exception as exc:  # last line of defence: fail closed, never raise on content
        return _fail("MALFORMED_EXPORT", f"unexpected {type(exc).__name__}")


def _audit_export(export: object, expected_manifest_hash: str) -> Verification:
    if type(export) is not dict:
        return _fail("MALFORMED_EXPORT", "an export must be an object")
    try:
        canonical_json(export)
    except ExactValueError:
        return _fail("MALFORMED_EXPORT", "the export does not fit the canonical value profile")
    if set(export) != _EXPORT_KEYS or export["format"] != FORMAT_CONVERSION_EXPORT:
        return _fail("MALFORMED_EXPORT", f"an export must have exactly its four keys and the format {FORMAT_CONVERSION_EXPORT}")
    for side in SIDES:
        if type(export[side]) is not dict or set(export[side]) != _EXPORT_SIDE_KEYS:
            return _fail("MALFORMED_EXPORT", f"{side} must have exactly pre, conversion and post")

    result, parsed = _check_manifest(export["manifest"])
    if not result.ok or parsed is None:
        return result
    manifest_hash = result.details["manifest_hash"]
    if manifest_hash != expected_manifest_hash:
        return _fail("MANIFEST_HASH_MISMATCH", "the manifest in the export is not the published one")
    declaration: ConversionDeclaration = parsed["declaration"]

    # the six trees
    leaves: dict[str, dict[str, dict[str, dict[str, Any]]]] = {side: {} for side in SIDES}
    for side in SIDES:
        for stage in _STAGES:
            prefix = stage.upper() + "_"
            tree_export = export[side][stage]
            context = parsed["contexts"][side][stage]
            root, total, leaf_count = parsed["sides"][side][stage]
            if type(tree_export) is dict and tree_export.get("format") == FORMAT_EXPORT and type(tree_export.get("context")) is dict:
                if tree_export["context"] != context.to_document():
                    return _fail(prefix + "CONTEXT_MISMATCH", "the tree export has another context than the manifest implies", side=side)
            audited = audit_sum_tree_export(tree_export, expected_root=root)
            if not audited.ok:
                return _prefixed(prefix, audited, side=side)
            if audited.details["context"] != context.to_document():
                return _fail(prefix + "CONTEXT_MISMATCH", "the tree export has another context than the manifest implies", side=side)
            if (audited.details["total"], audited.details["leaf_count"]) != (total, leaf_count):
                return _fail(prefix + "HEAD_MISMATCH", "the manifest states another total or leaf count than the root binds", side=side)
            leaves[side][stage] = {leaf["slot_id"]: leaf for leaf in tree_export["leaves"]}

    for stage in ("pre", "post"):
        if set(leaves["liabilities"][stage]) & set(leaves["receivables"][stage]):
            return _fail(stage.upper() + "_SLOT_IN_BOTH_TREES", "a slot id is both a liability and a receivable")

    # the records, in passes: each kind of check covers every record before the next begins
    for side in SIDES:
        if set(leaves[side]["pre"]) != set(leaves[side]["conversion"]):
            return _fail("RECORDS_DO_NOT_MATCH_PRE_SNAPSHOT", "the records and the pre leaves have different slots", side=side)
    converted: dict[str, list[tuple[str, str, int]]] = {side: [] for side in SIDES}  # (source, target, y)
    for side in SIDES:
        pre, records = leaves[side]["pre"], leaves[side]["conversion"]
        for slot_id in sorted(records):
            record = records[slot_id]
            x = int(pre[slot_id]["amount"])
            y = int(record["amount"])
            payload = _parse_record_payload(record["payload"])
            if payload is None:
                return _fail("RECORD_PAYLOAD_MALFORMED", "a record payload is not a canonical conversion record", slot_id=slot_id)
            recorded_source_amount, target_slot_id = payload
            if recorded_source_amount != x:
                return _fail("SOURCE_AMOUNT_MISMATCH", "a record does not repeat the pre amount", slot_id=slot_id)
            if y != convert(declaration, x).converted_amount:
                return _fail("CONVERTED_AMOUNT_MISMATCH", "a record does not carry Round(x * factor)", slot_id=slot_id)
            converted[side].append((slot_id, target_slot_id, y))
    targets_seen: set[str] = set()
    for side in SIDES:
        for slot_id, target_slot_id, _y in converted[side]:
            if target_slot_id in targets_seen:
                return _fail("TARGET_NOT_UNIQUE", "two records point at one target", slot_id=slot_id)
            targets_seen.add(target_slot_id)
    accounted_for: dict[str, int] = {side: 0 for side in SIDES}
    for side in SIDES:
        post = leaves[side]["post"]
        for slot_id, target_slot_id, y in converted[side]:
            if y == 0:
                continue
            post_leaf = post.get(target_slot_id)
            if post_leaf is None or int(post_leaf["amount"]) != y:
                return _fail("POST_AMOUNT_MISMATCH", "a converted amount is not the post balance of its target", slot_id=slot_id)
            accounted_for[side] += 1
    if parsed["scope"] == SCOPE_CONVERSION_ONLY:
        for side in SIDES:
            if len(leaves[side]["post"]) != accounted_for[side]:
                return _fail(
                    "POST_HAS_UNCONVERTED_LEAVES", "the post tree holds leaves that are not targets of this conversion", side=side
                )
    for side in SIDES:
        if parsed["sides"][side]["converted_nonzero_count"] != accounted_for[side]:
            return _fail("COUNT_MISMATCH", "converted_nonzero_count is not the number of records with a non-zero amount", side=side)

    details = dict(result.details)
    details["record_count"] = sum(len(leaves[side]["conversion"]) for side in SIDES)
    return Verification(ok=True, reason=None, details=details)
