# Auto-Insta

Aplicação multi-tenant para conectar contas pelo **Instagram Login for Business**, agendar fotos/vídeos e responder eventos básicos de mensagens e comentários. A aplicação usa FastAPI, SQLAlchemy 2 assíncrono com SQLite, APScheduler e sessões assinadas.

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

O endpoint `/media/upload` envia os bytes originais para o bucket público `SUPABASE_STORAGE_BUCKET` do Supabase Storage e devolve a URL pública do objeto. Em produção, configure `SUPABASE_URL`, `SUPABASE_KEY` e `SUPABASE_STORAGE_BUCKET` no Render. O bucket precisa existir, estar público para leitura e ter uma policy de `INSERT` para a chave usada pela aplicação; uma chave `service_role` pode ser usada no servidor para não depender de policy da chave anônima. O disco local do Render não é usado para armazenar as mídias.

## Meta Webhooks

Cadastre `https://SEU_HOST/webhook` no produto Instagram e use o mesmo `WEBHOOK_VERIFY_TOKEN`. O GET responde ao desafio `hub.challenge`; o POST aceita eventos `messaging` e `changes`, encontra a conta pelo `instagram_user_id` e, quando habilitado no dashboard, envia uma resposta automática pela API do Instagram. Configure também os campos de mensagens/comentários exigidos pelo painel Meta.

## Webhook Shark Bot

No Shark Bot, informe esta URL para receber pagamentos criados, pagamentos aprovados e novos leads:

`https://auto-insta-web.onrender.com/webhook/sharkbot`

O endpoint aceita os payloads `payment_created`, `payment_approved` e `user_joined`. Os valores da transação são gravados em BRL e os dados do cliente, bot, plano e transação ficam disponíveis no log e na tabela de métricas. Para uma instalação em outro domínio, substitua `auto-insta-web.onrender.com` pelo valor público de `PUBLIC_BASE_URL`.

## Deploy no Render

`render.yaml` cria apenas um Web Service Docker no plano gratuito. O SQLite e o APScheduler rodam na própria instância, sem serviços externos. Faça o blueprint apontar para este repositório, preencha os valores `sync: false` e defina `PUBLIC_BASE_URL` com a URL HTTPS do web service. O health check é `/health`.

## Estrutura

`app/config.py` configura ambiente; `models.py` contém o schema; `routes.py` implementa autenticação, dashboard, OAuth e CRUD; `jobs.py` publica agendamentos; `webhooks.py` trata eventos; `templates/` contém o HTML. O projeto não copia banco ou uploads de qualquer aplicação anterior.
