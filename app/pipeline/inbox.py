"""Reads replies from the live mailbox over IMAP (when configured) and routes them to their deals.

Matching: the reply's In-Reply-To / References headers against Message-IDs we sent,
falling back to the sender's address. Messages are read with BODY.PEEK so nothing is
marked as read in your mailbox; processed IDs are remembered in the store instead.
"""

import email
import imaplib
import logging
import re
from datetime import datetime, timedelta
from email.policy import default as default_policy
from email.utils import parseaddr

from app import store
from app.config import settings
from app.pipeline import runner

log = logging.getLogger(__name__)

QUOTE_START = re.compile(r"^(On .+wrote:|-{2,}\s*Original Message\s*-{2,}|From: .+|>.*)$", re.I)


def clean_body(text: str) -> str:
    """Keeps only the new part of a reply, dropping the quoted thread below it."""
    kept = []
    for line in text.splitlines():
        if QUOTE_START.match(line.strip()):
            break
        kept.append(line)
    return "\n".join(kept).strip()[:4000]


def _text_of(msg: email.message.EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_subtype() == "html":
        text = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
    return text


def poll() -> int:
    """Returns how many replies were routed to deals."""
    if not settings.reads_inbox:
        return 0
    routed = 0
    imap = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, timeout=20)
    try:
        imap.login(settings.imap_user, settings.imap_password)
        imap.select("INBOX", readonly=True)
        since = (datetime.now() - timedelta(days=14)).strftime("%d-%b-%Y")
        _, data = imap.search(None, "SINCE", since)
        for num in data[0].split():
            _, parts = imap.fetch(num, "(BODY.PEEK[])")
            msg = email.message_from_bytes(parts[0][1], policy=default_policy)
            mid = (msg["Message-ID"] or "").strip()
            refs = re.findall(r"<[^>]+>", f"{msg['In-Reply-To'] or ''} {msg['References'] or ''}")
            sender = parseaddr(msg["From"] or "")[1]
            deal_id = store.deal_for_message(refs) or store.deal_for_address(sender)
            if not deal_id or not mid or store.is_inbound_seen(mid):
                continue
            text = clean_body(_text_of(msg))
            if not text:
                continue
            try:
                runner.lead_replied(deal_id, text, mid, msg["Subject"] or "")
            except runner.NotWaiting:
                continue  # e.g. our previous answer is still waiting for approval; retry next poll
            store.mark_inbound_seen(mid)
            routed += 1
    finally:
        try:
            imap.logout()
        except (OSError, imaplib.IMAP4.error):
            pass
    return routed
