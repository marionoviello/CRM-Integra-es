"""Redação do contrato por IA + lint OAB (roadmap 3.x, caminho B).

No caminho B (decisão 15/jun) a IA REDIGE/adapta o texto do contrato (partindo
dos modelos do Mario) e o Mario aprova o TEXTO INTEGRAL antes de assinar.

Este módulo é a REDE DE PROTEÇÃO entre a redação e a aprovação. ``lint_contrato``
roda 17 checagens no texto gerado:
  - 9 BLOQUEIOS (B1–B9): travam o botão de aprovação — o texto não vai pra
    assinatura (promessa de êxito, cláusula penal nula, honorário divergente do
    que o Mario digitou, OAB ausente, quota litis abusiva, gratuidade enganosa,
    conflito vazado no texto…).
  - 8 ALERTAS (A1–A7 + A_valor_extra): avisam o Mario mas NÃO travam
    (placeholder esquecido, foro/data ausente, CPF cravado…).

É defesa-em-profundidade — NÃO substitui a leitura integral do advogado, mas
pega o que o olho pode deixar passar. Regras puras e testáveis. ``redigir_contrato``
(Claude) e ``render_pdf`` (WeasyPrint) entram num próximo incremento.

A trava de honorários (B4) é o coração: a IA pode ter alucinado um número. A
regra exige que o valor que o Mario digitou apareça na cláusula de honorários;
valores extras na cláusula viram ALERTA (parcela legítima vs valor inventado —
o Mario confere), em vez de bloquear cego.
"""

import contextlib
import re
from dataclasses import dataclass

# --- UFs reais (para o B5 não casar "Provimento OAB/CF") ----------------------
_UFS = (
    "AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MT|MS|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|"
    "RR|SC|SP|SE|TO"
)

# --- Regex das regras (IGNORECASE; \w casa acento no Python 3) ----------------

# B1: promessa/garantia de resultado.
#  - gatilhos ampliados (comprometer/convicção/indubitável/dado como certo…),
#    mas "garant\w+" exclui institutos processuais (garantia do juízo/real/…)
#    e "certeza" só dispara ligado a êxito/vitória/ganho/sucesso;
#  - alvos ampliados (deferimento/condenação/restituição/devolução/reforma da
#    sentença/absolvição/provimento/demanda/ação/causa/recurso). NÃO inclui
#    "benefício" (plano de saúde legítimo).
_RX_B1A = re.compile(
    r"\b("
    r"garant\w+(?!\s+(?:d[oa]\s+ju[íi]zo|real|hipotec\w+|fiduci\w+|pignorat\w+"
    r"|locat[íi]ci\w+|banc\w+))"
    r"|assegur\w+|compromet\w+|convic[çc]\w+|indubit\w+|inquestion\w+"
    r"|incontest\w+|dado[s]?\s+como\s+cert\w+|com\s+toda\s+(?:a\s+)?certeza"
    r")\b.{0,40}?\b("
    r"[êe]xito|resultad\w+|ganho|vit[óo]ria|proced[êe]ncia|aprova\w+"
    r"|defer\w+|condena[çc]\w+|restitui[çc]\w+|devolu[çc]\w+"
    r"|reforma\s+da\s+senten[çc]a|absolvi[çc]\w+|provid[êe]ncia|provimento"
    r"|demanda|a[çc][ãa]o|causa|recurso"
    r")"
    r"|\bcerteza\s+(?:d[eoa]s?\s+|de\s+)?([êe]xito|vit[óo]ria|ganh\w+|sucesso)\b",
    re.IGNORECASE | re.DOTALL,
)
_RX_B1B = re.compile(
    r"\b([êe]xito|resultad\w+|vit[óo]ria|proced[êe]ncia|provimento|ganho"
    r"|deferimento|condena[çc][ãa]o)\b.{0,40}?\b(garant\w+|assegurad\w+"
    r"|cert[ao]\w*|dado[s]?\s+como\s+cert\w+)",
    re.IGNORECASE | re.DOTALL,
)
# Guardas de falso-positivo para B1A (cláusulas lícitas que não devem bloquear).
_RX_B1_GUARDA_SIGILO = re.compile(
    r"\b(sigilo|confidencialidad\w+|tratativ\w+|negocia\w+)\b", re.IGNORECASE,
)
_RX_B1_GUARDA_GANHO_COND = re.compile(
    r"(?:em\s+caso\s+de|na\s+hip[óo]tese\s+de|havendo|se\s+houver|caso\s+haja)"
    r"\s+ganho",
    re.IGNORECASE,
)
_RX_B1_GUARDA_APROVA_CLIENTE = re.compile(
    r"aprova\w*\s+(?:d[oa]|pel[oa])\s+(?:cliente|contratante|outorgante|acordo)",
    re.IGNORECASE,
)
_RX_B1_GUARDA_QUALIDADE = re.compile(
    r"garant\w+\s+(?:a\s+)?qualidade", re.IGNORECASE,
)

