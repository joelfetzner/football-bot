"""Testes da Fase 4 - cliente Telegram e config de chats, sem rede."""

from __future__ import annotations

import pytest

import telegram as tg

TOKEN = "123456:TEST-TOKEN-NAO-REAL"

VALID_CHATS = """
chats:
  - key: canal
    chat_id: "-1001234567890"
    kind: channel
    content: results
    silent: false
  - key: privado
    chat_id: "123456789"
    kind: private
    content: results
    silent: true
"""


def write_chats(tmp_path, text: str = VALID_CHATS):
    path = tmp_path / "chats.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def ok(message_id: int = 1) -> dict:
    return {"ok": True, "result": {"message_id": message_id}}


class Recorder:
    """Transporte falso: devolve respostas na ordem e guarda os payloads."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method, payload):
        self.calls.append((method, dict(payload)))
        if not self.replies:
            raise AssertionError("transporte chamado mais vezes do que o previsto")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


# --------------------------------------------------------------------------
# load_chats
# --------------------------------------------------------------------------


def test_load_chats_reads_keys_ids_and_flags(tmp_path):
    chats = tg.load_chats(write_chats(tmp_path))
    assert [chat["key"] for chat in chats] == ["canal", "privado"]
    assert chats[0]["chat_id"] == "-1001234567890"
    assert chats[0]["kind"] == "channel"
    assert chats[0]["silent"] is False
    assert chats[1]["chat_id"] == "123456789"
    assert chats[1]["kind"] == "private"
    assert chats[1]["silent"] is True
    assert chats[1]["content"] == "results"


def test_load_chats_missing_file_points_to_example(tmp_path):
    missing = tmp_path / "nao-existe.yaml"
    with pytest.raises(tg.ChatConfigError) as info:
        tg.load_chats(missing)
    message = str(info.value)
    assert "chats.example.yaml" in message
    assert "nao existe" in message


def test_load_chats_rejects_invalid_kind(tmp_path):
    path = write_chats(
        tmp_path,
        text=(
            "chats:\n"
            '  - key: x\n    chat_id: "123"\n    kind: supergroup\n'
        ),
    )
    with pytest.raises(tg.ChatConfigError) as info:
        tg.load_chats(path)
    assert "kind invalido" in str(info.value)


def test_load_chats_rejects_placeholder_chat_id(tmp_path):
    path = write_chats(
        tmp_path,
        text=(
            "chats:\n"
            '  - key: privado\n    chat_id: "0000000000"\n    kind: private\n'
        ),
    )
    with pytest.raises(tg.ChatConfigError) as info:
        tg.load_chats(path)
    assert "placeholder" in str(info.value)


def test_load_chats_rejects_empty_chat_id(tmp_path):
    path = write_chats(
        tmp_path,
        text='chats:\n  - key: x\n    chat_id: ""\n    kind: private\n',
    )
    with pytest.raises(tg.ChatConfigError) as info:
        tg.load_chats(path)
    assert "chat_id" in str(info.value)


def test_load_chats_rejects_duplicate_keys(tmp_path):
    path = write_chats(
        tmp_path,
        text=(
            "chats:\n"
            '  - key: x\n    chat_id: "1"\n    kind: private\n'
            '  - key: x\n    chat_id: "2"\n    kind: private\n'
        ),
    )
    with pytest.raises(tg.ChatConfigError):
        tg.load_chats(path)


# --------------------------------------------------------------------------
# send_message / payload
# --------------------------------------------------------------------------


def test_payload_shape_and_string_chat_id():
    transport = Recorder([ok(10)])
    message_id = tg.send_message(
        -1001234567890, "ola", token=TOKEN, transport=transport
    )
    assert message_id == 10
    method, payload = transport.calls[0]
    assert method == "sendMessage"
    assert payload["chat_id"] == "-1001234567890"
    assert isinstance(payload["chat_id"], str)
    assert payload["text"] == "ola"
    assert payload["disable_web_page_preview"] is True
    assert payload["disable_notification"] is False


def test_payload_disable_notification_follows_silent():
    transport = Recorder([ok(11)])
    tg.send_message("42", "ola", silent=True, token=TOKEN, transport=transport)
    assert transport.calls[0][1]["disable_notification"] is True


def test_429_retries_once_after_retry_after():
    sleeps: list[float] = []
    transport = Recorder(
        [
            {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after 2",
                "parameters": {"retry_after": 2},
            },
            ok(77),
        ]
    )
    message_id = tg.send_message(
        "42", "ola", token=TOKEN, transport=transport, sleeper=sleeps.append
    )
    assert message_id == 77
    assert sleeps == [2.0]
    assert len(transport.calls) == 2


def test_second_429_raises():
    sleeps: list[float] = []
    transport = Recorder(
        [
            {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 3},
            },
            {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 9},
            },
        ]
    )
    with pytest.raises(tg.TelegramError) as info:
        tg.send_message(
            "42", "ola", token=TOKEN, transport=transport, sleeper=sleeps.append
        )
    assert info.value.error_code == 429
    assert sleeps == [3.0]  # so uma espera: o segundo 429 sobe
    assert len(transport.calls) == 2


def test_403_raises_with_code_and_no_token_leak():
    transport = Recorder(
        [
            {
                "ok": False,
                "error_code": 403,
                "description": "Forbidden: bot is not a member of the channel chat",
            }
        ]
    )
    with pytest.raises(tg.TelegramError) as info:
        tg.send_message("42", "ola", token=TOKEN, transport=transport)
    assert info.value.error_code == 403
    assert "Forbidden" in info.value.description
    assert TOKEN not in str(info.value)
    assert TOKEN not in info.value.description


def test_error_from_transport_with_token_is_redacted():
    transport = Recorder([tg.TelegramError(f"falhou para {TOKEN}")])
    with pytest.raises(tg.TelegramError) as info:
        tg.send_message("42", "ola", token=TOKEN, transport=transport)
    assert TOKEN not in str(info.value)
    assert "***" in str(info.value)


def test_network_error_becomes_rede_error():
    transport = Recorder([OSError("connection reset")])
    with pytest.raises(tg.TelegramError) as info:
        tg.send_message("42", "ola", token=TOKEN, transport=transport)
    assert str(info.value).startswith("rede:")


def test_missing_token_raises_before_network(monkeypatch):
    monkeypatch.delenv(tg.TOKEN_ENV, raising=False)
    with pytest.raises(tg.TelegramError) as info:
        tg.send_message("42", "ola", transport=Recorder([]))
    assert tg.TOKEN_ENV in str(info.value)


def test_missing_message_id_is_an_error():
    transport = Recorder([{"ok": True, "result": {}}])
    with pytest.raises(tg.TelegramError):
        tg.send_message("42", "ola", token=TOKEN, transport=transport)
