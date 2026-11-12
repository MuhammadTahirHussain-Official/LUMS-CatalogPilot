"""Connector port (INT-01..13): one interface, declared capabilities, typed failures.

The engine never inspects platform responses. A connector translates every outcome into
one of the exceptions below, which is what makes retry, pause and dead-letter decisions
uniform across platforms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ConnectorError(Exception):
    """Base class. ``message`` is plain language, safe to show to a seller (UX-06)."""

    def __init__(self, message: str, suggestion: str = ""):
        super().__init__(message)
        self.message, self.suggestion = message, suggestion


class RetryableError(ConnectorError):
    """Timeouts, 5xx, 429. The engine backs off and retries."""

    def __init__(self, message: str, retry_after: float | None = None, throttled: bool = False):
        super().__init__(message)
        self.retry_after, self.throttled = retry_after, throttled


class AuthError(ConnectorError):
    """401/403: the credential was revoked or expired. The channel pauses; items are not failed."""


class RejectedError(ConnectorError):
    """The platform refused this item (validation). Retrying the same payload cannot succeed."""


class CursorExpired(ConnectorError):
    """The platform no longer accepts a page cursor. The import restarts from the first page;
    because imports are upserts, nothing is duplicated."""


class UnsupportedOperation(ConnectorError):
    """Declared, never silently ignored (INT-02)."""


@dataclass(frozen=True)
class Capabilities:
    max_title_length: int = 255
    max_description_length: int = 65_000
    max_variants: int = 100
    max_options: int = 3
    max_images: int = 250
    max_tags: int = 250
    requires_price: bool = True
    requires_sku: bool = False
    requires_image: bool = False
    requires_gtin: bool = False
    supports_html: bool = True
    supports_idempotency_key: bool = False
    supports_delete: bool = True
    supports_delist: bool = True
    supports_read_back: bool = True
    supports_import: bool = False  # can products be read from this platform (source side)?
    currencies: tuple[str, ...] = ()  # empty = any
    requests_per_second: float = 2.0
    burst: int = 40
    credential_kinds: tuple[str, ...] = ()
    credential_fields: tuple[str, ...] = ()
    needs_base_url: bool = False


@dataclass
class CallContext:
    tenant_id: str
    channel_id: str
    base_url: str | None
    config: dict[str, Any]
    secret: dict[str, Any]  # short-lived credential lease; memory only, never logged
    task_id: str = ""
    fencing_token: int = 0

    def __repr__(self) -> str:  # never leak the secret through logs or tracebacks
        return f"CallContext(tenant={self.tenant_id}, channel={self.channel_id}, task={self.task_id})"


@dataclass
class WriteResult:
    external_id: str
    created: bool
    url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class Connector(Protocol):
    platform: str
    label: str

    def capabilities(self) -> Capabilities: ...
    def test_connection(self, ctx: CallContext) -> list[str]:
        """Read-only call that proves the credential works; returns granted scopes."""
        ...
    def upsert_product(self, ctx: CallContext, payload: dict[str, Any], external_id: str | None,
                       idempotency_key: str) -> WriteResult: ...
    def find_by_marker(self, ctx: CallContext, source_key: str) -> str | None:
        """Look up an object previously written for ``source_key`` (platforms without
        idempotency keys). Returns its external id, or None."""
        ...
    def get_product(self, ctx: CallContext, external_id: str) -> dict[str, Any] | None: ...
    def delist(self, ctx: CallContext, external_id: str, idempotency_key: str) -> None: ...
    def delete_product(self, ctx: CallContext, external_id: str, idempotency_key: str) -> None: ...
    def list_products(self, ctx: CallContext, cursor: str | None) -> tuple[list[dict[str, Any]], str | None]:
        """Source side: one page of products as UPM-shaped states, plus the next cursor."""
        ...
