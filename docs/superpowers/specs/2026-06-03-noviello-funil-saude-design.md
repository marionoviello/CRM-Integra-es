# Spec — Atendente IA Saúde (Jurichat ↔ Claude)

**Data:** 2026-06-03
**Autor:** Mario Noviello + Claude (brainstorming colaborativo)
**Status:** Aprovado para implementação
**Próximo passo:** `writing-plans` → plano de implementação

---

## 1. Objetivo

Construir um serviço Python que recebe webhooks do Jurichat, atende leads de plano de saúde via WhatsApp usando Claude com a skill `noviello-saude-suplementar`, para a conversação automaticamente quando o lead manifesta intent de fechar (ou em casos de handoff), e roda follow-up de leads inativos conforme regra de etiquetas do CRM.

Piloto do vertical SAÚDE para validar o ciclo lead → qualificação → handoff humano com IA real antes de expandir para outros verticais.

## 2. Escopo

### No escopo
- Servidor HTTP que recebe `POST /webhooks/jurichat` (mensagens entrantes)
- Triagem e conversação iterativa com Claude (skill `noviello-saude-suplementar`)
- Decisão estruturada do Claude: `responder` | `propor` | `handoff`
- Envio de respostas via `POST /conversation/send-message` do Jurichat
- Persistência de estado de cada lead em SQLite local
- Scheduler que dispara follow-ups com base em timer + regra de etiquetas
- Notificação para Mario quando lead atinge `aguardando_humano`
- Deploy em VPS Hostinger Brasil

### Fora do escopo
- ZapSign (contratos / assinatura digital)
- Asaas (cobrança) — coberta por integração nativa Jurichat → Juridiq → Asaas
- Juridiq (registro do caso) — coberta pela integração nativa
- Mover lead para etapa "Ganho" no CRM automaticamente
- Gate humano via comando WhatsApp (`APROVAR <id>`)
- Painel/dashboard próprio
- Outros verticais (aéreo, sucessório, etc.)
- Atendimento de múltiplos canais (apenas WhatsApp via Jurichat)

## 3. Decisões de design

| Tema | Decisão |
|---|---|
| Linguagem | Python 3.11 |
| Framework HTTP | FastAPI |
| Storage | SQLite local (arquivo único) |
| LLM | Claude via Anthropic SDK oficial (modelo: `claude-sonnet-4.5` ou superior na época do deploy) |
| Host | VPS Hostinger (Brasil/SP, já existente, ex-host do N8N) |
| Reverse proxy | Nginx + Let's Encrypt |
| Process manager | systemd (service + timer) |
| Gerenciador de deps | `uv` |
| Modo do Claude | "Modo A" — conversacional até detectar intent de fechar, então para |
| Condição de parada | (1) Claude detecta intent de fechar `→ propor`; (2) handoff necessário `→ handoff`; (3) limite de 20 turnos atingido; (4) Mario responde manualmente |
| Histórico de conversa | NÃO armazenado localmente; sempre puxado do Jurichat via `GET /conversation/{id}` |
| Notificação Mario | Mensagem WhatsApp via Jurichat send-message para número pessoal do Mario |
| Persistência | Estado próprio em SQLite (resiliente a downtime das plataformas externas) |

## 4. Arquitetura

### 4.1 Componentes

```
┌──────────────────────────────────────────────────────────────┐
│  noviello-funil-saude (FastAPI, porta 8000 local)            │
│                                                               │
│   ┌────────┐  ┌───────┐  ┌───────┐  ┌──────────┐            │
│   │webhooks│→ │ state │← │ brain │→ │ outbound │            │
│   │entrada │  │sqlite │  │claude │  │  jurichat│            │
│   └────────┘  └───────┘  └───────┘  └──────────┘            │
│                  ↑                                            │
│                  └────  scheduler  (systemd timer a cada 1h) │
└──────────────────────────────────────────────────────────────┘
```

### 4.2 Responsabilidade de cada módulo

