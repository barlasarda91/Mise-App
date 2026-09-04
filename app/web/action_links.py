"""Spot actionable links buried in email bodies — e-sign requests (Adobe Sign,
DocuSign, …) and hosted invoice/payment pages — so detail views can surface
them as a card instead of leaving them mid-thread."""

import re
from urllib.parse import urlparse

URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+")

# (host, path prefix, label) — host matches exactly or as a subdomain.
RULES = [
    ("documents.adobe.com", "", "Sign document · Adobe Sign"),
    ("adobesign.com", "", "Sign document · Adobe Sign"),
    ("echosign.com", "", "Sign document · Adobe Sign"),
    ("docusign.net", "", "Sign document · DocuSign"),
    ("docusign.com", "", "Sign document · DocuSign"),
    ("hellosign.com", "", "Sign document · Dropbox Sign"),
    ("sign.dropbox.com", "", "Sign document · Dropbox Sign"),
    ("pandadoc.com", "", "Sign document · PandaDoc"),
    ("connect.intuit.com", "", "View / pay invoice · QuickBooks"),
    ("pay.stripe.com", "", "Pay invoice · Stripe"),
    ("invoice.stripe.com", "", "Pay invoice · Stripe"),
    ("paypal.com", "/invoice", "Pay invoice · PayPal"),
    ("square.link", "", "Pay · Square"),
    ("squareup.com", "/pay", "Pay · Square"),
    ("bill.com", "", "View invoice · Bill.com"),
]

MAX_LINKS = 6


def _label_for(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    for rule_host, prefix, label in RULES:
        if (host == rule_host or host.endswith("." + rule_host)) and (
            not prefix or (parsed.path or "").startswith(prefix)
        ):
            return label
    return None


def extract_action_links(messages: list[dict]) -> list[dict]:
    """Actionable links across a conversation, newest mention first, deduped
    by URL. Each message dict needs body/from/date (full body, untruncated)."""
    seen: set[str] = set()
    found: list[dict] = []
    for message in reversed(messages):
        for url in URL_RE.findall(message.get("body") or ""):
            url = url.rstrip(".,;:!?")
            label = _label_for(url)
            if label is None or url in seen:
                continue
            seen.add(url)
            found.append(
                {
                    "label": label,
                    "url": url,
                    "from": message.get("from", ""),
                    "date": message.get("date", ""),
                }
            )
            if len(found) >= MAX_LINKS:
                return found
    return found
