"""Feed catalogue: picks the feed to use from the fish weight."""
from dataclasses import dataclass


@dataclass
class Feed:
    name: str
    min_weight_g: float
    max_weight_g: float
    protein: float  # fraction, e.g. 0.55
    fcr: float


def select_feed(feeds: list[Feed], weight_g: float) -> Feed | None:
    """Return the first feed whose [min, max) weight range contains the weight."""
    for f in sorted(feeds, key=lambda f: f.min_weight_g):
        if f.min_weight_g <= weight_g < f.max_weight_g:
            return f
    return None
