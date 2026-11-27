"""Testes da Fase 1+2 (sem rede, DB temporaria em tmp_path)."""

from __future__ import annotations

import pytest

import app
import render

TEAMS = {1767: "Gremio", 1779: "Corinthians"}


def make_match(fixture_id: int = 1001, **overrides) -> dict:
    base = {
        "fixture_id": fixture_id,
        "provider": "fdorg",
        "competition_code": "BSA",
        "competition": "Campeonato Brasileiro Serie A",
        "stage": "REGULAR_SEASON",
        "round": "37",
        "utc_date": "2026-11-25T21:00:00Z",
        "status": "SCHEDULED",
        "home_id": 1779,
        "home_name": "SC Corinthians Paulista",
        "away_id": 1767,
        "away_name": "Gremio FBPA",
        "goals_home": None,
        "goals_away": None,
        "penalties_home": None,
        "penalties_away": None,
        "winner": None,
        "venue": None,
    }
    base.update(overrides)
    return base


@pytest.fixture()
def conn(tmp_path):
    connection = app.connect(tmp_path / "football.db")
    try:
        yield connection
    finally:
        connection.close()


def _count(conn, table: str = "matches") -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _seed_finished_and_scheduled(conn) -> None:
    app.sync_fixtures(
        conn,
        [
            make_match(
                1,
                status="FINISHED",
                utc_date="2026-11-20T21:00:00Z",
                goals_home=1,
                goals_away=0,
            ),
            make_match(
                2,
                status="FINISHED",
                utc_date="2026-11-21T21:00:00Z",
                goals_home=2,
                goals_away=2,
            ),
            make_match(
                3,
                status="SCHEDULED",
                utc_date="2026-11-29T21:30:00Z",
                home_id=1767,
                home_name="Gremio FBPA",
                away_id=1779,
                away_name="SC Corinthians Paulista",
            ),
            # finalizado mas sem placar (a API omitiu) -> nunca entra em `due`
            make_match(4, status="FINISHED", utc_date="2026-11-22T21:00:00Z"),
        ],
    )


# --------------------------------------------------------------------------
# Esquema / migracao
# --------------------------------------------------------------------------


def test_init_db_is_idempotent(conn):
    app.init_db(conn)
    app.init_db(conn)
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"matches", "sent"} <= tables


def test_connect_twice_on_same_file_is_idempotent(tmp_path):
    path = tmp_path / "reuse.db"
    first = app.connect(path)
    first.close()
    second = app.connect(path)
    try:
        assert _count(second) == 0
    finally:
        second.close()


def test_connect_creates_missing_parent_dir(tmp_path):
    path = tmp_path / "nested" / "dir" / "football.db"
    connection = app.connect(path)
    connection.close()
    assert path.exists()


# --------------------------------------------------------------------------
# Upsert / sync
# --------------------------------------------------------------------------


def test_upsert_inserts_then_updates_status_and_score(conn):
    app.sync_fixtures(conn, [make_match()])
    row = conn.execute("SELECT * FROM matches WHERE fixture_id = 1001").fetchone()
    assert row["status"] == "SCHEDULED"
    assert row["goals_home"] is None and row["goals_away"] is None

    stats = app.sync_fixtures(
        conn,
        [make_match(status="FINISHED", goals_home=2, goals_away=1)],
    )
    assert stats["inserted"] == 0
    assert stats["updated"] == 1
    assert len(stats["changes"]) == 1

    row = conn.execute("SELECT * FROM matches WHERE fixture_id = 1001").fetchone()
    assert (row["status"], row["goals_home"], row["goals_away"]) == ("FINISHED", 2, 1)


def test_resync_with_null_score_keeps_known_score(conn):
    app.sync_fixtures(
        conn, [make_match(status="FINISHED", goals_home=2, goals_away=1)]
    )
    app.sync_fixtures(conn, [make_match(status="FINISHED")])

    row = conn.execute("SELECT * FROM matches WHERE fixture_id = 1001").fetchone()
    assert (row["goals_home"], row["goals_away"]) == (2, 1)


