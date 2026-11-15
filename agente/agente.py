"""Agente A2A da Central de Salas: servidor A2A por fora, host MCP por dentro.

Por fora fala A2A v1.0 (binding JSON-RPC 2.0 sobre HTTP): publica o Agent Card
em /.well-known/agent-card.json e atende SendMessage e GetTask em /a2a.
Por dentro e um cliente MCP do servidor de salas (ver mcp_host.py).

Nao ha LLM: o pedido chega em formato fixo e o agente so traduz protocolo.
Regra de sala (conflito, politica, alternativas) e decisao do servidor MCP.

Subir:  python agente/agente.py      (porta padrao 7300)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_host import ClienteMCP, ErroMCP, InputRequired, Rastro, escolher_tool
from rede import servir

HOST = os.environ.get("AGENT_HOST", "localhost")
PORTA = int(os.environ.get("AGENT_PORT", "7300"))
URL_PUBLICA = os.environ.get("AGENT_PUBLIC_URL", f"http://127.0.0.1:{PORTA}")
URL_MCP = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")

# Estados da Task (A2A v1.0)
SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"
TERMINAIS = {COMPLETED, CANCELED, FAILED}

# Codigos de erro JSON-RPC (A2A v1.0)
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
TASK_NOT_FOUND = -32001
UNSUPPORTED_OPERATION = -32004

PEDIDO = re.compile(
    r"^reservar\s+sala=(?P<sala>\S+)\s+inicio=(?P<inicio>\S+)\s+fim=(?P<fim>\S+)\s+responsavel=(?P<responsavel>.+)$"
)
ESCOLHA = re.compile(r"^escolha=(?P<valor>\S+)$")
RECUSAR = "recusar"

AGENT_CARD = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [
        {"url": f"{URL_PUBLICA}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
    ],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "reservar-sala",
            "name": "Reservar sala",
            "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            "tags": ["salas", "agenda"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
            "examples": [
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ],
        }
    ],
}


def novo_id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


def log(texto: str) -> None:
    print(f"{datetime.now().isoformat(timespec='milliseconds')} {texto}", file=sys.stderr, flush=True)


class ErroRPC(Exception):
    def __init__(self, codigo: int, mensagem: str) -> None:
        super().__init__(mensagem)
        self.codigo = codigo
        self.mensagem = mensagem


@dataclass
class Pausa:
    """O estado MCP interrompido de uma Task. Nunca sai do agente.

    O `requestState` e opaco: o agente guarda e ecoa, sem abrir nem interpretar.
    """

    tool: str
    argumentos: dict
    input_required: InputRequired


@dataclass
class Tarefa:
    id: str
    context_id: str
    rastro: Rastro
    estado: str = SUBMITTED
    mensagem_status: dict | None = None
    historico: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    pausa: Pausa | None = None
    trava: asyncio.Lock = field(default_factory=asyncio.Lock)

    def transitar(self, estado: str, texto: str | None = None) -> None:
        if self.estado in TERMINAIS:
            raise RuntimeError(f"Task {self.id} ja esta em estado terminal ({self.estado})")
        self.estado = estado
        if texto is not None:
            self.responder(texto)
        log(f"task={self.id} estado={estado} trace-id={self.rastro.trace_id}")

    def responder(self, texto: str) -> None:
        """Mensagem do agente na Task, sem mudar o estado."""
        mensagem = {
            "messageId": novo_id("msg"),
            "role": "ROLE_AGENT",
            "parts": [{"text": texto}],
            "taskId": self.id,
            "contextId": self.context_id,
        }
        self.mensagem_status = mensagem
        self.historico.append(mensagem)

    def wire(self) -> dict:
        status: dict[str, Any] = {"state": self.estado, "timestamp": datetime.now(timezone.utc).isoformat()}
        if self.mensagem_status is not None:
            status["message"] = self.mensagem_status
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": self.historico,
            "artifacts": self.artifacts,
        }


class Agente:
    def __init__(self, mcp: ClienteMCP) -> None:
        self.mcp = mcp
        self.tarefas: dict[str, Tarefa] = {}

    # ------------------------------------------------------------ A2A

    async def send_message(self, params: dict, traceparent: str | None) -> dict:
        mensagem = params.get("message")
        if not isinstance(mensagem, dict) or not isinstance(mensagem.get("parts"), list):
            raise ErroRPC(INVALID_PARAMS, "params.message com parts e obrigatorio")
        texto = " ".join(p.get("text", "") for p in mensagem["parts"] if isinstance(p, dict)).strip()
        rastro_header = Rastro.de_header(traceparent)
        task_id = mensagem.get("taskId")

        if not task_id:
            tarefa = Tarefa(
                id=novo_id("task"),
                context_id=mensagem.get("contextId") or novo_id("ctx"),
                rastro=rastro_header or Rastro.novo(),
            )
            self.tarefas[tarefa.id] = tarefa
            tarefa.historico.append(mensagem)
            log(f"task={tarefa.id} estado={SUBMITTED} trace-id={tarefa.rastro.trace_id}")
            async with tarefa.trava:
                await self._iniciar(tarefa, texto)
            return {"task": tarefa.wire()}

        tarefa = self.tarefas.get(task_id)
        if tarefa is None:
            raise ErroRPC(TASK_NOT_FOUND, f"Task {task_id} nao encontrada")
        async with tarefa.trava:
            if tarefa.estado in TERMINAIS:
                raise ErroRPC(UNSUPPORTED_OPERATION, f"Task {task_id} esta em estado terminal ({tarefa.estado})")
            if tarefa.estado != INPUT_REQUIRED or tarefa.pausa is None:
                raise ErroRPC(UNSUPPORTED_OPERATION, f"Task {task_id} nao esta aguardando entrada")
            if rastro_header is not None:
                tarefa.rastro = rastro_header
            tarefa.historico.append(mensagem)
            await self._continuar(tarefa, texto)
        return {"task": tarefa.wire()}

    def get_task(self, params: dict) -> dict:
        tarefa = self.tarefas.get(params.get("id", ""))
        if tarefa is None:
            raise ErroRPC(TASK_NOT_FOUND, f"Task {params.get('id')} nao encontrada")
        return {"task": tarefa.wire()}

    # ------------------------------------------------------------ a ponte

    async def _iniciar(self, tarefa: Tarefa, texto: str) -> None:
        tarefa.transitar(WORKING)
        casamento = PEDIDO.match(texto)
        if casamento is None:
            tarefa.transitar(
                FAILED,
                "Pedido invalido: use reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>",
            )
            return
        argumentos = casamento.groupdict()
        try:
            # Descoberta em runtime: tools/list antes de qualquer tools/call.
            tools = await self.mcp.listar_tools(tarefa.rastro)
            tool = escolher_tool(tools, argumentos)
            if tool is None:
                tarefa.transitar(FAILED, "O servidor MCP nao oferece uma tool que atenda a este pedido")
                return
            resultado = await self.mcp.chamar_tool(tool["name"], argumentos, tarefa.rastro)
            await self._tratar(tarefa, tool["name"], argumentos, resultado)
        except ErroMCP as erro:
            tarefa.transitar(FAILED, erro.mensagem)

    async def _continuar(self, tarefa: Tarefa, texto: str) -> None:
        pausa = tarefa.pausa
        assert pausa is not None
        casamento = ESCOLHA.match(texto)
        valor = casamento.group("valor") if casamento else None
        if valor != RECUSAR and valor not in pausa.input_required.opcoes:
            # Fora do enum da elicitation: continua pausada, repetindo a pergunta.
            tarefa.responder(linha_de_alternativas(pausa.input_required))
            return

        tarefa.transitar(WORKING)
        if valor == RECUSAR:
            resposta = {"action": "decline"}
        else:
            resposta = {"action": "accept", "content": {pausa.input_required.campo: valor}}
        try:
            # O retry e um request novo (id novo), com a mesma chave e o requestState ecoado intacto.
            resultado = await self.mcp.chamar_tool(
                pausa.tool,
                pausa.argumentos,
                tarefa.rastro,
                input_responses={pausa.input_required.chave: resposta},
                request_state=pausa.input_required.request_state,
            )
            tarefa.pausa = None
            await self._tratar(tarefa, pausa.tool, pausa.argumentos, resultado, recusado=valor == RECUSAR)
        except ErroMCP as erro:
            tarefa.pausa = None
            tarefa.transitar(FAILED, erro.mensagem)

    async def _tratar(
        self, tarefa: Tarefa, tool: str, argumentos: dict, resultado: dict | InputRequired, recusado: bool = False
    ) -> None:
        if isinstance(resultado, InputRequired):
            # MCP input_required -> A2A TASK_STATE_INPUT_REQUIRED; o requestState fica na Task.
            tarefa.pausa = Pausa(tool=tool, argumentos=argumentos, input_required=resultado)
            tarefa.transitar(INPUT_REQUIRED, linha_de_alternativas(resultado))
            return
        texto = " ".join(c.get("text", "") for c in resultado.get("content") or [] if c.get("type", "text") == "text")
        if resultado.get("isError"):
            tarefa.transitar(FAILED, texto)
            return
        dados = resultado.get("structuredContent") or {}
        if recusado or dados.get("reservado") is False:
            tarefa.transitar(CANCELED, "Reserva recusada: nenhuma sala foi reservada.")
            return
        politica = await self.mcp.versao_da_politica(tarefa.rastro)
        reserva = {
            "reserva": dados.get("reserva"),
            "sala": dados.get("sala"),
            "inicio": dados.get("inicio"),
            "fim": dados.get("fim"),
            "responsavel": dados.get("responsavel"),
            "politica": politica,
        }
        tarefa.artifacts.append(
            {"artifactId": novo_id("art"), "name": "reserva", "parts": [{"text": json.dumps(reserva, ensure_ascii=False)}]}
        )
        tarefa.transitar(COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")


def linha_de_alternativas(pedido: InputRequired) -> str:
    return "alternativas: " + ", ".join(pedido.opcoes)


# ------------------------------------------------------------ HTTP


def resposta_rpc(id_rpc: Any, *, resultado: Any = None, erro: ErroRPC | None = None) -> JSONResponse:
    corpo: dict[str, Any] = {"jsonrpc": "2.0", "id": id_rpc}
    if erro is not None:
        corpo["error"] = {"code": erro.codigo, "message": erro.mensagem}
    else:
        corpo["result"] = resultado
    return JSONResponse(corpo)


def criar_app() -> Starlette:
    cliente = ClienteMCP(URL_MCP)
    agente = Agente(cliente)

    async def agent_card(_: Request) -> JSONResponse:
        return JSONResponse(AGENT_CARD)

    async def a2a(request: Request) -> JSONResponse:
        try:
            corpo = json.loads(await request.body())
        except ValueError:
            return resposta_rpc(None, erro=ErroRPC(PARSE_ERROR, "JSON invalido"))
        if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or "method" not in corpo:
            return resposta_rpc(corpo.get("id") if isinstance(corpo, dict) else None,
                                erro=ErroRPC(INVALID_REQUEST, "requisicao JSON-RPC invalida"))
        id_rpc, metodo = corpo.get("id"), corpo["method"]
        params = corpo.get("params") if isinstance(corpo.get("params"), dict) else {}
        log(f"a2a method={metodo} id={id_rpc} traceparent={request.headers.get('traceparent', '-')}")
        try:
            if metodo == "SendMessage":
                resultado = await agente.send_message(params, request.headers.get("traceparent"))
            elif metodo == "GetTask":
                resultado = agente.get_task(params)
            else:
                raise ErroRPC(METHOD_NOT_FOUND, f"metodo nao suportado: {metodo}")
        except ErroRPC as erro:
            return resposta_rpc(id_rpc, erro=erro)
        return resposta_rpc(id_rpc, resultado=resultado)

    @asynccontextmanager
    async def ciclo(_: Starlette):
        yield
        await cliente.fechar()

    return Starlette(
        routes=[
            Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
            Route("/a2a", a2a, methods=["POST"]),
        ],
        lifespan=ciclo,
    )


def main() -> None:
    log(f"agente A2A em http://{HOST}:{PORTA} (card em /.well-known/agent-card.json, JSON-RPC em /a2a); MCP em {URL_MCP}")
    servir(criar_app(), HOST, PORTA)


if __name__ == "__main__":
    main()
