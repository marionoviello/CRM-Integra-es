# Integração AASP → Juridiq — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Job diário que puxa as intimações do recorte AASP e as grava como andamento manual no processo correspondente do Juridiq, com classificação de urgência, tarefa de prazo e alertas WhatsApp.

**Architecture:** Módulo único `aasp_intimacoes.py` no padrão dos jobs existentes (`carteira_datajud.py`, `publicacoes.py`): funções puras testáveis + `main()` fino; idempotência em SQLite; reuso do classificador de urgência (`publicacoes.classificar_urgencia`) e do gerador de tarefas (`prazo_tarefa`). Console script `noviello-aasp` + timer systemd diário 10:45 UTC.

**Tech Stack:** Python 3.12, httpx, pydantic-settings, SQLite, pytest + respx, systemd na VPS.

**Spec:** `docs/superpowers/specs/2026-08-24-aasp-intimacoes-juridiq-design.md`

Convenções do repo (ler antes): segredos só via `.env`/`config.py`; fixtures com dados FICTÍCIOS; docstrings em pt-BR contando o porquê; teste unit em `tests/unit/`.

---

### Task 1: Config + migrations

**Files:**
- Modify: `src/noviello_funil/config.py` (após o bloco `task_priority`, ~linha 158)
- Modify: `src/noviello_funil/db.py` (SCHEMA, após `boletim_competencia`)
- Modify: `.env.example`
- Test: `tests/unit/test_aasp_intimacoes.py` (novo)

- [ ] **Step 1: Failing test de config + tabelas**

```python
"""Tests do job de intimações AASP → Juridiq (aasp_intimacoes)."""

import datetime
import hashlib
import json

import httpx
import pytest

from noviello_funil.db import connect, run_migrations


@pytest.fixture()
def conn():
    c = connect(":memory:")
    run_migrations(c)
    yield c
    c.close()


def test_config_aasp_defaults(monkeypatch):
    from noviello_funil.config import Settings
    monkeypatch.setenv("JURICHAT_API_KEY", "x")
    s = Settings(_env_file=None)
    assert s.aasp_chave == ""
    assert s.aasp_base_url == "https://intimacaoapi.aasp.org.br"
    assert s.aasp_dias_janela == 3
    assert s.aasp_criar_tarefa is True


def test_migrations_criam_tabelas_aasp(conn):
    for tabela in ("aasp_raw", "aasp_intimacao_vista"):
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (tabela,),
        ).fetchone()
        assert row is not None, tabela
```

Nota: `test_config.py` existente mostra como instanciar Settings nos testes — seguir o mesmo padrão (se lá usarem outro conjunto de env obrigatória, copiar de lá).

- [ ] **Step 2: Rodar e ver falhar**

Run: `uv run pytest tests/unit/test_aasp_intimacoes.py -v`
Expected: FAIL (`aasp_chave` inexistente; tabelas ausentes)

- [ ] **Step 3: Implementar config**

Em `config.py`, após o bloco de `task_priority`:

```python
    # AASP (recorte de intimações) → andamento manual no Juridiq.
    # aasp_chave é fornecida pela AASP (portal do associado) — só no .env.
    # Janela de N dias por run: cobre fim de semana/falha de execução; a
    # dedup local (aasp_intimacao_vista) evita duplicar. Não usamos o
    # `diferencial=true` da API: o flag "não consultada" deles é consumido
    # na leitura — job morrendo no meio perderia intimação.
    aasp_chave: str = ""
    aasp_base_url: str = "https://intimacaoapi.aasp.org.br"
    aasp_dias_janela: int = Field(default=3, ge=1)
    aasp_criar_tarefa: bool = True
```

Em `db.py`, no SCHEMA (após `boletim_competencia`):

```sql
-- Intimações AASP (aasp_intimacoes). aasp_raw guarda TODO payload bruto
-- antes do parse (schema da AASP é desconhecido até o recorte fluir —
-- nada se perde se o parser errar). aasp_intimacao_vista = idempotência:
-- linha existe = intimação já processada (andamento criado OU não-casada
-- já alertada).
CREATE TABLE IF NOT EXISTS aasp_raw (
    hash          TEXT PRIMARY KEY,
    payload       TEXT NOT NULL,
    data_consulta TEXT NOT NULL,
    criado_em     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS aasp_intimacao_vista (
    chave       TEXT PRIMARY KEY,
    processo    TEXT,
    law_suit_id TEXT,
    criado_em   TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Em `.env.example`, junto das outras chaves:

```bash
# AASP — recorte de intimações (API intimacaoapi.aasp.org.br)
AASP_CHAVE=coloque-a-chave-da-aasp-aqui
```

- [ ] **Step 4: Rodar e ver passar**

Run: `uv run pytest tests/unit/test_aasp_intimacoes.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/noviello_funil/config.py src/noviello_funil/db.py .env.example tests/unit/test_aasp_intimacoes.py
git commit -m "feat(aasp): config e tabelas do job de intimacoes AASP"
```

---

### Task 2: Parser defensivo + helpers CNJ

**Files:**
- Create: `src/noviello_funil/aasp_intimacoes.py`
- Test: `tests/unit/test_aasp_intimacoes.py` (append)

- [ ] **Step 1: Failing tests**

```python
# --- normalizar_item / helpers -----------------------------------------------

