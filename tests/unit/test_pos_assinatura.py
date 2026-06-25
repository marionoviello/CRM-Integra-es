"""#36 (25/jun): testes do pós-assinatura (arquivo do PDF)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from noviello_funil.pos_assinatura import arquivar_pdf_assinado


@pytest.mark.asyncio
async def test_arquivar_pdf_grava_no_disco(tmp_path):
    zapsign = MagicMock()
    zapsign.download_signed_file = AsyncMock(return_value=b"%PDF-fake-bytes")

    caminho = await arquivar_pdf_assinado(
        zapsign, signed_file_url="https://zapsign/x.pdf",
        contrato_id=42, dir_base=str(tmp_path),
    )

    p = tmp_path / "contrato-42.pdf"
    assert caminho == str(p)
    assert p.exists()
    assert p.read_bytes() == b"%PDF-fake-bytes"
    zapsign.download_signed_file.assert_awaited_once_with("https://zapsign/x.pdf")


@pytest.mark.asyncio
async def test_arquivar_pdf_sem_url_retorna_none(tmp_path):
    zapsign = MagicMock()
    zapsign.download_signed_file = AsyncMock()

    caminho = await arquivar_pdf_assinado(
        zapsign, signed_file_url=None, contrato_id=1, dir_base=str(tmp_path),
    )

    assert caminho is None
    zapsign.download_signed_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_arquivar_pdf_download_falha_e_best_effort(tmp_path):
    zapsign = MagicMock()
    zapsign.download_signed_file = AsyncMock(side_effect=RuntimeError("URL expirou"))

    caminho = await arquivar_pdf_assinado(
        zapsign, signed_file_url="https://zapsign/x.pdf",
        contrato_id=1, dir_base=str(tmp_path),
    )

    assert caminho is None  # best-effort: não levanta


def test_montar_corpo_tarefa_abertura_sem_lawsuit():
    """#36: tarefa de abertura vai SÓ na Pessoa (personIds), nunca com lawSuitId
    (cliente recém-assinado não tem processo)."""
    from noviello_funil.pos_assinatura import montar_corpo_tarefa_abertura

    corpo = montar_corpo_tarefa_abertura(
        person_id="p-1", cliente_nome="Fulano Teste", tipo_caso="inventario",
        column_id="col-uuid", priority="Alta", initial_date="2026-06-25T16:00:00",
    )
    assert corpo["personIds"] == ["p-1"]
    assert "lawSuitId" not in corpo
    assert corpo["columnId"] == "col-uuid"
    assert corpo["initialDate"] == "2026-06-25T16:00:00"
    assert "Fulano Teste" in corpo["title"]


@pytest.mark.asyncio
async def test_criar_tarefa_abertura_sucesso_e_guardas():
    """#36: sucesso → task_id; sem column_id/person_id → skip; falha/erro → None
    (best-effort, NUNCA levanta)."""
    from noviello_funil.pos_assinatura import criar_tarefa_abertura

    base = dict(
        person_id="p-1", cliente_nome="Fulano Teste", tipo_caso="usucapiao",
        column_id="col-uuid", priority="Alta", initial_date="2026-06-25T16:00:00",
    )

    # sucesso
    j = MagicMock()
    j.create_task = AsyncMock(return_value=("t-99", "ok"))
    assert await criar_tarefa_abertura(j, **base) == "t-99"
    j.create_task.assert_awaited_once()

    # sem column_id → skip, não chama API
    j2 = MagicMock()
    j2.create_task = AsyncMock()
    assert await criar_tarefa_abertura(j2, **{**base, "column_id": ""}) is None
    j2.create_task.assert_not_awaited()

    # API devolve (None, detalhe 400) → None, não levanta
    j3 = MagicMock()
    j3.create_task = AsyncMock(return_value=(None, "http_400: lawSuitId required"))
    assert await criar_tarefa_abertura(j3, **base) is None

    # API levanta → None (best-effort)
    j4 = MagicMock()
    j4.create_task = AsyncMock(side_effect=RuntimeError("boom"))
    assert await criar_tarefa_abertura(j4, **base) is None


# --- F5: orquestração -------------------------------------------------------

from noviello_funil.contrato import criar_contrato  # noqa: E402
from noviello_funil.db import connect, run_migrations  # noqa: E402
from noviello_funil.pos_assinatura import processar_pos_assinatura  # noqa: E402


class _Settings:
    task_column_id = "col-uuid"
    task_priority = "Alta"
    mario_conversation_id = "conv-mario"


def _contrato_assinado(conn, *, telefone="5511999990000", person_id=None):
    c = criar_contrato(
        conn, cliente_nome="Fulano Teste", valor_honorarios="R$ 5.000",
        template_id="T1",
    )
    conn.execute(
        "UPDATE contrato SET estado='ASSINADO', cliente_telefone=?, "
        "cliente_email='c@x.com', person_id=?, tipo_caso='inventario', "
        "zapsign_doc_token='ZS-TOKEN' WHERE id=?",
        (telefone, person_id, c["id"]),
    )
    return c["id"]


@pytest.mark.asyncio
async def test_processar_pos_3_passos_e_idempotente(monkeypatch):
    conn = connect(":memory:")
    run_migrations(conn)
    cid = _contrato_assinado(conn, telefone="5511999990000", person_id=None)

    juridiq = MagicMock()
    juridiq.search_person_by_phone = AsyncMock(return_value=None)
    juridiq.create_person = AsyncMock(return_value={"id": "p-new"})
    juridiq.create_task = AsyncMock(return_value=("t-1", "ok"))
    zapsign = MagicMock()
    fake_arquivo = AsyncMock(return_value="data/contratos_assinados/contrato-1.pdf")
    notify = AsyncMock()
    monkeypatch.setattr("noviello_funil.pos_assinatura.arquivar_pdf_assinado", fake_arquivo)
    monkeypatch.setattr("noviello_funil.pos_assinatura.notify_mario", notify)

    await processar_pos_assinatura(
        conn, juridiq=juridiq, zapsign=zapsign, jurichat=MagicMock(),
        settings=_Settings(), contrato_id=cid, signed_file_url="https://z/x.pdf",
    )

    row = conn.execute(
        "SELECT intake_juridiq_em, person_id, arquivo_pdf_em, signed_file_path, "
        "tarefa_abertura_em, juridiq_task_id FROM contrato WHERE id=?", (cid,),
    ).fetchone()
    assert row["intake_juridiq_em"] and row["person_id"] == "p-new"
    assert row["arquivo_pdf_em"] and row["signed_file_path"].endswith("contrato-1.pdf")
    assert row["tarefa_abertura_em"] and row["juridiq_task_id"] == "t-1"
    assert notify.await_count == 1
    # initialDate tem que ser DATE pura 'YYYY-MM-DD' (não datetime ISO com tz) —
    # único formato aceito pelo POST /task/ do Juridiq.
    corpo = juridiq.create_task.await_args.args[0]
    assert "T" not in corpo["initialDate"]
    assert len(corpo["initialDate"]) == 10 and corpo["initialDate"][4] == "-"

    # 2ª reentrega → tudo já feito, nenhum passo re-roda, sem novo notify.
    await processar_pos_assinatura(
        conn, juridiq=juridiq, zapsign=zapsign, jurichat=MagicMock(),
        settings=_Settings(), contrato_id=cid, signed_file_url="https://z/x.pdf",
    )
    juridiq.create_person.assert_awaited_once()  # não duplicou
    juridiq.create_task.assert_awaited_once()
    assert notify.await_count == 1  # sem spam


@pytest.mark.asyncio
async def test_processar_pos_sem_telefone_pula_intake_e_tarefa(monkeypatch):
    conn = connect(":memory:")
    run_migrations(conn)
    cid = _contrato_assinado(conn, telefone="", person_id=None)

    juridiq = MagicMock()
    juridiq.create_person = AsyncMock()
    juridiq.create_task = AsyncMock()
    monkeypatch.setattr(
        "noviello_funil.pos_assinatura.arquivar_pdf_assinado",
        AsyncMock(return_value="data/contratos_assinados/contrato-1.pdf"),
    )
    notify = AsyncMock()
    monkeypatch.setattr("noviello_funil.pos_assinatura.notify_mario", notify)

    await processar_pos_assinatura(
        conn, juridiq=juridiq, zapsign=MagicMock(), jurichat=MagicMock(),
        settings=_Settings(), contrato_id=cid, signed_file_url="https://z/x.pdf",
    )

    row = conn.execute(
        "SELECT intake_juridiq_em, person_id, arquivo_pdf_em, tarefa_abertura_em "
        "FROM contrato WHERE id=?", (cid,),
    ).fetchone()
    # intake carimbado (pra não re-tentar em loop) mas SEM person_id
    assert row["intake_juridiq_em"] and row["person_id"] is None
    juridiq.create_person.assert_not_awaited()  # não criou sem telefone
    assert row["arquivo_pdf_em"]  # arquivo roda (independe de telefone)
    assert row["tarefa_abertura_em"] is None  # tarefa pulada (sem person_id)
    juridiq.create_task.assert_not_awaited()
    assert notify.await_count == 1  # resumo avisa "ficha pulada"


# --- F6: sweeper (retomada) -------------------------------------------------

from noviello_funil.scheduler import sweep_pos_assinatura  # noqa: E402
from noviello_funil.state import (  # noqa: E402
    list_contratos_pos_pendentes,
    marcar_pos_iniciado,
    marcar_pos_travado,
    registrar_tentativa_pos,
)


def test_list_contratos_pos_pendentes_filtros():
    """Sweep pega só ASSINADO + pos_iniciado + pendente + não-travado. Exclui
    pré-feature (não iniciado), tudo-feito, travado e intake-sem-telefone-feito."""
    conn = connect(":memory:")
    run_migrations(conn)
    # c1: iniciado + intake pendente → PEGA
    c1 = _contrato_assinado(conn, telefone="5511", person_id=None)
    marcar_pos_iniciado(conn, c1)
    # c2: PRÉ-FEATURE (não iniciado), tudo pendente → IGNORA
    _contrato_assinado(conn, telefone="5511", person_id=None)
    # c3: iniciado + tudo feito → IGNORA
    c3 = _contrato_assinado(conn, telefone="5511", person_id="p")
    marcar_pos_iniciado(conn, c3)
    conn.execute(
        "UPDATE contrato SET intake_juridiq_em=datetime('now'), "
        "arquivo_pdf_em=datetime('now'), tarefa_abertura_em=datetime('now') WHERE id=?",
        (c3,),
    )
    # c4: iniciado + travado → IGNORA
    c4 = _contrato_assinado(conn, telefone="5511", person_id=None)
    marcar_pos_iniciado(conn, c4)
    marcar_pos_travado(conn, c4)
    # c5: intake-pulado-sem-telefone (intake_em set, person NULL) + arquivo feito
    #     → NÃO pendente (tarefa exige person_id) → IGNORA (não loopa)
    c5 = _contrato_assinado(conn, telefone="", person_id=None)
    marcar_pos_iniciado(conn, c5)
    conn.execute(
        "UPDATE contrato SET intake_juridiq_em=datetime('now'), "
        "arquivo_pdf_em=datetime('now') WHERE id=?", (c5,),
    )
    ids = {r["id"] for r in list_contratos_pos_pendentes(conn, limite=10)}
    assert ids == {c1}


def test_marcadores_pos_sweep_set_once_e_incremento():
    conn = connect(":memory:")
    run_migrations(conn)
    c = _contrato_assinado(conn, telefone="5511", person_id=None)
    marcar_pos_iniciado(conn, c)
    t1 = conn.execute("SELECT pos_iniciado_em FROM contrato WHERE id=?", (c,)).fetchone()[0]
    marcar_pos_iniciado(conn, c)  # set-once (COALESCE) — não sobrescreve
    t2 = conn.execute("SELECT pos_iniciado_em FROM contrato WHERE id=?", (c,)).fetchone()[0]
    assert t1 == t2 and t1 is not None
    registrar_tentativa_pos(conn, c)
    registrar_tentativa_pos(conn, c)
    assert conn.execute("SELECT pos_tentativas FROM contrato WHERE id=?", (c,)).fetchone()[0] == 2
    marcar_pos_travado(conn, c)
    assert conn.execute("SELECT pos_travado_em FROM contrato WHERE id=?", (c,)).fetchone()[0] is not None


@pytest.mark.asyncio
async def test_sweep_pos_retoma_passo_que_falhou(monkeypatch):
    """1º sweep: intake falha (Juridiq fora) mas arquivo OK; 2º sweep: intake
    sucesso → tarefa criada. Prova a retomada que o webhook não dá."""
    conn = connect(":memory:")
    run_migrations(conn)
    cid = _contrato_assinado(conn, telefone="5511", person_id=None)
    marcar_pos_iniciado(conn, cid)  # simula que o webhook iniciou o pós

    juridiq = MagicMock()
    juridiq.search_person_by_phone = AsyncMock(return_value=None)
    juridiq.create_person = AsyncMock(side_effect=[RuntimeError("juridiq down"), {"id": "p-2"}])
    juridiq.create_task = AsyncMock(return_value=("t-1", "ok"))
    zapsign = MagicMock()
    zapsign.get_doc = AsyncMock(return_value={"signed_file": "https://z/fresh.pdf"})
    monkeypatch.setattr(
        "noviello_funil.pos_assinatura.arquivar_pdf_assinado",
        AsyncMock(return_value="data/contratos_assinados/contrato-1.pdf"),
    )
    monkeypatch.setattr("noviello_funil.pos_assinatura.notify_mario", AsyncMock())

    await sweep_pos_assinatura(
        get_db=lambda: conn, zapsign=zapsign, juridiq=juridiq,
        jurichat=MagicMock(), settings=_Settings(),
    )
    row = conn.execute(
        "SELECT intake_juridiq_em, arquivo_pdf_em, tarefa_abertura_em, pos_tentativas "
        "FROM contrato WHERE id=?", (cid,),
    ).fetchone()
    assert row["intake_juridiq_em"] is None  # intake falhou
    assert row["arquivo_pdf_em"] is not None  # arquivo (independente) OK
    assert row["tarefa_abertura_em"] is None  # sem person_id, tarefa pulada
    assert row["pos_tentativas"] == 1
    zapsign.get_doc.assert_awaited_with("ZS-TOKEN")  # re-buscou URL fresca

    await sweep_pos_assinatura(
        get_db=lambda: conn, zapsign=zapsign, juridiq=juridiq,
        jurichat=MagicMock(), settings=_Settings(),
    )
    row = conn.execute(
        "SELECT intake_juridiq_em, person_id, tarefa_abertura_em, pos_tentativas "
        "FROM contrato WHERE id=?", (cid,),
    ).fetchone()
    assert row["intake_juridiq_em"] and row["person_id"] == "p-2"
    assert row["tarefa_abertura_em"] and row["pos_tentativas"] == 2


@pytest.mark.asyncio
async def test_sweep_pos_escala_e_trava_apos_teto(monkeypatch):
    """No teto de tentativas com passo ainda pendente: trava (sai da fila) +
    alerta o Mario 1×."""
    conn = connect(":memory:")
    run_migrations(conn)
    cid = _contrato_assinado(conn, telefone="5511", person_id=None)
    marcar_pos_iniciado(conn, cid)
    conn.execute("UPDATE contrato SET pos_tentativas=4 WHERE id=?", (cid,))  # 1 abaixo do teto

    juridiq = MagicMock()
    juridiq.search_person_by_phone = AsyncMock(return_value=None)
    juridiq.create_person = AsyncMock(side_effect=RuntimeError("down"))  # intake sempre falha
    juridiq.create_task = AsyncMock()
    zapsign = MagicMock()
    zapsign.get_doc = AsyncMock(return_value={"signed_file": "x"})
    monkeypatch.setattr(
        "noviello_funil.pos_assinatura.arquivar_pdf_assinado", AsyncMock(return_value=None),
    )
    monkeypatch.setattr("noviello_funil.pos_assinatura.notify_mario", AsyncMock())
    notify_sched = AsyncMock()
    monkeypatch.setattr("noviello_funil.scheduler.notify_mario", notify_sched)

    await sweep_pos_assinatura(
        get_db=lambda: conn, zapsign=zapsign, juridiq=juridiq,
        jurichat=MagicMock(), settings=_Settings(),
    )
    row = conn.execute(
        "SELECT pos_tentativas, pos_travado_em FROM contrato WHERE id=?", (cid,),
    ).fetchone()
    assert row["pos_tentativas"] == 5  # 4+1 = teto
    assert row["pos_travado_em"] is not None  # travado
    notify_sched.assert_awaited_once()  # alertou o Mario

    # 2º sweep: travado → não é mais pego (não tenta de novo).
    await sweep_pos_assinatura(
        get_db=lambda: conn, zapsign=zapsign, juridiq=juridiq,
        jurichat=MagicMock(), settings=_Settings(),
    )
    assert juridiq.create_person.await_count == 1
