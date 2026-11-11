"""Outbound email seam. In development and tests, messages are written to ``var/outbox`` so
flows can be completed without a mail provider. Production must configure a real sender
(SPF/DKIM/DMARC, SEC-DNS-01); until then sending raises instead of silently dropping mail.

Emails never contain secrets other than single-use, expiring links (SEC-DNS-02)."""

from __future__ import annotations

import re

from ..config import get_settings
from ..ids import utcnow


def send(to: str, subject: str, body: str) -> None:
    settings = get_settings()
    if settings.is_production:
        raise RuntimeError("No email provider is configured; set one up before running in production.")
    outbox = settings.data_dir / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9@._-]", "_", to)[:80]
    stamp = utcnow().strftime("%Y%m%dT%H%M%S%f")
    (outbox / f"{stamp}-{safe}.txt").write_text(f"To: {to}\nSubject: {subject}\n\n{body}\n", encoding="utf-8")
