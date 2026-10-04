"""Outbound mail.

sandbox (default): everything goes to Mailpit on localhost:1025, or to data/outbox/
                   if Mailpit isn't running. Nothing leaves the machine.
live:              sends through the configured SMTP account (e.g. Brevo, Zoho), with a daily
                   cap, an opt-out footer and a suppression list.
"""

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path

from app import store
from app.config import settings

log = logging.getLogger(__name__)

OPT_OUT = "\n\n--\nNot relevant? Reply \"stop\" and we won't email you again."


class SendBlocked(RuntimeError):
    """The email was deliberately not sent (suppressed, over cap, bad address)."""


def _sender_domain() -> str:
    return parseaddr(settings.sender)[1].split("@")[-1] or "leadloop.local"


def send(
    deal_id: str,
    to: str,
    subject: str,
    body: str,
    attachment: tuple[str, str] | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
) -> tuple[str, str]:
    """Returns (delivery, message_id). Raises SendBlocked when a guardrail stops it."""
    if store.is_suppressed(to):
        raise SendBlocked(f"{to} has opted out")
    if settings.live_mail:
        if to.endswith(".local") or "@" not in to:
            raise SendBlocked(f"'{to}' is not a real address; add the lead's email first")
        if store.sends_today() >= settings.daily_send_cap:
            raise SendBlocked(f"daily send cap of {settings.daily_send_cap} reached")

    msg = EmailMessage()
    msg["From"] = settings.sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    message_id = make_msgid(domain=_sender_domain())
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = " ".join(references or [in_reply_to])
    msg.set_content(body + OPT_OUT)
    if attachment:
        filename, content = attachment
        msg.add_attachment(content.encode(), maintype="text", subtype="csv", filename=filename)

    delivery = _deliver(msg, to)
    store.record_send(message_id, deal_id, to)
    return delivery, message_id


def _deliver(msg: EmailMessage, to: str) -> str:
    if settings.live_mail:
        with _smtp(timeout=20) as smtp:
            smtp.send_message(msg)
        return "smtp"

    for attempt in range(2):
        try:
            with smtplib.SMTP("localhost", settings.sandbox_smtp_port, timeout=5) as smtp:
                smtp.send_message(msg)
            return "mailpit"
        except OSError as e:  # includes smtplib errors: say why instead of failing over silently
            log.warning("sandbox SMTP attempt %d failed (%s: %s)", attempt + 1, type(e).__name__, e)
    outbox: Path = settings.data_dir / "outbox"
    outbox.mkdir(exist_ok=True)
    safe = "".join(c if c.isalnum() else "_" for c in msg["Message-ID"])[:60]
    (outbox / f"{safe}.eml").write_bytes(bytes(msg))
    return "outbox"


def _smtp(timeout: int) -> smtplib.SMTP:
    """Connected and logged-in SMTP session. SMTP_SECURITY: ssl | starttls | none (local testing)."""
    if settings.smtp_security == "ssl":
        smtp = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=timeout)
    else:
        smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
        if settings.smtp_security == "starttls":
            smtp.starttls()
    if settings.smtp_user:
        smtp.login(settings.smtp_user, settings.smtp_password)
    return smtp


def check_connection() -> dict:
    """Logs in to SMTP and IMAP without sending anything."""
    import imaplib

    result = {"mode": settings.send_mode, "smtp": None, "imap": None}
    if not settings.live_mail:
        result["smtp"] = result["imap"] = "sandbox mode: using Mailpit, nothing to check"
        return result
    try:
        with _smtp(timeout=15):
            pass
        result["smtp"] = "ok"
    except (OSError, smtplib.SMTPException) as e:
        result["smtp"] = f"failed: {e}"
    if not settings.imap_host:
        result["imap"] = "not configured: paste the lead's replies on the board"
        return result
    try:
        imap = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, timeout=15)
        imap.login(settings.imap_user, settings.imap_password)
        imap.logout()
        result["imap"] = "ok"
    except (OSError, imaplib.IMAP4.error) as e:
        result["imap"] = f"failed: {e}"
    return result
