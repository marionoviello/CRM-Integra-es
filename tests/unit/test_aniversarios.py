"""Tests for the daily birthday job (aniversarios)."""

import datetime

import httpx
import pytest

from noviello_funil.aniversarios import (
    buscar_aniversariantes,
    eh_aniversariante_hoje,
    montar_mensagem,
)

# --- eh_aniversariante_hoje -------------------------------------------------

def test_aniversario_bate_dia_e_mes():
    assert eh_aniversariante_hoje("1982-06-11", datetime.date(2026, 6, 11))
    assert eh_aniversariante_hoje(
        "1982-06-11T00:00:00.000Z", datetime.date(2026, 6, 11),
    )  # formato com timestamp


def test_aniversario_outro_dia_nao_bate():
    assert not eh_aniversariante_hoje("1982-06-12", datetime.date(2026, 6, 11))
    assert not eh_aniversariante_hoje("1982-07-11", datetime.date(2026, 6, 11))


def test_aniversario_invalido_ou_vazio():
    assert not eh_aniversariante_hoje(None, datetime.date(2026, 6, 11))
    assert not eh_aniversariante_hoje("", datetime.date(2026, 6, 11))
    assert not eh_aniversariante_hoje("não-data", datetime.date(2026, 6, 11))
    assert not eh_aniversariante_hoje("1982-99-99", datetime.date(2026, 6, 11))


def test_29_fevereiro_celebra_28_em_ano_nao_bissexto():
    # 2026 não é bissexto → 29/fev celebra em 28/fev
    assert eh_aniversariante_hoje("1996-02-29", datetime.date(2026, 2, 28))
    # 2028 é bissexto → 28/fev NÃO é o dia; 29/fev é
    assert not eh_aniversariante_hoje("1996-02-29", datetime.date(2028, 2, 28))
    assert eh_aniversariante_hoje("1996-02-29", datetime.date(2028, 2, 29))


# --- montar_mensagem ---------------------------------------------------------

def test_mensagem_tem_link_wame_e_sugestao():
    msg = montar_mensagem(
        [
            {"nome": "Sergio Tellini", "telefone": "5511988887777",
             "email": "s@x.com", "person_id": "P1"},
            {"nome": "Cátia Masullo", "telefone": "",
             "email": "catia@x.com", "person_id": "P2"},
        ],
        datetime.date(2026, 6, 11),
    )
    assert msg.startswith("🎂 *Aniversariantes de hoje* (qui, 11/jun)")
    assert "Sergio Tellini — https://wa.me/5511988887777" in msg
    # Sem telefone → cai pro email
    assert "Cátia Masullo — catia@x.com" in msg
    assert "feliz aniversário" in msg
    assert "<" not in msg  # WhatsApp-safe


# --- buscar_aniversariantes --------------------------------------------------

@pytest.mark.asyncio
async def test_buscar_filtra_pelo_birthdate(respx_mock):
    respx_mock.get("https://api.juridiq.com.br/person/").mock(
        return_value=httpx.Response(200, json={
            "data": [{"id": "P1"}, {"id": "P2"}],
            "totalResults": 2, "totalPages": 1,
        }),
    )
    respx_mock.get("https://api.juridiq.com.br/person/P1").mock(
        return_value=httpx.Response(200, json={
            "id": "P1", "name": "Aniversariante",
            "phone": "5511911112222", "birthDate": "1980-06-11",
        }),
    )
    respx_mock.get("https://api.juridiq.com.br/person/P2").mock(
        return_value=httpx.Response(200, json={
            "id": "P2", "name": "Outro Dia",
            "phone": "5511933334444", "birthDate": "1980-12-25",
        }),
    )

    client = httpx.Client(
        base_url="https://api.juridiq.com.br",
        headers={"x-juridiq-api-key": "jq-test"},
    )
    try:
        result = buscar_aniversariantes(client, datetime.date(2026, 6, 11))
    finally:
        client.close()

    assert len(result) == 1
    assert result[0]["nome"] == "Aniversariante"
    assert result[0]["telefone"] == "5511911112222"


# --- Email de parabéns -------------------------------------------------------

def test_email_parabens_assunto_e_corpo():
    from noviello_funil.aniversarios import montar_email_parabens
    assunto, texto, html = montar_email_parabens("cátia de lourdes masullo")
    assert assunto == "Feliz aniversário, Cátia! 🎉"
    assert "Cátia" in texto
    assert "Noviello Advocacia" in texto
    assert "#68192E" in html  # claret da marca
    # Relacionamento puro — sem CTA comercial (OAB)
    for proibido in ("contrat", "consulta grátis", "desconto", "promoç"):
        assert proibido not in texto.lower()


def test_email_parabens_nome_vazio_nao_quebra():
    from noviello_funil.aniversarios import montar_email_parabens
    assunto, texto, _ = montar_email_parabens("")
    assert "amigo(a)" in assunto or "amigo(a)" in texto


