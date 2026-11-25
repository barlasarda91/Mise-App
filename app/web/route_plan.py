"""Route planner for the Today's visits tab.

Arda says where he is and where he's headed; this orders the day's stops
between the two and lays out a timed itinerary. Ordering minimizes
straight-line driving (exact for small days, nearest-neighbour + 2-opt
beyond that); times are estimates from LA surface-street speed plus a
parking buffer, and each shop gets a recommended stay — longer where the
fit is stronger or the shop is already a Pipeline lead. The result is
stored (AppState) so the tab keeps showing today's route until it's
cleared or the day changes.
"""

import math
from datetime import datetime, timedelta
from itertools import permutations
from urllib.parse import quote
from zoneinfo import ZoneInfo

from app.db import db_session
from app.models import AppState
from app.settings import get_settings

ROUTE_STATE_KEY = "visits:route"

# LA surface streets, lights and all: ~30 km/h door to door…
DRIVE_KMH = 30.0
# …plus finding parking and walking in, per stop.
PARK_MINUTES = 6
# Exact ordering up to this many stops (8! = 40k permutations); beyond,
# nearest-neighbour with 2-opt cleanup.
EXACT_LIMIT = 8


def dwell_minutes(card: dict) -> int:
    """Recommended time inside a shop: enough to order a coffee, read the
    room and talk to whoever runs the bar. Stronger fits and shops already
    in the Pipeline earn a longer stop."""
    if card.get("status") == "approved" or card.get("lead_id"):
        return 30
    fit = card.get("fit") or 3
    if fit >= 5:
        return 30
    if fit == 4:
        return 25
    return 20


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _path_km(points: list[tuple[float, float]]) -> float:
    return sum(_haversine_km(points[i], points[i + 1]) for i in range(len(points) - 1))


def _best_order(start: tuple, end: tuple, stops: list[tuple]) -> list[int]:
    """Indices into `stops`, ordered to minimize start → … → end distance."""
    n = len(stops)
    if n <= 1:
        return list(range(n))
    if n <= EXACT_LIMIT:
        best, best_km = None, None
        for perm in permutations(range(n)):
            km = _path_km([start] + [stops[i] for i in perm] + [end])
            if best_km is None or km < best_km:
                best, best_km = list(perm), km
        return best
    # Nearest-neighbour from the start…
    todo = set(range(n))
    order: list[int] = []
    here = start
    while todo:
        nxt = min(todo, key=lambda i: _haversine_km(here, stops[i]))
        order.append(nxt)
        todo.remove(nxt)
        here = stops[nxt]
    # …then 2-opt until no swap shortens the path.
    improved = True
    while improved:
        improved = False
        for i in range(n - 1):
            for j in range(i + 1, n):
                cand = order[:i] + order[i : j + 1][::-1] + order[j + 1 :]
                if _path_km([start] + [stops[k] for k in cand] + [end]) < _path_km(
                    [start] + [stops[k] for k in order] + [end]
                ):
                    order, improved = cand, True
    return order


def _drive_minutes(km: float) -> int:
    return max(1, round(km / DRIVE_KMH * 60)) + PARK_MINUTES


def _geocode(query: str) -> dict | None:
    from app.tools.places import geocode_place

    hit = geocode_place(query, "Los Angeles area", None)
    if hit and hit.get("lat") is not None:
        return hit
    return None


def _now_la() -> datetime:
    return datetime.now(ZoneInfo(get_settings().default_tz))