_RX_B2A = re.compile(
    r"(100\s*%|cem\s+por\s+cento).{0,20}?([êe]xito|aprova|sucesso)",
    re.IGNORECASE | re.DOTALL,
)
_RX_B2B = re.compile(
    r"\b(ganh\w+|venc\w+)\b.{0,10}?\bou\b.{0,12}?\bn[ãa]o\s+(?:se\s+)?pag\w+\b",
    re.IGNORECASE | re.DOTALL,
)
# "só cobro/pago se/quando/em caso de ganhar/êxito/vencer".
_RX_B2C = re.compile(
    r"\bs[óo]\s+(?:cobr\w+|pag\w+)\b.{0,20}?\b(?:se|quando|caso|em\s+caso)\b"
    r".{0,15}?(?:ganh\w+|[êe]xito|vencer|vit[óo]ria|sucesso|aprov\w+)",
    re.IGNORECASE | re.DOTALL,
)
# "perdeu/sem ganho → nada paga/não paga/sem honorários/isento".
_RX_B2D = re.compile(
    r"\b(?:perd\w+|sem\s+(?:ganho|[êe]xito|vit[óo]ria))\b.{0,25}?"
    r"(?:nada\s+pag\w+|n[ãa]o\s+(?:se\s+)?pag\w+|sem\s+honor\w+|isent\w+)",
    re.IGNORECASE | re.DOTALL,
)

# B3: cláusula penal por revogar/renunciar mandato — independente de ordem.
#  Bloqueia quando penalidade E ato-de-revogação co-ocorrem numa janela;
#  guarda de negação ("não haverá multa em caso de revogação") suprime.
_RX_B3_PENAL = re.compile(
    r"\b(multa|cl[áa]usula\s+penal|penalidade|indeniza\w+|rescis[óo]ri\w+)\b",
    re.IGNORECASE,
)
_RX_B3_REVOG = re.compile(
    r"\b(revoga\w+|destitui\w+|ren[úu]nci\w+|dispensa|substitui[çc][ãa]o"
    r"|distrato|res(?:c|s)is\w+\s+unilateral"
    r"|constitu\w+\s+(?:outro|nov\w+)\s+(?:advogad\w+|patrono)"
    r"|troca\s+de\s+patrono|mandant\w*)\b",
    re.IGNORECASE,
)
# Negação imediatamente antes da penalidade → cláusula lícita.
_RX_B3_NEGACAO = re.compile(
    r"(n[ãa]o\s+haver[áa]|n[ãa]o\s+ser[áa]\s+devid\w+|sem|isent\w+\s+de"
    r"|nenhum\w*|livre\s+de)\s*$",
    re.IGNORECASE,
)

