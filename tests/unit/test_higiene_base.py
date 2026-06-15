"""Tests for the base-hygiene report job (higiene_base, roadmap 2.3)."""

from noviello_funil.higiene_base import analisar_base, montar_mensagem


def _p(id_, nome, *, email="", phone="", document=""):
    return {"id": id_, "name": nome, "email": email, "phone": phone,
            "document": document}


# --- completude --------------------------------------------------------------

def test_conta_fichas_incompletas():
    diag = analisar_base([
        _p("1", "A", email="a@x.com", phone="5511999990001", document="11111111111"),
        _p("2", "B"),  # tudo vazio
        _p("3", "C", email="c@x.com"),  # só email
    ])
    assert diag["total"] == 3
    assert diag["sem_email"] == 1     # só B
    assert diag["sem_telefone"] == 2  # B e C
    assert diag["sem_documento"] == 2 # B e C


# --- duplicatas por documento (mesma pessoa) ---------------------------------

def test_documento_repetido_e_duplicata():
    diag = analisar_base([
        _p("1", "João Silva", document="123.456.789-00"),
        _p("2", "Joao Silva (2)", document="12345678900"),  # mesmo CPF, formatado
        _p("3", "Outro", document="98765432100"),
    ])
    docs = diag["dup_documento"]
    assert len(docs) == 1
    assert set(docs[0]["nomes"]) == {"João Silva", "Joao Silva (2)"}


def test_documento_curto_nao_conta():
    # documento < 11 dígitos (RG/lixo) não vira chave de duplicata
    diag = analisar_base([
        _p("1", "A", document="123"),
        _p("2", "B", document="123"),
    ])
    assert diag["dup_documento"] == []


# --- duplicatas por email / telefone -----------------------------------------

def test_email_compartilhado_sinalizado():
    diag = analisar_base([
        _p("1", "Eliane", email="san@bol.com.br"),
        _p("2", "Nayara", email="SAN@bol.com.br"),  # mesmo email, case
        _p("3", "Sozinho", email="solo@x.com"),
    ])
    assert len(diag["dup_email"]) == 1
    assert len(diag["dup_email"][0]["nomes"]) == 2


def test_telefone_compartilhado_sinalizado():
    diag = analisar_base([
        _p("1", "A", phone="5511999990001"),
        _p("2", "B", phone="11999990001"),  # mesmo número, sem 55
    ])
    assert len(diag["dup_telefone"]) == 1


# --- nomes similares (duplicata por digitação) -------------------------------

def test_nomes_quase_iguais_viram_par():
    diag = analisar_base([
        _p("1", "Carla Rosana Donati Corio"),
        _p("2", "Carla Rosana Donatti Corio"),   # Donati x Donatti
        _p("3", "Pedro Alves Souza"),
    ])
    pares = diag["nomes_similares"]
    assert len(pares) == 1
    assert set(pares[0]) == {"Carla Rosana Donati Corio", "Carla Rosana Donatti Corio"}


def test_nomes_diferentes_nao_pareiam():
    diag = analisar_base([
        _p("1", "Pedro Alves Souza"),
        _p("2", "Marcia Lima Santos"),
    ])
    assert diag["nomes_similares"] == []


def test_primeiro_nome_sozinho_nao_pareia():
    # nome de 1 palavra não entra (casaria muita gente)
    diag = analisar_base([_p("1", "Maria"), _p("2", "Maria")])
    assert diag["nomes_similares"] == []


# --- montar_mensagem ---------------------------------------------------------

def test_mensagem_vazia_quando_base_limpa():
    diag = analisar_base([
        _p("1", "A", email="a@x.com", phone="5511999990001", document="11111111111"),
    ])
    # base sem duplicatas e completa → ainda reporta completude? não, silêncio
    assert montar_mensagem(diag) is None


def test_mensagem_traz_numeros_e_amostras():
    diag = analisar_base([
        _p("1", "João Silva", document="12345678900"),
        _p("2", "Joao Silva 2", document="12345678900"),
        _p("3", "Carla Donati Corio", email=""),
        _p("4", "Carla Donatti Corio", email=""),
        _p("5", "Sem nada"),
    ])
    msg = montar_mensagem(diag)
    assert "CPF" in msg or "documento" in msg.lower()
    assert "duplicat" in msg.lower() or "parecid" in msg.lower()
    assert "<" not in msg  # WhatsApp-safe
