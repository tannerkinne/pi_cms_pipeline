"""
Thin wrapper around the Google Places API (New) for PI firm discovery.

Auth: requires GOOGLE_PLACES_API_KEY env var. The key must have "Places API (New)"
enabled in Google Cloud Console — NOT the older "Places API", which is a different
product. Enable it at: Cloud Console → APIs & Services → Library → "Places API (New)".

Endpoint used:
  POST https://places.googleapis.com/v1/places:searchText

Field mask is enforced on every request — only billing the fields we actually use.
Requesting additional fields (photos, ratings, etc.) silently increases cost.

All functions return plain dicts/lists and never raise — callers check for empty results.
"""
import os
import time
import requests

from . import cost_tracker

PLACES_BASE = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = "places.displayName,places.formattedAddress,places.websiteUri,places.id,nextPageToken"


def _api_key() -> str:
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not key:
        raise RuntimeError("GOOGLE_PLACES_API_KEY environment variable is not set.")
    return key


def _parse_city_state(formatted_address: str) -> tuple:
    """
    Extract city and state abbreviation from a Google Places formattedAddress.
    Typical format: "123 Main St, Newark, NJ 07102, USA"
    Returns ("Newark", "NJ") or ("", "") on parse failure.
    """
    parts = [p.strip() for p in formatted_address.split(",")]
    if len(parts) < 3:
        return "", ""
    city = parts[-3] if len(parts) >= 4 else parts[0]
    # State + zip in one segment, e.g. "NJ 07102"
    state_zip = parts[-2].strip()
    state_abbr = state_zip.split()[0] if state_zip else ""
    return city, state_abbr


# Map state abbreviation → full lowercase name used in TARGET_STATES
_STATE_ABBR_TO_NAME = {
    "NJ": "new jersey",
    "NY": "new york",
    "PA": "pennsylvania",
    "CT": "connecticut",
}


def search_pi_firms(state_name: str, bbox: dict, text_query: str,
                    page_token: str = None, timeout: int = 20) -> dict:
    """
    Run one page of a Google Places Text Search within a state bounding box.

    Args:
        state_name: e.g. "new jersey" — used only for logging context
        bbox: {"sw": (lat, lng), "ne": (lat, lng)}
        text_query: e.g. "personal injury law firm"
        page_token: continuation token from a previous call (for pagination)

    Returns:
        {"places": [list of place dicts], "next_page_token": str or None}
        Returns {"places": [], "next_page_token": None} on any failure.
    """
    payload = {
        "textQuery": text_query,
        "includedType": "lawyer",
        "locationRestriction": {
            "rectangle": {
                "low":  {"latitude": bbox["sw"][0], "longitude": bbox["sw"][1]},
                "high": {"latitude": bbox["ne"][0], "longitude": bbox["ne"][1]},
            }
        },
        "pageSize": 20,
    }
    if page_token:
        payload["pageToken"] = page_token

    headers = {
        "X-Goog-Api-Key": _api_key(),
        "X-Goog-FieldMask": FIELD_MASK,
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(PLACES_BASE, headers=headers, json=payload, timeout=timeout)
        if resp.status_code != 200:
            print(f"  [places] search failed for '{state_name}' / '{text_query}': "
                  f"HTTP {resp.status_code} {resp.text[:200]}")
            cost_tracker.record(
                "google_places", "places:searchText", success=False,
                error_message=f"HTTP {resp.status_code}", results_returned=0,
            )
            return {"places": [], "next_page_token": None}
        data = resp.json()
        places = data.get("places", [])
        cost_tracker.record(
            "google_places", "places:searchText", success=True,
            results_returned=len(places),
        )
        return {
            "places": places,
            "next_page_token": data.get("nextPageToken"),
        }
    except requests.RequestException as e:
        print(f"  [places] request error for '{state_name}' / '{text_query}': {e}")
        cost_tracker.record(
            "google_places", "places:searchText", success=False,
            error_message=str(e), results_returned=0,
        )
        return {"places": [], "next_page_token": None}


def extract_firm_fields(place: dict) -> dict:
    """
    Normalize a raw Google Places result into the fields firms_raw.csv expects.
    Returns None if the place has no website (no domain = no join key).
    """
    website = place.get("websiteUri", "").strip()
    if not website:
        return None

    name = (place.get("displayName") or {}).get("text", "").strip()
    address = place.get("formattedAddress", "")
    city, state_abbr = _parse_city_state(address)
    state_full = _STATE_ABBR_TO_NAME.get(state_abbr, state_abbr.lower())

    return {
        "place_id": place.get("id", ""),
        "firm_name": name,
        "website": website,
        "city": city,
        "state": state_full,
    }


def polite_sleep(seconds: float = 0.1):
    time.sleep(seconds)
