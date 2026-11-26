"""Testes da Fase 0. Nenhum teste faz chamadas de rede."""

from datetime import datetime, timedelta, timezone

import pytest

import discover
from discover import (
    AmbiguousTeamError,
    RateLimiter,
    apply_window,
    build_coverage_markdown,
    build_free_tier_notes,
    filter_by_days,
    load_env,
    match_roster_team,
    normalise,
    normalise_competitions,
    parse_apifootball_fixture,
    parse_env_file,
    parse_fdorg_fixture,
    parse_fdorg_roster,
    pick_team,
    resolve_team_fdorg,
    split_markdown_sections,
    summarise_competitions,
    write_coverage,
    write_teams_config,
)

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# .env
# --------------------------------------------------------------------------


def test_parse_env_file_comments_quotes_and_export():
    text = "\n".join(
        [
            "# comentario",
            "",
            "PLAIN=abc",
            '  QUOTED = "dois espacos"  ',
            "SINGLE='tres'",
            "export EXPORTED=quatro",
            "vazia=",
            "linha_sem_igual",
            "  # comentario indentado",
        ]
    )
    values = parse_env_file(text)
    assert values == {
        "PLAIN": "abc",
        "QUOTED": "dois espacos",
        "SINGLE": "tres",
        "EXPORTED": "quatro",
        "vazia": "",
    }


def test_load_env_does_not_override_existing_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'FOOTBALL_DATA_TOKEN="do ficheiro"\nAPIFOOTBALL_KEY="nova"\n',
        encoding="utf-8",
    )
    environ = {"FOOTBALL_DATA_TOKEN": "ja existia"}
    values = load_env(env_file, environ=environ)

    assert values == {
        "FOOTBALL_DATA_TOKEN": "do ficheiro",
        "APIFOOTBALL_KEY": "nova",
    }
    assert environ["FOOTBALL_DATA_TOKEN"] == "ja existia"
    assert environ["APIFOOTBALL_KEY"] == "nova"


def test_load_env_missing_file_returns_empty_and_touches_nothing(tmp_path):
    environ: dict[str, str] = {}
    assert load_env(tmp_path / "nao_existe.env", environ=environ) == {}
    assert environ == {}


def test_load_env_never_leaks_comments_into_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# SO=comentario\nOK=1\n", encoding="utf-8")
    values = load_env(env_file, environ={})
    assert values == {"OK": "1"}


# --------------------------------------------------------------------------
# normalise / pick_team
# --------------------------------------------------------------------------


def test_normalise_strips_accents_case_and_spaces():
    assert normalise("  Grêmio   FBPA ") == "gremio fbpa"
    assert normalise("SC CORINTHIANS Paulista") == "sc corinthians paulista"
    assert normalise("São Paulo") == "sao paulo"
    assert normalise("Atlético-MG") == "atletico-mg"
    assert normalise(None) == ""
    assert normalise("") == ""


CANDIDATES = [
    {"id": 130, "name": "Grêmio FBPA", "shortName": "Grêmio"},
    {"id": 131, "name": "SC Corinthians Paulista", "shortName": "Corinthians"},
]


def test_pick_team_exact_normalised_match_wins():
    assert pick_team(CANDIDATES, "gremio fbpa")["id"] == 130
    assert pick_team(CANDIDATES, "Grêmio")["id"] == 130


def test_pick_team_unique_substring_match():
    assert pick_team(CANDIDATES, "Corinthians")["id"] == 131
    assert pick_team(CANDIDATES, "corinthians paulista")["id"] == 131


def test_pick_team_single_candidate_fallback():
    only = [{"id": 7, "name": "Unico FC"}]
    assert pick_team(only, "nome completamente diferente")["id"] == 7


def test_pick_team_ambiguous_raises_and_lists_candidates():
    ambiguous = [
        {"id": 1, "name": "Grêmio A"},
        {"id": 2, "name": "Grêmio B"},
    ]
    with pytest.raises(AmbiguousTeamError) as info:
        pick_team(ambiguous, "Gremio")
    message = str(info.value)
    assert "1: Grêmio A" in message
    assert "2: Grêmio B" in message


