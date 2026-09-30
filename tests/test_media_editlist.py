"""Tests for media_cli.media.editlist: schema parsing + validation against probe facts."""

from __future__ import annotations

import copy
import json
import subprocess

import pytest

from media_cli.media import _tools
from media_cli.media import editlist as E
from media_cli.media.errors import (
    INPUT_REGION_OUTSIDE_FRAME,
    INPUT_TIMESTAMP_OUT_OF_RANGE,
    MediaInputError,
)
from media_cli.media.probe import MediaInfo, StreamInfo

INVALID = E.INPUT_EDITLIST_INVALID


def make_info(duration=10.0, start=0.0, width=320, height=240, video_start=None):
    streams = [
        StreamInfo(
            index=0,
            type="video",
            codec="h264",
            start_time=start if video_start is None else video_start,
            width=width,
            height=height,
        ),
        StreamInfo(index=1, type="audio", codec="aac", start_time=start),
    ]
    return MediaInfo(
        path="in.mp4",
        format_name="mov,mp4",
        duration=duration,
        start_time=start,
        streams=tuple(streams),
    )


INFO = make_info()


def good():
    return {
        "input": "in.mp4",
        "output": "out.mp4",
        "segments": [
            {
                "start": 0,
                "end": 4.5,
                "ops": [
                    {"op": "crop", "x": 0, "y": 0, "w": 160, "h": 120},
                    {"op": "speed", "factor": 2},
                    {"op": "fade", "direction": "in", "duration": 1},
                ],
            },
            {
                "start": 5,
                "end": 9,
                "ops": [
                    {
                        "op": "box",
                        "regions": [{"x": 10, "y": 10, "w": 50, "h": 50, "start": 5.5, "end": 6}],
                        "fill": "black",
                    },
                    {
                        "op": "blur",
                        "regions": [{"x": 0, "y": 0, "w": 320, "h": 240}],
                        "strength": 5,
                    },
                ],
            },
        ],
        "transitions": [{"type": "xfade", "style": "dissolve", "duration": 0.5}],
    }


def mutated(path, value):
    """Deep copy of good() with ``path`` (list of keys/indices) set; value DELETE removes it."""
    obj = good()
    cur = obj
    for k in path[:-1]:
        cur = cur[k]
    if value is DELETE:
        del cur[path[-1]]
    else:
        cur[path[-1]] = value
    return obj


DELETE = object()


def test_parse_good_and_defaults():
    el = E.parse(good())
    assert el.input == "in.mp4" and el.output == "out.mp4"
    assert el.overwrite is False and el.keep == ()
    assert len(el.segments) == 2
    crop, speed, fade = el.segments[0].ops
    assert (crop.op, crop.x, crop.w, crop.h) == ("crop", 0, 160, 120)
    assert speed.factor == 2 and fade.direction == "in"
    box, blur = el.segments[1].ops
    assert box.fill == "black" and box.regions[0].start == 5.5
    assert blur.strength == 5 and blur.regions[0].start is None
    assert el.transitions[0].type == "xfade" and el.transitions[0].style == "dissolve"


def test_parse_json_text_and_minimal():
    text = json.dumps({"input": "a", "output": "b", "segments": [{"start": 0, "end": 1}]})
    el = E.parse(text)
    assert el.segments[0].ops == () and el.transitions == ()
    assert E.parse(text) == el  # frozen dataclasses compare by value


def test_transition_style_default():
    obj = good()
    del obj["transitions"][0]["style"]
    assert E.parse(obj).transitions[0].style == "fade"


def test_parse_bad_json_text():
    with pytest.raises(MediaInputError) as ei:
        E.parse("{not json")
    assert ei.value.kind == INVALID


def test_validate_good():
    el = E.parse(good())
    assert E.validate(el, INFO) is el


