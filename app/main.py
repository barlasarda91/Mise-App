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

PUBLIC_PATHS = {"/login", "/health"}

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


@app.get("/health")
def health():
    db_status = check_db()
    body = {"status": "ok" if db_status in ("ok", "absent") else "degraded", "db": db_status}
    return JSONResponse(body, status_code=200 if body["status"] == "ok" else 503)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return render_page(request, "home.html", "home", db_status=check_db())


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
def pipeline(request: Request):
    return render_page(request, "placeholder.html", "pipeline", title="Wholesale Pipeline", milestone="7")


@app.get("/board", response_class=HTMLResponse)
def board(request: Request):
    return render_page(request, "placeholder.html", "board", title="Board", milestone="8")


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
    return render_page(
        request,
        "settings.html",
        "settings",
        report=connectivity_report(),
        secrets=secrets,
        routines=_load_routines(),
        msg=msg,
    )


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
