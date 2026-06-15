"""Tests for the client status-inquiry feature (atendimento_processo, 2.4).

Duas travas OAB do Mario: (1) só responde pro telefone que está no cadastro
como parte cliente; (2) processo sigiloso → não responde, escala Mario+Hilde.
"""

from noviello_funil.atendimento_processo import (
    alerta_nao_identificado,
    alerta_sigiloso,
    classificar_atendimento,
    construir_indice_cliente_processo,
    consultar_processos_do_telefone,
    detectar_pergunta_status,
    extrair_documento,
    montar_resposta_cliente,
)
from noviello_funil.db import connect, run_migrations
from noviello_funil.person_index import chaves_telefone

# --- detectar_pergunta_status (intenção) -------------------------------------

def test_perguntas_de_status_positivas():
    for t in [
        "Oi, como está meu processo?",
        "queria saber o andamento do processo",
        "tem alguma novidade no meu processo?",
        "qual o status da minha ação?",
        "meu processo teve alguma movimentação?",
        "como anda o caso?",
        "saiu alguma novidade do inventário?",
    ]:
        assert detectar_pergunta_status(t) is True, t


def test_nao_confunde_com_lead_novo():
    # Quem quer ABRIR um caso não deve disparar o fluxo de status.
    for t in [
        "quero abrir um processo contra meu vizinho",
        "preciso processar uma empresa",
        "como faço pra entrar com uma ação?",
        "tenho um problema com meu plano de saúde",
        "vocês pegam caso de usucapião?",
        "bom dia, gostaria de uma consulta",
    ]:
        assert detectar_pergunta_status(t) is False, t


# --- extrair_documento -------------------------------------------------------

def test_extrai_cpf_do_sufixo_do_nome():
    assert extrair_documento("Fulano Teste - CPF: 123.456.789-00") == "12345678900"
    assert extrair_documento("Empresa X - CNPJ: 12.345.678/0001-90") == "12345678000190"
    assert extrair_documento("Beltrano Sem Documento") == ""


# --- construir_indice + consultar (vínculo telefone↔processo) ----------------

class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeClient:
    """Devolve uma página só de /lawSuit/ — basta pro build."""

    def __init__(self, processos):
        self._p = processos

    def get(self, path, params=None):
        return _FakeResp({"data": self._p, "totalPages": 1})


def _seed_person(conn, tel, pid, nome, doc=""):
    for ch in chaves_telefone(tel):
        conn.execute(
            "INSERT OR REPLACE INTO person_index "
            "(telefone_chave, person_id, nome, document) VALUES (?, ?, ?, ?)",
            (ch, pid, nome, doc),
        )


def test_indice_casa_por_cpf():
    conn = connect(":memory:")
    run_migrations(conn)
    _seed_person(conn, "11999998888", "p1", "Fulano Teste", "123.456.789-00")
    client = _FakeClient([{
        "processNumber": "1000000-00.2024.8.26.0100",
        "isSecret": False,
        "lastMovementDate": "2026-06-10T00:00:00Z",
        "persons": [
            {"name": "Fulano Teste - CPF: 123.456.789-00", "personOrigin": "Cliente"},
            {"name": "Banco Réu S.A.", "personOrigin": "Requerida"},
        ],
    }])
    n = construir_indice_cliente_processo(client, conn)
    assert n >= 1
    procs = consultar_processos_do_telefone(conn, "11999998888")
    assert len(procs) == 1
    assert procs[0]["process_number"] == "1000000-00.2024.8.26.0100"
    assert procs[0]["is_secret"] is False
    assert procs[0]["last_movement_date"] == "2026-06-10"
    conn.close()


