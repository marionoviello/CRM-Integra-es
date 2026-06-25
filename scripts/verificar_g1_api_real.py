"""Verificação na API REAL do fix G1 (campo lead_recusou_videochamada).

Roda 2 triagens reais contra o schema novo (DECISAO_SCHEMA + structured outputs)
e confere se o modelo seta o campo corretamente:
  1. lead que RECUSA videochamada  → espera lead_recusou_videochamada = True
  2. lead DISPOSTO com restrição de horário → espera False (o bug do regex era
     marcar este como recusa)

Uso no VPS (uv não está no PATH do root):
    cd /opt/noviello-funil-saude && .venv/bin/python scripts/verificar_g1_api_real.py

Não grava nada, não envia nada — só lê a chave do .env e chama a API.
"""

import asyncio

from anthropic import AsyncAnthropic

from noviello_funil.brain import load_skill, triagem
from noviello_funil.config import Settings

CASO_RECUSA = (
    "Atendente: Posso te atender por videochamada (Google Meet)?\n"
    "Lead: olha, não quero videochamada não, prefiro ser atendido "
    "presencialmente no escritório. Meu email é joao@exemplo.com"
)

CASO_DISPOSTO_COM_RESTRICAO = (
    "Atendente: Posso te atender por videochamada (Google Meet)?\n"
    "Lead: pode sim! Só não consigo de manhã, videochamada de tarde tá ótimo. "
    "Meu email é maria@exemplo.com"
)


async def _rodar(client, model, skill, titulo, transcript, espera):
    d = await triagem(
        client=client,
        model=model,
        skill_content=skill,
        conversation_transcript=transcript,
    )
    ok = d.lead_recusou_videochamada is espera
    print(f"\n=== {titulo} ===")
    print(f"  acao: {d.acao}")
    print(f"  lead_recusou_videochamada: {d.lead_recusou_videochamada} "
          f"(esperado: {espera}) {'✅' if ok else '❌'}")
    print(f"  mensagem: {d.mensagem[:120]!r}")
    return ok


async def main():
    settings = Settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    skill = load_skill("atendente_geral")
    model = settings.anthropic_model
    print(f"Modelo de triagem: {model}")

    r1 = await _rodar(
        client, model, skill, "RECUSA de videochamada",
        CASO_RECUSA, espera=True,
    )
    r2 = await _rodar(
        client, model, skill, "DISPOSTO (só restrição de horário)",
        CASO_DISPOSTO_COM_RESTRICAO, espera=False,
    )

    print("\n" + "=" * 50)
    if r1 and r2:
        print("✅ OK — schema aceito pela API e modelo seta o campo certo. "
              "Pode reiniciar o serviço.")
    else:
        print("❌ ATENÇÃO — o modelo errou o campo em algum caso. NÃO reinicie; "
              "me mande a saída acima.")


if __name__ == "__main__":
    asyncio.run(main())
