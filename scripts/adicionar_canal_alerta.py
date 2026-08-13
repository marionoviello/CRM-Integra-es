"""Adiciona uma conversa ao rol de canais de alerta (MARIO_CONVERSATION_ID).

Feito no caso Hilde (13/ago): o terminal web da Hostinger corrompe colagens
longas, e a listagem da Jurichat oscila de exigência de parâmetros — este
script encapsula a busca + edição do .env num comando curto.

Modos:
  --fone 5511996559867   procura a conversa pelo telefone na listagem,
                         tentando as variantes de parâmetros conhecidas
  --conversa <id>        usa o id direto (copiado da URL do painel; aceita
                         a URL inteira ou "id=...")
Opções:
  --testar               envia mensagem de teste no canal após adicionar

Edita o .env com backup em /root/. Depois rode:
  systemctl restart noviello-funil.service
(o scheduler oneshot pega o .env novo sozinho no próximo tick)
"""

import argparse
import datetime
import shutil
import sys

import httpx

from noviello_funil.config import Settings

# Integrações já vistas neste ambiente (a reconexão do Fixo recria) — a busca
# tenta sem filtro e com cada uma destas.
INTEGRACOES_CONHECIDAS = ["cmryywd6o00mrql0ibhvz90ma"]


def _listar(http, key, base, inbox, extra, pages=6):
    achadas = []
    for p in range(1, pages + 1):
        r = http.get(
            f"{base}/conversation",
            headers={"x-jurichat-api-key": key},
            params={"page": str(p), "limit": "100", "inboxId": inbox, **extra},
        )
        if r.status_code >= 400:
            return achadas
        d = r.json()
        items = d.get("data") or []
        achadas.extend(items)
        if p >= int(d.get("totalPages") or 1) or not items:
            break
    return achadas


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fone", default="")
    ap.add_argument("--conversa", default="")
    ap.add_argument("--testar", action="store_true")
    a = ap.parse_args()

    s = Settings()
    base = s.jurichat_base_url.rstrip("/")
    cid = a.conversa.strip()
    if "id=" in cid:
        cid = cid.split("id=")[-1].split("&")[0].strip()

    with httpx.Client(timeout=30) as http:
        if not cid:
            if not a.fone:
                print("use --fone <numero> ou --conversa <id>")
                sys.exit(1)
            completo = {
                "showGroups": "true", "onlyGroups": "false",
                "showOnlyUnread": "false", "onlyArchived": "false",
            }
            variantes = [("completa", completo)]
            variantes += [
                (f"completa+integ {i[-6:]}", {**completo, "integrationId": i})
                for i in INTEGRACOES_CONHECIDAS
            ]
            variantes.append(("minima", {}))
            for nome, extra in variantes:
                convs = _listar(http, s.jurichat_api_key, base,
                                s.jurichat_inbox_id, extra)
                print(f"variante {nome}: {len(convs)} conversas")
                for c in convs:
                    p = c.get("person") or {}
                    if (p.get("phoneNumber") or "").strip() == a.fone:
                        cid = c["id"]
                        print(f"ACHEI: {cid} | {p.get('name')}")
                        break
                if cid:
                    break
            if not cid:
                print("\nNAO ACHEI pela listagem (API oscilando). PLANO B:")
                print("abra a conversa da pessoa no painel do Jurichat, copie a")
                print("URL inteira da barra do navegador e rode de novo com:")
                print("  --conversa '<URL colada>' --testar")
                sys.exit(2)

        r = http.get(
            f"{base}/conversation/{cid}",
            headers={"x-jurichat-api-key": s.jurichat_api_key},
        )
        if r.status_code != 200:
            print(f"conversa {cid} respondeu HTTP {r.status_code} — confira o id")
            sys.exit(2)
        data = r.json()
        pessoa = (data.get("data") or data).get("person") or {}
        print(f"conversa OK: {cid} | {pessoa.get('name')} | {pessoa.get('phoneNumber')}")

        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy(".env", f"/root/env-backup-{stamp}")
        linhas = open(".env", encoding="utf-8").read().splitlines()
        saida, mexeu = [], False
        for ln in linhas:
            if ln.startswith("MARIO_CONVERSATION_ID="):
                atual = ln.split("=", 1)[1].strip().strip('"')
                ids = [x.strip() for x in atual.split(",") if x.strip()]
                if cid in ids:
                    print("ja estava no rol de alertas")
                else:
                    ids.append(cid)
                    ln = "MARIO_CONVERSATION_ID=" + ",".join(ids)
                    print("rol novo:", ln)
                mexeu = True
            saida.append(ln)
        if not mexeu:
            saida.append(f"MARIO_CONVERSATION_ID={cid}")
            print("linha MARIO_CONVERSATION_ID criada")
        open(".env", "w", encoding="utf-8").write("\n".join(saida) + "\n")

        if a.testar:
            http.post(
                f"{base}/conversation/start-human-support",
                headers={"x-jurichat-api-key": s.jurichat_api_key},
                json={"conversationId": cid, "isRandom": True},
            )
            r2 = http.post(
                f"{base}/conversation/send-message",
                headers={"x-jurichat-api-key": s.jurichat_api_key},
                files={
                    "conversationId": (None, cid),
                    "message": (None, "[teste tecnico] Alertas do funil "
                                      "religados - pode ignorar."),
                    "type": (None, "text"),
                },
            )
            print("teste de envio:", r2.status_code, r2.text[:120])

        print("\nFalta so: systemctl restart noviello-funil.service")


if __name__ == "__main__":
    main()
