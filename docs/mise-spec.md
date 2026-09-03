# Mise — Concept Spec

**Status:** Concept spec, finalized for build handoff to Claude Code — **revised 2026-08-27 after design review** (see §12 for the resolutions)
**Owner:** Arda (Boxx Coffee Roasters)
**Deploy target:** Railway (self-hosted)
**Name:** **Mise** — the daily-routines ops hub. **Supersedes the existing desktop *Mise*** (email/calendar management), which is retired once this ships.

---

## 1. Overview

A self-hosted web app — **Mise** — that centralizes Boxx's daily "Claude Routines" (scheduled Claude API calls) and gives them a **persistent internal memory** so they stop re-deriving state from scratch every morning. Mise takes over the email/calendar role of the retired desktop Mise and extends it into a full routines + tracking hub.

Today each routine is stateless day-to-day: every run treats the world as new and re-scans entire mail/thread history to reconstruct what's happened. The hub replaces that with a **system-of-record database** the routines read from and write to, so a run only pulls *new* information since it last ran, and task/lead progress persists across days.

On top of the routine engine, the hub adds a **Slack/Monday-style project + task board** (replacing Todoist) and **in-app email drafting**.

### Primary user
Single operator (Arda). Design for one authenticated user; multi-user is out of scope for v1.

---

## 2. Core Design Principles

1. **State-first, incremental sync.** The hub DB is the source of truth. On each run a routine reads its current state, then queries external sources (Gmail, Calendar) only for deltas since `last_run_at`, and writes changes back. No full-history re-scans.
2. **One system of record.** Leads, tasks, projects, drafts, and run history all live in the hub's Postgres. No parallel stores to keep in sync (the Google Sheet and Todoist are both dropped — see §11).
3. **Runs are autonomous and single-phase; the dashboard is the review surface.** A scheduled run executes one autonomous turn, writes its findings to the hub DB as it goes, surfaces anything that needs Arda as dashboard items (priority checklist, tasks, pending confirmations), and closes. Progress tracking and manual input happen in the dashboard (Pipeline, Board, Drafts) — not by replying to the run. (See §4.)
4. **No irreversible actions without review.** Email is only ever drafted, never auto-sent. External mutations (calendar events, etc.) are deduped so re-runs don't double-create.
5. **Faithful to the prompt.** Routine behavior is defined by its system prompt; the engine supplies state and tools rather than hard-coding the routine's logic into rigid UI.

---

## 3. Architecture

### Stack
- **Language/runtime:** Python 3.12
- **Web framework:** FastAPI
- **Scheduler:** APScheduler with a **persistent jobstore in Postgres** (schedules survive redeploys/restarts)
- **Database:** Postgres (Railway Postgres plugin) — Railway's filesystem is ephemeral, so *all* state lives in Postgres
- **LLM:** Anthropic Python SDK (Messages API)
- **Frontend:** server-rendered Jinja templates + HTMX for interactivity *(recommended for simplicity; React is an option — see Open Decisions)*
- **Auth:** single-user password gate, session cookie

### Runtime shape on Railway
- One **web service** (FastAPI) serving the dashboard + API.
- APScheduler runs **in-process** in the web service on startup (single instance; no need for a separate worker at this scale). If the app is later scaled to >1 replica, move the scheduler to a dedicated worker to avoid duplicate firings.
- **Postgres** plugin for data + APScheduler jobstore.
- Secrets (API keys, Google credentials) as Railway environment variables.

### Component flow
```
APScheduler (cron, LA time)
      │ fires routine
      ▼
Run Engine ──► Anthropic Messages API ──► model
      │            ▲        │
      │            │        ▼ tool calls
      │        Tool layer (Gmail / Calendar / Hub DB)
      ▼
   Postgres (leads, tasks, runs, messages, drafts, sync_state)
      ▲
Dashboard (FastAPI + Jinja/HTMX) ── Arda reviews & replies
```

---

## 4. Execution Model — Autonomous Single-Phase Runs

> **Decided 2026-08-27 (supersedes the earlier "conversation per run" lock):** runs close when they run. A run's job is to get information over to the dashboard; once items are on the dashboard, progress is tracked there — not by replying to the run.

