#!/usr/bin/env python3
"""Send a sample SmartShelf VOC email to verify SMTP configuration."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from RaspberryPi.backend.services.email_notifier import EmailNotifier


async def main() -> int:
    notifier = EmailNotifier()
    if not notifier.is_configured:
        print(
            "Email not configured. Fill in config/email_config.json "
            "(copy from email_config.example.json) or set EMAIL_* env vars."
        )
        return 1

    recipients = notifier.recipients
    print(f"Sending test VOC alert to: {', '.join(recipients)}")
    ok = await notifier.send_voc_alert(
        cell_label="A3",
        assignment="Raw Meat Policy",
        severity="alert",   # try: warning | alert | critical
        score=72,
        sigma=3,
        sustain_minutes=5,
    )
    if ok:
        print("Success — check your inbox for the SmartShelf VOC email.")
        return 0

    print("Failed to send. Check SMTP host/credentials and network access.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
