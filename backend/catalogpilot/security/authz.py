"""Central authorization policy (SEC-AUZ-01): deny by default, RBAC plus attribute checks."""

from __future__ import annotations

from dataclasses import dataclass, field

# Permission catalog. Roles are sets of permissions (SRS 3.2).
P_CATALOG_READ = "catalog:read"
P_CATALOG_WRITE = "catalog:write"
P_UPLOAD = "uploads:write"
P_JOBS_READ = "jobs:read"
P_JOBS_WRITE = "jobs:write"
P_REVIEW = "reviews:write"
P_PUBLISH = "publish:write"
P_CHANNELS_READ = "channels:read"
P_CHANNELS_ADMIN = "channels:admin"
P_AUDIT_READ = "audit:read"
P_MEMBERS = "members:admin"
P_POLICY = "policy:admin"
P_COST = "catalog:cost"  # property-level: cost price (SEC-AUZ-05)
P_ORG_OWNER = "org:owner"
P_WEBHOOKS = "webhooks:admin"

_READ = {P_CATALOG_READ, P_JOBS_READ, P_CHANNELS_READ}

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "owner": {
        P_CATALOG_READ, P_CATALOG_WRITE, P_UPLOAD, P_JOBS_READ, P_JOBS_WRITE, P_REVIEW, P_PUBLISH,
        P_CHANNELS_READ, P_CHANNELS_ADMIN, P_AUDIT_READ, P_MEMBERS, P_POLICY, P_COST, P_ORG_OWNER,
        P_WEBHOOKS,
    },
    "admin": {
        P_CATALOG_READ, P_CATALOG_WRITE, P_UPLOAD, P_JOBS_READ, P_JOBS_WRITE, P_REVIEW, P_PUBLISH,
        P_CHANNELS_READ, P_CHANNELS_ADMIN, P_AUDIT_READ, P_MEMBERS, P_POLICY, P_COST, P_WEBHOOKS,
    },
    "editor": _READ | {P_CATALOG_WRITE, P_UPLOAD, P_REVIEW},
    "publisher": _READ | {P_JOBS_WRITE, P_PUBLISH},
    "reviewer": _READ | {P_REVIEW},
    "viewer": set(_READ),
    "auditor": {P_AUDIT_READ, P_CHANNELS_READ},
}

# Roles ordered by privilege: nobody may grant a role above their own (SEC-AUZ-10).
ROLE_RANK = {"viewer": 0, "auditor": 0, "reviewer": 1, "editor": 2, "publisher": 2, "admin": 3, "owner": 4}

# Never grantable to API keys: interactive user with step-up only (CRED-02).
API_KEY_FORBIDDEN = {P_CHANNELS_ADMIN, P_MEMBERS, P_POLICY, P_ORG_OWNER}


class Forbidden(Exception):
    def __init__(self, code: str = "forbidden", detail: str = "You do not have permission to do this."):
        self.code = code
        self.detail = detail


@dataclass
class Principal:
    tenant_id: str
    user_id: str
    role: str
    kind: str = "user"  # user | api_key
    actor_id: str = ""
    scopes: set[str] | None = None  # API keys: effective = role permissions ∩ scopes
    step_up_fresh: bool = False
    permissions: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        perms = set(ROLE_PERMISSIONS.get(self.role, set()))
        if self.kind == "api_key":
            perms = (perms & (self.scopes or set())) - API_KEY_FORBIDDEN
        self.permissions = perms
        self.actor_id = self.actor_id or self.user_id

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    def require(self, permission: str) -> None:
        if not self.can(permission):
            raise Forbidden()

    def require_step_up(self) -> None:
        if self.kind != "user":
            raise Forbidden(detail="This action requires an interactive user session.")
        if not self.step_up_fresh:
            raise Forbidden("step_up_required", "Please re-authenticate to continue.")


def can_grant(granter_role: str, target_role: str) -> bool:
    return target_role in ROLE_RANK and ROLE_RANK[target_role] <= ROLE_RANK.get(granter_role, -1)
