# JURIDIQ API — campos por recurso


## GET /audience/ — Listar audiências
  query: page(string), limit(string), title(string), type(string), responsibles(string), isMetrics(string), date(string), lawSuit(string), orderBy(string), orderByField(string), isPrivate(string), start(string), end(string)
  RESPOSTA (campos):
      id: string — ID da audiência
      title: string — Título
      date: string — Data
      initialHour: string — Hora inicial
      finalHour: string — Hora final
      modality: string — Modalidade
      type: string — Modalidade
      audienceLink: string — Link da audiência
      description: string — Descrição
      processNumber: string — Número do processo
      lawSuitId: string — ID do processo
      isPrivate: boolean — Audiência privada
      location: string — Local
      participants: array — Participantes
        id: string — ID do participante
        name: string — Nome
        email: string — E-mail
      ownerId: string — ID do responsável
      createdAt: string — Data de criação
      updatedAt: string — Data de atualização

## POST /audience/ — Criar audiência
  BODY:
      title: string — Título
      date: string — Data
      initialHour: string — Hora inicial
      finalHour: string — Hora final
      type: string enum=['online', 'presencial'] — Modalidade
      audienceParticipants: array — IDs dos participantes
      description: string — Descrição
      lawSuitId: string — ID do processo
      audienceLink: string — Link da audiência
      isPrivate: boolean — Audiência privada
      location: string — Local

## DELETE /audience/ — Excluir audiências em lote
  BODY:
      ids: array — IDs das audiências

## GET /audience/{id} — Buscar audiência
  RESPOSTA (campos):
      id: string — ID da audiência
      title: string — Título
      date: string — Data
      initialHour: string — Hora inicial
      finalHour: string — Hora final
      modality: string — Modalidade
      type: string — Modalidade
      audienceLink: string — Link da audiência
      description: string — Descrição
      processNumber: string — Número do processo
      lawSuitId: string — ID do processo
      isPrivate: boolean — Audiência privada
      location: string — Local
      participants: array — Participantes
        id: string — ID do participante
        name: string — Nome
        email: string — E-mail
      ownerId: string — ID do responsável
      createdAt: string — Data de criação
      updatedAt: string — Data de atualização

## PATCH /audience/{id} — Atualizar audiência
  BODY:
      type: string enum=['online', 'presencial'] — Modalidade
      title: string — Título
      date: string — Data
      initialHour: string — Hora inicial
      finalHour: string — Hora final
      audienceParticipants: array — IDs dos participantes
      description: string — Descrição
      process: ? — ID do processo
      lawSuitId: ? — ID do processo
      audienceLink: string — Link da audiência
      isPrivate: boolean — Audiência privada
      location: string — Local
  RESPOSTA (campos):
      id: string — ID da audiência
      title: string — Título
      date: string — Data
      initialHour: string — Hora inicial
      finalHour: string — Hora final
      modality: string — Modalidade
      type: string — Modalidade
      audienceLink: string — Link da audiência
      description: string — Descrição
      processNumber: string — Número do processo
      lawSuitId: string — ID do processo
      isPrivate: boolean — Audiência privada
      location: string — Local
      participants: array — Participantes
        id: string — ID do participante
        name: string — Nome
        email: string — E-mail
      ownerId: string — ID do responsável
      createdAt: string — Data de criação
      updatedAt: string — Data de atualização

## DELETE /audience/{id} — Excluir audiência

## GET /event/ — Listar eventos
  query: responsible(?), responsiblesMetrics(?), personId(string), lawSuitId(string), dateType(string), start(string), end(string), statusFilter(string), fullQuery(string), tag(?), itemType(string), taskPriority(?), taskTag(?)
  RESPOSTA (campos):
      id: string — ID do item
      title: string — Título
      start: string — Início (ISO ou data)
      end: string — Fim (ISO ou data)
      allDay: boolean — Dia inteiro
      textColor: string — Cor do texto
      backgroundColor: string — Cor de fundo
      borderColor: string — Cor da borda
      classNames: string — Classes CSS do calendário
      type: ? — Tipo do item
      isPrivate: boolean — Privado
      status: string — Status
      priority: string — Prioridade da tarefa
      tags: array — Etiquetas da tarefa
        id: string — 
        name: string — 
        color: string — 
      link: string — Link (Google)
      initialHour: string — Hora inicial
      finalHour: string — Hora final
      date: string — Data

