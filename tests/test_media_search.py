"""Semantic-query tests against a stub gateway giving canned yes/no answers."""

from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from media_cli.media import index, search, senses
from media_cli.media.errors import MediaEnvError, MediaInputError

CAPS = {
    "senses": {"role": "senses", "model": "model-a", "loaded": True},
    "stt": {"role": "stt", "model": "stt-a", "loaded": True},
}
ITEM = re.compile(r"^\[(\d+)\] (.*)$", re.M)


class Gateway:
    """Captions frame k 'red square' for k in red_frames; answers yes iff text has 'red'."""

    def __init__(self, red_frames=(4, 5, 6), matches_body=None, transcripts=None):
        self.red_frames = set(red_frames)
        self.matches_body = matches_body
        self.transcripts = list(transcripts or [])
        self.frames_seen = 0
        self.log: list[tuple[str, str]] = []
        gw = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                gw.log.append(("GET", self.path))
                self._send(CAPS)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                gw.log.append(("POST", self.path))
                if self.path.endswith("/transcriptions"):
                    text = gw.transcripts.pop(0) if gw.transcripts else ""
                    return self._send({"text": text})
                body = json.loads(raw)
                parts = body["messages"][0]["content"]
                if isinstance(parts, list):  # caption call
                    count = sum(1 for p in parts if p["type"] == "image_url")
                    caps = []
                    for _ in range(count):
                        caps.append("a red square" if gw.frames_seen in gw.red_frames else "empty")
                        gw.frames_seen += 1
                    return self._send(
                        {"choices": [{"message": {"content": json.dumps({"captions": caps})}}]}
                    )
                items = ITEM.findall(parts)
                if gw.matches_body is not None:
                    content = gw.matches_body
                else:
                    content = json.dumps(
                        {
                            "matches": [
                                {
                                    "i": int(i),
                                    "match": "red" in t,
                                    "confidence": 0.9 if "red" in t else 0.1,
                                }
                                for i, t in items
                            ]
                        }
                    )
                self._send({"choices": [{"message": {"content": content}}]})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def client(self):
        return senses.SensesClient(self.url, max_retries=0)

    def count(self, method, suffix=""):
        return sum(1 for m, p in self.log if m == method and p.endswith(suffix))

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def gw():
    g = Gateway()
    yield g
    g.close()


@pytest.fixture
def cache(tmp_path):
    return str(tmp_path / "cache")


def built(gw, media, cache, **kw):
    return index.build_index(media, fps=1, client=gw.client(), cache_dir=cache, **kw)


def test_frames_merge_adjacent_into_range(gw, cache, media_mp4):
    built(gw, media_mp4, cache)
    hits = search.query(
        media_mp4, "a red square", index_params={"fps": 1}, batch_size=4,
        client=gw.client(), cache_dir=cache,
    )  # fmt: skip
    assert len(hits) == 1
    h = hits[0]
    assert (h["start"], h["end"]) == (4.0, 7.0)  # samples 4,5,6 -> up to next sample t=7
    assert h["frame_index"] == h["samples"][0]["frame_index"]
    assert h["evidence"]["kind"] == "frame"
    assert os.path.isfile(h["evidence"]["frame_path"])
    assert h["evidence"]["caption"] == "a red square"
    assert h["evidence"]["model"] == "model-a"
    assert 0.5 <= h["score"] <= 1.0
    assert [s["t"] for s in h["samples"]] == [4.0, 5.0, 6.0]


def test_two_separate_runs_are_two_hits(cache, media_mp4):
    g = Gateway(red_frames=(1, 2, 6))
    try:
        built(g, media_mp4, cache)
        hits = search.query(media_mp4, "red", index_params={"fps": 1}, batch_size=4,
                            client=g.client(), cache_dir=cache)  # fmt: skip
        assert [(h["start"], h["end"]) for h in hits] == [(1.0, 3.0), (6.0, 7.0)]
    finally:
        g.close()


def test_last_sample_range_ends_at_media_end(cache, media_mp4):
    g = Gateway(red_frames=(8, 9))
    try:
        built(g, media_mp4, cache)
        hits = search.query(media_mp4, "red", index_params={"fps": 1}, batch_size=4,
                            client=g.client(), cache_dir=cache)  # fmt: skip
        assert hits[0]["start"] == 8.0
        assert 9.9 <= hits[0]["end"] <= 10.1
    finally:
        g.close()


