# football-bot - Fases 0 a 5

Bot de Telegram (fase seguinte) para publicar resultados de **Grêmio** e
**Corinthians**, uma mensagem por jogo assim que termina.

- **Fase 0** (`discover.py`): descobre os **IDs de equipa** e a **cobertura de
  competicoes** nos dois fornecedores e deixa `config/teams.yaml` preenchido.
- **Fases 1 e 2** (`app.py`): `sync` traz os jogos para `data/football.db`,
  `due` lista os finalizados por entregar e `mark-sent` grava a entrega
  (dedupe por `fixture_id` + `chat_id`).
- **Fase 4** (`telegram.py` + `app.py deliver`): envia cada jogo devido aos
  destinos de `config/chats.yaml` pela Bot API e grava `sent` (com
  `message_id`) **so depois** de um envio com sucesso.

## Chaves

| fornecedor | onde obter | tier gratuito |
| --- | --- | --- |
| football-data.org | <https://www.football-data.org/client/register> | 10 pedidos/min |
| api-football | <https://dashboard.api-football.com/> | 100 pedidos/dia |

Telegram: `TELEGRAM_BOT_TOKEN` no `.env` (obtido com `@BotFather`). O token
nunca e impresso nem incluido em mensagens de erro.

```powershell
Copy-Item .env.example .env
notepad .env                       # preencher FOOTBALL_DATA_TOKEN e TELEGRAM_BOT_TOKEN
pip install -r requirements.txt    # opcional: so PyYAML, para ler/escrever config
```

`.env` fica fora do git. As chaves viajam em cabecalhos HTTP e nunca sao
impressas nem gravadas em ficheiros.

## Comandos

```powershell
python discover.py                                      # config: Gremio e Corinthians, 10 jogos
python discover.py --provider apifootball --team Gremio --days 60
python discover.py --team-id fdorg:130 --limit 5        # salta a pesquisa
python discover.py --fdorg-competition BSA --fdorg-competition CLI
python discover.py --team Gremio --team Corinthians --days 60 --write-config --force
```

Flags: `--provider {fdorg,apifootball,both}`, `--team`, `--team-id`,
`--fdorg-competition` (repetivel; default `BSA`, `CLI`; maximo 3 codigos),
`--days` (30), `--limit` (10), `--out`, `--write-config`, `--force`.

## Comandos da Fase 1+2 (`app.py`)

```powershell
python app.py sync --days-back 10 --days-forward 7   # 1 pedido por bloco de 10 dias
python app.py due --limit 5                          # dry-run: imprime, nao grava
python app.py due --chat 123456789                   # fila real desse chat
python app.py mark-sent 555103 --chat 123456789 --message-id 42
```

- `sync`: `GET /v4/matches?competitions=BSA,CLI&dateFrom=...&dateTo=...`,
  filtra pelos `fdorg_id` de `config/teams.yaml` (1767/1779) e faz upsert em
  `data/football.db`. Nunca apaga linhas; um placar conhecido nunca e apagado
  por um NULL. Imprime `[ok] sync: N jogos (X finalizados, Y agendados)` e
  avisa quando um placar mudou.
- `due`: jogos `FINISHED`/`AWARDED` com placar e sem linha em `sent` para esse
  chat. Sem `--chat` usa `dry-run` e **nao** escreve em `sent`.
- `mark-sent`: idempotente (`INSERT OR IGNORE` na chave `(fixture_id, chat_id)`).

## Destinos Telegram (`config/chats.yaml`)

O ficheiro real tem IDs de conversas e por isso esta **no `.gitignore`**.
`config/chats.example.yaml` e o modelo publico: copie e preencha.

```yaml
chats:
  - key: privado
    chat_id: "123456789"    # string, mesmo para IDs negativos de canal
    kind: private            # channel | group | private
    content: results         # reservado; a Fase 4 entrega results
    silent: false            # true = sem notificacao sonora
```

`load_chats` recusa ficheiro ausente, `kind` desconhecido, `chat_id` vazio e
IDs placeholder (so zeros), para o exemplo nunca ser publicado por engano.

## Comandos da Fase 4 (`deliver`)

```powershell
python app.py deliver --dry-run                 # imprime, sem rede e sem gravar
python app.py deliver --only privado --limit 1  # um jogo, num destino
python app.py deliver --limit 5                 # todos os chats configurados
```

- Por cada chat: jogos `FINISHED`/`AWARDED` com placar e sem linha em `sent`
  para esse `chat_id`, **do mais antigo para o mais recente**, ate `--limit`
  (default 20).
- Renderiza com `render_match`, envia e so depois grava
  `sent(fixture_id, chat_id, message_id)`. `--dry-run` nao escreve nada.
- Falha num destino (`403`, `400`, rede) imprime
  `[erro] <key> <fixture_id>: <descricao>` e continua com o proximo jogo/chat.
- `429` respeita `parameters.retry_after` e repete **uma vez**.
- Entre dois envios ao mesmo chat espera 1 s (limite por chat do Telegram).
- Saida `1` apenas se houve falha e nada foi entregue.
- `0 pendentes` significa que todos os jogos devidos ja estao em `sent`.

Formato da mensagem (`render.py`, texto simples, ASCII):

```
Brasileirao Serie A - Rodada 37
Corinthians 1 x 1 Atletico Mineiro
25/11 21:00 (Luxembourg)

Estadio: Neo Quimica Arena
```

Fases de copa substituem "Rodada N" (`Copa Libertadores - Quartos de final`);
penaltis acrescentam `(pen. 4-3)`; a linha `Estadio:` desaparece quando a API
nao devolve recinto. Hora local via `zoneinfo` (`Europe/Luxembourg`).