## POST /event/ — Criar evento
  BODY:
      title: string — Título
      initialHour: string — Hora inicial
      finalHour: string — Hora final
      date: string — Data
      responsibleIds: array — IDs dos responsáveis
      description: string — Descrição
      lawSuitId: string — ID do processo
      allDay: boolean — Dia inteiro
      officeId: string — ID do escritório
      isPrivate: boolean — Evento privado

## DELETE /event/ — Excluir eventos
  BODY:
      id: array — IDs dos eventos

## GET /event/{id} — Buscar evento
  RESPOSTA (campos):
      id: string — ID do evento
      title: string — Título
      initialHour: string — Hora inicial
      finalHour: string — Hora final
      date: string — Data
      allDay: boolean — Dia inteiro
      isPrivate: boolean — Evento privado
      responsible: array — Responsáveis
        id: string — ID do responsável
        name: string — Nome
        avatar: string — URL da foto

## PATCH /event/{id} — Atualizar evento
  BODY:
      responsibleIds: array — IDs dos responsáveis
      title: string — Título
      description: string — Descrição
      lawSuitId: ? — ID do processo
      initialHour: string — Hora inicial
      finalHour: string — Hora final
      date: string — Data
      allDay: boolean — Dia inteiro
      officeId: string — ID do escritório
      isPrivate: boolean — Evento privado
      completedAt: string — Data de conclusão
  RESPOSTA (campos):
      id: string — ID do evento
      title: string — Título
      initialHour: string — Hora inicial
      finalHour: string — Hora final
      date: string — Data
      allDay: boolean — Dia inteiro
      isPrivate: boolean — Evento privado
      responsible: array — Responsáveis
        id: string — ID do responsável
        name: string — Nome
        avatar: string — URL da foto

## GET /lawSuit/ — Listar processos
  query: page(string), limit(string), processNumber(string), person(string), responsible(?), orderByField(string), orderBy(string), hasMonitoring(string), status(?), tag(?), isUniqueResponsible(string), fullQuery(string), isPrivate(string), isSecret(string), actionGroup(string), typeAction(?), code(string), corretor(string), court(string)
  RESPOSTA (campos):
      id: string — ID do processo
      processNumber: string — Número CNJ
      status: string — Status
      instance: number — Instância
      court: string — Tribunal
      isPrivate: boolean — Processo privado
      isSecret: boolean — Processo em segredo
      officeId: string — ID do escritório

## POST /lawSuit/ — Criar processo
  BODY:
      processNumber: string — Número CNJ
      serviceStep: string — Fase do processo
      responsibleIds: array — IDs dos responsáveis
      persons: array — Partes do processo
        id: string — ID da pessoa existente
        name: string — Nome da parte
        personOrigin: string — Origem da parte. Ex: autor, réu
      officeId: string — ID do escritório
      valueOfCause: string — Valor da causa
      valueOfFees: string — Valor dos honorários
      percentageOfFees: string — Percentual dos honorários
      requerimentDate: string — Data do requerimento
      protocolNumber: string — Número do protocolo
      annotations: string — Anotações
      originatingProcess: string — Processo originário
      actionGroup: ? — Grupo de ação
      typeAction: ? — Tipo de ação
      instance: number — Instância
      court: string — Tribunal
      tags: array — IDs das tags
      degreeOfRisk: string — Grau de risco
      isPrivate: boolean — Processo privado
      isSecret: boolean — Processo em segredo
      isProcessAdministrative: boolean — Processo administrativo
      linkedLawSuitId: array — IDs de processos vinculados
      processUrl: string — URL do processo
      courtBranch: string — Vara
      precatoryNumbers: array — Números de precatório
      status: string — Status
      publicationId: string — ID da publicação
      createdByAutomation: boolean — Criado por automação
      author: string — Autor
      defendant: string — Réu
      periciaResult: string — Resultado da perícia
      periciaExpertName: string — Nome do perito
      periciaExpertType: string — Tipo do perito
      condemnationAmount: string — Valor da condenação
      periciaExpertHonoraries: string — Honorários do perito
      valueHonorariesSuccession: string — Honorários de sucessão
      petition: string — Petição
      valuePetition: string — Valor da petição
      corretor: string — Corretor
      code: string — Código interno
      consultingId: string — ID da consultoria

