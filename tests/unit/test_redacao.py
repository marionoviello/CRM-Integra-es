"""Tests for the contract lint (redacao.lint_contrato, roadmap 3.x caminho B).

Dados fictícios ("Fulano Teste") por regra do CLAUDE.md do projeto.
"""

from noviello_funil.redacao import lint_contrato, lint_ok

VALOR = "R$ 5.000,00"

# Contrato LIMPO: passa todos os bloqueios e não dispara alertas.
LIMPO = """\
CONTRATO DE HONORÁRIOS ADVOCATÍCIOS

Contratante: Fulano Teste.
Contratado: Sociedade Teste Advogados, OAB/SP nº 123456.

Objeto: ação de repetição de indébito de IPTU.

Dos honorários: o Contratante pagará a título de honorários o valor de
R$ 5.000,00, à vista, mais o reembolso de custas e despesas processuais.

Foro da comarca de São Paulo. São Paulo, 15/06/2026.
"""


def _regras(texto, **kw):
    kw.setdefault("valor_honorarios", VALOR)
    return {a.regra for a in lint_contrato(texto, **kw)}


# --- limpo -------------------------------------------------------------------

def test_contrato_limpo_passa_sem_achados():
    achados = lint_contrato(LIMPO, valor_honorarios=VALOR)
    assert achados == []
    assert lint_ok(achados) is True


# --- bloqueios ---------------------------------------------------------------

def test_b1_promessa_de_exito():
    t = LIMPO + "\nGarantimos o êxito da ação."
    assert "B1" in _regras(t)
    assert lint_ok(lint_contrato(t, valor_honorarios=VALOR)) is False


def test_b2_slogan():
    assert "B2" in _regras(LIMPO + "\n100% de êxito ou seu dinheiro de volta.")


def test_b3_multa_por_revogar_mandato():
    t = LIMPO + "\nEm caso de multa por revogação do mandato pelo cliente."
    assert "B3" in _regras(t)


def test_b4_honorario_divergente_bloqueia():
    # texto fala R$ 8.000, mas o Mario digitou R$ 5.000,00 → bloqueia
    t = LIMPO.replace("R$ 5.000,00", "R$ 8.000,00")
    regras = _regras(t)
    assert "B4" in regras
    assert lint_ok(lint_contrato(t, valor_honorarios=VALOR)) is False


def test_b4_valor_digitado_presente_passa():
    assert "B4" not in _regras(LIMPO)


def test_b4_parcela_vira_alerta_nao_bloqueio():
    t = LIMPO.replace(
        "R$ 5.000,00, à vista",
        "R$ 5.000,00 em 5 parcelas de R$ 1.000,00",
    )
    achados = lint_contrato(t, valor_honorarios=VALOR)
    regras = {a.regra for a in achados}
    assert "B4" not in regras                 # o valor digitado aparece
    assert "A_valor_extra" in regras          # a parcela é só alerta
    assert lint_ok(achados) is True


def test_b5_sem_oab_bloqueia():
    t = LIMPO.replace(", OAB/SP nº 123456", "")
    assert "B5" in _regras(t)


def test_b6_sem_clausula_honorarios():
    t = "Contrato. Sociedade Teste, OAB/SP nº 123456. Foro comarca SP, 15/06/2026."
    # valor não aparece (B4) e não há cláusula de honorários (B6)
    assert "B6" in _regras(t)


def test_b8_quota_litis_acima_de_50():
    t = LIMPO + "\nHonorários de êxito de 60% sobre o proveito."
    assert "B8" in _regras(t)


def test_b8_cessao_de_credito():
    t = LIMPO + "\nO escritório cede o crédito do cliente a terceiros."
    assert "B8" in _regras(t)


def test_b9_gratuidade_enganosa():
    assert "B9" in _regras(LIMPO + "\nPrimeira consulta grátis, sem custo.")


def test_b7_conflito_no_texto():
    t = LIMPO + "\nParte adversa: Empresa Adversaria Ltda."
    regras = _regras(t, nomes_parte_contraria=["Empresa Adversaria Ltda"])
    assert "B7" in regras


def test_exige_honorarios_false_pula_b6_b8_b9():
    # procuração: sem cláusula de honorários NÃO bloqueia
    proc = (
        "PROCURAÇÃO. Outorgante: Fulano Teste. Outorgado: Sociedade Teste, "
        "OAB/SP nº 123456. Foro comarca SP, 15/06/2026, custas pelo outorgante."
    )
    regras = _regras(proc, exige_honorarios=False)
    assert "B6" not in regras and "B8" not in regras and "B9" not in regras


