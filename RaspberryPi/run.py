#!/usr/bin/env python3
"""Launch backend from RaspberryPi/backend without module path setup."""

from __future__ import annotations

import threading

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from RaspberryPi.backend.app.main_back import main_back
from RaspberryPi.frontend.main_front import main_front

threads = []

if __name__ == "__main__":
    t_back = threading.Thread(target=main_back, name="backend")
    threads.append(t_back)
    t_back.start()

    t_front = threading.Thread(target=main_front, name="frontend")
    threads.append(t_front)
    t_front.start()

    for t in threads:
        t.join()