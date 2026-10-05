"""Encryption for applicant data (passport etc.). Key comes from DATA_KEY (Fernet, base64)."""
from __future__ import annotations

import json
import os

from cryptography.fernet import Fernet, InvalidToken

_fernet: Fernet | None = None


def enabled() -> bool:
    return bool(os.getenv("DATA_KEY"))


def _f() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(os.environ["DATA_KEY"].encode())
    return _fernet


def encrypt(data: dict) -> str:
    return _f().encrypt(json.dumps(data, ensure_ascii=False).encode()).decode()


def decrypt(token: str) -> dict:
    try:
        return json.loads(_f().decrypt(token.encode()))
    except (InvalidToken, ValueError):
        return {}


def mask(value: str, keep: int = 3) -> str:
    value = value or ""
    return value[:keep] + "•" * max(0, len(value) - keep)
