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
        "cliente_email='c@x.com', person_id=?, tipo_caso='inventario' WHERE id=?",
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