## DELETE /lawSuit/ — Excluir processos
  BODY:
      id: array — IDs dos processos

## GET /lawSuit/movements/{lawSuitId} — Listar movimentações
  query: tagId(string), content(string)

## GET /lawSuit/{id} — Buscar processo
  query: fullQuery(string)
  RESPOSTA (campos):
      id: string — ID do processo
      processNumber: string — Número CNJ
      valueOfCause: string — Valor da causa
      status: string — Status
      instance: number — Instância
      court: string — Tribunal
      serviceStep: string — Fase do processo
      isPrivate: boolean — Processo privado
      isSecret: boolean — Processo em segredo
      officeId: string — ID do escritório

## PATCH /lawSuit/{id} — Atualizar processo
  BODY:
      serviceStep: string — Fase do processo
      responsibleIds: array — IDs dos responsáveis
      officeId: string — ID do escritório
      processNumber: string — Número CNJ
      valueOfCause: string — Valor da causa
      valueOfFees: string — Valor dos honorários
      percentageOfFees: string — Percentual dos honorários
      requerimentDate: string — Data do requerimento
      protocolNumber: string — Número do protocolo
      annotations: string — Anotações
      originatingProcess: string — Processo originário
      persons: array — Partes do processo
        id: string — ID da pessoa existente
        name: string — Nome da parte
        personOrigin: string — Origem da parte. Ex: autor, réu
      actionGroup: ? — Grupo de ação
      typeAction: ? — Tipo de ação
      degreeOfRiskId: ? — ID do grau de risco
      tags: array — IDs das tags
      court: string — Tribunal
      instance: number — Instância
      courtLawSuitDate: string — Data do processo no tribunal
      status: string — Status
      linkedLawSuitId: array — IDs de processos vinculados
      isPrivate: boolean — Processo privado
      isSecret: boolean — Processo em segredo
      processUrl: string — URL do processo
      courtBranch: string — Vara
      precatoryNumbers: array — Números de precatório
      author: string — Autor
      defendant: string — Réu
      periciaResult: string — Resultado da perícia
      periciaExpertName: string — Nome do perito
      periciaExpertType: string — Tipo do perito
      condemnationAmount: string — Valor da condenação
      periciaExpertHonoraries: string — Honorários do perito
      valueHonorariesSuccession: string — Honorários de sucessão
      petition: string — Petição
      valuePetition: string — Valor da petição
      corretor: string — Corretor
      code: string — Código interno
      consultingColumnId: ? — ID da coluna do CRM
  RESPOSTA (campos):
      id: string — ID do processo
      processNumber: string — Número CNJ
      valueOfCause: string — Valor da causa
      status: string — Status
      instance: number — Instância
      court: string — Tribunal
      serviceStep: string — Fase do processo
      isPrivate: boolean — Processo privado
      isSecret: boolean — Processo em segredo
      officeId: string — ID do escritório

## GET /person/ — Listar pessoas
  query: page(string), limit(string), name(string), type(string), phone(string), email(string), document(string), code(string), orderBy(string), orderByField(string), fullQuery(string), isPrivate(string), legalRepresentant(string), ownerId(string), corretor(string), tag(?)
  RESPOSTA (campos):
      id: string — ID da pessoa
      name: string — Nome
      email: string — E-mail
      phone: string — Telefone
      personOrigin: string — Origem. Ex: cliente, parte
      isPrivate: boolean — Registro privado
      ownerId: string — ID do responsável
      legalRepresentant: string — Representante legal
      corretor: string — Corretor
      code: string — Código interno
      govPassword: string — Senha gov.br

