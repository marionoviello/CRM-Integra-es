# Design — #39 Redação de contrato por IA (caminho B)

Brainstorming 25/jun. Gera a minuta de contratos **atípicos** por IA (caminho B:
PDF que nós geramos), quando o template fixo da ZapSign (caminho A) não cobre o
caso. Híbrido: o template segue padrão; a IA entra só nos atípicos.

## Decisões (Mario, 25/jun)

1. **Escopo:** híbrido — template fixo (caminho A) é o padrão; IA (caminho B) só
   pros atípicos.
2. **Gatilho:** o Mario sinaliza "atípico" no comando de gerar contrato (reusa o
   gate "Mario comanda", #37). A IA nunca decide sozinha sair do template.
3. **Revisão:** IA redige → render PDF → Mario revisa o **PDF** → 1-toque aprova
   (→ assinatura) ou rejeita (→ re-comanda). Reusa a aprovação 1-toque (#35).
4. **Escopo da redação:** a IA redige **só** as cláusulas variáveis/atípicas
   (objeto + cláusula atípica). As 14 cláusulas fixas vetadas (disclosure de IA
   Recom CFOAB 001/2024, golpe do falso advogado, ZapSign, foro, etc.) vêm
   **intactas**. O **valor dos honorários é o que o Mario digitou** — a IA só
   redige a cláusula em volta, nunca precifica (trava existente).
5. **Lint OAB:** bloqueia o crítico (promessa de resultado → re-redige), sinaliza
   o duvidoso no review. Reusa `redacao.lint_contrato`/`lint_ok`.
6. **PDF:** papel timbrado completo da marca (logo, claret #68192E, rodapé
   CNPJ/OAB), via `reportlab` (puro-Python, sem dep de sistema no VPS).
7. **Fonte das cláusulas fixas:** o Mario passa o contrato atual → vira template
   versionado no repo (14 fixas + slots das variáveis).

## O que JÁ existe (reuso) vs NOVO

**Reuso (no repo):**
- `redacao.py` — lint OAB de contrato completo: `lint_contrato()`, `lint_ok()`,
  `contem_promessa_resultado()`, `_checar_honorarios()`, `_oab_pessoa()`.
- `escopos.py` — `resolver_escopo(tipo_caso)` (objetos dos casos padrão).
- `contrato.py` — `montar_corpo_upload()` (caminho B: `POST /docs/` com base64) +
  `montar_signatario()` já prontos.
- Aprovação 1-toque (#35) + gate "Mario comanda" (#37) + pós-assinatura (#36).

**Novo:**
- `clausulas_fixas` — asset versionado com as 14 cláusulas (do contrato do Mario).
- `redacao_ia.py` — Claude (structured output) redige objeto + cláusula atípica.
- `render_pdf.py` — `reportlab`, monta o PDF timbrado a partir das cláusulas.
- `reportlab` como dependência.
- Flag/parâmetro "atípico" no comando + estado/rota do caminho B.

## Fluxo de dados

```
Mario comanda (flag atípico) + honorários digitados
  → redacao_ia: Claude redige {objeto, clausula_atipica} (schema)
  → montar_minuta: 14 fixas (intactas) + objeto + atípica + honorários(Mario)
  → lint_contrato:
        crítico (promessa de resultado) → re-redige (teto _MAX_REDACAO)
        duvidoso → marca pro review
  → render_pdf (reportlab, timbrado) → base64
  → notify Mario: PDF + achados do lint + aprovar/rejeitar 1-toque (#35)
  → APROVOU → montar_corpo_upload → POST /docs/ (caminho B) → assinatura
        → webhook signed → pós-assinatura (#36, já no ar)
  → REJEITOU → volta pro Mario re-comandar com ajuste
```

## Tratamento de erro / edge

- **Lint em loop** (re-redação não converge) → teto `_MAX_REDACAO` → handoff
  "redige manual" pro Mario (nunca trava silencioso).
- **Claude falha** (timeout/parse) → não monta, avisa o Mario (fire-and-forget no
  canal interno).
- **PDF falha** → não envia, avisa; o contrato não vai pro cliente quebrado.
- **Honorários** nunca vêm da IA — o valor é injetado do input do Mario; teste
  garante que a IA não emite número de honorário.
- **OAB:** as 14 fixas nunca passam pela IA → cláusulas sensíveis não mudam.

## Testes (TDD)

- `redacao_ia`: schema força {objeto, clausula_atipica}; honorários ausente do
  output; verificação na API real (1 caso atípico) antes de ligar.
- `montar_minuta`: fixas intactas + ordem; honorários = valor do Mario.
- lint gating: crítico bloqueia + re-redige; duvidoso passa marcado; teto.
- `render_pdf`: gera PDF não-vazio; contém marca/OAB; cláusulas presentes.
- fluxo: atípico → PDF + review; aprovar → POST /docs/; rejeitar → re-comanda.
- Tudo gated por flag (default OFF, sandbox-first) + revisão adversarial antes do
  deploy (padrão das frentes/#36).

## Pré-requisito (gate da implementação)

O Mario envia o **contrato atual** (texto/.docx/PDF). Sem o corpo canônico das 14
cláusulas, o PDF do caminho B não tem conteúdo. A implementação começa quando o
contrato chegar e eu transformá-lo no template versionado (Mario valida).

## Cortes YAGNI (fase 1)

- NÃO edição livre do rascunho (aprovar/rejeitar basta; editar = rejeitar +
  re-comandar). Anotações/edição assistida ficam pra v2.
- NÃO IA detectando atipicidade (o Mario sinaliza).
- NÃO a IA reescrever cláusula fixa (só variáveis/atípicas).
- NÃO nada financeiro novo (Asaas/honorários seguem como hoje).
