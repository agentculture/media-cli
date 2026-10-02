"""Characterization tests for the sonar p-sense refactor of ``media.regions``.

``_build_regions`` and ``_validate`` were split into helpers (S3776).  The frozen
pre-refactor bodies below are the oracle: on randomized inputs the refactored
functions must produce identical regions, gaps, edges and rejected-box records.
That keeps the fail-closed coverage contract (o9/o10) byte-identical, including
the single-sample and unpaired-box paths the rest of the suite never reaches.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from media_cli.media import regions as R
from media_cli.media.regions import (
    POINT_SPAN,
    _as_int,
    _Edges,
    _lerp,
    _pair,
    _region,
    _union,
)


def _orig_validate(
    reply: Any, width: int, height: int, t: float, rejected: list[dict]
) -> list[_Edges] | None:
    """Valid boxes for one frame as edges, or ``None`` (= no detection) for any malformed reply."""
    items = reply.get("boxes") if isinstance(reply, dict) else None
    if not isinstance(items, list):
        rejected.append({"t": t, "box": reply, "reason": "malformed"})
        return None
    ints: list[tuple[dict, int, int, int, int]] = []
    for item in items:
        vals = [_as_int(item.get(k)) for k in "xywh"] if isinstance(item, dict) else [None]
        if None in vals or len(vals) != 4:
            rejected.append({"t": t, "box": item, "reason": "malformed"})
            return None  # a box we cannot read may be the object: discard the frame
        ints.append((item, *vals))  # type: ignore[arg-type]
    out: list[_Edges] = []
    for item, x, y, w, h in ints:
        if w <= 0 or h <= 0:
            rejected.append({"t": t, "box": item, "reason": "non_positive_size"})
            continue
        x1, y1, x2, y2 = max(0, x), max(0, y), min(width, x + w), min(height, y + h)
        if x2 <= x1 or y2 <= y1:
            rejected.append({"t": t, "box": item, "reason": "outside_frame"})
            continue
        if (x1, y1, x2, y2) != (x, y, x + w, y + h):
            rejected.append({"t": t, "box": item, "reason": "clamped"})
        out.append((float(x1), float(y1), float(x2), float(y2)))
    return out or None


def _orig_build_regions(
    samples: list[tuple[float, list[_Edges] | None]],
    method: str,
    substeps: int,
    margin: int,
    width: int,
    height: int,
) -> tuple[list[dict], list[list[float]]]:
    regions: list[dict] = []
    gaps: list[list[float]] = []

    def emit(e: _Edges, a: float, b: float) -> None:
        r = _region(e, a, b, margin, width, height)
        if r is not None:
            regions.append(r)

    n = len(samples)
    if n == 1:
        t, boxes = samples[0]
        if boxes is None:
            gaps.append([t, t])
        for e in boxes or []:
            emit(e, t, t + POINT_SPAN)
        return regions, gaps
    uncovered = [False] * (n - 1)
    for i in range(n - 1):
        (ta, ba), (tb, bb) = samples[i], samples[i + 1]
        if ba is None or bb is None:
            uncovered[i] = True
            if gaps and gaps[-1][1] == ta:
                gaps[-1][1] = tb
            else:
                gaps.append([ta, tb])
            continue
        pairs, rest = _pair(ba, bb)
        for e in rest:
            emit(e, ta, tb)
        for p, q in pairs:
            if method == "hold":
                emit(_union(p, q), ta, tb)
                continue
            for k in range(substeps):
                f0, f1 = k / substeps, (k + 1) / substeps
                emit(
                    _union(_lerp(p, q, f0), _lerp(p, q, f1)),
                    ta + (tb - ta) * f0,
                    ta + (tb - ta) * f1,
                )
    for i, (t, boxes) in enumerate(samples):
        adjacent = [uncovered[j] for j in (i - 1, i) if 0 <= j < n - 1]
        if boxes and any(adjacent):
            for e in boxes:
                emit(e, t, t + POINT_SPAN)
    return regions, gaps


def _rand_box(rng: random.Random) -> tuple[float, float, float, float]:
    x1, y1 = rng.uniform(-20, 300), rng.uniform(-20, 200)
    return (x1, y1, x1 + rng.uniform(0, 80), y1 + rng.uniform(0, 80))


def _rand_samples(rng: random.Random, n: int) -> list:
    t = 0.0
    out = []
    for _ in range(n):
        t += rng.choice([0.5, 0.25, 1.0])
        boxes = None if rng.random() < 0.3 else [_rand_box(rng) for _ in range(rng.randint(0, 3))]
        out.append((t, boxes))
    return out


@pytest.mark.parametrize("seed", range(100))
def test_build_regions_matches_pre_refactor(seed):
    rng = random.Random(seed)
    samples = _rand_samples(rng, rng.randint(1, 7))
    method = rng.choice(R.METHODS)
    substeps, margin = rng.randint(1, 5), rng.randint(0, 6)
    args = (samples, method, substeps, margin, 320, 240)
    assert R._build_regions(*args) == _orig_build_regions(*args)


def test_single_sample_without_detection_is_a_point_gap():
    assert R._build_regions([(1.0, None)], "hold", 4, 0, 320, 240) == ([], [[1.0, 1.0]])


def test_single_sample_with_detection_emits_point_region():
    regions, gaps = R._build_regions([(1.0, [(1.0, 2.0, 11.0, 12.0)])], "hold", 4, 0, 320, 240)
    assert gaps == []
    assert regions == [{"x": 1, "y": 2, "w": 10, "h": 10, "start": 1.0, "end": 1.0 + POINT_SPAN}]


def test_unpaired_box_is_held_over_the_interval():
    a = [(0.0, 0.0, 10.0, 10.0), (100.0, 100.0, 110.0, 110.0)]
    b = [(1.0, 1.0, 11.0, 11.0)]
    regions, gaps = R._build_regions([(0.0, a), (1.0, b)], "hold", 4, 0, 320, 240)
    assert gaps == []
    assert {"x": 100, "y": 100, "w": 10, "h": 10, "start": 0.0, "end": 1.0} in regions


def test_abutting_gaps_merge():
    samples = [(0.0, None), (1.0, None), (2.0, None), (3.0, [(0.0, 0.0, 5.0, 5.0)])]
    _regions, gaps = R._build_regions(samples, "hold", 4, 0, 320, 240)
    assert gaps == [[0.0, 3.0]]


def _rand_item(rng: random.Random):
    roll = rng.random()
    if roll < 0.05:
        return "not a dict"
    if roll < 0.1:
        return {"x": 1, "y": 2, "w": 3}
    vals = {k: rng.randint(-50, 400) for k in "xywh"}
    if rng.random() < 0.1:
        vals[rng.choice("xywh")] = rng.choice([1.5, True, None, "3", 7.0])
    return vals


@pytest.mark.parametrize("seed", range(100))
def test_validate_matches_pre_refactor(seed):
    rng = random.Random(seed)
    roll = rng.random()
    if roll < 0.05:
        reply = {"nope": []}
    elif roll < 0.1:
        reply = ["boxes"]
    else:
        reply = {"boxes": [_rand_item(rng) for _ in range(rng.randint(0, 4))]}
    new_rej: list = []
    old_rej: list = []
    assert R._validate(reply, 320, 240, 1.5, new_rej) == _orig_validate(
        reply, 320, 240, 1.5, old_rej
    )
    assert new_rej == old_rej
