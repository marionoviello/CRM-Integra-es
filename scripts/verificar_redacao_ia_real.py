"""#39: verificação na API REAL do motor de redação por IA (caminho B).

Roda o motor completo (redação IA → minuta → lint OAB → render) num caso atípico
representativo e confere:
  - ok = True (o lint não bloqueou indevidamente);
  - o OBJETO redigido pela IA faz sentido e NÃO menciona honorário/promessa;
  - o PDF timbrado é gerado.

Salva o PDF em /tmp pra você baixar e conferir o visual. Não envia nada, não toca
ZapSign/Juridiq — só lê a chave do .env e chama o Claude.

Uso no VPS:
    cd /opt/noviello-funil-saude && .venv/bin/python scripts/verificar_redacao_ia_real.py
"""

import asyncio

from anthropic import AsyncAnthropic

from noviello_funil.caminho_b import gerar_minuta_atipica
from noviello_funil.config import Settings

_OUT = "/tmp/contrato_atipico_smoke.pdf"

_QUALIF = dict(
    cliente_nome="Fulano de Tal Teste",
    cliente_nacionalidade="brasileiro",
    cliente_estado_civil="casado",
    cliente_profissao="engenheiro",
    cliente_rg="12.345.678-9 SSP/SP",
    cliente_cpf="123.456.789-00",
    cliente_endereco="Rua Exemplo, 100, São Paulo/SP, CEP 01000-000",
    cliente_email="fulano@exemplo.com",
)
_CASO = (
    "Cliente quer ação de instituição de servidão de passagem forçada sobre o "
    "imóvel vizinho (encravamento do seu lote rural, sem saída para via pública), "
    "cumulada com indenização ao dono do prédio serviente."
)


async def main() -> None:
    s = Settings()
    client = AsyncAnthropic(api_key=s.anthropic_api_key)
    print(f"Modelo: {s.anthropic_model}")
    try:
        r = await gerar_minuta_atipica(
            client=client, model=s.anthropic_model, qualificacao=_QUALIF,
            descricao_caso=_CASO, honorarios_fixo="R$ 8.000,00 (oito mil reais)",
            honorarios_exito="10% (dez por cento)", data="26 de junho de 2026",
        )
        print(f"\nok = {r.ok}")
        print(f"bloqueios = {[b.regra for b in r.bloqueios]}")
        print(f"alertas   = {[a.regra for a in r.alertas]}")
        if r.ok:
            i = r.texto.find("DO OBJETO")
            print("\n--- OBJETO redigido pela IA (trecho) ---")
            print(r.texto[i + 30:i + 500].strip())
            with open(_OUT, "wb") as f:
                f.write(r.pdf)
            print(f"\n✅ PDF salvo em {_OUT} ({len(r.pdf)} bytes) — baixe pra conferir.")
        else:
            print("\n⚠️ Lint bloqueou (re-redação não limpou). Veja os bloqueios "
                  "acima — esperado SÓ se o caso forçar promessa de resultado.")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
