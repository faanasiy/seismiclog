"""Marshmallow schemas for SeismicLog.

One schema per request/response shape, per spec.  Marshmallow 3.x.
Validation errors raised from ``load`` are caught in routes and turned
into the 400 ``{"error": ..., "field": ...}`` envelope.
"""
from __future__ import annotations

from marshmallow import Schema, fields, validate


# ---------- Request schemas ----------


class EventsQuerySchema(Schema):
    """Query string for GET /api/events."""

    days = fields.Integer(load_default=1, validate=validate.OneOf([1, 7, 30]))
    min_mag = fields.Float(load_default=2.5, validate=validate.Range(min=0.0, max=9.0))
    bbox = fields.String(load_default=None, allow_none=True)


class WatchCreateSchema(Schema):
    """Body for POST /api/watch."""

    label = fields.String(required=True, validate=validate.Length(min=1, max=80))
    address = fields.String(required=True, validate=validate.Length(min=3, max=240))


# ---------- Response schemas ----------


class EventOutSchema(Schema):
    id = fields.Integer()
    usgs_id = fields.String()
    occurred_at = fields.Method("fmt_occurred_at")
    lat = fields.Float()
    lng = fields.Float()
    depth_km = fields.Float()
    magnitude = fields.Float()
    place = fields.String()
    severity = fields.String()
    # Only present in single-event endpoint
    usgs_url = fields.String(required=False)

    def fmt_occurred_at(self, obj) -> str:
        dt = obj["occurred_at"] if isinstance(obj, dict) else obj.occurred_at
        # Always emit Z-suffix UTC.
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class EventsListSchema(Schema):
    count = fields.Integer()
    window_days = fields.Integer()
    min_mag = fields.Float()
    source = fields.String()
    events = fields.List(fields.Nested(EventOutSchema))


class RefreshResultSchema(Schema):
    fetched = fields.Integer()
    inserted = fields.Integer()
    updated = fields.Integer()
    source = fields.String()
    note = fields.String(required=False)


class WatchOutSchema(Schema):
    id = fields.Integer()
    label = fields.String()
    address = fields.String()
    lat = fields.Float()
    lng = fields.Float()
    created_at = fields.Method("fmt_created_at")
    has_assessment = fields.Boolean()

    def fmt_created_at(self, obj) -> str:
        dt = obj["created_at"] if isinstance(obj, dict) else obj.created_at
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class WatchListSchema(Schema):
    count = fields.Integer()
    watches = fields.List(fields.Nested(WatchOutSchema))


class AssessmentOutSchema(Schema):
    computed_at = fields.Method("fmt_computed_at")
    n_events_30y = fields.Integer()
    max_magnitude = fields.Float(allow_none=True)
    max_magnitude_date = fields.Method("fmt_max_mag_date")
    dominant_depth_band = fields.String()
    soil_class = fields.String()
    p_m5_30y = fields.Float()
    p_m5_30y_label = fields.String()
    llm_summary = fields.String(allow_none=True)
    llm_model = fields.String(allow_none=True)

    def fmt_computed_at(self, obj) -> str:
        dt = obj["computed_at"] if isinstance(obj, dict) else obj.computed_at
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def fmt_max_mag_date(self, obj):
        dt = obj["max_magnitude_date"] if isinstance(obj, dict) else obj.max_magnitude_date
        if dt is None:
            return None
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class WatchDetailSchema(Schema):
    id = fields.Integer()
    label = fields.String()
    address = fields.String()
    lat = fields.Float()
    lng = fields.Float()
    created_at = fields.Method("fmt_created_at")
    assessment = fields.Nested(AssessmentOutSchema, allow_none=True)

    def fmt_created_at(self, obj) -> str:
        dt = obj["created_at"] if isinstance(obj, dict) else obj.created_at
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class HealthSchema(Schema):
    status = fields.String()
    version = fields.String()
    demo_offline = fields.Boolean()
    event_count = fields.Integer()
    watch_count = fields.Integer()


class ErrorSchema(Schema):
    error = fields.String()
    field = fields.String(required=False)
