"""The pvoutput.org add-status adapter (thin, typed, sign-honest).

This module is the whole wire boundary of the reporter that replaces the
site's old Docker writer (``byd/scheduler.py::_post_pv_metrics`` -- one POST
per 5-minute slot per battery).  Everything the operator's PVOutput dashboard
depends on is pinned HERE, with its evidence:

- **Endpoint + headers.** ``POST https://pvoutput.org/service/r2/addstatus.jsp``
  with ``X-Pvoutput-Apikey`` / ``X-Pvoutput-SystemId`` (the old container's
  exact header names) plus ``X-Rate-Limit: 1`` so every answer surfaces
  ``X-Rate-Limit-Remaining`` / ``-Limit`` / ``-Reset`` for observability (the
  add-status specification's own rate-limit disclosure switch).
- **``d``/``t`` derivation.** From the caller's UTC sample instant converted
  to the configured SITE timezone, ``t`` floored onto the pinned 5-minute
  slot grid (PVOutput rounds ``t`` to the site's configured status interval;
  the old container floored nothing and its minute wobbled -- the floor is
  the honest slot the data belongs to).  The sample instant is always an
  observation's own past stamp, so the slot can never be future-dated (the
  specification refuses future dates outright).
- **v7-v12 sign continuity (NO negation).** The old container posted the
  raw-signed battery-side power word (byd/battery.py: ``active_power`` ->
  ``EB1_BATT_SIDE_POWER`` at decimal 8201 = DCDC 0x2009; the operator also
  remembers the overlapping "reg 4103" = PCS 0x1007 grid P).  Both words
  share ONE live-proven orientation on this hardware (PROTOCOL_EVIDENCE 4b/4c:
  the battery family 0x5008/0x2009/0x0114 and the PCS grid P are NEGATIVE =
  CHARGE/import, POSITIVE = DISCHARGE/export -- the 2026-08-22 direction
  trial and captures proved it on the wire).  pod-manager's ``battery_watts``
  (system 0x0114) carries exactly that convention, so the per-unit power
  slots post the value UNNEGATED and the existing dashboard graphs stay
  continuous.
- **b1's spec-mandated flip.** PVOutput's NATIVE battery fields use the
  OPPOSITE convention -- the add-status specification says ``b1`` is
  "positive ... charging", "-200 (Discharge), 200 (Charge)" -- while
  pod-manager's ``battery_watts`` is negative = charge.  The uploader
  therefore passes ``b1`` as the FLEET aggregate in POD-MANAGER's convention
  (the plain sum of per-pod ``battery_watts``) and THIS boundary flips it to
  the spec's convention (``-sum``) -- the one place either convention is
  translated, so the two can never drift apart.
- **Typed refusals.** 401 and non-rate 403 (bad/disabled key, read-only key,
  and the donation-mode refusal -- v7-v12 and b1/b2 are donation-tier
  parameters, so a non-donating site's 403 disables the uploader LOUDLY as an
  auth-class failure: fixing it is a human act) raise
  :class:`PvOutputAuthError`; the rate 403 ("Exceeded number requests per
  hour") raises :class:`PvOutputRateLimited` carrying the reset instant;
  400 raises :class:`PvOutputRejected` with PVOutput's reason text VERBATIM
  (the spec's refusal wording is the operator's debugging surface); 5xx and
  transport failures raise :class:`PvOutputUnavailable`.

The transport is a port (the providers/http.py doctrine): the production
adapter is httpx-backed with a configured timeout, every test composes a
fake, and constructing the client opens no socket.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

__all__ = [
    "PVOUTPUT_ADD_STATUS_URL",
    "HttpxPvOutputTransport",
    "PvOutputAuthError",
    "PvOutputError",
    "PvOutputHttpResponse",
    "PvOutputHttpTransport",
    "PvOutputPostResult",
    "PvOutputRateLimited",
    "PvOutputRejected",
    "PvOutputStatusClient",
    "PvOutputUnavailable",
    "floor_to_slot",
]

#: The add-status endpoint (the specification's own URL; the old container's).
PVOUTPUT_ADD_STATUS_URL: Final[str] = "https://pvoutput.org/service/r2/addstatus.jsp"

#: The pinned slot grid: 300 s (PVOutput's 5-minute status interval).
SLOT_SECONDS: Final[int] = 300

#: The slot-parameter vocabulary this reporter may write: the extended values
#: v7..v12 (never v1-v6 -- the solar inverter's integration owns those slots
#: on this site) plus the native battery fields b1 (power W) / b2 (SoC %).
_SLOT_FIELDS: Final[frozenset[str]] = frozenset(
    [f"v{number}" for number in range(7, 13)] + ["b1", "b2"]
)

#: The rate-limited 403's own wording (the specification's refusal text): the
#: one discriminator between "back off" and "this key may never write".
_RATE_LIMIT_TEXT: Final[re.Pattern[str]] = re.compile(
    r"exceeded\s+number\s+requests", re.IGNORECASE
)


class PvOutputError(RuntimeError):
    """Base for every typed pvoutput.org refusal.

    Every subclass carries ``pvoutput_failure`` -- its class word from the
    pinned vocabulary ("auth", "rate_limited", "rejected", "unavailable") --
    so the application uploader classifies a refusal STRUCTURALLY, through
    the injected client port, without importing this adapter module (the
    architecture's layering pin; the composition root is the only place the
    two meet).
    """

    pvoutput_failure: str = "unavailable"


class PvOutputAuthError(PvOutputError):
    """The credentials may never write: 401, or a non-rate 403.

    Bad/disabled key, an invalid system id, a read-only key, and the
    donation-mode refusal (v7-v12 and b1/b2 are donation-tier parameters).
    Retrying cannot fix any of these; the uploader disables itself loudly and
    the status surface carries the note until an operator acts.
    """

    pvoutput_failure = "auth"


class PvOutputRateLimited(PvOutputError):
    """The hourly request budget is spent (the rate 403).

    Carries PVOutput's own reset instant (``X-Rate-Limit-Reset``, Unix UTC)
    when the answer included it, so the uploader backs off until the budget
    reopens instead of hammering a refused endpoint.
    """

    pvoutput_failure = "rate_limited"

    def __init__(self, message: str, *, reset_at_unix: int | None = None) -> None:
        super().__init__(message)
        self.reset_at_unix = reset_at_unix


class PvOutputRejected(PvOutputError):
    """PVOutput refused the payload (400): its reason text rides VERBATIM.

    The spec's refusal wording ("Invalid Date ...", "Moon Powered", ...)
    names the exact field at fault; paraphrasing it would hide the operator's
    debugging surface.
    """

    pvoutput_failure = "rejected"


class PvOutputUnavailable(PvOutputError):
    """PVOutput is unreachable or unhealthy: timeout, transport, or 5xx."""

    pvoutput_failure = "unavailable"


@dataclass(frozen=True, slots=True)
class PvOutputHttpResponse:
    """One wire answer: status, body text, and the rate-limit headers."""

    status_code: int
    text: str
    headers: Mapping[str, str]


class PvOutputHttpTransport(Protocol):
    """The narrow wire seam (one form POST -> status/text/headers)."""

    async def post_form(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> PvOutputHttpResponse: ...


class HttpxPvOutputTransport:
    """The production transport: httpx form POST with a configured timeout.

    The client is injectable so the composition root owns its lifecycle and a
    test can substitute a stub; when none is supplied a private client is
    created lazily (composition opens no socket) and closed with the
    transport.  Timeouts and transport failures normalize to
    :class:`PvOutputUnavailable` -- the status client never sees an
    httpx-specific exception.
    """

    def __init__(
        self,
        *,
        timeout_s: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not 0.0 < float(timeout_s) < 600.0:
            raise ValueError("timeout_s must lie in (0, 600)")
        self._timeout_s = float(timeout_s)
        self._client = client
        self._owns_client = client is None

    async def post_form(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> PvOutputHttpResponse:
        client = self._client if self._client is not None else self._make_client()
        try:
            response = await client.post(url, data=dict(data), headers=dict(headers))
        except httpx.TimeoutException as exc:
            raise PvOutputUnavailable(
                f"pvoutput request timed out after {self._timeout_s:g}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise PvOutputUnavailable(f"pvoutput transport failure: {exc}") from exc
        return PvOutputHttpResponse(
            status_code=response.status_code,
            text=response.text,
            headers={str(key): str(value) for key, value in response.headers.items()},
        )

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _make_client(self) -> httpx.AsyncClient:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_s),
            follow_redirects=True,
        )
        return self._client


@dataclass(frozen=True, slots=True)
class PvOutputPostResult:
    """One accepted POST's observability facts (the rate-limit headers)."""

    rate_remaining: int | None
    rate_limit: int | None
    rate_reset_unix: int | None


