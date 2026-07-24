"""Re-mapeamento pós-reconexão v2 — via LISTAGEM da API (2026-07-24).

Quando o Jurichat restaura o histórico depois de uma reconexão do Fixo, as
conversas reaparecem com IDs NOVOS — todos os leads já têm conversa nova
AGORA. Este script religa todo mundo de uma vez, sem esperar cada lead
escrever (diferente do remapear_conversas_pos_reconexao.py, que depende
das duplicatas do webhook):

  1. busca a conversa do canal de alertas (MARIO_CONVERSATION_ID, já
     atualizado no .env) e extrai dela o integrationId + inboxId NOVOS;
  2. lista TODAS as conversas da integração nova (paginado);
  3. monta o mapa telefone -> (conversa nova, person novo);
  4. para cada lead cujo telefone casa: transplanta os ids novos, zera
     contadores de erro e o transcript_hash (o próximo tick reprocessa; a
     guarda de última-linha-Atendente evita resposta a conversa parada);
  5. se uma linha DUPLICADA (criada pelo webhook durante a janela) já usa o
     jurichat_lead_id novo, apaga a duplicata antes (UNIQUE).

DRY-RUN por padrão. Aplica com ``--aplicar``. Canais de alerta são pulados.

Uso no VPS:
    cd /opt/noviello-funil-saude && .venv/bin/python scripts/remapear_por_listagem.py
    cd /opt/noviello-funil-saude && .venv/bin/python scripts/remapear_por_listagem.py --aplicar
"""

import argparse
import asyncio

import httpx

from noviello_funil.config import Settings
from noviello_funil.db import connect
from noviello_funil.outbound import split_conversation_ids


