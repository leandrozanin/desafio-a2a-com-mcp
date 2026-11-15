"""Servidor MCP da Central de Salas (Streamable HTTP, revisao 2026-07-28).

Expoe tres tools (listar_salas, consultar_disponibilidade, reservar_sala) e o
resource politica://uso. A reserva em intervalo ocupado usa o MRTR do SDK: um
resolver devolve `Elicit(...)`, o SDK responde `input_required` com o
`requestState` selado e, no retry, reinjeta a escolha do cliente.

Subir:  python servidor-mcp/servidor.py      (porta padrao 7301, endpoint /mcp)
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import (
    AcceptedElicitation,
    Elicit,
    ElicitationResult,
    MCPServer,
    RequestStateSecurity,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field, create_model

from log_requests import RequestLogMiddleware
from rede import servir

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "dados"

HOST = os.environ.get("MCP_HOST", "localhost")
PORTA = int(os.environ.get("MCP_PORT", "7301"))

# A politica fixa a janela e a duracao; o fuso de referencia e o de Sao Paulo.
FUSO = timezone(timedelta(hours=-3))
ABRE, FECHA = time(8, 0), time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAX_ALTERNATIVAS = 3
# Entre 5 e 30 minutos, conforme o enunciado.
VALIDADE_REQUEST_STATE = 10 * 60.0

ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


def chave_request_state() -> bytes:
    """Le REQUEST_STATE_SECRET do ambiente; nunca ha segredo no codigo."""
    bruto = os.environ.get("REQUEST_STATE_SECRET", "")
    try:
        chave = bytes.fromhex(bruto)
    except ValueError:
        chave = bruto.encode()
    if len(chave) < 32:
        sys.exit(
            "REQUEST_STATE_SECRET ausente ou curta (minimo 32 bytes). Gere com: "
            'python -c "import secrets; print(secrets.token_hex(32))"'
        )
    return chave


# ---------------------------------------------------------------- dominio

SALAS: list[dict] = json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))
# Em memoria: reservas criadas valem para o processo e somem num restart.
RESERVAS: list[dict] = json.loads((DADOS / "reservas.json").read_text(encoding="utf-8"))
TEXTO_POLITICA = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
VERSAO_POLITICA = TEXTO_POLITICA.splitlines()[0].split(":", 1)[1].strip()


@dataclass(frozen=True)
class Pedido:
    sala: dict
    inicio: datetime
    fim: datetime


def sala_por_id(sala_id: str) -> dict | None:
    return next((s for s in SALAS if s["id"] == sala_id), None)


def instante(valor: str) -> datetime:
    try:
        dt = datetime.fromisoformat(valor)
    except ValueError:
        raise ToolError(f"Data invalida: {valor}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=FUSO)
    return dt


def validar(sala_id: str, inicio: str, fim: str) -> Pedido:
    """Aplica as regras da politica na ordem: sala, intervalo, janela, duracao."""
    sala = sala_por_id(sala_id)
    if sala is None:
        raise ToolError(f"Sala inexistente: {sala_id}")
    ini, fi = instante(inicio), instante(fim)
    if fi <= ini:
        raise ToolError(ERRO_INTERVALO)
    ini_local, fim_local = ini.astimezone(FUSO), fi.astimezone(FUSO)
    if (
        ini_local.time() < ABRE
        or fim_local.date() != ini_local.date()
        or fim_local.time() > FECHA
    ):
        raise ToolError(ERRO_JANELA)
    if fi - ini > DURACAO_MAXIMA:
        raise ToolError(ERRO_DURACAO)
    return Pedido(sala, ini, fi)


def conflitos(sala_id: str, inicio: datetime, fim: datetime) -> list[dict]:
    return [
        r
        for r in RESERVAS
        if r["sala"] == sala_id and instante(r["inicio"]) < fim and inicio < instante(r["fim"])
    ]


def alternativas(pedido: Pedido) -> list[str]:
    """Salas livres no intervalo, capacidade >= a pedida, por capacidade e id, no maximo 3."""
    candidatas = [
        s
        for s in SALAS
        if s["id"] != pedido.sala["id"]
        and s["capacidade"] >= pedido.sala["capacidade"]
        and not conflitos(s["id"], pedido.inicio, pedido.fim)
    ]
    candidatas.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in candidatas[:MAX_ALTERNATIVAS]]


def proximo_id() -> str:
    numeros = [int(r["id"].split("-")[1]) for r in RESERVAS]
    return f"res-{max(numeros, default=0) + 1:04d}"


def gravar(sala_id: str, inicio: str, fim: str, responsavel: str) -> dict:
    reserva = {"id": proximo_id(), "sala": sala_id, "inicio": inicio, "fim": fim, "responsavel": responsavel}
    RESERVAS.append(reserva)
    return reserva


# ---------------------------------------------------------------- contratos


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


class EscolhaSala(BaseModel):
    sala: str


def esquema_de_escolha(opcoes: list[str]) -> type[EscolhaSala]:
    """Schema plano da elicitation: `sala` restrita as alternativas, na ordem calculada."""
    return create_model(
        "EscolhaSala",
        __base__=EscolhaSala,
        sala=(Literal[tuple(opcoes)], Field(title="Sala", description="Sala alternativa escolhida")),
    )


# ---------------------------------------------------------------- servidor

mcp = MCPServer(
    name="central-de-salas",
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[chave_request_state()], ttl=VALIDADE_REQUEST_STATE),
)


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**s) for s in SALAS])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    pedido = validar(sala, inicio, fim)
    em_conflito = conflitos(pedido.sala["id"], pedido.inicio, pedido.fim)
    return Disponibilidade(
        sala=sala,
        livre=not em_conflito,
        conflitos=[ConflitoOut(**{k: r[k] for k in ("id", "inicio", "fim", "responsavel")}) for r in em_conflito],
    )


def escolha_de_sala(sala: str, inicio: str, fim: str) -> Elicit[EscolhaSala] | None:
    """Resolver do MRTR: so pergunta quando a sala esta ocupada e existe alternativa.

    Roda de novo a cada rodada a partir dos argumentos selados no requestState;
    o que o cliente ecoa so e aceito se a pergunta renderizada for a mesma.
    """
    try:
        pedido = validar(sala, inicio, fim)
    except ToolError:
        return None  # o corpo da tool devolve o erro de execucao
    if not conflitos(pedido.sala["id"], pedido.inicio, pedido.fim):
        return None
    opcoes = alternativas(pedido)
    if not opcoes:
        return None
    return Elicit(
        "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.",
        esquema_de_escolha(opcoes),
    )


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[EscolhaSala], Resolve(escolha_de_sala)],
) -> ReservaOut:
    pedido = validar(sala, inicio, fim)
    destino = pedido.sala["id"]
    if conflitos(destino, pedido.inicio, pedido.fim):
        if not isinstance(escolha, AcceptedElicitation):
            # decline ou cancel: conclui normalmente, sem reservar.
            return ReservaOut(reservado=False, motivo="recusado")
        if escolha.data is None:
            raise ToolError(ERRO_SEM_ALTERNATIVA)
        destino = escolha.data.sala
        if destino not in alternativas(pedido):
            raise ToolError(ERRO_SEM_ALTERNATIVA)
    reserva = gravar(destino, inicio, fim, responsavel)
    return ReservaOut(
        reserva=reserva["id"],
        sala=destino,
        inicio=inicio,
        fim=fim,
        responsavel=responsavel,
        politica=VERSAO_POLITICA,
    )


@mcp.resource("politica://uso", name="politica-de-uso", mime_type="text/markdown",
              description="Politica de uso das salas; a primeira linha declara a versao.")
def politica_de_uso() -> str:
    return TEXTO_POLITICA


def main() -> None:
    app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, host=HOST)
    print(f"servidor MCP em http://{HOST}:{PORTA}/mcp", file=sys.stderr, flush=True)
    servir(RequestLogMiddleware(app), HOST, PORTA)


if __name__ == "__main__":
    main()