Each routine execution is a **single autonomous turn** (which may involve many tool calls) that closes when it finishes.

### Lifecycle
1. **Trigger** (scheduled, e.g. lead tracker at 8:30 AM LA — or manual "Run now"). Engine creates a `run` in state `running`.
2. **Autonomous execution.** Engine calls the Messages API with:
   - the routine's system prompt,
   - injected **runtime context** (current datetime + timezone, `last_run_at`, and a state snapshot relevant to the routine — e.g., open leads, or incomplete tasks),
   - the tool layer (Gmail/Calendar/Hub DB) available for tool use.
   The model gathers deltas, analyzes, and **applies safe updates via tools as it goes** (lead activity from found sent mail, new tasks, overdue-invoice tasks, drafts, calendar reminders — all through the dedup ledger).
3. **Surface, don't ask.** Anything that needs Arda's judgment is **not** applied — it becomes a dashboard item instead:
   - an action that would clear an overdue alert → a **pending confirmation** on the lead (see §7.1 hold-and-confirm); the timer resets only when Arda confirms it in Pipeline.
   - a new inbound lead → inserted at stage **New** plus a "Qualify …" task; Arda edits or discards it in Pipeline.
   - anything else actionable → a task in the right Board category (which feeds the Home priority list).
4. **Close.** Run moves to `completed` (or `failed` on error). The full transcript and a log of tool actions taken are retained and viewable read-only in the Runs page.

### Manual input path (replaces the interactive review phase)
Calls, texts, stage changes, closures, notes, and reminder requests are entered **directly in the Pipeline page** (lead detail: log an activity with the *actual date it happened*, change stage, close Won/Lost, set a reminder date → calendar event). These UI actions write the same tables the run tools do, so the next run picks them up as state — no chat required.

### Run execution & resilience
- Runs (scheduled or "Run now") always execute as **background jobs, never inside an HTTP request** — a model turn with tool calls can take 30–60s+, longer than Railway's proxy timeout. "Run now" returns immediately; the UI polls the run's status until it completes.
- On app startup, sweep any run stuck in `running` (orphaned by a redeploy/crash) to `failed` so it doesn't hang forever.

### Idempotency / dedup
Because a run can be re-executed (manual re-run, retry after failure), every external mutation records a **dedup key** in a ledger table:
- Calendar reminders keyed by `(lead_id, requested_date, purpose)`.
- Email drafts keyed by `(lead_id_or_task_id, draft_purpose, run_id)`.
- Tasks created by a routine keyed by a stable natural key so the same source email doesn't spawn duplicates across runs.
Before creating, the tool checks the ledger and updates instead of re-creating.

---

## 5. Connectors & Authentication

The headless backend **cannot borrow Arda's Claude.ai OAuth sessions** — it needs its own credentials.

> **Canonical mailbox:** `ardabarlas@boxxcoffee.com` (confirmed 2026-08-27). Anywhere the voice guide or shorthand says "arda@", it means this mailbox — impersonation, `in:sent` audits, and draft From headers all use `ardabarlas@`.

### Required connectors (v1)
| Source | Used for | Auth approach |
|---|---|---|
| **Gmail — ardabarlas@boxxcoffee.com** | read inbox + sent, create drafts | Google Workspace service account with domain-wide delegation, impersonating this mailbox |
| **Gmail — hello@boxxcoffee.com** | read **its own** inbox + sent directly, create drafts | Same service account, impersonating this mailbox (separate Workspace mailbox — not read via arda@) |
| **Google Calendar — ardabarlas@boxxcoffee.com** | read events, create reminder events | Same service account, Calendar scope |
| **Intuit QuickBooks Online** | read **issued customer invoices + A/R**, detect overdue receivables (Invoice Tracking + agenda bill/balance reconciliation). *A/R only — not A/P.* | **QuickBooks OAuth2 (Intuit) — separate from the Google SA**; refresh token + realm ID. **Refresh tokens rotate on every use** — see below. |