| Módulo | Faz | Não faz |
|---|---|---|
| `webhooks` | Recebe POST, valida HMAC-SHA256 do header `X-JuriChat-Signature`, checa idempotência, responde 200 em <100ms, enfileira BackgroundTask | Não chama Claude, não decide ação, não persiste estado |
| `state` | Leitura e escrita do SQLite; transições atômicas; único ponto que toca o DB | Não chama API externa, não decide ação |
| `brain` | Monta prompt (skill + histórico puxado do Jurichat); chama Anthropic SDK; valida JSON estruturado de retorno | Não persiste, não envia |
| `outbound` | Chamadas HTTP de saída para Jurichat (send-message, list-tags, notificar Mario); retry com backoff exponencial | Não decide o que enviar |
| `scheduler` | Script invocado por systemd timer; varre `leads WHERE proxima_acao_em < now()`; verifica regra de etiquetas; dispara follow-up apropriado | Não recebe webhook |

### 4.3 Por que essa divisão

Cada módulo tem **uma única razão para mudar**:

- API do Jurichat mudou → mexo só em `outbound`
- Skill jurídica evoluiu → atualizo o arquivo de skill, não toco no código
- Adiciono novo estado → mexo em `state` e `scheduler`
- Webhooks ganham novo evento → mexo só em `webhooks`

## 5. Máquina de estados

Lead novo entra direto em `em_conversa` no momento do primeiro webhook — não há estado `novo_lead` intermediário (seria redundante, já que sempre criamos+processamos no mesmo ciclo).

```
[em_conversa] ─────────────────────────────────────┐
   │                                                │
   │ Claude.acao = propor    → [aguardando_humano] │ ← notifica Mario 🔥
   │ Claude.acao = handoff   → [aguardando_humano] │ ← notifica Mario ⚠️
   │ turnos >= 20            → [aguardando_humano] │ ← notifica Mario ⏸
   │ Mario responde manual   → [aguardando_humano] │ ← (sem notificação)
   │ 48h sem msg do lead     → [follow_up_1_enviado]
   ↓
[follow_up_1_enviado]
   │ 72h sem resposta        → [follow_up_2_enviado]
   │ lead responde           → [em_conversa]
   │ Mario responde manual   → [aguardando_humano]
   ↓
[follow_up_2_enviado]
   │ 24h sem resposta        → [encerrado_sem_resposta]
   │ lead responde positivo  → [em_conversa]
   │ lead responde "encerra" → [encerrado_sem_resposta]
   ↓
[encerrado_sem_resposta]   ← lead pode reabrir mandando msg → [em_conversa]
[aguardando_humano]         ← terminal pro fluxo automático
```

**Erros NÃO viram estado.** Cada lead tem coluna `erro_atual TEXT NULL` independente do estado. Lead com erro fica parado no estado que tava, com o erro registrado, até intervenção manual ou retry agendado.

## 6. Esquema SQLite

```sql
-- Tabela principal: 1 linha por lead
CREATE TABLE leads (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    jurichat_lead_id         TEXT NOT NULL UNIQUE,
    jurichat_conversation_id TEXT NOT NULL,
    contato_telefone         TEXT NOT NULL,
    contato_nome             TEXT,
    estado                   TEXT NOT NULL,
    turnos                   INTEGER NOT NULL DEFAULT 0,
    ultima_msg_lead_em       TEXT,
    proxima_acao_em          TEXT,
    erro_atual               TEXT,
    criado_em                TEXT NOT NULL DEFAULT (datetime('now')),
    atualizado_em            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_leads_proxima_acao
    ON leads(proxima_acao_em)
    WHERE proxima_acao_em IS NOT NULL;

CREATE INDEX idx_leads_estado ON leads(estado);

-- Histórico de transições (auditoria)
CREATE TABLE transicoes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id         INTEGER NOT NULL REFERENCES leads(id),
    estado_anterior TEXT,
    estado_novo     TEXT NOT NULL,
    motivo          TEXT,
    payload_json    TEXT,
    criado_em       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_transicoes_lead ON transicoes(lead_id);

-- Idempotência de webhooks
CREATE TABLE webhooks_recebidos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fonte           TEXT NOT NULL,
    evento_id       TEXT NOT NULL,
    hash_payload    TEXT NOT NULL,
    recebido_em     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(fonte, evento_id)
);
```

### Decisões deliberadas

- `payload_json` em `transicoes` fica como **TEXT** (não BLOB) → permite `SELECT` legível no terminal
- Sem ORM completo (SQLAlchemy só pra connection pool e queries; sem models declarativos)
- Histórico de mensagens **não** vai pro nosso DB — sempre puxa do Jurichat via API
- `webhooks_recebidos` resolve duplicação sem precisar de Redis
- `proxima_acao_em` é o único campo que o `scheduler` precisa pra decidir o que processar (índice filtrado por NULL)

