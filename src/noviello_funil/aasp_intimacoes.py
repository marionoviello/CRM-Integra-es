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

MAX_ITENS = 12                # cap de itens detalhados na mensagem
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
