"""Pipeline page: board view-model + manual-entry services (spec §4/§7.1).

Manual entries are Arda's own reports, so unlike run-discovered email they
always count as confirmed: outbound activity advances the idle timer directly.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db import db_session
from app.models import (
    ActivitySource,
    EmailDraft,
    ExternalMutation,
    Lead,
    LeadActivity,
    LeadActivityType,
    LeadStage,
    MutationKind,
    OPEN_LEAD_STAGES,
)
from app.routines.cadence import idle_days, is_overdue, overdue_by
from app.settings import get_settings

OUTBOUND_TYPES = {
    LeadActivityType.EMAIL_SENT,
    LeadActivityType.CALL,
    LeadActivityType.TEXT,
    LeadActivityType.VISIT,
}

STAGE_LABELS = [
    (LeadStage.NEW, "New"),
    (LeadStage.CONTACTED, "Contacted"),
    (LeadStage.SAMPLED, "Sampled"),
    (LeadStage.NEGOTIATING, "Negotiating"),
]


def _today() -> date:
    return datetime.now(ZoneInfo(get_settings().default_tz)).date()


def _card(lead: Lead, today: date) -> dict:
    idle = idle_days(lead, today)
    return {
        "id": lead.id,
        "name": lead.business_name,
        "contact": lead.contact_name or lead.contact_email or "",
        "source": lead.lead_source or "",
        "last": lead.last_confirmed_action,
        "idle": idle,
        "overdue": is_overdue(lead, today),
        "overdue_by": overdue_by(lead, today),
        "pending": lead.pending_confirmation,
    }


def load_board() -> tuple[list[dict], dict]:
    today = _today()
    try:
        with db_session() as s:
            leads = s.scalars(select(Lead).where(Lead.stage.in_(OPEN_LEAD_STAGES))).all()
    except Exception:
        return [], {"open": 0, "overdue": 0}
    lanes = []
    for stage, label in STAGE_LABELS:
        cards = [_card(lead, today) for lead in leads if lead.stage == stage]
        cards.sort(key=lambda c: (c["idle"] is None, -(c["idle"] or 0)))
        lanes.append({"stage": stage.value, "label": label, "cards": cards})
    stats = {"open": len(leads), "overdue": sum(1 for l in leads if is_overdue(l, today))}
    return lanes, stats


def load_lead(lead_id: int) -> dict | None:
    today = _today()
    try:
        with db_session() as s:
            lead = s.get(Lead, lead_id)
            if lead is None:
                return None
            activities = s.scalars(
                select(LeadActivity)
                .where(LeadActivity.lead_id == lead_id)
                .order_by(LeadActivity.occurred_on.desc(), LeadActivity.id.desc())
            ).all()
            return {
                **_card(lead, today),
                "stage": lead.stage.value,
                "stage_since": lead.stage_since,
                "contact_name": lead.contact_name,
                "contact_email": lead.contact_email,
                "contact_phone": lead.contact_phone,
                "location": lead.location,
                "loss_reason": lead.loss_reason,
                "closed": lead.stage not in OPEN_LEAD_STAGES,
                "drafts": [
                    {
                        "id": d.id,
                        "subject": d.subject or "(no subject)",
                        "status": "sent ✓" if d.sent_at else d.status.value,
                    }
                    for d in s.scalars(
                        select(EmailDraft)
                        .where(EmailDraft.related_lead_id == lead_id)
                        .order_by(EmailDraft.id.desc())
                        .limit(8)
                    )
                ],
                "activities": [
                    {
                        "type": a.type.value,
                        "occurred_on": a.occurred_on,
                        "detail": a.detail,
                        "source": a.source.value,
                    }
                    for a in activities
                ],
            }
    except Exception:
        return None


def load_email_context(lead: dict | None) -> dict | None:
    """Recent Gmail history with the lead's contact, checked across both
    mailboxes (inbound usually lands in hello@, outreach in arda@). Returns
    the conversation with the newest activity."""
    if not lead or not lead.get("contact_email"):
        return None
    from email.utils import parsedate_to_datetime

    from app.web.drafts_view import load_thread

    def last_stamp(ctx) -> float:
        try:
            return parsedate_to_datetime(ctx["messages"][-1]["date"]).timestamp()
        except Exception:
            return 0.0

    best = None
    error_ctx = None
    for mailbox in ("hello", "arda"):
        ctx = load_thread({"thread_id": None, "mailbox": mailbox, "to": lead["contact_email"]})
        if ctx is None:
            continue
        if ctx.get("error"):
            error_ctx = error_ctx or ctx
            continue
        if best is None or last_stamp(ctx) > last_stamp(best):
            best = ctx
    return best or error_ctx


# ---------- manual-entry services (return a status message for the UI) ----------


def create_lead_manual(business_name: str, contact_name: str, contact_email: str, contact_phone: str, lead_source: str) -> str:
    if not business_name.strip():
        return "Business name is required."
    with db_session() as s:
        s.add(
            Lead(
                business_name=business_name.strip(),
                contact_name=contact_name.strip() or None,
                contact_email=contact_email.strip() or None,
                contact_phone=contact_phone.strip() or None,
                lead_source=lead_source.strip() or "manual",
                stage=LeadStage.NEW,
                stage_since=_today(),
            )
        )
    return f"Lead added: {business_name}."


def log_activity_manual(lead_id: int, type_: str, occurred_on: str, detail: str) -> str:
    activity_type = LeadActivityType(type_)
    when = date.fromisoformat(occurred_on) if occurred_on else _today()
    with db_session() as s:
        lead = s.get(Lead, lead_id)
        if lead is None:
            return "Lead not found."
        s.add(
            LeadActivity(
                lead_id=lead_id,
                type=activity_type,
                occurred_on=when,
                detail=detail.strip() or None,
                source=ActivitySource.MANUAL,
            )
        )
        if activity_type in OUTBOUND_TYPES and (
            lead.last_confirmed_action is None or when > lead.last_confirmed_action
        ):
            lead.last_confirmed_action = when  # Arda-reported: confirmed by definition
        from app.routines.task_sync import sync_lead_tasks

        done = sync_lead_tasks(s, lead, _today())
        name = lead.business_name
    suffix = f" Auto-completed: {', '.join(done)}." if done else ""
    return f"Logged {type_} on {when} for {name}.{suffix}"


def change_stage_manual(lead_id: int, stage: str, loss_reason: str) -> str:
    new_stage = LeadStage(stage)
    with db_session() as s:
        lead = s.get(Lead, lead_id)
        if lead is None:
            return "Lead not found."
        if new_stage == lead.stage:
            return "Stage unchanged."
        if new_stage == LeadStage.CLOSED_LOST and not loss_reason.strip():
            return "Closing as lost requires a loss reason."
        s.add(
            LeadActivity(
                lead_id=lead_id,
                type=LeadActivityType.STAGE_CHANGE,
                occurred_on=_today(),
                detail=f"{lead.stage.value} → {new_stage.value}",
                source=ActivitySource.MANUAL,
            )
        )
        lead.stage = new_stage
        lead.stage_since = _today()
        if new_stage == LeadStage.CLOSED_LOST:
            lead.loss_reason = loss_reason.strip()
        from app.routines.task_sync import sync_lead_tasks

        done = sync_lead_tasks(s, lead, _today())
    suffix = f" Auto-completed: {', '.join(done)}." if done else ""
    return f"Stage → {new_stage.value}.{suffix}"


def resolve_pending(lead_id: int, action: str) -> str:
    with db_session() as s:
        lead = s.get(Lead, lead_id)
        if lead is None or not lead.pending_confirmation:
            return "Nothing pending."
        pending = lead.pending_confirmation
        if action == "confirm":
            when = date.fromisoformat(pending["occurred_on"])
            s.add(
                LeadActivity(
                    lead_id=lead_id,
                    type=LeadActivityType.EMAIL_SENT,
                    occurred_on=when,
                    detail=pending.get("detail"),
                    source=ActivitySource.GMAIL,
                    gmail_msg_id=pending.get("gmail_msg_id"),
                    run_id=pending.get("found_by_run_id"),
                )
            )
            if lead.last_confirmed_action is None or when > lead.last_confirmed_action:
                lead.last_confirmed_action = when
            from app.routines.task_sync import sync_lead_tasks

            done = sync_lead_tasks(s, lead, _today())
            suffix = f" Auto-completed: {', '.join(done)}." if done else ""
            message = f"Confirmed — timer reset to {when}.{suffix}"
        else:
            message = "Dismissed."
        lead.pending_confirmation = None
    return message


def create_reminder_manual(lead_id: int, remind_on: str, note: str) -> str:
    when = date.fromisoformat(remind_on)
    dedup_key = f"lead:{lead_id}:{when}:reminder"
    with db_session() as s:
        lead = s.get(Lead, lead_id)
        if lead is None:
            return "Lead not found."
        if s.scalar(select(ExternalMutation).where(ExternalMutation.dedup_key == dedup_key)):
            return f"A reminder for {when} already exists."
        name, email, phone = lead.business_name, lead.contact_email, lead.contact_phone
    try:
        from app.tools.calendar import create_reminder

        created = create_reminder(
            when,
            f"Follow up {name} (Boxx wholesale)",
            f"{name} · {email or ''} {phone or ''}\n{note}".strip(),
        )
    except Exception as exc:
        return f"Calendar error: {type(exc).__name__}: {exc}"
    with db_session() as s:
        s.add(
            ExternalMutation(
                kind=MutationKind.CALENDAR_EVENT,
                dedup_key=dedup_key,
                external_id=created["event_id"],
            )
        )
    return f"Reminder on {when} at {created['start'][11:16]} — on the calendar."
