"""Valida o GATE do pós-assinatura (#36): o POST /task/ do Juridiq aceita uma
tarefa vinculada SÓ à Pessoa (personIds), SEM lawSuitId?

A tarefa de abertura do caso é criada quando o cliente assina — e nesse momento
ele AINDA NÃO tem processo (lawSuit). Este script confirma, contra a API real,
que a tarefa-só-na-Pessoa é aceita ANTES de ligar POS_ASSINATURA_ATIVO.

O que faz: cria uma Pessoa FICTÍCIA de teste, tenta criar a tarefa nela sem
lawSuit, e reporta o resultado. NÃO mexe em dados reais. Imprime os IDs criados
pra você apagar à mão no Juridiq (a API não expõe delete de pessoa/tarefa).

Uso no VPS:
    cd /opt/noviello-funil-saude && .venv/bin/python scripts/validar_tarefa_pos_assinatura.py
"""

import asyncio

from noviello_funil.config import Settings
from noviello_funil.juridiq_client import JuridiqClient
from noviello_funil.pos_assinatura import montar_corpo_tarefa_abertura

# Dados FICTÍCIOS (regra: nada real em teste).
_NOME_TESTE = "ZZ TESTE POS-ASSINATURA (apagar)"
_TEL_TESTE = "5500000000000"


async def main() -> None:
    settings = Settings()
    if not settings.juridiq_api_key:
        print("❌ JURIDIQ_API_KEY vazio no .env — configure antes de validar.")
        return
    if not settings.task_column_id:
        print("❌ TASK_COLUMN_ID vazio no .env — a tarefa precisa do UUID da "
              "coluna do kanban. Configure antes de validar.")
        return

    juridiq = JuridiqClient(settings.juridiq_api_key, settings.juridiq_base_url)
    try:
        print("1) Criando Pessoa fictícia de teste...")
        person = await juridiq.create_person(
            name=_NOME_TESTE, phone=_TEL_TESTE, email=None,
            annotation="Pessoa de teste do validador do pós-assinatura. APAGAR.",
        )
        person_id = person.get("id")
        print(f"   → person_id = {person_id}")
        if not person_id:
            print("❌ create_person não devolveu id — abortando.")
            return

        print("2) Tentando criar tarefa SÓ na Pessoa (sem lawSuit)...")
        corpo = montar_corpo_tarefa_abertura(
            person_id=person_id, cliente_nome=_NOME_TESTE, tipo_caso="teste",
            column_id=settings.task_column_id, priority=settings.task_priority,
            initial_date="2026-06-25",
        )
        task_id, detalhe = await juridiq.create_task(corpo)

        print("\n" + "=" * 56)
        if task_id:
            print(f"✅ ACEITA sem lawSuit — task_id = {task_id}")
            print("   → o caminho 'tarefa só na Pessoa' funciona. Pode seguir "
                  "pro smoke ponta-a-ponta e ligar POS_ASSINATURA_ATIVO.")
        else:
            print(f"❌ REJEITADA — detalhe: {detalhe}")
            print("   → a API NÃO aceita tarefa sem lawSuit. Me mande este "
                  "detalhe: caímos no plano B (anotação na Pessoa).")
        print("=" * 56)
        print(f"\n⚠️  APAGUE no Juridiq: Pessoa {person_id}"
              + (f" e Tarefa {task_id}" if task_id else ""))
    finally:
        await juridiq.aclose()


if __name__ == "__main__":
    asyncio.run(main())
