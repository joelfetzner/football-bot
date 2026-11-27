# ESTADO E OPERAÇÃO

> Documento de transferência. Escrito para sobreviver a uma pausa: se o projeto
> ficar parado semanas, é por aqui que se recomeça.
> Última atualização: 2026-11-27.

---

## 1. Objetivo

**football-bot** — publicar no Telegram os resultados de **Grêmio** e
**Corinthians** (jogos terminados, resultado final), a partir de dados de
futebol obtidos por API.

A **Fase 0** não publica nada: descobre os IDs de equipa e mede a cobertura de
competições em dois fornecedores, para decidir com dados qual usar na Fase 1.
As **Fases 1 e 2** (feitas em 2026-11-26) trazem os jogos para SQLite, filtram
Gremio/Corinthians, deduplicam entregas por `(fixture_id, chat_id)` e renderizam
a mensagem em modo *dry-run*. A **Fase 4** (feita em 2026-11-27) liga essa fila
aos destinos de `config/chats.yaml` pela Bot API, gravando `sent` só depois de
um envio com sucesso. A **Fase 5** (feita em 2026-11-27) põe o ciclo a correr
no GitHub Actions: um gate local sem rede decide quando chamar a API, o cron
corre de 5 em 5 minutos 24 horas por dia e o `data/football.db` só é
commitado quando muda.

```
Fase 0  discover.py -> IDs + docs/coverage.md + config/teams.yaml
Fase 1  app.py sync -> data/football.db (upsert, nunca apaga)
        app.py due  -> mensagens de jogos finalizados por entregar
Fase 2  app.py mark-sent -> tabela sent (dedupe fixture_id + chat_id)
Fase 4  telegram.py + app.py deliver -> Bot API -> sent (message_id)
Fase 5  app.py check + .github/workflows -> Actions (gate local + cron 5 min)
```

## 2. Requisitos

- Descobrir o ID de cada clube em **football-data.org** e **api-football** por
  nome, tolerando acentos ("Gremio" vs "Grêmio") e nomes longos
  ("SC Corinthians Paulista").
- Listar jogos terminados recentes com data UTC, competição, equipas, golos e
  estado — sem inventar resultados quando a API omite o placar.
- Produzir `docs/coverage.md` com contagem por competição e checklist manual.
- Gravar `config/teams.yaml` no formato usado pela Fase 1.
- Nunca falhar com traceback por rede/HTTP/parse; nunca expor chaves.
- Sem emoji na consola (`[ok]`, `[erro]`, `[aviso]`); tudo UTF-8.
- Rede apenas via stdlib (`urllib.request` + `json`); `PyYAML` opcional e
  importado só dentro da função de configuração.
- Testes pytest sem rede.

## 3. Decisões

- **Dois fornecedores, um formato comum.** Cada fixture é normalizado para
  `{provider, team_id, date_utc, competition, round, home, away, goals_home,
  goals_away, status}`. `goals_*` ficam `None` quando não há placar.
- **Chaves em `.env`, nunca em código.** `load_env()` não sobrepõe variáveis já
  definidas no ambiente (CI/local ganham ao ficheiro).
- **Pesquisa tolerante mas conservadora.** `pick_team()`: match exato
  normalizado > único candidato que contém o termo > único candidato existente;
  caso contrário `AmbiguousTeamError` com a lista (nunca escolhe ao calhas).
- **IDs fixos ganham à pesquisa.** `--team-id`, ou `fdorg_id`/`apifootball_id`
  já preenchidos na config, evitam chamadas (poupa quota).
- **Resolução por roster no football-data.org.** A pesquisa global
  `/v4/teams?name=` é instável no plano free (ignora o filtro e devolve clubes
  de outra liga). A resolução percorre os códigos de `--fdorg-competition`
  (default `BSA`, `CLI`, máximo 3) via `/v4/competitions/{CODE}/teams`, com
  cache por execução (cada roster é pedido no máximo uma vez). A pesquisa
  global fica apenas como último recurso documentado, com
  `[aviso] ... busca global instavel no plano free`.