# B5: OAB tolerante (UF antes/depois, hífen, "sob o nº", forma por extenso),
#  restrita a UFs reais e excluindo contexto normativo Provimento/Resolução/Lei.
_RX_OAB = re.compile(
    rf"\b(?:OAB|ordem\s+dos\s+advogados\s+do\s+brasil)\b"
    rf"(?:[\s\S]{{0,60}}?(?:{_UFS})\b[\s\S]{{0,12}}?\d{{3,6}}"
    rf"|[\s\S]{{0,40}}?\d{{1,3}}(?:\.\d{{3}})?[\s\S]{{0,6}}?(?:{_UFS})\b)",
    re.IGNORECASE,
)
# Contexto normativo: "Provimento/Resolução/Código/Estatuto/Lei ... OAB".
_RX_OAB_NORMATIVO = re.compile(
    r"(provimento|resolu[çc][ãa]o|c[óo]digo|estatuto|lei)\s+(?:n?[ºo.]?\s*\d*\s*)?"
    r"(?:da\s+)?OAB",
    re.IGNORECASE,
)

# _RX_HONORARIOS ampliado: honorária/verba honorária/verba advocatícia/
#  remuneração/contraprestação/pro labore (conserta B4 e B6 de uma vez).
_RX_HONORARIOS = re.compile(
    r"(honor[áa]ri\w+|verba\s+(?:honor[áa]ri\w+|advocat[íi]ci\w+)"
    r"|remunera[çc][ãa]o|contrapresta[çc][ãa]o|pro\s*-?\s*labore)",
    re.IGNORECASE,
)
_RX_FORMA_PGTO = re.compile(
    r"\b([àa]\s+vista|parcel\w+|presta[çc]\w+|por\s+cento|[êe]xito"
    r"|\d+\s*(?:vezes|x|parcelas)|uma\s+s[óo]\s+vez|no\s+ato"
    r"|mediante\s+(?:dep[óo]sito|boleto|transfer[êe]ncia|pix|cheque)"
    r"|quita[çc][ãa]o|moeda\s+corrente)\b|%|\bpix\b",
    re.IGNORECASE,
)
# B8 (quota litis >50%) — percentual em "%" ou "por cento", janela ampla,
#  independente de ordem.
_RX_B8_PCT = re.compile(
    r"\b(quota\s*litis|[êe]xit\w+)\b[^.]{0,120}?(\d{2,3})\s*(?:%|por\s*cento|porcento)",
    re.IGNORECASE | re.DOTALL,
)
# Percentual por extenso (cinquenta/sessenta/…) ligado a quota litis/êxito.
_RX_B8_PCT_EXTENSO = re.compile(
    r"\b(quota\s*litis|[êe]xit\w+)\b[^.]{0,120}?"
    r"\b(cinquenta|sessenta|setenta|oitenta|noventa|cem)\s+por\s+cento",
    re.IGNORECASE | re.DOTALL,
)
# Frações > 50% (dois terços, três quartos/quintos, quatro quintos…).
_RX_B8_FRACAO = re.compile(
    r"\b(quota\s*litis|[êe]xit\w+)\b[^.]{0,120}?"
    r"\b(dois\s+ter[çc]os|tr[êe]s\s+quart\w+|tr[êe]s\s+quint\w+"
    r"|quatro\s+quint\w+|maior\s+parte|metade\s+ou\s+mais)",
    re.IGNORECASE | re.DOTALL,
)
_EXTENSO_PCT = {
    "cinquenta": 50, "sessenta": 60, "setenta": 70,
    "oitenta": 80, "noventa": 90, "cem": 100,
}
# B8 cessão de crédito — co-ocorrência sem ordem.
_RX_B8_CESSAO_VERBO = re.compile(r"\b(ced\w+|cess[ãa]o|transfer\w+)\b", re.IGNORECASE)
_RX_B8_CESSAO_CRED = re.compile(
    r"\b(cr[ée]dito\w*|direitos\s+credit[óo]ri\w+|proveito)\b", re.IGNORECASE,
)
_RX_B8_CESSAO_CLIENTE = re.compile(
    r"\b(cliente|contratante|mandante|outorgante)\b", re.IGNORECASE,
)
# B9: gratuidade enganosa, com lookbehind p/ NÃO pegar "justiça gratuita".
_RX_B9 = re.compile(
    r"\b(gr[áa]tis|(?<!justi[çc]a\s)gratuit\w+"
    r"|sem\s+(?:nenhum|qualquer)\s+(?:custo|[ôo]nus|despesa|encargo)"
    r"|sem\s+custo\s+algum"
    r"|n[ãa]o\s+(?:paga|pagar[áa]|desembolsar[áa])\s+"
    r"(?:nada|valor\s+algum|qualquer)"
    r"|isent\w+\s+de\s+(?:honor|pagamento|custas))",
    re.IGNORECASE,
)
# A1: placeholders ({{}}, [], <<>>, «», #X#, pontilhado, ____ , XXXX).
_RX_A1 = re.compile(
    r"\{\{.*?\}\}|\[[^\]\n]{2,}\]|<<[^>\n]+>>|«[^»\n]+»|#[A-Z_]{2,}#"
    r"|_{4,}|X{4,}|\.{6,}",
)
_RX_A2_FORO = re.compile(r"\b(comarca|foro)\b", re.IGNORECASE)
_RX_A2_DATA = re.compile(
    r"\b\d{2}/\d{2}/\d{4}\b|\bde\s+(19|20)\d{2}\b", re.IGNORECASE,
)
_RX_A3 = re.compile(
    r"\b(custas|despesas|dilig[êe]ncia|reembolso)\b", re.IGNORECASE,
)
# A4: só alerta se "jurisprudência ... garante ... êxito/vitória" (não "direito").
_RX_A4 = re.compile(
    r"\b(jurisprud\w+|STJ|STF|TJ[A-Z]{0,2})\b.{0,30}?\b(garant\w+|assegur\w+"
    r"|sempre\s+ganha|pac[íi]fic\w+.{0,15}favor)\b.{0,40}?"
    r"\b([êe]xito|vit[óo]ria|ganho|proced[êe]ncia|favor[áa]ve\w+)\b",
    re.IGNORECASE | re.DOTALL,
)
_RX_CPF = re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")
_RX_A6_PARCELA = re.compile(r"\bparcel\w+|presta[çc][õo]es", re.IGNORECASE)
_RX_A6_INDICE = re.compile(
    r"\b([íi]ndice|IPCA|IGP-?M|corre[çc][ãa]o|juros)\b", re.IGNORECASE,
)
# A7: bidirecional ("elegem ... foro" / "foro ... eleição"), cobre flexões.
_RX_A7 = re.compile(
    r"\bforo\s+de\s+elei[çc][ãa]o\b|\beleg\w+\b.{0,40}?\bforo\b"
    r"|\bforo\b.{0,40}?\beleg\w+\b",
    re.IGNORECASE | re.DOTALL,
)
# Valor em R$, com sufixo multiplicador opcional (mil/milhão/k) e cents 1-2 díg.
_RX_DINHEIRO = re.compile(
    r"R\$\s?(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:,\d{1,2})?)"
    r"(?:\s*(mil|milh[õo]es|milh[ãa]o|mi|k))?",
    re.IGNORECASE,
)
# Decimal americano ambíguo: "R$ 5.000.00" (ponto onde se esperava vírgula).
_RX_DECIMAL_AMBIGUO = re.compile(r"R\$\s?\d{1,3}(?:\.\d{3})+\.\d{2}\b")
# Percentual genérico (pro caminho percentual do B4).
_RX_PCT = re.compile(r"(\d{1,3}(?:,\d+)?)\s*%")
# Marcadores de OUTROS papéis de valor (não-honorário) — filtram A_valor_extra.
_RX_NAO_HONORARIO = re.compile(
    r"\b(im[óo]vel|imovel|valor\s+da\s+causa|custas|despesas|bem\s+avaliad\w+"
    r"|avaliad\w+\s+em)\b",
    re.IGNORECASE,
)
# Número por extenso no input do Mario (para detectar "tem valor mas sem cifra").
_RX_EXTENSO_VALOR = re.compile(
    r"\b(um|dois|duas|tr[êe]s|quatro|cinco|seis|sete|oito|nove|dez|onze|doze"
    r"|vinte|trinta|quarenta|cinquenta|sessenta|setenta|oitenta|noventa|cem"
    r"|cento|mil|milh[ãa]o|milh[õo]es)\b",
    re.IGNORECASE,
)
_SUFIXO_FATOR = {"mil": 1_000, "k": 1_000, "mi": 1_000_000}


