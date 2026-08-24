# Integração AASP → Juridiq (intimações do recorte)

Data: 2026-08-24 · Status: aprovado em conversa, aguardando review final

## Objetivo

Puxar diariamente as intimações do recorte da AASP (API de Intimações) e
registrá-las nos processos correspondentes do Juridiq, cobrindo os gaps
conhecidos do monitoramento nativo: 2ª instância não monitorada, robô que
trava silenciosamente (status CADASTRADO defasado) e publicações que o
Juridiq não captura. Intimação urgente vira TAREFA com prazo sugerido no
painel (mesmo pipeline das publicações nativas).

## Fonte — API de Intimações AASP

- `GET https://intimacaoapi.aasp.org.br/api/Associado/intimacao/json`
  com query `chave` (env `AASP_CHAVE`, fornecida pela AASP) e `data`.
  Swagger: `https://intimacaoapi.aasp.org.br/swagger/v1/swagger.json`.
- Resposta: `{"intimacoes": [...], "erro": bool, "status": "Sucesso"}`.
- **Não usamos `diferencial=true`**: o flag "não consultadas" da AASP é
  consumido na leitura — se o job morrer no meio, perderíamos itens.
  Em vez disso: consultar por `data` explícita os últimos
  `aasp_dias_janela` dias (default 3, cobre fim de semana/falha de run)
  e deduplicar localmente.
- ⚠️ **Schema do item de intimação é DESCONHECIDO** (a doc não documenta
  o modelo e, em 24/08, a chave retorna zero intimações em 30 dias —
  recorte provavelmente recém-configurado; Mario confere o cadastro de
  nomes/OAB no portal AASP). Consequências de projeto:
  - Parser **defensivo**, tolerante a variantes de nome de campo
    (`numeroProcesso`/`processo`, `conteudo`/`despacho`/`texto`,
    `dataDisponibilizacao`/`dataPublicacao`/`dataDivulgacao`, etc.).
  - Todo payload bruto é salvo em tabela local (`aasp_raw`) antes de
    qualquer parse — nada se perde se o parser errar; item não parseável
    gera alerta "conferir" em vez de ser descartado.

## Destino — Juridiq

- `POST /lawSuit/movements` ("Adicionar andamento manual"): body
  `{lawSuitId, content, instance?, sharedWithJurichat?}`. O andamento
  entra no topo da timeline com `origin: manual` e **nasce privado**
  (`sharedWithJurichat` default false — manter; teor de intimação não
  vai pro cliente no Jurichat).
- `content` prefixado `[AASP]` + jornal/data + teor (para ser
  reconhecível e auditável na timeline).
- `instance`: se o número/contexto indicar 2º grau, enviar 2; senão
  omitir (API usa a instância atual do processo).
- Matching: `numeroProcesso` normalizado CNJ × carteira via
  `GET /lawSuit/` paginado (uma carga por run, ~245 processos). A
  normalização de número CNJ segue o helper já validado nos scripts de
  auditoria (20 dígitos → máscara `NNNNNNN-DD.AAAA.J.TR.OOOO`).

## Arquitetura

Módulo novo `src/noviello_funil/aasp_intimacoes.py` no repo
`noviello-funil-saude`, seguindo o padrão do `carteira_datajud.py`:
console script `noviello-aasp` + service/timer systemd diário na VPS
(07h45 BRT, antes do `noviello-publicacoes` 08h30). Sem chave
(`AASP_CHAVE` ou `JURIDIQ_API_KEY` ausente) → warning e exit 0.

### Fluxo do run

1. **Fetch**: para cada dia da janela, GET intimações. Salva bruto em
   `aasp_raw` (dedup por hash do item).
2. **Parse + dedup**: normaliza campos; chave de idempotência =
   sha256(numeroProcesso + data + teor) na tabela `aasp_intimacao_vista`
   (padrão `carteira_datajud_visto`). Já vista → skip.
