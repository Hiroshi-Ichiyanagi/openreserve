"""conversion.py — a declared fixed-rate conversion: declaration, arithmetic, posting, closing.

Scope: every balance of one source currency is converted into one target currency, at one
declared rate and rounding rule, as of one cut-off instant, inside one institution's ledger.

Arithmetic is exact. A rate is decimal text turned into a fraction of integers; amounts are
integers in minor units; nothing here uses a floating-point value. With ``x`` the signed
amount in source minor units, ``ds`` and ``dt`` the minor units of source and target::

    factor = 10**(dt - ds) / rate      for SOURCE_PER_TARGET (source units for one target unit)
    factor = 10**(dt - ds) * rate      for TARGET_PER_SOURCE (target units for one source unit)
    z = x * factor                     exact fraction: the unrounded amount in target minor units
    y = Round(z)                       integer: the converted amount
    e = y - z                          exact fraction: the rounding residue, |e| <= 1/2

The declared text and direction are used as they are. There is no operation that inverts a
rate or stores a decimal reciprocal: a rate declared one way is applied by exact division,
the other way by exact multiplication.

Rounding is to the nearest minor unit, once per account. The tie rule is part of the
declaration and has no default: ``HALF_AWAY_FROM_ZERO`` or ``HALF_EVEN``. Both are symmetric
in sign, ``Round(-z) == -Round(z)``, so a debt and a claim of the same size convert to the
same magnitude. For negative amounts this symmetry is a design choice of this library.

Posting goes through one clearing account per currency. The whole-ledger rounding difference
is booked to a residue account that belongs to the institution.

This module makes no claim about any real conversion. The specification is
docs/hypothetical/W_CHANGEOVER_DESIGN.md, section 8.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING, Any

from openreserve.core.currencies import CurrencySpec
from openreserve.core.exact import (
    AMOUNT_MAX,
    INSTANT_MAX,
    ExactValueError,
    amount_from_text,
    amount_to_text,
    canonical_json,
    instant_from_text,
    require_aware,
    sha256_hex,
)
from openreserve.core.ledger import Ledger
from openreserve.core.types import (
    Account,
    Entry,
    IntegrityError,
    Money,
    OwnerType,
    Transaction,
    TransactionStatus,
)

if TYPE_CHECKING:  # the module never constructs or reads a clock
    from datetime import datetime

__all__ = [
    "CLOSING_METADATA_KEY",
    "CLOSING_PURPOSE_CODE",
    "CONVERSION_METADATA_KEY",
    "CONVERSION_PURPOSE_CODE",
    "FORMAT_DECLARATION",
    "HALF_AWAY_FROM_ZERO",
    "HALF_EVEN",
    "SOURCE_PER_TARGET",
    "TARGET_PER_SOURCE",
    "ConversionBatchResult",
    "ConversionBlockedError",
    "ConversionDeclaration",
    "ConversionError",
    "ConversionIncompleteError",
    "ConversionReplayError",
    "ConvertedAmount",
    "DeclarationError",
    "PostedConversion",
    "close_conversion",
    "closing_transaction_id",
    "conversion_transaction_id",
    "convert",
    "fraction_from_document",
    "fraction_to_document",
    "load_declaration",
    "parse_rate_text",
    "post_conversion",
    "post_conversion_batch",
    "rate_significant_figures",
    "read_closing",
    "read_posted_conversion",
    "round_fraction",
]

FORMAT_DECLARATION = "openreserve.conversion.declaration/1"

SOURCE_PER_TARGET = "SOURCE_PER_TARGET"
TARGET_PER_SOURCE = "TARGET_PER_SOURCE"
QUOTATIONS = (SOURCE_PER_TARGET, TARGET_PER_SOURCE)

HALF_AWAY_FROM_ZERO = "HALF_AWAY_FROM_ZERO"
HALF_EVEN = "HALF_EVEN"
ROUNDING_MODES = (HALF_AWAY_FROM_ZERO, HALF_EVEN)

QUANTUM_MINOR_UNITS = "1"  # the only rounding quantum of this format version
ROUNDING_LEVEL = "ACCOUNT"  # the only rounding level of this format version

_RATE_TEXT = re.compile(r"(0|[1-9][0-9]{0,29})(\.[0-9]{1,30})?")
_HEX64 = re.compile(r"[0-9a-f]{64}")
# Numerators and denominators of exact totals can be longer than an amount (an amount times a
# scale of up to 10**30 times a rate of up to 60 digits), so they have their own bound.
_BIG_DECIMAL = re.compile(r"-?[1-9][0-9]{0,159}|0")
_BIG_UNSIGNED = re.compile(r"[1-9][0-9]{0,159}")

_DECLARATION_KEYS = frozenset(
    {
        "format",
        "declaration_id",
        "source",
        "target",
        "rate",
        "rate_significant_figures",
        "rounding",
        "cutoff_at",
        "reference_document_sha256",
    }
)
_CURRENCY_KEYS = frozenset({"code", "minor_units"})
_RATE_KEYS = frozenset({"text", "quotation"})
_ROUNDING_KEYS = frozenset({"mode", "quantum_minor_units", "level"})
_FRACTION_KEYS = frozenset({"num", "den"})


class ConversionError(ValueError):
    """A conversion cannot be computed or posted as asked."""


class DeclarationError(ConversionError):
    """A conversion declaration breaks a rule of its format."""


class ConversionReplayError(ConversionError):
    """The source account was already converted under this declaration."""


class ConversionBlockedError(ConversionError):
    """The state of an account does not allow its conversion (pending or later activity)."""


class ConversionIncompleteError(ConversionError):
    """The conversion of the source currency is not complete, or not closed."""


# ---------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------


def parse_rate_text(text: str) -> Fraction:
    """The exact value of a rate written as decimal text.

    The text must fully match ``(0|[1-9][0-9]{0,29})(\\.[0-9]{1,30})?`` and be greater than
    zero. Its value is ``digits / 10**(number of fractional digits)``, built from integers:
    ``"0.1"`` is 1/10, ``"1.95583"`` is 195583/100000, ``"340.750"`` is 1363/4. Anything that
    is not such a string is refused, including ``float``, ``int``, ``Decimal`` and
    ``Fraction`` values, exponents, signs, spaces and digit separators.
    """
    if type(text) is not str:
        raise DeclarationError(f"a rate must be decimal text, got {type(text).__name__}")
    match = _RATE_TEXT.fullmatch(text)
    if match is None:
        raise DeclarationError("rate text must match (0|[1-9][0-9]{0,29})(\\.[0-9]{1,30})?")
    whole, fraction_part = match.group(1), (match.group(2) or ".")[1:]
    value = Fraction(int(whole + fraction_part), 10 ** len(fraction_part))
    if value <= 0:
        raise DeclarationError("a rate must be greater than zero")
    return value


def rate_significant_figures(text: str) -> int:
    """Number of significant figures of a rate text.

    Counted from the left, starting at the first non-zero figure; trailing zeros count:
    ``"1.95583"`` and ``"340.750"`` have six, ``"0.0064"`` has two, ``"100"`` has three.
    """
    parse_rate_text(text)
    return len(text.replace(".", "").lstrip("0"))


# ---------------------------------------------------------------------------
# Rounding
# ---------------------------------------------------------------------------


def round_fraction(z: Fraction | int, mode: str) -> int:
    """Round an exact fraction to the nearest integer under a declared tie rule.

    For ``z = n/d`` with ``d > 0``, let ``q, r = divmod(n, d)``. If ``2r < d`` the result is
    ``q``; if ``2r > d`` it is ``q + 1``. On a tie (``2r == d``), ``HALF_AWAY_FROM_ZERO``
    gives ``q + 1`` for positive ``n`` and ``q`` for negative ``n``, and ``HALF_EVEN`` gives
    whichever of ``q`` and ``q + 1`` is even. There is no default mode.
    """
    if type(mode) is not str or mode not in ROUNDING_MODES:
        raise ConversionError(f"rounding mode must be one of {', '.join(ROUNDING_MODES)}")
    if type(z) is int:
        return z
    if type(z) is not Fraction:
        raise ConversionError(f"round_fraction needs a Fraction or an int, got {type(z).__name__}")
    n, d = z.numerator, z.denominator
    q, r = divmod(n, d)
    twice = 2 * r
    if twice < d:
        return q
    if twice > d:
        return q + 1
    if mode == HALF_AWAY_FROM_ZERO:
        return q + 1 if n > 0 else q
    return q if q % 2 == 0 else q + 1


# ---------------------------------------------------------------------------
# Fractions in documents
# ---------------------------------------------------------------------------


def fraction_to_document(value: Fraction | int) -> dict[str, str]:
    """``{"num": "<signed decimal>", "den": "<decimal>"}`` in lowest terms, denominator positive."""
    if type(value) is int:
        value = Fraction(value)
    if type(value) is not Fraction:
        raise ConversionError(f"expected a Fraction, got {type(value).__name__}")
    num, den = str(value.numerator), str(value.denominator)
    if _BIG_DECIMAL.fullmatch(num) is None or _BIG_UNSIGNED.fullmatch(den) is None:
        raise ConversionError("fraction is too large to be written in a document")
    return {"num": num, "den": den}


def fraction_from_document(document: object) -> Fraction:
    """Parse ``{"num", "den"}``. Refuses anything that is not canonical and in lowest terms."""
    if type(document) is not dict or set(document) != _FRACTION_KEYS:
        raise ConversionError("a fraction must be an object with exactly num and den")
    num, den = document["num"], document["den"]
    if type(num) is not str or type(den) is not str:
        raise ConversionError("num and den must be decimal text")
    if _BIG_DECIMAL.fullmatch(num) is None or _BIG_UNSIGNED.fullmatch(den) is None:
        raise ConversionError("num and den must be canonical decimal text, den positive")
    value = Fraction(int(num), int(den))
    if (str(value.numerator), str(value.denominator)) != (num, den):
        raise ConversionError("a fraction must be written in lowest terms")
    return value


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


def _label(value: object, what: str) -> str:
    if type(value) is not str:
        raise DeclarationError(f"{what} must be text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise DeclarationError(f"{what} is not valid Unicode text") from None
    if not 1 <= len(encoded) <= 255:
        raise DeclarationError(f"{what} must be 1 to 255 bytes in UTF-8")
    if any(ch < " " or ch == "\u007f" for ch in value):
        raise DeclarationError(f"{what} contains a control character")
    return value


@dataclass(frozen=True)
class ConvertedAmount:
    """One conversion: ``x``, the exact ``z``, the rounded ``y`` and the residue ``e = y - z``."""

    source_amount: int
    exact: Fraction
    converted_amount: int
    residue: Fraction


@dataclass(frozen=True)
class ConversionDeclaration:
    """A declared rate and rounding rule for one source and one target currency.

    Build it with keyword arguments or load it with :meth:`from_document`. Every rule is
    checked on construction and a violation raises :class:`DeclarationError`. The declaration
    is identified everywhere by ``declaration_hash``, the SHA-256 of its canonical JSON.
    """

    declaration_id: str
    source: CurrencySpec
    target: CurrencySpec
    rate_text: str
    quotation: str
    rounding_mode: str
    cutoff_at: str
    rate_significant_figures: int | None = None
    reference_document_sha256: str | None = None

    def __post_init__(self) -> None:
        _label(self.declaration_id, "declaration_id")
        if not isinstance(self.source, CurrencySpec) or not isinstance(self.target, CurrencySpec):
            raise DeclarationError("source and target must be CurrencySpec objects")
        if self.source.code == self.target.code:
            raise DeclarationError("source and target must be different currencies")
        parse_rate_text(self.rate_text)
        if type(self.quotation) is not str or self.quotation not in QUOTATIONS:
            raise DeclarationError(f"quotation must be one of {', '.join(QUOTATIONS)}")
        if type(self.rounding_mode) is not str or self.rounding_mode not in ROUNDING_MODES:
            raise DeclarationError(
                f"rounding mode must be stated and be one of {', '.join(ROUNDING_MODES)}; there is no default"
            )
        if self.rate_significant_figures is not None:
            if type(self.rate_significant_figures) is not int or self.rate_significant_figures < 1:
                raise DeclarationError("rate_significant_figures must be a positive int or None")
            actual = rate_significant_figures(self.rate_text)
            if actual != self.rate_significant_figures:
                raise DeclarationError(
                    f"the rate text has {actual} significant figures, "
                    f"the declaration requires {self.rate_significant_figures}"
                )
        try:
            instant_from_text(self.cutoff_at)
        except ExactValueError as exc:
            raise DeclarationError(f"cutoff_at: {exc}") from None
        ref = self.reference_document_sha256
        if ref is not None and (type(ref) is not str or _HEX64.fullmatch(ref) is None):
            raise DeclarationError("reference_document_sha256 must be null or 64 lower-case hexadecimal characters")

    # ----- derived values -----

    @property
    def rate(self) -> Fraction:
        """The declared rate as an exact fraction."""
        return parse_rate_text(self.rate_text)

    @property
    def factor(self) -> Fraction:
        """Target minor units per source minor unit, exact."""
        scale = Fraction(10) ** (self.target.minor_units - self.source.minor_units)
        return scale / self.rate if self.quotation == SOURCE_PER_TARGET else scale * self.rate

    @property
    def cutoff(self) -> "datetime":
        """The cut-off instant (aware, UTC). Balances settled at or before it are converted."""
        return instant_from_text(self.cutoff_at)

    def to_document(self) -> dict[str, Any]:
        """The declaration document (canonical profile: no numbers)."""
        figures = self.rate_significant_figures
        return {
            "format": FORMAT_DECLARATION,
            "declaration_id": self.declaration_id,
            "source": {"code": self.source.code, "minor_units": amount_to_text(self.source.minor_units)},
            "target": {"code": self.target.code, "minor_units": amount_to_text(self.target.minor_units)},
            "rate": {"text": self.rate_text, "quotation": self.quotation},
            "rate_significant_figures": None if figures is None else amount_to_text(figures),
            "rounding": {
                "mode": self.rounding_mode,
                "quantum_minor_units": QUANTUM_MINOR_UNITS,
                "level": ROUNDING_LEVEL,
            },
            "cutoff_at": self.cutoff_at,
            "reference_document_sha256": self.reference_document_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_document())

    @property
    def declaration_hash(self) -> str:
        """SHA-256 of the canonical JSON of the declaration, lower-case hexadecimal."""
        return sha256_hex(self.canonical_bytes())

    @property
    def declaration_hash_bytes(self) -> bytes:
        return hashlib.sha256(self.canonical_bytes()).digest()

    @classmethod
    def from_document(cls, document: object) -> ConversionDeclaration:
        """Load a declaration document. Every violation raises :class:`DeclarationError`."""
        if type(document) is not dict:
            raise DeclarationError("a declaration must be an object")
        try:
            canonical_json(document)
        except ExactValueError as exc:
            raise DeclarationError(f"the declaration does not fit the canonical value profile: {exc}") from None
        if set(document) != _DECLARATION_KEYS:
            raise DeclarationError("a declaration must have exactly its nine keys")
        if document["format"] != FORMAT_DECLARATION:
            raise DeclarationError(f"a declaration must have the format {FORMAT_DECLARATION}")
        rate, rounding = document["rate"], document["rounding"]
        if type(rate) is not dict or set(rate) != _RATE_KEYS:
            raise DeclarationError("rate must be an object with exactly text and quotation")
        if type(rounding) is not dict or set(rounding) != _ROUNDING_KEYS:
            raise DeclarationError("rounding must be an object with exactly mode, quantum_minor_units and level")
        if rounding["quantum_minor_units"] != QUANTUM_MINOR_UNITS:
            raise DeclarationError("unsupported rounding quantum: this format version rounds to 1 minor unit")
        if rounding["level"] != ROUNDING_LEVEL:
            raise DeclarationError("unsupported rounding level: this format version rounds per ACCOUNT")
        figures = document["rate_significant_figures"]
        if figures is not None:
            try:
                figures = amount_from_text(figures, signed=False)
            except ExactValueError:
                raise DeclarationError("rate_significant_figures must be null or canonical decimal text") from None
        return cls(
            declaration_id=document["declaration_id"],
            source=_currency_from_document(document["source"], "source"),
            target=_currency_from_document(document["target"], "target"),
            rate_text=rate["text"],
            quotation=rate["quotation"],
            rounding_mode=rounding["mode"],
            cutoff_at=document["cutoff_at"],
            rate_significant_figures=figures,
            reference_document_sha256=document["reference_document_sha256"],
        )

    # ----- arithmetic -----

    def convert(self, source_amount: int) -> ConvertedAmount:
        """Convert a signed amount in source minor units. See :func:`convert`."""
        return convert(self, source_amount)


def _currency_from_document(document: object, what: str) -> CurrencySpec:
    if type(document) is not dict or set(document) != _CURRENCY_KEYS:
        raise DeclarationError(f"{what} must be an object with exactly code and minor_units")
    try:
        minor_units = amount_from_text(document["minor_units"], signed=False)
        return CurrencySpec(code=document["code"], minor_units=minor_units)
    except ExactValueError as exc:
        raise DeclarationError(f"{what}: {exc}") from None


def load_declaration(document: object) -> ConversionDeclaration:
    """Load and check a declaration document. Raises :class:`DeclarationError`."""
    return ConversionDeclaration.from_document(document)


def _as_declaration(declaration: object) -> ConversionDeclaration:
    if isinstance(declaration, ConversionDeclaration):
        return declaration
    return ConversionDeclaration.from_document(declaration)


def convert(declaration: ConversionDeclaration | dict, source_amount: int) -> ConvertedAmount:
    """Convert a signed amount ``x`` in source minor units under a declaration.

    Returns ``x``, the exact fraction ``z = x * factor``, the integer ``y = Round(z)`` and
    the residue ``e = y - z``. ``x`` must be an ``int`` (not ``bool``); ``|x|`` and ``|y|``
    must not exceed ``10**38 - 1`` (:class:`ConversionError` otherwise).
    """
    declaration = _as_declaration(declaration)
    if type(source_amount) is not int:
        raise ConversionError(f"an amount must be int in minor units, got {type(source_amount).__name__}")
    if abs(source_amount) > AMOUNT_MAX:
        raise ConversionError("the source amount exceeds 10**38 - 1 in magnitude")
    exact = source_amount * declaration.factor
    converted = round_fraction(exact, declaration.rounding_mode)
    if abs(converted) > AMOUNT_MAX:
        raise ConversionError("the converted amount exceeds 10**38 - 1 in magnitude")
    return ConvertedAmount(
        source_amount=source_amount, exact=Fraction(exact), converted_amount=converted, residue=converted - exact
    )


# ---------------------------------------------------------------------------
# Posting through clearing accounts
# ---------------------------------------------------------------------------

CONVERSION_PURPOSE_CODE = "CURRENCY_CONVERSION"
CONVERSION_METADATA_KEY = "openreserve.conversion/1"
CLOSING_PURPOSE_CODE = "CURRENCY_CONVERSION_CLOSING"
CLOSING_METADATA_KEY = "openreserve.conversion.closing/1"

_ID_DOMAIN = b"openreserve.conversion.v1"
_RECORD_KEYS = frozenset(
    {"declaration_hash", "source_account_id", "source_amount", "target_account_id", "target_amount"}
)
_CLOSING_RECORD_KEYS = frozenset({"declaration_hash", "residue_account_change"})


def _account_id_bytes(account_id: object, what: str) -> bytes:
    """UTF-8 bytes of an account id that can also serve as a slot id in the evidence."""
    try:
        return _label(account_id, what).encode("utf-8")
    except DeclarationError as exc:
        raise ConversionError(str(exc)) from None


def conversion_transaction_id(declaration: ConversionDeclaration | dict, source_account_id: str) -> str:
    """The id of the one transaction that converts ``source_account_id`` under a declaration.

    ``conv_`` + hex of SHA-256(``"openreserve.conversion.v1"`` || 0x01 || declaration hash
    (32 bytes) || 4-byte big-endian length || UTF-8 of the source account id). The id does
    not depend on the target, so the same source cannot be converted twice.
    """
    declaration = _as_declaration(declaration)
    source = _account_id_bytes(source_account_id, "source_account_id")
    digest = hashlib.sha256(
        _ID_DOMAIN + b"\x01" + declaration.declaration_hash_bytes + len(source).to_bytes(4, "big") + source
    ).hexdigest()
    return "conv_" + digest


def closing_transaction_id(declaration: ConversionDeclaration | dict) -> str:
    """The id of the closing transaction of a declaration.

    ``convres_`` + hex of SHA-256(``"openreserve.conversion.v1"`` || 0x02 || declaration hash).
    """
    declaration = _as_declaration(declaration)
    return "convres_" + hashlib.sha256(_ID_DOMAIN + b"\x02" + declaration.declaration_hash_bytes).hexdigest()


@dataclass(frozen=True)
class PostedConversion:
    """What a conversion transaction in the ledger says, recomputed from its entries."""

    transaction_id: str
    source_account_id: str
    source_amount: int  # signed, source minor units
    target_account_id: str
    target_amount: int  # signed, target minor units


@dataclass(frozen=True)
class ConversionBatchResult:
    """Source account ids of a batch run: newly posted, already converted, zero balance."""

    posted: tuple[str, ...]
    skipped: tuple[str, ...]
    zero_balance: tuple[str, ...]


def _effective_instant(declaration: ConversionDeclaration, effective_at: "datetime") -> "datetime":
    try:
        require_aware(effective_at, "effective_at")
    except ExactValueError as exc:
        raise ConversionError(str(exc)) from None
    if not effective_at > declaration.cutoff:
        raise ConversionError("effective_at must be later than the cut-off of the declaration")
    return effective_at


def _get_account(ledger: Ledger, account_id: object, what: str) -> Account:
    _account_id_bytes(account_id, what)
    try:
        return ledger.get_account(account_id)  # type: ignore[arg-type]
    except IntegrityError:
        raise ConversionError(f"{what} does not exist: {account_id!r}") from None


def _has_currency(account: Account, spec: CurrencySpec) -> bool:
    return account.currency.code == spec.code and account.currency.minor_units == spec.minor_units


def _check_clearing_accounts(
    ledger: Ledger, declaration: ConversionDeclaration, clearing_source_account_id: str, clearing_target_account_id: str
) -> tuple[Account, Account]:
    clearing_source = _get_account(ledger, clearing_source_account_id, "clearing_source_account_id")
    clearing_target = _get_account(ledger, clearing_target_account_id, "clearing_target_account_id")
    for account, spec, what in (
        (clearing_source, declaration.source, "source"),
        (clearing_target, declaration.target, "target"),
    ):
        if account.owner_type is not OwnerType.CLEARING:
            raise ConversionError(f"the {what} clearing account must have the type CLEARING")
        if not _has_currency(account, spec):
            raise ConversionError(f"the {what} clearing account must be in {spec.code} with {spec.minor_units} minor units")
    return clearing_source, clearing_target


def _find_transaction(ledger: Ledger, transaction_id: str) -> Transaction | None:
    try:
        return ledger.get_transaction(transaction_id)
    except IntegrityError:
        return None


def read_posted_conversion(
    transaction: Transaction,
    declaration: ConversionDeclaration | dict,
    *,
    clearing_source_account_id: str,
    clearing_target_account_id: str,
) -> PostedConversion:
    """Recompute what a conversion transaction did from its entries and the declaration.

    The metadata record is only used for the target of a conversion whose converted amount
    is 0 (such a transaction has no target entry). Everything else is read from the entries
    and checked: the id is the derived one, the transaction is settled, the entries have the
    documented shape, and the target amount is ``Round(x * factor)``. Anything else raises
    :class:`ConversionError`.
    """
    declaration = _as_declaration(declaration)
    problem = ConversionError(f"transaction {transaction.transaction_id} is not a conversion under this declaration")
    record = None
    for key, value in transaction.metadata:
        if key == CONVERSION_METADATA_KEY:
            if record is not None:
                raise problem
            record = value
    if record is None or transaction.purpose_code != CONVERSION_PURPOSE_CODE:
        raise problem
    if transaction.status is not TransactionStatus.SETTLED or transaction.settled_at is None:
        raise problem
    entries = transaction.entries
    if len(entries) not in (2, 4) or [e.sequence for e in entries] != list(range(len(entries))):
        raise problem
    source_entry, clearing_entry = entries[0], entries[1]
    source_account_id = source_entry.account_id
    if conversion_transaction_id(declaration, source_account_id) != transaction.transaction_id:
        raise problem
    x = -source_entry.amount.cents
    if (
        x == 0
        or clearing_entry.account_id != clearing_source_account_id
        or clearing_entry.amount.cents != x
        or source_entry.amount.currency.code != declaration.source.code
        or clearing_entry.amount.currency.code != declaration.source.code
    ):
        raise problem
    y = convert(declaration, x).converted_amount
    try:
        parsed = json.loads(record)
        if type(parsed) is not dict or set(parsed) != _RECORD_KEYS or canonical_json(parsed).decode("utf-8") != record:
            raise problem
        target_account_id = parsed["target_account_id"]
        recorded = (
            parsed["declaration_hash"],
            parsed["source_account_id"],
            amount_from_text(parsed["source_amount"]),
            amount_from_text(parsed["target_amount"]),
        )
    except (ValueError, TypeError, RecursionError):
        raise problem from None
    if type(target_account_id) is not str or recorded != (declaration.declaration_hash, source_account_id, x, y):
        raise problem
    if y == 0:
        if len(entries) != 2:
            raise problem
    else:
        if len(entries) != 4:
            raise problem
        target_clearing_entry, target_entry = entries[2], entries[3]
        if (
            target_clearing_entry.account_id != clearing_target_account_id
            or target_clearing_entry.amount.cents != -y
            or target_entry.account_id != target_account_id
            or target_entry.amount.cents != y
            or target_entry.amount.currency.code != declaration.target.code
            or target_clearing_entry.amount.currency.code != declaration.target.code
        ):
            raise problem
    return PostedConversion(
        transaction_id=transaction.transaction_id,
        source_account_id=source_account_id,
        source_amount=x,
        target_account_id=target_account_id,
        target_amount=y,
    )


def post_conversion(
    ledger: Ledger,
    declaration: ConversionDeclaration | dict,
    *,
    source_account_id: str,
    target_account_id: str,
    clearing_source_account_id: str,
    clearing_target_account_id: str,
    effective_at: "datetime",
    initiator_id: str,
) -> Transaction | None:
    """Convert the balance of one account at the cut-off, in one settled transaction.

    With ``x`` the signed balance of the source account at the cut-off and ``y`` its
    conversion, the entries are, in this order::

        (source account,          -x)   source currency
        (source clearing account, +x)   source currency
        (target clearing account, -y)   target currency
        (target account,          +y)   target currency

    When ``y`` is 0 the last two entries are left out; when ``x`` is 0 nothing is posted and
    ``None`` is returned. Both legs are in one transaction, so they are stored together or
    not at all. The transaction id is derived from the declaration and the source account.

    Checks, in this order: ``effective_at`` is aware and later than the cut-off, the accounts
    exist, have the declared currencies, source and target have the same account type, the
    clearing accounts have the type ``CLEARING``, and the four accounts are different
    (:class:`ConversionError`); the source was not converted before under this declaration
    (:class:`ConversionReplayError`); the source has no pending transaction, the target has
    no entry at all, and nothing settled on the source after the cut-off
    (:class:`ConversionBlockedError`). The last check compares the settled entries, not only
    their sum, so two later entries that cancel each other are refused as well.
    """
    declaration = _as_declaration(declaration)
    # 1. arguments and accounts
    effective_at = _effective_instant(declaration, effective_at)
    if type(initiator_id) is not str or not initiator_id:
        raise ConversionError("initiator_id must be non-empty text")
    source = _get_account(ledger, source_account_id, "source_account_id")
    target = _get_account(ledger, target_account_id, "target_account_id")
    if not _has_currency(source, declaration.source):
        raise ConversionError(
            f"the source account must be in {declaration.source.code} with {declaration.source.minor_units} minor units"
        )
    if not _has_currency(target, declaration.target):
        raise ConversionError(
            f"the target account must be in {declaration.target.code} with {declaration.target.minor_units} minor units"
        )
    if source.owner_type is not target.owner_type:
        raise ConversionError("source and target account must have the same account type")
    _check_clearing_accounts(ledger, declaration, clearing_source_account_id, clearing_target_account_id)
    if len({source_account_id, target_account_id, clearing_source_account_id, clearing_target_account_id}) != 4:
        raise ConversionError("source, target and the two clearing accounts must be four different accounts")

    # 2. replay
    transaction_id = conversion_transaction_id(declaration, source_account_id)
    if _find_transaction(ledger, transaction_id) is not None:
        raise ConversionReplayError(
            f"account {source_account_id!r} was already converted under declaration {declaration.declaration_hash}"
        )

    # 3. no pending transaction on the source
    settled_ever = ledger.account_entries(source_account_id, as_of=INSTANT_MAX)
    with_pending = ledger.account_entries(source_account_id, as_of=INSTANT_MAX, include_pending=True)
    if len(with_pending) != len(settled_ever):
        raise ConversionBlockedError(
            f"account {source_account_id!r} has a pending transaction; settle or fail it before the conversion"
        )
    # 4. the target is untouched
    if ledger.account_entries(target_account_id, as_of=INSTANT_MAX, include_pending=True):
        raise ConversionBlockedError(f"target account {target_account_id!r} already has entries")
    # 5. nothing settled on the source after the cut-off
    at_cutoff = ledger.account_entries(source_account_id, as_of=declaration.cutoff)
    if len(at_cutoff) != len(settled_ever):
        raise ConversionBlockedError(
            f"account {source_account_id!r} has entries settled after the cut-off; "
            "its balance over all time must equal its balance at the cut-off"
        )

    # 6. the balance at the cut-off
    x = sum(entry.amount.cents for entry in at_cutoff)
    if x == 0:
        return None
    y = convert(declaration, x).converted_amount

    record = canonical_json(
        {
            "declaration_hash": declaration.declaration_hash,
            "source_account_id": source_account_id,
            "source_amount": amount_to_text(x),
            "target_account_id": target_account_id,
            "target_amount": amount_to_text(y),
        }
    ).decode("utf-8")
    legs: list[tuple[str, Money]] = [
        (source_account_id, Money(cents=-x, currency=source.currency)),
        (clearing_source_account_id, Money(cents=x, currency=source.currency)),
    ]
    if y != 0:
        legs += [
            (clearing_target_account_id, Money(cents=-y, currency=target.currency)),
            (target_account_id, Money(cents=y, currency=target.currency)),
        ]
    transaction = Transaction(
        transaction_id=transaction_id,
        entries=tuple(
            Entry(
                entry_id=f"{transaction_id}:{sequence}",
                transaction_id=transaction_id,
                account_id=account_id,
                amount=amount,
                sequence=sequence,
            )
            for sequence, (account_id, amount) in enumerate(legs)
        ),
        purpose_code=CONVERSION_PURPOSE_CODE,
        initiator_id=initiator_id,
        initiated_at=effective_at,
        settled_at=effective_at,
        status=TransactionStatus.SETTLED,
        external_refs=(),
        compliance_decision=None,
        metadata=((CONVERSION_METADATA_KEY, record),),
    )
    return ledger.post(transaction)


def post_conversion_batch(
    ledger: Ledger,
    declaration: ConversionDeclaration | dict,
    *,
    pairs: Any,
    clearing_source_account_id: str,
    clearing_target_account_id: str,
    effective_at: "datetime",
    initiator_id: str,
) -> ConversionBatchResult:
    """Convert several accounts: ``pairs`` is an iterable of ``(source id, target id)``.

    Works in ascending order of the source id. It can be run again after an interruption: a
    source that already has its conversion transaction with the same target is skipped and
    reported; with another target it raises :class:`ConversionReplayError`. A source or a
    target that appears twice in ``pairs`` is refused before anything is posted.
    """
    declaration = _as_declaration(declaration)
    pair_list: list[tuple[str, str]] = []
    for pair in pairs:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise ConversionError("each pair must be (source account id, target account id)")
        _account_id_bytes(pair[0], "source account id")
        _account_id_bytes(pair[1], "target account id")
        pair_list.append((pair[0], pair[1]))
    sources = [source for source, _ in pair_list]
    targets = [target for _, target in pair_list]
    if len(set(sources)) != len(sources):
        raise ConversionError("a source account appears twice in the batch")
    if len(set(targets)) != len(targets):
        raise ConversionError("a target account appears twice in the batch")

    posted: list[str] = []
    skipped: list[str] = []
    zero_balance: list[str] = []
    for source_account_id, target_account_id in sorted(pair_list):
        existing = _find_transaction(ledger, conversion_transaction_id(declaration, source_account_id))
        if existing is not None:
            done = read_posted_conversion(
                existing,
                declaration,
                clearing_source_account_id=clearing_source_account_id,
                clearing_target_account_id=clearing_target_account_id,
            )
            if done.target_account_id != target_account_id:
                raise ConversionReplayError(
                    f"account {source_account_id!r} was already converted into {done.target_account_id!r}"
                )
            skipped.append(source_account_id)
            continue
        transaction = post_conversion(
            ledger,
            declaration,
            source_account_id=source_account_id,
            target_account_id=target_account_id,
            clearing_source_account_id=clearing_source_account_id,
            clearing_target_account_id=clearing_target_account_id,
            effective_at=effective_at,
            initiator_id=initiator_id,
        )
        (zero_balance if transaction is None else posted).append(source_account_id)
    return ConversionBatchResult(posted=tuple(posted), skipped=tuple(skipped), zero_balance=tuple(zero_balance))


# ---------------------------------------------------------------------------
# Closing and the rounding residue
# ---------------------------------------------------------------------------


def _source_currency_accounts(ledger: Ledger, declaration: ConversionDeclaration) -> list[Account]:
    return [account for account in ledger.list_accounts() if _has_currency(account, declaration.source)]


def _posted_conversions(
    ledger: Ledger,
    declaration: ConversionDeclaration,
    clearing_source_account_id: str,
    clearing_target_account_id: str,
) -> dict[str, PostedConversion]:
    """Every conversion transaction of this declaration, found through the derived ids."""
    found: dict[str, PostedConversion] = {}
    for account in _source_currency_accounts(ledger, declaration):
        if account.account_id == clearing_source_account_id:
            continue
        try:
            transaction_id = conversion_transaction_id(declaration, account.account_id)
        except ConversionError:
            continue  # an id that cannot be a slot id was never converted by this module
        transaction = _find_transaction(ledger, transaction_id)
        if transaction is not None:
            found[account.account_id] = read_posted_conversion(
                transaction,
                declaration,
                clearing_source_account_id=clearing_source_account_id,
                clearing_target_account_id=clearing_target_account_id,
            )
    return found


def read_closing(
    ledger: Ledger,
    declaration: ConversionDeclaration | dict,
    *,
    clearing_source_account_id: str,
    clearing_target_account_id: str,
    residue_account_id: str,
    effective_at: "datetime",
) -> tuple[int, bool]:
    """Check the closing conditions without posting anything.

    Returns ``(S, closed)``: ``S`` is the sum of the target-clearing entries of this
    declaration's conversion transactions, which is the amount the residue account receives;
    ``closed`` says whether the closing entry for ``S`` is booked (always true when ``S`` is
    0). Raises :class:`ConversionIncompleteError` when an account of the source currency
    still has a balance at ``effective_at``, and :class:`ConversionError` when the target
    clearing account holds anything but this declaration's conversions.
    """
    declaration = _as_declaration(declaration)
    effective_at = _effective_instant(declaration, effective_at)
    _check_clearing_accounts(ledger, declaration, clearing_source_account_id, clearing_target_account_id)
    residue = _get_account(ledger, residue_account_id, "residue_account_id")
    if residue.owner_type is not OwnerType.FX_GAIN_LOSS:
        raise ConversionError("the residue account must have the type FX_GAIN_LOSS")
    if not _has_currency(residue, declaration.target):
        raise ConversionError(f"the residue account must be in {declaration.target.code}")
    if len({clearing_source_account_id, clearing_target_account_id, residue_account_id}) != 3:
        raise ConversionError("the clearing accounts and the residue account must be three different accounts")

    # 1. every account of the source currency is at zero
    for account in _source_currency_accounts(ledger, declaration):
        if ledger.balance(account.account_id, as_of=effective_at).cents != 0:
            raise ConversionIncompleteError(
                f"account {account.account_id!r} still has a balance in {declaration.source.code} at the effective instant"
            )

    # 2. the target clearing account holds exactly this declaration's conversions
    conversions = _posted_conversions(ledger, declaration, clearing_source_account_id, clearing_target_account_id)
    s = -sum(done.target_amount for done in conversions.values())
    if abs(s) > AMOUNT_MAX:
        raise ConversionError("the rounding difference exceeds 10**38 - 1 in magnitude")
    booked = 0
    closing = _find_transaction(ledger, closing_transaction_id(declaration))
    if closing is not None:
        expected_entries = [(clearing_target_account_id, -s), (residue_account_id, s)]
        if (
            s == 0
            or closing.status is not TransactionStatus.SETTLED
            or closing.purpose_code != CLOSING_PURPOSE_CODE
            or [(e.account_id, e.amount.cents) for e in closing.entries] != expected_entries
        ):
            raise ConversionError(
                "a closing transaction exists for this declaration, but not for this amount and these accounts"
            )
        booked = s
    if ledger.balance(clearing_target_account_id, as_of=INSTANT_MAX).cents != s - booked:
        raise ConversionError(
            "the target clearing account holds something else than this declaration's conversions; "
            "use one clearing account per currency for each declaration"
        )
    return s, (s == 0 or closing is not None)


def close_conversion(
    ledger: Ledger,
    declaration: ConversionDeclaration | dict,
    *,
    clearing_source_account_id: str,
    clearing_target_account_id: str,
    residue_account_id: str,
    effective_at: "datetime",
    initiator_id: str,
) -> int:
    """Close a conversion and book the whole-ledger rounding difference.

    Once every account of the source currency has been converted, the source clearing account
    is back at zero, and the target clearing account holds ``S``, minus the sum of all
    converted amounts: in general not zero, because every account was rounded on its own.
    If ``S`` is not 0, one closing transaction books ``(target clearing account, -S)`` and
    ``(residue account, +S)``. The residue account has the type ``FX_GAIN_LOSS`` and belongs
    to the institution, which bears the difference; no holder's amount is ever adjusted.

    Returns ``S``, the change of the residue account. ``S`` is not what rounding gave or cost
    the institution, because the institution's own converted accounts (reserve and platform
    accounts, for example) were rounded as well. That amount, the institution's own accounts
    after the conversion (the clearing and residue accounts included) minus the exact
    conversion of their balances at the cut-off, is minus the sum of the customers' residues
    ``e = y - z``. The conversion evidence reports the customers' residues per side as
    ``sum_of_residues``, the receivables as magnitudes, so the amount equals
    ``receivables.sum_of_residues - liabilities.sum_of_residues``. ``S`` can be 0 or have the
    other sign: at 2 source units per target unit, rounded half away from zero, a customer at
    +3, a reserve account at +3 and platform accounts at -3, -1, -1 and -1 give ``S = 1``,
    while the institution's accounts end 1/2 below their exact conversion, the 1/2 by which
    the customer's converted 2 exceeds the exact 3/2. Running the function again changes
    nothing.
    """
    declaration = _as_declaration(declaration)
    if type(initiator_id) is not str or not initiator_id:
        raise ConversionError("initiator_id must be non-empty text")
    s, closed = read_closing(
        ledger,
        declaration,
        clearing_source_account_id=clearing_source_account_id,
        clearing_target_account_id=clearing_target_account_id,
        residue_account_id=residue_account_id,
        effective_at=effective_at,
    )
    if closed:
        return s
    target_currency = ledger.get_account(clearing_target_account_id).currency
    transaction_id = closing_transaction_id(declaration)
    record = canonical_json(
        {"declaration_hash": declaration.declaration_hash, "residue_account_change": amount_to_text(s)}
    ).decode("utf-8")
    legs = [(clearing_target_account_id, -s), (residue_account_id, s)]
    ledger.post(
        Transaction(
            transaction_id=transaction_id,
            entries=tuple(
                Entry(
                    entry_id=f"{transaction_id}:{sequence}",
                    transaction_id=transaction_id,
                    account_id=account_id,
                    amount=Money(cents=cents, currency=target_currency),
                    sequence=sequence,
                )
                for sequence, (account_id, cents) in enumerate(legs)
            ),
            purpose_code=CLOSING_PURPOSE_CODE,
            initiator_id=initiator_id,
            initiated_at=effective_at,
            settled_at=effective_at,
            status=TransactionStatus.SETTLED,
            external_refs=(),
            compliance_decision=None,
            metadata=((CLOSING_METADATA_KEY, record),),
        )
    )
    return s
