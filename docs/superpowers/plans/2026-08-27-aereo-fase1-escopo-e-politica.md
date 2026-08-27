# Aéreo — Fase 1: escopo curado e política de liberação por tipo de caso

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Um contrato de `aereo_consumidor` é criado e **liberado ao cliente sem gate humano**, com o escritório contra-assinando; todo outro tipo de caso mantém exatamente o comportamento atual (gate humano).

**Architecture:** A liberação deixa de ser global e passa a ser decidida por um módulo puro novo (`politica_contrato.py`) que mapeia `tipo_caso → política`. O default é `humano` — nenhum tipo migra sem entrada explícita na config, então a Fase 1 é zero-regressão por construção. O `orquestrador_contrato.gerar_contrato` consulta a política no passo [6] e, quando ela é automática e nenhum freio dispara, chama o `aprovar_e_liberar` que já existe. O escopo aéreo entra no catálogo curado com os quatro textos recortados do contrato real do escritório.

**Tech Stack:** Python 3.11, pydantic-settings, SQLite, pytest + pytest-asyncio, ruff.

**Fundamento da mudança de invariante (decisão Mario, 26/ago/2026):** o comentário em `config.py` registrava "nunca 100% automático (Prov. 205/2021, mandato personalíssimo)". A invariante foi revista **apenas para o modo automático com contra-assinatura**: a minuta sai sem revisão prévia, mas o contrato só se perfaz com a assinatura do escritório no `order_group 2`, que já existe no fluxo.

Por isso a Task 5 **trava em teste** que o modo automático só libera quando há alguém em `order_group 2` na lista de signatários **do documento**. Esse freio lê o fato, não a config: derivá-lo de `contrato_escritorio_email` seria uma segunda fonte de verdade sobre a mesma coisa, e um chamador que montasse `signers_extra` sem o escritório — com o e-mail ainda no `.env` — liberaria contrato sem contra-assinatura. Lendo a lista real, o fundamento ético deixa de ser convenção e vira garantia estrutural.

**Fora do escopo desta fase (vêm nas Fases 2 e 3):** classificação de `tipo_caso` pelo modelo, coleta conversacional de dados cadastrais, e o gatilho que liga a conversa ao `gerar_contrato`. Ao fim desta fase o contrato aéreo ainda é disparado pelo `scripts/gerar_contrato.py` — mas já nasce e se libera sozinho.

---

> ## ⚠ Estado deste documento: EXECUTADO (27/ago/2026)
>
> As Tasks 1 a 6 estão implementadas, revisadas e commitadas na branch `feat/aereo-fase1`. **A Task 7 é a única pendente, e é do Mario.**
>
> Os blocos de código das Tasks 1 a 6 abaixo são a especificação **original**. Em vários pontos o que foi construído é diferente — e melhor — porque a revisão adversarial encontrou falhas na especificação. **Não reexecute os blocos de código deste documento: eles reconstruiriam versões vulneráveis.** O código real está nos commits.
>
> | Correção que a revisão impôs | Commit |
> |---|---|
> | Freio de contra-assinatura lê a lista do documento, não a config | `af13d2e` (plano), `1b6ccfa` (código) |
> | Texto jurídico condensado por engano — base de honorários restaurada | `32eec00` |
> | Trilha de auditoria não afirma mais aprovação humana no automático | `87c322f` |
> | **Não liberar documento que a chamada não criou** (buraco na contra-assinatura) | `a5d7842` |
> | Guarda de duplicata só bloqueia contrato vivo; comentários deixam de afirmar gate incondicional | `d932437` |
>
> O que a revisão adversarial pegou e as três revisões do plano não pegaram está registrado em "Lições" no fim do documento.

---

## Estrutura de arquivos

| Arquivo | Responsabilidade |
|---|---|
| `src/noviello_funil/politica_contrato.py` **(novo)** | Registry `tipo_caso → política` e a decisão de liberar. Puro: sem I/O, sem banco, sem rede. |
| `src/noviello_funil/escopos.py` (modificar) | Ganha `TIPOS_CASO` (lista canônica) e a entrada `aereo_consumidor`. |
| `src/noviello_funil/config.py` (modificar) | Ganha `contrato_politica_por_tipo`, `contrato_teto_automatico`. Comentário da invariante reescrito. |
| `src/noviello_funil/orquestrador_contrato.py` (modificar) | Passo [6] consulta a política; libera ou fica pendente. |
| `tests/unit/test_politica_contrato.py` **(novo)** | Testes do módulo puro. |
| `tests/unit/test_escopos.py` (modificar ou criar) | Escopo aéreo resolve sem placeholder pendente. |
| `tests/unit/test_orquestrador_contrato.py` (modificar) | Liberação automática, regressão do gate humano, freios. |

---

### Task 1: Módulo de política — parse da configuração

**Files:**
- Create: `src/noviello_funil/politica_contrato.py`
- Test: `tests/unit/test_politica_contrato.py`

- [ ] **Step 1: Escreva o teste que falha**

Crie `tests/unit/test_politica_contrato.py`:

```python
"""Testes do registry de política de liberação por tipo de caso."""

import pytest

from noviello_funil.politica_contrato import (
    AUTOMATICO,
    HUMANO,
    parse_politicas,
    politica_do_tipo,
)


def test_parse_politicas_vazio():
    assert parse_politicas("") == {}
    assert parse_politicas("   ") == {}


def test_parse_politicas_um_par():
    assert parse_politicas("aereo_consumidor:automatico") == {
        "aereo_consumidor": AUTOMATICO,
    }


def test_parse_politicas_varios_pares_e_espacos():
    raw = " aereo_consumidor : automatico , usucapiao:humano "
    assert parse_politicas(raw) == {
        "aereo_consumidor": AUTOMATICO,
        "usucapiao": HUMANO,
    }


def test_parse_politicas_ignora_par_malformado():
    """Entrada torta na config NÃO pode virar liberação automática acidental."""
    assert parse_politicas("aereo_consumidor,lixo:,:vazio") == {}


def test_parse_politicas_valor_desconhecido_vira_humano():
    """Qualquer coisa que não seja exatamente 'automatico' é gate humano."""
    assert parse_politicas("aereo_consumidor:auto") == {
        "aereo_consumidor": HUMANO,
    }


def test_politica_do_tipo_default_e_humano():
    """Tipo ausente do mapa NUNCA libera sozinho — é o que garante
    zero-regressão: nada migra sem entrada explícita."""
    assert politica_do_tipo("inventario", {}) == HUMANO
    assert politica_do_tipo("", {"aereo_consumidor": AUTOMATICO}) == HUMANO


def test_politica_do_tipo_encontrada():
    mapa = {"aereo_consumidor": AUTOMATICO}
    assert politica_do_tipo("aereo_consumidor", mapa) == AUTOMATICO
```

- [ ] **Step 2: Rode para ver falhar**

