"""Sobe o uvicorn escutando em IPv4 e IPv6 de loopback.

No Windows, `localhost` resolve primeiro para ::1; escutando so em 127.0.0.1
cada request de um cliente que usa `localhost` espera ~2 s pelo fallback.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import uvicorn


def _socket(familia: socket.AddressFamily, host: str, porta: int) -> socket.socket | None:
    try:
        s = socket.socket(familia, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if familia == socket.AF_INET6:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        s.bind((host, porta))
        s.listen(128)
        s.setblocking(False)
        return s
    except OSError:
        return None


def servir(app: Any, host: str, porta: int) -> None:
    if host in ("localhost", "loopback"):
        sockets = [s for s in (_socket(socket.AF_INET, "127.0.0.1", porta), _socket(socket.AF_INET6, "::1", porta)) if s]
    else:
        familia = socket.AF_INET6 if ":" in host else socket.AF_INET
        sockets = [s for s in (_socket(familia, host, porta),) if s]
    if not sockets:
        raise SystemExit(f"nao consegui escutar em {host}:{porta}")
    servidor = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    asyncio.run(servidor.serve(sockets=sockets))
