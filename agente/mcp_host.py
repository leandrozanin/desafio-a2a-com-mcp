"""Lado de dentro do agente: host MCP falando Streamable HTTP com o servidor.

Cliente JSON-RPC enxuto e explicito, sem callback de elicitation: um
`input_required` volta cru para quem chamou, que decide o que fazer com ele
(no agente, pausar a Task). Cada request:

- leva no `_meta` a versao de protocolo, as capabilities (elicitation em form
  mode) e o traceparent da Task, porque o protocolo nao tem sessao;
- espelha nos headers `MCP-Protocol-Version`, `Mcp-Method` e, em tools/call e
  resources/read, `Mcp-Name`;
- usa um id JSON-RPC novo; o retry do MRTR e um request independente.
"""

from __future__ import annotations

import itertools
import json
import secrets
from dataclasses import dataclass, field
from typing import Any

import httpx2

PROTOCOLO = "2026-07-28"
CLIENT_INFO = {"name": "agente-central-de-salas", "version": "1.0.0"}
CAPABILITIES = {"elicitation": {"form": {}}}
URI_POLITICA = "politica://uso"


class ErroMCP(Exception):
    """Erro de protocolo (campo `error` do JSON-RPC) devolvido pelo servidor MCP."""

    def __init__(self, codigo: int, mensagem: str, dados: Any = None) -> None:
        super().__init__(f"{mensagem} (codigo {codigo})")
        self.codigo = codigo
        self.mensagem = mensagem
        self.dados = dados


@dataclass
class Rastro:
    """Contexto W3C de uma Task: o trace-id e fixo, cada request MCP ganha um span novo."""

    trace_id: str
    flags: str = "01"

    @classmethod
    def de_header(cls, valor: str | None) -> Rastro | None:
        if not valor:
            return None
        partes = valor.strip().split("-")
        if len(partes) != 4 or len(partes[1]) != 32 or len(partes[2]) != 16:
            return None
        try:
            int(partes[1], 16), int(partes[2], 16)
        except ValueError:
            return None
        if set(partes[1]) == {"0"}:
            return None
        return cls(trace_id=partes[1].lower(), flags=partes[3][:2] or "01")

    @classmethod
    def novo(cls) -> Rastro:
        return cls(trace_id=secrets.token_hex(16))

    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{secrets.token_hex(8)}-{self.flags}"


@dataclass
class InputRequired:
    """O `input_required` do servidor, como chegou: a chave, o pedido e o estado opaco."""

    chave: str
    pedido: dict
    request_state: str = field(repr=False)

    @property
    def esquema(self) -> dict:
        return (self.pedido.get("params") or {}).get("requestedSchema") or {}

    @property
    def campo(self) -> str:
        """Nome da propriedade pedida (o schema e plano, com um unico campo)."""
        obrigatorios = self.esquema.get("required") or list((self.esquema.get("properties") or {}))
        return obrigatorios[0]

    @property
    def opcoes(self) -> list[str]:
        prop = (self.esquema.get("properties") or {}).get(self.campo) or {}
        if "enum" in prop:
            return list(prop["enum"])
        if "const" in prop:
            return [prop["const"]]
        return []


class ClienteMCP:
    def __init__(self, url: str) -> None:
        self.url = url
        self._ids = itertools.count(1)
        self._http = httpx2.AsyncClient(timeout=30.0)

    async def fechar(self) -> None:
        await self._http.aclose()

    async def _request(self, metodo: str, params: dict, rastro: Rastro, nome: str | None = None) -> dict:
        corpo = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": metodo,
            "params": {
                **params,
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": PROTOCOLO,
                    "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
                    "io.modelcontextprotocol/clientCapabilities": CAPABILITIES,
                    "traceparent": rastro.traceparent(),
                },
            },
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOLO,
            "Mcp-Method": metodo,
        }
        if nome is not None:
            headers["Mcp-Name"] = nome
        resposta = await self._http.post(self.url, json=corpo, headers=headers)
        mensagem = _decodificar(resposta.text, resposta.headers.get("content-type", ""))
        if "error" in mensagem:
            erro = mensagem["error"] or {}
            raise ErroMCP(erro.get("code", 0), erro.get("message", "erro desconhecido"), erro.get("data"))
        return mensagem.get("result") or {}

    async def listar_tools(self, rastro: Rastro) -> list[dict]:
        resultado = await self._request("tools/list", {}, rastro)
        return list(resultado.get("tools") or [])

    async def versao_da_politica(self, rastro: Rastro) -> str:
        resultado = await self._request("resources/read", {"uri": URI_POLITICA}, rastro, nome=URI_POLITICA)
        texto = "".join(c.get("text", "") for c in resultado.get("contents") or [])
        primeira = texto.splitlines()[0] if texto else ""
        chave, _, valor = primeira.partition(":")
        if chave.strip().lower() != "versao" or not valor.strip():
            raise ErroMCP(0, f"o resource {URI_POLITICA} nao declara a versao na primeira linha")
        return valor.strip()

    async def chamar_tool(
        self,
        nome: str,
        argumentos: dict,
        rastro: Rastro,
        *,
        input_responses: dict | None = None,
        request_state: str | None = None,
    ) -> dict | InputRequired:
        """Chama a tool. Devolve o resultado `complete` ou o `input_required` cru."""
        params: dict[str, Any] = {"name": nome, "arguments": argumentos}
        if input_responses is not None:
            params["inputResponses"] = input_responses
        if request_state is not None:
            params["requestState"] = request_state
        resultado = await self._request("tools/call", params, rastro, nome=nome)
        if resultado.get("resultType") == "input_required":
            pedidos = resultado.get("inputRequests") or {}
            if len(pedidos) != 1 or not resultado.get("requestState"):
                raise ErroMCP(0, "input_required fora do contrato: esperado um inputRequest e um requestState")
            chave, pedido = next(iter(pedidos.items()))
            return InputRequired(chave=chave, pedido=pedido, request_state=resultado["requestState"])
        return resultado


def _decodificar(corpo: str, content_type: str) -> dict:
    """Aceita resposta JSON ou SSE (ultimo evento `data:`), como o transporte permite."""
    if "text/event-stream" in content_type:
        dados = [linha[5:].strip() for linha in corpo.splitlines() if linha.startswith("data:")]
        corpo = dados[-1] if dados else "{}"
    try:
        return json.loads(corpo)
    except ValueError:
        raise ErroMCP(0, f"resposta MCP invalida: {corpo[:120]}") from None


def escolher_tool(tools: list[dict], argumentos: dict) -> dict | None:
    """Escolhe, entre as tools descobertas, a cujo inputSchema os argumentos atendem exatamente.

    O agente nao carrega a lista de tools no codigo: o pedido define os campos
    (sala, inicio, fim, responsavel) e a tool certa e a que exige exatamente esses.
    """
    campos = set(argumentos)
    for tool in tools:
        esquema = tool.get("inputSchema") or {}
        obrigatorios = set(esquema.get("required") or [])
        if obrigatorios == campos:
            return tool
    return None
