"""Contact directory for To/Cc/Bcc autocomplete.

Built from what the app already knows — the 90-day mail index (people Boxx
has actually corresponded with) and the pipeline's lead contacts. No new
Google scopes, nothing to maintain by hand: the directory refreshes itself
as mail is indexed. Bulk senders, muted and disregarded addresses never
appear.
"""

import re
import time
from email.utils import getaddresses

from sqlalchemy import select

from app.db import db_session

MAX_CONTACTS = 400
CACHE_TTL_SECONDS = 600

_cache: dict = {"at": 0.0, "contacts": None}

_ADDR_OK = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _excluded_addresses(session) -> set[str]:
    from app.models import DisregardRule, MutedSender

    excluded = {e.lower() for e in session.scalars(select(MutedSender.email))}
    excluded |= {
        r.contact_email.lower()
        for r in session.scalars(select(DisregardRule))
        if r.contact_email
    }
    return excluded


def _build() -> list[dict]:
    from app.models import Lead, MailMessage
    from app.tools.mail_index import _BULK_ADDR

    with db_session() as s:
        excluded = _excluded_addresses(s)
        contacts: dict[str, dict] = {}

        def slot(addr: str) -> dict | None:
            addr = addr.strip().lower()
            if not _ADDR_OK.match(addr) or addr in excluded or _BULK_ADDR.search(addr):
                return None
            return contacts.setdefault(
                addr, {"email": addr, "name": "", "hint": "", "outbound": 0, "last": ""}
            )

        for row in s.scalars(select(MailMessage).order_by(MailMessage.sent_at)):
            stamp = row.sent_at.isoformat() if row.sent_at else ""
            if row.is_outbound:
                for name, addr in getaddresses([row.to_addrs or ""]):
                    c = slot(addr)
                    if c is None:
                        continue
                    c["outbound"] += 1
                    c["last"] = max(c["last"], stamp)
                    if name and not c["name"]:
                        c["name"] = name[:120]
            elif not row.is_bulk and row.from_addr:
                c = slot(row.from_addr)
                if c is None:
                    continue
                c["last"] = max(c["last"], stamp)
                if row.from_name:
                    c["name"] = row.from_name[:120]  # their own display name wins

        # Correspondent rule: someone Boxx never wrote to isn't a contact —
        # unless they're a pipeline lead, which is an explicit relationship.
        directory = {a: c for a, c in contacts.items() if c["outbound"] > 0}
        for lead in s.scalars(select(Lead).where(Lead.discarded_at.is_(None))):
            addr = (lead.contact_email or "").strip().lower()
            if not _ADDR_OK.match(addr) or addr in excluded:
                continue
            c = directory.get(addr) or contacts.get(addr) or {
                "email": addr, "name": "", "hint": "", "outbound": 0, "last": ""
            }
            if not c["name"] and lead.contact_name:
                c["name"] = lead.contact_name[:120]
            c["hint"] = lead.business_name[:120]
            directory[addr] = c

    ranked = sorted(directory.values(), key=lambda c: (c["outbound"], c["last"]), reverse=True)
    return [
        {"email": c["email"], "name": c["name"], "hint": c["hint"]}
        for c in ranked[:MAX_CONTACTS]
    ]


def contact_directory() -> list[dict]:
    """Ranked contacts (most-written-to first), cached briefly; [] on any
    trouble so autocomplete can never break a page."""
    now = time.time()
    if _cache["contacts"] is not None and now - _cache["at"] < CACHE_TTL_SECONDS:
        return _cache["contacts"]
    try:
        contacts = _build()
    except Exception:
        return _cache["contacts"] or []
    _cache.update(at=now, contacts=contacts)
    return contacts
