# Auto-Insta

Aplicação multi-tenant para conectar contas pelo **Instagram Login for Business**, agendar fotos/vídeos e responder eventos básicos de mensagens e comentários. A aplicação usa FastAPI, SQLAlchemy 2 assíncrono com SQLite, APScheduler e sessões assinadas.

Envios em massa são organizados como **lotes**. Cada lote pode ser nomeado, pausado e retomado sem apagar as publicações pendentes. A configuração do lote também permite adicionar ou remover contas: remoções afetam somente posts ainda pendentes, enquanto novas contas recebem os mesmos conteúdos e horários do lote.

No Dashboard, clique em uma conta para abrir suas publicações; o filtro também permite exibir todas as contas conectadas sem recarregar a página. É possível selecionar e excluir publicações individualmente, em massa ou todas as publicações da visualização atual, além de tentar novamente posts bloqueados ou com falha. O modo privacidade e os filtros da fila são mantidos no navegador. Ao conectar uma conta, a aplicação consulta a Meta e considera o perfil válido como ativo; somente falhas reais de autenticação, como o código 190, desconectam a conta.

A fila é apresentada por lotes expansíveis: cada lote inicia fechado, mostra uma prévia de até oito mídias e só renderiza o restante após o comando "Mostrar fila completa". O reenvio de falhas abre um intervalo configurável; a primeira tentativa é agendada para dois minutos após a confirmação e as seguintes respeitam esse intervalo. Se o objeto do Supabase não estiver mais disponível, o worker tenta resgatar a mídia do Google Drive no momento da publicação.

A aba **Feed** consulta as publicações atuais das contas selecionadas pela Graph API, permite apagar itens marcados ou limpar múltiplas contas e aplica throttling de dois segundos entre exclusões. O importador do Google Drive aceita arquivos e pastas inteiras, percorre subpastas com paginação e armazena `drive_media_url` e `drive_account_email` junto à publicação para permitir o resgate posterior.

A aba **Automações** mantém regras específicas por conta ou globais para respostas de comentários e mensagens Direct. Cada regra pode conter texto, mídia enviada pelo navegador ou mídia importada do Drive. No webhook Meta, uma regra específica tem prioridade sobre a regra global; quando há anexo, a mídia é enviada primeiro e o texto em uma mensagem separada. Mídias do Drive são resgatadas para o Supabase Storage antes do envio, garantindo uma URL pública para a Meta.

Publicações impedidas por autorização ficam como `blocked`, podem ser verificadas novamente depois que o usuário for adicionado como testador no Meta App Dashboard e possuem ação de tentativa novamente. A API não fornece uma consulta pública para confirmar diretamente a lista de testadores do aplicativo; por isso a aplicação valida o token, o perfil e as permissões efetivamente retornadas pela Meta.

## Arquitetura e segurança

- `owner_id` está presente em contas e publicações; todas as consultas da interface filtram pelo usuário autenticado.
- Senhas usam `werkzeug` com scrypt. A sessão fica em cookie assinado por `SECRET_KEY`, com `HttpOnly`, `SameSite=Lax` e `COOKIE_SECURE=true` em produção.
- Access tokens do Instagram são cifrados em repouso com Fernet (`FERNET_KEY`); nenhum token é exibido em templates.
- O callback valida um `state` aleatório armazenado na sessão.
- OAuth e publicação usam o fluxo Instagram Login: `www.instagram.com`, `api.instagram.com` e `https://graph.instagram.com/{GRAPH_API_VERSION}`. Tokens obtidos nesse fluxo não devem ser enviados para `graph.facebook.com`.
- `init_db()` executa `create_all` de forma idempotente no startup. Para evoluções posteriores, adicione migrações Alembic.

## Desenvolvimento local

1. Crie um ambiente Python 3.12 e instale:

   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   copy .env.example .env
   ```

2. Gere uma chave Fernet sem segredo no repositório:

   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Coloque o resultado em `FERNET_KEY`. O SQLite assíncrono é usado localmente e no deploy; o arquivo `auto_insta.db` é criado no diretório da aplicação.

3. Execute a aplicação:

   ```bash
   uvicorn app.main:app --reload
   ```

4. Testes e validação:

   ```bash
   pytest -q
   python -m py_compile app\*.py
   ```

## Configuração Meta

No painel Meta Developers crie um produto Instagram Login for Business e configure exatamente o redirect URI:
`https://auto-insta-web.onrender.com/auth/callback`.

