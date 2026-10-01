"""Model-derived redaction regions with an honest coverage report (task t17).

Public API
----------
``find_regions(path, description, *, fps=2.0, method="hold", start=None, end=None,
client=None, workdir=None, margin=0, substeps=4) -> dict``

Samples frames of *path* at ``fps`` (plus one terminal sample on the last frame
of the range so every interval is bracketed), asks the local ``senses`` role
(Gemma 4) where *description* is on each sampled frame, and returns regions you
can drop straight into a ``box`` / ``blur`` op::

    {"regions": [{"x","y","w","h","start","end"}, ...],   # pixels, absolute normalized seconds
     "coverage": {"sample_fps", "method", "samples",
                  "frames_without_detection": [[a, b], ...],
                  "rejected_boxes": [{"t", "box", "reason"}, ...]},
     "model", "description"}

Nothing is rendered here: the returned dict *is* the dry-run, every region is
listed before any render.  The model's box accuracy is not measured by this
module; ``coverage`` states only what was sampled and what was not (o10).

Fail closed
-----------
* The reply must be a JSON object ``{"boxes": [{"x","y","w","h"}, ...]}`` of
  integers.  A reply that is not (or a box that is not four integers) is
  discarded for that frame -- the frame counts as *no detection*, nothing is
  guessed.  ``rejected_boxes`` records it (``reason: malformed``).
* A box with ``w<=0`` or ``h<=0`` (``non_positive_size``) or no area inside the
  frame (``outside_frame``) is dropped; a box partly outside is clamped
  (``clamped``, the original is recorded).
* An empty ``boxes`` list is also *no detection*: absence on a sampled frame is
  not proof that the object is absent on the frames in between.
* ``env.sense_not_local`` (and any non-``sense_unavailable`` error) propagates.
  A ``sense_unavailable`` failure on one frame is a rejected sample; if it hits
  every sample the first error is re-raised instead of reporting an empty run.

Interval rules (interval ``i`` = ``[t_i, t_{i+1}]`` between two consecutive
sampled frames, ``t`` = the frame's own normalized time)
-----------------------------------------------------------------------------
An interval is *covered* only if BOTH bounding samples detected something;
otherwise it is uncovered and nothing is held across it.  Multiple boxes per
frame: boxes of the two samples are paired greedily by nearest centre; a box
with no partner is held alone over the interval.  (Limitation: objects that
cross paths may be paired wrongly; the union of a wrong pair is still emitted
together with nothing else, so prefer a higher ``fps`` for crowded scenes.)

* ``hold``: each pair becomes ONE region over ``[t_i, t_{i+1}]`` covering the
  UNION (bounding box) of the two boxes, because the object may move.
* ``linear``: the interval is cut into ``substeps`` equal sub-intervals
  ``[a, b]``.  The pair's edges ``(x1, y1, x2, y2)`` are linearly interpolated;
  each sub-interval gets ONE region covering the union of the interpolated
  boxes at ``a`` and at ``b``, rounded outwards (floor/ceil) to pixels.  Under
  the linear-motion assumption every box in between lies inside that union
  (each edge moves monotonically), so the padding is exactly the endpoint union;
  if the object does not move linearly the reported coverage is only as good as
  that assumption -- ``hold`` makes no motion assumption beyond "stays inside
  the union".

A detected sample next to an uncovered interval still gets a one-frame region
at its own frame (``start == t``, ``end == t + 1 ms``: the edit list needs ``end > start``).
``frames_without_detection`` merges
consecutive uncovered intervals into ``[a, b]`` in normalized seconds; an
endpoint that is itself a detected sample is covered, the frames strictly
between ``a`` and ``b`` are not.  ``margin`` pads every emitted box by that
many pixels (clamped to the frame).  Frames are decoded at the stored
resolution; rotation metadata is not applied.  Extracted frames are written to
a private temporary directory (under ``workdir`` when given) and removed.
"""

from __future__ import annotations

import math
import os
import shutil
import tempfile
from typing import Any, Callable

from media_cli.media import frames, probe
from media_cli.media.errors import ENV_SENSE_UNAVAILABLE, MediaEnvError, MediaInputError

INPUT_BAD_REGION_REQUEST = "input.bad_region_request"
METHODS = ("hold", "linear")
_EPS = 1e-9
POINT_SPAN = 1e-3  # editlist needs end > start; a one-frame region lasts this long
_RESPONSE_FORMAT = {"type": "json_object"}
_Edges = tuple[float, float, float, float]  # x1, y1, x2, y2


def _bad(message: str, remediation: str) -> MediaInputError:
    return MediaInputError(INPUT_BAD_REGION_REQUEST, message, remediation)


def _prompt(description: str, width: int, height: int) -> str:
    return (
        f"The image is {width} pixels wide and {height} pixels tall. "
        f"Find every instance of the following in the image: {description}\n"
        "Answer with ONLY a JSON object of the form "
        '{"boxes": [{"x": <int>, "y": <int>, "w": <int>, "h": <int>}]} '
        "where x,y is the top-left corner and w,h the size, all integer pixel "
        "coordinates of this image, each box tightly enclosing one instance. "
        'If there is none, answer {"boxes": []}.'
    )


def _as_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and math.isfinite(v) and v.is_integer():
        return int(v)
    return None


