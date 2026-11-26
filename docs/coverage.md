<!-- gerado por discover.py -->

# Cobertura - Fase 0

Gerado em 2026-11-26T15:21:37Z.

Cada tabela mostra as competicoes devolvidas pelos fornecedores e o numero de jogos na janela consultada. Nao representa a epoca completa: serve para decidir que competicoes faltam.

## Tier gratuito football-data.org

- O plano gratuito expoe **13 competicoes**: BSA, ELC, PL, CL, EC, FL1, BL1, SA, DED, PPL, CLI, PD, WC.
- **BSA** (Brasileirao Serie A) e **CLI** (Copa Libertadores) estao incluidas e servem de roster para resolver as equipas.
- **Copa do Brasil NAO esta disponivel** no plano gratuito.
- **Campeonatos estaduais (Gauchao, Paulistao) NAO estao disponiveis**.
- A pesquisa global `/v4/teams?name=` e instavel neste plano (ignora o filtro e devolve clubes aleatorios); so se usa como ultimo recurso.
- Limite de 10 pedidos/minuto: `discover.py` espaca chamadas em 6.5 s.

## football-data.org

| equipa | id | competicoes (jogos) |
| --- | --- | --- |
| Gremio | 1767 | Campeonato Brasileiro Série A (8) |
| Corinthians | 1779 | Campeonato Brasileiro Série A (8) |
| _total_ | - | 16 jogos |

### Resolucao de equipas (roster)

| equipa | fonte | competicoes do roster |
| --- | --- | --- |
| Gremio | roster | BSA |
| Corinthians | roster | BSA, CLI |

`roster` = encontrada em `/v4/competitions/{CODE}/teams`; `global` = pesquisa instavel, ultimo recurso.

## Checklist manual

- [ ] Confirmar que o campeonato nacional (Serie A/B) aparece para os dois clubes.
- [ ] Confirmar se a Copa do Brasil aparece e com que nome.
- [ ] Confirmar se a Libertadores/Sul-Americana aparece nas fases finais.
- [ ] Registar se o campeonato estadual (Gauchao/Paulistao) existe na API.
- [ ] Verificar se a mesma competicao nao aparece duplicada com nomes diferentes.
- [ ] Rever o consumo diario depois de uma ronda completa (limites gratuitos).
