# EnergyPod controller image (docs/API_CONTRACTS.md "Operations surface").
#
# Shape (contract): multi-stage — a Node stage builds the operator console with
# corepack-pinned pnpm, a Python stage installs the pinned project into its own
# virtual environment and carries the built console. The runtime user is
# non-root, no secret is baked in (the .dockerignore keeps credentials and
# local state out of the build context), the configuration is mounted
# read-only at /etc/energypod, the SQLite durable store lives on the declared
# data volume /var/lib/energypod, and HEALTHCHECK probes the one
# unauthenticated endpoint GET /healthz.
#
# Versions are pinned to the reviewed runtime selection (ADR-0002 and
# pyproject.toml): Python 3.12 (3.12.10-slim, satisfies requires-python
# >=3.12,<3.13), Node 24.19.0, pnpm 11.22.0 via corepack.
#
# Static validation performed in this repository (Docker is NOT installed in
# the authoring environment; docs/CONTINUITY.md ledger step 12 defers the real
# image build to a Docker-capable environment):
#   1. Every COPY source path was verified to exist in the build context and
#      to survive the .dockerignore filters.
#   2. The base-image tags were verified to exist on Docker Hub
#      (python:3.12.10-slim, node:24.19.0-slim; multi-arch OCI indexes).
#   3. Version pins were cross-checked against pyproject.toml (requires-python,
#      console script energypod) and ADR-0002 (pnpm 11.22.0, Node 24.19.0,
#     React/Vite console built by `corepack pnpm build`).
#   4. The HEALTHCHECK URL matches the unauthenticated liveness route served
#      by the controller (GET /healthz on the loopback listener).
# Run in a Docker-capable environment:
#   docker build -t energypod-controller:0.1.0 .

# syntax=docker/dockerfile:1

# --- Stage 1: build the operator console with corepack-pinned pnpm ---------
FROM node:24.19.0-slim AS web-builder
ENV PNPM_HOME=/pnpm
ENV PATH=/pnpm:$PATH
RUN corepack enable \
    && corepack prepare pnpm@11.22.0 --activate \
    && corepack pnpm --version
WORKDIR /build/web
# Manifest-only layer first so dependency installs cache independently of src.
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN corepack pnpm install --frozen-lockfile
COPY web/ ./
RUN corepack pnpm build \
    && test -f /build/web/dist/index.html

# --- Stage 2: install the pinned Python project into its own venv ----------
FROM python:3.12.10-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/energypod-venv/bin:$PATH
# Dedicated non-root user: fixed uid, no home, no login shell.
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin energypod
RUN python -m venv /opt/energypod-venv
WORKDIR /app
# Install the pinned project (pyproject.toml drives every dependency pin).
COPY pyproject.toml README.md ./
COPY src ./src
RUN /opt/energypod-venv/bin/pip install --no-cache-dir .
# The built console ships inside the image (ADR-0002); serve it with
# web/nginx-spa.conf, which proxies /api/v1 and /healthz to this process.
COPY --from=web-builder /build/web/dist /app/web/dist
# Read-only configuration mount point; durable SQLite lives on the data volume.
RUN mkdir -p /etc/energypod /var/lib/energypod \
    && chown -R energypod:energypod /app /etc/energypod /var/lib/energypod
VOLUME /var/lib/energypod
USER energypod
EXPOSE 8080
# Liveness only (API_CONTRACTS): the one unauthenticated endpoint. This is a
# process-up probe, never readiness and never data.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"]
# The controller binds its loopback listener (energypod.main serving default);
# /healthz above and in-container clients reach it there.
CMD ["energypod", "run", "/etc/energypod/controller.yaml"]