BAD_SCHEMA = [
    # (path, value, expected substring of pointer)
    ([], [], "$"),
    (["input"], 5, "input"),
    (["input"], "", "input"),
    (["input"], DELETE, "input"),
    (["output"], None, "output"),
    (["overwrite"], "yes", "overwrite"),
    (["overwrite"], 1, "overwrite"),
    (["keep"], ["video"], "keep[0]"),
    (["keep"], "subtitles", "keep"),
    (["extra"], 1, "extra"),
    (["segments"], [], "segments"),
    (["segments"], {}, "segments"),
    (["segments", 0, "extra"], 1, "segments[0].extra"),
    (["segments", 0, "start"], "0", "segments[0].start"),
    (["segments", 0, "start"], True, "segments[0].start"),
    (["segments", 0, "start"], None, "segments[0].start"),
    (["segments", 0, "start"], float("nan"), "segments[0].start"),
    (["segments", 1, "end"], float("inf"), "segments[1].end"),
    (["segments", 1, "end"], "5", "segments[1].end"),
    (["segments", 1, "end"], DELETE, "segments[1].end"),
    (["segments", 0, "ops"], "crop", "segments[0].ops"),
    (["segments", 0, "ops", 0, "op"], "drawtext", "segments[0].ops[0].op"),
    (["segments", 0, "ops", 0, "op"], "movie=", "segments[0].ops[0].op"),
    (["segments", 0, "ops", 0, "op"], 3, "segments[0].ops[0].op"),
    (["segments", 0, "ops", 0, "op"], DELETE, "segments[0].ops[0].op"),
    (["segments", 0, "ops", 0, "x"], "0", "segments[0].ops[0].x"),
    (["segments", 0, "ops", 0, "x"], 1.5, "segments[0].ops[0].x"),
    (["segments", 0, "ops", 0, "x"], -1, "segments[0].ops[0].x"),
    (["segments", 0, "ops", 0, "w"], 0, "segments[0].ops[0].w"),
    (["segments", 0, "ops", 0, "h"], True, "segments[0].ops[0].h"),
    (["segments", 0, "ops", 0, "extra"], 1, "segments[0].ops[0].extra"),
    (["segments", 0, "ops", 1, "factor"], "2", "segments[0].ops[1].factor"),
    (["segments", 0, "ops", 1, "factor"], 0.1, "segments[0].ops[1].factor"),
    (["segments", 0, "ops", 1, "factor"], 4.5, "segments[0].ops[1].factor"),
    (["segments", 0, "ops", 1, "factor"], DELETE, "segments[0].ops[1].factor"),
    (["segments", 0, "ops", 2, "direction"], "sideways", "segments[0].ops[2].direction"),
    (["segments", 0, "ops", 2, "duration"], "1", "segments[0].ops[2].duration"),
    (["segments", 0, "ops", 2, "duration"], 0, "segments[0].ops[2].duration"),
    (["segments", 1, "ops", 0, "fill"], "red", "segments[1].ops[0].fill"),
    (["segments", 1, "ops", 0, "regions"], [], "segments[1].ops[0].regions"),
    (["segments", 1, "ops", 0, "regions"], [5], "segments[1].ops[0].regions[0]"),
    (["segments", 1, "ops", 0, "regions", 0, "w"], "50", "segments[1].ops[0].regions[0].w"),
    (["segments", 1, "ops", 0, "regions", 0, "w"], 0, "segments[1].ops[0].regions[0].w"),
    (["segments", 1, "ops", 0, "regions", 0, "x"], -1, "segments[1].ops[0].regions[0].x"),
    (["segments", 1, "ops", 0, "regions", 0, "q"], 1, "segments[1].ops[0].regions[0].q"),
    (["segments", 1, "ops", 0, "regions", 0, "start"], "5", "segments[1].ops[0].regions[0].start"),
    (["segments", 1, "ops", 1, "strength"], "5", "segments[1].ops[1].strength"),
    (["segments", 1, "ops", 1, "strength"], 0, "segments[1].ops[1].strength"),
    (["segments", 1, "ops", 1, "strength"], 51, "segments[1].ops[1].strength"),
    (["segments", 1, "ops", 1, "strength"], 2.5, "segments[1].ops[1].strength"),
    (["transitions"], {}, "transitions"),
    (["transitions", 0, "type"], "concat", "transitions[0].type"),
    (["transitions", 0, "style"], "circleopen", "transitions[0].style"),
    (["transitions", 0, "duration"], "0.5", "transitions[0].duration"),
    (["transitions", 0, "duration"], 0, "transitions[0].duration"),
    (["transitions", 0, "duration"], float("inf"), "transitions[0].duration"),
    (["transitions", 0, "extra"], 1, "transitions[0].extra"),
]


@pytest.mark.parametrize("path,value,pointer", BAD_SCHEMA)
def test_parse_rejects_bad_schema(path, value, pointer):
    obj = value if not path else mutated(path, value)
    with pytest.raises(MediaInputError) as ei:
        E.parse(obj)
    assert ei.value.kind == INVALID
    assert pointer in ei.value.message


