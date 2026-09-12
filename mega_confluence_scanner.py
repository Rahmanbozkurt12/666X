#!/usr/bin/env python3
"""Compatibility entry → single-file mega_confluence_all_in_one.py"""

from __future__ import annotations

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parent / "mega_confluence_all_in_one.py"), run_name="__main__")