def test_sync_twice_produces_no_duplicate_rows(conn):
    payload = [make_match(status="FINISHED", goals_home=0, goals_away=0)]
    app.sync_fixtures(conn, payload)
    second = app.sync_fixtures(conn, payload)

    assert _count(conn) == 1
    assert second["inserted"] == 0
    assert second["changes"] == []


def test_sync_ignores_match_without_utc_date(conn):
    app.sync_fixtures(conn, [make_match(utc_date="")])
    assert _count(conn) == 0


def test_filter_keeps_only_configured_teams():
    matches = [
        make_match(1, home_id=1779, away_id=1767),
        make_match(2, home_id=1767, away_id=99),
        make_match(3, home_id=500, away_id=501),
    ]
    kept = app.filter_matches(matches, [1767, 1779])
    assert [match["fixture_id"] for match in kept] == [1, 2]


def test_normalise_match_never_coerces_missing_score_to_zero():
    raw = {
        "id": 77,
        "utcDate": "2026-11-29T21:30:00Z",
        "status": "SCHEDULED",
        "competition": {"code": "BSA", "name": "Campeonato Brasileiro Serie A"},
        "homeTeam": {"id": 1767, "name": "Gremio FBPA"},
        "awayTeam": {"id": 1779, "name": "SC Corinthians Paulista"},
        "score": {"fullTime": {"home": None, "away": None}},
        "matchday": 37,
    }
    match = app.normalise_match(raw)
    assert match["goals_home"] is None
    assert match["goals_away"] is None
    assert match["competition_code"] == "BSA"
    assert match["round"] == "37"


def test_normalise_match_keeps_zero_zero(conn):
    raw = {
        "id": 78,
        "utcDate": "2026-11-20T21:00:00Z",
        "status": "FINISHED",
        "competition": {"code": "BSA", "name": "Campeonato Brasileiro Serie A"},
        "homeTeam": {"id": 1767, "name": "Gremio FBPA"},
        "awayTeam": {"id": 1779, "name": "SC Corinthians Paulista"},
        "score": {"fullTime": {"home": 0, "away": 0}},
        "matchday": 36,
    }
    match = app.normalise_match(raw)
    assert (match["goals_home"], match["goals_away"]) == (0, 0)

    app.sync_fixtures(conn, [match])
    row = conn.execute("SELECT * FROM matches WHERE fixture_id = 78").fetchone()
    assert (row["goals_home"], row["goals_away"]) == (0, 0)


# --------------------------------------------------------------------------
# Janela de pedidos (limite do free tier: 10 dias por pedido)
# --------------------------------------------------------------------------


def test_plan_windows_splits_default_window_into_two():
    from datetime import date

    windows = app.plan_windows(10, 7, today=date(2026, 11, 26))
    assert windows == [
        (date(2026, 11, 16), date(2026, 11, 25)),
        (date(2026, 11, 26), date(2026, 12, 3)),
    ]


def test_plan_windows_never_exceeds_ten_days():
    from datetime import date

    for start, end in app.plan_windows(30, 30, today=date(2026, 11, 26)):
        assert (end - start).days <= app.MAX_WINDOW_DAYS - 1


def test_build_matches_url_carries_both_competitions():
    from datetime import date

    url = app.build_matches_url(date(2026, 11, 16), date(2026, 11, 25))
    assert "competitions=BSA%2CCLI" in url
    assert "dateFrom=2026-11-16" in url
    assert "dateTo=2026-11-25" in url


# --------------------------------------------------------------------------
# due
# --------------------------------------------------------------------------


def test_due_excludes_sent_for_same_chat(conn):
    _seed_finished_and_scheduled(conn)
    assert app.mark_sent(conn, 1, "chat-a") is True

    assert [m["fixture_id"] for m in app.due_matches(conn, "chat-a")] == [2]
    assert [m["fixture_id"] for m in app.due_matches(conn, "chat-b")] == [1, 2]


