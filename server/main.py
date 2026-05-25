"""
WorkPulse Server — PostgreSQL edition
======================================

Backend centralizado do WorkPulse. Recebe eventos dos agentes, valida licenças,
expõe API REST de relatórios e serve os dashboards web.

Endpoints (versionados em /v1):

    Agentes (autenticação por token de licença):
        POST /v1/ingest                  - Envia lotes de eventos
        POST /v1/license/heartbeat       - Keep-alive da licença

    Admin (header X-Admin-Token):
        POST /v1/license/issue           - Emite nova licença
        POST /v1/license/revoke          - Revoga licença
        GET  /v1/users                   - Lista usuários cadastrados
        GET  /v1/users/{user_id}/summary - Resumo por usuário
        GET  /v1/team/summary            - Dashboard de equipe
        GET  /v1/events                  - Busca paginada de eventos
        GET  /v1/workforce/overview      - Visão geral da força de trabalho
        GET  /v1/workforce/timeseries    - Tendências temporais
        GET  /v1/productivity/rules      - Regras de produtividade
        POST /v1/productivity/rules      - Cria regra de produtividade
        DELETE /v1/productivity/rules/{id} - Remove regra
        GET  /v1/productivity/internet   - Uso de browser por site
        GET  /v1/productivity/apps       - Todos os aplicativos usados
        GET  /v1/departments             - Departamentos do tenant
        POST /v1/departments             - Cria departamento
        DELETE /v1/departments/{name}    - Remove departamento
        PATCH /v1/users/{user_id}        - Atualiza metadados do usuário
        GET  /v1/admin/tenants           - Lista tenants (plataforma)
        GET  /v1/admin/licenses          - Lista licenças (plataforma)
        GET  /v1/admin/stats             - Estatísticas globais
        GET  /v1/admin/audit             - Log de auditoria
        GET  /v1/admin/data-summary      - Contagem de dados por tenant
        POST /v1/admin/purge-data        - Apaga dados coletados

    Público:
        GET  /healthz                    - Liveness probe
        GET  /v1/license/pubkey          - Chave pública Ed25519

Variáveis de ambiente:
    DB_HOST                       Host do PostgreSQL (obrigatório)
    DB_PORT                       Porta do PostgreSQL (default: 5432)
    DB_NAME                       Nome do banco (default: workpulse)
    DB_USER                       Usuário do banco (obrigatório)
    DB_PASSWORD                   Senha do banco (obrigatório)
    WORKPULSE_ADMIN_TOKEN         Token de admin (>= 32 chars, obrigatório)
    WORKPULSE_LICENSE_PRIVKEY_B64 Chave Ed25519 em base64 (gerada no 1º boot se ausente)
    LICENSE_KEY_SECRET_ARN        ARN do Secrets Manager para gravar a chave no 1º boot
    WORKPULSE_CORS_ORIGINS        Origens CORS separadas por vírgula
    WORKPULSE_LISTEN              host:port (default: 0.0.0.0:8080)
    WEB_CONCURRENCY               Número de workers uvicorn (default: 2)
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import os
import re as _re
import secrets
import sys
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated, Any, cast

import psycopg2
import psycopg2.errors
import psycopg2.extras
import psycopg2.pool
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# ---------------------------------------------------------------------------
# Configuração (variáveis de ambiente)
# ---------------------------------------------------------------------------
DB_HOST     = os.getenv("DB_HOST", "localhost")
DB_PORT     = int(os.getenv("DB_PORT", "5432"))
DB_NAME     = os.getenv("DB_NAME", "workpulse")
DB_USER     = os.getenv("DB_USER", "workpulse")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

ADMIN_TOKEN  = os.getenv("WORKPULSE_ADMIN_TOKEN", "")
LISTEN_ADDR  = os.getenv("WORKPULSE_LISTEN", "0.0.0.0:8080")
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv(
        "WORKPULSE_CORS_ORIGINS",
        "https://workpulse.com.br,https://www.workpulse.com.br,http://localhost:8081"
    ).split(",") if o.strip()
]
LICENSE_KEY_B64     = os.getenv("WORKPULSE_LICENSE_PRIVKEY_B64", "")
LICENSE_KEY_SECRET_ARN = os.getenv("LICENSE_KEY_SECRET_ARN", "")

APP_VERSION = "2.0.0"

if not ADMIN_TOKEN or len(ADMIN_TOKEN) < 32:
    raise RuntimeError(
        "WORKPULSE_ADMIN_TOKEN must be set to a value >= 32 chars. "
        "Generate one with: openssl rand -hex 32"
    )

# ---------------------------------------------------------------------------
# Pool de conexões PostgreSQL
# ---------------------------------------------------------------------------
_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _build_dsn() -> str:
    return (
        f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} "
        f"user={DB_USER} password={DB_PASSWORD} "
        f"connect_timeout=10 application_name=workpulse-server"
    )


def _init_pool() -> None:
    global _pool
    _pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=2,
        maxconn=10,
        dsn=_build_dsn(),
    )


class _PGConn:
    """Wrapper fino sobre psycopg2 que imita a API do sqlite3.Connection.

    Permite que o restante do código use:
        with db() as c:
            rows = c.execute("SELECT ...", (args,)).fetchall()
        sem precisar criar cursores explicitamente.
    """

    def __init__(self, conn: psycopg2.extensions.connection) -> None:
        self._conn = conn
        self._conn.autocommit = True

    def execute(self, sql: str, params: tuple | list | None = None):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or ())
        return cur

    def executemany(self, sql: str, params_seq) -> None:
        cur = self._conn.cursor()
        psycopg2.extras.execute_batch(cur, sql, params_seq, page_size=500)
        cur.close()


@contextmanager
def db():
    assert _pool is not None, "Pool não inicializado — chame _init_pool() primeiro"
    conn = _pool.getconn()
    try:
        yield _PGConn(conn)
    finally:
        _pool.putconn(conn)


# ---------------------------------------------------------------------------
# Schema do banco (PostgreSQL)
# ---------------------------------------------------------------------------
_SCHEMA_STMTS = [
    """
    CREATE TABLE IF NOT EXISTS tenants (
        id            TEXT PRIMARY KEY,
        name          TEXT NOT NULL,
        plan          TEXT NOT NULL DEFAULT 'starter',
        status        TEXT NOT NULL DEFAULT 'active',
        created_at    TEXT NOT NULL,
        dash_token    TEXT,
        retention_days INTEGER DEFAULT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS licenses (
        lic_id    TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        plan      TEXT NOT NULL,
        seats     INTEGER NOT NULL,
        issued    TEXT NOT NULL,
        expires   TEXT NOT NULL,
        revoked   INTEGER NOT NULL DEFAULT 0,
        revoked_at TEXT,
        token     TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id      TEXT NOT NULL,
        tenant_id    TEXT NOT NULL,
        display_name TEXT,
        email        TEXT,
        department   TEXT,
        role         TEXT,
        login_user   TEXT,
        first_seen   TEXT NOT NULL,
        last_seen    TEXT NOT NULL,
        PRIMARY KEY (tenant_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS devices (
        device_id     TEXT PRIMARY KEY,
        user_id       TEXT NOT NULL,
        tenant_id     TEXT NOT NULL,
        hostname      TEXT,
        os            TEXT,
        os_version    TEXT,
        agent_version TEXT,
        last_seen     TEXT NOT NULL
    )
    """,
    # data_json como JSONB: permite expression indexes e operador ->>/->
    """
    CREATE TABLE IF NOT EXISTS events (
        id           BIGSERIAL PRIMARY KEY,
        tenant_id    TEXT NOT NULL,
        user_id      TEXT NOT NULL,
        device_id    TEXT NOT NULL,
        bucket       TEXT NOT NULL,
        ts           TEXT NOT NULL,
        duration_sec DOUBLE PRECISION NOT NULL,
        data_json    JSONB NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_summaries (
        id         BIGSERIAL PRIMARY KEY,
        tenant_id  TEXT NOT NULL,
        user_id    TEXT NOT NULL,
        day        TEXT NOT NULL,
        active_sec DOUBLE PRECISION NOT NULL DEFAULT 0,
        afk_sec    DOUBLE PRECISION NOT NULL DEFAULT 0,
        UNIQUE (tenant_id, user_id, day)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id     BIGSERIAL PRIMARY KEY,
        ts     TEXT NOT NULL,
        actor  TEXT NOT NULL,
        action TEXT NOT NULL,
        target TEXT,
        detail TEXT,
        ip     TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS productivity_rules (
        id          BIGSERIAL PRIMARY KEY,
        tenant_id   TEXT NOT NULL,
        label       TEXT NOT NULL,
        pattern     TEXT NOT NULL,
        match_field TEXT NOT NULL DEFAULT 'title',
        match_type  TEXT NOT NULL DEFAULT 'contains',
        category    TEXT NOT NULL,
        created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tenant_departments (
        id         BIGSERIAL PRIMARY KEY,
        tenant_id  TEXT NOT NULL,
        name       TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (tenant_id, name)
    )
    """,
    # Índices — incluindo expression indexes em JSONB para eliminar full-scans
    "CREATE INDEX IF NOT EXISTS idx_events_tenant_ts    ON events(tenant_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_events_user_ts      ON events(tenant_id, user_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_events_bucket_ts    ON events(tenant_id, bucket, ts)",
    "CREATE INDEX IF NOT EXISTS idx_events_app          ON events((data_json->>'app'))",
    "CREATE INDEX IF NOT EXISTS idx_events_title        ON events((data_json->>'title'))",
    "CREATE INDEX IF NOT EXISTS idx_daily_tenant_day    ON daily_summaries(tenant_id, day)",
    "CREATE INDEX IF NOT EXISTS idx_licenses_tenant     ON licenses(tenant_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_ts            ON audit_log(ts)",
    "CREATE INDEX IF NOT EXISTS idx_prod_rules_tenant   ON productivity_rules(tenant_id)",
    "CREATE INDEX IF NOT EXISTS idx_dept_tenant         ON tenant_departments(tenant_id)",
]


def init_db() -> None:
    with db() as c:
        for stmt in _SCHEMA_STMTS:
            c.execute(stmt)


# ---------------------------------------------------------------------------
# Cache em memória com TTL (compartilhado entre threads do mesmo processo)
# ---------------------------------------------------------------------------
_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL  = 90  # segundos


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
    with _CACHE_LOCK:
        stale = [k for k in _CACHE if k.startswith(f"{tenant}:")]
        for k in stale:
            del _CACHE[k]


# ---------------------------------------------------------------------------
# Auditoria
# ---------------------------------------------------------------------------
def audit(actor: str, action: str, target: str | None = None,
          detail: str | None = None, ip: str | None = None) -> None:
    try:
        with db() as c:
            c.execute(
                """INSERT INTO audit_log (ts, actor, action, target, detail, ip)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (dt.datetime.now(dt.timezone.utc).isoformat(),
                 actor, action, target, detail, ip),
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Licenças — Ed25519
# ---------------------------------------------------------------------------
def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _store_license_key_in_sm(pem: bytes) -> None:
    """Grava a chave no Secrets Manager (melhor esforço — não falha o boot)."""
    if not LICENSE_KEY_SECRET_ARN:
        return
    try:
        import boto3
        client = boto3.client("secretsmanager")
        client.put_secret_value(
            SecretId=LICENSE_KEY_SECRET_ARN,
            SecretString=base64.b64encode(pem).decode("ascii"),
        )
        print("[license] Chave de licença gravada no Secrets Manager.", flush=True)
    except Exception as exc:
        print(f"[license] AVISO: não foi possível gravar no Secrets Manager: {exc}", flush=True)


# Cache em memória — cada processo worker carrega a chave UMA vez no startup.
_cached_privkey: "Ed25519PrivateKey | None" = None


def load_or_create_keypair() -> "Ed25519PrivateKey":
    """Carrega a chave de licença.

    Prioridade:
      1. Cache em memória (evita regeneração a cada request)
      2. Secrets Manager diretamente (env var pode ter sido injetada com placeholder)
      3. Variável de ambiente WORKPULSE_LICENSE_PRIVKEY_B64
      4. Gera nova chave e grava no SM (único boot sem chave prévia)
    """
    global _cached_privkey
    if _cached_privkey is not None:
        return _cached_privkey

    # 1. Secrets Manager — fonte autoritativa; ignora env var stale
    if LICENSE_KEY_SECRET_ARN:
        try:
            import boto3
            sm_client = boto3.client("secretsmanager")
            resp = sm_client.get_secret_value(SecretId=LICENSE_KEY_SECRET_ARN)
            sm_b64 = resp.get("SecretString", "").strip()
            if sm_b64 and sm_b64 != "PLACEHOLDER":
                pem = base64.b64decode(sm_b64)
                _cached_privkey = cast(
                    "Ed25519PrivateKey",
                    serialization.load_pem_private_key(pem, password=None),
                )
                print("[license] Chave de licença carregada do Secrets Manager.", flush=True)
                return _cached_privkey
        except Exception as exc:
            print(f"[license] AVISO: não foi possível ler chave do SM: {exc}", flush=True)

    # 2. Variável de ambiente (fallback para testes/dev)
    key_b64 = LICENSE_KEY_B64.strip()
    if key_b64 and key_b64 != "PLACEHOLDER":
        pem = base64.b64decode(key_b64)
        _cached_privkey = cast(
            "Ed25519PrivateKey",
            serialization.load_pem_private_key(pem, password=None),
        )
        print("[license] Chave de licença carregada da variável de ambiente.", flush=True)
        return _cached_privkey

    # 3. Gera nova chave (único boot real sem chave prévia)
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    b64 = base64.b64encode(pem).decode("ascii")
    print(
        f"[license] ATENÇÃO: nenhuma chave de licença configurada. "
        f"Nova chave gerada. Armazene no Secrets Manager ({LICENSE_KEY_SECRET_ARN or 'ARN não configurado'}):\n"
        f"  WORKPULSE_LICENSE_PRIVKEY_B64={b64}",
        file=sys.stderr, flush=True,
    )
    _store_license_key_in_sm(pem)
    _cached_privkey = priv
    return priv


def sign_license(payload: dict[str, Any]) -> str:
    priv = load_or_create_keypair()
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig  = priv.sign(body)
    return f"{_b64url(body)}.{_b64url(sig)}"


def public_key_b64() -> str:
    priv = load_or_create_keypair()
    raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _b64url(raw)


# ---------------------------------------------------------------------------
# Modelos Pydantic
# ---------------------------------------------------------------------------
class IngestEvent(BaseModel):
    bucket:    str
    timestamp: dt.datetime
    duration:  float = 0.0
    data:      dict[str, Any] = Field(default_factory=dict)


class IngestPayload(BaseModel):
    device:        str
    os:            str | None = None
    os_version:    str | None = None
    agent_version: str | None = None
    user_id:       str | None = None
    login_user:    str | None = None
    events:        list[IngestEvent]


class UserUpdate(BaseModel):
    display_name: str | None = None
    email:        str | None = None
    department:   str | None = None
    role:         str | None = None


class LicenseIssueRequest(BaseModel):
    tenant_id:   str
    tenant_name: str
    plan:        str = "business"
    seats:       int = 10
    days_valid:  int = 365
    features:    list[str] = Field(
        default_factory=lambda: ["window", "afk", "browser", "sso"]
    )


class HeartbeatRequest(BaseModel):
    lic:    str
    tenant: str
    device: str | None = None


class ProductivityRuleCreate(BaseModel):
    label:       str
    pattern:     str
    match_field: str = "title"
    match_type:  str = "contains"
    category:    str


class DepartmentCreate(BaseModel):
    name: str


# ---------------------------------------------------------------------------
# Helpers de browser / produtividade
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
    return a in _BROWSER_APPS or any(kw in a for kw in _BROWSER_KEYWORDS)


def _clean_title(title: str) -> str:
    if not title or not title.strip():
        return "(sem título)"
    t = _PRIVACY_RE.sub(" ", title).strip()
    t = _BROWSER_SUFFIX_RE.sub("", t).strip()
    return t or title.strip()


def _apply_rules(site_name: str, app: str, rules: list[dict]) -> str:
    for rule in rules:
        field_val = site_name if rule["match_field"] == "title" else app
        pattern   = rule["pattern"].lower()
        v         = field_val.lower()
        mt        = rule["match_type"]
        if mt == "exact"      and v == pattern:          return rule["category"]
        if mt == "contains"   and pattern in v:          return rule["category"]
        if mt == "startswith" and v.startswith(pattern): return rule["category"]
    return "uncategorized"


# ---------------------------------------------------------------------------
# Autenticação
# ---------------------------------------------------------------------------
def require_admin(x_admin_token: Annotated[str | None, Header()] = None) -> None:
    if not x_admin_token or not secrets.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="invalid admin token")


def require_tenant_scope(
    tenant: str,
    x_admin_token: Annotated[str | None, Header()] = None,
) -> None:
    if not x_admin_token:
        raise HTTPException(status_code=401, detail="missing token")
    if secrets.compare_digest(x_admin_token, ADMIN_TOKEN):
        return
    with db() as c:
        row = c.execute(
            "SELECT dash_token FROM tenants WHERE id = %s", (tenant,)
        ).fetchone()
    if row and row["dash_token"] and secrets.compare_digest(x_admin_token, row["dash_token"]):
        return
    raise HTTPException(status_code=403, detail="token invalido para este tenant")


def parse_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return authorization.split(" ", 1)[1].strip()


def validate_agent_token(token: str, tenant_header: str) -> dict:
    try:
        payload_b64, sig_b64 = token.split(".")
        payload_bytes = _b64url_decode(payload_b64)
        sig           = _b64url_decode(sig_b64)
        priv          = load_or_create_keypair()
        pub: Ed25519PublicKey = priv.public_key()
        pub.verify(sig, payload_bytes)
        claims = json.loads(payload_bytes)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"invalid license: {exc}")

    if claims.get("tenant") != tenant_header:
        raise HTTPException(status_code=401, detail="tenant mismatch")
    if dt.datetime.fromisoformat(claims["expires"].replace("Z", "+00:00")) < dt.datetime.now(dt.timezone.utc):
        raise HTTPException(status_code=403, detail="license expired")

    with db() as c:
        row = c.execute(
            "SELECT revoked FROM licenses WHERE lic_id = %s", (claims["lic"],)
        ).fetchone()
        if row and row["revoked"]:
            raise HTTPException(status_code=403, detail="license revoked")
    return claims


# ---------------------------------------------------------------------------
# Worker de retenção (roda a cada 24 h)
# ---------------------------------------------------------------------------
_DEFAULT_RETENTION_DAYS = 90


def _do_retention() -> None:
    """Agrega eventos antigos em daily_summaries e os deleta.  Roda em thread separada."""
    try:
        with db() as c:
            tenants = c.execute(
                "SELECT id, COALESCE(retention_days, %s) AS days FROM tenants WHERE status='active'",
                (_DEFAULT_RETENTION_DAYS,),
            ).fetchall()

        for t in tenants:
            tid    = t["id"]
            days   = int(t["days"])
            cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).date().isoformat()

            with db() as c:
                old_rows = c.execute(
                    """SELECT user_id,
                              substr(ts, 1, 10) AS day,
                              SUM(CASE WHEN bucket='window' THEN duration_sec ELSE 0 END) AS active_sec,
                              SUM(CASE WHEN bucket='afk' AND data_json->>'status'='afk'
                                       THEN duration_sec ELSE 0 END) AS afk_sec
                       FROM events
                       WHERE tenant_id=%s AND ts < %s
                       GROUP BY user_id, day""",
                    (tid, cutoff),
                ).fetchall()

                if old_rows:
                    c.executemany(
                        """INSERT INTO daily_summaries (tenant_id, user_id, day, active_sec, afk_sec)
                           VALUES (%s, %s, %s, %s, %s)
                           ON CONFLICT (tenant_id, user_id, day) DO UPDATE SET
                               active_sec = daily_summaries.active_sec + EXCLUDED.active_sec,
                               afk_sec    = daily_summaries.afk_sec    + EXCLUDED.afk_sec""",
                        [(tid, r["user_id"], r["day"], r["active_sec"], r["afk_sec"])
                         for r in old_rows],
                    )
                    c.execute(
                        "DELETE FROM events WHERE tenant_id=%s AND ts < %s",
                        (tid, cutoff),
                    )
    except Exception as exc:
        print(f"[retention_worker] error: {exc}", flush=True)


async def _retention_worker() -> None:
    while True:
        # Roda a operação de DB em thread para não bloquear o event loop
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _do_retention)
        await asyncio.sleep(86400)


# ---------------------------------------------------------------------------
# FastAPI — app + lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_pool()
    init_db()
    load_or_create_keypair()
    task = asyncio.create_task(_retention_worker())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    if _pool:
        _pool.closeall()


app = FastAPI(title="WorkPulse Server", version=APP_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Admin-Token", "X-Tenant"],
    max_age=600,
)


# ===========================================================================
# Endpoints públicos
# ===========================================================================

@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": APP_VERSION}


@app.get("/v1/license/pubkey")
def public_key():
    return {"pubkey_b64": public_key_b64()}


# ===========================================================================
# Ingestão de eventos
# ===========================================================================

@app.post("/v1/ingest")
def ingest(
    payload: IngestPayload,
    authorization: Annotated[str | None, Header()] = None,
    x_tenant:      Annotated[str | None, Header()] = None,
    x_device:      Annotated[str | None, Header()] = None,
    x_os:          Annotated[str | None, Header()] = None,
):
    if not x_tenant:
        raise HTTPException(status_code=400, detail="X-Tenant header required")
    token  = parse_bearer(authorization)
    claims = validate_agent_token(token, x_tenant)

    user_id   = payload.user_id or (x_device or payload.device)
    device_id = x_device or payload.device
    now_iso   = dt.datetime.now(dt.timezone.utc).isoformat()

    with db() as c:
        c.execute(
            """INSERT INTO tenants (id, name, created_at)
               VALUES (%s, %s, %s)
               ON CONFLICT (id) DO NOTHING""",
            (claims["tenant"], claims["tenant"], now_iso),
        )
        c.execute(
            """INSERT INTO users (user_id, tenant_id, first_seen, last_seen)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (tenant_id, user_id) DO UPDATE SET last_seen = EXCLUDED.last_seen""",
            (user_id, claims["tenant"], now_iso, now_iso),
        )
        if payload.login_user:
            c.execute(
                """UPDATE users
                   SET login_user    = %s,
                       display_name  = CASE
                           WHEN (display_name IS NULL OR display_name = '') THEN %s
                           ELSE display_name
                       END
                   WHERE tenant_id = %s AND user_id = %s""",
                (payload.login_user, payload.login_user, claims["tenant"], user_id),
            )
        c.execute(
            """INSERT INTO devices
                   (device_id, user_id, tenant_id, hostname, os, os_version, agent_version, last_seen)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (device_id) DO UPDATE SET
                   hostname      = EXCLUDED.hostname,
                   os            = EXCLUDED.os,
                   os_version    = EXCLUDED.os_version,
                   agent_version = EXCLUDED.agent_version,
                   last_seen     = EXCLUDED.last_seen""",
            (device_id, user_id, claims["tenant"], payload.device,
             payload.os or x_os, payload.os_version, payload.agent_version, now_iso),
        )
        rows = [
            (
                claims["tenant"], user_id, device_id,
                e.bucket, e.timestamp.isoformat(), e.duration,
                psycopg2.extras.Json(e.data),
            )
            for e in payload.events
        ]
        c.executemany(
            """INSERT INTO events
                   (tenant_id, user_id, device_id, bucket, ts, duration_sec, data_json)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            rows,
        )

    _cache_bust(claims["tenant"])
    return {"accepted": len(payload.events)}


# ===========================================================================
# Licenças
# ===========================================================================

@app.post("/v1/license/heartbeat")
def license_heartbeat(req: HeartbeatRequest):
    with db() as c:
        row = c.execute(
            "SELECT revoked, expires FROM licenses WHERE lic_id = %s AND tenant_id = %s",
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


@app.post("/v1/license/issue", dependencies=[Depends(require_admin)])
def license_issue(req: LicenseIssueRequest, request: Request):
    lic_id  = secrets.token_hex(8)
    issued  = dt.datetime.now(dt.timezone.utc)
    expires = issued + dt.timedelta(days=req.days_valid)
    payload_dict = {
        "lic":     lic_id,
        "tenant":  req.tenant_id,
        "plan":    req.plan,
        "seats":   req.seats,
        "issued":  issued.isoformat().replace("+00:00", "Z"),
        "expires": expires.isoformat().replace("+00:00", "Z"),
        "features": req.features,
    }
    token = sign_license(payload_dict)

    with db() as c:
        c.execute(
            """INSERT INTO tenants (id, name, plan, created_at)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (id) DO NOTHING""",
            (req.tenant_id, req.tenant_name, req.plan, issued.isoformat()),
        )
        c.execute(
            "UPDATE tenants SET plan = %s WHERE id = %s",
            (req.plan, req.tenant_id),
        )
        c.execute(
            """UPDATE tenants SET dash_token = %s
               WHERE id = %s AND (dash_token IS NULL OR dash_token = '')""",
            (secrets.token_hex(16), req.tenant_id),
        )
        dash_row = c.execute(
            "SELECT dash_token FROM tenants WHERE id = %s", (req.tenant_id,)
        ).fetchone()
        c.execute(
            """INSERT INTO licenses (lic_id, tenant_id, plan, seats, issued, expires, token)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (lic_id, req.tenant_id, req.plan, req.seats,
             payload_dict["issued"], payload_dict["expires"], token),
        )

    audit("admin", "license.issue", target=lic_id,
          detail=f"tenant={req.tenant_id} plan={req.plan} seats={req.seats}",
          ip=request.client.host if request.client else None)
    return {
        "license_key": token,
        "lic_id":      lic_id,
        "expires":     payload_dict["expires"],
        "tenant_id":   req.tenant_id,
        "dash_token":  dash_row["dash_token"] if dash_row else None,
    }


@app.post("/v1/license/revoke", dependencies=[Depends(require_admin)])
def license_revoke(lic_id: str, request: Request):
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    with db() as c:
        cur = c.execute(
            "UPDATE licenses SET revoked = 1, revoked_at = %s WHERE lic_id = %s",
            (now_iso, lic_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="license not found")
    audit("admin", "license.revoke", target=lic_id,
          ip=request.client.host if request.client else None)
    return {"status": "revoked", "lic_id": lic_id}


# ===========================================================================
# Usuários
# ===========================================================================

@app.get("/v1/users", dependencies=[Depends(require_tenant_scope)])
def list_users(tenant: str):
    with db() as c:
        rows = c.execute(
            """SELECT user_id, display_name, email, department, role,
                      login_user, first_seen, last_seen
               FROM users WHERE tenant_id = %s ORDER BY last_seen DESC""",
            (tenant,),
        ).fetchall()
    return {"users": [dict(r) for r in rows]}


@app.get("/v1/users/{user_id}/summary", dependencies=[Depends(require_tenant_scope)])
def user_summary(user_id: str, tenant: str, days: int = 7):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    with db() as c:
        info = c.execute(
            "SELECT * FROM users WHERE tenant_id = %s AND user_id = %s",
            (tenant, user_id),
        ).fetchone()
        if not info:
            raise HTTPException(status_code=404, detail="user not found")

        totals = c.execute(
            """SELECT
                   SUM(CASE WHEN bucket='window' THEN duration_sec END) AS active,
                   SUM(CASE WHEN bucket='afk' AND data_json->>'status'='afk'
                            THEN duration_sec END) AS afk
               FROM events WHERE tenant_id=%s AND user_id=%s AND ts>=%s""",
            (tenant, user_id, since),
        ).fetchone()

        top_apps = c.execute(
            """SELECT data_json->>'app' AS app, SUM(duration_sec) AS seconds
               FROM events
               WHERE tenant_id=%s AND user_id=%s AND ts>=%s AND bucket='window'
               GROUP BY app ORDER BY seconds DESC LIMIT 10""",
            (tenant, user_id, since),
        ).fetchall()

        daily = c.execute(
            """SELECT substr(ts, 1, 10) AS day,
                      SUM(CASE WHEN bucket='window' THEN duration_sec ELSE 0 END) AS active,
                      SUM(CASE WHEN bucket='afk' AND data_json->>'status'='afk'
                               THEN duration_sec ELSE 0 END) AS afk
               FROM events WHERE tenant_id=%s AND user_id=%s AND ts>=%s
               GROUP BY day ORDER BY day""",
            (tenant, user_id, since),
        ).fetchall()

    return {
        "user":     dict(info),
        "since":    since,
        "active_seconds": totals["active"] or 0,
        "afk_seconds":    totals["afk"]    or 0,
        "top_apps": [dict(r) for r in top_apps],
        "daily":    [dict(r) for r in daily],
        "by_day":   [dict(r) for r in daily],
    }


@app.patch("/v1/users/{user_id}", dependencies=[Depends(require_tenant_scope)])
def update_user(user_id: str, body: UserUpdate, tenant: str, request: Request):
    fields: list[str] = []
    values: list[Any] = []
    if body.display_name is not None:
        fields.append("display_name = %s"); values.append(body.display_name)
    if body.email is not None:
        fields.append("email = %s"); values.append(body.email)
    if body.department is not None:
        fields.append("department = %s"); values.append(body.department)
    if body.role is not None:
        fields.append("role = %s"); values.append(body.role)
    if not fields:
        raise HTTPException(status_code=422, detail="no fields to update")
    values.extend([tenant, user_id])
    with db() as c:
        cur = c.execute(
            f"UPDATE users SET {', '.join(fields)} WHERE tenant_id = %s AND user_id = %s",
            values,
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="user not found")
    audit("admin", "user.update", target=user_id,
          detail=f"tenant={tenant} fields={list(body.model_dump(exclude_none=True).keys())}",
          ip=request.client.host if request.client else None)
    _cache_bust(tenant)
    return {"status": "updated", "user_id": user_id}


# ===========================================================================
# Resumo de equipe
# ===========================================================================

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
                      COALESCE(SUM(CASE WHEN e.bucket='window' THEN e.duration_sec END), 0) AS active,
                      COALESCE(SUM(CASE WHEN e.bucket='afk' AND e.data_json->>'status'='afk'
                                        THEN e.duration_sec END), 0) AS afk
               FROM users u LEFT JOIN events e
                    ON e.tenant_id = u.tenant_id AND e.user_id = u.user_id AND e.ts >= %s
               WHERE u.tenant_id = %s
               GROUP BY u.user_id, u.display_name, u.email, u.department, u.last_seen
               ORDER BY active DESC""",
            (since, tenant),
        ).fetchall()
        global_top = c.execute(
            """SELECT data_json->>'app' AS app, SUM(duration_sec) AS seconds
               FROM events
               WHERE tenant_id=%s AND ts>=%s AND bucket='window'
               GROUP BY app ORDER BY seconds DESC LIMIT 15""",
            (tenant, since),
        ).fetchall()

    result = {
        "tenant":   tenant,
        "since":    since,
        "users":    [dict(r) for r in per_user],
        "top_apps": [dict(r) for r in global_top],
    }
    _cache_set(cache_key, result)
    return result


# ===========================================================================
# Eventos
# ===========================================================================

@app.get("/v1/events", dependencies=[Depends(require_tenant_scope)])
def search_events(
    tenant:  str,
    user_id: str | None = None,
    bucket:  str | None = None,
    since:   str | None = None,
    until:   str | None = None,
    days:    int | None = None,
    limit:   int = Query(200, le=1000),
    offset:  int = 0,
):
    sql = ["SELECT * FROM events WHERE tenant_id = %s"]
    args: list = [tenant]
    if user_id:
        sql.append("AND user_id = %s"); args.append(user_id)
    sql.append("AND bucket = %s"); args.append(bucket or "window")
    if days:
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(days))).isoformat()
    if since:
        sql.append("AND ts >= %s"); args.append(since)
    if until:
        sql.append("AND ts <= %s"); args.append(until)
    sql.append("ORDER BY ts DESC LIMIT %s OFFSET %s")
    args.extend([limit, offset])

    with db() as c:
        rows = c.execute(" ".join(sql), args).fetchall()
        rules = [dict(r) for r in c.execute(
            "SELECT * FROM productivity_rules WHERE tenant_id=%s ORDER BY created_at",
            (tenant,),
        ).fetchall()]

    events = []
    for r in rows:
        data     = r["data_json"] if isinstance(r["data_json"], dict) else json.loads(r["data_json"])
        app_name = data.get("app", "")
        title    = _clean_title(data.get("title", ""))
        category = _apply_rules(title, app_name, rules) if r["bucket"] == "window" else "neutral"
        events.append({**dict(r), "data": data, "category": category})

    return {"count": len(events), "events": events}


@app.get("/v1/productivity/events_summary", dependencies=[Depends(require_tenant_scope)])
def productivity_events_summary(
    tenant:  str,
    days:    int = Query(7, le=90),
    user_id: str | None = None,
    bucket:  str | None = None,
):
    _bucket   = bucket or "window"
    cache_key = f"{tenant}:events_summary:{days}:{user_id or ''}:{_bucket}"
    cached    = _cache_get(cache_key)
    if cached is not None:
        return cached

    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()

    with db() as c:
        rules = [dict(r) for r in c.execute(
            "SELECT * FROM productivity_rules WHERE tenant_id=%s ORDER BY created_at",
            (tenant,),
        ).fetchall()]

        agg_sql  = ("SELECT data_json->>'app' AS app, data_json->>'title' AS title,"
                    " SUM(duration_sec) AS total_sec"
                    " FROM events WHERE tenant_id=%s AND bucket=%s AND ts>=%s")
        agg_args: list = [tenant, _bucket, since]
        if user_id:
            agg_sql += " AND user_id=%s"; agg_args.append(user_id)
        agg_sql += " GROUP BY app, title"
        agg_rows = c.execute(agg_sql, agg_args).fetchall()

        day_sql  = ("SELECT substr(ts,1,10) AS day, data_json->>'app' AS app,"
                    " data_json->>'title' AS title, SUM(duration_sec) AS total_sec"
                    " FROM events WHERE tenant_id=%s AND bucket=%s AND ts>=%s")
        day_args: list = [tenant, _bucket, since]
        if user_id:
            day_sql += " AND user_id=%s"; day_args.append(user_id)
        day_sql += " GROUP BY day, app, title ORDER BY day"
        day_raw = c.execute(day_sql, day_args).fetchall()

    totals: dict[str, float] = {
        "productive": 0.0, "unproductive": 0.0,
        "neutral": 0.0, "uncategorized": 0.0, "total": 0.0,
    }
    for row in agg_rows:
        app_name = row["app"] or ""
        title    = _clean_title(row["title"] or "")
        cat      = _apply_rules(title, app_name, rules)
        sec      = row["total_sec"] or 0.0
        totals[cat]    = totals.get(cat, 0.0) + sec
        totals["total"] += sec

    daily_agg: dict[str, dict[str, float]] = {}
    for row in day_raw:
        day      = row["day"]
        app_name = row["app"] or ""
        title    = _clean_title(row["title"] or "")
        cat      = _apply_rules(title, app_name, rules)
        if day not in daily_agg:
            daily_agg[day] = {"productive": 0.0, "unproductive": 0.0,
                               "neutral": 0.0, "uncategorized": 0.0}
        daily_agg[day][cat] = daily_agg[day].get(cat, 0.0) + (row["total_sec"] or 0.0)

    daily = [
        {"day": day,
         "productive":    round(v.get("productive",    0.0), 1),
         "unproductive":  round(v.get("unproductive",  0.0), 1),
         "neutral":       round(v.get("neutral",       0.0), 1),
         "uncategorized": round(v.get("uncategorized", 0.0), 1)}
        for day, v in sorted(daily_agg.items())
    ]

    result = {
        "tenant": tenant, "since": since, "days": days,
        "totals": {k: round(v, 1) for k, v in totals.items()},
        "daily":  daily,
    }
    _cache_set(cache_key, result)
    return result


# ===========================================================================
# Workforce dashboard
# ===========================================================================

@app.get("/v1/workforce/overview", dependencies=[Depends(require_tenant_scope)])
def workforce_overview(tenant: str, days: int = 7):
    cache_key = f"{tenant}:overview:{days}"
    cached    = _cache_get(cache_key)
    if cached is not None:
        return cached

    now              = dt.datetime.now(dt.timezone.utc)
    since            = (now - dt.timedelta(days=days)).isoformat()
    today_start      = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    online_threshold = (now - dt.timedelta(minutes=10)).isoformat()

    with db() as c:
        totals = c.execute(
            """SELECT
                   COUNT(DISTINCT user_id) AS total_users,
                   SUM(CASE WHEN bucket='window' THEN duration_sec END) AS active,
                   SUM(CASE WHEN bucket='afk' AND data_json->>'status'='afk'
                            THEN duration_sec END) AS afk,
                   SUM(CASE WHEN bucket='window' AND ts>=%s THEN duration_sec END) AS today_active
               FROM events WHERE tenant_id=%s AND ts>=%s""",
            (today_start, tenant, since),
        ).fetchone()
        today_active = totals["today_active"] or 0

        online = c.execute(
            "SELECT COUNT(*) AS n FROM users WHERE tenant_id=%s AND last_seen>=%s",
            (tenant, online_threshold),
        ).fetchone()["n"]

        hourly = c.execute(
            """SELECT CAST(substr(ts,12,2) AS INTEGER) AS hour, SUM(duration_sec) AS seconds
               FROM events WHERE tenant_id=%s AND ts>=%s AND bucket='window'
               GROUP BY hour ORDER BY hour""",
            (tenant, since),
        ).fetchall()

        daily = c.execute(
            """SELECT substr(ts,1,10) AS day,
                      SUM(CASE WHEN bucket='window' THEN duration_sec ELSE 0 END) AS active,
                      SUM(CASE WHEN bucket='afk' AND data_json->>'status'='afk'
                               THEN duration_sec ELSE 0 END) AS afk,
                      COUNT(DISTINCT user_id) AS users
               FROM events WHERE tenant_id=%s AND ts>=%s
               GROUP BY day ORDER BY day""",
            (tenant, since),
        ).fetchall()

        by_dept = c.execute(
            """SELECT COALESCE(u.department, 'Sem departamento') AS dept,
                      COUNT(DISTINCT u.user_id) AS users,
                      COALESCE(SUM(CASE WHEN e.bucket='window' THEN e.duration_sec END), 0) AS active
               FROM users u
               LEFT JOIN events e ON e.tenant_id=u.tenant_id AND e.user_id=u.user_id AND e.ts>=%s
               WHERE u.tenant_id=%s
               GROUP BY dept ORDER BY active DESC""",
            (since, tenant),
        ).fetchall()

        risk = c.execute(
            """SELECT u.user_id, u.display_name,
                      COALESCE(e.total_sec, 0) AS total_active
               FROM users u
               LEFT JOIN (
                   SELECT user_id, SUM(duration_sec) AS total_sec
                   FROM events WHERE tenant_id=%s AND ts>=%s AND bucket='window'
                   GROUP BY user_id
               ) e ON e.user_id = u.user_id
               WHERE u.tenant_id=%s""",
            (tenant, since, tenant),
        ).fetchall()

    overwork, underwork = [], []
    for r in risk:
        total_h = (r["total_active"] or 0) / 3600
        avg_h   = total_h / max(days, 1)
        if avg_h > 10:
            overwork.append({"user_id": r["user_id"], "display_name": r["display_name"],
                             "max_day_hours": round(avg_h, 1)})
        if 0 < avg_h < 2:
            underwork.append({"user_id": r["user_id"], "display_name": r["display_name"],
                              "avg_day_hours": round(avg_h, 1)})

    result = {
        "tenant": tenant, "since": since, "days": days,
        "totals": {
            "total_users":          totals["total_users"]  or 0,
            "online_now":           online,
            "active_seconds":       totals["active"]       or 0,
            "afk_seconds":          totals["afk"]          or 0,
            "today_active_seconds": today_active,
        },
        "hourly":        [{"hour": r["hour"], "seconds": r["seconds"]} for r in hourly],
        "daily":         [dict(r) for r in daily],
        "by_department": [dict(r) for r in by_dept],
        "alerts":        {"overwork": overwork[:10], "underwork": underwork[:10]},
    }
    _cache_set(cache_key, result)
    return result


@app.get("/v1/workforce/timeseries", dependencies=[Depends(require_tenant_scope)])
def workforce_timeseries(tenant: str, days: int = 30, bucket: str = "window"):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    with db() as c:
        rows = c.execute(
            """SELECT substr(ts,1,10) AS day, SUM(duration_sec) AS seconds
               FROM events WHERE tenant_id=%s AND ts>=%s AND bucket=%s
               GROUP BY day ORDER BY day""",
            (tenant, since, bucket),
        ).fetchall()
    return {"tenant": tenant, "bucket": bucket, "series": [dict(r) for r in rows]}


# ===========================================================================
# Regras de produtividade
# ===========================================================================

@app.get("/v1/productivity/rules", dependencies=[Depends(require_tenant_scope)])
def list_productivity_rules(tenant: str):
    with db() as c:
        rows = c.execute(
            "SELECT * FROM productivity_rules WHERE tenant_id=%s ORDER BY created_at",
            (tenant,),
        ).fetchall()
    return {"rules": [dict(r) for r in rows]}


@app.post("/v1/productivity/rules", dependencies=[Depends(require_tenant_scope)])
def create_productivity_rule(tenant: str, rule: ProductivityRuleCreate):
    allowed_fields = {"title", "app"}
    allowed_types  = {"contains", "exact", "startswith"}
    allowed_cats   = {"productive", "unproductive", "neutral"}
    if rule.match_field not in allowed_fields:
        raise HTTPException(status_code=400, detail=f"match_field must be one of {allowed_fields}")
    if rule.match_type not in allowed_types:
        raise HTTPException(status_code=400, detail=f"match_type must be one of {allowed_types}")
    if rule.category not in allowed_cats:
        raise HTTPException(status_code=400, detail=f"category must be one of {allowed_cats}")

    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    with db() as c:
        row = c.execute(
            """INSERT INTO productivity_rules
                   (tenant_id, label, pattern, match_field, match_type, category, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (tenant, rule.label, rule.pattern, rule.match_field,
             rule.match_type, rule.category, now_iso),
        ).fetchone()
        row_id = row["id"]

    _cache_bust(tenant)
    return {"id": row_id, "tenant_id": tenant, **rule.model_dump(), "created_at": now_iso}


@app.delete("/v1/productivity/rules/{rule_id}", dependencies=[Depends(require_tenant_scope)])
def delete_productivity_rule(rule_id: int, tenant: str):
    with db() as c:
        cur = c.execute(
            "DELETE FROM productivity_rules WHERE id=%s AND tenant_id=%s",
            (rule_id, tenant),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="rule not found")
    _cache_bust(tenant)
    return {"deleted": rule_id}


# ===========================================================================
# Internet / apps
# ===========================================================================

@app.get("/v1/productivity/internet", dependencies=[Depends(require_tenant_scope)])
def productivity_internet(
    tenant:  str,
    days:    int = Query(7, le=90),
    user_id: str | None = None,
):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()

    with db() as c:
        rules = [dict(r) for r in c.execute(
            "SELECT * FROM productivity_rules WHERE tenant_id=%s ORDER BY created_at",
            (tenant,),
        ).fetchall()]

        sql  = ("SELECT user_id, data_json->>'app' AS app, data_json->>'title' AS title,"
                " SUM(duration_sec) AS total_sec, COUNT(*) AS events,"
                " substr(MIN(ts),1,10) AS first_day, substr(MAX(ts),1,10) AS last_day"
                " FROM events WHERE tenant_id=%s AND bucket='window' AND ts>=%s")
        args: list = [tenant, since]
        if user_id:
            sql += " AND user_id=%s"; args.append(user_id)
        sql += " GROUP BY user_id, app, title ORDER BY total_sec DESC"
        raw_rows = c.execute(sql, args).fetchall()

        dsql  = ("SELECT substr(ts,1,10) AS day, data_json->>'app' AS app,"
                 " data_json->>'title' AS title, SUM(duration_sec) AS total_sec"
                 " FROM events WHERE tenant_id=%s AND bucket='window' AND ts>=%s")
        dargs: list = [tenant, since]
        if user_id:
            dsql += " AND user_id=%s"; dargs.append(user_id)
        dsql += " GROUP BY day, app, title ORDER BY day"
        daily_raw = c.execute(dsql, dargs).fetchall()

    site_agg: dict[str, dict] = {}
    for row in raw_rows:
        app = row["app"] or ""
        if not _is_browser(app):
            continue
        site_name = _clean_title(row["title"] or "")
        category  = _apply_rules(site_name, app, rules)
        key       = site_name
        if key not in site_agg:
            site_agg[key] = {"site_name": site_name, "category": category,
                              "total_sec": 0.0, "event_count": 0,
                              "users": set(), "per_user": {}}
        entry = site_agg[key]
        entry["total_sec"]   += row["total_sec"] or 0.0
        entry["event_count"] += row["events"] or 0
        entry["users"].add(row["user_id"])
        uid = row["user_id"]
        entry["per_user"][uid] = entry["per_user"].get(uid, 0.0) + (row["total_sec"] or 0.0)

    totals: dict[str, float] = {"productive": 0.0, "unproductive": 0.0,
                                 "neutral": 0.0, "uncategorized": 0.0, "total": 0.0}
    sites = []
    for entry in sorted(site_agg.values(), key=lambda x: x["total_sec"], reverse=True):
        cat = entry["category"]
        totals[cat]     = totals.get(cat, 0.0) + entry["total_sec"]
        totals["total"] += entry["total_sec"]
        sites.append({
            "site_name":   entry["site_name"],
            "category":    cat,
            "total_sec":   round(entry["total_sec"], 1),
            "event_count": entry["event_count"],
            "user_count":  len(entry["users"]),
            "users":       list(entry["users"]),
            "per_user":    {k: round(v, 1) for k, v in entry["per_user"].items()},
        })

    daily_agg: dict[str, dict[str, float]] = {}
    for row in daily_raw:
        app = row["app"] or ""
        if not _is_browser(app):
            continue
        day       = row["day"]
        site_name = _clean_title(row["title"] or "")
        category  = _apply_rules(site_name, app, rules)
        if day not in daily_agg:
            daily_agg[day] = {"productive": 0.0, "unproductive": 0.0,
                               "neutral": 0.0, "uncategorized": 0.0}
        daily_agg[day][category] = daily_agg[day].get(category, 0.0) + (row["total_sec"] or 0.0)

    daily = [
        {"day": day, "productive": round(v.get("productive", 0.0), 1),
         "unproductive": round(v.get("unproductive", 0.0), 1),
         "neutral": round(v.get("neutral", 0.0), 1),
         "uncategorized": round(v.get("uncategorized", 0.0), 1)}
        for day, v in sorted(daily_agg.items())
    ]

    return {
        "tenant": tenant, "since": since, "days": days,
        "totals": {k: round(v, 1) for k, v in totals.items()},
        "sites":  sites[:300],
        "daily":  daily,
    }


@app.get("/v1/productivity/apps", dependencies=[Depends(require_tenant_scope)])
def productivity_apps(tenant: str, days: int = Query(30, le=90)):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    with db() as c:
        rules = [dict(r) for r in c.execute(
            "SELECT * FROM productivity_rules WHERE tenant_id=%s ORDER BY created_at",
            (tenant,),
        ).fetchall()]
        rows = c.execute(
            """SELECT data_json->>'app' AS app,
                      SUM(duration_sec) AS total_sec,
                      COUNT(DISTINCT user_id) AS user_count
               FROM events WHERE tenant_id=%s AND bucket='window' AND ts>=%s
               GROUP BY app ORDER BY total_sec DESC LIMIT 150""",
            (tenant, since),
        ).fetchall()

    apps = []
    for row in rows:
        app      = row["app"] or "(desconhecido)"
        category = _apply_rules("", app, rules)
        apps.append({
            "app":        app,
            "is_browser": _is_browser(app),
            "category":   category,
            "total_sec":  round(row["total_sec"] or 0.0, 1),
            "user_count": row["user_count"] or 0,
        })
    return {"tenant": tenant, "since": since, "apps": apps}


# ===========================================================================
# Departamentos
# ===========================================================================

@app.get("/v1/departments", dependencies=[Depends(require_tenant_scope)])
def list_departments(tenant: str):
    with db() as c:
        depts  = c.execute(
            "SELECT id, name, created_at FROM tenant_departments WHERE tenant_id=%s ORDER BY name",
            (tenant,),
        ).fetchall()
        result = []
        for d in depts:
            count = c.execute(
                "SELECT COUNT(*) AS n FROM users WHERE tenant_id=%s AND department=%s",
                (tenant, d["name"]),
            ).fetchone()["n"]
            users = c.execute(
                """SELECT user_id, display_name, login_user, last_seen
                   FROM users WHERE tenant_id=%s AND department=%s ORDER BY last_seen DESC""",
                (tenant, d["name"]),
            ).fetchall()
            result.append({
                "id":         d["id"],
                "name":       d["name"],
                "created_at": d["created_at"],
                "user_count": count,
                "users":      [dict(u) for u in users],
            })
    return {"departments": result}


@app.post("/v1/departments", dependencies=[Depends(require_tenant_scope)])
def create_department(body: DepartmentCreate, tenant: str, request: Request):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name required")
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    with db() as c:
        try:
            c.execute(
                "INSERT INTO tenant_departments (tenant_id, name, created_at) VALUES (%s, %s, %s)",
                (tenant, name, now_iso),
            )
        except psycopg2.errors.UniqueViolation:
            raise HTTPException(status_code=409, detail="department already exists")
    audit("admin", "dept.create", target=name, detail=f"tenant={tenant}",
          ip=request.client.host if request.client else None)
    return {"status": "created", "name": name}


@app.delete("/v1/departments/{dept_name}", dependencies=[Depends(require_tenant_scope)])
def delete_department(dept_name: str, tenant: str, request: Request):
    with db() as c:
        cur = c.execute(
            "DELETE FROM tenant_departments WHERE tenant_id=%s AND name=%s",
            (tenant, dept_name),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="department not found")
        c.execute(
            "UPDATE users SET department=NULL WHERE tenant_id=%s AND department=%s",
            (tenant, dept_name),
        )
    _cache_bust(tenant)
    audit("admin", "dept.delete", target=dept_name, detail=f"tenant={tenant}",
          ip=request.client.host if request.client else None)
    return {"status": "deleted", "name": dept_name}


# ===========================================================================
# Admin — plataforma
# ===========================================================================

@app.post("/v1/admin/tenant-token", dependencies=[Depends(require_admin)])
def admin_tenant_token(tenant: str, request: Request):
    new_token = secrets.token_hex(16)
    with db() as c:
        row = c.execute("SELECT id FROM tenants WHERE id = %s", (tenant,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="tenant nao encontrado")
        c.execute("UPDATE tenants SET dash_token = %s WHERE id = %s", (new_token, tenant))
    audit("admin", "tenant.token", target=tenant,
          ip=request.client.host if request.client else None)
    return {"tenant_id": tenant, "dash_token": new_token}


@app.get("/v1/admin/tenants", dependencies=[Depends(require_admin)])
def admin_list_tenants():
    with db() as c:
        rows = c.execute(
            """SELECT t.id, t.name, t.plan, t.status, t.created_at, t.retention_days,
                      COUNT(DISTINCT l.lic_id)    AS active_licenses,
                      COUNT(DISTINCT u.user_id)   AS users,
                      COUNT(DISTINCT d.device_id) AS devices,
                      MAX(u.last_seen)            AS last_activity
               FROM tenants t
               LEFT JOIN licenses l ON l.tenant_id = t.id AND l.revoked = 0
               LEFT JOIN users    u ON u.tenant_id = t.id
               LEFT JOIN devices  d ON d.tenant_id = t.id
               GROUP BY t.id, t.name, t.plan, t.status, t.created_at, t.retention_days
               ORDER BY t.created_at DESC""",
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
    now = dt.datetime.now(dt.timezone.utc)
    licenses = []
    for r in rows:
        d = dict(r)
        try:
            exp = dt.datetime.fromisoformat(str(d["expires"]).replace("Z", "+00:00"))
            delta = exp - now
            d["days_remaining"] = delta.days
            d["expired"] = delta.total_seconds() < 0
        except Exception:
            d["days_remaining"] = None
            d["expired"] = False
        licenses.append(d)
    return {"licenses": licenses}


@app.get("/v1/admin/stats", dependencies=[Depends(require_admin)])
def admin_stats():
    now = dt.datetime.now(dt.timezone.utc)
    h24 = (now - dt.timedelta(hours=24)).isoformat()
    d7  = (now - dt.timedelta(days=7)).isoformat()
    with db() as c:
        tenants = c.execute("SELECT COUNT(*) AS n FROM tenants").fetchone()["n"]
        users   = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        devices = c.execute("SELECT COUNT(*) AS n FROM devices").fetchone()["n"]
        events  = c.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        lic_act = c.execute("SELECT COUNT(*) AS n FROM licenses WHERE revoked=0").fetchone()["n"]
        lic_rev = c.execute("SELECT COUNT(*) AS n FROM licenses WHERE revoked=1").fetchone()["n"]
        ev_24h  = c.execute("SELECT COUNT(*) AS n FROM events WHERE ts>=%s", (h24,)).fetchone()["n"]
        ev_7d   = c.execute("SELECT COUNT(*) AS n FROM events WHERE ts>=%s", (d7,)).fetchone()["n"]
        per_hour = c.execute(
            """SELECT substr(ts,1,13) AS hour, COUNT(*) AS events
               FROM events WHERE ts>=%s GROUP BY hour ORDER BY hour""",
            (h24,),
        ).fetchall()
        os_dist = c.execute(
            """SELECT COALESCE(os,'desconhecido') AS os, COUNT(*) AS n
               FROM devices GROUP BY os ORDER BY n DESC""",
        ).fetchall()
        plan_dist = c.execute(
            """SELECT COALESCE(plan,'starter') AS plan, COUNT(*) AS n
               FROM tenants GROUP BY plan ORDER BY n DESC""",
        ).fetchall()
        try:
            db_size = c.execute(
                "SELECT pg_database_size(current_database()) AS n"
            ).fetchone()["n"]
        except Exception:
            db_size = 0
    return {
        "version":      APP_VERSION,
        "server_time":  now.isoformat(),
        "db_size_bytes": db_size or 0,
        "counts": {
            "tenants":          tenants,
            "users":            users,
            "devices":          devices,
            "total_events":     events,
            "active_licenses":  lic_act,
            "revoked_licenses": lic_rev,
        },
        "events_last_24h": ev_24h,
        "events_last_7d":  ev_7d,
        "ingest_per_hour": [{"hour": r["hour"], "events": r["events"]} for r in per_hour],
        "os_distribution":   [dict(r) for r in os_dist],
        "plan_distribution": [dict(r) for r in plan_dist],
    }


@app.get("/v1/admin/audit", dependencies=[Depends(require_admin)])
def admin_audit_log(limit: int = Query(100, le=1000), offset: int = 0):
    with db() as c:
        rows = c.execute(
            "SELECT * FROM audit_log ORDER BY ts DESC LIMIT %s OFFSET %s",
            (limit, offset),
        ).fetchall()
    return {"entries": [dict(r) for r in rows]}


@app.put("/v1/admin/tenants/{tenant_id}/retention", dependencies=[Depends(require_admin)])
def set_tenant_retention(tenant_id: str, days: int = Query(..., ge=1, le=3650),
                         request: Request = None):
    with db() as c:
        cur = c.execute(
            "UPDATE tenants SET retention_days = %s WHERE id = %s",
            (days, tenant_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="tenant not found")
    audit("admin", "tenant.retention", target=tenant_id, detail=f"retention_days={days}",
          ip=request.client.host if request.client else None)
    return {"status": "updated", "tenant_id": tenant_id, "retention_days": days}


@app.get("/v1/admin/data-summary", dependencies=[Depends(require_admin)])
def admin_data_summary(tenant: str | None = None):
    with db() as c:
        if tenant:
            tenants_rows = c.execute(
                "SELECT id, name FROM tenants WHERE id=%s", (tenant,)
            ).fetchall()
        else:
            tenants_rows = c.execute(
                "SELECT id, name FROM tenants ORDER BY name"
            ).fetchall()

        results = []
        for t in tenants_rows:
            tid     = t["id"]
            users   = c.execute("SELECT COUNT(*) AS n FROM users          WHERE tenant_id=%s", (tid,)).fetchone()["n"]
            devices = c.execute("SELECT COUNT(*) AS n FROM devices         WHERE tenant_id=%s", (tid,)).fetchone()["n"]
            events  = c.execute("SELECT COUNT(*) AS n FROM events          WHERE tenant_id=%s", (tid,)).fetchone()["n"]
            sums    = c.execute("SELECT COUNT(*) AS n FROM daily_summaries WHERE tenant_id=%s", (tid,)).fetchone()["n"]
            oldest  = c.execute("SELECT MIN(ts)  AS v FROM events          WHERE tenant_id=%s", (tid,)).fetchone()["v"]
            newest  = c.execute("SELECT MAX(ts)  AS v FROM events          WHERE tenant_id=%s", (tid,)).fetchone()["v"]
            results.append({
                "tenant_id": tid, "tenant_name": t["name"],
                "users": users, "devices": devices,
                "events": events, "daily_summaries": sums,
                "oldest_event": oldest, "newest_event": newest,
            })
    return {"tenants": results}


@app.post("/v1/admin/purge-data", dependencies=[Depends(require_admin)])
def admin_purge_data(request: Request, tenant: str | None = None):
    with db() as c:
        where  = "WHERE tenant_id = %s" if tenant else ""
        args: list = [tenant] if tenant else []
        deleted: dict[str, int] = {}
        for tbl in ["events", "daily_summaries", "devices", "users"]:
            cur = c.execute(f"DELETE FROM {tbl} {where}", args)
            deleted[tbl] = cur.rowcount
        audit(
            actor="admin/purge-data", action="data_purge",
            target=tenant or "ALL", detail=str(deleted),
            ip=request.client.host if request.client else None,
        )

    if tenant:
        _cache_bust(tenant)
    else:
        with _CACHE_LOCK:
            _CACHE.clear()

    return {"status": "ok", "scope": tenant or "ALL", "deleted": deleted}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def serve() -> None:
    import uvicorn
    concurrency = int(os.getenv("WEB_CONCURRENCY", "2"))
    host, _, port_s = LISTEN_ADDR.rpartition(":")
    uvicorn.run(
        "main:app",
        host=host or "0.0.0.0",
        port=int(port_s or 8080),
        workers=concurrency,
    )


if __name__ == "__main__":
    serve()