def test_indice_casa_por_nome_quando_sem_cpf():
    conn = connect(":memory:")
    run_migrations(conn)
    _seed_person(conn, "11988887777", "p2", "Beltrano Souza Lima", doc="")
    client = _FakeClient([{
        "processNumber": "2-2",
        "isSecret": False,
        "persons": [{"name": "Beltrano Souza Lima", "personOrigin": "Cliente"}],
    }])
    construir_indice_cliente_processo(client, conn)
    assert len(consultar_processos_do_telefone(conn, "11988887777")) == 1
    conn.close()


def test_indice_marca_segredo_de_justica():
    conn = connect(":memory:")
    run_migrations(conn)
    _seed_person(conn, "11977776666", "p3", "Sigiloso Cliente", "111.111.111-11")
    client = _FakeClient([{
        "processNumber": "3-3",
        "isSecret": True,
        "persons": [{"name": "Sigiloso Cliente - CPF: 111.111.111-11",
                     "personOrigin": "Cliente"}],
    }])
    construir_indice_cliente_processo(client, conn)
    procs = consultar_processos_do_telefone(conn, "11977776666")
    assert procs[0]["is_secret"] is True
    conn.close()


def test_parte_contraria_nao_vira_vinculo():
    # O telefone do RÉU (se por acaso cadastrado) não pode puxar o processo.
    conn = connect(":memory:")
    run_migrations(conn)
    _seed_person(conn, "11955554444", "p4", "Banco Réu", "999.999.999-99")
    client = _FakeClient([{
        "processNumber": "4-4",
        "isSecret": False,
        "persons": [{"name": "Banco Réu - CPF: 999.999.999-99",
                     "personOrigin": "Requerida"}],
    }])
    construir_indice_cliente_processo(client, conn)
    assert consultar_processos_do_telefone(conn, "11955554444") == []
    conn.close()


def test_telefone_desconhecido_sem_vinculo():
    conn = connect(":memory:")
    run_migrations(conn)
    assert consultar_processos_do_telefone(conn, "11900000000") == []
    conn.close()


# --- classificar_atendimento -------------------------------------------------

def _p(num, secret):
    return {"process_number": num, "is_secret": secret,
            "last_movement_date": "2026-06-01", "cliente_nome": "Cliente"}


def test_classifica_nao_cadastrado():
    assert classificar_atendimento([])["acao"] == "nao_cadastrado"


def test_classifica_responder_quando_ha_publico():
    r = classificar_atendimento([_p("1", False), _p("2", True)])
    assert r["acao"] == "responder"
    assert [x["process_number"] for x in r["publicos"]] == ["1"]
    assert [x["process_number"] for x in r["sigilosos"]] == ["2"]


def test_classifica_sigiloso_quando_so_secreto():
    r = classificar_atendimento([_p("9", True)])
    assert r["acao"] == "sigiloso"
    assert r["publicos"] == []


# --- montar_resposta_cliente / alertas ---------------------------------------

def test_resposta_inclui_movimentacao_e_respeita_marca():
    publicos = [_p("1000000-00.2024.8.26.0100", False)]
    movs = {"1000000-00.2024.8.26.0100": {"data": "2026-06-12", "nome": "Conclusão"}}
    msg = montar_resposta_cliente(publicos, movs)
    assert "12/06/2026" in msg
    assert "Conclusão" in msg
    assert "nossa equipe" in msg.lower()
    assert "Dr. Mario" not in msg          # regra de marca
    assert "Mario" not in msg


def test_resposta_sem_datajud_usa_data_do_juridiq():
    msg = montar_resposta_cliente([_p("1", False)], movimentos=None)
    assert "01/06/2026" in msg


def test_alerta_sigiloso_vai_pro_interno_com_processo():
    msg = alerta_sigiloso("João Cliente", "5511999998888", [_p("7-7", True)])
    assert "7-7" in msg
    assert "manual" in msg.lower()
    assert "🔒" in msg


def test_alerta_nao_identificado_cita_numero_e_msg():
    msg = alerta_nao_identificado("5511900000000", "como está meu processo?")
    assert "5511900000000" in msg
    assert "como está meu processo" in msg
