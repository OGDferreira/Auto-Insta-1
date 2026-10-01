# Auditoria técnica, de segurança, performance e UX/UI — Auto-Insta

**Repositório auditado:** `OGDferreira/Auto-Insta-1`  
**Stack observada:** Python 3.11+, FastAPI, SQLAlchemy async, PostgreSQL/SQLite, APScheduler, Supabase Storage, Meta/Instagram Graph API, Google Drive OAuth, Sharkbot Webhooks e frontend Jinja/JavaScript/CSS.  
**Data da auditoria:** 2026-10-01  
**Escopo:** análise estática do repositório, fluxo de autenticação/OAuth, isolamento multiusuário, filas/loops, webhooks, notificações, uploads, operação no Render e experiência de uso. Não foi fornecida uma URL pública de produção para teste exploratório ponta a ponta; portanto, conclusões de runtime devem ser confirmadas no ambiente publicado.

## Resumo executivo

O projeto é um **monólito modular funcional**, adequado para continuar em FastAPI/Python no curto e médio prazo. Não recomendo uma reescrita para outra linguagem agora: o maior ganho virá de separar responsabilidades, introduzir migrações formais, mover jobs para um worker dedicado e fortalecer segurança/observabilidade.

A suíte atual passa: **40 testes aprovados**. A compilação Python e `git diff --check` também passaram. Isso indica uma base funcional, mas os testes atuais cobrem principalmente regras de negócio e não cobrem suficientemente segurança, concorrência, produção multi-instância, acessibilidade e contratos de integração.

### Riscos prioritários encontrados

| Prioridade | Achado | Impacto |
|---|---|---|
| **Alta** | Não há proteção CSRF visível para as rotas autenticadas que alteram estado | Um site malicioso pode induzir um usuário logado a excluir contas, publicar, alterar perfil ou modificar loops |
| **Alta** | APScheduler roda dentro do processo web | Em mais de uma instância pode haver publicação duplicada, notificações duplicadas e corrida na coleta de métricas |
| **Alta** | `TrustedHostMiddleware` está com `allowed_hosts=["*"]` | Reduz a proteção contra Host Header Injection e dificulta detectar tráfego para host indevido |
| **Alta** | OAuth Instagram aceita o `user_id` do estado assinado quando a sessão não está presente | O estado deve ser vinculado à sessão iniciadora; aceitar callback sem a sessão amplia risco de login/account-linking CSRF |
| **Alta** | Endpoint Sharkbot legado sem token continua aceitando e atribuindo evento por `instagram_user_id` | Um terceiro que conheça o identificador pode tentar injetar evento no workspace de outro usuário |
| **Média/Alta** | O frontend ainda contém limite de 30 mídias apesar da intenção de remover a limitação | A funcionalidade solicitada fica inconsistente: template e JS bloqueiam a playlist antes do backend |
| **Média** | Migrações são ad-hoc dentro de `init_db`, embora Alembic esteja instalado | Evolução de produção fica frágil, sem histórico, rollback e revisão de schema confiável |
| **Média** | Logs e diagnósticos retornam respostas detalhadas da Meta | Pode expor identificadores, dados de clientes e detalhes operacionais na interface de logs |

---

## 1. Arquitetura e stack tecnológica

### Arquitetura atual observada

- `app/main.py` cria a aplicação FastAPI, monta `/static` e `/uploads`, configura sessão, registra routers e inicializa banco/scheduler.
- `app/routes.py` concentra grande parte das páginas, autenticação, uploads, campanhas, analytics, contas, loops e APIs.
- `app/webhooks.py` concentra integração Sharkbot, regras de automação, contatos e respostas automáticas.
- `app/jobs.py` concentra publicação, recuperação de posts, métricas, notificações push e avanço de loops.
- `app/models.py` concentra o modelo relacional.
- Templates Jinja e JavaScript em módulos convivem com um bloco inline muito grande em `dashboard.html`.
- Supabase é usado para armazenamento público de mídia; PostgreSQL é esperado no Render via `DATABASE_URL`, com SQLite como fallback local.

### Recomendação de stack

**Manter Python + FastAPI + SQLAlchemy async.** A stack atual é coerente com integrações HTTP, OAuth, processamento assíncrono e tarefas de publicação. A troca para Node/NestJS, Go ou Django não resolveria os gargalos principais e criaria custo de migração sem benefício imediato.

