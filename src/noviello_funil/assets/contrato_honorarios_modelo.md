<!--
⚠️ v1 RECONSTRUÍDA (25/jun/2026) — PENDENTE VALIDAÇÃO JURÍDICA DO MARIO.
Texto das 14 cláusulas FIXAS reconstruído a partir do MAPA da skill
noviello-contrato-honorarios (SKILL.md), pois os arquivos de texto vetado
(templates/contrato-padrao.md, references/02-clausulas-fixas-texto-completo.md)
nunca foram criados na skill (flag REFERENCIA_QUEBRADA). NÃO é o texto vetado
original — é redação de partida do #39, a ser corrigida cláusula a cláusula pelo
Mario antes do primeiro uso real.

SLOTS VARIÁVEIS (preenchidos por caso; a IA do caminho B só toca estes):
  {{OBJETO}}             — Cláusula 1ª (objeto + instância). IA redige no atípico.
  {{HONORARIOS_FIXO}}    — Cláusula 5ª. VALOR DIGITADO PELO MARIO (IA nunca precifica).
  {{HONORARIOS_EXITO}}   — Cláusula 5ª (% êxito). Valor do Mario.
  {{MULTA_LIMINAR_PCT}}  — Cláusula 6ª (% sobre multa cominatória; padrão 30%).
Qualificação do cliente: {{CLIENTE_*}}, {{DATA}} (preenchidos do cadastro).
-->

# CONTRATO DE PRESTAÇÃO DE SERVIÇOS ADVOCATÍCIOS E HONORÁRIOS

**CONTRATADO:** NOVIELLO ADVOCACIA, sociedade individual de advocacia, inscrita na
OAB/SP sob o nº 21.788 e no CNPJ sob o nº 27.340.554/0001-94, com sede na Avenida
do Café, nº 238, Vila Guarani, São Paulo/SP, CEP 04311-000, neste ato representada
por seu titular **MARIO LUIZ NOVIELLO JUNIOR**, advogado inscrito na OAB/SP sob o
nº 370.796.

**CONTRATANTE:** {{CLIENTE_NOME}}, {{CLIENTE_NACIONALIDADE}}, {{CLIENTE_ESTADO_CIVIL}},
{{CLIENTE_PROFISSAO}}, portador(a) do RG nº {{CLIENTE_RG}} e inscrito(a) no CPF sob
o nº {{CLIENTE_CPF}}, residente e domiciliado(a) em {{CLIENTE_ENDERECO}}, endereço
eletrônico {{CLIENTE_EMAIL}}.

As partes, acima qualificadas, têm entre si justo e contratado o presente
instrumento particular de prestação de serviços advocatícios, que se regerá pelas
cláusulas e condições seguintes.

---

## CLÁUSULA 1ª — DO OBJETO  *(variável)*

{{OBJETO}}

**Parágrafo único.** A atuação ora contratada restringe-se à **1ª (primeira)
instância**. A atuação em grau de recurso, em instância superior ou em
procedimento autônomo (incidente, execução, cumprimento de sentença) será objeto
de **aditivo contratual próprio**, com honorários a serem ajustados em apartado.

## CLÁUSULA 2ª — DAS ATIVIDADES  *(fixa)*

Os serviços ora contratados constituem **obrigação de meio, e não de resultado**,
comprometendo-se o CONTRATADO a empregar a técnica, a diligência e o zelo
profissionais exigíveis na defesa dos interesses do CONTRATANTE.

**§ 1º.** O CONTRATADO **não garante**, e o CONTRATANTE expressamente reconhece que
não lhe foi prometido, qualquer resultado favorável, êxito ou prazo de duração do
processo, decisões essas que competem exclusivamente ao Poder Judiciário e dependem
de fatores alheios à vontade das partes.

**§ 2º.** O CONTRATADO atuará com lealdade e boa-fé, mantendo o CONTRATANTE
informado sobre o andamento da causa pelos canais oficiais do escritório.

**§ 3º.** Eventual insucesso na demanda **não enseja** a devolução dos honorários
fixos pagos, que remuneram o serviço técnico prestado.

**§ 4º.** O CONTRATADO poderá deixar de interpor recurso ou medida que repute
juridicamente inviável, mediante comunicação fundamentada ao CONTRATANTE.

**§ 5º.** A prestação de serviços observará a legislação aplicável e o Código de
Ética e Disciplina da OAB.

## CLÁUSULA 3ª — DO SUBSTABELECIMENTO  *(fixa)*

