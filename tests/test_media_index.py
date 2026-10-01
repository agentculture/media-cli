"""Search-index tests: a local stub gateway counts every request by path."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from media_cli.media import index, senses
from media_cli.media.errors import MediaEnvError, MediaInputError

SENSES_CAPS = {
    "senses": {
        "role": "senses",
        "model": "model-a",
        "loaded": True,
        "replicas": [{"origin": "http://x", "local": True}],
    }
}


class Gateway:
    def __init__(self, model="model-a", chat_body=None):
        self.model = model
        self.chat_body = chat_body  # override raw chat content string
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
                caps = json.loads(json.dumps(SENSES_CAPS))
                caps["senses"]["model"] = gw.model
                self._send(caps)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n))
                gw.log.append(("POST", self.path))
                parts = body["messages"][0]["content"]
                count = sum(1 for p in parts if p["type"] == "image_url")
                content = gw.chat_body
                if content is None:
                    content = json.dumps({"captions": [f"caption {i}" for i in range(count)]})
                self._send({"choices": [{"message": {"content": content}}]})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def client(self):
        return senses.SensesClient(self.url, max_retries=0)

    @property
    def requests(self):
        return len(self.log)

    @property
    def posts(self):
        return [e for e in self.log if e[0] == "POST"]

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


def build(gw, media, cache, **kw):
    kw.setdefault("fps", 0.5)
    kw.setdefault("batch_size", 2)
    return index.build_index(media, client=gw.client(), cache_dir=cache, **kw)


def test_build_stores_entries_and_layout(gw, cache, media_mp4):
    idx = build(gw, media_mp4, cache)
    assert idx["schema_version"] == index.SCHEMA_VERSION
    assert len(idx["entries"]) == 5  # 10 s at 0.5 fps
    assert len(gw.posts) == 3  # ceil(5/2)
    e = idx["entries"][0]
    assert set(e) >= {"t", "frame_index", "frame_path", "caption", "model", "prompt_version"}
    assert e["model"] == "model-a" and os.path.isfile(e["frame_path"])
    dirs = os.listdir(cache)
    assert len(dirs) == 1
    assert os.path.isfile(os.path.join(cache, dirs[0], "index.json"))
    assert (os.stat(cache).st_mode & 0o777) == 0o700


def test_unchanged_rebuild_one_capabilities_zero_captions(gw, cache, media_mp4):
    build(gw, media_mp4, cache)
    before = len(gw.log)
    again = build(gw, media_mp4, cache)
    assert gw.log[before:] == [("GET", "/capabilities")]
    assert len(again["entries"]) == 5


def test_load_index_zero_requests_of_any_path(gw, cache, media_mp4):
    build(gw, media_mp4, cache)
    before = gw.requests
    assert index.load_index(media_mp4, cache_dir=cache, fps=0.5, batch_size=2) is not None
    assert gw.requests == before


def test_verify_false_makes_no_requests(gw, cache, media_mp4):
    build(gw, media_mp4, cache)
    before = gw.requests
    build(gw, media_mp4, cache, verify=False)
    assert gw.requests == before


def test_prompt_version_change_reindexes(gw, cache, media_mp4):
    build(gw, media_mp4, cache)
    n = len(gw.posts)
    idx = build(gw, media_mp4, cache, prompt_version="2")
    assert len(gw.posts) == n + 3
    assert idx["entries"][0]["prompt_version"] == "2"


def test_served_model_change_reindexes_by_default(gw, cache, media_mp4):
    build(gw, media_mp4, cache)
    n = len(gw.posts)
    build(gw, media_mp4, cache)  # same model: only /capabilities
    assert len(gw.posts) == n
    gw.model = "model-b"
    idx = build(gw, media_mp4, cache)
    assert len(gw.posts) == n + 3
    assert idx["entries"][0]["model"] == "model-b"
    # still one cache dir: the re-index replaced the stale one
    assert len(os.listdir(cache)) == 1


def test_sampling_params_change_reindexes(gw, cache, media_mp4):
    build(gw, media_mp4, cache)
    build(gw, media_mp4, cache, fps=0.2)
    assert len(os.listdir(cache)) == 2


def test_budget_exceeded_before_any_request(gw, cache, media_mp4):
    with pytest.raises(MediaInputError) as ei:
        build(gw, media_mp4, cache, max_calls=2)
    assert ei.value.kind == "input.budget_exceeded"
    assert gw.requests == 0
    assert not os.path.exists(cache) or not os.listdir(cache)


def test_dry_run_exact_count_no_requests(gw, cache, media_mp4):
    plan = build(gw, media_mp4, cache, dry_run=True)
    assert plan["frames"] == 5 and plan["batches"] == 3 and plan["sense_calls"] == 3
    assert plan["cap"] == 600 and plan["cached"] is False
    assert gw.requests == 0
    with pytest.raises(MediaInputError) as ei:
        build(gw, media_mp4, cache, dry_run=True, max_calls=2)
    assert ei.value.kind == "input.budget_exceeded"
    assert gw.requests == 0
    build(gw, media_mp4, cache)
    assert build(gw, media_mp4, cache, dry_run=True)["sense_calls"] == 0


def test_dry_run_scene_mode_needs_no_gateway(gw, cache, media_red_square):
    plan = build(gw, media_red_square, cache, fps=None, scene=0.03, dry_run=True)
    assert plan["frames"] >= 1 and plan["sense_calls"] == plan["batches"]
    assert gw.requests == 0


def test_requires_exactly_one_sampler(gw, cache, media_mp4):
    with pytest.raises(MediaInputError):
        build(gw, media_mp4, cache, fps=None)
    with pytest.raises(MediaInputError):
        build(gw, media_mp4, cache, scene=0.3)


def test_malformed_caption_response_caches_nothing(cache, media_mp4):
    for body in ("not json", json.dumps({"captions": ["only one"]})):
        g = Gateway(chat_body=body)
        try:
            with pytest.raises((MediaEnvError, MediaInputError)):
                build(g, media_mp4, cache)
        finally:
            g.close()
        assert not os.path.exists(cache) or os.listdir(cache) == []


def test_purge_removes_all_artifacts_for_file(gw, cache, media_mp4):
    build(gw, media_mp4, cache)
    build(gw, media_mp4, cache, fps=0.2)
    assert len(os.listdir(cache)) == 2
    assert index.purge(media_mp4, cache_dir=cache) == 2
    assert os.listdir(cache) == []
    assert index.load_index(media_mp4, cache_dir=cache, fps=0.5, batch_size=2) is None


def test_lru_eviction_with_tiny_cap(gw, cache, media_mp4):
    build(gw, media_mp4, cache)
    first = os.listdir(cache)[0]
    build(gw, media_mp4, cache, fps=0.2, max_cache_bytes=1)
    left = os.listdir(cache)
    # the freshly built index is kept; the older one is evicted
    assert len(left) == 1 and left[0] != first


def test_lru_evicts_least_recently_used(gw, cache, media_mp4):
    build(gw, media_mp4, cache, fps=0.5)  # A
    build(gw, media_mp4, cache, fps=0.2)  # B, newer than A on disk
    name = {}
    for fps in (0.5, 0.2):
        d = index._location(
            cache,
            index.fingerprint(media_mp4),
            index._identity("senses", index.PROMPT_VERSION, index._sampling(fps, None, 2)),
        )[1]
        name[fps] = d
        os.utime(os.path.join(d, "index.json"), (1000 + fps, 1000 + fps))
    os.utime(os.path.join(name[0.2], "index.json"), (2000, 2000))
    # reading A makes it more recently used than B, so B is now the LRU
    assert index.load_index(media_mp4, cache_dir=cache, fps=0.5, batch_size=2) is not None
    cap = sum(index._dir_size(d) for d in name.values())
    build(gw, media_mp4, cache, fps=0.1, max_cache_bytes=cap)  # C overflows the cap
    assert os.path.isdir(name[0.5])
    assert not os.path.isdir(name[0.2])
    assert len(os.listdir(cache)) == 2


def test_fingerprint_changes_on_same_size_content_change(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"A" * 4096)
    st = p.stat()
    a = index.fingerprint(p)
    p.write_bytes(b"B" * 4096)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert index.fingerprint(p)["head_sha256"] != a["head_sha256"]
    assert index.fingerprint(p)["size"] == a["size"]


def test_default_cache_dir_uses_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert index.default_cache_dir() == str(tmp_path / "media-cli" / "index")
    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert index.default_cache_dir() == str(tmp_path / ".cache" / "media-cli" / "index")