| Opção | Vantagens | Desvantagens | Recomendação |
|---|---|---|---|
| FastAPI atual, modularizado | Reaproveita código e integrações; bom desempenho I/O; tipagem gradual | Exige disciplina de arquitetura; parte da aplicação está concentrada em arquivos grandes | **Escolha recomendada agora** |
| Django + Celery | Admin, ORM, auth e jobs maduros | Migração grande; adaptação das integrações async; custo alto | Considerar apenas numa reescrita futura |
| Node/NestJS | Ecossistema web e filas forte | Reescrita total e duplicação de conhecimento | Não justificado neste momento |
| Go | Baixo consumo e alta concorrência | Reescrita das integrações, templates e OAuth | Adequado apenas para um worker futuro específico |

### Arquitetura-alvo sugerida

```text
Web/API FastAPI
  ├─ Auth/OAuth
  ├─ Accounts
  ├─ Loops/Queue
  ├─ Analytics
  ├─ Sharkbot Webhook
  └─ Notifications
        │
        ├── PostgreSQL (fonte de verdade)
        ├── Object Storage (mídia)
        └── Fila/Worker (publicação, métricas e push)

Worker separado
  ├─ publica posts com lock transacional
  ├─ avança loops
  ├─ coleta insights
  ├─ processa notificações
  └─ executa retries/backoff
```

A separação não precisa acontecer em um único deploy: inicialmente pode ser um segundo serviço Render apontando para o mesmo repositório e banco, com um comando de entrada diferente.

---

## 2. Auditoria de segurança

### 2.1 Proteção CSRF — **Alta**

Todas as rotas autenticadas usam cookie de sessão e há várias mutações por `POST`, `PUT`, `PATCH` e `DELETE`, mas não foi encontrado middleware ou token CSRF nos formulários e requests JS.

Evidências:

- `SessionMiddleware` está ativo em `app/main.py:19-25`.
- Há dezenas de endpoints de mutação, por exemplo contas, perfil, loops, posts e automações em `app/routes.py:793+` e `app/routes.py:3101+`.
- Requests `fetch` do dashboard enviam JSON, mas não demonstram token CSRF.

**Correção recomendada:**

1. Gerar token CSRF aleatório por sessão.
2. Inserir token em todos os formulários Jinja.
3. Enviar `X-CSRF-Token` nos `fetch` de mutação.
4. Criar dependência global para exigir token em métodos inseguros autenticados.
5. Usar `Origin`/`Referer` como defesa complementar, não como única defesa.
6. Testar cada rota destrutiva e de configuração.

### 2.2 OAuth Instagram — **Alta**

O estado é assinado e possui expiração, o que é positivo. Porém, no callback a aplicação restaura `session["user_id"]` a partir do `signed_state` quando não existe sessão, conforme `app/routes.py` no callback Instagram. O comentário no código informa que isso foi feito para tolerar retorno sem cookie.

Isso resolve parte do erro `OAuth state inválido`, mas enfraquece a garantia clássica de que o callback pertence à sessão que iniciou o fluxo. O correto é:

- manter um nonce aleatório na sessão;
- gravar o estado assinado com `user_id`, `nonce`, `redirect_uri`, timestamp e contexto de reconexão;
- exigir sessão existente e igualdade do nonce salvo;
- permitir recuperação somente via fluxo explicitamente reautenticado, não automaticamente;
- validar `redirect_uri` exatamente igual ao cadastrado na Meta;
- não registrar a URL completa de autorização em log de nível warning em produção.

A correção deve preservar o tratamento de `error`, `error_reason` e `error_description` para que o usuário receba uma mensagem amigável em vez de uma tela crua JSON.

### 2.3 Host Header e headers de segurança — **Alta/Média**

`TrustedHostMiddleware` está configurado com `allowed_hosts=["*"]` em `app/main.py:26`. Isso elimina o benefício da proteção. Em produção, usar uma lista explícita:

- `auto-insta-aeqr.onrender.com`;
- domínio personalizado, quando existir;
- host local apenas em desenvolvimento.

Adicionar também:

- `Content-Security-Policy` progressiva;
- `X-Content-Type-Options: nosniff`;
- `Referrer-Policy: strict-origin-when-cross-origin`;
- `Permissions-Policy` mínima;
- `frame-ancestors 'none'` via CSP;
- HSTS somente quando o domínio estiver integralmente em HTTPS.

### 2.4 Segredos e defaults inseguros — **Alta**

