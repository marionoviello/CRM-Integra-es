# Auditoria do bot Jurichat+Claude — 2026-06-24

Auditoria multi-agente (6 dimensões + verificação adversarial de cada achado).
**35 levantados → 31 confirmados** (4 falsos-positivos derrubados). Objetivo: mapear
tudo entre o estado atual e "perfeito", já que o bot é o ÚNICO atendimento do escritório.

Severidades: **P0** quebra/perda direta · **P1** alto · **P2** médio · **P3** baixo/higiene.
Status: `[ ]` aberto · `[x]` corrigido + deployado.

---

## Frente A — Leads perdidos (máquina de estados)  ⟵ COMEÇAR AQUI

- [ ] **P0** Teto de turnos conta `Lead:` do transcript VITALÍCIO (`_count_lead_lines`) e nunca reseta → lead engajado (≥20 msgs) que volta semanas depois ("mudei de ideia, quero marcar") é jogado pra `aguardando_humano` SEM o bot ler a mensagem. Neutraliza a reativação justo no lead de maior intenção. `scheduler.py:1462`. **Fix:** contar só `Lead:` após a última `Atendente:` de fechamento, OU ativar a coluna `turnos` (hoje código morto) com reset na reativação.
- [ ] **P1** `AGUARDANDO_HUMANO` é buraco negro: lead entra e nunca mais é tocado (nem reativa, nem alerta). Sem atendente humano, "handoff" vira "ninguém responde". `scheduler.py` + `state.py:621,458,407`. **Fix:** fase de reativação p/ AH por motivo (opt_out/canal = mudo; propor/handoff/max_turnos/calendar = reabre ou re-alerta Mario).
- [ ] **P1** Reativação (FASE 0) ignora `AGUARDANDO_HUMANO` mesmo só pra alertar → lead quentíssimo ("fechou, manda o contrato") após handoff não gera sinal nenhum. `scheduler.py:1070`. (mesma raiz da anterior)

## Frente B — Cérebro robusto (structured outputs)

- [ ] **P1** Triagem depende de parse de texto + 1 retry; sem structured outputs. JSON malformado nas 2 tentativas = lead MUDO no tick + 2 chamadas Opus desperdiçadas; parser aceita ação com campo faltando. `brain.py:161-196`. **Fix:** migrar p/ tool use (`tool_choice` forçado + `input_schema` por ação), elimina retry e campo-faltando-silencioso.
- [ ] **P2** `first.content[0].text` sem validar → resposta vazia/refusal vira `IndexError`/`AttributeError` que cai no except genérico (sem retry, sem alerta Mario). `brain.py:167,227`. **Fix:** validar bloco de texto → levantar `DecisaoInvalida` (vai pro caminho que avisa o Mario).
- [ ] **P2** Claude precisa gerar ISO com ano/data, mas o transcript não tem âncora "hoje é AAAA-MM-DD" e os slots não têm ano. Risco de data errada em remarcação/virada de ano. `brain.py:153` + `calendar_client.py:58`. **Fix:** injetar data/hora atual no `user_text` + cross-validar o ISO contra `get_horarios_oferecidos` no confirmar.

## Frente C — Colisão bot×humano (integração Jurichat)

- [ ] **P1** `JURICHAT_BOT_USER_ID` default vazio E ausente do `.env.example` → Signal 0 (detecção "humano assumiu") desligado; restore/redeploy reintroduz o estado quebrado silenciosamente. `config.py:204`. **Fix:** add no `.env.example` + WARNING alto/validação no boot.
- [ ] **P1** Follow-up NÃO checa "humano assumiu" e re-reivindica a conversa via `start_human_support` → FU dispara por cima do humano + rouba a conversa de volta pro bot. `scheduler.py:2131-2171`. **Fix:** aplicar o predicado do Signal 0 antes de enviar FU; passar `bot_user_id` ao `run_followup_cycle`.
- [ ] **P1** Mensagem do lead que chega entre o fetch e o envio fica órfã: o Signal 1 vê a própria resposta do bot e pula → escolha de horário / email / opt-out nessa janela são ignorados. `scheduler.py:1127-1138,1538-1552`. **Fix:** no Signal 1, NÃO pular se houver `Lead:` após a última `Atendente:`.
- [ ] **P1** Humano respondendo pelo Jurichat web sem reatribuir não pausa o bot → bot retoma por cima. `scheduler.py:1174-1188`. **Fix:** capturar autor da última OUTBOUND e, se ≠ bot, transicionar pra AH (depende do payload expor senderId — verificar).
- [ ] **P2** Signal 0 só roda em leads "due" no poll → humano que assume um lead não-due passa na janela. `scheduler.py:1107-1163`. **Fix:** sweep leve de Signal 0 p/ todo EM_CONVERSA (ou follow-up herdar Signal 0).

## Frente D — Durabilidade do agendamento

- [ ] **P1** `create_event` OK + crash antes de `set_reuniao` (janela de deploy) → evento órfão no Google sem nenhum lembrete; hash já avançou, não reprocessa. `scheduler.py:495-582`. **Fix:** chamar `set_reuniao` IMEDIATAMENTE após `create_event` retornar, antes dos passos fire-and-forget.
- [ ] **P1** Double-booking: nenhuma reserva entre oferta e confirmação; `find_available_slots` só lê freeBusy (eventual-consistente), nunca o DB. Dois leads confirmam o mesmo horário. `scheduler.py:495-516`. **Fix:** `SELECT ... WHERE reuniao_em=? AND id!=?` no confirmar (+ índice).
- [ ] **P2** Ping de no-show marca o token ANTES de enviar → falha no `notify_mario` perde o alerta pra sempre. `scheduler.py:1852-1883`. **Fix:** marcar token só após envio confirmado (espelhar `_enviar_lembrete`).
- [ ] **P2** Reunião agendada FORA do bot não gera lembrete/no-show. **Mitigação parcial JÁ EXISTE:** `scripts/registrar_reuniao_manual.py` (manual). Falta a sync automática. `scheduler.py:579`. **Fix:** `list_events` do Calendar no reminder_cycle (janela 48h) → upsert por telefone/email.
- [ ] **P2** `set_reuniao` com ISO inparseável grava `reuniao_em=now` (reunião fantasma, todos lembretes pré-suprimidos, nunca limpa). Latente hoje (caller normaliza). `state.py:510`. **Fix:** rejeitar/normalizar no except + no reminder_cycle trocar `continue` por `clear_reuniao`+alerta.