def test_query_request_counts(gw, cache, media_mp4):
    built(gw, media_mp4, cache)
    g0, p0 = gw.count("GET"), gw.count("POST")
    client = gw.client()  # fresh client: ensure_local needs one /capabilities read
    search.query(media_mp4, "red", index_params={"fps": 1}, batch_size=4,
                 client=client, cache_dir=cache)  # fmt: skip
    assert gw.count("GET") - g0 == 1
    assert gw.count("POST") - p0 == 3  # 10 captions / batch 4; matching only, no captioning
    g1 = gw.count("GET")
    search.query(media_mp4, "red", index_params={"fps": 1}, batch_size=4,
                 client=client, cache_dir=cache)  # fmt: skip
    assert gw.count("GET") == g1  # verdict cached on the client: zero extra calls


def test_not_substring_matching(gw, cache, media_mp4):
    built(gw, media_mp4, cache)
    # "crimson box" is in no caption: a substring matcher finds nothing, the model says yes.
    hits = search.query(media_mp4, "crimson box", index_params={"fps": 1}, batch_size=4,
                        client=gw.client(), cache_dir=cache)  # fmt: skip
    assert [(h["start"], h["end"]) for h in hits] == [(4.0, 7.0)]


def test_threshold_filters_low_confidence(cache, media_mp4):
    g = Gateway()
    try:
        built(g, media_mp4, cache)
        g.matches_body = json.dumps(
            {"matches": [{"i": i, "match": True, "confidence": 0.3} for i in range(10)]}
        )
        hits = search.query(media_mp4, "x", index_params={"fps": 1}, batch_size=10,
                            client=g.client(), cache_dir=cache)  # fmt: skip
        assert hits == []
    finally:
        g.close()


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        json.dumps({"matches": "nope"}),
        json.dumps({"matches": [{"i": 0, "match": True, "confidence": 0.9}]}),  # incomplete
        json.dumps({"matches": [{"i": i, "match": "yes", "confidence": 0.9} for i in range(4)]}),
        json.dumps({"matches": [{"i": i, "match": True, "confidence": 7} for i in range(4)]}),
    ],
)
def test_malformed_reply_is_sense_unavailable(cache, media_mp4, body):
    g = Gateway()
    try:
        built(g, media_mp4, cache)
        g.matches_body = body
        client = g.client()
        with pytest.raises(MediaEnvError) as ei:
            search.query(media_mp4, "red", index_params={"fps": 1}, batch_size=4,
                         client=client, cache_dir=cache)  # fmt: skip
        assert ei.value.kind == "env.sense_unavailable"
    finally:
        g.close()


def test_missing_index_is_typed_input_error(gw, cache, media_mp4):
    client = gw.client()
    with pytest.raises(MediaInputError) as ei:
        search.query(media_mp4, "red", index_params={"fps": 1}, client=client, cache_dir=cache)
    assert ei.value.kind == search.INPUT_INDEX_MISSING == "input.index_missing"
    assert gw.log == []  # nothing sent anywhere


def test_build_if_missing_builds_then_queries(gw, cache, media_mp4):
    hits = search.query(media_mp4, "red", index_params={"fps": 1}, batch_size=4,
                        client=gw.client(), cache_dir=cache, build_if_missing=True)  # fmt: skip
    assert len(hits) == 1


def test_dead_gateway_is_sense_unavailable(gw, cache, media_mp4):
    built(gw, media_mp4, cache)
    dead = senses.SensesClient("http://127.0.0.1:1", max_retries=0)
    with pytest.raises(MediaEnvError) as ei:
        search.query(media_mp4, "red", index_params={"fps": 1}, batch_size=4,
                     client=dead, cache_dir=cache)  # fmt: skip
    assert ei.value.kind == "env.sense_unavailable"


def test_bad_arguments(gw, cache, media_mp4):
    client = gw.client()
    with pytest.raises(MediaInputError):
        search.query(media_mp4, "  ", client=client, cache_dir=cache)
    with pytest.raises(MediaInputError):
        search.query(media_mp4, "x", modality="smell", client=client, cache_dir=cache)


def test_speech_matches_transcript_and_caches(cache, media_mp4):
    g = Gateway(transcripts=["we talk about a red square here"])
    try:
        hits = search.query(media_mp4, "red thing", modality="speech",
                            client=g.client(), cache_dir=cache)  # fmt: skip
        assert len(hits) == 1
        h = hits[0]
        assert h["start"] == 0.0
        assert h["end"] == pytest.approx(10.0, abs=0.2)
        assert h["evidence"]["kind"] == "speech"
        assert "red square" in h["evidence"]["text"]
        assert os.path.isfile(h["evidence"]["transcript"])
        assert h["evidence"]["model"] == "model-a"
        assert "frame_index" not in h
        stt = g.count("POST", "/transcriptions")
        assert stt == 1
        search.query(media_mp4, "red thing", modality="speech", client=g.client(), cache_dir=cache)
        assert g.count("POST", "/transcriptions") == stt  # transcript cached
    finally:
        g.close()