def _clock(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def plan_route(start_query: str, end_query: str, depart_hhmm: str, visits: list[dict]) -> tuple[dict | None, str | None]:
    """(route, error). `visits` are today's visit cards (visit_on <= today).
    Stops without coordinates are listed on the route as unroutable rather
    than silently dropped."""
    start_query = " ".join((start_query or "").split())[:200]
    end_query = " ".join((end_query or "").split())[:200]
    if not start_query:
        return None, "Tell me where you are first."

    from app.tools.places import places_configured

    if not places_configured():
        return None, ("Google Places isn't configured — the route planner needs it "
                      "to place your start and destination.")

    stops = [dict(v) for v in visits]
    if not stops:
        return None, "Nothing on today's visit list to route."

    start = _geocode(start_query)
    if start is None:
        return None, f"Couldn't place “{start_query}” — try a fuller address."
    if end_query:
        end = _geocode(end_query)
        if end is None:
            return None, f"Couldn't place “{end_query}” — try a fuller address."
    else:
        end = dict(start, name=start.get("name"))  # loop back to the start

    # Last-chance geocode for stops still missing a pin.
    unroutable = []
    routable = []
    for v in stops:
        if v.get("lat") is None:
            hit = _geocode(f"{v['name']} {v.get('address') or v.get('city') or ''}")
            if hit:
                v["lat"], v["lng"] = hit["lat"], hit["lng"]
        (routable if v.get("lat") is not None else unroutable).append(v)
    if not routable:
        return None, "None of today's stops has a map pin — press Locate on the queue tab first."

    s_pt = (start["lat"], start["lng"])
    e_pt = (end["lat"], end["lng"])
    order = _best_order(s_pt, e_pt, [(v["lat"], v["lng"]) for v in routable])
    ordered = [routable[i] for i in order]

    # Departure time: Arda's, or the next five minutes from now.
    now = _now_la()
    try:
        h, m = depart_hhmm.split(":")
        at = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    except (ValueError, AttributeError):
        at = now + timedelta(minutes=5 - now.minute % 5)

    depart_at = at
    legs = []
    here = s_pt
    total_km = 0.0
    for v in ordered:
        km = _haversine_km(here, (v["lat"], v["lng"]))
        drive = _drive_minutes(km)
        at = at + timedelta(minutes=drive)
        stay = dwell_minutes(v)
        legs.append({
            "id": v["id"],
            "name": v["name"],
            "address": v.get("address") or v.get("city") or "",
            "drive_min": drive,
            "km": round(km, 1),
            "arrive": _clock(at),
            "stay_min": stay,
            "leave": _clock(at + timedelta(minutes=stay)),
        })
        at = at + timedelta(minutes=stay)
        total_km += km
        here = (v["lat"], v["lng"])
    last_km = _haversine_km(here, e_pt)
    total_km += last_km
    final_drive = _drive_minutes(last_km)
    at = at + timedelta(minutes=final_drive)

    # One-tap navigation: origin → waypoints in order → destination.
    waypoints = "|".join(f"{v['lat']},{v['lng']}" for v in ordered)
    maps_url = (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={quote(start.get('address') or start_query)}"
        f"&destination={quote(end.get('address') or end_query or start_query)}"
        f"&waypoints={quote(waypoints, safe='')}"
        "&travelmode=driving"
    )

    route = {
        "date": _now_la().date().isoformat(),
        "start_label": start.get("name") or start_query,
        "end_label": (end.get("name") or end_query) if end_query else "back to start",
        "depart": _clock(depart_at),
        "legs": legs,
        "final_drive_min": final_drive,
        "arrive_end": _clock(at),
        "total_km": round(total_km, 1),
        "total_mi": round(total_km * 0.621, 1),
        "maps_url": maps_url,
        "unroutable": [{"id": v["id"], "name": v["name"]} for v in unroutable],
    }
    return route, None


def save_route(route: dict) -> None:
    with db_session() as s:
        row = s.get(AppState, ROUTE_STATE_KEY)
        if row is None:
            s.add(AppState(key=ROUTE_STATE_KEY, value=route))
        else:
            row.value = route


def clear_route() -> None:
    with db_session() as s:
        row = s.get(AppState, ROUTE_STATE_KEY)
        if row is not None:
            s.delete(row)


def stored_route(visits: list[dict]) -> dict | None:
    """Today's stored route, trimmed to stops still on the visit list
    (visited/removed ones drop off; the times note says to replan)."""
    try:
        with db_session() as s:
            row = s.get(AppState, ROUTE_STATE_KEY)
            route = dict(row.value) if row is not None else None
    except Exception:
        return None
    if not route or route.get("date") != _now_la().date().isoformat():
        return None
    ids = {v["id"] for v in visits}
    legs = [l for l in route.get("legs", []) if l["id"] in ids]
    if not legs:
        return None
    route["trimmed"] = len(legs) < len(route.get("legs", []))
    route["legs"] = legs
    return route