**QuickBooks token handling (critical):** Intuit issues a *new* refresh token on every access-token refresh and invalidates the old one. The `QBO_REFRESH_TOKEN` env var is therefore only the **day-one bootstrap seed** — the app must persist the latest rotated refresh token in **Postgres** and always use that. Persist the new token *before* using the new access token (so a crash mid-refresh can't lose it). Cadence: the agenda's once-daily A/R check is ideal — frequent enough to stay far from Intuit's ~100-day idle expiry, infrequent enough to never rate-limit. Settings must include a **"Reconnect QuickBooks"** re-auth flow for the rare case a rotation is lost.

**Gmail scopes & the never-send guarantee:** request only `gmail.readonly` + `gmail.compose`. Note that `gmail.compose` technically permits sending, so "never auto-send" **cannot be enforced by scopes alone** — it is enforced at the tool layer: the model-facing Gmail tools expose `create_draft` / `update_draft` only; **no send function exists anywhere in the tool layer.**

### Recommended integration method
Use **Google's API client libraries directly**, wrapped as the app's own tool functions exposed to the model via tool use. Rationale: on a Workspace domain (`boxxcoffee.com`), a **service account with domain-wide delegation** is the clean headless path for impersonating both mailboxes and the calendar, with no interactive OAuth to refresh. This is more robust for unattended scheduled runs than remote-MCP-with-tokens. (Remote MCP via the API's `mcp_servers` param remains a fallback option.)

### hello@ mailbox — critical path
hello@boxxcoffee.com is a **separate mailbox inside the boxxcoffee.com Workspace** (not an alias on arda@). This is why it was unreadable in Claude.ai — that connector could only see arda@. The fix: the service account **impersonates hello@ directly** and reads hello@'s own inbox and sent folder via the Gmail API. **Do not** replicate the original prompt's `to:hello@` / `from:hello@` filtering inside arda@'s mailbox — that was a workaround for lacking direct access and silently misses mail that lives only in hello@.

**Provisioning caveat:** domain-wide delegation can impersonate only a **licensed user account**. Confirm hello@ is a full Workspace user seat. If it's configured as an unlicensed shared mailbox or a Google Group, it needs a license or an alternative read path (verify during provisioning).

### Dropped connectors
- **Google Drive** — only existed to host the pipeline Sheet, which is being dropped.
- **Todoist** — replaced by the internal task board.

### Provisioning checklist (Arda)
- [ ] Create a GCP service account; enable domain-wide delegation in Workspace admin.
- [ ] Grant scopes: Gmail read + compose/drafts, Calendar read + write.
- [ ] Authorize impersonation for `ardabarlas@` and `hello@`.
- [ ] Verify `hello@` is a **licensed user account** (required for impersonation); confirm the SA can read its inbox + sent with a connectivity test before wiring the routine.
- [ ] Anthropic API key.
- [ ] **QuickBooks Online OAuth** (Intuit developer app): authorize, seed the refresh token + realm ID (the app then owns the rotating token in Postgres — see token handling above). This is the one connector that does **not** ride the Google service account — it's Intuit's own OAuth2.
- [ ] (Optional/future) Boxx MCP token, Shopify.

### Secrets (Railway env vars)
`ANTHROPIC_API_KEY`, `GOOGLE_SA_JSON` (or path), `GOOGLE_DELEGATED_USERS`, `QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`, `QBO_REFRESH_TOKEN` (bootstrap seed only — live token rotates in Postgres), `QBO_REALM_ID`, `DATABASE_URL`, `APP_PASSWORD`, `SESSION_SECRET`, `DEFAULT_TZ=America/Los_Angeles`.

---

## 6. Data Model (Postgres)

Indicative schema — Claude Code to refine types/constraints.

### `routines`
`id, key, name, system_prompt (text), schedule_cron, timezone, model (default 'opus'), enabled, connectors (jsonb), created_at, updated_at`

### `runs`
`id, routine_id, status (running|completed|failed), trigger (scheduled|manual), started_at, completed_at, last_run_at_snapshot, error (nullable)`

### `run_messages`
`id, run_id, role (user|assistant|tool), content (jsonb), tool_calls (jsonb), created_at` — the full transcript of the run's autonomous turn (assistant output + tool calls/results), retained for read-only review in the Runs page.

### `sync_state`
`id, source (gmail_arda|gmail_hello|calendar), routine_id, last_run_at, cursor (jsonb)` — drives incremental delta queries. **Advancement rule:** the cursor/`last_run_at` advances only after the run's gather phase succeeds — never at run start. A failed run therefore re-covers its window on the next fire; overlapping windows are harmless because all delta processing is **idempotent by Gmail message ID** (see `lead_activity.gmail_msg_id`).

### `leads` (maps the Lead Schema from the routine prompt)
`id, business_name, contact_name, contact_email, contact_phone, location, lead_source, format (enum multi: espresso|filter|both), coffee_program (enum multi: blend_entry|rotating_single_farm|competition), projected_weekly_consumption, projected_unit (lb|kg), stage (new|contacted|sampled|negotiating|closed_won|closed_lost), stage_since (date), last_confirmed_action (date), pending_confirmation (jsonb nullable — a found action awaiting Arda's one-click confirm before the idle timer resets), loss_reason (nullable), created_at, updated_at`

> Single contact per lead is a deliberate v1 choice (confirmed at review); multi-contact can come later if needed.

### `lead_activity`
`id, lead_id, type (email_sent|call|text|visit|note|stage_change), occurred_on (date), detail (text), source (gmail|manual), gmail_msg_id (nullable, unique when set), run_id (nullable), created_at` — running timestamped log; `last_confirmed_action` derives from the latest outbound action here. `gmail_msg_id` makes email-derived activity idempotent across overlapping sync windows.

### `projects`
`id, name, description, status, created_at, updated_at`

### `tasks` (Slack/Monday-style board)
`id, category (enum: wholesale_leads|consultation|pop_ups|invoice_tracking|governance), project_id (nullable), title, description, status (fixed enum for v1: todo|doing|waiting|done), due_date (nullable), priority, assignee (text; Arda or team member name), source (manual|routine|email|calendar|quickbooks), source_ref (jsonb: lead_id / gmail_msg_id / event_id / qbo_invoice_id), waiting_on (text nullable), created_at, updated_at, completed_at`

> `category` is the top-level classification (the Board's tabs and the dashboard priority tag). `project_id` is an optional finer grouping within a category, for later.

### `task_activity`
`id, task_id, type, detail, actor, created_at` — activity log per task.

### `email_drafts`
`id, subject, body, to_addrs, cc_addrs, from_mailbox (arda|hello), gmail_thread_id (nullable), related_lead_id (nullable), related_task_id (nullable), gmail_draft_id (nullable), status (drafting|composed|saved_to_gmail|discarded), run_id (nullable), created_at`

- **From, To, and Cc are editable in the draft UI** before saving to Gmail.
- `gmail_thread_id`: when the draft replies to an existing conversation (an inbound inquiry, an ongoing thread), the Gmail draft is created **on that thread** with proper reply headers — not as a fresh compose.
- `drafting` = generation in progress; `composed` = ready for review.

### `external_mutations` (dedup ledger)
`id, kind (calendar_event|gmail_draft|task), dedup_key (unique), external_id, run_id, created_at`

### `users`
`id, username, password_hash` — single row for v1.

---

## 7. Routine Catalog

Both routines are refactored from "re-derive daily" to **state-first + incremental**. The original prompt text is preserved as the system prompt; the changes below describe how state/tools replace the re-scanning behavior.

### 7.1 Wholesale Lead Tracker
**Schedule:** daily **8:30 AM** America/Los_Angeles.
**Connectors:** Gmail (both mailboxes), Calendar (arda).
**Output shape:** autonomous run transcript — audit summary, tables, surfaced dashboard items (tasks / pending confirmations), end-of-run pipeline table.

**Refactor to state-first:**
- **Step 1 (Sent-email audit):** query sent mail only **since `last_run_at`** for contacts on open leads, instead of scanning all history. Read **each mailbox's own Sent folder directly** by impersonation — `in:sent` on arda@ *and* `in:sent` on hello@ (impersonated). Do **not** use `to:`/`from:hello@` filters inside arda@; hello@ is a separate mailbox and must be read as itself (see §5). Matches update `lead_activity` (type `email_sent`, keyed by `gmail_msg_id`) and advance `last_confirmed_action`. Calls/texts remain self-reported by Arda — logged in the Pipeline lead detail with the **actual date** they occurred (not today's).
- **Hold-and-confirm (dashboard form):** if a found action would clear an overdue alert, the run does **not** reset the clock — it writes it to `leads.pending_confirmation` and the lead surfaces on Home/Pipeline with a one-click **Confirm** (which applies the activity and resets `last_confirmed_action`) or **Dismiss**. Non-overdue matches apply automatically.
- **Step 2 (Inbox scan):** scan both inboxes **directly** (ardabarlas@ and hello@ impersonated) for wholesale-inquiry signals **since `last_run_at`**; insert new `leads` at stage **New** with a "Qualify [business]" task on the Board — Arda edits or discards them in Pipeline. hello@ is typically where inbound wholesale inquiries land, so its direct readability is essential to this step.
- **Step 3 (Pipeline audit):** idle-days computed in-DB from `last_confirmed_action`. Follow-up cadence rules (unchanged):
  - **New** — alert immediately, every day until Contacted
  - **Contacted** — every 3 days
  - **Sampled** — at 3 days, then 7 days, then every 5 days after
  - **Negotiating** — every 7 days
  **Anchor:** all cadence timers count from `last_confirmed_action`; a confirmed action resets the Sampled sequence to its start (3d → 7d → every 5d). Overdue leads are surfaced prioritized (days idle, last confirmed action date, last note) in the run summary and as Home priority items.
- **Step 4 (Manual entry & edits — via Pipeline, not the run):** new leads, stage changes, notes, and closures (Won/Lost; loss reason required on Lost) are entered directly in the Pipeline lead detail, with activity dated to when it actually happened. Reminder requests (from Pipeline or created by a run for a due follow-up) → **create a Google Calendar popup event** on arda's calendar at 9 AM on the requested date (10/11 AM if 9 is taken that day), description carrying contact name, phone/email, one-line context. Deduped via `external_mutations`.
- **Persistence:** all writes land in the hub DB. **The Google Sheet is removed entirely** — no load-from-Sheet at start, no save-to-Sheet at end.
- **Cold start:** if there are no leads in the DB (first run), reconstruct open leads by scanning **sent mail for wholesale outreach** *and* **hello@'s inbox for unanswered inbound wholesale inquiries** over the past 90 days (inbound-only prospects who never got a reply are the most important ones to catch). Insert them flagged with a "Review reconstructed lead" task each, for Arda to correct or discard in Pipeline.
- **End of run:** the run transcript closes with the full pipeline grouped by stage, sorted by days idle descending.

### 7.2 Daily Agenda / Briefing
**Schedule:** daily, **7:30 AM** America/Los_Angeles *(time defaulted — see Open Decisions)*.
**Connectors:** Gmail (arda; hello optional), Calendar (arda), **QuickBooks (A/R)**. **Task source is the internal board, not Todoist.**
**Output shape:** concise, scannable briefing; priority items as a checklist.

**Sections (behavior preserved, Todoist swapped for the hub board):**
1. **Schedule** — today's + upcoming meetings/calls/events with times and prep context; note recurring items. **Flag any invite where the displayed timezone label and the actual offset don't match**, so join time isn't misjudged.
2. **Important emails** — summarize unread emails needing attention, grouped by urgency. Reference contacts as **Name — Company**. If two sources cite different figures for what looks like the same bill/balance, **flag the discrepancy explicitly** for reconciliation rather than listing both numbers uncommented.
3. **Action items** — consolidate from three sources: open **tasks in the hub board**, follow-ups triggered by today's emails/calendar, and anything delegated to a team member due soon. **For each new action item surfaced this run, create a corresponding task in the hub board** (with a due date when known/implied, e.g. an overdue invoice). For delegated items, flag when confirmation exists only outside email/calendar (e.g. WhatsApp) and hasn't been verified in writing — **state plainly when no such written confirmation was found** rather than omitting the check.
   - **A/R check (QuickBooks):** query QuickBooks for **overdue customer invoices (receivables)**. Surface them in the briefing (customer, invoice #, amount, days overdue) and create/update **Invoice Tracking** tasks for any not already tracked. Receivables with a payment link already sent go to **Waiting on** instead. This is A/R only — no A/P (that's Wolverine).
4. **Waiting on** — anything blocked pending a third-party response, payment link, confirmation, or callback; clearly distinguished from act-now items.

**Behaviors:**
- **Carry-over is now a DB query:** check the board for open and recently-completed items from the prior day; include anything unresolved; follow up on prior-day agenda items with no recorded action.
- Skip empty sections rather than saying "nothing to report." If no high-priority items, surface secondary ones.
- **Graceful degradation:** if the board/DB is unreachable, say so plainly and still list action items in the briefing text rather than retrying silently.
- **Close:** the run posts the briefing and completes (no interactive close under the single-phase model). Anything Arda wants to add to the day goes onto the Board directly.

---

## 8. Feature Layer

### 8.1 Project & Task Board (replaces Todoist)
- **Standing categories (top-level classification):** every task is filed under one of five business buckets, which are the Board's tabs and the tag shown on dashboard priority items:
  1. **Wholesale Leads** — fed by the lead tracker + pipeline (follow-ups, pricing, agreements).
  2. **Consultation** — café buildouts, training, menu consults (manual).
  3. **Pop-Ups** — venue, staffing, supplies for pop-up events (manual).
  4. **Invoice Tracking** — **Accounts Receivable only**, QuickBooks-fed: invoices Boxx has *issued* to wholesale/consulting customers and whether they've been paid. Overdue receivables surface here as tasks (chase / remind / reconcile), with `qbo_invoice_id` + amount on the task. **Out of scope: Accounts Payable** (bills Boxx owes) — that's the separate *Wolverine* app; the hub does not touch A/P.
  5. **Governance** — taxes and forms (filings, licenses, W-9s, bookkeeping); mostly manual, deadline-driven.
- **Entities:** category → (optional project) → tasks. Tasks carry status/column, stage, due date, priority, assignee, activity log, and a source link (lead / email / calendar event / QuickBooks invoice / run).
- **Creation:** manual (in-app) and automatic (routines create tasks into the right category — the agenda's action items, lead follow-ups, and overdue-invoice items).
- **Views:** kanban board per category (columns by status), list view. Filter by due date, assignee, source.
- **Carry-over:** incomplete tasks persist across days and feed the agenda automatically.

### 8.2 Email Drafting
- Compose model-assisted drafts in-app (e.g., a follow-up to an overdue lead, generated from lead context).
- **From, To, and Cc are editable** in the draft UI before saving. Replies to an existing conversation carry `gmail_thread_id` and land **on the thread** in Gmail, not as a fresh compose.
- **Save as a native Gmail draft** in the correct mailbox (ardabarlas@ or hello@) via the Gmail tool, for Arda to review and send from Gmail — or **send directly from the Drafts UI** (added 2026-09-03): a Send button with a confirmation, which syncs the editor's latest content to the Gmail draft and sends it; sent drafts lock read-only. **Never auto-send** — sending is operator-only: the model-facing tool registry exposes no send capability (enforced by a test); routines can only draft.
- Drafts are linked to their lead/task and tracked in `email_drafts`.
- **Voice & tone:** drafts follow `mise-voice-and-tone.md` (derived from Arda's real sent mail). Two complete profiles keyed to the sending mailbox — Voice A (arda@, personal operator voice) and Voice B (hello@, brand front-desk voice: self-introduces, brand-plural "we", service-recovery patterns, EN/TR bilingual). The drafting prompt loads the profile matching the chosen `from_mailbox`.
- Support reusable follow-up templates (later enhancement).

---

## 9. Dashboard / UX

Pages:
- **Home** — actionable items only: a **Priority** checklist (each item tagged by category), key stats, and **Waiting-On**. Run status is **not** surfaced here — runs are reached from the Runs view. Anything from a run that needs action appears as a priority/task item instead.
- **Runs** — list of runs per routine; open a run to see its transcript **read-only** (runs are single-phase and closed — see §4). Show tool actions the model took (e.g., "created calendar reminder", "saved draft").
- **Pipeline** — leads grouped by stage, sorted by idle days; lead detail with activity log; manual edits.
- **Board** — task kanban, tabbed by the five standing categories (Wholesale Leads · Consultation · Pop-Ups · Invoice Tracking · Governance); columns by status.
- **Drafts** — composed/saved email drafts.
- **Settings** — routines (prompt, schedule, model, enabled), connector status, secrets health, auth.

Run transcript view: renders `run_messages` read-only with inline indicators of the mutations performed. (The reply composer shown in the mockup's Runs view predates the single-phase decision and is **not** built.)

### 9.1 Visual Identity

> **Mockup scope note (2026-08-27):** the mockup HTML is the **aesthetic** reference — layout, palette, type, boxed-module grammar. Its demo data is illustrative, not computed (numbers don't reconcile), and its email copy predates the voice guide; where they conflict, this spec and `mise-voice-and-tone.md` govern.

The dashboard carries Boxx's brand, not a generic admin skin. Direction: **"roaster's control room"** — brutalist-utilitarian but elegant, echoing the café's concrete/boxed aesthetic, the boxy `BOXX` wordmark, and the roastery's own data vernacular (cupping-sheet notation like `Natural · 1200 masl`, numbered blends like `Blend No: 01`). The interactive HTML reference is delivered as `boxx-routines-hub-mockups.html`; build to match it.

**Palette (CSS custom properties):**
| Token | Hex | Role |
|---|---|---|
| `--paper` | `#E8E4DB` | base background (warm concrete-paper) |
| `--panel` | `#DFDACF` | raised panels/boxes |
| `--panel-2` | `#D5CFC2` | inset / hover / selected |
| `--ink` | `#17150F` | text + 1px hairline borders |
| `--ink-soft` | `#6B6559` | secondary text |
| `--line` | `#BEB8AA` | light hairline on paper |
| `--rail` | `#17150F` | dark left nav rail |
| `--ember` | `#DC4B1B` | **signal only** — overdue, awaiting review, live/active |
| `--green` | `#4E6B3E` | confirmed / found / done (used sparingly) |

Two functional accents only (ember for attention, green for confirmation) over a monochrome concrete base. Ember is never decorative.

**Typography (Google Fonts):**
- Display — **Space Grotesk** (500/700): page titles, wordmark, big stat numbers.
- Body — **Hanken Grotesk** (400–700): names, prose, labels.
- Mono/utility — **IBM Plex Mono** (400/500): codes, timestamps, micro-labels, all data values and index markers.

**Layout & structure:**
- Dark vertical nav rail (index letters `00 / R / P / B / D / S` + mono labels) against a paper main area.
- Everything is a hard-edged **box**: 1px ink border, **0 border-radius**, flat fill, with a mono "specimen-label" header bar (label left, index code right) — the literal `BOXX` module motif.
- Kanban lanes for Pipeline and Board share the same boxed-card grammar. Data uses the cupping-sheet `A · B · C` dotted notation (e.g., `SAMPLED · 6D IDLE`).

**Signature element:** the boxed specimen-label module with a mono index code, plus reuse of the roastery's cupping/spec typography for ops data. This is the one memorable device; everything else stays quiet.

**Quality floor:** responsive to mobile (rail collapses to a top row), visible `:focus-visible` outlines in ember, `prefers-reduced-motion` respected, sentence-case UI copy in an active voice ("Save to Gmail", "Awaiting review").

---

## 10. Scheduling & Runtime Context

- APScheduler cron jobs per routine, in **America/Los_Angeles**, persistent jobstore in Postgres, plus manual "Run now."
- **Runtime context injected into every run** (the API model doesn't know the wall-clock date):
  - current datetime + timezone,
  - `last_run_at` for the routine's sources — **when a source has no `last_run_at` (the first ever run), the gather window defaults to the past 90 days (~3 months) of backlog** (`COLD_START_DAYS`), matching §7.1's cold-start reconstruction,
  - a compact state snapshot relevant to the routine (open leads for the tracker; incomplete tasks + prior-day agenda items for the agenda).
  This is what enables incremental behavior and keeps token usage down versus dumping full history.

---

## 11. Non-Goals / Dropped / Later

**Dropped (decided):**
- Google Sheet as pipeline store — replaced by hub DB.
- Todoist — replaced by internal board.

**In scope (added):**
- **QuickBooks Online** — feeds the Invoice Tracking category (overdue A/R → tasks) and the agenda's bill/balance reconciliation.

**Out of scope for v1 (candidate later features):**
- Auto-sending email.
- Slack / mobile push notifications for run-ready or overdue alerts.
- Multi-user roles/permissions.
- Shopify integration.
- Sheet export mirror (explicitly not wanted — hub DB only).
- Follow-up email template library.

---

## 12. Open Decisions / Assumptions

Items defaulted so the spec is complete:
1. **Execution model — RESOLVED 2026-08-27:** single-phase autonomous runs; runs close when they run and the dashboard is the review surface (see §4). Supersedes the earlier "conversation per run" lock.
2. **Agenda run time** defaulted to **7:30 AM** (ahead of the 8:30 tracker). Confirm or set your preferred time.
3. **Frontend** = Jinja + HTMX (recommended for a thin dashboard). Switch to React if you'd rather.
4. **Connector method** = direct Google API client via service account + domain-wide delegation (recommended over remote MCP for headless reliability).
5. **Model default** = Opus per routine, downgradable to Sonnet per routine for cost/speed.
6. **Single-user** app with a password gate.
7. **Name — decided:** **Mise** (supersedes the desktop Mise, which is retired). **RESOLVED 2026-08-27:** the desktop Mise was an unfinished app with token issues — no capability audit or data migration needed; nothing carries over.
**Resolved:** the QuickBooks A/R check is **folded into the morning agenda** (no separate routine). Invoice Tracking is A/R only and does **not** overlap Wolverine (A/P). No integration between the two.

### Design-review resolutions (2026-08-27)
- Canonical personal mailbox: **`ardabarlas@boxxcoffee.com`** ("arda@" in the voice guide refers to it).
- Voice guide **supersedes** the mockup's placeholder email copy.
- Follow-up cadence (New daily · Contacted 3d · Sampled 3/7/every-5 · Negotiating 7d) kept as-is; timers anchor to `last_confirmed_action`.
- Cold start also scans hello@'s inbox for unanswered inbound inquiries.
- QBO refresh token rotates on use → lives in Postgres; env var is bootstrap seed only; daily A/R check is the intended cadence.
- Never-send enforced at the tool layer (no send function); scopes `gmail.readonly` + `gmail.compose`.
- Runs execute as background jobs with status polling; startup sweep fails orphaned `running` runs.
- Draft From/To/Cc editable; reply drafts thread via `gmail_thread_id`; `drafting` status added.
- Task board columns fixed at `todo|doing|waiting|done` for v1; single contact per lead for v1.
- Mockup demo data is aesthetic/illustrative only.
- **2026-09-03:** operator-initiated Send added to the Drafts UI (confirm dialog, sent_at lock). The no-auto-send invariant narrows to its real intent: the model/routines can never send; only Arda can, explicitly.

---

## 13. Build Handoff Notes (for Claude Code)

**Suggested milestone order:**
1. Project scaffold: FastAPI + Postgres + auth gate + Railway deploy skeleton.
2. Data model + migrations.
3. Google service-account tool layer (Gmail read/draft, Calendar read/write) with a connectivity test.
4. Run engine + Anthropic client + runtime-context injection + `run_messages` persistence.
5. APScheduler wiring (persistent jobstore) + manual "Run now."
6. Dashboard: Runs transcript view first (unblocks testing routines end-to-end).
7. Lead Tracker routine (state-first refactor) + Pipeline page (incl. manual entry: log activity, stage changes, confirm/dismiss pending confirmations, reminders).
8. Task board + Agenda routine (writing to the board).
9. Email drafting.
10. Dedup ledger, graceful-degradation paths, polish.

**Repo sketch:**
```
app/
  main.py            # FastAPI app, auth, routes
  scheduler.py       # APScheduler setup
  engine/            # run lifecycle, Anthropic client, context injection
  tools/             # gmail.py, calendar.py, hubdb.py (model-facing tools)
  models/            # SQLAlchemy models + migrations
  routines/          # system prompts + per-routine config
  web/               # Jinja templates + HTMX
  settings.py        # env, secrets
```

**Env vars:** see §5.