- **Ritmo de pedidos.** `RateLimiter` garante >= 6.5 s entre chamadas
  consecutivas ao football-data.org (10 pedidos/min no free tier); só dorme o
  que falta.
- **`--days` é filtro informativo.** Se esvaziar a lista, avisa e mostra sem
  filtro — melhor cobertura visível do que uma tabela vazia.
- **Cobertura faz merge por secção.** Reescreve o ficheiro mas preserva secções
  de fornecedores que não correram nesta execução.
- **Sem dependências de runtime.** `--write-config` escreve YAML à mão; só a
  leitura de `teams.yaml` usa PyYAML (com aviso se faltar).
- **Códigos de saída.** `0` sucesso (mesmo com falhas parciais), `1` todos os
  fornecedores falharam, `2` nenhuma chave disponível.
- **Uma mensagem por jogo, logo que termina (Fase 1+2).** Nada de resumo
  diário: `due` devolve os finalizados ainda não entregues, por ordem de
  `utc_date`.
- **Dedupe `(fixture_id, chat_id)`.** `sent` tem chave primária composta;
  `mark-sent` usa `INSERT OR IGNORE` e é idempotente. `due` nunca escreve em
  `sent`: sem `--chat` usa o literal `dry-run` (modo de ensaio).
- **Âmbito BSA + CLI.** Um único pedido `/v4/matches` com
  `competitions=BSA,CLI`, filtrado localmente pelos `fdorg_id` da config
  (Gremio 1767, Corinthians 1779).
- **Upsert conservador.** `goals_*`/`penalties_*` ficam NULL quando a API os
  omite; o `ON CONFLICT` usa `COALESCE(excluded.x, matches.x)` para nunca
  apagar um placar já conhecido. Nenhuma linha é apagada.
- **Janela de 10 dias por pedido.** O free tier responde
  `400 Specified period must not exceed 10 days`; `sync` divide a janela
  (default 10 para trás + 7 para a frente = 2 pedidos, espaçados pelo
  `RateLimiter` de 6.5 s).
- **Mensagem em texto simples, ASCII.** `render_match()` é puro (sem I/O);
  `parse_mode` fica para a Fase 3. Nomes dos nossos clubes vêm do `display` da
  config; adversários vêm da API com acentos dobrados.
- **Horário local `Europe/Luxembourg`** via `zoneinfo`, formato
  `dd/mm HH:MM`. Coluna `venue` é uma extensão nullable ao esquema pedido (a
  linha `Estadio:` só aparece quando existe).
- **Telegram em stdlib (`telegram.py`).** `send_message` faz `POST` JSON em
  `/bot<token>/sendMessage` com `chat_id` string, `disable_web_page_preview:
  true` e `disable_notification: silent`. `transport(method, payload) -> dict`
  é injectável: os testes correm sem rede.
- **429 com uma única repetição.** Lê `parameters.retry_after`, dorme esse
  valor (sleeper injectável) e repete uma vez; um segundo 429 sobe como
  `TelegramError`. `403`/`400` sobem com `error_code` + `description`, sem
  traceback.
- **Token nunca aparece em erros.** `TELEGRAM_BOT_TOKEN` vem do `.env`; as
  mensagens de excepção passam por redacção (`***`) e o transporte nunca inclui
  a URL com token.
- **`config/chats.yaml` é gitignored.** Tem IDs reais; o modelo público é
  `config/chats.example.yaml`. `load_chats` recusa `kind` fora de
  `channel/group/private`, `chat_id` vazio e IDs placeholder (só zeros), para
  ninguém publicar o exemplo por engano.
- **`deliver` grava `sent` só após sucesso.** Reutiliza `due_matches` (mesma
  query de dedupe), do mais antigo para o mais recente, com `--limit` por chat.
  Um destino que falha imprime `[erro]` e não interrompe os restantes. Entre
  dois envios ao mesmo chat espera 1 s (limite por chat do Telegram).
  `--dry-run` não toca na rede nem escreve em `sent`.
- **Código de saída do `deliver`.** `1` só quando houve falha e nenhuma entrega;
  caso contrário `0` (falha parcial continua a ser execução útil).

## 4. Estado atual (2026-11-27)