Defina `META_APP_ID`, `META_APP_SECRET`, `PUBLIC_BASE_URL`, `GRAPH_API_VERSION` e todos os valores de `.env.example`. Os escopos solicitados são:
`instagram_business_basic`, `instagram_business_content_publish`,
`instagram_business_manage_messages` e `instagram_business_manage_comments`.

O worker cria um container em `/{ig_id}/media` e o publica em `/{ig_id}/media_publish` usando `https://graph.instagram.com/{GRAPH_API_VERSION}`. Vídeos são enviados como `REELS` e só são publicados após o container retornar `status_code=FINISHED`. Imagens são enviadas diretamente por `image_url`, sem polling. URLs de mídia precisam ser públicas para que o Instagram consiga buscá-las.

O endpoint `/media/upload` envia os bytes originais para o bucket público `SUPABASE_STORAGE_BUCKET` do Supabase Storage e devolve a URL pública do objeto. Em produção, configure `SUPABASE_URL`, `SUPABASE_STORAGE_BUCKET` e a variável `SERVICE_ROLE` (preferencial) ou `SUPABASE_KEY` no Render. O bucket precisa existir e estar público para leitura; a chave `service_role` permite o upload no servidor sem depender de policy da chave anônima. O disco local do Render não é usado para armazenar as mídias.

Uploads também geram uma miniatura JPEG leve. Após uma publicação bem-sucedida, o worker remove o original do bucket e mantém a miniatura para o histórico. Para importar mídias do Google Drive, configure `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` e `GOOGLE_REDIRECT_URI` no `.env` e cadastre o redirect URI no OAuth Client do Google Cloud Console.

Para notificações em celular e desktop, gere um par de chaves VAPID e configure `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY` e `VAPID_SUBJECT`. No menu de perfil, ative as notificações e use "Testar notificações". O navegador precisa estar em HTTPS (exceto localhost) e permitir notificações; em celulares, o navegador precisa aceitar Push API.

## Meta Webhooks

Cadastre `https://SEU_HOST/webhook` no produto Instagram e use o mesmo `WEBHOOK_VERIFY_TOKEN`. O GET responde ao desafio `hub.challenge`; o POST aceita eventos `messaging` e `changes`, encontra a conta pelo `instagram_user_id` e, quando habilitado no dashboard, envia uma resposta automática pela API do Instagram. Configure também os campos de mensagens/comentários exigidos pelo painel Meta.

## Webhook Shark Bot

No Shark Bot, informe esta URL para receber pagamentos criados, pagamentos aprovados e novos leads:

`https://auto-insta-web.onrender.com/webhook/sharkbot`

O endpoint aceita os payloads `payment_created`, `payment_approved` e `user_joined`. Os valores da transação são gravados em BRL e os dados do cliente, bot, plano e transação ficam disponíveis no log e na tabela de métricas. Para uma instalação em outro domínio, substitua `auto-insta-web.onrender.com` pelo valor público de `PUBLIC_BASE_URL`.

## Deploy no Render

`render.yaml` cria apenas um Web Service Docker no plano gratuito. O SQLite e o APScheduler rodam na própria instância, sem serviços externos. Faça o blueprint apontar para este repositório, preencha os valores `sync: false` e defina `PUBLIC_BASE_URL` com a URL HTTPS do web service. O health check é `/health`.

Depois de atualizar as dependências localmente:

```bash
python -m pip install -r requirements.txt
```

## Estrutura

`app/config.py` configura ambiente; `models.py` contém o schema; `routes.py` implementa autenticação, dashboard, OAuth e CRUD; `jobs.py` publica agendamentos; `webhooks.py` trata eventos; `templates/` contém o HTML. O projeto não copia banco ou uploads de qualquer aplicação anterior.
