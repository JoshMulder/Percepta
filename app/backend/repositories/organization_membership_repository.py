import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.database.models.organization_membership import OrganizationMembership


class OrganizationMembershipRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(
        self, *, user_id: uuid.UUID, organization_id: uuid.UUID
    ) -> OrganizationMembership | None:
        return self.db.execute(
            select(OrganizationMembership).where(
                OrganizationMembership.user_id == user_id,
                OrganizationMembership.organization_id == organization_id,
            )
        ).scalar_one_or_none()

    def roles(self, *, user_id: uuid.UUID, organization_id: uuid.UUID) -> list[str]:
        membership = self.get(user_id=user_id, organization_id=organization_id)
        return list(membership.roles or []) if membership else []
