"""Relational schema (03_Database_Design), portable between SQLite (dev) and PostgreSQL."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, TenantOwned
from .ids import utcnow, uuid7

ID = String(36)


def pk() -> Mapped[str]:
    return mapped_column(ID, primary_key=True, default=uuid7)


def tenant_fk() -> Mapped[str]:
    return mapped_column(ID, ForeignKey("tenants.id"), nullable=False, index=True)


def ts(nullable: bool = False) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=nullable, default=None if nullable else utcnow)


ITEM_STATES = (
    "RECEIVED", "IMPORTED", "NORMALIZED", "ENRICHED", "VALIDATED", "AWAITING_REVIEW", "APPROVED",
    "QUEUED", "PUSHING", "RETRY_WAIT", "PUSHED", "SYNCED", "FAILED", "DEAD_LETTER", "SKIPPED",
    "CANCELLED", "ROLLED_BACK", "DELISTED",
)
JOB_STATES = (
    "draft", "previewing", "awaiting_approval", "running", "paused", "completed",
    "partially_completed", "failed", "cancelled", "rolling_back", "rolled_back",
)


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN (" + ",".join(f"'{v}'" for v in values) + ")"


# ---------------------------------------------------------------- identity & tenancy

class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    region: Mapped[str] = mapped_column(String(8), default="eu")
    plan: Mapped[str] = mapped_column(String(32), default="free")
    status: Mapped[str] = mapped_column(String(16), default="active")
    frozen: Mapped[bool] = mapped_column(Boolean, default=False)  # SEC-IR-07 emergency freeze
    created_at: Mapped[datetime] = ts()
    __table_args__ = (CheckConstraint(_in("status", ("active", "suspended", "deleting", "deleted"))),)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    password_hash: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(String(200))
    totp_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_last_counter: Mapped[int | None] = mapped_column(BigInteger)  # single-use codes (SEC-IAM-07)
    verify_token_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    verify_expires_at: Mapped[datetime | None] = ts(nullable=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = ts(nullable=True)
    created_at: Mapped[datetime] = ts()
    last_login_at: Mapped[datetime | None] = ts(nullable=True)
    disabled_at: Mapped[datetime | None] = ts(nullable=True)


class Membership(Base, TenantOwned):
    __tablename__ = "memberships"
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ID, ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = ts()


class Invitation(Base, TenantOwned):
    __tablename__ = "invitations"
    id: Mapped[str] = pk()
    tenant_id: Mapped[str] = tenant_fk()
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    invited_by: Mapped[str] = mapped_column(ID, ForeignKey("users.id"))
    expires_at: Mapped[datetime] = ts()
    used_at: Mapped[datetime | None] = ts(nullable=True)


class OrgPolicy(Base, TenantOwned):
    __tablename__ = "org_policies"
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.id"), primary_key=True)
    mfa_required: Mapped[bool] = mapped_column(Boolean, default=False)
    session_idle_minutes: Mapped[int] = mapped_column(Integer, default=30)
    external_ai_allowed: Mapped[bool] = mapped_column(Boolean, default=False)  # PRIV-03
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    ai_mode: Mapped[str] = mapped_column(String(16), default="assisted")  # manual|assisted|automatic
    ai_field_allowlist: Mapped[list[str]] = mapped_column(
        JSON, default=lambda: ["title", "description_html", "attributes", "category", "seo"]
    )
    auto_approve_threshold: Mapped[float] = mapped_column(Float, default=0.9)
    bulk_four_eyes_threshold: Mapped[int] = mapped_column(Integer, default=50)
    bulk_four_eyes_percent: Mapped[int] = mapped_column(Integer, default=10)


class UserSession(Base):
    __tablename__ = "sessions"
    id_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    user_id: Mapped[str] = mapped_column(ID, ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str | None] = mapped_column(ID, ForeignKey("tenants.id"))
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    mfa_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = ts()
    last_seen_at: Mapped[datetime] = ts()
    expires_at: Mapped[datetime] = ts()
    step_up_at: Mapped[datetime | None] = ts(nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    revoked_at: Mapped[datetime | None] = ts(nullable=True)


class RecoveryCode(Base):
    __tablename__ = "recovery_codes"
    id: Mapped[str] = pk()
    user_id: Mapped[str] = mapped_column(ID, ForeignKey("users.id"), index=True)
    code_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    used_at: Mapped[datetime | None] = ts(nullable=True)


class ApiKey(Base, TenantOwned):
    __tablename__ = "api_keys"
    id: Mapped[str] = pk()
    tenant_id: Mapped[str] = tenant_fk()
    name: Mapped[str] = mapped_column(String(120), default="")
    prefix: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    secret_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(ID, ForeignKey("users.id"))
    created_at: Mapped[datetime] = ts()
    expires_at: Mapped[datetime] = ts()
    last_used_at: Mapped[datetime | None] = ts(nullable=True)
    revoked_at: Mapped[datetime | None] = ts(nullable=True)


# ---------------------------------------------------------------- connections & vault

class Channel(Base, TenantOwned):
    __tablename__ = "channels"
    id: Mapped[str] = pk()
    tenant_id: Mapped[str] = tenant_fk()
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="destination")
    base_url: Mapped[str | None] = mapped_column(String(500))
    credential_id: Mapped[str | None] = mapped_column(ID)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    health: Mapped[str] = mapped_column(String(24), default="unknown")
    paused_reason: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[str | None] = mapped_column(ID)
    created_at: Mapped[datetime] = ts()
    __table_args__ = (
        CheckConstraint(_in("role", ("source", "destination", "both"))),
        CheckConstraint(_in("health", ("ok", "degraded", "reauth_required", "paused", "unknown"))),
    )


class Credential(Base, TenantOwned):
    """Ciphertext only. Plaintext exists solely inside the broker, for the life of a lease."""

    __tablename__ = "credentials"
    id: Mapped[str] = pk()
    tenant_id: Mapped[str] = tenant_fk()
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    wrapped_data_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, default=1)
    hint: Mapped[str] = mapped_column(String(16), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime | None] = ts(nullable=True)
    rotate_due_at: Mapped[datetime | None] = ts(nullable=True)
    created_by: Mapped[str] = mapped_column(ID)
    created_at: Mapped[datetime] = ts()
    last_used_at: Mapped[datetime | None] = ts(nullable=True)
    revoked_at: Mapped[datetime | None] = ts(nullable=True)
    shredded_at: Mapped[datetime | None] = ts(nullable=True)
    __table_args__ = (
        CheckConstraint(_in("kind", (
            "oauth_grant", "api_key_pair", "access_token", "app_password", "service_account", "sftp_key",
        ))),
    )


class CredentialUsage(Base, TenantOwned):
    __tablename__ = "credential_usage"
    id: Mapped[str] = pk()
    tenant_id: Mapped[str] = tenant_fk()
    credential_id: Mapped[str] = mapped_column(ID, index=True)
    task_id: Mapped[str] = mapped_column(ID)
    handler_id: Mapped[str] = mapped_column(String(120))
    operation: Mapped[str] = mapped_column(String(16))
    result: Mapped[str] = mapped_column(String(32))
    at: Mapped[datetime] = ts()


# ---------------------------------------------------------------- data hub

class Catalog(Base, TenantOwned):
    __tablename__ = "catalogs"
    id: Mapped[str] = pk()
    tenant_id: Mapped[str] = tenant_fk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    head_version: Mapped[int] = mapped_column(Integer, default=0)
    default_currency: Mapped[str] = mapped_column(String(3), default="USD")
    default_locale: Mapped[str] = mapped_column(String(16), default="en")
    created_at: Mapped[datetime] = ts()


class CatalogVersion(Base, TenantOwned):
    __tablename__ = "catalog_versions"
    catalog_id: Mapped[str] = mapped_column(ID, ForeignKey("catalogs.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = tenant_fk()
    parent: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    diff: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # per-product before/after
    created_by: Mapped[str | None] = mapped_column(ID)
    created_at: Mapped[datetime] = ts()


class Upload(Base, TenantOwned):
    __tablename__ = "uploads"
    id: Mapped[str] = pk()
    tenant_id: Mapped[str] = tenant_fk()
    catalog_id: Mapped[str] = mapped_column(ID, ForeignKey("catalogs.id"))
    created_by: Mapped[str] = mapped_column(ID)
    state: Mapped[str] = mapped_column(String(16), default="open")
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = ts()


class UploadFile(Base, TenantOwned):
    __tablename__ = "upload_files"
    id: Mapped[str] = pk()
    tenant_id: Mapped[str] = tenant_fk()
    upload_id: Mapped[str] = mapped_column(ID, ForeignKey("uploads.id"), index=True)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)  # sanitized metadata only
    object_key: Mapped[str] = mapped_column(String(400), nullable=False)  # server-generated
    bucket: Mapped[str] = mapped_column(String(16), default="quarantine")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    detected_type: Mapped[str | None] = mapped_column(String(32))
    bytes_received: Mapped[int] = mapped_column(BigInteger, default=0)  # durable offset -> resumable
    state: Mapped[str] = mapped_column(String(16), default="receiving")
    parse_meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    mapping: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    import_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class ScanResult(Base, TenantOwned):
    __tablename__ = "scan_results"
    id: Mapped[str] = pk()
    tenant_id: Mapped[str] = tenant_fk()
    file_id: Mapped[str] = mapped_column(ID, ForeignKey("upload_files.id"), index=True)
    engine: Mapped[str] = mapped_column(String(64))
    verdict: Mapped[str] = mapped_column(String(24))
    detail: Mapped[str | None] = mapped_column(Text)
    scanned_at: Mapped[datetime] = ts()


class MappingTemplate(Base, TenantOwned):
    __tablename__ = "mapping_templates"
    id: Mapped[str] = pk()
    tenant_id: Mapped[str] = tenant_fk()
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    mapping: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = ts()
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)


# ---------------------------------------------------------------- catalog (UPM)

class Product(Base, TenantOwned):
    __tablename__ = "products"
    id: Mapped[str] = pk()
    tenant_id: Mapped[str] = tenant_fk()
    catalog_id: Mapped[str] = mapped_column(ID, ForeignKey("catalogs.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)  # optimistic concurrency
    external_key: Mapped[str] = mapped_column(String(300), nullable=False)  # stable upsert identity
    source_ref: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    title: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    description_html: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    brand: Mapped[str | None] = mapped_column(String(200))
    product_type: Mapped[str | None] = mapped_column(String(200))
    source_category: Mapped[list[str]] = mapped_column(JSON, default=list)
    mapped_taxonomy_id: Mapped[str | None] = mapped_column(String(64))
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    handle: Mapped[str | None] = mapped_column(String(300))
    seo: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    attributes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    media: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    variants: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    platform_extras: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    original: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # FR-16: never overwritten
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = ts()
    updated_at: Mapped[datetime] = ts()
    __table_args__ = (
        UniqueConstraint("tenant_id", "catalog_id", "external_key"),
        CheckConstraint(_in("status", ("draft", "active", "archived"))),
        Index("products_tenant_catalog", "tenant_id", "catalog_id", "status"),
    )


class ChannelOverride(Base, TenantOwned):
    __tablename__ = "channel_overrides"
    tenant_id: Mapped[str] = tenant_fk()
    product_id: Mapped[str] = mapped_column(ID, ForeignKey("products.id", ondelete="CASCADE"), primary_key=True)
    channel_id: Mapped[str] = mapped_column(ID, ForeignKey("channels.id"), primary_key=True)
    field_path: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    author: Mapped[str | None] = mapped_column(ID)
    created_at: Mapped[datetime] = ts()


class ChannelListing(Base, TenantOwned):
    __tablename__ = "channel_listings"
    tenant_id: Mapped[str] = tenant_fk()
    product_id: Mapped[str] = mapped_column(ID, ForeignKey("products.id"), primary_key=True)
    channel_id: Mapped[str] = mapped_column(ID, ForeignKey("channels.id"), primary_key=True)
    destination_external_id: Mapped[str | None] = mapped_column(String(300))
    state: Mapped[str] = mapped_column(String(16), default="draft")
    last_published_hash: Mapped[str | None] = mapped_column(String(64))
    last_published_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    last_synced_at: Mapped[datetime | None] = ts(nullable=True)
    __table_args__ = (
        CheckConstraint(_in("state", ("draft", "pending", "live", "rejected", "delisted", "error"))),
    )


class ProductSnapshot(Base, TenantOwned):
    __tablename__ = "product_snapshots"
    id: Mapped[str] = pk()
    tenant_id: Mapped[str] = tenant_fk()
    product_id: Mapped[str] = mapped_column(ID, index=True)
    channel_id: Mapped[str | None] = mapped_column(ID)  # NULL = master record
    job_id: Mapped[str | None] = mapped_column(ID, index=True)
    existed: Mapped[bool] = mapped_column(Boolean, default=True)  # False -> job created the object
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = ts()


# ---------------------------------------------------------------- execution

class Job(Base, TenantOwned):
    __tablename__ = "jobs"
    id: Mapped[str] = pk()
    tenant_id: Mapped[str] = tenant_fk()
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="draft")
    catalog_id: Mapped[str | None] = mapped_column(ID)
    channel_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    preview: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    started_by: Mapped[str | None] = mapped_column(ID)
    approved_by: Mapped[str | None] = mapped_column(ID)
    second_approver: Mapped[str | None] = mapped_column(ID)
    needs_second_approver: Mapped[bool] = mapped_column(Boolean, default=False)
    parent_job_id: Mapped[str | None] = mapped_column(ID)
    created_at: Mapped[datetime] = ts()
    finished_at: Mapped[datetime | None] = ts(nullable=True)
    __table_args__ = (
        CheckConstraint(_in("kind", ("import", "migration", "publish", "sync", "rollback", "export"))),
        CheckConstraint(_in("state", JOB_STATES)),
    )


class JobItem(Base, TenantOwned):
    __tablename__ = "job_items"
    id: Mapped[str] = pk()
    tenant_id: Mapped[str] = tenant_fk()
    job_id: Mapped[str] = mapped_column(ID, ForeignKey("jobs.id"), nullable=False)
    product_id: Mapped[str | None] = mapped_column(ID)
    channel_id: Mapped[str | None] = mapped_column(ID)
    operation: Mapped[str] = mapped_column(String(16), default="upsert")  # upsert|delist|restore|delete
    state: Mapped[str] = mapped_column(String(20), default="VALIDATED")
    risk: Mapped[str] = mapped_column(String(8), default="low")
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # channel-shaped product at approval
    payload_hash: Mapped[str | None] = mapped_column(String(64))
    diff: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    issues: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    not_before: Mapped[datetime] = ts()
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_until: Mapped[datetime | None] = ts(nullable=True)
    lease_fencing_token: Mapped[int | None] = mapped_column(BigInteger)  # HA-06
    started_at: Mapped[datetime | None] = ts(nullable=True)  # set when a handler begins the external call
    crash_count: Mapped[int] = mapped_column(Integer, default=0)
    error_fingerprint: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    destination_ref: Mapped[str | None] = mapped_column(String(300))
    updated_at: Mapped[datetime] = ts()
    __table_args__ = (
        CheckConstraint(_in("state", ITEM_STATES)),
        CheckConstraint(_in("risk", ("low", "medium", "high"))),
        UniqueConstraint("job_id", "product_id", "channel_id"),
        Index("job_items_job_state", "tenant_id", "job_id", "state"),
        Index("job_items_claim", "state", "not_before"),
    )


class FencingCounter(Base):
    """Monotonic fencing-token source that works on SQLite and PostgreSQL alike."""

    __tablename__ = "fencing_counter"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, default=0)


class IdMapping(Base, TenantOwned):
    __tablename__ = "id_mappings"
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.id"), primary_key=True)
    channel_id: Mapped[str] = mapped_column(ID, primary_key=True)
    source_key: Mapped[str] = mapped_column(String(300), primary_key=True)
    destination_external_id: Mapped[str] = mapped_column(String(300), nullable=False)
    first_job_id: Mapped[str] = mapped_column(ID)
    created_at: Mapped[datetime] = ts()


class IdempotencyKey(Base, TenantOwned):
    __tablename__ = "idempotency_keys"
    tenant_id: Mapped[str] = mapped_column(ID, ForeignKey("tenants.id"), primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    request_hash: Mapped[str | None] = mapped_column(String(64))
    status_code: Mapped[int | None] = mapped_column(Integer)
    response: Mapped[Any] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = ts()
    expires_at: Mapped[datetime] = ts()


class BreakerState(Base, TenantOwned):
    """Circuit breaker per destination channel (HA-14); durable so every handler sees it."""

    __tablename__ = "breaker_states"
    tenant_id: Mapped[str] = tenant_fk()
    channel_id: Mapped[str] = mapped_column(ID, primary_key=True)
    state: Mapped[str] = mapped_column(String(12), default="closed")  # closed|open|half_open
    recent: Mapped[list[int]] = mapped_column(JSON, default=list)  # 1 = error, 0 = ok
    opened_at: Mapped[datetime | None] = ts(nullable=True)
    tokens: Mapped[float] = mapped_column(Float, default=0.0)  # token bucket (REL-05)
    rate_per_sec: Mapped[float] = mapped_column(Float, default=0.0)
    refilled_at: Mapped[datetime | None] = ts(nullable=True)
    blocked_until: Mapped[datetime | None] = ts(nullable=True)  # Retry-After from the platform


class OutboxEvent(Base, TenantOwned):
    __tablename__ = "outbox_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = tenant_fk()
    topic: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = ts()
    published_at: Mapped[datetime | None] = ts(nullable=True)


# ---------------------------------------------------------------- AI & review

class AIProposal(Base, TenantOwned):
    __tablename__ = "ai_proposals"
    id: Mapped[str] = pk()
    tenant_id: Mapped[str] = tenant_fk()
    product_id: Mapped[str] = mapped_column(ID, ForeignKey("products.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(ID, index=True)
    task: Mapped[str] = mapped_column(String(32), nullable=False)
    field_path: Mapped[str] = mapped_column(String(200), nullable=False)
    original_value: Mapped[Any] = mapped_column(JSON, nullable=True)
    proposed_value: Mapped[Any] = mapped_column(JSON, nullable=True)
    rationale: Mapped[str | None] = mapped_column(String(400))
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    signals: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    risk: Mapped[str] = mapped_column(String(8), nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    validator_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    product_version: Mapped[int] = mapped_column(Integer, default=1)  # stale-proposal detection
    created_at: Mapped[datetime] = ts()
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1"),
        CheckConstraint(_in("status", (
            "pending", "auto_approved", "approved", "rejected", "edited", "superseded",
        ))),
    )


class ReviewDecision(Base, TenantOwned):
    __tablename__ = "review_decisions"
    id: Mapped[str] = pk()
    tenant_id: Mapped[str] = tenant_fk()
    proposal_id: Mapped[str] = mapped_column(ID, ForeignKey("ai_proposals.id"), index=True)
    decided_by: Mapped[str | None] = mapped_column(ID)  # NULL = policy auto-approval
    decision: Mapped[str] = mapped_column(String(8), nullable=False)
    final_value: Mapped[Any] = mapped_column(JSON, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(400))
    decided_at: Mapped[datetime] = ts()


# ---------------------------------------------------------------- audit, usage, webhooks

class AuditEvent(Base, TenantOwned):
    __tablename__ = "audit_events"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = tenant_fk()
    at: Mapped[datetime] = ts()
    actor_type: Mapped[str] = mapped_column(String(12), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    resource: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    request_id: Mapped[str | None] = mapped_column(String(64))
    source_ip: Mapped[str | None] = mapped_column(String(64))
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)


class AuditAnchor(Base, TenantOwned):
    __tablename__ = "audit_anchors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = tenant_fk()
    up_to_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    chain_head: Mapped[str] = mapped_column(String(64), nullable=False)
    sealed_at: Mapped[datetime] = ts()


class UsageLedger(Base, TenantOwned):
    __tablename__ = "usage_ledger"
    id: Mapped[str] = pk()
    tenant_id: Mapped[str] = tenant_fk()
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    ref: Mapped[str | None] = mapped_column(String(120))
    at: Mapped[datetime] = ts()


class WebhookEndpoint(Base, TenantOwned):
    __tablename__ = "webhook_endpoints"
    id: Mapped[str] = pk()
    tenant_id: Mapped[str] = tenant_fk()
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    events: Mapped[list[str]] = mapped_column(JSON, default=list)
    secret_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    disabled_at: Mapped[datetime | None] = ts(nullable=True)
    created_at: Mapped[datetime] = ts()


class WebhookDelivery(Base, TenantOwned):
    __tablename__ = "webhook_deliveries"
    id: Mapped[str] = pk()
    tenant_id: Mapped[str] = tenant_fk()
    endpoint_id: Mapped[str] = mapped_column(ID, ForeignKey("webhook_endpoints.id"), index=True)
    event: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    response_code: Mapped[int | None] = mapped_column(Integer)
    next_attempt_at: Mapped[datetime] = ts()
    created_at: Mapped[datetime] = ts()
