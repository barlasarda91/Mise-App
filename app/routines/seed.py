"""Seed the two standing routines (spec §7) and keep their system prompts in
sync with the versioned prompt files.

Prompts live in app/routines/prompts/<key>.md; on startup a routine whose
stored prompt differs from its file is updated (the file is the source of
truth until an in-app prompt editor exists). Routines without a prompt file
keep a placeholder. `enabled` is never touched here — that's Arda's switch.
"""

from pathlib import Path

from sqlalchemy import select

from app.db import db_session
from app.models import Routine

PROMPTS_DIR = Path(__file__).parent / "prompts"

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


def prompt_for(key: str) -> str:
    path = PROMPTS_DIR / f"{key}.md"
    return path.read_text() if path.exists() else PLACEHOLDER_NOTE


def seed_routines(session_factory=db_session) -> int:
    """Create missing routines; sync existing prompts from files. Returns the
    number of routines created."""
    created = 0
    with session_factory() as s:
        existing = {r.key: r for r in s.scalars(select(Routine)).all()}
        for spec in ROUTINE_DEFAULTS:
            prompt = prompt_for(spec["key"])
            routine = existing.get(spec["key"])
            if routine is None:
                s.add(
                    Routine(
                        key=spec["key"],
                        name=spec["name"],
                        system_prompt=prompt,
                        schedule_cron=spec["schedule_cron"],
                        timezone="America/Los_Angeles",
                        model="opus",
                        enabled=False,
                        connectors=spec["connectors"],
                    )
                )
                created += 1
            elif routine.system_prompt != prompt:
                routine.system_prompt = prompt
    return created