`app/config.py:28-30` contém defaults como `change-me-in-production` para `SECRET_KEY` e token de verificação. Embora o Render tenha `SECRET_KEY` configurado no `render.yaml`, produção deve falhar no startup se segredos obrigatórios estiverem ausentes ou com valor padrão.

Também convém revisar os nomes das variáveis para evitar divergências entre painel e código: o projeto usa `SERVICE_ROLE` para a chave de serviço do Supabase, enquanto a convenção mais clara seria `SUPABASE_SERVICE_ROLE_KEY`.

### 2.5 Webhook Sharkbot e isolamento multiusuário — **Alta**

O endpoint tokenizado `/webhook/sharkbot/{webhook_token}` filtra contas por `owner_id` (`app/webhooks.py:315-347` e `409-414`), o que é correto. Contudo, as rotas sem token continuam habilitadas (`app/webhooks.py:298-301`). Sem token, o evento pode ser associado globalmente pelo `instagram_user_id` e gravado para o dono encontrado.

Recomendação:

- tornar obrigatório o token por usuário;
- remover ou desativar o endpoint global após período de migração;
- aceitar apenas uma URL personalizada por usuário, com token aleatório de alta entropia;
- validar assinatura HMAC do provedor quando disponível;
- aplicar rate limit por token/IP;
- registrar `webhook_token_id`, não o token bruto;
- adicionar índice/constraint de idempotência no banco.

Além disso, o duplicate check só é usado quando há `webhook_id`. Eventos sem esse campo podem ser duplicados. Deve existir uma chave idempotente normalizada com `owner_id + source_event_id` ou hash do payload canônico.

### 2.6 Uploads e mídia — **Média/Alta**

O upload limita bytes (`app/routes.py:793-846`), usa nome UUID e envia para Storage, o que é bom. Porém:

- valida apenas extensão/content-type declarado pelo cliente;
- não valida assinatura real do arquivo nem dimensões/duração;
- não há quota por usuário;
- não há limpeza de objetos órfãos quando um loop/post é excluído;
- o bucket retorna URL pública, o que pode ser inadequado para mídias privadas ou com informações sensíveis;
- importação de pastas do Drive pode acumular conteúdo em memória e executar muitas chamadas sequenciais.

Adicionar validação com Pillow/ffprobe, quotas, limites de quantidade por requisição, limpeza assíncrona e URLs assinadas quando privacidade for necessária.

### 2.7 Logs e dados sensíveis — **Média**

Existe redator de query string em `observability.py`, mas o sistema também registra respostas detalhadas da Meta no diagnóstico e expõe logs por `/api/logs`. É necessário:

- mascarar IDs, nomes de clientes e payloads de negócio;
- separar logs operacionais de logs de auditoria;
- limitar logs por role e workspace;
- aplicar retenção e paginação;
- nunca registrar tokens, códigos OAuth, credenciais Drive ou URLs privadas;
- retornar ao usuário apenas mensagens operacionais seguras.

---

## 3. Qualidade do código e manutenção

### Pontos positivos

- SQLAlchemy async e consultas geralmente escopadas por `owner_id`.
- Tokens Instagram/Drive são criptografados antes de persistência.
- Há funções de validação e normalização de eventos.
- O webhook possui tentativa de deduplicação.
- O loop remove contas com erro e preserva histórico publicado, conforme lógica de `app/jobs.py`.
- Existem testes de colaboradores, loops, métricas e alertas.
- `compileall` e `git diff --check` passaram.

### Pontos de atenção

| Área | Evidência/diagnóstico | Ação |
|---|---|---|
| Arquivos grandes | `routes.py`, `dashboard.html` e scripts inline concentram muita responsabilidade | Extrair módulos por domínio e componentes de UI |
| Migrações | `alembic` está em `requirements.txt`, mas o runtime usa criação/migrações manuais | Criar baseline Alembic e bloquear schema incompatível |
| Tipagem | Muitos payloads são `dict` sem schemas Pydantic | Criar DTOs Pydantic para webhook, loops, uploads e campanhas |
| Erros | Vários `except Exception` convertem problemas diferentes em respostas genéricas | Usar exceções de domínio e códigos de erro rastreáveis |
| Concorrência | `asyncio.create_task` para resposta atrasada não é persistente | Colocar atraso em fila durável |
| Testabilidade | Regras dependem de globals/settings/scheduler | Injetar clock, cliente Meta, storage e scheduler |
| Frontend | Muito JavaScript inline e responsabilidades duplicadas | Consolidar módulos, build e componentes reutilizáveis |
| Dependências | Há avisos de depreciação de `on_event`, `TemplateResponse` e Supabase/gotrue | Planejar upgrade antes de quebrar versões |