def test_enviar_email_sucesso_e_falha(monkeypatch):
    from noviello_funil import aniversarios as mod

    enviados = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def starttls(self):
            pass
        def login(self, u, p):
            pass
        def sendmail(self, de, para, corpo):
            enviados.append((de, para))

    monkeypatch.setattr(mod.smtplib, "SMTP", FakeSMTP)
    ok = mod.enviar_email_parabens(
        smtp_host="smtp.gmail.com", smtp_port=587,
        smtp_user="mario@noviello.adv.br", smtp_password="app-pass",
        from_name="Mario", destinatario="cliente@x.com", nome="Cliente",
    )
    assert ok is True
    assert enviados == [("mario@noviello.adv.br", ["cliente@x.com"])]

    class BrokenSMTP(FakeSMTP):
        def login(self, u, p):
            raise RuntimeError("auth failed")

    monkeypatch.setattr(mod.smtplib, "SMTP", BrokenSMTP)
    ok = mod.enviar_email_parabens(
        smtp_host="x", smtp_port=587, smtp_user="u", smtp_password="p",
        from_name="M", destinatario="c@x.com", nome="C",
    )  # MUST NOT raise
    assert ok is False


def test_mensagem_marca_quem_recebeu_email():
    msg = montar_mensagem(
        [
            {"nome": "Com Email", "telefone": "5511911112222",
             "email": "a@x.com", "person_id": "P1", "email_enviado": True},
            {"nome": "Sem Email", "telefone": "5511933334444",
             "email": "", "person_id": "P2"},
        ],
        datetime.date(2026, 6, 11),
    )
    assert "Com Email — https://wa.me/5511911112222 📧✅" in msg
    assert "Sem Email — https://wa.me/5511933334444" in msg
    assert "já recebeu email" in msg


# --- Arte por gênero (clara=mulheres, escura=homens) ------------------------

def test_escolher_arte_heuristica_nomes_br():
    from noviello_funil.aniversarios import ARTE_CLARA, ARTE_ESCURA, escolher_arte
    # Femininos clássicos (sufixo 'a' + exceções sem 'a')
    for nome in ("Maria Silva", "Cátia de Lourdes", "Madalena Vanda",
                 "Isabel Cristina", "Raquel", "ALINE SOUZA"):
        assert escolher_arte(nome) == ARTE_CLARA, nome
    # Masculinos (sem 'a' + exceções com 'a')
    for nome in ("Sergio Tellini", "Mario Noviello", "João Pedro",
                 "Luca Mendes", "Denis"):
        assert escolher_arte(nome) == ARTE_ESCURA, nome
    # Vazio/estranho → escura (fallback)
    assert escolher_arte("") == ARTE_ESCURA


def test_email_com_arte_usa_cid_e_multipart_related(monkeypatch, tmp_path):
    from noviello_funil import aniversarios as mod

    # Simula as artes existindo
    clara = tmp_path / "clara.png"
    escura = tmp_path / "escura.png"
    clara.write_bytes(b"\x89PNG\r\n\x1a\nfakeclara")
    escura.write_bytes(b"\x89PNG\r\n\x1a\nfakeescura")
    monkeypatch.setattr(mod, "ARTE_CLARA", clara)
    monkeypatch.setattr(mod, "ARTE_ESCURA", escura)

    corpos = []

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def starttls(self):
            pass
        def login(self, u, p):
            pass
        def sendmail(self, de, para, corpo):
            corpos.append(corpo)

    monkeypatch.setattr(mod.smtplib, "SMTP", FakeSMTP)
    ok = mod.enviar_email_parabens(
        smtp_host="h", smtp_port=587, smtp_user="u@x.com",
        smtp_password="p", from_name="Mario",
        destinatario="cliente@x.com", nome="Maria Silva",
    )
    assert ok
    corpo = corpos[0]
    assert "multipart/related" in corpo
    assert "Content-ID: <arte>" in corpo
    # O HTML vai base64-encoded no MIME — valida o cid no template cru
    _, _, html = mod.montar_email_parabens("Maria", com_arte=True)
    assert 'src="cid:arte"' in html
    # Arte CLARA pra Maria (base64 do conteúdo fake da clara presente)
    import base64
    assert base64.b64encode(b"\x89PNG\r\n\x1a\nfakeclara").decode() in \
        corpo.replace("\n", "")


def test_email_sem_arte_continua_funcionando(monkeypatch, tmp_path):
    """Assets ausentes (ex: antes do deploy das imagens) → versão texto."""
    from noviello_funil import aniversarios as mod
    monkeypatch.setattr(mod, "ARTE_CLARA", tmp_path / "nao-existe.png")
    monkeypatch.setattr(mod, "ARTE_ESCURA", tmp_path / "nao-existe2.png")

    corpos = []

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def starttls(self):
            pass
        def login(self, u, p):
            pass
        def sendmail(self, de, para, corpo):
            corpos.append(corpo)

    monkeypatch.setattr(mod.smtplib, "SMTP", FakeSMTP)
    ok = mod.enviar_email_parabens(
        smtp_host="h", smtp_port=587, smtp_user="u@x.com",
        smtp_password="p", from_name="M",
        destinatario="c@x.com", nome="Sergio",
    )
    assert ok
    assert "cid:arte" not in corpos[0]
