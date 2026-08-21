"""Thread-safe idempotency coordination for inbound adapters.

The coordinator deliberately stores only completed application-facade results.  It
does not grant authority and is not a persistence substitute; durable operation
identity remains the responsibility of the injected service.
"""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from threading import Lock
from typing import Any


class IdempotencyConflictError(Exception):
    """A key was reused with a different canonical request payload."""


@dataclass(frozen=True)
class StoredResult:
    status_code: int
    body: Mapping[str, Any]


@dataclass
class _Entry:
    fingerprint: str
    future: Future[StoredResult]


class IdempotencyCoordinator:
    """Execute one operation per principal/key, including across event loops."""

    def __init__(self, *, max_completed_entries: int = 4096) -> None:
        if isinstance(max_completed_entries, bool) or max_completed_entries < 1:
            raise ValueError("max_completed_entries must be a positive integer")
        self._lock = Lock()
        self._max_completed_entries = max_completed_entries
        self._entries: OrderedDict[tuple[str, str], _Entry] = OrderedDict()

    async def execute(
        self,
        *,
        principal_subject: str,
        key: str,
        payload: Mapping[str, Any],
        operation: Callable[[], Awaitable[StoredResult]],
    ) -> StoredResult:
        fingerprint = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        namespace = (principal_subject, key)
        with self._lock:
            entry = self._entries.get(namespace)
            if entry is not None and entry.fingerprint != fingerprint:
                raise IdempotencyConflictError
            if entry is None:
                entry = _Entry(fingerprint=fingerprint, future=Future())
                self._entries[namespace] = entry
                owner = True
            else:
                self._entries.move_to_end(namespace)
                owner = False

        if not owner:
            # A duplicate caller owns only its wait. Cancelling it must never
            # cancel the shared completion or corrupt the leader's operation.
            return await asyncio.shield(asyncio.wrap_future(entry.future))

        try:
            result = await operation()
        except BaseException as exc:
            entry.future.set_exception(exc)
            # Failed attempts are not cached: a retry may safely reach the service,
            # whose durable idempotency boundary remains authoritative.
            with self._lock:
                if self._entries.get(namespace) is entry:
                    del self._entries[namespace]
            raise
        else:
            entry.future.set_result(result)
            with self._lock:
                self._entries.move_to_end(namespace)
                self._evict_completed()
            return result

    def _evict_completed(self) -> None:
        """Bound replay memory without ever evicting an in-flight operation."""
        completed = sum(entry.future.done() for entry in self._entries.values())
        if completed <= self._max_completed_entries:
            return
        for namespace, entry in tuple(self._entries.items()):
            if entry.future.done():
                del self._entries[namespace]
                completed -= 1
                if completed <= self._max_completed_entries:
                    return