def test_formatar_cnj():
    from noviello_funil.aasp_intimacoes import formatar_cnj
    assert formatar_cnj("12345670820268260100") == "1234567-08.2026.8.26.0100"
    assert formatar_cnj("1234567-08.2026.8.26.0100") == "1234567-08.2026.8.26.0100"
    assert formatar_cnj("123") == ""
    assert formatar_cnj("") == ""


def test_instancia_sugerida():
    from noviello_funil.aasp_intimacoes import instancia_sugerida
    assert instancia_sugerida("22345670820268260000") == 2   # origem 0000 = 2º grau
    assert instancia_sugerida("12345670820268260100") is None
    assert instancia_sugerida("123") is None


def test_normalizar_item_campos_padrao():
    from noviello_funil.aasp_intimacoes import normalizar_item
    item = normalizar_item({
        "numeroProcesso": "1234567-08.2026.8.26.0100",
        "conteudo": "<p>Intime-se a parte <b>autora</b>.</p>",
        "dataDisponibilizacao": "20/08/2026",
        "jornal": "DJE - Caderno Judicial",
    })
    assert item["processo"] == "1234567-08.2026.8.26.0100"
    assert item["processo_digitos"] == "12345670820268260100"
    assert item["teor"] == "Intime-se a parte autora."
    assert item["data"] == "20/08/2026"
    assert item["jornal"] == "DJE - Caderno Judicial"
    assert len(item["chave"]) == 64


def test_normalizar_item_variantes_e_case_insensitive():
    from noviello_funil.aasp_intimacoes import normalizar_item
    item = normalizar_item({
        "Processo": "12345670820268260100",
        "Despacho": "Vistos.",
        "DataPublicacao": "2026-08-20",
        "NomeJornal": "DJE SP",
    })
    assert item["processo"] == "1234567-08.2026.8.26.0100"  # máscara aplicada
    assert item["teor"] == "Vistos."
    assert item["data"] == "2026-08-20"
    assert item["jornal"] == "DJE SP"


def test_normalizar_item_sem_processo_nao_quebra():
    from noviello_funil.aasp_intimacoes import normalizar_item
    item = normalizar_item({"conteudo": "Edital genérico."})
    assert item["processo"] == ""
    assert item["processo_digitos"] == ""
    assert item["teor"] == "Edital genérico."


def test_chave_dedup_estavel_e_distinta():
    from noviello_funil.aasp_intimacoes import normalizar_item
    a = {"numeroProcesso": "1", "conteudo": "X", "dataDisponibilizacao": "20/08/2026"}
    assert normalizar_item(a)["chave"] == normalizar_item(dict(a))["chave"]
    b = dict(a, conteudo="Y")
    assert normalizar_item(a)["chave"] != normalizar_item(b)["chave"]
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `uv run pytest tests/unit/test_aasp_intimacoes.py -v`
Expected: FAIL (módulo não existe)

- [ ] **Step 3: Criar o módulo com parser**

`src/noviello_funil/aasp_intimacoes.py`:

