"""One clock (tg_tools_common.clocks): one rounding rule, three renderings (SV-condensed D2).

| case                                   | proves                                                     |
| one rounding rule                      | 3599.6 s is the same second everywhere; halves go up       |
| from the first hour                    | M:SS below an hour, H:MM:SS above it, no 60:00              |
| iTunes field                           | HH:MM:SS always: a 12 s item is 00:00:12, not 00:0:12      |
| unknown and impossible times           | None, a negative and a NaN raise; nothing renders as ""    |
"""
import math

import pytest

from tg_tools_common import clocks


def test_one_rounding_rule_for_every_surface():
    # The pilot's disagreement: the notes truncated, the page and the seek line rounded.
    assert clocks.whole(3599.6) == 3600
    assert clocks.clock(3599.6) == clocks.clock(3599.9) == "1:00:00"
    assert clocks.itunes(3599.6) == "01:00:00"
    assert clocks.whole(3599.4) == 3599
    assert clocks.clock(3599.4) == "59:59"


def test_halves_round_up_like_every_other_second():
    assert [clocks.whole(s) for s in (0.5, 1.5, 2.5, 3.5)] == [1, 2, 3, 4]


def test_a_clock_is_m_ss_below_the_hour_and_h_mm_ss_above():
    assert clocks.clock(0) == "0:00"
    assert clocks.clock(12) == "0:12"
    assert clocks.clock(59.6) == "1:00"
    assert clocks.clock(754) == "12:34"
    assert clocks.clock(3600) == "1:00:00"
    assert clocks.clock(3600 + 754) == "1:12:34"
    assert clocks.clock(3 * 3600 + 5) == "3:00:05"


def test_the_itunes_field_is_hh_mm_ss():
    assert clocks.itunes(12) == "00:00:12"
    assert clocks.itunes(754) == "00:12:34"
    assert clocks.itunes(3600) == "01:00:00"
    assert clocks.itunes(36 * 3600) == "36:00:00"


def test_an_unknown_time_has_no_clock():
    for value in (None, -1, -0.4, float("nan"), math.inf, -math.inf):
        with pytest.raises((TypeError, ValueError)):
            clocks.clock(value)