```bash
cd C:/Users/mario/noviello-funil-saude && uv run pytest tests/unit/test_politica_contrato.py -v
```

Esperado: `ModuleNotFoundError: No module named 'noviello_funil.politica_contrato'`

- [ ] **Step 3: Implemente o mínimo**

Crie `src/noviello_funil/politica_contrato.py`:

```python
"""Política de LIBERAÇÃO do contrato, por tipo de caso.

Até 26/ago/2026 a liberação era global e sempre humana: o doc nascia em
silêncio (``send_automatic_email=False``) e só ia ao cliente quando o Mario
aprovava o PDF real. Este módulo torna isso decidível POR TIPO DE CASO, para
que um produto padronizado (aéreo do consumidor) rode sem intervenção.

DEFAULT É ``HUMANO``. Um tipo só libera sozinho se estiver EXPLÍCITO na
config — entrada ausente, vazia ou malformada cai no gate humano. É o que
faz esta mudança ser zero-regressão por construção.

Módulo PURO de propósito: sem banco, sem rede, sem settings. Recebe tudo por
parâmetro e devolve decisão. Assim a regra que libera contrato ao cliente é
testável sem subir nada.
"""

from typing import Final

HUMANO: Final = "humano"
AUTOMATICO: Final = "automatico"


def parse_politicas(raw: str) -> dict[str, str]:
    """``"aereo_consumidor:automatico,usucapiao:humano"`` → dict.

    Par sem ``:``, com tipo vazio ou com valor vazio é DESCARTADO (não vira
    entrada nenhuma). Valor que não seja exatamente ``automatico`` vira
    ``HUMANO`` — na dúvida, gate humano.
    """
    mapa: dict[str, str] = {}
    for par in (raw or "").split(","):
        if ":" not in par:
            continue
        tipo, _, valor = par.partition(":")
        tipo, valor = tipo.strip(), valor.strip().lower()
        if not tipo or not valor:
            continue
        mapa[tipo] = AUTOMATICO if valor == AUTOMATICO else HUMANO
    return mapa


def politica_do_tipo(tipo_caso: str, politicas: dict[str, str]) -> str:
    """Política do tipo. Ausente → ``HUMANO``."""
    return politicas.get((tipo_caso or "").strip(), HUMANO)
```

- [ ] **Step 4: Rode para ver passar**

```bash
cd C:/Users/mario/noviello-funil-saude && uv run pytest tests/unit/test_politica_contrato.py -v
```

Esperado: 7 passed.

- [ ] **Step 5: Commit**

```bash
cd C:/Users/mario/noviello-funil-saude && git add src/noviello_funil/politica_contrato.py tests/unit/test_politica_contrato.py && git commit -m "feat(contrato): registry de politica de liberacao por tipo de caso"
```

---

### Task 2: Módulo de política — a decisão de liberar, com os freios

**Files:**
- Modify: `src/noviello_funil/politica_contrato.py`
- Test: `tests/unit/test_politica_contrato.py`

- [ ] **Step 1: Escreva o teste que falha**

Acrescente ao fim de `tests/unit/test_politica_contrato.py`:

```python
from noviello_funil.politica_contrato import decidir_liberacao


def _ctx(**over):
    base = dict(
        tipo_caso="aereo_consumidor",
        politicas={"aereo_consumidor": AUTOMATICO},
        valor_honorarios=1000.0,
        teto_automatico=0.0,
        tem_contra_assinante=True,
    )
    base.update(over)
    return base


def test_libera_automatico_no_caminho_feliz():
    libera, motivo = decidir_liberacao(**_ctx())
    assert libera is True
    assert motivo == "politica_automatica"


def test_nao_libera_quando_politica_e_humana():
    libera, motivo = decidir_liberacao(**_ctx(tipo_caso="inventario"))
    assert libera is False
    assert motivo == "politica_humana"


def test_nao_libera_sem_contra_assinante():
    """FREIO DURO. O fundamento que sustenta a liberação automática (decisão
    Mario 26/ago/2026) é a contra-assinatura do escritório no order_group 2.
    Sem contra-assinante configurado, some o fundamento — não libera."""
    libera, motivo = decidir_liberacao(**_ctx(tem_contra_assinante=False))
    assert libera is False
    assert motivo == "sem_contra_assinante"


def test_nao_libera_acima_do_teto():
    libera, motivo = decidir_liberacao(
        **_ctx(valor_honorarios=5000.0, teto_automatico=1500.0)
    )
    assert libera is False
    assert motivo == "acima_do_teto"


def test_teto_zero_desliga_a_checagem_de_teto():
    libera, _ = decidir_liberacao(
        **_ctx(valor_honorarios=99999.0, teto_automatico=0.0)
    )
    assert libera is True


def test_valor_exatamente_no_teto_libera():
    libera, _ = decidir_liberacao(
        **_ctx(valor_honorarios=1500.0, teto_automatico=1500.0)
    )
    assert libera is True


def test_ordem_dos_freios_contra_assinante_antes_do_teto():
    """Os dois freios ativos ao mesmo tempo: reporta o do fundamento ético,
    que é o que o Mario precisa ver primeiro no alerta."""
    libera, motivo = decidir_liberacao(
        **_ctx(tem_contra_assinante=False, valor_honorarios=9e9,
               teto_automatico=10.0)
    )
    assert libera is False
    assert motivo == "sem_contra_assinante"
```

- [ ] **Step 2: Rode para ver falhar**

```bash
cd C:/Users/mario/noviello-funil-saude && uv run pytest tests/unit/test_politica_contrato.py -v
```

Esperado: `ImportError: cannot import name 'decidir_liberacao'`

- [ ] **Step 3: Implemente o mínimo**

Acrescente ao fim de `src/noviello_funil/politica_contrato.py`:

```python
def decidir_liberacao(
    *,
    tipo_caso: str,
    politicas: dict[str, str],
    valor_honorarios: float,
    teto_automatico: float,
    tem_contra_assinante: bool,
) -> tuple[bool, str]:
    """Libera a assinatura sozinho? Devolve ``(libera, motivo)``.

    Motivos possíveis: ``politica_automatica`` (libera), ``politica_humana``,
    ``sem_contra_assinante``, ``acima_do_teto``.

    Os freios DUROS do pipeline (conflito de interesse, escopo ausente, CPF
    inválido, sem canal de contato) já barraram antes — nada chega aqui sem
    ter passado por eles. Aqui só ficam os freios da LIBERAÇÃO.

    Ordem importa: ``sem_contra_assinante`` é checado antes do teto porque é
    o freio do fundamento ético, e é o que precisa aparecer no alerta.
    """
    if politica_do_tipo(tipo_caso, politicas) != AUTOMATICO:
        return False, "politica_humana"
    if not tem_contra_assinante:
        return False, "sem_contra_assinante"
    if teto_automatico > 0 and valor_honorarios > teto_automatico:
        return False, "acima_do_teto"
    return True, "politica_automatica"
```

