# syntax=docker/dockerfile:1
# ============================================================================ #
# RouterX — 100% Python server (FastAPI + Jinja2 UI + SQLAlchemy), plus a
# dependency-free bun sidecar for the built-in RouterX Free engine, which routes
# onto the bundled RouterX free-model catalogue (engine/free-models.json).
# The server binds 0.0.0.0:$PORT (Render assigns PORT dynamically).
# ============================================================================ #

# --------------------------------------------------------------------------- #
# Stage 1 — bun binary only (hosts the dependency-free engine sidecar)
# --------------------------------------------------------------------------- #
FROM oven/bun:1 AS bun-bin

# --------------------------------------------------------------------------- #
# Stage 2 — Python runtime + bun (engine sidecar host)
# --------------------------------------------------------------------------- #
FROM python:3.12-slim AS runner

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates libstdc++6 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python dependencies first (best layer caching)
COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

# bun binary — hosts the RouterX Free engine sidecar (127.0.0.1:$ENGINE_PORT)
COPY --from=bun-bin /usr/local/bin/bun /usr/local/bin/bun

# Engine sidecar — zero npm dependencies; the catalogue ships with the image
COPY engine /app/engine

# Python application: API + frontend module (Jinja2 templates + static assets)
COPY main.py ./
COPY maintenance.py ./
# The same image also runs the terminal on its own (`python3 -m
# terminal.service`), so the Root@Build shells can be hosted separately from
# the gateway. `terminal/` is the whole feature — nothing terminal-specific
# lives outside it.
COPY terminal ./terminal
COPY nova ./nova
COPY routers ./routers
COPY ui ./ui
COPY templates ./templates
COPY static ./static

# Database + engine runtime configuration
RUN mkdir -p /app/db
VOLUME /app/db
ENV PYTHONUNBUFFERED=1 \
    DATABASE_URL=file:/app/db/custom.db \
    ENGINE_PORT=3099

EXPOSE 3000
# The terminal service's own port — never the app's, for the same reason the
# engine sidecar never shares it.
EXPOSE 3100

# requirement #1: main.py binds 0.0.0.0:$PORT — PORT comes from the
# environment (Render injects it dynamically; default 3000 locally).
CMD ["python3", "main.py"]