def test_due_excludes_unfinished_and_null_score(conn):
    _seed_finished_and_scheduled(conn)
    ids = [match["fixture_id"] for match in app.due_matches(conn, "chat-a", 20)]
    assert ids == [1, 2]
    assert 3 not in ids  # agendado
    assert 4 not in ids  # finalizado sem placar


def test_due_respects_limit_and_order(conn):
    _seed_finished_and_scheduled(conn)
    ids = [match["fixture_id"] for match in app.due_matches(conn, "chat-a", 1)]
    assert ids == [1]


def test_due_dry_run_does_not_write_to_sent(conn):
    _seed_finished_and_scheduled(conn)

    messages = app.due_messages(conn, app.DRY_RUN_CHAT, 20, TEAMS)
    app.due_messages(conn, app.DRY_RUN_CHAT, 20, TEAMS)

    assert len(messages) == 2
    assert _count(conn, "sent") == 0


def test_mark_sent_is_idempotent(conn):
    _seed_finished_and_scheduled(conn)

    assert app.mark_sent(conn, 1, "chat-a", message_id=10) is True
    assert app.mark_sent(conn, 1, "chat-a", message_id=11) is False

    row = conn.execute(
        "SELECT * FROM sent WHERE fixture_id = 1 AND chat_id = 'chat-a'"
    ).fetchone()
    assert row is not None
    assert row["message_id"] == 10
    assert _count(conn, "sent") == 1


def test_max_one_message_per_match_per_chat(conn):
    _seed_finished_and_scheduled(conn)
    app.mark_sent(conn, 1, "chat-a")
    app.mark_sent(conn, 1, "chat-a")
    app.mark_sent(conn, 1, "chat-b")

    assert _count(conn, "sent") == 2


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------


def test_render_league_with_matchday():
    match = make_match(
        status="FINISHED",
        goals_home=1,
        goals_away=1,
        home_name="SC Corinthians Paulista",
        away_id=500,
        away_name="Atletico Mineiro",
    )
    text = render.render_match(match, TEAMS)
    assert text == (
        "Brasileirao Serie A - Rodada 37\n"
        "Corinthians 1 x 1 Atletico Mineiro\n"
        "25/11 22:00 (Luxembourg)"
    )


def test_render_cup_stage_replaces_matchday():
    match = make_match(
        competition_code="CLI",
        competition="CONMEBOL Libertadores",
        stage="QUARTER_FINALS",
        round=None,
        utc_date="2026-09-16T00:30:00Z",
    )
    text = render.render_match(match, TEAMS)
    assert text.splitlines()[0] == "Copa Libertadores - Quartos de final"


def test_render_missing_round_keeps_only_competition():
    match = make_match(round=None)
    assert render.render_match(match, TEAMS).splitlines()[0] == "Brasileirao Serie A"


def test_render_zero_zero():
    match = make_match(goals_home=0, goals_away=0)
    assert render.render_match(match, TEAMS).splitlines()[1] == (
        "Corinthians 0 x 0 Gremio"
    )


def test_render_penalties_on_second_line():
    match = make_match(
        competition_code="CLI",
        competition="CONMEBOL Libertadores",
        stage="FINAL",
        round=None,
        goals_home=1,
        goals_away=1,
        penalties_home=4,
        penalties_away=3,
    )
    lines = render.render_match(match, TEAMS).splitlines()
    assert lines[1] == "Corinthians 1 x 1 Gremio"
    assert lines[2] == "(pen. 4-3)"


def test_render_omits_venue_line_when_unknown():
    text = render.render_match(make_match(venue=None), TEAMS)
    assert "Estadio" not in text


def test_render_includes_venue_without_accents():
    text = render.render_match(make_match(venue="Neo Qu\u00edmica Arena"), TEAMS)
    assert "Estadio: Neo Quimica Arena" in text


def test_render_luxembourg_date_conversion_late_kickoff():
    # 29/11 21:30 em UTC-3 == 30/11 00:30 UTC == 30/11 01:30 em Luxembourg
    match = make_match(utc_date="2026-11-30T00:30:00Z")
    lines = render.render_match(match, TEAMS).splitlines()
    assert lines[2] == "30/11 01:30 (Luxembourg)"


