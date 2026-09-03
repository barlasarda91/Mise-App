"""Follow-up cadence rules (spec §7.1) — pure functions shared by the lead
tracker tools and the Pipeline UI.

New — alert immediately, every day until Contacted
Contacted — every 3 days
Sampled — at 3 days, then 7, then every 5 after
Negotiating — every 7 days

Timers anchor to last_confirmed_action (falling back to stage_since, then
created date); a confirmed action restarts the sequence.
"""

from datetime import date

from app.models import Lead, LeadStage

FIRST_ALERT_DAYS = {
    LeadStage.NEW: 1,
    LeadStage.CONTACTED: 3,
    LeadStage.SAMPLED: 3,
    LeadStage.NEGOTIATING: 7,
}


def reference_date(lead: Lead) -> date | None:
    if lead.last_confirmed_action:
        return lead.last_confirmed_action
    if lead.stage_since:
        return lead.stage_since
    if lead.created_at is not None:
        return lead.created_at.date()
    return None


def idle_days(lead: Lead, today: date) -> int | None:
    ref = reference_date(lead)
    return (today - ref).days if ref else None


def is_overdue(lead: Lead, today: date) -> bool:
    threshold = FIRST_ALERT_DAYS.get(lead.stage)
    if threshold is None:  # closed stages never alert
        return False
    idle = idle_days(lead, today)
    return idle is not None and idle >= threshold


def overdue_by(lead: Lead, today: date) -> int:
    """Days past the stage's first alert threshold (0 when not overdue)."""
    if not is_overdue(lead, today):
        return 0
    return idle_days(lead, today) - FIRST_ALERT_DAYS[lead.stage] + 1