@dataclass
class Achado:
    """Um achado do lint. ``severidade`` ∈ {'bloqueia', 'alerta'}."""
    regra: str
    severidade: str
    descricao: str
    trecho: str = ""


def _fator_sufixo(suf: str | None) -> int:
    """Fator multiplicador de um sufixo (mil/milhão/k); 1 se ausente."""
    if not suf:
        return 1
    s = suf.lower()
    if s.startswith("milh"):
        return 1_000_000
    return _SUFIXO_FATOR.get(s, 1)


def _valores_centavos(s: str | None) -> set[int]:
    """Todos os valores R$ de um texto, normalizados a centavos (int).

    Aplica sufixo multiplicador (R$ 5 mil → 500000 centavos).
    """
    out: set[int] = set()
    for m in _RX_DINHEIRO.finditer(s or ""):
        raw = m.group(1).replace(".", "").replace(",", ".")
        fator = _fator_sufixo(m.group(2))
        with contextlib.suppress(ValueError):
            out.add(round(float(raw) * fator * 100))
    return out


def _parse_valor_digitado(valor: str | None) -> set[int]:
    """Parse tolerante do valor que o Mario DIGITOU (não o texto da IA).

    Aceita com/sem "R$", "5000"/"5.000"/"5.000,00", sufixo "5 mil"/"10k".
    Retorna conjunto de centavos. Vazio se não houver número parseável.
    """
    if not valor:
        return set()
    out: set[int] = set()
    # Remove tokens percentuais ('20%') ANTES de varrer valores R$ — percentual
    # é tratado à parte no caminho do B4 e não deve virar um valor monetário.
    sem_pct = _RX_PCT.sub(" ", valor)
    # número (com milhar/decimal) + sufixo opcional, COM ou SEM "R$". A 1ª
    # alternativa EXIGE separador de milhar real ('5.000'); '5000' cai na 2ª
    # (\d+) e não vira '500' + '0'.
    rx = re.compile(
        r"(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:,\d{1,2})?)"
        r"(?:\s*(mil|milh[õo]es|milh[ãa]o|mi|k))?",
        re.IGNORECASE,
    )
    for m in rx.finditer(sem_pct):
        raw = m.group(1).replace(".", "").replace(",", ".")
        fator = _fator_sufixo(m.group(2))
        with contextlib.suppress(ValueError):
            out.add(round(float(raw) * fator * 100))
    return out


