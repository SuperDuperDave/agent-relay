#!/usr/bin/env python3
"""Development entrypoint for the source-bound Relay coordination ledger.

This entrypoint is for repository-local development and isolated tests. It is
not an installed launcher or proof of workspace enrollment.
"""

from relay_core.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
