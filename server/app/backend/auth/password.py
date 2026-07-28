"""Password hashing.

bcrypt via passlib, matching DroneOps. bcrypt silently truncates at 72 bytes, so
longer inputs are rejected rather than quietly accepted - a user who sets a
100-character passphrase should not find that only the first 72 mattered.
"""

from passlib.context import CryptContext

_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_LENGTH = 12


class PasswordError(ValueError):
    pass


def hash_password(password: str) -> str:
    validate_password(password)
    return _context.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        # Still run a hash so a user with no password set takes the same time as
        # a wrong password - otherwise the response time reveals which accounts
        # exist and are unprovisioned.
        _context.dummy_verify()
        return False
    try:
        return _context.verify(password, password_hash)
    except ValueError:
        return False


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise PasswordError(
            f"Password must be at most {MAX_PASSWORD_BYTES} bytes. bcrypt "
            "ignores anything beyond that, so a longer one would give a false "
            "sense of strength."
        )
