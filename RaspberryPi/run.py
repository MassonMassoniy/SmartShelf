#!/usr/bin/env python3
"""Launch backend from RaspberryPi/backend without module path setup."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from RaspberryPi.backend.app.main import main

if __name__ == "__main__":
    main()
