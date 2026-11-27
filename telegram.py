#!/usr/bin/env python3
"""football-bot / Fase 4 - cliente minimo da Bot API do Telegram.

Sem dependencias novas: `urllib.request` + `json` da stdlib. O token vem de
`TELEGRAM_BOT_TOKEN` (`.env` ou ambiente) e nunca aparece em mensagens de erro,
`print` ou ficheiros.

Dois pontos de injecao mantem tudo testavel sem rede:

    transport(method, payload) -> dict   # devolve o JSON da Bot API
    sleeper(seconds)                     # por omissao `time.sleep`

`content` de cada chat esta reservado para fases seguintes; esta fase entrega
sempre o texto de `render.render_match()` (`content: results`).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_CHATS = "config/chats.yaml"
CHATS_EXAMPLE = "config/chats.example.yaml"

API_BASE = "https://api.telegram.org"
TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
DEFAULT_TIMEOUT = 30.0
RETRY_FALLBACK = 1.0

CHAT_KINDS = frozenset({"channel", "group", "private"})
PLACEHOLDER_CHAT_IDS = frozenset({"0", "0000000000", "-1000000000000"})


class ChatConfigError(ValueError):
    """config/chats.yaml ausente, invalido ou ainda com os IDs de exemplo."""


class TelegramError(Exception):
    """Falha da Bot API ou de rede, com o codigo e a descricao do Telegram."""

    def __init__(self, description: str, error_code: int | None = None) -> None:
        super().__init__(description)
        self.description = description
        self.error_code = error_code


# --------------------------------------------------------------------------
# Configuracao dos destinos
# --------------------------------------------------------------------------


def resolve_chats_path(value: Any = None) -> Path:
    """Caminhos relativos ficam sempre dentro do projeto."""
    path = Path(value) if value else Path(DEFAULT_CHATS)
    if not path.is_absolute():
        path = ROOT / path
    return path


def _as_text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on", "sim"}


def _parse_chats_minimal(text: str, path: Path) -> Mapping:
    """Leitor de reserva (lista de mapas) quando PyYAML nao esta instalado."""
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    seen_chats = False

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "chats:":
            seen_chats = True
            continue
        if stripped.startswith("- "):
            current = {}
            entries.append(current)
            stripped = stripped[2:].strip()
            if not stripped:
                continue
        if current is None or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        current[key.strip()] = value

    if not seen_chats:
        raise ChatConfigError(f"{path}: falta a chave 'chats'")
    return {"chats": entries}


def _read_chats_document(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ChatConfigError(
            f"{path} nao existe; copie {CHATS_EXAMPLE} para "
            f"{DEFAULT_CHATS} e preencha os chat_id"
        ) from None
    except OSError as exc:
        raise ChatConfigError(
            f"nao foi possivel ler {path}: {exc.strerror or exc}"
        ) from None

    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return _parse_chats_minimal(text, path)

    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        detail = " ".join(str(exc).split())
        raise ChatConfigError(f"{path}: YAML invalido ({detail})") from None


def load_chats(path: Any = None) -> list[dict[str, Any]]:
    """Devolve [{key, chat_id, kind, content, silent}] de config/chats.yaml."""
    target = resolve_chats_path(path)
    document = _read_chats_document(target)

    if not isinstance(document, Mapping) or "chats" not in document:
        raise ChatConfigError(f"{target}: falta a chave 'chats'")
    raw_chats = document.get("chats")
    if not isinstance(raw_chats, list) or not raw_chats:
        raise ChatConfigError(f"{target}: 'chats' tem de ser uma lista nao vazia")

    chats: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for position, entry in enumerate(raw_chats, start=1):
        if not isinstance(entry, Mapping):
            raise ChatConfigError(f"{target}: entrada {position} de 'chats' nao e um mapa")

        key = _as_text(entry.get("key"))
        if not key:
            raise ChatConfigError(f"{target}: entrada {position} sem 'key'")
        if key in seen_keys:
            raise ChatConfigError(f"{target}: key duplicada '{key}'")
        seen_keys.add(key)

        chat_id = _as_text(entry.get("chat_id"))
        if not chat_id:
            label = f"chat '{key}'"
            raise ChatConfigError(f"{target}: {label} sem 'chat_id' (texto nao vazio)")
        digits = chat_id.lstrip("-+").strip()
        if not digits or set(digits) == {"0"} or chat_id in PLACEHOLDER_CHAT_IDS:
            raise ChatConfigError(
                f"{target}: chat '{key}' tem chat_id placeholder ({chat_id}); "
                f"copie {CHATS_EXAMPLE} e preencha os IDs reais"
            )

        kind = _as_text(entry.get("kind")).lower()
        if kind not in CHAT_KINDS:
            allowed = ", ".join(sorted(CHAT_KINDS))
            raise ChatConfigError(
                f"{target}: chat '{key}' tem kind invalido ('{kind}'); use {allowed}"
            )

        chats.append(
            {
                "key": key,
                "chat_id": chat_id,
                "kind": kind,
                "content": _as_text(entry.get("content")) or "results",
                "silent": _as_bool(entry.get("silent"), default=False),
            }
        )
    return chats


# --------------------------------------------------------------------------
# Bot API
# --------------------------------------------------------------------------


def resolve_token(token: str | None = None) -> str:
    value = (token or os.environ.get(TOKEN_ENV) or "").strip()
    if not value:
        raise TelegramError(
            f"{TOKEN_ENV} ausente: copie .env.example para .env e preencha "
            "o token do bot"
        )
    return value


def _redact(text: str, secret: str) -> str:
    if secret and secret in text:
        return text.replace(secret, "***")
    return text


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _retry_after(data: Mapping) -> float:
    parameters = data.get("parameters")
    if isinstance(parameters, Mapping):
        seconds = _as_int(parameters.get("retry_after"))
        if seconds is not None and seconds >= 0:
            return float(seconds)
    return RETRY_FALLBACK


def build_payload(
    chat_id: Any, text: Any, silent: bool = False
) -> dict[str, Any]:
    """Corpo JSON de `sendMessage` (ids sempre como string)."""
    return {
        "chat_id": str(chat_id).strip(),
        "text": str(text),
        "disable_web_page_preview": True,
        "disable_notification": bool(silent),
    }


def http_transport(
    token: str, timeout: float = DEFAULT_TIMEOUT
) -> Callable[[str, Mapping], dict]:
    """Transporte real: POST JSON para /bot<token>/<method>."""

    def transport(method: str, payload: Mapping) -> dict:
        url = f"{API_BASE}/bot{token}/{method}"
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # A Bot API devolve o JSON de erro no corpo tambem em 4xx/5xx.
            try:
                raw = exc.read()
            except OSError:
                raw = b""
            if not raw:
                raise TelegramError(f"HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", None) or exc
            raise TelegramError(f"rede: {reason}") from None

        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise TelegramError("resposta invalida da API Telegram") from None
        if not isinstance(data, dict):
            raise TelegramError("resposta invalida da API Telegram")
        return data

    return transport


def send_message(
    chat_id: Any,
    text: Any,
    silent: bool = False,
    token: str | None = None,
    transport: Callable[[str, Mapping], dict] | None = None,
    sleeper: Callable[[float], Any] | None = None,
) -> int:
    """Envia uma mensagem e devolve o `message_id`.

    Um 429 e repetido uma unica vez apos `parameters.retry_after` segundos.
    `403`/`400` e restantes erros sobem como `TelegramError` (sem traceback).
    """
    secret = resolve_token(token)
    send = transport or http_transport(secret)
    sleep = sleeper or time.sleep
    payload = build_payload(chat_id, text, silent)

    for attempt in (0, 1):
        try:
            data = send("sendMessage", payload)
        except TelegramError as exc:
            raise TelegramError(
                _redact(str(exc), secret), error_code=exc.error_code
            ) from None
        except (OSError, TimeoutError) as exc:
            raise TelegramError(_redact(f"rede: {exc}", secret)) from None

        if not isinstance(data, Mapping):
            raise TelegramError("resposta inesperada da API Telegram")

        if data.get("ok"):
            result = data.get("result")
            message_id = _as_int(result.get("message_id")) if isinstance(
                result, Mapping
            ) else None
            if message_id is None:
                raise TelegramError("resposta sem message_id")
            return message_id

        code = _as_int(data.get("error_code")) or 0
        description = _redact(
            str(data.get("description") or "erro da API Telegram"), secret
        )
        if code == 429 and attempt == 0:
            sleep(_retry_after(data))
            continue
        raise TelegramError(description, error_code=code)

    raise TelegramError("erro da API Telegram", error_code=429)
