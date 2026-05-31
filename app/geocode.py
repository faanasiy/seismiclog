"""Nominatim wrapper + soil-class regional lookup.

Stdlib ``urllib.request`` only.  Politeness: ``time.sleep(1.0)`` before
each call.  On any network error or empty result we fall back to a
small offline table covering the three demo addresses.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional


log = logging.getLogger("seismiclog.geocode")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "seismiclog-demo/0.1"
TIMEOUT_S = 8.0


# ---------- Offline geocoder ----------

_OFFLINE_TABLE: list[dict] = [
    {
        "match": ["san francisco", "sf", "san francisco, ca", "san francisco, california"],
        "lat": 37.7749,
        "lng": -122.4194,
        "display_name": "San Francisco, California, USA",
        "country_code": "us",
        "state": "California",
        "city": "San Francisco",
    },
    {
        "match": ["tokyo", "tokyo, japan", "東京"],
        "lat": 35.6762,
        "lng": 139.6503,
        "display_name": "Tokyo, Japan",
        "country_code": "jp",
        "state": "Tokyo",
        "city": "Tokyo",
    },
    {
        "match": ["reykjavik", "reykjavík", "reykjavik, iceland"],
        "lat": 64.1466,
        "lng": -21.9426,
        "display_name": "Reykjavik, Iceland",
        "country_code": "is",
        "state": "Capital Region",
        "city": "Reykjavik",
    },
]


class GeocodeResult(dict):
    """Plain dict carrying ``lat, lng, display_name, country_code, state, city``."""


class GeocodeError(Exception):
    """Raised when no result can be obtained, including offline."""


def _offline_lookup(query: str) -> Optional[GeocodeResult]:
    q = query.strip().lower()
    for entry in _OFFLINE_TABLE:
        for needle in entry["match"]:
            if needle in q:
                return GeocodeResult({
                    "lat": entry["lat"],
                    "lng": entry["lng"],
                    "display_name": entry["display_name"],
                    "country_code": entry["country_code"],
                    "state": entry["state"],
                    "city": entry["city"],
                })
    return None


def geocode(address: str, demo_offline: bool = False) -> GeocodeResult:
    """Resolve an address to ``GeocodeResult``.

    Order: offline-mode short-circuit → Nominatim → offline fallback.
    """
    if demo_offline:
        hit = _offline_lookup(address)
        if hit is None:
            raise GeocodeError("Geocoding is offline and the address is not in the demo list.")
        return hit

    # Politeness sleep before each external call.  Single-user demo:
    # a hard 1-second wait is acceptable.
    try:
        time.sleep(1.0)
        params = {
            "q": address,
            "format": "json",
            "limit": "1",
            "addressdetails": "1",
        }
        url = f"{NOMINATIM_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read()
        arr = json.loads(raw.decode("utf-8"))
        if not arr:
            raise GeocodeError("Address not found.")
        first = arr[0]
        addr = first.get("address") or {}
        return GeocodeResult({
            "lat": float(first["lat"]),
            "lng": float(first["lon"]),
            "display_name": first.get("display_name") or address,
            "country_code": (addr.get("country_code") or "").lower(),
            "state": addr.get("state") or "",
            "city": addr.get("city") or addr.get("town") or addr.get("village") or "",
        })
    except GeocodeError:
        raise
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError) as exc:
        log.warning("nominatim.unreachable: %s", exc)
        hit = _offline_lookup(address)
        if hit is None:
            raise GeocodeError(
                "Geocoder unreachable and address is not in the offline demo list."
            ) from exc
        return hit


# ---------- Soil-class lookup ----------

_SOIL_TABLE: dict[tuple[str, str], tuple[str, str]] = {
    ("us", "california"): ("D", "180–360"),
    ("us", "washington"): ("D", "180–360"),
    ("us", "alaska"):     ("C", "360–760"),
}

_SOIL_BY_COUNTRY: dict[str, tuple[str, str]] = {
    "jp": ("D", "180–360"),
    "is": ("B", "760–1500"),
    "it": ("C", "360–760"),
    "tr": ("D", "180–360"),
    "cl": ("C", "360–760"),
    "nz": ("D", "180–360"),
    "mx": ("D", "180–360"),
}

_SOIL_FALLBACK = ("C", "360–760")

_BAND_LABEL = {
    "B": "stiff rock",
    "C": "very dense / stiff soil",
    "D": "soft soil",
    "E": "very soft soil",
}


def soil_class(country_code: str, state: str) -> str:
    """Return the human label, e.g. ``'D — soft soil / Vs30 180–360 m/s'``."""
    cc = (country_code or "").lower()
    st = (state or "").lower()
    key = (cc, st)
    if key in _SOIL_TABLE:
        cls, band = _SOIL_TABLE[key]
    elif cc in _SOIL_BY_COUNTRY:
        cls, band = _SOIL_BY_COUNTRY[cc]
    else:
        cls, band = _SOIL_FALLBACK
    label = _BAND_LABEL.get(cls, "soil")
    return f"{cls} — {label} / Vs30 {band} m/s"


def build_region_string(geo: GeocodeResult) -> str:
    """Build a ``'<city or state>, <country>'`` region string for the prompt."""
    city = geo.get("city") or geo.get("state") or ""
    # Map common country codes to readable country names; if not known,
    # fall back to display_name's last comma-separated chunk.
    country_map = {
        "us": "USA",
        "jp": "Japan",
        "is": "Iceland",
        "it": "Italy",
        "tr": "Turkey",
        "cl": "Chile",
        "nz": "New Zealand",
        "mx": "Mexico",
        "gb": "United Kingdom",
        "de": "Germany",
        "fr": "France",
        "cn": "China",
        "in": "India",
        "id": "Indonesia",
        "ph": "Philippines",
        "ru": "Russia",
        "ca": "Canada",
        "au": "Australia",
        "pe": "Peru",
        "gr": "Greece",
    }
    cc = (geo.get("country_code") or "").lower()
    country = country_map.get(cc)
    if not country:
        # Best-effort: take the tail of the display name.
        dn = geo.get("display_name") or ""
        parts = [p.strip() for p in dn.split(",") if p.strip()]
        country = parts[-1] if parts else ""
    if city and country:
        return f"{city}, {country}"
    return geo.get("display_name") or "unknown region"
