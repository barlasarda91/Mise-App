"""Gmail tool layer: read both mailboxes, create/update drafts, and send a
draft the OPERATOR explicitly approved.

INVARIANT (spec §5, amended 2026-09-03): the model-facing tool layer (the
engine toolkit registry) must never expose a send capability — routines can
only draft. send_draft below exists solely for the Drafts UI's Send button,
which Arda clicks after reviewing; never wire it into a model tool.
"""

import base64
import re
from datetime import datetime
from email.message import EmailMessage
from email.parser import BytesParser
from email import policy

from app.models.enums import FromMailbox
from app.settings import get_settings
from app.tools.google_client import gmail_service

METADATA_HEADERS = ["From", "To", "Cc", "Subject", "Date", "Message-ID"]


def mailbox_address(mailbox: FromMailbox) -> str:
    settings = get_settings()
    return {
        FromMailbox.ARDA: settings.gmail_arda_address,
        FromMailbox.HELLO: settings.gmail_hello_address,
    }[mailbox]


# ---------- pure helpers (unit-tested without credentials) ----------


def gmail_query(base: str, after: datetime | None = None) -> str:
    """Gmail search query with an incremental lower bound (epoch seconds)."""
    if after is None:
        return base
    return f"{base} after:{int(after.timestamp())}"


def build_mime(
    from_addr: str,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> str:
    """RFC 2822 message, base64url-encoded for the Gmail API `raw` field."""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def decode_mime(raw: str) -> EmailMessage:
    return BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))


def _b64part(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode()).decode(errors="replace")


def extract_body(payload: dict) -> str:
    """Best-effort plain text from a Gmail API message payload: prefer
    text/plain parts, fall back to tag-stripped text/html."""
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data:
            if mime == "text/plain":
                plain.append(_b64part(data))
            elif mime == "text/html":
                html.append(_b64part(data))
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    if plain:
        return "\n".join(plain).strip()
    if html:
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", "\n".join(html), flags=re.S | re.I)
        text = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"[ \t]+", " ", text).strip()
    return ""


def _headers_dict(message: dict) -> dict[str, str]:
    return {
        h["name"].lower(): h["value"]
        for h in (message.get("payload") or {}).get("headers") or []
    }


def _summarize(message: dict) -> dict:
    headers = _headers_dict(message)
    return {
        "id": message["id"],
        "thread_id": message.get("threadId"),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "message_id_header": headers.get("message-id", ""),
        "snippet": message.get("snippet", ""),
    }


# ---------- API operations ----------


def search_messages(
    mailbox: FromMailbox,
    query: str,
    after: datetime | None = None,
    max_results: int = 50,
) -> list[dict]:
    """Search a mailbox (e.g. 'in:sent', 'in:inbox') since `after`; returns
    header summaries, newest first."""
    svc = gmail_service(mailbox_address(mailbox))
    listing = (
        svc.users()
        .messages()
        .list(userId="me", q=gmail_query(query, after), maxResults=max_results)
        .execute()
    )
    out = []
    for ref in listing.get("messages", []):
        message = (
            svc.users()
            .messages()
            .get(userId="me", id=ref["id"], format="metadata", metadataHeaders=METADATA_HEADERS)
            .execute()
        )
        out.append(_summarize(message))
    return out


def get_message(mailbox: FromMailbox, msg_id: str) -> dict:
    """Full message: header summary + extracted plain-text body."""
    svc = gmail_service(mailbox_address(mailbox))
    message = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
    summary = _summarize(message)
    summary["body"] = extract_body(message.get("payload") or {})
    return summary


def send_draft(mailbox: FromMailbox, draft_id: str) -> dict:
    """Send an existing Gmail draft. OPERATOR-ONLY: called from the Drafts
    UI's Send button after Arda's review — never from a model tool."""
    svc = gmail_service(mailbox_address(mailbox))
    message = svc.users().drafts().send(userId="me", body={"id": draft_id}).execute()
    return {"message_id": message["id"], "thread_id": message.get("threadId")}


def get_thread_messages(mailbox: FromMailbox, thread_id: str, last_n: int = 3) -> list[dict]:
    """Last N messages of a thread with extracted bodies — context for reply drafts."""
    svc = gmail_service(mailbox_address(mailbox))
    thread = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
    out = []
    for message in (thread.get("messages") or [])[-last_n:]:
        summary = _summarize(message)
        summary["body"] = extract_body(message.get("payload") or {})
        out.append(summary)
    return out


def _thread_reply_headers(svc, thread_id: str) -> tuple[str | None, str | None]:
    """(in_reply_to, references) from the last message on a thread."""
    thread = (
        svc.users()
        .threads()
        .get(userId="me", id=thread_id, format="metadata", metadataHeaders=["Message-ID", "References"])
        .execute()
    )
    messages = thread.get("messages") or []
    if not messages:
        return None, None
    headers = _headers_dict(messages[-1])
    return headers.get("message-id"), headers.get("references")


def create_draft(
    mailbox: FromMailbox,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    thread_id: str | None = None,
) -> dict:
    """Create a native Gmail draft in the given mailbox. With `thread_id`, the
    draft is attached to that conversation with proper reply headers."""
    address = mailbox_address(mailbox)
    svc = gmail_service(address)
    in_reply_to = references = None
    if thread_id:
        in_reply_to, references = _thread_reply_headers(svc, thread_id)
    message: dict = {
        "raw": build_mime(address, to, subject, body, cc, in_reply_to, references)
    }
    if thread_id:
        message["threadId"] = thread_id
    draft = svc.users().drafts().create(userId="me", body={"message": message}).execute()
    return {
        "draft_id": draft["id"],
        "message_id": draft["message"]["id"],
        "thread_id": draft["message"].get("threadId"),
    }


def update_draft(
    mailbox: FromMailbox,
    draft_id: str,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    thread_id: str | None = None,
) -> dict:
    address = mailbox_address(mailbox)
    svc = gmail_service(address)
    in_reply_to = references = None
    if thread_id:
        in_reply_to, references = _thread_reply_headers(svc, thread_id)
    message: dict = {
        "raw": build_mime(address, to, subject, body, cc, in_reply_to, references)
    }
    if thread_id:
        message["threadId"] = thread_id
    draft = (
        svc.users()
        .drafts()
        .update(userId="me", id=draft_id, body={"message": message})
        .execute()
    )
    return {
        "draft_id": draft["id"],
        "message_id": draft["message"]["id"],
        "thread_id": draft["message"].get("threadId"),
    }
