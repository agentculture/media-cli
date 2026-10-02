"""Pin ``compile._scaled`` (the size tracker behind a ``scale`` node) before refactoring.

Sonar S2589 flagged ``h <= 0`` in the third branch as always true: once
``w > 0 and h > 0`` and ``w <= 0 and h > 0`` have both returned, ``h > 0`` is
impossible, so only ``w > 0`` discriminates.  These cases pin every branch,
including the fall-through for ``w <= 0 and h <= 0`` (keep the current size).
"""

from __future__ import annotations

import itertools

import pytest

from media_cli.media import compile as C


@pytest.mark.parametrize(
    "w,h,cur,expected",
    [
        # both explicit: taken as-is, current size irrelevant
        (160, 120, (320, 240), (160, 120)),
        (161, 121, (None, None), (161, 121)),
        # unknown current size and an auto side: unknown result
        (-1, 120, (None, 240), (None, None)),
        (160, -1, (320, None), (None, None)),
        (-1, -1, (0, 240), (None, None)),
        # width derived from height (-1 keeps parity as computed, -2 rounds up to even)
        (-1, 120, (320, 240), (160, 120)),
        (-1, 121, (320, 240), (161, 121)),
        (-2, 121, (320, 240), (162, 121)),
        (0, 121, (320, 240), (161, 121)),
        # height derived from width
        (160, -1, (320, 240), (160, 120)),
        (161, -1, (320, 240), (161, 121)),
        (161, -2, (320, 240), (161, 122)),
        (161, 0, (320, 240), (161, 121)),
        # neither side given: keep the current size
        (-1, -1, (320, 240), (320, 240)),
        (-2, -2, (320, 240), (320, 240)),
        (0, 0, (320, 240), (320, 240)),
        (-1, 0, (300, 200), (300, 200)),
    ],
)
def test_scaled_pins_every_branch(w, h, cur, expected):
    assert C._scaled(w, h, cur) == expected


def _reference(w, h, cur):
    """The pre-refactor body, verbatim, as an oracle."""
    cw, ch = cur
    if w > 0 and h > 0:
        return w, h
    if not cw or not ch:
        return None, None
    if w <= 0 and h > 0:
        w2 = round(h * cw / ch)
        return (w2 + (w2 % 2) if w == -2 else w2), h
    if h <= 0 and w > 0:
        h2 = round(w * ch / cw)
        return w, (h2 + (h2 % 2) if h == -2 else h2)
    return cw, ch


def test_scaled_matches_reference_over_a_grid():
    sides = (-2, -1, 0, 1, 99, 160, 161)
    currents = ((None, None), (0, 240), (320, 0), (320, 240), (333, 187), (1, 1))
    for w, h, cur in itertools.product(sides, sides, currents):
        assert C._scaled(w, h, cur) == _reference(w, h, cur), (w, h, cur)
