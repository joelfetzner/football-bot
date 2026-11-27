#!/usr/bin/env python3
"""football-bot / Fase 1 - render da mensagem de um jogo (texto simples).

Funcao pura: sem I/O, sem rede, sem base de dados. Recebe um jogo ja
normalizado (o mesmo formato da tabela `matches`) e devolve o texto a enviar.

O `parse_mode` do Telegram fica para a Fase 3: por isso nada de HTML nem
Markdown. Tudo em ASCII para a consola (acentos dobrados via NFD).

Layout:
    Brasileirao Serie A - Rodada 37
    Corinthians 1 x 1 Atletico Mineiro
    25/11 21:00 (Luxembourg)

    Estadio: Neo Quimica Arena

Exemplos:
    >>> render_match({"competition_code": "BSA", "round": "37", ...})
    'Brasileirao Serie A - Rodada 37\\n...'
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Europe/Luxembourg")
TIME_LABEL = "Luxembourg"

# Nomes curtos e estaveis para as duas competicoes da Fase 1.
COMPETITION_LABELS = {
    "BSA": "Brasileirao Serie A",
    "CLI": "Copa Libertadores",
}

# `stage` regular = jornada de liga; tudo o resto (grupos, elimininas) e fase.
REGULAR_STAGES = frozenset({"", "REGULAR_SEASON"})

STAGE_LABELS = {
    "GROUP_STAGE": "Fase de grupos",
    "LEAGUE_STAGE": "Fase de liga",
    "PRELIMINARY_ROUND": "Ronda preliminar",
    "PLAY_OFF_ROUND": "Play-off",
    "FIRST_QUALIFYING_ROUND": "1a pre-eliminatoria",
    "SECOND_QUALIFYING_ROUND": "2a pre-eliminatoria",
    "THIRD_QUALIFYING_ROUND": "3a pre-eliminatoria",
    "ROUND_OF_16": "Oitavas de final",
    "QUARTER_FINALS": "Quartos de final",
    "SEMI_FINALS": "Meias-finais",
    "FINAL": "Final",
}

UNKNOWN_TEAM = "?"


def ascii_text(value: Any) -> str:
    """Remove acentos, colapsa espacos e garante saida ASCII."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("\u00a0", " ").replace("\u2013", "-").replace("\u2014", "-")
    text = " ".join(text.split())
    return text.encode("ascii", "ignore").decode("ascii")


def _to_local(value: Any) -> datetime | None:
    """Converte utc_date (ISO 8601) para Europe/Luxembourg."""
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
    try:
        return parsed.astimezone(LOCAL_TZ)
    except (OverflowError, ValueError):
        return None


def _team_name(team_id: Any, fallback: Any, teams: Mapping[int, str]) -> str:
    """`display` da config para os nossos clubes; nome da API para o resto."""
    try:
        key = int(team_id) if team_id is not None else None
    except (TypeError, ValueError):
        key = None
    if key is not None and key in teams:
        return ascii_text(teams[key]) or UNKNOWN_TEAM
    return ascii_text(fallback) or UNKNOWN_TEAM


def _goal(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "-"


def _pretty_stage(stage: str) -> str:
    known = STAGE_LABELS.get(stage)
    if known:
        return known
    return ascii_text(stage.replace("_", " ").title())


def _header(match: Mapping) -> str:
    code = str(match.get("competition_code") or "").strip().upper()
    competition = COMPETITION_LABELS.get(code) or ascii_text(
        match.get("competition") or ""
    )
    if not competition:
        competition = code or "Jogo"

    stage = str(match.get("stage") or "").strip().upper()
    if stage and stage not in REGULAR_STAGES:
        return f"{competition} - {_pretty_stage(stage)}"

    round_ = str(match.get("round") or "").strip()
    if round_:
        return f"{competition} - Rodada {ascii_text(round_)}"
    return competition


def render_match(
    match: Mapping, teams: Mapping[int, str] | None = None
) -> str:
    """Devolve o texto de um jogo. `teams` = {fdorg_id: display}."""
    displays = dict(teams or {})
    home = _team_name(match.get("home_id"), match.get("home_name"), displays)
    away = _team_name(match.get("away_id"), match.get("away_name"), displays)
    score = (
        f"{home} {_goal(match.get('goals_home'))} x "
        f"{_goal(match.get('goals_away'))} {away}"
    )

    lines = [_header(match), score]

    penalties_home = match.get("penalties_home")
    penalties_away = match.get("penalties_away")
    if penalties_home is not None and penalties_away is not None:
        lines.append(f"(pen. {_goal(penalties_home)}-{_goal(penalties_away)})")

    moment = _to_local(match.get("utc_date"))
    if moment is not None:
        lines.append(f"{moment.strftime('%d/%m %H:%M')} ({TIME_LABEL})")
    elif match.get("utc_date"):
        lines.append(ascii_text(match.get("utc_date")))

    venue = ascii_text(match.get("venue") or "")
    if venue:
        lines.extend(["", f"Estadio: {venue}"])

    return "\n".join(lines)
