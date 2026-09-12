#!/usr/bin/env python3
"""Source entry for the native peer helper; installed users run multithread peer."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from relay_runtime.provider import peer_main

if __name__ == "__main__":
    raise SystemExit(peer_main())
