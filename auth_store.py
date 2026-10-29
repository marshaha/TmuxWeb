"""The console's own password, kept out of the environment so it can be changed at run time.

The password never touches the environment or the logs again: we store a pbkdf2 hash in a small
JSON file next to the other databases. When no file exists the class is inert and the caller keeps
using TMUX_WEB_PASSWORD, so an install that never opened the settings panel behaves exactly as
before. Session tokens are derived from the *hash* (not from the password) once the file exists,
which is what lets a password change invalidate the old cookie without restarting the server.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

ITERATIONS = 200_000


def hash_password(password: str, salt: bytes | None = None, iterations: int = ITERATIONS) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual.hex(), digest_hex)


class PasswordStore:
    """One password, one file. Absent file = the environment password is still in charge."""

    def __init__(self, path: str | os.PathLike | None):
        self.path = Path(path) if path else None

    # ------------------------------------------------------------------ read
    def _read(self) -> dict:
        if not self.path:
            return {}
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def encoded(self) -> str:
        """The stored hash, or '' when the environment password is still the one in use."""
        value = self._read().get("password")
        return value if isinstance(value, str) else ""

    def material(self) -> str:
        """What session tokens are derived from: the hash once set, else nothing."""
        return self.encoded()

    def verify(self, password: str) -> bool:
        encoded = self.encoded()
        return verify_password(password, encoded) if encoded else False

    # ------------------------------------------------------------------ write
    def set(self, password: str) -> str:
        if not self.path:
            raise RuntimeError("no password file configured")
        encoded = hash_password(password)
        payload = {"password": encoded}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(payload, handle)
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)
        return encoded
