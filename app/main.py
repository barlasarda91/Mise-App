"""Mise — FastAPI app: auth gate, dashboard shell, health."""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth import issue_session_token, request_is_authenticated, secret_usable, verify_password
from app.db import check_db
from app.settings import get_settings

BASE_DIR = Path(__file__).parent

# Surface app/scheduler/engine INFO logs in the deploy logs (uvicorn only
# configures its own loggers).
logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Crash recovery: fail any run orphaned in `running` by a redeploy (spec §4).
    try:
        from app.engine.runner import sweep_orphan_runs

        swept = sweep_orphan_runs()
        if swept:
            log.warning("startup sweep: marked %d orphaned run(s) failed", swept)
    except Exception as exc:
        log.warning("startup sweep skipped: %s", exc)
    # Seed the standing routines, then start the scheduler (both need the DB).
    try:
        from app.routines.seed import seed_routines

        seed_routines()
    except Exception as exc:
        log.warning("routine seed skipped: %s", exc)
    try:
        from app.scheduler import start_scheduler, stop_scheduler

        start_scheduler()
    except Exception as exc:
        log.warning("scheduler not started: %s", exc)
    yield
    try:
        stop_scheduler()
    except Exception:
        pass


app = FastAPI(title="Mise", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "web" / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "web" / "templates")

PUBLIC_PATHS = {"/login", "/health", "/legal/terms", "/legal/privacy"}

NAV = [
    {"view": "home", "ix": "00", "label": "Today", "path": "/"},
    {"view": "inbox", "ix": "I", "label": "Inbox", "path": "/inbox"},
    {"view": "calendar", "ix": "C", "label": "Calendar", "path": "/calendar"},
    {"view": "runs", "ix": "R", "label": "Runs", "path": "/runs"},
    {"view": "pipeline", "ix": "P", "label": "Pipeline", "path": "/pipeline"},
    {"view": "board", "ix": "B", "label": "Board", "path": "/board"},
    {"view": "drafts", "ix": "D", "label": "Drafts", "path": "/drafts"},
    {"view": "settings", "ix": "S", "label": "Settings", "path": "/settings"},
]


