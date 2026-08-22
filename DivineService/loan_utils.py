import re

# Indian numbering-system suffix multipliers used to parse amounts like "1.5 lakh",
# "80k", "1 crore" into a plain rupee float. Order matters only for readability - the
# regex below matches the longest/most specific alias first via alternation order.
_SUFFIX_MULTIPLIERS = (
    (r"crores?|cr\b", 1e7),
    (r"lakhs?|lacs?|l\b", 1e5),
    (r"thousand|k\b", 1e3),
)

_AMOUNT_RE = re.compile(
    r"(?P<number>[\d,]+(?:\.\d+)?)\s*(?P<suffix>crores?|cr|lakhs?|lacs?|l|thousand|k)?",
    re.IGNORECASE,
)


def normalize_indian_amount(raw) -> float:
    """Parse an Indian-money expression into a plain rupee float, or None if unparseable.

    Handles "1.5 lakh", "₹1.5L", "95 lakhs", "1 crore", "80k", "150000", "₹75 lakh".
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)

    text = str(raw).strip().replace("₹", "").replace(",", "")
    if not text:
        return None

    match = _AMOUNT_RE.search(text)
    if not match or not match.group("number"):
        return None

    try:
        number = float(match.group("number"))
    except ValueError:
        return None

    suffix = (match.group("suffix") or "").lower()
    if not suffix:
        return number

    for pattern, multiplier in _SUFFIX_MULTIPLIERS:
        if re.fullmatch(pattern.replace(r"\b", ""), suffix, re.IGNORECASE):
            return number * multiplier

    return number
