# Fila Loop: funcionamento atual e evolução proposta

Análise da base `74ffa93b832f725c1f98ed862965d2dcdf403d77`, em 28/09/2026. Este trabalho não altera o algoritmo da Fila Loop.

## Como funciona hoje

| Tema | Comportamento observado no código |
| --- | --- |
| Ordem | `create_bulk_posts` recebe a lista de mídias e grava `loop_index` na ordem enviada. Cria todas as mídias do primeiro ciclo para cada conta. Limite atual: 30 mídias por lote. |
| Início | Loop começa aproximadamente 3 minutos após a criação, independentemente do horário informado para um agendamento normal. |
| Intervalo | De 1 a 1.440 minutos. No primeiro ciclo, horários são início + posição × intervalo. Após esgotar a fila de uma conta, o próximo item é criado para agora + intervalo; processamento e atrasos podem deslocar os horários. |
| Reinício | Quando a conta não tem mais itens pendentes/processando, `_advance_loop_after_post` usa `(último índice + 1) % quantidade de índices`. Copia os dados do primeiro registro histórico daquele índice. Repetição infinita, sem contador nem limite de ciclos. |
| Várias contas | Cada conta tem sua própria progressão. O mesmo intervalo vale para todas no lote. As contas começam nos mesmos horários, sem espaçamento automático entre elas. |
| Novas contas | `_include_account_in_existing_loops`, chamado ao conectar uma conta autorizada, a inclui automaticamente em todos os loops ativos ou pausados do proprietário. Nos pausados, cria registros sem agendar sua execução. Também é possível incluir/remover contas no editor. |
| Novas mídias durante a execução | O editor permite nome, intervalo e contas; o pool de mídias é apenas de leitura. Não há operação específica para anexar mídias, removê-las da playlist ou reordená-las durante o loop. Criar outro lote não amplia a playlist existente. |
| Pausa/continuação | Já existem. Pausar desagenda pendências. Ao retomar, pendências são reagendadas; horários vencidos são convertidos para agora, podendo concentrar publicações. Uma publicação já em processamento pode terminar. |
| Falhas | Publicações recebem `failed` ou `blocked`, mensagem e horário do erro. O avanço também é chamado após falhar: a fila pode seguir, mesmo com problema persistente na conta. Há reenvio manual de falhas; não há política de tentativas limitadas com espera progressiva por tipo de erro. |
| Próximas/histórico | A interface mostra publicações e horários já materializados por conta, contadores de publicadas, pendentes e erros. Não calcula uma previsão completa de ciclos futuros. Registros de ciclos anteriores ficam no banco. |
| Reinício do servidor | Pendências são recarregadas no APScheduler. Uma rotina a cada 15 segundos recupera até 50 itens vencidos. Registros travados em `processing` e falhas entre concluir um item e criar o seguinte precisam de uma recuperação específica. |

Referências: `app/routes.py` (`create_bulk_posts`, `pause_batch`, `resume_batch`, `update_batch_accounts`, `_include_account_in_existing_loops`); `app/jobs.py` (`_publish`, `_advance_loop_after_post`, `schedule_post`, `schedule_pending_posts`, `process_due_posts`); `app/templates/dashboard.html`; `app/static/js/loop-manager.js`.

## Melhorias recomendadas, em ordem de implementação

1. **Separar playlist e histórico.** Criar itens persistentes da playlist com posição e identificador estável, mais um cursor por conta. Hoje os posts antigos fazem também o papel de templates. Deletar um post não equivale a remover uma mídia da playlist; lacunas nos índices podem impedir o avanço. Separar esses conceitos permite editar sem perder histórico.
2. **Editor de mídias.** Arrastar para ordenar, mover por botões acessíveis, incluir/remover sem recriar e editar legenda. Informar se a mudança vale imediatamente para pendências ou a partir do próximo ciclo. Não modificar um item já em publicação; remoção deve desativar o item para ciclos futuros.
3. **Pausa com retomada previsível.** Pausar por loop ou por conta. Ao continuar, distribuir pendências a partir de agora, respeitando intervalo e janela permitida. Oferecer opção explícita para pular atrasadas. Exibir quando há item ainda em processamento.
4. **Agenda e intervalos.** Escolher primeiro horário, fuso, dias da semana e janelas (inclusive atravessando meia-noite). Separar intervalo entre mídias, descanso entre ciclos e espaçamento entre contas; respeitar limites da API. Permitir configuração compartilhada ou ajustes individuais por conta.
5. **Próximas publicações.** Mostrar próxima mídia + conta + horário, tempo restante, e previsão das próximas 10–20 execuções. Distinguir previsão de agendamento confirmado e atualizar ao editar, pausar ou falhar.
6. **Ciclos e histórico.** Escolher infinito ou N ciclos, contar por conta e definir se o ciclo é concluído por tentativa ou por sucesso. Mostrar progresso, último sucesso, tentativas, erros e duração; filtrar/exportar histórico com paginação e retenção configurável.
7. **Falhas controladas.** Tentativas limitadas, espera progressiva para falhas transitórias e limites da API; pausar apenas a conta afetada quando o token expirar. Após esgotar tentativas, enviar para lista de intervenção, com ações de repetir/pular. Consultar a situação de uma publicação ambígua antes de reenviá-la para evitar duplicação.
8. **Escala e consistência.** Worker separado do processo web, fila durável, concorrência limitada por conta, bloqueio/lease e chave única por loop/conta/item/ciclo. Gravar conclusão e avanço de forma transacional, recuperar leases vencidos e agendar uma janela curta de próximos jobs. Paginar o dashboard em vez de carregar todo o histórico. Fazer a adesão de novas contas aos loops depender de seleção explícita, com prévia do volume.

Fluxo de configuração sugerido: **mídias → contas → agenda → ciclos → revisão das próximas publicações → iniciar**. O resumo deve mostrar número de contas × mídias, frequência estimada e regras de falha antes de ativar.
