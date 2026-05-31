"""Flask application factory for SeismicLog.

This module wires together SQLAlchemy, the blueprint, and the one-time
seed-if-empty bootstrap. The factory is intentionally minimal: no
extensions besides SQLAlchemy, no flask-migrate (per spec, the DB is
created with ``Base.metadata.create_all``).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from .models import Base


def _default_db_url() -> str:
    """Return the default sqlite URL pointing at ``data/seismiclog.db``.

    The path is resolved relative to the package root so the URL is
    correct both for ``flask --app wsgi run`` invocations from the
    project root and for the container's ``/app`` working directory.
    """
    pkg_root = Path(__file__).resolve().parent.parent
    data_dir = pkg_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "seismiclog.db"
    return f"sqlite:///{db_path.as_posix()}"


def create_app() -> Flask:
    app = Flask(
        __name__,
        static_folder=str(Path(__file__).resolve().parent.parent / "static"),
        template_folder=str(Path(__file__).resolve().parent.parent / "templates"),
    )

    # Logging
    log_level = logging.DEBUG if os.environ.get("FLASK_ENV") == "development" else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Config
    app.config["VERSION"] = "0.1"
    app.config["DEMO_OFFLINE"] = os.environ.get("DEMO_OFFLINE", "0") == "1"
    app.config["DATABASE_URL"] = os.environ.get("DATABASE_URL") or _default_db_url()

    # Engine + scoped session.  Single-threaded SQLite is fine for the
    # demo's tiny load; gunicorn runs 2 threaded workers so we set
    # ``check_same_thread=False``.
    engine = create_engine(
        app.config["DATABASE_URL"],
        future=True,
        connect_args={"check_same_thread": False} if app.config["DATABASE_URL"].startswith("sqlite") else {},
    )
    SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True))

    app.extensions["engine"] = engine
    app.extensions["SessionLocal"] = SessionLocal

    # Create tables once.
    Base.metadata.create_all(engine)

    # Seed if empty (events + demo watches).
    from .seed import seed_if_empty

    with SessionLocal() as session:
        seed_if_empty(session)
        session.commit()

    # Register routes.
    from .routes import bp as api_bp

    app.register_blueprint(api_bp)

    @app.teardown_appcontext
    def _shutdown_session(exc):  # noqa: ARG001
        SessionLocal.remove()

    return app
