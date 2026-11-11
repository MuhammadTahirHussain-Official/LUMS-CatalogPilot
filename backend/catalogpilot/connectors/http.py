"""Shared outbound HTTP for connectors: timeouts, TLS verification always on, no redirects,
SSRF-checked hosts, and one translation from HTTP status to the SDK's typed errors."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx

from ..security.netguard import UnsafeUrl, check_public_https_url, resolve_pinned
from .sdk import AuthError, ConnectorError, RejectedError, RetryableError

TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=20.0, pool=5.0)  # shorter than the item lease
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def request(method: str, url: str, *, headers: dict[str, str] | None = None, json: Any = None,
            params: dict[str, Any] | None = None, auth: tuple[str, str] | None = None) -> Any:
    try:
        url = check_public_https_url(url.split("?", 1)[0]) + ("?" + url.split("?", 1)[1] if "?" in url else "")
        resolve_pinned(urlsplit(url).hostname or "")  # refuse hosts that resolve to private space
    except UnsafeUrl as exc:
        raise ConnectorError(str(exc), "Check the store URL on the channel.") from exc
    try:
        # verify=True is never configurable (SEC-OUT-07); redirects are not followed (SEC-OUT-02).
        with httpx.Client(timeout=TIMEOUT, verify=True, follow_redirects=False) as client:
            response = client.request(method, url, headers=headers, json=json, params=params, auth=auth)
    except httpx.TimeoutException as exc:
        raise RetryableError("The store did not answer in time.") from exc
    except httpx.TransportError as exc:
        raise RetryableError(f"Could not reach the store ({type(exc).__name__}).") from exc
    return interpret(response)


def interpret(response: httpx.Response) -> Any:
    status = response.status_code
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ConnectorError("The store sent an unexpectedly large response.")
    if status in (401, 403):
        raise AuthError("The store rejected our access. Reconnect the channel.", "Reconnect the channel.")
    if status == 429:
        retry_after = response.headers.get("Retry-After", "")
        try:
            seconds = min(float(retry_after), 300.0)
        except ValueError:
            seconds = 2.0
        raise RetryableError("The store is rate limiting requests.", retry_after=seconds, throttled=True)
    if status >= 500 or status == 408:
        raise RetryableError(f"The store returned a temporary error ({status}).")
    if 300 <= status < 400:
        raise ConnectorError("The store URL redirects elsewhere.", "Use the store's final https address.")
    try:
        body = response.json() if response.content else {}
    except ValueError:
        raise ConnectorError("The store sent a response we could not read.") from None
    if status >= 400:
        message = ""
        if isinstance(body, dict):
            message = str(body.get("message") or body.get("errors") or body.get("error") or "")[:300]
        raise RejectedError(f"The store rejected the product: {message or status}",
                            "Fix the product data mentioned above, then retry.")
    return body
