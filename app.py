#!/usr/bin/env python3
"""football-bot / Fase 1 + 2 - sincronizar jogos, deduplicar e renderizar.

Um unico ponto de entrada CLI:

    python app.py sync [--days-back 10] [--days-forward 7]
    python app.py due [--chat CHAT_ID] [--limit 20]
    python app.py mark-sent FIXTURE_ID --chat CHAT_ID [--message-id N]
    python app.py deliver [--only KEY] [--limit N] [--dry-run]
    python app.py check [--limit N] [--dry-run] [--margin-min CODE=MIN]

`sync` faz um ou dois pedidos a football-data.org (/v4/matches por janela de
datas; o free tier limita cada pedido a 10 dias, por isso a janela e dividida),
filtra os jogos de Gremio/Corinthians e faz upsert em `data/football.db`.
Nunca apaga linhas e nunca apaga um placar conhecido com NULL.

`due` lista os jogos finalizados que ainda nao foram entregues nesse chat.
Sem `--chat` usa o literal `dry-run`: imprime as mensagens mas nao grava nada
em `sent` (so `mark-sent` escreve nessa tabela).

`deliver` (Fase 4) le os destinos de `config/chats.yaml`, envia cada jogo
devido pela Bot API e grava `sent` apenas depois de um envio com sucesso.
`--dry-run` mostra o que seria enviado sem rede e sem escrever em `sent`.

`check` (Fase 5) e o comando do cron. Primeiro corre um gate local, sem rede:
um jogo da janela [agora - 8 h, agora + 30 min] so e "checkable" quando
`utc_date + margem + grace` ja passou (margem 120 min em `BSA`, 165 min em
`CLI`). Sem nada checkable imprime uma linha e sai 0 sem rede e sem escrever
na base de dados. Com algo checkable faz um `sync` de 3 dias (1 pedido) e a
mesma entrega do `deliver`.

Reutiliza de `discover.py` o `RateLimiter` (6.5 s entre pedidos), o
`http_get_json` (stdlib urllib), o `load_env` e o `load_team_config`. PyYAML
continua opcional: sem ele usa-se a lista de equipas de reserva.

Marcadores de consola (ASCII, sem emoji): [ok] [erro] [aviso]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from discover import (
    ENV_PATH,
    FDORG_BASE,
    PROVIDER_ENV,
    ProviderError,
    RateLimiter,
    ROOT,
    configure_output,
    http_get_json,
    load_env,
    load_team_config,
)
from render import ascii_text, render_match
from telegram import TelegramError, load_chats, send_message

DEFAULT_DB = "data/football.db"
DEFAULT_CHATS = "config/chats.yaml"
DRY_RUN_CHAT = "dry-run"
CHAT_SEND_INTERVAL = 1.0
COMPETITIONS = ("BSA", "CLI")
FINISHED_STATUSES = frozenset({"FINISHED", "AWARDED"})
# O free tier recusa janelas maiores: "Specified period must not exceed 10 days."
MAX_WINDOW_DAYS = 10

# Fase 5: gate local do `check` (sem rede). A margem cobre o tempo tipico de
# jogo + intervalo + descontos; o grace evita pedir no minuto exacto do fim.
DEFAULT_MARGINS = {"BSA": 120, "CLI": 165}
DEFAULT_GRACE_MIN = 15
CHECK_LOOKBACK = timedelta(hours=8)
CHECK_LOOKAHEAD = timedelta(minutes=30)
CHECK_SYNC_DAYS_BACK = 1
CHECK_SYNC_DAYS_FORWARD = 1

# Colunas comparadas para decidir se um upsert escreve alguma coisa. Se nada
# mudou o UPDATE e saltado e o ficheiro da base de dados fica intacto (o CI
# nao cria commits vazios). `updated_at` so muda com estes campos materiais.
COMPARED_COLUMNS = (
    "provider",
    "competition_code",
    "competition",
    "stage",
    "round",
    "utc_date",
    "status",
    "home_id",
    "home_name",
    "away_id",
    "away_name",
    "goals_home",
    "goals_away",
    "penalties_home",
    "penalties_away",
    "winner",
    "venue",
)
ALWAYS_REPLACED = frozenset({"provider", "utc_date"})
MATERIAL_FIELDS = (
    "status",
    "goals_home",
    "goals_away",
    "penalties_home",
    "penalties_away",
    "venue",
)

# Reserva quando config/teams.yaml nao pode ser lido (PyYAML ausente).
FALLBACK_TEAMS = {1767: "Gremio", 1779: "Corinthians"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
  fixture_id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL DEFAULT 'fdorg',
  competition_code TEXT,
  competition TEXT,
  stage TEXT,
  round TEXT,
  utc_date TEXT NOT NULL,
  status TEXT,
  home_id INTEGER, home_name TEXT,
  away_id INTEGER, away_name TEXT,
  goals_home INTEGER, goals_away INTEGER,
  penalties_home INTEGER, penalties_away INTEGER,
  winner TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sent (
  fixture_id INTEGER NOT NULL,
  chat_id TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  message_id INTEGER,
  PRIMARY KEY (fixture_id, chat_id)
);
"""

