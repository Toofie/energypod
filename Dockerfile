# EnergyPod controller + console — the ALL-IN-ONE deployment image.
#
# The operator's deployment model (2026-08-29): ONE self-contained image —
# the controller, the console, the commissioned config, AND the credentials
# and database baked in at build time.  Local-only deployment: the image
# carries secrets and must never leave the operator's machine (no registry
# push).  No volumes, no mounts: `docker compose up -d` is the whole deploy.
#
# Trade-off, stated plainly (docs/DEPLOY_DOCKER.md §6): the sqlite database
# baked at build time is the build-day snapshot.  Nightly restarts preserve
# everything (the container's writable layer persists across restarts), but
# a REBUILD resets history to the build-day state — so rebuild only when
# upgrading, and accept the history reset (or copy var/live-write.sqlite3
# out first and back in after the build).
#
#   console-build — node/pnpm: builds the console (web/) into static files.
#   runtime       — the controller (default): python + src/energypod/,
#                   uvicorn on 8080, entrypoint sources the baked env files.
#   console-nginx — nginx serving the built console; proxies /api/v1
#                   (WebSocket included) and /healthz to the energypod
#                   service — the same paths web/vite.config.ts proxies in
#                   development.

# --- stage 1: the console build -------------------------------------------------

FROM node:20-alpine AS console-build
WORKDIR /build
COPY web/package.json web/pnpm-lock.yaml ./
# pnpm is pinned here, never floated: corepack's unpinned default moved to a
# pnpm that needs Node >= 22 and the build broke against this Node 20 base
# (2026-09-03).  Major 10 runs here and reads the lockfileVersion 9.0 file
# natively.
RUN npm install -g pnpm@10 && pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

# --- stage 2: the controller (the default target) -------------------------------

FROM python:3.12-slim AS runtime

# tzdata: the civil-time windows (night charge, health watch, calibration)
# are computed from the site timezone via zoneinfo — the OS tz database must
# exist, and TZ keeps logs readable in site time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
ENV TZ=Australia/Brisbane

WORKDIR /app

# The project itself (energypod-controller).  Copy the packaging metadata
# first so dependency wheels cache across source-only edits.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# Non-root runtime user (uid 1000; the baked /app/var is chowned to match —
# the sqlite database must be writable).
RUN useradd --uid 1000 --create-home energypod

# The embedded deployment: the commissioned configuration AND the operator's
# var/ (credentials, API-key env files, the database snapshot).  The
# entrypoint sources the env files from here; the controller reads the
# bearer-token store and the database from here.  Owned by the runtime user.
COPY config/ ./config/
COPY var/ ./var/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chown -R energypod:energypod /app/var
USER energypod

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

EXPOSE 8080
# /healthz is the unauthenticated liveness read (rest.py) — no bearer needed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" || exit 1

# --- stage 3: the console's nginx (the console-nginx target) --------------------

FROM nginx:1.27-alpine AS console-nginx
COPY --from=console-build /build/dist /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
ENV TZ=Australia/Brisbane
EXPOSE 80