def floor_to_slot(local: datetime) -> datetime:
    """Floor one site-local instant onto the pinned 5-minute slot grid."""
    return local.replace(minute=(local.minute // 5) * 5, second=0, microsecond=0)


def _header_int(headers: Mapping[str, str], name: str) -> int | None:
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _coerced_int(value: object) -> int:
    """The old container's own coercion: whole numbers on the wire."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"pvoutput field values must be numbers; got {value!r}")
    return round(float(value))


class PvOutputStatusClient:
    """The add-status client: wire formatting, headers, and typed refusals.

    Credentials arrive as constructor material -- resolved from secret
    references by the composition root, never stored in configuration.  The
    timezone name is the SITE's configured zone (``site.timezone``): ``d``/``t``
    are civil-time facts at the site, and the slot floor keeps the posted
    stamp inside the interval the data actually belongs to.
    """

    def __init__(
        self,
        *,
        transport: PvOutputHttpTransport,
        api_key: str,
        system_id: str,
        timezone_name: str,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must be non-empty key material")
        if not isinstance(system_id, str) or not system_id.strip():
            raise ValueError("system_id must be non-empty key material")
        try:
            zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone_name must be an IANA timezone") from exc
        self._transport = transport
        self._api_key = api_key.strip()
        self._system_id = system_id.strip()
        self._zone = zone

    async def post_status(
        self,
        *,
        sample_at: datetime,
        fields: Mapping[str, int | float],
    ) -> PvOutputPostResult:
        """POST one slot's fields; ``sample_at`` is the observation's UTC stamp.

        ``d``/``t`` derive from that stamp converted to the site timezone and
        floored to the 5-minute grid -- a past stamp can never floor into a
        future slot.  Every field value is coerced to a whole number (the old
        container's ``int(...)``); ``b1`` arrives in pod-manager's convention
        (negative = charge) and is NEGATED onto the spec's (positive =
        charge) exactly here; and only the reporter's own slot vocabulary is
        accepted, so a caller can never smuggle a v1-v6 write through this
        boundary.
        """
        if not isinstance(sample_at, datetime) or sample_at.tzinfo is None:
            raise ValueError("sample_at must be a timezone-aware datetime")
        payload: dict[str, str] = {}
        for name, value in dict(fields).items():
            if name not in _SLOT_FIELDS:
                raise ValueError(
                    f"pvoutput field {name!r} is outside the reporter's vocabulary "
                    f"{sorted(_SLOT_FIELDS)} (v1-v6 belong to the solar inverter's "
                    "integration on this site)"
                )
            coerced = _coerced_int(value)
            if name == "b1":
                # The spec's sign (positive = charge) is the opposite of the
                # battery_watts convention the aggregate was summed in; the
                # translation lives HERE and nowhere else.
                coerced = -coerced
            payload[name] = str(coerced)
        local = sample_at.astimezone(self._zone)
        slot = floor_to_slot(local)
        payload["d"] = slot.strftime("%Y%m%d")
        payload["t"] = slot.strftime("%H:%M")
        response = await self._transport.post_form(
            PVOUTPUT_ADD_STATUS_URL,
            data=payload,
            headers={
                "X-Pvoutput-Apikey": self._api_key,
                "X-Pvoutput-SystemId": self._system_id,
                # The spec's rate-limit disclosure switch: the answer then
                # carries X-Rate-Limit-Remaining/-Limit/-Reset.
                "X-Rate-Limit": "1",
            },
        )
        return self._interpret(response)

    def _interpret(self, response: PvOutputHttpResponse) -> PvOutputPostResult:
        """Map one wire answer onto the typed refusals or the rate facts."""
        text = response.text.strip()
        status = response.status_code
        if status == 200:
            return PvOutputPostResult(
                rate_remaining=_header_int(response.headers, "X-Rate-Limit-Remaining"),
                rate_limit=_header_int(response.headers, "X-Rate-Limit-Limit"),
                rate_reset_unix=_header_int(response.headers, "X-Rate-Limit-Reset"),
            )
        if status == 400:
            raise PvOutputRejected(text or "pvoutput rejected the status (400)")
        if status == 401:
            raise PvOutputAuthError(text or "pvoutput refused the credentials (401)")
        if status == 403:
            reset = _header_int(response.headers, "X-Rate-Limit-Reset")
            if _RATE_LIMIT_TEXT.search(text) is not None:
                raise PvOutputRateLimited(
                    text or "pvoutput rate limit exceeded (403)", reset_at_unix=reset
                )
            # Read-only key, invalid system/key pair, or the donation-mode
            # refusal (v7-v12 and b1/b2 are donation-tier parameters): none
            # of these is retryable -- the uploader must stop and say so.
            raise PvOutputAuthError(text or "pvoutput refused the write (403)")
        if status >= 500:
            raise PvOutputUnavailable(
                f"pvoutput is unhealthy (HTTP {status})" + (f": {text}" if text else "")
            )
        raise PvOutputUnavailable(
            f"pvoutput answered an unexpected HTTP {status}"
            + (f": {text}" if text else "")
        )
