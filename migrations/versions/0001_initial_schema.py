"""Initial schema — spec §6 with the 2026-08-27 review resolutions.

Revision ID: 0001
Revises:
Create Date: 2026-08-27
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# JSONB on Postgres, plain JSON elsewhere (keeps the migration testable on SQLite).
JSON_ = sa.JSON().with_variant(JSONB(), "postgresql")


def enum_(*values: str) -> sa.Enum:
    # VARCHAR + CHECK, not native PG enums (matches app.models.base.db_enum).
    return sa.Enum(*values, native_enum=False, length=32)


TS = sa.DateTime(timezone=True)
NOW = sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    op.create_table(
        "routines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("schedule_cron", sa.String(100), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("connectors", JSON_, nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(100), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(200), nullable=False),
    )

    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
    )

    op.create_table(
        "leads",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("business_name", sa.String(200), nullable=False),
        sa.Column("contact_name", sa.String(200), nullable=True),
        sa.Column("contact_email", sa.String(320), nullable=True),
        sa.Column("contact_phone", sa.String(50), nullable=True),
        sa.Column("location", sa.String(200), nullable=True),
        sa.Column("lead_source", sa.String(100), nullable=True),
        sa.Column("format", enum_("espresso", "filter", "both"), nullable=True),
        sa.Column("coffee_program", JSON_, nullable=True),
        sa.Column("projected_weekly_consumption", sa.Numeric(8, 2), nullable=True),
        sa.Column("projected_unit", enum_("lb", "kg"), nullable=True),
        sa.Column(
            "stage",
            enum_("new", "contacted", "sampled", "negotiating", "closed_won", "closed_lost"),
            nullable=False,
        ),
        sa.Column("stage_since", sa.Date(), nullable=True),
        sa.Column("last_confirmed_action", sa.Date(), nullable=True),
        sa.Column("pending_confirmation", JSON_, nullable=True),
        sa.Column("loss_reason", sa.Text(), nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
    )
    op.create_index("ix_leads_contact_email", "leads", ["contact_email"])
    op.create_index("ix_leads_stage", "leads", ["stage"])

    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("routine_id", sa.Integer(), sa.ForeignKey("routines.id"), nullable=False),
        sa.Column("status", enum_("running", "completed", "failed"), nullable=False),
        sa.Column("trigger", enum_("scheduled", "manual"), nullable=False),
        sa.Column("started_at", TS, nullable=False, server_default=NOW),
        sa.Column("completed_at", TS, nullable=True),
        sa.Column("last_run_at_snapshot", TS, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_runs_routine_started", "runs", ["routine_id", "started_at"])

    op.create_table(
        "run_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", enum_("user", "assistant", "tool"), nullable=False),
        sa.Column("content", JSON_, nullable=False),
        sa.Column("tool_calls", JSON_, nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
    )
    op.create_index("ix_run_messages_run_id", "run_messages", ["run_id"])

    op.create_table(
        "sync_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", enum_("gmail_arda", "gmail_hello", "calendar"), nullable=False),
        sa.Column("routine_id", sa.Integer(), sa.ForeignKey("routines.id"), nullable=False),
        sa.Column("last_run_at", TS, nullable=True),
        sa.Column("cursor", JSON_, nullable=True),
        sa.UniqueConstraint("source", "routine_id", name="uq_sync_source_routine"),
    )

    op.create_table(
        "lead_activity",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "lead_id",
            sa.Integer(),
            sa.ForeignKey("leads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "type",
            enum_("email_sent", "call", "text", "visit", "note", "stage_change"),
            nullable=False,
        ),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("source", enum_("gmail", "manual"), nullable=False),
        sa.Column("gmail_msg_id", sa.String(128), nullable=True),
        sa.Column(
            "run_id", sa.Integer(), sa.ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
    )
    op.create_index("ix_lead_activity_lead_id", "lead_activity", ["lead_id"])
    op.create_index(
        "uq_lead_activity_gmail_msg_id",
        "lead_activity",
        ["gmail_msg_id"],
        unique=True,
        postgresql_where=sa.text("gmail_msg_id IS NOT NULL"),
        sqlite_where=sa.text("gmail_msg_id IS NOT NULL"),
    )

    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "category",
            enum_("wholesale_leads", "consultation", "pop_ups", "invoice_tracking", "governance"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", enum_("todo", "doing", "waiting", "done"), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("priority", enum_("low", "normal", "high"), nullable=False),
        sa.Column("assignee", sa.String(100), nullable=True),
        sa.Column(
            "source",
            enum_("manual", "routine", "email", "calendar", "quickbooks"),
            nullable=False,
        ),
        sa.Column("source_ref", JSON_, nullable=True),
        sa.Column("waiting_on", sa.String(300), nullable=True),
        sa.Column("completed_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
    )
    op.create_index("ix_tasks_category_status", "tasks", ["category", "status"])

    op.create_table(
        "task_activity",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id",
            sa.Integer(),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(100), nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
    )
    op.create_index("ix_task_activity_task_id", "task_activity", ["task_id"])

    op.create_table(
        "email_drafts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject", sa.String(500), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("to_addrs", JSON_, nullable=True),
        sa.Column("cc_addrs", JSON_, nullable=True),
        sa.Column("from_mailbox", enum_("arda", "hello"), nullable=False),
        sa.Column("gmail_thread_id", sa.String(128), nullable=True),
        sa.Column(
            "related_lead_id",
            sa.Integer(),
            sa.ForeignKey("leads.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "related_task_id",
            sa.Integer(),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("gmail_draft_id", sa.String(128), nullable=True),
        sa.Column(
            "status",
            enum_("drafting", "composed", "saved_to_gmail", "discarded"),
            nullable=False,
        ),
        sa.Column(
            "run_id", sa.Integer(), sa.ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
    )

    op.create_table(
        "external_mutations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", enum_("calendar_event", "gmail_draft", "task"), nullable=False),
        sa.Column("dedup_key", sa.String(300), nullable=False, unique=True),
        sa.Column("external_id", sa.String(200), nullable=True),
        sa.Column(
            "run_id", sa.Integer(), sa.ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
    )


def downgrade() -> None:
    for table in (
        "external_mutations",
        "email_drafts",
        "task_activity",
        "tasks",
        "lead_activity",
        "sync_state",
        "run_messages",
        "runs",
        "leads",
        "projects",
        "users",
        "routines",
    ):
        op.drop_table(table)
