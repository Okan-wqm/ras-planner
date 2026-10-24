"""Numeric parsing shared by the views: comma-tolerant, None when a value is not a number."""
import math


def _finite(v, digits=4):
    """JSON has no Infinity; send null instead. Text (e.g. what sets the flow) passes as it is."""
    if isinstance(v, str):
        return v
    return None if v is None or v == float("inf") else round(v, digits)


def _int_or_none(value):
    if value is None or not str(value).strip():
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _float_or_none(value):
    if value is None or not str(value).strip():
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def _num(v):
    try:
        n = None if v in (None, "") else float(str(v).replace(",", "."))
    except ValueError:
        return None
    return n if n is not None and math.isfinite(n) else None  # nan/inf are not inputs
