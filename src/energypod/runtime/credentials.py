"""The run-mode credential store: bearer tokens loaded from a JSON file.

API_CONTRACTS "Operations surface" and "Security and privacy" pin the run-mode
authentication story: the configuration carries only a *reference* to the
operator credential, the credential material lives in a JSON file mapping
bearer tokens to ``{subject, scopes, interactive, site_id}`` entries, and
rotation happens by replacing that file — a deployment never restarts to
rotate a credential.

Decisions implemented here, each pinned one way by the contract tests:

- Loading is eager and loud at the store boundary.  A present-but-invalid
  file — unparseable JSON, a non-object document, an entry with the wrong
  shape, an invented scope — raises ``ValueError`` naming the file, and a
  missing or unreadable file raises ``OSError``; no error ever echoes a
  bearer token, and neither does ``repr``/``str`` of the store.
- Authentication is fail-closed and never raises.  Every offer, mapped or
  not, is compared through :func:`hmac.compare_digest` against the mapped
  tokens (as UTF-8 bytes), so no timing side channel short-circuits on the
  first differing byte; an offer that matches no token is simply no
  credential, and a matched token keeps working (credentials are not
  single-use).
- Rotation is per authentication: the file is re-read whenever its stat
  changes, so a replacement file adds, rescopes, and drops tokens without a
  restart.  An unusable replacement — or a deleted file — refuses every
  offer; the stale generation is never served as a fallback, and the same
  store instance recovers the moment a valid file is back.
- A principal carries exactly ``subject``, ``scopes`` (a ``frozenset`` over
  the boundary's scope vocabulary), ``interactive``, and ``site_id``; the
  facade's cross-site refusal applies to file principals like any other.

The store is pure file I/O plus comparison: no socket is ever opened, and a
bearer token is a secret, not an identity — it is never validated against the
boundary's identifier grammar.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Any, Final

__all__ = ["CREDENTIAL_SCOPES", "CredentialPrincipal", "FileCredentialStore"]


#: The complete scope vocabulary a credential may carry (API_CONTRACTS
#: "API and MCP"): nothing invented, nothing missing.  Maintenance is absent.
CREDENTIAL_SCOPES: Final[frozenset[str]] = frozenset(
    {"observe", "audit:read", "dispatch", "arm", "stop", "stop:acknowledge"}
)

_ENTRY_KEYS: Final[frozenset[str]] = frozenset({"subject", "scopes", "interactive", "site_id"})

# The file identity that gates the per-authentication re-read: rewriting the
# file changes its mtime or size, replacing it (write-then-rename) changes the
# inode as well, and the test suite advances mtimes explicitly so rotation is
# never decided by timestamp granularity.
_StatKey = tuple[int, int, int, int]

_Entries = tuple[tuple[bytes, "CredentialPrincipal"], ...]


@dataclass(slots=True)
class CredentialPrincipal:
    """One credential-backed principal as the guarded boundary sees it.

    The boundary's ``Principal`` protocol declares settable attributes, and
    mypy models frozen-dataclass fields as read-only, so — like the simulator
    development principal — immutability is enforced by ownership instead:
    the store mints a fresh instance per authentication and nothing else ever
    constructs or holds one, so no caller can poison another's principal.
    """

    subject: str
    scopes: frozenset[str]
    interactive: bool
    site_id: str


def _encode_token(bearer_token: str) -> bytes | None:
    """UTF-8 bytes for one offer; ``None`` when it cannot be a credential.

    ``hmac.compare_digest`` accepts any bytes, so a non-ASCII offer stays
    comparable (and refusable) instead of becoming an exception the boundary
    would have to absorb; text that cannot be encoded at all (lone
    surrogates from a broken decoder) is no credential.
    """
    if not isinstance(bearer_token, str):
        return None
    try:
        return bearer_token.encode("utf-8")
    except UnicodeEncodeError:
        return None


class FileCredentialStore:
    """Bearer authentication over one JSON credential file.

    ``authenticate`` never raises and never performs network I/O.  The file is
    re-read on the stat change of a replacement, so rotation needs no restart;
    see the module docstring for the full set of pinned decisions.
    """

    def __init__(self, path: str | PathLike[str]) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._entries: _Entries = ()
        self._stat_key: _StatKey | None = None
        # Loading is eager and loud: commissioning learns about an unusable
        # credential file here, not from a runtime 401.
        entries, stat_key = self._load()
        self._entries = entries
        self._stat_key = stat_key

    async def authenticate(self, bearer_token: str) -> CredentialPrincipal | None:
        """Authenticate one offer against the file's tokens; fail-closed."""
        offered = _encode_token(bearer_token)
        if offered is None:
            return None
        entries = self._refresh()
        for token, principal in entries:
            # Resolved as a module attribute at call time so the comparison
            # stays on the standard library's constant-time digest.
            if hmac.compare_digest(offered, token):
                return CredentialPrincipal(
                    subject=principal.subject,
                    scopes=principal.scopes,
                    interactive=principal.interactive,
                    site_id=principal.site_id,
                )
        return None

    def __repr__(self) -> str:
        # The path is configuration, not secret material; the tokens are never
        # rendered, not even counted per token.
        return f"{type(self).__name__}(path={str(self._path)!r})"

    def __str__(self) -> str:
        return repr(self)

    # --- loading and rotation -------------------------------------------------

    def _load(self) -> tuple[_Entries, _StatKey]:
        """Read, parse, and validate the file; raise loudly when unusable.

        An unreadable or missing file raises ``OSError`` (whose message names
        the path); invalid content raises ``ValueError`` naming the file.  The
        stat is taken before the read so a file rewritten mid-read leaves a
        stat that differs from the loaded generation: the next authentication
        re-reads instead of serving one stale generation forever.
        """
        stat_key = _stat_key_of(self._path.stat())
        text: str
        try:
            text = self._path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(
                f"credential file {str(self._path)!r} is not valid UTF-8 text: {error}"
            ) from error
        try:
            document: Any = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"credential file {str(self._path)!r} is not valid JSON: {error}"
            ) from error
        return self._validate_document(document), stat_key

    def _refresh(self) -> _Entries:
        """Serve the current generation, re-reading the file when it changed.

        Rotation is per authentication: the stat is probed on every call, a
        changed file is re-read and re-validated, and an unusable file (or a
        missing one) empties the store without raising — never falling back to
        the stale generation.  Once a valid file is back, the same instance
        adopts it on the next authentication.
        """
        with self._lock:
            try:
                stat_key = _stat_key_of(self._path.stat())
            except OSError:
                self._entries = ()
                self._stat_key = None
                return self._entries
            if self._stat_key is not None and stat_key == self._stat_key:
                return self._entries
            try:
                entries, loaded_key = self._load()
            except (OSError, ValueError):
                self._entries = ()
                self._stat_key = None
                return self._entries
            self._entries = entries
            self._stat_key = loaded_key
            return self._entries

    # --- validation ------------------------------------------------------------

    def _validate_document(self, document: Any) -> _Entries:
        name = str(self._path)
        if not isinstance(document, dict):
            raise ValueError(
                f"credential file {name!r} must be a JSON object mapping bearer tokens to "
                "{subject, scopes, interactive, site_id} entries"
            )
        entries: list[tuple[bytes, CredentialPrincipal]] = []
        for index, item in enumerate(document.items()):
            token, entry = item
            # The token is a secret, never an identity: entries are reported
            # by position and nothing about a key is echoed into any error.
            if not isinstance(token, str) or not token:
                raise ValueError(
                    f"credential file {name!r} entry {index} must be keyed by a non-empty "
                    "bearer token string"
                )
            try:
                token_bytes = token.encode("utf-8")
            except UnicodeEncodeError as error:
                raise ValueError(
                    f"credential file {name!r} entry {index} bearer token must be valid "
                    "Unicode text"
                ) from error
            entries.append((token_bytes, self._validate_entry(index, entry)))
        return tuple(entries)

    def _validate_entry(self, index: int, entry: Any) -> CredentialPrincipal:
        name = str(self._path)
        if not isinstance(entry, dict):
            raise ValueError(
                f"credential file {name!r} entry {index} must be an object with exactly the "
                "keys subject, scopes, interactive and site_id"
            )
        keys = set(entry.keys())
        if keys != _ENTRY_KEYS:
            missing = sorted(_ENTRY_KEYS - keys)
            unknown = sorted(keys - _ENTRY_KEYS)
            raise ValueError(
                f"credential file {name!r} entry {index} must define exactly subject, scopes, "
                f"interactive and site_id (missing {missing}, unknown {unknown})"
            )
        subject = entry["subject"]
        if not isinstance(subject, str) or not subject:
            raise ValueError(
                f"credential file {name!r} entry {index} subject must be a non-empty string"
            )
        site_id = entry["site_id"]
        if not isinstance(site_id, str) or not site_id:
            raise ValueError(
                f"credential file {name!r} entry {index} site_id must be a non-empty string"
            )
        scopes = entry["scopes"]
        if not isinstance(scopes, list):
            raise ValueError(
                f"credential file {name!r} entry {index} scopes must be a list of scope strings"
            )
        for scope in scopes:
            if not isinstance(scope, str) or scope not in CREDENTIAL_SCOPES:
                raise ValueError(
                    f"credential file {name!r} entry {index} declares the unknown scope "
                    f"{scope!r}; a credential may carry only these scopes: "
                    f"{', '.join(sorted(CREDENTIAL_SCOPES))}"
                )
        interactive = entry["interactive"]
        if not isinstance(interactive, bool):
            raise ValueError(
                f"credential file {name!r} entry {index} interactive must be a boolean"
            )
        return CredentialPrincipal(
            subject=subject,
            scopes=frozenset(scope for scope in scopes if isinstance(scope, str)),
            interactive=interactive,
            site_id=site_id,
        )


def _stat_key_of(stat: os.stat_result) -> _StatKey:
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino, stat.st_dev)
