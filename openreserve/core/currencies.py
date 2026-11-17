"""core/currencies.py — a currency described as plain data.

A :class:`CurrencySpec` is a code and a number of minor units, nothing more. The rules are
purely syntactic:

* the code matches ``[A-Z][A-Z0-9_]{0,31}``;
* a code that starts with ``FICTIONAL_`` marks a fictional entry and must have at least one
  more character; only fictional entries may use that prefix, so a fictional entry is
  visible in every artifact that prints its code;
* ``minor_units`` is an integer from 0 to 30.

The rules do not rely on any property of real currency code lists, and a spec says nothing
about whether a currency exists. The ledger keeps its own closed ``Currency`` enumeration;
:meth:`CurrencySpec.of` bridges from it.

A :class:`CurrencyRegistry` is an immutable, hash-identified list of specs, in the document
format ``openreserve.currency-registry/1`` (canonical JSON, entries sorted by code). Adding a
code that is already present is refused, also when the entry is identical; extending returns
a new registry and leaves the old one and its hash unchanged. :data:`DEFAULT_REGISTRY` holds
exactly the eight currencies of the ledger's ``Currency`` enumeration and no fictional entry.
A registry entry cannot yet be used for ledger accounts; effective dates, successor entries
and a registry hash pinned in ledger files are not part of this format version.

See docs/hypothetical/W_CHANGEOVER_DESIGN.md, section 9.4.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

from openreserve.core.exact import ExactValueError, amount_from_text, amount_to_text, canonical_json, sha256_hex

__all__ = [
    "DEFAULT_REGISTRY",
    "FICTIONAL_PREFIX",
    "FORMAT_REGISTRY",
    "MAX_MINOR_UNITS",
    "CurrencyRegistry",
    "CurrencyRegistryError",
    "CurrencySpec",
    "CurrencySpecError",
]

FICTIONAL_PREFIX = "FICTIONAL_"
MAX_MINOR_UNITS = 30

FORMAT_REGISTRY = "openreserve.currency-registry/1"

_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,31}")
_DOCUMENT_KEYS = frozenset({"code", "fictional", "minor_units"})
_REGISTRY_KEYS = frozenset({"format", "currencies"})


class CurrencySpecError(ExactValueError):
    """A currency code or a number of minor units breaks the rules of this module."""


@dataclass(frozen=True)
class CurrencySpec:
    """A currency as data: ``code`` and ``minor_units``."""

    code: str
    minor_units: int

    def __post_init__(self) -> None:
        if type(self.code) is not str or _CODE.fullmatch(self.code) is None:
            raise CurrencySpecError("a currency code must match [A-Z][A-Z0-9_]{0,31}")
        if self.code == FICTIONAL_PREFIX:
            raise CurrencySpecError(f"{FICTIONAL_PREFIX} must be followed by at least one character")
        if type(self.minor_units) is not int or not 0 <= self.minor_units <= MAX_MINOR_UNITS:
            raise CurrencySpecError(f"minor_units must be an int from 0 to {MAX_MINOR_UNITS}")

    @property
    def fictional(self) -> bool:
        """True exactly when the code starts with ``FICTIONAL_``."""
        return self.code.startswith(FICTIONAL_PREFIX)

    @classmethod
    def of(cls, currency: Any) -> CurrencySpec:
        """The spec of an object with ``code`` and ``minor_units`` (such as a ledger ``Currency``)."""
        return cls(code=currency.code, minor_units=currency.minor_units)

    def to_document(self) -> dict[str, Any]:
        """``{"code", "fictional", "minor_units"}`` with the integer written as a string."""
        return {"code": self.code, "fictional": self.fictional, "minor_units": amount_to_text(self.minor_units)}

    @classmethod
    def from_document(cls, document: object) -> CurrencySpec:
        """Parse the document form. The ``fictional`` flag must agree with the code."""
        if type(document) is not dict or set(document) != _DOCUMENT_KEYS:
            raise CurrencySpecError("a currency entry must be an object with code, fictional and minor_units")
        try:
            minor_units = amount_from_text(document["minor_units"], signed=False)
        except ExactValueError:
            raise CurrencySpecError("minor_units must be canonical unsigned decimal text") from None
        spec = cls(code=document["code"], minor_units=minor_units)
        if document["fictional"] is not spec.fictional:
            raise CurrencySpecError(
                f"the fictional flag must be true exactly for codes that start with {FICTIONAL_PREFIX}"
            )
        return spec


class CurrencyRegistryError(ExactValueError):
    """A registry document or an extension breaks the rules of this module."""


class CurrencyRegistry:
    """An immutable list of :class:`CurrencySpec`, sorted by code, identified by its hash.

    ``registry_hash`` is the SHA-256 of the canonical JSON of :meth:`to_document`.
    """

    __slots__ = ("_specs", "_by_code", "_hash")

    def __init__(self, specs: Iterable[CurrencySpec] = ()) -> None:
        given = list(specs)
        if any(type(spec) is not CurrencySpec for spec in given):
            raise CurrencyRegistryError("a registry holds CurrencySpec entries only")
        ordered = sorted(given, key=lambda spec: spec.code)
        by_code: dict[str, CurrencySpec] = {}
        for spec in ordered:
            if spec.code in by_code:
                raise CurrencyRegistryError(f"currency code {spec.code} is given twice")
            by_code[spec.code] = spec
        object.__setattr__(self, "_specs", tuple(ordered))
        object.__setattr__(self, "_by_code", by_code)
        object.__setattr__(self, "_hash", sha256_hex(canonical_json(self.to_document())))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("a CurrencyRegistry is immutable; use extended() for a new one")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("a CurrencyRegistry is immutable")

    def __iter__(self) -> Iterator[CurrencySpec]:
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, code: object) -> bool:
        return code in self._by_code

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CurrencyRegistry) and other._specs == self._specs

    def __hash__(self) -> int:
        return hash(self._specs)

    def __repr__(self) -> str:
        return f"CurrencyRegistry({', '.join(spec.code for spec in self._specs)}; {self._hash[:12]})"

    @property
    def codes(self) -> tuple[str, ...]:
        """The codes, sorted."""
        return tuple(spec.code for spec in self._specs)

    @property
    def registry_hash(self) -> str:
        """SHA-256 of the canonical JSON of the registry document, lower-case hexadecimal."""
        return self._hash

    def get(self, code: str) -> CurrencySpec:
        """The entry for ``code``; ``KeyError`` when there is none."""
        try:
            return self._by_code[code]
        except (KeyError, TypeError):
            raise KeyError(code) from None

    def extended(self, *specs: CurrencySpec) -> CurrencyRegistry:
        """A new registry with ``specs`` added. A code that is already present is refused,
        also when the entry is identical. This registry is not changed."""
        for spec in specs:
            if type(spec) is not CurrencySpec:
                raise CurrencyRegistryError("a registry holds CurrencySpec entries only")
            if spec.code in self._by_code:
                raise CurrencyRegistryError(f"currency code {spec.code} is already in the registry")
        return CurrencyRegistry(self._specs + specs)

    def to_document(self) -> dict[str, Any]:
        """``{"format": "openreserve.currency-registry/1", "currencies": [...]}``, sorted by code."""
        return {"format": FORMAT_REGISTRY, "currencies": [spec.to_document() for spec in self._specs]}

    @classmethod
    def from_document(cls, document: object, *, expected_hash: str | None = None) -> CurrencyRegistry:
        """Parse a registry document. Its entries must be in ascending order of code with no
        code twice, so that a document has one form. With ``expected_hash`` the registry hash
        must equal it."""
        if type(document) is not dict or set(document) != _REGISTRY_KEYS:
            raise CurrencyRegistryError("a registry document is an object with format and currencies")
        if document["format"] != FORMAT_REGISTRY:
            raise CurrencyRegistryError(f"a registry document has the format {FORMAT_REGISTRY}")
        entries = document["currencies"]
        if type(entries) is not list:
            raise CurrencyRegistryError("currencies must be a list")
        specs = [CurrencySpec.from_document(entry) for entry in entries]
        codes = [spec.code for spec in specs]
        if any(a >= b for a, b in zip(codes, codes[1:])):
            raise CurrencyRegistryError("registry entries must be in ascending order of code, each code once")
        registry = cls(specs)
        if expected_hash is not None and registry.registry_hash != expected_hash:
            raise CurrencyRegistryError("the registry hash is not the expected one")
        return registry


#: Exactly the eight currencies of the ledger's ``Currency`` enumeration; no fictional entry.
DEFAULT_REGISTRY = CurrencyRegistry(
    [
        CurrencySpec("EUR", 2),
        CurrencySpec("GBP", 2),
        CurrencySpec("JPY", 0),
        CurrencySpec("JPYC", 18),
        CurrencySpec("PROGMAT_JPY", 0),
        CurrencySpec("USD", 2),
        CurrencySpec("USDC", 6),
        CurrencySpec("USDT", 6),
    ]
)