- Fase 0 **executada com o token real** (football-data.org free) em 2026-11-26.
- `Gremio` -> **1767** (Grêmio FBPA, roster `BSA`).
- `Corinthians` -> **1779** (SC Corinthians Paulista, rosters `BSA` + `CLI`).
- `config/teams.yaml` tem `fdorg_id` preenchido para os dois clubes;
  `apifootball_id` continua `null` (`APIFOOTBALL_KEY` ausente).
- `docs/coverage.md` lista 8 jogos por clube, todos no Campeonato Brasileiro
  Série A; nenhum jogo de Copa Libertadores na janela de 60 dias.
- **Fase 1+2 executada com o token real** em 2026-11-26:
  `python app.py sync --days-back 10 --days-forward 7` ->
  `[ok] sync: 4 jogos (3 finalizados, 1 agendados)`; segunda execução manteve
  4 linhas (sem duplicados).
- `data/football.db` contém a ronda 35 (Corinthians 4-0 Botafogo,
  Gremio 0-0 Bahia), a ronda 36 (Santos 0-2 Gremio) e o
  **Corinthians x Gremio de 29/11 21:30 UTC (ronda 37, status TIMED)**.
- `python app.py due --limit 5` -> `[ok] 3 mensagens devidas` (modo dry-run,
  tabela `sent` continua vazia).
- **Fase 4 executada a 2026-11-27.** Bot `@fut_results_bot`; destinos em
  `config/chats.yaml` (gitignored): canal do Telegram + conversa privada.
  Em CI o ficheiro é gerado a partir dos secrets `TELEGRAM_CHAT_CANAL` e
  `TELEGRAM_CHAT_PRIVADO`.
- **Fase 5 executada a 2026-11-27.** Repositório público
  `github.com/joelfetzner/football-bot`, branch `main`, tag `v0.5.0`.
  Actions gratuitas e ilimitadas (repo público); budgets do GitHub em $0.
  - `sync.yml` (05:13 UTC): calendário de hoje..+7 dias, 1 pedido.
  - `check.yml` (`*/5 * * * *`): gate local, rede só se houver jogo.
  - `test-send.yml` (manual): `only`, `limit`, `dry_run`; não commita o banco.
  - `data/football.db` só é commitado quando muda (mtime/SHA verificados).
- **Entregas reais feitas:** 3 jogos no canal (rondas 35 e 36) e 1 no privado.
  O canal foi limpo e reenviado a 27/11 depois de adicionar subscritores.
- `python app.py check` -> `[ok] check: nada na janela (1 jogos futuros no
  banco)`, exit 0, sem rede e sem tocar no banco.
- `python -m pytest tests -q` -> **112 passed**, zero rede.
- `git status` limpo; sem pendências locais.

## 5. Bloqueios

- `APIFOOTBALL_KEY` continua ausente; a comparação com api-football fica por
  fazer. O football-data.org já está validado.
- Cobertura confirmada no free tier: **13 competições**, com `BSA` e `CLI`.
  **Copa do Brasil e campeonatos estaduais não existem** nesse plano.

## 6. Próximo passo

1. **29/11 21:30 UTC — Corinthians x Gremio (ronda 37).** Primeira entrega
   automática real. O gate libera a checagem às 23:45 UTC (apito + 120 min de
   margem + 15 de folga). Esperado: 1 mensagem no canal e 1 no privado,
   entre 23:45 e 00:15 UTC.
2. Verificar no dia seguinte: `gh run list --workflow=check.yml --limit 10` e
   o log do run das 23:45.
3. Se não chegar nada: ver runbook na secção 10.
4. **Futuro (não iniciado):** competições europeias. `PL`, `PD`, `SA`, `BL1`,
   `FL1`, `CL`, `ELC`, `PPL` estão no free tier e cabem no mesmo pedido
   (`?competitions=BSA,CLI,PL,...`). Requer margem por código (`CL` = 165 min)
   e cron 24h (`*/5 * * * *`), que num repo público custa $0. Com 8 competições,
   avaliar digest diário em vez de mensagem por jogo.

## 7. Arquivos relevantes

