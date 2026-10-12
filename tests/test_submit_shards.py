"""Shard planning for multi-server rollouts. No Slurm calls."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scb_slurm.submit_batch import _shard_plan  # noqa: E402


def test_two_servers_split_throttle_and_tasks() -> None:
    plan = _shard_plan(36, ["http://ga01:8000/v1", "http://ga02:8000/v1"], 16)
    assert plan == [
        ("http://ga01:8000/v1", "0-17%8"),
        ("http://ga02:8000/v1", "18-35%8"),
    ]


def test_one_server_keeps_the_whole_throttle() -> None:
    plan = _shard_plan(36, ["http://ga02:8000/v1"], 8)
    assert plan == [("http://ga02:8000/v1", "0-35%8")]


def test_throttle_must_divide_across_servers() -> None:
    with pytest.raises(Exception, match="multiple"):
        _shard_plan(36, ["http://a", "http://b"], 15)