# --- alertas (não travam) ----------------------------------------------------

def test_a1_placeholder_alerta_nao_bloqueia():
    t = LIMPO + "\nCidade: {{CIDADE}}."
    achados = lint_contrato(t, valor_honorarios=VALOR)
    assert "A1" in {a.regra for a in achados}
    assert lint_ok(achados) is True          # alerta não trava


def test_a5_cpf_cravado_alerta():
    t = LIMPO + "\nCPF: 123.456.789-00."
    achados = lint_contrato(t, valor_honorarios=VALOR)
    assert "A5" in {a.regra for a in achados}
    assert lint_ok(achados) is True


# =============================================================================
# Regressão dos 33 achados da revisão adversarial.
# Convenção: falso_negativo → o exemplo agora DEVE bloquear/alertar;
#            falso_positivo → o exemplo NÃO pode bloquear/alertar.
# Dados fictícios ("Fulano Teste", OAB/SP nº 123456) por regra do projeto.
# =============================================================================

# Base com cláusula de honorários válida — pra testar uma regra sem que B6
# dispare junto. O texto-frase de cada caso é apenso a esta base.
_BASE = (
    "Sociedade Teste Advogados, OAB/SP nº 123456. Dos honorários: o "
    "Contratante pagará a título de honorários o valor de R$ 5.000,00, à "
    "vista, mais o reembolso de custas. Foro da comarca de São Paulo, "
    "15/06/2026.\n"
)


# --- B1: promessa/garantia de resultado (falsos negativos) -------------------

def test_b1_fn_compromete_procedencia():
    assert "B1" in _regras(_BASE + "O escritório compromete-se a obter a "
                           "procedência total da demanda.")


def test_b1_fn_conviccao_do_exito():
    assert "B1" in _regras(_BASE + "Temos plena convicção do êxito da ação.")


def test_b1_fn_provimento_dado_como_certo():
    assert "B1" in _regras(_BASE + "O provimento do recurso é dado como certo.")


def test_b1_fn_garante_devolucao_em_dobro():
    assert "B1" in _regras(_BASE + "Garantimos a devolução em dobro dos "
                           "valores cobrados.")


def test_b1_fn_assegura_deferimento():
    assert "B1" in _regras(_BASE + "Asseguramos ao cliente o deferimento do "
                           "pedido.")


# --- B1: cláusulas lícitas (falsos positivos — NÃO bloquear) -----------------

def test_b1_fp_sigilo_dos_resultados():
    t = _BASE + ("As partes garantem o sigilo e a confidencialidade dos "
                 "resultados das tratativas.")
    assert "B1" not in _regras(t)
    assert lint_ok(lint_contrato(t, valor_honorarios=VALOR)) is True


def test_b1_fp_em_caso_de_ganho_de_causa():
    assert "B1" not in _regras(_BASE + "O contratante assegura, em caso de "
                               "ganho de causa, o repasse da verba de "
                               "sucumbência ao escritório.")


def test_b1_fp_aprovacao_pelo_cliente():
    assert "B1" not in _regras(_BASE + "O escritório garante a qualidade "
                               "técnica dos serviços até a aprovação do "
                               "acordo pelo cliente.")


def test_b1_fp_garantia_do_juizo():
    assert "B1" not in _regras(_BASE + "A garantia do juízo será prestada "
                               "para discutir o resultado da execução fiscal.")


def test_b1_fp_certeza_tecnica():
    assert "B1" not in _regras(_BASE + "A petição será elaborada com a "
                               "certeza técnica necessária, visando a "
                               "procedência dos pedidos.")


def test_b1_fp_compromisso_de_conduta():
    # promessa de CONDUTA (comparecer), não de resultado
    assert "B1" not in _regras(_BASE + "O advogado compromete-se a comparecer "
                               "à audiência.")


def test_b1_tp_garante_exito_continua_bloqueando():
    # true-positive: a guarda não pode abrir brecha
    assert "B1" in _regras(_BASE + "O advogado garante o êxito da ação e a "
                           "procedência do pedido.")


# --- B2: "só pago se ganhar" / "perdeu, nada paga" ---------------------------

def test_b2_fn_so_cobra_se_ganhar():
    assert "B2" in _regras(_BASE + "Só cobramos se você ganhar.")


def test_b2_fn_so_paga_quando_ganhar():
    assert "B2" in _regras(_BASE + "Você só paga quando ganhar.")


