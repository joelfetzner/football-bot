# ESTADO E OPERAÇÃO

> Documento de transferência. Escrito para sobreviver a uma pausa: se o projeto
> ficar parado semanas, é por aqui que se recomeça.
> Última atualização: 2026-11-26.

---

## 1. Objetivo

**football-bot** — publicar no Telegram os resultados de **Grêmio** e
**Corinthians** (jogos terminados, resultado final), a partir de dados de
futebol obtidos por API.

A **Fase 0** não publica nada: descobre os IDs de equipa e mede a cobertura de
competições em dois fornecedores, para decidir com dados qual usar na Fase 1.

```
Fase 0  discover.py -> IDs + docs/coverage.md + config/teams.yaml
Fase 1  leitura periódica -> deteção de jogos novos -> envio Telegram
Fase 2  agendamento (GitHub Actions ou tarefa local), estado persistido
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

## 4. Estado atual (2026-11-26)

- Fase 0 **executada com o token real** (football-data.org free) em 2026-11-26.
- `Gremio` -> **1767** (Grêmio FBPA, roster `BSA`).
- `Corinthians` -> **1779** (SC Corinthians Paulista, rosters `BSA` + `CLI`).
- `config/teams.yaml` tem `fdorg_id` preenchido para os dois clubes;
  `apifootball_id` continua `null` (`APIFOOTBALL_KEY` ausente).
- `docs/coverage.md` lista 8 jogos por clube, todos no Campeonato Brasileiro
  Série A; nenhum jogo de Copa Libertadores na janela de 60 dias.
- `python -m pytest tests -q` -> **46 passed**, zero rede.
- Git inicializado (`main`), sem commit e sem identidade configurada.

## 5. Bloqueios

- `APIFOOTBALL_KEY` continua ausente; a comparação com api-football fica por
  fazer. O football-data.org já está validado.
- Cobertura confirmada no free tier: **13 competições**, com `BSA` e `CLI`.
  **Copa do Brasil e campeonatos estaduais não existem** nesse plano.

## 6. Próximo passo (Fase 1)

1. Rever `docs/coverage.md` já gerado e decidir fornecedor principal e aliases
   de competição; repetir `discover.py` só quando a janela mudar.
2. Fixar o esquema de estado (últimos jogos vistos por clube) — ficheiro JSON
   ou SQLite, idempotente.
3. Implementar o cliente Telegram (sendMessage, sem preview) e o
   `config/chats.yaml` real (privado + grupo, `content`, `silent`).
4. Definir agendamento e orçamento de chamadas dentro do tier gratuito.

## 7. Arquivos relevantes

```
discover.py             CLI Fase 0 (argparse, urllib, parsers, cobertura)
config/teams.yaml       key, display, fdorg_id, apifootball_id
config/chats.yaml       esqueleto de destinos Telegram (Fase 1)
.env.example            nomes das chaves + onde obter + limites
docs/coverage.md        relatório por fornecedor, roster e checklist manual
tests/conftest.py       põe a raiz no sys.path
tests/test_discover.py  46 testes, sem rede
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
- Actualizar este documento sempre que a Fase 1 começar.
