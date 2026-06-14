# Roadmap de Automações — Noviello Funil (Fase 2)

> Análise consolidada das 68 propostas dos 6 analistas. Removi duplicatas, descartei o que já roda em produção e o que esbarra na OAB/LGPD, e ordenei por impacto × esforço. Tudo aqui respeita as três regras de ouro do escritório: **comunicação com cliente é sempre humana e sóbria** (nunca captação), **dado sensível nunca sai dos canais internos**, e **o bot nunca cita "Dr. Mario" individualmente**.

---

## Resumo executivo — as 5 de maior impacto

O sistema hoje é excelente captando leads novos. Mas ele **não enxerga a carteira de 284 processos nem as 1.464 pessoas que já são clientes**. As cinco recomendações abaixo fecham exatamente esse vão — transformam o bot de "máquina de agendar leads" em "copiloto de toda a operação". São também as de melhor relação valor/esforço:

1. **Prazo detectado vira tarefa rastreável no Juridiq** (não some no WhatsApp). Hoje o robô avisa "tem prazo" e pronto — se ninguém anotar, perdeu. Esta automação cria automaticamente uma tarefa com data e dono no painel. **É a defesa direta contra o erro mais caro e mais processável de um escritório: perder prazo.** Esforço pequeno, reusa o que já existe.

2. **Vigia se o monitoramento do Juridiq falhou** (cruzamento semanal com o DataJud do CNJ). Já aconteceu na carteira: o Juridiq para de capturar andamentos sem ninguém perceber. Esta é a "rede de segurança da rede de segurança". O script já existe (roda à mão hoje) — só falta virar automático.

3. **Radar de prescrição e processos parados.** Levanta a bandeira quando um processo fica parado tempo demais — o sinal clássico de risco de prescrição intercorrente, que faz o cliente perder o direito e o escritório responder por isso. Detecta cedo, a tempo de agir.

4. **Lembrete automático de audiências para a equipe** (e, com aprovação humana, para o cliente). Audiência perdida = revelia/preclusão. Hoje o bot só lembra de reuniões comerciais; as audiências judiciais são invisíveis para ele. Reusa o mecanismo de lembrete que já é maduro e testado.

5. **O bot reconhece quem já é cliente** (em vez de tratar como lead frio). Quando um cliente da casa manda mensagem, o robô para de pedir e-mail e qualificar — saúda pelo nome e leva direto para a equipe com o contexto do processo. Elimina um constrangimento real e abre caminho para responder "como está meu processo?".

**Pré-requisito técnico transversal:** quase tudo acima depende de casar telefone do WhatsApp com a ficha do Juridiq de forma confiável — o que hoje falha por causa de um defeito conhecido da API. A primeira coisa a construir é um índice telefone→ficha (Onda 0), barato e que destrava todo o resto.

---

## Como ler este documento

- **Esforço:** P = poucas horas · M = 1-2 dias · G = semana ou mais
- ⭐ = *quick win* (alto valor, esforço P)
- **Ondas:** Onda 0 = base técnica · Onda 1 = fazer já · Onda 2 = depois · Onda 3 = ambicioso

---

# ONDA 0 — Base técnica (destrava o resto)

### 0.1 ⭐ Índice telefone→ficha e resolução robusta de pessoa
**O que faz:** Cria um helper único que, dado um telefone, acha a ficha certa no Juridiq — mesmo com o defeito conhecido (`/person/search` devolve erro 400 quando não acha, e a listagem não filtra por telefone). Solução: índice telefone→id em SQLite, repovoado de madrugada pelo scan que o job de aniversários **já faz hoje** (varre a base inteira 1x/dia — desperdiçado fora do aniversário). Centraliza a normalização de número brasileiro (com/sem 55, com/sem 9º dígito).
**Endpoints:** `GET /person/` (scan, já feito), `GET /person/{id}`, `GET /person/search`.
**Por que importa:** Sem isto, toda automação que liga WhatsApp ↔ cliente é frágil. Hoje o intake já duplica fichas por causa desse defeito. É a peça de fundação mais barata e mais alavancadora do plano.
**Risco:** O índice fica levemente desatualizado entre scans — aceitável para relacionamento (não para cobrança). Telefone só em base protegida/gitignored.

