from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from html import escape
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_DEFAULT_CONFIG_PATH = _BACKEND_DIR / "config" / "email_config.json"
_DEFAULT_EXAMPLE_PATH = _BACKEND_DIR / "config" / "email_config.example.json"

REQUEST_TIMEOUT_SECONDS = 15

_ZONE_COLORS = {
    "green": "#059669",
    "yellow": "#d97706",
    "red": "#dc2626",
    "unknown": "#64748b",
}


class EmailNotifier:
    """Sends trigger alerts by email via SMTP.

    Reads settings from `config/email_config.json` (auto-created from the example
    on first run, gitignored). Environment variables take precedence when set:
    EMAIL_SMTP_HOST, EMAIL_SMTP_PORT, EMAIL_USE_TLS, EMAIL_USERNAME,
    EMAIL_PASSWORD, EMAIL_FROM, EMAIL_FROM_NAME, EMAIL_RECIPIENTS
    (comma-separated). No-ops with a one-time warning until SMTP credentials and
    at least one recipient are configured.
    """

    def __init__(
        self,
        config_path: str | Path = _DEFAULT_CONFIG_PATH,
        example_path: str | Path = _DEFAULT_EXAMPLE_PATH,
    ) -> None:
        self._config_path = Path(config_path)
        self._example_path = Path(example_path)
        self._warned_unconfigured = False
        self._config = self._load_config()

    def _load_config(self) -> dict[str, Any]:
        if not self._config_path.exists() and self._example_path.exists():
            os.makedirs(self._config_path.parent, exist_ok=True)
            shutil.copyfile(self._example_path, self._config_path)

        file_cfg: dict[str, Any] = {}
        if self._config_path.exists():
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                if isinstance(payload, dict):
                    file_cfg = payload
            except (OSError, ValueError):
                pass

        recipients_raw = os.environ.get("EMAIL_RECIPIENTS")
        if recipients_raw is not None:
            recipients = [part.strip() for part in recipients_raw.split(",") if part.strip()]
        else:
            recipients = [
                str(item).strip()
                for item in (file_cfg.get("recipients") or [])
                if str(item).strip()
            ]

        use_tls_env = os.environ.get("EMAIL_USE_TLS")
        if use_tls_env is not None:
            use_tls = use_tls_env.strip().lower() in {"1", "true", "yes", "on"}
        else:
            use_tls = bool(file_cfg.get("use_tls", True))

        try:
            smtp_port = int(os.environ.get("EMAIL_SMTP_PORT", file_cfg.get("smtp_port", 587)))
        except (TypeError, ValueError):
            smtp_port = 587

        return {
            "smtp_host": str(os.environ.get("EMAIL_SMTP_HOST", file_cfg.get("smtp_host") or "")).strip(),
            "smtp_port": smtp_port,
            "use_tls": use_tls,
            "username": str(os.environ.get("EMAIL_USERNAME", file_cfg.get("username") or "")).strip(),
            "password": str(os.environ.get("EMAIL_PASSWORD", file_cfg.get("password") or "")).strip(),
            "from_address": str(
                os.environ.get("EMAIL_FROM", file_cfg.get("from_address") or "")
            ).strip(),
            "from_name": str(
                os.environ.get("EMAIL_FROM_NAME", file_cfg.get("from_name") or "SmartShelf")
            ).strip()
            or "SmartShelf",
            "recipients": recipients,
        }

    @property
    def is_configured(self) -> bool:
        cfg = self._config
        placeholder_markers = ("your.account@", "your-16-char", "example.com")
        secrets = f"{cfg['username']} {cfg['password']} {cfg['from_address']}"
        if any(marker in secrets for marker in placeholder_markers):
            return False
        return bool(
            cfg["smtp_host"]
            and cfg["username"]
            and cfg["password"]
            and cfg["from_address"]
            and cfg["recipients"]
        )

    @property
    def recipients(self) -> list[str]:
        return list(self._config["recipients"])

    def _build_message(
        self,
        *,
        subject: str,
        cell_label: str,
        assignment: str,
        metric_label: str,
        previous_zone: str,
        new_zone: str,
        value_str: str,
        sustain_minutes: float,
    ) -> EmailMessage:
        prev = previous_zone.upper()
        new = new_zone.upper()
        prev_color = _ZONE_COLORS.get(previous_zone.lower(), _ZONE_COLORS["unknown"])
        new_color = _ZONE_COLORS.get(new_zone.lower(), _ZONE_COLORS["unknown"])

        plain = (
            f"SmartShelf Alert\n"
            f"{'=' * 40}\n\n"
            f"Cell: {cell_label}\n"
            f"Product / bounds: {assignment}\n"
            f"Metric: {metric_label}\n"
            f"Zone change: {prev} -> {new}\n"
            f"Sustained: {sustain_minutes:g}+ minutes\n"
            f"Current reading: {value_str}\n\n"
            f"This alert was sent because the reading stayed in a worse zone "
            f"continuously past the sustain threshold.\n"
        )

        html = f"""\
<!DOCTYPE html>
<html>
  <body style="margin:0;padding:0;background:#f1f5f9;font-family:Segoe UI,Helvetica,Arial,sans-serif;color:#0f172a;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f1f5f9;padding:24px 12px;">
      <tr>
        <td align="center">
          <table role="presentation" width="560" cellspacing="0" cellpadding="0" style="max-width:560px;width:100%;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e2e8f0;">
            <tr>
              <td style="background:#0f172a;padding:20px 24px;">
                <div style="font-size:13px;letter-spacing:0.08em;text-transform:uppercase;color:#94a3b8;">SmartShelf</div>
                <div style="font-size:22px;font-weight:700;color:#ffffff;margin-top:4px;">Shelf condition alert</div>
              </td>
            </tr>
            <tr>
              <td style="padding:24px;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-bottom:20px;">
                  <tr>
                    <td style="padding:12px 14px;background:#f8fafc;border-radius:8px;border:1px solid #e2e8f0;">
                      <div style="font-size:12px;color:#64748b;text-transform:uppercase;letter-spacing:0.06em;">Cell</div>
                      <div style="font-size:20px;font-weight:700;margin-top:2px;">{escape(cell_label)}</div>
                      <div style="font-size:14px;color:#475569;margin-top:4px;">{escape(assignment)}</div>
                    </td>
                  </tr>
                </table>

                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-bottom:20px;">
                  <tr>
                    <td width="48%" style="padding:12px 14px;background:#f8fafc;border-radius:8px;border:1px solid #e2e8f0;vertical-align:top;">
                      <div style="font-size:12px;color:#64748b;text-transform:uppercase;letter-spacing:0.06em;">Metric</div>
                      <div style="font-size:16px;font-weight:600;margin-top:2px;">{escape(metric_label)}</div>
                    </td>
                    <td width="4%"></td>
                    <td width="48%" style="padding:12px 14px;background:#f8fafc;border-radius:8px;border:1px solid #e2e8f0;vertical-align:top;">
                      <div style="font-size:12px;color:#64748b;text-transform:uppercase;letter-spacing:0.06em;">Reading</div>
                      <div style="font-size:16px;font-weight:600;margin-top:2px;">{escape(value_str)}</div>
                    </td>
                  </tr>
                </table>

                <div style="text-align:center;padding:18px 12px;background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;margin-bottom:16px;">
                  <div style="font-size:12px;color:#9a3412;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:8px;">Zone change</div>
                  <div style="font-size:18px;font-weight:700;">
                    <span style="color:{prev_color};">{escape(prev)}</span>
                    <span style="color:#94a3b8;"> &rarr; </span>
                    <span style="color:{new_color};">{escape(new)}</span>
                  </div>
                  <div style="font-size:13px;color:#78716c;margin-top:8px;">
                    Sustained {escape(f"{sustain_minutes:g}")}+ minutes
                  </div>
                </div>

                <p style="margin:0;font-size:13px;line-height:1.5;color:#64748b;">
                  This alert fires once when a metric stays in a worse zone
                  (green&rarr;yellow or yellow&rarr;red) past the sustain threshold.
                </p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""

        msg = EmailMessage()
        cfg = self._config
        msg["Subject"] = subject
        msg["From"] = formataddr((cfg["from_name"], cfg["from_address"]))
        msg["To"] = ", ".join(cfg["recipients"])
        msg.set_content(plain)
        msg.add_alternative(html, subtype="html")
        return msg

    def _send_sync(self, message: EmailMessage) -> bool:
        cfg = self._config
        try:
            with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=REQUEST_TIMEOUT_SECONDS) as server:
                if cfg["use_tls"]:
                    server.starttls()
                server.login(cfg["username"], cfg["password"])
                server.send_message(message)
            return True
        except (OSError, smtplib.SMTPException):
            logger.exception("Email send failed")
            return False

    async def send_alert(
        self,
        *,
        cell_label: str,
        assignment: str,
        metric_label: str,
        previous_zone: str,
        new_zone: str,
        value_str: str,
        sustain_minutes: float,
    ) -> bool:
        if not self.is_configured:
            if not self._warned_unconfigured:
                logger.warning(
                    "Email SMTP/recipients not configured; skipping alert. "
                    "Fill in %s (or EMAIL_* env vars) to enable notifications.",
                    self._config_path,
                )
                self._warned_unconfigured = True
            return False

        subject = (
            f"SmartShelf Alert: Cell {cell_label} — "
            f"{metric_label} {previous_zone.upper()} → {new_zone.upper()}"
        )
        message = self._build_message(
            subject=subject,
            cell_label=cell_label,
            assignment=assignment,
            metric_label=metric_label,
            previous_zone=previous_zone,
            new_zone=new_zone,
            value_str=value_str,
            sustain_minutes=sustain_minutes,
        )
        return await asyncio.to_thread(self._send_sync, message)
