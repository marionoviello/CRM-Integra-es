"""Tests for the client status-inquiry feature (atendimento_processo, 2.4).

Duas travas OAB do Mario: (1) só responde pro telefone que está no cadastro
como parte cliente; (2) processo sigiloso → não responde, escala Mario+Hilde.
Autenticação SÓ POR CPF (homônimo por nome não vincula — revisão 15/jun).
"""

from noviello_funil.atendimento_processo import (
    alerta_ambiguo,
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
        "meu processo andou?",
        "novidades do meu processo?",
    ]:
        assert detectar_pergunta_status(t) is True, t


def test_nao_confunde_com_lead_novo():
    # Quem quer ABRIR um caso não deve disparar o fluxo de status (FP caro).
    for t in [
        "quero abrir um processo contra meu vizinho",
        "preciso processar uma empresa",
        "como faço pra entrar com uma ação?",
        "tenho um problema com meu plano de saúde",
        "vocês pegam caso de usucapião?",
        "bom dia, gostaria de uma consulta",
        "meu caso é o seguinte: quero processar alguém",
        "tenho um processo trabalhista pra abrir",
    ]:
        assert detectar_pergunta_status(t) is False, t


# --- extrair_documento -------------------------------------------------------

def test_extrai_cpf_do_sufixo_do_nome():
    assert extrair_documento("Fulano Teste - CPF: 123.456.789-00") == "12345678900"
    assert extrair_documento("Empresa X - CNPJ: 12.345.678/0001-90") == "12345678000190"
    assert extrair_documento("Beltrano Sem Documento") == ""


# --- construir_indice + consultar (vínculo telefone↔processo, SÓ por CPF) -----

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
    assert construir_indice_cliente_processo(client, conn) >= 1
    procs = consultar_processos_do_telefone(conn, "11999998888")
    assert len(procs) == 1
    assert procs[0]["process_number"] == "1000000-00.2024.8.26.0100"
    assert procs[0]["person_id"] == "p1"
    assert procs[0]["is_secret"] is False
    assert procs[0]["last_movement_date"] == "2026-06-10"
    conn.close()


def test_indice_casa_por_person_id():
    # Caminho FORTE primário: o id da parte Cliente == person_id da ficha.
    # (No /lawSuit/ real o cliente é {id, name, personOrigin}, sem CPF.)
    conn = connect(":memory:")
    run_migrations(conn)
    _seed_person(conn, "11999998888", "pid-123", "Fulano Teste", doc="")
    client = _FakeClient([{
        "processNumber": "1-1",
        "isSecret": False,
        "lastMovementDate": "2026-06-09",
        "persons": [{"id": "pid-123", "name": "Fulano Teste",
                     "personOrigin": "Cliente"}],
    }])
    assert construir_indice_cliente_processo(client, conn) >= 1
    procs = consultar_processos_do_telefone(conn, "11999998888")
    assert len(procs) == 1
    assert procs[0]["person_id"] == "pid-123"
    conn.close()


def test_id_desconhecido_e_sem_cpf_nao_vincula():
    # Cliente cuja ficha não está no person_index (id não bate) e sem CPF →
    # nenhum vínculo automático (cai em humano).
    conn = connect(":memory:")
    run_migrations(conn)
    _seed_person(conn, "11988887777", "p2", "Beltrano Souza Lima", doc="")
    client = _FakeClient([{
        "processNumber": "2-2",
        "isSecret": False,
        "persons": [{"id": "outra-ficha", "name": "Beltrano Souza Lima",
                     "personOrigin": "Cliente"}],
    }])
    construir_indice_cliente_processo(client, conn)
    assert consultar_processos_do_telefone(conn, "11988887777") == []
    conn.close()


def test_homonimo_so_vincula_o_cpf_certo():
    # Dois "João Silva" distintos (CPFs diferentes). O processo do A NUNCA
    # pode vincular ao telefone do B — esse era o bypass da revisão 15/jun.
    conn = connect(":memory:")
    run_migrations(conn)
    _seed_person(conn, "11911112222", "pA", "João Silva", "111.111.111-11")
    _seed_person(conn, "11933334444", "pB", "João Silva", "222.222.222-22")
    client = _FakeClient([{
        "processNumber": "A-1",
        "isSecret": False,
        "persons": [{"name": "João Silva - CPF: 111.111.111-11",
                     "personOrigin": "Cliente"}],
    }])
    construir_indice_cliente_processo(client, conn)
    assert len(consultar_processos_do_telefone(conn, "11911112222")) == 1  # A
    assert consultar_processos_do_telefone(conn, "11933334444") == []       # B: nada
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


def test_telefone_compartilhado_por_duas_fichas_fica_ambiguo():
    # Mesmo número em duas fichas (CPFs distintos) → consulta traz 2 pessoas →
    # classificar recusa autenticar.
    conn = connect(":memory:")
    run_migrations(conn)
    _seed_person(conn, "11912345678", "pA", "Ana", "111.111.111-11")
    _seed_person(conn, "11912345678", "pB", "Bia", "222.222.222-22")
    client = _FakeClient([
        {"processNumber": "A", "isSecret": False,
         "persons": [{"id": "pA", "name": "Ana", "personOrigin": "Cliente"}]},
        {"processNumber": "B", "isSecret": False,
         "persons": [{"id": "pB", "name": "Bia", "personOrigin": "Cliente"}]},
    ])
    construir_indice_cliente_processo(client, conn)
    procs = consultar_processos_do_telefone(conn, "11912345678")
    assert len({p["person_id"] for p in procs}) == 2
    assert classificar_atendimento(procs)["acao"] == "ambiguo"
    conn.close()


# --- classificar_atendimento -------------------------------------------------

def _p(num, secret, pid="pX"):
    return {"person_id": pid, "process_number": num, "is_secret": secret,
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


def test_classifica_ambiguo_quando_duas_pessoas():
    r = classificar_atendimento([_p("1", False, "pA"), _p("2", False, "pB")])
    assert r["acao"] == "ambiguo"


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


def test_alerta_ambiguo_avisa_homonimo():
    msg = alerta_ambiguo("5511912345678", "meu processo tem novidade?")
    assert "5511912345678" in msg
    assert "AMBÍGUO" in msg or "ambíguo" in msg.lower()
