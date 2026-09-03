# Mise — Boxx Daily Ops Hub

Self-hosted routines + tracking hub for Boxx Coffee Roasters. Scheduled Claude
routines (wholesale lead tracker, daily agenda) with a persistent Postgres
system of record, a task board, a wholesale pipeline, and in-app email drafting.

Full concept spec: [`docs/mise-spec.md`](docs/mise-spec.md) ·
Voice guide: [`docs/mise-voice-and-tone.md`](docs/mise-voice-and-tone.md) ·
UX reference: [`docs/boxx-routines-hub-mockups.html`](docs/boxx-routines-hub-mockups.html)

## Status

- Milestone 1 — scaffold: FastAPI app, single-user auth gate, Postgres wiring,
  Boxx dashboard shell, Railway deploy skeleton. ✅
- Milestone 2 — data model + Alembic migrations (all 12 tables from spec §6). ✅
- Milestone 3 — Google service-account tool layer: Gmail read/draft for both
  mailboxes, Calendar read + reminder events, connectivity test
  (`python -m app.tools.check_google`, also shown on /settings). ✅
- Milestone 4 — run engine: Anthropic client (model aliases → Claude Opus 5 /
  Sonnet 5), runtime-context injection, single-phase agentic tool-use loop
  with per-step `run_messages` persistence, tool registry, refusal handling,
  startup orphan-run sweep. Needs `ANTHROPIC_API_KEY` set to execute real
  runs. First-ever runs (no sync state) scan the past 90 days of backlog. ✅
- Milestone 5 — scheduling: APScheduler with a persistent Postgres jobstore,
  cron per routine in LA time, routines seeded on startup (disabled, with
  placeholder prompts until milestones 7–8), Settings shows routines with
  enable/disable toggles and a background "Run now". ✅
- Milestone 6 — Runs transcript view: read-only review surface with the
  mockup's history-list + transcript layout, tool-call chips, collapsible
  tool results, failure display, and auto-refresh while a run is executing. ✅
- Milestone 7 — Lead Tracker routine + Pipeline page: state-first system
  prompt (versioned in `app/routines/prompts/`, synced on startup), Gmail
  search/read tools, lead/task mutation tools with hold-and-confirm and
  dedup, cadence rules, sync-cursor advancement (`mark_gather_complete`),
  Pipeline kanban with lead detail, manual entry (activity/stage/reminder)
  and one-click pending confirmations. Enable the routine in Settings to
  start daily runs. ✅
- Milestone 8 — Board + Daily Agenda: five-category kanban (tasks by status,
  move/add, source links), agenda routine prompt (schedule / important
  emails / action items with QuickBooks A/R / waiting-on), QuickBooks layer
  with rotation-safe token storage in Postgres and an in-app OAuth Connect
  flow on Settings, Home priority checklist + Waiting-On. ✅
- Milestone 9 — email drafting: voice-profiled generation (Voice A arda@ /
  Voice B hello@ from `docs/mise-voice-and-tone.md`, distilled into
  `app/routines/prompts/voice_*.md`), Drafts UI with editable From/To/Cc/
  subject/body, background generation from lead + thread context, save as
  native Gmail draft (on-thread for replies), discard; routines can prepare
  drafts via `create_email_draft` (deduped). The hub never sends email. ✅

## QuickBooks provisioning

Create an Intuit developer app (production keys), register the redirect URI
`https://<your-app-domain>/settings/qbo/callback`, set `QBO_CLIENT_ID` and
`QBO_CLIENT_SECRET` on Railway, then click **Connect** on the Settings page
and authorize — the rotating refresh token is stored in Postgres
automatically. A/R only; A/P stays in Wolverine.

## Google connector provisioning

Follow spec §5: create a GCP service account, enable domain-wide delegation in
Workspace admin with scopes `gmail.readonly`, `gmail.compose`,
`calendar.events`, authorize impersonation for `ardabarlas@` and `hello@`
(hello@ must be a **licensed user seat**), then set `GOOGLE_SA_JSON` and run
`python -m app.tools.check_google` — all three connectors should report ok.
The tool layer exposes drafts only; **no send function exists** (enforced by a
test).

## Migrations

Schema lives in `app/models/`, migrations in `migrations/versions/`.

```bash
alembic upgrade head          # apply (uses DATABASE_URL)
alembic revision -m "..."     # new migration (write by hand, mirror the models)
```

The deploy start command runs `alembic upgrade head` before the server boots,
so Railway deploys migrate automatically. A test
(`test_migration_matches_models`) asserts the migration chain produces the
same tables/columns as the models — keep it green when changing either side.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # set APP_PASSWORD, SESSION_SECRET; DEV_MODE=1 for http
uvicorn app.main:app --reload
```

Visit http://localhost:8000 — you'll be redirected to the login gate.
`DATABASE_URL` is optional locally; `/health` reports `db: absent` without it.

Tests: `pip install pytest httpx && pytest`

## Railway deploy

1. New Railway project → deploy this repo (Procfile is picked up automatically;
   Python version pinned by `.python-version`).
2. Add the **Postgres plugin**; Railway injects `DATABASE_URL`.
3. Set service variables: `APP_PASSWORD`, `SESSION_SECRET` (long random string),
   `DEFAULT_TZ=America/Los_Angeles`. Later milestones add the connector secrets
   listed in `.env.example`.
4. `GET /health` is the health-check endpoint (checks DB connectivity).

All state lives in Postgres — Railway's filesystem is ephemeral.

## Layout

```
app/
  main.py        # FastAPI app, auth gate, routes
  settings.py    # env-driven settings
  db.py          # engine/session, health probe
  auth.py        # password check + signed session cookie
  models/        # SQLAlchemy models (leads, tasks, runs, drafts, sync, dedup)
  engine/        # run lifecycle + Anthropic client (milestone 4)
  tools/         # gmail / calendar / hubdb model-facing tools (milestone 3+)
  routines/      # system prompts + routine config (milestones 7–8)
  web/           # Jinja templates + static (Boxx visual system)
docs/            # spec, voice guide, UX mockups
```