def test_b2_fn_perdeu_nada_paga():
    assert "B2" in _regras(_BASE + "Se perder a causa, o cliente nada pagará.")


def test_b2_fn_sem_ganho_sem_honorarios():
    assert "B2" in _regras(_BASE + "Sem ganho, sem honorários.")


def test_b2_fp_parcelamento_nao_dispara():
    assert "B2" not in _regras(_BASE + "Honorários parcelados em 12x.")


# --- B3: cláusula penal por revogar mandato (independente de ordem) ----------

def test_b3_fn_revogacao_antes_da_multa():
    assert "B3" in _regras(_BASE + "Em caso de revogação do mandato pelo "
                           "cliente, será devida multa de 20%.")


def test_b3_fn_destituicao_penalidade():
    assert "B3" in _regras(_BASE + "A destituição do advogado antes do "
                           "trânsito implicará penalidade de R$ 5.000.")


def test_b3_fn_constituir_outro_advogado():
    assert "B3" in _regras(_BASE + "Se o cliente constituir outro advogado, "
                           "deverá indenizar o escritório.")


def test_b3_fn_renuncia_multa_invertida():
    assert "B3" in _regras(_BASE + "A renúncia aos poderes não isenta o "
                           "cliente da multa contratual.")


def test_b3_fn_distrato_mandante():
    assert "B3" in _regras(_BASE + "O distrato unilateral pelo mandante "
                           "acarreta multa rescisória.")


def test_b3_fp_negacao_nao_havera_multa():
    assert "B3" not in _regras(_BASE + "Não haverá multa em caso de revogação "
                               "do mandato; a procuração poderá ser revogada "
                               "a qualquer tempo.")


# --- B4: valor de honorários (o coração) -------------------------------------

def test_b4_fn_input_sem_prefixo_rs():
    # Mario digita "5000" (sem R$); a IA escreve R$ 5.000,00 → não bloqueia
    t = _BASE + "Honorários advocatícios: a quantia de R$ 5.000,00, à vista."
    assert "B4" not in _regras(t, valor_honorarios="5000")


def test_b4_fn_input_com_ponto_milhar():
    t = _BASE + "Honorários advocatícios: a quantia de R$ 5.000,00, à vista."
    assert "B4" not in _regras(t, valor_honorarios="5.000")


def test_b4_fn_sinonimo_verba_honoraria():
    t = ("Sociedade Teste, OAB/SP nº 123456. A verba honorária corresponde a "
         "R$ 10.000,00, à vista. Foro comarca SP, 15/06/2026, custas pelo "
         "cliente.")
    regras = _regras(t, valor_honorarios="R$ 10.000,00")
    assert "B4" not in regras
    assert "B6" not in regras           # sinônimo conserta B4 e B6 juntos


def test_b4_fp_soma_de_parcelas():
    # Mario digitou o TOTAL R$ 10.000; entrada 5k + saldo 5k = 10k → não bloqueia
    t = ("Sociedade Teste, OAB/SP nº 123456. Honorários pagos em 2 parcelas: "
         "R$ 5.000,00 de entrada e mais R$ 5.000,00 em 30 dias. Foro comarca "
         "SP, 15/06/2026, custas.")
    achados = lint_contrato(t, valor_honorarios="R$ 10.000,00")
    assert "B4" not in {a.regra for a in achados}


def test_b4_fn_so_percentual_diverge():
    # Mario digitou 20%; a IA escreveu 30% → bloqueia
    t = (_BASE + "Honorários de êxito de 30% sobre o proveito econômico.")
    assert "B4" in _regras(t, valor_honorarios="20%")


def test_b4_fp_so_percentual_bate():
    t = (_BASE + "Honorários de êxito de 20% sobre o proveito econômico.")
    assert "B4" not in _regras(t, valor_honorarios="20%")


def test_b4_fn_decimal_americano_ambiguo():
    # "R$ 5.000.00" (ponto decimal americano) → bloqueia para conferência
    t = ("Sociedade Teste, OAB/SP nº 123456. Honorários advocatícios de "
         "R$ 5.000.00, à vista. Foro comarca SP, 15/06/2026, custas.")
    assert "B4" in _regras(t, valor_honorarios="R$ 5.000,00")


def test_b4_fp_valor_da_causa_nao_confunde():
    # o valor digitado coincide com honorário; valor-da-causa igual não conta
    t = ("Sociedade Teste, OAB/SP nº 123456. Ação com valor da causa de "
         "R$ 5.000,00. Honorários advocatícios de R$ 5.000,00, à vista. "
         "Foro comarca SP, 15/06/2026, custas.")
    assert "B4" not in _regras(t, valor_honorarios="R$ 5.000,00")


