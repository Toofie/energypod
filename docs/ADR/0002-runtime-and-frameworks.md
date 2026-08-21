# ADR 0002: Single-process Python control runtime with separate React build

## Status

Accepted for the initial product; dependency versions are pinned and reviewed per release.

## Context

The controller must own three independent RTU-over-TCP gateways, continuously supervise short-lived
authorizations, expose local interfaces, and run in one Docker container. Splitting the safety path
across network services would add failure modes and distributed coordination without improving the
three-unit deployment.

Current dependency compatibility was verified on 2026-08-21. FastMCP 3.4.7 requires a modern
Starlette; FastAPI 0.116.1 from the former handoff was incompatible. FastAPI 0.141.1 resolves with
FastMCP 3.4.7 and Starlette 1.6.0. PyModbus 3.15.0 uses `device_id=` and supports explicit
`FramerType.RTU` on `AsyncModbusTcpClient`.

## Decision

- Python 3.12 is the sole backend runtime.
- One process and one event loop use `asyncio.TaskGroup` for structured supervision.
- One per-unit actor owns each socket. A critical control-task failure revokes authorization and
  cancels the control scope.
- FastAPI 0.141.1 provides REST/static hosting; FastMCP 3.4.7 is mounted as an ASGI application with
  its lifespan explicitly composed.
- SQLite in WAL mode stores audit, schedule versions, events, and bounded historical telemetry.
  The live authorization repository remains in memory and is never restored after restart.
- React 19.2.8 + TypeScript + Vite 8.2.1 is built separately and served as static assets.
- pnpm with a committed lockfile supplies deterministic frontend installs.
- One container image contains the backend and built static assets. The simulator is a separate
  development image/target, not imported by production runtime code.

## Consequences

- The safety path has no network hop, broker, or external database dependency.
- Uvicorn must run one worker. Horizontal replication is forbidden while control is enabled.
- CPU-heavy forecasting and model training must run outside the control event loop, initially as
  advisory jobs or separate future services.
- SQLite writes use a dedicated bounded writer and fail closed for actuation audit events.
- Framework adapters remain replaceable because application code imports only ports and domain types.

## Rejected alternatives

- Microservices/event bus: unnecessary distributed failure modes for three local units.
- Streamlit as process supervisor: UI lifecycle must not own command renewal.
- Browser-direct Modbus or agent-direct Modbus: violates single-writer and safety boundaries.
- PostgreSQL requirement: adds deployment complexity without an initial concurrency need.
- Deep base-class framework: encourages shared mutable lifecycle state; composition and narrow
  protocols are safer.
