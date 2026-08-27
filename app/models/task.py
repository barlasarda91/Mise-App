"""Board: projects, tasks (five standing categories), task activity log."""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, PortableJSON, TimestampMixin, db_enum
from app.models.enums import TaskCategory, TaskPriority, TaskSource, TaskStatus


class Project(TimestampMixin, Base):
    """Optional finer grouping within a category, for later."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="active")

    tasks: Mapped[list["Task"]] = relationship(back_populates="project")


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_category_status", "category", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[TaskCategory] = mapped_column(db_enum(TaskCategory))
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[TaskStatus] = mapped_column(db_enum(TaskStatus), default=TaskStatus.TODO)
    due_date: Mapped[date | None] = mapped_column(Date)
    priority: Mapped[TaskPriority] = mapped_column(
        db_enum(TaskPriority), default=TaskPriority.NORMAL
    )
    assignee: Mapped[str | None] = mapped_column(String(100))
    source: Mapped[TaskSource] = mapped_column(db_enum(TaskSource), default=TaskSource.MANUAL)
    # {lead_id | gmail_msg_id | event_id | qbo_invoice_id | run_id, ...}
    source_ref: Mapped[dict | None] = mapped_column(PortableJSON)
    waiting_on: Mapped[str | None] = mapped_column(String(300))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project: Mapped[Project | None] = relationship(back_populates="tasks")
    activities: Mapped[list["TaskActivity"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskActivity.id",
    )


class TaskActivity(Base):
    __tablename__ = "task_activity"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(50))
    detail: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    task: Mapped[Task] = relationship(back_populates="activities")
