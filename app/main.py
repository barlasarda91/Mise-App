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

from app.auth import issue_session_token, request_is_authenticated, verify_password
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


app.add_middleware(AuthGateMiddleware)


def _clock() -> str:
    settings = get_settings()
    now = datetime.now(ZoneInfo(settings.default_tz))
    return now.strftime("%a · %d %b %Y · %H:%M LA").upper()


def render_page(request: Request, template: str, view: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        template,
        {"nav": NAV, "active_view": view, "clock": _clock(), **context},
    )


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if request_is_authenticated(request, get_settings()):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, password: str = Form("")):
    settings = get_settings()
    if not verify_password(settings, password):
        await asyncio.sleep(0.5)  # blunt brute-force damper
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
                            "href": "/board",
                            "urgent": bool(task.due_date and task.due_date < today),
                        }
                    )
            priority = priority[:10]
            waiting = [
                {
                    "title": t.title,
                    "sub": t.waiting_on or t.category.value.replace("_", " "),
                    "age": (today - t.updated_at.date()).days if t.updated_at else None,
                }
                for t in open_tasks
                if t.status == TaskStatus.WAITING
            ][:8]
    except Exception:
        pass
    return render_page(
        request, "home.html", "home", db_status=check_db(), stats=stats, priority=priority, waiting=waiting
    )


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
    from app.web.pipeline_view import load_lead

    lead = load_lead(lead_id)
    if lead is None:
        return RedirectResponse("/pipeline?msg=Lead not found.", status_code=303)
    return render_page(request, "lead.html", "pipeline", lead=lead, msg=msg)


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


@app.post("/tasks/{task_id}/status")
def task_set_status(task_id: int, status: str = Form(...), waiting_on: str = Form("")):
    from app.web.board_view import set_task_status

    try:
        msg = set_task_status(task_id, status, waiting_on)
    except Exception as exc:
        msg = f"Error: {exc}"
    return RedirectResponse(f"/board?msg={msg}", status_code=303)


@app.get("/drafts", response_class=HTMLResponse)
def drafts(request: Request):
    return render_page(request, "placeholder.html", "drafts", title="Email Drafts", milestone="9")


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
        outbound_ip=outbound_ip,
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
