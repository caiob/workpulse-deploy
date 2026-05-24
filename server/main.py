"""
WorkPulse Server
================

Centralized backend for the WorkPulse platform. Receives activity events from
agents, validates licenses, exposes a query/reporting REST API, and serves the
web dashboards (overview, workforce, platform admin).

Endpoints (versioned under /v1):

    Agents (License-bearer auth):
        POST /v1/ingest                  - Push event batches
        POST /v1/license/heartbeat       - License keep-alive

    Admin (X-Admin-Token auth):
        POST /v1/license/issue           - Issue a new license
        POST /v1/license/revoke          - Revoke a license
        GET  /v1/users                   - List enrolled users
        GET  /v1/users/{user_id}/summary - Per-user productivity summary
        GET  /v1/team/summary            - Team-level dashboard data
        GET  /v1/events                  - Raw event search (paginated)
        GET  /v1/admin/tenants           - List all tenants (platform admin)
        GET  /v1/admin/licenses          - List all licenses (platform admin)
        GET  /v1/admin/stats             - System-wide statistics
        GET  /v1/admin/audit             - Admin action audit log
        GET  /v1/workforce/overview      - Workforce dashboard data
        GET  /v1/workforce/timeseries    - Workforce trends over time
        GET  /v1/productivity/rules      - List productivity rules for tenant
        POST /v1/productivity/rules      - Create productivity rule
        DELETE /v1/productivity/rules/{id} - Delete productivity rule
        GET  /v1/productivity/internet   - Aggregated browser/internet usage
        GET  /v1/productivity/apps       - All apps with productivity category

    Public:
        GET  /healthz                    - Liveness probe
        GET  /v1/license/pubkey          - Ed25519 public key

Environment variables:
    WORKPULSE_DB              SQLite path (default: ./workpulse.db)
    WORKPULSE_LICENSE_PRIVKEY Ed25519 private key path (default: ./license_priv.key)
    WORKPULSE_ADMIN_TOKEN     Admin bearer token
    WORKPULSE_LISTEN          host:port (default: 0.0.0.0:8080) — used by serve()

Run:
    uvicorn server.main:app --host 0.0.0.0 --port 8080
    # or:
    python -m server.main          (reads WORKPULSE_LISTEN)
"""

from __future__ import annotations

import asyncio
import os
import re as _re
import json
import sqlite3
import secrets
import datetime as dt
import base64
import time
import threading
from contextlib import contextmanager, asynccontextmanager
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Depends, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization

# ---------------------------------------------------------------------------
# Configuration (env-driven)
# ---------------------------------------------------------------------------
DB_PATH = os.getenv("WORKPULSE_DB", "./workpulse.db")
LICENSE_PRIVKEY_PATH = os.getenv("WORKPULSE_LICENSE_PRIVKEY", "./license_priv.key")
ADMIN_TOKEN = os.getenv("WORKPULSE_ADMIN_TOKEN", "")
LISTEN_ADDR = os.getenv("WORKPULSE_LISTEN", "0.0.0.0:8080")
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv(
        "WORKPULSE_CORS_ORIGINS",
        "https://console.workpulse.example,https://workpulse.example,http://localhost:8081"
    ).split(",") if o.strip()
]
APP_VERSION = "1.0.0"

# Refuse to start without an explicit admin token. Failing closed prevents
# accidental production deploys with a default credential.
if not ADMIN_TOKEN or len(ADMIN_TOKEN) < 32:
    raise RuntimeError(
        "WORKPULSE_ADMIN_TOKEN must be set to a value >= 32 chars. "
        "Generate one with: openssl rand -hex 32"
    )

# ---------------------------------------------------------------------------
# Database (single-file SQLite – ready for Postgres swap with same schema)
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    plan           TEXT NOT NULL DEFAULT 'starter',
    status         TEXT NOT NULL DEFAULT 'active',
    created_at     TEXT NOT NULL,
    dash_token     TEXT,
    retention_days INTEGER DEFAULT NULL
);
CREATE TABLE IF NOT EXISTS daily_summaries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    day         TEXT NOT NULL,
    active_sec  REAL NOT NULL DEFAULT 0,
    afk_sec     REAL NOT NULL DEFAULT 0,
    UNIQUE(tenant_id, user_id, day)
);
CREATE INDEX IF NOT EXISTS idx_daily_tenant_day ON daily_summaries(tenant_id, day);
CREATE TABLE IF NOT EXISTS licenses (
    lic_id        TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    plan          TEXT NOT NULL,
    seats         INTEGER NOT NULL,
    issued        TEXT NOT NULL,
    expires       TEXT NOT NULL,
    revoked       INTEGER NOT NULL DEFAULT 0,
    revoked_at    TEXT,
    token         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT NOT NULL,
    tenant_id     TEXT NOT NULL,
    display_name  TEXT,
    email         TEXT,
    department    TEXT,
    role          TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    PRIMARY KEY (tenant_id, user_id)
);
CREATE TABLE IF NOT EXISTS devices (
    device_id     TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    tenant_id     TEXT NOT NULL,
    hostname      TEXT,
    os            TEXT,
    os_version    TEXT,
    agent_version TEXT,
    last_seen     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id     TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    device_id     TEXT NOT NULL,
    bucket        TEXT NOT NULL,
    ts            TEXT NOT NULL,
    duration_sec  REAL NOT NULL,
    data_json     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    actor         TEXT NOT NULL,
    action        TEXT NOT NULL,
    target        TEXT,
    detail        TEXT,
    ip            TEXT
);
CREATE TABLE IF NOT EXISTS productivity_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL,
    label       TEXT NOT NULL,
    pattern     TEXT NOT NULL,
    match_field TEXT NOT NULL DEFAULT 'title',
    match_type  TEXT NOT NULL DEFAULT 'contains',
    category    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_tenant_ts     ON events(tenant_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_user_ts       ON events(tenant_id, user_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_bucket        ON events(bucket);
CREATE INDEX IF NOT EXISTS idx_events_bucket_ts     ON events(tenant_id, bucket, ts);
CREATE INDEX IF NOT EXISTS idx_licenses_tenant      ON licenses(tenant_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts             ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_prod_rules_tenant    ON productivity_rules(tenant_id);
CREATE TABLE IF NOT EXISTS tenant_departments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id  TEXT NOT NULL,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(tenant_id, name)
);
CREATE INDEX IF NOT EXISTS idx_dept_tenant ON tenant_departments(tenant_id);
"""


@contextmanager
def db():
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.executescript(SCHEMA)
        # Migrações incrementais para bancos já existentes
        cols = [r["name"] for r in c.execute("PRAGMA table_info(tenants)").fetchall()]
        if "dash_token" not in cols:
            c.execute("ALTER TABLE tenants ADD COLUMN dash_token TEXT")
        if "retention_days" not in cols:
            c.execute("ALTER TABLE tenants ADD COLUMN retention_days INTEGER DEFAULT NULL")
        user_cols = [r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()]
        if "login_user" not in user_cols:
            c.execute("ALTER TABLE users ADD COLUMN login_user TEXT")
        # Tabela de departamentos por tenant (cria se não existir)
        c.execute("""
            CREATE TABLE IF NOT EXISTS tenant_departments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(tenant_id, name)
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_dept_tenant ON tenant_departments(tenant_id)")


# ---------------------------------------------------------------------------
# In-memory TTL cache (shared across requests, same process)
# Prevents repeated heavy SQL for the same tenant/period within 90 seconds.
# ---------------------------------------------------------------------------
_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 90  # seconds


def _cache_get(key: str) -> Any | None:
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and (time.monotonic() - entry[0]) < _CACHE_TTL:
            return entry[1]
    return None


def _cache_set(key: str, val: Any) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), val)


