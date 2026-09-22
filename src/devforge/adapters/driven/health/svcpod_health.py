#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/health/
"""svc-pod host port forwarding check (legacy checker.py:679-701).

Rootless bridge publishes ports via rootlessport (userspace proxy). If that
process dies, containers stay healthy but the host cannot reach them
(2026-09-12 incident). Verified by a real TCP connect, not container state.
"""
from __future__ import annotations

import asyncio
import socket
from typing import Mapping

from devforge.ports.health_check import HealthCheckPort
from devforge.ports.types import HealthCheck

DEFAULT_COMPONENT = "svc:svc-pod-forwarding"


def _tcp_connect(port: int, timeout: float) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex(("127.0.0.1", port)) == 0
    finally:
        sock.close()


class SvcpodForwardingHealthChecker(HealthCheckPort):
    def __init__(self, ports: Mapping[int, str], component: str = DEFAULT_COMPONENT,
                 timeout: float = 1.0) -> None:
        self._ports = dict(ports)
        self._component = component
        self._timeout = timeout

    async def check_health(self) -> list[HealthCheck]:
        down: list[str] = []
        for port, label in self._ports.items():
            ok = await asyncio.to_thread(_tcp_connect, port, self._timeout)
            if not ok:
                down.append(f"{port}({label})")
        if down:
            return [HealthCheck(self._component, False,
                                "svc pod ports not forwarded: " + ", ".join(down))]
        return [HealthCheck(self._component, True, f"{len(self._ports)} ports forwarded")]
