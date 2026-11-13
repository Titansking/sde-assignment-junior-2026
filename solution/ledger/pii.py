"""PII protection at rest.

* Phones are indexed with a keyed HMAC-SHA256 ("blind index") so events can be
  joined to leads by equality without storing the number. A plain SHA-256 would
  be trivially brute-forced: there are only ~4e9 Indian mobile numbers.
* All PII fields of a lead (name, phone) are stored together in one
  Fernet-encrypted (AES-128-CBC + HMAC) JSON blob, recoverable only with the key.
* Only the last 4 phone digits are kept in clear, because that is exactly what
  the masked report column shows.

The master key comes from ``LEDGER_PII_KEY`` (url-safe base64, 32 bytes) or, for
local runs, from a ``<db>.key`` file created with 0600 permissions. In production
it would come from a KMS / secret manager instead.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

KEY_ENV = "LEDGER_PII_KEY"


class PiiVault:
    def __init__(self, master_key: bytes):
        if len(master_key) < 32:
            raise ValueError("PII master key must be at least 32 bytes")
        self._index_key = hmac.new(master_key, b"ledger/phone-index/v1", hashlib.sha256).digest()
        enc_key = hmac.new(master_key, b"ledger/pii-encryption/v1", hashlib.sha256).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(enc_key))
        # Lets us detect "same DB opened with a different key" (which would silently
        # break event->lead linking) without storing anything derived from PII.
        self.fingerprint = hmac.new(master_key, b"ledger/key-fingerprint/v1", hashlib.sha256).hexdigest()

    def phone_index(self, canonical_phone: str) -> str:
        return hmac.new(self._index_key, canonical_phone.encode(), hashlib.sha256).hexdigest()

    def encrypt(self, values: dict[str, str | None]) -> bytes:
        return self._fernet.encrypt(json.dumps(values, sort_keys=True).encode())

    def decrypt(self, token: bytes) -> dict[str, str | None]:
        return json.loads(self._fernet.decrypt(token))


def load_master_key(db_path: Path) -> bytes:
    env = os.environ.get(KEY_ENV)
    if env:
        return base64.urlsafe_b64decode(env.encode())
    key_path = Path(f"{db_path}.key")
    if key_path.exists():
        return base64.urlsafe_b64decode(key_path.read_bytes().strip())
    key = secrets.token_bytes(32)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(base64.urlsafe_b64encode(key))
    return key