- [ ] **Step 4: Rode para ver passar**

```bash
cd C:/Users/mario/noviello-funil-saude && uv run pytest tests/unit/test_politica_contrato.py -v
```

Esperado: 14 passed.

- [ ] **Step 5: Commit**

```bash
cd C:/Users/mario/noviello-funil-saude && git add src/noviello_funil/politica_contrato.py tests/unit/test_politica_contrato.py && git commit -m "feat(contrato): decidir_liberacao com freio de contra-assinatura e teto"
```

---

### Task 3: Escopo curado do aéreo

**Files:**
- Modify: `src/noviello_funil/escopos.py`
- Test: `tests/unit/test_escopos.py`

**Origem do texto:** recorte literal das Cláusulas 1ª (caput, §1 e §2) e 4ª (§1 e §2) do contrato aéreo do escritório (`Ações 2026\_Geral\Contrato - Aereo.docx`). Nenhuma frase foi redigida pela IA — invariante I6.

- [ ] **Step 1: Escreva o teste que falha**

Crie `tests/unit/test_escopos.py` (se já existir, acrescente ao fim):

```python
"""Testes do catálogo de escopos curados."""

from noviello_funil.escopos import (
    ESCOPOS,
    TIPOS_CASO,
    resolver_escopo,
    tipos_disponiveis,
)


def test_aereo_esta_no_catalogo():
    assert "aereo_consumidor" in ESCOPOS
    assert "aereo_consumidor" in tipos_disponiveis()


def test_aereo_tem_os_quatro_textos_preenchidos():
    e = ESCOPOS["aereo_consumidor"]
    for chave in (
        "area_atuacao", "objeto_contrato",
        "contexto_normativo", "descricao_honorarios",
    ):
        assert e.get(chave, "").strip(), f"{chave} vazio"


def test_aereo_resolve_sem_placeholder_pendente():
    """Pegadinha da ZapSign: ela NÃO substitui placeholder dentro do valor de
    outro placeholder. Depois de resolver_escopo, nenhum {{...}} pode sobrar."""
    escopo = resolver_escopo(
        "aereo_consumidor",
        substituicoes={
            "{{VALOR_HONORARIOS}}": "500,00",
            "{{VALOR_HONORARIOS_EXTENSO}}": "quinhentos reais",
        },
    )
    assert escopo is not None
    for chave, texto in escopo.items():
        assert "{{" not in texto, f"placeholder pendente em {chave}: {texto}"


def test_tipos_disponiveis_e_subconjunto_de_tipos_caso():
    """TIPOS_CASO é a lista canônica (tudo que o escritório atende).
    tipos_disponiveis() é o subconjunto com texto curado escrito."""
    assert set(tipos_disponiveis()) <= set(TIPOS_CASO)


def test_tipos_caso_nao_tem_duplicata():
    assert len(TIPOS_CASO) == len(set(TIPOS_CASO))


def test_honorarios_padrao_do_aereo():
    """O valor vem de TABELA CURADA pelo advogado, nunca do modelo
    (invariante I7). R$ 500,00 é decisão do Mario de 27/ago/2026."""
    from noviello_funil.escopos import HONORARIOS_PADRAO

    assert HONORARIOS_PADRAO["aereo_consumidor"] == (500.0, "quinhentos reais")


def test_honorarios_padrao_so_para_tipo_conhecido():
    from noviello_funil.escopos import HONORARIOS_PADRAO

    assert set(HONORARIOS_PADRAO) <= set(TIPOS_CASO)


def test_todo_tipo_com_honorario_padrao_tem_escopo_escrito():
    """Ter preço de tabela sem texto curado geraria contrato sem cláusula.
    Se um dia alguém acrescentar um preço, o escopo tem que vir junto."""
    from noviello_funil.escopos import HONORARIOS_PADRAO

    assert set(HONORARIOS_PADRAO) <= set(tipos_disponiveis())
```

- [ ] **Step 2: Rode para ver falhar**

```bash
cd C:/Users/mario/noviello-funil-saude && uv run pytest tests/unit/test_escopos.py -v
```

Esperado: `ImportError: cannot import name 'TIPOS_CASO'`

- [ ] **Step 3: Implemente o mínimo**

Em `src/noviello_funil/escopos.py`, logo antes de `ESCOPOS: dict[...]`, acrescente a lista canônica:

```python
# Lista CANÔNICA dos tipos de caso que o escritório atende. É a fonte única
# de verdade — a Fase 2 (classificação pelo modelo) vai gerar o enum do
# schema a partir daqui, pra nunca haver tipo que o modelo emite e o catálogo
# desconhece. Ter entrada aqui NÃO significa ter escopo escrito: quem tem
# texto curado é ESCOPOS/tipos_disponiveis().
TIPOS_CASO: list[str] = [
    "aereo_consumidor",
    "urbanistico_iptu_regularizacao",
    "saude_suplementar",
    "sucessorio_inventario",
    "usucapiao",
    "imobiliario_compra_venda",
    "locacao_despejo",
    "condominial",
    "previdenciario_inss",
    "direito_senior",
]

# Honorários INICIAIS (pro labore) de tabela, por tipo de caso:
# ``tipo → (valor, valor_por_extenso)``. Curado pelo advogado, igual ao texto
# do escopo — é o que permite fechar sem humano no circuito SEM violar a
# invariante de que a IA nunca precifica. Tipo ausente daqui exige valor
# informado na chamada (é o caso de todos os que passam por reunião).
# O êxito (ad exitum) NÃO entra aqui: é percentual, vive no texto do escopo.
HONORARIOS_PADRAO: dict[str, tuple[float, str]] = {
    # Decisão Mario 27/ago/2026: R$ 500,00 + 35% de êxito. ATENÇÃO — o
    # contrato-modelo em `Ações 2026\_Geral\Contrato - Aereo.docx` ainda diz
    # R$ 1.000,00 no texto fixo da Cláusula 4ª §1; o valor vigente é ESTE.
    "aereo_consumidor": (500.0, "quinhentos reais"),
}
```

Ainda em `escopos.py`, acrescente a entrada dentro do dict `ESCOPOS` (antes do bloco de comentários `# PENDENTE`):

