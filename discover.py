#!/usr/bin/env python3
"""football-bot / Fase 0 - descobrir IDs de equipa e cobertura de competicoes.

Fornecedores (chaves em .env, na raiz do projeto):
  * football-data.org  -> FOOTBALL_DATA_TOKEN  (header X-Auth-Token)
  * api-football       -> APIFOOTBALL_KEY      (header x-apisports-key)

Rede apenas com a stdlib (urllib.request + json). PyYAML e opcional e so e
importado dentro de load_team_config(), para ler config/teams.yaml.

Marcadores de consola (ASCII, sem emoji): [ok] [erro] [aviso]

Exemplos:
    python discover.py
    python discover.py --provider apifootball --team Gremio --days 60
    python discover.py --team-id fdorg:130 --limit 5
    python discover.py --fdorg-competition BSA --fdorg-competition CLI
    python discover.py --team Gremio --team Corinthians --days 60 --write-config --force
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
CONFIG_TEAMS = ROOT / "config" / "teams.yaml"
DEFAULT_OUT = "docs/coverage.md"

DEFAULT_TEAMS = (("gremio", "Grêmio"), ("corinthians", "Corinthians"))

PROVIDER_ORDER = ("fdorg", "apifootball")
PROVIDER_LABELS = {"fdorg": "football-data.org", "apifootball": "api-football"}
PROVIDER_ENV = {"fdorg": "FOOTBALL_DATA_TOKEN", "apifootball": "APIFOOTBALL_KEY"}

FDORG_BASE = "https://api.football-data.org/v4"
APIFOOTBALL_BASE = "https://v3.football.api-sports.io"
APIFOOTBALL_FINISHED = frozenset({"FT", "AET", "PEN"})

# Resolucao de equipas por roster (verificado com o token free a 2026-11-26):
#   * /v4/teams?name= ... (pesquisa global) e instavel no plano free;
#   * /v4/competitions/{CODE}/teams devolve o plantel completo da competicoes.
FDORG_DEFAULT_COMPETITIONS = ("BSA", "CLI")
FDORG_ROSTER_CAP = 3
FDORG_MIN_INTERVAL = 6.5  # 10 pedidos/min => 6.5 s de margem
# Competicoes visiveis no plano gratuito (count: 13, verificado a 2026-11-26).
FDORG_FREE_TIER = (
    "BSA",
    "ELC",
    "PL",
    "CL",
    "EC",
    "FL1",
    "BL1",
    "SA",
    "DED",
    "PPL",
    "CLI",
    "PD",
    "WC",
)
FREE_TIER_TITLE = "Tier gratuito football-data.org"


COVERAGE_HEADER = "<!-- gerado por discover.py -->"
TABLE_HEADERS = ("DATE UTC", "COMPETITION", "HOME", "AWAY", "SCORE", "STATUS")
TABLE_MAX_WIDTH = (16, 30, 22, 22, 7, 8)

CHECKLIST = (
    "Confirmar que o campeonato nacional (Serie A/B) aparece para os dois clubes.",
    "Confirmar se a Copa do Brasil aparece e com que nome.",
    "Confirmar se a Libertadores/Sul-Americana aparece nas fases finais.",
    "Registar se o campeonato estadual (Gauchao/Paulistao) existe na API.",
    "Verificar se a mesma competicao nao aparece duplicada com nomes diferentes.",
    "Rever o consumo diario depois de uma ronda completa (limites gratuitos).",
)


# --------------------------------------------------------------------------
# Erros
# --------------------------------------------------------------------------


class ProviderError(Exception):
    """Falha de rede, HTTP ou parse. Nunca inclui chaves nem cabecalhos."""


class AmbiguousTeamError(LookupError):
    """Equipa nao encontrada ou com varios candidatos."""

    def __init__(self, wanted: str, candidates: list[dict], reason: str = "ambiguo"):
        self.wanted = wanted
        self.candidates = list(candidates or [])
        listing = "; ".join(
            f"{c.get('id')}: {c.get('name')}" for c in self.candidates
        ) or "nenhum"
        super().__init__(f"equipa '{wanted}' {reason}: {listing}")


class TeamNotFoundError(AmbiguousTeamError):
    def __init__(self, wanted: str, candidates: list[dict] | None = None):
        super().__init__(wanted, candidates or [], "nao encontrada")


# --------------------------------------------------------------------------
# Texto / nomes
# --------------------------------------------------------------------------


def normalise(value: Any) -> str:
    """minusculas, sem acentos, espacos colapsados."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.lower().split())