Fica o CONTRATADO autorizado a substabelecer o mandato, com ou sem reserva de
poderes, a outros advogados de sua confiança, **correndo por sua conta** eventuais
custos de tal substabelecimento, sem ônus adicional ao CONTRATANTE.

## CLÁUSULA 4ª — DAS DESPESAS  *(fixa)*

As **despesas processuais e extraprocessuais** — tais como custas judiciais, taxas,
emolumentos cartorários, certidões, diligências, honorários periciais, cópias,
deslocamentos e viagens — correm **por conta exclusiva do CONTRATANTE**, não estando
compreendidas nos honorários ajustados na Cláusula 5ª.

**Parágrafo único.** O CONTRATADO não está obrigado a antecipar despesas; quando o
fizer, serão reembolsadas pelo CONTRATANTE mediante comprovação.

## CLÁUSULA 5ª — DOS HONORÁRIOS  *(variável — valor digitado pelo Mario)*

Pelos serviços contratados, o CONTRATANTE pagará ao CONTRATADO honorários
advocatícios no valor fixo de **{{HONORARIOS_FIXO}}**, acrescidos de **honorários de
êxito** correspondentes a **{{HONORARIOS_EXITO}}** sobre o proveito econômico obtido.

**§ 1º.** Os pagamentos serão realizados exclusivamente via **PIX** para a chave
**CNPJ 27.340.554/0001-94** (Noviello Advocacia — Nubank, agência 0001, conta
corrente 40210468-9). **Nenhum outro dado bancário é válido** (ver Cláusula 15ª).

**§ 2º.** Os honorários sucumbenciais, fixados em favor do advogado por força de lei
(art. 85 do CPC), pertencem ao CONTRATADO e **não se confundem** com os honorários
contratuais ora ajustados.

**§ 3º.** Os honorários de êxito incidem sobre o benefício econômico efetivamente
auferido pelo CONTRATANTE, apurado ao final, ainda que por acordo.

**§ 4º.** O **início dos trabalhos** fica condicionado ao **pagamento da 1ª parcela
e ao envio do respectivo comprovante** ao CONTRATADO.

## CLÁUSULA 6ª — DA MULTA POR DESCUMPRIMENTO DE LIMINAR  *(variável — padrão 30%)*

Na hipótese de a parte adversa ser condenada ao pagamento de **multa cominatória**
(astreintes) por descumprimento de decisão judicial obtida em favor do CONTRATANTE,
caberá ao CONTRATADO, a título de honorários, o percentual de **{{MULTA_LIMINAR_PCT}}**
sobre o valor efetivamente recebido a esse título.

## CLÁUSULA 7ª — DA SUCESSÃO  *(fixa)*

Em caso de morte ou incapacidade do CONTRATADO, fica facultado ao CONTRATANTE
constituir novo patrono, sendo devidos ao CONTRATADO (ou a seus sucessores) os
honorários proporcionais aos serviços já prestados até a data do evento.

## CLÁUSULA 8ª — DO ATRASO NO PAGAMENTO  *(fixa)*

O atraso no pagamento de qualquer parcela sujeitará o CONTRATANTE à **multa
moratória de 2% (dois por cento)** sobre o valor em atraso, acrescida de **juros de
3% (três por cento) ao mês** e correção monetária.

**Parágrafo único.** O atraso **superior a 30 (trinta) dias** faculta ao CONTRATADO
a **rescisão** do contrato e a suspensão imediata dos serviços, sem prejuízo da
cobrança dos valores devidos.

## CLÁUSULA 9ª — DA VIGÊNCIA E DA RESCISÃO  *(fixa)*

O presente contrato vigora a partir de sua assinatura até o encerramento dos
serviços na 1ª instância. Qualquer das partes poderá rescindi-lo mediante **aviso
prévio de 30 (trinta) dias**.

**Parágrafo único.** A rescisão imotivada por parte do CONTRATANTE, ou por culpa a
ele imputável, **não o exime** do pagamento dos honorários proporcionais aos
serviços prestados, acrescidos de **multa de 10% (dez por cento)** sobre o saldo
contratual.

## CLÁUSULA 10ª — DA RESPONSABILIDADE  *(fixa)*

A responsabilidade do CONTRATADO limita-se à atuação técnica diligente no objeto
contratado, não respondendo por fatos, provas ou informações não fornecidos
tempestivamente pelo CONTRATANTE.

