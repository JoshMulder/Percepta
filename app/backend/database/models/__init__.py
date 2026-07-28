"""Model registry.

Every model must be imported here so Base.metadata is complete before Alembic
autogenerates a migration - a model that is only imported by the module that
uses it is invisible to autogenerate, which then cheerfully writes a migration
dropping its table.
"""

from backend.database.models.audit_log import AuditLog
from backend.database.models.auth_session import AuthSession
from backend.database.models.device import Device, DeviceKind
from backend.database.models.enums import UserRole
from backend.database.models.ground_station import GroundStation
from backend.database.models.organization import Organization
from backend.database.models.organization_membership import OrganizationMembership
from backend.database.models.station_grant import StationGrant
from backend.database.models.user import User

__all__ = [
    "AuditLog",
    "AuthSession",
    "Device",
    "DeviceKind",
    "GroundStation",
    "Organization",
    "OrganizationMembership",
    "StationGrant",
    "User",
    "UserRole",
]
