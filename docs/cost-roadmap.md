# Cost roadmap

Goal: cut the routines' API spend meaningfully without dulling the product.
Drafting is negligible (~a few cents per draft, a handful per day); effectively
all spend is the two routines × 21 weekday runs (agenda 07:00–17:00, tracker
07:30–16:30, both Claude Opus at $5/$25 per MTok, adaptive thinking, effort
defaulting to high).

Current mechanics that drive cost (verified in code, 2026-09-10):

- `app/engine/runner.py` puts a cache breakpoint on the **system prompt only**.
  Each loop iteration re-sends the whole growing conversation as fresh input —
  on a 12-iteration run the transcript is billed ~12 times over.
- `response.usage` is discarded — no record of what any run actually cost.
- Every run executes even when nothing happened; the model itself discovers
  "no new mail" by making tool calls, which costs real tokens to learn nothing.
- Effort is unset (= high) on every run, including one-line delta updates.

## Phase 0 — instrument (SHIPPED 2026-09-10)

Store `usage` per API call on the run (new columns or a JSON on `runs`):
input / output / cache-read / cache-write tokens and computed dollars. Show
cost per run on the Runs page and a daily/weekly rollup (Settings or Today
footer). Everything after this phase gets measured against real numbers
instead of estimates, and regressions become visible the day they happen.

## Phase 1 — free wins (no quality change)

**1a. Skip-if-quiet pre-flight (SHIPPED 2026-09-10; biggest single cut).** Before starting a run,
plain code (zero tokens) checks: any new mail in either mailbox since the last
gather (`gmail.count_messages` with an after: filter)? For the tracker: any
lead newly crossing its cadence threshold since the last run? If all quiet,
record a `skipped — no new activity` run and never call the model. Intraday
hours are mostly quiet; expect roughly half to two-thirds of the 21 daily runs
to become $0. First-of-day runs always execute (calendar + A/R + inbox sweep).

**1b. Cache the conversation, not just the system prompt. (SHIPPED 2026-09-10)** Move to top-level
`cache_control: {"type": "ephemeral"}` (auto-caches the deepest prefix) or a
breakpoint on the last message each iteration. Iterations are seconds apart,
well inside the 5-minute TTL, so from iteration 2 onward almost all input
bills at ~0.1×. Verify with the Phase 0 numbers: `cache_read_input_tokens`
should dominate. Expect the input side of a run to drop by well over half.

**1c. Tool-result hygiene.** Cap Gmail body text returned to the model
(`get_gmail_message` currently returns full bodies; thread context already
caps at 2.5k chars — apply a similar cap, with a "truncated" marker, to the
run tools) and keep search results to header summaries. Runtime context is
already capped (40 leads / 25 tasks) — revisit only if Phase 0 shows it heavy.

## Phase 2 — cheap tradeoffs (quality held where it matters)

**2a. Effort by run type.** First run of the day: effort high (it writes the
briefing). Intraday delta runs: effort low or medium via
`output_config={"effort": ...}` — they mostly check mail and file a task or
two. Lower effort also means fewer, more consolidated tool calls, which
compounds with 1b.

**2b. Sonnet for intraday runs.** Keep Opus for the morning briefing (the
product's voice and judgment showcase); run intraday deltas on Claude Sonnet 5
($2/$10 — 60% cheaper per token). The `routines.model` column already exists;
this needs a per-run override (first-of-day vs later) plus a Settings toggle
so it's reversible in one click. Watch a week of intraday reports for quality
drift before considering Sonnet for mornings too.

## Phase 3 — structural (only if Phases 1–2 aren't enough)

- **1-hour cache TTL** (`cache_control: {"type": "ephemeral", "ttl": "1h"}`)
  on the tools+system prefix so consecutive hourly runs share it. Writes cost
  2× but reads 0.1×; only pays off on runs that actually execute — evaluate
  with Phase 0 data after 1a lands (skipping quiet runs reduces how often the
  prefix is reused).
- **Cadence dial**: hourly → every 2 hours intraday, if the measured value of
  hourly freshness isn't worth its cost in practice.
- **MAX_ITERATIONS / per-run task budget** guardrail so a pathological run
  can't burn a day's budget.

## Expected shape (to be replaced by Phase 0 measurements)

Rough current estimate $5–15/weekday. Phase 1 alone should land around
$2–4/day (quiet-hour skips + cached transcript); Phase 2 around $1–2/day.
Weekend spend is already $0.

## Sequencing

0 → 1a → 1b (each independently shippable, verify with the cost readout)
→ 1c → 2a → 2b → reassess with a week of real numbers before any Phase 3.
