"""Entry point for ``python -m media_cli``."""

from __future__ import annotations

import sys

from media_cli.cli import main

if __name__ == "__main__":
    sys.exit(main())
