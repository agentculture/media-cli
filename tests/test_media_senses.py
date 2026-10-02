"""Senses client tests: every test talks to a local stub HTTP server, never a live gateway."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from media_cli.media import senses
from media_cli.media.errors import (
    ENV_SENSE_NOT_LOCAL,
    ENV_SENSE_UNAVAILABLE,
    INPUT_UNREADABLE,
    MediaEnvError,
    MediaInputError,
)

# A REAL /capabilities payload recorded from this host's gateway
# (GET http://localhost:8001/capabilities, 2026-09-30), trimmed to four roles and
# the fields the client reads. ``senses``/``stt`` are served here; ``cortex`` is
# proxied to a peer; ``muse`` is not loaded. Values are verbatim from the recording.
RECORDED_CAPS = {
    "cortex": {
        "role": "cortex",
        "model": "unsloth/Qwen3.8-27B-NVFP4",
        "endpoint": "http://localhost:8001",
        "feasible": False,
        "ready": True,
        "loaded": False,
        "member": "spark2",
        "proxied": True,
        "hosted_by": "http://spark2.tail0be7e0.ts.net:8000",
    },
    "senses": {
        "role": "senses",
        "model": "nvidia/Gemma-4-26B-A4B-NVFP4",
        "endpoint": "http://localhost:8001",
        "feasible": True,
        "ready": True,
        "loaded": True,
        "replicas": [{"origin": "http://spark.tail0be7e0.ts.net:8001", "local": True}],
    },
    "muse": {
        "role": "muse",
        "model": "nvidia/Gemma-4-31B-IT-NVFP4",
        "endpoint": "http://localhost:8001",
        "feasible": False,
        "ready": False,
        "loaded": False,
    },
    "stt": {
        "role": "stt",
        "model": "ivrit-ai/whisper-large-v3-turbo",
        "endpoint": "http://localhost:8001",
        "feasible": True,
        "ready": True,
        "loaded": True,
        "replicas": [{"origin": "http://spark.tail0be7e0.ts.net:8001", "local": True}],
    },
}

# SYNTHESIZED (not recorded): a senses lane that is proxied to a peer, in the shape
# the recorded ``cortex`` entry shows, plus a suffixed-lane-only listing (lobes#284).
PROXIED_SENSES = {
    "senses": {**RECORDED_CAPS["cortex"], "role": "senses", "model": "nvidia/Gemma-4-26B"},
}
SUFFIXED_ONLY = {"senses-thor": {**RECORDED_CAPS["senses"], "role": "senses-thor"}}
REPLICA_REMOTE_ONLY = {
    "senses": {
        **RECORDED_CAPS["senses"],
        "replicas": [{"origin": "http://thor.example:8001", "local": False}],
    }
}


class Stub:
    """A tiny gateway: serves /capabilities, records requests, scripts POST statuses."""

    def __init__(self, caps, post_statuses=None, caps_body=None, delay=0.0):
        self.caps = caps
        self.caps_body = caps_body
        self.post_statuses = list(post_statuses or [])
        self.delay = delay
        self.log = []  # (method, path, body_bytes, headers)
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _send(self, status, body):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                stub.log.append(("GET", self.path, b"", dict(self.headers)))
                if stub.delay:
                    import time

                    time.sleep(stub.delay)
                self._send(200, stub.caps_body if stub.caps_body is not None else stub.caps)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                stub.log.append(("POST", self.path, body, dict(self.headers)))
                status = stub.post_statuses.pop(0) if stub.post_statuses else 200
                if status != 200:
                    self._send(status, {"error": "scripted"})
                elif self.path.endswith("/audio/transcriptions"):
                    self._send(200, {"text": "hello world"})
                else:
                    want_json = b"json_object" in body
                    content = '{"yes": true}' if want_json else "a red square"
                    self._send(200, {"choices": [{"message": {"content": content}}]})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    @property
    def posts(self):
        return [e for e in self.log if e[0] == "POST"]


@pytest.fixture
def stub_factory():
    made = []

    def make(*a, **kw):
        s = Stub(*a, **kw)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def client_for(stub, **kw):
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("timeout", 5.0)
    return senses.SensesClient(base_url=stub.url, **kw)


@pytest.fixture
def img(tmp_path):
    p = tmp_path / "f.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\nfakepixels")
    return p


@pytest.fixture
def wav(tmp_path):
    p = tmp_path / "a.wav"
    p.write_bytes(b"RIFFfakewav")
    return p


def test_describe_images_local_sends_data_url(stub_factory, img):
    stub = stub_factory(RECORDED_CAPS)
    out = client_for(stub).describe_images([img], "what is this?")
    assert out == "a red square"
    assert stub.log[0][:2] == ("GET", "/capabilities")
    body = json.loads(stub.posts[0][2])
    assert body["model"] == "senses"
    parts = body["messages"][-1]["content"]
    assert parts[0] == {"type": "text", "text": "what is this?"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_describe_images_response_format_returns_dict(stub_factory, img):
    stub = stub_factory(RECORDED_CAPS)
    out = client_for(stub).describe_images(
        [img], "yes/no?", response_format={"type": "json_object"}
    )
    assert out == {"yes": True}


def test_transcribe_multipart(stub_factory, wav):
    stub = stub_factory(RECORDED_CAPS)
    assert client_for(stub).transcribe(wav, language="he") == "hello world"
    method, path, body, headers = stub.posts[0]
    assert path == "/v1/audio/transcriptions"
    assert headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'name="file"' in body
    assert b"RIFFfakewav" in body
    assert b'name="language"' in body
    assert b"he" in body
    # stt is checked for locality, not senses
    assert stub.log[0][:2] == ("GET", "/capabilities")


def test_served_model(stub_factory):
    stub = stub_factory(RECORDED_CAPS)
    c = client_for(stub)
    assert c.served_model("senses") == "nvidia/Gemma-4-26B-A4B-NVFP4"
    assert c.served_model("stt") == "ivrit-ai/whisper-large-v3-turbo"


def test_locality_verdict_cached(stub_factory, img):
    stub = stub_factory(RECORDED_CAPS)
    c = client_for(stub)
    c.describe_images([img], "a")
    c.describe_images([img], "b")
    assert len([e for e in stub.log if e[0] == "GET"]) == 1


@pytest.mark.parametrize(
    "caps,body",
    [
        (PROXIED_SENSES, None),
        (SUFFIXED_ONLY, None),
        (REPLICA_REMOTE_ONLY, None),
        ({"muse": RECORDED_CAPS["muse"]}, None),  # role absent
        (RECORDED_CAPS["senses"], None),  # wrong shape (role not keyed)
        ([1, 2], None),
        (None, b"<html>not json</html>"),
        (None, b"[]"),
        ({"senses": {"loaded": "yes"}}, None),  # not positively loaded
    ],
)
def test_not_local_fails_closed_and_sends_no_media(stub_factory, img, wav, caps, body):
    stub = stub_factory(caps, caps_body=body)
    c = client_for(stub)
    with pytest.raises(MediaEnvError) as ei:
        c.describe_images([img], "x")
    assert ei.value.kind == ENV_SENSE_NOT_LOCAL
    with pytest.raises(MediaEnvError) as ei2:
        c.transcribe(wav)
    assert ei2.value.kind == ENV_SENSE_NOT_LOCAL
    assert stub.posts == []  # obligation o4: not one media byte left the process


def test_dead_port_is_unavailable(img):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    c = senses.SensesClient(base_url=f"http://127.0.0.1:{port}", sleep=lambda s: None)
    with pytest.raises(MediaEnvError) as ei:
        c.describe_images([img], "x")
    assert ei.value.kind == ENV_SENSE_UNAVAILABLE


def test_timeout_is_unavailable(stub_factory, img):
    stub = stub_factory(RECORDED_CAPS, delay=1.5)
    c = client_for(stub, timeout=0.2)
    with pytest.raises(MediaEnvError) as ei:
        c.describe_images([img], "x")
    assert ei.value.kind == ENV_SENSE_UNAVAILABLE
    assert stub.posts == []


def test_429_then_200_retries(stub_factory, img):
    stub = stub_factory(RECORDED_CAPS, post_statuses=[429, 503])
    sleeps = []
    c = client_for(stub, sleep=sleeps.append)
    assert c.describe_images([img], "x") == "a red square"
    assert len(stub.posts) == 3
    assert len(sleeps) == 2
    assert all(0 <= s <= senses.MAX_BACKOFF_SECONDS for s in sleeps)


def test_retries_are_bounded(stub_factory, img):
    stub = stub_factory(RECORDED_CAPS, post_statuses=[502] * 50)
    c = client_for(stub, max_retries=2)
    with pytest.raises(MediaEnvError) as ei:
        c.describe_images([img], "x")
    assert ei.value.kind == ENV_SENSE_UNAVAILABLE
    assert len(stub.posts) == 3


def test_non_retryable_status_fails_fast(stub_factory, img):
    stub = stub_factory(RECORDED_CAPS, post_statuses=[401, 200])
    c = client_for(stub)
    with pytest.raises(MediaEnvError) as ei:
        c.describe_images([img], "x")
    assert ei.value.kind == ENV_SENSE_UNAVAILABLE
    assert len(stub.posts) == 1


def test_missing_image_is_input_error_without_network(stub_factory, tmp_path):
    stub = stub_factory(RECORDED_CAPS)
    c = client_for(stub)
    with pytest.raises(MediaInputError) as ei:
        c.describe_images([tmp_path / "nope.png"], "x")
    assert ei.value.kind == INPUT_UNREADABLE
    assert stub.log == []


def test_api_key_sent_as_bearer_and_never_in_errors(stub_factory, img, monkeypatch):
    stub = stub_factory(RECORDED_CAPS)
    monkeypatch.setenv("MEDIA_CLI_LOBES_KEY", "sekret")
    c = senses.SensesClient(base_url=stub.url, sleep=lambda s: None)
    c.describe_images([img], "x")
    assert all(e[3].get("Authorization") == "Bearer sekret" for e in stub.log)
    assert "sekret" not in repr(c)


def test_url_resolution_env_beats_lobes_beats_default(monkeypatch):
    monkeypatch.setenv("MEDIA_CLI_LOBES_URL", "http://env.example:1/")
    assert senses.resolve_base_url("senses") == "http://env.example:1"
    monkeypatch.delenv("MEDIA_CLI_LOBES_URL")
    monkeypatch.setattr(senses.shutil, "which", lambda n: None)
    assert senses.resolve_base_url("senses") == senses.DEFAULT_URL == "http://localhost:8001"


def test_url_resolution_lobes_endpoint(monkeypatch):
    monkeypatch.delenv("MEDIA_CLI_LOBES_URL", raising=False)
    monkeypatch.setattr(senses.shutil, "which", lambda n: "/x/lobes")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return subprocess.CompletedProcess(
            argv, 0, stdout='{"role": "senses", "endpoint": "http://lobes.example:9"}', stderr=""
        )

    monkeypatch.setattr(senses.subprocess, "run", fake_run)
    assert senses.resolve_base_url("senses") == "http://lobes.example:9"
    assert seen["argv"] == ["/x/lobes", "endpoint", "senses", "--json"]
    assert not seen["kw"].get("shell")
    assert seen["kw"].get("timeout")


@pytest.mark.parametrize("out", ["garbage", '{"endpoint": 5}', '{"endpoint": "ftp://x"}'])
def test_url_resolution_lobes_failure_falls_back(monkeypatch, out):
    monkeypatch.delenv("MEDIA_CLI_LOBES_URL", raising=False)
    monkeypatch.setattr(senses.shutil, "which", lambda n: "/x/lobes")
    monkeypatch.setattr(
        senses.subprocess, "run", lambda a, **k: subprocess.CompletedProcess(a, 0, out, "")
    )
    assert senses.resolve_base_url("senses") == senses.DEFAULT_URL


def test_url_resolution_lobes_raises_falls_back(monkeypatch):
    monkeypatch.delenv("MEDIA_CLI_LOBES_URL", raising=False)
    monkeypatch.setattr(senses.shutil, "which", lambda n: "/x/lobes")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("lobes", 1)

    monkeypatch.setattr(senses.subprocess, "run", boom)
    assert senses.resolve_base_url("senses") == senses.DEFAULT_URL


def test_no_ml_framework_imported():
    code = (
        "import sys, media_cli.media.senses, media_cli.cli;"
        "bad=[m for m in ('torch','transformers','cv2','numpy') if m in sys.modules];"
        "print(bad)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
