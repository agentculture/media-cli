"""ffmpeg/ffprobe tool layer: the single subprocess seam for media binaries.

No other module may call ``subprocess`` for ffmpeg/ffprobe; everything goes
through here.  Every invocation is an absolute-path argv *list* with
``shell=False``.  Standard library only.

Public API
----------
``ffmpeg() -> str`` / ``ffprobe() -> str``
    Absolute path via ``shutil.which`` (cached).  Missing binary raises
    ``MediaEnvError(ENV_FFMPEG_MISSING)`` whose remediation names the binary
    and its package.
``has_filter(name) -> bool`` / ``has_encoder(name) -> bool``
    Parsed from ``ffmpeg -hide_banner -filters`` / ``-encoders`` (cached).
``require_filter(name)`` / ``require_encoder(name)``
    Raise ``MediaEnvError(ENV_FILTER_UNAVAILABLE)`` with an install hint.
``run(tool, args, *, timeout=None, check=True, capture=True) -> CompletedProcess``
    ``tool`` is ``"ffmpeg"`` or ``"ffprobe"``; ``args`` excludes the binary.
    Output is text.  Non-zero exit with ``check=True`` raises
    ``ffmpeg_failure(stderr)``; a timeout raises ``MediaEnvError(ENV_FFMPEG_FAILED)``.
``spawn(tool, args, **popen_kwargs) -> subprocess.Popen``
    For long runs (progress parsing, process groups via
    ``start_new_session=True``).  ``shell`` may not be passed.
``reset_cache()``
    Clear all cached lookups (for tests).
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
from typing import Any

from media_cli.media.errors import (
    ENV_FFMPEG_FAILED,
    ENV_FFMPEG_MISSING,
    ENV_FILTER_UNAVAILABLE,
    MediaEnvError,
    ffmpeg_failure,
)

_TOOLS = ("ffmpeg", "ffprobe")
_FLAG_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ.|=")
_INSTALL_HINT = "install the ffmpeg package (e.g. sudo apt-get install ffmpeg)"


def _which(name: str) -> str | None:
    return shutil.which(name)


@functools.lru_cache(maxsize=None)
def _resolve(name: str) -> str:
    found = _which(name)
    if not found:
        raise MediaEnvError(
            ENV_FFMPEG_MISSING,
            f"{name} not found on PATH",
            f"{name} is required: {_INSTALL_HINT}",
        )
    return os.path.abspath(found)


def ffmpeg() -> str:
    """Absolute path to ffmpeg (cached)."""
    return _resolve("ffmpeg")


def ffprobe() -> str:
    """Absolute path to ffprobe (cached)."""
    return _resolve("ffprobe")


def _binary(tool: str) -> str:
    if tool not in _TOOLS:
        raise ValueError(f"tool must be one of {_TOOLS}, got {tool!r}")
    return _resolve(tool)


def reset_cache() -> None:
    """Clear cached binary paths and capability listings."""
    _resolve.cache_clear()
    _listing.cache_clear()


@functools.lru_cache(maxsize=None)
def _listing(flag: str) -> frozenset[str]:
    cp = run("ffmpeg", ["-hide_banner", flag])
    names: set[str] = set()
    for line in cp.stdout.splitlines():
        parts = line.split()
        # Entry lines: "<flags> <name> ..." after a "------" separator; the
        # flags column is 6-7 chars of [A-Z.|=] and never contains a space.
        if len(parts) >= 2 and len(parts[0]) >= 3 and set(parts[0]) <= _FLAG_CHARS:
            names.add(parts[1])
    return frozenset(names)


def has_filter(name: str) -> bool:
    """True if this ffmpeg build lists filter ``name``."""
    return name in _listing("-filters")


def has_encoder(name: str) -> bool:
    """True if this ffmpeg build lists encoder ``name``."""
    return name in _listing("-encoders")


def require_filter(name: str) -> None:
    if not has_filter(name):
        raise MediaEnvError(
            ENV_FILTER_UNAVAILABLE,
            f"ffmpeg filter '{name}' is not available in this build",
            f"use an ffmpeg build that includes the '{name}' filter: {_INSTALL_HINT}",
        )


def require_encoder(name: str) -> None:
    if not has_encoder(name):
        raise MediaEnvError(
            ENV_FILTER_UNAVAILABLE,
            f"ffmpeg encoder '{name}' is not available in this build",
            f"use an ffmpeg build that includes the '{name}' encoder: {_INSTALL_HINT}",
        )


def run(
    tool: str,
    args: list[str],
    *,
    timeout: float | None = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    """Run ffmpeg/ffprobe synchronously (absolute argv list, shell=False)."""
    argv = [_binary(tool), *map(str, args)]
    try:
        cp = subprocess.run(  # noqa: S603  # nosec B603
            argv,
            capture_output=capture,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaEnvError(
            ENV_FFMPEG_FAILED,
            f"{tool} timed out after {timeout}s",
            "retry with a longer timeout or a shorter input",
        ) from exc
    except OSError as exc:
        raise MediaEnvError(
            ENV_FFMPEG_FAILED, f"could not execute {tool}: {exc}", _INSTALL_HINT
        ) from exc
    if check and cp.returncode != 0:
        raise ffmpeg_failure(cp.stderr or "", f"{tool} failed (exit {cp.returncode})")
    return cp


def spawn(tool: str, args: list[str], **popen_kwargs: Any) -> subprocess.Popen:
    """Start ffmpeg/ffprobe without waiting (absolute argv list, shell=False)."""
    if popen_kwargs.pop("shell", False):
        raise ValueError("the shell option is not permitted")
    argv = [_binary(tool), *map(str, args)]
    try:
        return subprocess.Popen(argv, shell=False, **popen_kwargs)  # noqa: S603  # nosec B603
    except OSError as exc:
        raise MediaEnvError(
            ENV_FFMPEG_FAILED, f"could not execute {tool}: {exc}", _INSTALL_HINT
        ) from exc