```
discover.py             CLI Fase 0 (argparse, urllib, parsers, cobertura)
app.py                  CLI Fase 1+2 (sync, due, mark-sent; SQLite)
render.py               render_match(): texto ASCII, Europe/Luxembourg
telegram.py             load_chats + send_message (Bot API, transport injectável)
data/football.db        matches + sent (portátil, sem caminhos absolutos)
config/teams.yaml       key, display, fdorg_id, apifootball_id
config/chats.yaml       destinos reais (gitignored)
config/chats.example.yaml  modelo público de chats.yaml
.env.example            nomes das chaves + onde obter + limites
docs/coverage.md        relatório por fornecedor, roster e checklist manual
tests/conftest.py       põe a raiz no sys.path
tests/test_discover.py  46 testes da Fase 0, sem rede
tests/test_app.py       40 testes das Fases 1+2+4, sem rede
tests/test_telegram.py  13 testes do cliente Telegram, sem rede
README.md               uso e limitações
requirements.txt        PyYAML>=6.0 (só config)
```

## 8. Contexto importante

- **Windows 11 + PowerShell.** Comandos em PowerShell; `Copy-Item`, não `cp`.
- **Consola sem emoji.** Só `[ok]` / `[erro]` / `[aviso]`; stdout reconfigurado
  para UTF-8 com `errors="replace"` no arranque.
- **Python 3.14.5**, invocado como `python`.
- **Rede só com a stdlib.** Nada de `requests`; `urllib.request` + `json`.
  PyYAML é a única dependência externa e é opcional.
- **Projeto vizinho `jobradar`**: referência de estilo/handover, somente
  leitura — não alterar.
- **Segredos.** Chaves só em `.env` (ignorado pelo git) ou no ambiente; erros
  HTTP mostram código/URL, nunca cabeçalhos.
- **Free tier football-data.org.** 13 competições
  (`BSA, ELC, PL, CL, EC, FL1, BL1, SA, DED, PPL, CLI, PD, WC`), 10 pedidos/min.
  A pesquisa global `/v4/teams?name=` é instável neste plano: resolver equipas
  pelos rosters de `BSA`/`CLI`.
- **Janela da API.** Cada pedido `/v4/matches` aceita no máximo 10 dias
  (`400 Specified period must not exceed 10 days`); a janela default do `sync`
  (10 para trás + 7 para a frente) gasta 2 pedidos.
- Actualizar este documento sempre que uma fase fechar.

## 9. Fase 5 - GitHub Actions (2026-11-27)

### Feito

- **`python app.py check [--limit N] [--dry-run]`.** Gate local sem rede:
  lê os jogos com `utc_date` em `[agora - 8 h, agora + 30 min]` numa conexão
  SQLite `mode=ro` (o ficheiro nem pode ser tocado); um jogo é "checkable"
  quando `status` não é `FINISHED`/`AWARDED` e `agora >= kickoff + margem +
  grace` (margem 120 min em `BSA`, 165 min em `CLI`, grace 15 min).
  - Nada checkable: imprime
    `[ok] check: nada na janela (N jogos futuros no banco)`, sai 0, sem rede e
    sem escrever na DB (o CI não cria commits vazios).
  - Algo checkable: `sync` de 3 dias (ontem..amanhã, **1 pedido**), a mesma
    entrega do `deliver` (todos os chats, com dedupe) e o resumo
    `[ok] check: X entregues, Y falhas, Z jogos atualizados`.
  - Saída `0` quando nada falhou; `1` quando houve falha de envio e nenhuma
    entrega; `2` quando falta uma chave.
- **`sync` não escreve quando nada mudou.** O upsert compara a linha existente
  com a nova; se todas as colunas forem iguais o `UPDATE` é saltado e o
  ficheiro da DB fica byte-idêntico. `updated_at` só avança quando muda
  `status`, golos, penalties ou `venue`. `sync` imprime agora
  `[ok] sync: N jogos (X finalizados, Y agendados, Z alterados)`.