def test_transitions_wrong_count_three_segments():
    obj = good()
    obj["segments"].append({"start": 9, "end": 9.5})
    with pytest.raises(MediaInputError) as ei:
        E.parse(obj)  # 3 segments need 2 transitions (or 0)
    assert "transitions" in ei.value.message
    obj["transitions"] = []
    E.parse(obj)  # zero transitions is fine


INJECTION = ["movie=", ",", ";", "[", "fade,movie=/etc/passwd", "crop;x"]

ENUM_FIELDS = [
    ["segments", 0, "ops", 0, "op"],
    ["segments", 0, "ops", 2, "direction"],
    ["segments", 1, "ops", 0, "fill"],
    ["transitions", 0, "type"],
    ["transitions", 0, "style"],
    ["keep", 0],
]


@pytest.mark.parametrize("payload", INJECTION)
@pytest.mark.parametrize("path", ENUM_FIELDS)
def test_injection_into_enum_fields_rejected(path, payload):
    obj = good()
    obj["keep"] = ["subtitles"]
    cur = obj
    for k in path[:-1]:
        cur = cur[k]
    cur[path[-1]] = payload
    with pytest.raises(MediaInputError) as ei:
        E.parse(obj)
    assert ei.value.kind == INVALID


@pytest.mark.parametrize("payload", INJECTION)
@pytest.mark.parametrize(
    "path",
    [
        ["segments", 0, "start"],
        ["segments", 0, "ops", 0, "x"],
        ["segments", 0, "ops", 1, "factor"],
        ["segments", 0, "ops", 2, "duration"],
        ["segments", 1, "ops", 0, "regions", 0, "w"],
        ["segments", 1, "ops", 0, "regions", 0, "start"],
        ["segments", 1, "ops", 1, "strength"],
        ["transitions", 0, "duration"],
    ],
)
def test_injection_into_numeric_fields_rejected(path, payload):
    with pytest.raises(MediaInputError) as ei:
        E.parse(mutated(path, payload))
    assert ei.value.kind == INVALID


@pytest.mark.parametrize("field", ["input", "output"])
@pytest.mark.parametrize("bad", ["", "-rf", "a\x00b", 7, None, ["a"]])
def test_path_fields_hardened(field, bad):
    with pytest.raises(MediaInputError) as ei:
        E.parse(mutated([field], bad))
    assert ei.value.kind == INVALID and field in ei.value.message


def test_paths_with_filter_metacharacters_are_just_paths():
    # input/output are argv paths, never interpolated into a filtergraph.
    obj = good()
    obj["input"] = "my,clip;v1[0].mp4"
    assert E.parse(obj).input == "my,clip;v1[0].mp4"


T, R = INPUT_TIMESTAMP_OUT_OF_RANGE, INPUT_REGION_OUTSIDE_FRAME

BAD_BOUNDS = [
    (["segments", 0, "start"], -0.5, T, "segments[0].start"),
    (["segments", 1, "end"], 10.5, T, "segments[1].end"),
    (["segments", 0, "end"], 0, T, "segments[0].end"),  # end == start
    (["segments", 1, "end"], 4, T, "segments[1].end"),  # end < start
    (["segments", 0, "ops", 2, "duration"], 5, T, "segments[0].ops[2].duration"),
    (["transitions", 0, "duration"], 4.5, T, "transitions[0].duration"),  # not < seg 0 (4.5)
    (["transitions", 0, "duration"], 5, T, "transitions[0].duration"),
    (["segments", 0, "ops", 0, "w"], 321, R, "segments[0].ops[0].w"),
    (["segments", 0, "ops", 0, "x"], 200, R, "segments[0].ops[0].w"),  # 200+160 > 320
    (["segments", 0, "ops", 0, "h"], 241, R, "segments[0].ops[0].h"),
    (["segments", 1, "ops", 0, "regions", 0, "x"], 300, R, "segments[1].ops[0].regions[0].w"),
    (["segments", 1, "ops", 0, "regions", 0, "x"], 400, R, "segments[1].ops[0].regions[0].x"),
    (["segments", 1, "ops", 0, "regions", 0, "h"], 300, R, "segments[1].ops[0].regions[0].h"),
    (["segments", 1, "ops", 1, "regions", 0, "w"], 321, R, "segments[1].ops[1].regions[0].w"),
    (["segments", 1, "ops", 0, "regions", 0, "start"], 4, T, "segments[1].ops[0].regions[0].start"),
    (["segments", 1, "ops", 0, "regions", 0, "end"], 9.5, T, "segments[1].ops[0].regions[0].end"),
    (["segments", 1, "ops", 0, "regions", 0, "end"], 5.5, T, "segments[1].ops[0].regions[0].end"),
]