def _tem_indicio_valor(valor: str | None) -> bool:
    """True se o input do Mario tem dígito OU número por extenso OU %.

    Distingue 'a combinar' (sem indício → não checa B4) de um valor que
    deveria ter sido parseado mas falhou (→ avisar, nunca desligar B4 mudo).
    """
    if not valor:
        return False
    if re.search(r"\d", valor):
        return True
    return bool(_RX_EXTENSO_VALOR.search(valor))


def _trecho(texto: str, m: re.Match) -> str:
    """Excerto curto ao redor do match (pro Mario ver no alerta)."""
    ini = max(0, m.start() - 15)
    return re.sub(r"\s+", " ", texto[ini : m.end() + 15]).strip()[:120]


def _valores_janela_honorarios(texto: str) -> list[int]:
    """Valores R$ (com repetição) nas janelas BIDIRECIONAIS ao redor de cada
    'honorários', excluindo os ancorados a outros papéis (imóvel/causa/custas).

    Retorna LISTA (não set) para que a soma de parcelas iguais (entrada R$5k +
    saldo R$5k = R$10k) feche; valores de janelas sobrepostas são deduplicados
    por posição no texto.
    """
    vistos: set[int] = set()  # posições já contadas (evita dupla contagem)
    out: list[int] = []
    for m in _RX_HONORARIOS.finditer(texto):
        ini = max(0, m.start() - 140)
        for vm in _RX_DINHEIRO.finditer(texto, ini, m.start() + 200):
            if vm.start() in vistos:
                continue
            antes = texto[max(0, vm.start() - 25) : vm.start()]
            if _RX_NAO_HONORARIO.search(antes):
                continue
            raw = vm.group(1).replace(".", "").replace(",", ".")
            fator = _fator_sufixo(vm.group(2))
            with contextlib.suppress(ValueError):
                vistos.add(vm.start())
                out.append(round(float(raw) * fator * 100))
    return out