- **Workflows** em `.github/workflows/`:
  - `check.yml`: cron `*/5 * * * *` (UTC) + `workflow_dispatch`,
    `contents: write`, concorrência `football-state` sem cancelamento,
    `timeout-minutes: 5`; gera `config/chats.yaml` a partir dos secrets e
    commita `data/football.db` apenas se `git diff --quiet` falhar.
  - `sync.yml`: cron `13 5 * * *`, `python app.py sync --days-back 0
    --days-forward 7` (1 pedido), mesmo commit condicional.
  - `test-send.yml`: manual (`only` canal/privado/all, `limit`, `dry_run`),
    **não** commita a DB (estado de teste tem de ser revisto).
- `tests/test_app.py`: +13 testes (gate BSA/CLI, janela, `--margin-min`,
  caminho sem nada checkable sem rede/escrita, caminho checkable sync+entrega,
  códigos de saída, sync no-op byte-idêntico, `updated_at` material).
  Total: **112 passed**, zero rede.

### Decisões

- Repositório **público**: minutos de Actions grátis, por isso o cron pode ser
  de 5 em 5 minutos 24 horas por dia.
- **Gate kickoff + margem + grace** em vez de sondar a API a cada 5 minutos:
  poupa quota do free tier (10 pedidos/min) e evita resultados provisórios.
- **DB commitada só quando muda**: o passo de commit compara
  `git diff --quiet -- data/football.db`; um `check` sem jogos não produz
  commit nenhum.
- `config/chats.yaml` continua gitignored e é **gerado no runner** a partir de
  `TELEGRAM_CHAT_CANAL`/`TELEGRAM_CHAT_PRIVADO`; nunca vai para o repo.

### Próximo passo

- 29/11: Corinthians x Grêmio (21:30 UTC). O primeiro `check` com o gate
  passado (~23:45 UTC) faz o `sync`, vê o jogo `FINISHED` e entrega a primeira
  mensagem real no `canal`; o `privado` segue a mesma fila.
- Confirmar no Actions que o commit automático da DB aparece uma única vez por
  alteração e que os runs "nada na janela" terminam verdes sem commit.
- Só depois disso considerar `parse_mode`/`content: results_and_scorers`.

## 10. Fecho de 2026-11-27 - operacao, privacidade e exposicao

### Runbook

- Ver execucoes: `gh run list --workflow=check.yml --limit 10`
- Forcar sincronizacao: `gh workflow run sync.yml`
- Envio manual: `gh workflow run test-send.yml -f only=privado -f limit=1 -f dry_run=false`
- Checagem local sem rede: `python app.py check`
- Quem esta na fila: `python app.py deliver --dry-run`
- Reverter codigo: `git revert <sha>` + push. O banco nao volta atras:
  `sent` e append-only de proposito (apagar linha = reenvio futuro).

### Se uma noite nao chegar mensagem (ordem de investigacao)

1. O cron rodou? `gh run list --workflow=check.yml` (roda 24h, de 5 em 5 minutos).
2. O log diz `nada na janela` (gate ainda nao liberou) ou erro de API?
3. O jogo esta `FINISHED` no banco? `python app.py due` local.
4. Jogo adiado nao gera erro: fica fora do gate e entra quando for jogado.

### Privacidade

- **Publico:** codigo, testes, `docs/`, `config/teams.yaml` e
  `data/football.db` (que contem os chat_id na tabela `sent` - estado do
  dedupe, nao credencial).
- **Privado:** `.env` (token do bot + chave da API) e `config/chats.yaml`
  (IDs reais). Ambos no `.gitignore`; em CI os IDs vem dos secrets.
- **Secret scanning + push protection**: ativos no GitHub.
- **Commits**: os primeiros usam o email pessoal; a partir de 27/11 o repo
  local usa `360575301+joelfetzner@users.noreply.github.com`.
- **Perfil**: o email do perfil esta `null` (nao publico). O login/avatar e a
  atividade publica sao visiveis, como em qualquer conta GitHub.

### Limites conhecidos

- Free tier football-data.org: 13 competicoes, 10 pedidos/min, janela maxima
  de 10 dias por pedido. Sem Copa do Brasil nem estaduais.
- Cron do GitHub atrasa 5-15 min sob carga; a entrega e "apos o apito", nao
  "ao vivo".
- Mensagem por jogo nao tem botao: o canal e somente leitura.