class AuthGateMiddleware(BaseHTTPMiddleware):
    """Everything is behind the password gate except login, health, and static."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)
        if not request_is_authenticated(request, get_settings()):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response


app.add_middleware(AuthGateMiddleware)
app.add_middleware(SecurityHeadersMiddleware)


def _clock() -> str:
    settings = get_settings()
    now = datetime.now(ZoneInfo(settings.default_tz))
    return now.strftime("%a · %d %b %Y · %H:%M LA").upper()


def _nav_badges() -> dict:
    """Per-tab notification counts: unread mail, overdue pipeline leads, and
    routine-prepared drafts awaiting review. Each fails soft to no badge."""
    badges = {}
    try:
        from app.web.inbox_view import unread_count

        badges["inbox"] = unread_count()
    except Exception:
        pass
    try:
        from app.web.pipeline_view import overdue_count

        badges["pipeline"] = overdue_count()
    except Exception:
        pass
    try:
        from app.web.drafts_view import auto_ready_count

        badges["drafts"] = auto_ready_count()
    except Exception:
        pass
    return badges


def render_page(request: Request, template: str, view: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        template,
        {"nav": NAV, "active_view": view, "clock": _clock(), "badges": _nav_badges(), **context},
    )


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if request_is_authenticated(request, get_settings()):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, password: str = Form("")):
    settings = get_settings()
    if not secret_usable(settings):
        # session_is_valid also refuses the default secret, so the gate stays
        # closed to forged cookies too — this just gives a clear message.
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "SESSION_SECRET is not configured on the server."},
            status_code=503,
        )
    if not verify_password(settings, password):
        # Flat damper: a per-attempt counter would let an attacker degrade the
        # operator (shared state) while barely slowing parallel guessing.
        await asyncio.sleep(0.5)
        message = (
            "APP_PASSWORD is not configured on the server."
            if not settings.app_password
            else "Wrong password."
        )
        return templates.TemplateResponse(
            request, "login.html", {"error": message}, status_code=401
        )
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        settings.session_cookie_name,
        issue_session_token(settings),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=not settings.dev_mode,
    )
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(get_settings().session_cookie_name)
    return response


LEGAL_EFFECTIVE = "September 3, 2026"

TERMS_BODY = """
<p>Mise is a private, internal operations application built and operated by Boxx Coffee
Roasters ("Boxx") for its own business. It is not offered to the public, has no external
customers, and access is limited to Boxx's authorized operator.</p>
<h2>Use</h2>
<ul>
<li>The application may be used only by Boxx and only for Boxx's business operations.</li>
<li>Connections to third-party services (Google Workspace, Intuit QuickBooks Online,
Anthropic) are authorized by Boxx, act on Boxx's own accounts, and are governed by those
services' own terms.</li>
<li>The application is provided as-is for internal use, without warranties of any kind.</li>
</ul>
<h2>Contact</h2>
<p>Boxx Coffee Roasters, Arts District, Los Angeles — hello@boxxcoffee.com</p>
"""

PRIVACY_BODY = """
<p>Mise is a single-tenant internal tool: the only data subject and the only user is Boxx
Coffee Roasters ("Boxx") itself. This policy describes how the application handles data.</p>
<h2>What the application processes</h2>
<ul>
<li><b>Email metadata and content</b> from Boxx's own Google Workspace mailboxes, read to
track wholesale leads and prepare a daily briefing.</li>
<li><b>Calendar events</b> from Boxx's own Google Calendar, read to prepare briefings and
to create reminder events at Boxx's request.</li>
<li><b>QuickBooks Online accounting data</b> (issued customer invoices / accounts
receivable), read to surface overdue receivables. The application never writes to or
deletes QuickBooks data.</li>
</ul>
<h2>How data is stored and shared</h2>
<ul>
<li>Data is stored in a private database operated for Boxx (hosted on Railway) and shown
only to Boxx's authenticated operator behind a login.</li>
<li>Portions of the data are processed by Anthropic's Claude API to generate summaries
and briefings, acting as a processor on Boxx's behalf.</li>
<li>Data is never sold and never shared with, shown to, or used by any party other than
Boxx and the processors named above.</li>
</ul>
<h2>Retention and control</h2>
<p>Boxx controls all stored data and may delete it at any time. Third-party connections
can be revoked at any time from the respective service (Google Admin Console, QuickBooks
connected-apps settings).</p>
<h2>Contact</h2>
<p>Boxx Coffee Roasters, Arts District, Los Angeles — hello@boxxcoffee.com</p>
"""


@app.get("/legal/terms", response_class=HTMLResponse)
def legal_terms(request: Request):
    return templates.TemplateResponse(
        request, "legal.html",
        {"title": "End-User License Agreement", "effective": LEGAL_EFFECTIVE, "body": TERMS_BODY},
    )


@app.get("/legal/privacy", response_class=HTMLResponse)
def legal_privacy(request: Request):
    return templates.TemplateResponse(
        request, "legal.html",
        {"title": "Privacy Policy", "effective": LEGAL_EFFECTIVE, "body": PRIVACY_BODY},
    )


@app.get("/health")
def health():
    db_status = check_db()
    body = {"status": "ok" if db_status in ("ok", "absent") else "degraded", "db": db_status}
    return JSONResponse(body, status_code=200 if body["status"] == "ok" else 503)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    stats = {"open": "—", "overdue": "—", "due_today": "—", "drafts": "—"}
    priority: list[dict] = []
    waiting: list[dict] = []
    try:
        from sqlalchemy import select

        from app.db import db_session
        from app.models import DraftStatus, EmailDraft, Lead, OPEN_LEAD_STAGES, Task, TaskStatus
        from app.routines.cadence import idle_days, is_overdue

        today = datetime.now(ZoneInfo(get_settings().default_tz)).date()
        with db_session() as s:
            leads = s.scalars(select(Lead).where(Lead.stage.in_(OPEN_LEAD_STAGES))).all()
            stats["open"] = len(leads)
            stats["overdue"] = sum(1 for lead in leads if is_overdue(lead, today))
            open_tasks = s.scalars(
                select(Task)
                .where(Task.status != TaskStatus.DONE)
                .order_by(Task.due_date.is_(None), Task.due_date)
            ).all()
            stats["due_today"] = sum(1 for t in open_tasks if t.due_date and t.due_date <= today)
            stats["drafts"] = len(
                s.scalars(select(EmailDraft).where(EmailDraft.status == DraftStatus.COMPOSED)).all()
            )

            for lead in leads:
                if lead.pending_confirmation:
                    priority.append(
                        {
                            "title": f"Confirm found email — {lead.business_name}",
                            "sub": f"wholesale leads · pending confirmation",
                            "href": f"/pipeline/lead/{lead.id}",
                            "urgent": True,
                        }
                    )
            for task in open_tasks:
                if task.status == TaskStatus.WAITING:
                    continue
                due_flag = task.due_date and task.due_date <= today
                if due_flag or task.priority.value == "high":
                    priority.append(
                        {
                            "title": task.title,
                            "sub": f"{task.category.value.replace('_', ' ')}"
                            + (f" · due {task.due_date}" if task.due_date else ""),
                            "href": f"/board/task/{task.id}",
                            "urgent": bool(task.due_date and task.due_date < today),
                        }
                    )
            priority = priority[:10]
            waiting = [
                {
                    "title": t.title,
                    "sub": t.waiting_on or t.category.value.replace("_", " "),
                    "age": (today - t.updated_at.date()).days if t.updated_at else None,
                    "href": f"/board/task/{t.id}",
                }
                for t in open_tasks
                if t.status == TaskStatus.WAITING
            ][:8]
    except Exception:
        pass
    return render_page(
        request, "home.html", "home", db_status=check_db(), stats=stats, priority=priority, waiting=waiting
    )


@app.get("/inbox", response_class=HTMLResponse)
def inbox(request: Request, open: str | None = None, msg: str | None = None):
    from app.web.inbox_view import load_inbox, load_open_message

    data = load_inbox()
    opened = None
    if open and ":" in open:
        mailbox, _, msg_id = open.partition(":")
        opened = load_open_message(mailbox, msg_id)
    return render_page(
        request, "inbox.html", "inbox",
        inbox=data, opened=opened, msg=msg,
    )


@app.post("/inbox/mute")
def inbox_mute(email: str = Form(...), back: str = Form("/inbox")):
    from app.web.inbox_view import mute_sender

    try:
        message = mute_sender(email)
    except Exception as exc:
        message = f"Error: {exc}"
    target = back if back.startswith("/") and not back.startswith("//") else "/inbox"
    return RedirectResponse(f"{target}?msg={message}", status_code=303)


@app.post("/inbox/unmute")
def inbox_unmute(email: str = Form(...), back: str = Form("/settings")):
    from app.web.inbox_view import unmute_sender

    try:
        message = unmute_sender(email)
    except Exception as exc:
        message = f"Error: {exc}"
    target = back if back.startswith("/") and not back.startswith("//") else "/settings"
    return RedirectResponse(f"{target}?msg={message}", status_code=303)


@app.post("/inbox/draft")
def inbox_draft(
    instruction: str = Form(""),
    mailbox: str = Form("arda"),
    thread_id: str = Form(""),
    to: str = Form(""),
):
    from app.web.drafts_view import start_generation

    try:
        message, draft_id = start_generation(instruction, mailbox, "", thread_id, to=to)
    except Exception as exc:
        message, draft_id = f"Error: {exc}", None
    if draft_id:
        return RedirectResponse(f"/drafts?draft={draft_id}&msg={message}", status_code=303)
    return RedirectResponse(f"/inbox?msg={message}", status_code=303)


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request):
    from app.web.calendar_view import load_calendar

    return render_page(request, "calendar.html", "calendar", cal=load_calendar())


@app.get("/runs", response_class=HTMLResponse)
def runs(request: Request, run: int | None = None):
    from app.web.runs_view import load_runs_index, load_transcript

    index = load_runs_index()
    selected, transcript = (None, [])
    if index:
        selected, transcript = load_transcript(run if run is not None else index[0]["id"])
    return render_page(
        request, "runs.html", "runs", runs=index, selected=selected, transcript=transcript
    )


@app.get("/pipeline", response_class=HTMLResponse)
def pipeline(request: Request, msg: str | None = None):
    from app.web.pipeline_view import load_board

    lanes, _stats = load_board()
    return render_page(request, "pipeline.html", "pipeline", lanes=lanes, msg=msg)


@app.get("/pipeline/lead/{lead_id}", response_class=HTMLResponse)
def lead_detail(request: Request, lead_id: int, msg: str | None = None):
    from app.web.pipeline_view import load_email_context, load_lead

    lead = load_lead(lead_id)
    if lead is None:
        return RedirectResponse("/pipeline?msg=Lead not found.", status_code=303)
    return render_page(
        request, "lead.html", "pipeline", lead=lead, email_ctx=load_email_context(lead), msg=msg
    )


@app.post("/leads/{lead_id}/draft")
def lead_draft_email(
    lead_id: int,
    instruction: str = Form(""),
    mailbox: str = Form("arda"),
    thread_id: str = Form(""),
):
    from app.web.drafts_view import start_generation

    try:
        msg, draft_id = start_generation(instruction, mailbox, str(lead_id), thread_id)
    except Exception as exc:
        msg, draft_id = f"Error: {exc}", None
    if draft_id:
        return RedirectResponse(f"/drafts?draft={draft_id}&msg={msg}", status_code=303)
    return RedirectResponse(f"/pipeline/lead/{lead_id}?msg={msg}", status_code=303)


@app.post("/pipeline/leads")
def pipeline_add_lead(
    business_name: str = Form(""),
    contact_name: str = Form(""),
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
    lead_source: str = Form(""),
):
    from app.web.pipeline_view import create_lead_manual

    try:
        msg = create_lead_manual(business_name, contact_name, contact_email, contact_phone, lead_source)
    except Exception as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/pipeline?msg={msg}", status_code=303)


@app.post("/leads/{lead_id}/activity")
def lead_log_activity(lead_id: int, type: str = Form(...), occurred_on: str = Form(""), detail: str = Form("")):
    from app.web.pipeline_view import log_activity_manual

    try:
        msg = log_activity_manual(lead_id, type, occurred_on, detail)
    except Exception as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/pipeline/lead/{lead_id}?msg={msg}", status_code=303)


@app.post("/leads/{lead_id}/stage")
def lead_change_stage(lead_id: int, stage: str = Form(...), loss_reason: str = Form("")):
    from app.web.pipeline_view import change_stage_manual

    try:
        msg = change_stage_manual(lead_id, stage, loss_reason)
    except Exception as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/pipeline/lead/{lead_id}?msg={msg}", status_code=303)


@app.post("/leads/{lead_id}/pending")
def lead_resolve_pending(lead_id: int, action: str = Form(...)):
    from app.web.pipeline_view import resolve_pending

    try:
        msg = resolve_pending(lead_id, action)
    except Exception as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/pipeline/lead/{lead_id}?msg={msg}", status_code=303)


@app.post("/leads/{lead_id}/reminder")
def lead_create_reminder(lead_id: int, remind_on: str = Form(...), note: str = Form("")):
    from app.web.pipeline_view import create_reminder_manual

    try:
        msg = create_reminder_manual(lead_id, remind_on, note)
    except Exception as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/pipeline/lead/{lead_id}?msg={msg}", status_code=303)


@app.get("/board", response_class=HTMLResponse)
def board(request: Request, msg: str | None = None):
    from app.web.board_view import load_boards

    return render_page(request, "board.html", "board", boards=load_boards(), msg=msg)


@app.post("/board/tasks")
def board_add_task(
    category: str = Form(...),
    title: str = Form(""),
    due_date: str = Form(""),
    assignee: str = Form(""),
    priority: str = Form("normal"),
):
    from app.web.board_view import create_task_manual

    try:
        msg = create_task_manual(category, title, due_date, assignee, priority)
    except Exception as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/board?msg={msg}", status_code=303)


@app.get("/board/task/{task_id}", response_class=HTMLResponse)
def task_detail(request: Request, task_id: int, msg: str | None = None):
    from app.web.board_view import load_task, load_task_email_context
    from app.web.drafts_view import open_leads_for_picker

    task = load_task(task_id)
    if task is None:
        return RedirectResponse("/board?msg=Task not found.", status_code=303)
    return render_page(
        request, "task.html", "board", task=task, email_ctx=load_task_email_context(task),
        leads=open_leads_for_picker(), msg=msg,
    )


@app.post("/tasks/{task_id}/link-lead")
def task_link_lead(task_id: int, lead_id: str = Form("")):
    from app.web.board_view import link_task_to_lead, unlink_task_lead

    try:
        msg = link_task_to_lead(task_id, int(lead_id)) if lead_id else unlink_task_lead(task_id)
    except Exception as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/board/task/{task_id}?msg={msg}", status_code=303)


@app.post("/tasks/{task_id}/status")
def task_set_status(task_id: int, status: str = Form(...), waiting_on: str = Form(""), next: str = Form("/board")):
    from app.web.board_view import set_task_status

    try:
        msg = set_task_status(task_id, status, waiting_on)
    except Exception as exc:
        msg = f"Error: {exc}"
    target = next if next.startswith("/") and not next.startswith("//") else "/board"
    return RedirectResponse(f"{target}?msg={msg}", status_code=303)


@app.post("/tasks/{task_id}/draft")
def task_draft_email(
    task_id: int,
    instruction: str = Form(""),
    mailbox: str = Form("arda"),
    thread_id: str = Form(""),
    to: str = Form(""),
):
    from app.web.board_view import load_task
    from app.web.drafts_view import start_generation

    task = load_task(task_id)
    lead_id = str(task.get("lead_id") or "") if task else ""
    try:
        msg, draft_id = start_generation(
            instruction, mailbox, lead_id, thread_id, task_id=str(task_id), to=to
        )
    except Exception as exc:
        msg, draft_id = f"Error: {exc}", None
    if draft_id:
        return RedirectResponse(f"/drafts?draft={draft_id}&msg={msg}", status_code=303)
    return RedirectResponse(f"/board/task/{task_id}?msg={msg}", status_code=303)


@app.post("/tasks/{task_id}/edit")
def task_edit(
    task_id: int,
    due_date: str = Form(""),
    assignee: str = Form(""),
    priority: str = Form("normal"),
    description: str = Form(""),
):
    from app.web.board_view import edit_task_manual

    try:
        msg = edit_task_manual(task_id, due_date, assignee, priority, description)
    except Exception as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/board/task/{task_id}?msg={msg}", status_code=303)


@app.get("/drafts", response_class=HTMLResponse)
def drafts(request: Request, draft: int | None = None, msg: str | None = None):
    from app.web.drafts_view import load_draft, load_drafts_index, load_thread, open_leads_for_picker

    index = load_drafts_index()
    selected = None
    if index:
        selected = load_draft(draft if draft is not None else index[0]["id"])
    from app.web.drafts_view import library_files

    return render_page(
        request, "drafts.html", "drafts",
        drafts=index, selected=selected, thread=load_thread(selected),
        leads=open_leads_for_picker(), library=library_files(), msg=msg,
    )


@app.post("/drafts/generate")
def drafts_generate(
    instruction: str = Form(""),
    mailbox: str = Form("arda"),
    lead_id: str = Form(""),
    thread_id: str = Form(""),
):
    from app.web.drafts_view import start_generation

    try:
        msg, draft_id = start_generation(instruction, mailbox, lead_id, thread_id)
    except Exception as exc:
        msg, draft_id = f"Error: {exc}", None
    target = f"/drafts?draft={draft_id}&msg={msg}" if draft_id else f"/drafts?msg={msg}"
    return RedirectResponse(target, status_code=303)


@app.post("/drafts/blank")
def drafts_blank(mailbox: str = Form("arda")):
    from app.web.drafts_view import create_blank

    try:
        draft_id = create_blank(mailbox)
        return RedirectResponse(f"/drafts?draft={draft_id}", status_code=303)
    except Exception as exc:
        return RedirectResponse(f"/drafts?msg=Error: {exc}", status_code=303)


@app.post("/drafts/{draft_id}")
def drafts_update(
    draft_id: int,
    from_mailbox: str = Form(...),
    to: str = Form(""),
    cc: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
):
    from app.web.drafts_view import update_fields

    try:
        msg = update_fields(draft_id, from_mailbox, to, cc, subject, body)
    except Exception as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/drafts?draft={draft_id}&msg={msg}", status_code=303)


@app.post("/drafts/{draft_id}/save-to-gmail")
def drafts_save_to_gmail(draft_id: int):
    from app.web.drafts_view import save_to_gmail

    try:
        msg = save_to_gmail(draft_id)
    except Exception as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/drafts?draft={draft_id}&msg={msg}", status_code=303)


@app.post("/drafts/{draft_id}/attach")
async def drafts_attach(draft_id: int, request: Request):
    from app.web.drafts_view import attach_upload

    try:
        form = await request.form()
        upload = form.get("file")
        content = await upload.read() if upload is not None and upload.filename else b""
        msg = attach_upload(
            draft_id,
            upload.filename if upload is not None else "",
            content,
            getattr(upload, "content_type", "") or "",
            form.get("to_library") == "on",
            str(form.get("label") or ""),
        )
    except Exception as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/drafts?draft={draft_id}&msg={msg}", status_code=303)


@app.post("/drafts/{draft_id}/attach-library")
def drafts_attach_library(draft_id: int, file_id: int = Form(...)):
    from app.web.drafts_view import attach_from_library

    try:
        msg = attach_from_library(draft_id, file_id)
    except Exception as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/drafts?draft={draft_id}&msg={msg}", status_code=303)


@app.post("/drafts/{draft_id}/attachments/{attachment_id}/remove")
def drafts_remove_attachment(draft_id: int, attachment_id: int):
    from app.web.drafts_view import remove_attachment

    try:
        msg = remove_attachment(draft_id, attachment_id)
    except Exception as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/drafts?draft={draft_id}&msg={msg}", status_code=303)


@app.post("/drafts/{draft_id}/send")
def drafts_send(draft_id: int):
    from app.web.drafts_view import send_now

    try:
        msg = send_now(draft_id)
    except Exception as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/drafts?draft={draft_id}&msg={msg}", status_code=303)


@app.post("/drafts/{draft_id}/discard")
def drafts_discard(draft_id: int):
    from app.web.drafts_view import discard

    try:
        msg = discard(draft_id)
    except Exception as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/drafts?msg={msg}", status_code=303)


def _load_routines() -> list:
    from sqlalchemy import select

    from app.db import db_session
    from app.models import Routine

    try:
        with db_session() as s:
            return list(s.scalars(select(Routine).order_by(Routine.id)))
    except Exception:
        return []


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str | None = None):
    from app.tools.check_google import connectivity_report

    settings = get_settings()
    secrets = {
        "APP_PASSWORD": bool(settings.app_password),
        "SESSION_SECRET": settings.session_secret != "dev-secret-change-me",
        "DATABASE_URL": bool(settings.database_url),
        "GOOGLE_SA_JSON": bool(settings.google_sa_json),
        "ANTHROPIC_API_KEY": bool(settings.anthropic_api_key),
    }
    from app.tools.quickbooks import configured as qbo_configured
    from app.tools.quickbooks import qbo_status
    from app.web.inbox_view import muted_list

    outbound_ip = "unavailable"
    try:
        import requests

        outbound_ip = requests.get("https://api.ipify.org", timeout=5).text.strip()
    except Exception:
        pass

    return render_page(
        request,
        "settings.html",
        "settings",
        report=connectivity_report(),
        secrets=secrets,
        routines=_load_routines(),
        qbo=qbo_status(),
        qbo_configured=qbo_configured(),
        qbo_redirect_uri=_qbo_redirect_uri(request),
        outbound_ip=outbound_ip,
        muted=muted_list(),
        msg=msg,
    )


def _qbo_redirect_uri(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/settings/qbo/callback"


@app.get("/settings/qbo/connect")
def qbo_connect(request: Request):
    from itsdangerous import URLSafeTimedSerializer

    from app.tools.quickbooks import authorize_url, configured

    if not configured():
        return RedirectResponse("/settings?msg=Set QBO_CLIENT_ID and QBO_CLIENT_SECRET first.", status_code=303)
    state = URLSafeTimedSerializer(get_settings().session_secret, salt="qbo-state").dumps("qbo")
    return RedirectResponse(authorize_url(_qbo_redirect_uri(request), state), status_code=303)


@app.get("/settings/qbo/callback")
def qbo_callback(request: Request, code: str = "", state: str = "", realmId: str = ""):
    from itsdangerous import BadSignature, URLSafeTimedSerializer

    from app.tools.quickbooks import exchange_code

    try:
        URLSafeTimedSerializer(get_settings().session_secret, salt="qbo-state").loads(state, max_age=600)
    except BadSignature:
        return RedirectResponse("/settings?msg=QuickBooks connect failed: bad state.", status_code=303)
    if not code or not realmId:
        return RedirectResponse("/settings?msg=QuickBooks connect was cancelled.", status_code=303)
    try:
        exchange_code(code, _qbo_redirect_uri(request), realmId)
        msg = "QuickBooks connected."
    except Exception as exc:
        msg = f"QuickBooks connect failed: {type(exc).__name__}: {exc}"
    return RedirectResponse(f"/settings?msg={msg}", status_code=303)


@app.post("/routines/{routine_id}/run")
def routine_run_now(routine_id: int):
    from app.scheduler import trigger_run_now

    if trigger_run_now(routine_id):
        msg = "Run started in the background — transcript appears in Runs."
    else:
        msg = "Cannot run: scheduler is not running (DATABASE_URL not configured)."
    return RedirectResponse(f"/settings?msg={msg}", status_code=303)


@app.post("/routines/{routine_id}/toggle")
def routine_toggle(routine_id: int):
    from app.db import db_session
    from app.models import Routine
    from app.scheduler import get_scheduler, sync_jobs

    try:
        with db_session() as s:
            routine = s.get(Routine, routine_id)
            if routine is None:
                return RedirectResponse("/settings?msg=Routine not found.", status_code=303)
            routine.enabled = not routine.enabled
            state = "enabled" if routine.enabled else "disabled"
            name = routine.name
    except Exception:
        return RedirectResponse("/settings?msg=Database not configured.", status_code=303)
    sched = get_scheduler()
    if sched is not None:
        sync_jobs(sched)
    return RedirectResponse(f"/settings?msg={name} {state}.", status_code=303)
