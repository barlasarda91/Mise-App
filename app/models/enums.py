"""Domain enums (spec §6, incl. the 2026-08-27 review resolutions)."""

import enum


class RunStatus(str, enum.Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class RunTrigger(str, enum.Enum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"


class MessageRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class SyncSource(str, enum.Enum):
    GMAIL_ARDA = "gmail_arda"
    GMAIL_HELLO = "gmail_hello"
    CALENDAR = "calendar"


class LeadStage(str, enum.Enum):
    NEW = "new"
    CONTACTED = "contacted"
    SAMPLED = "sampled"
    NEGOTIATING = "negotiating"
    CLOSED_WON = "closed_won"
    CLOSED_LOST = "closed_lost"


OPEN_LEAD_STAGES = (
    LeadStage.NEW,
    LeadStage.CONTACTED,
    LeadStage.SAMPLED,
    LeadStage.NEGOTIATING,
)


class LeadFormat(str, enum.Enum):
    ESPRESSO = "espresso"
    FILTER = "filter"
    BOTH = "both"


class CoffeeProgram(str, enum.Enum):
    """Values stored in leads.coffee_program (JSON list — multi-select)."""

    BLEND_ENTRY = "blend_entry"
    ROTATING_SINGLE_FARM = "rotating_single_farm"
    COMPETITION = "competition"


class ProjectedUnit(str, enum.Enum):
    LB = "lb"
    KG = "kg"


class LeadActivityType(str, enum.Enum):
    EMAIL_SENT = "email_sent"
    CALL = "call"
    TEXT = "text"
    VISIT = "visit"
    NOTE = "note"
    STAGE_CHANGE = "stage_change"


class ActivitySource(str, enum.Enum):
    GMAIL = "gmail"
    MANUAL = "manual"


class TaskCategory(str, enum.Enum):
    WHOLESALE_LEADS = "wholesale_leads"
    CONSULTATION = "consultation"
    POP_UPS = "pop_ups"
    INVOICE_TRACKING = "invoice_tracking"
    GOVERNANCE = "governance"


class TaskStatus(str, enum.Enum):
    """Fixed board columns for v1 (review resolution)."""

    TODO = "todo"
    DOING = "doing"
    WAITING = "waiting"
    DONE = "done"


class TaskPriority(str, enum.Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class TaskSource(str, enum.Enum):
    MANUAL = "manual"
    ROUTINE = "routine"
    EMAIL = "email"
    CALENDAR = "calendar"
    QUICKBOOKS = "quickbooks"


class FromMailbox(str, enum.Enum):
    ARDA = "arda"  # ardabarlas@boxxcoffee.com (canonical)
    HELLO = "hello"  # hello@boxxcoffee.com


class DraftStatus(str, enum.Enum):
    DRAFTING = "drafting"
    COMPOSED = "composed"
    SAVED_TO_GMAIL = "saved_to_gmail"
    DISCARDED = "discarded"


class MutationKind(str, enum.Enum):
    CALENDAR_EVENT = "calendar_event"
    GMAIL_DRAFT = "gmail_draft"
    TASK = "task"
