"""``python -m media_cli.media.daemon``: run the media daemon in the foreground.

This is what :class:`media_cli.media.daemon.client.DaemonClient` spawns
(detached, stdio to the daemon log). It prints nothing on success; a losing
instance (another daemon holds the lock) exits 0 quietly.
"""

from __future__ import annotations

import argparse
import sys

from media_cli.media.daemon import handlers  # noqa: F401  (registers the "index" job kind)
from media_cli.media.daemon import server
from media_cli.media.daemon.jobs import JobStore


def _positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def _non_negative_float(value: str) -> float:
    x = float(value)
    if x < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return x


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m media_cli.media.daemon",
        description="Run the local media daemon (normally started by the client).",
    )
    parser.add_argument("--sockdir", default=None, help="socket directory")
    parser.add_argument("--jobs-root", default=None, help="job store directory")
    parser.add_argument("--idle-timeout", type=_non_negative_float, default=None)
    parser.add_argument("--max-concurrent", type=_positive_int, default=None)
    parser.add_argument("--kill-grace", type=_non_negative_float, default=None)
    args = parser.parse_args(argv)
    kwargs = {}
    if args.idle_timeout is not None:
        kwargs["idle_timeout"] = args.idle_timeout
    if args.max_concurrent is not None:
        kwargs["max_concurrent"] = args.max_concurrent
    if args.kill_grace is not None:
        kwargs["kill_grace"] = args.kill_grace
    store = JobStore(args.jobs_root) if args.jobs_root else None
    return server.serve(args.sockdir, store=store, **kwargs)


if __name__ == "__main__":
    sys.exit(main())
