"""One clock for every reader surface: notes heading, page stamp, seek line, iTunes field.

The pilot of the condensed Surveillance edition had five clock functions and two rounding rules: a
paragraph at 3 599.6 s read ``59:59`` in the notes (truncated) and ``1:00:00`` on the page and in the
seek line (rounded), and the iTunes field was built as ``00:MM:SS``, which is malformed under a minute
(SV-condensed D2). One rounding rule here, and each surface derives its text from it:

* ``whole`` — the rounding rule (nearest second, halves up);
* ``clock`` — ``M:SS``, or ``H:MM:SS`` from the first hour: the notes heading, the page stamp, a seek line;
* ``itunes`` — ``HH:MM:SS`` always: ``<itunes:duration>``.

A caller with an unknown time has no clock: pass a number or handle the absence itself. ``None`` is
never rendered as an empty string (an empty duration in an item is SV-condensed D15).
"""
from __future__ import annotations

import math


def whole(seconds: float) -> int:
    """The one rounding rule: the nearest whole second, halves rounded up.

    ``round()`` is left out on purpose: it rounds halves to even, so 2.5 s and 3.5 s would move in
    opposite directions and two calls over the same paragraph could disagree.
    """
    value = float(seconds)
    if math.isnan(value) or math.isinf(value):
        raise ValueError("no clock for %r" % (seconds,))
    if value < 0:
        raise ValueError("a clock is not negative: %r" % (seconds,))
    return int(math.floor(value + 0.5))


def clock(seconds: float) -> str:
    """``M:SS``, or ``H:MM:SS`` from the first hour: a notes heading, a page stamp, a seek line."""
    total = whole(seconds)
    if total >= 3600:
        return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"
    return f"{total // 60}:{total % 60:02d}"


def itunes(seconds: float) -> str:
    """``HH:MM:SS``, the item's ``<itunes:duration>``: ``12`` is ``00:00:12``, not ``00:0:12``."""
    total = whole(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"
