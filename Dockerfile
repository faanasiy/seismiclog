# Multi-stage build for SeismicLog.
# Stage 1: build a wheel cache.  Stage 2: install + run as non-root.

# ---------- builder ----------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
COPY requirements.txt /build/requirements.txt
RUN pip install --upgrade pip && \
    pip wheel --wheel-dir=/wheels -r requirements.txt

# ---------- runtime ----------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8002

RUN groupadd --system --gid 1000 quake && \
    useradd  --system --uid 1000 --gid quake --home /app --shell /usr/sbin/nologin quake && \
    mkdir -p /app/data && \
    chown -R quake:quake /app

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt /app/requirements.txt
RUN pip install --no-index --find-links=/wheels -r /app/requirements.txt && rm -rf /wheels

COPY --chown=quake:quake app/        /app/app/
COPY --chown=quake:quake static/     /app/static/
COPY --chown=quake:quake templates/  /app/templates/
COPY --chown=quake:quake wsgi.py     /app/wsgi.py

USER quake

EXPOSE 8002

CMD ["gunicorn", "-w", "2", "-k", "gthread", "--threads", "4", \
     "-b", "0.0.0.0:8002", "--access-logfile", "-", "wsgi:app"]
