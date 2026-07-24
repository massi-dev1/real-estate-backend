"""Field-level encryption for reversible secrets at rest (§10.7).

AES-256-GCM: a per-value random 96-bit nonce, AEAD-authenticated so a
tampered ciphertext fails to decrypt rather than silently returning garbage.
The key itself is never a raw passphrase — ``field_encryption_key`` is
stretched through SHA-256 once per process, same as the rest of the codebase
treats secrets (e.g. JWT signing, ``sign_value``).

Key-rotation-ready: every ciphertext is stored as ``{key_id}:{b64(nonce+tag+
ct)}``, so ``encrypt_value`` always uses the *current* key id
(``field_encryption_key_id``) while ``decrypt_value`` looks the embedded id up
in a small keyring (current key plus any retired keys from
``field_encryption_keys``). Rotating the key means: add the new key id to
config as current, keep the old id+key in the retired map so existing rows
keep decrypting, and re-save each row (a lazy re-encrypt-on-write, or a
one-off backfill) to actually move it onto the new key — this module only
provides the read/write primitive, not a migration job.
"""

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.types import String, TypeDecorator

from app.core.config import Settings, get_settings

_NONCE_LENGTH = 12


def _derive_key(secret: str) -> bytes:
    """A raw passphrase is not a valid AES-256 key; stretch it to 32 bytes."""
    return hashlib.sha256(secret.encode()).digest()


class FieldCipher:
    """Encrypt/decrypt strings for one process, built once from ``Settings``."""

    def __init__(self, settings: Settings) -> None:
        self._current_key_id = settings.field_encryption_key_id
        self._keyring: dict[str, bytes] = {
            settings.field_encryption_key_id: _derive_key(settings.field_encryption_key)
        }
        for entry in settings.field_encryption_keys.split(","):
            entry = entry.strip()
            if not entry:
                continue
            key_id, _, key_value = entry.partition("=")
            if not key_id or not key_value:
                continue
            self._keyring[key_id] = _derive_key(key_value)

    def encrypt(self, plaintext: str) -> str:
        key = self._keyring[self._current_key_id]
        nonce = os.urandom(_NONCE_LENGTH)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
        payload = base64.urlsafe_b64encode(nonce + ciphertext).decode()
        return f"{self._current_key_id}:{payload}"

    def decrypt(self, token: str) -> str:
        key_id, sep, payload = token.partition(":")
        if not sep:
            raise ValueError("Malformed ciphertext: missing key id prefix.")
        key = self._keyring.get(key_id)
        if key is None:
            raise ValueError(f"Unknown field-encryption key id: {key_id!r}")
        raw = base64.urlsafe_b64decode(payload.encode())
        nonce, ciphertext = raw[:_NONCE_LENGTH], raw[_NONCE_LENGTH:]
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
        return plaintext.decode()


_cipher: FieldCipher | None = None


def get_field_cipher(settings: Settings) -> FieldCipher:
    """Process-wide cipher, built once from settings (mirrors ``get_settings``'s
    own ``lru_cache`` — the key material shouldn't be re-derived per call)."""
    global _cipher
    if _cipher is None:
        _cipher = FieldCipher(settings)
    return _cipher


class EncryptedString(TypeDecorator[str]):
    """A ``String`` column that is transparently AES-GCM-encrypted at rest.

    Reversible (unlike the password hash / token-hash columns elsewhere in
    the codebase) — for secrets the app must read back, like an MFA TOTP seed.
    Never filterable/indexable by value: a query can't compare ciphertext to
    a plaintext literal, which is the correct trade for something this
    sensitive.

    Takes no settings at construction — columns are declared at class-body
    (import) time, long before a ``Settings`` instance exists — and instead
    resolves the process-wide cipher lazily via ``get_settings()``, the same
    ``lru_cache`` singleton every other module in ``core`` reads on demand.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: object) -> str | None:
        if value is None:
            return None
        return get_field_cipher(get_settings()).encrypt(value)

    def process_result_value(self, value: str | None, dialect: object) -> str | None:
        if value is None:
            return None
        return get_field_cipher(get_settings()).decrypt(value)
