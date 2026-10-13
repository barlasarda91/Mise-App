"""Google Places API (New) text search — structured business data for lead
discovery (address, phone, website, rating, review volume, hours).

Optional: with no GOOGLE_PLACES_API_KEY the discovery routine works from web
search alone. Read-only; the key never leaves the server.
"""

import requests

from app.settings import get_settings

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

# Only the fields the agent uses — Places bills by the richest field requested.
FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.nationalPhoneNumber",
        "places.websiteUri",
        "places.googleMapsUri",
        "places.rating",
        "places.userRatingCount",
        "places.primaryType",
        "places.businessStatus",
        "places.regularOpeningHours.weekdayDescriptions",
    ]
)

# Location bias per territory: (lat, lng, radius in metres; API max 50 km).
REGION_BIAS = {
    "los_angeles": (34.0522, -118.2437, 30000),
    "san_diego": (32.7157, -117.1611, 30000),
    "san_gabriel_valley": (34.0800, -118.0500, 20000),
    "riverside": (33.9533, -117.3962, 30000),
    "santa_barbara": (34.4208, -119.6982, 25000),
}

TIMEOUT_SECONDS = 15


def places_configured() -> bool:
    return bool(get_settings().google_places_api_key)


def search_text(query: str, region: str | None = None, max_results: int = 10) -> list[dict]:
    """Text search ("specialty coffee Riverside", "24 hour diner Pasadena").
    Returns compact place dicts; raises on HTTP errors."""
    key = get_settings().google_places_api_key
    if not key:
        raise RuntimeError("GOOGLE_PLACES_API_KEY is not configured")
    body: dict = {"textQuery": query, "maxResultCount": max(1, min(max_results, 20))}
    if region in REGION_BIAS:
        lat, lng, radius = REGION_BIAS[region]
        body["locationBias"] = {
            "circle": {"center": {"latitude": lat, "longitude": lng}, "radius": float(radius)}
        }
    response = requests.post(
        SEARCH_URL,
        json=body,
        headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELD_MASK},
        timeout=TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Places API {response.status_code}: {response.text[:300]}")
    return [_compact(p) for p in response.json().get("places", [])]


def _compact(place: dict) -> dict:
    hours = (place.get("regularOpeningHours") or {}).get("weekdayDescriptions")
    return {
        "place_id": place.get("id"),
        "name": (place.get("displayName") or {}).get("text"),
        "address": place.get("formattedAddress"),
        "phone": place.get("nationalPhoneNumber"),
        "website": place.get("websiteUri"),
        "maps_url": place.get("googleMapsUri"),
        "rating": place.get("rating"),
        "review_count": place.get("userRatingCount"),
        "primary_type": place.get("primaryType"),
        "business_status": place.get("businessStatus"),
        "hours": hours,
    }
