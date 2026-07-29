"""Check the password reset flow end to end, against the database.

Auth code, so it gets a verifier like the rest of it. Runs against a throwaway
user it creates and removes, and never touches SMTP - `send` is the one part
that needs a mail server, and it is exercised by using the console with Mailpit
running rather than from here.

    docker exec percepta-app python -m backend.scripts.verify_password_reset
"""

import logging
import sys
import uuid
from datetime import UTC, datetime, timedelta

from backend.auth.password import PasswordError, hash_password, verify_password
from backend.core.crypto import lookup_hash
from backend.database.models.password_reset_token import PasswordResetToken
from backend.database.models.user import User
from backend.database.session import PrivilegedSessionLocal
from backend.services import password_reset

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("verify")

U = uuid.UUID("dddddddd-0000-0000-0000-00000000d001")
_failures: list[str] = []


def check(label: str, passed: bool, detail: str = "") -> None:
    log.info("  %s  %s%s", "PASS" if passed else "FAIL", label,
             f" - {detail}" if detail and not passed else "")
    if not passed:
        _failures.append(label)


def seed(db) -> User:
    user = User(
        id=U,
        email=f"{U.hex[:8]}@reset.test",
        display_name="reset target",
        password_hash=hash_password("original-password-x"),
        is_active=True,
    )
    db.add(user)
    db.commit()
    return user


def cleanup(db) -> None:
    db.query(PasswordResetToken).filter(PasswordResetToken.user_id == U).delete()
    db.query(User).filter(User.id == U).delete()
    db.commit()


def main() -> int:
    with PrivilegedSessionLocal() as db:
        cleanup(db)
        user = seed(db)
        try:
            log.info("")
            log.info("1. Issue")
            token, plaintext = password_reset.issue(db, user=user, requested_by=None)
            db.commit()
            check("the plaintext is not stored", token.token_hash != plaintext)
            check("stored as a lookup hash", token.token_hash == lookup_hash(plaintext))
            check("unused when issued", token.used_at is None)

            log.info("")
            log.info("2. A second issue supersedes the first")
            _, second = password_reset.issue(db, user=user, requested_by=None)
            db.commit()
            db.refresh(token)
            check("the first link is spent", token.used_at is not None)
            try:
                password_reset.redeem(
                    db, token_value=plaintext, new_password="a-good-password-1"
                )
                check("the superseded link is refused", False)
            except password_reset.ResetError:
                check("the superseded link is refused", True)
            db.rollback()

            log.info("")
            log.info("3. A rejected password does not spend the link")
            try:
                password_reset.redeem(db, token_value=second, new_password="short")
                check("a weak password is refused", False)
            except PasswordError:
                check("a weak password is refused", True)
            except password_reset.ResetError:
                check("a weak password is refused", False, "raised ResetError")
            db.rollback()
            password_reset.redeem(
                db, token_value=second, new_password="a-good-password-1"
            )
            db.commit()
            check("the link still worked afterwards", True)

            db.refresh(user)
            check(
                "the password actually changed",
                verify_password("a-good-password-1", user.password_hash),
            )
            check(
                "the old password no longer works",
                not verify_password("original-password-x", user.password_hash),
            )

            log.info("")
            log.info("4. Single use")
            try:
                password_reset.redeem(
                    db, token_value=second, new_password="another-good-one-2"
                )
                check("a redeemed link cannot be reused", False)
            except password_reset.ResetError:
                check("a redeemed link cannot be reused", True)
            db.rollback()

            log.info("")
            log.info("5. Expiry")
            expired, expired_plain = password_reset.issue(
                db, user=user, requested_by=None, ttl=timedelta(seconds=-1)
            )
            db.commit()
            check("it really is in the past", expired.expires_at < datetime.now(UTC))
            try:
                password_reset.redeem(
                    db, token_value=expired_plain, new_password="another-good-one-2"
                )
                check("an expired link is refused", False)
            except password_reset.ResetError:
                check("an expired link is refused", True)
            db.rollback()

            log.info("")
            log.info("6. Unknown tokens are indistinguishable from spent ones")
            messages = set()
            for value in ("not-a-real-token", second, expired_plain):
                try:
                    password_reset.redeem(
                        db, token_value=value, new_password="another-good-one-2"
                    )
                except password_reset.ResetError as exc:
                    messages.add(str(exc))
                db.rollback()
            check("one message for every failure", len(messages) == 1, str(messages))
        finally:
            cleanup(db)

    log.info("")
    if _failures:
        log.error("FAILED: %d check(s): %s", len(_failures), _failures)
        return 1
    log.info("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