def _validate(
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


def _centre(e: _Edges) -> tuple[float, float]:
    return (e[0] + e[2]) / 2, (e[1] + e[3]) / 2


def _pair(a: list[_Edges], b: list[_Edges]) -> tuple[list[tuple[_Edges, _Edges]], list[_Edges]]:
    """Greedy nearest-centre pairing; returns (pairs, unpaired boxes of either side)."""
    cand = sorted(
        (math.dist(_centre(p), _centre(q)), i, j) for i, p in enumerate(a) for j, q in enumerate(b)
    )
    ui, uj = set(), set()
    pairs = []
    for _d, i, j in cand:
        if i not in ui and j not in uj:
            ui.add(i)
            uj.add(j)
            pairs.append((a[i], b[j]))
    rest = [p for i, p in enumerate(a) if i not in ui] + [q for j, q in enumerate(b) if j not in uj]
    return pairs, rest


def _union(*boxes: _Edges) -> _Edges:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _lerp(p: _Edges, q: _Edges, f: float) -> _Edges:
    return tuple(p[i] + (q[i] - p[i]) * f for i in range(4))  # type: ignore[return-value]


def _down(v: float) -> float:
    return math.floor(v * 1e6) / 1e6


def _up(v: float) -> float:
    return math.ceil(v * 1e6) / 1e6


def _region(e: _Edges, a: float, b: float, margin: int, width: int, height: int) -> dict | None:
    x1 = max(0, math.floor(e[0] + _EPS) - margin)
    y1 = max(0, math.floor(e[1] + _EPS) - margin)
    x2 = min(width, math.ceil(e[2] - _EPS) + margin)
    y2 = min(height, math.ceil(e[3] - _EPS) + margin)
    if x2 <= x1 or y2 <= y1:
        return None
    return {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1, "start": _down(a), "end": _up(b)}


def _plan_times(
    info: probe.MediaInfo, fps: float, start: float | None, end: float | None
) -> list[float]:
    ftimes = probe.frame_times(info)
    lo_i = 0 if start is None else probe.to_frame_index(info, start)
    hi_i = len(ftimes) - 1 if end is None else probe.to_frame_index(info, end)
    if hi_i < lo_i:
        raise _bad("end is before start", "pass start <= end (normalized seconds)")
    lo, hi = ftimes[lo_i], ftimes[hi_i]
    idx = {lo_i}
    k = 1
    while lo + k / fps < hi - _EPS:
        idx.add(probe.to_frame_index(info, lo + k / fps))
        k += 1
    idx.add(hi_i)
    return [ftimes[i] for i in sorted(idx)]


def _build_regions(
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


def find_regions(
    path: str | os.PathLike,
    description: str,
    *,
    fps: float = 2.0,
    method: str = "hold",
    start: float | None = None,
    end: float | None = None,
    client: Any = None,
    workdir: str | os.PathLike | None = None,
    margin: int = 0,
    substeps: int = 4,
) -> dict[str, Any]:
    """Find redaction regions for *description*; see the module docstring for the contract."""
    if not isinstance(description, str) or not description.strip():
        raise _bad("description is empty", "say what to find, e.g. 'the license plate'")
    if method not in METHODS:
        raise _bad(f"unknown method {method!r}", f"use one of: {', '.join(METHODS)}")
    if not (isinstance(fps, (int, float)) and math.isfinite(fps) and fps > 0):
        raise _bad(f"fps must be > 0, got {fps!r}", "pass a positive sampling rate")
    if _as_int(margin) is None or margin < 0 or _as_int(substeps) is None or substeps < 1:
        raise _bad("margin must be >= 0 and substeps >= 1", "pass non-negative integers")
    src = os.fspath(path)
    info = probe.probe(src)
    if info.video is None or not info.video.width or not info.video.height:
        raise _bad(f"{src} has no video stream", "regions can only be found in video")
    width, height = info.video.width, info.video.height
    if client is None:
        from media_cli.media.senses import SensesClient  # lazy: offline paths never import it

        client = SensesClient()
    plan = _plan_times(info, float(fps), start, end)

    tmp = tempfile.mkdtemp(prefix="regions-", dir=None if workdir is None else os.fspath(workdir))
    rejected: list[dict] = []
    samples: list[tuple[float, list[_Edges] | None]] = []
    errors: list[MediaEnvError] = []
    try:
        shots = frames.extract(src, times=plan, outdir=tmp, max_frames=len(plan))
        prompt = _prompt(description, width, height)
        for shot in shots:
            t = shot["t"]
            try:
                reply = client.describe_images(
                    [shot["path"]], prompt, role="senses", response_format=_RESPONSE_FORMAT
                )
            except MediaEnvError as exc:
                if exc.kind != ENV_SENSE_UNAVAILABLE:
                    raise
                errors.append(exc)
                rejected.append({"t": t, "box": {"error": exc.message}, "reason": "model_error"})
                samples.append((t, None))
                continue
            samples.append((t, _validate(reply, width, height, t, rejected)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if errors and len(errors) == len(samples):
        raise errors[0]

    out_regions, gaps = _build_regions(samples, method, int(substeps), int(margin), width, height)
    served: Callable[..., Any] | None = getattr(client, "served_model", None)
    return {
        "regions": out_regions,
        "coverage": {
            "sample_fps": float(fps),
            "method": method,
            "samples": len(samples),
            "frames_without_detection": gaps,
            "rejected_boxes": rejected,
        },
        "model": served("senses") if served else None,
        "description": description,
    }
