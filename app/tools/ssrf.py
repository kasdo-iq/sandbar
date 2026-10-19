"""SSRF protection shared by web_fetch and http_download.

Blocks: loopback, RFC1918/link-local IPv4, ULA/link-local IPv6, metadata
services (169.254.169.254, fd00:ec2::254), unspecified/reserved/multicast.
Each redirect hop re-checks the target host BEFORE following.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from app.logger import logger

MAX_REDIRECTS = 5

#: 169.254.169.254 style cloud metadata endpoint lives in the link-local
#: range already, but the IPv6 metadata addresses need an explicit entry.
_EXPLICIT_BLOCKED = {"169.254.169.254", "fd00:ec2::254", "fe80::1%eth0"}

BLOCKED_NETWORKS: list[ipaddress._BaseNetwork] = [  # type: ignore[name-defined]
    ipaddress.ip_network(n)
    for n in [
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    ]
]


class SSRFBlocked(Exception):
    """The target (or a redirect target) resolves to a forbidden range."""


def resolve_ips(host: str) -> list[str]:
    """Resolve a hostname to IPs; empty list means unresolvable."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        return []
    return [str(info[4][0]) for info in infos]


def public_ip_allowed(ip: str) -> bool:
    if ip in _EXPLICIT_BLOCKED:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_unspecified:
        return False
    if addr.is_multicast or addr.is_reserved:
        return False
    return all(addr not in net for net in BLOCKED_NETWORKS)


def validate_url(url: str) -> urlsplit.SplitResult:
    """Scheme/DNS level check, reused by every HTTP-based tool."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise SSRFBlocked(f"scheme not allowed: {parts.scheme!r}")
    host = parts.hostname
    if not host:
        raise SSRFBlocked("URL has no host")
    try:
        ips = resolve_ips(host)
    except Exception as exc:
        raise SSRFBlocked(f"dns failure for {host}: {exc}") from exc
    if not ips:
        raise SSRFBlocked(f"host does not resolve: {host}")
    for ip in ips:
        if not public_ip_allowed(ip):
            logger.warning(f"[ssrf] blocked {url} (resolved {ip})")
            raise SSRFBlocked(f"host resolves to a forbidden range ({ip})")
    return parts