def test_b4_fn_honorario_inventado_apesar_da_causa():
    # número do Mario virou valor-da-causa; honorário foi inventado → bloqueia
    t = ("Sociedade Teste, OAB/SP nº 123456. Ação com valor da causa de "
         "R$ 5.000,00. Honorários advocatícios de R$ 9.999,00, à vista. "
         "Foro comarca SP, 15/06/2026, custas.")
    assert "B4" in _regras(t, valor_honorarios="R$ 5.000,00")


def test_b4_fp_valor_antes_da_palavra():
    # janela bidirecional: valor ANTES de "honorários"
    t = ("Sociedade Teste, OAB/SP nº 123456. Será pago R$ 5.000,00 a título "
         "de honorários advocatícios, à vista. Foro comarca SP, 15/06/2026, "
         "custas.")
    assert "B4" not in _regras(t, valor_honorarios="R$ 5.000,00")


def test_b4_fn_extenso_sem_cifra_avisa():
    # Mario digita por extenso sem cifra → B4 não pode desligar mudo: avisa
    t = (_BASE + "O cliente pagará cinco mil reais a título de honorários.")
    assert "B4" in _regras(t, valor_honorarios="cinco mil reais")


def test_b4_fp_input_5_mil_com_sufixo():
    # "R$ 5 mil" digitado = 5.000; a IA escreve R$ 5.000,00 → não bloqueia
    t = ("Sociedade Teste, OAB/SP nº 123456. Honorários advocatícios de "
         "R$ 5.000,00, à vista. Foro comarca SP, 15/06/2026, custas.")
    assert "B4" not in _regras(t, valor_honorarios="R$ 5 mil")


def test_b4_a_combinar_nao_checa():
    # 'a combinar' não tem número → B4 não dispara (nem o aviso de parse)
    t = ("Sociedade Teste, OAB/SP nº 123456. Honorários a serem ajustados, "
         "R$ 5.000,00 à vista. Foro comarca SP, 15/06/2026, custas.")
    assert "B4" not in _regras(t, valor_honorarios="a combinar")


# --- B5: OAB tolerante + exclusão de contexto normativo ----------------------

def test_b5_fn_oab_sob_o_numero():
    t = ("Sociedade Teste, inscrita na OAB/SP sob o nº 123.456. Honorários: "
         "R$ 5.000,00, à vista, custas. Foro comarca SP, 15/06/2026.")
    assert "B5" not in _regras(t)


def test_b5_fn_oab_hifen():
    t = ("Sociedade Teste, OAB-SP 123456. Honorários: R$ 5.000,00, à vista, "
         "custas. Foro comarca SP, 15/06/2026.")
    assert "B5" not in _regras(t)


def test_b5_fn_oab_uf_depois_do_numero():
    t = ("Sociedade Teste, inscrita na OAB sob nº 123.456/SP. Honorários: "
         "R$ 5.000,00, à vista, custas. Foro comarca SP, 15/06/2026.")
    assert "B5" not in _regras(t)


def test_b5_fn_ordem_dos_advogados_por_extenso():
    t = ("Sociedade Teste, inscrita na Ordem dos Advogados do Brasil, Seção "
         "de São Paulo, sob o número 123.456. Honorários: R$ 5.000,00, à "
         "vista, custas. Foro comarca SP, 15/06/2026.")
    assert "B5" not in _regras(t)


def test_b5_bug_provimento_nao_conta_como_inscricao():
    # cita só o Provimento OAB/CF — SEM inscrição real → B5 DEVE bloquear
    t = ("Conforme o Provimento OAB/CF nº 205/2021, o advogado atua com ética. "
         "Honorários: R$ 5.000,00, à vista, custas. Foro comarca SP, "
         "15/06/2026.")
    assert "B5" in _regras(t)


# --- B6: valor R$ OU percentual + forma de pagamento -------------------------

def test_b6_fp_honorario_so_percentual():
    # ad exitum de 30% sem valor fixo → cláusula válida, NÃO bloqueia B6
    t = ("Sociedade Teste, OAB/SP nº 123456. Honorários ad exitum de 30% "
         "sobre o proveito econômico, sem valor fixo, devidos somente em "
         "caso de êxito. Foro comarca SP, 15/06/2026, custas.")
    assert "B6" not in _regras(t, valor_honorarios="30%")


