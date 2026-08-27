from datetime import date

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ActivitySource,
    Base,
    DraftStatus,
    EmailDraft,
    ExternalMutation,
    FromMailbox,
    Lead,
    LeadActivity,
    LeadActivityType,
    LeadStage,
    MessageRole,
    MutationKind,
    Routine,
    Run,
    RunMessage,
    RunStatus,
    SyncSource,
    SyncState,
    Task,
    TaskCategory,
    TaskStatus,
)


@pytest.fixture
def engine():
    engine = create_engine("sqlite://")
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session(engine):
    with Session(engine) as session:
        yield session


def test_routine_run_transcript_roundtrip(session):
    routine = Routine(key="lead_tracker", name="Wholesale Lead Tracker", system_prompt="...")
    run = Run(routine=routine)
    run.messages = [
        RunMessage(role=MessageRole.ASSISTANT, content={"text": "audit"}, tool_calls=[{"a": 1}]),
        RunMessage(role=MessageRole.TOOL, content={"result": "ok"}),
    ]
    session.add(routine)
    session.commit()

    loaded = session.get(Run, run.id)
    assert loaded.status == RunStatus.RUNNING
    assert loaded.routine.key == "lead_tracker"
    assert [m.role for m in loaded.messages] == [MessageRole.ASSISTANT, MessageRole.TOOL]

    session.delete(loaded)
    session.commit()
    assert session.query(RunMessage).count() == 0  # transcript goes with the run


def test_lead_activity_and_defaults(session):
    lead = Lead(business_name="Ember Room", contact_email="deniz@emberroom.la")
    lead.activities.append(
        LeadActivity(
            type=LeadActivityType.CALL,
            occurred_on=date(2026, 8, 26),
            detail="5 lb/wk Blend No:1",
        )
    )
    session.add(lead)
    session.commit()

    assert lead.stage == LeadStage.NEW
    assert lead.activities[0].source == ActivitySource.MANUAL


def test_gmail_msg_id_idempotency(session):
    lead = Lead(business_name="Kettle & Stone")
    session.add(lead)
    session.commit()

    def activity(msg_id):
        return LeadActivity(
            lead_id=lead.id,
            type=LeadActivityType.EMAIL_SENT,
            occurred_on=date(2026, 8, 22),
            source=ActivitySource.GMAIL,
            gmail_msg_id=msg_id,
        )

    session.add(activity("msg-123"))
    session.commit()

    session.add(activity("msg-123"))  # same message re-found in an overlapping window
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    # NULL msg ids (manual entries) are exempt from uniqueness.
    session.add_all([activity(None), activity(None)])
    session.commit()


def test_dedup_ledger_unique_key(session):
    session.add(ExternalMutation(kind=MutationKind.CALENDAR_EVENT, dedup_key="lead:1:2026-08-28:followup"))
    session.commit()
    session.add(ExternalMutation(kind=MutationKind.CALENDAR_EVENT, dedup_key="lead:1:2026-08-28:followup"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_sync_state_unique_per_source_routine(session):
    routine = Routine(key="agenda", name="Daily Agenda", system_prompt="...")
    session.add(routine)
    session.commit()

    session.add(SyncState(source=SyncSource.GMAIL_ARDA, routine_id=routine.id))
    session.commit()
    session.add(SyncState(source=SyncSource.GMAIL_ARDA, routine_id=routine.id))
    with pytest.raises(IntegrityError):
        session.commit()


def test_task_and_draft_defaults(session):
    task = Task(category=TaskCategory.WHOLESALE_LEADS, title="Send agreement to Ember Room")
    draft = EmailDraft(
        subject="Boxx wholesale — Blend No:1 agreement",
        from_mailbox=FromMailbox.ARDA,
        to_addrs=["deniz@emberroom.la"],
    )
    session.add_all([task, draft])
    session.commit()

    assert task.status == TaskStatus.TODO
    assert draft.status == DraftStatus.DRAFTING
    assert draft.cc_addrs is None


def test_migration_matches_models(tmp_path):
    """The hand-written 0001 migration must produce the same tables/columns
    as Base.metadata — catches drift between models and migration."""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "migrated.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "head")

    migrated = create_engine(f"sqlite:///{db_path}")
    model_engine = create_engine("sqlite://")
    Base.metadata.create_all(model_engine)

    def schema(engine, skip=("alembic_version",)):
        insp = inspect(engine)
        return {
            table: {
                col["name"]: bool(col["nullable"]) for col in insp.get_columns(table)
            }
            for table in insp.get_table_names()
            if table not in skip
        }

    assert schema(migrated) == schema(model_engine)
