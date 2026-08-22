"""Process-local, fleet-wide authority generation fencing."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

_MAX_EPOCH = (1 << 63) - 1
_MAX_REASON_LENGTH = 200


@dataclass(frozen=True, slots=True)
class AuthorityGenerationSnapshot:
    """An immutable point-in-time authority epoch."""

    epoch: int

    def __post_init__(self) -> None:
        if type(self.epoch) is not int:
            raise TypeError("epoch must be an integer")
        if not 0 <= self.epoch <= _MAX_EPOCH:
            raise ValueError("epoch must fit a non-negative signed 64-bit integer")


class AuthorityGenerationCoordinator:
    """Linearizable, irreversible generation fencing for one process instance."""

    def __init__(self) -> None:
        self._epoch = 0
        self._lock = asyncio.Lock()

    async def snapshot(self) -> AuthorityGenerationSnapshot:
        async with self._lock:
            return AuthorityGenerationSnapshot(self._epoch)

    async def advance(self, *, reason: str) -> AuthorityGenerationSnapshot:
        self._validate_reason(reason)
        async with self._lock:
            if self._epoch == _MAX_EPOCH:
                raise OverflowError("authority generation is exhausted")
            # No cancellation point exists between mutation and return. A call
            # cancelled before acquiring the lock does not commit; once this
            # line executes, the epoch is permanently consumed.
            self._epoch += 1
            return AuthorityGenerationSnapshot(self._epoch)

    async def advance_past(self, epoch: int, *, reason: str) -> AuthorityGenerationSnapshot:
        """Reconcile strictly beyond a fence another component already applied.

        2026-08-23 live desync: a revocation that fences the authorization
        repository without advancing this coordinator leaves every later mint
        at a permanently fenced epoch.  This operation repairs exactly that
        gap: it moves the epoch to ``epoch + 1`` when the coordinator sits at
        or below ``epoch``, and is an idempotent no-op when already beyond.
        The epoch never regresses and is never reused; nothing at or below
        the reconciled fence may publish again, so the permanent generation
        fence keeps its full strength.
        """
        if type(epoch) is not int or not 0 <= epoch <= _MAX_EPOCH:
            raise ValueError("epoch must fit a non-negative signed 64-bit integer")
        self._validate_reason(reason)
        async with self._lock:
            target = epoch + 1
            if self._epoch >= target:
                return AuthorityGenerationSnapshot(self._epoch)
            if target > _MAX_EPOCH:
                raise OverflowError("authority generation is exhausted")
            # Same atomicity contract as ``advance``: no cancellation point
            # exists between the mutation and the returned snapshot.
            self._epoch = target
            return AuthorityGenerationSnapshot(self._epoch)

    @staticmethod
    def _validate_reason(reason: str) -> None:
        if type(reason) is not str:
            raise TypeError("reason must be a string")
        if (
            not reason
            or reason != reason.strip()
            or len(reason) > _MAX_REASON_LENGTH
            or any(character in reason for character in "\r\n")
            or any(ord(character) < 32 or ord(character) == 127 for character in reason)
        ):
            raise ValueError("reason must be normalized, printable, and at most 200 characters")


__all__ = ["AuthorityGenerationCoordinator", "AuthorityGenerationSnapshot"]
