"""Canonical decimal parsing and fixed-point integer arithmetic.

Doses must be *canonical decimal strings*: e.g. ``12``, ``0.5``, ``3.140``
is rejected because ``3.14`` is the canonical form, and scientific
notation (``1E2``) is rejected. This removes ambiguity for idempotency
comparisons (the same dose must byte-for-byte mean the same thing) while
arithmetic itself is done in micro-units (1e-6) integers.
"""
from __future__ import annotations

from typing import Iterable

# Resolution: 6 decimal places. Everything in the clinical spec is
# expressed with far fewer digits; any finer input is rejected instead
# of silently rounded.
SCALE = 1_000_000

_CANON_RE = __import__("re").compile(r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")


class DecimalError(ValueError):
    """Raised when a dose string is not a positive canonical decimal."""


def parse_dose(text: str, *, positive: bool = True) -> int:
    """Parse a canonical decimal dose string into micro-units.

    Rules:
      * no sign, no whitespace, no exponent, no underscores
      * no leading zeros, no trailing zeros after the decimal point
      * at most 6 fractional digits
      * when ``positive``, strictly greater than zero
    """
    if not isinstance(text, str):
        raise DecimalError("dose must be a decimal string")
    if not _CANON_RE.fullmatch(text):
        raise DecimalError(f"non-canonical or non-decimal dose: {text!r}")
    int_part, _, frac_part = text.partition(".")
    if frac_part:
        if len(frac_part) > 6:
            raise DecimalError(f"dose has more than 6 decimal places: {text!r}")
        if frac_part.endswith("0"):
            raise DecimalError(f"non-canonical (trailing zero) dose: {text!r}")
    value = int(int_part) * SCALE + (int(frac_part.ljust(6, "0")) if frac_part else 0)
    if positive and value <= 0:
        raise DecimalError(f"dose must be positive: {text!r}")
    return value


def to_canonical(micro: int) -> str:
    """Format micro-units back into a canonical decimal string."""
    if micro == 0:
        return "0"
    whole, frac = divmod(micro, SCALE)
    if frac == 0:
        return str(whole)
    frac_str = str(frac).rjust(6, "0").rstrip("0")
    return f"{whole}.{frac_str}"


def sum_micro(values: Iterable[int]) -> int:
    total = 0
    for v in values:
        total += v
    return total
