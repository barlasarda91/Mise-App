"""Seed the two standing routines (spec §7) if they don't exist.

Seeded DISABLED with placeholder prompts — milestones 7 and 8 install the real
system prompts and flip them on. This keeps the scheduler from burning API
calls on stubs while making Settings and "Run now" real from milestone 5.
"""

from sqlalchemy import select

from app.db import db_session
from app.models import Routine

PLACEHOLDER_NOTE = (
    "You are a placeholder routine for Mise (Boxx Coffee Roasters' ops hub). "
    "Your real system prompt has not been installed yet. Briefly confirm the "
    "runtime context you received (date, open leads, tasks) using your tools, "
    "then state that this routine's full prompt lands in a later milestone."
)

ROUTINE_DEFAULTS = [
    {
        "key": "lead_tracker",
        "name": "Wholesale Lead Tracker",
        "schedule_cron": "30 8 * * *",  # daily 08:30 LA (spec §7.1)
        "connectors": ["gmail_arda", "gmail_hello", "calendar"],
    },
    {
        "key": "daily_agenda",
        "name": "Daily Agenda",
        "schedule_cron": "30 7 * * *",  # daily 07:30 LA (spec §7.2)
        "connectors": ["gmail_arda", "calendar", "quickbooks"],
    },
]


def seed_routines(session_factory=db_session) -> int:
    created = 0
    with session_factory() as s:
        existing = set(s.scalars(select(Routine.key)).all())
        for spec in ROUTINE_DEFAULTS:
            if spec["key"] in existing:
                continue
            s.add(
                Routine(
                    key=spec["key"],
                    name=spec["name"],
                    system_prompt=PLACEHOLDER_NOTE,
                    schedule_cron=spec["schedule_cron"],
                    timezone="America/Los_Angeles",
                    model="opus",
                    enabled=False,
                    connectors=spec["connectors"],
                )
            )
            created += 1
    return created
