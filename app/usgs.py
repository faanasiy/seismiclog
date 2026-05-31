"""USGS Earthquake API client + offline fall-through.

stdlib ``urllib.request`` only.  On any network/parse error, callers
fall through to the seed dataset.  Per spec §3.1: 8 second timeout,
WARNING log ``usgs.unreachable: <reason>``.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional


log = logging.getLogger("seismiclog.usgs")

USGS_BASE = "https://earthquake.usgs.gov/fdsnws/event/1/query"
USER_AGENT = "seismiclog-demo/0.1 (+https://example.invalid)"
TIMEOUT_S = 8.0
UTC = timezone.utc


def _iso(dt: datetime) -> str:
    """ISO 8601 without microseconds, without timezone suffix
    (USGS expects naive UTC)."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def fetch_events(
    starttime: datetime,
    endtime: Optional[datetime] = None,
    minmagnitude: float = 2.5,
    minlatitude: Optional[float] = None,
    maxlatitude: Optional[float] = None,
    minlongitude: Optional[float] = None,
    maxlongitude: Optional[float] = None,
) -> list[dict]:
    """Hit USGS and return a list of parsed event dicts.

    Each dict matches the Event ORM column names so the caller can
    upsert directly.  Raises ``RuntimeError`` on any failure; callers
    are expected to catch it and fall through to seeded data.
    """
    params = {
        "format": "geojson",
        "starttime": _iso(starttime),
        "minmagnitude": str(minmagnitude),
    }
    if endtime is not None:
        params["endtime"] = _iso(endtime)
    if minlatitude is not None:
        params["minlatitude"] = str(minlatitude)
    if maxlatitude is not None:
        params["maxlatitude"] = str(maxlatitude)
    if minlongitude is not None:
        params["minlongitude"] = str(minlongitude)
    if maxlongitude is not None:
        params["maxlongitude"] = str(maxlongitude)

    url = f"{USGS_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError) as exc:
        log.warning("usgs.unreachable: %s", exc)
        raise RuntimeError(f"USGS unreachable: {exc}") from exc

    features = data.get("features") or []
    parsed: list[dict] = []
    for feat in features:
        try:
            props = feat.get("properties") or {}
            geom = feat.get("geometry") or {}
            mag = props.get("mag")
            if mag is None:
                continue  # spec: skip feature if magnitude is null
            coords = geom.get("coordinates") or []
            if len(coords) < 2:
                continue
            lng = float(coords[0])
            lat = float(coords[1])
            depth = float(coords[2]) if len(coords) > 2 and coords[2] is not None else 10.0
            time_ms = int(props.get("time") or 0)
            place = (props.get("place") or "")[:200]
            usgs_id = feat.get("id")
            if not usgs_id:
                continue
            parsed.append({
                "usgs_id": str(usgs_id),
                "occurred_at": datetime.fromtimestamp(time_ms / 1000.0, tz=UTC),
                "lat": lat,
                "lng": lng,
                "depth_km": depth,
                "magnitude": float(mag),
                "place": place,
                "source": "usgs",
            })
        except (TypeError, ValueError, KeyError) as exc:
            log.warning("usgs.parse_skip: %s", exc)
            continue
    return parsed


def usgs_event_url(usgs_id: str) -> str:
    """Compose the canonical event-page URL."""
    return f"https://earthquake.usgs.gov/earthquakes/eventpage/{usgs_id}"