## 7. Fluxos de dados

### 7.1 Cenário A — Mensagem nova do lead

```
[Lead manda msg no WhatsApp]
   ↓
[Jurichat dispara webhook]
   ↓ POST /webhooks/jurichat
[Servidor]
  1. webhooks: valida HMAC; se inválido → 401, log, fim
  2. webhooks: SELECT/INSERT em webhooks_recebidos (idempotência);
               se já existe → 200, fim
  3. webhooks: responde 200 e enfileira BackgroundTask
  4. (async) state: SELECT lead por jurichat_conversation_id;
                    se não existe, INSERT com estado=em_conversa, turnos=0
  5. (async) state: detecta autor da msg;
                    se autor=Mario (atendente) → estado=aguardando_humano, fim
                    se autor=lead E estado=aguardando_humano → fim (Claude parou)
                    se autor=lead E estado in {em_conversa, follow_up_*, encerrado_sem_resposta}:
                      → se encerrado_sem_resposta, reabre para em_conversa E zera turnos=0
                      → continua processamento
  6. (async) outbound: GET /conversation/{id} no Jurichat (transcrição pronta)
  7. (async) brain: monta prompt (skill saude + histórico); chama Claude
                    espera retorno em JSON estrito (acao, mensagem, ...)
                    retry 1x se JSON inválido
  8. (async) state: atualiza turnos++, ultima_msg_lead_em, atualizado_em
                    grava transicao
  9. (async) roteamento:
       acao=responder → outbound envia mensagem; estado fica em_conversa
       acao=propor    → outbound envia proposta; estado vira aguardando_humano;
                        outbound envia notificação 🔥 pro Mario
       acao=handoff   → estado vira aguardando_humano;
                        outbound envia notificação ⚠️ pro Mario
       turnos == 20   → estado vira aguardando_humano;
                        outbound envia notificação ⏸ pro Mario
 10. (async) state: define proxima_acao_em = now + 48h (se ainda em em_conversa)
```

### 7.2 Cenário B — Scheduler (timer 1h)

```
[systemd timer dispara scheduler.py]
   ↓
[Script]
  1. state: SELECT leads WHERE proxima_acao_em < now()
            AND estado IN ('em_conversa','follow_up_1_enviado','follow_up_2_enviado')
  2. Para cada lead:
       a. outbound: GET etiquetas atuais do lead (Jurichat API)
       b. Regra de elegibilidade (estritamente opt-in OU sem etiqueta):
            elegível ⇔ NENHUMA etiqueta presente
                      OU 'Fazer Follow up' está presente
                      OU 'Proposta enviada' está presente
            (Etiquetas como Pagamento pendente, Cliente Ativo, Advogado
             adverso, Reunião marcada, Desqualificado por si só NÃO
             tornam elegível — mas se combinadas com 'Fazer Follow up'
             ou 'Proposta enviada', a presença do opt-in vence.)
       c. Se NÃO elegível: state grava erro_atual='excluido_followup_etiqueta';
                            proxima_acao_em=NULL; segue próximo
       d. Se elegível, despacha por estado atual:
            em_conversa:
              brain gera mensagem contextual de retomada
              outbound envia
              state: estado=follow_up_1_enviado; proxima_acao_em=now+72h
            follow_up_1_enviado:
              outbound envia texto fixo de encerramento
                ("Maria, percebi que talvez não seja o momento certo.
                  Posso encerrar nosso atendimento por aqui?...")
              state: estado=follow_up_2_enviado; proxima_acao_em=now+24h
            follow_up_2_enviado:
              state: estado=encerrado_sem_resposta; proxima_acao_em=NULL
              (sem nova mensagem ao lead — encerramento silencioso)
```

### 7.3 Cenário C — Mensagem do Mario na conversa

Mario responde manualmente pelo Jurichat (interface web ou app). Jurichat dispara webhook normalmente. No passo 5 do Cenário A, detecta autor=Mario e força estado=`aguardando_humano`. Claude não responde mais essa conversa, mesmo se o lead voltar a mandar mensagem (estado terminal para o fluxo automático).

## 8. Contrato do Claude

A `brain` chama Claude com instrução de retornar **APENAS JSON válido** com este schema:

```json
{
  "acao": "responder" | "propor" | "handoff",
  "mensagem": "<texto a enviar ao lead, ou texto da proposta>",
  "resumo_caso": "<para notificação Mario; só preenche se acao=propor ou handoff>",
  "motivo_handoff": "<só preenche se acao=handoff>"
}
```

