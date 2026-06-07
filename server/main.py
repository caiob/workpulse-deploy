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
import email.mime.application
import email.mime.multipart
import email.mime.text
import io
import json
import os
import re as _re
import secrets
import sys
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated, Any, cast

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - fallback se tzdata ausente
    ZoneInfo = None  # type: ignore

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
FROM_EMAIL   = os.getenv("WORKPULSE_FROM_EMAIL", "noreply@workpulse.com.br")
SES_REGION   = os.getenv("WORKPULSE_SES_REGION", "us-east-1")
# Timezone usada para "fechar" a jornada de trabalho por dia local (início/fim/pausas).
# Os eventos sao gravados em UTC; aqui convertemos para o fuso do cliente.
REPORT_TZ    = os.getenv("WORKPULSE_TZ", "America/Sao_Paulo")

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
    # ── Relatórios automáticos ──────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS report_configs (
        id           BIGSERIAL PRIMARY KEY,
        tenant_id    TEXT NOT NULL,
        name         TEXT NOT NULL,
        recipients   TEXT NOT NULL,          -- JSON array of email strings
        frequency    TEXT NOT NULL,          -- 'daily' | 'weekly' | 'monthly'
        sections     TEXT NOT NULL,          -- JSON array: coletas|jornada|tendencias|alertas
        user_filter  TEXT,                   -- JSON array of user_ids or NULL = all
        enabled      BOOLEAN NOT NULL DEFAULT TRUE,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS report_history (
        id           BIGSERIAL PRIMARY KEY,
        config_id    BIGINT NOT NULL REFERENCES report_configs(id) ON DELETE CASCADE,
        tenant_id    TEXT NOT NULL,
        sent_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        recipients   TEXT NOT NULL,          -- JSON array of emails actually sent to
        status       TEXT NOT NULL,          -- 'ok' | 'error'
        error_msg    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_report_configs_tenant ON report_configs(tenant_id)",
    "CREATE INDEX IF NOT EXISTS idx_report_history_config ON report_history(config_id)",
    "CREATE INDEX IF NOT EXISTS idx_report_history_tenant ON report_history(tenant_id, sent_at DESC)",
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


def _since_for_days(days: int) -> str:
    """Retorna o ISO timestamp de início do período.
    Para days=1 (Hoje), usa meia-noite UTC do dia atual.
    Para outros valores usa now - N dias."""
    now = dt.datetime.now(dt.timezone.utc)
    if days == 1:
        return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return (now - dt.timedelta(days=days)).isoformat()


def _no_browser_where() -> tuple[str, list]:
    """SQL fragment + args to exclude browser apps from queries.
    Append the fragment to an existing WHERE clause; extend args list."""
    in_ph   = ", ".join(["%s"] * len(_BROWSER_APPS))
    like_or = " OR ".join(
        "LOWER(data_json->>'app') LIKE %s"
        for _ in _BROWSER_KEYWORDS
    )
    sql  = f" AND LOWER(data_json->>'app') NOT IN ({in_ph}) AND NOT ({like_or})"
    args: list = list(_BROWSER_APPS) + [f"%{kw}%" for kw in _BROWSER_KEYWORDS]
    return sql, args


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
    request: Request,
    tenant: str,
    x_admin_token: Annotated[str | None, Header()] = None,
) -> None:
    # 1) Sessão por cookie (auth v2): usuário do próprio tenant ou Admin WorkPulse.
    sid = request.cookies.get("wp_sess")
    if sid:
        u = _load_session_user(sid)
        if u and (u.get("role") == "wp_admin" or u.get("tenant_id") == tenant):
            return
    # 2) Token master da plataforma (break-glass / compatibilidade).
    if x_admin_token and secrets.compare_digest(x_admin_token, ADMIN_TOKEN):
        return
    # 3) dash_token do tenant (break-glass / compatibilidade).
    if x_admin_token:
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
    task_retention = asyncio.create_task(_retention_worker())
    task_reports   = asyncio.create_task(_reports_worker())
    yield
    task_retention.cancel()
    task_reports.cancel()
    for t in (task_retention, task_reports):
        try:
            await t
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
def user_summary(user_id: str, tenant: str, days: int = 7,
                 since: str | None = None, until: str | None = None):
    if since is None:
        since = _since_for_days(days)
    if until is None:
        until = dt.datetime.now(dt.timezone.utc).isoformat()
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
               FROM events WHERE tenant_id=%s AND user_id=%s AND ts>=%s AND ts<=%s""",
            (tenant, user_id, since, until),
        ).fetchone()

        top_apps = c.execute(
            """SELECT MIN(data_json->>'app') AS app, SUM(duration_sec) AS seconds
               FROM events
               WHERE tenant_id=%s AND user_id=%s AND ts>=%s AND ts<=%s AND bucket='window'
               GROUP BY LOWER(data_json->>'app') ORDER BY seconds DESC LIMIT 10""",
            (tenant, user_id, since, until),
        ).fetchall()

        daily = c.execute(
            """SELECT substr(ts, 1, 10) AS day,
                      SUM(CASE WHEN bucket='window' THEN duration_sec ELSE 0 END) AS active,
                      SUM(CASE WHEN bucket='afk' AND data_json->>'status'='afk'
                               THEN duration_sec ELSE 0 END) AS afk
               FROM events WHERE tenant_id=%s AND user_id=%s AND ts>=%s AND ts<=%s
               GROUP BY day ORDER BY day""",
            (tenant, user_id, since, until),
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
def team_summary(tenant: str, days: int = 7,
                 since: str | None = None, until: str | None = None):
    if since is None:
        since = _since_for_days(days)
    if until is None:
        until = dt.datetime.now(dt.timezone.utc).isoformat()
    cache_key = f"{tenant}:team_summary:{since}:{until}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    with db() as c:
        per_user = c.execute(
            """SELECT u.user_id, u.display_name, u.email, u.department, u.last_seen,
                      COALESCE(SUM(CASE WHEN e.bucket='window' THEN e.duration_sec END), 0) AS active,
                      COALESCE(SUM(CASE WHEN e.bucket='afk' AND e.data_json->>'status'='afk'
                                        THEN e.duration_sec END), 0) AS afk,
                      (SELECT d.agent_version FROM devices d
                       WHERE d.tenant_id = u.tenant_id AND d.user_id = u.user_id
                       ORDER BY d.last_seen DESC LIMIT 1) AS agent_version
               FROM users u LEFT JOIN events e
                    ON e.tenant_id = u.tenant_id AND e.user_id = u.user_id
                    AND e.ts >= %s AND e.ts <= %s
               WHERE u.tenant_id = %s
               GROUP BY u.user_id, u.display_name, u.email, u.department, u.last_seen, u.tenant_id
               ORDER BY active DESC""",
            (since, until, tenant),
        ).fetchall()
        global_top = c.execute(
            """SELECT MIN(data_json->>'app') AS app, SUM(duration_sec) AS seconds
               FROM events
               WHERE tenant_id=%s AND ts>=%s AND ts<=%s AND bucket='window'
               GROUP BY LOWER(data_json->>'app') ORDER BY seconds DESC LIMIT 15""",
            (tenant, since, until),
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
    tenant:     str,
    user_id:    str | None = None,
    bucket:     str | None = None,
    since:      str | None = None,
    until:      str | None = None,
    days:       int | None = None,
    limit:      int = Query(200, le=1000),
    offset:     int = 0,
    no_browser: bool = False,   # quando True, exclui browsers (usado pela aba Aplicações)
    order:      str = Query("desc", pattern="^(asc|desc)$"),
):
    # Filtros base (reutilizados na query de contagem e na query principal)
    where = ["tenant_id = %s"]
    args: list = [tenant]
    if user_id:
        where.append("user_id = %s"); args.append(user_id)
    where.append("bucket = %s"); args.append(bucket or "window")
    if days:
        since = _since_for_days(int(days))
    if since:
        where.append("ts >= %s"); args.append(since)
    if until:
        where.append("ts <= %s"); args.append(until)

    where_clause = " AND ".join(where)
    nb_sql, nb_args = _no_browser_where() if no_browser else ("", [])

    with db() as c:
        total = c.execute(
            f"SELECT COUNT(*) AS n FROM events WHERE {where_clause}{nb_sql}",
            args + nb_args,
        ).fetchone()["n"]

        rows = c.execute(
            f"SELECT * FROM events WHERE {where_clause}{nb_sql} ORDER BY ts {order.upper()} LIMIT %s OFFSET %s",
            args + nb_args + [limit, offset],
        ).fetchall()

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

    return {"count": len(events), "total": total, "offset": offset, "limit": limit, "events": events}


@app.get("/v1/jornada", dependencies=[Depends(require_tenant_scope)])
def jornada(
    tenant:  str,
    user_id: str,
    since:   str | None = None,
    until:   str | None = None,
    days:    int | None = None,
):
    """Jornada de trabalho agregada por DIA LOCAL, calculada no banco.

    Por que existe: a aba Jornada antes baixava os eventos crus e calculava
    início/fim no navegador. Como o agente emite um heartbeat 'window' a cada
    poucos segundos, um único dia ultrapassa o limite de 1000 eventos por
    chamada do /v1/events — e o "corte" do limite fazia o início/fim saírem
    errados (ex.: início às 16:01 em vez de 08:01).

    Aqui usamos MIN/MAX/SUM com ts::timestamptz (comparação cronológica, não
    lexicográfica) e bucketizamos por dia no fuso REPORT_TZ. Sem LIMIT: imune
    ao "corte" e independente do fuso do navegador.
    """
    if days:
        since = _since_for_days(int(days))
    if since is None:
        since = _since_for_days(7)
    if until is None:
        until = dt.datetime.now(dt.timezone.utc).isoformat()

    cache_key = f"{tenant}:jornada:{user_id}:{since}:{until}"
    cached    = _cache_get(cache_key)
    if cached is not None:
        return cached

    # %(tz)s parametriza o fuso; ts::timestamptz garante ordenação cronológica
    # mesmo com formatos mistos ('+00:00', 'Z', com/sem milissegundos).
    sql = """
        WITH w AS (
            SELECT (ts::timestamptz AT TIME ZONE %(tz)s)                       AS lts,
                   (ts::timestamptz AT TIME ZONE %(tz)s)
                       + (duration_sec || ' seconds')::interval               AS lend,
                   duration_sec
            FROM events
            WHERE tenant_id = %(tenant)s AND user_id = %(uid)s AND bucket = 'window'
              AND ts::timestamptz >= %(since)s::timestamptz
              AND ts::timestamptz <  %(until)s::timestamptz
        ),
        win AS (
            SELECT lts::date         AS day,
                   MIN(lts)          AS start_ts,
                   MAX(lend)         AS end_ts,
                   SUM(duration_sec) AS focus_sec
            FROM w GROUP BY lts::date
        ),
        afk AS (
            SELECT (ts::timestamptz AT TIME ZONE %(tz)s)::date AS day,
                   SUM(duration_sec)                           AS pauses_sec
            FROM events
            WHERE tenant_id = %(tenant)s AND user_id = %(uid)s AND bucket = 'afk'
              AND data_json->>'status' = 'afk'
              AND ts::timestamptz >= %(since)s::timestamptz
              AND ts::timestamptz <  %(until)s::timestamptz
            GROUP BY 1
        )
        SELECT to_char(win.day, 'YYYY-MM-DD')        AS day,
               to_char(win.start_ts, 'HH24:MI:SS')   AS start_hm,
               to_char(win.end_ts,   'HH24:MI:SS')   AS end_hm,
               EXTRACT(EPOCH FROM (win.start_ts - win.day::timestamp)) AS start_sec,
               EXTRACT(EPOCH FROM (win.end_ts   - win.day::timestamp)) AS end_sec,
               win.focus_sec                         AS focus_sec,
               COALESCE(afk.pauses_sec, 0)           AS pauses_sec
        FROM win LEFT JOIN afk ON afk.day = win.day
        ORDER BY win.day
    """
    params = {"tz": REPORT_TZ, "tenant": tenant, "uid": user_id,
              "since": since, "until": until}
    with db() as c:
        rows = c.execute(sql, params).fetchall()

    today_local = dt.datetime.now(
        ZoneInfo(REPORT_TZ)
    ).date().isoformat() if ZoneInfo else None

    days_out = []
    for r in rows:
        focus  = float(r["focus_sec"]  or 0.0)
        pauses = float(r["pauses_sec"] or 0.0)
        start_sec = float(r["start_sec"] or 0.0)
        end_sec   = float(r["end_sec"]   or 0.0)
        gross  = max(0.0, end_sec - start_sec)
        days_out.append({
            "day":        r["day"],
            "start":      r["start_hm"][:5],          # HH:MM
            "end":        r["end_hm"][:5],            # HH:MM
            "start_float": round(start_sec / 3600.0, 4),
            "end_float":   round(end_sec   / 3600.0, 4),
            "focus_sec":  round(focus, 1),
            "pauses_sec": round(pauses, 1),
            "gross_sec":  round(gross, 1),
            "net_sec":    round(max(0.0, gross - pauses), 1),
            "is_today":   (r["day"] == today_local),
        })

    result = {"tenant": tenant, "user_id": user_id,
              "since": since, "until": until, "tz": REPORT_TZ, "days": days_out}
    _cache_set(cache_key, result)
    return result


@app.get("/v1/productivity/events_summary", dependencies=[Depends(require_tenant_scope)])
def productivity_events_summary(
    tenant:  str,
    days:    int = Query(7, le=90),
    user_id: str | None = None,
    bucket:  str | None = None,
    since:   str | None = None,
    until:   str | None = None,
):
    if since is None:
        since = _since_for_days(days)
    if until is None:
        until = dt.datetime.now(dt.timezone.utc).isoformat()
    _bucket   = bucket or "window"
    cache_key = f"{tenant}:events_summary:{since}:{until}:{user_id or ''}:{_bucket}"
    cached    = _cache_get(cache_key)
    if cached is not None:
        return cached

    with db() as c:
        rules = [dict(r) for r in c.execute(
            "SELECT * FROM productivity_rules WHERE tenant_id=%s ORDER BY created_at",
            (tenant,),
        ).fetchall()]

        nb_sql, nb_args = _no_browser_where()

        agg_sql  = ("SELECT data_json->>'app' AS app, data_json->>'title' AS title,"
                    " SUM(duration_sec) AS total_sec"
                    " FROM events WHERE tenant_id=%s AND bucket=%s AND ts>=%s AND ts<=%s")
        agg_args: list = [tenant, _bucket, since, until]
        if user_id:
            agg_sql += " AND user_id=%s"; agg_args.append(user_id)
        agg_sql += nb_sql; agg_args += nb_args
        agg_sql += " GROUP BY app, title"
        agg_rows = c.execute(agg_sql, agg_args).fetchall()

        day_sql  = ("SELECT substr(ts,1,13) AS h, data_json->>'app' AS app,"
                    " data_json->>'title' AS title, SUM(duration_sec) AS total_sec"
                    " FROM events WHERE tenant_id=%s AND bucket=%s AND ts>=%s AND ts<=%s")
        day_args: list = [tenant, _bucket, since, until]
        if user_id:
            day_sql += " AND user_id=%s"; day_args.append(user_id)
        day_sql += nb_sql; day_args += nb_args
        day_sql += " GROUP BY h, app, title ORDER BY h"
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
        day      = _utc_hour_to_local_day(row["h"])
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

@app.get("/v1/license/info", dependencies=[Depends(require_tenant_scope)])
def license_info(tenant: str):
    """Retorna informações da licença e do tenant para exibição no menu de conta."""
    with db() as c:
        t    = c.execute("SELECT id, name, plan FROM tenants WHERE id=%s", (tenant,)).fetchone()
        lic  = c.execute(
            "SELECT plan, seats, expires FROM licenses WHERE tenant_id=%s ORDER BY issued DESC LIMIT 1",
            (tenant,),
        ).fetchone()
        used = c.execute(
            "SELECT COUNT(DISTINCT user_id) AS n FROM users WHERE tenant_id=%s", (tenant,)
        ).fetchone()["n"]
    return {
        "tenant_id":   tenant,
        "tenant_name": t["name"]    if t   else tenant,
        "plan":        lic["plan"]  if lic else (t["plan"] if t else "starter"),
        "seats":       lic["seats"] if lic else 0,
        "used_seats":  used,
        "expires":     lic["expires"] if lic else None,
    }


# Offset fixo do fuso para "fechar o dia" nos gráficos diários do painel.
# Brasil (America/Sao_Paulo) = UTC-3, sem horário de verão desde 2019.
DASH_TZ_OFFSET_HOURS = -3


def _utc_hour_to_local_day(h: str) -> str:
    """Converte 'YYYY-MM-DDTHH' (UTC) na data local (BRT) 'YYYY-MM-DD'."""
    base = dt.datetime(int(h[0:4]), int(h[5:7]), int(h[8:10]), int(h[11:13]),
                       tzinfo=dt.timezone.utc) + dt.timedelta(hours=DASH_TZ_OFFSET_HOURS)
    return base.strftime("%Y-%m-%d")


@app.get("/v1/workforce/overview", dependencies=[Depends(require_tenant_scope)])
def workforce_overview(tenant: str, days: int = 7,
                       since: str | None = None, until: str | None = None):
    now = dt.datetime.now(dt.timezone.utc)
    if since is None:
        since = _since_for_days(days)
    if until is None:
        until = now.isoformat()
    cache_key        = f"{tenant}:overview:{since}:{until}"
    cached           = _cache_get(cache_key)
    if cached is not None:
        return cached

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
               FROM events WHERE tenant_id=%s AND ts>=%s AND ts<=%s""",
            (today_start, tenant, since, until),
        ).fetchone()
        today_active = totals["today_active"] or 0

        online = c.execute(
            "SELECT COUNT(*) AS n FROM users WHERE tenant_id=%s AND last_seen>=%s",
            (tenant, online_threshold),
        ).fetchone()["n"]

        hourly = c.execute(
            """SELECT CAST(substr(ts,12,2) AS INTEGER) AS hour, SUM(duration_sec) AS seconds
               FROM events WHERE tenant_id=%s AND ts>=%s AND ts<=%s AND bucket='window'
               GROUP BY hour ORDER BY hour""",
            (tenant, since, until),
        ).fetchall()

        # Agrupa por HORA UTC; o re-bucket para data LOCAL (BRT) é feito em Python
        # (ver DASH_TZ_OFFSET_HOURS) para que cada dia local seja uma única barra.
        # substr é portável (PostgreSQL + harness sqlite).
        _hour_dur = c.execute(
            """SELECT substr(ts,1,13) AS h,
                      SUM(CASE WHEN bucket='window' THEN duration_sec ELSE 0 END) AS active,
                      SUM(CASE WHEN bucket='afk' AND data_json->>'status'='afk'
                               THEN duration_sec ELSE 0 END) AS afk
               FROM events WHERE tenant_id=%s AND ts>=%s AND ts<=%s
               GROUP BY h""",
            (tenant, since, until),
        ).fetchall()
        _hour_usr = c.execute(
            """SELECT DISTINCT substr(ts,1,13) AS h, user_id
               FROM events WHERE tenant_id=%s AND ts>=%s AND ts<=%s""",
            (tenant, since, until),
        ).fetchall()

        by_dept = c.execute(
            """SELECT COALESCE(u.department, 'Sem departamento') AS dept,
                      COUNT(DISTINCT u.user_id) AS users,
                      COALESCE(SUM(CASE WHEN e.bucket='window' THEN e.duration_sec END), 0) AS active
               FROM users u
               LEFT JOIN events e ON e.tenant_id=u.tenant_id AND e.user_id=u.user_id
                    AND e.ts>=%s AND e.ts<=%s
               WHERE u.tenant_id=%s
               GROUP BY dept ORDER BY active DESC""",
            (since, until, tenant),
        ).fetchall()

        risk = c.execute(
            """SELECT u.user_id, u.display_name,
                      COALESCE(e.total_sec, 0) AS total_active
               FROM users u
               LEFT JOIN (
                   SELECT user_id, SUM(duration_sec) AS total_sec
                   FROM events WHERE tenant_id=%s AND ts>=%s AND ts<=%s AND bucket='window'
                   GROUP BY user_id
               ) e ON e.user_id = u.user_id
               WHERE u.tenant_id=%s""",
            (tenant, since, until, tenant),
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

    # Re-bucketiza horas UTC em datas LOCAIS (BRT, offset fixo). 1 barra por dia local.
    _daily_map: dict[str, dict] = {}
    for _r in _hour_dur:
        _d = _utc_hour_to_local_day(_r["h"])
        _e = _daily_map.setdefault(_d, {"day": _d, "active": 0.0, "afk": 0.0, "_users": set()})
        _e["active"] += _r["active"] or 0.0
        _e["afk"]    += _r["afk"] or 0.0
    for _r in _hour_usr:
        _d = _utc_hour_to_local_day(_r["h"])
        _daily_map.setdefault(_d, {"day": _d, "active": 0.0, "afk": 0.0, "_users": set()})["_users"].add(_r["user_id"])
    daily = [{"day": v["day"], "active": v["active"], "afk": v["afk"], "users": len(v["_users"])}
             for v in sorted(_daily_map.values(), key=lambda x: x["day"])]

    # Re-bucketiza horas UTC -> hora LOCAL (BRT) para o heatmap "Quando a equipe trabalha".
    _hourly_local: dict[int, float] = {}
    for _hr in hourly:
        _lh = (int(_hr["hour"]) + DASH_TZ_OFFSET_HOURS) % 24
        _hourly_local[_lh] = _hourly_local.get(_lh, 0.0) + (_hr["seconds"] or 0.0)

    result = {
        "tenant": tenant, "since": since, "days": days,
        "totals": {
            "total_users":          totals["total_users"]  or 0,
            "online_now":           online,
            "active_seconds":       totals["active"]       or 0,
            "afk_seconds":          totals["afk"]          or 0,
            "today_active_seconds": today_active,
        },
        "hourly":        [{"hour": h, "seconds": _hourly_local[h]} for h in sorted(_hourly_local)],
        "daily":         [dict(r) for r in daily],
        "by_department": [dict(r) for r in by_dept],
        "alerts":        {"overwork": overwork[:10], "underwork": underwork[:10]},
    }
    _cache_set(cache_key, result)
    return result


@app.get("/v1/workforce/timeseries", dependencies=[Depends(require_tenant_scope)])
def workforce_timeseries(tenant: str, days: int = 30, bucket: str = "window",
                         since: str | None = None, until: str | None = None):
    if since is None:
        since = _since_for_days(days)
    if until is None:
        until = dt.datetime.now(dt.timezone.utc).isoformat()
    with db() as c:
        rows = c.execute(
            """SELECT substr(ts,1,10) AS day, SUM(duration_sec) AS seconds
               FROM events WHERE tenant_id=%s AND ts>=%s AND ts<=%s AND bucket=%s
               GROUP BY day ORDER BY day""",
            (tenant, since, until, bucket),
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
    since:   str | None = None,
    until:   str | None = None,
):
    if since is None:
        since = _since_for_days(days)
    if until is None:
        until = dt.datetime.now(dt.timezone.utc).isoformat()

    with db() as c:
        rules = [dict(r) for r in c.execute(
            "SELECT * FROM productivity_rules WHERE tenant_id=%s ORDER BY created_at",
            (tenant,),
        ).fetchall()]

        sql  = ("SELECT user_id, data_json->>'app' AS app, data_json->>'title' AS title,"
                " SUM(duration_sec) AS total_sec, COUNT(*) AS events,"
                " MIN(ts) AS first_ts, MAX(ts) AS last_ts"
                " FROM events WHERE tenant_id=%s AND bucket='window' AND ts>=%s AND ts<=%s")
        args: list = [tenant, since, until]
        if user_id:
            sql += " AND user_id=%s"; args.append(user_id)
        sql += " GROUP BY user_id, app, title ORDER BY total_sec DESC"
        raw_rows = c.execute(sql, args).fetchall()

        dsql  = ("SELECT substr(ts,1,13) AS h, data_json->>'app' AS app,"
                 " data_json->>'title' AS title, SUM(duration_sec) AS total_sec"
                 " FROM events WHERE tenant_id=%s AND bucket='window' AND ts>=%s AND ts<=%s")
        dargs: list = [tenant, since, until]
        if user_id:
            dsql += " AND user_id=%s"; dargs.append(user_id)
        dsql += " GROUP BY h, app, title ORDER BY h"
        daily_raw = c.execute(dsql, dargs).fetchall()

    site_agg: dict[str, dict] = {}
    for row in raw_rows:
        app = row["app"] or ""
        if not _is_browser(app):
            continue
        site_name = _clean_title(row["title"] or "")
        category  = _apply_rules(site_name, app, rules)
        key       = site_name.casefold()
        if key not in site_agg:
            site_agg[key] = {"site_name": site_name, "category": category,
                              "total_sec": 0.0, "event_count": 0,
                              "users": set(), "per_user": {},
                              "last_ts": row["last_ts"] or ""}
        entry = site_agg[key]
        entry["total_sec"]   += row["total_sec"] or 0.0
        entry["event_count"] += row["events"] or 0
        entry["users"].add(row["user_id"])
        uid = row["user_id"]
        entry["per_user"][uid] = entry["per_user"].get(uid, 0.0) + (row["total_sec"] or 0.0)
        # Mantém o timestamp mais recente entre todos os registros do site
        if (row["last_ts"] or "") > entry["last_ts"]:
            entry["last_ts"] = row["last_ts"]

    totals: dict[str, float] = {"productive": 0.0, "unproductive": 0.0,
                                 "neutral": 0.0, "uncategorized": 0.0, "total": 0.0}
    sites = []
    for entry in sorted(site_agg.values(), key=lambda x: x["last_ts"] or "", reverse=True):
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
            "last_access": entry["last_ts"],
        })

    daily_agg: dict[str, dict[str, float]] = {}
    for row in daily_raw:
        app = row["app"] or ""
        if not _is_browser(app):
            continue
        day       = _utc_hour_to_local_day(row["h"])
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