def test_pick_team_no_candidates_raises():
    with pytest.raises(AmbiguousTeamError):
        pick_team([], "Gremio")


# --------------------------------------------------------------------------
# Normalizacao de jogos
# --------------------------------------------------------------------------


def test_parse_fdorg_fixture_common_shape():
    match = {
        "utcDate": "2026-11-20T22:30:00Z",
        "competition": {"name": "Brasileirao Serie A"},
        "matchday": 34,
        "homeTeam": {"id": 130, "name": "Grêmio"},
        "awayTeam": {"id": 131, "name": "Corinthians"},
        "score": {"fullTime": {"home": 2, "away": 1}},
        "status": "FINISHED",
    }
    fixture = parse_fdorg_fixture(match)

    assert fixture["provider"] == "fdorg"
    assert fixture["team_id"] == 130
    assert fixture["date_utc"] == "2026-11-20T22:30:00Z"
    assert fixture["competition"] == "Brasileirao Serie A"
    assert fixture["round"] == "34"
    assert fixture["home"] == "Grêmio"
    assert fixture["away"] == "Corinthians"
    assert fixture["goals_home"] == 2
    assert fixture["goals_away"] == 1
    assert fixture["status"] == "FINISHED"


def test_parse_fdorg_fixture_missing_score_keeps_none():
    match = {
        "utcDate": "2026-11-20T22:30:00Z",
        "competition": {"name": "Copa do Brasil"},
        "homeTeam": {"name": "Grêmio"},
        "awayTeam": {"name": "Corinthians"},
        "score": {"fullTime": {"home": None, "away": None}},
    }
    fixture = parse_fdorg_fixture(match, team_id=130)

    assert fixture["goals_home"] is None
    assert fixture["goals_away"] is None
    assert fixture["team_id"] == 130
    assert fixture["status"] == "FINISHED"


def test_parse_fdorg_fixture_without_goals_key_keeps_none():
    fixture = parse_fdorg_fixture({"homeTeam": {}, "awayTeam": {}})
    assert fixture["goals_home"] is None
    assert fixture["goals_away"] is None


def test_parse_apifootball_fixture_common_shape():
    item = {
        "fixture": {
            "date": "2026-11-21T00:00:00+00:00",
            "status": {"short": "FT"},
        },
        "league": {"name": "Serie A", "round": "Regular Season - 34"},
        "teams": {
            "home": {"id": 131, "name": "Corinthians"},
            "away": {"id": 130, "name": "Grêmio"},
        },
        "goals": {"home": 0, "away": 0},
    }
    fixture = parse_apifootball_fixture(item)

    assert fixture["provider"] == "apifootball"
    assert fixture["team_id"] == 131
    assert fixture["date_utc"] == "2026-11-21T00:00:00+00:00"
    assert fixture["competition"] == "Serie A"
    assert fixture["round"] == "Regular Season - 34"
    assert fixture["home"] == "Corinthians"
    assert fixture["away"] == "Grêmio"
    assert fixture["goals_home"] == 0
    assert fixture["goals_away"] == 0
    assert fixture["status"] == "FT"


def test_parse_apifootball_fixture_missing_goals_keeps_none():
    item = {
        "fixture": {"date": "2026-11-21T00:00:00+00:00", "status": {"short": "PST"}},
        "league": {"name": "Serie A"},
        "teams": {"home": {"name": "Corinthians"}, "away": {"name": "Grêmio"}},
    }
    fixture = parse_apifootball_fixture(item, team_id=131)

    assert fixture["goals_home"] is None
    assert fixture["goals_away"] is None
    assert fixture["status"] == "PST"


# --------------------------------------------------------------------------
# Janela --days
# --------------------------------------------------------------------------


