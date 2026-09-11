"""Vendor registry: green bean importers vs everything else Boxx buys from.

The distinction matters because a green coffee bill is inventory — miss it
and roasting stops — while a services bill is ordinary A/P. Known importers
are seeded once (industry list, trimmable in Settings); routines register
vendors they meet in invoice mail as unconfirmed rows for Arda to confirm.
"""

from sqlalchemy import select

from app.db import db_session
from app.models import Vendor

VENDOR_KINDS = ("green_importer", "other")
KIND_LABELS = {"green_importer": "Green bean importer", "other": "Other vendor"}

# Well-known US specialty green coffee importers — a starting roster, not a
# claim that Boxx buys from all of them. Seeded only into an empty table;
# Arda trims/extends in Settings.
KNOWN_IMPORTERS = [
    ("Royal Coffee", "royalcoffee.com"),
    ("Cafe Imports", "cafeimports.com"),
    ("InterAmerican Coffee", "interamericancoffee.com"),
    ("Ally Coffee", "allycoffee.com"),
    ("Sucafina Specialty", "sucafina.com"),
    ("Covoya Specialty Coffee", "covoya.com"),
    ("Genuine Origin", "genuineorigin.com"),
    ("Balzac Brothers", "balzacbrothers.com"),
    ("Mercanta", "coffeehunter.com"),
    ("Bodhi Leaf Coffee Traders", "bodhileafcoffee.com"),
    ("Red Fox Coffee Merchants", "redfoxcoffeemerchants.com"),
    ("Osito Coffee", "ositocoffee.com"),
]


def seed_known_importers(session_factory=db_session) -> int:
    """One-time seed into an EMPTY vendors table only — a deploy never
    resurrects vendors Arda deleted."""
    with session_factory() as s:
        if s.scalar(select(Vendor.id).limit(1)) is not None:
            return 0
        for name, domain in KNOWN_IMPORTERS:
            s.add(Vendor(name=name, kind="green_importer", domains=domain,
                         confirmed=True, notes="seeded industry list"))
        return len(KNOWN_IMPORTERS)


def _domains(vendor: Vendor) -> list[str]:
    return [d.strip().lower() for d in (vendor.domains or "").split(",") if d.strip()]


def _matches(addr_domain: str, vendor: Vendor) -> bool:
    return any(addr_domain == d or addr_domain.endswith("." + d) for d in _domains(vendor))


def classify_email(session, email: str) -> Vendor | None:
    """The vendor a sender address belongs to, by domain, or None."""
    addr_domain = (email or "").rsplit("@", 1)[-1].strip().lower()
    if not addr_domain:
        return None
    for vendor in session.scalars(select(Vendor)):
        if _matches(addr_domain, vendor):
            return vendor
    return None


def register_vendor_impl(session, name: str, domain: str, kind: str, notes: str | None = None) -> dict:
    """Routine-facing auto-recognition: create an unconfirmed vendor unless
    the domain already belongs to one. Never reclassifies an existing row —
    that's Arda's call in Settings."""
    domain = (domain or "").strip().lower().lstrip("@")
    name = (name or "").strip()[:200]
    if not domain or "." not in domain or not name:
        return {"outcome": "error", "error": "name and a real email domain are required"}
    if kind not in VENDOR_KINDS:
        kind = "other"
    existing = classify_email(session, f"x@{domain}")
    if existing is not None:
        return {"outcome": "exists", "vendor": existing.name, "kind": existing.kind,
                "confirmed": existing.confirmed}
    vendor = Vendor(name=name, kind=kind, domains=domain, confirmed=False,
                    notes=(notes or "")[:300] or None)
    session.add(vendor)
    session.flush()
    return {"outcome": "created", "vendor": vendor.name, "kind": kind,
            "note": "unconfirmed until Arda confirms it in Settings"}


def vendor_groups() -> dict:
    """Vendors for the Settings box: confirmed by kind, unconfirmed last."""
    with db_session() as s:
        rows = s.scalars(select(Vendor).order_by(Vendor.name)).all()
        return {
            "green": [_row(v) for v in rows if v.confirmed and v.kind == "green_importer"],
            "other": [_row(v) for v in rows if v.confirmed and v.kind != "green_importer"],
            "unconfirmed": [_row(v) for v in rows if not v.confirmed],
        }


def _row(v: Vendor) -> dict:
    return {"id": v.id, "name": v.name, "kind": v.kind, "domains": v.domains,
            "confirmed": v.confirmed, "notes": v.notes or ""}


def add_vendor(name: str, domains: str, kind: str) -> str:
    name = (name or "").strip()[:200]
    domains = ",".join(
        d.strip().lower().lstrip("@") for d in (domains or "").split(",") if d.strip()
    )[:500]
    if not name or not domains:
        return "A vendor needs a name and at least one email domain."
    if kind not in VENDOR_KINDS:
        kind = "other"
    with db_session() as s:
        first = domains.split(",")[0]
        existing = classify_email(s, f"x@{first}")
        if existing is not None:
            return f"{first} already belongs to {existing.name}."
        s.add(Vendor(name=name, kind=kind, domains=domains, confirmed=True))
    return f"Added {name} ({KIND_LABELS[kind]})."


def set_vendor_kind(vendor_id: int, kind: str) -> str:
    """Confirm and/or reclassify — either way the row becomes Arda-confirmed."""
    if kind not in VENDOR_KINDS:
        return "Unknown vendor kind."
    with db_session() as s:
        vendor = s.get(Vendor, vendor_id)
        if vendor is None:
            return "Vendor not found."
        vendor.kind = kind
        vendor.confirmed = True
        name = vendor.name
    return f"{name} → {KIND_LABELS[kind]}."


def remove_vendor(vendor_id: int) -> str:
    with db_session() as s:
        vendor = s.get(Vendor, vendor_id)
        if vendor is None:
            return "Vendor not found."
        name = vendor.name
        s.delete(vendor)
    return f"Removed {name}. Routines may re-suggest it if their mail keeps arriving."
