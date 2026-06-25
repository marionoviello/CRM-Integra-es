# D4 — Sync de reunião marcada fora do bot (híbrido)

Auditoria 24/jun, Frente D item deferido (P2). Aprovado pelo Mario em 25/jun
(abordagem C: híbrido — auto-vincular por email + alertar o resto).

## Problema

Uma reunião que o Mario marca FORA do bot (direto no Google Calendar, ex.:
respondendo uma ligação) NÃO entra no motor de lembretes (24h/2h/30min) nem no
no-show — o `reminder_cycle` só age sobre `leads.reuniao_em`, que só o bot
preenche. Mitigação atual: `scripts/registrar_reuniao_manual.py` (manual, exige
o Mario rodar à mão com o conversation_id). Buraco: ele esquece → no-show
silencioso → cliente perdido (a mesma classe que a Frente D fechou).

## Restrição de matching (a decisão de design)

- Eventos manuais do Mario têm **nome + email** do cliente (título + convidado).
- O lead no nosso DB tem **nome + telefone** (de WhatsApp) — **sem email**.
- Único campo em comum hoje = nome (frágil: homônimos/abreviações).
- → Pra casar com segurança, **passamos a guardar o email do lead**.

## Design (abordagem C — híbrido)

### 1. Persistir o email do lead
- Coluna `contato_email` em `leads` (migration `_ensure_column`).
- `set_lead_email(conn, lead_id, email)` em `state.py` (idempotente; só grava se
  mudou/estava vazio).
- No `run_poll_cycle`, ao processar um lead: se `_extrair_email(transcript)`
  achar email → persiste. Constrói o índice email→lead de QUALQUER conversa
  (não só agendamento), maximizando cobertura ao longo do tempo.

### 2. `list_events` no calendar client
- `GoogleCalendarClient.list_events(time_min, time_max)` → `GET .../events` com
  `timeMin`/`timeMax` (ISO), `singleEvents=true`, `orderBy=startTime`,
  `conferenceDataVersion=1`. Retorna lista de
  `{id, start_iso, summary, attendee_emails, meet_link}` (parse defensivo;
  pula eventos all-day/sem start dateTime).

### 3. `sync_reunioes_manuais(conn, calendar, jurichat, mario_conversation_id)`
Roda como passo do `_full_cycle` ANTES do `run_reminder_cycle` (gated por
`settings.calendar_sync_manual`). Lógica por evento na janela [agora, agora+48h]:

1. `tracked = {reuniao_event_id de todo lead}`. Evento já tracked → pula
   (auto-dedup: depois de auto-vincular, o event_id fica no lead → não reprocessa).
2. Convidados externos = attendees SEM as flags `self`/`organizer` do Google
   (essas marcam o dono do calendário/organizador → o cliente é convidado, não
   organizador). Sem convidado externo → pula (audiência/pessoal). Isso dispensa
   guardar o email do Mario no config.
3. Casa email do convidado com `leads.contato_email`:
   - **1 lead, sem `reuniao_em`** → `set_reuniao(lead, start, event_id, meet_link)`
     + `notify_mario` ✅ ("vinculei a reunião manual de [nome] aos lembretes").
   - **1 lead, mas já tem OUTRA `reuniao_em`** (event_id != este) → alerta
     CONFLITO, NÃO sobrescreve (1 reunião/lead no schema; 2 reuniões = fora de
     escopo). Dedup via tabela.
   - **Nenhum lead casado** (mas tem convidado externo) → alerta UMA vez
     ("📅 reunião não rastreada com [email] em [horário] — registre: `<comando
     preenchido>`"). Dedup via tabela.

### 4. Dedup de alerta
- Tabela `eventos_manuais_alertados(event_id TEXT PRIMARY KEY, alertado_em TEXT)`.
- Antes de alertar (conflito ou não-rastreada): se já está na tabela → não
  re-alerta. A sync roda a cada ~30s; sem isso seria spam.

### 5. Config
- `CALENDAR_SYNC_MANUAL: bool = True` em `config.py` + `.env.example`. Desligável
  se virar barulho. Pula a sync inteira se off.
- Organizador identificado pelas flags `self`/`organizer` do attendee (não
  precisa de email no config).
- Alerta "não rastreada": o comando vem com `--quando`/`--meet`/`--event-id`
  preenchidos (sei do evento) e `--conversa` em branco (Mario preenche com o
  lead) — não tenho o conversation_id pois nenhum lead casou.

## Pontos aceitos (Mario, 25/jun)
- Auto-vínculo só pega leads cujo email já temos (cresce com o tempo); sem email
  → cai no alerta "registre manual".
- O alerta de "não rastreada" dispara pra qualquer evento com convidado externo
  (inclui reunião com colega/advogado) — Mario ignora os não-cliente; dedup 1×.
- "Se precisar ajustar no tempo, ajusta" — começar simples, iterar.

## Testes
- Unidade: `list_events` parse (evento normal, all-day, sem attendee); matching
  por email (1 casa / nenhum / conflito); dedup de alerta; `set_lead_email`.
- Integração: `sync_reunioes_manuais` — auto-registra evento casado; não
  sobrescreve reunião existente (conflito); alerta não-rastreada 1×; ignora
  audiência (sem attendee) e evento já tracked.

## Fora de escopo
- 2 reuniões simultâneas no mesmo lead (schema é 1/lead) → vira alerta de conflito.
- Matching por nome (frágil) — não fazemos; só email.
- Reunião manual com NÃO-lead (sem conversa Jurichat) → não há canal pra lembrar;
  cai no alerta "não rastreada" e o Mario decide.
