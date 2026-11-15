# A Ponte — agente A2A com MCP por dentro

Entrega do desafio *A Ponte* (enunciado original no repositório base [devfullcycle/desafio-a2a-com-mcp](https://github.com/devfullcycle/desafio-a2a-com-mcp)).

Dois processos, em Python, falando HTTP entre si:

| Processo | Pasta | Porta | Papel |
|----------|-------|-------|-------|
| Servidor MCP `central-de-salas` | `servidor-mcp/` | 7301, endpoint `/mcp` (Streamable HTTP, revisão `2026-07-28`) | 3 tools (`listar_salas`, `consultar_disponibilidade`, `reservar_sala`), resource `politica://uso`, MRTR na reserva |
| Agente `Central de Salas` | `agente/` | 7300, card em `/.well-known/agent-card.json`, JSON-RPC em `/a2a` | Servidor A2A v1.0 por fora (`SendMessage`, `GetTask`), host MCP por dentro |

Stack (versões travadas em `pyproject.toml` e `uv.lock`): SDK oficial `mcp==2.3.0` (v2), `starlette==1.9.0`, `uvicorn==0.54.0`, `httpx2==2.15.1`, `pydantic==2.14.1`. Sem LLM e sem SDK de provedor de LLM: o agente interpreta o pedido em formato fixo e decide por regra.

## Como rodar

Pré-requisitos: Python 3.12+ e [uv](https://docs.astral.sh/uv/). O validador roda com qualquer Python 3.10+, sem dependências.

```bash
git clone https://github.com/leandrozanin/desafio-a2a-com-mcp.git
cd desafio-a2a-com-mcp
uv sync                                   # cria .venv com as versões do uv.lock
```

Gere a chave de integridade do `requestState` (mínimo 32 bytes de aleatoriedade) e exporte-a **no terminal do servidor MCP**. Nunca versione o valor:

```bash
# bash / zsh
export REQUEST_STATE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
```

```powershell
# PowerShell
$env:REQUEST_STATE_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
```

Terminal 1 — servidor MCP (o stderr mostra método, id, alvo, traceparent e capabilities de cada request):

```bash
uv run python servidor-mcp/servidor.py
```

Terminal 2 — agente:

```bash
uv run python agente/agente.py
```

Terminal 3 — validador (com os dois processos recém-iniciados):

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Variáveis opcionais, com os padrões do enunciado: `MCP_HOST`/`MCP_PORT` (`localhost`/7301), `AGENT_HOST`/`AGENT_PORT` (`localhost`/7300), `MCP_URL` (`http://127.0.0.1:7301/mcp`, para onde o agente aponta) e `AGENT_PUBLIC_URL` (`http://127.0.0.1:7300`, base da URL anunciada no card). Com `localhost`, cada processo escuta em `127.0.0.1` **e** em `::1`: no Windows `localhost` resolve primeiro para IPv6, e escutar só em IPv4 custava ~2 s de fallback por request.

Exemplo manual, com o corpo de `exemplos/wire/08-a2a-send-message.json`:

```bash
python3 -c "import json;print(json.dumps(json.load(open('exemplos/wire/08-a2a-send-message.json'))['request']['body']))" \
  | curl -s http://localhost:7300/a2a -H 'Content-Type: application/json' \
      -H 'traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01' -d @-
# -> TASK_STATE_INPUT_REQUIRED com a mensagem "alternativas: sala-fusca, sala-mirante"
```

## Onde a ponte acontece

A costura está em `agente/agente.py`. O host MCP (`agente/mcp_host.py`, `ClienteMCP.chamar_tool`) não registra callback de elicitation: quando o servidor responde `resultType: "input_required"`, ele devolve um objeto `InputRequired` cru — a chave atribuída pelo servidor, o pedido `elicitation/create` e o `requestState` — em vez de responder sozinho. Em `Agente._tratar`, o ramo `isinstance(resultado, InputRequired)` transforma isso em pausa: guarda uma `Pausa` (tool, argumentos originais e o `InputRequired`) **dentro daquela Task** e leva a Task para `TASK_STATE_INPUT_REQUIRED` com a linha `alternativas: <ids>` na ordem do enum.

O `requestState` volta para o servidor em `Agente._continuar`: quando chega o `SendMessage` de continuação com `taskId` e `escolha=<valor>`, o agente valida o valor contra o enum da elicitation (fora dele, a Task continua pausada e repete a pergunta) e refaz o `tools/call` original com `inputResponses` usando a mesma chave e o `requestState` ecoado sem modificação (`input_responses={pausa.input_required.chave: resposta}`, `request_state=pausa.input_required.request_state`). O retry é um request novo: o id JSON-RPC sai de um contador (`next(self._ids)` em `ClienteMCP._request`), então nunca repete o id do request inicial. `escolha=recusar` vira `action: "decline"` e a Task termina em `TASK_STATE_CANCELED`.

Do lado do servidor (`servidor-mcp/servidor.py`), quem pergunta é o resolver `escolha_de_sala`: ele só devolve `Elicit(...)` quando a sala está ocupada e existe alternativa; o SDK transforma isso no `input_required` e, no retry, reinjeta a escolha no parâmetro `escolha` de `reservar_sala`. O resolver roda de novo em cada rodada a partir dos argumentos — nada fica em memória entre a pausa e o retry.

## Decisões técnicas

**Proteção do `requestState`.** Uso o utilitário do próprio SDK, `RequestStateSecurity` (`servidor-mcp/servidor.py`), que instala o `RequestStateBoundary` como middleware: todo `requestState` que sai é **cifrado e autenticado com AES-256-GCM** (chave derivada por HKDF-SHA256 de `REQUEST_STATE_SECRET`, lida só do ambiente em `chave_request_state()`, que recusa chaves com menos de 32 bytes) e todo `requestState` que volta é verificado antes de qualquer handler rodar. O envelope selado amarra método, nome da tool, um digest dos argumentos, audiência (o nome do servidor) e a expiração. Consequências: trocar um caractere do token, apresentá-lo depois de expirado ou reenviar argumentos diferentes dos selados dá `-32602 Invalid or expired requestState` (o motivo real vai só para o log do servidor). Como a chave vem do ambiente, e não do processo, um retry apresentado depois de reiniciar o servidor MCP continua válido.

**Validade.** 10 minutos (`VALIDADE_REQUEST_STATE`), dentro da janela de 5 a 30 minutos.

**Estado das Tasks.** Em memória no agente: `Agente.tarefas`, um `dict` por id da Task. Cada `Tarefa` tem sua própria `asyncio.Lock` e sua própria `Pausa`, então duas Tasks pausadas ao mesmo tempo nunca trocam de `requestState`. O `requestState` fica só no objeto `Pausa` e nunca é serializado: `Tarefa.wire()` expõe id, contextId, status, history e artifacts. Estado terminal é definitivo: `SendMessage` para Task `COMPLETED`/`CANCELED`/`FAILED` é recusado com o erro JSON-RPC `-32004` (UnsupportedOperation); Task inexistente dá `-32001` (TaskNotFound). As reservas do servidor MCP também ficam em memória, como o enunciado permite.

**Host MCP sem sessão.** Todo request leva no `_meta` `protocolVersion`, `clientInfo`, `clientCapabilities: {"elicitation": {"form": {}}}` e um `traceparent` com o trace-id da Task (span novo por request), e espelha `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name` nos headers. A cada Task o agente faz `tools/list` antes do primeiro `tools/call` e escolhe a tool cujo `inputSchema.required` coincide com os campos do pedido (sem lista fixa de tools no código); a versão da política vem de `resources/read politica://uso`, primeira linha, e vai para o campo `politica` do artifact `reserva`.

**Fronteira de responsabilidades.** O agente não sabe nada de salas: conflito, janela, duração e alternativas são decididos pelo servidor; o agente só traduz protocolo e repassa a mensagem de erro da tool para o histórico da Task (`TASK_STATE_FAILED`).

**Limitações do SDK.** Nenhuma impediu algum item. Observação: com uma única alternativa, o gerador de schema do SDK emite `const` em vez de `enum` (aceito pelo enunciado); o agente lê os dois.

## Saída do validador

Última execução, com os dois processos recém-iniciados:

```text
trace-id desta execucao: 321e4a3c552374c15763806f9528587c
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
