# Plano — ZapSign pós-assinatura (intake + arquivo + tarefa)

Task #36. Produzido por ultraplan (25/jun): pesquisa multi-agente do código real
→ síntese. Fundamentado no que EXISTE no repo.

## Resumo

Engancha em `_processar_zapsign` (rotas_contrato.py), **depois** da transição
para `ASSINADO`, e dispara **3 sub-passos best-effort e independentes**:
1. **INTAKE** — garantir a Pessoa no Juridiq (reusa o padrão idempotente-por-
   telefone de `intake_lead_agendado`).
2. **ARQUIVO** — baixar os bytes do PDF assinado (a `signed_file_url` salva é
   EFÊMERA, ~60min; re-buscar via `zapsign.get_doc`) e gravar em destino durável.
3. **TAREFA** — criar a tarefa de abertura do caso no Juridiq (`POST /task/`).

## Restrições que dominam o desenho (achadas no código)

- **Invariante do webhook:** o processor JAMAIS pode vazar exceção (o 200 já foi
  respondido). Todo o pós é fire-and-forget (igual `intake_lead_agendado`, que
  engole tudo e retorna None).
- **Falha PARCIAL é o caso normal** (3 chamadas externas independentes) → exige
  **marca por-passo durável** (timestamps NULL=pendente) com **CAS por-passo**,
  não um flag global. A guarda atual `if estado==ASSINADO: return` só protege o
  caminho feliz e não distingue qual sub-passo faltou.
- **Bloqueios de API confirmados:** `POST /task/` exige `lawSuitId` E `columnId`,
  mas o cliente recém-assinado **ainda não tem lawSuit**; o Juridiq **não tem
  endpoint de upload de documento** (→ PDF arquivado FORA do Juridiq); o
  `JuridiqClient` só expõe search/create person (sem PATCH, sem create_lawsuit);
  `criar_tarefa` usa `httpx.Client` **síncrono** (não chamar direto no handler
  async — usar executor/thread).
- **Gap de wiring decisivo:** o `JuridiqClient` **nem é instanciado em main.py
  hoje** e `_processar_zapsign` não o recebe → habilitar o pós exige wiring novo.

## Abordagem recomendada: B (inline + marcação por-passo com CAS)

- **A** (inline sem marcação): mínimo de código, MAS falha parcial fica invisível
  e não-retomável (re-disparo bate na guarda `estado==ASSINADO` e não refaz o
  passo perdido). Rejeitada.
- **B (RECOMENDADA):** colunas por-passo (`intake_juridiq_em`, `arquivo_pdf_em`,
  `tarefa_abertura_em` + refs) com CAS (`UPDATE ... WHERE passo_em IS NULL`,
  rowcount==1) — espelha `tarefa_publicacao`/`lembrete_*_em` já no repo.
  Retomável, idempotente por-passo, auditável, sem worker novo.
- **C** (B + sweeper no scheduler): defesa em profundidade contra crash; **YAGNI
  na fase 1** (o webhook da ZapSign reentrega) — add-on só se aparecer passo preso.

## Fases (TDD, tudo gated por flag default OFF)

- **F0 — Wiring do JuridiqClient** (pré-requisito, sem comportamento novo):
  instanciar em main.py (guard `if settings.juridiq_api_key`), passar a
  `_processar_zapsign` + `background_tasks`, incluir no `aclose()`. Config:
  `juridiq_api_key/base_url` + flag `pos_assinatura_ativo` (default OFF).
  Verificar: app sobe com flag OFF e juridiq=None sem erro; ASSINADO ainda
  transiciona (degradação graciosa).
- **F1 — Schema por-passo** (TDD, sem I/O): `_ensure_column` no contrato + helpers
  de CAS. Verificar: migração idempotente; CAS (1º claim True, 2º False).
- **F2 — INTAKE:** reusa person_id se existe; senão search_by_phone + create;
  fire-and-forget. Persiste `juridiq_person_id`.
- **F3 — ARQUIVO:** baixa via `zapsign.download_signed_file` (signed_file FRESCO
  do get_doc) → grava em `data/contratos_assinados/contrato-<id>.pdf` (herda
  `.gitignore` de `data/`; PII no servidor). Carimba só após sucesso. Opcional:
  email com PDF anexo (reusa SMTP de aniversarios.py).
- **F4 — TAREFA:** DEPENDE de decisão do Mario (lawSuit). Reusa
  `montar_corpo_tarefa/criar_tarefa` (executor p/ o client síncrono). Persiste
  `juridiq_task_id`; CAS.
- **F5 — Orquestração + notify + ativação:** `processar_pos_assinatura` chama
  F2/F3/F4 em sequência best-effort (cada um isolado) dentro do try/except;
  `notify_mario` resume (passos ok/pendentes). Liga a flag só após smoke sandbox.

## Riscos (mitigações)

- Janela 60min da signed_file_url → arquivar INLINE (handler tem o signed_file
  fresco); sweeper só com re-fetch.
- Sync/async: `criar_tarefa` é síncrono → executor/thread (não bloquear o loop).
- Duplicação de Pessoa (Juridiq não dedupe por external_ref) → preferir
  person_id; senão buscar por telefone antes de criar.
- Telefone vazio quebra a dedupe-por-telefone → priorizar person_id; fallback a
  decidir.
- lawSuitId ausente no POST /task/ → validar na sandbox antes de codar a forma.
- Falha de sub-passo NÃO pode reverter o ASSINADO (fato consumado) → try/except
  por-passo, marca NULL, alerta o Mario.
- PII no PDF → destino gitignored; nunca logar corpo com PII.

## Cortes YAGNI (fase 1)

- NÃO criar lawSuit/processo de verdade agora (sem nº CNJ; exigiria novos métodos
  + officeId/responsibleIds) — "abertura" pode ser tarefa/anotação.
- NÃO implementar PATCH/promoção de Pessoa (lead→cliente, setar CPF/origem) —
  garantir que EXISTE já cobre o intake.
- NÃO arquivar no Drive/Box (conectores MCP vivem na sessão do agente, não no
  runtime do bot; exigiria OAuth próprio) — disco local + email resolvem.
- NÃO construir o sweeper (C) na fase 1.
- NÃO disparar nada financeiro (Asaas só no fechamento; honorários só o Mario).
- NÃO tabela-ledger dedicada — colunas timestamp bastam.
- NÃO enviar PDF por Jurichat/WhatsApp (PII em 3º; canal só text).

## Perguntas abertas (decisões do Mario — gate da implementação)

1. **ARQUIVO** — destino do PDF? (rec.: `data/contratos_assinados/`, gitignored;
   + cópia por email pro Workspace?)
2. **TAREFA** — sem lawSuit no ASSINADO: (a) tarefa só na Pessoa (`personIds[]`),
   (b) anotação na Pessoa, (c) criar lawSuit antes (mais trabalho)?
3. **TAREFA** — conteúdo (título/descrição/prioridade) e em qual coluna do kanban
   (`task_column_id`)?
4. **INTAKE** — sem person_id E telefone vazio: criar Pessoa mesmo assim (nome+
   email) ou pular + alertar?
5. **INTAKE** — Pessoa já existe: só garantir, ou enriquecer (CPF/origem)?
   (enriquecer exige PATCH não exposto hoje.)
6. **ATIVAÇÃO** — uma flag `pos_assinatura_ativo` ou três (intake/arquivo/tarefa)?