```python
"""Intimações do recorte AASP → andamento manual no Juridiq.

O monitoramento nativo do Juridiq falha em silêncio (status CADASTRADO
defasado — auditoria jun/2026) e NÃO cobre 2ª instância. O recorte da
AASP é fonte independente: este job busca as intimações do dia na API da
AASP, casa o número CNJ com a carteira e grava cada uma como andamento
manual (`POST /lawSuit/movements`, prefixo [AASP], privado — não vai pro
cliente no Jurichat). Intimação nova passa pelo classificador de urgência
das publicações; urgente vira TAREFA de prazo no painel. Intimação de
processo FORA da carteira vira alerta grave (cadastrar o processo!).

Quirks de projeto:
- O schema do item da AASP é DESCONHECIDO (doc não documenta; recorte
  contratado em 24/08/2026, ainda sem publicações). Parser defensivo com
  variantes de nome de campo + payload bruto salvo em `aasp_raw` antes de
  qualquer parse — item que o parser não entender não se perde.
- NÃO usamos `diferencial=true` da AASP: o flag deles é consumido na
  leitura; se o job morrer no meio, perderíamos intimação. Consultamos por
  data explícita (janela de `aasp_dias_janela` dias) e deduplicamos local
  (`aasp_intimacao_vista`).
- Só marcamos como vista DEPOIS do andamento criado (casadas) ou do alerta
  enviado (não-casadas) — falha no meio = retry no próximo run.

Execução: console script ``noviello-aasp`` via systemd timer diário
(10:45 UTC = 07:45 BRT, antes do noviello-publicacoes 08:30 BRT).
"""

import datetime
import hashlib
import json
import logging
import re

import httpx

logger = logging.getLogger(__name__)

MAX_ITENS = 12               # cap de itens detalhados na mensagem
_TEOR_ANDAMENTO_CHARS = 4000  # teor no andamento do Juridiq
_RESUMO_CHARS = 90

# Variantes de nome de campo (schema AASP desconhecido — ver docstring).
_CAMPOS_PROCESSO = ("numeroProcesso", "numeroProcessoMascara", "processo",
                    "numProcesso")
_CAMPOS_TEOR = ("conteudo", "despacho", "texto", "teor", "textoPublicacao",
                "publicacao")
_CAMPOS_DATA = ("dataDisponibilizacao", "dataPublicacao", "dataDivulgacao",
                "data")
_CAMPOS_JORNAL = ("jornal", "nomeJornal", "descricaoJornal", "diario",
                  "caderno")


def _so_digitos(s: object) -> str:
    return re.sub(r"\D", "", str(s or ""))


def formatar_cnj(numero: object) -> str:
    """20 dígitos → máscara CNJ. Qualquer outra coisa → ''."""
    d = _so_digitos(numero)
    if len(d) != 20:
        return ""
    return f"{d[:7]}-{d[7:9]}.{d[9:13]}.{d[13]}.{d[14:16]}.{d[16:]}"


def instancia_sugerida(digits: str) -> int | None:
    """Origem 0000 = processo de 2º grau (TJSP/TRF) → instance 2.

    Heurística conservadora: só afirma quando a origem é o marcador
    inequívoco de 2ª instância; caso contrário deixa a API usar a
    instância atual do processo (omitir).
    """
    if len(digits) == 20 and digits[16:] == "0000":
        return 2
    return None


def _limpar_html(html: object) -> str:
    txt = re.sub(r"<[^>]+>", " ", str(html or ""))
    return re.sub(r"\s+", " ", txt).strip()


def _primeiro_campo(raw: dict, campos: tuple[str, ...]) -> str:
    for c in campos:
        v = raw.get(c)
        if v and str(v).strip():
            return str(v).strip()
    lower = {str(k).lower(): v for k, v in raw.items()}
    for c in campos:
        v = lower.get(c.lower())
        if v and str(v).strip():
            return str(v).strip()
    return ""


def normalizar_item(raw: dict) -> dict:
    """Item bruto da AASP → dict normalizado com chave de dedup.

    chave = sha256(dígitos do processo | data | teor) — estável entre runs
    e independente de campos cosméticos que a AASP mude.
    """
    processo_raw = _primeiro_campo(raw, _CAMPOS_PROCESSO)
    digits = _so_digitos(processo_raw)
    teor = _limpar_html(_primeiro_campo(raw, _CAMPOS_TEOR))
    data = _primeiro_campo(raw, _CAMPOS_DATA)
    jornal = _primeiro_campo(raw, _CAMPOS_JORNAL)
    chave = hashlib.sha256(f"{digits}|{data}|{teor}".encode()).hexdigest()
    return {
        "chave": chave,
        "processo_raw": processo_raw,
        "processo_digitos": digits,
        "processo": formatar_cnj(digits) or processo_raw,
        "teor": teor,
        "data": data,
        "jornal": jornal,
    }
```

- [ ] **Step 4: Rodar e ver passar**

Run: `uv run pytest tests/unit/test_aasp_intimacoes.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/noviello_funil/aasp_intimacoes.py tests/unit/test_aasp_intimacoes.py
git commit -m "feat(aasp): parser defensivo de intimacoes + helpers CNJ"
```

---

### Task 3: Fetch AASP + raw store + dedup local

**Files:**
- Modify: `src/noviello_funil/aasp_intimacoes.py`
- Test: `tests/unit/test_aasp_intimacoes.py` (append)

- [ ] **Step 1: Failing tests**

```python
# --- buscar_intimacoes / raw / vista -----------------------------------------

def _aasp_client():
    return httpx.Client(base_url="https://intimacaoapi.aasp.org.br")


def test_buscar_intimacoes_ok(respx_mock):
    from noviello_funil.aasp_intimacoes import buscar_intimacoes
    respx_mock.get("https://intimacaoapi.aasp.org.br/api/Associado/intimacao/json").mock(
        return_value=httpx.Response(200, json={
            "intimacoes": [{"numeroProcesso": "1"}, "lixo-nao-dict"],
            "erro": False, "status": "Sucesso",
        }),
    )
    c = _aasp_client()
    try:
        itens = buscar_intimacoes(c, "chave-teste", datetime.date(2026, 8, 20))
    finally:
        c.close()
    assert itens == [{"numeroProcesso": "1"}]   # não-dict filtrado
    req = respx_mock.calls.last.request
    assert "chave=chave-teste" in str(req.url)
    assert "data=2026-08-20" in str(req.url)


def test_buscar_intimacoes_erro_da_api_levanta(respx_mock):
    from noviello_funil.aasp_intimacoes import buscar_intimacoes
    respx_mock.get("https://intimacaoapi.aasp.org.br/api/Associado/intimacao/json").mock(
        return_value=httpx.Response(200, json={
            "intimacoes": [], "erro": True, "status": "Chave inválida",
        }),
    )
    c = _aasp_client()
    try:
        with pytest.raises(RuntimeError, match="Chave inv"):
            buscar_intimacoes(c, "chave-ruim", datetime.date(2026, 8, 20))
    finally:
        c.close()


def test_salvar_raw_dedup(conn):
    from noviello_funil.aasp_intimacoes import salvar_raw
    item = {"numeroProcesso": "1", "conteudo": "X"}
    salvar_raw(conn, item, "2026-08-20")
    salvar_raw(conn, item, "2026-08-21")   # mesmo payload → não duplica
    n = conn.execute("SELECT COUNT(*) FROM aasp_raw").fetchone()[0]
    assert n == 1


def test_vista_roundtrip(conn):
    from noviello_funil.aasp_intimacoes import ja_vista, marcar_vista
    assert not ja_vista(conn, "abc")
    marcar_vista(conn, "abc", "1234567-08.2026.8.26.0100", "uuid-1")
    assert ja_vista(conn, "abc")
    marcar_vista(conn, "abc", "x", "y")   # idempotente, não levanta
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `uv run pytest tests/unit/test_aasp_intimacoes.py -v`
Expected: FAIL (funções não existem)

- [ ] **Step 3: Implementar**

Append em `aasp_intimacoes.py`:

```python
def buscar_intimacoes(
    client: httpx.Client, chave_api: str, data: datetime.date,
) -> list[dict]:
    """GET /api/Associado/intimacao/json de UM dia. Levanta em erro.

    `erro: true` com HTTP 200 é o jeito da AASP sinalizar falha (chave
    inválida etc.) — vira exceção pra o run falhar visível no journal,
    nunca "zero intimações" silencioso.
    """
    r = client.get(
        "/api/Associado/intimacao/json",
        params={"chave": chave_api, "data": data.isoformat()},
    )
    r.raise_for_status()
    corpo = r.json()
    if corpo.get("erro"):
        raise RuntimeError(f"AASP retornou erro: {corpo.get('status')!r}")
    return [i for i in (corpo.get("intimacoes") or []) if isinstance(i, dict)]


