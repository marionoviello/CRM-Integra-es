"""Tests for the pre-meeting team briefing (briefing_reuniao, roadmap 3.2)."""

from noviello_funil.briefing_reuniao import montar_briefing


def _p(num, secret=False, data="2026-06-10"):
    return {"process_number": num, "is_secret": secret,
            "last_movement_date": data, "cliente_nome": "Cliente"}


def test_briefing_cliente_lista_processos_e_data():
    msg = montar_briefing(
        "João Cliente", "5511999998888", "ter (16/jun) às 15h",
        "https://meet.google.com/abc", [_p("1000000-00.2024.8.26.0100")],
    )
    assert "João Cliente" in msg
    assert "5511999998888" in msg
    assert "1000000-00.2024.8.26.0100" in msg
    assert "10/06/2026" in msg
    assert "Cliente da casa" in msg
    assert "meet.google.com/abc" in msg


def test_briefing_marca_sigiloso_mas_inclui():
    # Interno: processo sigiloso ENTRA no briefing (equipe cuida), marcado 🔒.
    msg = montar_briefing("Ana", "5511988887777", "qua às 10h", "",
                          [_p("9-9", secret=True)])
    assert "9-9" in msg
    assert "🔒" in msg


def test_briefing_lead_novo_sem_processo():
    msg = montar_briefing("Beltrano", "5511900000000", "hoje às 18h", "", [])
    assert "Não consta como cliente" in msg
    assert "Beltrano" in msg


def test_briefing_sem_nome_usa_fallback():
    msg = montar_briefing("", "5511900000000", "às 9h", "", [])
    assert "Lead" in msg


def test_briefing_trunca_muitos_processos():
    procs = [_p(str(i)) for i in range(12)]
    msg = montar_briefing("X", "5511", "às 9h", "", procs)
    assert "e mais 4" in msg