## CLÁUSULA 11ª — DAS INFORMAÇÕES DO CONTRATANTE  *(fixa)*

O CONTRATANTE obriga-se a fornecer ao CONTRATADO, com veracidade e tempestividade,
todos os documentos, dados e informações necessários à atuação, respondendo pela
exatidão do que prestar.

## CLÁUSULA 12ª — DA COMUNICAÇÃO  *(fixa)*

As comunicações entre as partes far-se-ão pelos canais oficiais informados neste
contrato (e-mail e telefone do escritório), mantendo o CONTRATANTE seus dados de
contato atualizados.

## CLÁUSULA 13ª — DO DEVER DE INFORMAR ALTERAÇÕES  *(fixa)*

O CONTRATANTE compromete-se a comunicar de imediato ao CONTRATADO qualquer
alteração de endereço, telefone, e-mail ou estado civil, bem como qualquer contato
recebido em nome do escritório, para conferência de autenticidade (Cláusula 15ª).

## CLÁUSULA 14ª — DO USO DE INTELIGÊNCIA ARTIFICIAL  *(fixa)*

Em observância à **Recomendação nº 001/2024 do Conselho Federal da OAB**, o
CONTRATADO informa que poderá utilizar **ferramentas de inteligência artificial**
como apoio à atividade advocatícia, **sob supervisão e responsabilidade humana do
advogado**, preservados o sigilo profissional, a proteção de dados pessoais (LGPD)
e a vedação à substituição do juízo técnico do profissional.

## CLÁUSULA 15ª — DA SEGURANÇA E DA PREVENÇÃO A FRAUDES  *(fixa)*

Para proteção do CONTRATANTE contra o **"golpe do falso advogado"**, ficam
estabelecidas as seguintes regras de segurança:

**§ 1º.** **Pagamentos** somente são solicitados via **PIX para a chave CNPJ
27.340.554/0001-94** (Cláusula 5ª, § 1º). O escritório **jamais** solicita depósito,
PIX ou transferência para conta de pessoa física ou para chave diversa.

**§ 2º.** O escritório **não** solicita pagamentos por telefonemas urgentes,
mensagens com pressa ou links de cobrança não oficiais.

**§ 3º.** Os **canais oficiais** são exclusivamente os informados neste contrato;
qualquer contato por número ou e-mail distintos deve ser tratado como suspeito.

**§ 4º.** Em caso de dúvida sobre a autenticidade de qualquer cobrança ou
comunicação, o CONTRATANTE deve **confirmar diretamente** com o escritório pelos
canais oficiais **antes** de qualquer pagamento.

**§ 5º.** O escritório não envia boletos nem cobra valores não previstos neste
contrato sem comunicação formal prévia.

**§ 6º.** Alterações de dados bancários **nunca** ocorrem por mensagem; a chave PIX
é e permanece o CNPJ acima.

**§ 7º.** O CONTRATANTE será orientado a desconfiar de qualquer urgência incomum.

**§ 8º.** O descumprimento destas cautelas pelo CONTRATANTE, com pagamento a
terceiro fraudador, **não gera** responsabilidade do CONTRATADO.

## CLÁUSULA 16ª — DA ASSINATURA ELETRÔNICA  *(fixa)*

As partes reconhecem a **validade jurídica** da assinatura eletrônica deste
instrumento, realizada por meio da plataforma **ZapSign**, nos termos da **MP nº
2.200-2/2001** (ICP-Brasil e assinaturas eletrônicas) e da **Lei nº 14.063/2020**,
com trilha de auditoria (data, hora, IP, e-mail e demais elementos de
integridade), para todos os fins de direito.

## CLÁUSULA 17ª — DO FORO  *(fixa)*

Fica eleito o **Foro Central da Comarca de São Paulo/SP**, com renúncia a qualquer
outro, por mais privilegiado que seja, para dirimir as questões oriundas do
presente contrato.

---

E, por estarem assim justas e contratadas, as partes assinam o presente
eletronicamente, na mesma sessão ZapSign.

São Paulo, {{DATA}}.

**CONTRATANTE:** {{CLIENTE_NOME}} — CPF {{CLIENTE_CPF}}

**CONTRATADO:** Mario Luiz Noviello Junior — OAB/SP 370.796
Noviello Advocacia — CNPJ 27.340.554/0001-94

**TESTEMUNHAS:**
1. Nome: _____________________________ CPF: ________________
2. Nome: _____________________________ CPF: ________________