```python
    "aereo_consumidor": {
        "area_atuacao": "Direito do Consumidor e Direito Aéreo",
        "objeto_contrato": (
            "O objeto do presente contrato é a prestação de serviços "
            "advocatícios, em âmbito judicial e/ou extrajudicial, para a "
            "defesa dos interesses do CONTRATANTE em decorrência de eventos "
            "relativos a falhas na execução do contrato de transporte aéreo, "
            "abrangendo, de forma exemplificativa e não exaustiva, as "
            "seguintes hipóteses de ilícito civil praticado pelas companhias "
            "aéreas ou demais fornecedores envolvidos na cadeia de consumo: "
            "atraso ou cancelamento de voo, total ou parcial; preterição de "
            "embarque (overbooking); extravio, perda, furto, avaria ou atraso "
            "na entrega de bagagem despachada; negativa de assistência "
            "material em solo, incluindo alimentação, comunicação e "
            "hospedagem, nos prazos e formas exigidos pela regulamentação "
            "setorial; perda de conexão que resulte na inviabilidade da "
            "viagem ou no atraso significativo na chegada ao destino final; "
            "alteração unilateral e indevida do contrato de transporte pelo "
            "fornecedor; falha no cumprimento de pacotes turísticos que "
            "envolvam o serviço aéreo; e qualquer outra ocorrência que viole "
            "os direitos do passageiro aéreo estabelecidos no arcabouço "
            "normativo brasileiro. A prestação engloba todas as fases do "
            "processo ou procedimento necessário à plena defesa dos direitos "
            "do CONTRATANTE, desde a análise preliminar da documentação e "
            "fatos, passando pela fase de tentativa de acordo amigável, "
            "instauração de reclamações perante órgãos reguladores ou "
            "plataformas de consumo, até o ajuizamento, acompanhamento e "
            "condução da ação judicial em todas as suas instâncias, inclusive "
            "por meio de recursos cabíveis, procedimentos de cumprimento de "
            "sentença e realização de atos de expropriação, se necessários."
        ),
        "contexto_normativo": (
            "A atividade de defesa dos direitos do passageiro aéreo se insere "
            "no sistema normativo brasileiro que compreende o Código de "
            "Defesa do Consumidor (Lei nº 8.078/90) como norma "
            "principiológica de proteção, a Resolução nº 400/2016 da Agência "
            "Nacional de Aviação Civil (ANAC) e suas alterações, que dispõe "
            "sobre as condições gerais de transporte aéreo e os deveres de "
            "assistência ao passageiro, e os Tratados e Convenções "
            "Internacionais, como a Convenção de Montreal, que podem ter "
            "aplicação preponderante, especialmente nos casos de transporte "
            "aéreo internacional, para a fixação de limites de indenização "
            "por danos materiais, conforme entendimento consolidado pelas "
            "Cortes Superiores brasileiras."
        ),
        "descricao_honorarios": (
            "Honorários iniciais (pro labore): quantia fixa, única e "
            "irrepetível de R$ {{VALOR_HONORARIOS}} "
            "({{VALOR_HONORARIOS_EXTENSO}}), devida pela aceitação do "
            "mandato, pela consultoria jurídica especializada inicial, pela "
            "análise dos documentos e fatos do caso e pela elaboração das "
            "peças inaugurais, paga no ato da assinatura do contrato. "
            "Honorários de êxito (ad exitum): 35% (trinta e cinco por cento) "
            "sobre o proveito econômico bruto total auferido pelo "
            "CONTRATANTE em decorrência da atuação do CONTRATADO, "
            "compreendida a integralidade dos valores recebidos a título de "
            "danos morais, danos materiais, reembolso de passagens ou "
            "serviços e valores oriundos de acordos judiciais ou "
            "extrajudiciais. Os honorários de sucumbência, quando houver "
            "condenação da parte adversa, pertencem integralmente ao "
            "CONTRATADO."
        ),
    },
```

- [ ] **Step 4: Rode para ver passar**

```bash
cd C:/Users/mario/noviello-funil-saude && uv run pytest tests/unit/test_escopos.py -v
```

Esperado: 8 passed.

- [ ] **Step 5: Commit**

```bash
cd C:/Users/mario/noviello-funil-saude && git add src/noviello_funil/escopos.py tests/unit/test_escopos.py && git commit -m "feat(escopos): escopo curado do aereo do consumidor + lista canonica TIPOS_CASO"
```

---

### Task 4: Configuração e reescrita do comentário da invariante

**Files:**
- Modify: `src/noviello_funil/config.py:187-193`
- Modify: `.env.example`

O `CLAUDE.md` do projeto é explícito: variável nova entra no `config.py` **e** no `.env.example`, sempre com placeholder, nunca com valor real. As duas coisas na mesma tarefa.

- [ ] **Step 1: Substitua o bloco de comentário e acrescente os campos**

Em `src/noviello_funil/config.py`, o bloco atual é:

```python
    # Fluxo 1-TOQUE: o bot monta a minuta e o Mario aprova UM contrato por
    # vez. O create-doc SÓ roda depois da aprovação humana — nunca 100%
    # automático (Prov. 205/2021, mandato personalíssimo). contratos_zapsign
    # liga a feature (default OFF). Token e secret no .env (gitignored),
    # nunca no código. Escopo inicial: SÓ contrato de honorários (procuração
    # fica fora até confirmar aceitação no foro — decisão 15/jun).
    contratos_zapsign: bool = False
```

Troque por:

```python
    # Fluxo 1-TOQUE: o bot monta a minuta e o Mario aprova UM contrato por
    # vez. contratos_zapsign liga a feature (default OFF). Token e secret no
    # .env (gitignored), nunca no código. Escopo inicial: SÓ contrato de
    # honorários (procuração fica fora até confirmar aceitação no foro —
    # decisão 15/jun).
    #
    # INVARIANTE REVISTA em 26/ago/2026 (decisão Mario). Antes: "nunca 100%
    # automático (Prov. 205/2021, mandato personalíssimo)". Agora a liberação
    # é decidida POR TIPO DE CASO (ver politica_contrato.py), e o modo
    # automático se sustenta na CONTRA-ASSINATURA: a minuta sai sem revisão
    # prévia, mas o contrato só se perfaz com a assinatura do escritório no
    # order_group 2 — ou seja, o mandato continua tendo ato de advogado. Por
    # isso decidir_liberacao NÃO libera sem contra-assinante configurado.
    # Default de todo tipo de caso continua sendo o gate humano.
    contratos_zapsign: bool = False
```

- [ ] **Step 2: Acrescente os campos novos logo após `asaas_payment_due_days`**

Localize em `config.py`:

```python
    asaas_payment_due_days: int = Field(default=7, ge=1)
```

Acrescente imediatamente abaixo:

```python

    # Política de liberação por tipo de caso, no formato
    # "tipo:politica,tipo:politica" — ex.: "aereo_consumidor:automatico".
    # Tipo ausente = gate humano (default seguro). Ver politica_contrato.py.
    contrato_politica_por_tipo: str = ""
    # Teto de honorários para liberação automática. 0 = sem teto. Existe pra
    # que um valor fora da curva num tipo padronizado caia no gate humano em
    # vez de ir ao cliente.
    contrato_teto_automatico: float = Field(default=0.0, ge=0)
```

- [ ] **Step 3: Espelhe no `.env.example`**

Em `.env.example`, logo após o bloco `CONTRATO_TESTEMUNHA_2_CPF=`, acrescente:

```
# Liberação da assinatura POR TIPO DE CASO: "tipo:politica,tipo:politica".
# Só "automatico" libera sem revisão humana; qualquer outro valor, e qualquer
# tipo ausente daqui, cai no gate humano (default seguro). O modo automático
# ainda exige o escritório contra-assinando (order_group 2) — sem
# CONTRATO_ESCRITORIO_EMAIL preenchido, não libera. Ex.:
# CONTRATO_POLITICA_POR_TIPO=aereo_consumidor:automatico
CONTRATO_POLITICA_POR_TIPO=
# Teto de honorários da liberação automática. 0 = sem teto. Ligue junto com a
# política: é o que faz um valor fora da curva cair no gate humano em vez de
# ir ao cliente. Ex.: 600
CONTRATO_TETO_AUTOMATICO=0
```

- [ ] **Step 4: Verifique que o Settings ainda carrega**

```bash
cd C:/Users/mario/noviello-funil-saude && uv run python -c "from noviello_funil.config import Settings; print([c for c in Settings.model_fields if 'politica' in c or 'teto' in c])"
```

Esperado: `['contrato_politica_por_tipo', 'contrato_teto_automatico']`

- [ ] **Step 5: Rode a suíte inteira para garantir zero regressão**

```bash
cd C:/Users/mario/noviello-funil-saude && uv run pytest -q
```

Esperado: tudo que passava antes continua passando.

- [ ] **Step 6: Commit**

```bash
cd C:/Users/mario/noviello-funil-saude && git add src/noviello_funil/config.py .env.example && git commit -m "feat(config): politica de liberacao por tipo + teto; invariante revista com fundamento da contra-assinatura"
```

---

### Task 5: O orquestrador honra a política

**Files:**
- Modify: `src/noviello_funil/orquestrador_contrato.py` (função `gerar_contrato`, final)
- Modify: `src/noviello_funil/config.py` — retirar o aviso de "sem efeito"
- Modify: `.env.example` — idem
- Test: `tests/unit/test_orquestrador_contrato.py`

A Task 4 deixou em `config.py` e `.env.example` um aviso dizendo que os dois campos **não têm efeito nenhum** enquanto o orquestrador não consultar a política. Esta tarefa é o wiring — no instante em que ela entra, aquele aviso vira falso. **Retirá-lo faz parte desta tarefa**, não da seguinte. Um comentário que mente sobre o estado do sistema é o defeito que a Task 4 existiu para remover; não vale reintroduzi-lo pelo outro lado.

O `gerar_contrato` hoje termina devolvendo `{"status": "pendente_revisao", ...}`. Vai passar a consultar a política e, quando automática, chamar o `aprovar_e_liberar` que já existe no módulo.

- [ ] **Step 1: Escreva os testes que falham**

Acrescente ao fim de `tests/unit/test_orquestrador_contrato.py`:

```python
from noviello_funil.politica_contrato import AUTOMATICO

# --- liberação automática por tipo de caso (Fase 1 aéreo) --------------------


@pytest.mark.asyncio
async def test_tipo_sem_politica_continua_no_gate_humano():
    """REGRESSÃO: sem config de política, tudo se comporta como antes."""
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()

    out = await _gerar(conn, asaas, zap)

    assert out["status"] == "pendente_revisao"
    assert zap.resend_calls == []


@pytest.mark.asyncio
async def test_politica_automatica_libera_e_chama_resend():
    """SIGNERS_EXTRA já traz o escritório em order_group 2 — a contra-assinatura
    existe, então o freio ético não dispara."""
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()

    out = await _gerar(conn, asaas, zap, politicas={TIPO: AUTOMATICO})

    assert out["status"] == "liberado_automatico"
    assert out["motivo_liberacao"] == "politica_automatica"
    assert zap.resend_calls == [zap.doc_token]


@pytest.mark.asyncio
async def test_sem_escritorio_na_lista_nao_libera():
    """FREIO ESTRUTURAL. Sem ninguém em order_group 2 no documento, não há
    contra-assinatura — e é ela que sustenta o fundamento do modo automático.
    O freio lê a lista REAL de signatários, não a config: config e documento
    poderiam divergir, e aí o freio protegeria o fato errado."""
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()
    so_testemunha = [s for s in SIGNERS_EXTRA if s.get("order_group") != 2]

    out = await _gerar(
        conn, asaas, zap,
        politicas={TIPO: AUTOMATICO},
        signers_extra=so_testemunha,
    )

    assert out["status"] == "pendente_revisao"
    assert out["motivo_liberacao"] == "sem_contra_assinante"
    assert zap.resend_calls == []


@pytest.mark.asyncio
async def test_lista_de_signatarios_vazia_nao_libera():
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()

    out = await _gerar(
        conn, asaas, zap, politicas={TIPO: AUTOMATICO}, signers_extra=[],
    )

    assert out["status"] == "pendente_revisao"
    assert out["motivo_liberacao"] == "sem_contra_assinante"
    assert zap.resend_calls == []


@pytest.mark.asyncio
async def test_politica_automatica_acima_do_teto_nao_libera():
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()

    out = await _gerar(
        conn, asaas, zap,
        politicas={TIPO: AUTOMATICO},
        teto_automatico=100.0,
    )

    assert out["status"] == "pendente_revisao"
    assert out["motivo_liberacao"] == "acima_do_teto"
    assert zap.resend_calls == []


@pytest.mark.asyncio
async def test_send_automatic_email_continua_false_mesmo_no_automatico():
    """INVARIANTE: o doc SEMPRE nasce em silêncio. Criar e liberar seguem
    sendo duas chamadas — é o que permite mudar de política sem reescrever
    o pipeline."""
    conn = _db()
    asaas, zap = FakeAsaas(), FakeZapSign()

    await _gerar(conn, asaas, zap, politicas={TIPO: AUTOMATICO})

    assert zap.create_calls[0]["send_automatic_email"] is False
```

- [ ] **Step 2: Rode para ver falhar**

```bash
cd C:/Users/mario/noviello-funil-saude && uv run pytest tests/unit/test_orquestrador_contrato.py -k "politica or gate_humano or automatic_email_continua" -v
```

Esperado: FAIL — `gerar_contrato() got an unexpected keyword argument 'politicas'`

- [ ] **Step 3: Implemente**

> **SUPERADO.** O código abaixo não tem a guarda `doc_preexistente` nem o `motivo=` da auditoria, e chama a política na ordem errada. Ver `a5d7842`, `87c322f` e `d932437` para o que realmente foi construído.

`gerar_contrato` tem **dois** `return await _finalizar_apos_cobranca(...)` — o do caminho da cobrança nova e o do caminho do dedupe. Colocar a política nos dois seria duplicar a regra. Em vez disso, criamos um envelope só, e os dois passam a chamá-lo.

Em `src/noviello_funil/orquestrador_contrato.py`, acrescente o import junto dos outros imports locais:

```python
from .politica_contrato import decidir_liberacao, parse_politicas
```

Na assinatura de `gerar_contrato`, acrescente os dois parâmetros keyword-only ao final (depois de `lead_id: int | None = None`):

```python
    politicas: dict[str, str] | None = None,
    teto_automatico: float = 0.0,
```

Repare que **não** existe parâmetro `tem_contra_assinante`. Ele é derivado dentro do envelope, a partir da lista de signatários real. Um parâmetro seria uma segunda fonte de verdade sobre o mesmo fato, e as duas poderiam divergir.

Na docstring de `gerar_contrato`, acrescente à lista de status:

```
      - 'liberado_automatico' → a política do tipo de caso é automática e
        nenhum freio disparou; a assinatura JÁ foi liberada ao cliente. O doc
        nasceu em silêncio do mesmo jeito — criar e liberar seguem separados.
```

Acrescente a função-envelope logo **antes** de `_finalizar_apos_cobranca`:

```python
async def _finalizar_e_liberar(
    conn: sqlite3.Connection,
    zapsign: Any,
    *,
    tipo_caso: str,
    valor_honorarios: float,
    politicas: dict[str, str],
    teto_automatico: float,
    **kwargs: Any,
) -> dict[str, Any]:
    """[6]+[7]: cria o doc em silêncio e, se a política do tipo de caso mandar,
    LIBERA a assinatura ao cliente sem gate humano.

    Envelope único: ``gerar_contrato`` tem dois caminhos até a finalização
    (cobrança nova e dedupe) e a regra de liberação não pode viver duplicada
    nos dois. O doc SEMPRE nasce em silêncio — criar e liberar continuam
    sendo duas chamadas, e é isso que permite trocar de política sem
    reescrever o pipeline.

    ``tem_contra_assinante`` é derivado da lista de signatários QUE VAI NO
    DOCUMENTO, não da config. Ler do ``Settings`` seria ler um proxy: um
    chamador que montasse ``signers_extra`` sem o escritório, com o e-mail
    ainda no ``.env``, liberaria contrato sem contra-assinatura — e é
    justamente a contra-assinatura que sustenta o fundamento do modo
    automático (Prov. 205/2021). Derivando do fato, o freio deixa de ser
    convenção e passa a ser garantia estrutural.

    Falha ao liberar NÃO é falha do contrato: ele existe, está cobrado e
    revisável. Fica em PENDENTE_REVISAO pro Mario liberar na mão. Esse caso
    devolve um 5º motivo, ``falha_ao_liberar``, que NÃO vem do
    ``decidir_liberacao`` — quem lê ``motivo_liberacao`` tem que contar com ele.
    """
    resultado = await _finalizar_apos_cobranca(conn, zapsign, **kwargs)
    if resultado.get("status") != "pendente_revisao":
        return resultado

    # order_group 2 = o escritório contra-assinando depois do cliente. Só
    # existe na lista se montar_signers_padrao encontrou o e-mail na config.
    tem_contra_assinante = any(
        s.get("order_group") == 2 for s in kwargs.get("signers_extra") or []
    )
    libera, motivo = decidir_liberacao(
        tipo_caso=tipo_caso,
        politicas=politicas,
        valor_honorarios=valor_honorarios,
        teto_automatico=teto_automatico,
        tem_contra_assinante=tem_contra_assinante,
    )
    resultado["motivo_liberacao"] = motivo
    if not libera:
        return resultado

    contrato = get_contrato(conn, kwargs["contrato_id"])
    saida = await aprovar_e_liberar(
        conn, zapsign,
        token=contrato["aprovacao_token"],
        ator="sistema",
    )
    if saida.get("status") != "liberado":
        logger.warning(
            "liberação automática falhou (contrato=%s): %r",
            kwargs["contrato_id"], saida,
        )
        resultado["motivo_liberacao"] = "falha_ao_liberar"
        return resultado
    resultado["status"] = "liberado_automatico"
    return resultado
```

Agora troque os **dois** `return await _finalizar_apos_cobranca(...)` dentro de `gerar_contrato` por chamadas ao envelope. Os dois têm exatamente o mesmo corpo de argumentos; acrescente os cinco novos em cada:

```python
        return await _finalizar_e_liberar(
            conn, zapsign,
            tipo_caso=tipo_caso,
            valor_honorarios=valor_honorarios,
            politicas=politicas or {},
            teto_automatico=teto_automatico,
            contrato_id=contrato_id, cliente=cliente,
            escopo=escopo, valor_fmt=valor_fmt, valor_extenso=valor_extenso,
            invoice_url=invoice_url, template_id=template_id,
            signers_extra=signers_extra, base_url=base_url,
        )
```

**Atenção à indentação:** o primeiro dos dois está dentro do bloco `else:` (cobrança nova) e o segundo está no nível da função (caminho do dedupe). Preserve a indentação de cada um.

**Por que `ator="sistema"`:** `aprovar_e_liberar` carimba `aprovado_por` para auditoria e o default é `"mario"`. Um contrato liberado por política NÃO foi aprovado pelo Mario, e a trilha de auditoria precisa dizer a verdade.

- [ ] **Step 4: Rode para ver passar, depois a suíte inteira**

```bash
cd C:/Users/mario/noviello-funil-saude && uv run pytest tests/unit/test_orquestrador_contrato.py -v
```

Esperado: todos passam, incluindo os que já existiam.

```bash
cd C:/Users/mario/noviello-funil-saude && uv run pytest -q
```

Esperado: zero falhas.

- [ ] **Step 5: Commit**

```bash
cd C:/Users/mario/noviello-funil-saude && git add src/noviello_funil/orquestrador_contrato.py tests/unit/test_orquestrador_contrato.py && git commit -m "feat(contrato): gerar_contrato honra a politica de liberacao por tipo de caso"
```

---

### Task 6: O script de disparo passa a política adiante

> **INCOMPLETO.** Os passos abaixo cobrem só o `args_politica` e a ligação no script. A guarda `--novo-caso` foi acrescentada depois (`95b2d76`) e corrigida em seguida (`d932437`), porque na primeira versão ela bloqueava retentativa de contrato aberto — regressão para todos os tipos de caso.

> **Acrescentado em 27/ago após a revisão da Task 5.** O índice `uq_contrato_aberto` cobre só estados ABERTOS. Assim que um contrato é liberado, ele sai do campo de visão do dedupe — e uma segunda execução para o mesmo CPF e tipo cria **contrato novo, cobrança nova e liberação nova**, medido em teste pelo executor da Task 5. No gate humano isso era improvável (a janela durava até você aprovar); no modo automático a janela é permanente.
>
> A raiz é uma pergunta que só o Mario responde — **o que identifica um caso aéreo distinto?** O mesmo cliente pode ter dois voos e dois contratos legítimos, então nem deduplicar para sempre nem deduplicar só enquanto aberto está certo. Enquanto não houver resposta, o script **não pode** disparar em silêncio sobre um CPF que já tem contrato do mesmo tipo. Isso não decide a pergunta: só impede que ela seja respondida por acidente.