**Critérios que Claude deve usar (definidos no system prompt):**

- `responder`: lead ainda está se informando, tirando dúvidas, contando o caso. Não há intent claro de contratar nem indicação de problemas.
- `propor`: lead manifestou intent de contratar (perguntou valor, perguntou "como faço pra começar", aceitou explicitamente seguir adiante) E há dor jurídica concreta identificada.
- `handoff`: lead pediu falar com humano, lead virou agressivo, tema fora da skill de saúde, lead em situação de emergência médica real.

**Mensagens do Claude seguem voz Noviello** — referência à skill `noviello-voz-padrao` do ecossistema (incluída como contexto adicional do system prompt).

## 9. Notificações para Mario

Via `outbound`, enviando mensagem ao número `NOTIFICACAO_TELEFONE` (env var), formato padronizado:

```
🔥 Lead Maria (5511...) — QUER FECHAR
Última msg: "como faço pra contratar?"
Resumo Claude: Plano negou bariátrica, paciente IMC 42, falsa coletivização
Link: https://app.jurichat.com/conversation/<id>
```

```
⚠️ Lead João (5511...) — PRECISA DE VOCÊ
Motivo: pediu falar com humano
Link: ...
```

```
⏸ Lead Ana (5511...) — 20 turnos sem progresso
Última msg: "vou pensar"
Link: ...
```

Notificações são **fire-and-forget**: se o envio falha, registra `erro_atual` mas não bloqueia transição do lead.

## 10. Tratamento de erros

| Erro | Comportamento |
|---|---|
| HMAC inválido no webhook | HTTP 401, log estruturado, ignora |
| Webhook duplicado (já em `webhooks_recebidos`) | HTTP 200, log, ignora |
| Claude retorna JSON malformado | Retry 1x com prompt "responda em JSON estrito"; se falha de novo: `erro_atual='claude_invalid_json'`, estado mantém, notifica Mario |
| Jurichat API 5xx ao enviar msg | Backoff exponencial 3x (1s, 3s, 9s); se persiste: `erro_atual='jurichat_unreachable'`, agenda retry em 30min via `proxima_acao_em` |
| Anthropic API rate limit (429) | Respeita `Retry-After`; se >5min: erro, notifica Mario, lead fica parado |
| Scheduler encontra lead órfão (Jurichat 404 ao buscar etiquetas) | `erro_atual='lead_nao_encontrado'`, estado vira `encerrado_sem_resposta`, `proxima_acao_em=NULL` |
| SQLite locked | Timeout 30s no `sqlite3.connect` resolve quase sempre; se ainda falhar, erro 500, BackgroundTask reprocessa |
| Falha em enviar notificação ao Mario | Log, registra `erro_atual='notificacao_falhou'`, mas transição do lead segue |

## 11. Segurança e LGPD

