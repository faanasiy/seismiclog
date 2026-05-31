"""HTTP routes for SeismicLog.

Single Blueprint ``api`` holding both the JSON endpoints under
``/api/*`` and the ``GET /`` index that serves the single-page front-end.
Error envelope: ``{"error": "...", "field": "..."}``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from flask import Blueprint, current_app, jsonify, render_template, request
from marshmallow import ValidationError
from sqlalchemy import func, select

from . import ai, geocode, risk, usgs
from .models import Event, RiskAssessment, Watch
from .schemas import (
    AssessmentOutSchema,
    ErrorSchema,
    EventOutSchema,
    EventsListSchema,
    EventsQuerySchema,
    HealthSchema,
    RefreshResultSchema,
    WatchCreateSchema,
    WatchDetailSchema,
    WatchListSchema,
    WatchOutSchema,
)


log = logging.getLogger("seismiclog.routes")
UTC = timezone.utc

bp = Blueprint("api", __name__)


# ---------- helpers ----------

def _session():
    return current_app.extensions["SessionLocal"]()


def _err(msg: str, *, field: Optional[str] = None, status: int = 400):
    payload = {"error": msg}
    if field is not None:
        payload["field"] = field
    return jsonify(ErrorSchema().dump(payload)), status


def _event_to_dict(e: Event, *, with_usgs_url: bool = False) -> dict:
    d = {
        "id": e.id,
        "usgs_id": e.usgs_id,
        "occurred_at": e.occurred_at,
        "lat": e.lat,
        "lng": e.lng,
        "depth_km": e.depth_km,
        "magnitude": e.magnitude,
        "place": e.place,
        "severity": risk.severity_label(e.magnitude),
    }
    if with_usgs_url:
        d["usgs_url"] = usgs.usgs_event_url(e.usgs_id)
    return d


def _watch_summary(w: Watch) -> dict:
    return {
        "id": w.id,
        "label": w.label,
        "address": w.address,
        "lat": w.lat,
        "lng": w.lng,
        "created_at": w.created_at,
        "has_assessment": w.assessment is not None,
    }


def _watch_detail(w: Watch) -> dict:
    out = {
        "id": w.id,
        "label": w.label,
        "address": w.address,
        "lat": w.lat,
        "lng": w.lng,
        "created_at": w.created_at,
        "assessment": None,
    }
    if w.assessment is not None:
        a = w.assessment
        out["assessment"] = {
            "computed_at": a.computed_at,
            "n_events_30y": a.n_events_30y,
            "max_magnitude": a.max_magnitude,
            "max_magnitude_date": a.max_magnitude_date,
            "dominant_depth_band": a.dominant_depth_band,
            "soil_class": a.soil_class,
            "p_m5_30y": a.p_m5_30y,
            "p_m5_30y_label": risk.p_label(a.p_m5_30y),
            "llm_summary": a.llm_summary,
            "llm_model": a.llm_model,
        }
    return out


def _parse_bbox(raw: Optional[str]) -> Optional[tuple[float, float, float, float]]:
    if not raw:
        return None
    parts = raw.split(",")
    if len(parts) != 4:
        raise ValidationError("bbox must be 'lat1,lng1,lat2,lng2'", field_name="bbox")
    try:
        lat1, lng1, lat2, lng2 = (float(p) for p in parts)
    except ValueError as exc:
        raise ValidationError("bbox values must be numeric", field_name="bbox") from exc
    return (min(lat1, lat2), min(lng1, lng2), max(lat1, lat2), max(lng1, lng2))


# ---------- index ----------

@bp.get("/")
def index():
    return render_template("index.html", version=current_app.config["VERSION"])


# ---------- /api/health ----------

@bp.get("/api/health")
def health():
    s = _session()
    event_count = s.scalar(select(func.count(Event.id))) or 0
    watch_count = s.scalar(select(func.count(Watch.id))) or 0
    payload = {
        "status": "ok",
        "version": current_app.config["VERSION"],
        "demo_offline": bool(current_app.config["DEMO_OFFLINE"]),
        "event_count": int(event_count),
        "watch_count": int(watch_count),
    }
    return jsonify(HealthSchema().dump(payload))


# ---------- /api/events ----------

@bp.get("/api/events")
def list_events():
    try:
        q = EventsQuerySchema().load(request.args.to_dict())
    except ValidationError as ve:
        field = next(iter(ve.messages.keys()), None)
        msg = "; ".join(str(v) for v in ve.messages.values())
        return _err(f"Invalid query: {msg}", field=field, status=400)

    try:
        bbox = _parse_bbox(q.get("bbox"))
    except ValidationError as ve:
        field = next(iter(ve.messages.keys()), None) if isinstance(ve.messages, dict) else "bbox"
        return _err("Invalid bbox.", field=field or "bbox", status=400)

    days = q["days"]
    min_mag = q["min_mag"]
    cutoff = datetime.now(tz=UTC) - timedelta(days=days)

    s = _session()
    stmt = (
        select(Event)
        .where(Event.occurred_at >= cutoff)
        .where(Event.magnitude >= min_mag)
        .order_by(Event.occurred_at.desc())
        .limit(1000)
    )
    if bbox is not None:
        min_lat, min_lng, max_lat, max_lng = bbox
        stmt = stmt.where(Event.lat >= min_lat, Event.lat <= max_lat,
                          Event.lng >= min_lng, Event.lng <= max_lng)

    rows = list(s.scalars(stmt))
    # Determine source: if any row in the window is from usgs, source =
    # "usgs"; if only seed rows, source = "seed".
    source = "seed"
    for r in rows:
        if r.source == "usgs":
            source = "usgs"
            break

    events_out = [_event_to_dict(e) for e in rows]
    payload = {
        "count": len(rows),
        "window_days": days,
        "min_mag": min_mag,
        "source": source,
        "events": events_out,
    }
    return jsonify(EventsListSchema().dump(payload))


@bp.get("/api/events/<int:event_id>")
def get_event(event_id: int):
    s = _session()
    e = s.get(Event, event_id)
    if e is None:
        return _err("Event not found.", status=404)
    return jsonify(EventOutSchema().dump(_event_to_dict(e, with_usgs_url=True)))


@bp.post("/api/events/refresh")
def refresh_events():
    s = _session()
    demo_offline = bool(current_app.config["DEMO_OFFLINE"])
    if demo_offline:
        return jsonify(RefreshResultSchema().dump({
            "fetched": 0,
            "inserted": 0,
            "updated": 0,
            "source": "seed",
            "note": "DEMO_OFFLINE=1; using seeded data.",
        }))

    start = datetime.now(tz=UTC) - timedelta(days=1)
    try:
        rows = usgs.fetch_events(starttime=start, minmagnitude=2.5)
    except RuntimeError:
        return jsonify(RefreshResultSchema().dump({
            "fetched": 0,
            "inserted": 0,
            "updated": 0,
            "source": "seed",
            "note": "USGS unreachable; using seeded data.",
        }))

    inserted = 0
    updated = 0
    for row in rows:
        existing = s.scalar(select(Event).where(Event.usgs_id == row["usgs_id"]))
        if existing is None:
            s.add(Event(**row))
            inserted += 1
        else:
            existing.occurred_at = row["occurred_at"]
            existing.lat = row["lat"]
            existing.lng = row["lng"]
            existing.depth_km = row["depth_km"]
            existing.magnitude = row["magnitude"]
            existing.place = row["place"]
            existing.source = row["source"]
            updated += 1
    s.commit()

    return jsonify(RefreshResultSchema().dump({
        "fetched": len(rows),
        "inserted": inserted,
        "updated": updated,
        "source": "usgs",
    }))


# ---------- /api/watch ----------

@bp.get("/api/watch")
def list_watches():
    s = _session()
    watches = list(s.scalars(select(Watch).order_by(Watch.created_at.asc())))
    out = {
        "count": len(watches),
        "watches": [_watch_summary(w) for w in watches],
    }
    return jsonify(WatchListSchema().dump(out))


@bp.post("/api/watch")
def create_watch():
    try:
        body = WatchCreateSchema().load(request.get_json(silent=True) or {})
    except ValidationError as ve:
        field = next(iter(ve.messages.keys()), None)
        msg = "; ".join(str(v) for v in ve.messages.values())
        return _err(f"Invalid request: {msg}", field=field, status=400)

    demo_offline = bool(current_app.config["DEMO_OFFLINE"])
    try:
        geo = geocode.geocode(body["address"], demo_offline=demo_offline)
    except geocode.GeocodeError as exc:
        return _err(str(exc) or "Address not found.", field="address", status=422)

    s = _session()
    watch = Watch(
        label=body["label"],
        address=body["address"],
        lat=geo["lat"],
        lng=geo["lng"],
    )
    s.add(watch)
    s.flush()

    risk.build_assessment_numbers(
        s, watch,
        country_code=geo.get("country_code", ""),
        state=geo.get("state", ""),
    )
    s.commit()
    s.refresh(watch)

    return jsonify(WatchOutSchema().dump(_watch_summary(watch))), 201


@bp.delete("/api/watch/<int:watch_id>")
def delete_watch(watch_id: int):
    s = _session()
    w = s.get(Watch, watch_id)
    if w is None:
        return _err("Watch not found.", status=404)
    s.delete(w)
    s.commit()
    return ("", 204)


@bp.get("/api/watch/<int:watch_id>")
def get_watch(watch_id: int):
    s = _session()
    w = s.get(Watch, watch_id)
    if w is None:
        return _err("Watch not found.", status=404)

    # Lazy LLM summary on first detail view.
    if w.assessment is not None and not w.assessment.llm_summary:
        # Derive a passable region string for the prompt.  For seeded
        # demo watches, geocode the stored address against the offline
        # table to recover country/state and produce a stable region.
        try:
            geo = geocode.geocode(w.address, demo_offline=True)
            region = geocode.build_region_string(geo)
        except geocode.GeocodeError:
            region = w.address
        risk.ensure_briefing(w.assessment, w.address, region, force=False)
        s.commit()

    return jsonify(WatchDetailSchema().dump(_watch_detail(w)))


@bp.post("/api/watch/<int:watch_id>/checklist")
def watch_checklist(watch_id: int):
    s = _session()
    w = s.get(Watch, watch_id)
    if w is None:
        return _err("Watch not found.", status=404)
    if w.assessment is None:
        return _err("Risk assessment is not ready yet.", status=409)

    building_type = (request.args.get("building_type") or "apartment").strip().lower()
    if building_type not in ai.BUILDING_TYPES:
        return _err(
            f"Invalid building_type; expected one of {', '.join(ai.BUILDING_TYPES)}.",
            field="building_type",
            status=400,
        )

    items, tier, model = ai.generate_checklist(
        address=w.address,
        building_type=building_type,
        soil_class=w.assessment.soil_class,
        p_m5_30y=w.assessment.p_m5_30y,
    )
    return jsonify({
        "watch_id": w.id,
        "building_type": building_type,
        "risk_tier": tier,
        "items": items,
        "llm_model": model,
    })


@bp.post("/api/watch/<int:watch_id>/recompute")
def recompute_watch(watch_id: int):
    s = _session()
    w = s.get(Watch, watch_id)
    if w is None:
        return _err("Watch not found.", status=404)

    # Recover regional data (country/state) for soil + region string.
    # We use the offline table when possible (covers the three demo
    # watches); otherwise we try Nominatim with the demo_offline flag
    # in mind.
    demo_offline = bool(current_app.config["DEMO_OFFLINE"])
    try:
        geo = geocode.geocode(w.address, demo_offline=demo_offline)
        country = geo.get("country_code", "")
        state = geo.get("state", "")
        region = geocode.build_region_string(geo)
    except geocode.GeocodeError:
        country = ""
        state = ""
        region = w.address

    ra = risk.build_assessment_numbers(s, w, country_code=country, state=state)
    s.flush()
    risk.ensure_briefing(ra, w.address, region, force=True)
    s.commit()
    s.refresh(w)
    return jsonify(WatchDetailSchema().dump(_watch_detail(w)))