## Resolucao de equipas (roster)

No football-data.org a pesquisa global `/v4/teams?name=` e **instavel no plano
gratuito**: ignora o filtro e devolve dezenas de clubes aleatorios. Por isso a
resolucao e feita por **roster**:

1. Para cada codigo de `--fdorg-competition` (default `BSA`, `CLI`), faz
   `GET /v4/competitions/{CODE}/teams` e compara o nome com `normalise()`.
2. Match exato normalizado ganha; senao o unico clube cujo nome contem o termo.
3. Cada roster e pedido no maximo uma vez por execucao (cache em memoria).
4. Se a equipa nao aparecer em nenhum roster, so entao tenta a pesquisa global
   (ultimo recurso) e imprime
   `[aviso] ... busca global instavel no plano free`.

Os pedidos sao espacados em **6.5 s** (limite de 10/min), por isso uma ronda com
2 equipas demora ~40 s. Isso e normal, nao e um bloqueio.


## Ler a saida

- `[ok] ... -> 9001 (Gremio FBPA / roster: BSA)` - ID escolhido e rosters onde
  a equipa apareceu (fonte `roster`, `global`, `config` ou `override`).
- `[aviso] N candidato(s)` - varios nomes devolvidos; ganha o match exato, senao
  o unico que contem o termo.
- Tabela `DATE UTC | COMPETITION | HOME | AWAY | SCORE | STATUS`; `SCORE` fica
  `-` quando a API omite o placar (nunca assume 0-0).
- `[ok] N jogos | competicoes: ...` - resumo da janela.
- `[ok] <chat> <fixture_id> <home> x <away> -> message_id=N` - entrega
  confirmada e gravada em `sent`.
- `[erro] <chat> <fixture_id>: ...` - falha isolada; a execucao continua.
- `[erro] ...` - falha isolada, segue para o proximo. Exit `1` se todos os
  fornecedores falharem, `2` se nao houver chaves.

`docs/coverage.md` e reescrito a cada execucao (mantendo seccoes de fornecedores
que nao correram) e termina num checklist manual.

## Limitacoes do tier gratuito

- football-data.org free expoe **13 competicoes**:
  `BSA, ELC, PL, CL, EC, FL1, BL1, SA, DED, PPL, CLI, PD, WC` (verificado a
  2026-11-26). **BSA** (Brasileirao Serie A) e **CLI** (Copa Libertadores)
  estao incluidas.
- **Copa do Brasil NAO esta disponivel** no free tier; **campeonatos estaduais**
  (Gauchao, Paulistao) **tambem nao**.
- A pesquisa global de equipas (`/v4/teams?name=`) e instavel no free tier; a
  resolucao usa rosters de competicoes.
- Limite de **10 pedidos/minuto**; `discover.py` espaca chamadas em 6.5 s.
  `app.py sync` gasta 1 pedido por bloco de 10 dias (2 na janela default).
- api-football: 100/dia chega para descoberta, nao para polling continuo.
- `--days` filtra so a lista devolvida (`status=FINISHED` / `last=N`).
- Nomes de competicao diferem entre fornecedores: a Fase 1 precisa de aliases.


## Rodando no GitHub

O agendamento vive em `.github/workflows/` (repo publico = Actions gratis):

| workflow | quando | o que faz |
| --- | --- | --- |
| `check.yml` | cron `*/5 21-23,0-5 * * *` (UTC) + manual | gate local, sync de 3 dias e entrega |
| `sync.yml` | cron `13 5 * * *` + manual | `sync --days-back 0 --days-forward 7` |
| `test-send.yml` | manual | `deliver` de teste (`canal`, `privado` ou `all`) |

Passo a passo, botao a botao:

1. GitHub -> **New repository** -> nome `football-bot` -> **Public** ->
   Create repository. Enviar `main` (o `data/football.db` vai no repo).
2. **Settings -> Secrets and variables -> Actions -> New repository secret**:
   criar os 4 segredos
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_CANAL`, `TELEGRAM_CHAT_PRIVADO`,
   `FOOTBALL_DATA_TOKEN`.
3. **Settings -> Actions -> General -> Workflow permissions**:
   escolher **Read and write permissions** (o `check`/`sync` commitam
   `data/football.db`) e guardar.
4. Separador **Actions** -> aceitar a ativacao dos workflows.
5. **Actions -> sync -> Run workflow** (`main`): primeira carga manual.
   Confirmar o commit `chore: estado ...` com a DB.
6. **Actions -> check -> Run workflow**: primeira execucao do gate. Fora da
   janela imprime `[ok] check: nada na janela (N jogos futuros no banco)` e
   nao cria commit.
7. (Opcional) **Actions -> test-send -> Run workflow** com `dry_run` ligado
   antes de experimentar um envio real.

`config/chats.yaml` e **gerado no runner** a partir de
`TELEGRAM_CHAT_CANAL`/`TELEGRAM_CHAT_PRIVADO` e nunca deve ser commitado: tem
IDs reais e ja esta no `.gitignore`. O commit de `data/football.db` so acontece
quando o ficheiro muda mesmo (`git diff --quiet`); um `check` sem jogos na
janela nao produz commit nenhum.

Gate do `check`: um jogo so e consultado depois de
`kickoff + margem + grace` (120 + 15 min em `BSA`, 165 + 15 min em `CLI`).
`--margin-min CODE=MIN` sobrepoe a margem e `--grace-min` o grace.