3. **Match**: número CNJ normalizado × carteira Juridiq.
4. **Casou** → `POST /lawSuit/movements`. Só marca como vista DEPOIS do
   201 (falha no meio não perde intimação; retry no próximo run).
5. **Classificação de urgência**: reusa `classificar_urgencia` /
   `_parse_veredictos` (fail-safe: falha → urgente "conferir") sobre as
   intimações NOVAS do run. Urgente + processo casado → TAREFA via
   `prazo_tarefa` (`calcular_prazo_sugerido`, `montar_corpo_tarefa`,
   `criar_tarefa`), idempotente por chave da intimação, gated por
   `task_column_id` (mesmo gate das publicações).
6. **Não casou** → item entra no alerta como **processo fora da
   carteira** (sinal grave: intimação de processo que o Juridiq nem
   conhece). Não cria nada no Juridiq.
7. **Alerta WhatsApp** (via `notify_mario`, padrão dos outros jobs): só
   quando houver novidade — urgentes destacadas (com prazo), não-casadas
   em bloco próprio, rodapé com contagem do que virou andamento/tarefa.
   Zero novidade → silêncio (exit 0).

### Isolamento de falhas

- try/except POR intimação: uma falha não derruba as demais (padrão do
  `publicacoes.py`).
- Criação de tarefa isolada: jamais impede o andamento nem o alerta.
- Tarefa criada mas marcação local falhou → loga órfã com task_id (não
  propaga; mesmo contrato do `_criar_tarefas_de_prazo`).
- AASP fora do ar / HTTP ≥ 400 → loga e sai 1 (systemd registra); nada
  parcial é marcado como visto.

## Config (novas, em `config.py` + `.env.example` com placeholder)

- `aasp_chave: str = ""` — chave da API (NUNCA versionada; vai no `.env`
  da VPS).
- `aasp_base_url` — default `https://intimacaoapi.aasp.org.br`.
- `aasp_dias_janela: int = 3`.
- `aasp_criar_tarefa: bool = True` — gate do passo 5 (além do
  `task_column_id` existente).

## Banco (migrations em `db.py`, padrão existente)

- `aasp_raw(hash PK, payload_json, data_consulta, criado_em)`.
- `aasp_intimacao_vista(chave PK, processo, law_suit_id, movement_ok,
  task_id, criado_em)`.

## Testes

- Fixtures sintéticas (nomes/números fictícios, regra do CLAUDE.md) com
  ≥2 variantes de nomes de campo para o parser defensivo.
- Testes: normalização CNJ/matching, dedup idempotente (rodar 2× não
  duplica andamento nem tarefa), fail-safe de classificação, item sem
  processo → balde não-casado, janela de datas.
- Suíte inteira verde antes e depois (hoje 795+ testes).

## Deploy

- Commit em `feat/mvp` (branch atual, com trabalho não-pushado — o
  deploy do conjunto segue o fluxo já pendente de revisão/push).
- VPS: `AASP_CHAVE` no `.env` de `/opt/noviello-funil-saude`, units
  `noviello-aasp.service` + `noviello-aasp.timer` (10h45 UTC = 07h45
  BRT), enable + smoke run manual.
- **Gate de deploy**: nada sobe sem confirmação do Mario (regra da
  casa).

## Fora de escopo (v1)

- Marcar publicação nativa do Juridiq como tratada a partir da AASP.
- Backfill histórico AASP (API só expõe consulta por data; janela de 3
  dias basta a partir da ativação).
- Reconciliação AASP × publicações nativas (dedup entre fontes): risco
  aceito de o mesmo ato aparecer como publicação do Juridiq E andamento
  [AASP]; o prefixo torna o duplicado óbvio. Reavaliar quando o recorte
  estiver fluindo.

## Pendência externa (Mario)

- Conferir no portal da AASP se o recorte está ativo e com os nomes/OAB
  de pesquisa cadastrados (hoje a API retorna zero intimações em 30
  dias). Sem isso a integração roda vazia.