---

## 4. Performance e confiabilidade

### 4.1 Scheduler no processo web — **Alta**

`app/main.py:31-37` inicia o APScheduler no startup de cada processo. `app/jobs.py:347-379` registra jobs periódicos. Em escala horizontal, cada instância executará os mesmos jobs. Mesmo com update condicional em alguns posts, isso pode causar:

- chamadas duplicadas à Meta;
- coleta repetida de insights;
- notificações duplicadas;
- custo de API e rate limit;
- condições de corrida ao avançar loops.

**Plano:** mover scheduler para worker único ou usar fila distribuída. No mínimo, usar lock PostgreSQL (`pg_advisory_lock`) ou uma tabela de leases para garantir um único líder.

### 4.2 Publicação e avanço do loop

A publicação marca o post como `processing` antes da chamada externa, o que é uma boa defesa. Ainda assim, chamadas externas não são transacionais: se a Meta publicar e o processo cair antes do commit, o retry pode publicar novamente. Implementar idempotência operacional por container/publication attempt e reconciliação de status.

O avanço do loop consulta e recria itens com base em estado de banco. Adicionar testes de concorrência para dois workers, falha após publicação e substituição de playlist durante execução.

### 4.3 Métricas e Feed

Analytics percorre contas e mídia sequencialmente, com várias chamadas por conta e por mídia. Para muitas contas isso degradará o tempo de resposta da página/API. Melhorias:

- persistir métricas em background;
- usar limites/paginação explícitos;
- cache curto por `owner_id + período + contas`;
- concorrência limitada por semáforo;
- endpoint assíncrono com job/status para relatórios grandes.

### 4.4 Upload e importação Drive

A leitura do arquivo todo em memória é aceitável em 50 MB e baixa escala, mas não em alta concorrência. Usar streaming para Storage, limite de request no proxy, fila de importação de pastas e progress/status para operações grandes.

---

## 5. UX/UI e layout

### Diagnóstico

A direção visual é consistente com um dashboard escuro, cards, ícones Lucide e tabs inferiores em mobile. A imagem do Hub mostra, porém, problemas de densidade e hierarquia:

- cards de contas muito altos e repetitivos;
- ações aparecem como ícones pouco autoexplicativos;
- sobreposição visual e pouco respiro em telas estreitas;
- ações destrutivas sem contexto suficientemente visível;
- notificações/sino competem com navegação;
- filas e contas compartilham muita informação no mesmo painel.

### Estrutura visual sugerida

1. **Shell global**: marca, workspace atual, status da conexão, notificações e menu de usuário.
2. **Navegação principal**: Visão geral, Contas, Fila/Loops, Automações, Analytics, Logs & Sistema.
3. **Visão geral**: quatro métricas principais, alertas críticos, atividade recente e CTA principal.
4. **Hub de contas**: filtros por status, busca, seleção em massa, cartões compactos e painel lateral de detalhes.
5. **Fila/Loops**: cada loop como card resumido com contas associadas, intervalo, próxima publicação, status e botão “Editar mídias”.
6. **Editor de loop**: playlist reorderável, substituir tudo, adicionar/remover, pré-visualização e confirmação do impacto.
7. **Logs**: filtros por nível, período, domínio e conta; detalhes em drawer, sem payload sensível por padrão.

### Melhorias de interação

- Usar texto + tooltip nos botões de reconectar, copiar webhook e remover.
- Substituir `alert`/`confirm` nativos por dialogs acessíveis com foco controlado.
- Mostrar estado de loading, sucesso, erro e retry em toda ação assíncrona.
- Seleção em massa deve apresentar barra contextual com contagem e confirmação clara.
- Contas com erro devem aparecer agrupadas no topo com ação “Remover contas com erro” e resumo do impacto nos loops.
- Manter mensagens de erro no contexto da ação, não somente no toast.
- Adicionar empty states explicativos para “nenhuma conta”, “nenhum loop” e “nenhuma notificação”.
- Garantir contraste AA, foco visível, navegação por teclado e labels acessíveis.
- Reduzir CSS inline e adicionar breakpoints para 320, 375, 768, 1024 e desktop.