def test_render_keeps_long_team_names():
    long_name = "Sport Club Internacional de Porto Alegre"
    match = make_match(away_id=500, away_name=long_name)
    line = render.render_match(match, TEAMS).splitlines()[1]
    assert long_name in line


def test_render_output_is_ascii():
    match = make_match(competition="Campeonato Brasileiro S\u00e9rie A", venue="Arena")
    text = render.render_match(match, TEAMS)
    text.encode("ascii")


# --------------------------------------------------------------------------
# deliver (Fase 4)
# --------------------------------------------------------------------------

PRIVATE = {
    "key": "privado",
    "chat_id": "123456789",
    "kind": "private",
    "content": "results",
    "silent": False,
}
CHANNEL = {
    "key": "canal",
    "chat_id": "-1001234567890",
    "kind": "channel",
    "content": "results",
    "silent": True,
}


def _reply(message_id: int = 1) -> dict:
    return {"ok": True, "result": {"message_id": message_id}}


def _write_chats(tmp_path):
    path = tmp_path / "chats.yaml"
    path.write_text(
        "chats:\n"
        '  - key: privado\n    chat_id: "123456789"\n    kind: private\n'
        "    content: results\n    silent: false\n",
        encoding="utf-8",
    )
    return path


def test_deliver_marks_sent_only_after_success(conn, capsys):
    import telegram

    _seed_finished_and_scheduled(conn)
    calls = {"n": 0}

    def transport(method, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise telegram.TelegramError("Forbidden", error_code=403)
        return _reply(501)

    stats = app.deliver(
        conn,
        [PRIVATE],
        TEAMS,
        transport=transport,
        token="test-token",
        sleeper=lambda seconds: None,
    )

    assert stats == {"delivered": 1, "failed": 1, "pending": 0}
    rows = conn.execute(
        "SELECT fixture_id, chat_id, message_id FROM sent ORDER BY fixture_id"
    ).fetchall()
    assert [(row["fixture_id"], row["chat_id"], row["message_id"]) for row in rows] == [
        (2, PRIVATE["chat_id"], 501)
    ]
    out = capsys.readouterr().out
    assert "[erro] privado 1:" in out
    assert "[ok] privado 2 " in out
    assert "message_id=501" in out


def test_deliver_skips_matches_already_sent(conn):
    _seed_finished_and_scheduled(conn)
    app.mark_sent(conn, 1, PRIVATE["chat_id"], message_id=10)

    texts: list[str] = []

    def transport(method, payload):
        texts.append(payload["text"])
        return _reply(900 + len(texts))

    app.deliver(
        conn,
        [PRIVATE],
        TEAMS,
        transport=transport,
        token="test-token",
        sleeper=lambda seconds: None,
    )

    assert len(texts) == 1
    assert "21/11" in texts[0]
    assert _count(conn, "sent") == 2


def test_deliver_respects_limit(conn):
    _seed_finished_and_scheduled(conn)
    sleeps: list[float] = []

    app.deliver(
        conn,
        [PRIVATE],
        TEAMS,
        limit=1,
        transport=lambda method, payload: _reply(7),
        token="test-token",
        sleeper=sleeps.append,
    )

    rows = conn.execute("SELECT fixture_id FROM sent").fetchall()
    assert [row["fixture_id"] for row in rows] == [1]
    assert sleeps == []  # um unico envio nao espera


def test_deliver_continues_after_one_failing_chat(conn, capsys):
    import telegram

    _seed_finished_and_scheduled(conn)
    seen: list[str] = []

    def transport(method, payload):
        seen.append(payload["chat_id"])
        if payload["chat_id"] == CHANNEL["chat_id"]:
            assert payload["disable_notification"] is True
            raise telegram.TelegramError("Forbidden", error_code=403)
        return _reply(300 + len(seen))

    app.deliver(
        conn,
        [CHANNEL, PRIVATE],
        TEAMS,
        transport=transport,
        token="test-token",
        sleeper=lambda seconds: None,
    )

    chats = {
        row["chat_id"] for row in conn.execute("SELECT chat_id FROM sent").fetchall()
    }
    assert chats == {PRIVATE["chat_id"]}
    assert _count(conn, "sent") == 2
    out = capsys.readouterr().out
    assert "[erro] canal 1:" in out
    assert "[ok] privado 1 " in out
    assert "[ok] privado 2 " in out


def test_deliver_dry_run_sends_nothing_and_writes_nothing(conn, capsys):
    _seed_finished_and_scheduled(conn)

    def boom(method, payload):
        raise AssertionError("dry-run nao pode tocar a rede")

    stats = app.deliver(
        conn, [PRIVATE], TEAMS, dry_run=True, transport=boom, token="test-token"
    )

    assert stats == {"delivered": 0, "failed": 0, "pending": 2}
    assert _count(conn, "sent") == 0
    out = capsys.readouterr().out
    assert "(dry-run)" in out
    assert "Corinthians" in out


def test_deliver_orders_oldest_first_and_sleeps_between_same_chat(conn):
    _seed_finished_and_scheduled(conn)
    texts: list[str] = []
    sleeps: list[float] = []

    def transport(method, payload):
        texts.append(payload["text"])
        return _reply(700 + len(texts))

    app.deliver(
        conn,
        [PRIVATE],
        TEAMS,
        transport=transport,
        token="test-token",
        sleeper=sleeps.append,
    )

    assert "20/11" in texts[0]
    assert "21/11" in texts[1]
    assert sleeps == [app.CHAT_SEND_INTERVAL]


def test_deliver_unknown_only_raises(conn):
    with pytest.raises(ValueError) as info:
        app.deliver(conn, [PRIVATE], TEAMS, only="inexistente")
    assert "chat desconhecido" in str(info.value)


def test_deliver_cli_dry_run_reports_zero_pending(tmp_path, capsys):
    db_path = tmp_path / "empty.db"
    chats_path = _write_chats(tmp_path)

    code = app.main(
        [
            "deliver",
            "--dry-run",
            "--db",
            str(db_path),
            "--chats",
            str(chats_path),
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "0 pendentes" in out
    assert _count(app.connect(db_path), "sent") == 0


def test_deliver_cli_exit_one_when_nothing_delivered(tmp_path, monkeypatch, capsys):
    import telegram

    db_path = tmp_path / "football.db"
    connection = app.connect(db_path)
    try:
        _seed_finished_and_scheduled(connection)
    finally:
        connection.close()
    chats_path = _write_chats(tmp_path)

    def fail(*args, **kwargs):
        raise telegram.TelegramError("Forbidden", error_code=403)

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(app, "send_message", fail)

    code = app.main(
        ["deliver", "--db", str(db_path), "--chats", str(chats_path)]
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "[erro] privado 1:" in out
    assert "0 entregues" in out


# --------------------------------------------------------------------------
# check (Fase 5): gate local, zero rede quando nada e checkable
# --------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_match_gate_bsa_is_kickoff_plus_120_plus_15():
    match = make_match(competition_code="BSA", utc_date="2026-11-29T21:30:00Z")
    assert app.match_gate(match) == _utc(2026, 11, 29, 23, 45)


def test_match_gate_cli_is_kickoff_plus_165_plus_15():
    match = make_match(competition_code="CLI", utc_date="2026-11-29T21:30:00Z")
    assert app.match_gate(match) == _utc(2026, 11, 30, 0, 30)


def test_match_gate_unknown_competition_uses_largest_margin():
    match = make_match(competition_code="PL", utc_date="2026-11-29T21:30:00Z")
    assert app.match_gate(match) == _utc(2026, 11, 30, 0, 30)


def test_checkable_window_uses_gate_status_and_lookahead(conn):
    app.sync_fixtures(
        conn,
        [
            make_match(
                1, status="TIMED", competition_code="BSA",
                utc_date="2026-11-29T21:30:00Z",
            ),
            make_match(
                2, status="TIMED", competition_code="CLI",
                utc_date="2026-11-30T00:00:00Z",
            ),
            make_match(
                3, status="FINISHED", competition_code="BSA",
                utc_date="2026-11-29T21:00:00Z",
                goals_home=1, goals_away=0,
            ),
        ],
    )

    # 00:00 UTC de 30/11: BSA fecha as 23:45, CLI so as 03:00.
    now = _utc(2026, 11, 30, 0, 0)
    ids = [row["fixture_id"] for row in app.checkable_matches(conn, now=now)]
    assert ids == [1]


def test_checkable_ignores_kickoff_outside_window(conn):
    app.sync_fixtures(
        conn,
        [make_match(1, status="TIMED", utc_date="2026-11-20T21:30:00Z")],
    )
    assert app.checkable_matches(conn, now=_utc(2026, 11, 30, 0, 0)) == []


def test_checkable_respects_margin_override(conn):
    app.sync_fixtures(
        conn,
        [make_match(
            1, status="TIMED", competition_code="BSA",
            utc_date="2026-11-29T21:30:00Z",
        )],
    )
    now = _utc(2026, 11, 29, 23, 30)

    assert app.checkable_matches(conn, now=now) == []
    margins = app.parse_margin_overrides(["BSA=100"])
    found = app.checkable_matches(conn, now=now, margins=margins)
    assert [row["fixture_id"] for row in found] == [1]


def test_parse_margin_overrides_rejects_garbage():
    with pytest.raises(ValueError):
        app.parse_margin_overrides(["BSA"])
    with pytest.raises(ValueError):
        app.parse_margin_overrides(["BSA=muitos"])


def test_check_cli_nothing_checkable_does_no_network_and_no_db_write(
    tmp_path, monkeypatch, capsys
):
    db_path = tmp_path / "football.db"
    later = (datetime.now(timezone.utc) + timedelta(days=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    connection = app.connect(db_path)
    try:
        app.sync_fixtures(
            connection, [make_match(1, status="TIMED", utc_date=later)]
        )
    finally:
        connection.close()

    before = db_path.read_bytes()
    before_mtime = db_path.stat().st_mtime_ns

    def boom(*args, **kwargs):
        raise AssertionError("nada checkable nao pode tocar a rede")

    monkeypatch.setattr(app, "fetch_matches", boom)
    monkeypatch.setattr(app, "send_message", boom)
    monkeypatch.delenv("FOOTBALL_DATA_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    code = app.main(["check", "--db", str(db_path)])

    out = capsys.readouterr().out
    assert code == 0
    assert "nada na janela" in out
    assert "(1 jogos futuros no banco)" in out
    assert db_path.read_bytes() == before
    assert db_path.stat().st_mtime_ns == before_mtime


def test_check_cli_missing_db_persists_nothing(tmp_path, capsys):
    db_path = tmp_path / "never.db"
    code = app.main(["check", "--db", str(db_path)])

    assert code == 0
    assert "nada na janela (0 jogos futuros no banco)" in capsys.readouterr().out
    assert not db_path.exists()


def _finished_raw(fixture_id: int, utc_date: str) -> dict:
    return {
        "id": fixture_id,
        "utcDate": utc_date,
        "status": "FINISHED",
        "competition": {"code": "BSA", "name": "Campeonato Brasileiro Serie A"},
        "homeTeam": {"id": 1779, "name": "SC Corinthians Paulista"},
        "awayTeam": {"id": 1767, "name": "Gremio FBPA"},
        "score": {"fullTime": {"home": 2, "away": 0}, "winner": "HOME_TEAM"},
        "matchday": 37,
    }


def test_check_cli_syncs_three_days_then_delivers(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "football.db"
    chats_path = _write_chats(tmp_path)
    started = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    connection = app.connect(db_path)
    try:
        app.sync_fixtures(
            connection,
            [make_match(1, status="TIMED", utc_date=started)],
        )
    finally:
        connection.close()

    calls: list[tuple[int, int]] = []

    def fake_fetch(token, days_back=0, days_forward=0, **kwargs):
        calls.append((days_back, days_forward))
        return [_finished_raw(1, started)]

    monkeypatch.setattr(app, "fetch_matches", fake_fetch)
    monkeypatch.setattr(app, "send_message", lambda chat_id, text, **kw: 77)
    monkeypatch.setenv("FOOTBALL_DATA_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")

    code = app.main(
        ["check", "--db", str(db_path), "--chats", str(chats_path)]
    )

    assert code == 0
    assert calls == [(app.CHECK_SYNC_DAYS_BACK, app.CHECK_SYNC_DAYS_FORWARD)]

    connection = app.connect(db_path)
    try:
        row = connection.execute(
            "SELECT fixture_id, chat_id, message_id FROM sent"
        ).fetchone()
        status = connection.execute(
            "SELECT status FROM matches WHERE fixture_id = 1"
        ).fetchone()[0]
    finally:
        connection.close()
    assert (row["fixture_id"], row["chat_id"], row["message_id"]) == (
        1, "123456789", 77,
    )
    assert status == "FINISHED"

    out = capsys.readouterr().out
    assert "[ok] check: 1 entregues, 0 falhas, 1 jogos atualizados" in out


def test_check_cli_exit_one_when_nothing_delivered(tmp_path, monkeypatch, capsys):
    import telegram

    db_path = tmp_path / "football.db"
    chats_path = _write_chats(tmp_path)
    started = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    connection = app.connect(db_path)
    try:
        app.sync_fixtures(
            connection,
            [make_match(1, status="TIMED", utc_date=started)],
        )
    finally:
        connection.close()

    monkeypatch.setattr(
        app, "fetch_matches", lambda token, **kwargs: [_finished_raw(1, started)]
    )

    def refuse(*args, **kwargs):
        raise telegram.TelegramError("Forbidden", error_code=403)

    monkeypatch.setattr(app, "send_message", refuse)
    monkeypatch.setenv("FOOTBALL_DATA_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")

    code = app.main(
        ["check", "--db", str(db_path), "--chats", str(chats_path)]
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "[ok] check: 0 entregues, 1 falhas, 1 jogos atualizados" in out


# --------------------------------------------------------------------------
# sync sem writes quando os dados nao mudaram (Fase 5)
# --------------------------------------------------------------------------


def test_noop_sync_keeps_db_file_byte_identical(tmp_path):
    db_path = tmp_path / "football.db"
    payload = [
        make_match(
            1, status="FINISHED", goals_home=1, goals_away=0, venue="Arena"
        )
    ]
    connection = app.connect(db_path)
    try:
        app.sync_fixtures(connection, payload)
    finally:
        connection.close()

    before = db_path.read_bytes()
    before_mtime = db_path.stat().st_mtime_ns

    connection = app.connect(db_path)
    try:
        stats = app.sync_fixtures(connection, payload)
    finally:
        connection.close()

    assert stats["changed"] == 0
    assert db_path.read_bytes() == before
    assert db_path.stat().st_mtime_ns == before_mtime


def test_updated_at_only_moves_on_material_change(conn):
    app.sync_fixtures(conn, [make_match(1)], now=_utc(2026, 11, 27, 10, 0))
    first = conn.execute(
        "SELECT updated_at FROM matches WHERE fixture_id = 1"
    ).fetchone()[0]

    # round muda mas nao e material: o carimbo fica.
    app.sync_fixtures(
        conn, [make_match(1, round="38")], now=_utc(2026, 11, 27, 11, 0)
    )
    row = conn.execute("SELECT * FROM matches WHERE fixture_id = 1").fetchone()
    assert row["round"] == "38"
    assert row["updated_at"] == first

    # status + placar sao materiais: o carimbo avanca.
    app.sync_fixtures(
        conn,
        [make_match(1, round="38", status="FINISHED", goals_home=1, goals_away=0)],
        now=_utc(2026, 11, 27, 12, 0),
    )
    row = conn.execute("SELECT * FROM matches WHERE fixture_id = 1").fetchone()
    assert row["updated_at"] == "2026-11-27T12:00:00Z"
