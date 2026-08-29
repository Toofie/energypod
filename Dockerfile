# EnergyPod controller + console — the deployment image (three targets).
#
#   console-build — node/pnpm: builds the console (web/) into static files.
#   runtime       — the controller (default): python + src/energypod/,
#                   uvicorn on 8080, entrypoint sources the operator's env
#                   files from the mounted var/.
#   console-nginx — nginx serving the built console; docker-compose.yml mounts
#                   docker/nginx.conf, which proxies /api/v1 (WebSocket
#                   included) and /healthz to the energypod service — the same
#                   paths web/vite.config.ts proxies in development.
#
# NO SECRET IS BAKED INTO ANY TARGET.  Credentials, the API-key env files,
# and the sqlite database arrive at RUNTIME as mounted volumes (see
# compose.yaml and docs/DEPLOY_DOCKER.md).

# --- stage 1: the console build -------------------------------------------------

FROM node:20-alpine AS console-build
WORKDIR /build
COPY web/package.json web/pnpm-lock.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile
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

# Non-root runtime user (uid 1000 — docs/DEPLOY_DOCKER.md chowns the mounted
# var/ directory to match, because the sqlite database must be writable).
RUN useradd --uid 1000 --create-home energypod
USER energypod

# The guarded entrypoint: source the operator's credential env files (they
# carry `export KEY=...` lines) and hand over to the standing launch command.
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

EXPOSE 8080
# /healthz is the unauthenticated liveness read (rest.py) — no bearer needed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" || exit 1

# --- stage 3: the console's nginx (the console-nginx target) --------------------

FROM nginx:1.27-alpine AS console-nginx
COPY --from=console-build /build/dist /usr/share/nginx/html
# docker/nginx.conf is mounted read-only by compose.yaml (editing the proxy
# needs no rebuild); this COPY only provides a sane default.
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
ENV TZ=Australia/Brisbane
EXPOSE 80
