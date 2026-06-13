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
# Janela para considerar uma pessoa "online" (agente reportou neste intervalo).
ONLINE_THRESHOLD_MINUTES = int(os.getenv("WORKPULSE_ONLINE_MINUTES", "30"))

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

    with db() as c:
        row = c.execute(
            "SELECT revoked, expires FROM licenses WHERE lic_id = %s", (claims["lic"],)
        ).fetchone()
    if row and row["revoked"]:
        raise HTTPException(status_code=403, detail="license revoked")
    # Expiração autoritativa pelo banco (editável no ADM); cai para o claim se ausente.
    exp_src = (row["expires"] if (row and row.get("expires")) else claims.get("expires"))
    try:
        exp_dt = dt.datetime.fromisoformat(str(exp_src).replace("Z", "+00:00"))
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=dt.timezone.utc)
    except Exception:
        exp_dt = dt.datetime.fromisoformat(claims["expires"].replace("Z", "+00:00"))
    if exp_dt < dt.datetime.now(dt.timezone.utc):
        raise HTTPException(status_code=403, detail="license expired")
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
    request:       Request,
    payload:       IngestPayload,
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
        # Registra o IP público de conexão (X-Forwarded-For do ALB), agregado por dia local.
        _client_addr = _client_ip(request)
        if _client_addr:
            try:
                _day_local = (dt.datetime.now(ZoneInfo(REPORT_TZ)).date().isoformat()
                              if ZoneInfo else dt.date.today().isoformat())
            except Exception:
                _day_local = dt.date.today().isoformat()
            c.execute(
                """INSERT INTO client_ips (tenant_id, user_id, ip, day, first_seen, last_seen, hits)
                   VALUES (%s,%s,%s,%s,%s,%s,1)
                   ON CONFLICT (tenant_id, user_id, ip, day)
                   DO UPDATE SET last_seen = EXCLUDED.last_seen, hits = client_ips.hits + 1""",
                (claims["tenant"], user_id, _client_addr, _day_local, now_iso, now_iso),
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
def list_users(tenant: str, request: Request):
    allowed = _lider_allowed_ids(request, tenant)
    with db() as c:
        rows = c.execute(
            """SELECT user_id, display_name, email, department, role,
                      login_user, first_seen, last_seen
               FROM users WHERE tenant_id = %s ORDER BY last_seen DESC""",
            (tenant,),
        ).fetchall()
    out = [dict(r) for r in rows]
    if allowed is not None:
        aset = set(allowed)
        out = [r for r in out if r["user_id"] in aset]
    return {"users": out}


@app.get("/v1/users/{user_id}/summary", dependencies=[Depends(require_tenant_scope)])
def user_summary(user_id: str, tenant: str, request: Request, days: int = 7,
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
        _allowed = _lider_allowed_ids(request, tenant)
        if _allowed is not None and user_id not in set(_allowed):
            raise HTTPException(status_code=403, detail="colaborador fora do seu escopo")

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
def team_summary(tenant: str, request: Request, days: int = 7,
                 since: str | None = None, until: str | None = None):
    if since is None:
        since = _since_for_days(days)
    if until is None:
        until = dt.datetime.now(dt.timezone.utc).isoformat()
    allowed, _stag = _scope_for(request, tenant)
    SC = " AND user_id = ANY(%s)" if allowed is not None else ""
    A  = [allowed] if allowed is not None else []
    cache_key = f"{tenant}:team_summary:{since}:{until}{_stag}"
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
        if allowed is not None:
            _aset = set(allowed)
            per_user = [r for r in per_user if r["user_id"] in _aset]
        global_top = c.execute(
            f"""SELECT MIN(data_json->>'app') AS app, SUM(duration_sec) AS seconds
               FROM events
               WHERE tenant_id=%s{SC} AND ts>=%s AND ts<=%s AND bucket='window'
               GROUP BY LOWER(data_json->>'app') ORDER BY seconds DESC LIMIT 15""",
            tuple([tenant, *A, since, until]),
        ).fetchall()

        # AFK *dentro da jornada* por colaborador (mesma lógica de /v1/jornada):
        # só conta AFK cujo horário local cai entre o início e o fim da jornada do
        # dia (1ª coleta → última coleta). Exclui o "AFK pós jornada".
        afk_in_rows = c.execute(
            """
            WITH w AS (
                SELECT user_id,
                       (ts::timestamptz AT TIME ZONE %(tz)s)                 AS lts,
                       (ts::timestamptz AT TIME ZONE %(tz)s)
                           + (duration_sec || ' seconds')::interval          AS lend
                FROM events
                WHERE tenant_id = %(tenant)s AND bucket = 'window'
                  AND ts::timestamptz >= %(since)s::timestamptz
                  AND ts::timestamptz <  %(until)s::timestamptz
            ),
            win AS (
                SELECT user_id, lts::date AS day, MIN(lts) AS start_ts, MAX(lend) AS end_ts
                FROM w GROUP BY user_id, lts::date
            ),
            afk_ev AS (
                SELECT user_id,
                       (ts::timestamptz AT TIME ZONE %(tz)s)        AS lts,
                       (ts::timestamptz AT TIME ZONE %(tz)s)::date  AS day,
                       duration_sec
                FROM events
                WHERE tenant_id = %(tenant)s AND bucket = 'afk'
                  AND data_json->>'status' = 'afk'
                  AND ts::timestamptz >= %(since)s::timestamptz
                  AND ts::timestamptz <  %(until)s::timestamptz
            )
            SELECT a.user_id,
                   COALESCE(SUM(a.duration_sec) FILTER (
                       WHERE a.lts >= win.start_ts AND a.lts <= win.end_ts), 0) AS afk_in
            FROM afk_ev a JOIN win ON win.user_id = a.user_id AND win.day = a.day
            GROUP BY a.user_id
            """,
            {"tz": REPORT_TZ, "tenant": tenant, "since": since, "until": until},
        ).fetchall()

    afk_in_map = {r["user_id"]: float(r["afk_in"] or 0.0) for r in afk_in_rows}

    users_out = []
    for r in per_user:
        d = dict(r)
        d["afk_in"] = afk_in_map.get(r["user_id"], 0.0)   # AFK na Jornada (exclui pós-jornada)
        users_out.append(d)

    result = {
        "tenant":   tenant,
        "since":    since,
        "users":    users_out,
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
    request:    Request,
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

    _allowed = _lider_allowed_ids(request, tenant)
    if _allowed is not None:
        where.append("user_id = ANY(%s)"); args.append(_allowed)

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
    request: Request,
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
    _allowed = _lider_allowed_ids(request, tenant)
    if _allowed is not None and user_id not in set(_allowed):
        raise HTTPException(status_code=403, detail="colaborador fora do seu escopo")

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
        afk_ev AS (
            SELECT (ts::timestamptz AT TIME ZONE %(tz)s)        AS lts,
                   (ts::timestamptz AT TIME ZONE %(tz)s)::date  AS day,
                   duration_sec
            FROM events
            WHERE tenant_id = %(tenant)s AND user_id = %(uid)s AND bucket = 'afk'
              AND data_json->>'status' = 'afk'
              AND ts::timestamptz >= %(since)s::timestamptz
              AND ts::timestamptz <  %(until)s::timestamptz
        ),
        afk AS (
            SELECT a.day,
                   COALESCE(SUM(a.duration_sec) FILTER (
                       WHERE a.lts >= win.start_ts AND a.lts <= win.end_ts), 0) AS afk_in,
                   COALESCE(SUM(a.duration_sec) FILTER (
                       WHERE a.lts > win.end_ts), 0)                            AS afk_after
            FROM afk_ev a JOIN win ON win.day = a.day
            GROUP BY a.day
        )
        SELECT to_char(win.day, 'YYYY-MM-DD')        AS day,
               to_char(win.start_ts, 'HH24:MI:SS')   AS start_hm,
               to_char(win.end_ts,   'HH24:MI:SS')   AS end_hm,
               EXTRACT(EPOCH FROM (win.start_ts - win.day::timestamp)) AS start_sec,
               EXTRACT(EPOCH FROM (win.end_ts   - win.day::timestamp)) AS end_sec,
               win.focus_sec                         AS focus_sec,
               COALESCE(afk.afk_in, 0)               AS pauses_sec,
               COALESCE(afk.afk_after, 0)            AS afk_after_sec
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
        focus     = float(r["focus_sec"]     or 0.0)   # soma das janelas (tempo ativo)
        afk_in    = float(r["pauses_sec"]    or 0.0)   # AFK dentro da jornada (início→fim)
        afk_after = float(r["afk_after_sec"] or 0.0)   # AFK após a última coleta de tela
        start_sec = float(r["start_sec"] or 0.0)
        end_sec   = float(r["end_sec"]   or 0.0)
        gross  = max(0.0, end_sec - start_sec)
        net    = max(0.0, gross - afk_in)              # Líquida = Bruta − AFK na Jornada
        days_out.append({
            "day":        r["day"],
            "start":      r["start_hm"][:5],
            "end":        r["end_hm"][:5],
            "start_float": round(start_sec / 3600.0, 4),
            "end_float":   round(end_sec   / 3600.0, 4),
            "focus_sec":  round(min(focus, gross), 1),
            "pauses_sec": round(afk_in, 1),            # AFK na Jornada
            "gross_sec":  round(gross, 1),
            "net_sec":    round(net, 1),
            "idle_after_sec": round(afk_after, 1),     # AFK Pós Jornada
            "is_today":   (r["day"] == today_local),
        })

    result = {"tenant": tenant, "user_id": user_id,
              "since": since, "until": until, "tz": REPORT_TZ, "days": days_out}
    _cache_set(cache_key, result)
    return result


@app.get("/v1/productivity/events_summary", dependencies=[Depends(require_tenant_scope)])
def productivity_events_summary(
    tenant:  str,
    request: Request,
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
    allowed, _stag = _scope_for(request, tenant)
    cache_key = f"{tenant}:events_summary:{since}:{until}:{user_id or ''}:{_bucket}{_stag}"
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
        if allowed is not None:
            agg_sql += " AND user_id = ANY(%s)"; agg_args.append(allowed)
        agg_sql += nb_sql; agg_args += nb_args
        agg_sql += " GROUP BY app, title"
        agg_rows = c.execute(agg_sql, agg_args).fetchall()

        day_sql  = ("SELECT substr(ts,1,13) AS h, data_json->>'app' AS app,"
                    " data_json->>'title' AS title, SUM(duration_sec) AS total_sec"
                    " FROM events WHERE tenant_id=%s AND bucket=%s AND ts>=%s AND ts<=%s")
        day_args: list = [tenant, _bucket, since, until]
        if user_id:
            day_sql += " AND user_id=%s"; day_args.append(user_id)
        if allowed is not None:
            day_sql += " AND user_id = ANY(%s)"; day_args.append(allowed)
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
def workforce_overview(tenant: str, request: Request, days: int = 7,
                       since: str | None = None, until: str | None = None):
    now = dt.datetime.now(dt.timezone.utc)
    if since is None:
        since = _since_for_days(days)
    if until is None:
        until = now.isoformat()
    allowed, _stag   = _scope_for(request, tenant)
    SC  = " AND user_id = ANY(%s)"   if allowed is not None else ""
    SCU = " AND u.user_id = ANY(%s)" if allowed is not None else ""
    A   = [allowed] if allowed is not None else []
    cache_key        = f"{tenant}:overview:{since}:{until}{_stag}"
    cached           = _cache_get(cache_key)
    if cached is not None:
        return cached

    today_start      = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    online_threshold = (now - dt.timedelta(minutes=ONLINE_THRESHOLD_MINUTES)).isoformat()

    with db() as c:
        totals = c.execute(
            f"""SELECT
                   COUNT(DISTINCT user_id) AS total_users,
                   SUM(CASE WHEN bucket='window' THEN duration_sec END) AS active,
                   SUM(CASE WHEN bucket='afk' AND data_json->>'status'='afk'
                            THEN duration_sec END) AS afk,
                   SUM(CASE WHEN bucket='window' AND ts>=%s THEN duration_sec END) AS today_active
               FROM events WHERE tenant_id=%s{SC} AND ts>=%s AND ts<=%s""",
            tuple([today_start, tenant, *A, since, until]),
        ).fetchone()
        today_active = totals["today_active"] or 0

        online = c.execute(
            f"SELECT COUNT(*) AS n FROM users WHERE tenant_id=%s{SC} AND last_seen>=%s",
            tuple([tenant, *A, online_threshold]),
        ).fetchone()["n"]

        hourly = c.execute(
            f"""SELECT CAST(substr(ts,12,2) AS INTEGER) AS hour, SUM(duration_sec) AS seconds
               FROM events WHERE tenant_id=%s{SC} AND ts>=%s AND ts<=%s AND bucket='window'
               GROUP BY hour ORDER BY hour""",
            tuple([tenant, *A, since, until]),
        ).fetchall()

        # Agrupa por HORA UTC; o re-bucket para data LOCAL (BRT) é feito em Python
        # (ver DASH_TZ_OFFSET_HOURS) para que cada dia local seja uma única barra.
        # substr é portável (PostgreSQL + harness sqlite).
        _hour_dur = c.execute(
            f"""SELECT substr(ts,1,13) AS h,
                      SUM(CASE WHEN bucket='window' THEN duration_sec ELSE 0 END) AS active,
                      SUM(CASE WHEN bucket='afk' AND data_json->>'status'='afk'
                               THEN duration_sec ELSE 0 END) AS afk
               FROM events WHERE tenant_id=%s{SC} AND ts>=%s AND ts<=%s
               GROUP BY h""",
            tuple([tenant, *A, since, until]),
        ).fetchall()
        _hour_usr = c.execute(
            f"""SELECT DISTINCT substr(ts,1,13) AS h, user_id
               FROM events WHERE tenant_id=%s{SC} AND ts>=%s AND ts<=%s""",
            tuple([tenant, *A, since, until]),
        ).fetchall()

        by_dept = c.execute(
            f"""SELECT COALESCE(u.department, 'Sem departamento') AS dept,
                      COUNT(DISTINCT u.user_id) AS users,
                      COALESCE(SUM(CASE WHEN e.bucket='window' THEN e.duration_sec END), 0) AS active
               FROM users u
               LEFT JOIN events e ON e.tenant_id=u.tenant_id AND e.user_id=u.user_id
                    AND e.ts>=%s AND e.ts<=%s
               WHERE u.tenant_id=%s{SCU}
               GROUP BY dept ORDER BY active DESC""",
            tuple([since, until, tenant, *A]),
        ).fetchall()

        risk = c.execute(
            f"""SELECT u.user_id, u.display_name,
                      COALESCE(e.total_sec, 0) AS total_active
               FROM users u
               LEFT JOIN (
                   SELECT user_id, SUM(duration_sec) AS total_sec
                   FROM events WHERE tenant_id=%s AND ts>=%s AND ts<=%s AND bucket='window'
                   GROUP BY user_id
               ) e ON e.user_id = u.user_id
               WHERE u.tenant_id=%s{SCU}""",
            tuple([tenant, since, until, tenant, *A]),
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
def workforce_timeseries(tenant: str, request: Request, days: int = 30, bucket: str = "window",
                         since: str | None = None, until: str | None = None):
    if since is None:
        since = _since_for_days(days)
    if until is None:
        until = dt.datetime.now(dt.timezone.utc).isoformat()
    allowed = _lider_allowed_ids(request, tenant)
    SC = " AND user_id = ANY(%s)" if allowed is not None else ""
    A  = [allowed] if allowed is not None else []
    with db() as c:
        rows = c.execute(
            f"""SELECT substr(ts,1,10) AS day, SUM(duration_sec) AS seconds
               FROM events WHERE tenant_id=%s{SC} AND ts>=%s AND ts<=%s AND bucket=%s
               GROUP BY day ORDER BY day""",
            tuple([tenant, *A, since, until, bucket]),
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
    request: Request,
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
        _allowed = _lider_allowed_ids(request, tenant)
        if _allowed is not None:
            sql += " AND user_id = ANY(%s)"; args.append(_allowed)
        sql += " GROUP BY user_id, app, title ORDER BY total_sec DESC"
        raw_rows = c.execute(sql, args).fetchall()

        dsql  = ("SELECT substr(ts,1,13) AS h, data_json->>'app' AS app,"
                 " data_json->>'title' AS title, SUM(duration_sec) AS total_sec"
                 " FROM events WHERE tenant_id=%s AND bucket='window' AND ts>=%s AND ts<=%s")
        dargs: list = [tenant, since, until]
        if user_id:
            dsql += " AND user_id=%s"; dargs.append(user_id)
        if _allowed is not None:
            dsql += " AND user_id = ANY(%s)"; dargs.append(_allowed)
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
def productivity_apps(tenant: str, request: Request, days: int = Query(30, le=90)):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    allowed = _lider_allowed_ids(request, tenant)
    SC = " AND user_id = ANY(%s)" if allowed is not None else ""
    A  = [allowed] if allowed is not None else []
    with db() as c:
        rules = [dict(r) for r in c.execute(
            "SELECT * FROM productivity_rules WHERE tenant_id=%s ORDER BY created_at",
            (tenant,),
        ).fetchall()]
        rows = c.execute(
            f"""SELECT data_json->>'app' AS app,
                      SUM(duration_sec) AS total_sec,
                      COUNT(DISTINCT user_id) AS user_count
               FROM events WHERE tenant_id=%s{SC} AND bucket='window' AND ts>=%s
               GROUP BY app ORDER BY total_sec DESC LIMIT 150""",
            tuple([tenant, *A, since]),
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
def list_departments(tenant: str, request: Request):
    allowed = _lider_allowed_ids(request, tenant)
    SC = " AND user_id = ANY(%s)" if allowed is not None else ""
    A  = [allowed] if allowed is not None else []
    with db() as c:
        depts  = c.execute(
            "SELECT id, name, created_at FROM tenant_departments WHERE tenant_id=%s ORDER BY name",
            (tenant,),
        ).fetchall()
        result = []
        for d in depts:
            count = c.execute(
                f"SELECT COUNT(*) AS n FROM users WHERE tenant_id=%s{SC} AND department=%s",
                tuple([tenant, *A, d["name"]]),
            ).fetchone()["n"]
            users = c.execute(
                f"""SELECT user_id, display_name, login_user, last_seen
                   FROM users WHERE tenant_id=%s{SC} AND department=%s ORDER BY last_seen DESC""",
                tuple([tenant, *A, d["name"]]),
            ).fetchall()
            result.append({
                "id":         d["id"],
                "name":       d["name"],
                "created_at": d["created_at"],
                "user_count": count,
                "users":      [dict(u) for u in users],
            })
    if allowed is not None:
        result = [d for d in result if d["user_count"] > 0]
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

CLIENT_ROLES   = ("admin_master", "supervisor", "lider")
ALL_ROLES      = ("admin_master", "supervisor", "lider", "wp_admin")

# --- Tabelas novas (aditivas; init_db() as cria se não existirem) ----------
_SCHEMA_STMTS.extend([
    """
    CREATE TABLE IF NOT EXISTS dash_users (
        id             BIGSERIAL PRIMARY KEY,
        tenant_id      TEXT REFERENCES tenants(id) ON DELETE CASCADE,
        email          TEXT NOT NULL,
        password_hash  TEXT NOT NULL,
        display_name   TEXT,
        role           TEXT NOT NULL DEFAULT 'supervisor',
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
    "ALTER TABLE dash_users DROP CONSTRAINT IF EXISTS dash_users_role_check",
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
        # Consumo real = colaboradores monitorados (mesma lógica de /v1/license/info)
        used = c.execute(
            "SELECT COUNT(DISTINCT user_id) AS n FROM users WHERE tenant_id=%s", (tid,)
        ).fetchone()["n"]
    seats = int(lic["seats"]) if lic and lic.get("seats") is not None else 0
    return {
        "license": (dict(lic) if lic else None),
        "seats": seats,
        "used": used,
        "available": max(0, seats - used),
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



# ===========================================================================
# Edição de licença (ADM): seats, validade e plano
# ===========================================================================
class LicenseEditRequest(BaseModel):
    plan: str | None = None
    seats: int | None = None
    expires: str | None = None   # 'YYYY-MM-DD' ou ISO completo
    add_days: int | None = None  # alternativa: estender N dias a partir da validade atual


@app.patch("/v1/admin/licenses/{lic_id}", dependencies=[Depends(require_admin)])
def admin_edit_license(lic_id: str, req: LicenseEditRequest, request: Request):
    with db() as c:
        row = c.execute("SELECT * FROM licenses WHERE lic_id=%s", (lic_id,)).fetchone()
    if not row:
        raise HTTPException(404, "licença não encontrada")
    lic = dict(row)
    if lic.get("revoked"):
        raise HTTPException(400, "licença revogada não pode ser editada")

    new_plan  = (req.plan or lic["plan"] or "business").strip()
    new_seats = int(req.seats) if req.seats is not None else int(lic["seats"])
    if new_seats < 1:
        raise HTTPException(400, "a quantidade de seats deve ser >= 1")

    try:
        cur_exp = dt.datetime.fromisoformat(str(lic["expires"]).replace("Z", "+00:00"))
        if cur_exp.tzinfo is None:
            cur_exp = cur_exp.replace(tzinfo=dt.timezone.utc)
    except Exception:
        cur_exp = dt.datetime.now(dt.timezone.utc)

    if req.expires:
        try:
            ne = req.expires.strip()
            if len(ne) == 10:  # 'YYYY-MM-DD' → fim do dia em UTC
                ne = ne + "T23:59:59+00:00"
            new_exp = dt.datetime.fromisoformat(ne.replace("Z", "+00:00"))
            if new_exp.tzinfo is None:
                new_exp = new_exp.replace(tzinfo=dt.timezone.utc)
        except Exception:
            raise HTTPException(400, "data de validade inválida (use AAAA-MM-DD)")
    elif req.add_days:
        new_exp = cur_exp + dt.timedelta(days=int(req.add_days))
    else:
        new_exp = cur_exp

    exp_iso = new_exp.isoformat().replace("+00:00", "Z")
    # IMPORTANTE: o token assinado instalado nos agentes NÃO é alterado aqui.
    # Plano e seats são metadados no banco; a validade é autoritativa pelo banco
    # (heartbeat e ingestão já leem daqui). Por isso editar a licença NÃO exige
    # reinstalar nem redistribuir token nos agentes da ponta.
    with db() as c:
        c.execute(
            "UPDATE licenses SET plan=%s, seats=%s, expires=%s WHERE lic_id=%s",
            (new_plan, new_seats, exp_iso, lic_id),
        )
        c.execute("UPDATE tenants SET plan=%s WHERE id=%s", (new_plan, lic["tenant_id"]))
    audit("admin", "license.edit", target=lic_id,
          detail=f"plan={new_plan};seats={new_seats};expires={exp_iso}", ip=_client_ip(request))
    return {
        "ok": True, "lic_id": lic_id, "tenant_id": lic["tenant_id"],
        "plan": new_plan, "seats": new_seats, "expires": exp_iso,
    }


# ===========================================================================
# Custos AWS (Cost Explorer) — painel ADM
# ===========================================================================
_AWS_COST_CACHE = {"ts": 0.0, "data": None}
_AWS_COST_TTL = 6 * 3600  # 6h (a API custa ~US$0,01/consulta e atualiza ~1x/dia)


@app.get("/v1/admin/aws-costs", dependencies=[Depends(require_admin)])
def admin_aws_costs():
    if _AWS_COST_CACHE["data"] and (time.time() - _AWS_COST_CACHE["ts"]) < _AWS_COST_TTL:
        return _AWS_COST_CACHE["data"]
    try:
        import boto3 as _boto3
        ce = _boto3.client("ce", region_name="us-east-1")  # Cost Explorer só em us-east-1
        today = dt.date.today()
        start = (today.replace(day=1) - dt.timedelta(days=31 * 5)).replace(day=1)
        end   = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        tp = {"Start": start.isoformat(), "End": end.isoformat()}

        # 1) Custo de USO por serviço (sem o desconto do nível gratuito) — gráfico + nº real.
        resp_svc = ce.get_cost_and_usage(
            TimePeriod=tp, Granularity="MONTHLY", Metrics=["UnblendedCost"],
            Filter={"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}},
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        # 2) Por tipo de registro — separa uso, faturado (net) e economia do nível gratuito.
        resp_rt = ce.get_cost_and_usage(
            TimePeriod=tp, Granularity="MONTHLY", Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "RECORD_TYPE"}],
        )

        svc_by_month = {}
        for r in resp_svc.get("ResultsByTime", []):
            m = r["TimePeriod"]["Start"][:7]
            services = {}
            for g in r.get("Groups", []):
                amt = float(g["Metrics"]["UnblendedCost"]["Amount"])
                if amt > 0:
                    services[g["Keys"][0]] = round(amt, 2)
            svc_by_month[m] = services

        rt_by_month = {}
        for r in resp_rt.get("ResultsByTime", []):
            m = r["TimePeriod"]["Start"][:7]
            usage, net = 0.0, 0.0
            for g in r.get("Groups", []):
                amt = float(g["Metrics"]["UnblendedCost"]["Amount"])
                net += amt
                if g["Keys"][0] == "Usage":
                    usage += amt
            rt_by_month[m] = {"usage": round(usage, 2), "net": round(net, 2),
                              "savings": round(usage - net, 2)}

        months = []
        for m in sorted(set(list(svc_by_month) + list(rt_by_month))):
            services = svc_by_month.get(m, {})
            rt = rt_by_month.get(m, {"usage": round(sum(services.values()), 2), "net": 0.0, "savings": 0.0})
            months.append({
                "month": m, "services": services, "total": rt["usage"],  # 'total' = uso (gráfico)
                "usage": rt["usage"], "net": rt["net"], "savings": rt["savings"],
            })
        cur = months[-1] if months else {"month": None, "usage": 0.0, "net": 0.0, "savings": 0.0}

        # Projeção de fim de mês: uso até agora + previsão do restante (somente USO).
        forecast_usage = None
        try:
            fc = ce.get_cost_forecast(
                TimePeriod={"Start": today.isoformat(), "End": end.isoformat()},
                Metric="UNBLENDED_COST", Granularity="MONTHLY",
                Filter={"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}},
            )
            rest = float(fc["Total"]["Amount"])
            forecast_usage = round((cur.get("usage") or 0.0) + rest, 2)
        except Exception:
            forecast_usage = None

        with db() as c:
            users = c.execute(
                "SELECT COUNT(DISTINCT (tenant_id || '/' || user_id)) AS n FROM users"
            ).fetchone()["n"]

        data = {
            "available": True, "currency": "USD", "months": months,
            "current_month": {"month": cur["month"], "usage": cur["usage"],
                              "net": cur["net"], "savings": cur["savings"]},
            "active_users": users,
            "cost_per_user": (round(cur["usage"] / users, 2) if users else None),
            "free_tier": bool(cur.get("savings", 0) and cur["savings"] > 0.01),
            "forecast_usage": forecast_usage,
        }
        _AWS_COST_CACHE["data"] = data
        _AWS_COST_CACHE["ts"] = time.time()
        return data
    except Exception as exc:
        return {"available": False, "error": str(exc)[:300]}


# ===========================================================================
# Custo estimado por cliente (rateio do custo de USO da AWS)
# ===========================================================================
@app.get("/v1/admin/tenant-costs", dependencies=[Depends(require_admin)])
def admin_tenant_costs(driver: str = "users"):
    costs = admin_aws_costs()  # reaproveita o cache do Cost Explorer
    usage_total = 0.0
    if costs.get("available"):
        usage_total = float(costs.get("current_month", {}).get("usage") or 0.0)

    month_start = dt.date.today().replace(day=1).isoformat()
    with db() as c:
        rows = c.execute(
            """SELECT t.id, t.name, t.plan,
                      COUNT(DISTINCT u.user_id)   AS users,
                      COUNT(DISTINCT d.device_id) AS devices
               FROM tenants t
               LEFT JOIN users   u ON u.tenant_id = t.id
               LEFT JOIN devices d ON d.tenant_id = t.id
               GROUP BY t.id, t.name, t.plan
               ORDER BY users DESC"""
        ).fetchall()
        erows = c.execute(
            "SELECT tenant_id, COUNT(*) AS n FROM events WHERE ts >= %s GROUP BY tenant_id",
            (month_start,),
        ).fetchall()
    events_by_tenant = {r["tenant_id"]: r["n"] for r in erows}
    tenants = [dict(r) for r in rows]
    for t in tenants:
        t["events"] = int(events_by_tenant.get(t["id"], 0))

    if driver == "devices":
        key, total = "devices", sum((t["devices"] or 0) for t in tenants)
    elif driver == "events":
        key, total = "events", sum((t["events"] or 0) for t in tenants)
    else:
        driver, key = "users", "users"
        total = sum((t["users"] or 0) for t in tenants)
    total = total or 1

    out = []
    for t in tenants:
        share = (t[key] or 0) / total
        cost  = round(usage_total * share, 2)
        users, devices = (t["users"] or 0), (t["devices"] or 0)
        out.append({
            "tenant_id": t["id"], "name": t["name"], "plan": t["plan"],
            "users": users, "devices": devices, "events": int(t["events"] or 0), "cost": cost,
            "cost_per_user":  (round(cost / users, 2)   if users   else None),
            "cost_per_agent": (round(cost / devices, 2) if devices else None),
        })
    return {
        "available": costs.get("available", False),
        "currency": "USD",
        "month": costs.get("current_month", {}).get("month"),
        "usage_total": round(usage_total, 2),
        "driver": driver,
        "tenants": out,
    }


# ===========================================================================
# Simulador de custo por nº de usuários (autocalibra com dados reais da AWS)
# ===========================================================================
# Serviços tratados como FIXOS (não mudam com nº de usuários). Os demais são
# tratados como variáveis (escalam proporcional aos usuários).
_SIM_FIXED_SERVICES = {
    "Amazon Elastic Container Service",   # Fargate roda 24/7
    "Amazon Elastic Load Balancing",      # ALB sempre ligado
    "Amazon Virtual Private Cloud",       # horas do NAT Gateway
    "Amazon Relational Database Service", # instância do banco (storage cresce devagar)
}
# Observação: "EC2 - Other" (transferência/NAT data/EBS) e "AmazonCloudWatch" NÃO
# são fixos — escalam com usuários/eventos, então entram como variáveis.


@app.get("/v1/admin/cost-simulate", dependencies=[Depends(require_admin)])
def admin_cost_simulate(users: int = 0):
    import calendar
    costs = admin_aws_costs()  # usa cache do Cost Explorer
    if not costs.get("available"):
        return {"available": False, "error": costs.get("error", "custos indisponíveis")}

    months = costs.get("months", [])
    base = None
    for mm in reversed(months):
        if mm.get("services"):
            base = mm
            break
    if not base:
        return {"available": False, "error": "sem dados de custo por serviço"}

    today = dt.date.today()
    cur_label = today.strftime("%Y-%m")
    if base.get("month") == cur_label:
        days_elapsed = today.day or 1
        days_in_month = calendar.monthrange(today.year, today.month)[1]
        factor_month = days_in_month / days_elapsed
    else:
        factor_month = 1.0  # mês já fechado

    with db() as c:
        ref_users = c.execute(
            "SELECT COUNT(DISTINCT (tenant_id || '/' || user_id)) AS n FROM users"
        ).fetchone()["n"] or 1

    n = int(users) if users and int(users) > 0 else ref_users
    scale = n / ref_users if ref_users else 1.0

    items, total = [], 0.0
    for svc, mtd in sorted(base.get("services", {}).items(), key=lambda x: -x[1]):
        month_val = mtd * factor_month            # custo do mês cheio (usuários atuais)
        is_fixed  = svc in _SIM_FIXED_SERVICES
        proj      = round(month_val if is_fixed else month_val * scale, 2)
        items.append({
            "service": svc, "fixed": is_fixed,
            "base_month": round(month_val, 2), "projected": proj,
        })
        total += proj
    total = round(total, 2)

    return {
        "available": True, "currency": "USD",
        "base_month": base.get("month"), "ref_users": ref_users, "users": n,
        "items": items, "total": total,
        "cost_per_user": (round(total / n, 2) if n else None),
    }


# ===========================================================================
# Localização — IPs públicos de conexão + geolocalização (cache)
# ===========================================================================
_SCHEMA_STMTS.extend([
    """
    CREATE TABLE IF NOT EXISTS client_ips (
        tenant_id  TEXT NOT NULL,
        user_id    TEXT NOT NULL,
        ip         TEXT NOT NULL,
        day        TEXT NOT NULL,
        first_seen TEXT NOT NULL,
        last_seen  TEXT NOT NULL,
        hits       INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (tenant_id, user_id, ip, day)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_client_ips_tenant_day ON client_ips(tenant_id, day)",
    """
    CREATE TABLE IF NOT EXISTS ip_geo (
        ip          TEXT PRIMARY KEY,
        country     TEXT,
        region      TEXT,
        city        TEXT,
        org         TEXT,
        lat         DOUBLE PRECISION,
        lon         DOUBLE PRECISION,
        resolved_at TEXT
    )
    """,
])


def _is_public_ip(ip: str) -> bool:
    try:
        import ipaddress
        a = ipaddress.ip_address(ip)
        return not (a.is_private or a.is_loopback or a.is_reserved
                    or a.is_link_local or a.is_multicast or a.is_unspecified)
    except Exception:
        return False


def _http_get_json(url: str, timeout: int = 6) -> dict:
    """GET com User-Agent explícito (muitas APIs grátis bloqueiam o UA padrão)."""
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "WorkPulse/1.0 (+https://workpulse.com.br)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _fetch_geo(ip: str) -> dict | None:
    """Resolve IP -> cidade/UF/pais/operadora. FreeIPAPI (sem token, comercial ok)
    por padrao; usa IPinfo se WORKPULSE_IPINFO_TOKEN estiver setado."""
    token = os.getenv("WORKPULSE_IPINFO_TOKEN", "")
    try:
        if token:
            d = _http_get_json(f"https://ipinfo.io/{ip}/json?token={token}")
            loc = (d.get("loc") or ",").split(",")
            return {
                "country": d.get("country"), "region": d.get("region"),
                "city": d.get("city"), "org": d.get("org"),
                "lat": (float(loc[0]) if loc and loc[0] else None),
                "lon": (float(loc[1]) if len(loc) > 1 and loc[1] else None),
            }
        d = _http_get_json(f"https://freeipapi.com/api/json/{ip}")
        return {
            "country": d.get("countryName"), "region": d.get("regionName"),
            "city": d.get("cityName"),
            "org": d.get("asnOrganization") or (("AS" + str(d["asn"])) if d.get("asn") else None),
            "lat": d.get("latitude"), "lon": d.get("longitude"),
        }
    except Exception as exc:
        print(f"[geo] falha ao resolver {ip}: {type(exc).__name__}: {exc}", flush=True)
        return None


def _resolve_ip_geo(ip: str) -> dict | None:
    if not _is_public_ip(ip):
        return None
    with db() as c:
        row = c.execute(
            "SELECT ip, country, region, city, org, lat, lon FROM ip_geo WHERE ip=%s", (ip,)
        ).fetchone()
    if row:
        return dict(row)
    geo = _fetch_geo(ip)
    if geo is not None:
        with db() as c:
            c.execute(
                """INSERT INTO ip_geo (ip, country, region, city, org, lat, lon, resolved_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (ip) DO UPDATE SET country=EXCLUDED.country, region=EXCLUDED.region,
                       city=EXCLUDED.city, org=EXCLUDED.org, lat=EXCLUDED.lat, lon=EXCLUDED.lon,
                       resolved_at=EXCLUDED.resolved_at""",
                (ip, geo.get("country"), geo.get("region"), geo.get("city"),
                 geo.get("org"), geo.get("lat"), geo.get("lon"), _now_utc().isoformat()),
            )
        geo["ip"] = ip
        return geo
    return None


def _geo_for_ips(ips, max_new: int = 25) -> dict:
    out, new = {}, 0
    for ip in ips:
        with db() as c:
            row = c.execute(
                "SELECT ip, country, region, city, org, lat, lon FROM ip_geo WHERE ip=%s", (ip,)
            ).fetchone()
        if row:
            out[ip] = dict(row); continue
        if new < max_new and _is_public_ip(ip):
            g = _resolve_ip_geo(ip)
            if g:
                out[ip] = g
            new += 1
    return out


def _geo_label(g: dict | None) -> str:
    if not g:
        return ""
    parts = [p for p in (g.get("city"), g.get("region"), g.get("country")) if p]
    return ", ".join(parts)


@app.get("/v1/locations", dependencies=[Depends(require_tenant_scope)])
def locations(tenant: str, request: Request, user_id: str | None = None,
              since: str | None = None, until: str | None = None, days: int | None = None):
    _allowed = _lider_allowed_ids(request, tenant)
    if user_id and _allowed is not None and user_id not in set(_allowed):
        raise HTTPException(status_code=403, detail="colaborador fora do seu escopo")
    if days:
        since = _since_for_days(int(days))
    if since is None:
        since = _since_for_days(7)
    d_since = since[:10]
    d_until = (until or _now_utc().isoformat())[:10]

    with db() as c:
        if user_id:
            rows = c.execute(
                """SELECT user_id, ip, day, first_seen, last_seen, hits
                   FROM client_ips
                   WHERE tenant_id=%s AND user_id=%s AND day>=%s AND day<=%s
                   ORDER BY day DESC, first_seen DESC""",
                (tenant, user_id, d_since, d_until),
            ).fetchall()
        else:
            rows = c.execute(
                """SELECT user_id, ip, day, first_seen, last_seen, hits
                   FROM client_ips
                   WHERE tenant_id=%s AND day>=%s AND day<=%s
                   ORDER BY last_seen DESC""",
                (tenant, d_since, d_until),
            ).fetchall()
        unames = {r["user_id"]: (r["display_name"] or r["login_user"] or r["user_id"])
                  for r in c.execute(
                      "SELECT user_id, display_name, login_user FROM users WHERE tenant_id=%s",
                      (tenant,)).fetchall()}

    rows = [dict(r) for r in rows]
    geo = _geo_for_ips({r["ip"] for r in rows})

    if user_id:
        items = []
        for r in rows:
            g = geo.get(r["ip"])
            items.append({**r, "geo": _geo_label(g),
                          "city": (g or {}).get("city"), "country": (g or {}).get("country"),
                          "org": (g or {}).get("org"),
                          "lat": (g or {}).get("lat"), "lon": (g or {}).get("lon")})
        # nº de IPs distintos por dia
        from collections import defaultdict
        per_day = defaultdict(set)
        for r in rows:
            per_day[r["day"]].add(r["ip"])
        by_day = [{"day": d, "ips": len(s)} for d, s in sorted(per_day.items(), reverse=True)]
        return {"tenant": tenant, "user_id": user_id,
                "display_name": unames.get(user_id, user_id),
                "items": items, "by_day": by_day,
                "distinct_ips": len({r["ip"] for r in rows})}

    # Resumo por pessoa
    from collections import defaultdict
    agg = defaultdict(lambda: {"ips": set(), "days": set(), "last_seen": "", "last_ip": ""})
    for r in rows:
        a = agg[r["user_id"]]
        a["ips"].add(r["ip"]); a["days"].add(r["day"])
        if r["last_seen"] > a["last_seen"]:
            a["last_seen"] = r["last_seen"]; a["last_ip"] = r["ip"]
    users_out = []
    for uid, a in agg.items():
        g = geo.get(a["last_ip"])
        users_out.append({
            "user_id": uid, "display_name": unames.get(uid, uid),
            "distinct_ips": len(a["ips"]), "days": len(a["days"]),
            "last_ip": a["last_ip"], "last_seen": a["last_seen"],
            "last_geo": _geo_label(g),
            "last_city": (g or {}).get("city"), "last_country": (g or {}).get("country"),
            "last_lat": (g or {}).get("lat"), "last_lon": (g or {}).get("lon"),
        })
    if _allowed is not None:
        _aset = set(_allowed)
        users_out = [u for u in users_out if u["user_id"] in _aset]
    users_out.sort(key=lambda x: (-x["distinct_ips"], x["display_name"].lower()))
    return {"tenant": tenant, "since": d_since, "until": d_until, "users": users_out}


@app.get("/v1/admin/geo-test", dependencies=[Depends(require_admin)])
def admin_geo_test(ip: str = "8.8.8.8"):
    """Diagnóstico: testa a resolução geo do IP a partir do servidor."""
    import urllib.request
    out = {"ip": ip, "is_public": _is_public_ip(ip)}
    with db() as c:
        row = c.execute("SELECT * FROM ip_geo WHERE ip=%s", (ip,)).fetchone()
    out["cached"] = (dict(row) if row else None)
    token = os.getenv("WORKPULSE_IPINFO_TOKEN", "")
    out["provider"] = "ipinfo" if token else "freeipapi"
    url = (f"https://ipinfo.io/{ip}/json?token=***" if token else f"https://freeipapi.com/api/json/{ip}")
    out["url"] = url
    try:
        real = (f"https://ipinfo.io/{ip}/json?token={token}" if token else f"https://freeipapi.com/api/json/{ip}")
        req = urllib.request.Request(real, headers={
            "User-Agent": "WorkPulse/1.0 (+https://workpulse.com.br)", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=6) as r:
            out["http_status"] = getattr(r, "status", None) or r.getcode()
            out["raw"] = r.read().decode("utf-8")[:1000]
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    out["parsed"] = _fetch_geo(ip)
    return out


# ===========================================================================
# Escopo do Líder — quais colaboradores cada Líder pode ver
# ===========================================================================
_SCHEMA_STMTS.extend([
    """
    CREATE TABLE IF NOT EXISTS lider_scope (
        tenant_id   TEXT NOT NULL,
        lider_id    BIGINT NOT NULL,
        scope_type  TEXT NOT NULL,   -- 'department' | 'user'
        scope_value TEXT NOT NULL,   -- nome do departamento OU user_id do colaborador
        PRIMARY KEY (tenant_id, lider_id, scope_type, scope_value)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_lider_scope ON lider_scope(tenant_id, lider_id)",
])


class LiderScopeIn(BaseModel):
    departments: list[str] = []
    users: list[str] = []


def _lider_allowed_ids(request: Request, tenant: str):
    """Para uma sessão de LÍDER, devolve a lista de user_ids (colaboradores) que
    ele pode ver. Devolve None quando NÃO há restrição (admin_master, supervisor,
    wp_admin ou acesso por token). Lista vazia = Líder sem nada atribuído (vê nada)."""
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return None  # acesso por token/break-glass → sem restrição
    u = _load_session_user(sid)
    if not u or u.get("role") != "lider" or u.get("tenant_id") != tenant:
        return None
    with db() as c:
        rows = c.execute(
            "SELECT scope_type, scope_value FROM lider_scope WHERE tenant_id=%s AND lider_id=%s",
            (tenant, u["id"]),
        ).fetchall()
    depts = [r["scope_value"] for r in rows if r["scope_type"] == "department"]
    users = set(r["scope_value"] for r in rows if r["scope_type"] == "user")
    if depts:
        with db() as c:
            urows = c.execute(
                "SELECT user_id FROM users WHERE tenant_id=%s AND department = ANY(%s)",
                (tenant, depts),
            ).fetchall()
        users.update(r["user_id"] for r in urows)
    return list(users)


def _scope_for(request: Request, tenant: str):
    """Devolve (allowed_or_None, cache_tag). allowed=None => sem restrição.
    cache_tag entra na chave de cache para o Líder não pegar o cache do time todo."""
    allowed = _lider_allowed_ids(request, tenant)
    tag = "" if allowed is None else (":scope:" + ",".join(sorted(allowed)) or ":scope:none")
    return allowed, tag


@app.get("/v1/tenant-users/{uid}/scope")
def tenant_user_scope_get(uid: int, tenant: str,
                          ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    with db() as c:
        rows = c.execute(
            "SELECT scope_type, scope_value FROM lider_scope WHERE tenant_id=%s AND lider_id=%s",
            (tid, uid),
        ).fetchall()
    return {
        "departments": [r["scope_value"] for r in rows if r["scope_type"] == "department"],
        "users":       [r["scope_value"] for r in rows if r["scope_type"] == "user"],
    }


@app.put("/v1/tenant-users/{uid}/scope")
def tenant_user_scope_set(uid: int, body: LiderScopeIn, tenant: str, request: Request,
                          ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    user = _fetch_tenant_user(uid, tid, ctx["role"])
    with db() as c:
        c.execute("DELETE FROM lider_scope WHERE tenant_id=%s AND lider_id=%s", (tid, uid))
        for d in set(x for x in body.departments if x):
            c.execute(
                """INSERT INTO lider_scope (tenant_id, lider_id, scope_type, scope_value)
                   VALUES (%s,%s,'department',%s)
                   ON CONFLICT (tenant_id, lider_id, scope_type, scope_value) DO NOTHING""",
                (tid, uid, d),
            )
        for usr in set(x for x in body.users if x):
            c.execute(
                """INSERT INTO lider_scope (tenant_id, lider_id, scope_type, scope_value)
                   VALUES (%s,%s,'user',%s)
                   ON CONFLICT (tenant_id, lider_id, scope_type, scope_value) DO NOTHING""",
                (tid, uid, usr),
            )
    audit(ctx["email"], "lider_scope.set", target=user["email"],
          detail=f"depts={len(set(body.departments))};users={len(set(body.users))}",
          ip=_client_ip(request))
    return {"ok": True}


# ===========================================================================
# Financeiro — preços por plano, billing por tenant (override/desconto),
# histórico de pagamentos (baixa manual). Valores em CENTAVOS (BRL).
# ===========================================================================
_SCHEMA_STMTS.extend([
    """
    CREATE TABLE IF NOT EXISTS plan_prices (
        plan        TEXT PRIMARY KEY,
        price_cents INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tenant_billing (
        tenant_id        TEXT PRIMARY KEY,
        seat_price_cents INTEGER,              -- override absoluto do preço/seat (NULL = usa plano)
        discount_pct     REAL NOT NULL DEFAULT 0,
        updated_at       TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS payments (
        id           BIGSERIAL PRIMARY KEY,
        tenant_id    TEXT NOT NULL,
        competencia  TEXT NOT NULL,            -- 'AAAA-MM'
        amount_cents INTEGER NOT NULL,
        paid_at      TEXT,                     -- 'AAAA-MM-DD'
        method       TEXT,
        note         TEXT,
        created_by   TEXT,
        created_at   TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_payments_tenant ON payments(tenant_id)",
    # Preços padrão (placeholder editável). starter/business/enterprise.
    "INSERT INTO plan_prices (plan, price_cents) VALUES ('starter', 1500)    ON CONFLICT (plan) DO NOTHING",
    "INSERT INTO plan_prices (plan, price_cents) VALUES ('business', 2500)   ON CONFLICT (plan) DO NOTHING",
    "INSERT INTO plan_prices (plan, price_cents) VALUES ('enterprise', 4000) ON CONFLICT (plan) DO NOTHING",
])

_PLAN_ORDER = ["starter", "business", "enterprise"]


def _plan_prices_map() -> dict:
    with db() as c:
        rows = c.execute("SELECT plan, price_cents FROM plan_prices").fetchall()
    return {r["plan"]: int(r["price_cents"]) for r in rows}


def _effective_seat_cents(tenant: str, plan: str):
    """Devolve (base_cents, discount_pct, eff_cents). base = override do tenant
    se houver, senão preço do plano. eff = base com desconto aplicado."""
    prices = _plan_prices_map()
    plan_cents = prices.get((plan or "").strip(), 0)
    with db() as c:
        tb = c.execute(
            "SELECT seat_price_cents, discount_pct FROM tenant_billing WHERE tenant_id=%s",
            (tenant,),
        ).fetchone()
    override = tb["seat_price_cents"] if tb and tb["seat_price_cents"] is not None else None
    disc = float(tb["discount_pct"]) if tb and tb["discount_pct"] is not None else 0.0
    base = override if override is not None else plan_cents
    eff = int(round(base * (1.0 - disc / 100.0)))
    return base, disc, eff


def _payments_list(tenant: str) -> list:
    with db() as c:
        rows = c.execute(
            """SELECT id, competencia, amount_cents, paid_at, method, note, created_at
               FROM payments WHERE tenant_id=%s
               ORDER BY competencia DESC, id DESC""",
            (tenant,),
        ).fetchall()
    return [dict(r) for r in rows]


# --- Modelos ---
class PlanPricesIn(BaseModel):
    prices: dict[str, int]   # plan -> centavos


class TenantBillingIn(BaseModel):
    seat_price_cents: int | None = None   # None/0 → usa preço do plano
    discount_pct: float = 0


class PaymentIn(BaseModel):
    competencia: str          # 'AAAA-MM'
    amount_cents: int
    paid_at: str | None = None
    method: str | None = None
    note: str | None = None


# --- Endpoints PLATAFORMA (WorkPulse admin) ---
@app.get("/v1/admin/plan-prices", dependencies=[Depends(require_admin)])
def admin_plan_prices_get():
    prices = _plan_prices_map()
    return {"prices": {p: prices.get(p, 0) for p in _PLAN_ORDER}}


@app.put("/v1/admin/plan-prices", dependencies=[Depends(require_admin)])
def admin_plan_prices_set(body: PlanPricesIn):
    with db() as c:
        for plan, cents in body.prices.items():
            if plan not in _PLAN_ORDER:
                continue
            cents = max(0, int(cents))
            c.execute(
                "INSERT INTO plan_prices (plan, price_cents) VALUES (%s,%s) "
                "ON CONFLICT (plan) DO UPDATE SET price_cents=EXCLUDED.price_cents",
                (plan, cents),
            )
    return {"ok": True, "prices": _plan_prices_map()}


@app.get("/v1/admin/tenant-billing", dependencies=[Depends(require_admin)])
def admin_tenant_billing_get(tenant: str):
    with db() as c:
        lic = c.execute(
            "SELECT plan, seats FROM licenses WHERE tenant_id=%s ORDER BY issued DESC LIMIT 1",
            (tenant,),
        ).fetchone()
    plan = (lic["plan"] if lic else "") or ""
    seats = int(lic["seats"]) if lic and lic.get("seats") is not None else 0
    base, disc, eff = _effective_seat_cents(tenant, plan)
    prices = _plan_prices_map()
    with db() as c:
        tb = c.execute(
            "SELECT seat_price_cents, discount_pct FROM tenant_billing WHERE tenant_id=%s",
            (tenant,),
        ).fetchone()
    return {
        "tenant": tenant,
        "plan": plan,
        "seats": seats,
        "plan_price_cents": prices.get(plan, 0),
        "seat_price_cents": (tb["seat_price_cents"] if tb else None),
        "discount_pct": disc,
        "effective_seat_cents": eff,
        "monthly_cents": eff * seats,
        "payments": _payments_list(tenant),
    }


@app.put("/v1/admin/tenant-billing", dependencies=[Depends(require_admin)])
def admin_tenant_billing_set(body: TenantBillingIn, tenant: str):
    sp = body.seat_price_cents
    if sp is not None:
        sp = int(sp)
        if sp <= 0:
            sp = None
    disc = max(0.0, min(100.0, float(body.discount_pct or 0)))
    with db() as c:
        c.execute(
            """INSERT INTO tenant_billing (tenant_id, seat_price_cents, discount_pct, updated_at)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT (tenant_id) DO UPDATE
                 SET seat_price_cents=EXCLUDED.seat_price_cents,
                     discount_pct=EXCLUDED.discount_pct,
                     updated_at=EXCLUDED.updated_at""",
            (tenant, sp, disc, _now_utc().isoformat()),
        )
    return {"ok": True}


@app.get("/v1/admin/payments", dependencies=[Depends(require_admin)])
def admin_payments_get(tenant: str):
    return {"payments": _payments_list(tenant)}


@app.post("/v1/admin/payments", dependencies=[Depends(require_admin)])
def admin_payments_add(body: PaymentIn, tenant: str, request: Request):
    comp = (body.competencia or "").strip()
    if len(comp) != 7 or comp[4] != "-":
        raise HTTPException(400, "competência inválida (use AAAA-MM)")
    amt = int(body.amount_cents)
    if amt < 0:
        raise HTTPException(400, "valor inválido")
    with db() as c:
        c.execute(
            """INSERT INTO payments (tenant_id, competencia, amount_cents, paid_at, method, note, created_by, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (tenant, comp, amt, (body.paid_at or None), (body.method or None),
             (body.note or None), "platform", _now_utc().isoformat()),
        )
    return {"ok": True}


@app.delete("/v1/admin/payments/{pid}", dependencies=[Depends(require_admin)])
def admin_payments_del(pid: int, tenant: str):
    with db() as c:
        c.execute("DELETE FROM payments WHERE id=%s AND tenant_id=%s", (pid, tenant))
    return {"ok": True}


# --- Endpoint CLIENTE (Admin Master) — página Financeiro ---
@app.get("/v1/tenant-finance")
def tenant_finance_get(tenant: str, ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    with db() as c:
        lic = c.execute(
            "SELECT plan, seats, expires FROM licenses WHERE tenant_id=%s ORDER BY issued DESC LIMIT 1",
            (tid,),
        ).fetchone()
        used = c.execute(
            "SELECT COUNT(DISTINCT user_id) AS n FROM users WHERE tenant_id=%s", (tid,)
        ).fetchone()["n"]
    plan = (lic["plan"] if lic else "") or ""
    seats = int(lic["seats"]) if lic and lic.get("seats") is not None else 0
    base, disc, eff = _effective_seat_cents(tid, plan)
    return {
        "currency": "BRL",
        "plan": plan,
        "seats": seats,
        "used": used,
        "expires": (lic["expires"] if lic else None),
        "seat_price_cents": eff,
        "discount_pct": disc,
        "monthly_cents": eff * seats,
        "payments": _payments_list(tid),
    }


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
