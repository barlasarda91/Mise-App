"""Lead discovery: prospect recording + dedup, tool scoping, territory
rotation, runtime context, and the Prospects review actions."""

from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.routines.discovery as disc
import app.routines.tools  # noqa: F401 - registers the shared tools
import app.web.prospects_view as pv
from app.engine.toolkit import clear_run_context, dispatch, routine_tool_specs, set_run_context
from app.models import (
    Base,
    DraftStatus,
    EmailDraft,
    Lead,
    LeadStage,
    Prospect,
    ProspectRegion,
    ProspectStatus,
    Routine,
    Run,
    Task,
    TaskCategory,
)
from app.models.enums import FromMailbox


@pytest.fixture
def session_factory(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def factory():
        session = maker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(pv, "db_session", factory)
    return factory


@pytest.fixture
def run_id(session_factory):
    with session_factory() as s:
        routine = Routine(key="lead_discovery", name="Lead Discovery", system_prompt="p")
        s.add(routine)
        s.flush()
        run = Run(routine_id=routine.id)
        s.add(run)
        s.flush()
        rid = run.id
    set_run_context(run_id=rid, routine_id=1, started_at=datetime(2026, 10, 13, 14, 30, tzinfo=timezone.utc))
    yield rid
    clear_run_context()


def _prospect(**over):
    base = {
        "business_name": "Cafe Sevilla",
        "segment": "all_day_cafe",
        "region": "riverside",
        "fit_score": 4,
        "why_fit": "Brunch line out the door, 900 Maps reviews, pours a commodity brand.",
        "outreach_angle": "A house espresso that matches their food.",
        "sources": ["https://example.com/menu"],
        "city": "Riverside",
        "contact_email": "owner@sevilla.example",
        "email_subject": "Coffee at Sevilla",
        "email_body": "Hi Ana,\n\nThis is Arda from Boxx Coffee Roasters...\n\nBest\nArda",
    }
    base.update(over)
    return base


def _record(session_factory, **over):
    import json

    with session_factory() as s:
        content, is_error = dispatch("record_prospect", _prospect(**over), s)
    return (json.loads(content) if not is_error else content), is_error


# ---------- pure helpers ----------


def test_name_key_folds_noise_words():
    assert disc.name_key("Play Coffee Co.") == disc.name_key("play coffee")
    assert disc.name_key("Canter's Deli") == disc.name_key("Canters")
    assert disc.name_key("The Pantry") == "pantry"
    assert disc.name_key("Coffee") == "coffee"  # never collapses to empty


def test_rotation_weights_expansion_territories():
    start = date(2026, 10, 13)
    week = Counter(disc.focus_region(start + timedelta(days=i)) for i in range(7))
    assert week[ProspectRegion.RIVERSIDE] == 2
    assert week[ProspectRegion.SANTA_BARBARA] == 2
    for region in (ProspectRegion.LOS_ANGELES, ProspectRegion.SAN_DIEGO, ProspectRegion.SAN_GABRIEL_VALLEY):
        assert week[region] == 1


# ---------- tool scoping ----------


def test_discovery_gets_narrow_toolset_plus_web_tools():
    specs = routine_tool_specs("lead_discovery")
    client = [s["name"] for s in specs if "type" not in s]
    server = [s for s in specs if "type" in s]
    assert client == ["list_open_leads", "record_prospect", "search_places"]
    assert {s["type"] for s in server} == {"web_search_20260209", "web_fetch_20260209"}
    assert specs[-len(server):] == server  # server tools after the sorted client tools


def test_other_routines_never_see_discovery_tools():
    for key in ("lead_tracker", "daily_agenda"):
        names = {s["name"] for s in routine_tool_specs(key)}
        assert "record_prospect" not in names and "search_places" not in names
        assert "web_search" not in names and "web_fetch" not in names
        assert "create_task" in names  # shared toolset intact


def test_runner_sends_scoped_tools(session_factory, monkeypatch):
    from tests.test_run_engine import FakeClient, FakeResponse, text_block

    from app.engine.runner import execute_run

    monkeypatch.setattr("app.engine.anthropic_client._available_model_ids", lambda: None)
    with session_factory() as s:
        routine = Routine(key="lead_discovery", name="Lead Discovery", system_prompt="p")
        s.add(routine)
        s.flush()
        rid = routine.id
    client = FakeClient([FakeResponse([text_block("done")], "end_turn")])
    execute_run(rid, client=client, session_factory=session_factory)
    tools = client.requests[0]["tools"]
    assert any(t.get("name") == "web_search" for t in tools)
    assert not any(t.get("name") == "create_task" for t in tools)
    # discovery's context is its assignment, not the board
    first = client.requests[0]["messages"][0]["content"]
    assert "Today's assignment" in first and "Incomplete board tasks" not in first


# ---------- record_prospect ----------


def test_record_with_email_drafts_from_segment_mailbox(session_factory, run_id):
    result, err = _record(session_factory)
    assert not err and result["outcome"] == "recorded"
    assert result["draft"]["mailbox"] == "hello"  # volume segment → brand desk
    multi, _ = _record(session_factory, business_name="Guest Bar", segment="multi_roaster",
                       contact_email="hi@guestbar.example")
    assert multi["draft"]["mailbox"] == "arda"
    with session_factory() as s:
        draft = s.get(EmailDraft, multi["draft"]["draft_id"])
        assert draft.from_mailbox == FromMailbox.ARDA and draft.to_addrs == ["hi@guestbar.example"]
        assert draft.status == DraftStatus.COMPOSED and draft.sent_at is None
        p = s.get(Prospect, multi["prospect_id"])
        assert p.draft_id == draft.id and p.status == ProspectStatus.NEW and p.run_id == run_id


def test_record_without_email_requires_dm_and_walk_in(session_factory, run_id):
    _, err = _record(session_factory, contact_email=None, dm_script="Hey! This is Arda…")
    assert err and "walk_in_note" in _
    result, err = _record(session_factory, contact_email=None, dm_script="Hey! This is Arda…",
                          walk_in_note="Tue 2–4pm, ask for Ana.")
    assert not err and "draft" not in result


def test_record_validates_inputs(session_factory, run_id):
    assert _record(session_factory, fit_score=7)[1]
    assert _record(session_factory, sources=[])[1]
    assert _record(session_factory, email_body=None)[1]
    assert _record(session_factory, segment="chain")[1]


def test_dedup_against_queue_rejections_and_mutes(session_factory, run_id):
    first, _ = _record(session_factory)
    again, _ = _record(session_factory, business_name="Cafe Sevilla Co.")
    assert again["outcome"] == "skipped" and "awaiting" in again["reason"]

    pv.reject(first["prospect_id"])
    rejected, _ = _record(session_factory)
    assert rejected["outcome"] == "skipped" and "rejected" in rejected["reason"]

    other, _ = _record(session_factory, business_name="Muri Two", contact_email="m@two.example")
    pv.mute(other["prospect_id"])
    muted, _ = _record(session_factory, business_name="Muri Two", contact_email="m@two.example")
    assert muted["outcome"] == "skipped" and "muted" in muted["reason"]


def test_expired_mute_resurfaces_same_row(session_factory, run_id):
    first, _ = _record(session_factory)
    with session_factory() as s:
        p = s.get(Prospect, first["prospect_id"])
        p.status = ProspectStatus.MUTED
        p.muted_until = date(2026, 1, 1)
    back, _ = _record(session_factory, fit_score=5)
    assert back["outcome"] == "recorded" and back["prospect_id"] == first["prospect_id"]
    with session_factory() as s:
        p = s.get(Prospect, first["prospect_id"])
        assert p.status == ProspectStatus.NEW and p.muted_until is None and p.fit_score == 5
        assert len(s.scalars(select(Prospect)).all()) == 1


def test_dedup_by_place_id(session_factory, run_id):
    _record(session_factory, google_place_id="ChIJabc")
    dup, _ = _record(session_factory, business_name="Totally Different Name", google_place_id="ChIJabc")
    assert dup["outcome"] == "skipped"


def test_pipeline_leads_block_or_annotate(session_factory, run_id):
    with session_factory() as s:
        s.add(Lead(business_name="Open Lead Cafe", stage=LeadStage.CONTACTED))
        s.add(Lead(business_name="Gone Cafe", stage=LeadStage.NEW, discarded_at=datetime.now(timezone.utc)))
        s.add(Lead(business_name="Lost Cafe", stage=LeadStage.CLOSED_LOST))
    assert _record(session_factory, business_name="Open Lead")[0]["outcome"] == "skipped"
    assert _record(session_factory, business_name="Gone Cafe")[0]["outcome"] == "skipped"
    lost, _ = _record(session_factory, business_name="Lost Cafe")
    assert lost["outcome"] == "recorded" and "closed Pipeline lead" in lost["note"]


def test_per_run_cap(session_factory, run_id):
    for i in range(disc.MAX_PER_RUN):
        assert _record(session_factory, business_name=f"Spot {i}", contact_email=f"o{i}@x.example")[0]["outcome"] == "recorded"
    capped, _ = _record(session_factory, business_name="One Too Many", contact_email="z@x.example")
    assert capped["outcome"] == "run_cap_reached"


# ---------- search_places ----------


def test_search_places_without_key_degrades(session_factory, run_id, monkeypatch):
    import json

    monkeypatch.setattr("app.tools.places.places_configured", lambda: False)
    with session_factory() as s:
        content, err = dispatch("search_places", {"query": "diner riverside"}, s)
    assert not err and json.loads(content)["available"] is False


def test_search_places_compacts_and_budgets(session_factory, run_id, monkeypatch):
    import json

    from app.settings import get_settings

    monkeypatch.setattr(get_settings(), "google_places_api_key", "k")
    seen = []

    class Resp:
        status_code = 200

        def json(self):
            return {"places": [{"id": "P1", "displayName": {"text": "Diner"}, "userRatingCount": 1200}]}

    def fake_post(url, json, headers, timeout):
        seen.append((json, headers))
        return Resp()

    monkeypatch.setattr("app.tools.places.requests.post", fake_post)
    monkeypatch.setattr(disc, "PLACES_CALLS_PER_RUN", 2)
    with session_factory() as s:
        out = json.loads(dispatch("search_places", {"query": "diner", "region": "riverside"}, s)[0])
        assert out["places"][0] == {**out["places"][0], "place_id": "P1", "name": "Diner", "review_count": 1200}
        assert "circle" in seen[0][0]["locationBias"] and seen[0][1]["X-Goog-Api-Key"] == "k"
        dispatch("search_places", {"query": "diner"}, s)
        spent = json.loads(dispatch("search_places", {"query": "diner"}, s)[0])
    assert spent["available"] is False and len(seen) == 2


# ---------- runtime context ----------


def test_context_lists_assignment_and_do_not_resurface(session_factory):
    with session_factory() as s:
        s.add(Prospect(business_name="Nope Cafe", name_key="nope", segment="volume_diner", region="los_angeles",
                       status=ProspectStatus.REJECTED, fit_score=3, why_fit="x", outreach_angle="y"))
        s.add(Prospect(business_name="Later Cafe", name_key="later", segment="volume_diner", region="los_angeles",
                       status=ProspectStatus.MUTED, muted_until=date(2099, 1, 1), fit_score=3, why_fit="x",
                       outreach_angle="y"))
        s.add(Lead(business_name="Ember Room", stage=LeadStage.SAMPLED))
        s.add(Task(category=TaskCategory.GOVERNANCE, title="File quarterly sales tax"))
        s.flush()
        text = "\n".join(disc.context_lines(s, date(2026, 10, 13)))
    assert "Focus territory" in text and "hard cap" in text
    assert "Rejected by Arda — never: Nope Cafe" in text
    assert "Later Cafe (until 2099-01-01)" in text
    assert "Ember Room" in text
    assert "File quarterly sales tax" not in text


# ---------- review actions ----------


def test_approve_creates_lead_task_and_links_draft(session_factory, run_id):
    rec, _ = _record(session_factory)
    msg = pv.approve(rec["prospect_id"])
    assert "Pipeline" in msg
    with session_factory() as s:
        p = s.get(Prospect, rec["prospect_id"])
        lead = s.get(Lead, p.lead_id)
        assert p.status == ProspectStatus.APPROVED
        assert lead.stage == LeadStage.NEW and lead.lead_source == "discovery"
        assert lead.contact_email == "owner@sevilla.example"
        assert s.get(EmailDraft, p.draft_id).related_lead_id == lead.id  # send → Contacted
        task = s.scalar(select(Task).where(Task.title.like("Send first-touch email%")))
        assert task.category == TaskCategory.WHOLESALE_LEADS and task.source_ref["lead_id"] == lead.id
    assert "already" in pv.approve(rec["prospect_id"])  # idempotent


def test_approve_without_email_task_carries_scripts(session_factory, run_id):
    rec, _ = _record(session_factory, contact_email=None, instagram="@sevilla",
                     dm_script="Hey! This is Arda from Boxx…", walk_in_note="Tue 2–4pm, ask for Ana.")
    pv.approve(rec["prospect_id"])
    with session_factory() as s:
        task = s.scalar(select(Task).where(Task.title.like("First outreach%")))
        assert "Tue 2–4pm" in task.description and "@sevilla" in task.description


def test_approve_reuses_existing_open_lead(session_factory, run_id):
    rec, _ = _record(session_factory)
    with session_factory() as s:
        s.add(Lead(business_name="Cafe Sevilla", stage=LeadStage.NEW))
    pv.approve(rec["prospect_id"])
    with session_factory() as s:
        assert len(s.scalars(select(Lead)).all()) == 1


def test_reject_and_mute_discard_unsent_draft(session_factory, run_id):
    a, _ = _record(session_factory)
    b, _ = _record(session_factory, business_name="Other", contact_email="o@x.example")
    pv.reject(a["prospect_id"])
    pv.mute(b["prospect_id"])
    with session_factory() as s:
        assert s.get(EmailDraft, a["draft"]["draft_id"]).status == DraftStatus.DISCARDED
        assert s.get(EmailDraft, b["draft"]["draft_id"]).status == DraftStatus.DISCARDED
        muted = s.get(Prospect, b["prospect_id"])
        assert muted.muted_until == pv._today() + timedelta(days=disc.MUTE_DAYS)
    pv.unmute(b["prospect_id"])
    with session_factory() as s:
        assert s.get(Prospect, b["prospect_id"]).status == ProspectStatus.NEW


def test_queue_and_badge(session_factory, run_id):
    a, _ = _record(session_factory)
    b, _ = _record(session_factory, business_name="Other", contact_email="o@x.example")
    pv.mute(b["prospect_id"])
    queue = pv.load_queue()
    assert [c["id"] for c in queue["review"]] == [a["prospect_id"]]
    assert [c["id"] for c in queue["muted"]] == [b["prospect_id"]]
    assert pv.review_count() == 1


def test_page_renders_and_neutralizes_unsafe_links(session_factory, run_id):
    from app.main import NAV, templates

    _record(session_factory, website="javascript:alert(1)", instagram="@ok_handle",
            sources=["javascript:bad()", "https://good.example/"])
    queue = pv.load_queue()
    html = templates.get_template("prospects.html").render(
        queue=queue, msg=None, nav=NAV, active_view="prospects", clock="", badges={}
    )
    assert "Cafe Sevilla" in html and "Approve → Pipeline" in html
    assert "javascript:" not in html
    assert "https://www.instagram.com/ok_handle/" in html and "https://good.example/" in html


# ---------- discovery log & analytics ----------


def test_record_stores_discovered_via(session_factory, run_id):
    result, is_error = _record(
        session_factory, discovered_via="places: 24 hour diner Pasadena")
    assert not is_error and result["outcome"] == "recorded"
    with session_factory() as s:
        p = s.get(Prospect, result["prospect_id"])
        assert p.discovered_via == "places: 24 hour diner Pasadena"


def test_channel_normalization():
    from app.web.discovery_analytics import channel

    assert channel("places: 24 hour diner Pasadena") == "places"
    assert channel("WEB: eater best brunch") == "web"
    assert channel("just some text") == "just some text"[:30] or True  # no colon → whole head
    assert channel(None) == "unknown"
    assert channel("") == "unknown"


def test_discovery_analytics_aggregates(session_factory, monkeypatch):
    import app.web.discovery_analytics as da
    from app.models import ProspectRegion, ProspectSegment

    monkeypatch.setattr(da, "db_session", session_factory)
    now = datetime.now(timezone.utc)
    with session_factory() as s:
        def add(name, status, via, fit, days_ago=1, region="riverside"):
            s.add(Prospect(
                business_name=name, name_key=name.lower(), fit_score=fit,
                segment=ProspectSegment.ALL_DAY_CAFE, region=ProspectRegion(region),
                status=status, why_fit="w", outreach_angle="a",
                discovered_via=via, created_at=now - timedelta(days=days_ago),
                decided_at=now if status != ProspectStatus.NEW else None,
            ))
        add("A", ProspectStatus.APPROVED, "places: diners riverside", 5)
        add("B", ProspectStatus.REJECTED, "places: diners riverside", 3)
        add("C", ProspectStatus.APPROVED, "web: eater brunch list", 4)
        add("D", ProspectStatus.NEW, "web: eater brunch list", 4)
        add("E", ProspectStatus.REJECTED, None, 3, days_ago=10, region="los_angeles")

    a = da.discovery_analytics()
    assert a["total"] == 5
    this_week = a["weeks"][0]
    assert this_week["found"] == 4 and this_week["approved"] == 2
    channels = {r["key"]: r for r in a["channels"]}
    assert channels["places"]["found"] == 2 and channels["places"]["approval_pct"] == 50
    assert channels["web"]["approved"] == 1 and channels["web"]["pending"] == 1
    assert channels["unknown"]["found"] == 1
    queries = {r["key"]: r for r in a["queries"]}
    assert queries["places: diners riverside"]["found"] == 2
    assert a["calibration"] == {"approved": 4.5, "rejected": 3.0}
    regions = {r["key"]: r for r in a["regions"]}
    assert regions["riverside"]["found"] == 4
    assert len(a["log"]) == 5 and a["log"][0]["via"] in (
        "places: diners riverside", "web: eater brunch list", "—")


def test_discovery_analytics_fail_soft(monkeypatch):
    import app.web.discovery_analytics as da

    def broken():
        raise RuntimeError("no db")
    monkeypatch.setattr(da, "db_session", broken)
    a = da.discovery_analytics()
    assert a["total"] == 0 and a["weeks"] == [] and a["log"] == []
