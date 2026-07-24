"""TOTP second factor (§7.1).

A thin wrapper over ``pyotp`` so the algorithm lives in one place and the auth
service never imports the library directly. Secrets are base32 (RFC 4648), the
format every authenticator app expects; they are stored **encrypted at rest**
via ``core.crypto``'s ``EncryptedString`` (§10.7) — the column type does that
transparently, so nothing here handles ciphertext.

``verify_totp`` accepts a small window either side of the current 30-second
step (``mfa_totp_valid_window``): phone clocks drift, and a rejected valid code
sends the person to support rather than into the app. The window is deliberately
narrow — each extra step widens the guess space for an online attacker, which
is why the MFA-verify endpoint carries its own rate limit and the pending token
expires in minutes.
"""

import pyotp

from app.core.config import Settings


def generate_totp_secret() -> str:
    """A fresh base32 TOTP seed (160 bits, pyotp's default)."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, *, account_name: str, issuer: str) -> str:
    """The ``otpauth://`` URI an authenticator app scans.

    Returned as a string, not a rendered QR image: the client draws the QR
    code, which keeps an image-rendering dependency (and the secret itself)
    out of the API response pipeline.
    """
    return pyotp.TOTP(secret).provisioning_uri(name=account_name, issuer_name=issuer)


def verify_totp(secret: str, code: str, settings: Settings) -> bool:
    """Constant-time-compared TOTP verification (``pyotp`` uses ``hmac.compare_digest``)."""
    cleaned = code.strip().replace(" ", "")
    if not cleaned.isdigit():
        return False
    return bool(pyotp.TOTP(secret).verify(cleaned, valid_window=settings.mfa_totp_valid_window))


__all__ = ["generate_totp_secret", "provisioning_uri", "verify_totp"]