**Files:**
- Modify: `src/noviello_funil/orquestrador_contrato.py` (helper novo)
- Modify: `scripts/gerar_contrato.py`
- Test: `tests/unit/test_orquestrador_contrato.py`

Quem chama o `gerar_contrato` precisa montar os três parâmetros novos a partir do `Settings`. Isso é um helper, não lógica espalhada pelos chamadores.

- [ ] **Step 1: Escreva o teste que falha**

Acrescente ao fim de `tests/unit/test_orquestrador_contrato.py`:

```python
from noviello_funil.orquestrador_contrato import args_politica


class _FakeSettings:
    def __init__(self, politica="", teto=0.0):
        self.contrato_politica_por_tipo = politica
        self.contrato_teto_automatico = teto


def test_args_politica_default_e_gate_humano():
    args = args_politica(_FakeSettings())
    assert args == {"politicas": {}, "teto_automatico": 0.0}


def test_args_politica_le_a_config():
    s = _FakeSettings(politica="aereo_consumidor:automatico", teto=600.0)
    args = args_politica(s)
    assert args["politicas"] == {"aereo_consumidor": AUTOMATICO}
    assert args["teto_automatico"] == 600.0


def test_args_politica_nao_decide_contra_assinatura():
    """A contra-assinatura NÃO sai da config: ela é derivada da lista de
    signatários que vai no documento, dentro do orquestrador. Config e
    documento poderiam divergir, e o freio precisa proteger o fato."""
    args = args_politica(_FakeSettings(politica="aereo_consumidor:automatico"))
    assert "tem_contra_assinante" not in args
```

- [ ] **Step 2: Rode para ver falhar**

```bash
cd C:/Users/mario/noviello-funil-saude && uv run pytest tests/unit/test_orquestrador_contrato.py -k args_politica -v
```

Esperado: `ImportError: cannot import name 'args_politica'`

- [ ] **Step 3: Implemente**

Em `src/noviello_funil/orquestrador_contrato.py`, acrescente logo após `montar_signers_padrao`:

```python
def args_politica(settings: Any) -> dict[str, Any]:
    """Kwargs de política do ``gerar_contrato``, a partir do ``Settings``.

    NÃO devolve ``tem_contra_assinante``: esse fato é derivado dentro do
    ``_finalizar_e_liberar``, da lista de signatários que realmente vai no
    documento. Ler da config seria ler um proxy — config e documento podem
    divergir, e o freio precisa proteger o fato, não a intenção.
    """
    return {
        "politicas": parse_politicas(
            getattr(settings, "contrato_politica_por_tipo", "") or ""
        ),
        "teto_automatico": float(
            getattr(settings, "contrato_teto_automatico", 0.0) or 0.0
        ),
    }
```

O import de `parse_politicas` já foi feito na Task 5.

- [ ] **Step 4: Ligue no script de disparo**

Em `scripts/gerar_contrato.py`, troque o import do orquestrador por:

```python
from noviello_funil.orquestrador_contrato import (
    args_politica,
    gerar_contrato,
    montar_signers_padrao,
)
```

E na chamada dentro de `_run`, acrescente o desempacotamento como último argumento (depois de `base_url=settings.funil_base_url,`):

```python
            base_url=settings.funil_base_url,
            **args_politica(settings),
        )
```

Atualize também a docstring do módulo, que hoje promete o gate humano de forma incondicional. Troque a frase:

```
real e libera; NADA vai pro cliente até a sua aprovação.
```

por:

```
real e libera; NADA vai pro cliente até a sua aprovação — EXCETO nos tipos
de caso marcados como automáticos em CONTRATO_POLITICA_POR_TIPO, em que a
assinatura é liberada na hora (ver politica_contrato.py).
```

- [ ] **Step 5: Rode os testes e a suíte**

```bash
cd C:/Users/mario/noviello-funil-saude && uv run pytest -q && uv run ruff check src tests
```

Esperado: zero falhas, zero avisos do ruff.

- [ ] **Step 6: Commit**

```bash
cd C:/Users/mario/noviello-funil-saude && git add src/noviello_funil/orquestrador_contrato.py scripts/gerar_contrato.py tests/unit/test_orquestrador_contrato.py && git commit -m "feat(contrato): args_politica derivando a politica do Settings e ligando no script"
```

---

### Task 7: Ensaio a seco antes de ligar em produção

**Files:** nenhum — é verificação. **Executada pelo Mario, não por agente.**

> ### Leia isto antes de rodar qualquer coisa
>
> **A ZapSign não tem sandbox.** Só o Asaas tem. O `zapsign_base_url` aponta para produção sempre. Cada passo abaixo cria um **documento ZapSign real**, consome cota real, e o Step 4 dispara um **e-mail de assinatura real**. O "comece no SANDBOX" da docstring do script cobre só o lado do dinheiro.
>
> **Use um CPF de teste diferente em cada etapa.** A guarda de duplicata bloqueia um segundo contrato vivo para o mesmo CPF + tipo de caso. Reaproveitar o CPF do Step 1 no Step 4 não produz o resultado esperado — produz `doc_preexistente`.
>
> **Nunca use `--novo-caso` para destravar.** Ele existe para "é outro voo, outro caso". Num contrato já liberado, ele cria um **segundo contrato e uma segunda cobrança real** — e, com a política ligada, libera a segunda assinatura ao cliente.
>
> **Confira que `CONTRATO_ESCRITORIO_EMAIL` é o e-mail de um advogado.** O freio só verifica que *alguém* ocupa o `order_group 2`. Essa variável estar correta é o fundamento regulatório inteiro da feature, e nenhum código consegue verificar isso por você.

- [ ] **Step 1: Gere um contrato aéreo com a política DESLIGADA**

Monte `caso_aereo.json` com dados de teste (**CPF de teste nº 1**, e-mail seu), `"tipo_caso": "aereo_consumidor"`, `"valor_honorarios": 500.0`, `"valor_extenso": "quinhentos reais"`. Com `CONTRATO_POLITICA_POR_TIPO` vazio no `.env` e `ASAAS_BASE_URL` apontando ao sandbox:

```bash
cd C:/Users/mario/noviello-funil-saude && uv run python scripts/gerar_contrato.py caso_aereo.json
```

Esperado: `status: pendente_revisao`.

- [ ] **Step 2: Leia o PDF inteiro**

Abra o `sign_url`. **Leia o contrato do começo ao fim.** Com a política ligada não haverá PDF para revisar antes de o cliente ver — esta é literalmente a última leitura humana da minuta aérea.

Confira contra `Ações 2026\_Geral\Contrato - Aereo.docx`: as Cláusulas 1ª e 4ª saíram com o texto certo, `R$ 500,00` e `quinhentos reais` nos lugares certos, e **nenhum `{{...}}` sobrou** no documento.