@pytest.mark.parametrize("path,value,kind,pointer", BAD_BOUNDS)
def test_validate_rejects_against_probe(path, value, kind, pointer):
    el = E.parse(mutated(path, value))  # structurally fine
    with pytest.raises(MediaInputError) as ei:
        E.validate(el, INFO)
    assert ei.value.kind == kind
    assert pointer in ei.value.message


def test_region_pointer_deep():
    obj = good()
    obj["segments"][1]["ops"][0]["regions"] = [
        {"x": 0, "y": 0, "w": 5, "h": 5},
        {"x": 0, "y": 0, "w": 5, "h": 5},
        {"x": 0, "y": 0, "w": 400, "h": 5},
    ]
    with pytest.raises(MediaInputError) as ei:
        E.validate(E.parse(obj), INFO)
    assert "segments[1].ops[0].regions[2].w" in ei.value.message


def test_regions_after_crop_bound_by_cropped_frame():
    obj = {
        "input": "a",
        "output": "b",
        "segments": [
            {
                "start": 0,
                "end": 2,
                "ops": [
                    {"op": "crop", "x": 0, "y": 0, "w": 100, "h": 100},
                    {
                        "op": "box",
                        "regions": [{"x": 50, "y": 0, "w": 100, "h": 10}],
                        "fill": "black",
                    },
                ],
            }
        ],
    }
    with pytest.raises(MediaInputError) as ei:
        E.validate(E.parse(obj), INFO)
    assert ei.value.kind == R and "segments[0].ops[1].regions[0].w" in ei.value.message


def test_normalized_bounds_with_offset_origin():
    # container start 1.5, video starts 1.5, duration 10 -> normalized end is 10.0,
    # and the edit list is never shifted by the container start.
    info = make_info(duration=10.0, start=1.5)
    obj = {"input": "a", "output": "b", "segments": [{"start": 0, "end": 10}]}
    E.validate(E.parse(obj), info)
    obj["segments"][0]["end"] = 10.6
    with pytest.raises(MediaInputError):
        E.validate(E.parse(obj), info)


def test_spatial_ops_need_video_stream():
    audio_only = MediaInfo(
        path="a.wav",
        format_name="wav",
        duration=5.0,
        start_time=0.0,
        streams=(StreamInfo(index=0, type="audio", codec="pcm"),),
    )
    obj = {
        "input": "a",
        "output": "b",
        "segments": [
            {"start": 0, "end": 1, "ops": [{"op": "crop", "x": 0, "y": 0, "w": 1, "h": 1}]}
        ],
    }
    with pytest.raises(MediaInputError) as ei:
        E.validate(E.parse(obj), audio_only)
    assert ei.value.kind == INVALID and "segments[0].ops[0]" in ei.value.message
    obj["segments"][0]["ops"] = [{"op": "speed", "factor": 2}]
    E.validate(E.parse(obj), audio_only)


def test_validate_catches_hand_built_invalid_editlist():
    # validate is authoritative even for dataclasses constructed without parse().
    el = E.EditList(
        input="a",
        output="b",
        segments=(E.Segment(start=3.0, end=2.0),),
    )
    with pytest.raises(MediaInputError) as ei:
        E.validate(el, INFO)
    assert ei.value.kind == T


def test_validate_is_pure_no_subprocess(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("validate() must not spawn a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(_tools, "run", boom)
    monkeypatch.setattr(_tools, "spawn", boom)
    el = E.parse(good())
    assert E.validate(el, INFO) is el
    with pytest.raises(MediaInputError):
        E.validate(E.parse(mutated(["segments", 1, "end"], 99)), INFO)


def test_load_and_validate_uses_probe(media_mp4, tmp_path):
    p = tmp_path / "el.json"
    obj = {"input": str(media_mp4), "output": "o.mp4", "segments": [{"start": 1, "end": 2}]}
    p.write_text(json.dumps(obj))
    assert E.load_and_validate(p).segments[0].end == 2
    obj["segments"][0]["end"] = 99
    p.write_text(json.dumps(obj))
    with pytest.raises(MediaInputError) as ei:
        E.load_and_validate(p)
    assert ei.value.kind == T


def test_load_and_validate_unreadable_editlist(tmp_path):
    with pytest.raises(MediaInputError):
        E.load_and_validate(tmp_path / "missing.json")


def test_inputs_not_mutated():
    obj = good()
    snap = copy.deepcopy(obj)
    E.validate(E.parse(obj), INFO)
    assert obj == snap