def test_all_modality_combines_sorted(cache, media_mp4):
    g = Gateway(transcripts=["a red square"])
    try:
        built(g, media_mp4, cache)
        hits = search.query(media_mp4, "red", modality="all", index_params={"fps": 1},
                            batch_size=4, client=g.client(), cache_dir=cache)  # fmt: skip
        assert {h["evidence"]["kind"] for h in hits} == {"frame", "speech"}
        assert [h["start"] for h in hits] == sorted(h["start"] for h in hits)
    finally:
        g.close()


def _speech(g, media, cache, **kw):
    return search.query(media, "red", modality="speech", client=g.client(), cache_dir=cache, **kw)


def test_purge_transcripts_by_fingerprint_and_source(cache, tmp_path, media_mp4):
    import shutil

    other = tmp_path / "other.mp4"
    shutil.copy(media_mp4, other)
    g = Gateway(transcripts=["a red square", "a red square", "a red square"])
    try:
        _speech(g, str(media_mp4), cache)
        _speech(g, str(other), cache)
        assert len(search.cache_report(cache_dir=cache)["transcripts"]) == 2
        assert search.purge_transcripts(str(media_mp4), cache_dir=cache) == 1
        rep = search.cache_report(cache_dir=cache)["transcripts"]
        assert [os.path.realpath(t["source"]) for t in rep] == [os.path.realpath(other)]
        # file changes: fingerprint differs, recorded source still matches
        _speech(g, str(media_mp4), cache)
        with open(media_mp4, "rb") as fh:
            data = fh.read()
        mutated = tmp_path / "m.mp4"
        mutated.write_bytes(data)
        _speech(g, str(mutated), cache)
        with open(mutated, "ab") as fh:
            fh.write(b"x")
        assert search.purge_transcripts(str(mutated), cache_dir=cache) == 1
    finally:
        g.close()


def test_purge_returns_both_counts(gw, cache, media_mp4):
    g = Gateway(transcripts=["a red square"])
    try:
        built(g, media_mp4, cache)
        _speech(g, str(media_mp4), cache)
        assert search.purge(str(media_mp4), cache_dir=cache) == {"indexes": 1, "transcripts": 1}
        assert search.purge(str(media_mp4), cache_dir=cache) == {"indexes": 0, "transcripts": 0}
    finally:
        g.close()


def test_transcript_cache_is_lru_bounded_and_keeps_newest(cache, tmp_path, media_mp4):
    import shutil

    g = Gateway(transcripts=["a red square one", "a red square two", "a red square three"])
    try:
        files = []
        for n in range(3):
            f = tmp_path / f"c{n}.mp4"
            shutil.copy(media_mp4, f)
            with open(f, "ab") as fh:
                fh.write(bytes([n]) * 10)
            files.append(str(f))
        _speech(g, files[0], cache, max_transcript_bytes=10**6)
        first = search.cache_report(cache_dir=cache)["transcripts"][0]["file"]
        os.utime(first, (1, 1))  # oldest
        _speech(g, files[1], cache, max_transcript_bytes=1)  # cap tiny: evicts old, keeps new
        rep = search.cache_report(cache_dir=cache)["transcripts"]
        assert len(rep) == 1
        assert not os.path.exists(first)
        assert os.path.realpath(rep[0]["source"]) == os.path.realpath(files[1])
    finally:
        g.close()


def test_cache_report_lists_indexes_and_transcripts(cache, media_mp4):
    g = Gateway(transcripts=["a red square"])
    try:
        built(g, media_mp4, cache)
        _speech(g, str(media_mp4), cache)
        rep = search.cache_report(str(media_mp4), cache_dir=cache)
        assert len(rep["indexes"]) == 1
        assert len(rep["transcripts"]) == 1
        i, t = rep["indexes"][0], rep["transcripts"][0]
        assert i["entries"] == 10
        assert i["bytes"] > 0
        assert i["fingerprint"] == t["fingerprint"]
        assert os.path.realpath(i["source"]) == os.path.realpath(media_mp4)
        assert t["segments"] == 1
        assert rep["total_bytes"] == i["bytes"] + t["bytes"]
        assert search.cache_report("/nonexistent-x", cache_dir=cache)["indexes"] == []
        assert search.cache_report(cache_dir=str(cache) + "-none") == {
            "indexes": [], "transcripts": [], "total_bytes": 0
        }  # fmt: skip
    finally:
        g.close()


@pytest.mark.live_gateway
def test_live_finds_red_square_range(media_red_square, tmp_path):
    cache = str(tmp_path / "cache")
    index.build_index(media_red_square, fps=2, cache_dir=cache)
    hits = search.query(media_red_square, "a red square", index_params={"fps": 2},
                        cache_dir=cache)  # fmt: skip
    assert any(h["start"] < 6.0 and h["end"] > 4.0 for h in hits), hits
