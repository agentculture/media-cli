"""Pin the allowlist self-check and FilterNode validation order across the S3776 split."""

from __future__ import annotations

import pytest

from media_cli.media import ops
from media_cli.media.ops import FilterNode, Param


@pytest.mark.parametrize(
    "table,match",
    [
        ({"Bad-Name": {}}, r"filter name 'Bad-Name' is not a plain token"),
        ({"crop": {"W!": ops._i()}}, r"param crop\.W! is not a plain token"),
        ({"crop": {"m": ops._e("ok", "no way")}}, r"enum crop\.m has a non-token value"),
        ({"crop": {"m": (ops._i(), Param(str))}}, r"param crop\.m has an untyped kind"),
    ],
)
def test_check_table_refuses_malformed_entries(monkeypatch, table, match):
    monkeypatch.setattr(ops, "ALLOWLIST", table)
    with pytest.raises(AssertionError, match=match):
        ops._check_table()


def test_check_table_accepts_the_shipped_allowlist():
    ops._check_table()


_BOX = {"x": 1, "y": 2, "w": 3, "h": 4}
_CROP = {"w": 3, "h": 4}


@pytest.mark.parametrize(
    "name,params,exc,match",
    [
        # unknown keys are reported before missing required ones and before enable checks
        ("xfade", {"bogus": 1, "enable_start": 1.0}, ValueError, "unknown param 'bogus'"),
        ("xfade", {"enable_start": 1.0}, ValueError, "missing required param 'transition'"),
        ("crop", {**_CROP, "enable_start": 1.0, "enable_end": 2.0}, ValueError, "does not support"),
        ("drawbox", {**_BOX, "enable_start": 1.0}, ValueError, "go together"),
        ("drawbox", {**_BOX, "enable_end": 1.0}, ValueError, "go together"),
        ("drawbox", {**_BOX, "enable_start": "1", "enable_end": 2.0}, TypeError, "enable_start"),
        ("drawbox", {**_BOX, "enable_start": 2.0, "enable_end": 1.0}, ValueError, "enable_end < "),
    ],
)
def test_filter_node_validation_order(name, params, exc, match):
    with pytest.raises(exc, match=match):
        FilterNode(name, params, "v")


def test_filter_node_enable_pair_is_normalized_to_floats():
    n = FilterNode("drawbox", {**_BOX, "enable_start": 1, "enable_end": 2}, "v")
    assert n.params["enable_start"] == 1.0
    assert isinstance(n.params["enable_start"], float)
    assert n.render() == "drawbox=x=1:y=2:w=3:h=4:enable=between(t\\,1\\,2)"