## POST /person/ — Criar pessoa
  BODY:
      name: string — Nome
      personOrigin: string — Origem. Ex: cliente, parte
      email: string — E-mail
      imageUrl: string — URL da foto
      document: string — CPF ou CNPJ
      phone: string — Telefone
      personType: string — Tipo. Ex: física, jurídica
      maritalStatus: string — Estado civil
      zipCode: string — CEP
      state: string — Estado
      city: string — Cidade
      neighborhood: string — Bairro
      streetAndNumber: string — Rua e número
      clientDiscoverOffice: string — Como conheceu o escritório
      annotation: string — Anotação em texto
      officeId: string — ID do escritório
      rg: string — RG
      nationality: string — Nacionalidade
      profession: string — Profissão
      birthDate: string — Data de nascimento
      isPrivate: boolean — Registro privado
      legalRepresentant: string — Representante legal
      tags: array — IDs das tags
      corretor: string — Corretor
      code: string — Código interno
      govPassword: string — Senha gov.br
      addressComplement: string — Complemento do endereço

## DELETE /person/ — Excluir pessoas
  BODY:
      id: array — IDs das pessoas
      deleteLawSuit: boolean — true para excluir processos vinculados
  RESPOSTA (campos):
      message: string — Mensagem de sucesso

## GET /person/search — Buscar pessoa por telefone
  query: phoneNumber(string)
  RESPOSTA (campos):
      id: string — ID da pessoa
      name: string — Nome
      email: string — E-mail
      phone: string — Telefone
      personOrigin: string — Origem. Ex: cliente, parte
      isPrivate: boolean — Registro privado
      ownerId: string — ID do responsável
      legalRepresentant: string — Representante legal
      corretor: string — Corretor
      code: string — Código interno
      govPassword: string — Senha gov.br

## GET /person/{id} — Buscar pessoa
  RESPOSTA (campos):
      id: string — ID da pessoa
      name: string — Nome
      email: string — E-mail
      document: string — CPF ou CNPJ
      phone: string — Telefone
      personOrigin: string — Origem. Ex: cliente, parte
      personType: string — Tipo. Ex: física, jurídica
      maritalStatus: string — Estado civil
      zipCode: string — CEP
      state: string — Estado
      city: string — Cidade
      neighborhood: string — Bairro
      streetAndNumber: string — Rua e número
      imageUrl: string — URL da foto
      clientDiscoverOffice: string — Como conheceu o escritório
      annotation: ? — Anotações
      officeId: string — ID do escritório
      rg: string — RG
      nationality: string — Nacionalidade
      profession: string — Profissão
      birthDate: string — Data de nascimento
      isPrivate: boolean — Registro privado
      ownerId: string — ID do responsável
      legalRepresentant: string — Representante legal
      corretor: string — Corretor
      code: string — Código interno
      govPassword: string — Senha gov.br
      addressComplement: string — Complemento do endereço
      createdAt: ? — Data de criação
      updatedAt: ? — Data da última alteração

