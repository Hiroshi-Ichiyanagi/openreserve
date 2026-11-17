"""core/exact.py — exact encodings shared by every artifact format.

Three things live here, and nothing else:

1. Amounts. An amount is a Python ``int`` in minor units, bounded by ``AMOUNT_MAX``. It has one
   text form (canonical decimal) and one byte form (16 bytes, unsigned, big-endian).
2. Canonical JSON. A strict profile without numbers, used for every document that is hashed.
3. Instants. A timezone-aware datetime has one text form: UTC with six fractional digits.

Nothing in this module reads a clock, a random source or the network, and nothing uses a
floating-point value. Every refusal raises :class:`ExactValueError` (a ``ValueError``).

The rules are specified in docs/hypothetical/W_CHANGEOVER_DESIGN.md, sections 4.1 to 4.3.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

__all__ = [
    "AMOUNT_MAX",
    "INSTANT_MAX",
    "ExactValueError",
    "NaiveDatetimeError",
    "amount_from_text",
    "amount_to_text",
    "amount_to_u128",
    "canonical_json",
    "instant_from_stored_text",
    "instant_from_text",
    "instant_to_text",
    "require_aware",
    "sha256_hex",
]

#: Largest magnitude of any amount, sum or total. Two values in range add up to less than
#: 2**128, so a verifier using unsigned 128-bit arithmetic cannot overflow in one addition
#: before it checks the range.
AMOUNT_MAX = 10**38 - 1

#: Deepest nesting ``canonical_json`` accepts. No format of this library nests deeper than
#: ten levels; the limit keeps a hostile document from exhausting the interpreter stack.
MAX_JSON_DEPTH = 64


class ExactValueError(ValueError):
    """A value does not fit the exact encodings of this module."""


class NaiveDatetimeError(ExactValueError):
    """A datetime without a UTC offset was given where an instant is required."""


# ---------------------------------------------------------------------------
# Amounts
# ---------------------------------------------------------------------------

# ``[0-9]`` is deliberate: ``\d`` would also match non-ASCII digits.
_SIGNED_DECIMAL = re.compile(r"-?[1-9][0-9]{0,37}|0")
_UNSIGNED_DECIMAL = re.compile(r"[1-9][0-9]{0,37}|0")


def amount_to_text(value: int) -> str:
    """Canonical decimal text of an amount: no ``+``, no leading zero, no ``-0``.

    Refuses anything that is not exactly an ``int`` (so ``bool`` and ``float`` are refused)
    and any value whose magnitude exceeds ``AMOUNT_MAX``.
    """
    if type(value) is not int:
        raise ExactValueError(f"amount must be int, got {type(value).__name__}")
    if value > AMOUNT_MAX or value < -AMOUNT_MAX:
        raise ExactValueError("amount magnitude exceeds 10**38 - 1")
    return str(value)


def amount_from_text(text: str, *, signed: bool = True) -> int:
    """Parse canonical decimal text. The whole string must match; nothing is stripped.

    With ``signed=False`` a leading ``-`` is refused as well.
    """
    if type(text) is not str:
        raise ExactValueError(f"amount text must be str, got {type(text).__name__}")
    pattern = _SIGNED_DECIMAL if signed else _UNSIGNED_DECIMAL
    if pattern.fullmatch(text) is None:
        raise ExactValueError("amount text is not canonical decimal within 10**38 - 1")
    return int(text)


def amount_to_u128(value: int) -> bytes:
    """Sixteen bytes, unsigned, big-endian. Only for ``0 <= value <= AMOUNT_MAX``."""
    if type(value) is not int:
        raise ExactValueError(f"amount must be int, got {type(value).__name__}")
    if value < 0 or value > AMOUNT_MAX:
        raise ExactValueError("amount is outside 0 .. 10**38 - 1")
    return value.to_bytes(16, "big")


# ---------------------------------------------------------------------------
# Canonical JSON
# ---------------------------------------------------------------------------

_KEY = re.compile(r"[a-z][a-z0-9_]*")


def _check_canonical_value(obj: Any, depth: int) -> None:
    if obj is None or obj is True or obj is False:
        return
    kind = type(obj)
    if kind is str:
        try:
            obj.encode("utf-8")
        except UnicodeEncodeError:
            raise ExactValueError("string contains a lone surrogate") from None
        return
    if depth >= MAX_JSON_DEPTH:
        raise ExactValueError(f"document is nested deeper than {MAX_JSON_DEPTH} levels")
    if kind is dict:
        for key, value in obj.items():
            if type(key) is not str or _KEY.fullmatch(key) is None:
                raise ExactValueError(f"object key {key!r} does not match [a-z][a-z0-9_]*")
            _check_canonical_value(value, depth + 1)
        return
    if kind is list:
        for value in obj:
            _check_canonical_value(value, depth + 1)
        return
    raise ExactValueError(
        f"{kind.__name__} is not allowed in a canonical document "
        "(objects, arrays, strings, true, false and null only; integers are written as strings)"
    )


def canonical_json(obj: Any) -> bytes:
    """Canonical UTF-8 bytes of a document in the strict profile.

    Allowed values: ``dict`` with keys matching ``[a-z][a-z0-9_]*``, ``list``, ``str``,
    ``True``, ``False`` and ``None``. Numbers are not allowed at all. The output has no
    insignificant whitespace, keys sorted by code point and the string escapes of RFC 8785.
    For such documents the bytes equal RFC 8785 output.
    """
    _check_canonical_value(obj, 0)
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """SHA-256 of ``data`` in lower-case hexadecimal."""
    if not isinstance(data, (bytes, bytearray)):
        raise ExactValueError(f"sha256_hex needs bytes, got {type(data).__name__}")
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Instants
# ---------------------------------------------------------------------------

_CANONICAL_INSTANT = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})\.([0-9]{6})Z"
)
# What ``datetime.isoformat()`` writes: optional microseconds, optional offset. Older ledger
# files hold this form.
_LEGACY_INSTANT = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{6}))?"
    r"(?:([+-])([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:\.([0-9]{6}))?)?)?"
)


#: The last representable instant. As a cut-off it means "over all time".
INSTANT_MAX = datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)


def require_aware(value: datetime, name: str = "datetime") -> datetime:
    """Return ``value`` unchanged if it is a timezone-aware datetime.

    A naive datetime raises :class:`NaiveDatetimeError`: its instant would depend on a guess
    about the zone. Anything that is not a datetime raises :class:`ExactValueError`.
    """
    if not isinstance(value, datetime):
        raise ExactValueError(f"{name} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise NaiveDatetimeError(
            f"{name} has no UTC offset; pass a timezone-aware datetime "
            "(for example with tzinfo=timezone.utc)"
        )
    return value


def instant_to_text(value: datetime) -> str:
    """The one text of an instant: ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (UTC, 27 characters).

    The value is converted to UTC first. The text is built from the fields, not with
    ``strftime``, whose padding of years below 1000 differs between platforms.
    """
    require_aware(value, "instant")
    try:
        utc = value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise ExactValueError("instant is outside the representable range in UTC") from None
    return (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}"
        f"T{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}.{utc.microsecond:06d}Z"
    )


def instant_from_text(text: str) -> datetime:
    """Parse the canonical instant text, and only that form. The result is aware, in UTC."""
    if type(text) is not str:
        raise ExactValueError(f"instant text must be str, got {type(text).__name__}")
    match = _CANONICAL_INSTANT.fullmatch(text)
    if match is None:
        raise ExactValueError("instant text is not of the form YYYY-MM-DDTHH:MM:SS.ffffffZ")
    year, month, day, hour, minute, second, micro = (int(g) for g in match.groups())
    try:
        return datetime(year, month, day, hour, minute, second, micro, tzinfo=timezone.utc)
    except ValueError:
        raise ExactValueError("instant text does not name a valid date and time") from None


def instant_from_stored_text(text: str) -> datetime:
    """Parse a timestamp read from a ledger file. The result is aware, in UTC.

    Accepted: the canonical text, and the ISO-8601 form with an explicit offset that earlier
    versions wrote (``2026-01-01T09:00:00+09:00``, optionally with microseconds). A value
    without an offset raises :class:`NaiveDatetimeError`: it cannot be read as an instant.
    """
    if type(text) is not str:
        raise ExactValueError(f"stored instant must be str, got {type(text).__name__}")
    if _CANONICAL_INSTANT.fullmatch(text) is not None:
        return instant_from_text(text)
    match = _LEGACY_INSTANT.fullmatch(text)
    if match is None:
        raise ExactValueError("stored instant is neither canonical nor ISO-8601 with an offset")
    year, month, day, hour, minute, second = (int(g) for g in match.groups()[:6])
    micro = int(match.group(7) or "0")
    sign = match.group(8)
    if sign is None:
        raise NaiveDatetimeError("stored instant has no UTC offset")
    offset = timedelta(
        hours=int(match.group(9)),
        minutes=int(match.group(10)),
        seconds=int(match.group(11) or "0"),
        microseconds=int(match.group(12) or "0"),
    )
    if int(match.group(10)) > 59 or int(match.group(11) or "0") > 59 or offset >= timedelta(hours=24):
        raise ExactValueError("stored instant has an invalid UTC offset")
    if sign == "-":
        offset = -offset
    try:
        local = datetime(year, month, day, hour, minute, second, micro, tzinfo=timezone(offset))
        return local.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise ExactValueError("stored instant does not name a valid date and time") from None