async def _get(http: httpx.AsyncClient, key: str, url: str, **params):
    r = await http.get(url, headers={"x-jurichat-api-key": key}, params=params or None)
    r.raise_for_status()
    return r.json()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aplicar", action="store_true")
    ap.add_argument("--conversa", default="",
                    help="ID de QUALQUER conversa viva do painel — fonte do "
                         "integrationId/inboxId novos. Sem ele, usa o 1º "
                         "MARIO_CONVERSATION_ID do .env (que precisa já estar "
                         "atualizado pro ID novo).")
    args = ap.parse_args()

    s = Settings()
    canais = split_conversation_ids(s.mario_conversation_id)
    fonte = args.conversa.strip() or (canais[0] if canais else "")
    if "id=" in fonte:
        fonte = fonte.split("id=")[-1].split("&")[0].strip()
    if not fonte:
        print("Passe --conversa <id> ou configure MARIO_CONVERSATION_ID.")
        raise SystemExit(1)
    base = s.jurichat_base_url.rstrip("/")

    async with httpx.AsyncClient(timeout=30) as http:
        # 1. inbox extraído do detalhe de uma conversa viva qualquer.
        #    (Formato REAL verificado 2026-07-24: o detalhe traz só ``inboxId``
        #    — o objeto ``integration`` aparece apenas na LISTAGEM.)
        d = await _get(http, s.jurichat_api_key, f"{base}/conversation/{fonte}")
        data = d.get("data") or d
        inbox = data.get("inboxId") or (data.get("inbox") or {}).get("id") or ""
        if not inbox:
            print(f"Nao achei inboxId na conversa {fonte} — "
                  "confira se o ID e de uma conversa viva do painel.")
            raise SystemExit(1)
        print(f"Inbox: {inbox}\n")

        # 2. lista todas as conversas do inbox (SEM filtro de integração —
        #    a listagem inboxId-only volta a funcionar pós-reconexão e a
        #    integração antiga lista 0, então tudo que vem é da nova).
        conversas: list[dict] = []
        page = 1
        while True:
            d = await _get(
                http, s.jurichat_api_key, f"{base}/conversation",
                page=str(page), limit="100", inboxId=inbox,
            )
            items = d.get("data") or []
            conversas.extend(items)
            total = int(d.get("totalPages") or 1)
            print(f"  pagina {page}/{total} — {len(items)} conversas")
            if page >= total or not items:
                break
            page += 1
        if not conversas:
            print("Listagem vazia — nada a remapear.")
            raise SystemExit(1)

        # 2b. fixa a integração MAJORITÁRIA e filtra por ela — se alguma
        #     conversa morta de integração velha vazar na listagem, não entra.
        contagem: dict[str, int] = {}
        for c in conversas:
            iid = (c.get("integration") or {}).get("id") or ""
            contagem[iid] = contagem.get(iid, 0) + 1
        integ = max(contagem, key=lambda k: contagem[k])
        print(f"Integracao majoritaria: {integ} "
              f"({contagem[integ]}/{len(conversas)} conversas)")
        conversas = [
            c for c in conversas
            if ((c.get("integration") or {}).get("id") or "") == integ
        ]

        # 3. telefone -> conversa nova (a mais recente ganha, ordem da API).
        por_tel: dict[str, dict] = {}
        for c in conversas:
            if c.get("isGroup"):
                continue
            pessoa = c.get("person") or {}
            tel = (pessoa.get("phoneNumber") or "").strip()
            if tel and tel not in por_tel:
                por_tel[tel] = c
        print(f"\nTelefones na listagem nova: {len(por_tel)}")

        conn = connect(s.database_path)
        leads = conn.execute(
            "SELECT * FROM leads ORDER BY id"
        ).fetchall()
        planos, sem_match = [], 0
        canais_set = set(canais)
        for lead in leads:
            if lead["jurichat_conversation_id"] in canais_set:
                continue
            nova = por_tel.get((lead["contato_telefone"] or "").strip())
            if nova is None:
                sem_match += 1
                continue
            nova_conv = nova["id"]
            if nova_conv in canais_set or nova_conv == lead["jurichat_conversation_id"]:
                continue
            novo_person = (nova.get("person") or {}).get("id") or ""
            planos.append((lead, nova_conv, novo_person))

        print(f"Leads a religar: {len(planos)} | sem conversa nova: {sem_match}\n")
        for lead, nova_conv, _novo_person in planos:
            print(f"  #{lead['id']} {lead['contato_nome'] or '(sem nome)'} "
                  f"({lead['contato_telefone']}, estado={lead['estado']}) -> {nova_conv}")

        if not args.aplicar:
            print("\nDRY-RUN: re-rode com --aplicar pra executar.")
            return

        ok = 0
        for lead, nova_conv, novo_person in planos:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # duplicata do webhook segurando o person novo? apaga antes.
                dup = conn.execute(
                    "SELECT id FROM leads WHERE jurichat_lead_id = ? AND id != ?",
                    (novo_person, lead["id"]),
                ).fetchone()
                if dup is not None:
                    conn.execute("DELETE FROM transicoes WHERE lead_id = ?", (dup["id"],))
                    conn.execute("DELETE FROM leads WHERE id = ?", (dup["id"],))
                conn.execute(
                    "UPDATE leads SET jurichat_lead_id = ?, "
                    "jurichat_conversation_id = ?, erro_atual = NULL, "
                    "erro_consecutivo = 0, erro_alertado_em = NULL, "
                    "ultimo_transcript_hash = NULL, "
                    "atualizado_em = datetime('now') WHERE id = ?",
                    (novo_person or lead["jurichat_lead_id"], nova_conv, lead["id"]),
                )
                conn.execute(
                    "INSERT INTO transicoes (lead_id, estado_novo, motivo) "
                    "VALUES (?, ?, ?)",
                    (lead["id"], lead["estado"], "remap_listagem_pos_reconexao"),
                )
                conn.execute("COMMIT")
                ok += 1
            except Exception as exc:
                conn.execute("ROLLBACK")
                print(f"  FALHOU #{lead['id']}: {exc}")
        print(f"\nConcluido: {ok}/{len(planos)} religados.")


if __name__ == "__main__":
    asyncio.run(main())
