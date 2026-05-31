"""SQLAlchemy ORM models for SeismicLog.

Per spec §6: three tables (Event, Watch, RiskAssessment), all UTC
datetimes, indexes on event(occurred_at), event(lat,lng), unique
event(usgs_id).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


UTC = timezone.utc


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "event"

    id: Mapped[int] = mapped_column(primary_key=True)
    usgs_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    occurred_at: Mapped[datetime] = mapped_column(index=True)
    lat: Mapped[float]
    lng: Mapped[float]
    depth_km: Mapped[float] = mapped_column(default=10.0)
    magnitude: Mapped[float]
    place: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(8))  # "usgs" | "seed"

    __table_args__ = (
        Index("ix_event_lat_lng", "lat", "lng"),
    )


class Watch(Base):
    __tablename__ = "watch"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(80))
    address: Mapped[str] = mapped_column(String(240))
    lat: Mapped[float]
    lng: Mapped[float]
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(tz=UTC))

    assessment: Mapped[Optional["RiskAssessment"]] = relationship(
        back_populates="watch",
        uselist=False,
        cascade="all, delete-orphan",
    )


class RiskAssessment(Base):
    __tablename__ = "risk_assessment"

    id: Mapped[int] = mapped_column(primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watch.id", ondelete="CASCADE"), unique=True)
    computed_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(tz=UTC))
    n_events_30y: Mapped[int]
    max_magnitude: Mapped[Optional[float]]
    max_magnitude_date: Mapped[Optional[datetime]]
    dominant_depth_band: Mapped[str] = mapped_column(String(16))
    soil_class: Mapped[str] = mapped_column(String(40))
    p_m5_30y: Mapped[float]
    llm_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    llm_model: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)

    watch: Mapped["Watch"] = relationship(back_populates="assessment")