def test_b6_fp_forma_moeda_corrente_no_ato():
    t = ("Sociedade Teste, OAB/SP nº 123456. Honorários de R$ 5.000,00 "
         "quitados em moeda corrente no ato. Foro comarca SP, 15/06/2026, "
         "custas.")
    assert "B6" not in _regras(t)


# --- B8: quota litis > 50% (% / por extenso / frações) -----------------------

def test_b8_fn_por_cento_digito():
    assert "B8" in _regras(_BASE + "A título de êxito, o advogado fará jus a "
                           "60 por cento do proveito econômico.")


def test_b8_fn_percentual_por_extenso():
    assert "B8" in _regras(_BASE + "Honorários de êxito de sessenta por cento.")


def test_b8_fn_fracao_dois_tercos():
    assert "B8" in _regras(_BASE + "Honorários de êxito de dois terços do "
                           "proveito.")


def test_b8_fp_cinquenta_por_cento_no_limite():
    # limite é > 50%; exatamente 50% NÃO bloqueia
    assert "B8" not in _regras(_BASE + "Honorários de êxito de 50 por cento.")


def test_b8_fn_janela_ampla():
    assert "B8" in _regras(_BASE + "Os honorários de êxito, conforme "
                           "detalhado na cláusula seguinte e ajustado entre "
                           "as partes, serão de 60% do proveito.")


def test_b8_fn_cessao_credito_ordem_invertida():
    assert "B8" in _regras(_BASE + "O crédito do cliente fica cedido ao "
                           "escritório.")


def test_b8_fn_cessao_direitos_creditorios():
    assert "B8" in _regras(_BASE + "Cessão dos direitos creditórios do "
                           "contratante ao advogado.")


# --- B9: gratuidade enganosa (sem pegar "justiça gratuita") ------------------

def test_b9_fn_inteiramente_gratuita():
    assert "B9" in _regras(_BASE + "A primeira consulta é inteiramente "
                           "gratuita.")


def test_b9_fn_nao_desembolsara():
    assert "B9" in _regras(_BASE + "O cliente não desembolsará qualquer "
                           "quantia.")


def test_b9_fn_sem_qualquer_onus():
    assert "B9" in _regras(_BASE + "Sem qualquer ônus para o cliente.")


def test_b9_fp_justica_gratuita_legitima():
    # benefício processual do art. 98 CPC — NÃO é gratuidade enganosa
    assert "B9" not in _regras(_BASE + "O cliente faz jus à justiça gratuita "
                               "nos termos do art. 98 do CPC.")


# --- A1: placeholders adicionais (alerta) ------------------------------------

def test_a1_fn_placeholder_angle():
    achados = lint_contrato(_BASE + "Nome: <<NOME>>.", valor_honorarios=VALOR)
    assert "A1" in {a.regra for a in achados}


def test_a1_fn_placeholder_guillemets():
    achados = lint_contrato(_BASE + "Valor: «VALOR».", valor_honorarios=VALOR)
    assert "A1" in {a.regra for a in achados}


def test_a1_fn_placeholder_hash():
    achados = lint_contrato(_BASE + "Cliente: #NOME#.", valor_honorarios=VALOR)
    assert "A1" in {a.regra for a in achados}


def test_a1_fn_placeholder_pontilhado():
    achados = lint_contrato(
        _BASE + "Assinatura: ............ .", valor_honorarios=VALOR,
    )
    assert "A1" in {a.regra for a in achados}


# --- A4: jurisprudência só alerta se garante ÊXITO ---------------------------

def test_a4_fp_jurisprudencia_garante_direito():
    t = _BASE + ("A jurisprudência do STJ garante ao consumidor o direito à "
                 "informação adequada (Tema 952).")
    assert "A4" not in _regras(t)


def test_a4_fn_jurisprudencia_garante_exito():
    t = _BASE + "A jurisprudência é pacífica e garante o êxito da ação."
    achados = lint_contrato(t, valor_honorarios=VALOR)
    assert "A4" in {a.regra for a in achados}


# --- A7: foro de eleição bidirecional ----------------------------------------

def test_a7_fn_elegem_antes_de_foro():
    t = _BASE + ("As partes elegem o foro da Comarca da Capital para dirimir "
                 "as controvérsias.")
    achados = lint_contrato(t, valor_honorarios=VALOR)
    assert "A7" in {a.regra for a in achados}
    assert lint_ok(achados) is True          # A7 é só alerta
