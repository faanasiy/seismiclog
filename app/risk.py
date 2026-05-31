"""Risk computation: haversine, per-watch metrics, severity ramp.

The probability heuristic and the soil-class lookup are tagged as
heuristics in the UI; we do not try to fake a real hazard model.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import ai, geocode
from .models import Event, RiskAssessment, Watch


UTC = timezone.utc
EARTH_R_KM = 6371.0


# ---------- Geometry ----------

def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return the great-circle distance between two lat/lng pairs, in km."""
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return 2 * EARTH_R_KM * asin(sqrt(a))


def bbox_around(lat: float, lng: float, dlat_deg: float = 1.0) -> tuple[float, float, float, float]:
    """Square-ish bbox of ``±dlat_deg`` latitude, ``±dlat_deg / cos(lat)``
    longitude.  Used as a cheap pre-filter before the haversine pass.
    """
    cos_lat = max(0.05, cos(radians(lat)))  # don't blow up near the poles
    dlng_deg = dlat_deg / cos_lat
    return (lat - dlat_deg, lat + dlat_deg, lng - dlng_deg, lng + dlng_deg)


# ---------- Severity ----------

def severity_label(mag: float) -> str:
    if mag < 3.0:
        return "micro"
    if mag < 4.0:
        return "minor"
    if mag < 5.0:
        return "light"
    if mag < 6.0:
        return "moderate"
    if mag < 7.0:
        return "strong"
    return "major"


def severity_color(mag: float) -> str:
    if mag < 3.0:
        return "#9aa0a6"
    if mag < 4.0:
        return "#1a73e8"
    if mag < 5.0:
        return "#f9a825"
    if mag < 6.0:
        return "#e8731c"
    if mag < 7.0:
        return "#c83737"
    return "#7c1d6f"


# ---------- Per-watch metrics ----------

def _classify_depth(depth_km: float) -> str:
    if depth_km < 30:
        return "shallow"
    if depth_km <= 300:
        return "intermediate"
    return "deep"


def compute_metrics(session: Session, watch: Watch) -> dict:
    """Compute the raw numeric assessment for a watch.

    Returns a dict with: ``n_events_30y, max_magnitude,
    max_magnitude_date, dominant_depth_band, p_m5_30y``.
    """
    horizon = datetime.now(tz=UTC) - timedelta(days=365 * 30)
    min_lat, max_lat, min_lng, max_lng = bbox_around(watch.lat, watch.lng, 1.0)

    stmt = (
        select(Event)
        .where(Event.magnitude >= 4.0)
        .where(Event.occurred_at >= horizon)
        .where(Event.lat >= min_lat)
        .where(Event.lat <= max_lat)
        .where(Event.lng >= min_lng)
        .where(Event.lng <= max_lng)
    )
    candidates = list(session.scalars(stmt))

    within: list[Event] = [
        e for e in candidates
        if haversine_km(watch.lat, watch.lng, e.lat, e.lng) <= 100.0
    ]

    n = len(within)
    if not within:
        return {
            "n_events_30y": 0,
            "max_magnitude": None,
            "max_magnitude_date": None,
            "dominant_depth_band": "unknown",
            "p_m5_30y": 0.0,
        }

    max_evt = max(within, key=lambda e: e.magnitude)
    band_counts = {"shallow": 0, "intermediate": 0, "deep": 0}
    for e in within:
        band_counts[_classify_depth(e.depth_km)] += 1
    # Tie-break: shallow > intermediate > deep.
    ordered = sorted(band_counts.items(), key=lambda kv: (-kv[1], {"shallow": 0, "intermediate": 1, "deep": 2}[kv[0]]))
    dominant = ordered[0][0]

    p = min(0.95, n / 25.0)

    return {
        "n_events_30y": n,
        "max_magnitude": float(max_evt.magnitude),
        "max_magnitude_date": max_evt.occurred_at,
        "dominant_depth_band": dominant,
        "p_m5_30y": float(p),
    }


# ---------- Assessment orchestration ----------

def build_assessment_numbers(
    session: Session,
    watch: Watch,
    country_code: str = "",
    state: str = "",
) -> RiskAssessment:
    """Compute (or refresh) the numeric ``RiskAssessment`` row.

    Does NOT generate or refresh the LLM summary; callers control that.
    Returns the persisted (but un-committed) row.
    """
    metrics = compute_metrics(session, watch)
    soil = geocode.soil_class(country_code, state)

    existing = watch.assessment
    if existing is None:
        ra = RiskAssessment(
            watch_id=watch.id,
            computed_at=datetime.now(tz=UTC),
            n_events_30y=metrics["n_events_30y"],
            max_magnitude=metrics["max_magnitude"],
            max_magnitude_date=metrics["max_magnitude_date"],
            dominant_depth_band=metrics["dominant_depth_band"],
            soil_class=soil,
            p_m5_30y=metrics["p_m5_30y"],
            llm_summary=None,
            llm_model=None,
        )
        session.add(ra)
        watch.assessment = ra
        return ra

    # Update existing.  We deliberately invalidate the cached LLM summary
    # whenever a number changes, per spec §4.6.
    changed = (
        existing.n_events_30y != metrics["n_events_30y"]
        or existing.max_magnitude != metrics["max_magnitude"]
        or existing.max_magnitude_date != metrics["max_magnitude_date"]
        or existing.dominant_depth_band != metrics["dominant_depth_band"]
        or existing.soil_class != soil
        or abs(existing.p_m5_30y - metrics["p_m5_30y"]) > 1e-9
    )
    existing.computed_at = datetime.now(tz=UTC)
    existing.n_events_30y = metrics["n_events_30y"]
    existing.max_magnitude = metrics["max_magnitude"]
    existing.max_magnitude_date = metrics["max_magnitude_date"]
    existing.dominant_depth_band = metrics["dominant_depth_band"]
    existing.soil_class = soil
    existing.p_m5_30y = metrics["p_m5_30y"]
    if changed:
        existing.llm_summary = None
        existing.llm_model = None
    return existing


def ensure_briefing(
    ra: RiskAssessment,
    address: str,
    region: str,
    force: bool = False,
) -> None:
    """Generate or refresh the LLM briefing on the assessment row in-place.

    If ``force`` is False and a summary already exists, this is a no-op.
    The function never raises (the local template always succeeds).
    """
    if not force and ra.llm_summary:
        return
    max_mag_date = (
        ra.max_magnitude_date.strftime("%Y-%m-%d") if ra.max_magnitude_date else "n/a"
    )
    text, model = ai.generate_briefing(
        address=address,
        region=region,
        n_events_30y=ra.n_events_30y,
        max_mag=ra.max_magnitude,
        max_mag_date=max_mag_date,
        depth_band=ra.dominant_depth_band,
        soil_class=ra.soil_class,
        p_m5_30y=ra.p_m5_30y,
    )
    ra.llm_summary = text
    ra.llm_model = model


def p_label(p: float) -> str:
    return f"{p*100:.0f}%"