def slugify(value: Any) -> str:
    text = normalise(value)
    out: list[str] = []
    for ch in text:
        if ch.isascii() and ch.isalnum():
            out.append(ch)
        elif ch in " -_":
            out.append("-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "equipa"


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _field(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _candidate_names(candidate: Mapping) -> list[str]:
    names = []
    for key in ("name", "shortName"):
        text = normalise(candidate.get(key) or "")
        if text:
            names.append(text)
    return names


def pick_team(candidates: list[dict], wanted: str) -> dict:
    """Escolhe um candidato: exato > substring unica > unico candidato."""
    if not candidates:
        raise TeamNotFoundError(wanted)
    wanted_n = normalise(wanted)
    if not wanted_n:
        raise TeamNotFoundError(wanted)

    for candidate in candidates:
        if wanted_n in _candidate_names(candidate):
            return candidate

    matches = [
        candidate
        for candidate in candidates
        if any(wanted_n in name for name in _candidate_names(candidate))
    ]
    if len(matches) == 1:
        return matches[0]
    if len(candidates) == 1:
        return candidates[0]
    raise AmbiguousTeamError(wanted, matches or candidates)


def match_roster_team(roster: list[dict], wanted: str) -> dict | None:
    """Aplica a semantica de pick_team() a um roster; None quando nada bate.

    Diferenca face a pick_team(): um roster de 20 clubes sem match nao e
    ambiguidade, e sim "nao esta nesta competicao" -> devolve None.
    """
    if not roster:
        return None
    wanted_n = normalise(wanted)
    if not wanted_n:
        return None
    named = [(candidate, _candidate_names(candidate)) for candidate in roster]
    for candidate, names in named:
        if wanted_n in names:
            return candidate
    matches = [
        candidate
        for candidate, names in named
        if any(wanted_n in name for name in names)
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AmbiguousTeamError(wanted, matches)
    if len(roster) == 1:
        return roster[0]
    return None


def normalise_competitions(values: Sequence[str] | None) -> list[str]:
    """Codigos em maiusculas, sem duplicados; fallback para o default."""
    codes: list[str] = []
    for raw in values or ():
        code = str(raw).strip().upper()
        if code and code not in codes:
            codes.append(code)
    return codes or list(FDORG_DEFAULT_COMPETITIONS)



# --------------------------------------------------------------------------
# .env
# --------------------------------------------------------------------------


def parse_env_file(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_env(path: Any = None, environ: Mapping | None = None) -> dict[str, str]:
    """Le .env e aplica em os.environ sem sobrepor valores ja existentes."""
    target = Path(path) if path is not None else ENV_PATH
    target_environ = os.environ if environ is None else environ
    try:
        text = target.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return {}
    values = parse_env_file(text)
    for key, value in values.items():
        if key and key not in target_environ:
            try:
                target_environ[key] = value  # type: ignore[index]
            except TypeError:
                pass
    return values


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def _safe_url(url: str) -> str:
    return url if len(url) <= 160 else url[:157] + "..."


def http_get_json(url: str, headers: Mapping[str, str], timeout: float = 25.0) -> Any:
    request = urllib.request.Request(url, headers=dict(headers))
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", "football-bot-fase0/0.1")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        snippet = ""
        try:
            snippet = exc.read().decode("utf-8", "replace").strip()
        except Exception:
            snippet = ""
        if snippet:
            snippet = " " + snippet[:160].replace("\n", " ")
        raise ProviderError(f"HTTP {exc.code} em {_safe_url(url)}{snippet}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ProviderError(f"rede: {reason} ({_safe_url(url)})") from None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise ProviderError(f"JSON invalido em {_safe_url(url)}: {exc}") from None


# --------------------------------------------------------------------------
# Fornecedores
# --------------------------------------------------------------------------


class RateLimiter:
    """Espaca pedidos consecutivos (tier free: 10/min -> intervalo >= 6.5 s)."""

    def __init__(
        self,
        min_interval: float = FDORG_MIN_INTERVAL,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        self.min_interval = max(float(min_interval or 0.0), 0.0)
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._last: float | None = None

    def wait(self) -> float:
        """Dorme apenas o que falta; devolve os segundos dormidos."""
        if self.min_interval <= 0:
            return 0.0
        now = self._clock()
        slept = 0.0
        if self._last is not None:
            remaining = self.min_interval - (now - self._last)
            if remaining > 0:
                self._sleep(remaining)
                slept = remaining
                now = self._clock()
        self._last = now
        return slept


def parse_fdorg_team(team: Mapping) -> dict:
    area = team.get("area")
    return {
        "id": _as_int(team.get("id")),
        "name": team.get("name") or "",
        "shortName": team.get("shortName") or "",
        "area": (area.get("name") if isinstance(area, Mapping) else "") or "",
    }


def parse_fdorg_roster(teams: Any) -> list[dict]:
    """Normaliza `teams[]` de /competitions/{CODE}/teams (so com id valido)."""
    roster: list[dict] = []
    for team in teams or []:
        if not isinstance(team, Mapping):
            continue
        parsed = parse_fdorg_team(team)
        if parsed["id"] is not None:
            roster.append(parsed)
    return roster


class FootballDataOrgProvider:
    name = "fdorg"
    label = PROVIDER_LABELS["fdorg"]
    env_key = PROVIDER_ENV["fdorg"]
    base = FDORG_BASE

    def __init__(self, api_key: str, limiter: RateLimiter | None = None):
        self._headers = {"X-Auth-Token": api_key}
        self._limiter = limiter or RateLimiter()
        self._rosters: dict[str, list[dict]] = {}

    @property
    def roster_cache(self) -> dict[str, list[dict]]:
        return self._rosters

    def _get(self, path: str, params: Mapping[str, Any] | None = None) -> dict:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(dict(params))
        self._limiter.wait()
        data = http_get_json(url, self._headers)
        if not isinstance(data, dict):
            raise ProviderError(f"resposta inesperada em {path} (esperado objeto JSON)")
        return data

    def search_teams(self, wanted: str) -> list[dict]:
        # Ultimo recurso: no plano free este endpoint ignora o filtro `name`.
        data = self._get("/teams", {"name": wanted})
        return parse_fdorg_roster(data.get("teams"))

    def fetch_competition_teams(self, code: str) -> list[dict]:
        data = self._get(f"/competitions/{code}/teams")
        return parse_fdorg_roster(data.get("teams"))

    def fetch_roster(self, code: str) -> list[dict]:
        """Roster por competicao, em cache dentro da execucao (1 fetch/codigo)."""
        key = str(code).strip().upper()
        if key not in self._rosters:
            self._rosters[key] = self.fetch_competition_teams(key)
        return self._rosters[key]


    def describe(self, candidate: Mapping) -> str:
        parts = [candidate.get("name") or "?"]
        short = candidate.get("shortName") or ""
        if short and short != parts[0]:
            parts.append(short)
        area = candidate.get("area") or ""
        if area:
            parts.append(area)
        return " / ".join(parts)

    def fetch_fixtures(self, team_id: int, limit: int) -> list[dict]:
        data = self._get(
            f"/teams/{int(team_id)}/matches",
            {"status": "FINISHED", "limit": int(limit)},
        )
        return [parse_fdorg_fixture(match, team_id) for match in data.get("matches") or []]


class ApiFootballProvider:
    name = "apifootball"
    label = PROVIDER_LABELS["apifootball"]
    env_key = PROVIDER_ENV["apifootball"]
    base = APIFOOTBALL_BASE

    def __init__(self, api_key: str):
        self._headers = {"x-apisports-key": api_key}

    def _get(self, path: str, params: Mapping[str, Any] | None = None) -> dict:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(dict(params))
        data = http_get_json(url, self._headers)
        if not isinstance(data, dict):
            raise ProviderError(f"resposta inesperada em {path} (esperado objeto JSON)")
        errors = data.get("errors")
        if errors:
            if isinstance(errors, Mapping):
                text = "; ".join(f"{k}: {v}" for k, v in errors.items())
            else:
                text = str(errors)
            raise ProviderError(f"API devolveu erro: {text[:200]}")
        return data

    def search_teams(self, wanted: str) -> list[dict]:
        data = self._get("/teams", {"search": wanted})
        candidates = []
        for row in data.get("response") or []:
            team = (row or {}).get("team") or {}
            if not isinstance(team, Mapping):
                continue
            candidates.append(
                {
                    "id": _as_int(team.get("id")),
                    "name": team.get("name") or "",
                    "shortName": "",
                    "area": team.get("country") or "",
                }
            )
        return candidates

    def describe(self, candidate: Mapping) -> str:
        parts = [candidate.get("name") or "?"]
        area = candidate.get("area") or ""
        if area:
            parts.append(area)
        return " / ".join(parts)

    def fetch_fixtures(self, team_id: int, limit: int) -> list[dict]:
        data = self._get("/fixtures", {"team": int(team_id), "last": int(limit)})
        fixtures = []
        for row in data.get("response") or []:
            if not isinstance(row, Mapping):
                continue
            parsed = parse_apifootball_fixture(row, team_id)
            if parsed["status"] in APIFOOTBALL_FINISHED:
                fixtures.append(parsed)
        return fixtures


PROVIDER_CLASSES = {
    "fdorg": FootballDataOrgProvider,
    "apifootball": ApiFootballProvider,
}


def resolve_team_fdorg(
    name: str,
    competitions: Sequence[str] | None = None,
    fetch_roster: Callable[[str], list[dict]] | None = None,
    search_global: Callable[[str], list[dict]] | None = None,
    cache: MutableMapping[str, list[dict]] | None = None,
) -> dict:
    """Resolve uma equipa pelos rosters das competicoes (roster-first).

    Percorre no maximo FDORG_ROSTER_CAP codigos, reutilizando `cache` para nao
    pedir o mesmo roster duas vezes. Devolve
    {id, name, shortName, area, competition, competitions, source}.
    A pesquisa global (`search_global`) so corre como ultimo recurso, quando
    nenhum roster contem a equipa.
    """
    codes = normalise_competitions(competitions)[:FDORG_ROSTER_CAP]
    if not codes:
        raise ValueError("sem competicoes para procurar rosters")
    if fetch_roster is None:
        raise ValueError("resolve_team_fdorg exige fetch_roster")

    store = cache if cache is not None else {}
    found: dict | None = None
    first_code = ""
    hits: list[str] = []
    for code in codes:
        roster = store.get(code)
        if roster is None:
            roster = list(fetch_roster(code) or [])
            store[code] = roster
        candidate = match_roster_team(roster, name)
        if candidate is None:
            continue
        hits.append(code)
        if found is None:
            found = candidate
            first_code = code

    if found is not None:
        return {
            "id": _as_int(found.get("id")),
            "name": found.get("name") or name,
            "shortName": found.get("shortName") or "",
            "area": found.get("area") or "",
            "competition": first_code,
            "competitions": hits,
            "source": "roster",
        }

    # Ultimo recurso documentado: pesquisa global, instavel no plano free.
    if search_global is not None:
        print(
            "[aviso] football-data.org: busca global instavel no plano free; "
            f"'{name}' nao esta nos rosters {', '.join(codes)}"
        )
        chosen = pick_team(list(search_global(name) or []), name)
        return {
            "id": _as_int(chosen.get("id")),
            "name": chosen.get("name") or name,
            "shortName": chosen.get("shortName") or "",
            "area": chosen.get("area") or "",
            "competition": "",
            "competitions": [],
            "source": "global",
        }

    raise TeamNotFoundError(name)



# --------------------------------------------------------------------------
# Normalizacao de jogos
# --------------------------------------------------------------------------


def _to_utc(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_fdorg_fixture(match: Mapping, team_id: int | None = None) -> dict:
    score = (match.get("score") or {})
    full_time = (score.get("fullTime") or {}) if isinstance(score, Mapping) else {}
    home_team = match.get("homeTeam") or {}
    away_team = match.get("awayTeam") or {}
    competition = match.get("competition") or {}
    matchday = match.get("matchday")
    if team_id is None:
        team_id = _as_int(home_team.get("id")) or _as_int(away_team.get("id"))
    return {
        "provider": "fdorg",
        "team_id": team_id,
        "date_utc": match.get("utcDate") or "",
        "competition": competition.get("name") or "",
        "round": str(matchday) if matchday is not None else "",
        "home": home_team.get("name") or "",
        "away": away_team.get("name") or "",
        "goals_home": full_time.get("home"),
        "goals_away": full_time.get("away"),
        "status": match.get("status") or "FINISHED",
    }


def parse_apifootball_fixture(item: Mapping, team_id: int | None = None) -> dict:
    fixture = item.get("fixture") or {}
    league = item.get("league") or {}
    teams = item.get("teams") or {}
    goals = item.get("goals")
    if not isinstance(goals, Mapping):
        goals = {}
    home_team = teams.get("home") or {}
    away_team = teams.get("away") or {}
    status = (fixture.get("status") or {})
    if team_id is None:
        team_id = _as_int(home_team.get("id")) or _as_int(away_team.get("id"))
    return {
        "provider": "apifootball",
        "team_id": team_id,
        "date_utc": fixture.get("date") or "",
        "competition": league.get("name") or "",
        "round": league.get("round") or "",
        "home": home_team.get("name") or "",
        "away": away_team.get("name") or "",
        "goals_home": goals.get("home"),
        "goals_away": goals.get("away"),
        "status": (status.get("short") or "") if isinstance(status, Mapping) else str(status),
    }


def filter_by_days(
    fixtures: list[dict], days: int, now: datetime | None = None
) -> list[dict]:
    if not days or days <= 0:
        return list(fixtures)
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    lower = reference - timedelta(days=int(days))
    kept = []
    for fixture in fixtures:
        moment = _to_utc(fixture.get("date_utc"))
        if moment is None:
            continue
        if lower <= moment <= reference:
            kept.append(fixture)
    return kept


def apply_window(
    fixtures: list[dict], days: int, context: str, now: datetime | None = None
) -> list[dict]:
    windowed = filter_by_days(fixtures, days, now=now)
    if windowed or not fixtures:
        return windowed
    print(
        f"[aviso] {context}: filtro de --days {days} removeu todos os "
        f"{len(fixtures)} jogos; a mostrar sem filtro"
    )
    return list(fixtures)


def summarise_competitions(fixtures: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for fixture in fixtures:
        name = str(_field(fixture, "competition") or "?").strip() or "?"
        counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


# --------------------------------------------------------------------------
# Tabela de consola
# --------------------------------------------------------------------------


def _clip(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: max(width - 1, 0)] + "."


def format_fixture_row(fixture: Mapping) -> tuple[str, ...]:
    goals_home = fixture.get("goals_home")
    goals_away = fixture.get("goals_away")
    if goals_home is None or goals_away is None:
        score = "-"
    else:
        score = f"{goals_home}-{goals_away}"
    moment = _to_utc(fixture.get("date_utc"))
    date = moment.strftime("%Y-%m-%d %H:%M") if moment else (fixture.get("date_utc") or "-")
    return (
        str(date),
        str(fixture.get("competition") or "-"),
        str(fixture.get("home") or "-"),
        str(fixture.get("away") or "-"),
        score,
        str(fixture.get("status") or "-"),
    )


def render_table(rows: list[tuple[str, ...]]) -> list[str]:
    if not rows:
        return []
    widths = [len(name) for name in TABLE_HEADERS]
    for row in rows:
        for index, cell in enumerate(row[: len(TABLE_HEADERS)]):
            widths[index] = max(widths[index], len(str(cell)))
    widths = [
        min(width, cap) for width, cap in zip(widths, TABLE_MAX_WIDTH)
    ]
    header = "  ".join(
        _clip(name, width).ljust(width) for name, width in zip(TABLE_HEADERS, widths)
    ).rstrip()
    rule = "  ".join("-" * width for width in widths)
    lines = [header, rule]
    for row in rows:
        cells = [
            _clip(str(cell), width).ljust(width)
            for cell, width in zip(row, widths)
        ]
        lines.append("  ".join(cells).rstrip())
    return lines


# --------------------------------------------------------------------------
# Cobertura markdown
# --------------------------------------------------------------------------


def _md_cell(value: Any) -> str:
    return str(value if value is not None else "-").replace("|", "\\|").replace("\n", " ")


def _provider_section(label: str, items: list) -> list[str]:
    lines = [f"## {label}", "", "| equipa | id | competicoes (jogos) |", "| --- | --- | --- |"]
    total = 0
    notes = []
    resolution = []
    for item in items:
        fixtures = list(_field(item, "fixtures") or [])
        total += len(fixtures)
        counts = summarise_competitions(fixtures)
        cell = "; ".join(f"{name} ({count})" for name, count in counts.items()) or "sem jogos"
        team_id = _field(item, "team_id")
        lines.append(
            f"| {_md_cell(_field(item, 'team'))} | "
            f"{team_id if team_id is not None else '-'} | {_md_cell(cell)} |"
        )
        note = _field(item, "note")
        if note:
            notes.append(f"- {_field(item, 'team')}: {note}")
        source = str(_field(item, "source") or "")
        codes = [str(code) for code in (_field(item, "roster_competitions") or [])]
        if source or codes:
            resolution.append(
                f"| {_md_cell(_field(item, 'team'))} | "
                f"{_md_cell(source or '-')} | "
                f"{_md_cell(', '.join(codes) if codes else '-')} |"
            )
    lines.append(f"| _total_ | - | {total} jogos |")
    lines.append("")
    if resolution:
        lines.extend(
            [
                "### Resolucao de equipas (roster)",
                "",
                "| equipa | fonte | competicoes do roster |",
                "| --- | --- | --- |",
                *resolution,
                "",
                "`roster` = encontrada em `/v4/competitions/{CODE}/teams`; "
                "`global` = pesquisa instavel, ultimo recurso.",
                "",
            ]
        )
    if notes:
        lines.extend(notes)
        lines.append("")
    return lines



def build_free_tier_notes() -> list[str]:
    """Factos do plano gratuito do football-data.org, em markdown."""
    codes = ", ".join(FDORG_FREE_TIER)
    return [
        f"- O plano gratuito expoe **13 competicoes**: {codes}.",
        "- **BSA** (Brasileirao Serie A) e **CLI** (Copa Libertadores) estao "
        "incluidas e servem de roster para resolver as equipas.",
        "- **Copa do Brasil NAO esta disponivel** no plano gratuito.",
        "- **Campeonatos estaduais (Gauchao, Paulistao) NAO estao disponiveis**.",
        "- A pesquisa global `/v4/teams?name=` e instavel neste plano (ignora o "
        "filtro e devolve clubes aleatorios); so se usa como ultimo recurso.",
        "- Limite de 10 pedidos/minuto: `discover.py` espaca chamadas em 6.5 s.",
    ]


def build_coverage_markdown(
    results: list, generated_at: Any = None, preserved: Mapping[str, list] | None = None
) -> str:
    stamp = generated_at or datetime.now(timezone.utc)
    if isinstance(stamp, datetime):
        stamp_text = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        stamp_text = str(stamp)
    kept = {key: list(value) for key, value in dict(preserved or {}).items()}

    lines = [
        COVERAGE_HEADER,
        "",
        "# Cobertura - Fase 0",
        "",
        f"Gerado em {stamp_text}.",
        "",
        "Cada tabela mostra as competicoes devolvidas pelos fornecedores e o "
        "numero de jogos na janela consultada. Nao representa a epoca completa: "
        "serve para decidir que competicoes faltam.",
        "",
        f"## {FREE_TIER_TITLE}",
        "",
    ]
    lines.extend(build_free_tier_notes())
    lines.append("")

    by_provider: dict[str, list] = {}
    for item in results or []:
        provider = str(_field(item, "provider") or "?")
        by_provider.setdefault(provider, []).append(item)

    for provider in PROVIDER_ORDER:
        label = PROVIDER_LABELS[provider]
        items = by_provider.pop(provider, [])
        if items:
            lines.extend(_provider_section(label, items))
        elif kept.get(label):
            lines.extend([f"## {label}", ""])
            lines.extend(kept.pop(label))
            lines.append("")
        elif label in kept:
            kept.pop(label)

    for provider, items in by_provider.items():
        label = str(_field(items[0], "label") or provider)
        lines.extend(_provider_section(label, items))

    for title, body in kept.items():
        lines.extend([f"## {title}", ""])
        lines.extend(body)
        lines.append("")

    lines.extend(["## Checklist manual", ""])
    for check in CHECKLIST:
        lines.append(f"- [ ] {check}")
    lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _trim(lines: list[str]) -> list[str]:
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    end = len(lines)
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def split_markdown_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    title: str | None = None
    buffer: list[str] = []
    for line in str(text).splitlines():
        if line.startswith("## "):
            if title is not None:
                sections[title] = _trim(buffer)
            title = line[3:].strip()
            buffer = []
        elif title is not None:
            buffer.append(line)
    if title is not None:
        sections[title] = _trim(buffer)
    return sections


def write_coverage(
    path: Any, results: list, generated_at: Any = None
) -> Path:
    target = Path(path)
    preserved: dict[str, list[str]] = {}
    known_labels = set(PROVIDER_LABELS.values())
    active = {str(_field(item, "provider") or "?") for item in results or []}

    if target.exists():
        try:
            old = split_markdown_sections(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            old = {}
        for provider in PROVIDER_ORDER:
            label = PROVIDER_LABELS[provider]
            if provider in active:
                continue
            if label in old:
                preserved[label] = old[label]
        for title, body in old.items():
            if title in known_labels or title in ("Checklist manual", FREE_TIER_TITLE):
                continue
            preserved[title] = body

    text = build_coverage_markdown(results, generated_at, preserved)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


# --------------------------------------------------------------------------
# Config de equipas
# --------------------------------------------------------------------------


@dataclass
class TeamSpec:
    key: str
    display: str
    ids: dict[str, int | None] = field(default_factory=dict)


@dataclass
class TeamResult:
    provider: str
    label: str
    team: str
    team_id: int | None
    fixtures: list = field(default_factory=list)
    note: str = ""
    source: str = ""
    roster_competitions: list = field(default_factory=list)


@dataclass
class ResolvedTeam:
    id: int
    source: str = "search"
    roster_competitions: list = field(default_factory=list)


def load_team_config(path: Any = None) -> list[dict]:
    target = Path(path) if path is not None else CONFIG_TEAMS
    if not target.exists():
        return []
    try:
        import yaml  # noqa: PLC0415 - dependencia opcional, so para config
    except ImportError:
        print(
            "[aviso] PyYAML ausente: config/teams.yaml ignorado "
            "(pip install PyYAML)"
        )
        return []
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError):
        print(f"[erro] nao foi possivel ler {target}")
        return []
    except Exception as exc:  # yaml.YAMLError e outros
        print(f"[erro] YAML invalido em {target}: {exc}")
        return []
    teams = data.get("teams") if isinstance(data, Mapping) else None
    if not isinstance(teams, list):
        print(f"[aviso] {target} sem lista 'teams'; ignorado")
        return []
    return [entry for entry in teams if isinstance(entry, Mapping)]


def _yaml_scalar(value: Any) -> str:
    text = "" if value is None else str(value)
    plain_chars = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _-."
    )
    plain = bool(text) and all(
        ch in plain_chars or ord(ch) > 127 for ch in text
    )
    reserved = {"null", "true", "false", "yes", "no", "on", "off", "~"}
    if plain and text.strip() == text and text.lower() not in reserved:
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_teams_config(
    path: Any, entries: list, force: bool = False, generated_at: Any = None
) -> bool:
    target = Path(path)
    if target.exists() and not force:
        return False
    stamp = generated_at or datetime.now(timezone.utc)
    stamp_text = (
        stamp.strftime("%Y-%m-%d") if isinstance(stamp, datetime) else str(stamp)
    )
    lines = [f"# Gerado por discover.py em {stamp_text}", "teams:"]
    for entry in entries:
        fdorg_id = _as_int(_field(entry, "fdorg_id"))
        api_id = _as_int(_field(entry, "apifootball_id"))
        lines.append(f"  - key: {_yaml_scalar(_field(entry, 'key'))}")
        lines.append(f"    display: {_yaml_scalar(_field(entry, 'display'))}")
        lines.append(f"    fdorg_id: {fdorg_id if fdorg_id else 'null'}")
        lines.append(
            f"    apifootball_id: {api_id if api_id else 'null'}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def build_team_specs(args: argparse.Namespace) -> list[TeamSpec]:
    config = load_team_config()
    by_key = {str(entry.get("key")): entry for entry in config if entry.get("key")}

    specs: list[TeamSpec] = []
    if args.team:
        for raw in args.team:
            display = str(raw).strip()
            if not display:
                continue
            key = slugify(display)
            entry = by_key.get(key) or {}
            specs.append(
                TeamSpec(
                    key=key,
                    display=display,
                    ids={
                        "fdorg": _as_int(entry.get("fdorg_id")),
                        "apifootball": _as_int(entry.get("apifootball_id")),
                    },
                )
            )
    elif config:
        for entry in config:
            display = str(entry.get("display") or entry.get("key") or "").strip()
            if not display:
                continue
            key = str(entry.get("key") or slugify(display))
            specs.append(
                TeamSpec(
                    key=key,
                    display=display,
                    ids={
                        "fdorg": _as_int(entry.get("fdorg_id")),
                        "apifootball": _as_int(entry.get("apifootball_id")),
                    },
                )
            )
    else:
        print("[aviso] config/teams.yaml ausente ou vazio; a usar equipas por omissao")
        for key, display in DEFAULT_TEAMS:
            specs.append(TeamSpec(key=key, display=display, ids={}))

    seen: set[str] = set()
    unique: list[TeamSpec] = []
    for spec in specs:
        if spec.key in seen:
            continue
        seen.add(spec.key)
        unique.append(spec)
    return unique


def parse_team_id_args(values: list[str]) -> dict[str, int]:
    overrides: dict[str, int] = {}
    for raw in values or []:
        text = str(raw).strip()
        provider, separator, ident = text.partition(":")
        provider = provider.strip().lower()
        if not separator or provider not in PROVIDER_ORDER:
            raise ValueError(
                f"--team-id invalido: {text!r} (esperado fdorg:ID ou apifootball:ID)"
            )
        try:
            number = int(ident.strip())
        except ValueError:
            raise ValueError(f"--team-id invalido: {text!r} (ID nao numerico)") from None
        if number <= 0:
            raise ValueError(f"--team-id invalido: {text!r} (ID tem de ser positivo)")
        overrides[provider] = number
    return overrides


def build_config_entries(
    specs: list[TeamSpec], resolved: Mapping[str, Mapping[str, int]]
) -> list[dict]:
    entries = []
    for spec in specs:
        found = dict(resolved.get(spec.key) or {})
        entries.append(
            {
                "key": spec.key,
                "display": spec.display,
                "fdorg_id": found.get("fdorg") or spec.ids.get("fdorg"),
                "apifootball_id": found.get("apifootball")
                or spec.ids.get("apifootball"),
            }
        )
    return entries


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def configure_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="discover.py",
        description=(
            "Fase 0: descobre IDs de equipa e cobertura de competicoes em "
            "football-data.org e api-football."
        ),
        epilog=(
            "Exemplos:\n"
            "  python discover.py\n"
            "  python discover.py --provider apifootball --team Gremio --days 60\n"
            "  python discover.py --team-id fdorg:130 --limit 5\n"
            "  python discover.py --write-config --force\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--provider",
        choices=("fdorg", "apifootball", "both"),
        default="both",
        help="fornecedor a consultar (default: both)",
    )
    parser.add_argument(
        "--team",
        action="append",
        metavar="NAME",
        help="nome da equipa (repetivel); por omissao le config/teams.yaml",
    )
    parser.add_argument(
        "--team-id",
        action="append",
        default=[],
        dest="team_id",
        metavar="PROVIDER:ID",
        help="ID fixo, ex. fdorg:130; evita a pesquisa (repetivel, aplica-se a 1a equipa)",
    )
    parser.add_argument(
        "--fdorg-competition",
        action="append",
        dest="fdorg_competition",
        metavar="CODE",
        default=None,
        help=(
            "codigo de competicao cujo roster resolve equipas em football-data.org "
            f"(repetivel; default: {', '.join(FDORG_DEFAULT_COMPETITIONS)}; "
            f"max {FDORG_ROSTER_CAP})"
        ),
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="janela informativa em dias (default: 30; 0 desliga)",
    )
    parser.add_argument(
        "--limit", type=int, default=10, help="jogos por equipa (default: 10)"
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_OUT,
        help=f"ficheiro de cobertura (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--write-config",
        action="store_true",
        dest="write_config",
        help="escreve config/teams.yaml com os IDs encontrados",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="permite sobrepor config/teams.yaml e a cobertura",
    )
    return parser


def resolve_team_id(
    provider_name: str,
    provider: Any,
    spec: TeamSpec,
    override: int | None,
    competitions: Sequence[str] | None = None,
) -> ResolvedTeam:
    if override is not None:
        return ResolvedTeam(id=override, source="override")
    from_config = spec.ids.get(provider_name)
    if from_config:
        print(f"[ok] {provider.label}: '{spec.display}' -> {from_config} (config)")
        return ResolvedTeam(id=from_config, source="config")

    if provider_name == "fdorg":
        match = resolve_team_fdorg(
            spec.display,
            competitions,
            fetch_roster=provider.fetch_roster,
            search_global=provider.search_teams,
            cache=provider.roster_cache,
        )
        team_id = _as_int(match.get("id"))
        if team_id is None:
            raise ProviderError(f"candidato sem id para '{spec.display}'")
        codes = list(match.get("competitions") or [])
        source = str(match.get("source") or "roster")
        label = match.get("name") or spec.display
        suffix = f" / {source}: {', '.join(codes) if codes else '-'}"
        print(f"[ok] {provider.label}: '{spec.display}' -> {team_id} ({label}{suffix})")
        return ResolvedTeam(id=team_id, source=source, roster_competitions=codes)

    candidates = provider.search_teams(spec.display)
    if candidates:
        listing = "; ".join(provider.describe(c) for c in candidates[:8])
        print(
            f"[aviso] {provider.label}: {len(candidates)} candidato(s) para "
            f"'{spec.display}': {listing}"
        )
    chosen = pick_team(candidates, spec.display)
    team_id = _as_int(chosen.get("id"))
    if team_id is None:
        raise ProviderError(f"candidato sem id para '{spec.display}'")
    label = chosen.get("name") or spec.display
    area = chosen.get("area") or ""
    suffix = f" / {area}" if area else ""
    print(f"[ok] {provider.label}: '{spec.display}' -> {team_id} ({label}{suffix})")
    return ResolvedTeam(id=team_id, source="search")


def discover(
    args: argparse.Namespace, instances: dict[str, Any]
) -> tuple[list[TeamResult], dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    overrides = parse_team_id_args(args.team_id)
    specs = build_team_specs(args)
    roster_codes = normalise_competitions(getattr(args, "fdorg_competition", None))
    if len(roster_codes) > FDORG_ROSTER_CAP:
        print(
            f"[aviso] --fdorg-competition: a usar apenas os primeiros "
            f"{FDORG_ROSTER_CAP} codigos ({', '.join(roster_codes[:FDORG_ROSTER_CAP])})"
        )
        roster_codes = roster_codes[:FDORG_ROSTER_CAP]
    if overrides and len(specs) > 1:
        print("[aviso] --team-id aplica-se apenas a primeira equipa")

    results: list[TeamResult] = []
    resolved: dict[str, dict[str, int]] = {}
    stats: dict[str, dict[str, int]] = {}

    for provider_name, provider in instances.items():
        stats[provider_name] = {"attempted": 0, "succeeded": 0, "failed": 0}
        for index, spec in enumerate(specs):
            stats[provider_name]["attempted"] += 1
            override = overrides.get(provider_name) if index == 0 else None
            try:
                match = resolve_team_id(
                    provider_name, provider, spec, override, roster_codes
                )
            except AmbiguousTeamError as exc:
                print(f"[erro] {provider.label}: {exc}")
                stats[provider_name]["failed"] += 1
                continue
            except ProviderError as exc:
                print(f"[erro] {provider.label}: pesquisa de '{spec.display}': {exc}")
                stats[provider_name]["failed"] += 1
                continue
            except Exception as exc:
                print(f"[erro] {provider.label}: falha na pesquisa de '{spec.display}': {exc}")
                stats[provider_name]["failed"] += 1
                continue

            team_id = match.id
            if team_id is None:
                print(f"[erro] {provider.label}: sem ID para '{spec.display}'")
                stats[provider_name]["failed"] += 1
                continue
            resolved.setdefault(spec.key, {})[provider_name] = team_id


            try:
                fixtures = provider.fetch_fixtures(team_id, max(int(args.limit), 1))
            except ProviderError as exc:
                print(
                    f"[erro] {provider.label}: jogos de {spec.display} "
                    f"({team_id}): {exc}"
                )
                stats[provider_name]["failed"] += 1
                continue
            except Exception as exc:
                print(
                    f"[erro] {provider.label}: falha inesperada nos jogos de "
                    f"{spec.display} ({team_id}): {exc}"
                )
                stats[provider_name]["failed"] += 1
                continue

            windowed = apply_window(
                fixtures, args.days, f"{provider.label}/{spec.display}"
            )
            windowed.sort(key=lambda fx: fx.get("date_utc") or "")
            print(f"--- {provider.label} | {spec.display} | id {team_id} ---")
            rows = [format_fixture_row(fixture) for fixture in windowed]
            if rows:
                for line in render_table(rows):
                    print(line)
            else:
                print("(sem jogos devolvidos)")
            comp_counts = summarise_competitions(windowed)
            if comp_counts:
                print(
                    f"[ok] {len(windowed)} jogos | competicoes: "
                    + "; ".join(comp_counts)
                )
            else:
                print("[ok] 0 jogos | competicoes: -")
            print()

            stats[provider_name]["succeeded"] += 1
            results.append(
                TeamResult(
                    provider=provider_name,
                    label=provider.label,
                    team=spec.display,
                    team_id=team_id,
                    fixtures=windowed,
                    source=match.source,
                    roster_competitions=list(match.roster_competitions),
                )
            )

    return results, resolved, stats


def main(argv: list[str] | None = None) -> int:
    configure_output()
    parser = build_parser()
    args = parser.parse_args(argv)

    load_env(ENV_PATH)

    requested = (
        ["fdorg", "apifootball"]
        if args.provider == "both"
        else [args.provider]
    )

    instances: dict[str, Any] = {}
    for provider_name in requested:
        env_key = PROVIDER_ENV[provider_name]
        api_key = (os.environ.get(env_key) or "").strip()
        if not api_key:
            print(
                f"[aviso] {env_key} ausente: {PROVIDER_LABELS[provider_name]} ignorado"
            )
            continue
        instances[provider_name] = PROVIDER_CLASSES[provider_name](api_key)

    if not instances:
        print(
            "[erro] Nenhuma chave encontrada. Crie .env a partir de .env.example."
        )
        return 2

    try:
        results, resolved, stats = discover(args, instances)
    except ValueError as exc:
        print(f"[erro] {exc}")
        return 2

    if args.write_config:
        specs = build_team_specs(args)
        entries = build_config_entries(specs, resolved)
        if write_teams_config(CONFIG_TEAMS, entries, force=args.force):
            print(f"[ok] config/teams.yaml escrito ({len(entries)} equipas)")
        else:
            print("[aviso] config/teams.yaml ja existe (use --force)")

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    if results:
        write_coverage(out_path, results)
        print(f"[ok] cobertura escrita em {out_path}")
    else:
        print("[aviso] cobertura nao atualizada (sem jogos)")

    failed = [
        name for name, counters in stats.items() if counters["succeeded"] == 0
    ]
    if stats and len(failed) == len(stats):
        print("[erro] todos os fornecedores falharam")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("[aviso] interrompido pelo utilizador")
        sys.exit(130)