def salvar_raw(conn, item: dict, data_consulta: str) -> None:
    """Payload bruto → aasp_raw (dedup por hash do JSON canônico)."""
    payload = json.dumps(item, ensure_ascii=False, sort_keys=True)
    h = hashlib.sha256(payload.encode()).hexdigest()
    conn.execute(
        "INSERT OR IGNORE INTO aasp_raw (hash, payload, data_consulta) "
        "VALUES (?, ?, ?)",
        (h, payload, data_consulta),
    )


def ja_vista(conn, chave: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM aasp_intimacao_vista WHERE chave = ?", (chave,),
    ).fetchone()
    return row is not None


def marcar_vista(conn, chave: str, processo: str, law_suit_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO aasp_intimacao_vista "
        "(chave, processo, law_suit_id) VALUES (?, ?, ?)",
        (chave, processo, law_suit_id),
    )
```

- [ ] **Step 4: Rodar e ver passar**

Run: `uv run pytest tests/unit/test_aasp_intimacoes.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/noviello_funil/aasp_intimacoes.py tests/unit/test_aasp_intimacoes.py
git commit -m "feat(aasp): fetch da API AASP, raw store e dedup local"
```

---

### Task 4: Match com a carteira + andamento manual

**Files:**
- Modify: `src/noviello_funil/aasp_intimacoes.py`
- Test: `tests/unit/test_aasp_intimacoes.py` (append)

- [ ] **Step 1: Failing tests**

```python
# --- indexar_carteira / criar_andamento --------------------------------------

def _jq_client():
    return httpx.Client(
        base_url="https://api.juridiq.com.br",
        headers={"x-juridiq-api-key": "jq-test"},
    )


def test_indexar_carteira_paginada(respx_mock):
    from noviello_funil.aasp_intimacoes import indexar_carteira
    url = "https://api.juridiq.com.br/lawSuit/"
    respx_mock.get(url, params={"page": 1, "limit": 100}).mock(
        return_value=httpx.Response(200, json={
            "data": [{"id": "uuid-1",
                      "processNumber": "1234567-08.2026.8.26.0100"}],
            "totalPages": 2,
        }),
    )
    respx_mock.get(url, params={"page": 2, "limit": 100}).mock(
        return_value=httpx.Response(200, json={
            "data": [{"id": "uuid-2", "processNumber": ""}],  # sem número: fora
            "totalPages": 2,
        }),
    )
    c = _jq_client()
    try:
        idx = indexar_carteira(c)
    finally:
        c.close()
    assert idx == {"12345670820268260100": "uuid-1"}


def test_montar_conteudo():
    from noviello_funil.aasp_intimacoes import montar_conteudo
    txt = montar_conteudo({
        "jornal": "DJE SP", "data": "20/08/2026", "teor": "Intime-se." ,
    })
    assert txt.startswith("[AASP] Intimação — DJE SP — 20/08/2026")
    assert "Intime-se." in txt
    sem_teor = montar_conteudo({"jornal": "", "data": "", "teor": ""})
    assert "[AASP]" in sem_teor and "conferir" in sem_teor


def test_criar_andamento_ok_e_erro(respx_mock):
    from noviello_funil.aasp_intimacoes import criar_andamento
    respx_mock.post("https://api.juridiq.com.br/lawSuit/movements").mock(
        return_value=httpx.Response(201, json={"id": "mv-1"}),
    )
    c = _jq_client()
    try:
        ok, det = criar_andamento(c, "uuid-1", "[AASP] x", instance=2)
        assert (ok, det) == (True, "ok")
        body = json.loads(respx_mock.calls.last.request.content)
        assert body == {"lawSuitId": "uuid-1", "content": "[AASP] x",
                        "instance": 2}

        respx_mock.post("https://api.juridiq.com.br/lawSuit/movements").mock(
            return_value=httpx.Response(400, json={"message": "ruim"}),
        )
        ok, det = criar_andamento(c, "uuid-1", "x")
        assert ok is False and det.startswith("http_400")
    finally:
        c.close()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `uv run pytest tests/unit/test_aasp_intimacoes.py -v`
Expected: FAIL

