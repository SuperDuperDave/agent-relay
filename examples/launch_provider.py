#!/usr/bin/env python3
"""Source compatibility entry; installed users can run multithread launch instead."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from relay_runtime.provider import launch_main

if __name__ == "__main__":
    raise SystemExit(launch_main())
