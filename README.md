# football-bot - Fase 0

Bot de Telegram (fases seguintes) para publicar resultados de **Grêmio** e
**Corinthians**. A Fase 0 nao fala com o Telegram: descobre os **IDs de equipa**
e a **cobertura de competicoes** nos dois fornecedores de dados e deixa
`config/teams.yaml` preenchido. Prova que os nomes, os jogos terminados e as
competicoes chegam de forma utilizavel antes de desenhar a Fase 1.

## Chaves

| fornecedor | onde obter | tier gratuito |
| --- | --- | --- |
| football-data.org | <https://www.football-data.org/client/register> | 10 pedidos/min |
| api-football | <https://dashboard.api-football.com/> | 100 pedidos/dia |

```powershell
Copy-Item .env.example .env
notepad .env                       # preencher FOOTBALL_DATA_TOKEN e APIFOOTBALL_KEY
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
- api-football: 100/dia chega para descoberta, nao para polling continuo.
- `--days` filtra so a lista devolvida (`status=FINISHED` / `last=N`).
- Nomes de competicao diferem entre fornecedores: a Fase 1 precisa de aliases.

