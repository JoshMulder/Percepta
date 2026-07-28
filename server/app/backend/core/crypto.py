"""Application-layer encryption for secrets held at rest.

Ported from DroneOps. Scope differs: there is no Xero here, but the secrets that
matter are TOTP seeds (mint valid second factors, defeating MFA entirely) and,
once device enrolment lands, ground station credentials. Password hashes are
*not* in scope - bcrypt already makes those useless to a thief.

Encrypted with Fernet (AES-128-CBC + HMAC-SHA256, authenticated) via the
`EncryptedString` column type, so callers keep reading and writing plain `str`
and never see ciphertext.

Two properties worth knowing before changing anything here:

*Fernet is randomised* - encrypting the same value twice gives different
ciphertext. That is what we want (it hides equality), but it means an encrypted
column can never be looked up with `WHERE col = ?`. For a secret that is
*presented back to us* to identify a row, store `lookup_hash(value)` in a
separate indexed column and query that instead. The hash is safe at rest because
the input is CSPRNG output, far beyond brute force - unlike a password, which is
why passwords get bcrypt and these get SHA-256.

*The key lives outside the database.* Losing SECRETS_ENCRYPTION_KEY means the
encrypted columns are unrecoverable, so back it up separately from the DB -
storing it beside the dump would defeat the entire control.
"""

import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from backend.core.config import settings

logger = logging.getLogger(__name__)

# Version tag on every ciphertext. It tells decrypt_secret whether a stored
# value is encrypted or a legacy plaintext row written before this existed, and
# gives us a migration path if the scheme ever changes (enc:v2:...).
_PREFIX = "enc:v1:"


class SecretsKeyError(RuntimeError):
    """SECRETS_ENCRYPTION_KEY is set but unusable, or can't decrypt a value."""


def _load_fernet() -> Fernet | None:
    raw = (settings.secrets_encryption_key or "").strip()
    if not raw:
        return None
    try:
        return Fernet(raw.encode())
    except Exception as exc:
        raise SecretsKeyError(
            "SECRETS_ENCRYPTION_KEY is not a valid Fernet key. Generate one with:\n"
            '  python3 -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        ) from exc


_fernet: Fernet | None = _load_fernet()


def encryption_enabled() -> bool:
    return _fernet is not None


def encrypt_secret(plaintext: str | None) -> str | None:
    """Encrypt a secret for storage. Without a key configured the value is
    stored as-is, so an install that hasn't set one keeps working (loudly warned
    about at startup) rather than failing to boot.
    """
    if plaintext is None:
        return None
    if _fernet is None:
        return plaintext
    if plaintext.startswith(_PREFIX):
        # Already encrypted - don't double-wrap (e.g. a re-run data migration).
        return plaintext
    return _PREFIX + _fernet.encrypt(plaintext.encode()).decode()


def decrypt_secret(stored: str | None) -> str | None:
    """Decrypt a stored secret.

    Values without the prefix are legacy plaintext (written while no key was
    configured) and are passed through, so reads keep working during a rollout.
    A prefixed value that won't decrypt means the key changed and is a hard
    error - failing loudly beats silently handing back ciphertext.
    """
    if stored is None:
        return None
    if not stored.startswith(_PREFIX):
        return stored
    if _fernet is None:
        raise SecretsKeyError(
            "Found an encrypted value but SECRETS_ENCRYPTION_KEY is not set. "
            "Restore the key that was used to write this data."
        )
    try:
        return _fernet.decrypt(stored[len(_PREFIX) :].encode()).decode()
    except InvalidToken as exc:
        raise SecretsKeyError(
            "Could not decrypt a stored secret with the current "
            "SECRETS_ENCRYPTION_KEY. The key has changed or the value is "
            "corrupt; restore the original key."
        ) from exc


def lookup_hash(value: str) -> str:
    """Deterministic hash for equality lookups on a high-entropy secret.

    SHA-256 hex (64 chars). Only ever use this for CSPRNG-generated tokens -
    never for user-chosen input, which is guessable offline.
    """
    return hashlib.sha256(value.encode()).hexdigest()


def generate_key() -> str:
    return Fernet.generate_key().decode()


def warn_if_unencrypted() -> None:
    """Called at startup so an install without a key can't quietly believe its
    secrets are protected."""
    if encryption_enabled():
        logger.info("Secrets encryption is enabled (SECRETS_ENCRYPTION_KEY set).")
        return
    logger.warning(
        "SECRETS_ENCRYPTION_KEY is not set - TOTP secrets are stored in "
        "PLAINTEXT. Anyone with a copy of the database or a backup can read "
        "them. Generate a key with: python3 -c \"from cryptography.fernet "
        'import Fernet; print(Fernet.generate_key().decode())"'
    )


class EncryptedString(TypeDecorator):
    """Transparently encrypting column type.

    Backed by Text, not String(n): ciphertext is much longer than its input and
    varies with it, so a length cap sized for the plaintext would truncate.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt_secret(value)

    def process_result_value(self, value, dialect):
        return decrypt_secret(value)