## PATCH /person/{id} — Atualizar pessoa
  BODY:
      name: string — Nome
      personOrigin: string — Origem. Ex: cliente, parte
      email: string — E-mail
      imageUrl: string — URL da foto
      document: string — CPF ou CNPJ
      phone: string — Telefone
      personType: string — Tipo. Ex: física, jurídica
      maritalStatus: string — Estado civil
      zipCode: string — CEP
      state: string — Estado
      city: string — Cidade
      neighborhood: string — Bairro
      streetAndNumber: string — Rua e número
      clientDiscoverOffice: string — Como conheceu o escritório
      annotation: string — Anotação em texto
      officeId: string — ID do escritório
      createdAt: string — Data de criação
      rg: string — RG
      nationality: string — Nacionalidade
      profession: string — Profissão
      birthDate: string — Data de nascimento
      isPrivate: boolean — Registro privado
      legalRepresentant: string — Representante legal
      tags: array — IDs das tags
      corretor: string — Corretor
      code: string — Código interno
      govPassword: string — Senha gov.br
      addressComplement: string — Complemento do endereço
  RESPOSTA (campos):
      id: string — ID da pessoa
      name: string — Nome
      email: string — E-mail
      document: string — CPF ou CNPJ
      phone: string — Telefone
      personOrigin: string — Origem. Ex: cliente, parte
      personType: string — Tipo. Ex: física, jurídica
      maritalStatus: string — Estado civil
      zipCode: string — CEP
      state: string — Estado
      city: string — Cidade
      neighborhood: string — Bairro
      streetAndNumber: string — Rua e número
      imageUrl: string — URL da foto
      clientDiscoverOffice: string — Como conheceu o escritório
      annotation: ? — Anotações
      officeId: string — ID do escritório
      rg: string — RG
      nationality: string — Nacionalidade
      profession: string — Profissão
      birthDate: string — Data de nascimento
      isPrivate: boolean — Registro privado
      ownerId: string — ID do responsável
      legalRepresentant: string — Representante legal
      corretor: string — Corretor
      code: string — Código interno
      govPassword: string — Senha gov.br
      addressComplement: string — Complemento do endereço
      createdAt: ? — Data de criação
      updatedAt: ? — Data da última alteração

## GET /publication/ — Listar publicações
  query: page(string), limit(string), orderBy(string), keySearch(string), isHandled(string), start(string), end(string)
  RESPOSTA (campos):
      id: string — ID da movimentação
      title: string — Título
      content: string — Conteúdo
      descriptionSmall: string — Resumo
      officialDiary: string — Diário oficial
      availabilityDate: string — Data de disponibilização
      publicationDate: string — Data da publicação
      location: string — Local
      processNumber: string — Número do processo
      isHandled: boolean — Já tratada
      readAt: string — Data da leitura
      readBy: object — Usuário que leu
        id: string — ID do usuário que leu
        name: string — Nome do usuário que leu
        imageUrl: string — URL da foto
      lawSuitId: string — ID do processo
      hasSimilarity: boolean — Tem similaridade

## DELETE /publication/discard — Descartar publicações
  BODY:
      ids: array — IDs das movimentações

## PATCH /publication/read-movements-many — Marcar publicações como lidas
  BODY:
      ids: array — IDs das movimentações
  RESPOSTA (campos):
      message: string — Mensagem de sucesso
      updatedCount: number — Quantidade atualizada
      processedIds: array — IDs processados

## GET /publication/{id} — Buscar publicação
  RESPOSTA (campos):
      id: string — ID da movimentação
      title: string — Título
      content: string — Conteúdo
      descriptionSmall: string — Resumo
      officialDiary: string — Diário oficial
      availabilityDate: string — Data de disponibilização
      publicationDate: string — Data da publicação
      location: string — Local
      processNumber: string — Número do processo
      isHandled: boolean — Já tratada
      readAt: string — Data da leitura
      readBy: object — Usuário que leu
        id: string — ID do usuário que leu
        name: string — Nome do usuário que leu
        imageUrl: string — URL da foto
      lawSuitId: string — ID do processo
      hasSimilarity: boolean — Tem similaridade

## GET /tag/ — Listar etiquetas
  query: page(string), limit(string), name(string), orderBy(string), module(string)
  RESPOSTA (campos):
      id: string — ID da etiqueta
      name: string — Nome
      color: string — Cor
      modules: array — Módulos vinculados
      officeId: string — ID do escritório
      updatedAt: ? — Atualizado em
      createdAt: ? — Criado em

## POST /tag/ — Criar etiqueta
  BODY:
      name: string — Nome
      color: string — Cor
      modules: array — Módulos. Padrão: lawSuit

## DELETE /tag/ — Excluir etiquetas
  BODY:
      id: array — IDs das etiquetas

## GET /tag/{id} — Buscar etiqueta
  RESPOSTA (campos):
      id: string — ID da etiqueta
      name: string — Nome
      color: string — Cor
      modules: array — Módulos vinculados
      officeId: string — ID do escritório
      updatedAt: ? — Atualizado em
      createdAt: ? — Criado em

