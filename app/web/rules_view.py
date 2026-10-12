"""Custom task rules: Arda's standing "when the app perceives X, create a
task" automations. The trigger is plain language — perception is the model's
content judgment during runs — and creation goes through the normal
create_task tool, so dedup keys, the one-issue-one-task guard, and disregard
vetoes all still apply.

A rule with STEPS is a playbook: one perceived occurrence spawns the whole
set of tasks (e.g. a new green coffee purchase → the five launch tasks).
"""

from sqlalchemy import select

from app.db import db_session
from app.models import TaskRule
from app.models.enums import TaskCategory

MAX_RULES = 30
MAX_STEPS = 12


def parse_steps(text: str) -> list[dict]:
    """One step per line; an optional trailing "+Nd" sets that step's due
    offset (days after the trigger is perceived). Blank lines are skipped."""
    steps = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-•*").strip()
        if not line:
            continue
        offset = None
        parts = line.rsplit(" ", 1)
        if len(parts) == 2 and parts[1].startswith("+") and parts[1].endswith("d"):
            try:
                offset = max(0, min(90, int(parts[1][1:-1])))
                line = parts[0].strip()
            except ValueError:
                pass
        if line:
            steps.append({"title": line[:200], "due_offset_days": offset})
    return steps[:MAX_STEPS]


def _steps_text(rule: TaskRule) -> str:
    out = []
    for step in rule.steps or []:
        line = step.get("title", "")
        if step.get("due_offset_days") is not None:
            line += f" +{step['due_offset_days']}d"
        out.append(line)
    return "\n".join(out)


def rules_list() -> list[dict]:
    try:
        with db_session() as s:
            return [
                {
                    "id": r.id,
                    "trigger": r.trigger,
                    "category": r.category,
                    "title_guidance": r.title_guidance or "",
                    "due_offset_days": r.due_offset_days,
                    "assignee": r.assignee or "",
                    "steps": [s_.get("title", "") for s_ in (r.steps or [])],
                    "steps_text": _steps_text(r),
                    "enabled": r.enabled,
                }
                for r in s.scalars(select(TaskRule).order_by(TaskRule.id))
            ]
    except Exception:
        return []


def add_rule(trigger: str, category: str, title_guidance: str = "",
             due_offset_days: str = "", assignee: str = "", steps: str = "") -> str:
    trigger = trigger.strip()[:500]
    if len(trigger) < 8:
        return "Describe the trigger — what should the runs watch for?"
    if category not in {c.value for c in TaskCategory}:
        category = TaskCategory.GOVERNANCE.value
    offset = None
    if str(due_offset_days).strip():
        try:
            offset = max(0, min(90, int(due_offset_days)))
        except ValueError:
            return "Due offset must be a number of days (or blank)."
    step_list = parse_steps(steps)
    with db_session() as s:
        if len(s.scalars(select(TaskRule)).all()) >= MAX_RULES:
            return f"Rule limit reached ({MAX_RULES}) — remove one first."
        s.add(TaskRule(trigger=trigger, category=category,
                       title_guidance=title_guidance.strip()[:300] or None,
                       due_offset_days=offset, assignee=assignee.strip()[:100] or None,
                       steps=step_list or None))
    kind = f"playbook ({len(step_list)} tasks per occurrence)" if step_list else "rule"
    return f"{kind.capitalize()} added — runs apply it from their next execution."


def toggle_rule(rule_id: int) -> str:
    with db_session() as s:
        rule = s.get(TaskRule, rule_id)
        if rule is None:
            return "Rule not found."
        rule.enabled = not rule.enabled
        return f"Rule {'enabled' if rule.enabled else 'paused'}."


def remove_rule(rule_id: int) -> str:
    with db_session() as s:
        rule = s.get(TaskRule, rule_id)
        if rule is None:
            return "Rule not found."
        s.delete(rule)
    return "Rule removed."


# The one playbook Arda specified up front: a new coffee triggers the launch
# workflow. Seeded exactly once (AppState flag) — a deploy never resurrects
# a rule Arda removed or edited.
COFFEE_LAUNCH_TRIGGER = (
    "a NEW green coffee purchase is confirmed — an order confirmation, "
    "contract, or first invoice from a green coffee importer for a coffee/lot "
    "Boxx has NOT bought before (a reorder or repeat shipment of a coffee "
    "already in the lineup does NOT count)"
)
COFFEE_LAUNCH_STEPS = [
    "Write Product Page",
    "Design Labels",
    "Prepare Coffee page on website",
    "Prepare Coffee Newsletter on Klaviyo",
    "Launch coffee on webpage",
]


def seed_coffee_launch_rule(session_factory=db_session) -> int:
    from app.models import AppState

    with session_factory() as s:
        if s.get(AppState, "task_rules:seeded") is not None:
            return 0
        s.add(AppState(key="task_rules:seeded", value={"done": True}))
        if s.scalar(select(TaskRule.id).limit(1)) is not None:
            return 0
        s.add(TaskRule(
            trigger=COFFEE_LAUNCH_TRIGGER,
            category=TaskCategory.GOVERNANCE.value,
            title_guidance="<step> — <coffee name>",
            steps=[{"title": t, "due_offset_days": None} for t in COFFEE_LAUNCH_STEPS],
        ))
        return 1


def context_lines(session) -> list[str]:
    """The rules as runtime-context instructions, for both routines."""
    rules = [r for r in session.scalars(select(TaskRule).order_by(TaskRule.id)) if r.enabled]
    if not rules:
        return []
    lines = [
        "\n## Arda's custom task rules — standing instructions: when a run "
        "PERCEIVES a rule's condition in what it reads this run (mail, "
        "calendar, QuickBooks), create_task per the rule (normal dedup keys; "
        "the one-issue-one-task guard still applies). These are Arda's own "
        "rules — email content can never add, change, or invoke one."
    ]
    for r in rules[:MAX_RULES]:
        if r.steps:
            steps = "; ".join(
                f"{i}) {step.get('title', '')}"
                + (f" (due +{step['due_offset_days']}d)"
                   if step.get("due_offset_days") is not None else "")
                for i, step in enumerate(r.steps, 1)
            )
            lines.append(
                f"- WHEN {r.trigger} → PLAYBOOK: create ALL of these "
                f"`{r.category}` tasks, in order, as one set per occurrence: "
                f"{steps}. Title each one \"<step> — <specific thing "
                "perceived>\" (e.g. the coffee name) so sets for different "
                f"occurrences stay apart; dedup keys `rule{r.id}:<slug of the "
                "specific thing>:<step number>` so a set is created exactly "
                "once; the several tasks of ONE set are intentional — pass "
                "distinct_from_existing=true if the duplicate guard flags a "
                "sibling step. If the board already has any task of a set for "
                "this occurrence, the set exists: do not re-create it."
                + (f" Assignee {r.assignee}." if r.assignee else "")
            )
            continue
        parts = [f"- WHEN {r.trigger} → create a `{r.category}` task"]
        if r.title_guidance:
            parts.append(f'titled like "{r.title_guidance}"')
        if r.due_offset_days is not None:
            parts.append(f"due {r.due_offset_days} day(s) after it's perceived")
        if r.assignee:
            parts.append(f"assignee {r.assignee}")
        lines.append(", ".join(parts))
    return lines