# O esquema pedido nao inclui `venue`; a coluna e acrescentada (nullable) para
# a linha "Estadio:" poder aparecer. Additive e idempotente via ALTER TABLE.
EXTRA_COLUMNS = (("matches", "venue", "TEXT"),)

UPSERT_SQL = """
INSERT INTO matches (
  fixture_id, provider, competition_code, competition, stage, round, utc_date,
  status, home_id, home_name, away_id, away_name, goals_home, goals_away,
  penalties_home, penalties_away, winner, venue, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(fixture_id) DO UPDATE SET
  provider = excluded.provider,
  competition_code = COALESCE(excluded.competition_code, matches.competition_code),
  competition = COALESCE(excluded.competition, matches.competition),
  stage = COALESCE(excluded.stage, matches.stage),
  round = COALESCE(excluded.round, matches.round),
  utc_date = excluded.utc_date,
  status = COALESCE(excluded.status, matches.status),
  home_id = COALESCE(excluded.home_id, matches.home_id),
  home_name = COALESCE(excluded.home_name, matches.home_name),
  away_id = COALESCE(excluded.away_id, matches.away_id),
  away_name = COALESCE(excluded.away_name, matches.away_name),
  goals_home = COALESCE(excluded.goals_home, matches.goals_home),
  goals_away = COALESCE(excluded.goals_away, matches.goals_away),
  penalties_home = COALESCE(excluded.penalties_home, matches.penalties_home),
  penalties_away = COALESCE(excluded.penalties_away, matches.penalties_away),
  winner = COALESCE(excluded.winner, matches.winner),
  venue = COALESCE(excluded.venue, matches.venue),
  updated_at = excluded.updated_at
"""

DUE_SQL = """
SELECT m.* FROM matches AS m
WHERE m.status IN ('FINISHED', 'AWARDED')
  AND m.goals_home IS NOT NULL
  AND m.goals_away IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM sent AS s
    WHERE s.fixture_id = m.fixture_id AND s.chat_id = ?
  )
ORDER BY m.utc_date ASC, m.fixture_id ASC
LIMIT ?
"""


# --------------------------------------------------------------------------
# Base de dados
# --------------------------------------------------------------------------


def resolve_db_path(value: Any = None) -> Path:
    """Caminhos relativos ficam sempre dentro do projeto (DB portatil)."""
    path = Path(value) if value else Path(DEFAULT_DB)
    if not path.is_absolute():
        path = ROOT / path
    return path


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, decl: str
) -> None:
    names = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in names:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db(conn: sqlite3.Connection) -> None:
    """Cria esquema e migracoes; seguro correr varias vezes."""
    conn.executescript(SCHEMA)
    for table, column, decl in EXTRA_COLUMNS:
        _ensure_column(conn, table, column, decl)
    conn.commit()


def connect(path: Any = None) -> sqlite3.Connection:
    target = resolve_db_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def connect_readonly(path: Any = None) -> sqlite3.Connection | None:
    """Conexao `mode=ro`: o gate do `check` nunca pode tocar no ficheiro.

    Devolve None quando a base ainda nao existe (primeira execucao), para o
    `check` poder responder "nada na janela" sem criar `data/football.db`.
    """
    target = resolve_db_path(path)
    if not target.exists():
        return None
    try:
        conn = sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------------