## PATCH /tag/{id} — Atualizar etiqueta
  BODY:
      name: string — Nome
      color: string — Cor
      modules: array — Módulos
  RESPOSTA (campos):
      id: string — ID da etiqueta
      name: string — Nome
      color: string — Cor
      modules: array — Módulos vinculados
      officeId: string — ID do escritório
      updatedAt: ? — Atualizado em
      createdAt: ? — Criado em

## GET /task/ — Listar tarefas
  query: page(string), limit(string), title(string), responsible(string), priority(string), lawSuit(string), isHome(string), orderBy(string), orderByField(string), isUniqueResponsible(string), isArchived(string), isPrivate(string), start(string), end(string), tag(string), dateMethod(string), personId(string), points(string), parentTaskId(string), taskColumnId(string), folderId(string), boardId(string), taskKanbanColumnIds(?)
  RESPOSTA (campos):
      id: string — ID da tarefa
      title: string — Título
      priority: string — Prioridade
      status: string — Status
      initialDate: string — Data inicial
      finalDate: string — Data final
      isArchived: boolean — Arquivada
      isPrivate: boolean — Privada
      ownerId: string — ID do criador
      points: number — Pontos

## POST /task/ — Criar tarefa
  BODY:
      title: string — Título
      priority: string — Prioridade
      columnId: string — ID da coluna kanban
      initialDate: string — Data inicial
      description: ? — Descrição. Texto ou JSON do editor
      finalDate: string — Data final
      initialHour: string — Hora inicial
      finalHour: string — Hora final
      lawSuitId: string — ID do processo
      responsiblesId: array — IDs dos responsáveis
      officeId: string — ID do escritório
      isPrivate: boolean — Tarefa privada
      tags: array — IDs das tags
      points: number — Pontos
      needValidation: boolean — Exige validação
      isValidated: boolean — Já validada
      validatorsId: array — IDs dos validadores
      personIds: array — IDs das pessoas

## DELETE /task/ — Excluir tarefas
  BODY:
      id: array — IDs das tarefas

## GET /task/{id} — Buscar tarefa
  query: page(string), limit(string)
  RESPOSTA (campos):
      id: string — ID da tarefa
      title: string — Título
      priority: string — Prioridade
      status: string — Status
      initialDate: string — Data inicial
      finalDate: string — Data final
      isArchived: boolean — Arquivada
      isPrivate: boolean — Privada
      ownerId: string — ID do criador
      points: number — Pontos

## PATCH /task/{id} — Atualizar tarefa
  BODY:
      title: string — Título
      priority: string — Prioridade
      status: string — Status
      initialDate: string — Data inicial
      lawSuitId: string — ID do processo
      responsiblesId: array — IDs dos responsáveis
      isArchived: boolean — Arquivada
      isPrivate: boolean — Privada
      officeId: string — ID do escritório
      description: ? — Descrição. Texto ou JSON do editor
      finalDate: string — Data final
      initialHour: string — Hora inicial
      finalHour: string — Hora final
      createdAt: string — Data de criação
      currentIndex: number — Índice na coluna kanban
      tags: array — IDs das tags
      points: number — Pontos
      needValidation: boolean — Exige validação
      isValidated: boolean — Já validada
      validatorsId: array — IDs dos validadores
      personIds: array — IDs das pessoas
      columnId: string — ID da coluna kanban
  RESPOSTA (campos):
      id: string — ID da tarefa
      title: string — Título
      priority: string — Prioridade
      status: string — Status
      initialDate: string — Data inicial
      finalDate: string — Data final
      isArchived: boolean — Arquivada
      isPrivate: boolean — Privada
      ownerId: string — ID do criador
      points: number — Pontos

