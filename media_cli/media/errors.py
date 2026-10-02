"""Typed media errors inside the CliError contract.

``MediaInputError`` exits 1 (caller's input is at fault); ``MediaEnvError``
exits 2 (the environment is at fault). Each carries a stable machine ``kind``
string (``input.*`` / ``env.*``) emitted as ``kind`` in ``--json`` error output.
"""

from __future__ import annotations

from media_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError

# Input errors (exit 1)
INPUT_UNREADABLE = "input.unreadable"
INPUT_TIMESTAMP_OUT_OF_RANGE = "input.timestamp_out_of_range"
INPUT_REGION_OUTSIDE_FRAME = "input.region_outside_frame"

# Environment errors (exit 2)
ENV_FFMPEG_MISSING = "env.ffmpeg_missing"
ENV_FFMPEG_FAILED = "env.ffmpeg_failed"
ENV_FILTER_UNAVAILABLE = "env.filter_unavailable"
ENV_SENSE_UNAVAILABLE = "env.sense_unavailable"
ENV_SENSE_NOT_LOCAL = "env.sense_not_local"

STDERR_TAIL_LINES = 20


class MediaInputError(CliError):
    """The caller's input is unusable (exit 1)."""

    def __init__(self, kind: str, message: str, remediation: str = "") -> None:
        if not kind.startswith("input."):
            raise ValueError(f"MediaInputError kind must start with 'input.': {kind!r}")
        super().__init__(EXIT_USER_ERROR, message, remediation, kind)


class MediaEnvError(CliError):
    """The environment cannot do the work (exit 2)."""

    def __init__(self, kind: str, message: str, remediation: str = "") -> None:
        if not kind.startswith("env."):
            raise ValueError(f"MediaEnvError kind must start with 'env.': {kind!r}")
        super().__init__(EXIT_ENV_ERROR, message, remediation, kind)


def ffmpeg_failure(stderr: str, message: str = "ffmpeg failed") -> MediaEnvError:
    """Build an env error whose remediation is the last ~20 lines of ffmpeg stderr."""
    lines = (stderr or "").strip().splitlines()[-STDERR_TAIL_LINES:]
    return MediaEnvError(ENV_FFMPEG_FAILED, message, "\n".join(lines))