- [ ] **Step 3: Implementar**

Append em `aasp_intimacoes.py`:

```python
def indexar_carteira(client: httpx.Client) -> dict[str, str]:
    """GET /lawSuit/ paginado → {dígitos do nº CNJ: lawSuitId}.

    Comparação por dígitos (não máscara): imune a diferença de formatação
    entre AASP e Juridiq. Processo sem número fica de fora (não casável).
    """
    idx: dict[str, str] = {}
    page = 1
    while True:
        r = client.get("/lawSuit/", params={"page": page, "limit": 100})
        r.raise_for_status()
        data = r.json()
        for p in data.get("data", []):
            digits = _so_digitos(p.get("processNumber"))
            if digits and p.get("id"):
                idx[digits] = p["id"]
        if page >= int(data.get("totalPages") or 1):
            break
        page += 1
    return idx


def montar_conteudo(item: dict) -> str:
    """Texto do andamento: cabeçalho [AASP] reconhecível + teor."""
    cab = "[AASP] Intimação"
    if item.get("jornal"):
        cab += f" — {item['jornal']}"
    if item.get("data"):
        cab += f" — {item['data']}"
    teor = (item.get("teor") or "").strip()
    if not teor:
        teor = "(sem teor no retorno da AASP — conferir no portal)"
    return f"{cab}\n\n{teor[:_TEOR_ANDAMENTO_CHARS]}"


def criar_andamento(
    client: httpx.Client, law_suit_id: str, content: str,
    instance: int | None = None,
) -> tuple[bool, str]:
    """POST /lawSuit/movements → (ok, detalhe). Não levanta — o caller
    decide (uma falha não pode derrubar as outras intimações)."""
    body: dict = {"lawSuitId": law_suit_id, "content": content}
    if instance:
        body["instance"] = instance
    try:
        r = client.post("/lawSuit/movements", json=body)
    except httpx.HTTPError as exc:
        return False, f"erro_{type(exc).__name__}"
    if r.status_code >= 400:
        return False, f"http_{r.status_code}: {r.text[:400]}"
    return True, "ok"
```

- [ ] **Step 4: Rodar e ver passar**

Run: `uv run pytest tests/unit/test_aasp_intimacoes.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/noviello_funil/aasp_intimacoes.py tests/unit/test_aasp_intimacoes.py
git commit -m "feat(aasp): match com a carteira e andamento manual no Juridiq"
```

---

### Task 5: Mensagem WhatsApp

**Files:**
- Modify: `src/noviello_funil/aasp_intimacoes.py`
- Test: `tests/unit/test_aasp_intimacoes.py` (append)

- [ ] **Step 1: Failing tests**

```python
# --- montar_mensagem ----------------------------------------------------------

def test_montar_mensagem_vazia():
    from noviello_funil.aasp_intimacoes import montar_mensagem
    assert montar_mensagem([], [], 0) is None


def test_montar_mensagem_completa():
    from noviello_funil.aasp_intimacoes import montar_mensagem
    casadas = [
        {"processo": "1234567-08.2026.8.26.0100", "data": "20/08/2026",
         "jornal": "DJE SP", "urgente": True, "motivo": "sentença publicada",
         "prazo": "15 dias", "andamento_ok": True},
        {"processo": "7654321-08.2026.8.26.0200", "data": "20/08/2026",
         "jornal": "DJE SP", "urgente": False, "motivo": "",
         "prazo": "", "andamento_ok": True},
    ]
    fora = [{"processo": "9999999-08.2026.8.26.0300", "data": "20/08/2026",
             "jornal": "DJE SP", "urgente": True,
             "motivo": "citação", "prazo": ""}]
    txt = montar_mensagem(casadas, fora, n_tarefas=1)
    assert "3" in txt                       # total de novas
    assert "2" in txt                       # viraram andamento
    assert "sentença publicada" in txt
    assert "prazo: 15 dias" in txt
    assert "9999999-08.2026.8.26.0300" in txt
    assert "fora da carteira" in txt.lower()
    assert "tarefa" in txt.lower()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `uv run pytest tests/unit/test_aasp_intimacoes.py -v`
Expected: FAIL

- [ ] **Step 3: Implementar**

Append em `aasp_intimacoes.py`:

```python
def montar_mensagem(
    casadas: list[dict], fora_carteira: list[dict], n_tarefas: int,
) -> str | None:
    """Resumo WhatsApp do run. None = nada novo (silêncio)."""
    total = len(casadas) + len(fora_carteira)
    if not total:
        return None
    plural = "intimações novas" if total > 1 else "intimação nova"
    blocos = [f"📨 *AASP: {total} {plural} no recorte*"]
    ok = sum(1 for c in casadas if c.get("andamento_ok"))
    if ok:
        blocos.append(f"✅ {ok} registrada(s) como andamento [AASP] no Juridiq.")
    falhas = len(casadas) - ok
    if falhas:
        blocos.append(f"⚠️ {falhas} falhou(aram) ao gravar — nova tentativa no próximo run.")
    if n_tarefas:
        blocos.append(
            f"✅ {n_tarefas} virou tarefa no painel (prazo SUGERIDO — confira a contagem)."
        )

    urgentes = [c for c in casadas if c.get("urgente")]
    if urgentes:
        blocos.append("\n⚠️ *Urgentes:*")
        for c in urgentes[:MAX_ITENS]:
            linha = f"• {c.get('data') or '?'} — {c.get('processo') or '(sem nº)'}"
            motivo = (c.get("motivo") or "").strip()
            prazo = (c.get("prazo") or "").strip()
            detalhe = motivo
            if prazo:
                detalhe = f"{motivo} (prazo: {prazo})" if motivo else f"prazo: {prazo}"
            if detalhe:
                linha += f"\n   _{detalhe[:_RESUMO_CHARS]}_"
            blocos.append(linha)

    if fora_carteira:
        blocos.append(
            "\n🚨 *Fora da carteira* (intimação de processo que NÃO está no "
            "Juridiq — cadastrar):"
        )
        for f in fora_carteira[:MAX_ITENS]:
            ref = f.get("processo") or f.get("jornal") or "(sem referência)"
            blocos.append(f"• {f.get('data') or '?'} — {ref}")

    blocos.append(
        "\nFonte: recorte AASP. Andamentos entram privados (não vão pro "
        "cliente no Jurichat)."
    )
    return "\n".join(blocos)
