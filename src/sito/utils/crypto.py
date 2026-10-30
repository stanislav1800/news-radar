"""Шифрование секретов подключений (сессии Telegram, токены) ключом из SITO_SECRET_KEY.

Как fernet_key в Airflow: без ключа дамп или бэкап базы секретов не раскрывает.
Fernet — симметричное шифрование с проверкой целостности: подменённые или испорченные
данные не расшифруются, а не превратятся в мусор.
"""

import base64
import json
from collections.abc import Mapping
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# SITO_SECRET_KEY подписывает и cookie сессий. Для шифрования из него выводится отдельный ключ:
# так одно назначение ключа не влияет на другое
_KEY_INFO = b"sito: connector secrets v1"


class SecretsError(Exception):
    """Секреты не расшифровываются: SITO_SECRET_KEY не тот, которым их шифровали, или данные испорчены."""


class SecretBox:
    def __init__(self, secret_key: str) -> None:
        derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_KEY_INFO).derive(secret_key.encode())
        self._fernet = Fernet(base64.urlsafe_b64encode(derived))

    def encrypt(self, secrets: Mapping[str, Any]) -> bytes:
        return self._fernet.encrypt(json.dumps(dict(secrets), ensure_ascii=False).encode())

    def decrypt(self, token: bytes) -> dict[str, Any]:
        try:
            data = json.loads(self._fernet.decrypt(token))
        except InvalidToken as exc:
            raise SecretsError(
                "Секреты подключения не расшифровываются: SITO_SECRET_KEY не тот, которым их сохраняли. "
                "Верните прежний ключ или введите учётные данные подключения заново."
            ) from exc
        if not isinstance(data, dict):
            raise SecretsError("Секреты подключения испорчены")
        return data
