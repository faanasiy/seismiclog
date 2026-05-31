"""Deterministic seed generator: 200 events + 3 demo watches.

Uses ``random.seed(7)`` so reruns produce identical data.  Called from
the factory on startup, only when the corresponding table is empty.
"""
from __future__ import annotations

import logging
import math
import random
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Event, RiskAssessment, Watch


log = logging.getLogger("seismiclog.seed")
UTC = timezone.utc


# (label, lat, lng, radius_deg, weight)
_CLUSTERS: list[tuple[str, float, float, float, float]] = [
    ("Cascadia",                 47.0, -122.5, 4.0, 0.10),
    ("California",               36.0, -120.0, 4.0, 0.10),
    ("Aleutians / Alaska",       56.0, -158.0, 8.0, 0.10),
    ("Japan trench",             37.0,  142.0, 5.0, 0.12),
    ("Philippines / Indonesia",  -2.0,  125.0, 10.0, 0.13),
    ("Chile / Peru",            -23.0,  -70.0, 8.0, 0.10),
    ("Mediterranean",            38.0,   22.0, 6.0, 0.08),
    ("Turkey / Anatolian",       39.0,   37.0, 5.0, 0.07),
    ("Iceland / MAR",            64.5,  -19.0, 3.0, 0.10),
    ("Himalayan front",          28.0,   85.0, 6.0, 0.10),
]

_DIRS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
         "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def _sample_magnitude(rng: random.Random) -> float:
    """Clipped Gutenberg-Richter: 60% [2.5,4), 30% [4,5), 8% [5,6), 2% [6,7.5]."""
    r = rng.random()
    if r < 0.60:
        return round(rng.uniform(2.5, 3.999), 2)
    if r < 0.90:
        return round(rng.uniform(4.0, 4.999), 2)
    if r < 0.98:
        return round(rng.uniform(5.0, 5.999), 2)
    return round(rng.uniform(6.0, 7.499), 2)


def _sample_depth(rng: random.Random) -> float:
    """log-normal(mean=ln 25, sigma=1.0) clipped to [5, 600]."""
    while True:
        d = rng.lognormvariate(math.log(25.0), 1.0)
        if 5.0 <= d <= 600.0:
            return round(d, 1)


def _sample_cluster(rng: random.Random):
    total = sum(c[4] for c in _CLUSTERS)
    r = rng.random() * total
    acc = 0.0
    for name, lat, lng, radius, w in _CLUSTERS:
        acc += w
        if r <= acc:
            return name, lat, lng, radius
    name, lat, lng, radius, _ = _CLUSTERS[-1]
    return name, lat, lng, radius


def _sample_point(rng: random.Random, lat0: float, lng0: float, radius_deg: float) -> tuple[float, float]:
    """Uniform-ish sample inside a disk of ``radius_deg`` around ``(lat0,lng0)``."""
    while True:
        u = rng.uniform(-1.0, 1.0)
        v = rng.uniform(-1.0, 1.0)
        if u * u + v * v <= 1.0:
            return lat0 + u * radius_deg, lng0 + v * radius_deg


def _generate_events(rng: random.Random, n: int = 200) -> list[Event]:
    now = datetime.now(tz=UTC)
    out: list[Event] = []
    for _ in range(n):
        name, lat0, lng0, radius = _sample_cluster(rng)
        lat, lng = _sample_point(rng, lat0, lng0, radius)
        mag = _sample_magnitude(rng)
        depth = _sample_depth(rng)
        offset_seconds = rng.randint(0, 30 * 24 * 3600)
        when = now - timedelta(seconds=offset_seconds)
        distance_km = round(rng.uniform(2.0, 80.0))
        direction = rng.choice(_DIRS)
        place = f"{distance_km} km {direction} of {name}"[:200]
        out.append(Event(
            usgs_id="seed-" + uuid.uuid4().hex[:12],
            occurred_at=when,
            lat=lat,
            lng=lng,
            depth_km=depth,
            magnitude=mag,
            place=place,
            source="seed",
        ))
    return out


# Demo watches (see spec §9.2).
_DEMO_WATCHES: list[dict] = [
    {
        "label": "Home",
        "address": "San Francisco, California, US",
        "lat": 37.7749,
        "lng": -122.4194,
        "country_code": "us",
        "state": "California",
        "region": "San Francisco, USA",
    },
    {
        "label": "Mum's",
        "address": "Tokyo, Japan",
        "lat": 35.6762,
        "lng": 139.6503,
        "country_code": "jp",
        "state": "Tokyo",
        "region": "Tokyo, Japan",
    },
    {
        "label": "Cabin",
        "address": "Reykjavik, Iceland",
        "lat": 64.1466,
        "lng": -21.9426,
        "country_code": "is",
        "state": "Capital Region",
        "region": "Reykjavik, Iceland",
    },
]


def seed_if_empty(session: Session) -> None:
    """Seed events + watches if their tables are empty."""
    # Need risk imports here to avoid a circular import at module load.
    from . import risk

    if session.scalar(select(Event.id).limit(1)) is None:
        rng = random.Random(7)
        events = _generate_events(rng, n=200)
        session.add_all(events)
        session.flush()
        log.info("seed: inserted %d events", len(events))

    if session.scalar(select(Watch.id).limit(1)) is None:
        for w in _DEMO_WATCHES:
            watch = Watch(
                label=w["label"],
                address=w["address"],
                lat=w["lat"],
                lng=w["lng"],
            )
            session.add(watch)
            session.flush()
            # Compute the numeric assessment but leave LLM summary null
            # so the first detail view triggers a real generation.
            risk.build_assessment_numbers(
                session,
                watch,
                country_code=w["country_code"],
                state=w["state"],
            )
        log.info("seed: inserted %d demo watches", len(_DEMO_WATCHES))