def _cache_bust(tenant: str) -> None:
    """Invalidate all cached results for a tenant (called on each ingest)."""
    with _CACHE_LOCK:
        stale = [k for k in _CACHE if k.startswith(f"{tenant}:")]
        for k in stale:
            del _CACHE[k]


def audit(actor: str, action: str, target: str | None = None,
          detail: str | None = None, ip: str | None = None):
    """Best-effort write to audit_log."""
    try:
        with db() as c:
            c.execute(
                """INSERT INTO audit_log (ts, actor, action, target, detail, ip)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (dt.datetime.now(dt.timezone.utc).isoformat(),
                 actor, action, target, detail, ip),
            )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# License handling — Ed25519 signed JWT-like tokens
# ---------------------------------------------------------------------------
def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def load_or_create_keypair() -> Ed25519PrivateKey:
    """Load Ed25519 private key from disk; create one on first run."""
    if not os.path.exists(LICENSE_PRIVKEY_PATH):
        priv = Ed25519PrivateKey.generate()
        pem = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        parent = os.path.dirname(LICENSE_PRIVKEY_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(LICENSE_PRIVKEY_PATH, "wb") as f:
            f.write(pem)
        try:
            os.chmod(LICENSE_PRIVKEY_PATH, 0o600)
        except OSError:
            pass
    with open(LICENSE_PRIVKEY_PATH, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def sign_license(payload: dict[str, Any]) -> str:
    priv = load_or_create_keypair()
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = priv.sign(body)
    return f"{_b64url(body)}.{_b64url(sig)}"


def public_key_b64() -> str:
    priv = load_or_create_keypair()
    raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _b64url(raw)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class IngestEvent(BaseModel):
    bucket: str
    timestamp: dt.datetime
    duration: float = 0.0
    data: dict[str, Any] = Field(default_factory=dict)


class IngestPayload(BaseModel):
    device: str
    os: str | None = None
    os_version: str | None = None
    agent_version: str | None = None
    user_id: str | None = None
    # login_user: nome do usuário autenticado no SO (USERNAME/USER env var)
    # Enviado pelo agente; auto-preenche display_name se ainda não definido.
    login_user: str | None = None
    events: list[IngestEvent]


class UserUpdate(BaseModel):
    display_name: str | None = None
    email: str | None = None
    department: str | None = None
    role: str | None = None


class LicenseIssueRequest(BaseModel):
    tenant_id: str
    tenant_name: str
    plan: str = "business"
    seats: int = 10
    days_valid: int = 365
    features: list[str] = Field(
        default_factory=lambda: ["window", "afk", "browser", "sso"]
    )


class HeartbeatRequest(BaseModel):
    lic: str
    tenant: str
    device: str | None = None


class ProductivityRuleCreate(BaseModel):
    label: str
    pattern: str
    match_field: str = "title"   # 'title' or 'app'
    match_type: str = "contains"  # 'contains', 'exact', 'startswith'
    category: str                 # 'productive', 'unproductive', 'neutral'


# ---------------------------------------------------------------------------
# Browser / productivity helpers
# ---------------------------------------------------------------------------
_BROWSER_APPS: set[str] = {
    "msedge.exe", "msedge", "microsoftedge",
    "chrome.exe", "googlechrome",
    "firefox.exe", "firefox",
    "brave.exe", "brave",
    "opera.exe", "opera",
    "safari", "safari.exe",
    "iexplore.exe", "internetexplorer",
    "chromium.exe", "chromium",
    "vivaldi.exe", "vivaldi",
    "arc.exe", "arc",
    "waterfox.exe", "waterfox",
    "librewolf.exe", "librewolf",
    "floorp.exe", "floorp",
}

_BROWSER_KEYWORDS = ("edge", "chrome", "firefox", "brave", "opera", "safari",
                     "chromium", "vivaldi", "browser", "navigator")

# Strips " - Browser Name" suffix (and optional [InPrivate] / (Incognito) markers)
_BROWSER_SUFFIX_RE = _re.compile(
    r"\s*[-–—]\s*"
    r"(?:Microsoft\s+Edge|Google\s+Chrome|Mozilla\s+Firefox|Opera(?:\s+GX)?"
    r"|Brave(?:\s+Browser)?|Safari|Internet\s+Explorer|Chromium|Vivaldi"
    r"|Arc|Waterfox|LibreWolf|Floorp)"
    r"(?:\s*[\[\(][^\]\)]*[\]\)])?"
    r"\s*$",
    _re.IGNORECASE,
)
_PRIVACY_RE = _re.compile(
    r"\s*[\[\(](?:InPrivate|Incognito|Aba\s+an[oô]nima|Private(?:\s+Browsing)?)[\]\)]\s*",
    _re.IGNORECASE,
)


def _is_browser(app: str) -> bool:
    a = app.lower().replace(" ", "").replace("-", "")
    if a in _BROWSER_APPS:
        return True
    return any(kw in a for kw in _BROWSER_KEYWORDS)


def _clean_title(title: str) -> str:
    """Return site name by stripping browser name from window title."""
    if not title or not title.strip():
        return "(sem título)"
    t = _PRIVACY_RE.sub(" ", title).strip()
    t = _BROWSER_SUFFIX_RE.sub("", t).strip()
    return t or title.strip()


def _apply_rules(site_name: str, app: str, rules: list[dict]) -> str:
    """Return productivity category by matching rules in order. Falls back to 'uncategorized'."""
    for rule in rules:
        field_val = site_name if rule["match_field"] == "title" else app
        pattern = rule["pattern"].lower()
        v = field_val.lower()
        mt = rule["match_type"]
        if mt == "exact" and v == pattern:
            return rule["category"]
        elif mt == "contains" and pattern in v:
            return rule["category"]
        elif mt == "startswith" and v.startswith(pattern):
            return rule["category"]
    return "uncategorized"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def require_admin(x_admin_token: Annotated[str | None, Header()] = None):
    # Constant-time comparison defends against timing attacks that could leak
    # the admin token byte-by-byte over many requests.
    if not x_admin_token or not secrets.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="invalid admin token")


def require_tenant_scope(
    tenant: str,
    x_admin_token: Annotated[str | None, Header()] = None,
):
    """Autoriza endpoints com escopo de tenant.

    Aceita o token de admin master (qualquer tenant) ou o token de painel
    (dash_token) do proprio tenant solicitado. Assim cada empresa-cliente
    acessa apenas os proprios dados, com a sua propria credencial.
    """
    if not x_admin_token:
        raise HTTPException(status_code=401, detail="missing token")
    if secrets.compare_digest(x_admin_token, ADMIN_TOKEN):
        return
    with db() as c:
        row = c.execute(
            "SELECT dash_token FROM tenants WHERE id = ?", (tenant,)
        ).fetchone()
    if row and row["dash_token"] and secrets.compare_digest(x_admin_token, row["dash_token"]):
        return
    raise HTTPException(status_code=403, detail="token invalido para este tenant")


def parse_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return authorization.split(" ", 1)[1].strip()


def validate_agent_token(token: str, tenant_header: str) -> dict:
    """Verify license signature (locally) and check DB record."""
    try:
        payload_b64, sig_b64 = token.split(".")
        payload_bytes = _b64url_decode(payload_b64)
        sig = _b64url_decode(sig_b64)
        priv = load_or_create_keypair()
        pub: Ed25519PublicKey = priv.public_key()
        pub.verify(sig, payload_bytes)
        claims = json.loads(payload_bytes)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=401, detail=f"invalid license: {exc}")

    if claims.get("tenant") != tenant_header:
        raise HTTPException(status_code=401, detail="tenant mismatch")
    if dt.datetime.fromisoformat(claims["expires"].replace("Z", "+00:00")) < dt.datetime.now(dt.timezone.utc):
        raise HTTPException(status_code=403, detail="license expired")

    with db() as c:
        row = c.execute(
            "SELECT revoked FROM licenses WHERE lic_id = ?",
            (claims["lic"],),
        ).fetchone()
        if row and row["revoked"]:
            raise HTTPException(status_code=403, detail="license revoked")
    return claims


# ---------------------------------------------------------------------------
# Background retention worker
# ---------------------------------------------------------------------------
_DEFAULT_RETENTION_DAYS = 90


async def _retention_worker():
    """Runs every 24 h. Archives old events into daily_summaries then deletes them."""
    while True:
        try:
            with db() as c:
                tenants = c.execute(
                    "SELECT id, COALESCE(retention_days, ?) AS days FROM tenants WHERE status='active'",
                    (_DEFAULT_RETENTION_DAYS,),
                ).fetchall()

            for t in tenants:
                tid = t["id"]
                days = int(t["days"])
                cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).date().isoformat()

                with db() as c:
                    # Aggregate active/afk seconds per (tenant, user, day) for events older than cutoff
                    old_rows = c.execute(
                        """SELECT user_id,
                                  substr(ts,1,10) AS day,
                                  SUM(CASE WHEN bucket='window' THEN duration_sec ELSE 0 END) AS active_sec,
                                  SUM(CASE WHEN bucket='afk' AND json_extract(data_json,'$.status')='afk'
                                           THEN duration_sec ELSE 0 END) AS afk_sec
                           FROM events
                           WHERE tenant_id=? AND ts < ?
                           GROUP BY user_id, day""",
                        (tid, cutoff),
                    ).fetchall()

                    if old_rows:
                        c.executemany(
                            """INSERT INTO daily_summaries (tenant_id, user_id, day, active_sec, afk_sec)
                               VALUES (?, ?, ?, ?, ?)
                               ON CONFLICT(tenant_id, user_id, day) DO UPDATE SET
                                   active_sec = daily_summaries.active_sec + excluded.active_sec,
                                   afk_sec    = daily_summaries.afk_sec    + excluded.afk_sec""",
                            [(tid, r["user_id"], r["day"], r["active_sec"], r["afk_sec"])
                             for r in old_rows],
                        )
                        c.execute(
                            "DELETE FROM events WHERE tenant_id=? AND ts < ?",
                            (tid, cutoff),
                        )
        except Exception as exc:  # noqa: BLE001
            # Non-fatal — log and continue next cycle
            print(f"[retention_worker] error: {exc}")

        # Sleep 24 hours before next run
        await asyncio.sleep(86400)


# ---------------------------------------------------------------------------
# FastAPI app + lifespan (modern; replaces deprecated @app.on_event)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    load_or_create_keypair()
    task = asyncio.create_task(_retention_worker())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="WorkPulse Server", version=APP_VERSION, lifespan=lifespan)

# CORS: explicitly listed origins only. Override via WORKPULSE_CORS_ORIGINS
# (comma-separated). Wildcard origins combined with credentials would allow
# any site to make authenticated requests on the user's behalf.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Admin-Token", "X-Tenant"],
    max_age=600,
)


# -- Public ----------------------------------------------------------------
@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": APP_VERSION}


@app.get("/v1/license/pubkey")
def public_key():
    return {"pubkey_b64": public_key_b64()}


# -- Ingestion -------------------------------------------------------------
@app.post("/v1/ingest")
def ingest(
    payload: IngestPayload,
    authorization: Annotated[str | None, Header()] = None,
    x_tenant: Annotated[str | None, Header()] = None,
    x_device: Annotated[str | None, Header()] = None,
    x_os: Annotated[str | None, Header()] = None,
):
    if not x_tenant:
        raise HTTPException(status_code=400, detail="X-Tenant header required")
    token = parse_bearer(authorization)
    claims = validate_agent_token(token, x_tenant)

    # Use the machine hostname as user_id when the agent doesn't provide one.
    # The tenant scope is always enforced via tenant_id, so no prefix needed.
    user_id = payload.user_id or (x_device or payload.device)
    device_id = x_device or payload.device
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    with db() as c:
        c.execute(
            """INSERT OR IGNORE INTO tenants (id, name, created_at)
               VALUES (?, ?, ?)""",
            (claims["tenant"], claims["tenant"], now_iso),
        )
        c.execute(
            """INSERT INTO users (user_id, tenant_id, first_seen, last_seen)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(tenant_id, user_id) DO UPDATE SET last_seen = excluded.last_seen""",
            (user_id, claims["tenant"], now_iso, now_iso),
        )
        # Persistir login_user e auto-preencher display_name se ainda não definido pelo admin.
        if payload.login_user:
            c.execute(
                """UPDATE users
                   SET login_user = ?,
                       display_name = CASE
                           WHEN (display_name IS NULL OR display_name = '') THEN ?
                           ELSE display_name
                       END
                   WHERE tenant_id = ? AND user_id = ?""",
                (payload.login_user, payload.login_user, claims["tenant"], user_id),
            )
        c.execute(
            """INSERT INTO devices (device_id, user_id, tenant_id, hostname, os, os_version, agent_version, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(device_id) DO UPDATE SET
                   hostname = excluded.hostname,
                   os = excluded.os,
                   os_version = excluded.os_version,
                   agent_version = excluded.agent_version,
                   last_seen = excluded.last_seen""",
            (
                device_id,
                user_id,
                claims["tenant"],
                payload.device,
                payload.os or x_os,
                payload.os_version,
                payload.agent_version,
                now_iso,
            ),
        )

        rows = [
            (
                claims["tenant"],
                user_id,
                device_id,
                e.bucket,
                e.timestamp.isoformat(),
                e.duration,
                json.dumps(e.data, ensure_ascii=False),
            )
            for e in payload.events
        ]
        c.executemany(
            """INSERT INTO events
               (tenant_id, user_id, device_id, bucket, ts, duration_sec, data_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
    # Invalidate cached aggregations so the dashboard reflects new data promptly.
    _cache_bust(claims["tenant"])
    return {"accepted": len(payload.events)}


# -- License heartbeat -----------------------------------------------------
@app.post("/v1/license/heartbeat")
def license_heartbeat(req: HeartbeatRequest):
    with db() as c:
        row = c.execute(
            "SELECT revoked, expires FROM licenses WHERE lic_id = ? AND tenant_id = ?",
            (req.lic, req.tenant),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="license not registered")
    if row["revoked"]:
        raise HTTPException(status_code=403, detail="license revoked")
    expires = dt.datetime.fromisoformat(row["expires"].replace("Z", "+00:00"))
    if expires < dt.datetime.now(dt.timezone.utc):
        raise HTTPException(status_code=403, detail="license expired")
    return {"status": "ok", "expires": row["expires"]}


# -- Admin: licenses --------------------------------------------------------
@app.post("/v1/license/issue", dependencies=[Depends(require_admin)])
def license_issue(req: LicenseIssueRequest, request: Request):
    lic_id = secrets.token_hex(8)
    issued = dt.datetime.now(dt.timezone.utc)
    expires = issued + dt.timedelta(days=req.days_valid)
    payload = {
        "lic": lic_id,
        "tenant": req.tenant_id,
        "plan": req.plan,
        "seats": req.seats,
        "issued": issued.isoformat().replace("+00:00", "Z"),
        "expires": expires.isoformat().replace("+00:00", "Z"),
        "features": req.features,
    }
    token = sign_license(payload)
    with db() as c:
        c.execute(
            """INSERT OR IGNORE INTO tenants (id, name, plan, created_at)
               VALUES (?, ?, ?, ?)""",
            (req.tenant_id, req.tenant_name, req.plan, issued.isoformat()),
        )
        c.execute("UPDATE tenants SET plan = ? WHERE id = ?", (req.plan, req.tenant_id))
        # Gera o token de painel (dash_token) do tenant, se ainda nao tiver.
        c.execute(
            """UPDATE tenants SET dash_token = ?
               WHERE id = ? AND (dash_token IS NULL OR dash_token = '')""",
            (secrets.token_hex(16), req.tenant_id),
        )
        dash_row = c.execute(
            "SELECT dash_token FROM tenants WHERE id = ?", (req.tenant_id,)
        ).fetchone()
        c.execute(
            """INSERT INTO licenses (lic_id, tenant_id, plan, seats, issued, expires, token)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (lic_id, req.tenant_id, req.plan, req.seats,
             payload["issued"], payload["expires"], token),
        )
    audit("admin", "license.issue", target=lic_id,
          detail=f"tenant={req.tenant_id} plan={req.plan} seats={req.seats}",
          ip=request.client.host if request.client else None)
    return {
        "license_key": token,
        "lic_id": lic_id,
        "expires": payload["expires"],
        "tenant_id": req.tenant_id,
        "dash_token": dash_row["dash_token"] if dash_row else None,
    }


@app.post("/v1/license/revoke", dependencies=[Depends(require_admin)])
def license_revoke(lic_id: str, request: Request):
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    with db() as c:
        cur = c.execute(
            "UPDATE licenses SET revoked = 1, revoked_at = ? WHERE lic_id = ?",
            (now_iso, lic_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="license not found")
    audit("admin", "license.revoke", target=lic_id,
          ip=request.client.host if request.client else None)
    return {"status": "revoked", "lic_id": lic_id}


# -- Admin: workforce reporting ---------------------------------------------
@app.get("/v1/users", dependencies=[Depends(require_tenant_scope)])
def list_users(tenant: str):
    with db() as c:
        rows = c.execute(
            """SELECT u.user_id, u.display_name, u.email, u.department, u.role, u.last_seen,
                      (SELECT COUNT(*) FROM devices d
                         WHERE d.tenant_id = u.tenant_id AND d.user_id = u.user_id) AS devices
               FROM users u WHERE u.tenant_id = ? ORDER BY u.last_seen DESC""",
            (tenant,),
        ).fetchall()
    return {"users": [dict(r) for r in rows]}


@app.get("/v1/users/{user_id}/summary", dependencies=[Depends(require_tenant_scope)])
def user_summary(user_id: str, tenant: str, days: int = 7):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    with db() as c:
        total = c.execute(
            """SELECT SUM(duration_sec) AS s FROM events
               WHERE tenant_id=? AND user_id=? AND ts>=? AND bucket='window'""",
            (tenant, user_id, since),
        ).fetchone()["s"] or 0
        afk = c.execute(
            """SELECT SUM(duration_sec) AS s FROM events
               WHERE tenant_id=? AND user_id=? AND ts>=? AND bucket='afk'
               AND json_extract(data_json,'$.status')='afk'""",
            (tenant, user_id, since),
        ).fetchone()["s"] or 0
        top_apps = c.execute(
            """SELECT json_extract(data_json,'$.app') AS app,
                      SUM(duration_sec) AS seconds
               FROM events
               WHERE tenant_id=? AND user_id=? AND ts>=? AND bucket='window'
               GROUP BY app ORDER BY seconds DESC LIMIT 10""",
            (tenant, user_id, since),
        ).fetchall()
        by_day = c.execute(
            """SELECT substr(ts,1,10) AS day,
                      SUM(CASE WHEN bucket='window' THEN duration_sec ELSE 0 END) AS active,
                      SUM(CASE WHEN bucket='afk' AND json_extract(data_json,'$.status')='afk'
                               THEN duration_sec ELSE 0 END) AS afk
               FROM events
               WHERE tenant_id=? AND user_id=? AND ts>=?
               GROUP BY day ORDER BY day""",
            (tenant, user_id, since),
        ).fetchall()
    return {
        "user_id": user_id,
        "since": since,
        "active_seconds": max(0, total - afk),
        "afk_seconds": afk,
        "top_apps": [dict(r) for r in top_apps],
        "by_day": [dict(r) for r in by_day],
    }


@app.get("/v1/team/summary", dependencies=[Depends(require_tenant_scope)])
def team_summary(tenant: str, days: int = 7):
    cache_key = f"{tenant}:team_summary:{days}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    with db() as c:
        per_user = c.execute(
            """SELECT u.user_id, u.display_name, u.email, u.department, u.last_seen,
                      COALESCE(SUM(CASE WHEN e.bucket='window' THEN e.duration_sec END),0) AS active,
                      COALESCE(SUM(CASE WHEN e.bucket='afk' AND
                            json_extract(e.data_json,'$.status')='afk'
                            THEN e.duration_sec END),0) AS afk
               FROM users u LEFT JOIN events e
                    ON e.tenant_id = u.tenant_id AND e.user_id = u.user_id AND e.ts >= ?
               WHERE u.tenant_id = ?
               GROUP BY u.user_id ORDER BY active DESC""",
            (since, tenant),
        ).fetchall()
        global_top = c.execute(
            """SELECT json_extract(data_json,'$.app') AS app,
                      SUM(duration_sec) AS seconds
               FROM events
               WHERE tenant_id=? AND ts>=? AND bucket='window'
               GROUP BY app ORDER BY seconds DESC LIMIT 15""",
            (tenant, since),
        ).fetchall()
    result = {
        "tenant": tenant, "since": since,
        "users": [dict(r) for r in per_user],
        "top_apps": [dict(r) for r in global_top],
    }
    _cache_set(cache_key, result)
    return result


@app.get("/v1/events", dependencies=[Depends(require_tenant_scope)])
def search_events(
    tenant: str,
    user_id: str | None = None,
    bucket: str | None = None,
    since: str | None = None,
    until: str | None = None,
    days: int | None = None,
    limit: int = Query(200, le=1000),
    offset: int = 0,
):
    sql = ["SELECT * FROM events WHERE tenant_id = ?"]
    args: list = [tenant]
    if user_id:
        sql.append("AND user_id = ?"); args.append(user_id)
    # If `bucket` not specified, default to window events (raw log view)
    sql.append("AND bucket = ?"); args.append(bucket or "window")
    if days:
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(days))).isoformat()
    if since:
        sql.append("AND ts >= ?"); args.append(since)
    if until:
        sql.append("AND ts <= ?"); args.append(until)
    sql.append("ORDER BY ts DESC LIMIT ? OFFSET ?")
    args.extend([limit, offset])

    with db() as c:
        rows = c.execute(" ".join(sql), args).fetchall()
        rules = [dict(r) for r in c.execute(
            "SELECT * FROM productivity_rules WHERE tenant_id=? ORDER BY created_at",
            (tenant,),
        ).fetchall()]

    events = []
    for r in rows:
        data = json.loads(r["data_json"])
        app_name = data.get("app", "")
        title = _clean_title(data.get("title", ""))
        category = _apply_rules(title, app_name, rules) if r["bucket"] == "window" else "neutral"
        events.append({**dict(r), "data": data, "category": category})

    return {"count": len(events), "events": events}


@app.get("/v1/productivity/events_summary", dependencies=[Depends(require_tenant_scope)])
def productivity_events_summary(
    tenant: str,
    days: int = Query(7, le=90),
    user_id: str | None = None,
    bucket: str | None = None,
):
    """Totals + daily breakdown with productivity classification for ALL window events."""
    _bucket = bucket or "window"
    cache_key = f"{tenant}:events_summary:{days}:{user_id or ''}:{_bucket}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()

    with db() as c:
        rules = [dict(r) for r in c.execute(
            "SELECT * FROM productivity_rules WHERE tenant_id=? ORDER BY created_at",
            (tenant,),
        ).fetchall()]

        # Aggregate by app+title to limit Python-side iteration
        agg_sql = (
            "SELECT json_extract(data_json,'$.app') AS app,"
            " json_extract(data_json,'$.title') AS title,"
            " SUM(duration_sec) AS total_sec"
            " FROM events WHERE tenant_id=? AND bucket=? AND ts>=?"
        )
        agg_args: list = [tenant, _bucket, since]
        if user_id:
            agg_sql += " AND user_id=?"; agg_args.append(user_id)
        agg_sql += " GROUP BY app, title"
        agg_rows = c.execute(agg_sql, agg_args).fetchall()

        # Daily breakdown for chart
        day_sql = (
            "SELECT substr(ts,1,10) AS day,"
            " json_extract(data_json,'$.app') AS app,"
            " json_extract(data_json,'$.title') AS title,"
            " SUM(duration_sec) AS total_sec"
            " FROM events WHERE tenant_id=? AND bucket=? AND ts>=?"
        )
        day_args: list = [tenant, _bucket, since]
        if user_id:
            day_sql += " AND user_id=?"; day_args.append(user_id)
        day_sql += " GROUP BY day, app, title ORDER BY day"
        day_raw = c.execute(day_sql, day_args).fetchall()

    # Totals
    totals: dict[str, float] = {
        "productive": 0.0, "unproductive": 0.0,
        "neutral": 0.0, "uncategorized": 0.0, "total": 0.0,
    }
    for row in agg_rows:
        app_name = row["app"] or ""
        title = _clean_title(row["title"] or "")
        cat = _apply_rules(title, app_name, rules)
        sec = row["total_sec"] or 0.0
        totals[cat] = totals.get(cat, 0.0) + sec
        totals["total"] += sec

    # Daily
    daily_agg: dict[str, dict[str, float]] = {}
    for row in day_raw:
        day = row["day"]
        app_name = row["app"] or ""
        title = _clean_title(row["title"] or "")
        cat = _apply_rules(title, app_name, rules)
        if day not in daily_agg:
            daily_agg[day] = {
                "productive": 0.0, "unproductive": 0.0,
                "neutral": 0.0, "uncategorized": 0.0,
            }
        daily_agg[day][cat] = daily_agg[day].get(cat, 0.0) + (row["total_sec"] or 0.0)

    daily = [
        {
            "day": day,
            "productive":    round(v.get("productive",    0.0), 1),
            "unproductive":  round(v.get("unproductive",  0.0), 1),
            "neutral":       round(v.get("neutral",       0.0), 1),
            "uncategorized": round(v.get("uncategorized", 0.0), 1),
        }
        for day, v in sorted(daily_agg.items())
    ]

    result = {
        "tenant": tenant, "since": since, "days": days,
        "totals": {k: round(v, 1) for k, v in totals.items()},
        "daily": daily,
    }
    _cache_set(cache_key, result)
    return result


# -- Workforce dashboard endpoints -----------------------------------------
@app.get("/v1/workforce/overview", dependencies=[Depends(require_tenant_scope)])
def workforce_overview(tenant: str, days: int = 7):
    """Aggregate metrics for the workforce dashboard."""
    cache_key = f"{tenant}:overview:{days}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    now = dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(days=days)).isoformat()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    online_threshold = (now - dt.timedelta(minutes=10)).isoformat()

    with db() as c:
        totals = c.execute(
            """SELECT
                 COUNT(DISTINCT user_id) AS total_users,
                 SUM(CASE WHEN bucket='window' THEN duration_sec END) AS active,
                 SUM(CASE WHEN bucket='afk' AND json_extract(data_json,'$.status')='afk'
                          THEN duration_sec END) AS afk
               FROM events WHERE tenant_id=? AND ts>=?""",
            (tenant, since),
        ).fetchone()

        online = c.execute(
            """SELECT COUNT(*) AS n FROM users
               WHERE tenant_id=? AND last_seen>=?""",
            (tenant, online_threshold),
        ).fetchone()["n"]

        today_active = c.execute(
            """SELECT SUM(duration_sec) AS s FROM events
               WHERE tenant_id=? AND ts>=? AND bucket='window'""",
            (tenant, today_start),
        ).fetchone()["s"] or 0

        # Hourly distribution
        hourly = c.execute(
            """SELECT CAST(substr(ts,12,2) AS INTEGER) AS hour,
                      SUM(duration_sec) AS seconds
               FROM events WHERE tenant_id=? AND ts>=? AND bucket='window'
               GROUP BY hour ORDER BY hour""",
            (tenant, since),
        ).fetchall()

        # Daily totals
        daily = c.execute(
            """SELECT substr(ts,1,10) AS day,
                      SUM(CASE WHEN bucket='window' THEN duration_sec ELSE 0 END) AS active,
                      SUM(CASE WHEN bucket='afk' AND json_extract(data_json,'$.status')='afk'
                               THEN duration_sec ELSE 0 END) AS afk,
                      COUNT(DISTINCT user_id) AS users
               FROM events WHERE tenant_id=? AND ts>=?
               GROUP BY day ORDER BY day""",
            (tenant, since),
        ).fetchall()

        # Department breakdown
        by_dept = c.execute(
            """SELECT COALESCE(u.department,'Sem departamento') AS dept,
                      COUNT(DISTINCT u.user_id) AS users,
                      COALESCE(SUM(CASE WHEN e.bucket='window' THEN e.duration_sec END),0) AS active
               FROM users u
               LEFT JOIN events e ON e.tenant_id=u.tenant_id AND e.user_id=u.user_id AND e.ts>=?
               WHERE u.tenant_id=?
               GROUP BY dept ORDER BY active DESC""",
            (since, tenant),
        ).fetchall()

        # Risk flags (overwork / under-engagement)
        risk = c.execute(
            """SELECT u.user_id, u.display_name,
                      MAX(daily.active) AS max_day_active,
                      AVG(daily.active) AS avg_day_active
               FROM users u
               LEFT JOIN (
                 SELECT user_id, substr(ts,1,10) AS d, SUM(duration_sec) AS active
                 FROM events WHERE tenant_id=? AND ts>=? AND bucket='window'
                 GROUP BY user_id, d
               ) daily ON daily.user_id = u.user_id
               WHERE u.tenant_id=?
               GROUP BY u.user_id""",
            (tenant, since, tenant),
        ).fetchall()

    overwork, underwork = [], []
    for r in risk:
        max_h = (r["max_day_active"] or 0) / 3600
        avg_h = (r["avg_day_active"] or 0) / 3600
        if max_h > 10:
            overwork.append({"user_id": r["user_id"],
                             "display_name": r["display_name"],
                             "max_day_hours": round(max_h, 1)})
        if 0 < avg_h < 2:
            underwork.append({"user_id": r["user_id"],
                              "display_name": r["display_name"],
                              "avg_day_hours": round(avg_h, 1)})

    result = {
        "tenant": tenant, "since": since, "days": days,
        "totals": {
            "total_users": totals["total_users"] or 0,
            "online_now": online,
            "active_seconds": totals["active"] or 0,
            "afk_seconds": totals["afk"] or 0,
            "today_active_seconds": today_active,
        },
        "hourly": [{"hour": r["hour"], "seconds": r["seconds"]} for r in hourly],
        "daily": [dict(r) for r in daily],
        "by_department": [dict(r) for r in by_dept],
        "alerts": {"overwork": overwork[:10], "underwork": underwork[:10]},
    }
    _cache_set(cache_key, result)
    return result


@app.get("/v1/workforce/timeseries", dependencies=[Depends(require_tenant_scope)])
def workforce_timeseries(tenant: str, days: int = 30, bucket: str = "window"):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    with db() as c:
        rows = c.execute(
            """SELECT substr(ts,1,10) AS day,
                      SUM(duration_sec) AS seconds
               FROM events WHERE tenant_id=? AND ts>=? AND bucket=?
               GROUP BY day ORDER BY day""",
            (tenant, since, bucket),
        ).fetchall()
    return {"tenant": tenant, "bucket": bucket, "series": [dict(r) for r in rows]}


# -- Productivity rules & internet analytics --------------------------------

@app.get("/v1/productivity/rules", dependencies=[Depends(require_tenant_scope)])
def list_productivity_rules(tenant: str):
    with db() as c:
        rows = c.execute(
            "SELECT * FROM productivity_rules WHERE tenant_id=? ORDER BY created_at",
            (tenant,),
        ).fetchall()
    return {"rules": [dict(r) for r in rows]}


@app.post("/v1/productivity/rules", dependencies=[Depends(require_tenant_scope)])
def create_productivity_rule(tenant: str, rule: ProductivityRuleCreate):
    allowed_fields = {"title", "app"}
    allowed_types = {"contains", "exact", "startswith"}
    allowed_cats = {"productive", "unproductive", "neutral"}
    if rule.match_field not in allowed_fields:
        raise HTTPException(status_code=400, detail=f"match_field must be one of {allowed_fields}")
    if rule.match_type not in allowed_types:
        raise HTTPException(status_code=400, detail=f"match_type must be one of {allowed_types}")
    if rule.category not in allowed_cats:
        raise HTTPException(status_code=400, detail=f"category must be one of {allowed_cats}")
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    with db() as c:
        c.execute(
            """INSERT INTO productivity_rules
               (tenant_id, label, pattern, match_field, match_type, category, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (tenant, rule.label, rule.pattern, rule.match_field,
             rule.match_type, rule.category, now_iso),
        )
        row_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    return {"id": row_id, "tenant_id": tenant, **rule.model_dump(), "created_at": now_iso}


@app.delete("/v1/productivity/rules/{rule_id}", dependencies=[Depends(require_tenant_scope)])
def delete_productivity_rule(rule_id: int, tenant: str):
    with db() as c:
        cur = c.execute(
            "DELETE FROM productivity_rules WHERE id=? AND tenant_id=?",
            (rule_id, tenant),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="rule not found")
    return {"deleted": rule_id}


@app.get("/v1/productivity/internet", dependencies=[Depends(require_tenant_scope)])
def productivity_internet(
    tenant: str,
    days: int = Query(7, le=90),
    user_id: str | None = None,
):
    """Aggregated browser/internet usage with productivity classification."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()

    with db() as c:
        rules = [dict(r) for r in c.execute(
            "SELECT * FROM productivity_rules WHERE tenant_id=? ORDER BY created_at",
            (tenant,),
        ).fetchall()]

        # Fetch all window events, group by user+app+title to reduce rows
        sql = (
            "SELECT user_id, json_extract(data_json,'$.app') AS app,"
            " json_extract(data_json,'$.title') AS title,"
            " SUM(duration_sec) AS total_sec, COUNT(*) AS events,"
            " substr(MIN(ts),1,10) AS first_day, substr(MAX(ts),1,10) AS last_day"
            " FROM events"
            " WHERE tenant_id=? AND bucket='window' AND ts>=?"
        )
        args: list = [tenant, since]
        if user_id:
            sql += " AND user_id=?"; args.append(user_id)
        sql += " GROUP BY user_id, app, title ORDER BY total_sec DESC"
        raw_rows = c.execute(sql, args).fetchall()

        # Daily breakdown (for chart) — only browser apps
        daily_sql = (
            "SELECT substr(ts,1,10) AS day,"
            " json_extract(data_json,'$.app') AS app,"
            " json_extract(data_json,'$.title') AS title,"
            " SUM(duration_sec) AS total_sec"
            " FROM events"
            " WHERE tenant_id=? AND bucket='window' AND ts>=?"
        )
        daily_args: list = [tenant, since]
        if user_id:
            daily_sql += " AND user_id=?"; daily_args.append(user_id)
        daily_sql += " GROUP BY day, app, title ORDER BY day"
        daily_raw = c.execute(daily_sql, daily_args).fetchall()

    # ---- Aggregate per-site ----
    site_agg: dict[str, dict] = {}
    for row in raw_rows:
        app = row["app"] or ""
        if not _is_browser(app):
            continue
        site_name = _clean_title(row["title"] or "")
        category = _apply_rules(site_name, app, rules)
        key = site_name
        if key not in site_agg:
            site_agg[key] = {
                "site_name": site_name,
                "category": category,
                "total_sec": 0.0,
                "event_count": 0,
                "users": set(),
                "per_user": {},
            }
        entry = site_agg[key]
        entry["total_sec"] += row["total_sec"] or 0.0
        entry["event_count"] += row["events"] or 0
        entry["users"].add(row["user_id"])
        uid = row["user_id"]
        entry["per_user"][uid] = entry["per_user"].get(uid, 0.0) + (row["total_sec"] or 0.0)

    totals: dict[str, float] = {"productive": 0.0, "unproductive": 0.0,
                                 "neutral": 0.0, "uncategorized": 0.0, "total": 0.0}
    sites = []
    for entry in sorted(site_agg.values(), key=lambda x: x["total_sec"], reverse=True):
        cat = entry["category"]
        totals[cat] = totals.get(cat, 0.0) + entry["total_sec"]
        totals["total"] += entry["total_sec"]
        sites.append({
            "site_name": entry["site_name"],
            "category": cat,
            "total_sec": round(entry["total_sec"], 1),
            "event_count": entry["event_count"],
            "user_count": len(entry["users"]),
            "users": list(entry["users"]),
            "per_user": {k: round(v, 1) for k, v in entry["per_user"].items()},
        })

    # ---- Daily breakdown for chart ----
    daily_agg: dict[str, dict[str, float]] = {}
    for row in daily_raw:
        app = row["app"] or ""
        if not _is_browser(app):
            continue
        day = row["day"]
        site_name = _clean_title(row["title"] or "")
        category = _apply_rules(site_name, app, rules)
        if day not in daily_agg:
            daily_agg[day] = {"productive": 0.0, "unproductive": 0.0,
                               "neutral": 0.0, "uncategorized": 0.0}
        daily_agg[day][category] = daily_agg[day].get(category, 0.0) + (row["total_sec"] or 0.0)

    daily = [
        {
            "day": day,
            "productive": round(v.get("productive", 0.0), 1),
            "unproductive": round(v.get("unproductive", 0.0), 1),
            "neutral": round(v.get("neutral", 0.0), 1),
            "uncategorized": round(v.get("uncategorized", 0.0), 1),
        }
        for day, v in sorted(daily_agg.items())
    ]

    return {
        "tenant": tenant, "since": since, "days": days,
        "totals": {k: round(v, 1) for k, v in totals.items()},
        "sites": sites[:300],
        "daily": daily,
    }


@app.get("/v1/productivity/apps", dependencies=[Depends(require_tenant_scope)])
def productivity_apps(
    tenant: str,
    days: int = Query(30, le=90),
):
    """All apps seen in the period with productivity classification."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    with db() as c:
        rules = [dict(r) for r in c.execute(
            "SELECT * FROM productivity_rules WHERE tenant_id=? ORDER BY created_at",
            (tenant,),
        ).fetchall()]
        rows = c.execute(
            """SELECT json_extract(data_json,'$.app') AS app,
                      SUM(duration_sec) AS total_sec,
                      COUNT(DISTINCT user_id) AS user_count
               FROM events WHERE tenant_id=? AND bucket='window' AND ts>=?
               GROUP BY app ORDER BY total_sec DESC LIMIT 150""",
            (tenant, since),
        ).fetchall()

    apps = []
    for row in rows:
        app = row["app"] or "(desconhecido)"
        category = _apply_rules("", app, rules)
        apps.append({
            "app": app,
            "is_browser": _is_browser(app),
            "category": category,
            "total_sec": round(row["total_sec"] or 0.0, 1),
            "user_count": row["user_count"] or 0,
        })
    return {"tenant": tenant, "since": since, "apps": apps}


# -- Platform admin (super-admin) endpoints --------------------------------
@app.post("/v1/admin/tenant-token", dependencies=[Depends(require_admin)])
def admin_tenant_token(tenant: str, request: Request):
    """Gera (ou renova) o token de painel de um tenant existente."""
    new_token = secrets.token_hex(16)
    with db() as c:
        row = c.execute("SELECT id FROM tenants WHERE id = ?", (tenant,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="tenant nao encontrado")
        c.execute("UPDATE tenants SET dash_token = ? WHERE id = ?", (new_token, tenant))
    audit("admin", "tenant.token", target=tenant,
          ip=request.client.host if request.client else None)
    return {"tenant_id": tenant, "dash_token": new_token}


@app.get("/v1/admin/tenants", dependencies=[Depends(require_admin)])
def admin_list_tenants():
    with db() as c:
        rows = c.execute(
            """SELECT t.id, t.name, t.plan, t.status, t.created_at,
                      t.retention_days,
                      COUNT(DISTINCT l.lic_id) AS licenses,
                      COUNT(DISTINCT u.user_id) AS users
               FROM tenants t
               LEFT JOIN licenses l ON l.tenant_id = t.id AND l.revoked = 0
               LEFT JOIN users u ON u.tenant_id = t.id
               GROUP BY t.id ORDER BY t.created_at DESC""",
        ).fetchall()
    return {"tenants": [dict(r) for r in rows]}


@app.get("/v1/admin/licenses", dependencies=[Depends(require_admin)])
def admin_list_licenses():
    with db() as c:
        rows = c.execute(
            """SELECT l.*, t.name AS tenant_name
               FROM licenses l LEFT JOIN tenants t ON t.id = l.tenant_id
               ORDER BY l.issued DESC""",
        ).fetchall()
    return {"licenses": [dict(r) for r in rows]}


@app.get("/v1/admin/stats", dependencies=[Depends(require_admin)])
def admin_stats():
    """System-wide stats: counts, ingestion rate, DB size."""
    now = dt.datetime.now(dt.timezone.utc)
    last_24h = (now - dt.timedelta(hours=24)).isoformat()
    last_7d  = (now - dt.timedelta(days=7)).isoformat()

    db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0

    with db() as c:
        counts = c.execute(
            """SELECT
                 (SELECT COUNT(*) FROM tenants) AS tenants,
                 (SELECT COUNT(*) FROM users) AS users,
                 (SELECT COUNT(*) FROM devices) AS devices,
                 (SELECT COUNT(*) FROM licenses WHERE revoked=0) AS active_licenses,
                 (SELECT COUNT(*) FROM licenses WHERE revoked=1) AS revoked_licenses,
                 (SELECT COUNT(*) FROM events) AS total_events"""
        ).fetchone()
        events_24h = c.execute(
            "SELECT COUNT(*) AS n FROM events WHERE ts>=?", (last_24h,),
        ).fetchone()["n"]
        events_7d = c.execute(
            "SELECT COUNT(*) AS n FROM events WHERE ts>=?", (last_7d,),
        ).fetchone()["n"]
        ingest_hourly = c.execute(
            """SELECT substr(ts,1,13) AS hour, COUNT(*) AS events
               FROM events WHERE ts>=?
               GROUP BY hour ORDER BY hour""",
            (last_24h,),
        ).fetchall()
        plan_dist = c.execute(
            "SELECT plan, COUNT(*) AS n FROM tenants GROUP BY plan"
        ).fetchall()
    return {
        "db_size_bytes": db_size,
        "counts": dict(counts),
        "events_24h": events_24h,
        "events_7d": events_7d,
        "ingest_by_hour": [dict(r) for r in ingest_hourly],
        "plan_distribution": [dict(r) for r in plan_dist],
    }


@app.get("/v1/admin/audit", dependencies=[Depends(require_admin)])
def admin_audit_log(limit: int = Query(100, le=1000), offset: int = 0):
    with db() as c:
        rows = c.execute(
            "SELECT * FROM audit_log ORDER BY ts DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return {"count": len(rows), "log": [dict(r) for r in rows]}


# -- User management -------------------------------------------------------
@app.patch("/v1/users/{user_id}", dependencies=[Depends(require_tenant_scope)])
def update_user(user_id: str, body: UserUpdate, tenant: str, request: Request):
    """Atualiza metadados de um colaborador (nome, e-mail, depto, função)."""
    fields: list[str] = []
    values: list[Any] = []
    if body.display_name is not None:
        fields.append("display_name = ?")
        values.append(body.display_name)
    if body.email is not None:
        fields.append("email = ?")
        values.append(body.email)
    if body.department is not None:
        fields.append("department = ?")
        values.append(body.department)
    if body.role is not None:
        fields.append("role = ?")
        values.append(body.role)
    if not fields:
        raise HTTPException(status_code=422, detail="no fields to update")
    values.extend([tenant, user_id])
    with db() as c:
        cur = c.execute(
            f"UPDATE users SET {', '.join(fields)} WHERE tenant_id = ? AND user_id = ?",
            values,
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="user not found")
    audit("admin", "user.update", target=user_id,
          detail=f"tenant={tenant} fields={list(body.model_dump(exclude_none=True).keys())}",
          ip=request.client.host if request.client else None)
    _cache_bust(tenant)
    return {"status": "updated", "user_id": user_id}



# -- Departments -----------------------------------------------------------
class DepartmentCreate(BaseModel):
    name: str


@app.get("/v1/departments", dependencies=[Depends(require_tenant_scope)])
def list_departments(tenant: str):
    """Lista departamentos do tenant com contagem de usuários."""
    with db() as c:
        depts = c.execute(
            "SELECT id, name, created_at FROM tenant_departments WHERE tenant_id=? ORDER BY name",
            (tenant,),
        ).fetchall()
        result = []
        for d in depts:
            count = c.execute(
                "SELECT COUNT(*) AS n FROM users WHERE tenant_id=? AND department=?",
                (tenant, d["name"]),
            ).fetchone()["n"]
            users = c.execute(
                """SELECT user_id, display_name, login_user, last_seen
                   FROM users WHERE tenant_id=? AND department=? ORDER BY last_seen DESC""",
                (tenant, d["name"]),
            ).fetchall()
            result.append({
                "id": d["id"],
                "name": d["name"],
                "created_at": d["created_at"],
                "user_count": count,
                "users": [dict(u) for u in users],
            })
    return {"departments": result}


@app.post("/v1/departments", dependencies=[Depends(require_tenant_scope)])
def create_department(body: DepartmentCreate, tenant: str, request: Request):
    """Cria um novo departamento para o tenant."""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name required")
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    with db() as c:
        try:
            c.execute(
                "INSERT INTO tenant_departments (tenant_id, name, created_at) VALUES (?, ?, ?)",
                (tenant, name, now_iso),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="department already exists")
    audit("admin", "dept.create", target=name,
          detail=f"tenant={tenant}",
          ip=request.client.host if request.client else None)
    return {"status": "created", "name": name}


@app.delete("/v1/departments/{dept_name}", dependencies=[Depends(require_tenant_scope)])
def delete_department(dept_name: str, tenant: str, request: Request):
    """Exclui departamento e limpa o campo department dos usuários vinculados."""
    with db() as c:
        cur = c.execute(
            "DELETE FROM tenant_departments WHERE tenant_id=? AND name=?",
            (tenant, dept_name),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="department not found")
        c.execute(
            "UPDATE users SET department=NULL WHERE tenant_id=? AND department=?",
            (tenant, dept_name),
        )
    _cache_bust(tenant)
    audit("admin", "dept.delete", target=dept_name,
          detail=f"tenant={tenant}",
          ip=request.client.host if request.client else None)
    return {"status": "deleted", "name": dept_name}


# -- Tenant retention policy -----------------------------------------------
@app.put("/v1/admin/tenants/{tenant_id}/retention", dependencies=[Depends(require_admin)])
def set_tenant_retention(tenant_id: str, days: int = Query(..., ge=1, le=3650), request: Request = None):
    """Define a política de retenção de dados (em dias) para um tenant."""
    with db() as c:
        cur = c.execute(
            "UPDATE tenants SET retention_days = ? WHERE id = ?",
            (days, tenant_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="tenant not found")
    audit("admin", "tenant.retention", target=tenant_id,
          detail=f"retention_days={days}",
          ip=request.client.host if request.client else None)
    return {"status": "updated", "tenant_id": tenant_id, "retention_days": days}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def serve():
    import uvicorn
    host, _, port_s = LISTEN_ADDR.rpartition(":")
    uvicorn.run(app, host=host or "0.0.0.0", port=int(port_s or 8080))


if __name__ == "__main__":
    serve()