- Todas as chaves em `.env` (gitignored), template em `.env.example`
- HMAC obrigatório em webhook do Jurichat
- Servidor não expõe rota autenticada por usuário (não há painel)
- Servidor escuta apenas em `127.0.0.1:8000`; nginx faz proxy
- Nginx termina TLS (Let's Encrypt) no subdomínio (a definir, ex.: `funil.noviello.adv.br`)
- Log estruturado JSON com **metadados apenas**: `lead_id`, `estado`, `acao`, timestamps. **Não loga corpo de mensagens** do lead nem da resposta do Claude — proteção LGPD e sigilo cliente-advogado
- Erros do Claude podem logar o **primeiro token de erro** mas não o conteúdo da mensagem
- Backup do `.db` é responsabilidade operacional do Mario (rsync diário sugerido)

## 12. Testes

| Tipo | Cobertura | Ferramenta |
|---|---|---|
| Unit `brain` | Mock Anthropic; valida que prompt inclui skill + histórico; valida parse JSON; retry em malformado | pytest + respx |
| Unit `state` | DB em memória; transições válidas/inválidas; idempotência; constraints | pytest |
| Unit `outbound` | Mock httpx; retry; backoff exponencial; throttling | pytest + respx |
| Integration `webhooks` | TestClient FastAPI; HMAC válido/inválido; idempotência ponta-a-ponta | pytest |
| Integration `scheduler` | DB com fixtures de leads vencidos; regra de etiquetas com responses mockados | pytest |
| Smoke E2E (manual) | Dispara webhook fake; verifica msg saiu no WhatsApp de teste | script `scripts/smoke.sh` |

CI: GitHub Actions rodando unit + integration; sem testes contra API real em CI.

## 13. Estrutura de diretórios

```
noviello-funil-saude/
├── .env                          (gitignored)
├── .env.example
├── .gitignore
├── pyproject.toml                (uv-managed)
├── README.md
├── src/noviello_funil/
│   ├── __init__.py
│   ├── main.py                   (FastAPI app, registra rotas)
│   ├── config.py                 (lê env vars, valida)
│   ├── db.py                     (connect, migrations idempotentes)
│   ├── webhooks.py
│   ├── state.py
│   ├── brain.py
│   ├── outbound.py
│   ├── scheduler.py              (entry point pro systemd timer)
│   └── skills/
│       └── saude_suplementar.md  (system prompt do Claude)
├── tests/
│   ├── unit/
│   │   ├── test_brain.py
│   │   ├── test_state.py
│   │   └── test_outbound.py
│   └── integration/
│       ├── test_webhooks_flow.py
│       └── test_scheduler_flow.py
├── deploy/
│   ├── noviello-funil.service    (systemd, FastAPI)
│   ├── noviello-followup.service (systemd, oneshot)
│   ├── noviello-followup.timer   (systemd, hourly)
│   └── nginx.conf
├── scripts/
│   ├── smoke.sh                  (E2E manual)
│   └── deploy.sh                 (push pro VPS)
└── docs/
    └── superpowers/specs/
        └── 2026-06-03-noviello-funil-saude-design.md
```

## 14. Deploy

- **VPS:** Hostinger (Brasil/SP, já existente)
- **OS:** Ubuntu 22.04 LTS
- **Python:** 3.11 via `uv` (não system Python)
- **Usuário:** `noviello` (não-root, sem login interativo)
- **Serviço principal:** `noviello-funil.service` (FastAPI + uvicorn, restart on-failure)
- **Timer follow-up:** `noviello-followup.timer` chamando `noviello-followup.service` (oneshot) a cada 1h
- **Nginx:** reverse proxy `127.0.0.1:8000` ← `https://funil.noviello.adv.br/` (subdomínio exato a definir)
- **TLS:** Let's Encrypt via certbot
- **Logs:** journalctl (systemd nativo); rotação automática

## 15. Riscos e assumptions a validar antes do production

1. ⚠️ **Webhook do Jurichat dispara por mensagem nova individual?** Não confirmado. Verificar disparando 3 mensagens consecutivas em conversa de teste e observando se cada uma gera evento `chat.conversation.updated` (ou equivalente).
   - **Plano B se não dispara:** scheduler também faz polling a cada 1min em leads `em_conversa` (custa rate limit, mas funciona).

2. **Rate limit do `send-message` do Jurichat desconhecido.** Mitigação: throttle de 1 msg/segundo por conversa via `THROTTLE_MSG_POR_SEGUNDO`. Ajustar após observação.

3. **Custo da Claude API por conversa não estimado.** Estimativa grossa: 20 turnos × ~2k tokens entrada + ~500 tokens saída ≈ R$ 0,30/lead. Mitigação: log de tokens por conversa, alarme em SQLite se custo diário ultrapassar threshold (definir após observação).

4. **Endpoint exato de listar etiquetas do Jurichat** não está mapeado no checkpoint. Verificar na docs (`docs.jurichat.com`) ou perguntar para o Rafael.

5. **Confirmar que rotação da chave Jurichat** foi feita antes do primeiro deploy (chave anterior estava comprometida).

6. **Regra de detecção de autor da mensagem** (lead vs Mario) precisa ser validada com payload real do webhook — campo exato (`from_me`, `direction`, etc.) só vai ficar claro inspecionando um webhook de teste.

## 16. Roadmap pós-MVP (não fazer agora)

- Reativar o gate humano via comando WhatsApp (`APROVAR <id>`) quando voltar a integrar ZapSign + Asaas
- Adicionar painel web simples (read-only, autenticado por API key) para Mario ver leads ativos sem entrar no SQLite
- Skill `noviello-aereo` (não existe ainda) para piloto do segundo vertical
- Migrar SQLite → Postgres se volume passar de ~100 leads/dia
- Persistir histórico de conversa no SQLite (em vez de só puxar do Jurichat) se houver problema de latência ou rate limit