def _checar_honorarios(texto: str, valor_honorarios: str) -> list[Achado]:
    """B4 + A_valor_extra: o valor digitado pelo Mario tem que aparecer na
    cláusula de honorários; valores extras na cláusula viram ALERTA."""
    achados: list[Achado] = []

    digitados = _parse_valor_digitado(valor_honorarios)
    pcts_digitados = {
        m.group(1).replace(",", ".") for m in _RX_PCT.finditer(valor_honorarios or "")
    }

    if not digitados and not pcts_digitados:
        # Sem número parseável. Distinguir 'a combinar' (sem indício → não checa)
        # de 'tinha indício mas o parse falhou' (→ avisar, B4 nunca mudo).
        if _tem_indicio_valor(valor_honorarios):
            achados.append(Achado(
                "B4", "bloqueia",
                "não foi possível extrair o valor numérico do honorário digitado "
                f"('{valor_honorarios}') — B4 não pôde validar; confira o número",
            ))
        return achados

    # Decimal americano ambíguo na cláusula ("R$ 5.000.00") → bloqueia.
    if _RX_DECIMAL_AMBIGUO.search(texto):
        achados.append(Achado(
            "B4", "bloqueia",
            "formato monetário ambíguo (ponto onde se esperava vírgula decimal) "
            "na cláusula de honorários — confira manualmente",
        ))

    valores_em_janela = _valores_janela_honorarios(texto)
    set_janela = set(valores_em_janela)

    # --- caminho R$ ---
    if digitados:
        soma_janela = sum(valores_em_janela)
        digitado_total = max(digitados) if len(digitados) == 1 else None
        parcelado_bate = (
            digitado_total is not None
            and bool(valores_em_janela)
            and soma_janela == digitado_total
        )
        if not (digitados & set_janela) and not parcelado_bate:
            if digitados & _valores_centavos(texto):
                achados.append(Achado(
                    "B4", "bloqueia",
                    "valor de honorários digitado não aparece na cláusula de "
                    "honorários (aparece solto no texto)",
                ))
            else:
                achados.append(Achado(
                    "B4", "bloqueia",
                    "valor de honorários digitado pelo Mario NÃO aparece no texto "
                    "(a IA pode ter trocado o número)",
                ))

    # --- caminho percentual (honorário de êxito puro) ---
    if pcts_digitados:
        pcts_em_janela: set[str] = set()
        for m in _RX_HONORARIOS.finditer(texto):
            ini = max(0, m.start() - 140)
            janela = texto[ini : m.start() + 200]
            pcts_em_janela |= {
                pm.group(1).replace(",", ".") for pm in _RX_PCT.finditer(janela)
            }
        if not (pcts_digitados & pcts_em_janela):
            achados.append(Achado(
                "B4", "bloqueia",
                "percentual de honorários digitado pelo Mario NÃO aparece/diverge "
                "na cláusula (a IA pode ter trocado o número)",
            ))

    # valores na cláusula que não são o digitado → alerta (parcela? inventado?)
    if digitados:
        extras = set_janela - digitados
        if extras:
            achados.append(Achado(
                "A_valor_extra", "alerta",
                "há valor(es) na cláusula de honorários diferentes do que você "
                "digitou — confira se é parcela ou número errado",
            ))
    return achados


def _oab_pessoa(texto: str) -> bool:
    """True se há uma inscrição OAB de PESSOA fora de contexto normativo.

    Mascara os trechos 'Provimento/Resolução/... OAB' e procura uma inscrição
    real (OAB/UF nº) no restante. Evita que a citação de um Provimento OAB/CF
    sirva de qualificação do advogado.
    """
    limpo = _RX_OAB_NORMATIVO.sub(" ", texto)
    return bool(_RX_OAB.search(limpo))