def _fixture(days_ago: int, label: str) -> dict:
    return {
        "provider": "fdorg",
        "team_id": 130,
        "date_utc": (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "competition": label,
        "round": "",
        "home": "A",
        "away": "B",
        "goals_home": 1,
        "goals_away": 0,
        "status": "FINISHED",
    }


def test_filter_by_days_keeps_only_window():
    fixtures = [_fixture(1, "recente"), _fixture(40, "antigo")]
    future = _fixture(-1, "futuro")
    kept = filter_by_days(fixtures + [future], 30, now=NOW)
    assert [f["competition"] for f in kept] == ["recente"]


def test_apply_window_warns_and_falls_back_when_empty(capsys):
    fixtures = [_fixture(90, "antigo")]
    kept = apply_window(fixtures, 7, "fdorg/Gremio", now=NOW)
    assert kept == fixtures
    out = capsys.readouterr().out
    assert "[aviso]" in out
    assert "sem filtro" in out


# --------------------------------------------------------------------------
# Cobertura markdown
# --------------------------------------------------------------------------


def _result(provider="fdorg", label="football-data.org", team="Grêmio", team_id=130, comps=()):
    return {
        "provider": provider,
        "label": label,
        "team": team,
        "team_id": team_id,
        "fixtures": [{"competition": comp} for comp in comps],
    }


def test_summarise_competitions_counts_and_orders():
    fixtures = [
        {"competition": "Brasileirao Serie A"},
        {"competition": "Brasileirao Serie A"},
        {"competition": "Copa do Brasil"},
    ]
    counts = summarise_competitions(fixtures)
    assert counts == {"Brasileirao Serie A": 2, "Copa do Brasil": 1}
    assert list(counts) == ["Brasileirao Serie A", "Copa do Brasil"]


def test_build_coverage_markdown_has_table_with_competition_count():
    results = [
        _result(
            comps=[
                "Brasileirao Serie A",
                "Brasileirao Serie A",
                "Copa do Brasil",
            ]
        )
    ]
    markdown = build_coverage_markdown(results, generated_at=NOW)

    assert markdown.startswith("<!-- gerado por discover.py -->")
    assert "| equipa | id | competicoes (jogos) |" in markdown
    assert "| Grêmio | 130 | Brasileirao Serie A (2); Copa do Brasil (1) |" in markdown
    assert "| _total_ | - | 3 jogos |" in markdown
    assert "## Checklist manual" in markdown
    assert "- [ ] " in markdown
    assert "football-data.org" in markdown


def test_build_coverage_markdown_handles_team_without_fixtures():
    markdown = build_coverage_markdown([_result(comps=[])], generated_at=NOW)
    assert "sem jogos" in markdown
    assert "| _total_ | - | 0 jogos |" in markdown


def test_write_coverage_merges_and_preserves_untouched_provider(tmp_path):
    path = tmp_path / "coverage.md"
    path.write_text(
        "<!-- gerado por discover.py -->\n\n"
        "## api-football\n\n"
        "| equipa | id | competicoes (jogos) |\n"
        "| --- | --- | --- |\n"
        "| Corinthians | 131 | Serie A (5) |\n",
        encoding="utf-8",
    )

    write_coverage(path, [_result(comps=["Brasileirao Serie A"])], generated_at=NOW)
    text = path.read_text(encoding="utf-8")

    assert "## football-data.org" in text
    assert "Brasileirao Serie A (1)" in text
    assert "## api-football" in text
    assert "Serie A (5)" in text
    assert text.count("## api-football") == 1


def test_write_coverage_overwrites_stale_same_provider_section(tmp_path):
    path = tmp_path / "coverage.md"
    write_coverage(path, [_result(comps=["Antiga"])], generated_at=NOW)
    write_coverage(path, [_result(comps=["Nova"])], generated_at=NOW)
    text = path.read_text(encoding="utf-8")
    assert "Antiga" not in text
    assert "Nova (1)" in text


def test_split_markdown_sections_roundtrip():
    sections = split_markdown_sections(
        "<!-- x -->\n# T\n\n## Um\n\na\n\n## Checklist manual\n\n- [ ] y\n"
    )
    assert sections == {"Um": ["a"], "Checklist manual": ["- [ ] y"]}


# --------------------------------------------------------------------------
# config/teams.yaml
# --------------------------------------------------------------------------


ENTRIES = [
    {
        "key": "gremio",
        "display": "Grêmio",
        "fdorg_id": 130,
        "apifootball_id": None,
    },
    {
        "key": "corinthians",
        "display": "Corinthians",
        "fdorg_id": None,
        "apifootball_id": 131,
    },
]


def test_write_teams_config_refuses_existing_without_force(tmp_path):
    path = tmp_path / "teams.yaml"
    path.write_text("teams: []\n", encoding="utf-8")

    assert write_teams_config(path, ENTRIES, force=False, generated_at=NOW) is False
    assert path.read_text(encoding="utf-8") == "teams: []\n"


def test_write_teams_config_writes_expected_shape(tmp_path):
    path = tmp_path / "config" / "teams.yaml"
    assert write_teams_config(path, ENTRIES, force=True, generated_at=NOW) is True
    text = path.read_text(encoding="utf-8")

    assert text.startswith("# Gerado por discover.py em 2026-11-26")
    assert "teams:" in text
    assert "  - key: gremio" in text
    assert "    display: Grêmio" in text
    assert "    fdorg_id: 130" in text
    assert "    apifootball_id: null" in text
    assert text.count("fdorg_id:") == 2


def test_write_teams_config_creates_parent_directory(tmp_path):
    path = tmp_path / "novo" / "dir" / "teams.yaml"
    assert write_teams_config(path, ENTRIES, force=True, generated_at=NOW) is True
    assert path.exists()


# --------------------------------------------------------------------------
# CLI: sem chaves -> exit 2, sem traceback
# --------------------------------------------------------------------------


def test_main_without_keys_exits_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(discover, "ENV_PATH", tmp_path / "sem.env")
    for name in ("FOOTBALL_DATA_TOKEN", "APIFOOTBALL_KEY"):
        monkeypatch.delenv(name, raising=False)

    code = discover.main(["--out", str(tmp_path / "coverage.md")])

    assert code == 2
    out = capsys.readouterr().out
    assert "Nenhuma chave encontrada" in out
    assert "Traceback" not in out


def test_parse_team_id_args_rejects_bad_values():
    with pytest.raises(ValueError):
        discover.parse_team_id_args(["gremio:130"])
    with pytest.raises(ValueError):
        discover.parse_team_id_args(["fdorg:abc"])
    assert discover.parse_team_id_args(["fdorg:130"]) == {"fdorg": 130}


# --------------------------------------------------------------------------
# Resolucao por roster (football-data.org)
# --------------------------------------------------------------------------


ROSTER_BSA = [
    {"id": 9001, "name": "Grêmio FBPA", "shortName": "Grêmio", "area": "Brazil"},
    {
        "id": 9002,
        "name": "SC Corinthians Paulista",
        "shortName": "Corinthians",
        "area": "Brazil",
    },
    {"id": 9003, "name": "CR Flamengo", "shortName": "Flamengo", "area": "Brazil"},
]

ROSTER_CLI = [
    {"id": 9001, "name": "Grêmio FBPA", "shortName": "Grêmio", "area": "Brazil"},
    {"id": 9003, "name": "CR Flamengo", "shortName": "Flamengo", "area": "Brazil"},
]


def _fake_fetcher(rosters, calls=None):
    def fetch(code):
        if calls is not None:
            calls.append(code)
        return list(rosters.get(code, []))

    return fetch


def test_parse_fdorg_roster_normalises_and_skips_invalid():
    roster = parse_fdorg_roster(
        [
            {
                "id": "9001",
                "name": "Grêmio FBPA",
                "shortName": "Grêmio",
                "area": {"name": "Brazil"},
            },
            {"id": None, "name": "Sem ID"},
            "lixo",
            {"id": 9003, "name": "CR Flamengo"},
        ]
    )
    assert roster == [
        {"id": 9001, "name": "Grêmio FBPA", "shortName": "Grêmio", "area": "Brazil"},
        {"id": 9003, "name": "CR Flamengo", "shortName": "", "area": ""},
    ]


def test_resolve_team_fdorg_exact_match_uses_roster():
    calls = []
    match = resolve_team_fdorg(
        "Grêmio FBPA",
        ["BSA"],
        fetch_roster=_fake_fetcher({"BSA": ROSTER_BSA}, calls),
    )
    assert match["id"] == 9001
    assert match["name"] == "Grêmio FBPA"
    assert match["competition"] == "BSA"
    assert match["competitions"] == ["BSA"]
    assert match["source"] == "roster"
    assert calls == ["BSA"]


def test_resolve_team_fdorg_substring_match():
    match = resolve_team_fdorg(
        "Corinthians",
        ["BSA"],
        fetch_roster=_fake_fetcher({"BSA": ROSTER_BSA}),
    )
    assert match["id"] == 9002


def test_resolve_team_fdorg_records_every_roster_with_the_team():
    match = resolve_team_fdorg(
        "Gremio",
        ["BSA", "CLI"],
        fetch_roster=_fake_fetcher({"BSA": ROSTER_BSA, "CLI": ROSTER_CLI}),
    )
    assert match["id"] == 9001
    assert match["competition"] == "BSA"
    assert match["competitions"] == ["BSA", "CLI"]


def test_resolve_team_fdorg_not_found_raises_without_global_search():
    with pytest.raises(discover.TeamNotFoundError):
        resolve_team_fdorg(
            "Inexistente",
            ["BSA"],
            fetch_roster=_fake_fetcher({"BSA": ROSTER_BSA}),
        )


def test_resolve_team_fdorg_global_search_is_last_resort(capsys):
    order = []

    def fetch(code):
        order.append(("roster", code))
        return []

    def search(name):
        order.append(("global", name))
        return [{"id": 9100, "name": "Algum FC"}]

    match = resolve_team_fdorg(
        "Algum", ["BSA", "CLI"], fetch_roster=fetch, search_global=search
    )

    assert match["source"] == "global"
    assert match["id"] == 9100
    assert match["competitions"] == []
    assert order == [("roster", "BSA"), ("roster", "CLI"), ("global", "Algum")]
    out = capsys.readouterr().out
    assert "[aviso]" in out
    assert "busca global instavel no plano free" in out


def test_resolve_team_fdorg_reuses_roster_cache():
    calls = []
    cache = {}
    fetch = _fake_fetcher({"BSA": ROSTER_BSA}, calls)

    resolve_team_fdorg("Gremio", ["BSA"], fetch_roster=fetch, cache=cache)
    resolve_team_fdorg("Corinthians", ["BSA"], fetch_roster=fetch, cache=cache)

    assert calls == ["BSA"]
    assert "BSA" in cache


def test_resolve_team_fdorg_caps_roster_scans_at_three_codes():
    calls = []
    fetch = _fake_fetcher({"BSA": ROSTER_BSA, "CLI": ROSTER_CLI}, calls)

    match = resolve_team_fdorg(
        "Gremio", ["BSA", "CLI", "PL", "PD"], fetch_roster=fetch
    )

    assert calls == ["BSA", "CLI", "PL"]
    assert match["competitions"] == ["BSA", "CLI"]


def test_match_roster_team_ambiguous_raises():
    roster = [{"id": 1, "name": "Grêmio A"}, {"id": 2, "name": "Grêmio B"}]
    with pytest.raises(AmbiguousTeamError):
        match_roster_team(roster, "Gremio")


def test_match_roster_team_returns_none_when_absent():
    assert match_roster_team(ROSTER_BSA, "Inexistente") is None


def test_normalise_competitions_uppercases_and_dedupes():
    assert normalise_competitions(["bsa", "BSA", " cli "]) == ["BSA", "CLI"]
    assert normalise_competitions([]) == ["BSA", "CLI"]
    assert normalise_competitions(None) == ["BSA", "CLI"]


def test_parser_fdorg_competition_defaults_and_repeatable():
    parser = discover.build_parser()
    args = parser.parse_args([])
    assert normalise_competitions(args.fdorg_competition) == ["BSA", "CLI"]

    args = parser.parse_args(
        ["--fdorg-competition", "PL", "--fdorg-competition", "pd"]
    )
    assert normalise_competitions(args.fdorg_competition) == ["PL", "PD"]


def test_build_free_tier_notes_lists_13_codes_and_unavailable_cups():
    text = "\n".join(build_free_tier_notes())
    assert "13 competicoes" in text
    for code in ("BSA", "CLI", "PL", "WC"):
        assert code in text
    assert "Copa do Brasil NAO esta disponivel" in text
    assert "estaduais" in text


def test_build_coverage_markdown_records_roster_source_and_codes():
    result = _result(comps=["Brasileirao Serie A"])
    result["source"] = "roster"
    result["roster_competitions"] = ["BSA", "CLI"]

    markdown = build_coverage_markdown([result], generated_at=NOW)

    assert "### Resolucao de equipas (roster)" in markdown
    assert "| Grêmio | roster | BSA, CLI |" in markdown
    assert "## Tier gratuito football-data.org" in markdown
    assert "13 competicoes" in markdown


def test_write_coverage_does_not_duplicate_free_tier_section(tmp_path):
    path = tmp_path / "coverage.md"
    write_coverage(path, [_result(comps=["Antiga"])], generated_at=NOW)
    write_coverage(path, [_result(comps=["Nova"])], generated_at=NOW)
    text = path.read_text(encoding="utf-8")
    assert text.count("## Tier gratuito football-data.org") == 1


def test_build_config_entries_prefers_resolved_fdorg_ids():
    specs = [
        discover.TeamSpec(key="gremio", display="Gremio"),
        discover.TeamSpec(key="corinthians", display="Corinthians"),
    ]
    entries = discover.build_config_entries(
        specs,
        {"gremio": {"fdorg": 9001}, "corinthians": {"fdorg": 9002}},
    )
    assert [entry["fdorg_id"] for entry in entries] == [9001, 9002]


def test_rate_limiter_spaces_requests_without_oversleeping():
    slept = []
    clock = {"now": 100.0}

    def fake_sleep(seconds):
        slept.append(seconds)
        clock["now"] += seconds

    limiter = RateLimiter(6.5, clock=lambda: clock["now"], sleep=fake_sleep)

    assert limiter.wait() == 0.0
    clock["now"] += 1.0
    assert limiter.wait() == pytest.approx(5.5)
    clock["now"] += 10.0
    assert limiter.wait() == 0.0
    assert slept == [pytest.approx(5.5)]


def test_discover_keeps_roster_codes_separate_from_fixture_competitions(
    monkeypatch, tmp_path
):
    # Regressao: o resumo de competicoes dos jogos nao pode substituir os
    # codigos de roster usados na equipa seguinte.
    monkeypatch.setattr(discover, "CONFIG_TEAMS", tmp_path / "sem-config.yaml")
    rosters = {"BSA": ROSTER_BSA, "CLI": ROSTER_CLI}

    class FakeProvider:
        name = "fdorg"
        label = "football-data.org"

        def __init__(self):
            self.roster_calls = []
            self.roster_cache = {}

        def fetch_roster(self, code):
            self.roster_calls.append(code)
            if code not in self.roster_cache:
                self.roster_cache[code] = list(rosters.get(code, []))
            return self.roster_cache[code]

        def fetch_fixtures(self, team_id, limit):
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return [
                {
                    "provider": "fdorg",
                    "team_id": team_id,
                    "date_utc": stamp,
                    "competition": "Campeonato Brasileiro Serie A",
                    "round": "",
                    "home": "A",
                    "away": "B",
                    "goals_home": 1,
                    "goals_away": 0,
                    "status": "FINISHED",
                }
            ]

        def search_teams(self, name):
            return []

        def describe(self, candidate):
            return candidate.get("name") or ""

    fake = FakeProvider()
    args = discover.argparse.Namespace(
        team=["Gremio", "Corinthians"],
        team_id=[],
        fdorg_competition=None,
        days=60,
        limit=10,
        write_config=False,
        force=False,
        out=str(tmp_path / "coverage.md"),
        provider="fdorg",
    )

    results, resolved, stats = discover.discover(args, {"fdorg": fake})

    assert fake.roster_calls == ["BSA", "CLI"]
    assert [result.team_id for result in results] == [9001, 9002]
    assert results[0].roster_competitions == ["BSA", "CLI"]
    assert results[1].roster_competitions == ["BSA"]
    assert resolved == {"gremio": {"fdorg": 9001}, "corinthians": {"fdorg": 9002}}
    assert stats["fdorg"]["succeeded"] == 2