```

- [ ] **Step 4: Rodar e ver passar**

Run: `uv run pytest tests/unit/test_aasp_intimacoes.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/noviello_funil/aasp_intimacoes.py tests/unit/test_aasp_intimacoes.py
git commit -m "feat(aasp): mensagem WhatsApp do resumo diario"
```

---

### Task 6: main() — wiring completo

**Files:**
- Modify: `src/noviello_funil/aasp_intimacoes.py`
- Modify: `pyproject.toml` (`[project.scripts]`)
- Test: `tests/unit/test_aasp_intimacoes.py` (append)

- [ ] **Step 1: Failing test do processamento (função extraída, testável)**

O `main()` fica fino; a lógica por-item vai numa função pura-ish
`processar_novas` testada com respx + db em memória:

```python
# --- processar_novas ----------------------------------------------------------

def test_processar_novas_casa_grava_e_marca(respx_mock, conn):
    from noviello_funil.aasp_intimacoes import normalizar_item, processar_novas
    respx_mock.post("https://api.juridiq.com.br/lawSuit/movements").mock(
        return_value=httpx.Response(201, json={"id": "mv-1"}),
    )
    novas = [normalizar_item({
        "numeroProcesso": "1234567-08.2026.8.26.0100",
        "conteudo": "Intime-se.", "dataDisponibilizacao": "20/08/2026",
        "jornal": "DJE SP",
    })]
    idx = {"12345670820268260100": "uuid-1"}
    c = _jq_client()
    try:
        casadas, fora = processar_novas(c, conn, novas, idx)
    finally:
        c.close()
    assert len(casadas) == 1 and not fora
    assert casadas[0]["andamento_ok"] is True
    assert casadas[0]["law_suit_id"] == "uuid-1"
    # marcada como vista SÓ depois do 201
    row = conn.execute(
        "SELECT law_suit_id FROM aasp_intimacao_vista WHERE chave = ?",
        (novas[0]["chave"],),
    ).fetchone()
    assert row["law_suit_id"] == "uuid-1"


def test_processar_novas_falha_no_post_nao_marca(respx_mock, conn):
    from noviello_funil.aasp_intimacoes import normalizar_item, processar_novas
    respx_mock.post("https://api.juridiq.com.br/lawSuit/movements").mock(
        return_value=httpx.Response(500, json={"message": "boom"}),
    )
    novas = [normalizar_item({
        "numeroProcesso": "1234567-08.2026.8.26.0100", "conteudo": "X",
    })]
    idx = {"12345670820268260100": "uuid-1"}
    c = _jq_client()
    try:
        casadas, fora = processar_novas(c, conn, novas, idx)
    finally:
        c.close()
    assert casadas[0]["andamento_ok"] is False
    n = conn.execute("SELECT COUNT(*) FROM aasp_intimacao_vista").fetchone()[0]
    assert n == 0    # não marcada → retry no próximo run


def test_processar_novas_fora_da_carteira_nao_posta(respx_mock, conn):
    from noviello_funil.aasp_intimacoes import normalizar_item, processar_novas
    novas = [normalizar_item({
        "numeroProcesso": "9999999-08.2026.8.26.0300", "conteudo": "X",
    })]
    c = _jq_client()
    try:
        casadas, fora = processar_novas(c, conn, novas, {})
    finally:
        c.close()
    assert not casadas and len(fora) == 1
    assert not respx_mock.calls    # nenhum POST
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `uv run pytest tests/unit/test_aasp_intimacoes.py -v`
Expected: FAIL

- [ ] **Step 3: Implementar `processar_novas` + `main`**

Append em `aasp_intimacoes.py`:

```python
def processar_novas(
    jq: httpx.Client, conn, novas: list[dict], idx: dict[str, str],
) -> tuple[list[dict], list[dict]]:
    """Cria o andamento das casadas; separa as fora-da-carteira.

    try/except POR intimação (padrão publicacoes.py): uma falha não
    derruba as demais. Vista só é marcada após 201 — retry natural.
    Fora-da-carteira NÃO é marcada aqui (só depois do alerta enviado,
    no main — senão um crash antes do alerta silenciaria pra sempre).
    """
    casadas, fora = [], []
    for item in novas:
        try:
            law_suit_id = idx.get(item["processo_digitos"] or "—")
            if not law_suit_id:
                fora.append(item)
                continue
            ok, det = criar_andamento(
                jq, law_suit_id, montar_conteudo(item),
                instance=instancia_sugerida(item["processo_digitos"]),
            )
            item["law_suit_id"] = law_suit_id
            item["andamento_ok"] = ok
            if ok:
                marcar_vista(conn, item["chave"], item["processo"], law_suit_id)
            else:
                logger.error(
                    "aasp: andamento falhou processo=%s: %s",
                    item["processo"], det,
                )
            casadas.append(item)
        except Exception as exc:
            logger.exception(
                "aasp: erro na intimação %s: %s", item.get("chave"), exc,
            )
    return casadas, fora


def _criar_tarefas(settings, conn, jq: httpx.Client, casadas: list[dict]) -> int:
    """Urgente + andamento gravado → TAREFA de prazo (reuso prazo_tarefa).

    Idempotência pela MESMA tabela das publicações (tarefa_publicacao),
    com publication_id prefixado "aasp:". Falha em tarefa não derruba
    nada (o alerta é o canal fail-safe).
    """
    from noviello_funil.prazo_tarefa import (
        calcular_prazo_sugerido, criar_tarefa, deve_criar_tarefa, ja_criada,
        marcar_criada, montar_corpo_tarefa, montar_descricao, montar_titulo,
    )

    if not (settings.aasp_criar_tarefa and settings.task_column_id):
        return 0
    hoje = datetime.date.today()
    n = 0
    for item in casadas:
        try:
            if not (deve_criar_tarefa(item) and item.get("andamento_ok")):
                continue
            pid = f"aasp:{item['chave']}"
            if ja_criada(conn, pid):
                continue
            corpo = montar_corpo_tarefa(
                titulo=montar_titulo(
                    item.get("motivo") or "intimação AASP", item["processo"],
                ),
                descricao=montar_descricao(
                    item.get("motivo"), item.get("prazo"), item.get("teor"),
                    item.get("data"),
                ),
                final_date=calcular_prazo_sugerido(
                    item.get("prazo"), item.get("data"), hoje=hoje,
                ),
                initial_date=hoje.isoformat(),
                law_suit_id=item["law_suit_id"],
                column_id=settings.task_column_id,
                priority=settings.task_priority,
            )
            tid, det = criar_tarefa(jq, corpo)
            if not tid:
                logger.error("aasp: tarefa falhou %s: %s", item["processo"], det)
                continue
            try:
                marcar_criada(conn, pid, item["processo"], tid)
            except Exception as exc:
                logger.error(
                    "aasp: tarefa %s CRIADA mas marcar_criada falhou (órfã): %s",
                    tid, exc,
                )
            n += 1
        except Exception as exc:
            logger.exception("aasp: erro na tarefa de %s: %s", item.get("chave"), exc)
    return n


def main() -> int:
    """Entry point do console script ``noviello-aasp``.

    Nada novo no recorte → exit 0 silencioso.
    """
    import asyncio

    from anthropic import AsyncAnthropic

    from noviello_funil.config import Settings
    from noviello_funil.db import connect, run_migrations
    from noviello_funil.outbound import JurichatClient, notify_mario
    from noviello_funil.publicacoes import classificar_urgencia

    settings = Settings()
    logging.basicConfig(level=settings.log_level)

    for campo in ("aasp_chave", "juridiq_api_key"):
        if not getattr(settings, campo):
            logger.warning("aasp: %s não configurada — pulando", campo.upper())
            return 0
    if (
        not settings.mario_conversation_id
        or settings.mario_conversation_id == "placeholder-pendente"
    ):
        logger.warning("aasp: MARIO_CONVERSATION_ID não configurado — pulando")
        return 0

    hoje = datetime.date.today()
    aasp = httpx.Client(base_url=settings.aasp_base_url, timeout=30.0)
    brutos: list[tuple[str, dict]] = []
    try:
        for i in range(settings.aasp_dias_janela):
            d = hoje - datetime.timedelta(days=i)
            for raw in buscar_intimacoes(aasp, settings.aasp_chave, d):
                brutos.append((d.isoformat(), raw))
    finally:
        aasp.close()
    logger.info("aasp: %d itens na janela de %d dias", len(brutos),
                settings.aasp_dias_janela)

    conn = connect(settings.database_path)
    run_migrations(conn)
    jq = httpx.Client(
        base_url=settings.juridiq_base_url,
        headers={"x-juridiq-api-key": settings.juridiq_api_key},
        timeout=30.0,
    )
    try:
        novas = []
        for data_consulta, raw in brutos:
            salvar_raw(conn, raw, data_consulta)
            item = normalizar_item(raw)
            if not ja_vista(conn, item["chave"]):
                novas.append(item)
        logger.info("aasp: %d nova(s)", len(novas))
        if not novas:
            return 0

        idx = indexar_carteira(jq)
        casadas, fora = processar_novas(jq, conn, novas, idx)

        async def _run() -> None:
            anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)
            # Classifica TODAS as novas (casadas e fora) de uma vez; o
            # id/resumo que o classificador espera vem do adapter abaixo.
            para_classificar = [
                {**it, "id": it["chave"],
                 "resumo": f"Intimação AASP — {it['jornal'] or 'diário'}"}
                for it in casadas + fora
            ]
            classificadas = await classificar_urgencia(
                anthropic, settings.anthropic_model, para_classificar,
            )
            por_chave = {c["chave"]: c for c in classificadas}
            for it in casadas + fora:
                v = por_chave.get(it["chave"], {})
                it["urgente"] = bool(v.get("urgente"))
                it["motivo"] = v.get("motivo") or ""
                it["prazo"] = v.get("prazo") or ""

            n_tarefas = 0
            try:
                n_tarefas = _criar_tarefas(settings, conn, jq, casadas)
            except Exception as exc:
                logger.exception("aasp: criação de tarefas falhou (alerta segue): %s", exc)

            texto = montar_mensagem(casadas, fora, n_tarefas)
            if texto is None:
                return
            logger.info("aasp:\n%s", texto)
            jurichat = JurichatClient(
                api_key=settings.jurichat_api_key,
                base_url=settings.jurichat_base_url,
                bot_user_id=settings.jurichat_bot_user_id,
            )
            try:
                await notify_mario(
                    jurichat,
                    mario_conversation_id=settings.mario_conversation_id,
                    mensagem=texto,
                )
            finally:
                await jurichat.aclose()
            # Alerta enviado → agora sim as fora-da-carteira estão tratadas.
            for it in fora:
                marcar_vista(conn, it["chave"], it["processo"], "")

        asyncio.run(_run())
    finally:
        jq.close()
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Em `pyproject.toml`, `[project.scripts]`:

```toml
noviello-aasp = "noviello_funil.aasp_intimacoes:main"
```

- [ ] **Step 4: Rodar tudo**

Run: `uv run pytest tests/unit/test_aasp_intimacoes.py -v`
Expected: all passed
Run: `uv run pytest -q`
Expected: suíte inteira verde (795+)

- [ ] **Step 5: Commit**

```bash
git add src/noviello_funil/aasp_intimacoes.py pyproject.toml tests/unit/test_aasp_intimacoes.py
git commit -m "feat(aasp): main() do job noviello-aasp com classificacao e tarefas"
```

---

### Task 7: Units systemd + deploy

**Files:**
- Create: `deploy/noviello-aasp.service`
- Create: `deploy/noviello-aasp.timer`

- [ ] **Step 1: Criar units (padrão dos existentes)**

`deploy/noviello-aasp.service`:

```ini
[Unit]
Description=Noviello Funil Saude - Intimacoes AASP -> Juridiq
After=network.target

