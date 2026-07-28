from collections.abc import Iterator

from sqlalchemy.orm import Session

from backend.database.session import SessionLocal


def get_db() -> Iterator[Session]:
    """Request-scoped session on the least-privilege engine.

    FastAPI caches this per request, which matters more here than it looks: the
    org context for row-level security is stashed on this session's connection
    (see session.set_request_org_context), so the auth dependency and the
    endpoint must be handed the same one.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
