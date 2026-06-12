# CLAUDE.md — noviello-funil-saude

## Segredos e configuração (regra inegociável)

- TODO dado de configuração (chave de API, senha, token, ID de conta)
  fica FORA do código: somente variável de ambiente via `.env`, tipada
  em `src/noviello_funil/config.py` (pydantic-settings).
- NUNCA hardcodar segredo em código, teste, script ou doc versionada.
  Exemplo em docstring usa placeholder óbvio (`GOCSPX-...`, `1//0g...`).
- `.env` e `.env.*` são gitignored; só `.env.example` (placeholders) é
  versionado. Variável nova → adicionar no `.env.example` E no
  `config.py`, nunca com valor real.
- Smoke test com chave real: chave inline no ambiente
  (`VAR=... uv run ...`) ou no `.env` local — jamais colada em arquivo
  do repo.
- Planilhas/relatórios com PII de clientes ou dados de processos do
  escritório NUNCA são versionados (padrões já no `.gitignore`; na
  dúvida, adicione o arquivo lá ANTES de criá-lo).
- Dados reais de clientes não entram em teste: fixtures usam nomes e
  telefones fictícios ("Fulano Teste", `5500000000001`).
