"""Varredura ampla: leads SEM RESPOSTA do lead ha mais de 15 dias, ainda
abertos (nao arquivados), sem etiqueta e com telefone em formato valido.

Diferente da varredura "nunca respondeu" (2026-07-03): aqui entra quem JA
respondeu alguma vez mas esfriou (ultima mensagem INBOUND ha 15+ dias), alem
de quem nunca respondeu (usa a data de criacao da conversa nesse caso).

So LEITURA - nao manda nada. Uso no VPS:
    cd /opt/noviello-funil-saude && .venv/bin/python scripts/varredura_sem_resposta_15dias.py
"""

import asyncio
import re
from datetime import datetime, timezone

import httpx

from noviello_funil.config import Settings

INBOX_ID = "cmhphehs612ucpp0ilvlf0c9v"
INTEGRATION_ID = "cmnt2le7702tzqt0iyfd52uty"

# Ja mensageados na rodada de reengajamento de hoje - excluir (ainda na
# janela de 24h de espera pela resposta).
JA_REENGAJADOS = {
    "5511933542176", "5519989151551", "5511964770140",
    "5511981478154", "5511963736007", "5511963906553",
}

TELEFONE_VALIDO = re.compile(r"^55\d{10,11}$")


async def listar_todas_conversas(http, api_key, base_url):
    conversas = []
    page = 1
    while True:
        resp = await http.get(
            f"{base_url}/conversation",
            headers={"x-jurichat-api-key": api_key},
            params={
                "page": str(page), "limit": "100",
                "inboxId": INBOX_ID, "integrationId": INTEGRATION_ID,
                "showGroups": "true", "onlyGroups": "false",
                "showOnlyUnread": "false", "onlyArchived": "false",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data") or []
        conversas.extend(items)
        total_pages = int(data.get("totalPages") or 1)
        print(f"  pagina {page}/{total_pages} - {len(items)} conversas")
        if page >= total_pages or not items:
            break
        page += 1
    return conversas


async def ultima_msg_inbound(http, api_key, base_url, conv_id):
    """Retorna o messageAt da ultima mensagem INBOUND, ou None se nunca houve."""
    resp = await http.get(
        f"{base_url}/conversation/{conv_id}",
        headers={"x-jurichat-api-key": api_key},
    )
    if resp.status_code >= 400:
        return "erro"
    data = resp.json()
    msgs = (data.get("data") or data).get("messages") or []
    inbound = [m for m in msgs if m.get("direction") == "INBOUND"]
    if not inbound:
        return None
    return max(m.get("messageAt") or "" for m in inbound)


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


async def main() -> None:
    s = Settings()
    agora = datetime.now(timezone.utc)
    async with httpx.AsyncClient(timeout=30) as http:
        print("Buscando todas as conversas nao-arquivadas...")
        conversas = await listar_todas_conversas(http, s.jurichat_api_key, s.jurichat_base_url)
        conversas = [c for c in conversas if not c.get("isGroup")]

        # Filtros baratos primeiro (sem chamada extra): sem etiqueta, tel
        # valido, nao esta na lista de ja-reengajados.
        candidatas = []
        for c in conversas:
            pessoa = c.get("person") or {}
            tel = (pessoa.get("phoneNumber") or "").strip()
            if c.get("tags"):
                continue
            if tel in JA_REENGAJADOS:
                continue
            if not TELEFONE_VALIDO.match(tel):
                continue
            candidatas.append(c)
        print(f"\nTotal apos filtro barato (sem etiqueta, tel valido, nao ja-reengajado): {len(candidatas)}")

        # Pre-filtro: se ultima msg do lead foi ha <=15 dias, descarta sem
        # gastar chamada extra.
        precisa_checar = []
        para_relatorio = []
        for c in candidatas:
            last = c.get("lastMessage") or {}
            if last.get("direction") == "INBOUND":
                dt = parse_iso(last["messageAt"])
                dias = (agora - dt).days
                if dias > 15:
                    para_relatorio.append((c, dias, dt.isoformat()))
                # se <=15, ja sabemos que respondeu recente - descarta.
            else:
                # ultima msg e nossa (ou nunca respondeu) - precisa checar
                # o historico completo pra achar a ultima INBOUND real.
                precisa_checar.append(c)

        print(f"Ja decididas pelo lastMessage: {len(para_relatorio)} (>15 dias)")
        print(f"Precisam checar historico completo: {len(precisa_checar)}\n")

        for i, c in enumerate(precisa_checar, 1):
            resultado = await ultima_msg_inbound(http, s.jurichat_api_key, s.jurichat_base_url, c["id"])
            if resultado == "erro":
                pass  # nao afirma nada sem confirmar
            elif resultado is None:
                # nunca respondeu -> usa createdAt
                dt = parse_iso(c.get("createdAt") or agora.isoformat())
                dias = (agora - dt).days
                if dias > 15:
                    para_relatorio.append((c, dias, "nunca respondeu"))
            else:
                dt = parse_iso(resultado)
                dias = (agora - dt).days
                if dias > 15:
                    para_relatorio.append((c, dias, dt.isoformat()))
            if i % 20 == 0:
                print(f"  ...{i}/{len(precisa_checar)} checadas")
            await asyncio.sleep(0.1)

        para_relatorio.sort(key=lambda x: -x[1])
        print(f"\n=== {len(para_relatorio)} leads sem resposta ha mais de 15 dias (abertos, sem etiqueta, tel valido) ===")
        for c, dias, ultima in para_relatorio:
            pessoa = c.get("person") or {}
            print(
                f"{dias:>4}d  {pessoa.get('name', '(sem nome)'):<28} "
                f"{pessoa.get('phoneNumber', ''):<16} status={c.get('status', ''):<20} "
                f"ultima={ultima}"
            )


if __name__ == "__main__":
    asyncio.run(main())