## GET /user/ — Listar usuários
  query: page(string), limit(string), name(string), profile(string), orderBy(string)
  RESPOSTA (campos):
      id: string — ID do usuário
      name: string — Nome completo
      email: string — E-mail de login
      document: string — CPF ou CNPJ
      contact: string — Telefone ou contato
      role: string — Cargo no escritório
      admissionDate: string — Data de admissão
      resignationDate: string — Data de demissão
      dateOfBirth: string — Data de nascimento
      ctpsNumber: string — Número da CTPS
      imageUrl: string — URL da foto
      oabNumber: string — Número da OAB
      personType: string — Tipo de pessoa. Ex: física, jurídica
      salary: string — Salário
      postalCode: string — CEP
      addressNumber: string — Número do endereço
      firstLogin: boolean — Primeiro acesso pendente
      showBannerAsaas: boolean — Exibir banner Asaas
      officeId: string — ID do escritório
      referralToken: string — Token de indicação
      profile: object — 
        id: string — ID do perfil
        name: string — Nome do perfil
      createdAt: string — Data de criação (ISO 8601)
      updatedAt: string — Data da última alteração (ISO 8601)

## POST /user/ — Criar usuário
  BODY:
      name: string — Nome completo
      email: string — E-mail de login
      password: string — Senha de acesso
      document: string — CPF ou CNPJ
      contact: string — Telefone ou contato
      role: string — Cargo no escritório
      profileId: string — ID do perfil de permissões
      officeId: string — ID do escritório
      admissionDate: string — Data de admissão
      resignationDate: string — Data de demissão
      dateOfBirth: string — Data de nascimento
      ctpsNumber: string — Número da CTPS
      imageUrl: string — URL da foto
      oabNumber: string — Número da OAB
      personType: string — Tipo de pessoa. Ex: física, jurídica
      salary: string — Salário
      postalCode: string — CEP
      addressNumber: string — Número do endereço
      kanbanBoardsIds: array — IDs dos quadros Kanban

## GET /user/me — Me
  query: scope(string)
  RESPOSTA (campos):
      office: object — Dados do escritório
        id: string — ID do escritório
        name: string — Nome do escritório
        email: string — E-mail do escritório
        logo: string — URL do logo
        site: string — Site
        document: string — CNPJ do escritório
        contact: string — Contato
        isArchived: boolean — Escritório arquivado
        referralToken: string — Token de indicação
        archivedAt: string — Data do arquivamento
        onboardingEmailSent: number — E-mails de onboarding enviados
        isDefaultDataImported: boolean — Dados padrão importados
        hasPromotion: boolean — Tem promoção ativa
        productivityReportsDate: string — Data dos relatórios de produtividade
        autoCompleteCalendarItems: boolean — Completar itens do calendário automaticamente
        createdAt: string — Data de criação
      user: object — Dados do usuário
        id: string — ID do usuário
        name: string — Nome completo
        email: string — E-mail de login
        imageUrl: string — URL da foto
        firstLogin: boolean — Primeiro acesso pendente
        showBannerAsaas: boolean — Exibir banner Asaas
        role: string — Cargo no escritório
        referralToken: string — Token de indicação
        profile: object — 
          id: string — ID do perfil
          name: string — Nome do perfil
          permissions: ? — Permissões do perfil

## GET /user/{id} — Buscar usuário
  RESPOSTA (campos):
      id: string — ID do usuário
      name: string — Nome completo
      email: string — E-mail de login
      document: string — CPF ou CNPJ
      contact: string — Telefone ou contato
      role: string — Cargo no escritório
      admissionDate: string — Data de admissão
      resignationDate: string — Data de demissão
      dateOfBirth: string — Data de nascimento
      ctpsNumber: string — Número da CTPS
      imageUrl: string — URL da foto
      oabNumber: string — Número da OAB
      personType: string — Tipo de pessoa. Ex: física, jurídica
      salary: string — Salário
      postalCode: string — CEP
      addressNumber: string — Número do endereço
      firstLogin: boolean — Primeiro acesso pendente
      showBannerAsaas: boolean — Exibir banner Asaas
      officeId: string — ID do escritório
      referralToken: string — Token de indicação
      profile: object — 
        id: string — ID do perfil
        name: string — Nome do perfil
      createdAt: string — Data de criação (ISO 8601)
      updatedAt: string — Data da última alteração (ISO 8601)