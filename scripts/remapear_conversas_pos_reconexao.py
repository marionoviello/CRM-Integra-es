"""Re-mapeamento pós-reconexão do Fixo (incidente 2026-07-24).

Quando a conexão WhatsApp (Fixo) é refeita, o Jurichat recria TODAS as
conversas — os ids gravados em ``leads.jurichat_conversation_id`` morrem
(404 em massa) e cada lead que escreve de novo vira um cadastro NOVO
(duplicado) criado pelo webhook.

Este script religa cada conversa nova ao cadastro ANTIGO do mesmo telefone,
preservando estado/reunião/histórico, e apaga a duplicata:

  1. agrupa leads por ``contato_telefone``;
  2. par (antigo criado < corte, novo criado >= corte, ids diferentes) →
     transplanta ``jurichat_lead_id``/``jurichat_conversation_id`` do novo
     pro antigo, zera contadores de erro e o transcript_hash (o próximo
     tick reprocessa a conversa nova inteira);
  3. apaga a linha duplicada (e suas transições) — o UNIQUE de
     jurichat_lead_id exige apagar antes de transplantar;
  4. conversas dos canais de alerta do Mario (MARIO_CONVERSATION_ID) são
     puladas.

DRY-RUN por padrão (só imprime o plano). Aplica com ``--aplicar``.

Uso no VPS (corte = quando a reconexão aconteceu, UTC):
    cd /opt/noviello-funil-saude && .venv/bin/python scripts/remapear_conversas_pos_reconexao.py --corte 2026-07-24
    cd /opt/noviello-funil-saude && .venv/bin/python scripts/remapear_conversas_pos_reconexao.py --corte 2026-07-24 --aplicar
"""

import argparse

from noviello_funil.config import Settings
from noviello_funil.db import connect
from noviello_funil.outbound import split_conversation_ids


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corte", required=True,
                    help="data/hora UTC da reconexão (ISO, ex: 2026-07-24)")
    ap.add_argument("--aplicar", action="store_true",
                    help="executa de verdade (sem isso, só imprime o plano)")
    args = ap.parse_args()

    s = Settings()
    canais = set(split_conversation_ids(s.mario_conversation_id))
    conn = connect(s.database_path)

    telefones = [
        r["contato_telefone"] for r in conn.execute(
            "SELECT contato_telefone FROM leads "
            "GROUP BY contato_telefone HAVING COUNT(*) >= 2"
        )
    ]
    print(f"Telefones com cadastro duplicado: {len(telefones)}\n")

    pares = []
    for tel in telefones:
        rows = conn.execute(
            "SELECT * FROM leads WHERE contato_telefone = ? ORDER BY id",
            (tel,),
        ).fetchall()
        antigos = [r for r in rows if r["criado_em"] < args.corte]
        novos = [r for r in rows if r["criado_em"] >= args.corte]
        if not antigos or not novos:
            continue
        # mais recente de cada lado: o antigo com mais história, o novo vivo.
        antigo, novo = antigos[-1], novos[-1]
        if novo["jurichat_conversation_id"] in canais:
            print(f"  pulando {tel}: conversa nova é canal de alerta do Mario")
            continue
        if antigo["jurichat_conversation_id"] == novo["jurichat_conversation_id"]:
            continue
        pares.append((tel, antigo, novo))

    if not pares:
        print("Nada a remapear (nenhum par antigo+novo pós-corte).")
        return

    for tel, antigo, novo in pares:
        print(
            f"  {tel}: lead antigo #{antigo['id']} ({antigo['contato_nome']}, "
            f"estado={antigo['estado']}) <- conversa nova do lead #{novo['id']} "
            f"(conv {novo['jurichat_conversation_id']}); duplicata #{novo['id']} sera apagada"
        )

    if not args.aplicar:
        print(f"\nDRY-RUN: {len(pares)} pares. Re-rode com --aplicar pra executar.")
        return

    for tel, antigo, novo in pares:
        novo_lead_id = novo["jurichat_lead_id"]
        nova_conv = novo["jurichat_conversation_id"]
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "DELETE FROM transicoes WHERE lead_id = ?", (novo["id"],))
            conn.execute("DELETE FROM leads WHERE id = ?", (novo["id"],))
            conn.execute(
                "UPDATE leads SET jurichat_lead_id = ?, "
                "jurichat_conversation_id = ?, erro_atual = NULL, "
                "erro_consecutivo = 0, erro_alertado_em = NULL, "
                "ultimo_transcript_hash = NULL, "
                "atualizado_em = datetime('now') WHERE id = ?",
                (novo_lead_id, nova_conv, antigo["id"]),
            )
            conn.execute(
                "INSERT INTO transicoes (lead_id, estado_novo, motivo) "
                "VALUES (?, ?, ?)",
                (antigo["id"], antigo["estado"], "remap_pos_reconexao_fixo"),
            )
            conn.execute("COMMIT")
            print(f"  OK {tel}: #{antigo['id']} religado à conversa {nova_conv}")
        except Exception as exc:
            conn.execute("ROLLBACK")
            print(f"  FALHOU {tel}: {exc} — par pulado")

    print(f"\nConcluído: {len(pares)} pares processados.")


if __name__ == "__main__":
    main()
