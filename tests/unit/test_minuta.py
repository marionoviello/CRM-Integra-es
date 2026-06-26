"""#39: montar_minuta — preenche o template aprovado mantendo as fixas intactas."""

import pytest

from noviello_funil.minuta import DadosMinuta, montar_minuta


def _dados(**over):
    base = dict(
        cliente_nome="Fulano Teste",
        cliente_nacionalidade="brasileiro",
        cliente_estado_civil="solteiro",
        cliente_profissao="comerciante",
        cliente_rg="12.345.678-9 SSP/SP",
        cliente_cpf="123.456.789-00",
        cliente_endereco="Rua X, 1, São Paulo/SP, CEP 00000-000",
        cliente_email="fulano@exemplo.com",
        objeto="Ação de inventário e partilha dos bens deixados por...",
        honorarios_fixo="R$ 5.000,00 (cinco mil reais)",
        honorarios_exito="10% (dez por cento)",
        multa_liminar_pct="30% (trinta por cento)",
        data="25 de junho de 2026",
    )
    base.update(over)
    return DadosMinuta(**base)


def test_montar_minuta_preenche_slots_e_mantem_fixas():
    texto = montar_minuta(_dados())
    # slots variáveis preenchidos
    assert "Fulano Teste" in texto
    assert "Ação de inventário e partilha" in texto
    assert "R$ 5.000,00 (cinco mil reais)" in texto
    assert "30% (trinta por cento)" in texto
    # cláusulas FIXAS intactas (amostras das protetivas)
    assert "obrigação de meio" in texto  # cl 2ª
    assert "Recomendação nº 001/2024" in texto  # cl 14ª (IA)
    assert "falso advogado" in texto  # cl 15ª (segurança)
    assert "Foro Central da Comarca de São Paulo" in texto  # cl 17ª
    assert "27.340.554/0001-94" in texto  # PIX/CNPJ
    # nenhum slot sobrando
    assert "{{" not in texto and "}}" not in texto
    # o aviso v1 (comentário HTML de metadados) NÃO vaza pro contrato
    assert "REFERENCIA_QUEBRADA" not in texto
    assert "<!--" not in texto


def test_montar_minuta_honorarios_e_o_valor_passado():
    # o valor de honorários é EXATAMENTE o que veio (do Mario), nunca inventado
    texto = montar_minuta(_dados(honorarios_fixo="R$ 12.345,67"))
    assert "R$ 12.345,67" in texto


def test_montar_minuta_cpf_aparece_na_qualificacao_e_na_assinatura():
    texto = montar_minuta(_dados(cliente_cpf="999.888.777-66"))
    assert texto.count("999.888.777-66") >= 2  # qualificação + bloco de assinatura


def test_montar_minuta_slot_nao_preenchido_levanta():
    # blindagem: se o template ganhar um slot novo sem campo, falha ALTO
    # (não envia contrato com {{...}} cru pro cliente).
    import noviello_funil.minuta as m

    original = m._carregar_template
    m._carregar_template = lambda: original() + "\n{{SLOT_ORFAO}}\n"
    try:
        with pytest.raises(ValueError, match="SLOT_ORFAO"):
            montar_minuta(_dados())
    finally:
        m._carregar_template = original
