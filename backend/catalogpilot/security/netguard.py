"""SSRF protection for user-influenced URLs (SEC-OUT-01/02, CRED-19).

``check_public_https_url`` validates shape; ``resolve_pinned`` resolves DNS once and returns
the vetted IP so the caller connects to exactly the address that was checked
(DNS-rebinding protection). Redirects must be re-validated by the caller, hop by hop.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit


class UnsafeUrl(ValueError):
    pass


_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_BLOCKED_HOSTS = {"localhost", "metadata", "metadata.google.internal", "instance-data"}


def _ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified):
        return False
    if ip.version == 6:
        return not ip.is_site_local
    return ip not in _CGNAT  # carrier-grade NAT range is not flagged by is_private on all versions


def check_public_https_url(url: str, allow_http: bool = False) -> str:
    """Returns the normalized URL or raises UnsafeUrl with a seller-readable reason."""
    if not isinstance(url, str) or len(url) > 2000 or any(ord(c) < 0x20 for c in url):
        raise UnsafeUrl("The URL is not valid.")
    parts = urlsplit(url.strip())
    schemes = ("https", "http") if allow_http else ("https",)
    if parts.scheme.lower() not in schemes:
        raise UnsafeUrl("The URL must start with https://.")
    if parts.username or parts.password:
        raise UnsafeUrl("The URL must not contain a username or password.")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeUrl("The URL has no host name.")
    if host in _BLOCKED_HOSTS or host.endswith((".local", ".internal", ".localhost")):
        raise UnsafeUrl("That host is not reachable from our servers.")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
        # Decimal / hex / octal integer hosts ("2130706433", "0x7f000001") resolve to IPs in some stacks.
        if host.replace(".", "").isdigit() or host.startswith("0x"):
            raise UnsafeUrl("Use a host name, not a numeric address.") from None
        if "." not in host:
            raise UnsafeUrl("The URL must use a full domain name.")
    if literal is not None and not _ip_is_public(literal):
        raise UnsafeUrl("That address is private or reserved.")
    try:
        port = parts.port
    except ValueError:
        raise UnsafeUrl("The URL has an invalid port.") from None
    netloc = host if literal is None or literal.version == 4 else f"[{host}]"
    if port and port not in (80, 443):
        netloc += f":{port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path.rstrip("/"), "", ""))


def resolve_pinned(host: str, port: int = 443) -> str:
    """Resolve once; every returned address must be public. Returns the IP to connect to."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrl("The host name could not be resolved.") from exc
    addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not addresses or not all(_ip_is_public(a) for a in addresses):
        raise UnsafeUrl("That host resolves to a private or reserved address.")
    return str(addresses[0])
