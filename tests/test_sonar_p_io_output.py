"""Pin atomic_output's commit-or-discard guarantee (o5) without needing ffmpeg."""

from __future__ import annotations

import os

import pytest

from media_cli.media import output as out


def _plan(tmp_path, *, overwrite: bool = False) -> out.OutputPlan:
    src = tmp_path / "src.mp4"
    src.write_bytes(b"source")
    dst = tmp_path / "out.mp4"
    return out.OutputPlan(
        src=str(src),
        dst=str(dst),
        tmp_path=str(tmp_path / ".out.mp4.abc.tmp.mp4"),
        container="mp4",
        overwrite=overwrite,
        streams=(),
        requested_dst=str(dst),
    )


@pytest.mark.parametrize("exc", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_exception_in_block_leaves_no_dst_and_no_tmp(tmp_path, exc):
    plan = _plan(tmp_path)
    with pytest.raises(exc):
        with out.atomic_output(plan) as tmp:
            with open(tmp, "wb") as f:
                f.write(b"partial")
            raise exc("boom")
    assert not os.path.exists(plan.dst)
    assert not os.path.exists(plan.tmp_path)
    assert sorted(os.listdir(tmp_path)) == ["src.mp4"]


def test_early_generator_close_discards(tmp_path):
    plan = _plan(tmp_path)
    cm = out.atomic_output(plan)
    tmp = cm.__enter__()
    with open(tmp, "wb") as f:
        f.write(b"partial")
    cm.gen.close()  # early exit: GeneratorExit at the yield
    assert not os.path.exists(plan.dst)
    assert not os.path.exists(plan.tmp_path)


def test_success_commits(tmp_path):
    plan = _plan(tmp_path)
    with out.atomic_output(plan) as tmp:
        with open(tmp, "wb") as f:
            f.write(b"complete")
    with open(plan.dst, "rb") as f:
        assert f.read() == b"complete"
    assert not os.path.exists(plan.tmp_path)


def test_success_with_empty_tmp_raises_and_leaves_nothing(tmp_path):
    plan = _plan(tmp_path)
    with pytest.raises(out.MediaEnvError):
        with out.atomic_output(plan):
            pass  # nothing written
    assert not os.path.exists(plan.dst)
    assert not os.path.exists(plan.tmp_path)
