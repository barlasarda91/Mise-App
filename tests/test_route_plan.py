"""Route planner: ordering, timing, dwell recommendations, persistence."""

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.web.route_plan as rp
from app.models.base import Base


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

    monkeypatch.setattr(rp, "db_session", factory)
    return factory


def _visit(i, lat, lng, **over):
    base = {"id": i, "name": f"Stop {i}", "address": f"{i} Main St", "city": "LA",
            "lat": lat, "lng": lng, "fit": 3, "status": "new", "lead_id": None}
    base.update(over)
    return base


def _fake_places(monkeypatch, lookups):
    import app.tools.places as places

    monkeypatch.setattr(places, "places_configured", lambda: True)
    monkeypatch.setattr(rp, "_geocode", lambda q: lookups.get(q.strip()))


def test_dwell_recommendations():
    assert rp.dwell_minutes(_visit(1, 0, 0, fit=5)) == 30
    assert rp.dwell_minutes(_visit(1, 0, 0, fit=4)) == 25
    assert rp.dwell_minutes(_visit(1, 0, 0, fit=3)) == 20
    assert rp.dwell_minutes(_visit(1, 0, 0, fit=2, status="approved")) == 30
    assert rp.dwell_minutes(_visit(1, 0, 0, fit=2, lead_id=7)) == 30


def test_best_order_follows_the_line():
    start, end = (34.0, -118.5), (34.0, -118.0)
    # Handed in scrambled: the shortest start→end path sorts them west→east.
    stops = [(34.0, -118.1), (34.0, -118.4), (34.0, -118.25)]
    assert rp._best_order(start, end, stops) == [1, 2, 0]


def test_best_order_large_day_uses_heuristic():
    start = end = (34.0, -118.3)
    stops = [(34.0, -118.3 + 0.01 * i) for i in range(1, 11)]  # 10 stops, one line
    order = rp._best_order(start, end, stops)
    assert sorted(order) == list(range(10))
    # Out and back along a line: the outbound half must be monotonic.
    assert order[0] == 0 and order[-1] in (0, 9)


def test_plan_route_orders_times_and_links(session_factory, monkeypatch):
    _fake_places(monkeypatch, {
        "Roastery": {"name": "Boxx Roastery", "address": "Arts District", "lat": 34.03, "lng": -118.23},
        "Home": {"name": "Home", "address": "Eagle Rock", "lat": 34.14, "lng": -118.21},
    })
    visits = [
        _visit(1, 34.05, -118.25, fit=5),           # closest to start
        _visit(2, 34.12, -118.22, status="approved"),  # closest to end
        _visit(3, 34.08, -118.24),
    ]
    route, error = rp.plan_route("Roastery", "Home", "09:00", visits)
    assert error is None
    assert [l["id"] for l in route["legs"]] == [1, 3, 2]
    assert [l["stay_min"] for l in route["legs"]] == [30, 20, 30]
    # Clock runs forward and starts after 09:00.
    times = [route["depart"]] + [l["arrive"] for l in route["legs"]] + [route["arrive_end"]]
    assert times[0] == "09:00" and times == sorted(times)
    assert route["legs"][0]["drive_min"] >= rp.PARK_MINUTES + 1
    # The nav link carries the waypoints in route order.
    assert "origin=Arts%20District" in route["maps_url"]
    assert "destination=Eagle%20Rock" in route["maps_url"]
    w = route["maps_url"].split("waypoints=")[1].split("&")[0]
    assert w.index("34.05") < w.index("34.08") < w.index("34.12")
    assert route["unroutable"] == []


def test_plan_route_blank_destination_loops_back(session_factory, monkeypatch):
    _fake_places(monkeypatch, {
        "Roastery": {"name": "Boxx Roastery", "address": "Arts District", "lat": 34.03, "lng": -118.23},
    })
    route, error = rp.plan_route("Roastery", "", "09:00", [_visit(1, 34.05, -118.25)])
    assert error is None and route["end_label"] == "back to start"


def test_plan_route_errors(session_factory, monkeypatch):
    _fake_places(monkeypatch, {})
    assert rp.plan_route("", "", "", [_visit(1, 34, -118)])[1].startswith("Tell me where")
    assert "route" in rp.plan_route("Roastery", "", "", [])[1]
    assert "Couldn't place" in rp.plan_route("Nowhere", "", "", [_visit(1, 34, -118)])[1]


def test_plan_route_lists_unroutable_stop(session_factory, monkeypatch):
    _fake_places(monkeypatch, {
        "Roastery": {"name": "R", "address": "A", "lat": 34.03, "lng": -118.23},
    })
    visits = [_visit(1, 34.05, -118.25), _visit(2, None, None, name="No Pin Cafe")]
    route, error = rp.plan_route("Roastery", "", "09:00", visits)
    assert error is None
    assert [l["id"] for l in route["legs"]] == [1]
    assert route["unroutable"] == [{"id": 2, "name": "No Pin Cafe"}]


def test_stored_route_trims_and_expires(session_factory, monkeypatch):
    _fake_places(monkeypatch, {
        "Roastery": {"name": "R", "address": "A", "lat": 34.03, "lng": -118.23},
    })
    visits = [_visit(1, 34.05, -118.25), _visit(2, 34.08, -118.24)]
    route, _ = rp.plan_route("Roastery", "", "09:00", visits)
    rp.save_route(route)

    got = rp.stored_route(visits)
    assert got and len(got["legs"]) == 2 and got["trimmed"] is False
    # One stop visited → it drops off and the note flag flips.
    got = rp.stored_route(visits[:1])
    assert [l["id"] for l in got["legs"]] == [1] and got["trimmed"] is True
    # Every stop gone → no route.
    assert rp.stored_route([]) is None
    # Yesterday's route never shows.
    stale = dict(route, date="2020-01-01")
    rp.save_route(stale)
    assert rp.stored_route(visits) is None
    rp.clear_route()
    with session_factory() as s:
        from app.models import AppState

        assert s.get(AppState, rp.ROUTE_STATE_KEY) is None