## Frente E — Compliance OAB / LGPD

- [ ] **P1** Ciclo de follow-up NÃO consulta `esta_suprimido` (opt-out) antes de enviar — viola a garantia LGPD do próprio módulo, no sender de maior volume. `scheduler.py:2094-2171`. **Fix:** `esta_suprimido` no início do loop → pular + transicionar `motivo=opt_out`.
- [ ] **P1** Detector de opt-out (regex-only) perde frases PT-BR comuns ("para", "me deixa em paz", "não envie mais", "chega de"). `opt_out.py:25-55`. **Fix:** ampliar `_PADROES` + sinal de opt-out no brain como rede OR + fixtures.
- [ ] **P1** Mensagens ao lead NÃO passam por filtro de promessa de resultado (OAB Prov. 205/2021). Só a marca tem backstop; o êxito não. O linter `lint_contrato` (B1/B2) existe mas só roda no fluxo de contrato. `outbound.py:54-81`. **Fix:** aplicar os padrões B1/B2 num verificador leve em `_sanitize_for_whatsapp` (ou degradar p/ handoff + alerta).
- [ ] **P2** Sanitizer de marca cobre `Dr./Dra.` mas não "doutor(a)" por extenso nem "Dr. Noviello" sem "Mario". `outbound.py:48-51`. **Fix:** estender o regex + testes.
- [ ] **P3** Templates de lembrete trazem "com o Mario" hardcoded (dependem 100% do sanitizer). `scheduler.py:2028,2050,2059`. **Fix:** trocar p/ "com nossa equipe" na origem.
- [ ] **P3** Lead chamado "Mário" tem o próprio nome trocado por "nossa equipe" pelo sanitizer. `outbound.py:78-79`. **Fix:** desligar a substituição quando o nome do lead normalizado é "mario".

## Frente F — Escalonamento de erros

- [ ] **P1** `register_error` sobrescreve `erro_atual` sem contador nem alerta (coluna write-only morta) → lead preso em falha de API recorrente fica dias sem resposta e o Mario nunca sabe. `scheduler.py:1126-1135` (~13 call sites). **Fix:** contador `erro_consecutivo` (zera em sucesso) → ao cruzar N, `notify_mario` 1× + opcional transicionar AH.
- [ ] **P2** `except (GoogleCalendarError, Exception)` trata bug determinístico = falha transitória → reschedule mudo infinito sem alerta, mascara regressão pós-deploy. `scheduler.py:318-324`. **Fix:** separar transitório (retry/handoff) de inesperado (logar bug + notify_mario + handoff).

## Frente G — Qualidade da triagem / prompt da Julia

- [ ] **P1** Guardrail de `propor` força agendamento mesmo quando o lead RECUSOU videochamada → bot insiste em Meet com 50+ que disse não. Contradiz a própria skill. `scheduler.py:1565-1606`. **Fix:** preservar `propor → handoff` quando há recusa/fora-de-escopo (sinal no transcript ou campo na Decisao).
- [ ] **P2** Email-gate antes de oferecer horários só existe no prompt; `_handle_oferecer_horarios` não revalida → pode mandar 4 slots sem ter pedido email. `scheduler.py:275-370`. **Fix:** espelhar o guardrail do confirmar (checar `_extrair_email` no início do handler).
- [ ] **P2** Lembrete de 5min PROMETE "cancelamento automático" que o código nunca executa → lead atrasado confia, não entra, Mario espera na chamada. `scheduler.py:2063-2067`. **Fix:** alinhar texto a "se atrasar, me avise que remarcamos" (design é semi-auto).
- [ ] **P2** Rede de cancelamento (bug Daniel) só atua com lembrete pendente → "cancela" após o 5min é ignorado. `scheduler.py:1954-1979`. **Fix:** rodar `_lead_pediu_cancelamento` antes do `if tag is None: continue` na janela final.
- [ ] **P3** `saude_suplementar.md` é skill morta (mono-skill atual cobre os 3 verticais). `scheduler.py:2260`. **Fix:** excluir o arquivo órfão (limpeza); roteamento por vertical é enhancement separado.

## Frente H — Robustez de infra

- [ ] **P2** Web + scheduler escrevem no mesmo SQLite sem invariante de writer único → possível `database is locked` sob colisão. `db.py:238-247`. **Fix:** `PRAGMA busy_timeout` explícito + retry no `OperationalError('locked')` dentro de `transicao`.
- [ ] **P3** `get_conversation` serial sem teto na reativação+polling → Jurichat lento estoura a janela de 30s e atrasa os lembretes (5min/30min rodam no mesmo ciclo). `scheduler.py:1070-1135`. **Fix:** paralelizar com `asyncio.gather`+semáforo (como já no DataJud).

---

## Plano de execução
Frente por frente, de cima pra baixo (A→H). Cada correção: **teste primeiro (TDD) → verificação na API real quando aplicável → commit → deploy incremental** (o bot melhora continuamente, sem big-bang). Frentes que tocam `scheduler.py` (quase todas) vão em série pra evitar conflito.