[Service]
Type=oneshot
TimeoutStartSec=600
User=noviello
Group=noviello
WorkingDirectory=/opt/noviello-funil-saude
EnvironmentFile=/opt/noviello-funil-saude/.env
ExecStart=/opt/noviello-funil-saude/.venv/bin/noviello-aasp
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true
PrivateTmp=true
```

`deploy/noviello-aasp.timer`:

```ini
[Unit]
Description=Noviello Funil Saude - Intimacoes AASP (diario 07h45 BRT)

[Timer]
# 10:45 UTC = 07:45 America/Sao_Paulo — antes do noviello-publicacoes
# (08h30 BRT). Diario incluindo fds: publicacao e rara no sabado mas a
# janela de 3 dias + dedup tornam o run barato e idempotente.
OnCalendar=*-*-* 10:45:00
Persistent=true
Unit=noviello-aasp.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 2: Commit + push**

```bash
git add deploy/noviello-aasp.service deploy/noviello-aasp.timer
git commit -m "feat(aasp): units systemd do job diario"
git push origin feat/mvp
```

- [ ] **Step 3: Deploy na VPS** (root@srv1740232, app em `/opt/noviello-funil-saude`)

```bash
ssh root@srv1740232 "cd /opt/noviello-funil-saude && git pull && .venv/bin/pip install -e . --quiet && cp deploy/noviello-aasp.service deploy/noviello-aasp.timer /etc/systemd/system/ && systemctl daemon-reload"
```

Adicionar `AASP_CHAVE=<chave real>` ao `/opt/noviello-funil-saude/.env`
(editar lá, nunca commitar). Depois:

```bash
ssh root@srv1740232 "systemctl enable --now noviello-aasp.timer && systemctl start noviello-aasp.service && journalctl -u noviello-aasp.service -n 30 --no-pager"
```

Expected: exit 0 com log "aasp: 0 itens na janela de 3 dias" (recorte
novo, vazio — normal).

- [ ] **Step 4: Verificação final**

```bash
ssh root@srv1740232 "systemctl list-timers | grep aasp"
```

Expected: `noviello-aasp.timer` agendado para o próximo 10:45 UTC.

---

## Self-review (feito na escrita)

- Spec coverage: fetch janela ✓ (T3+T6), raw store ✓ (T3), parser defensivo ✓ (T2), dedup ✓ (T3), match ✓ (T4), andamento+instance ✓ (T4), classificação+tarefa ✓ (T6), alertas/fora-carteira ✓ (T5+T6), config/env ✓ (T1), units ✓ (T7), gates de chave ✓ (T6).
- Sem placeholders; tipos consistentes entre tasks (`item` dict com chaves chave/processo/processo_digitos/teor/data/jornal definidas na T2 e usadas nas T4–T6).
- Nota consciente (registrada na spec): tarefa que falhar após o andamento gravado não é re-tentada (vista já marcada) — o alerta urgente cobre; aceito na v1.