# Normalizacao
# --------------------------------------------------------------------------


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def normalise_match(raw: Mapping) -> dict:
    """Converte um jogo do football-data.org para a linha de `matches`.

    `goals_*` e `penalties_*` ficam None quando a API os omite: nunca 0.
    """
    score = raw.get("score")
    score = score if isinstance(score, Mapping) else {}
    full_time = score.get("fullTime")
    full_time = full_time if isinstance(full_time, Mapping) else {}
    penalties = score.get("penalties")
    penalties = penalties if isinstance(penalties, Mapping) else {}
    competition = raw.get("competition")
    competition = competition if isinstance(competition, Mapping) else {}
    home = raw.get("homeTeam")
    home = home if isinstance(home, Mapping) else {}
    away = raw.get("awayTeam")
    away = away if isinstance(away, Mapping) else {}
    matchday = raw.get("matchday")

    return {
        "fixture_id": _as_int(raw.get("id")),
        "provider": "fdorg",
        "competition_code": _as_text(competition.get("code")),
        "competition": _as_text(competition.get("name")),
        "stage": _as_text(raw.get("stage")),
        "round": str(_as_int(matchday)) if _as_int(matchday) is not None else None,
        "utc_date": _as_text(raw.get("utcDate")) or "",
        "status": (_as_text(raw.get("status")) or "").upper() or None,
        "home_id": _as_int(home.get("id")),
        "home_name": _as_text(home.get("name")),
        "away_id": _as_int(away.get("id")),
        "away_name": _as_text(away.get("name")),
        "goals_home": _as_int(full_time.get("home")),
        "goals_away": _as_int(full_time.get("away")),
        "penalties_home": _as_int(penalties.get("home")),
        "penalties_away": _as_int(penalties.get("away")),
        "winner": _as_text(score.get("winner")),
        "venue": _as_text(raw.get("venue")),
    }


def filter_matches(
    matches: Iterable[Mapping], team_ids: Iterable[int]
) -> list[dict]:
    """Mantem apenas jogos com casa ou fora nas equipas configuradas."""
    wanted = {int(team_id) for team_id in team_ids if _as_int(team_id) is not None}
    kept: list[dict] = []
    for match in matches:
        home = _as_int(match.get("home_id"))
        away = _as_int(match.get("away_id"))
        if home in wanted or away in wanted:
            kept.append(dict(match))
    return kept


# --------------------------------------------------------------------------
# Upsert / sync
# --------------------------------------------------------------------------


