from django import template

register = template.Library()


@register.filter
def num(value, digits=1):
    """Number format with thousands separators: 12345.678 -> 12,345.7"""
    if value is None or value == "":
        return "–"
    try:
        if float(value) == float("inf"):
            return "∞"
        s = f"{float(value):,.{int(digits)}f}"
    except (TypeError, ValueError):
        return value
    return s


@register.filter
def pct(value, digits=1):
    return num(None if value is None else float(value) * 100, digits)


@register.filter
def get(mapping, key):
    """{{ dict|get:key }} — a value by a variable key ('' when missing)."""
    if not mapping:
        return ""
    v = mapping.get(key, "")
    return "" if v is None else v


@register.filter
def plain(value):
    """A number for an input's value/placeholder: 2.5 -> 2.5, 3.0 -> 3, 2.4e6 -> 2400000."""
    if value in (None, ""):
        return ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return value
    text = f"{v:.6f}".rstrip("0").rstrip(".")  # never exponent notation: 2400000, 0.25
    return text if abs(v) < 1e5 else f"{v:.0f}"



@register.filter
def mm(value):
    """Metres shown as whole millimetres: 1.6 -> 1,600."""
    return num(None if value in (None, "") else float(value) * 1000, 0)


@register.filter
def neg_mm(value):
    """A level below zero shown as a positive depth in mm: -1.3 -> 1,300."""
    return num(None if value in (None, "") else -float(value) * 1000, 0)


@register.filter
def multiply(value, arg):
    return None if value in (None, "") else float(value) * float(arg)


@register.filter
def divide(value, arg):
    try:
        return None if value in (None, "") or float(arg) == 0 else float(value) / float(arg)
    except (TypeError, ValueError):
        return None


@register.filter
def subtract(value, arg):
    try:
        return None if value in (None, "") else float(value) - float(arg)
    except (TypeError, ValueError):
        return None