def _b1_dispara(texto: str) -> re.Match | None:
    """B1 com guardas de falso-positivo (sigilo, ganho condicional, aprovação
    pelo cliente, garantia de qualidade)."""
    for m in _RX_B1A.finditer(texto):
        trecho = m.group(0)
        if _RX_B1_GUARDA_SIGILO.search(trecho):
            continue
        if _RX_B1_GUARDA_GANHO_COND.search(trecho):
            continue
        if _RX_B1_GUARDA_APROVA_CLIENTE.search(trecho):
            continue
        if _RX_B1_GUARDA_QUALIDADE.search(trecho):
            continue
        return m
    return _RX_B1B.search(texto)


def contem_promessa_resultado(texto: object) -> bool:
    """E3 (auditoria 24/jun): ``True`` se o texto promete/garante resultado (B1)
    ou traz slogan de êxito (B2A-D) — a parte OAB (Prov. 205/2021) das regras de
    contrato, reaproveitada como backstop nas mensagens AO LEAD.

    Reusa ``_b1_dispara`` (com as guardas de falso-positivo: sigilo, ganho
    condicional, aprovação pelo cliente, garantia de qualidade) + B2A-D. As
    regexes já cobrem com/sem acento, então não precisa normalizar antes.
    """
    if not texto or not isinstance(texto, str):
        return False
    if _b1_dispara(texto):
        return True
    return any(rx.search(texto) for rx in (_RX_B2A, _RX_B2B, _RX_B2C, _RX_B2D))