### 0.2 Descobrir e fixar os IDs estruturais do Juridiq (.env)
**O que faz:** Tarefa de campo, não código: descobrir os IDs de coluna do kanban ("A Fazer"), o mapeamento processo→advogado responsável, o enum de status de tarefa ("concluída") e o significado exato de `itemType` no `/event/`. Vários itens das ondas seguintes dependem disso.
**Por que importa:** É o que separa "ideia" de "implementável". Meia hora com o Mario no painel destrava metade da Onda 1.
**Risco:** Nenhum. Só leitura/inspeção.

---

# ONDA 1 — Fazer já (segurança jurídica + ganhos imediatos)

## Tema A — Blindagem de prazos e processos (o risco nº 1 do escritório)

### 1.1 ⭐ Prazo de publicação urgente vira tarefa no Juridiq
*(Funde brutas #1, #14, #60)*
**O que faz:** O job de publicações já classifica urgência e extrai o prazo — mas o prazo só vira texto no WhatsApp. Esta automação fecha o loop: quando há prazo, cria uma **Tarefa** no Juridiq (`title="PRAZO: <ato> — <processo>"`, `finalDate` calculada, `priority=alta`, vinculada ao processo e ao advogado responsável), com o teor na descrição. A publicação só é marcada como lida depois da tarefa criada. Idempotência por `publication_id` para nunca duplicar.
**Endpoints:** `GET /publication/` (já usado), `POST /task/`, `GET /lawSuit/?processNumber=`, `PATCH /publication/read-movements-many`.
**Por que importa:** Transforma um aviso volátil ("vi no WhatsApp e esqueci") em item de trabalho com data e dono no painel. **Reduz diretamente o risco de perda de prazo** — responsabilidade civil e disciplinar do advogado.
**Risco (importante):** A data é uma **estimativa da IA** — a tarefa sai marcada *"prazo sugerido, conferir contagem no painel"*, com folga (buffer). Dias úteis × corridos, suspensões e feriados forenses variam; jamais apresentar como cálculo oficial.

### 1.2 Sincronização DataJud → Juridiq como vigia semanal
*(brutas #2)*
**O que faz:** Promove o script de auditoria que já existe (cruza os 284 processos com o DataJud do CNJ e classifica "monitoramento falhou / em dia / parado") de manual-com-Excel para job automático semanal. Em vez de planilha, manda ao Mario um resumo **só dos casos acionáveis** ("X processos com o tribunal à frente do Juridiq: ..."). Guarda o último resultado por processo para só avisar quando **muda** (não repete a mesma lista toda semana).
**Endpoints:** `GET /lawSuit/`, DataJud público, opcional `POST /task/`, `notify_mario`.
**Por que importa:** Cobre o ponto cego mais perigoso já observado na carteira: o monitoramento do Juridiq parar de capturar andamentos **em silêncio**. Base já validada (303→284 em jun/12).
**Risco:** Baixo. Rate-limit do DataJud já tratado (0,6s); 284 processos ≈ 3 min. Só número de processo no resumo, sem outros dados pessoais.

### 1.3 Radar de prescrição intercorrente / processos parados
*(brutas #3)*
**O que faz:** No mesmo cruzamento DataJud, isola processos sem movimentação acima de faixas (ex.: 11 meses = "vigiar"; 2 anos+ = "agir"), priorizando execuções/cumprimentos (onde a prescrição intercorrente mais morde). Digest mensal ao Mario; para os "vermelhos", abre tarefa "Avaliar prescrição / impulso — <nº>, parado há N dias".
**Endpoints:** `GET /lawSuit/{id}` (tipo de ação), DataJud, `POST /task/`, `notify_mario`.
**Por que importa:** Prescrição é uma das maiores fontes de perda de direito do cliente e de ação contra o escritório. Detectar "parado há N meses" com antecedência tem valor jurídico e defensivo altíssimo.
**Risco:** A faixa de tempo é **triagem, não parecer** — o aviso diz "avaliar", nunca "prescreveu". Arquivamentos legítimos geram falso-positivo; por isso é revisão humana.

## Tema B — Audiências (ato de maior impacto, hoje invisível ao bot)

### 1.4 ⭐ Lembrete de audiência para a equipe (D-1 e 2h antes)
*(Funde brutas #6, #13, #40 — a parte interna)*
**O que faz:** Job que lê `GET /audience/` e dispara ao canal da equipe lembretes escalonados das audiências dos próximos dias (D-1 às 17h e 2h antes, já com o link se for online ou o endereço se presencial), anexando processo e fase. Reusa o padrão de lembrete 24h/2h/30min já testado. Inclui um *red flag* "audiência sem responsável atribuído".
**Endpoints:** `GET /audience/?start&end`, `GET /lawSuit/{id}`, `GET /user/`, `notify_mario`. SQLite para idempotência.
**Por que importa:** Audiência perdida é o erro mais caro (revelia, preclusão, dano ao cliente, risco disciplinar). Hoje o recurso `/audience/` está **100% ocioso** e o bot só lembra de reuniões comerciais.
**Risco:** Confirmar se o Juridiq já manda lembrete nativo, para não duplicar (mesma lição do job de publicações). Só canal interno.

### 1.5 Confirmação de presença em audiência pelo cliente (com humano no loop)
*(Funde brutas #44, #67 — a parte do cliente)*
**O que faz:** Para audiências próximas, o bot **prepara um rascunho** de lembrete sóbrio ao cliente (data, hora, modalidade, local/link, "responda SIM para confirmar") e manda esse rascunho ao WhatsApp do Mario para aprovar/enviar. Onde houver casamento confiável de telefone, pode enviar e capturar a resposta; na dúvida, cai em `notify_mario` com link `wa.me` pronto (mesmo padrão dos aniversários).
**Endpoints:** `GET /audience/`, resolução de pessoa (item 0.1), Jurichat send/start_human_support, `notify_mario`.
**Por que importa:** Reduz não-comparecimento do cliente (que gera revelia/preclusão). Comunicação a cliente sobre o próprio caso é relacionamento permitido pela OAB.
**Risco (alto se automatizado):** Telefone errado = quebra de sigilo. Por isso: casamento de telefone rigoroso, **só ao próprio cliente**, tom sóbrio, "nossa equipe". Pular processos em segredo de justiça.

## Tema C — O bot reconhece a base de clientes

### 1.6 ⭐ Detecção de cliente existente vs. lead novo no primeiro contato
*(Funde brutas #49, #61-a)*
**O que faz:** Antes de tratar uma conversa como lead frio, cruza o telefone com o índice do item 0.1. Se for **cliente ativo**: o bot não qualifica nem pede e-mail — saúda pelo nome e faz handoff direto à equipe com contexto ("cliente ativo retornou", processos anexados). Na dúvida, mantém o comportamento atual (qualifica).
**Endpoints:** índice do 0.1, `GET /person/{id}`, `GET /lawSuit/?person=`, Jurichat.
**Por que importa:** Elimina o erro constrangedor de tratar quem já é da casa como estranho. Cliente percebe continuidade ("lembram de mim"). É a porta de entrada para o item 2.x ("como está meu processo?").
**Risco:** Match por telefone é dica, não certeza — na dúvida, fluxo atual. Sem risco OAB.

### 1.7 Detecção de conflito de interesse no primeiro contato
*(brutas #61-b)*
**O que faz:** Quando entra um lead novo, verifica se o nome/documento aparece como **parte contrária** em algum processo do escritório. Se sim, levanta flag de **possível conflito de interesse** e faz handoff imediato à equipe (sem agendar), avisando para análise. O sistema só **suspeita**; a decisão é humana.
**Endpoints:** `GET /lawSuit/?fullQuery=<nome>`, `GET /lawSuit/{id}` (papel da parte), Claude para matching de nome.
**Por que importa:** Detecção de impedimento é proteção **ética de altíssimo valor** (Código de Ética) — hoje depende da memória do advogado. Um lead que é o réu de outro cliente é um problema sério se passar batido.
**Risco:** Homonímia gera falso-positivo — só levanta suspeita. **Nunca revelar ao lead** que ele apareceu como parte contrária (isso é só para a equipe).

## Tema D — Produtividade e supressão (proteções rápidas)

### 1.8 ⭐ Agenda do dia consolidada para a equipe (manhã) + fechamento (fim de tarde)
*(Funde brutas #9, #12, #20, #43)*
**O que faz:** Um único cartão matinal (≈7h45) — "Bom dia, sua carteira hoje": audiências e perícias da semana, prazos detectados ontem, andamentos críticos novos, contagem de processos em alerta. E uma contraparte vespertina (18h30): "ainda abertas hoje; amanhã X audiências + Y prazos". Os jobs especializados continuam, mas escrevem num estado comum e este é o ponto único de leitura.
**Endpoints:** `GET /audience/`, `GET /event/`, `GET /task/`, estado SQLite dos outros jobs, `notify_mario`.
**Por que importa:** Reduz a fadiga de notificação (problema real quando vários alertas se somam) e dá visão da operação em 30 segundos. Aumenta a chance de cada alerta ser de fato lido.
**Risco:** Pequena refatoração (jobs gravam em estado comum). Cap por bloco para não virar texto gigante. Só canal interno.

### 1.9 ⭐ Follow-up de tarefas e prazos vencendo (SLA interno)
*(Funde brutas #15, #35)*
**O que faz:** Job diário que lista tarefas com `finalDate` vencida ou vencendo em 1-3 dias, agrupa por responsável e manda ao gestor um resumo priorizado ("🔴 2 vencidas, 🟡 4 vencem até sexta"). Sem nada pendente, silêncio.
**Endpoints:** `GET /task/` (filtros de data/responsável/status), `GET /user/`, `notify_mario`.
**Por que importa:** Cria um "segundo par de olhos" automático sobre prazos. Pressão saudável de fechamento, sem caçar no kanban.
**Risco:** Cobertura depende da disciplina de cadastrar prazos como tarefa — declarar isso, não prometer cobertura total. Cap de itens.

### 1.10 ⭐ Opt-out centralizado (compliance LGPD/WhatsApp)
*(brutas #56)*
**O que faz:** Handler que detecta intenção de descadastro ("pare", "não quero mais receber") em qualquer conversa, registra numa lista de supressão e marca a ficha. **Todos** os jobs de envio (follow-up, reativação, aniversário, futuros broadcasts) consultam essa lista antes de enviar. Separa "transacional" (lembrete de reunião agendada = serviço, continua) de "marketing" (suprimido).
**Endpoints:** tag no Jurichat/Juridiq, tabela de supressão SQLite consultada por todos os senders.
**Por que importa:** Respeitar opt-out é exigência de LGPD e de boa conduta. Evita o pior risco reputacional: insistir com quem pediu para parar. **É pré-requisito de segurança** para qualquer envio em escala.
**Risco:** Baixíssimo de implementar, altíssimo de mitigação. Tem de ser respeitado por todos os senders sem exceção.

### 1.11 ⭐ Resumo de conversa pronto no handoff
*(brutas #53)*
**O que faz:** No momento do handoff, o Claude gera (com modelo barato) um resumo estruturado da conversa — nome, vertical, o que o lead quer, urgência, dados já coletados, objeção pendente — e anexa ao alerta do Mario. Quem assume não precisa ler a conversa inteira.
**Endpoints:** transcript já em mãos, `notify_mario`. Nenhum endpoint novo.
**Por que importa:** Handoff instantâneo e produtivo. Reduz tempo de resposta humana e melhora a transição percebida pelo lead.
**Risco:** Mínimo. Instruir o prompt a extrair só do transcript (não inventar). Uso interno.

### 1.12 ⭐ Escalonamento imediato de urgência jurídica do lead
*(brutas #54)*
**O que faz:** Classificador que, na mensagem do lead, detecta marcadores de urgência real ("fui citado, prazo é amanhã", "leilão do meu imóvel", "recebi penhora", "estão me despejando"). Resposta curta e acolhedora + alerta 🚨 imediato ao Mario, **sem esperar a qualificação completa**. Reusa o léxico de urgência que já existe no classificador de publicações.
**Endpoints:** Jurichat, Claude/keywords. Nenhum endpoint novo.
**Por que importa:** Lead com dor aguda e prazo fatal não pode esperar o funil. Casos de alta conversão e alto valor.
**Risco:** Resposta acolhe sem prometer resultado ("vamos te ajudar a entender" ≠ "você vai ganhar"). Escalar a mais é seguro.

---

# ONDA 2 — Depois (relacionamento, gestão, atendimento ao cliente)

## Tema E — Segmentação e qualidade da base (1.464 pessoas)

### 2.1 Segmentação da base por vertical via tags
*(brutas #23)*
**O que faz:** Job de madrugada que, para cada pessoa sem etiqueta de vertical, lê anotação + tipo dos processos vinculados e o Claude classifica a vertical (imobiliário, sucessório, saúde, sênior, previdenciário, agro), aplicando a tag. Idempotente (só toca quem não tem). Reusa o scan de aniversários.
**Endpoints:** `GET /person/`, `GET /person/{id}`, `GET /lawSuit/?person=`, `POST /tag/`, `PATCH /person/{id}`, Claude.
**Por que importa:** **Destrava toda comunicação dirigida e relatório por área.** Transforma 1.464 contatos "mortos" numa base segmentável. É pré-requisito de quase todo o relacionamento.
**Risco:** Roda só sobre dados já no Juridiq. Validar amostra antes do PATCH em massa.

### 2.2 Tags de jornada nascendo no intake (Jurichat ↔ Juridiq)
*(brutas #29, #46)*
**O que faz:** No intake (que já cria a Pessoa), aplicar já as tags certas — "Origem: Funil Julia", "Vertical: <classificada>", "Etapa: Reunião agendada". A vertical entra como campo novo na decisão do Claude (sem chamada extra) e aparece no alerta ("🆕 Lead novo — SUCESSÓRIO").
**Endpoints:** `POST /person/` (já aceita `tags`), `PATCH /person/{id}`, `GET /tag/`.
**Por que importa:** A ficha nasce contando a história certa, sem digitação. A segmentação passa a estar correta a cada novo lead, não só corrigida depois.
**Risco:** Resolver nome→id de tag e fazer *append* (não sobrescrever tags existentes). Interno.

### 2.3 Higienização e enriquecimento da base
*(brutas #24)*
**O que faz:** Scan que computa "completude" de cada ficha (faltam e-mail/telefone/aniversário?), normaliza telefone e detecta duplicatas prováveis. Relatório semanal ao Mario ("23 clientes sem e-mail", "8 telefones duplicados") com link para correção manual. **Não faz merge automático** — só sinaliza.
**Endpoints:** `GET /person/`, `GET /person/{id}`, `notify_mario`.
**Por que importa:** Aumenta a cobertura dos automatismos que já existem (aniversário não dispara sem data; e-mail não sai sem e-mail) e limpa fichas-fantasma. Mesma dor que motivou o saneamento da carteira de processos, agora nas pessoas.
**Risco:** Relatório tem dados pessoais — só canal do Mario, nunca versionado. Não fundir duplicatas automaticamente.

## Tema F — Atendimento ao cliente (relacionamento, com cuidado OAB)

### 2.4 Atendente de status processual no WhatsApp ("como está meu processo?")
*(Funde brutas #57, #5, #10)*
**O que faz:** Para **cliente identificado** (item 1.6), o bot trata a pergunta nº1 do cliente. Whitelist de intenções seguras (status, próxima audiência, documento pendente). Lê a última movimentação e o Claude **reescreve em linguagem leiga sem prognóstico nem prazo** ("o processo recebeu um despacho; nossa equipe acompanha"). Qualquer dúvida de mérito → handoff. Quando o ato depende de algo do cliente (custas, documento, procuração), gera tarefa interna + rascunho de cobrança para o Mario aprovar.
**Endpoints:** Jurichat, `GET /lawSuit/?person=`, `GET /lawSuit/movements/{lawSuitId}`, Claude.
**Por que importa:** Desafoga a equipe da pergunta que mais interrompe o trabalho. Resposta 24/7 aumenta a percepção de cuidado. Relacionamento com cliente é permitido pela OAB.
**Risco (alto):** **Nunca** prometer resultado/prazo de desfecho. Só responder ao **titular** (telefone tem de bater com o cadastro). Bloquear processos em segredo de justiça. Reescrita leiga não pode virar consulta jurídica — handoff em qualquer mérito.

### 2.5 Pesquisa de satisfação (NPS) pós-reunião e pós-encerramento, com humano no loop
*(Funde brutas #25, #47)*
**O que faz:** Após uma reunião realizada (ou ao encerrar um processo), o bot **sugere ao Mario** uma pergunta sóbria de satisfação (0 a 10) com link `wa.me` pronto, para envio assistido. Detrator (≤6) vira alerta para ligação de recuperação. Notas agregadas no relatório semanal.
**Endpoints:** estado de reunião (já existe), `GET /lawSuit/?status=`, `notify_mario`, tag de NPS.
**Por que importa:** Fecha a jornada, gera prova social legítima e detecta insatisfação antes de virar reclamação no PROCON/OAB.
**Risco (OAB):** A mensagem **não pode** ter CTA comercial nem pedir indicação ("indique amigos"). Tom sóbrio, envio assistido, opt-out respeitado. Passar pelo verificador de ética antes.

### 2.6 FAQ determinístico antes do Claude (operacional)
*(brutas #48)*
**O que faz:** Camada de respostas pré-aprovadas para perguntas administrativas frequentes (horário, endereço, "como envio documentos", formas de pagamento). Casou com alta confiança → responde do texto curado, sem chamar o Claude. Ambíguo → segue para o Claude/humano.
**Endpoints:** Jurichat. Nenhum endpoint novo.
**Por que importa:** Respostas instantâneas e consistentes para o repetitivo; menos custo de IA; menos alucinação em pergunta factual.
**Risco:** **Nunca** responder dúvida jurídica automaticamente (parecer é ato privativo). FAQ só cobre o operacional.

### 2.7 Segunda via de documentos: roteia, nunca envia
*(brutas #52)*
**O que faz:** Quando o cliente pede "me manda o contrato / segunda via", o bot **não anexa nada** — direciona para o canal seguro e alerta a equipe com contexto ("📄 cliente pediu 2ª via — verificar identidade antes de enviar"). O envio efetivo é sempre humano.
**Endpoints:** Jurichat, `GET /person/{id}`.
**Por que importa:** Atende um pedido frequente sem expor o escritório a vazamento.
**Risco (crítico):** Sigilo — o bot **jamais** envia documento nem confirma dado sensível por WhatsApp sem verificação humana de identidade.

## Tema G — Gestão da operação (relatórios para o Mario)

### 2.8 Relatório gerencial semanal da carteira
*(Funde brutas #33, #28, #38, #41, #55)*
**O que faz:** Estende o relatório semanal (hoje só de funil) com a **operação**: processos por status/vertical/responsável e variação vs. semana anterior; saúde da base (clientes por vertical, fichas incompletas); KPIs de atendimento (tempo de resposta a handoffs — dado que o sistema **já coleta** na tabela de transições; nº de handoffs e motivos; no-shows); e, quando houver, origem dos leads que viraram processo. Tudo agregado, sem dados pessoais no corpo.
**Endpoints:** `GET /lawSuit/`, `GET /user/`, `GET /tag/`, tabela de transições local, `notify_mario`. Snapshot diário em SQLite para não fazer 1.464 GETs/semana.
**Por que importa:** Dá ao Mario um pulso gerencial que hoje não existe — para onde a carteira cresce, qual vertical pesa, se a equipe responde rápido aos leads quentes. Base objetiva para decisão de contratação e foco comercial.
**Risco:** **Nunca** incluir salário nem dados de cliente — só contagens. Métrica de produtividade individual só no canal do gestor.

### 2.9 Painel diário de carga por advogado
*(Funde brutas #17, #34)*
**O que faz:** Cálculo diário da carga de cada advogado (tarefas abertas/vencidas + audiências da semana + processos sob responsabilidade), com ranking enxuto destacando sobrecarregado × ocioso. Só leitura.
**Endpoints:** `GET /user/`, `GET /task/?responsible=`, `GET /audience/?responsibles=`, `GET /lawSuit/?responsible=`.
**Por que importa:** Sobrecarga vira atraso e prazo perdido. O Mario vê o gargalo de pessoas antes de virar problema, sem montar planilha.
**Risco:** Dado de produtividade individual — **só no canal do gestor**, nunca em grupo. Sem salário.

### 2.10 Triagem financeira de movimentações (RPV, precatório, penhora, leilão)
*(brutas #7)*
**O que faz:** Varredura semântica das movimentações novas que **mexem com dinheiro ou patrimônio** — penhora, bloqueio Sisbajud, leilão, expedição de RPV/precatório, alvará de levantamento. Para "dinheiro a levantar", abre tarefa de alta prioridade e avisa o Mario.
**Endpoints:** `GET /lawSuit/movements/{lawSuitId}`, Claude/regex, `POST /task/`, `POST /tag/`.
**Por que importa:** RPV/precatório/alvará esquecido = honorário e dinheiro do cliente parados; penhora/leilão exige reação rápida. ROI direto.
**Risco:** Valores são sensíveis — canal interno. Falso-positivo gera tarefa a mais (tolerável).

---

# ONDA 3 — Ambicioso (alto valor, maior esforço/risco)

### 3.1 Boletim proativo de andamento ao cliente (opt-in, curadoria conservadora)
*(Funde brutas #58, #5, #22)*
**O que faz:** Inverte o fluxo: em vez de esperar a pergunta, avisa quando há novidade **comunicável** (audiência designada, sentença, acordo homologado). O Claude separa "comunicável ao cliente" de "técnico/interno" e gera mensagem leiga. **Só para clientes com opt-in explícito** (tag "avisar andamento"), e — dada a sensibilidade — o desenho recomendado é **gerar rascunho para aprovação humana**, não disparo automático.
**Endpoints:** `GET /lawSuit/`, DataJud, `POST /tag/`, Jurichat, Claude. Tabela de idempotência por hash.
**Por que importa:** Cliente que recebe "sua audiência foi marcada" percebe escritório atento — fideliza e reduz ligações ansiosas. Aproveita o código DataJud já validado.
**Risco (alto):** Opt-in obrigatório; filtro conservador (um "penhora" fora de contexto gera pânico); nunca antecipar desfecho; pular segredo de justiça; marcar como "fonte pública, confirmação pela equipe".

### 3.2 Briefing pré-reunião automático para a equipe
*(brutas #64)*
**O que faz:** No lembrete de 2h que **já roda**, anexa para a equipe um dossiê: resumo do caso, processos ativos + última movimentação (se cliente), pontos-chave da conversa de WhatsApp e vertical. A pessoa chega à reunião preparada sem garimpar três sistemas.
**Endpoints:** transcript, `GET /lawSuit/?person=`, `GET /lawSuit/movements`, Claude. Reusa o reminder cycle.
**Por que importa:** Reunião mais produtiva = maior conversão e melhor atendimento. Add-on barato sobre gatilho que já existe.
**Risco:** Baixo, saída interna. "Teses iniciais" do Claude são hipóteses marcadas como tais, não vão ao cliente.

### 3.3 Dossiê de processo sob demanda (comando no WhatsApp do Mario)
*(brutas #4)*
**O que faz:** Mario manda "dossiê <nº ou código>" no canal interno e o bot devolve um briefing consolidado (partes, fase, próximos atos, último andamento, pendências) cruzando 3 recursos do Juridiq + audiências, sintetizado pelo Claude. Útil antes de uma reunião ou ligação.
**Endpoints:** `GET /lawSuit/`, `GET /lawSuit/{id}`, `GET /lawSuit/movements`, `GET /audience/`, Claude.
**Por que importa:** Contexto instantâneo sem abrir o painel. Ativa o endpoint `movements`, hoje ocioso.
**Risco:** **Só** no canal do Mario/sócia (allowlist estrita). Bloquear processos em segredo de justiça.

### 3.4 Assistente interno da equipe via WhatsApp (copiloto read-only do Juridiq)
*(brutas #59)*
**O que faz:** Um número/conversa na allowlist da equipe vira copiloto: "quantas audiências essa semana?", "tarefas que vencem hoje?", "publicação urgente pendente?". Claude com *function-calling* sobre os GETs do Juridiq. **Read-only na v1** (não cria/altera nada).
**Endpoints:** Jurichat (allowlist), Claude tool-use, GETs do Juridiq.
**Por que importa:** Tira o Juridiq do navegador e põe no bolso da equipe. O 3.3 é o caso de uso fundador disto.
**Risco (segurança):** Allowlist de `conversation_id` blindada — quem cai no modo assistente lê a base inteira. Read-only reduz risco de ação destrutiva por ambiguidade.

### 3.5 Espelho de audiências no Google Calendar (bot deixa de marcar reunião sobre audiência)
*(Funde brutas #18, #21)*
**O que faz:** Job de madrugada que espelha cada audiência do Juridiq como bloqueio "OCUPADO" no Google Calendar que o bot consulta. Efeito: a busca de horários que o bot já usa passa a **respeitar audiências automaticamente** — resolve na raiz o risco de marcar reunião comercial em cima de audiência judicial.
**Endpoints:** `GET /audience/`, `GoogleCalendarClient.create_event/cancel_event` (já no projeto), mapeamento em SQLite.
**Por que importa:** Previne (não só detecta) o pior caso: faltar a uma audiência por estar em reunião. Uma vez espelhado, todo o fluxo de slots fica "audiência-aware" sem mudar o bot.
**Risco:** Manter sincronia (audiência remarcada/cancelada). Idempotência rígida para não duplicar eventos. Começar em dry-run validando horários/timezone.

### 3.6 Broadcast segmentado, OAB-safe, com aprovação humana em duas fases
*(brutas #45)*
**O que faz:** Comunicado a um grupo por tag (ex.: recesso forense, mudança de endereço, novo canal) em fluxo seguro: o operador descreve, o sistema monta o público + preview + contagem e **exige confirmação explícita**; só então envia com throttle e idempotência. Conteúdo sempre informativo/relacionamento, **nunca oferta**.
**Endpoints:** `GET /tag/`, `GET /person/?tag=`, Jurichat.
**Por que importa:** Hoje não há canal de comunicado em massa legítimo. Avisa clientes de mudanças coletivas sem digitar 1-a-1.
**Risco (alto OAB/LGPD):** Aprovação humana obrigatória; só relacionamento (sem mercantilização/captação); só clientes; opt-out (item 1.10) respeitado; telefone casado com rigor para nunca enviar à parte adversa. Passar pelo verificador de ética.

### 3.7 Reconciliação de fase Juridiq vs. tribunal + auto-avanço do kanban
*(Funde brutas #8, #68)*
**O que faz:** Mensalmente, o Claude detecta incoerências grosseiras de fase (Juridiq diz "contestação", tribunal mostra "trânsito em julgado") e **sugere** a correção. E sincroniza o kanban do CRM com eventos óbvios do funil (lead agendou → "Reunião marcada"; virou processo → "Caso ativo"). Só **sugere** o que é ambíguo; aplica só o trivial e inequívoco.
**Endpoints:** `GET/PATCH /lawSuit/{id}`, DataJud, Claude.
**Por que importa:** Mantém o CRM confiável — base de todos os relatórios e do funil. Elimina o trabalho manual de arrastar cards.
**Risco:** Inferência de fase é ruidosa — por padrão só sugere. Não sobrescrever movimentação manual da equipe. Testar em poucos antes de soltar. Depende de mapear os IDs de coluna (item 0.2).

---

# Descartados (e por quê)

- **Aviso de aniversário, lembretes de reunião 24h/2h/30min, follow-up, reativação de leads frios, alerta de publicações urgentes, intake automático, notificações de lead/handoff, relatório de funil, backup, dead-man switch** — **já existem em produção**. As ideias dos analistas que apenas re-descreviam isso foram absorvidas como *melhorias* nos itens acima (ex.: a "reativação contextual por vertical sazonal" das brutas #50/#66 vira uma evolução da reativação atual, mas só depois da segmentação 2.1 — fica embutida lá, com **teto de 1 toque/trimestre, gatilhos curados por humano e opt-in/opt-out**, dada a linha tênue com captação do Provimento 205).
- **Watchlist VIP por valor/risco (bruta #11)** — boa ideia, mas depende de campos (`degreeOfRisk`, `valueOfCause`) que provavelmente estão vazios na base; absorvida como refinamento futuro do radar 1.2/1.3 via tag manual "VIP", não como item próprio.
- **Mapa de núcleo familiar (bruta #32)** — valor real em sucessório/sênior, mas inferência de vínculo familiar é dado sensível com muito falso-positivo por sobrenome; fica como ideia de Onda 3+ a confirmar com o Mario, fora deste corte.
- **Onboarding de novo membro / offboarding / auditoria de permissões (brutas #36, #37, #42)** — são automações de **RH/segurança interna**, não do funil/carteira; valor real, mas fora do foco deste roadmap (relacionamento + operação jurídica). Menção: a criação de usuário (`POST /user/`) mexe em permissões de acesso a processos sigilosos — se um dia entrar, exige confirmação humana e profileId parametrizado, nunca hardcodado.
- **Minuta de procuração/contrato no fechamento (bruta #62)** e **roteiro de peça pela IA (bruta #65)** — alto valor, mas alto risco técnico-jurídico (documento e peça exigem revisão de advogado sempre); se entrarem, só param no "kit pronto para revisão", nunca enviam/assinam. Ficam como Onda 3+ fora deste corte por dependerem de gerador de PDF e templates versionados.
- **Debounce de mensagens em rajada (bruta #51)** — melhoria técnica de UX legítima (público idoso digita fragmentado), mas é refinamento de robustez do poll cycle, não automação de negócio; vale agendar como dívida técnica, não como item de roadmap para o dono.

---

## Sequência sugerida de execução

1. **Onda 0** (índice telefone→ficha + IDs do .env) — fundação, dias.
2. **Onda 1 quick wins primeiro:** 1.1 (prazo→tarefa), 1.10 (opt-out), 1.11 (resumo no handoff), 1.12 (urgência), 1.8/1.9 (agenda + SLA). Depois 1.2/1.3 (DataJud + prescrição), 1.4/1.5 (audiências), 1.6/1.7 (reconhecer cliente + conflito).
3. **Onda 2:** começar por 2.1 (segmentação) porque destrava relacionamento; depois gestão (2.8/2.9) e atendimento ao cliente (2.4).
4. **Onda 3** conforme apetite e validação do Mario.