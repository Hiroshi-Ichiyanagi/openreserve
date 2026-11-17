"""transparency/sumtree.py — the Merkle-sum tree, format ``openreserve.sumtree/1``.

Every node carries a sum. A node hash binds both child hashes and both child sums, and a final
root hash binds the tree context, the leaf count and the total. One published value therefore
answers "which tree, how many leaves, what total". There is no padding and no duplication:
the shape is that of RFC 6962 section 2.1 for any number of leaves. Leaves are salted and
ordered by their hash, so the tree is a function of the set of leaves.

Byte layouts (``H`` is SHA-256, never truncated; ``lp(b)`` is a 4-byte big-endian length
followed by ``b``; ``u128`` is 16 bytes big-endian)::

    DOMAIN  = b"openreserve.sumtree.v1"
    context = H(DOMAIN || 0x03 || lp(kind) || lp(institution_id) || lp(currency)
                || u8(minor_units) || lp(as_of) || lp(binding))
    salt    = HMAC-SHA256(salt_key, DOMAIN || 0x10 || context || lp(slot_id))
    leaf    = H(DOMAIN || 0x00 || context || lp(slot_id) || u128(amount) || salt || lp(payload))
    node    = H(DOMAIN || 0x01 || left_hash || u128(left_sum) || right_hash || u128(right_sum))
    root    = H(DOMAIN || 0x02 || context || u64(leaf_count) || u128(total) || top_hash)

What an accepted inclusion proof shows: against one published root, the total bound in the
root is at least the sum of the amounts of all accepted proofs for different slot ids. It
does not show that an account nobody checked is in the tree, that every liability was
recorded, that every holder was given the same root, or anything about reserves.

What a proof discloses: for each step of the path, the hash and the exact sum of the sibling
subtree. When that subtree is a single leaf, the sum is another account's exact amount. A
proof never contains another account's identifier, salt or payload.

Verifiers here never raise on the content of what they verify; they return a
:class:`Verification`. The specification is docs/hypothetical/W_CHANGEOVER_DESIGN.md,
section 6.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from openreserve.core.exact import (
    AMOUNT_MAX,
    ExactValueError,
    amount_from_text,
    amount_to_text,
    amount_to_u128,
    canonical_json,
    instant_from_text,
)

__all__ = [
    "DOMAIN",
    "FORMAT_EXPORT",
    "FORMAT_INCLUSION",
    "KINDS",
    "SNAPSHOT_KINDS",
    "SumTree",
    "SumTreeContext",
    "SumTreeError",
    "SumTreeLeaf",
    "Verification",
    "audit_sum_tree_export",
    "check_identifier",
    "check_salt_key",
    "derive_salt",
    "is_hash_text",
    "parse_head",
    "verify_sum_tree_inclusion",
]

DOMAIN = b"openreserve.sumtree.v1"
FORMAT_INCLUSION = "openreserve.sumtree.inclusion/1"
FORMAT_EXPORT = "openreserve.sumtree.export/1"

SNAPSHOT_KINDS = ("liabilities", "receivables")
KINDS = SNAPSHOT_KINDS + ("conversion-liabilities", "conversion-receivables")

MAX_MINOR_UNITS = 30
MAX_LABEL_BYTES = 255
MAX_PAYLOAD_BYTES = 4096
MIN_SALT_KEY_BYTES = 32
_LEAF_COUNT_LIMIT = 2**64  # the count is written as u64 inside the root hash

_TYPE_LEAF = b"\x00"
_TYPE_NODE = b"\x01"
_TYPE_ROOT = b"\x02"
_TYPE_CONTEXT = b"\x03"
_TYPE_SALT = b"\x10"
_EMPTY_TOP = bytes(32)

_HEX64 = re.compile(r"[0-9a-f]{64}")
_CURRENCY_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,31}")

_CONTEXT_KEYS = frozenset({"kind", "institution_id", "currency", "minor_units", "as_of", "binding"})
_LEAF_KEYS = frozenset({"slot_id", "amount", "salt", "payload"})
_STEP_KEYS = frozenset({"hash", "sum"})
_PROOF_KEYS = frozenset({"format", "context", "leaf_count", "leaf_index", "leaf", "path"})
_EXPORT_KEYS = frozenset({"format", "context", "leaves"})


class SumTreeError(ExactValueError):
    """A context, leaf or key does not satisfy the rules of the sum tree."""


@dataclass(frozen=True)
class Verification:
    """Result of a check. Truthy exactly when ``ok`` is.

    ``reason`` is ``None`` on success and a reason code otherwise. ``details`` holds what was
    authenticated on success, and a short message on failure.
    """

    ok: bool
    reason: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok


def _fail(reason: str, message: str = "") -> Verification:
    return Verification(ok=False, reason=reason, details={"message": message} if message else {})


# ---------------------------------------------------------------------------
# Field rules
# ---------------------------------------------------------------------------


def _lp(data: bytes) -> bytes:
    return len(data).to_bytes(4, "big") + data


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _is_hex64(value: object) -> bool:
    return type(value) is str and _HEX64.fullmatch(value) is not None


def _label_bytes(value: object, what: str) -> bytes:
    """UTF-8 bytes of an identifier: 1 to 255 bytes, no control character."""
    if type(value) is not str:
        raise SumTreeError(f"{what} must be str")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise SumTreeError(f"{what} is not valid Unicode text") from None
    if not 1 <= len(encoded) <= MAX_LABEL_BYTES:
        raise SumTreeError(f"{what} must be 1 to {MAX_LABEL_BYTES} bytes in UTF-8")
    for ch in value:
        if ch < " " or ch == "\u007f":
            raise SumTreeError(f"{what} contains a control character")
    return encoded


def _payload_bytes(value: object, kind: str) -> bytes:
    if type(value) is not str:
        raise SumTreeError("payload must be str")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise SumTreeError("payload is not valid Unicode text") from None
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise SumTreeError(f"payload exceeds {MAX_PAYLOAD_BYTES} bytes")
    if kind in SNAPSHOT_KINDS and encoded:
        raise SumTreeError(f"payload must be empty in a tree of kind {kind}")
    return encoded


def _check_salt_key(salt_key: object) -> bytes:
    if type(salt_key) is not bytes:
        raise SumTreeError("salt_key must be bytes")
    if len(salt_key) < MIN_SALT_KEY_BYTES:
        raise SumTreeError(f"salt_key must be at least {MIN_SALT_KEY_BYTES} bytes")
    return salt_key


def _require_hex64(value: object, what: str) -> str:
    if not _is_hex64(value):
        raise ValueError(f"{what} must be 64 lower-case hexadecimal characters")
    return value  # type: ignore[return-value]


def is_hash_text(value: object) -> bool:
    """Whether ``value`` is exactly 64 lower-case hexadecimal characters."""
    return _is_hex64(value)


def check_identifier(value: object, what: str = "identifier") -> None:
    """Rule for ``institution_id`` and ``slot_id``: text of 1 to 255 bytes in UTF-8 without
    control characters. Raises :class:`SumTreeError` otherwise."""
    _label_bytes(value, what)


def check_salt_key(salt_key: object) -> None:
    """Rule for the operator's salt key: ``bytes``, at least 32 long. Raises
    :class:`SumTreeError` otherwise."""
    _check_salt_key(salt_key)


# ---------------------------------------------------------------------------
# Context and leaves
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SumTreeContext:
    """Which tree this is. Every leaf hash and the root hash bind its digest.

    ``as_of`` is the canonical instant text (``YYYY-MM-DDTHH:MM:SS.ffffffZ``). ``binding`` is
    empty, or 32 bytes that tie the tree to another document.
    """

    kind: str
    institution_id: str
    currency: str
    minor_units: int
    as_of: str
    binding: bytes = b""

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in KINDS:
            raise SumTreeError(f"kind must be one of {', '.join(KINDS)}")
        _label_bytes(self.institution_id, "institution_id")
        if type(self.currency) is not str or _CURRENCY_CODE.fullmatch(self.currency) is None:
            raise SumTreeError("currency must match [A-Z][A-Z0-9_]{0,31}")
        if type(self.minor_units) is not int or not 0 <= self.minor_units <= MAX_MINOR_UNITS:
            raise SumTreeError(f"minor_units must be an int from 0 to {MAX_MINOR_UNITS}")
        try:
            instant_from_text(self.as_of)
        except ExactValueError as exc:
            raise SumTreeError(f"as_of: {exc}") from None
        if type(self.binding) is not bytes or len(self.binding) not in (0, 32):
            raise SumTreeError("binding must be empty or exactly 32 bytes")

    def digest(self) -> bytes:
        """The 32-byte context digest."""
        return _sha256(
            DOMAIN
            + _TYPE_CONTEXT
            + _lp(self.kind.encode("utf-8"))
            + _lp(self.institution_id.encode("utf-8"))
            + _lp(self.currency.encode("utf-8"))
            + bytes([self.minor_units])
            + _lp(self.as_of.encode("utf-8"))
            + _lp(self.binding)
        )

    def to_document(self) -> dict[str, str]:
        """The JSON form used inside proofs and exports (every value is a string)."""
        return {
            "kind": self.kind,
            "institution_id": self.institution_id,
            "currency": self.currency,
            "minor_units": amount_to_text(self.minor_units),
            "as_of": self.as_of,
            "binding": self.binding.hex(),
        }

    @classmethod
    def from_document(cls, document: object) -> SumTreeContext:
        """Parse the JSON form. Raises :class:`SumTreeError` for anything but an exact match."""
        if type(document) is not dict or set(document) != _CONTEXT_KEYS:
            raise SumTreeError("context must be an object with exactly its six keys")
        binding = document["binding"]
        if binding != "" and not _is_hex64(binding):
            raise SumTreeError("binding must be empty or 64 lower-case hexadecimal characters")
        try:
            minor_units = amount_from_text(document["minor_units"], signed=False)
        except ExactValueError:
            raise SumTreeError("minor_units must be canonical unsigned decimal text") from None
        return cls(
            kind=document["kind"],
            institution_id=document["institution_id"],
            currency=document["currency"],
            minor_units=minor_units,
            as_of=document["as_of"],
            binding=bytes.fromhex(binding),
        )


def derive_salt(salt_key: bytes, context: SumTreeContext, slot_id: str) -> bytes:
    """The leaf salt of ``slot_id`` in the tree ``context``: an HMAC under the operator key.

    The key is an explicit input of at least 32 bytes. The library never creates, stores or
    logs it. The same slot gets a different salt in every context.
    """
    key = _check_salt_key(salt_key)
    message = DOMAIN + _TYPE_SALT + context.digest() + _lp(_label_bytes(slot_id, "slot_id"))
    return hmac.new(key, message, hashlib.sha256).digest()


@dataclass(frozen=True)
class SumTreeLeaf:
    """One leaf: a slot, its non-negative amount, its 32-byte salt and a payload."""

    slot_id: str
    amount: int
    salt: bytes
    payload: str = ""

    def to_document(self) -> dict[str, str]:
        return {
            "slot_id": self.slot_id,
            "amount": amount_to_text(self.amount),
            "salt": self.salt.hex(),
            "payload": self.payload,
        }


def _leaf_hash(context: SumTreeContext, context_digest: bytes, leaf: SumTreeLeaf) -> bytes:
    """Validate a leaf against the field rules and return its hash."""
    slot = _label_bytes(leaf.slot_id, "slot_id")
    if type(leaf.amount) is not int or not 0 <= leaf.amount <= AMOUNT_MAX:
        raise SumTreeError("leaf amount must be an int from 0 to 10**38 - 1")
    if type(leaf.salt) is not bytes or len(leaf.salt) != 32:
        raise SumTreeError("leaf salt must be exactly 32 bytes")
    payload = _payload_bytes(leaf.payload, context.kind)
    return _sha256(
        DOMAIN + _TYPE_LEAF + context_digest + _lp(slot) + amount_to_u128(leaf.amount) + leaf.salt + _lp(payload)
    )


def _node(left: tuple[bytes, int], right: tuple[bytes, int]) -> tuple[bytes, int]:
    """Combine two (hash, sum) pairs. The caller has checked that the sum is in range."""
    return (
        _sha256(DOMAIN + _TYPE_NODE + left[0] + amount_to_u128(left[1]) + right[0] + amount_to_u128(right[1])),
        left[1] + right[1],
    )


def _root_hash(context_digest: bytes, leaf_count: int, total: int, top_hash: bytes) -> str:
    return _sha256(
        DOMAIN + _TYPE_ROOT + context_digest + leaf_count.to_bytes(8, "big") + amount_to_u128(total) + top_hash
    ).hex()


def _sibling_plan(index: int, count: int) -> list[tuple[str, int]]:
    """Side and size of each sibling subtree on the path of leaf ``index``, leaf upwards.

    The shape is fixed by ``count`` alone: a list of ``n`` nodes splits at the largest power
    of two smaller than ``n``. ``'R'`` means the sibling is the right subtree, ``'L'`` the
    left one; the second element is the number of leaves under that sibling.
    """
    plan: list[tuple[str, int]] = []
    i, n = index, count
    while n > 1:
        k = 1 << ((n - 1).bit_length() - 1)
        if i < k:
            plan.append(("R", n - k))
            n = k
        else:
            plan.append(("L", k))
            i -= k
            n -= k
    plan.reverse()
    return plan


# ---------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------


class SumTree:
    """A Merkle-sum tree over a set of leaves.

    ``SumTree(context, leaves)`` takes leaves with their salts. ``SumTree.build(context,
    items, salt_key=...)`` derives the salts. Building refuses a duplicate slot id, an amount
    outside ``0 .. 10**38 - 1``, a salt that is not 32 bytes, a total above the bound, and a
    non-empty payload in a tree of kind ``liabilities`` or ``receivables``.
    """

    def __init__(self, context: SumTreeContext, leaves: Iterable[SumTreeLeaf]) -> None:
        if not isinstance(context, SumTreeContext):
            raise SumTreeError("context must be a SumTreeContext")
        self._context = context
        self._context_digest = context.digest()

        hashed: list[tuple[bytes, SumTreeLeaf]] = []
        seen: set[str] = set()
        for leaf in leaves:
            if not isinstance(leaf, SumTreeLeaf):
                raise SumTreeError("leaves must be SumTreeLeaf objects")
            leaf_hash = _leaf_hash(context, self._context_digest, leaf)
            if leaf.slot_id in seen:
                raise SumTreeError(f"duplicate slot id: {leaf.slot_id!r}")
            seen.add(leaf.slot_id)
            hashed.append((leaf_hash, leaf))
        # Order: ascending leaf hash. The tree is a function of the set of leaves.
        hashed.sort(key=lambda pair: pair[0])
        if len(hashed) >= _LEAF_COUNT_LIMIT:
            raise SumTreeError("too many leaves")

        self._leaves: tuple[SumTreeLeaf, ...] = tuple(leaf for _, leaf in hashed)
        self._index: dict[str, int] = {leaf.slot_id: i for i, leaf in enumerate(self._leaves)}

        total = 0
        for _, leaf in hashed:
            total += leaf.amount
        if total > AMOUNT_MAX:
            raise SumTreeError("the total of the tree exceeds 10**38 - 1")
        self._total = total

        # Level by level. An unpaired last node moves up unchanged: it is never paired with
        # itself and nothing is padded. Every partial sum is at most the total, so in range.
        level: list[tuple[bytes, int]] = [(leaf_hash, leaf.amount) for leaf_hash, leaf in hashed]
        self._levels: list[list[tuple[bytes, int]]] = [level]
        while len(level) > 1:
            upper = [_node(level[i], level[i + 1]) for i in range(0, len(level) - 1, 2)]
            if len(level) % 2 == 1:
                upper.append(level[-1])
            self._levels.append(upper)
            level = upper

        self._top_hash = level[0][0] if level else _EMPTY_TOP
        self._root = _root_hash(self._context_digest, len(self._leaves), total, self._top_hash)

    @classmethod
    def build(
        cls,
        context: SumTreeContext,
        items: Iterable[tuple],
        *,
        salt_key: bytes,
    ) -> SumTree:
        """Build from ``(slot_id, amount)`` or ``(slot_id, amount, payload)`` items.

        Each salt is derived from ``salt_key``, the context and the slot id.
        """
        if not isinstance(context, SumTreeContext):
            raise SumTreeError("context must be a SumTreeContext")
        _check_salt_key(salt_key)
        leaves = []
        for item in items:
            if not isinstance(item, (tuple, list)) or len(item) not in (2, 3):
                raise SumTreeError("each item must be (slot_id, amount) or (slot_id, amount, payload)")
            slot_id, amount = item[0], item[1]
            payload = item[2] if len(item) == 3 else ""
            leaves.append(
                SumTreeLeaf(slot_id=slot_id, amount=amount, salt=derive_salt(salt_key, context, slot_id), payload=payload)
            )
        return cls(context, leaves)

    # ----- what is published -----

    @property
    def context(self) -> SumTreeContext:
        return self._context

    @property
    def root(self) -> str:
        """The root hash in lower-case hexadecimal. It binds context, leaf count and total."""
        return self._root

    @property
    def total(self) -> int:
        return self._total

    @property
    def leaf_count(self) -> int:
        return len(self._leaves)

    @property
    def top_hash(self) -> str:
        """Hash of the top node (32 zero bytes for the empty tree), before the root step."""
        return self._top_hash.hex()

    def head(self) -> dict[str, str]:
        """The published head: root, total and leaf count, every value a string."""
        return {
            "root": self._root,
            "total": amount_to_text(self._total),
            "leaf_count": amount_to_text(len(self._leaves)),
        }

    # ----- what stays with the operator, holders and auditors -----

    @property
    def leaves(self) -> tuple[SumTreeLeaf, ...]:
        """Leaves in tree order (ascending leaf hash)."""
        return self._leaves

    def leaf_hashes(self) -> tuple[str, ...]:
        return tuple(h.hex() for h, _ in self._levels[0])

    def __contains__(self, slot_id: object) -> bool:
        return slot_id in self._index

    def index_of(self, slot_id: str) -> int:
        """Position of the slot in tree order. Raises ``KeyError`` when it has no leaf."""
        return self._index[slot_id]

    def proof_for(self, slot_id: str) -> dict[str, Any]:
        """Inclusion proof for one slot. It contains the slot's salt: deliver it privately.

        Raises ``KeyError`` when the slot has no leaf in this tree.
        """
        index = self._index[slot_id]
        path: list[dict[str, str]] = []
        position = index
        for level in self._levels[:-1]:
            if position % 2 == 1:
                sibling: tuple[bytes, int] | None = level[position - 1]
            elif position + 1 < len(level):
                sibling = level[position + 1]
            else:
                sibling = None  # unpaired last node: it moved up unchanged
            if sibling is not None:
                path.append({"hash": sibling[0].hex(), "sum": amount_to_text(sibling[1])})
            position //= 2
        return {
            "format": FORMAT_INCLUSION,
            "context": self._context.to_document(),
            "leaf_count": amount_to_text(len(self._leaves)),
            "leaf_index": amount_to_text(index),
            "leaf": self._leaves[index].to_document(),
            "path": path,
        }

    def export(self) -> dict[str, Any]:
        """Every leaf with its amount and salt, in tree order. For auditors, not for publication."""
        return {
            "format": FORMAT_EXPORT,
            "context": self._context.to_document(),
            "leaves": [leaf.to_document() for leaf in self._leaves],
        }


# ---------------------------------------------------------------------------
# Parsing of documents (never raises anything but SumTreeError)
# ---------------------------------------------------------------------------


def _unsigned(value: object, what: str) -> int:
    try:
        return amount_from_text(value, signed=False)  # type: ignore[arg-type]
    except ExactValueError:
        raise SumTreeError(f"{what} must be canonical unsigned decimal text") from None


def _leaf_from_document(document: object) -> SumTreeLeaf:
    if type(document) is not dict or set(document) != _LEAF_KEYS:
        raise SumTreeError("leaf must be an object with exactly its four keys")
    if not _is_hex64(document["salt"]):
        raise SumTreeError("salt must be 64 lower-case hexadecimal characters")
    if type(document["slot_id"]) is not str or type(document["payload"]) is not str:
        raise SumTreeError("slot_id and payload must be strings")
    return SumTreeLeaf(
        slot_id=document["slot_id"],
        amount=_unsigned(document["amount"], "amount"),
        salt=bytes.fromhex(document["salt"]),
        payload=document["payload"],
    )


def parse_head(head: object) -> tuple[str, int, int]:
    """``(root, total, leaf_count)`` of a published head ``{"root", "total", "leaf_count"}``.

    Raises :class:`SumTreeError` unless the object has exactly these keys, a root of 64
    lower-case hexadecimal characters, and canonical unsigned decimal text for the two
    integers, with a leaf count below 2**64.
    """
    if type(head) is not dict or set(head) != {"root", "total", "leaf_count"}:
        raise SumTreeError("a head must be an object with exactly root, total and leaf_count")
    if not _is_hex64(head["root"]):
        raise SumTreeError("a root must be 64 lower-case hexadecimal characters")
    total = _unsigned(head["total"], "total")
    leaf_count = _unsigned(head["leaf_count"], "leaf_count")
    if leaf_count >= _LEAF_COUNT_LIMIT:
        raise SumTreeError("leaf_count must be below 2**64")
    return head["root"], total, leaf_count


def _in_profile(document: object) -> bool:
    """Whether the whole document fits the canonical value profile (no numbers, plain keys)."""
    try:
        canonical_json(document)
    except ExactValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Inclusion check
# ---------------------------------------------------------------------------


def verify_sum_tree_inclusion(proof: object, *, expected_root: str, expected_slot_id: str) -> Verification:
    """Check an inclusion proof against a root the verifier already trusts.

    A proof contains no root; ``expected_root`` is the published one. ``expected_slot_id`` is
    the identifier the holder knows to be theirs. A malformed ``expected_root`` or
    ``expected_slot_id`` is a programming error and raises ``ValueError``. No content of
    ``proof`` makes the function raise.

    Reason codes: ``UNSUPPORTED_FORMAT``, ``MALFORMED_PROOF``, ``EMPTY_TREE``,
    ``INDEX_OUT_OF_RANGE``, ``PATH_LENGTH_MISMATCH``, ``SUM_OUT_OF_RANGE``,
    ``ROOT_MISMATCH``, ``SLOT_MISMATCH``.

    On success ``details`` holds what the root authenticated: ``context``, ``leaf_count``,
    ``total``, ``slot_id``, ``amount``, ``payload`` and ``disclosed``, the (leaf count, sum)
    of each sibling subtree from the leaf upwards. Compare ``total`` and ``leaf_count`` with
    the published summary, and ``amount`` with your own record.
    """
    _require_hex64(expected_root, "expected_root")
    try:
        _label_bytes(expected_slot_id, "expected_slot_id")
    except SumTreeError as exc:
        raise ValueError(str(exc)) from None
    try:
        return _verify_inclusion(proof, expected_root, expected_slot_id)
    except Exception as exc:  # last line of defence: fail closed, never raise on content
        return _fail("MALFORMED_PROOF", f"unexpected {type(exc).__name__}")


def _verify_inclusion(proof: object, expected_root: str, expected_slot_id: str) -> Verification:
    # 1. shape, types and field rules
    if type(proof) is not dict or proof.get("format") != FORMAT_INCLUSION:
        return _fail("UNSUPPORTED_FORMAT", f"not a document of format {FORMAT_INCLUSION}")
    if not _in_profile(proof):
        return _fail("MALFORMED_PROOF", "the document does not fit the canonical value profile")
    if set(proof) != _PROOF_KEYS:
        return _fail("MALFORMED_PROOF", "the proof does not have exactly its six keys")
    try:
        context = SumTreeContext.from_document(proof["context"])
        leaf_count = _unsigned(proof["leaf_count"], "leaf_count")
        leaf_index = _unsigned(proof["leaf_index"], "leaf_index")
        leaf = _leaf_from_document(proof["leaf"])
        context_digest = context.digest()
        leaf_hash = _leaf_hash(context, context_digest, leaf)  # 4. applies the leaf field rules
        raw_path = proof["path"]
        if type(raw_path) is not list:
            raise SumTreeError("path must be a list")
        path: list[tuple[bytes, int]] = []
        for step in raw_path:
            if type(step) is not dict or set(step) != _STEP_KEYS or not _is_hex64(step["hash"]):
                raise SumTreeError("each path element must be {hash, sum} with a 64-character hash")
            path.append((bytes.fromhex(step["hash"]), _unsigned(step["sum"], "sum")))
    except SumTreeError as exc:
        return _fail("MALFORMED_PROOF", str(exc))

    # 2. count and index
    if leaf_count == 0:
        return _fail("EMPTY_TREE", "an empty tree has no leaf to prove")
    if leaf_count >= _LEAF_COUNT_LIMIT:
        return _fail("MALFORMED_PROOF", "leaf_count must be below 2**64")
    if leaf_index >= leaf_count:
        return _fail("INDEX_OUT_OF_RANGE", "leaf_index must be below leaf_count")

    # 3. the shape fixes the path length
    plan = _sibling_plan(leaf_index, leaf_count)
    if len(path) != len(plan):
        return _fail("PATH_LENGTH_MISMATCH", f"expected {len(plan)} path elements, got {len(path)}")

    # 5. fold upwards; the side of each sibling follows from index and count, not from the proof
    current = (leaf_hash, leaf.amount)
    for (side, _size), sibling in zip(plan, path):
        if current[1] + sibling[1] > AMOUNT_MAX:
            return _fail("SUM_OUT_OF_RANGE", "a sum on the path exceeds 10**38 - 1")
        current = _node(current, sibling) if side == "R" else _node(sibling, current)

    # 6. the root binds context, count, total and top hash
    if not hmac.compare_digest(_root_hash(context_digest, leaf_count, current[1], current[0]), expected_root):
        return _fail("ROOT_MISMATCH", "the proof does not lead to the expected root")

    # 7. the slot the holder expects
    if leaf.slot_id != expected_slot_id:
        return _fail("SLOT_MISMATCH", "the proof is for another slot id")

    return Verification(
        ok=True,
        reason=None,
        details={
            "context": context.to_document(),
            "leaf_count": leaf_count,
            "total": current[1],
            "slot_id": leaf.slot_id,
            "amount": leaf.amount,
            "payload": leaf.payload,
            "disclosed": tuple((size, sibling[1]) for (_side, size), sibling in zip(plan, path)),
        },
    )


# ---------------------------------------------------------------------------
# Auditor export: full recomputation
# ---------------------------------------------------------------------------


def audit_sum_tree_export(export: object, *, expected_root: str) -> Verification:
    """Rebuild a tree from its complete leaf list and compare with the expected root.

    Checks the key sets and value rules (``MALFORMED_EXPORT``), rebuilds the tree from the
    leaves, sorting them itself (duplicates and out-of-range values are
    ``MALFORMED_EXPORT``), compares the root (``ROOT_MISMATCH``) and then requires the listed
    order to be the tree order (``NOT_IN_CANONICAL_ORDER``). A document of another format is
    ``UNSUPPORTED_FORMAT``. On success ``details`` holds ``root``, ``total``, ``leaf_count``,
    the published ``head`` and the ``context``.

    A malformed ``expected_root`` raises ``ValueError``; no content of ``export`` makes the
    function raise.
    """
    _require_hex64(expected_root, "expected_root")
    try:
        return _audit_export(export, expected_root)
    except Exception as exc:  # last line of defence: fail closed, never raise on content
        return _fail("MALFORMED_EXPORT", f"unexpected {type(exc).__name__}")


def _audit_export(export: object, expected_root: str) -> Verification:
    if type(export) is not dict or export.get("format") != FORMAT_EXPORT:
        return _fail("UNSUPPORTED_FORMAT", f"not a document of format {FORMAT_EXPORT}")
    if not _in_profile(export):
        return _fail("MALFORMED_EXPORT", "the document does not fit the canonical value profile")
    if set(export) != _EXPORT_KEYS:
        return _fail("MALFORMED_EXPORT", "the export does not have exactly its three keys")
    try:
        context = SumTreeContext.from_document(export["context"])
        raw_leaves = export["leaves"]
        if type(raw_leaves) is not list:
            raise SumTreeError("leaves must be a list")
        listed = [_leaf_from_document(item) for item in raw_leaves]
        tree = SumTree(context, listed)  # sorts; refuses duplicates and out-of-range values
    except SumTreeError as exc:
        return _fail("MALFORMED_EXPORT", str(exc))
    if not hmac.compare_digest(tree.root, expected_root):
        return _fail("ROOT_MISMATCH", "the leaves do not give the expected root")
    if tuple(listed) != tree.leaves:
        return _fail("NOT_IN_CANONICAL_ORDER", "the leaves are not listed in tree order")
    return Verification(
        ok=True,
        reason=None,
        details={
            "root": tree.root,
            "total": tree.total,
            "leaf_count": tree.leaf_count,
            "head": tree.head(),
            "context": context.to_document(),
        },
    )