def lint_contrato(
    texto: str,
    *,
    valor_honorarios: str,
    exige_honorarios: bool = True,
    nomes_parte_contraria: list[str] | None = None,
) -> list[Achado]:
    """Roda as 17 regras no texto gerado. Retorna a lista de achados.

    ``exige_honorarios=False`` (ex.: procuração) pula B6/B8/B9.
    ``nomes_parte_contraria`` (do índice de conflito) alimenta B7 — defesa em
    profundidade no nível do texto, além do gate de conflito do fluxo.
    """
    texto = texto or ""
    achados: list[Achado] = []

    # --- BLOQUEIOS ---
    m = _b1_dispara(texto)
    if m:
        achados.append(Achado(
            "B1", "bloqueia", "promessa/garantia de resultado", _trecho(texto, m),
        ))
    for rx in (_RX_B2A, _RX_B2B, _RX_B2C, _RX_B2D):
        m = rx.search(texto)
        if m:
            achados.append(Achado(
                "B2", "bloqueia", "slogan de êxito ('100%'/'ganha ou não paga')",
                _trecho(texto, m),
            ))
            break

    # B3: penalidade E ato-de-revogação numa janela, qualquer ordem; guarda de
    #  negação ("não haverá multa em caso de revogação") suprime.
    for mp in _RX_B3_PENAL.finditer(texto):
        antes = texto[max(0, mp.start() - 20) : mp.start()]
        if _RX_B3_NEGACAO.search(antes):
            continue
        ini = max(0, mp.start() - 120)
        janela = texto[ini : mp.end() + 120]
        if _RX_B3_REVOG.search(janela):
            achados.append(Achado(
                "B3", "bloqueia",
                "cláusula penal/multa por revogar ou renunciar o mandato "
                "(nula, STJ)",
                _trecho(texto, mp),
            ))
            break

    achados.extend(_checar_honorarios(texto, valor_honorarios))

    if not _oab_pessoa(texto):
        achados.append(Achado(
            "B5", "bloqueia",
            "falta a qualificação do advogado com número OAB/UF",
        ))

    if exige_honorarios:
        tem_honorarios = bool(_RX_HONORARIOS.search(texto))
        tem_valor_rs = bool(_valores_centavos(texto))
        tem_percentual = bool(re.search(r"\d{1,3}\s*%", texto)) and tem_honorarios
        tem_forma = bool(_RX_FORMA_PGTO.search(texto))
        tem_clausula = tem_honorarios and (tem_valor_rs or tem_percentual) and tem_forma
        if not tem_clausula:
            achados.append(Achado(
                "B6", "bloqueia",
                "falta a cláusula de honorários (valor/percentual + forma de "
                "pagamento)",
            ))
        # B8: quota litis > 50% — %, por extenso ou frações.
        bloqueou_b8_pct = False
        for m in _RX_B8_PCT.finditer(texto):
            if int(m.group(2)) > 50:
                achados.append(Achado(
                    "B8", "bloqueia",
                    f"honorário de êxito/quota litis acima de 50% ({m.group(2)}%)",
                    _trecho(texto, m),
                ))
                bloqueou_b8_pct = True
                break
        if not bloqueou_b8_pct:
            me = _RX_B8_PCT_EXTENSO.search(texto)
            if me and _EXTENSO_PCT[me.group(2).lower()] > 50:
                achados.append(Achado(
                    "B8", "bloqueia",
                    "honorário de êxito/quota litis acima de 50% "
                    f"({me.group(2)} por cento)",
                    _trecho(texto, me),
                ))
                bloqueou_b8_pct = True
        if not bloqueou_b8_pct:
            mf = _RX_B8_FRACAO.search(texto)
            if mf:
                achados.append(Achado(
                    "B8", "bloqueia",
                    "honorário de êxito/quota litis acima de 50% (fração "
                    f"'{mf.group(2)}')",
                    _trecho(texto, mf),
                ))
        # B8 cessão de crédito — co-ocorrência sem ordem.
        if (
            _RX_B8_CESSAO_VERBO.search(texto)
            and _RX_B8_CESSAO_CRED.search(texto)
            and _RX_B8_CESSAO_CLIENTE.search(texto)
        ):
            mc = _RX_B8_CESSAO_VERBO.search(texto)
            achados.append(Achado(
                "B8", "bloqueia",
                "cessão de crédito/direitos do cliente ao advogado",
                _trecho(texto, mc),
            ))
        m9 = _RX_B9.search(texto)
        if m9:
            achados.append(Achado(
                "B9", "bloqueia",
                "promessa de gratuidade em contrato de honorários",
                _trecho(texto, m9),
            ))

    for nome in nomes_parte_contraria or []:
        if nome and re.search(rf"\b{re.escape(nome)}\b", texto, re.IGNORECASE):
            achados.append(Achado(
                "B7", "bloqueia",
                f"nome de parte contrária de cliente aparece no texto: {nome}",
            ))
            break

    # --- ALERTAS ---
    ma = _RX_A1.search(texto)
    if ma:
        achados.append(Achado(
            "A1", "alerta", "placeholder não substituído", _trecho(texto, ma),
        ))
    if not (_RX_A2_FORO.search(texto) and _RX_A2_DATA.search(texto)):
        achados.append(Achado("A2", "alerta", "foro/comarca ou data ausente"))
    if not _RX_A3.search(texto):
        achados.append(Achado(
            "A3", "alerta", "reembolso de custas/despesas não explicitado",
        ))
    m4 = _RX_A4.search(texto)
    if m4:
        achados.append(Achado(
            "A4", "alerta", "tom de 'jurisprudência garantida' (revise)",
            _trecho(texto, m4),
        ))
    m5 = _RX_CPF.search(texto)
    if m5:
        achados.append(Achado(
            "A5", "alerta", "CPF cravado no texto (confira LGPD/merge)",
            _trecho(texto, m5),
        ))
    if _RX_A6_PARCELA.search(texto) and not _RX_A6_INDICE.search(texto):
        achados.append(Achado(
            "A6", "alerta", "parcelamento sem índice de reajuste",
        ))
    m7 = _RX_A7.search(texto)
    if m7:
        achados.append(Achado(
            "A7", "alerta", "foro de eleição (cuidado se cliente PF — CDC)",
            _trecho(texto, m7),
        ))

    return achados


def lint_ok(achados: list[Achado]) -> bool:
    """True se NÃO há bloqueios (libera o botão de aprovação)."""
    return not any(a.severidade == "bloqueia" for a in achados)
