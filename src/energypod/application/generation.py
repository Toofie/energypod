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
