from __future__ import annotations

import socket
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from coding_agent.profile.schema import Service

DEFAULT_PROBE_TIMEOUT = 2.0


class ServiceProber(Protocol):
    """A port so tests can fake the network (mirrors `GitHubClient`'s real
    and fake adapters, contract §5's `L3` design)."""

    def probe(self, host: str, port: int, *, timeout: float) -> bool: ...


class SocketServiceProber:
    """The real adapter: a bare TCP connect, nothing more. The deployment
    provides the service (contract §7); the agent only verifies reachability."""

    def probe(self, host: str, port: int, *, timeout: float = DEFAULT_PROBE_TIMEOUT) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False


@dataclass(frozen=True)
class ServiceUnreachable:
    service: Service
    reason: str


def check_service(
    service: Service,
    env: Mapping[str, str],
    prober: ServiceProber,
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> ServiceUnreachable | None:
    """`None` means the declared service is reachable.

    An unreachable declared service is Unsupported Environment (contract
    §7, L2-14), checked before implementation begins: the profile is
    correct about what it needs, the deployment has not supplied it.
    """
    raw_url = env.get(service.url_env)
    if not raw_url:
        return ServiceUnreachable(
            service, f"{service.url_env} is not set in the deployment environment"
        )

    netloc_source = raw_url if "//" in raw_url else f"//{raw_url}"
    parsed = urlsplit(netloc_source)
    host, port = parsed.hostname, parsed.port
    if host is None or port is None:
        return ServiceUnreachable(
            service, f"{service.url_env}={raw_url!r} does not carry a host and port to probe"
        )

    if not prober.probe(host, port, timeout=timeout):
        return ServiceUnreachable(service, f"TCP connect to {host}:{port} failed")
    return None
