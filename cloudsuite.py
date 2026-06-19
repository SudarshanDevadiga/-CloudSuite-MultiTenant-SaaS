"""
CloudSuite - Multi-Tenant SaaS Platform
========================================
A Python-based implementation demonstrating core multi-tenant SaaS concepts:
- Tenant management and onboarding
- Tenant-aware request handling
- Role-Based Access Control (RBAC)
- Tenant-specific configuration loading
- Billing management
- Monitoring and metrics
"""

import uuid
import hashlib
import hmac
import json
import time
import logging
from datetime import datetime, timedelta
from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Any
from functools import wraps

# ─── Logging Setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("CloudSuite")


# ─── Enums ────────────────────────────────────────────────────────────────────
class TenantPlan(Enum):
    FREE = "free"
    STARTER = "starter"
    PROFESSIONAL = "professional"
    ENTERPRISE = "enterprise"


class TenantStatus(Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    PENDING = "pending"
    CANCELLED = "cancelled"


class UserRole(Enum):
    SUPER_ADMIN = "super_admin"   # CloudSuite platform admin
    TENANT_ADMIN = "tenant_admin" # Admin of a specific tenant org
    MANAGER = "manager"
    MEMBER = "member"
    VIEWER = "viewer"


# ─── Data Models ──────────────────────────────────────────────────────────────
@dataclass
class Tenant:
    """Represents an organization (tenant) on the platform."""
    tenant_id: str
    name: str
    domain: str
    plan: TenantPlan
    status: TenantStatus
    created_at: str
    config: Dict[str, Any] = field(default_factory=dict)
    resource_limits: Dict[str, int] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class User:
    """Represents a user belonging to a tenant."""
    user_id: str
    tenant_id: str
    email: str
    role: UserRole
    password_hash: str
    created_at: str
    is_active: bool = True
    permissions: List[str] = field(default_factory=list)
    last_login: Optional[str] = None


@dataclass
class Session:
    """Represents an authenticated session (JWT-like token structure)."""
    session_id: str
    user_id: str
    tenant_id: str
    role: UserRole
    issued_at: float
    expires_at: float


@dataclass
class BillingRecord:
    """Represents a billing entry for a tenant."""
    record_id: str
    tenant_id: str
    period_start: str
    period_end: str
    plan: TenantPlan
    amount_usd: float
    status: str  # paid, pending, overdue
    line_items: List[Dict] = field(default_factory=list)


@dataclass
class MetricEvent:
    """A monitoring metric event."""
    event_id: str
    tenant_id: str
    service: str
    metric_name: str
    value: float
    timestamp: str
    tags: Dict[str, str] = field(default_factory=dict)


# ─── In-Memory Storage (simulates database layer) ─────────────────────────────
class InMemoryDatabase:
    """
    Simulated database with tenant-aware data isolation.
    In production, this maps to PostgreSQL with row-level security
    or separate schemas per tenant.
    """
    def __init__(self):
        self._tenants: Dict[str, Tenant] = {}
        self._users: Dict[str, User] = {}          # key: user_id
        self._sessions: Dict[str, Session] = {}    # key: session_id
        self._billing: Dict[str, List[BillingRecord]] = {}  # key: tenant_id
        self._configs: Dict[str, Dict] = {}        # key: tenant_id
        self._metrics: List[MetricEvent] = []
        # Tenant-scoped data tables: tenant_id -> {table -> [rows]}
        self._tenant_data: Dict[str, Dict[str, List]] = {}

    def get_tenant(self, tenant_id: str) -> Optional[Tenant]:
        return self._tenants.get(tenant_id)

    def save_tenant(self, tenant: Tenant):
        self._tenants[tenant.tenant_id] = tenant
        if tenant.tenant_id not in self._tenant_data:
            self._tenant_data[tenant.tenant_id] = {}

    def get_user(self, user_id: str) -> Optional[User]:
        return self._users.get(user_id)

    def get_user_by_email(self, tenant_id: str, email: str) -> Optional[User]:
        for user in self._users.values():
            if user.tenant_id == tenant_id and user.email == email:
                return user
        return None

    def save_user(self, user: User):
        self._users[user.user_id] = user

    def save_session(self, session: Session):
        self._sessions[session.session_id] = session

    def get_session(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    def delete_session(self, session_id: str):
        self._sessions.pop(session_id, None)

    def save_config(self, tenant_id: str, config: Dict):
        self._configs[tenant_id] = config

    def get_config(self, tenant_id: str) -> Dict:
        return self._configs.get(tenant_id, {})

    def add_billing_record(self, record: BillingRecord):
        self._billing.setdefault(record.tenant_id, []).append(record)

    def get_billing_records(self, tenant_id: str) -> List[BillingRecord]:
        return self._billing.get(tenant_id, [])

    def add_metric(self, event: MetricEvent):
        self._metrics.append(event)

    def get_metrics(self, tenant_id: str, service: str = None) -> List[MetricEvent]:
        results = [m for m in self._metrics if m.tenant_id == tenant_id]
        if service:
            results = [m for m in results if m.service == service]
        return results

    def tenant_table_insert(self, tenant_id: str, table: str, row: Dict):
        """Tenant-scoped data insert – enforces isolation."""
        self._tenant_data.setdefault(tenant_id, {}).setdefault(table, []).append(row)

    def tenant_table_query(self, tenant_id: str, table: str) -> List[Dict]:
        """Tenant-scoped data query – never leaks across tenants."""
        return self._tenant_data.get(tenant_id, {}).get(table, [])


# ─── Singleton DB Instance ────────────────────────────────────────────────────
db = InMemoryDatabase()


# ─── Caching Layer ────────────────────────────────────────────────────────────
class TenantConfigCache:
    """
    Simple TTL-based in-memory cache for tenant configurations.
    In production: Redis with tenant-prefixed keys.
    """
    def __init__(self, ttl_seconds: int = 300):
        self._cache: Dict[str, Dict] = {}
        self._expiry: Dict[str, float] = {}
        self.ttl = ttl_seconds

    def get(self, tenant_id: str) -> Optional[Dict]:
        if tenant_id in self._cache:
            if time.time() < self._expiry[tenant_id]:
                logger.debug(f"Cache HIT for tenant {tenant_id}")
                return self._cache[tenant_id]
            else:
                logger.debug(f"Cache EXPIRED for tenant {tenant_id}")
                self.invalidate(tenant_id)
        return None

    def set(self, tenant_id: str, config: Dict):
        self._cache[tenant_id] = config
        self._expiry[tenant_id] = time.time() + self.ttl
        logger.debug(f"Cache SET for tenant {tenant_id}")

    def invalidate(self, tenant_id: str):
        self._cache.pop(tenant_id, None)
        self._expiry.pop(tenant_id, None)


config_cache = TenantConfigCache(ttl_seconds=300)


# ─── Plan Resource Limits ─────────────────────────────────────────────────────
PLAN_LIMITS = {
    TenantPlan.FREE:         {"max_users": 5,    "max_storage_gb": 1,   "api_rate_limit": 100},
    TenantPlan.STARTER:      {"max_users": 25,   "max_storage_gb": 10,  "api_rate_limit": 500},
    TenantPlan.PROFESSIONAL: {"max_users": 100,  "max_storage_gb": 100, "api_rate_limit": 2000},
    TenantPlan.ENTERPRISE:   {"max_users": 9999, "max_storage_gb": 5000,"api_rate_limit": 10000},
}

PLAN_PRICING = {
    TenantPlan.FREE:         0.00,
    TenantPlan.STARTER:      29.00,
    TenantPlan.PROFESSIONAL: 99.00,
    TenantPlan.ENTERPRISE:   499.00,
}

ROLE_PERMISSIONS = {
    UserRole.SUPER_ADMIN:  ["*"],  # All permissions
    UserRole.TENANT_ADMIN: ["tenant:read","tenant:write","users:read","users:write",
                             "data:read","data:write","billing:read","config:read","config:write"],
    UserRole.MANAGER:      ["users:read","users:write","data:read","data:write","config:read"],
    UserRole.MEMBER:       ["data:read","data:write"],
    UserRole.VIEWER:       ["data:read"],
}


# ─── Helper Utilities ─────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    """Securely hash a password using SHA-256 with a salt."""
    salt = "cloudsuite_salt_v1"  # In production: use bcrypt with per-user salt
    return hashlib.sha256(f"{salt}{password}".encode()).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    return hash_password(password) == password_hash


def generate_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


# ─── Tenant Management Service ────────────────────────────────────────────────
class TenantManagementService:
    """
    Handles tenant lifecycle: onboarding, configuration, suspension, offboarding.
    """

    def onboard_tenant(self, name: str, domain: str, plan: TenantPlan,
                       admin_email: str, admin_password: str) -> Dict:
        """
        Onboard a new tenant with an admin user.
        Returns tenant_id and admin user_id.
        """
        # Validate domain uniqueness
        for t in db._tenants.values():
            if t.domain == domain:
                raise ValueError(f"Domain '{domain}' is already registered.")

        tenant_id = generate_id("tenant_")
        limits = PLAN_LIMITS[plan].copy()

        tenant = Tenant(
            tenant_id=tenant_id,
            name=name,
            domain=domain,
            plan=plan,
            status=TenantStatus.ACTIVE,
            created_at=now_iso(),
            config={
                "theme": "default",
                "timezone": "UTC",
                "language": "en",
                "features": self._features_for_plan(plan),
                "custom_branding": False,
            },
            resource_limits=limits,
            metadata={"onboarded_by": "system"}
        )
        db.save_tenant(tenant)
        db.save_config(tenant_id, tenant.config)

        # Create initial billing record
        billing_svc = BillingService()
        billing_svc.create_billing_record(tenant_id, plan)

        # Create tenant admin user
        auth_svc = AuthenticationService()
        admin = auth_svc.register_user(
            tenant_id=tenant_id,
            email=admin_email,
            password=admin_password,
            role=UserRole.TENANT_ADMIN
        )

        logger.info(f"Tenant onboarded: {tenant_id} ({name}) on plan {plan.value}")
        return {"tenant_id": tenant_id, "admin_user_id": admin.user_id, "status": "active"}

    def _features_for_plan(self, plan: TenantPlan) -> List[str]:
        base = ["dashboard", "reporting"]
        if plan in (TenantPlan.STARTER, TenantPlan.PROFESSIONAL, TenantPlan.ENTERPRISE):
            base += ["analytics", "workflow_automation"]
        if plan in (TenantPlan.PROFESSIONAL, TenantPlan.ENTERPRISE):
            base += ["custom_branding", "api_access", "advanced_reporting"]
        if plan == TenantPlan.ENTERPRISE:
            base += ["sso", "dedicated_support", "sla_99_9"]
        return base

    def update_tenant_plan(self, tenant_id: str, new_plan: TenantPlan) -> Tenant:
        tenant = db.get_tenant(tenant_id)
        if not tenant:
            raise ValueError(f"Tenant {tenant_id} not found.")
        tenant.plan = new_plan
        tenant.resource_limits = PLAN_LIMITS[new_plan].copy()
        tenant.config["features"] = self._features_for_plan(new_plan)
        db.save_tenant(tenant)
        config_cache.invalidate(tenant_id)
        logger.info(f"Tenant {tenant_id} upgraded to plan {new_plan.value}")
        return tenant

    def suspend_tenant(self, tenant_id: str, reason: str = ""):
        tenant = db.get_tenant(tenant_id)
        if not tenant:
            raise ValueError(f"Tenant {tenant_id} not found.")
        tenant.status = TenantStatus.SUSPENDED
        tenant.metadata["suspension_reason"] = reason
        tenant.metadata["suspended_at"] = now_iso()
        db.save_tenant(tenant)
        config_cache.invalidate(tenant_id)
        logger.warning(f"Tenant {tenant_id} suspended. Reason: {reason}")

    def get_tenant_info(self, tenant_id: str) -> Optional[Tenant]:
        return db.get_tenant(tenant_id)


# ─── Configuration Service ────────────────────────────────────────────────────
class ConfigurationService:
    """
    Loads and manages tenant-specific configurations with caching.
    Core of tenant customization handling.
    """

    def load_config(self, tenant_id: str) -> Dict:
        """
        Tenant-specific configuration loading with cache-aside pattern.
        Cache miss → load from DB → populate cache → return.
        """
        # Step 1: Check cache
        cached = config_cache.get(tenant_id)
        if cached:
            return cached

        # Step 2: Load from database
        tenant = db.get_tenant(tenant_id)
        if not tenant:
            raise ValueError(f"Tenant {tenant_id} not found.")
        if tenant.status == TenantStatus.SUSPENDED:
            raise PermissionError(f"Tenant {tenant_id} is suspended.")

        config = db.get_config(tenant_id)
        if not config:
            config = tenant.config

        # Step 3: Merge with plan-level defaults
        merged = self._merge_with_defaults(config, tenant.plan)

        # Step 4: Populate cache
        config_cache.set(tenant_id, merged)
        logger.info(f"Config loaded from DB for tenant {tenant_id}")
        return merged

    def _merge_with_defaults(self, tenant_config: Dict, plan: TenantPlan) -> Dict:
        """Merge tenant config with plan-level feature defaults."""
        defaults = {
            "max_users": PLAN_LIMITS[plan]["max_users"],
            "api_rate_limit": PLAN_LIMITS[plan]["api_rate_limit"],
            "max_storage_gb": PLAN_LIMITS[plan]["max_storage_gb"],
        }
        return {**defaults, **tenant_config}

    def update_config(self, tenant_id: str, updates: Dict) -> Dict:
        """Update tenant configuration and invalidate cache."""
        config = db.get_config(tenant_id) or {}
        config.update(updates)
        db.save_config(tenant_id, config)
        config_cache.invalidate(tenant_id)
        logger.info(f"Config updated for tenant {tenant_id}: {list(updates.keys())}")
        return config

    def get_feature_flag(self, tenant_id: str, feature: str) -> bool:
        """Check if a specific feature is enabled for a tenant."""
        config = self.load_config(tenant_id)
        return feature in config.get("features", [])


# ─── Authentication Service ───────────────────────────────────────────────────
class AuthenticationService:
    """
    Handles user registration, login, session management, and logout.
    Implements tenant-scoped authentication.
    """
    SESSION_TTL_HOURS = 8

    def register_user(self, tenant_id: str, email: str, password: str,
                      role: UserRole = UserRole.MEMBER) -> User:
        """Register a new user under a specific tenant."""
        tenant = db.get_tenant(tenant_id)
        if not tenant:
            raise ValueError(f"Tenant {tenant_id} not found.")

        if db.get_user_by_email(tenant_id, email):
            raise ValueError(f"Email {email} already registered in this tenant.")

        # Check user limit for the plan
        current_users = sum(1 for u in db._users.values() if u.tenant_id == tenant_id)
        max_users = tenant.resource_limits.get("max_users", 5)
        if current_users >= max_users:
            raise PermissionError(f"User limit ({max_users}) reached for this plan.")

        user = User(
            user_id=generate_id("user_"),
            tenant_id=tenant_id,
            email=email,
            role=role,
            password_hash=hash_password(password),
            created_at=now_iso(),
            permissions=ROLE_PERMISSIONS.get(role, [])
        )
        db.save_user(user)
        logger.info(f"User registered: {user.user_id} in tenant {tenant_id} as {role.value}")
        return user

    def login(self, tenant_id: str, email: str, password: str) -> Session:
        """Authenticate user and create a session."""
        tenant = db.get_tenant(tenant_id)
        if not tenant or tenant.status != TenantStatus.ACTIVE:
            raise PermissionError("Tenant is not active.")

        user = db.get_user_by_email(tenant_id, email)
        if not user or not user.is_active:
            raise PermissionError("Invalid credentials.")

        if not verify_password(password, user.password_hash):
            raise PermissionError("Invalid credentials.")

        now = time.time()
        session = Session(
            session_id=generate_id("sess_"),
            user_id=user.user_id,
            tenant_id=tenant_id,
            role=user.role,
            issued_at=now,
            expires_at=now + (self.SESSION_TTL_HOURS * 3600)
        )
        db.save_session(session)
        user.last_login = now_iso()
        db.save_user(user)
        logger.info(f"Login successful: {user.user_id} in tenant {tenant_id}")
        return session

    def validate_session(self, session_id: str) -> Session:
        """Validate a session token and return it if valid."""
        session = db.get_session(session_id)
        if not session:
            raise PermissionError("Session not found or expired.")
        if time.time() > session.expires_at:
            db.delete_session(session_id)
            raise PermissionError("Session expired.")
        return session

    def logout(self, session_id: str):
        """Invalidate a session."""
        db.delete_session(session_id)
        logger.info(f"Session {session_id} logged out.")


# ─── Authorization / RBAC ─────────────────────────────────────────────────────
class AuthorizationService:
    """
    Role-Based Access Control (RBAC) for tenant-aware permission checks.
    """

    def check_permission(self, session: Session, required_permission: str) -> bool:
        """
        Check if the session's role has the required permission.
        Wildcards (*) grant all permissions.
        """
        permissions = ROLE_PERMISSIONS.get(session.role, [])
        if "*" in permissions:
            return True
        return required_permission in permissions

    def enforce(self, session: Session, required_permission: str):
        """Enforce a permission check; raise if not authorized."""
        if not self.check_permission(session, required_permission):
            raise PermissionError(
                f"Role '{session.role.value}' lacks permission '{required_permission}'."
            )

    def require_permission(self, permission: str):
        """Decorator factory for permission-guarded functions."""
        def decorator(fn):
            @wraps(fn)
            def wrapper(*args, **kwargs):
                session = kwargs.get("session") or (args[1] if len(args) > 1 else None)
                if not session or not isinstance(session, Session):
                    raise PermissionError("No valid session provided.")
                self.enforce(session, permission)
                return fn(*args, **kwargs)
            return wrapper
        return decorator


# ─── Tenant-Aware Request Handler ─────────────────────────────────────────────
class RequestContext:
    """Holds per-request context: tenant, user session, config."""
    def __init__(self, tenant_id: str, session: Session, config: Dict):
        self.tenant_id = tenant_id
        self.session = session
        self.config = config
        self.request_id = generate_id("req_")
        self.started_at = time.time()

    def elapsed_ms(self) -> float:
        return round((time.time() - self.started_at) * 1000, 2)


class TenantAwareRequestHandler:
    """
    Core middleware: resolves tenant from request, validates session,
    loads tenant config, and dispatches to the appropriate service.
    This is the heart of tenant-aware request handling.
    """

    def __init__(self):
        self.auth_svc = AuthenticationService()
        self.authz_svc = AuthorizationService()
        self.config_svc = ConfigurationService()
        self.monitor_svc = MonitoringService()

    def handle(self, tenant_id: str, session_id: str, action: str,
               payload: Dict = None) -> Dict:
        """
        Main request handling pipeline:
        1. Resolve tenant
        2. Validate session
        3. Load tenant config
        4. Enforce RBAC
        5. Execute action
        6. Emit metrics
        """
        start = time.time()
        payload = payload or {}

        try:
            # Step 1: Validate tenant
            tenant = db.get_tenant(tenant_id)
            if not tenant:
                raise ValueError(f"Unknown tenant: {tenant_id}")
            if tenant.status != TenantStatus.ACTIVE:
                raise PermissionError(f"Tenant {tenant_id} is {tenant.status.value}.")

            # Step 2: Validate session and ensure it belongs to this tenant
            session = self.auth_svc.validate_session(session_id)
            if session.tenant_id != tenant_id:
                raise PermissionError("Session does not belong to this tenant.")

            # Step 3: Load tenant-specific config
            config = self.config_svc.load_config(tenant_id)

            # Step 4: Build request context
            ctx = RequestContext(tenant_id=tenant_id, session=session, config=config)

            # Step 5: Route action
            result = self._dispatch(ctx, action, payload)

            # Step 6: Emit success metric
            duration_ms = (time.time() - start) * 1000
            self.monitor_svc.record_metric(
                tenant_id=tenant_id,
                service="request_handler",
                metric_name="request_duration_ms",
                value=duration_ms,
                tags={"action": action, "status": "success", "role": session.role.value}
            )

            logger.info(f"[{ctx.request_id}] {action} for tenant {tenant_id} | "
                        f"{duration_ms:.1f}ms | role={session.role.value}")
            return {"success": True, "request_id": ctx.request_id, "data": result}

        except PermissionError as e:
            logger.warning(f"Auth error for tenant {tenant_id} action {action}: {e}")
            self.monitor_svc.record_metric(tenant_id, "request_handler",
                                           "auth_error_count", 1, {"action": action})
            return {"success": False, "error": "forbidden", "message": str(e)}
        except ValueError as e:
            logger.error(f"Validation error for tenant {tenant_id}: {e}")
            return {"success": False, "error": "bad_request", "message": str(e)}
        except Exception as e:
            logger.exception(f"Unexpected error for tenant {tenant_id}: {e}")
            return {"success": False, "error": "internal_error", "message": "Internal server error"}

    def _dispatch(self, ctx: RequestContext, action: str, payload: Dict) -> Any:
        """Route action to the appropriate handler with RBAC enforcement."""
        authz = self.authz_svc

        if action == "data:read":
            authz.enforce(ctx.session, "data:read")
            return self._handle_data_read(ctx, payload)

        elif action == "data:write":
            authz.enforce(ctx.session, "data:write")
            return self._handle_data_write(ctx, payload)

        elif action == "config:read":
            authz.enforce(ctx.session, "config:read")
            return {"config": ctx.config}

        elif action == "config:update":
            authz.enforce(ctx.session, "config:write")
            return ConfigurationService().update_config(ctx.tenant_id, payload)

        elif action == "users:list":
            authz.enforce(ctx.session, "users:read")
            users = [u for u in db._users.values() if u.tenant_id == ctx.tenant_id]
            return {"users": [{"user_id": u.user_id, "email": u.email,
                                "role": u.role.value} for u in users]}

        elif action == "billing:view":
            authz.enforce(ctx.session, "billing:read")
            records = db.get_billing_records(ctx.tenant_id)
            return {"records": [asdict(r) for r in records]}

        else:
            raise ValueError(f"Unknown action: {action}")

    def _handle_data_read(self, ctx: RequestContext, payload: Dict) -> Dict:
        table = payload.get("table", "records")
        rows = db.tenant_table_query(ctx.tenant_id, table)
        return {"table": table, "rows": rows, "count": len(rows)}

    def _handle_data_write(self, ctx: RequestContext, payload: Dict) -> Dict:
        table = payload.get("table", "records")
        row = payload.get("row", {})
        row["_created_by"] = ctx.session.user_id
        row["_created_at"] = now_iso()
        db.tenant_table_insert(ctx.tenant_id, table, row)
        return {"table": table, "inserted": row}


# ─── Billing Service ──────────────────────────────────────────────────────────
class BillingService:
    """
    Manages subscription billing and usage tracking.
    """

    def create_billing_record(self, tenant_id: str, plan: TenantPlan) -> BillingRecord:
        now = datetime.utcnow()
        record = BillingRecord(
            record_id=generate_id("bill_"),
            tenant_id=tenant_id,
            period_start=now.isoformat() + "Z",
            period_end=(now + timedelta(days=30)).isoformat() + "Z",
            plan=plan,
            amount_usd=PLAN_PRICING[plan],
            status="pending" if PLAN_PRICING[plan] > 0 else "paid",
            line_items=[
                {"description": f"{plan.value.capitalize()} Plan", "amount": PLAN_PRICING[plan]}
            ]
        )
        db.add_billing_record(record)
        logger.info(f"Billing record created for tenant {tenant_id}: ${record.amount_usd}/mo")
        return record

    def get_invoice(self, tenant_id: str) -> List[Dict]:
        records = db.get_billing_records(tenant_id)
        return [asdict(r) for r in records]

    def mark_paid(self, tenant_id: str, record_id: str):
        for record in db.get_billing_records(tenant_id):
            if record.record_id == record_id:
                record.status = "paid"
                logger.info(f"Invoice {record_id} marked paid for tenant {tenant_id}")
                return
        raise ValueError(f"Billing record {record_id} not found.")


# ─── Monitoring Service ───────────────────────────────────────────────────────
class MonitoringService:
    """
    Records per-tenant metrics. In production: Prometheus + Grafana.
    """

    def record_metric(self, tenant_id: str, service: str, metric_name: str,
                      value: float, tags: Dict[str, str] = None):
        event = MetricEvent(
            event_id=generate_id("metric_"),
            tenant_id=tenant_id,
            service=service,
            metric_name=metric_name,
            value=value,
            timestamp=now_iso(),
            tags=tags or {}
        )
        db.add_metric(event)

    def get_tenant_metrics(self, tenant_id: str) -> Dict:
        metrics = db.get_metrics(tenant_id)
        summary = {}
        for m in metrics:
            key = f"{m.service}.{m.metric_name}"
            if key not in summary:
                summary[key] = []
            summary[key].append(m.value)
        return {k: {"count": len(v), "avg": round(sum(v)/len(v), 2), "total": sum(v)}
                for k, v in summary.items()}

    def health_check(self) -> Dict:
        return {
            "status": "healthy",
            "timestamp": now_iso(),
            "tenants": len(db._tenants),
            "users": len(db._users),
            "active_sessions": len(db._sessions),
        }


# ─── Demo / Main ──────────────────────────────────────────────────────────────
def run_demo():
    print("=" * 65)
    print("  CloudSuite Multi-Tenant SaaS Platform - Demo")
    print("=" * 65)

    # Services
    tenant_svc = TenantManagementService()
    auth_svc = AuthenticationService()
    config_svc = ConfigurationService()
    billing_svc = BillingService()
    monitor_svc = MonitoringService()
    handler = TenantAwareRequestHandler()

    # ── 1. Onboard Tenant A ──────────────────────────────────────────────────
    print("\n[1] Onboarding Tenant A (Acme Corp) on PROFESSIONAL plan...")
    result_a = tenant_svc.onboard_tenant(
        name="Acme Corp",
        domain="acme.cloudsuite.io",
        plan=TenantPlan.PROFESSIONAL,
        admin_email="admin@acme.com",
        admin_password="SecurePass123!"
    )
    tenant_a_id = result_a["tenant_id"]
    print(f"    Tenant ID : {tenant_a_id}")
    print(f"    Admin ID  : {result_a['admin_user_id']}")

    # ── 2. Onboard Tenant B ──────────────────────────────────────────────────
    print("\n[2] Onboarding Tenant B (Beta Startup) on FREE plan...")
    result_b = tenant_svc.onboard_tenant(
        name="Beta Startup",
        domain="beta.cloudsuite.io",
        plan=TenantPlan.FREE,
        admin_email="admin@beta.com",
        admin_password="BetaPass456!"
    )
    tenant_b_id = result_b["tenant_id"]
    print(f"    Tenant ID : {tenant_b_id}")

    # ── 3. Register Additional User in Tenant A ──────────────────────────────
    print("\n[3] Registering a MEMBER user in Tenant A...")
    member_user = auth_svc.register_user(
        tenant_id=tenant_a_id,
        email="member@acme.com",
        password="MemberPass789!",
        role=UserRole.MEMBER
    )
    print(f"    Member User ID: {member_user.user_id}")

    # ── 4. Login as Admin (Tenant A) ─────────────────────────────────────────
    print("\n[4] Logging in as Tenant A admin...")
    admin_session = auth_svc.login(tenant_a_id, "admin@acme.com", "SecurePass123!")
    print(f"    Session ID : {admin_session.session_id}")
    print(f"    Role       : {admin_session.role.value}")
    print(f"    Expires    : {datetime.fromtimestamp(admin_session.expires_at).strftime('%Y-%m-%d %H:%M:%S')} UTC")

    # ── 5. Login as Member (Tenant A) ────────────────────────────────────────
    print("\n[5] Logging in as Tenant A member...")
    member_session = auth_svc.login(tenant_a_id, "member@acme.com", "MemberPass789!")
    print(f"    Session ID : {member_session.session_id}")
    print(f"    Role       : {member_session.role.value}")

    # ── 6. Load Tenant Config ────────────────────────────────────────────────
    print("\n[6] Loading tenant-specific config for Tenant A (with caching)...")
    config_a = config_svc.load_config(tenant_a_id)
    print(f"    Plan Features  : {config_a.get('features')}")
    print(f"    Max Users      : {config_a.get('max_users')}")
    print(f"    API Rate Limit : {config_a.get('api_rate_limit')}/min")

    # Load again → should hit cache
    print("    Loading again (should be cache HIT)...")
    config_svc.load_config(tenant_a_id)

    # ── 7. Tenant-Aware Request: Write Data ──────────────────────────────────
    print("\n[7] Admin writes a customer record (Tenant A)...")
    write_result = handler.handle(
        tenant_id=tenant_a_id,
        session_id=admin_session.session_id,
        action="data:write",
        payload={"table": "customers", "row": {"name": "Alice", "email": "alice@example.com"}}
    )
    print(f"    Success: {write_result['success']}")
    print(f"    Result : {write_result.get('data')}")

    # ── 8. Member reads data ─────────────────────────────────────────────────
    print("\n[8] Member reads customer records (Tenant A)...")
    read_result = handler.handle(
        tenant_id=tenant_a_id,
        session_id=member_session.session_id,
        action="data:read",
        payload={"table": "customers"}
    )
    print(f"    Rows returned: {read_result['data']['count']}")
    print(f"    Data: {read_result['data']['rows']}")

    # ── 9. Tenant Isolation Test ─────────────────────────────────────────────
    print("\n[9] Cross-tenant isolation test: Tenant B session trying Tenant A data...")
    tenant_b_session = auth_svc.login(tenant_b_id, "admin@beta.com", "BetaPass456!")
    cross_tenant_result = handler.handle(
        tenant_id=tenant_a_id,  # Tenant A's data
        session_id=tenant_b_session.session_id,  # Tenant B's session
        action="data:read",
        payload={"table": "customers"}
    )
    print(f"    Blocked: {not cross_tenant_result['success']}")
    print(f"    Reason : {cross_tenant_result.get('message')}")

    # ── 10. RBAC Test: Member can't update config ─────────────────────────────
    print("\n[10] RBAC test: Member tries to update config (should be denied)...")
    rbac_result = handler.handle(
        tenant_id=tenant_a_id,
        session_id=member_session.session_id,
        action="config:update",
        payload={"theme": "dark"}
    )
    print(f"    Denied: {not rbac_result['success']}")
    print(f"    Reason: {rbac_result.get('message')}")

    # ── 11. Admin updates config ──────────────────────────────────────────────
    print("\n[11] Admin updates tenant config (theme → dark)...")
    update_result = handler.handle(
        tenant_id=tenant_a_id,
        session_id=admin_session.session_id,
        action="config:update",
        payload={"theme": "dark", "language": "en-US"}
    )
    print(f"    Updated: {update_result['success']}")

    # ── 12. Billing ───────────────────────────────────────────────────────────
    print("\n[12] Viewing billing records for Tenant A...")
    billing_result = handler.handle(
        tenant_id=tenant_a_id,
        session_id=admin_session.session_id,
        action="billing:view"
    )
    for rec in billing_result["data"]["records"]:
        print(f"    Plan: {rec['plan']} | Amount: ${rec['amount_usd']}/mo | Status: {rec['status']}")

    # ── 13. Plan Upgrade ──────────────────────────────────────────────────────
    print("\n[13] Upgrading Tenant B from FREE → ENTERPRISE...")
    updated_tenant = tenant_svc.update_tenant_plan(tenant_b_id, TenantPlan.ENTERPRISE)
    print(f"    New Plan   : {updated_tenant.plan.value}")
    print(f"    New Limits : {updated_tenant.resource_limits}")

    # ── 14. Monitoring Metrics ────────────────────────────────────────────────
    print("\n[14] Monitoring metrics for Tenant A...")
    metrics = monitor_svc.get_tenant_metrics(tenant_a_id)
    for key, val in metrics.items():
        print(f"    {key}: avg={val['avg']} count={val['count']}")

    # ── 15. Health Check ──────────────────────────────────────────────────────
    print("\n[15] Platform health check...")
    health = monitor_svc.health_check()
    for k, v in health.items():
        print(f"    {k}: {v}")

    # ── 16. Tenant Suspension ─────────────────────────────────────────────────
    print("\n[16] Suspending Tenant B for non-payment...")
    tenant_svc.suspend_tenant(tenant_b_id, reason="Payment overdue")
    suspended_result = handler.handle(
        tenant_id=tenant_b_id,
        session_id=tenant_b_session.session_id,
        action="data:read",
        payload={"table": "records"}
    )
    print(f"    Access blocked: {not suspended_result['success']}")
    print(f"    Reason        : {suspended_result.get('message')}")

    print("\n" + "=" * 65)
    print("  Demo Complete ✓")
    print("=" * 65)


if __name__ == "__main__":
    run_demo()
