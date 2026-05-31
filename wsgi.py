"""Gunicorn / Flask entry-point.

Usage::

    gunicorn -w 2 -k gthread -b 0.0.0.0:8002 wsgi:app
    flask --app wsgi run --port 8002
"""
from app import create_app

app = create_app()
