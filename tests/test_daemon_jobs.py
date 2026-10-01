"""Tests for the daemon job store (t18)."""

from __future__ import annotations

import json
import os
import threading

import pytest

from media_cli.media.daemon import jobs as jobs_mod
from media_cli.media.daemon.jobs import SCHEMA_VERSION, JobStore
from media_cli.media.errors import MediaInputError


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return JobStore()


def test_default_root_uses_xdg_state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    s = JobStore()
    assert s.root == tmp_path / "media-cli" / "jobs"
    assert s.root.is_dir()
    assert (s.root.stat().st_mode & 0o777) == 0o700


def test_default_root_falls_back_to_home(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert JobStore().root == tmp_path / ".local" / "state" / "media-cli" / "jobs"


def test_create_persists_record(store):
    rec = store.create("trim", ["media", "trim", "a.mp4"], output="out.mp4", meta={"x": 1})
    path = store.root / f"{rec.id}.json"
    data = json.loads(path.read_text())
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["kind"] == "trim"
    assert data["state"] == "queued"
    assert data["argv"] == ["media", "trim", "a.mp4"]
    assert data["output"] == "out.mp4"
    assert data["sense_calls"] == []
    assert data["progress"] is None
    assert data["error"] is None
    assert data["meta"] == {"x": 1}
    assert set(data["timings"]) >= {"created", "started", "finished"}
    assert data["timings"]["started"] is None


def test_restart_survival(store, tmp_path):
    rec = store.create("k", ["a"])
    store.update(rec.id, state="running")
    fresh = JobStore(root=store.root)
    got = fresh.get(rec.id)
    assert got.state == "running"
    assert got.timings["started"] is not None
    assert [r.id for r in fresh.list()] == [rec.id]


def test_ids_unique_and_sortable_under_threads(store):
    ids: list[str] = []
    lock = threading.Lock()

    def work():
        for _ in range(25):
            r = store.create("k", [])
            with lock:
                ids.append(r.id)

    ts = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(ids) == 200 == len(set(ids))
    assert len(store.list()) == 200
    listed = [r.id for r in store.list()]
    assert listed == sorted(listed)


def test_ids_sort_in_creation_order(store):
    made = [store.create("k", []).id for _ in range(20)]
    assert made == sorted(made)


def test_legal_transitions(store):
    a = store.create("k", [])
    store.update(a.id, state="running")
    store.update(a.id, state="done", progress=1.0)
    rec = store.get(a.id)
    assert rec.state == "done" and rec.progress == 1.0
    assert rec.timings["finished"] is not None
    b = store.create("k", [])
    assert store.update(b.id, state="cancelled").state == "cancelled"
    c = store.create("k", [])
    store.update(c.id, state="running")
    store.update(c.id, state="cancelled")


@pytest.mark.parametrize(
    "path",
    [["done"], ["failed"], ["running", "queued"], ["running", "done", "running"], ["bogus"]],
)
def test_illegal_transitions(store, path):
    rec = store.create("k", [])
    with pytest.raises(MediaInputError) as ei:
        for st in path:
            store.update(rec.id, state=st)
    assert ei.value.kind.startswith("input.")


def test_same_state_update_allowed_for_non_state_fields(store):
    rec = store.create("k", [])
    store.update(rec.id, state="running")
    store.update(rec.id, progress=0.5, sense_calls=[{"a": 1}])
    got = store.get(rec.id)
    assert got.progress == 0.5 and got.sense_calls == [{"a": 1}]


def test_unknown_field_rejected(store):
    rec = store.create("k", [])
    with pytest.raises(MediaInputError):
        store.update(rec.id, nope=1)
    with pytest.raises(MediaInputError):
        store.update(rec.id, id="other")


def test_get_missing(store):
    with pytest.raises(MediaInputError):
        store.get("nope")
    with pytest.raises(MediaInputError):
        store.get("../etc/passwd")


def test_mark_failed_tail_and_log_path(store):
    rec = store.create("k", [])
    store.update(rec.id, state="running")
    stderr = "\n".join(f"line{i}" for i in range(50))
    store.mark_failed(rec.id, stderr)
    got = store.get(rec.id)
    assert got.state == "failed"
    assert got.error["kind"] == "env.ffmpeg_failed"
    assert got.error["log_path"] == str(store.log_path(rec.id))
    tail = got.error["stderr_tail"]
    assert tail.splitlines()[-1] == "line49" and "line29" not in tail
    assert len(tail.splitlines()) == 20
    assert "line30" in tail


def test_mark_failed_from_queued(store):
    rec = store.create("k", [])
    store.mark_failed(rec.id, "boom")
    assert store.get(rec.id).state == "failed"


def test_append_log(store):
    rec = store.create("k", [])
    store.append_log(rec.id, "a\n")
    store.append_log(rec.id, "b\n")
    assert store.log_path(rec.id).read_text() == "a\nb\n"


def test_newer_schema_raises(store):
    rec = store.create("k", [])
    p = store.root / f"{rec.id}.json"
    d = json.loads(p.read_text())
    d["schema_version"] = SCHEMA_VERSION + 1
    p.write_text(json.dumps(d))
    with pytest.raises(MediaInputError) as ei:
        store.get(rec.id)
    assert "schema" in str(ei.value).lower()
    with pytest.raises(MediaInputError):
        store.list()


def test_atomic_write_leaves_no_partial(store, monkeypatch):
    rec = store.create("k", [])
    before = (store.root / f"{rec.id}.json").read_text()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(jobs_mod.os, "replace", boom)
    with pytest.raises(OSError):
        store.update(rec.id, state="running")
    monkeypatch.undo()
    assert (store.root / f"{rec.id}.json").read_text() == before
    assert sorted(os.listdir(store.root)) == [f"{rec.id}.json"]


def test_atomic_write_serialization_failure(store):
    rec = store.create("k", [])
    with pytest.raises(TypeError):
        store.update(rec.id, meta={"bad": object()})
    assert sorted(os.listdir(store.root)) == [f"{rec.id}.json"]
    assert store.get(rec.id).meta == {}


def test_mark_failed_on_terminal_job_rejected(store):
    rec = store.create("k", [])
    store.update(rec.id, state="running")
    store.update(rec.id, state="done")
    with pytest.raises(MediaInputError):
        store.mark_failed(rec.id, "x")


def test_custom_root_outside_allowed_bases_is_refused(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    with pytest.raises(MediaInputError) as exc:
        JobStore("/etc/media-cli-jobs")
    assert exc.value.kind == "input.job_store_invalid"
    assert exc.value.remediation


def test_custom_root_that_is_a_file_is_refused(tmp_path) -> None:
    f = tmp_path / "not-a-dir"
    f.write_text("x")
    with pytest.raises(MediaInputError) as exc:
        JobStore(f)
    assert exc.value.kind == "input.job_store_invalid"


def test_custom_root_symlink_resolved_before_check(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    store = JobStore(link)
    assert store.root == real.resolve()