def _stamp(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _score_text(score: tuple[Any, Any] | None) -> str:
    if not score:
        return "-"
    home, away = score
    left = "-" if home is None else str(home)
    right = "-" if away is None else str(away)
    return f"{left}-{right}"


def _incoming_row(fixture_id: int, match: Mapping) -> dict:
    """Linha que o upsert tentaria gravar (NULL = manter o valor anterior)."""
    return {
        "fixture_id": fixture_id,
        "provider": str(match.get("provider") or "fdorg"),
        "competition_code": match.get("competition_code"),
        "competition": match.get("competition"),
        "stage": match.get("stage"),
        "round": match.get("round"),
        "utc_date": str(match.get("utc_date")),
        "status": match.get("status"),
        "home_id": _as_int(match.get("home_id")),
        "home_name": match.get("home_name"),
        "away_id": _as_int(match.get("away_id")),
        "away_name": match.get("away_name"),
        "goals_home": _as_int(match.get("goals_home")),
        "goals_away": _as_int(match.get("goals_away")),
        "penalties_home": _as_int(match.get("penalties_home")),
        "penalties_away": _as_int(match.get("penalties_away")),
        "winner": match.get("winner"),
        "venue": match.get("venue"),
    }


def upsert_match(
    conn: sqlite3.Connection, match: Mapping, now: datetime | None = None
) -> dict:
    """Insere ou actualiza (sem apagar placares conhecidos com NULL).

    Uma re-sincronizacao de dados identicos nao escreve nada: o UPDATE so
    corre quando alguma coluna comparavel muda. `updated_at` so avanca quando
    mudam `status`, golos, penalties ou `venue`; as restantes alteracoes
    mantem o carimbo anterior.
    """
    fixture_id = _as_int(match.get("fixture_id"))
    if fixture_id is None:
        raise ValueError("jogo sem fixture_id")
    if not match.get("utc_date"):
        raise ValueError(f"jogo {fixture_id} sem utc_date")

    incoming = _incoming_row(fixture_id, match)
    previous = conn.execute(
        "SELECT * FROM matches WHERE fixture_id = ?", (fixture_id,)
    ).fetchone()

    if previous is None:
        conn.execute(
            UPSERT_SQL,
            (fixture_id,)
            + tuple(incoming[column] for column in COMPARED_COLUMNS)
            + (_stamp(now),),
        )
        return {
            "fixture_id": fixture_id,
            "inserted": True,
            "changed": True,
            "material": True,
            "score_changed": False,
            "before": None,
            "after": (incoming["goals_home"], incoming["goals_away"]),
            "label": f"{match.get('home_name') or '?'} x {match.get('away_name') or '?'}",
        }

    before = dict(previous)
    merged = dict(before)
    for column in COMPARED_COLUMNS:
        value = incoming[column]
        if column in ALWAYS_REPLACED or value is not None:
            merged[column] = value

    differences = [
        column for column in COMPARED_COLUMNS if merged[column] != before[column]
    ]
    material = any(column in MATERIAL_FIELDS for column in differences)
    score_changed = (before["goals_home"], before["goals_away"]) != (
        merged["goals_home"],
        merged["goals_away"],
    )

    if differences:
        stamp = _stamp(now) if material else before["updated_at"]
        conn.execute(
            UPSERT_SQL,
            (fixture_id,)
            + tuple(merged[column] for column in COMPARED_COLUMNS)
            + (stamp,),
        )

    return {
        "fixture_id": fixture_id,
        "inserted": False,
        "changed": bool(differences),
        "material": material,
        "score_changed": score_changed,
        "before": (before["goals_home"], before["goals_away"]),
        "after": (merged["goals_home"], merged["goals_away"]),
        "label": f"{match.get('home_name') or '?'} x {match.get('away_name') or '?'}",
    }


def sync_fixtures(
    conn: sqlite3.Connection,
    matches: Iterable[Mapping],
    now: datetime | None = None,
) -> dict:
    """Upsert de uma lista normalizada; devolve contagens e mudancas de placar."""
    items = list(matches)
    inserted = 0
    updated = 0
    changed = 0
    changes: list[dict] = []
    for match in items:
        try:
            result = upsert_match(conn, match, now=now)
        except ValueError as exc:
            print(f"[aviso] sync: {exc} (jogo ignorado)")
            continue
        if result["inserted"]:
            inserted += 1
        else:
            updated += 1
        if result["changed"]:
            changed += 1
        if result["score_changed"]:
            changes.append(result)
    conn.commit()

    finished = sum(
        1
        for match in items
        if (str(match.get("status") or "")).upper() in FINISHED_STATUSES
    )
    return {
        "total": len(items),
        "inserted": inserted,
        "updated": updated,
        "changed": changed,
        "finished": finished,
        "scheduled": len(items) - finished,
        "changes": changes,
    }


# --------------------------------------------------------------------------
# Gate do check (Fase 5, sem rede)
# --------------------------------------------------------------------------


def _as_utc(value: datetime | None = None) -> datetime:
    moment = value or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _parse_utc(value: Any) -> datetime | None:
    """ISO 8601 (com Z ou offset) -> datetime UTC; None se nao der para ler."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _as_utc(parsed)


def parse_margin_overrides(values: Iterable[str] | None) -> dict[str, int]:
    """`--margin-min BSA=120` (repetivel) sobrepoe DEFAULT_MARGINS."""
    margins = dict(DEFAULT_MARGINS)
    for raw in values or []:
        text = str(raw).strip()
        if not text:
            continue
        code, separator, value = text.partition("=")
        code = code.strip().upper()
        minutes = _as_int(value)
        if not separator or not code or minutes is None or minutes < 0:
            raise ValueError(
                f"--margin-min invalido: {raw} (formato CODE=MIN, minutos >= 0)"
            )
        margins[code] = minutes
    return margins


def match_gate(
    match: Mapping,
    margins: Mapping[str, int] | None = None,
    grace: int = DEFAULT_GRACE_MIN,
) -> datetime | None:
    """Instante a partir do qual vale a pena perguntar pela API por este jogo.

    gate = kickoff + margem da competicao + grace. Uma competicao fora de
    `margins` usa a maior margem conhecida (a opcao conservadora).
    """
    kickoff = _parse_utc(match.get("utc_date"))
    if kickoff is None:
        return None
    table = dict(margins if margins is not None else DEFAULT_MARGINS)
    code = str(match.get("competition_code") or "").strip().upper()
    margin = table.get(code)
    if margin is None:
        margin = max(table.values(), default=max(DEFAULT_MARGINS.values()))
    return kickoff + timedelta(minutes=int(margin) + int(grace))


def _window_rows(
    conn: sqlite3.Connection, now: datetime | None
) -> list[dict]:
    """Linhas cujo kickoff cai em [agora - 8 h, agora + 30 min]."""
    moment = _as_utc(now)
    start = moment - CHECK_LOOKBACK
    end = moment + CHECK_LOOKAHEAD
    rows: list[dict] = []
    for row in conn.execute("SELECT * FROM matches ORDER BY utc_date ASC"):
        kickoff = _parse_utc(row["utc_date"])
        if kickoff is not None and start <= kickoff <= end:
            rows.append(dict(row))
    return rows


def checkable_matches(
    conn: sqlite3.Connection,
    now: datetime | None = None,
    margins: Mapping[str, int] | None = None,
    grace: int = DEFAULT_GRACE_MIN,
) -> list[dict]:
    """Jogos da janela, ainda nao finalizados, com o gate ja passado."""
    moment = _as_utc(now)
    pending: list[dict] = []
    for row in _window_rows(conn, moment):
        status = (str(row.get("status") or "")).upper()
        if status in FINISHED_STATUSES:
            continue
        gate = match_gate(row, margins, grace)
        if gate is not None and moment >= gate:
            pending.append(row)
    return pending


def count_future_matches(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """Jogos com kickoff no futuro (para a mensagem "nada na janela")."""
    moment = _as_utc(now)
    future = 0
    for row in conn.execute("SELECT utc_date FROM matches"):
        kickoff = _parse_utc(row["utc_date"])
        if kickoff is not None and kickoff > moment:
            future += 1
    return future


# --------------------------------------------------------------------------
# Fila de entrega
# --------------------------------------------------------------------------


def due_matches(
    conn: sqlite3.Connection, chat: str = DRY_RUN_CHAT, limit: int = 20
) -> list[dict]:
    """Jogos finalizados com placar e ainda nao entregues nesse chat."""
    maximum = _as_int(limit)
    if maximum is None or maximum <= 0:
        return []
    rows = conn.execute(DUE_SQL, (str(chat), maximum)).fetchall()
    return [dict(row) for row in rows]


def due_messages(
    conn: sqlite3.Connection,
    chat: str = DRY_RUN_CHAT,
    limit: int = 20,
    teams: Mapping[int, str] | None = None,
) -> list[str]:
    return [render_match(row, teams) for row in due_matches(conn, chat, limit)]


def mark_sent(
    conn: sqlite3.Connection,
    fixture_id: int,
    chat: str,
    message_id: int | None = None,
    now: datetime | None = None,
) -> bool:
    """Grava a entrega; idempotente (INSERT OR IGNORE). True se inseriu."""
    fixture = _as_int(fixture_id)
    if fixture is None:
        raise ValueError("fixture_id invalido")
    cursor = conn.execute(
        "INSERT OR IGNORE INTO sent (fixture_id, chat_id, sent_at, message_id) "
        "VALUES (?, ?, ?, ?)",
        (fixture, str(chat), _stamp(now), _as_int(message_id)),
    )
    conn.commit()
    return cursor.rowcount > 0


def _match_label(match: Mapping, teams: Mapping[int, str] | None = None) -> str:
    """`Home x Away` com os nomes de exibicao da config."""
    displays = dict(teams or {})
    home = (
        ascii_text(displays.get(match.get("home_id")) or match.get("home_name") or "")
        or "?"
    )
    away = (
        ascii_text(displays.get(match.get("away_id")) or match.get("away_name") or "")
        or "?"
    )
    return f"{home} x {away}"


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line if line else line for line in text.splitlines())


def deliver(
    conn: sqlite3.Connection,
    chats: Iterable[Mapping],
    teams: Mapping[int, str] | None = None,
    limit: int = 20,
    only: str | None = None,
    dry_run: bool = False,
    transport: Any = None,
    sleeper: Any = None,
    token: str | None = None,
    out: Any = print,
) -> dict:
    """Entrega os jogos devidos a cada chat; grava `sent` so apos sucesso.

    Um chat que falha nao interrompe os restantes. Entre dois envios ao mesmo
    chat espera `CHAT_SEND_INTERVAL` segundos (limite por chat do Telegram).
    """
    selected = [dict(chat) for chat in chats]
    if only:
        selected = [chat for chat in selected if chat.get("key") == only]
        if not selected:
            raise ValueError(f"chat desconhecido: {only}")

    sleep = sleeper or time.sleep
    delivered = 0
    failed = 0
    pending = 0

    for chat in selected:
        key = str(chat.get("key"))
        chat_id = str(chat.get("chat_id"))
        rows = due_matches(conn, chat_id, limit)
        attempts = 0
        for row in rows:
            fixture_id = row["fixture_id"]
            label = _match_label(row, teams)
            text = render_match(row, teams)

            if dry_run:
                out(f"[ok] {key} {fixture_id} {label} (dry-run)")
                out(_indent(text))
                pending += 1
                continue

            if attempts:
                sleep(CHAT_SEND_INTERVAL)
            attempts += 1

            try:
                message_id = send_message(
                    chat_id,
                    text,
                    silent=bool(chat.get("silent")),
                    token=token,
                    transport=transport,
                    sleeper=sleep,
                )
            except TelegramError as exc:
                out(f"[erro] {key} {fixture_id}: {exc.description}")
                failed += 1
                continue

            mark_sent(conn, fixture_id, chat_id, message_id)
            out(f"[ok] {key} {fixture_id} {label} -> message_id={message_id}")
            delivered += 1

    return {"delivered": delivered, "failed": failed, "pending": pending}


# --------------------------------------------------------------------------
# Equipas / API
# --------------------------------------------------------------------------


def load_teams(path: Any = None) -> dict[int, str]:
    """{fdorg_id: display} a partir de config/teams.yaml."""
    teams: dict[int, str] = {}
    for entry in load_team_config(path):
        team_id = _as_int(entry.get("fdorg_id"))
        display = _as_text(entry.get("display") or entry.get("key")) or ""
        if team_id is not None and display:
            teams[team_id] = display
    return teams


def load_teams_or_fallback() -> dict[int, str]:
    teams = load_teams()
    if teams:
        return teams
    print(
        "[aviso] config/teams.yaml indisponivel; a usar "
        + ", ".join(f"{key}:{value}" for key, value in FALLBACK_TEAMS.items())
    )
    return dict(FALLBACK_TEAMS)


def build_matches_url(date_from: Any, date_to: Any) -> str:
    """URL de um pedido /v4/matches para um intervalo de datas."""
    params = {
        "competitions": ",".join(COMPETITIONS),
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
    }
    return f"{FDORG_BASE}/matches?{urlencode(params)}"


def plan_windows(
    days_back: int = 10, days_forward: int = 7, today: Any = None
) -> list[tuple[Any, Any]]:
    """Divide a janela em blocos de <= 10 dias (limite do free tier).

    A janela por omissao (10 para tras + 7 para a frente) da dois blocos, logo
    dois pedidos espacados pelo RateLimiter.
    """
    reference = today or datetime.now(timezone.utc).date()
    if isinstance(reference, datetime):
        reference = reference.date()
    start = reference - timedelta(days=max(int(days_back), 0))
    end = reference + timedelta(days=max(int(days_forward), 0))
    step = timedelta(days=max(MAX_WINDOW_DAYS - 1, 0))
    windows: list[tuple[Any, Any]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + step, end)
        windows.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return windows


def fetch_matches(
    token: str,
    days_back: int = 10,
    days_forward: int = 7,
    limiter: RateLimiter | None = None,
    today: Any = None,
) -> list[dict]:
    """Pedidos /matches da janela (1 por bloco), com dedupe por id."""
    limiter = limiter or RateLimiter()
    fetched: dict[Any, dict] = {}
    for date_from, date_to in plan_windows(days_back, days_forward, today):
        limiter.wait()
        data = http_get_json(
            build_matches_url(date_from, date_to),
            {"X-Auth-Token": token},
        )
        if not isinstance(data, Mapping):
            raise ProviderError(
                "resposta inesperada de /matches (esperado objeto JSON)"
            )
        for index, match in enumerate(data.get("matches") or []):
            if not isinstance(match, Mapping):
                continue
            key = _as_int(match.get("id"))
            if key is None:
                key = f"{date_from}:{index}"
            fetched.setdefault(key, dict(match))
    return list(fetched.values())


# --------------------------------------------------------------------------
# Comandos
# --------------------------------------------------------------------------


def run_sync(
    db: Any,
    token: str,
    days_back: int = 10,
    days_forward: int = 7,
    limiter: RateLimiter | None = None,
    today: Any = None,
) -> dict:
    """Caminho unico de sincronizacao partilhado por `sync` e `check`."""
    teams = load_teams_or_fallback()
    payload = fetch_matches(
        token,
        days_back=days_back,
        days_forward=days_forward,
        limiter=limiter,
        today=today,
    )
    normalised = [normalise_match(raw) for raw in payload]
    matches = filter_matches(normalised, list(teams))

    conn = connect(db)
    try:
        return sync_fixtures(conn, matches)
    finally:
        conn.close()


def cmd_sync(args: argparse.Namespace) -> int:
    token = (os.environ.get(PROVIDER_ENV["fdorg"]) or "").strip()
    if not token:
        print(
            "[erro] FOOTBALL_DATA_TOKEN ausente: copie .env.example para .env "
            "e preencha a chave"
        )
        return 2

    try:
        stats = run_sync(
            args.db,
            token,
            days_back=args.days_back,
            days_forward=args.days_forward,
        )
    except ProviderError as exc:
        print(f"[erro] sync: {exc}")
        return 1

    print(
        f"[ok] sync: {stats['total']} jogos "
        f"({stats['finished']} finalizados, {stats['scheduled']} agendados, "
        f"{stats['changed']} alterados)"
    )
    for change in stats["changes"]:
        print(
            f"[aviso] placar {change['fixture_id']} {change['label']}: "
            f"{_score_text(change['before'])} -> {_score_text(change['after'])}"
        )
    return 0


def cmd_due(args: argparse.Namespace) -> int:
    chat = str(args.chat).strip() if args.chat else DRY_RUN_CHAT
    teams = load_teams_or_fallback()
    conn = connect(args.db)
    try:
        messages = due_messages(conn, chat, args.limit, teams)
    finally:
        conn.close()

    for index, message in enumerate(messages):
        if index:
            print()
        print(message)
    print(f"[ok] {len(messages)} mensagens devidas")
    return 0


def cmd_mark_sent(args: argparse.Namespace) -> int:
    chat = str(args.chat).strip()
    if not chat:
        print("[erro] mark-sent: --chat vazio")
        return 2
    conn = connect(args.db)
    try:
        known = conn.execute(
            "SELECT 1 FROM matches WHERE fixture_id = ?", (int(args.fixture_id),)
        ).fetchone()
        inserted = mark_sent(
            conn, args.fixture_id, chat, message_id=args.message_id
        )
    finally:
        conn.close()

    if known is None:
        print(f"[aviso] mark-sent: fixture {args.fixture_id} nao existe em matches")
    suffix = "" if inserted else " (ja marcado)"
    print(f"[ok] mark-sent: {args.fixture_id} -> {chat}{suffix}")
    return 0


def cmd_deliver(args: argparse.Namespace) -> int:
    if not args.dry_run:
        token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not token:
            print(
                "[erro] TELEGRAM_BOT_TOKEN ausente: copie .env.example para .env "
                "e preencha o token do bot"
            )
            return 2

    chats = load_chats(args.chats)
    teams = load_teams_or_fallback()
    conn = connect(args.db)
    try:
        stats = deliver(
            conn,
            chats,
            teams,
            limit=args.limit,
            only=args.only,
            dry_run=args.dry_run,
        )
    finally:
        conn.close()

    total = stats["delivered"] + stats["failed"] + stats["pending"]
    if not total:
        print("[ok] deliver: 0 pendentes")
    elif args.dry_run:
        print(f"[ok] deliver: {stats['pending']} pendentes (dry-run, nada enviado)")
    else:
        print(
            f"[ok] deliver: {stats['delivered']} entregues, "
            f"{stats['failed']} falhas"
        )

    if stats["failed"] and not stats["delivered"]:
        return 1
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Gate local -> sync de 3 dias -> entrega (o comando do cron)."""
    now = getattr(args, "now", None)
    margins = parse_margin_overrides(getattr(args, "margin_min", None))
    grace = int(getattr(args, "grace_min", DEFAULT_GRACE_MIN))

    gate_conn = connect_readonly(args.db)
    try:
        if gate_conn is None:
            pending: list[dict] = []
            future = 0
        else:
            pending = checkable_matches(
                gate_conn, now=now, margins=margins, grace=grace
            )
            future = count_future_matches(gate_conn, now=now)
    except sqlite3.Error as exc:
        print(f"[erro] check: base de dados ilegivel ({exc})")
        return 1
    finally:
        if gate_conn is not None:
            gate_conn.close()

    if not pending:
        print(f"[ok] check: nada na janela ({future} jogos futuros no banco)")
        return 0

    token = (os.environ.get(PROVIDER_ENV["fdorg"]) or "").strip()
    if not token:
        print(
            "[erro] FOOTBALL_DATA_TOKEN ausente: copie .env.example para .env "
            "e preencha a chave"
        )
        return 2

    if not args.dry_run:
        telegram_token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not telegram_token:
            print(
                "[erro] TELEGRAM_BOT_TOKEN ausente: copie .env.example para .env "
                "e preencha o token do bot"
            )
            return 2

    try:
        stats = run_sync(
            args.db,
            token,
            days_back=CHECK_SYNC_DAYS_BACK,
            days_forward=CHECK_SYNC_DAYS_FORWARD,
        )
    except ProviderError as exc:
        print(f"[erro] check: {exc}")
        return 1

    chats = load_chats(args.chats)
    teams = load_teams_or_fallback()
    conn = connect(args.db)
    try:
        delivery = deliver(
            conn,
            chats,
            teams,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    finally:
        conn.close()

    print(
        f"[ok] check: {delivery['delivered']} entregues, "
        f"{delivery['failed']} falhas, {stats['changed']} jogos atualizados"
    )
    if delivery["failed"] and not delivery["delivered"]:
        return 1
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.py",
        description=(
            "Fase 1+2: sincroniza jogos de Gremio/Corinthians em SQLite e "
            "lista os finalizados ainda nao entregues."
        ),
        epilog=(
            "Exemplos:\n"
            "  python app.py sync --days-back 10 --days-forward 7\n"
            "  python app.py due --limit 5\n"
            "  python app.py due --chat 123456789\n"
            "  python app.py mark-sent 12345 --chat 123456789 --message-id 42\n"
            "  python app.py deliver --only privado --limit 1\n"
            "  python app.py deliver --dry-run\n"
            "  python app.py check --dry-run\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync = subparsers.add_parser("sync", help="um pedido /matches + upsert")
    sync.add_argument(
        "--days-back", type=int, default=10, help="dias para tras (default: 10)"
    )
    sync.add_argument(
        "--days-forward", type=int, default=7, help="dias para a frente (default: 7)"
    )
    sync.add_argument(
        "--db", default=DEFAULT_DB, help=f"base de dados (default: {DEFAULT_DB})"
    )
    sync.set_defaults(handler=cmd_sync)

    due = subparsers.add_parser("due", help="jogos finalizados por entregar")
    due.add_argument(
        "--chat",
        default=None,
        help="destino Telegram; por omissao usa 'dry-run' (nao grava estado)",
    )
    due.add_argument("--limit", type=int, default=20, help="maximo (default: 20)")
    due.add_argument(
        "--db", default=DEFAULT_DB, help=f"base de dados (default: {DEFAULT_DB})"
    )
    due.set_defaults(handler=cmd_due)

    mark = subparsers.add_parser("mark-sent", help="marca um jogo como entregue")
    mark.add_argument("fixture_id", type=int, help="id do jogo (matches.fixture_id)")
    mark.add_argument("--chat", required=True, help="destino Telegram")
    mark.add_argument(
        "--message-id", type=int, default=None, dest="message_id",
        help="id da mensagem devolvida pelo Telegram",
    )
    mark.add_argument(
        "--db", default=DEFAULT_DB, help=f"base de dados (default: {DEFAULT_DB})"
    )
    mark.set_defaults(handler=cmd_mark_sent)

    deliver_cmd = subparsers.add_parser(
        "deliver", help="envia os jogos devidos aos chats de config/chats.yaml"
    )
    deliver_cmd.add_argument(
        "--only", default=None, help="apenas o chat com esta key (ex: privado)"
    )
    deliver_cmd.add_argument(
        "--limit", type=int, default=20, help="maximo por chat (default: 20)"
    )
    deliver_cmd.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="imprime o que seria enviado, sem rede e sem gravar em sent",
    )
    deliver_cmd.add_argument(
        "--chats", default=DEFAULT_CHATS,
        help=f"ficheiro de destinos (default: {DEFAULT_CHATS})",
    )
    deliver_cmd.add_argument(
        "--db", default=DEFAULT_DB, help=f"base de dados (default: {DEFAULT_DB})"
    )
    deliver_cmd.set_defaults(handler=cmd_deliver)

    check = subparsers.add_parser(
        "check",
        help="gate local + sync de 3 dias + entrega (comando do cron)",
    )
    check.add_argument(
        "--limit", type=int, default=20, help="maximo por chat (default: 20)"
    )
    check.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="faz o sync mas imprime a entrega sem rede e sem gravar em sent",
    )
    check.add_argument(
        "--margin-min", action="append", default=None, dest="margin_min",
        metavar="CODE=MIN",
        help="margem em minutos por competicao (repetivel; default: BSA=120, CLI=165)",
    )
    check.add_argument(
        "--grace-min", type=int, default=DEFAULT_GRACE_MIN, dest="grace_min",
        help=f"minutos extra depois da margem (default: {DEFAULT_GRACE_MIN})",
    )
    check.add_argument(
        "--chats", default=DEFAULT_CHATS,
        help=f"ficheiro de destinos (default: {DEFAULT_CHATS})",
    )
    check.add_argument(
        "--db", default=DEFAULT_DB, help=f"base de dados (default: {DEFAULT_DB})"
    )
    check.set_defaults(handler=cmd_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    configure_output()
    parser = build_parser()
    args = parser.parse_args(argv)

    load_env(ENV_PATH)

    try:
        return int(args.handler(args))
    except ValueError as exc:
        print(f"[erro] {exc}")
        return 2
    except ProviderError as exc:
        print(f"[erro] {exc}")
        return 1
    except TelegramError as exc:
        print(f"[erro] {exc}")
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("[aviso] interrompido pelo utilizador")
        sys.exit(130)
