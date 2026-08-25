"""Reproduz um erro de triagem AO VIVO pra um lead especifico — chama a MESMA
funcao triagem() da producao com a transcricao real, capturando o traceback
completo na hora (os logs do journald rotacionam rapido pelo volume de INFO
do httpx, entao cavar log historico nao e confiavel).

So LEITURA (GET na conversa + chamada Claude) - nao envia nada ao lead.

Uso no VPS:
    cd /opt/noviello-funil-saude && .venv/bin/python scripts/reproduzir_triagem_erro.py <JURICHAT_CONVERSATION_ID>
"""

import asyncio
import sys
import traceback

import httpx
from anthropic import AsyncAnthropic

from noviello_funil.brain import load_skill, triagem
from noviello_funil.config import Settings
from noviello_funil.outbound import JurichatClient


async def main() -> None:
    conv_id = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    if not conv_id:
        print("uso: reproduzir_triagem_erro.py <JURICHAT_CONVERSATION_ID>")
        return

    s = Settings()
    async with httpx.AsyncClient(timeout=30) as http:
        jurichat = JurichatClient(
            api_key=s.jurichat_api_key,
            base_url=s.jurichat_base_url,
            bot_user_id=s.jurichat_bot_user_id,
        )
        print(f"Buscando conversa {conv_id}...")
        conv = await jurichat.get_conversation(conv_id)
        transcript = conv.get("transcription", "") or ""
        print(f"Transcricao: {len(transcript)} chars, {len(transcript.splitlines())} linhas\n")
        print("=== ULTIMAS 15 LINHAS DA TRANSCRICAO ===")
        for linha in transcript.splitlines()[-15:]:
            print(f"  {linha[:150]}")
        print()

        skill_content = load_skill("atendente_geral")
        anthropic_client = AsyncAnthropic(api_key=s.anthropic_api_key)

        print("=== CHAMANDO triagem() AO VIVO ===")
        try:
            decisao = await triagem(
                client=anthropic_client,
                model=s.anthropic_model,
                skill_content=skill_content,
                conversation_transcript=transcript,
            )
            print("SUCESSO — decisao retornada:")
            print(f"  acao: {decisao.acao}")
            print(f"  mensagem: {decisao.mensagem[:300]!r}")
        except Exception:
            print("FALHOU — traceback completo:\n")
            traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
