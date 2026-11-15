"""Middleware ASGI que registra no stderr cada request JSON-RPC recebido.

Registra metodo, id, o nome da tool/URI quando houver e o traceparent do _meta.
O corpo lido e reentregue intacto ao app. Usa stderr direto, no lugar do
logging de protocolo (notifications/message) que a spec depreciou.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Any


def registrar(corpo: bytes) -> None:
    try:
        mensagem = json.loads(corpo)
    except ValueError:
        print(f"{datetime.now().isoformat(timespec='milliseconds')} corpo nao-JSON ({len(corpo)} bytes)",
              file=sys.stderr, flush=True)
        return
    for item in mensagem if isinstance(mensagem, list) else [mensagem]:
        if not isinstance(item, dict):
            continue
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        alvo = params.get("name") or params.get("uri") or "-"
        retry = " retry" if "requestState" in params else ""
        caps = meta.get("io.modelcontextprotocol/clientCapabilities", "-")
        print(
            f"{datetime.now().isoformat(timespec='milliseconds')} "
            f"method={item.get('method')} id={item.get('id')} alvo={alvo}{retry} "
            f"traceparent={meta.get('traceparent', '-')} "
            f"caps={json.dumps(caps, separators=(',', ':'))}",
            file=sys.stderr,
            flush=True,
        )


class RequestLogMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        partes: list[bytes] = []
        while True:
            evento = await receive()
            partes.append(evento.get("body", b""))
            if not evento.get("more_body"):
                break
        corpo = b"".join(partes)
        registrar(corpo)
        entregue = False

        async def reentregar() -> dict:
            nonlocal entregue
            if not entregue:
                entregue = True
                return {"type": "http.request", "body": corpo, "more_body": False}
            return await receive()

        await self.app(scope, reentregar, send)
