"""Tests for the weekly funnel report (relatorio.gerar_relatorio_semanal)."""

from noviello_funil.relatorio import gerar_relatorio_semanal
from noviello_funil.state import Estado


def _insert_lead(conn, lead_id, estado=Estado.EM_CONVERSA, criado_offset="-1 days"):
    conn.execute(
        """INSERT INTO leads
           (jurichat_lead_id, jurichat_conversation_id, contato_telefone,
            contato_nome, estado, criado_em)
           VALUES (?, ?, '5511999999999', 'Lead', ?, datetime('now', ?))""",
        (f"L-{lead_id}", f"C-{lead_id}", estado, criado_offset),
    )
    return conn.execute(
        "SELECT id FROM leads WHERE jurichat_lead_id = ?", (f"L-{lead_id}",)
    ).fetchone()["id"]


def _insert_transicao(conn, lead_id, motivo, criado_offset="-1 days"):
    conn.execute(
        """INSERT INTO transicoes
           (lead_id, estado_anterior, estado_novo, motivo, criado_em)
           VALUES (?, 'em_conversa', 'aguardando_humano', ?, datetime('now', ?))""",
        (lead_id, motivo, criado_offset),
    )


def test_relatorio_db_vazio(db_conn):
    texto = gerar_relatorio_semanal(db_conn)
    assert "Leads novos: 0" in texto
    assert "Agendamentos criados: 0" in texto
    assert "Em conversa agora: 0" in texto
    # Sem leads novos, não mostra percentual (divisão por zero protegida)
    assert "%" not in texto


def test_relatorio_conta_leads_novos_da_janela(db_conn):
    _insert_lead(db_conn, 1, criado_offset="-2 days")    # dentro
    _insert_lead(db_conn, 2, criado_offset="-6 days")    # dentro
    _insert_lead(db_conn, 3, criado_offset="-10 days")   # FORA da janela 7d

    texto = gerar_relatorio_semanal(db_conn)
    assert "Leads novos: 2" in texto


def test_relatorio_conta_agendamentos_e_taxa(db_conn):
    lid1 = _insert_lead(db_conn, 1)
    lid2 = _insert_lead(db_conn, 2)
    _insert_lead(db_conn, 3)
    _insert_transicao(db_conn, lid1, "claude_confirmar_horario")
    # Agendamento antigo (fora da janela) não conta
    _insert_transicao(db_conn, lid2, "claude_confirmar_horario",
                      criado_offset="-9 days")

    texto = gerar_relatorio_semanal(db_conn)
    assert "Agendamentos criados: 1 (33% dos novos)" in texto


def test_relatorio_agrupa_handoffs(db_conn):
    lid = _insert_lead(db_conn, 1)
    _insert_transicao(db_conn, lid, "claude_propor")
    _insert_transicao(db_conn, lid, "claude_handoff")
    _insert_transicao(db_conn, lid, "max_turnos")
    # Motivo que NÃO é handoff
    _insert_transicao(db_conn, lid, "scheduler_followup_1")

    texto = gerar_relatorio_semanal(db_conn)
    assert "Handoffs pra equipe: 3" in texto
    assert "Follow-ups enviados: 1" in texto


def test_relatorio_snapshot_em_conversa(db_conn):
    _insert_lead(db_conn, 1, estado=Estado.EM_CONVERSA)
    _insert_lead(db_conn, 2, estado=Estado.EM_CONVERSA)
    _insert_lead(db_conn, 3, estado=Estado.AGUARDANDO_HUMANO)

    texto = gerar_relatorio_semanal(db_conn)
    assert "Em conversa agora: 2" in texto


def test_relatorio_formato_whatsapp(db_conn):
    """Sem HTML, com bullets e header em negrito WhatsApp (asteriscos)."""
    texto = gerar_relatorio_semanal(db_conn)
    assert "<" not in texto
    assert texto.startswith("📊 *Relatório semanal")
    assert "• " in texto
