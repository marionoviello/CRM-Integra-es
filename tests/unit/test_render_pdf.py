"""#39: render_pdf — markdown da minuta → PDF timbrado válido."""

from noviello_funil.minuta import DadosMinuta, montar_minuta
from noviello_funil.render_pdf import render_contrato_pdf


def test_render_pdf_valido_e_nao_vazio():
    md = "# TÍTULO\n\n## CLÁUSULA 1ª — DO OBJETO  *(variável)*\n\nTexto **negrito** comum.\n\n---\n\nFim."
    pdf = render_contrato_pdf(md)
    assert pdf[:5] == b"%PDF-"  # assinatura de PDF
    assert len(pdf) > 1500


def test_render_pdf_da_minuta_real_renderiza():
    # integração: template aprovado → montar_minuta → render sem erro, multi-página
    dados = DadosMinuta(
        cliente_nome="Fulano Teste", cliente_nacionalidade="brasileiro",
        cliente_estado_civil="solteiro", cliente_profissao="comerciante",
        cliente_rg="12.345.678-9 SSP/SP", cliente_cpf="123.456.789-00",
        cliente_endereco="Rua X, 1, São Paulo/SP", cliente_email="f@x.com",
        objeto="Ação de inventário e partilha.", honorarios_fixo="R$ 5.000,00",
        honorarios_exito="10%", multa_liminar_pct="30%", data="25/06/2026",
    )
    pdf = render_contrato_pdf(montar_minuta(dados))
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 3000  # contrato inteiro = vários KB


def test_render_pdf_escapa_caracteres_especiais():
    # & < > em nome/objeto não podem quebrar o markup do reportlab
    md = "## CLÁUSULA\n\nEmpresa A & B <Ltda> contra C & D."
    pdf = render_contrato_pdf(md)  # não levanta
    assert pdf[:5] == b"%PDF-"