### Inconsistência funcional confirmada

Apesar da intenção de liberar qualquer quantidade de mídias, ainda há:

- nota visual “Limite de 30 publicações por lote” em `app/templates/dashboard.html:502`;
- bloqueio `media.length >= 30` em `app/templates/dashboard.html:837`;
- bloqueio `media.length > 30` em `app/templates/dashboard.html:934`.

O backend da substituição já aceita playlist variável, mas a experiência ainda impede o usuário no navegador. Remover os três bloqueios e trocar por limites operacionais configuráveis de tamanho total/quantidade somente se forem realmente necessários.

---

## 6. Plano de ação priorizado

### Prioridade Alta — antes de ampliar tráfego

1. **Implementar CSRF** em formulários e APIs autenticadas.
2. **Endurecer OAuth Instagram**: estado vinculado à sessão, nonce, expiração, redirect URI estrito e mensagens de erro amigáveis.
3. **Desativar webhook Sharkbot sem token** após migração e exigir URL/token por usuário.
4. **Adicionar idempotência no banco** para eventos Sharkbot e publicação.
5. **Remover `allowed_hosts=["*"]`** e configurar hosts permitidos por ambiente.
6. **Falhar startup com segredos obrigatórios ausentes/defaults inseguros**.
7. **Separar scheduler/worker do web** ou implementar leader lock PostgreSQL.
8. **Corrigir definitivamente o frontend do limite de 30 mídias**.
9. **Adicionar testes de segurança e concorrência**: CSRF, isolamento de workspace, webhook sem token, OAuth sem sessão, dois workers no mesmo post.

### Prioridade Média — estabilidade e escala

1. Migrar schema para Alembic com revisão inicial e migrações idempotentes.
2. Extrair `routes.py` em módulos: auth, accounts, queue, loops, analytics, integrations.
3. Criar schemas Pydantic para payloads de API e webhook.
4. Trocar `asyncio.create_task` atrasado por fila durável.
5. Implementar retry com backoff, circuit breaker e limites por integração externa.
6. Persistir/cachar analytics e mover consultas pesadas para jobs.
7. Validar MIME real, duração e dimensões de mídia; criar quota por workspace.
8. Limpar objetos órfãos do Storage quando posts/loops forem excluídos.
9. Melhorar logs com correlação por request, workspace e publicação, sem dados sensíveis.
10. Adicionar error tracking externo e métricas de latência/erro.

### Prioridade Baixa — produto e acabamento

1. Consolidar JavaScript inline em módulos com build/versionamento.
2. Adicionar design tokens, componentes de botão/card/dialog e CSS responsivo organizado.
3. Adicionar acessibilidade automatizada (axe/Lighthouse) no CI.
4. Adicionar testes E2E para login, conexão Instagram, criação/edição de loop, webhook e remoção em massa.
5. Criar documentação operacional: variáveis, callback Meta/Google, webhook Sharkbot, restore e runbook de incidentes.
6. Adicionar feature flags para mudanças sensíveis de publicação e webhooks.

---

## 7. Critérios de aceite recomendados

- Um usuário nunca visualiza ou grava eventos Sharkbot de outro workspace.
- Um callback OAuth sem a sessão iniciadora é rejeitado com mensagem orientativa, sem vincular conta indevidamente.
- Todo POST/PUT/PATCH/DELETE autenticado sem CSRF retorna `403`.
- Com duas instâncias do serviço, cada post é publicado no máximo uma vez.
- Uma conta em erro é removida dos loops ativos e deixa uma notificação persistente apenas para seu dono.
- Um loop pode substituir sua playlist preservando contas, intervalo e histórico publicado.
- O usuário consegue adicionar qualquer quantidade permitida pelo armazenamento e pela quota, sem bloqueio artificial de 30 no frontend.
- O webhook individual aparece no painel com botão “Copiar”, URL mascarada em logs e rotação de token disponível.
- Testes unitários, integração, segurança e E2E passam no CI.

## Conclusão

O Auto-Insta já possui uma base funcional e não precisa de reescrita de linguagem/framework. O próximo salto de qualidade depende de **segurança de sessão/webhook, execução confiável fora do processo web, migrações formais e decomposição do frontend/backend**. A correção mais urgente é impedir mutações autenticadas sem CSRF e retirar a aceitação global do webhook; a melhoria funcional mais evidente é eliminar o limite residual de 30 mídias e validar o editor de loops com testes de integração.