# ===========================================================================
# RELATÓRIOS AUTOMÁTICOS
# ===========================================================================

# ---------------------------------------------------------------------------
# Modelos Pydantic
# ---------------------------------------------------------------------------
class ReportConfigIn(BaseModel):
    name:        str
    recipients:  list[str]                          # lista de e-mails
    frequency:   str                                 # daily | weekly | monthly
    sections:    list[str]                           # coletas | jornada | tendencias | alertas
    user_filter: list[str] | None = None            # user_ids ou None = todos
    enabled:     bool = True


class ReportConfigOut(ReportConfigIn):
    id:         int
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# Geração de PDF com reportlab
# ---------------------------------------------------------------------------
def _build_report_pdf(
    tenant_id: str,
    cfg: dict,
    period_label: str,
) -> bytes:
    """Gera o PDF do relatório e devolve bytes."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        Paragraph, Spacer, Table, TableStyle, SimpleDocTemplate, HRFlowable
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    title_style  = ParagraphStyle("wp_title",  parent=styles["Title"],  fontSize=18, spaceAfter=6)
    h2_style     = ParagraphStyle("wp_h2",     parent=styles["Heading2"], spaceBefore=14, spaceAfter=4)
    normal_style = styles["Normal"]
    dim_style    = ParagraphStyle("wp_dim",    parent=normal_style, textColor=colors.gray, fontSize=9)

    story = []

    # Cabeçalho
    story.append(Paragraph("WorkPulse — Relatório Automático", title_style))
    story.append(Paragraph(f"Período: <b>{period_label}</b>", normal_style))
    story.append(Paragraph(f"Relatório: <b>{cfg['name']}</b>", normal_style))
    story.append(Paragraph(
        f"Gerado em: {dt.datetime.utcnow().strftime('%d/%m/%Y %H:%M')} UTC",
        dim_style,
    ))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#3b82f6"), spaceAfter=10))

    sections = json.loads(cfg["sections"]) if isinstance(cfg["sections"], str) else cfg["sections"]
    user_filter = json.loads(cfg["user_filter"]) if cfg.get("user_filter") else None

    # Busca dados do banco
    with db() as c:
        # ── COLETAS POR PESSOA ─────────────────────────────────────────────
        if "coletas" in sections:
            story.append(Paragraph("Coletas por Pessoa", h2_style))
            uid_filter = user_filter or []
            if uid_filter:
                rows = c.execute(
                    """SELECT u.display_name, u.email, u.department,
                              COALESCE(SUM(CASE WHEN e.bucket='window' THEN e.duration_sec END),0) AS active,
                              COALESCE(SUM(CASE WHEN e.bucket='afk' AND e.data_json->>'status'='afk'
                                               THEN e.duration_sec END),0) AS afk
                       FROM users u LEFT JOIN events e
                            ON e.tenant_id=u.tenant_id AND e.user_id=u.user_id
                               AND e.ts >= NOW() - INTERVAL '31 days'
                       WHERE u.tenant_id=%s AND u.user_id=ANY(%s)
                       GROUP BY u.display_name, u.email, u.department
                       ORDER BY active DESC""",
                    (tenant_id, uid_filter),
                ).fetchall()
            else:
                rows = c.execute(
                    """SELECT u.display_name, u.email, u.department,
                              COALESCE(SUM(CASE WHEN e.bucket='window' THEN e.duration_sec END),0) AS active,
                              COALESCE(SUM(CASE WHEN e.bucket='afk' AND e.data_json->>'status'='afk'
                                               THEN e.duration_sec END),0) AS afk
                       FROM users u LEFT JOIN events e
                            ON e.tenant_id=u.tenant_id AND e.user_id=u.user_id
                               AND e.ts >= NOW() - INTERVAL '31 days'
                       WHERE u.tenant_id=%s
                       GROUP BY u.display_name, u.email, u.department
                       ORDER BY active DESC""",
                    (tenant_id,),
                ).fetchall()

            if rows:
                data = [["Colaborador", "Depto", "Ativo (h)", "AFK (h)"]]
                for r in rows:
                    data.append([
                        r["display_name"] or r["email"],
                        r["department"] or "—",
                        f"{r['active']/3600:.1f}",
                        f"{r['afk']/3600:.1f}",
                    ])
                tbl = Table(data, colWidths=[6*cm, 4*cm, 3*cm, 3*cm])
                tbl.setStyle(TableStyle([
                    ("BACKGROUND",   (0, 0), (-1, 0), colors.HexColor("#3b82f6")),
                    ("TEXTCOLOR",    (0, 0), (-1, 0), colors.white),
                    ("FONTNAME",     (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE",     (0, 0), (-1, -1), 8),
                    ("GRID",         (0, 0), (-1, -1), 0.25, colors.lightgrey),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0f4ff")]),
                    ("ALIGN",        (2, 0), (-1, -1), "RIGHT"),
                ]))
                story.append(tbl)
            else:
                story.append(Paragraph("Nenhum dado no período.", dim_style))
            story.append(Spacer(1, 10))

        # ── JORNADA ───────────────────────────────────────────────────────
        if "jornada" in sections:
            story.append(Paragraph("Jornada de Trabalho", h2_style))
            rows = c.execute(
                """SELECT u.display_name, u.email,
                          ds.day,
                          ds.active_sec, ds.afk_sec
                   FROM daily_summaries ds
                   JOIN users u ON u.tenant_id=ds.tenant_id AND u.user_id=ds.user_id
                   WHERE ds.tenant_id=%s
                     AND ds.day >= to_char(NOW() - INTERVAL '30 days', 'YYYY-MM-DD')
                   ORDER BY u.display_name, ds.day""",
                (tenant_id,),
            ).fetchall()
            if rows:
                data = [["Colaborador", "Dia", "Ativo (h)", "AFK (h)"]]
                for r in rows:
                    data.append([
                        r["display_name"] or r["email"],
                        r["day"],
                        f"{r['active_sec']/3600:.1f}",
                        f"{r['afk_sec']/3600:.1f}",
                    ])
                tbl = Table(data, colWidths=[6*cm, 3*cm, 3*cm, 3*cm])
                tbl.setStyle(TableStyle([
                    ("BACKGROUND",   (0, 0), (-1, 0), colors.HexColor("#3b82f6")),
                    ("TEXTCOLOR",    (0, 0), (-1, 0), colors.white),
                    ("FONTNAME",     (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE",     (0, 0), (-1, -1), 8),
                    ("GRID",         (0, 0), (-1, -1), 0.25, colors.lightgrey),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0f4ff")]),
                    ("ALIGN",        (2, 0), (-1, -1), "RIGHT"),
                ]))
                story.append(tbl)
            else:
                story.append(Paragraph("Nenhum dado no período.", dim_style))
            story.append(Spacer(1, 10))

        # ── TENDÊNCIAS ────────────────────────────────────────────────────
        if "tendencias" in sections:
            story.append(Paragraph("Tendências (últimos 14 dias)", h2_style))
            rows = c.execute(
                """SELECT substr(ts,1,10) AS day,
                          SUM(CASE WHEN bucket='window' THEN duration_sec ELSE 0 END) AS active,
                          SUM(CASE WHEN bucket='afk' AND data_json->>'status'='afk'
                                   THEN duration_sec ELSE 0 END) AS afk,
                          COUNT(DISTINCT user_id) AS users
                   FROM events
                   WHERE tenant_id=%s AND ts >= NOW() - INTERVAL '14 days'
                   GROUP BY day ORDER BY day""",
                (tenant_id,),
            ).fetchall()
            if rows:
                data = [["Dia", "Usuários", "Ativo total (h)", "AFK total (h)"]]
                for r in rows:
                    data.append([
                        r["day"], str(r["users"]),
                        f"{r['active']/3600:.1f}",
                        f"{r['afk']/3600:.1f}",
                    ])
                tbl = Table(data, colWidths=[4*cm, 3*cm, 4.5*cm, 4.5*cm])
                tbl.setStyle(TableStyle([
                    ("BACKGROUND",   (0, 0), (-1, 0), colors.HexColor("#3b82f6")),
                    ("TEXTCOLOR",    (0, 0), (-1, 0), colors.white),
                    ("FONTNAME",     (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE",     (0, 0), (-1, -1), 8),
                    ("GRID",         (0, 0), (-1, -1), 0.25, colors.lightgrey),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0f4ff")]),
                    ("ALIGN",        (1, 0), (-1, -1), "RIGHT"),
                ]))
                story.append(tbl)
            else:
                story.append(Paragraph("Nenhum dado no período.", dim_style))
            story.append(Spacer(1, 10))

        # ── ALERTAS ───────────────────────────────────────────────────────
        if "alertas" in sections:
            story.append(Paragraph("Alertas de Produtividade", h2_style))
            # Usuários sem atividade nos últimos 3 dias úteis
            rows = c.execute(
                """SELECT u.display_name, u.email, u.department,
                          u.last_seen
                   FROM users u
                   WHERE u.tenant_id=%s
                     AND (u.last_seen IS NULL
                          OR u.last_seen::timestamptz < NOW() - INTERVAL '72 hours')
                   ORDER BY u.last_seen ASC NULLS FIRST""",
                (tenant_id,),
            ).fetchall()
            if rows:
                data = [["Colaborador", "Depto", "Última atividade"]]
                for r in rows:
                    data.append([
                        r["display_name"] or r["email"],
                        r["department"] or "—",
                        r["last_seen"] or "Nunca",
                    ])
                tbl = Table(data, colWidths=[6*cm, 4*cm, 6*cm])
                tbl.setStyle(TableStyle([
                    ("BACKGROUND",   (0, 0), (-1, 0), colors.HexColor("#ef4444")),
                    ("TEXTCOLOR",    (0, 0), (-1, 0), colors.white),
                    ("FONTNAME",     (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE",     (0, 0), (-1, -1), 8),
                    ("GRID",         (0, 0), (-1, -1), 0.25, colors.lightgrey),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fff0f0")]),
                ]))
                story.append(Paragraph(
                    f"⚠ {len(rows)} colaborador(es) sem atividade nas últimas 72h:", dim_style
                ))
                story.append(tbl)
            else:
                story.append(Paragraph("Nenhum alerta encontrado.", dim_style))
            story.append(Spacer(1, 10))

    story.append(Spacer(1, 20))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey))
    story.append(Paragraph(
        "WorkPulse — Monitoramento de Força de Trabalho | workpulse.com.br",
        dim_style,
    ))

    doc.build(story)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Envio via AWS SES
# ---------------------------------------------------------------------------
def _send_report_email(
    recipients: list[str],
    subject: str,
    body_html: str,
    pdf_bytes: bytes,
    filename: str,
) -> None:
    import boto3 as _boto3

    msg = email.mime.multipart.MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = FROM_EMAIL
    msg["To"]      = ", ".join(recipients)

    body_part = email.mime.text.MIMEText(body_html, "html", "utf-8")
    msg.attach(body_part)

    attachment = email.mime.application.MIMEApplication(pdf_bytes, Name=filename)
    attachment["Content-Disposition"] = f'attachment; filename="{filename}"'
    msg.attach(attachment)

    client = _boto3.client("ses", region_name=SES_REGION)
    client.send_raw_email(
        Source=FROM_EMAIL,
        Destinations=recipients,
        RawMessage={"Data": msg.as_bytes()},
    )


# ---------------------------------------------------------------------------
# Função central de execução de um relatório
# ---------------------------------------------------------------------------
def _run_report(cfg: dict, tenant_id: str, period_label: str) -> None:
    """Gera o PDF e envia o e-mail para um config de relatório."""
    config_id  = cfg["id"]
    recipients = json.loads(cfg["recipients"]) if isinstance(cfg["recipients"], str) else cfg["recipients"]
    rname      = cfg["name"]

    status, err_msg = "ok", None
    try:
        pdf_bytes = _build_report_pdf(tenant_id, cfg, period_label)
        filename  = f"workpulse_{rname.lower().replace(' ', '_')}_{dt.date.today().isoformat()}.pdf"
        subject   = f"WorkPulse — {rname} ({period_label})"
        body_html = f"""
        <p>Olá,</p>
        <p>Segue em anexo o relatório automático <b>{rname}</b> referente ao período <b>{period_label}</b>.</p>
        <p>Acesse o dashboard para mais detalhes: <a href="https://workpulse.com.br/dashboard/workforce.html">WorkPulse Dashboard</a></p>
        <br><p style="color:#888;font-size:12px">WorkPulse — Monitoramento de Força de Trabalho</p>
        """
        _send_report_email(recipients, subject, body_html, pdf_bytes, filename)
        print(f"[reports] sent '{rname}' → {recipients}", flush=True)
    except Exception as exc:
        status, err_msg = "error", str(exc)
        print(f"[reports] ERROR sending '{rname}': {exc}", flush=True)

    # Grava histórico
    with db() as c:
        c.execute(
            """INSERT INTO report_history (config_id, tenant_id, recipients, status, error_msg)
               VALUES (%s, %s, %s, %s, %s)""",
            (config_id, tenant_id, json.dumps(recipients), status, err_msg),
        )


# ---------------------------------------------------------------------------
# Worker de agendamento (roda diariamente às 10:00 UTC = 07:00 BRT)
# ---------------------------------------------------------------------------
def _do_reports() -> None:
    """Verifica quais configs devem ser enviados hoje e os envia."""
    now_utc  = dt.datetime.utcnow()
    today    = now_utc.date()
    weekday  = today.weekday()   # 0=Mon … 6=Sun
    monthday = today.day

    with db() as c:
        configs = c.execute(
            "SELECT * FROM report_configs WHERE enabled = TRUE"
        ).fetchall()

    for cfg in configs:
        freq = cfg["frequency"]
        should_run = (
            freq == "daily"
            or (freq == "weekly"  and weekday == 0)           # segunda-feira
            or (freq == "monthly" and monthday == 1)          # dia 1 do mês
        )
        if not should_run:
            continue

        if freq == "daily":
            period_label = today.strftime("%d/%m/%Y")
        elif freq == "weekly":
            sunday = today + dt.timedelta(days=6)
            period_label = f"{today.strftime('%d/%m/%Y')} – {sunday.strftime('%d/%m/%Y')}"
        else:
            import calendar
            last_day = calendar.monthrange(today.year, today.month)[1]
            period_label = today.strftime("%B/%Y")

        _run_report(dict(cfg), cfg["tenant_id"], period_label)


async def _reports_worker() -> None:
    """Aguarda até 10:00 UTC do próximo dia e dispara os relatórios."""
    while True:
        now_utc     = dt.datetime.utcnow()
        target      = now_utc.replace(hour=10, minute=0, second=0, microsecond=0)
        if now_utc >= target:
            target += dt.timedelta(days=1)
        sleep_secs  = (target - now_utc).total_seconds()
        print(f"[reports_worker] próximo disparo em {sleep_secs/3600:.1f}h ({target} UTC)", flush=True)
        await asyncio.sleep(sleep_secs)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _do_reports)


# ---------------------------------------------------------------------------
# Endpoints de Relatórios
# ---------------------------------------------------------------------------

@app.get("/v1/reports", dependencies=[Depends(require_tenant_scope)])
def list_reports(tenant: str):
    with db() as c:
        rows = c.execute(
            "SELECT * FROM report_configs WHERE tenant_id=%s ORDER BY id",
            (tenant,),
        ).fetchall()
    return {"reports": [dict(r) for r in rows]}


@app.post("/v1/reports", dependencies=[Depends(require_tenant_scope)])
def create_report(tenant: str, body: ReportConfigIn):
    tid = tenant
    with db() as c:
        row = c.execute(
            """INSERT INTO report_configs
               (tenant_id, name, recipients, frequency, sections, user_filter, enabled)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               RETURNING *""",
            (
                tid, body.name,
                json.dumps(body.recipients),
                body.frequency,
                json.dumps(body.sections),
                json.dumps(body.user_filter) if body.user_filter is not None else None,
                body.enabled,
            ),
        ).fetchone()
    return dict(row)


@app.put("/v1/reports/{cfg_id}", dependencies=[Depends(require_tenant_scope)])
def update_report(cfg_id: int, tenant: str, body: ReportConfigIn):
    tid = tenant
    with db() as c:
        row = c.execute(
            """UPDATE report_configs
               SET name=%s, recipients=%s, frequency=%s, sections=%s,
                   user_filter=%s, enabled=%s, updated_at=NOW()
               WHERE id=%s AND tenant_id=%s
               RETURNING *""",
            (
                body.name,
                json.dumps(body.recipients),
                body.frequency,
                json.dumps(body.sections),
                json.dumps(body.user_filter) if body.user_filter is not None else None,
                body.enabled,
                cfg_id, tid,
            ),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Report config not found")
    return dict(row)


@app.delete("/v1/reports/{cfg_id}", dependencies=[Depends(require_tenant_scope)])
def delete_report(cfg_id: int, tenant: str):
    tid = tenant
    with db() as c:
        result = c.execute(
            "DELETE FROM report_configs WHERE id=%s AND tenant_id=%s",
            (cfg_id, tid),
        )
    if result.rowcount == 0:
        raise HTTPException(404, "Report config not found")
    return {"status": "deleted"}


@app.post("/v1/reports/{cfg_id}/send", dependencies=[Depends(require_tenant_scope)])
def send_report_now(cfg_id: int, tenant: str):
    """Envio imediato (test button)."""
    tid = tenant
    with db() as c:
        row = c.execute(
            "SELECT * FROM report_configs WHERE id=%s AND tenant_id=%s",
            (cfg_id, tid),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Report config not found")
    cfg  = dict(row)
    t = threading.Thread(
        target=_run_report,
        args=(cfg, tid, dt.date.today().strftime("%d/%m/%Y")),
        daemon=True,
    )
    t.start()
    return {"status": "queued"}


@app.get("/v1/reports/{cfg_id}/history", dependencies=[Depends(require_tenant_scope)])
def report_history(cfg_id: int, tenant: str):
    tid = tenant
    with db() as c:
        rows = c.execute(
            """SELECT * FROM report_history
               WHERE config_id=%s AND tenant_id=%s
               ORDER BY sent_at DESC LIMIT 50""",
            (cfg_id, tid),
        ).fetchall()
    return {"history": [dict(r) for r in rows]}


# ===========================================================================
# AUTH v2 — Login por e-mail/senha, perfis e gestão de acessos
# (bloco aditivo; ver PLANO_AUTH_EMAIL_SENHA_V2.md)
# ===========================================================================
import hashlib as _hashlib
from fastapi import Response, Cookie
from argon2 import PasswordHasher as _Argon2PH

# --- Configuração de auth (envs com defaults seguros) ----------------------
SESSION_COOKIE     = "wp_sess"
SESSION_TTL_HOURS  = int(os.getenv("WORKPULSE_SESSION_TTL_HOURS", "12"))
LOCKOUT_MAX        = int(os.getenv("WORKPULSE_LOCKOUT_MAX", "5"))
LOCKOUT_MINUTES    = int(os.getenv("WORKPULSE_LOCKOUT_MINUTES", "15"))
COOKIE_SECURE      = os.getenv("WORKPULSE_COOKIE_SECURE", "true").lower() != "false"
COOKIE_SAMESITE    = os.getenv("WORKPULSE_COOKIE_SAMESITE", "strict")
PANEL_BASE_URL     = os.getenv("WORKPULSE_PANEL_URL", "https://workpulse.com.br/dashboard")
PLATFORM_NOTIFY    = os.getenv("WORKPULSE_NOTIFY_EMAIL", FROM_EMAIL)
TOKEN_TTL_HOURS    = int(os.getenv("WORKPULSE_PWTOKEN_TTL_HOURS", "48"))

CLIENT_ROLES   = ("admin_master", "supervisor")
ALL_ROLES      = ("admin_master", "supervisor", "wp_admin")

# --- Tabelas novas (aditivas; init_db() as cria se não existirem) ----------
_SCHEMA_STMTS.extend([
    """
    CREATE TABLE IF NOT EXISTS dash_users (
        id             BIGSERIAL PRIMARY KEY,
        tenant_id      TEXT REFERENCES tenants(id) ON DELETE CASCADE,
        email          TEXT NOT NULL,
        password_hash  TEXT NOT NULL,
        display_name   TEXT,
        role           TEXT NOT NULL DEFAULT 'supervisor'
                       CHECK (role IN ('admin_master','supervisor','wp_admin')),
        status         TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active','disabled')),
        must_change_pw BOOLEAN NOT NULL DEFAULT TRUE,
        twofa_secret   TEXT,
        twofa_enabled  BOOLEAN NOT NULL DEFAULT FALSE,
        failed_logins  INTEGER NOT NULL DEFAULT 0,
        locked_until   TIMESTAMPTZ,
        last_login     TIMESTAMPTZ,
        created_by     BIGINT,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_dash_users_email ON dash_users (lower(email))",
    "CREATE INDEX IF NOT EXISTS idx_dash_users_tenant ON dash_users(tenant_id)",
    """
    CREATE TABLE IF NOT EXISTS dash_sessions (
        id          TEXT PRIMARY KEY,
        user_id     BIGINT NOT NULL REFERENCES dash_users(id) ON DELETE CASCADE,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at  TIMESTAMPTZ NOT NULL,
        ip          TEXT,
        user_agent  TEXT,
        revoked     BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dash_sessions_user ON dash_sessions(user_id)",
    """
    CREATE TABLE IF NOT EXISTS dash_user_tokens (
        token_hash  TEXT PRIMARY KEY,
        user_id     BIGINT NOT NULL REFERENCES dash_users(id) ON DELETE CASCADE,
        purpose     TEXT NOT NULL CHECK (purpose IN ('set_password','reset_password')),
        expires_at  TIMESTAMPTZ NOT NULL,
        used_at     TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dash_seat_alloc (
        tenant_id   TEXT NOT NULL,
        user_id     TEXT NOT NULL,
        allocated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (tenant_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dash_license_requests (
        id          BIGSERIAL PRIMARY KEY,
        tenant_id   TEXT NOT NULL,
        requested_by TEXT,
        plan        TEXT,
        seats       INTEGER,
        message     TEXT,
        status      TEXT NOT NULL DEFAULT 'open',
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
])

# --- Hash de senha (argon2id) ----------------------------------------------
_ph = _Argon2PH()

def hash_password(pw: str) -> str:
    return _ph.hash(pw)

def verify_password(stored_hash: str, pw: str) -> bool:
    try:
        _ph.verify(stored_hash, pw)
        return True
    except Exception:
        return False

def validate_password_policy(pw: str) -> None:
    if not pw or len(pw) < 10:
        raise HTTPException(400, "A senha deve ter ao menos 10 caracteres.")
    classes = sum([
        bool(_re.search(r"[a-z]", pw)),
        bool(_re.search(r"[A-Z]", pw)),
        bool(_re.search(r"[0-9]", pw)),
        bool(_re.search(r"[^A-Za-z0-9]", pw)),
    ])
    if classes < 3:
        raise HTTPException(
            400,
            "A senha deve conter ao menos 3 de 4 tipos: minúscula, maiúscula, número e símbolo.",
        )

# --- Utilitários de tempo/token --------------------------------------------
def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def _parse_ts(v) -> dt.datetime | None:
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)
    try:
        d = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None

def _gen_secret() -> str:
    return secrets.token_urlsafe(32)

def _hash_token(t: str) -> str:
    return _hashlib.sha256(t.encode("utf-8")).hexdigest()

def _to_bool(v) -> bool:
    return v in (True, 1, "t", "true", "TRUE", "1")

# --- E-mail genérico (SES) -------------------------------------------------
def _send_email_html(recipients: list[str], subject: str, body_html: str) -> None:
    import boto3 as _boto3
    msg = email.mime.multipart.MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = FROM_EMAIL
    msg["To"]      = ", ".join(recipients)
    msg.attach(email.mime.text.MIMEText(body_html, "html", "utf-8"))
    client = _boto3.client("ses", region_name=SES_REGION)
    client.send_raw_email(
        Source=FROM_EMAIL,
        Destinations=recipients,
        RawMessage={"Data": msg.as_bytes()},
    )

def _send_password_token(user_row: dict, purpose: str) -> str:
    """Gera token de uso único, envia o link por e-mail (SES) e retorna o link."""
    raw = _gen_secret()
    th  = _hash_token(raw)
    exp = (_now_utc() + dt.timedelta(hours=TOKEN_TTL_HOURS)).isoformat()
    with db() as c:
        c.execute(
            "DELETE FROM dash_user_tokens WHERE user_id=%s AND purpose=%s",
            (user_row["id"], purpose),
        )
        c.execute(
            """INSERT INTO dash_user_tokens (token_hash, user_id, purpose, expires_at)
               VALUES (%s,%s,%s,%s)""",
            (th, user_row["id"], purpose, exp),
        )
    link = f"{PANEL_BASE_URL}/?action=set-password&token={raw}"
    acao = "definir sua senha de acesso" if purpose == "set_password" else "redefinir sua senha"
    html = (
        f"<p>Olá {user_row.get('display_name') or ''},</p>"
        f"<p>Use o link abaixo para {acao} no Painel WorkPulse "
        f"(válido por {TOKEN_TTL_HOURS} horas):</p>"
        f'<p><a href="{link}">{link}</a></p>'
        f"<p>Se você não solicitou, ignore este e-mail.</p>"
    )
    try:
        _send_email_html([user_row["email"]], "WorkPulse — acesso ao painel", html)
    except Exception as exc:
        print(f"[auth] AVISO: falha ao enviar e-mail de senha: {exc}", flush=True)
    return link

# --- Sessões ---------------------------------------------------------------
def _load_session_user(sid: str) -> dict | None:
    if not sid:
        return None
    with db() as c:
        row = c.execute(
            """SELECT u.id, u.tenant_id, u.email, u.display_name, u.role, u.status,
                      s.expires_at AS s_exp, s.revoked AS s_revoked
               FROM dash_sessions s JOIN dash_users u ON u.id = s.user_id
               WHERE s.id = %s""",
            (sid,),
        ).fetchone()
    if not row:
        return None
    r = dict(row)
    if _to_bool(r.get("s_revoked")):
        return None
    exp = _parse_ts(r.get("s_exp"))
    if exp and exp < _now_utc():
        return None
    if r.get("status") != "active":
        return None
    return r

def _create_session(user_id, ip: str | None, ua: str | None) -> str:
    sid = _gen_secret()
    exp = (_now_utc() + dt.timedelta(hours=SESSION_TTL_HOURS)).isoformat()
    with db() as c:
        c.execute(
            """INSERT INTO dash_sessions (id, user_id, created_at, expires_at, ip, user_agent, revoked)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (sid, user_id, _now_utc().isoformat(), exp, ip, (ua or "")[:300], False),
        )
    return sid

def _revoke_user_sessions(user_id) -> None:
    with db() as c:
        c.execute("UPDATE dash_sessions SET revoked=%s WHERE user_id=%s", (True, user_id))

def _set_session_cookie(response: Response, sid: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE, value=sid, max_age=SESSION_TTL_HOURS * 3600,
        httponly=True, secure=COOKIE_SECURE, samesite=COOKIE_SAMESITE, path="/",
    )

def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE, path="/")

# --- Contexto de autenticação / autorização --------------------------------
def _session_ctx(request: Request, x_admin_token: str | None, tenant: str | None) -> dict | None:
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        u = _load_session_user(sid)
        if u:
            return {
                "user_id": u["id"], "email": u["email"], "role": u["role"],
                "tenant": u["tenant_id"], "display_name": u.get("display_name"),
                "via": "session",
            }
    if x_admin_token:
        if secrets.compare_digest(x_admin_token, ADMIN_TOKEN):
            return {"user_id": None, "email": "platform@token", "role": "wp_admin",
                    "tenant": tenant, "display_name": "Admin WorkPulse (token)", "via": "master_token"}
        if tenant:
            with db() as c:
                row = c.execute("SELECT dash_token FROM tenants WHERE id=%s", (tenant,)).fetchone()
            if row and row["dash_token"] and secrets.compare_digest(x_admin_token, row["dash_token"]):
                return {"user_id": None, "email": "tenant@token", "role": "admin_master",
                        "tenant": tenant, "display_name": "Admin Master (token)", "via": "tenant_token"}
    return None

def require_session(
    request: Request,
    x_admin_token: Annotated[str | None, Header()] = None,
    tenant: str | None = None,
) -> dict:
    ctx = _session_ctx(request, x_admin_token, tenant)
    if not ctx:
        raise HTTPException(401, "não autenticado")
    if ctx["via"] == "session" and ctx["role"] != "wp_admin" and tenant and ctx["tenant"] != tenant:
        raise HTTPException(403, "tenant fora do escopo da sessão")
    return ctx

def require_role(*roles: str):
    allowed = set(roles)
    def _dep(ctx: Annotated[dict, Depends(require_session)]) -> dict:
        role = ctx["role"]
        if role == "wp_admin":
            return ctx
        if role in allowed:
            return ctx
        raise HTTPException(403, "permissão insuficiente para esta ação")
    return _dep

def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None

def _user_public(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "tenant_id": row.get("tenant_id"),
        "email": row.get("email"),
        "display_name": row.get("display_name"),
        "role": row.get("role"),
        "status": row.get("status"),
        "must_change_pw": _to_bool(row.get("must_change_pw")),
        "last_login": row.get("last_login"),
        "created_at": row.get("created_at"),
    }

# --- Modelos ---------------------------------------------------------------
class LoginIn(BaseModel):
    email: str
    senha: str

class ChangePasswordIn(BaseModel):
    senha_atual: str
    senha_nova: str

class SetPasswordIn(BaseModel):
    token: str
    senha: str

class ForgotIn(BaseModel):
    email: str

class TenantUserCreate(BaseModel):
    email: str
    display_name: str | None = None
    role: str = "supervisor"

class TenantUserUpdate(BaseModel):
    display_name: str | None = None
    role: str | None = None
    status: str | None = None

class AdminUserCreate(BaseModel):
    tenant_id: str | None = None
    email: str
    display_name: str | None = None
    role: str = "admin_master"

class SeatAssignIn(BaseModel):
    user_id: str
    allocated: bool

class LicenseRequestIn(BaseModel):
    plan: str | None = None
    seats: int | None = None
    message: str | None = None

def _norm_email(e: str) -> str:
    return (e or "").strip().lower()

def _email_taken(email_norm: str, exclude_id=None) -> bool:
    with db() as c:
        if exclude_id is None:
            row = c.execute("SELECT id FROM dash_users WHERE lower(email)=%s", (email_norm,)).fetchone()
        else:
            row = c.execute(
                "SELECT id FROM dash_users WHERE lower(email)=%s AND id<>%s",
                (email_norm, exclude_id),
            ).fetchone()
    return row is not None

# ===========================================================================
# Endpoints — Autenticação
# ===========================================================================
@app.post("/v1/auth/login")
def auth_login(body: LoginIn, request: Request, response: Response):
    email_norm = _norm_email(body.email)
    ip = _client_ip(request)
    with db() as c:
        row = c.execute(
            "SELECT * FROM dash_users WHERE lower(email)=%s", (email_norm,)
        ).fetchone()
    generic = HTTPException(401, "e-mail ou senha inválidos")
    if not row:
        audit("anon", "login.fail", target=email_norm, detail="user_not_found", ip=ip)
        raise generic
    user = dict(row)
    if user.get("status") != "active":
        audit(user["email"], "login.fail", detail="disabled", ip=ip)
        raise HTTPException(403, "conta desativada — contate o administrador")
    locked = _parse_ts(user.get("locked_until"))
    if locked and locked > _now_utc():
        raise HTTPException(423, "conta temporariamente bloqueada — tente novamente em alguns minutos")
    if not verify_password(user["password_hash"], body.senha):
        failed = int(user.get("failed_logins") or 0) + 1
        lock_until = None
        if failed >= LOCKOUT_MAX:
            lock_until = (_now_utc() + dt.timedelta(minutes=LOCKOUT_MINUTES)).isoformat()
        with db() as c:
            c.execute(
                "UPDATE dash_users SET failed_logins=%s, locked_until=%s, updated_at=%s WHERE id=%s",
                (failed, lock_until, _now_utc().isoformat(), user["id"]),
            )
        audit(user["email"], "login.fail", detail=f"bad_password({failed})", ip=ip)
        raise generic
    # sucesso
    with db() as c:
        c.execute(
            "UPDATE dash_users SET failed_logins=0, locked_until=NULL, last_login=%s, updated_at=%s WHERE id=%s",
            (_now_utc().isoformat(), _now_utc().isoformat(), user["id"]),
        )
    sid = _create_session(user["id"], ip, request.headers.get("user-agent"))
    _set_session_cookie(response, sid)
    audit(user["email"], "login.ok", detail=f"tenant={user.get('tenant_id')}", ip=ip)
    return {
        "ok": True,
        "role": user["role"],
        "tenant": user.get("tenant_id"),
        "display_name": user.get("display_name"),
        "must_change_pw": _to_bool(user.get("must_change_pw")),
    }

@app.post("/v1/auth/logout")
def auth_logout(request: Request, response: Response):
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        with db() as c:
            c.execute("UPDATE dash_sessions SET revoked=%s WHERE id=%s", (True, sid))
    _clear_session_cookie(response)
    return {"ok": True}

@app.get("/v1/auth/me")
def auth_me(ctx: Annotated[dict, Depends(require_session)]):
    return {
        "email": ctx["email"],
        "role": ctx["role"],
        "tenant": ctx["tenant"],
        "display_name": ctx["display_name"],
        "via": ctx["via"],
    }

@app.post("/v1/auth/password")
def auth_change_password(body: ChangePasswordIn, request: Request,
                         ctx: Annotated[dict, Depends(require_session)]):
    if ctx["via"] != "session" or not ctx.get("user_id"):
        raise HTTPException(400, "troca de senha indisponível para acesso por token")
    with db() as c:
        row = c.execute("SELECT * FROM dash_users WHERE id=%s", (ctx["user_id"],)).fetchone()
    if not row or not verify_password(row["password_hash"], body.senha_atual):
        raise HTTPException(403, "senha atual incorreta")
    validate_password_policy(body.senha_nova)
    with db() as c:
        c.execute(
            "UPDATE dash_users SET password_hash=%s, must_change_pw=%s, updated_at=%s WHERE id=%s",
            (hash_password(body.senha_nova), False, _now_utc().isoformat(), ctx["user_id"]),
        )
    audit(ctx["email"], "password.change", ip=_client_ip(request))
    return {"ok": True}

@app.post("/v1/auth/set-password")
def auth_set_password(body: SetPasswordIn, request: Request, response: Response):
    validate_password_policy(body.senha)
    th = _hash_token(body.token)
    with db() as c:
        tok = c.execute(
            "SELECT * FROM dash_user_tokens WHERE token_hash=%s", (th,)
        ).fetchone()
    if not tok:
        raise HTTPException(400, "token inválido")
    tok = dict(tok)
    if tok.get("used_at"):
        raise HTTPException(400, "token já utilizado")
    exp = _parse_ts(tok.get("expires_at"))
    if exp and exp < _now_utc():
        raise HTTPException(400, "token expirado")
    with db() as c:
        c.execute(
            "UPDATE dash_users SET password_hash=%s, must_change_pw=%s, failed_logins=0, locked_until=NULL, updated_at=%s WHERE id=%s",
            (hash_password(body.senha), False, _now_utc().isoformat(), tok["user_id"]),
        )
        c.execute(
            "UPDATE dash_user_tokens SET used_at=%s WHERE token_hash=%s",
            (_now_utc().isoformat(), th),
        )
        urow = c.execute("SELECT * FROM dash_users WHERE id=%s", (tok["user_id"],)).fetchone()
    _revoke_user_sessions(tok["user_id"])
    audit(urow["email"] if urow else str(tok["user_id"]), "password.set", ip=_client_ip(request))
    return {"ok": True}

@app.post("/v1/auth/forgot")
def auth_forgot(body: ForgotIn, request: Request):
    email_norm = _norm_email(body.email)
    with db() as c:
        row = c.execute(
            "SELECT * FROM dash_users WHERE lower(email)=%s AND status='active'", (email_norm,)
        ).fetchone()
    if row:
        _send_password_token(dict(row), "reset_password")
        audit(email_norm, "password.forgot", ip=_client_ip(request))
    # Resposta sempre 200 — não revela existência da conta
    return {"ok": True}

# ===========================================================================
# Endpoints — Gestão de logins do tenant (Admin Master do cliente)
# ===========================================================================
@app.get("/v1/tenant-users")
def tenant_users_list(tenant: str, ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    with db() as c:
        rows = c.execute(
            "SELECT * FROM dash_users WHERE tenant_id=%s ORDER BY created_at", (tid,)
        ).fetchall()
    return {"users": [_user_public(dict(r)) for r in rows]}

@app.post("/v1/tenant-users")
def tenant_users_create(body: TenantUserCreate, tenant: str, request: Request,
                        ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    if body.role not in CLIENT_ROLES:
        raise HTTPException(400, "perfil inválido (use admin_master ou supervisor)")
    email_norm = _norm_email(body.email)
    if not email_norm or "@" not in email_norm:
        raise HTTPException(400, "e-mail inválido")
    if _email_taken(email_norm):
        raise HTTPException(409, "e-mail já cadastrado")
    tmp_hash = hash_password(_gen_secret())  # senha aleatória; usuário define via link de e-mail
    with db() as c:
        row = c.execute(
            """INSERT INTO dash_users (tenant_id, email, password_hash, display_name, role, status, must_change_pw, created_by, created_at, updated_at)
               VALUES (%s,%s,%s,%s,%s,'active',%s,%s,%s,%s) RETURNING *""",
            (tid, body.email.strip(), tmp_hash, body.display_name, body.role, True,
             ctx.get("user_id"), _now_utc().isoformat(), _now_utc().isoformat()),
        ).fetchone()
    set_link = _send_password_token(dict(row), "set_password")
    audit(ctx["email"], "tenant_user.create", target=email_norm, detail=f"tenant={tid};role={body.role}", ip=_client_ip(request))
    return {"ok": True, "user": _user_public(dict(row)), "set_password_link": set_link}

def _fetch_tenant_user(uid: int, tid: str, role: str) -> dict:
    with db() as c:
        if role == "wp_admin":
            row = c.execute("SELECT * FROM dash_users WHERE id=%s", (uid,)).fetchone()
        else:
            row = c.execute("SELECT * FROM dash_users WHERE id=%s AND tenant_id=%s", (uid, tid)).fetchone()
    if not row:
        raise HTTPException(404, "usuário não encontrado")
    return dict(row)

@app.put("/v1/tenant-users/{uid}")
def tenant_users_update(uid: int, body: TenantUserUpdate, tenant: str, request: Request,
                        ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    user = _fetch_tenant_user(uid, tid, ctx["role"])
    sets, params = [], []
    if body.display_name is not None:
        sets.append("display_name=%s"); params.append(body.display_name)
    if body.role is not None:
        if body.role not in CLIENT_ROLES:
            raise HTTPException(400, "perfil inválido")
        sets.append("role=%s"); params.append(body.role)
    if body.status is not None:
        if body.status not in ("active", "disabled"):
            raise HTTPException(400, "status inválido")
        sets.append("status=%s"); params.append(body.status)
    if not sets:
        raise HTTPException(400, "nada para atualizar")
    sets.append("updated_at=%s"); params.append(_now_utc().isoformat())
    params.append(uid)
    with db() as c:
        c.execute(f"UPDATE dash_users SET {', '.join(sets)} WHERE id=%s", tuple(params))
    if body.status == "disabled":
        _revoke_user_sessions(uid)
    audit(ctx["email"], "tenant_user.update", target=user["email"], detail=str(body.dict(exclude_none=True)), ip=_client_ip(request))
    return {"ok": True}

@app.post("/v1/tenant-users/{uid}/block")
def tenant_users_block(uid: int, tenant: str, request: Request,
                       ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    user = _fetch_tenant_user(uid, tid, ctx["role"])
    with db() as c:
        c.execute("UPDATE dash_users SET status='disabled', updated_at=%s WHERE id=%s",
                  (_now_utc().isoformat(), uid))
    _revoke_user_sessions(uid)
    audit(ctx["email"], "tenant_user.block", target=user["email"], ip=_client_ip(request))
    return {"ok": True}

@app.post("/v1/tenant-users/{uid}/reset-password")
def tenant_users_reset(uid: int, tenant: str, request: Request,
                       ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    user = _fetch_tenant_user(uid, tid, ctx["role"])
    reset_link = _send_password_token(user, "reset_password")
    audit(ctx["email"], "tenant_user.reset_password", target=user["email"], ip=_client_ip(request))
    return {"ok": True, "reset_password_link": reset_link}

@app.delete("/v1/tenant-users/{uid}")
def tenant_users_delete(uid: int, tenant: str, request: Request,
                        ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    # Soft-delete (regra do projeto): desativa + revoga sessões. Hard-delete só via /admin/users.
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    user = _fetch_tenant_user(uid, tid, ctx["role"])
    with db() as c:
        c.execute("UPDATE dash_users SET status='disabled', updated_at=%s WHERE id=%s",
                  (_now_utc().isoformat(), uid))
    _revoke_user_sessions(uid)
    audit(ctx["email"], "tenant_user.softdelete", target=user["email"], ip=_client_ip(request))
    return {"ok": True, "mode": "soft"}

# ===========================================================================
# Endpoints — Gestão de logins pela plataforma (Admin WorkPulse, cross-tenant)
# ===========================================================================
@app.get("/v1/admin/users")
def admin_users_list(ctx: Annotated[dict, Depends(require_role("wp_admin"))], tenant: str | None = None):
    with db() as c:
        if tenant:
            rows = c.execute("SELECT * FROM dash_users WHERE tenant_id=%s ORDER BY created_at", (tenant,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM dash_users ORDER BY tenant_id NULLS FIRST, created_at").fetchall()
    return {"users": [_user_public(dict(r)) for r in rows]}

@app.post("/v1/admin/users")
def admin_users_create(body: AdminUserCreate, request: Request,
                       ctx: Annotated[dict, Depends(require_role("wp_admin"))]):
    if body.role not in ALL_ROLES:
        raise HTTPException(400, "perfil inválido")
    if body.role == "wp_admin":
        tid = None
    else:
        if not body.tenant_id:
            raise HTTPException(400, "tenant_id obrigatório para perfis de cliente")
        with db() as c:
            t = c.execute("SELECT id FROM tenants WHERE id=%s", (body.tenant_id,)).fetchone()
        if not t:
            raise HTTPException(404, "tenant não encontrado")
        tid = body.tenant_id
    email_norm = _norm_email(body.email)
    if not email_norm or "@" not in email_norm:
        raise HTTPException(400, "e-mail inválido")
    if _email_taken(email_norm):
        raise HTTPException(409, "e-mail já cadastrado")
    tmp_hash = hash_password(_gen_secret())  # senha aleatória; usuário define via link de e-mail
    with db() as c:
        row = c.execute(
            """INSERT INTO dash_users (tenant_id, email, password_hash, display_name, role, status, must_change_pw, created_by, created_at, updated_at)
               VALUES (%s,%s,%s,%s,%s,'active',%s,%s,%s,%s) RETURNING *""",
            (tid, body.email.strip(), tmp_hash, body.display_name, body.role, True,
             ctx.get("user_id"), _now_utc().isoformat(), _now_utc().isoformat()),
        ).fetchone()
    set_link = _send_password_token(dict(row), "set_password")
    audit(ctx["email"], "admin_user.create", target=email_norm, detail=f"tenant={tid};role={body.role}", ip=_client_ip(request))
    return {"ok": True, "user": _user_public(dict(row)), "set_password_link": set_link}

@app.put("/v1/admin/users/{uid}")
def admin_users_update(uid: int, body: TenantUserUpdate, request: Request,
                       ctx: Annotated[dict, Depends(require_role("wp_admin"))]):
    with db() as c:
        row = c.execute("SELECT * FROM dash_users WHERE id=%s", (uid,)).fetchone()
    if not row:
        raise HTTPException(404, "usuário não encontrado")
    sets, params = [], []
    if body.display_name is not None:
        sets.append("display_name=%s"); params.append(body.display_name)
    if body.role is not None:
        if body.role not in ALL_ROLES:
            raise HTTPException(400, "perfil inválido")
        sets.append("role=%s"); params.append(body.role)
    if body.status is not None:
        if body.status not in ("active", "disabled"):
            raise HTTPException(400, "status inválido")
        sets.append("status=%s"); params.append(body.status)
    if not sets:
        raise HTTPException(400, "nada para atualizar")
    sets.append("updated_at=%s"); params.append(_now_utc().isoformat())
    params.append(uid)
    with db() as c:
        c.execute(f"UPDATE dash_users SET {', '.join(sets)} WHERE id=%s", tuple(params))
    if body.status == "disabled":
        _revoke_user_sessions(uid)
    audit(ctx["email"], "admin_user.update", target=row["email"], detail=str(body.dict(exclude_none=True)), ip=_client_ip(request))
    return {"ok": True}

@app.post("/v1/admin/users/{uid}/reset-password")
def admin_users_reset(uid: int, request: Request,
                      ctx: Annotated[dict, Depends(require_role("wp_admin"))]):
    with db() as c:
        row = c.execute("SELECT * FROM dash_users WHERE id=%s", (uid,)).fetchone()
    if not row:
        raise HTTPException(404, "usuário não encontrado")
    reset_link = _send_password_token(dict(row), "reset_password")
    audit(ctx["email"], "admin_user.reset_password", target=row["email"], ip=_client_ip(request))
    return {"ok": True, "reset_password_link": reset_link}

@app.delete("/v1/admin/users/{uid}")
def admin_users_delete(uid: int, request: Request,
                       ctx: Annotated[dict, Depends(require_role("wp_admin"))], hard: bool = False):
    with db() as c:
        row = c.execute("SELECT * FROM dash_users WHERE id=%s", (uid,)).fetchone()
    if not row:
        raise HTTPException(404, "usuário não encontrado")
    _revoke_user_sessions(uid)
    if hard:
        with db() as c:
            c.execute("DELETE FROM dash_users WHERE id=%s", (uid,))
        audit(ctx["email"], "admin_user.harddelete", target=row["email"], ip=_client_ip(request))
        return {"ok": True, "mode": "hard"}
    with db() as c:
        c.execute("UPDATE dash_users SET status='disabled', updated_at=%s WHERE id=%s",
                  (_now_utc().isoformat(), uid))
    audit(ctx["email"], "admin_user.softdelete", target=row["email"], ip=_client_ip(request))
    return {"ok": True, "mode": "soft"}

# ===========================================================================
# Endpoints — Licenças do tenant (Admin Master: ver, alocar seats, solicitar)
# ===========================================================================
@app.get("/v1/tenant-license")
def tenant_license_get(tenant: str, ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    with db() as c:
        lic = c.execute(
            "SELECT plan, seats, issued, expires, revoked FROM licenses WHERE tenant_id=%s ORDER BY issued DESC LIMIT 1",
            (tid,),
        ).fetchone()
        alloc = c.execute("SELECT user_id FROM dash_seat_alloc WHERE tenant_id=%s", (tid,)).fetchall()
        colabs = c.execute(
            "SELECT user_id, display_name, email, department FROM users WHERE tenant_id=%s ORDER BY display_name",
            (tid,),
        ).fetchall()
    allocated_ids = {a["user_id"] for a in alloc}
    seats = int(lic["seats"]) if lic and lic.get("seats") is not None else 0
    colaboradores = []
    for r in colabs:
        d = dict(r)
        d["allocated"] = d["user_id"] in allocated_ids
        colaboradores.append(d)
    return {
        "license": (dict(lic) if lic else None),
        "seats": seats,
        "allocated": len(allocated_ids),
        "available": max(0, seats - len(allocated_ids)),
        "colaboradores": colaboradores,
    }

@app.post("/v1/tenant-license/assign")
def tenant_license_assign(body: SeatAssignIn, tenant: str, request: Request,
                          ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    with db() as c:
        lic = c.execute(
            "SELECT seats FROM licenses WHERE tenant_id=%s ORDER BY issued DESC LIMIT 1", (tid,)
        ).fetchone()
        seats = int(lic["seats"]) if lic and lic.get("seats") is not None else 0
        cur = c.execute("SELECT COUNT(*) AS n FROM dash_seat_alloc WHERE tenant_id=%s", (tid,)).fetchone()["n"]
    if body.allocated:
        if cur >= seats:
            raise HTTPException(409, "todos os assentos (seats) da licença já estão em uso")
        with db() as c:
            c.execute(
                "INSERT INTO dash_seat_alloc (tenant_id, user_id, allocated_at) VALUES (%s,%s,%s) ON CONFLICT (tenant_id, user_id) DO NOTHING",
                (tid, body.user_id, _now_utc().isoformat()),
            )
        action = "assign"
    else:
        with db() as c:
            c.execute("DELETE FROM dash_seat_alloc WHERE tenant_id=%s AND user_id=%s", (tid, body.user_id))
        action = "unassign"
    audit(ctx["email"], f"license.seat_{action}", target=body.user_id, detail=f"tenant={tid}", ip=_client_ip(request))
    return {"ok": True}

@app.post("/v1/tenant-license/request")
def tenant_license_request(body: LicenseRequestIn, tenant: str, request: Request,
                           ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    with db() as c:
        c.execute(
            """INSERT INTO dash_license_requests (tenant_id, requested_by, plan, seats, message, status, created_at)
               VALUES (%s,%s,%s,%s,%s,'open',%s)""",
            (tid, ctx["email"], body.plan, body.seats, body.message, _now_utc().isoformat()),
        )
    html = (
        f"<p>Nova solicitação de licença.</p>"
        f"<ul><li>Tenant: {tid}</li><li>Solicitante: {ctx['email']}</li>"
        f"<li>Plano: {body.plan or '-'}</li><li>Seats: {body.seats or '-'}</li></ul>"
        f"<p>Mensagem: {body.message or '-'}</p>"
    )
    try:
        _send_email_html([PLATFORM_NOTIFY], f"WorkPulse — solicitação de licença ({tid})", html)
    except Exception as exc:
        print(f"[auth] AVISO: falha ao notificar plataforma: {exc}", flush=True)
    audit(ctx["email"], "license.request", target=tid, detail=f"plan={body.plan};seats={body.seats}", ip=_client_ip(request))
    return {"ok": True}



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