Confira também que o escritório aparece como signatário. Se não aparecer, pare: `CONTRATO_ESCRITORIO_EMAIL` está vazio, e o modo automático não vai liberar (corretamente).

- [ ] **Step 3: Reprove este contrato**

Use o link de aprovação para **reprovar**. Isso libera o CPF de teste nº 1 e mantém o sandbox limpo. Confira que a cobrança Asaas foi cancelada.

- [ ] **Step 4: Ligue a política e gere um contrato NOVO**

No `.env`:

```
CONTRATO_POLITICA_POR_TIPO=aereo_consumidor:automatico
CONTRATO_TETO_AUTOMATICO=600
```

Monte `caso_aereo_2.json` com **CPF de teste nº 2** — diferente do Step 1. Não reutilize o arquivo anterior.

```bash
cd C:/Users/mario/noviello-funil-saude && uv run python scripts/gerar_contrato.py caso_aereo_2.json
```

Esperado: `status: liberado_automatico`, `motivo_liberacao: politica_automatica`, e o e-mail de assinatura chegando na caixa de teste **sem você ter aprovado nada**.

Se vier `pendente_revisao` com `motivo_liberacao: doc_preexistente`, você reutilizou o CPF — recomece com um terceiro.
Se vier `sem_contra_assinante`, o `CONTRATO_ESCRITORIO_EMAIL` está vazio.

- [ ] **Step 5: Confirme que o teto morde**

Gere um terceiro caso, **CPF de teste nº 3**, com `"valor_honorarios": 5000.0` (o erro de digitação que o teto existe para pegar).

Esperado: `status: pendente_revisao`, `motivo_liberacao: acima_do_teto`, nenhum e-mail ao cliente.

- [ ] **Step 6: Confirme que os outros tipos não mudaram**

Rode um caso `urbanistico_iptu_regularizacao`, com a política aérea ainda ligada.

Esperado: `status: pendente_revisao`, `motivo_liberacao: politica_humana`, nenhum e-mail ao cliente. É a prova de que ligar o aéreo não mexeu em mais nada.

- [ ] **Step 7: Decida sobre o docx desatualizado**

`Ações 2026\_Geral\Contrato - Aereo.docx` ainda diz **R$ 1.000,00** na Cláusula 4ª §1. O sistema emite **R$ 500,00**. Enquanto os dois existirem, quem abrir o arquivo procurando o valor vigente acha o errado.

---

## Ao final desta fase

Um contrato aéreo é gerado, cobrado e **liberado ao cliente sem intervenção**, com o escritório contra-assinando. Todo outro tipo de caso segue idêntico ao de hoje. O disparo ainda é você no terminal — por isso não há aviso no WhatsApp nesta fase.

**Ainda falta para "o aéreo roda sozinho de ponta a ponta":**

- **Fase 2 — Coleta:** o modelo classifica `tipo_caso` (enum vindo de `TIPOS_CASO`), e um módulo novo conduz a coleta conversacional dos 16 campos que o template exige (`nome_completo`, `nacionalidade`, `estado_civil`, `profissao`, `rg`, `orgao_emissor`, `cpf`, `logradouro`, `numero`, `complemento`, `bairro`, `cidade`, `uf`, `cep`, `celular`, `email`), com validação de dígito verificador de CPF e eco de confirmação.
- **Fase 3 — Gatilho:** ao confirmar os dados, a conversa chama o `gerar_contrato` com o valor fixo do tipo de caso (R$ 500,00 no aéreo, vindo de `HONORARIOS_PADRAO` — a IA nunca precifica, invariante I7). Inclui o gate de viabilidade aérea (prescrição — 5 anos no CDC contra 2 anos na Convenção de Montreal para voo internacional, calculado no código a partir da data do voo, nunca pelo modelo) e o **aviso ao Mario no WhatsApp** a cada liberação automática, que é onde ele passa a fazer sentido: na Fase 1 quem dispara é você no terminal e já vê o resultado na tela.

---

## Lições — o que a revisão adversarial pegou e o plano não

Cinco defeitos chegaram ao código. **Nenhum foi erro de execução:** os agentes transcreveram fielmente o que a especificação mandava. Os cinco estavam na especificação, escrita e revisada três vezes antes de qualquer código.

1. **Ler o proxy em vez do fato.** `tem_contra_assinante` vinha de `contrato_escritorio_email` na config. Quem assina de verdade é a lista `signers_extra` do documento. As duas podem divergir, e o freio protegeria a intenção.
2. **O mesmo erro, um nível mais fundo.** Corrigido para ler a lista, ainda estava errado: a lista só *é* o documento quando o documento é criado naquela chamada. Num documento que já existia, ela volta a ser intenção. O caminho real — operador esquece o e-mail, preenche, roda de novo — liberaria contrato sem contra-assinatura.
3. **Condensar texto jurídico.** A extração do docx virou a definição aberta de proveito econômico numa lista fechada de quatro itens, estreitando a base de honorários do escritório. Ninguém decidiu isso.
4. **Afirmar no presente o que só vale depois.** Duas vezes: um comentário dizendo que o orquestrador já lia a política, e outro dizendo que a trilha de auditoria prova aprovação humana.
5. **Guarda larga demais.** A trava de duplicata bloqueava retentativa de contrato aberto, para todos os tipos de caso — regressão criada ao corrigir outro problema.

O padrão: **os quatro primeiros são a mesma classe de erro.** Uma afirmação sobre o mundo que era verdadeira no caso que eu tinha em mente e falsa no caso que eu não tinha. Só apareceram porque alguém leu o código sem confiar na descrição dele.

Duas coisas que funcionaram e valem repetir:

- **Fazer o revisor conferir contra a fonte, não contra a minha descrição.** O erro no texto jurídico só apareceu porque o revisor abriu o docx.
- **Executor que se recusa a fazer teste passar.** Um teste meu assumia que a segunda chamada cairia num curto-circuito. Não caía — criava segundo contrato e segunda cobrança. Forçar o teste a passar teria certificado esse caminho como seguro.

Uma coisa que não funcionou: **rodar git enquanto um subagente trabalha no mesmo repositório.** Um commit meu engoliu o stage de um agente.

## Pendente de decisão do Mario

**O que identifica um caso aéreo distinto?** A chave de negócio é CPF + tipo de caso, mas o mesmo cliente pode ter dois voos e dois contratos legítimos. Depois que um contrato é liberado ele sai do índice `uq_contrato_aberto`, e uma segunda execução cria contrato novo e **cobrança nova** — medido, não suposto.

Na Fase 1 isso é contido pela guarda `--novo-caso` no script, porque quem dispara é humano. **Na Fase 3, com o bot disparando, vira obrigatório ter resposta.** Decidir antes de ligar o gatilho conversacional, não depois.
