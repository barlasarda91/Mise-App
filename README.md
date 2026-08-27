# Mise — Boxx Daily Ops Hub

Self-hosted routines + tracking hub for Boxx Coffee Roasters. Scheduled Claude
routines (wholesale lead tracker, daily agenda) with a persistent Postgres
system of record, a task board, a wholesale pipeline, and in-app email drafting.

Full concept spec: [`docs/mise-spec.md`](docs/mise-spec.md) ·
Voice guide: [`docs/mise-voice-and-tone.md`](docs/mise-voice-and-tone.md) ·
UX reference: [`docs/boxx-routines-hub-mockups.html`](docs/boxx-routines-hub-mockups.html)

## Status

Milestone 1 — scaffold: FastAPI app, single-user auth gate, Postgres wiring,
Boxx dashboard shell, Railway deploy skeleton.

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
  models/        # SQLAlchemy base (models + migrations: milestone 2)
  engine/        # run lifecycle + Anthropic client (milestone 4)
  tools/         # gmail / calendar / hubdb model-facing tools (milestone 3+)
  routines/      # system prompts + routine config (milestones 7–8)
  web/           # Jinja templates + static (Boxx visual system)
docs/            # spec, voice guide, UX mockups
```
