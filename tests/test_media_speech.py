"""Speech search: chunk planning, offsets, overlap merge (stub transcriber) + a live check."""

from __future__ import annotations

import os
import re
import subprocess
import wave
from pathlib import Path

import pytest

from media_cli.media import speech
from media_cli.media.speech import plan_chunks, transcribe_media


def test_plan_chunks_75s():
    assert plan_chunks(75.0) == [(0.0, 30.0), (28.0, 58.0), (56.0, 75.0)]


def test_plan_chunks_edges():
    assert plan_chunks(0) == []
    assert plan_chunks(10.0) == [(0.0, 10.0)]
    assert plan_chunks(30.0) == [(0.0, 30.0)]
    assert plan_chunks(30.5) == [(0.0, 30.0), (28.0, 30.5)]


def test_plan_chunks_invariants():
    for d in (1.0, 29.9, 57.9, 58.0, 58.1, 75.0, 600.0):
        chunks = plan_chunks(d)
        assert chunks[0][0] == 0.0
        assert chunks[-1][1] == pytest.approx(d)
        for (s, e), nxt in zip(chunks, chunks[1:] + [None]):
            assert 0 < e - s <= 30.0
            if nxt:
                assert e - nxt[0] == pytest.approx(2.0)


def test_plan_chunks_rejects_bad_params():
    with pytest.raises(ValueError):
        plan_chunks(10.0, chunk=5.0, overlap=5.0)


def _wav_seconds(path: str) -> float:
    with wave.open(path, "rb") as w:
        assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (1, 16000, 2)
        return w.getnframes() / w.getframerate()


@pytest.fixture(scope="module")
def sine75(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("speech") / "sine75.wav"
    try:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=75", str(out)],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"ffmpeg unavailable: {exc}")
    return out


def test_transcribe_75s_boundaries_and_offsets(sine75, tmp_path):
    seen: list[tuple[float, bool]] = []

    def stub(wav, language="en"):
        assert language == "en"
        seen.append((_wav_seconds(wav), Path(wav).exists()))
        return f"chunk{len(seen) - 1} is {len(seen) - 1}"

    segs = transcribe_media(str(sine75), transcriber=stub, workdir=str(tmp_path))
    assert len(seen) == 3
    durs = [d for d, _ in seen]
    assert all(d <= 30.05 for d in durs)
    assert durs == pytest.approx([30.0, 30.0, 19.0], abs=0.1)
    assert [(s["start"], s["end"]) for s in segs] == [(0.0, 30.0), (28.0, 58.0), (56.0, 75.0)]
    assert [s["text"] for s in segs] == ["chunk0 is 0", "chunk1 is 1", "chunk2 is 2"]
    # temp chunk files are cleaned up
    assert list(tmp_path.iterdir()) == []


def test_start_offset_shifts_times(sine75):
    segs = transcribe_media(str(sine75), transcriber=lambda w, language="en": "x", start_offset=1.5)
    assert segs[0]["start"] == 1.5
    assert segs[-1]["end"] == 76.5


def test_overlap_duplicates_merged(sine75):
    texts = iter(
        [
            "the quick brown fox jumps over the lazy dog",
            "Over the lazy dog, and then some more words",
            "more words appear",
        ]
    )
    segs = transcribe_media(str(sine75), transcriber=lambda w, language="en": next(texts))
    assert [s["text"] for s in segs] == [
        "the quick brown fox jumps over the lazy dog",
        "and then some more words",
        "appear",
    ]


def test_empty_transcripts_yield_no_segment(sine75):
    texts = iter(["hello there", "   ", "hello there"])
    segs = transcribe_media(str(sine75), transcriber=lambda w, language="en": next(texts))
    assert [s["text"] for s in segs] == ["hello there", "hello there"]
    assert segs[1]["start"] == 56.0


def test_fully_duplicated_chunk_dropped(sine75):
    texts = iter(["one two three", "two three", "unrelated"])
    segs = transcribe_media(str(sine75), transcriber=lambda w, language="en": next(texts))
    assert [s["text"] for s in segs] == ["one two three", "unrelated"]


def test_merge_words_no_overlap_keeps_text():
    assert speech._strip_overlap(["a", "b"], ["c", "d"]) == ["c", "d"]


def test_explicit_duration_skips_probe(sine75, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("probe should not run")

    monkeypatch.setattr(speech, "_probe_duration", boom)
    segs = transcribe_media(str(sine75), transcriber=lambda w, language="en": "hi", duration=10.0)
    assert [(s["start"], s["end"]) for s in segs] == [(0.0, 10.0)]


@pytest.mark.live_gateway
def test_live_real_speech_within_one_second():
    fixture = os.environ.get("MEDIA_CLI_SPEECH_FIXTURE")
    expect = os.environ.get("MEDIA_CLI_SPEECH_EXPECT", "")
    if not fixture or not Path(fixture).is_file():
        pytest.skip("set MEDIA_CLI_SPEECH_FIXTURE to a recorded speech clip")
    m = re.fullmatch(r"(.+)@(\d+(?:\.\d+)?)", expect)
    if not m:
        pytest.skip('set MEDIA_CLI_SPEECH_EXPECT="<phrase>@<seconds>"')
    phrase, true_t = m.group(1).lower(), float(m.group(2))
    segs = transcribe_media(fixture)
    hits = [s for s in segs if phrase in s["text"].lower()]
    assert hits, f"phrase {phrase!r} not found in {segs}"
    assert any(s["start"] - 1.0 <= true_t <= s["end"] + 1.0 for s in hits)
