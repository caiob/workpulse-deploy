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
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
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
FROM_EMAIL   = os.getenv("WORKPULSE_FROM_EMAIL", "noreply@workpulse.com.br")  # fallback p/ remetente/notify (SES removido)
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
# TTL do cache dos painéis. Dashboard de monitoramento tolera alguns minutos de
# defasagem; um TTL maior faz recargas repetidas baterem em cache quente (rápido)
# em vez de re-agregar a tabela events. Ajustável via env.
_CACHE_TTL  = int(os.getenv("WORKPULSE_CACHE_TTL", "300"))  # segundos


def _cache_get(key: str) -> Any | None:
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry and (time.monotonic() - entry[0]) < _CACHE_TTL:
            return entry[1]
    return None


def _cache_set(key: str, val: Any) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), val)


class _Perf:
    """Cronômetro leve para medir gargalos. Loga só o que passar do limiar.
    Uso:  p = _Perf("overview"); ...; p.mark("totals"); ...; p.done()."""
    THRESHOLD_MS = float(os.getenv("WORKPULSE_PERF_MS", "200"))

    def __init__(self, label: str):
        self.label = label
        self.t0 = time.monotonic()
        self.last = self.t0
        self.parts: list[str] = []

    def mark(self, name: str) -> None:
        now = time.monotonic()
        ms = (now - self.last) * 1000.0
        self.last = now
        self.parts.append(f"{name}={ms:.0f}ms")

    def done(self) -> None:
        total = (time.monotonic() - self.t0) * 1000.0
        if total >= self.THRESHOLD_MS:
            print(f"[perf] {self.label} total={total:.0f}ms | " + " ".join(self.parts), flush=True)


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


def _only_browser_where() -> tuple[str, list]:
    """SQL fragment + args para manter SÓ apps de navegador (inverso de
    _no_browser_where). Pré-filtra a varredura para uma fração das linhas."""
    in_ph   = ", ".join(["%s"] * len(_BROWSER_APPS))
    like_or = " OR ".join(
        "LOWER(data_json->>'app') LIKE %s"
        for _ in _BROWSER_KEYWORDS
    )
    sql  = f" AND (LOWER(data_json->>'app') IN ({in_ph}) OR ({like_or}))"
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
# ROLLUP (agregados) — compute, worker e helpers de leitura
# ---------------------------------------------------------------------------
ROLLUP_INTERVAL_SEC  = int(os.getenv("WORKPULSE_ROLLUP_SEC", "300"))          # refresh a cada 5 min
ROLLUP_BACKFILL_DAYS = int(os.getenv("WORKPULSE_ROLLUP_BACKFILL_DAYS", "3"))  # dias antigos por ciclo (suave)
# Versão do esquema de rollup. Ao subir (ex.: nova tabela agg_app_day), o worker
# reabre o backfill UMA vez por tenant p/ repreencher os agregados antigos.
ROLLUP_SCHEMA_VER    = 2


def _local_day_utc_bounds(local_day: dt.date) -> tuple[str, str]:
    """(início, fim) em ISO-UTC cobrindo o dia LOCAL inteiro.
    BRT = UTC + DASH_TZ_OFFSET_HOURS (-3) → meia-noite local = 03:00 UTC."""
    start = dt.datetime(local_day.year, local_day.month, local_day.day,
                        tzinfo=dt.timezone.utc) - dt.timedelta(hours=DASH_TZ_OFFSET_HOURS)
    return start.isoformat(), (start + dt.timedelta(days=1)).isoformat()


def _today_local(now: dt.datetime) -> dt.date:
    return (now + dt.timedelta(hours=DASH_TZ_OFFSET_HOURS)).date()


def _rollup_hours(c, tenant: str, since: str, until: str) -> None:
    """(Re)agrega ativo/AFK por (usuário, hora-UTC) na faixa [since, until).
    Seguro p/ qualquer faixa: cada bucket de hora é independente."""
    c.execute(
        """INSERT INTO agg_user_hour (tenant_id, user_id, hour, active_sec, afk_sec)
           SELECT %s, user_id, substr(ts,1,13) AS hour,
                  SUM(CASE WHEN bucket='window' THEN duration_sec ELSE 0 END),
                  SUM(CASE WHEN bucket='afk' AND data_json->>'status'='afk'
                           THEN duration_sec ELSE 0 END)
           FROM events
           WHERE tenant_id=%s AND ts>=%s AND ts<%s
           GROUP BY user_id, substr(ts,1,13)
           ON CONFLICT (tenant_id, user_id, hour) DO UPDATE SET
               active_sec = EXCLUDED.active_sec,
               afk_sec    = EXCLUDED.afk_sec""",
        (tenant, tenant, since, until),
    )


def _rollup_days(c, tenant: str, since: str, until: str) -> None:
    """(Re)agrega a JORNADA por (usuário, dia-LOCAL). A faixa DEVE cobrir dias
    locais inteiros (senão start/end/afk_in saem parciais)."""
    c.execute(
        """INSERT INTO agg_user_day
               (tenant_id, user_id, day, focus_sec, afk_in_sec, afk_after_sec, start_sec, end_sec)
           WITH w AS (
               SELECT user_id,
                      (ts::timestamptz AT TIME ZONE %(tz)s)                 AS lts,
                      (ts::timestamptz AT TIME ZONE %(tz)s)
                          + (duration_sec || ' seconds')::interval          AS lend,
                      duration_sec
               FROM events
               WHERE tenant_id=%(tenant)s AND bucket='window'
                 AND ts>=%(since)s AND ts<%(until)s
           ),
           win AS (
               SELECT user_id, lts::date AS day, MIN(lts) AS start_ts,
                      MAX(lend) AS end_ts, SUM(duration_sec) AS focus_sec
               FROM w GROUP BY user_id, lts::date
           ),
           afk_ev AS (
               SELECT user_id,
                      (ts::timestamptz AT TIME ZONE %(tz)s)        AS lts,
                      (ts::timestamptz AT TIME ZONE %(tz)s)::date  AS day,
                      duration_sec
               FROM events
               WHERE tenant_id=%(tenant)s AND bucket='afk'
                 AND data_json->>'status'='afk'
                 AND ts>=%(since)s AND ts<%(until)s
           ),
           afk AS (
               SELECT a.user_id, a.day,
                      COALESCE(SUM(a.duration_sec) FILTER (
                          WHERE a.lts >= win.start_ts AND a.lts <= win.end_ts), 0) AS afk_in,
                      COALESCE(SUM(a.duration_sec) FILTER (
                          WHERE a.lts > win.end_ts), 0)                            AS afk_after
               FROM afk_ev a JOIN win ON win.user_id=a.user_id AND win.day=a.day
               GROUP BY a.user_id, a.day
           )
           SELECT %(tenant)s, win.user_id, to_char(win.day,'YYYY-MM-DD'),
                  win.focus_sec,
                  COALESCE(afk.afk_in, 0), COALESCE(afk.afk_after, 0),
                  EXTRACT(EPOCH FROM (win.start_ts - win.day::timestamp)),
                  EXTRACT(EPOCH FROM (win.end_ts   - win.day::timestamp))
           FROM win LEFT JOIN afk ON afk.user_id=win.user_id AND afk.day=win.day
           ON CONFLICT (tenant_id, user_id, day) DO UPDATE SET
               focus_sec     = EXCLUDED.focus_sec,
               afk_in_sec    = EXCLUDED.afk_in_sec,
               afk_after_sec = EXCLUDED.afk_after_sec,
               start_sec     = EXCLUDED.start_sec,
               end_sec       = EXCLUDED.end_sec""",
        {"tz": REPORT_TZ, "tenant": tenant, "since": since, "until": until},
    )


def _rollup_apps(c, tenant: str, since: str, until: str) -> None:
    """(Re)agrega tempo ativo por (dia-LOCAL, app). Faixa = dias locais inteiros."""
    c.execute(
        """INSERT INTO agg_app_day (tenant_id, day, app, active_sec)
           SELECT %(tenant)s,
                  to_char((ts::timestamptz AT TIME ZONE %(tz)s)::date, 'YYYY-MM-DD') AS day,
                  MIN(data_json->>'app')                                             AS app,
                  SUM(duration_sec)                                                  AS active_sec
           FROM events
           WHERE tenant_id=%(tenant)s AND bucket='window'
             AND ts>=%(since)s AND ts<%(until)s
             AND data_json->>'app' IS NOT NULL
           GROUP BY (ts::timestamptz AT TIME ZONE %(tz)s)::date, LOWER(data_json->>'app')
           ON CONFLICT (tenant_id, day, app) DO UPDATE SET active_sec = EXCLUDED.active_sec""",
        {"tz": REPORT_TZ, "tenant": tenant, "since": since, "until": until},
    )


def _rollup_local_day(c, tenant: str, local_day: dt.date) -> None:
    s, u = _local_day_utc_bounds(local_day)
    _rollup_hours(c, tenant, s, u)
    _rollup_days(c, tenant, s, u)
    _rollup_apps(c, tenant, s, u)


ROLLUP_LOCK_KEY = 0x52504C50  # 'RPLP' — advisory lock p/ 1 backfiller por vez


def _do_rollups() -> None:
    """Refresh dos dias quentes (ontem+hoje) + backfill incremental p/ trás.

    Proteções (o backfill usa queries pesadas com casts e não pode derrubar o
    banco/pool sob carga):
      - kill-switch por env WORKPULSE_ROLLUP_ENABLED;
      - advisory lock: SÓ UM processo faz rollup por ciclo (há N workers web);
      - statement_timeout: nenhuma query do rollup segura a conexão p/ sempre;
      - lote pequeno + pausa entre dias p/ ceder CPU às requisições dos usuários.
    Toda a operação roda numa ÚNICA conexão (a que detém a trava)."""
    # LIGADO por padrão (RDS subiu p/ t3.small + backfill resiliente, 2026-06-18).
    # Kill-switch de emergência: WORKPULSE_ROLLUP_ENABLED=0 desliga.
    if os.getenv("WORKPULSE_ROLLUP_ENABLED", "1") != "1":
        return
    try:
        with db() as c:
            got = c.execute("SELECT pg_try_advisory_lock(%s) AS ok", (ROLLUP_LOCK_KEY,)).fetchone()
            if not got or not got["ok"]:
                return  # outra instância já está cuidando deste ciclo
            try:
                # Protege a conexão: queries do rollup têm teto de tempo.
                c.execute("SET statement_timeout = %s",
                          (os.getenv("WORKPULSE_ROLLUP_STMT_TIMEOUT", "20s"),))
                now   = dt.datetime.now(dt.timezone.utc)
                today = _today_local(now)
                yday  = today - dt.timedelta(days=1)
                sleep_s = float(os.getenv("WORKPULSE_ROLLUP_SLEEP_SEC", "2"))

                tenants = c.execute(
                    "SELECT id, COALESCE(retention_days, %s) AS days FROM tenants WHERE status='active'",
                    (_DEFAULT_RETENTION_DAYS,),
                ).fetchall()

                for t in tenants:
                    tid    = t["id"]
                    cutoff = today - dt.timedelta(days=int(t["days"]))
                    try:
                        c.execute(
                            "INSERT INTO rollup_state (tenant_id, updated_at) VALUES (%s,%s) "
                            "ON CONFLICT (tenant_id) DO NOTHING",
                            (tid, now.isoformat()),
                        )
                        st = c.execute(
                            "SELECT oldest_day, done, ver FROM rollup_state WHERE tenant_id=%s", (tid,)
                        ).fetchone()

                        # 1) Dias QUENTES: ontem (completo) e hoje (parcial). Cada
                        #    dia é resiliente — falha de um não aborta o ciclo.
                        for _hot in (yday, today):
                            try:
                                _rollup_local_day(c, tid, _hot)
                            except Exception as exc:
                                print(f"[rollup_worker] dia quente {_hot} pulado (tenant={tid}): {exc}", flush=True)
                            time.sleep(sleep_s)

                        oldest = (st["oldest_day"] if st and st["oldest_day"] else yday.isoformat())
                        if oldest > yday.isoformat():
                            oldest = yday.isoformat()
                        done = bool(st["done"]) if st else False

                        # Migração de esquema: reabre o backfill p/ repreencher
                        # agregados novos (ex.: agg_app_day). Uma vez por tenant.
                        if (st["ver"] if st and st["ver"] is not None else 0) < ROLLUP_SCHEMA_VER:
                            oldest = yday.isoformat()
                            done   = False

                        # 2) BACKFILL: poucos dias por ciclo. SEMPRE avança (pula o
                        #    dia que falhar) — nunca trava reprocessando o mesmo dia.
                        if not done:
                            cur = dt.date.fromisoformat(oldest)
                            for _ in range(ROLLUP_BACKFILL_DAYS):
                                prev = cur - dt.timedelta(days=1)
                                if prev < cutoff:
                                    done = True
                                    break
                                try:
                                    _rollup_local_day(c, tid, prev)
                                except Exception as exc:
                                    print(f"[rollup_worker] backfill {prev} pulado (tenant={tid}): {exc}", flush=True)
                                cur = prev   # avança mesmo se o dia falhar
                                time.sleep(sleep_s)
                            oldest = cur.isoformat()

                        c.execute(
                            "UPDATE rollup_state SET oldest_day=%s, done=%s, ver=%s, updated_at=%s "
                            "WHERE tenant_id=%s",
                            (oldest, done, ROLLUP_SCHEMA_VER, now.isoformat(), tid),
                        )
                    except Exception as exc:
                        print(f"[rollup_worker] tenant={tid} error: {exc}", flush=True)
            finally:
                try:
                    c.execute("SET statement_timeout = 0")
                except Exception:
                    pass
                c.execute("SELECT pg_advisory_unlock(%s)", (ROLLUP_LOCK_KEY,))
    except Exception as exc:
        print(f"[rollup_worker] error: {exc}", flush=True)


async def _rollup_worker() -> None:
    await asyncio.sleep(20)   # deixa o boot respirar
    while True:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _do_rollups)
        await asyncio.sleep(ROLLUP_INTERVAL_SEC)


def _agg_ready(c, tenant: str, since: str) -> bool:
    """True se o range [since..] já está coberto pelos agregados (backfill alcançou).
    Com o rollup DESLIGADO, retorna False → os endpoints leem os eventos crus
    (dados frescos, com o cache segurando), evitando servir agregado parado."""
    if os.getenv("WORKPULSE_ROLLUP_ENABLED", "1") != "1":
        return False
    r = c.execute("SELECT oldest_day FROM rollup_state WHERE tenant_id=%s", (tenant,)).fetchone()
    if not r or not r["oldest_day"]:
        return False
    return _utc_hour_to_local_day(since[:13]) >= r["oldest_day"]


def _hour_rows(c, tenant, since, until, SC, A):
    """Linhas (h, user_id, active, afk): do agregado se pronto, senão varre events."""
    if _agg_ready(c, tenant, since):
        return c.execute(
            f"""SELECT hour AS h, user_id, active_sec AS active, afk_sec AS afk
                FROM agg_user_hour
                WHERE tenant_id=%s{SC} AND hour>=%s AND hour<=%s""",
            tuple([tenant, *A, since[:13], until[:13]]),
        ).fetchall()
    return c.execute(
        f"""SELECT substr(ts,1,13) AS h, user_id,
                   SUM(CASE WHEN bucket='window' THEN duration_sec ELSE 0 END) AS active,
                   SUM(CASE WHEN bucket='afk' AND data_json->>'status'='afk'
                            THEN duration_sec ELSE 0 END) AS afk
            FROM events WHERE tenant_id=%s{SC} AND ts>=%s AND ts<=%s
            GROUP BY h, user_id""",
        tuple([tenant, *A, since, until]),
    ).fetchall()


def _top_apps(c, tenant, since, until, SC, A, scoped):
    """Top 15 apps por tempo ativo. Lê de agg_app_day quando pronto E sem escopo
    de líder (o agregado não guarda user_id); senão varre events."""
    if not scoped and _agg_ready(c, tenant, since):
        d0 = _utc_hour_to_local_day(since[:13])
        d1 = _utc_hour_to_local_day(until[:13])
        return c.execute(
            """SELECT MIN(app) AS app, SUM(active_sec) AS seconds
               FROM agg_app_day
               WHERE tenant_id=%s AND day>=%s AND day<=%s
               GROUP BY LOWER(app) ORDER BY seconds DESC LIMIT 15""",
            (tenant, d0, d1),
        ).fetchall()
    return c.execute(
        f"""SELECT MIN(data_json->>'app') AS app, SUM(duration_sec) AS seconds
           FROM events
           WHERE tenant_id=%s{SC} AND ts>=%s AND ts<=%s AND bucket='window'
           GROUP BY LOWER(data_json->>'app') ORDER BY seconds DESC LIMIT 15""",
        tuple([tenant, *A, since, until]),
    ).fetchall()


def _afk_in_by_user(c, tenant, since, until):
    """[(user_id, afk_in)]: do agregado se pronto, senão CTE sobre events."""
    if _agg_ready(c, tenant, since):
        d0 = _utc_hour_to_local_day(since[:13])
        d1 = _utc_hour_to_local_day(until[:13])
        return c.execute(
            """SELECT user_id, SUM(afk_in_sec) AS afk_in
               FROM agg_user_day
               WHERE tenant_id=%s AND day>=%s AND day<=%s
               GROUP BY user_id""",
            (tenant, d0, d1),
        ).fetchall()
    return c.execute(
        """
        WITH w AS (
            SELECT user_id,
                   (ts::timestamptz AT TIME ZONE %(tz)s)                 AS lts,
                   (ts::timestamptz AT TIME ZONE %(tz)s)
                       + (duration_sec || ' seconds')::interval          AS lend
            FROM events
            WHERE tenant_id = %(tenant)s AND bucket = 'window'
              AND ts >= %(since)s AND ts < %(until)s
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
              AND ts >= %(since)s AND ts < %(until)s
        )
        SELECT a.user_id,
               COALESCE(SUM(a.duration_sec) FILTER (
                   WHERE a.lts >= win.start_ts AND a.lts <= win.end_ts), 0) AS afk_in
        FROM afk_ev a JOIN win ON win.user_id = a.user_id AND win.day = a.day
        GROUP BY a.user_id
        """,
        {"tz": REPORT_TZ, "tenant": tenant, "since": since, "until": until},
    ).fetchall()


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
    task_alerts    = asyncio.create_task(_alerts_worker())
    task_rollup    = asyncio.create_task(_rollup_worker())
    yield
    task_retention.cancel()
    task_reports.cancel()
    task_alerts.cancel()
    task_rollup.cancel()
    for t in (task_retention, task_reports, task_alerts, task_rollup):
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

    # Bloqueio do agente: se o Device foi excluído/bloqueado pelo Admin Master, descarta
    # o envio — NÃO recria o usuário nem grava eventos. Responde ok para o agente não
    # ficar reenviando em loop. (Desbloquear remove daqui e a coleta volta a entrar.)
    with db() as c:
        _blk = c.execute(
            "SELECT 1 FROM blocked_devices WHERE tenant_id=%s AND user_id=%s",
            (claims["tenant"], user_id),
        ).fetchone()
    if _blk:
        return {"ok": True, "blocked": True, "stored": 0}

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
    SC  = " AND user_id = ANY(%s)"   if allowed is not None else ""
    SCU = " AND u.user_id = ANY(%s)" if allowed is not None else ""
    A   = [allowed] if allowed is not None else []
    cache_key = f"{tenant}:team_summary:{since}:{until}{_stag}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    _perf = _Perf(f"team_summary tenant={tenant} since={since}")
    with db() as c:
        _perf.mark("connect")
        # Ativo/AFK por usuário — do agregado (agg_user_hour) quando pronto.
        _sum: dict[str, list] = {}
        for r in _hour_rows(c, tenant, since, until, SC, A):
            s = _sum.setdefault(r["user_id"], [0.0, 0.0])
            s[0] += float(r["active"] or 0.0)
            s[1] += float(r["afk"]    or 0.0)
        _perf.mark("active_afk")

        # Metadados dos usuários (tabela pequena) + versão do agente.
        meta = c.execute(
            f"""SELECT u.user_id, u.display_name, u.email, u.department, u.last_seen,
                       (SELECT d.agent_version FROM devices d
                        WHERE d.tenant_id = u.tenant_id AND d.user_id = u.user_id
                        ORDER BY d.last_seen DESC LIMIT 1) AS agent_version
                FROM users u WHERE u.tenant_id=%s{SCU}""",
            tuple([tenant, *A]),
        ).fetchall()
        _perf.mark("meta")

        global_top = _top_apps(c, tenant, since, until, SC, A, allowed is not None)
        _perf.mark("global_top")

        # AFK *dentro da jornada* por colaborador — do agregado (agg_user_day)
        # quando pronto; senão CTE sobre events (mesma lógica de /v1/jornada).
        afk_in_rows = _afk_in_by_user(c, tenant, since, until)
        _perf.mark("afk_in")
    _perf.done()

    afk_in_map = {r["user_id"]: float(r["afk_in"] or 0.0) for r in afk_in_rows}

    users_out = []
    for u in meta:
        uid       = u["user_id"]
        act, afkv = _sum.get(uid, (0.0, 0.0))
        users_out.append({
            "user_id":       uid,
            "display_name":  u["display_name"],
            "email":         u["email"],
            "department":    u["department"],
            "last_seen":     u["last_seen"],
            "agent_version": u["agent_version"],
            "active":        act,
            "afk":           afkv,
            "afk_in":        afk_in_map.get(uid, 0.0),   # AFK na Jornada (exclui pós-jornada)
        })
    users_out.sort(key=lambda d: d["active"], reverse=True)

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


@app.get("/v1/productivity/consolidado", dependencies=[Depends(require_tenant_scope)])
def productivity_consolidado(
    tenant:  str,
    request: Request,
    user_id: str | None = None,
    bucket:  str | None = None,   # ''/'window' = atividade | 'afk' | 'all' = todos
    since:   str | None = None,
    until:   str | None = None,
    days:    int = Query(1, le=90),
):
    """Coleta CONSOLIDADA de uma pessoa no período — agregada NO SERVIDOR por
    app/site (o dia todo, sem limite de eventos). Inclui browsers (internet) e,
    opcionalmente, AFK. É o backend da aba Consolidado."""
    if since is None:
        since = _since_for_days(days)
    if until is None:
        until = dt.datetime.now(dt.timezone.utc).isoformat()
    allowed = _lider_allowed_ids(request, tenant)
    if allowed is not None and user_id and user_id not in set(allowed):
        raise HTTPException(status_code=403, detail="colaborador fora do seu escopo")

    all_b = (bucket == "all")
    one_b = None if all_b else (bucket or "window")
    cache_key = f"{tenant}:consolidado:{user_id or ''}:{since}:{until}:{bucket or 'window'}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    where = ["tenant_id=%s", "ts>=%s", "ts<=%s"]
    args: list = [tenant, since, until]
    if not all_b:
        where.append("bucket=%s"); args.append(one_b)
    if user_id:
        where.append("user_id=%s"); args.append(user_id)
    elif allowed is not None:
        where.append("user_id = ANY(%s)"); args.append(allowed)
    wc = " AND ".join(where)

    with db() as c:
        rules = [dict(r) for r in c.execute(
            "SELECT * FROM productivity_rules WHERE tenant_id=%s ORDER BY created_at", (tenant,),
        ).fetchall()]
        agg = c.execute(
            f"""SELECT data_json->>'app' AS app, data_json->>'title' AS title, bucket,
                       SUM(duration_sec) AS total_sec, MAX(ts) AS last_ts
                FROM events WHERE {wc}
                GROUP BY app, title, bucket""",
            tuple(args),
        ).fetchall()
        day_raw = c.execute(
            f"""SELECT substr(ts,1,13) AS h, data_json->>'app' AS app,
                       data_json->>'title' AS title, bucket, SUM(duration_sec) AS total_sec
                FROM events WHERE {wc}
                GROUP BY h, app, title, bucket""",
            tuple(args),
        ).fetchall()

    def _cat(row):
        return "afk" if row["bucket"] == "afk" else _apply_rules(_clean_title(row["title"] or ""), row["app"] or "", rules)

    totals = {"productive": 0.0, "unproductive": 0.0, "neutral": 0.0, "uncategorized": 0.0, "afk": 0.0, "total": 0.0}
    items = []
    for row in agg:
        cat = _cat(row); sec = row["total_sec"] or 0.0
        totals[cat]    = totals.get(cat, 0.0) + sec
        totals["total"] += sec
        app  = row["app"] or "—"
        tipo = "AFK" if row["bucket"] == "afk" else ("Internet" if _is_browser(app) else "App")
        items.append({
            "app": app, "title": _clean_title(row["title"] or ""),
            "total_sec": round(sec, 1), "category": cat, "type": tipo,
            "last_access": row["last_ts"],
        })
    items.sort(key=lambda x: -x["total_sec"])
    items = items[:500]   # top 500 por tempo — payload enxuto, cobre o dia

    dagg: dict[str, dict] = {}
    for row in day_raw:
        day = _utc_hour_to_local_day(row["h"]); cat = _cat(row)
        d = dagg.setdefault(day, {"productive": 0.0, "unproductive": 0.0, "neutral": 0.0, "uncategorized": 0.0, "afk": 0.0})
        d[cat] = d.get(cat, 0.0) + (row["total_sec"] or 0.0)
    daily = [{"day": k, **{kk: round(vv, 1) for kk, vv in v.items()}} for k, v in sorted(dagg.items())]

    result = {
        "tenant": tenant, "since": since, "until": until,
        "totals": {k: round(v, 1) for k, v in totals.items()},
        "items":  items,
        "daily":  daily,
    }
    _cache_set(cache_key, result)
    return result


@app.get("/v1/productivity/timeline", dependencies=[Depends(require_tenant_scope)])
def productivity_timeline(
    tenant:  str,
    request: Request,
    user_id: str,
    bucket:  str | None = None,   # ''/'window' | 'afk' | 'all' (recomendado p/ ver tudo)
    since:   str | None = None,
    until:   str | None = None,
    days:    int = Query(1, le=90),
):
    """TIMELINE de uma pessoa: tudo que ela fez na ORDEM cronológica, colapsando
    eventos consecutivos iguais em uma linha (ex.: 45 min num app = 1 linha).
    Processado NO SERVIDOR → cobre o dia todo, sem o limite de eventos."""
    if not (user_id or "").strip():
        raise HTTPException(status_code=400, detail="informe user_id")
    if since is None:
        since = _since_for_days(days)
    if until is None:
        until = dt.datetime.now(dt.timezone.utc).isoformat()
    allowed = _lider_allowed_ids(request, tenant)
    if allowed is not None and user_id not in set(allowed):
        raise HTTPException(status_code=403, detail="colaborador fora do seu escopo")

    all_b = (bucket == "all")
    one_b = None if all_b else (bucket or "window")
    cache_key = f"{tenant}:timeline:{user_id}:{since}:{until}:{bucket or 'window'}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    where = ["tenant_id=%s", "user_id=%s", "ts>=%s", "ts<=%s"]
    args: list = [tenant, user_id, since, until]
    if not all_b:
        where.append("bucket=%s"); args.append(one_b)
    wc = " AND ".join(where)

    with db() as c:
        rules = [dict(r) for r in c.execute(
            "SELECT * FROM productivity_rules WHERE tenant_id=%s ORDER BY created_at", (tenant,),
        ).fetchall()]
        rows = c.execute(
            f"""SELECT data_json->>'app' AS app, data_json->>'title' AS title,
                       bucket, ts, duration_sec
                FROM events WHERE {wc}
                ORDER BY ts LIMIT 300000""",
            tuple(args),
        ).fetchall()

    runs: list = []
    for r in rows:
        b     = r["bucket"]
        app   = r["app"] or "—"
        title = _clean_title(r["title"] or "")
        dur   = r["duration_sec"] or 0.0
        ts    = r["ts"]
        key   = ("afk",) if b == "afk" else (app.lower(), title.lower())
        if runs and runs[-1]["_k"] == key:
            runs[-1]["total_sec"] += dur
            runs[-1]["end_ts"]     = ts
            runs[-1]["count"]     += 1
        else:
            cat  = "afk" if b == "afk" else _apply_rules(title, app, rules)
            tipo = "AFK" if b == "afk" else ("Internet" if _is_browser(app) else "App")
            runs.append({"_k": key, "app": app, "title": title, "type": tipo,
                         "category": cat, "start_ts": ts, "end_ts": ts,
                         "total_sec": dur, "count": 1})

    totals = {"productive": 0.0, "unproductive": 0.0, "neutral": 0.0, "uncategorized": 0.0, "afk": 0.0, "total": 0.0}
    dagg: dict[str, dict] = {}
    for run in runs:
        cat = run["category"]; sec = run["total_sec"]
        totals[cat]    = totals.get(cat, 0.0) + sec
        totals["total"] += sec
        day = _utc_hour_to_local_day(run["start_ts"][:13])
        d   = dagg.setdefault(day, {"productive": 0.0, "unproductive": 0.0, "neutral": 0.0, "uncategorized": 0.0, "afk": 0.0})
        d[cat] = d.get(cat, 0.0) + sec
        run["total_sec"]   = round(sec, 1)
        run["last_access"] = run["end_ts"]
        run.pop("_k", None)

    daily = [{"day": k, **{kk: round(vv, 1) for kk, vv in v.items()}} for k, v in sorted(dagg.items())]

    result = {
        "tenant": tenant, "since": since, "until": until, "timeline": True,
        "totals": {k: round(v, 1) for k, v in totals.items()},
        "items":  runs[:8000],
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
    today_h          = today_start[:13]      # 'YYYY-MM-DDTHH' p/ comparar com substr(ts,1,13)
    online_threshold = (now - dt.timedelta(minutes=ONLINE_THRESHOLD_MINUTES)).isoformat()

    _perf = _Perf(f"overview tenant={tenant} since={since}")
    with db() as c:
        _perf.mark("connect")
        # Buckets por (hora-UTC, usuário) com ativo (window) e AFK. Lê do agregado
        # (agg_user_hour) quando pronto; senão varre events (fallback). Tudo o que
        # o overview precisa de eventos sai disto, agregado em Python.
        rows = _hour_rows(c, tenant, since, until, SC, A)
        _perf.mark("scan")

        # Tabela de usuários (pequena) — depto, nomes e status online.
        urows = c.execute(
            f"""SELECT user_id, display_name,
                      COALESCE(department,'Sem departamento') AS dept, last_seen
               FROM users u WHERE u.tenant_id=%s{SCU}""",
            tuple([tenant, *A]),
        ).fetchall()
        _perf.mark("users")
    _perf.done()

    # ---- Agregações em memória (poucas centenas/milhares de linhas) ----
    active_total = afk_total = today_active = 0.0
    active_by_user: dict[str, float] = {}
    users_seen: set[str] = set()
    _hourly_local: dict[int, float] = {}     # heatmap por hora local (BRT)
    _daily_map: dict[str, dict] = {}         # 1 barra por dia local (BRT)
    for r in rows:
        h    = r["h"]
        uid  = r["user_id"]
        act  = float(r["active"] or 0.0)
        afkv = float(r["afk"] or 0.0)
        active_total += act
        afk_total    += afkv
        if h >= today_h:
            today_active += act
        active_by_user[uid] = active_by_user.get(uid, 0.0) + act
        users_seen.add(uid)
        try:                                  # heatmap: hora UTC -> hora local
            _lh = (int(h[11:13]) + DASH_TZ_OFFSET_HOURS) % 24
            _hourly_local[_lh] = _hourly_local.get(_lh, 0.0) + act
        except (ValueError, IndexError):
            pass
        _d = _utc_hour_to_local_day(h)        # barra diária: hora UTC -> dia local
        _e = _daily_map.setdefault(_d, {"day": _d, "active": 0.0, "afk": 0.0, "_users": set()})
        _e["active"] += act
        _e["afk"]    += afkv
        _e["_users"].add(uid)

    daily = [{"day": v["day"], "active": v["active"], "afk": v["afk"], "users": len(v["_users"])}
             for v in sorted(_daily_map.values(), key=lambda x: x["day"])]

    # online + por departamento (lista completa de usuários, inclui os zerados)
    online = 0
    _dept_map: dict[str, dict] = {}
    overwork, underwork = [], []
    for u in urows:
        uid = u["user_id"]
        if u["last_seen"] and u["last_seen"] >= online_threshold:
            online += 1
        ua = active_by_user.get(uid, 0.0)
        de = _dept_map.setdefault(u["dept"], {"dept": u["dept"], "users": 0, "active": 0.0})
        de["users"]  += 1
        de["active"] += ua
        avg_h = (ua / 3600.0) / max(days, 1)
        if avg_h > 10:
            overwork.append({"user_id": uid, "display_name": u["display_name"],
                             "max_day_hours": round(avg_h, 1)})
        if 0 < avg_h < 2:
            underwork.append({"user_id": uid, "display_name": u["display_name"],
                              "avg_day_hours": round(avg_h, 1)})
    by_dept = sorted(_dept_map.values(), key=lambda x: x["active"], reverse=True)
    totals  = {"total_users": len(users_seen), "active": active_total, "afk": afk_total}

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
    detail:  int = Query(1),   # 0 = só a lista de sites (sem o gráfico diário) — p/ Categorização
    uncat:   int = Query(0),   # 1 = retorna só sites NÃO categorizados (lista de trabalho)
):
    if since is None:
        since = _since_for_days(days)
    if until is None:
        until = dt.datetime.now(dt.timezone.utc).isoformat()

    _allowed = _lider_allowed_ids(request, tenant)
    ck = (f"{tenant}:internet:{since}:{until}:{user_id or '-'}:"
          f"{detail}:u{uncat}:{'sc' if _allowed is not None else 'all'}")
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    # Pré-filtra a varredura para SÓ apps de navegador (fração das linhas).
    bsql, bargs = _only_browser_where()

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
        if _allowed is not None:
            sql += " AND user_id = ANY(%s)"; args.append(_allowed)
        sql += bsql; args += bargs
        sql += " GROUP BY user_id, app, title ORDER BY total_sec DESC"
        raw_rows = c.execute(sql, args).fetchall()

        daily_raw = []
        if detail:
            dsql  = ("SELECT substr(ts,1,13) AS h, data_json->>'app' AS app,"
                     " data_json->>'title' AS title, SUM(duration_sec) AS total_sec"
                     " FROM events WHERE tenant_id=%s AND bucket='window' AND ts>=%s AND ts<=%s")
            dargs: list = [tenant, since, until]
            if user_id:
                dsql += " AND user_id=%s"; dargs.append(user_id)
            if _allowed is not None:
                dsql += " AND user_id = ANY(%s)"; dargs.append(_allowed)
            dsql += bsql; dargs += bargs
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
        if uncat and cat != "uncategorized":   # lista de trabalho: só o que falta
            continue
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

    result = {
        "tenant": tenant, "since": since, "days": days,
        "totals": {k: round(v, 1) for k, v in totals.items()},
        "sites":  sites[:300],
        "daily":  daily,
    }
    _cache_set(ck, result)
    return result


@app.get("/v1/productivity/apps", dependencies=[Depends(require_tenant_scope)])
def productivity_apps(tenant: str, request: Request, days: int = Query(30, le=90),
                      uncat: int = Query(0)):
    now     = dt.datetime.now(dt.timezone.utc)
    since   = (now - dt.timedelta(days=days)).isoformat()
    until   = now.isoformat()
    allowed = _lider_allowed_ids(request, tenant)
    scoped  = allowed is not None
    lim     = 500 if uncat else 150   # uncat puxa a cauda longa (depois filtra)

    ck = f"{tenant}:cfg_apps:{days}:{'sc' if scoped else 'all'}:u{uncat}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    SC = " AND user_id = ANY(%s)" if scoped else ""
    A  = [allowed] if scoped else []
    with db() as c:
        rules = [dict(r) for r in c.execute(
            "SELECT * FROM productivity_rules WHERE tenant_id=%s ORDER BY created_at",
            (tenant,),
        ).fetchall()]
        # Caminho rápido: lê do rollup agg_app_day (não cresce com eventos crus).
        # Só quando o agregado cobre o período E não há escopo de líder (o
        # agregado não guarda user_id). Senão varre eventos como antes.
        if (not scoped) and _agg_ready(c, tenant, since):
            d0 = _utc_hour_to_local_day(since[:13])
            d1 = _utc_hour_to_local_day(until[:13])
            rows = c.execute(
                """SELECT MIN(app) AS app, SUM(active_sec) AS total_sec, NULL::int AS user_count
                   FROM agg_app_day
                   WHERE tenant_id=%s AND day>=%s AND day<=%s
                   GROUP BY LOWER(app) ORDER BY total_sec DESC LIMIT %s""",
                (tenant, d0, d1, lim),
            ).fetchall()
        else:
            rows = c.execute(
                f"""SELECT data_json->>'app' AS app,
                          SUM(duration_sec) AS total_sec,
                          COUNT(DISTINCT user_id) AS user_count
                   FROM events WHERE tenant_id=%s{SC} AND bucket='window' AND ts>=%s
                   GROUP BY app ORDER BY total_sec DESC LIMIT %s""",
                tuple([tenant, *A, since, lim]),
            ).fetchall()

    apps = []
    for row in rows:
        app      = row["app"] or "(desconhecido)"
        category = _apply_rules("", app, rules)
        if uncat and category != "uncategorized":
            continue
        apps.append({
            "app":        app,
            "is_browser": _is_browser(app),
            "category":   category,
            "total_sec":  round(row["total_sec"] or 0.0, 1),
            "user_count": row["user_count"] or 0,
        })
    result = {"tenant": tenant, "since": since, "apps": apps}
    _cache_set(ck, result)
    return result


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
        # Tabelas pequenas: contagem exata, barata.
        tenants = c.execute("SELECT COUNT(*) AS n FROM tenants").fetchone()["n"]
        users   = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        devices = c.execute("SELECT COUNT(*) AS n FROM devices").fetchone()["n"]
        lic_act = c.execute("SELECT COUNT(*) AS n FROM licenses WHERE revoked=0").fetchone()["n"]
        lic_rev = c.execute("SELECT COUNT(*) AS n FROM licenses WHERE revoked=1").fetchone()["n"]

        # `events` é enorme: COUNT(*) cheio varre a tabela e travava o login do
        # Platform Admin sob carga. Total = estimativa instantânea do planner; as
        # contagens por janela ficam protegidas por statement_timeout (best-effort,
        # nunca derrubam o /admin/stats).
        try:
            events = int(c.execute(
                "SELECT reltuples::bigint AS n FROM pg_class WHERE relname='events'"
            ).fetchone()["n"] or 0)
        except Exception:
            events = 0

        ev_24h, ev_7d, per_hour = 0, 0, []
        try:
            c.execute("SET statement_timeout = '6s'")
            ev_24h = c.execute("SELECT COUNT(*) AS n FROM events WHERE ts>=%s", (h24,)).fetchone()["n"]
            ev_7d  = c.execute("SELECT COUNT(*) AS n FROM events WHERE ts>=%s", (d7,)).fetchone()["n"]
            per_hour = c.execute(
                """SELECT substr(ts,1,13) AS hour, COUNT(*) AS events
                   FROM events WHERE ts>=%s GROUP BY hour ORDER BY hour""",
                (h24,),
            ).fetchall()
        except Exception as exc:
            print(f"[admin_stats] contagens de events puladas (lento/timeout): {exc}", flush=True)
        finally:
            try:
                c.execute("SET statement_timeout = 0")
            except Exception:
                pass
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
def _report_period_range(freq: str):
    """Devolve (since_iso_utc, until_iso_utc, label) para o período do relatório,
    no fuso REPORT_TZ. Sempre o **período ANTERIOR fechado** (porque o envio é no
    início do período seguinte): diário = ontem; semanal = a semana passada (seg–dom);
    mensal = o mês passado. Assim o relatório enviado na segunda cobre a semana toda,
    e não só a segunda. O preview mostra exatamente o que seria enviado."""
    tz = ZoneInfo(REPORT_TZ) if ZoneInfo else dt.timezone.utc
    today = dt.datetime.now(tz).date()
    if freq == "weekly":
        this_monday = today - dt.timedelta(days=today.weekday())   # segunda desta semana
        start_d = this_monday - dt.timedelta(days=7)               # segunda da semana passada
        end_d   = this_monday - dt.timedelta(days=1)               # domingo da semana passada
        label = f"{start_d.strftime('%d/%m/%Y')} – {end_d.strftime('%d/%m/%Y')}"
    elif freq == "monthly":
        end_d   = today.replace(day=1) - dt.timedelta(days=1)      # último dia do mês passado
        start_d = end_d.replace(day=1)                            # 1º do mês passado
        label = start_d.strftime("%m/%Y")
    else:  # daily → ontem
        start_d = today - dt.timedelta(days=1)
        end_d   = start_d
        label = start_d.strftime("%d/%m/%Y")
    start = dt.datetime(start_d.year, start_d.month, start_d.day, 0, 0, 0, tzinfo=tz)
    end   = dt.datetime(end_d.year, end_d.month, end_d.day, 23, 59, 59, tzinfo=tz)
    since = start.astimezone(dt.timezone.utc).isoformat()
    until = end.astimezone(dt.timezone.utc).isoformat()
    return since, until, label


def _build_report_pdf(
    tenant_id: str,
    cfg: dict,
    period_label: str,
    since: str,
    until: str,
) -> bytes:
    """Gera o PDF do relatório e devolve bytes. since/until = janela do período (UTC ISO)."""
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
                               AND e.ts::timestamptz >= %s::timestamptz
                               AND e.ts::timestamptz <= %s::timestamptz
                       WHERE u.tenant_id=%s AND u.user_id=ANY(%s)
                       GROUP BY u.display_name, u.email, u.department
                       ORDER BY active DESC""",
                    (since, until, tenant_id, uid_filter),
                ).fetchall()
            else:
                rows = c.execute(
                    """SELECT u.display_name, u.email, u.department,
                              COALESCE(SUM(CASE WHEN e.bucket='window' THEN e.duration_sec END),0) AS active,
                              COALESCE(SUM(CASE WHEN e.bucket='afk' AND e.data_json->>'status'='afk'
                                               THEN e.duration_sec END),0) AS afk
                       FROM users u LEFT JOIN events e
                            ON e.tenant_id=u.tenant_id AND e.user_id=u.user_id
                               AND e.ts::timestamptz >= %s::timestamptz
                               AND e.ts::timestamptz <= %s::timestamptz
                       WHERE u.tenant_id=%s
                       GROUP BY u.display_name, u.email, u.department
                       ORDER BY active DESC""",
                    (since, until, tenant_id),
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
            # Calcula a partir dos eventos (mesma lógica da tela Jornada):
            # janela do dia = 1ª coleta → última coleta; AFK = AFK dentro da janela.
            _uids = user_filter or []
            _uf   = " AND user_id = ANY(%(uids)s)" if _uids else ""
            _jq   = f"""
                WITH w AS (
                    SELECT user_id,
                           (ts::timestamptz AT TIME ZONE %(tz)s)                 AS lts,
                           (ts::timestamptz AT TIME ZONE %(tz)s)
                               + (duration_sec || ' seconds')::interval          AS lend,
                           duration_sec
                    FROM events
                    WHERE tenant_id = %(tenant)s AND bucket = 'window'
                      AND ts::timestamptz >= %(since)s::timestamptz
                      AND ts::timestamptz <= %(until)s::timestamptz{_uf}
                ),
                win AS (
                    SELECT user_id, lts::date AS day, MIN(lts) AS start_ts,
                           MAX(lend) AS end_ts, SUM(duration_sec) AS focus_sec
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
                      AND ts::timestamptz <= %(until)s::timestamptz{_uf}
                ),
                afk AS (
                    SELECT a.user_id, a.day,
                           COALESCE(SUM(a.duration_sec) FILTER (
                               WHERE a.lts >= win.start_ts AND a.lts <= win.end_ts), 0) AS afk_in,
                           COALESCE(SUM(a.duration_sec) FILTER (
                               WHERE a.lts > win.end_ts), 0)                            AS afk_after
                    FROM afk_ev a JOIN win ON win.user_id = a.user_id AND win.day = a.day
                    GROUP BY a.user_id, a.day
                )
                SELECT u.display_name, u.email,
                       to_char(win.day, 'YYYY-MM-DD')                  AS day,
                       to_char(win.start_ts, 'HH24:MI')                AS start_hm,
                       to_char(win.end_ts,   'HH24:MI')                AS end_hm,
                       EXTRACT(EPOCH FROM (win.end_ts - win.start_ts)) AS gross_sec,
                       COALESCE(afk.afk_in, 0)                         AS afk_in,
                       COALESCE(afk.afk_after, 0)                      AS afk_after
                FROM win
                JOIN users u ON u.tenant_id = %(tenant)s AND u.user_id = win.user_id
                LEFT JOIN afk ON afk.user_id = win.user_id AND afk.day = win.day
                ORDER BY u.display_name, win.day
            """
            _jp = {"tz": REPORT_TZ, "tenant": tenant_id, "since": since, "until": until}
            if _uids:
                _jp["uids"] = _uids
            rows = c.execute(_jq, _jp).fetchall()
            if rows:
                from itertools import groupby as _groupby

                def _hhmm(sec):
                    s = int(sec or 0)
                    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}"

                person_style = ParagraphStyle(
                    "wp_person", parent=normal_style, fontSize=11,
                    fontName="Helvetica-Bold", textColor=colors.HexColor("#1e3a8a"),
                    spaceBefore=12, spaceAfter=3,
                )
                _keyf = lambda r: (r["display_name"] or r["email"] or "—")
                for person, grp in _groupby(rows, key=_keyf):
                    story.append(Paragraph(person, person_style))
                    data = [["Dia", "Início", "Final", "AFK na Jornada",
                             "AFK Pós Jornada", "Bruta", "Líquida"]]
                    for r in grp:
                        gross  = float(r["gross_sec"] or 0)
                        afk_in = float(r["afk_in"] or 0)
                        net    = max(0.0, gross - afk_in)
                        data.append([
                            r["day"],
                            r["start_hm"] or "—",
                            r["end_hm"] or "—",
                            _hhmm(afk_in),
                            _hhmm(r["afk_after"]) if (r["afk_after"] or 0) else "—",
                            _hhmm(gross),
                            _hhmm(net),
                        ])
                    tbl = Table(data, colWidths=[2.4*cm, 1.9*cm, 1.9*cm, 3.0*cm, 3.0*cm, 1.9*cm, 1.9*cm])
                    tbl.setStyle(TableStyle([
                        ("BACKGROUND",   (0, 0), (-1, 0), colors.HexColor("#3b82f6")),
                        ("TEXTCOLOR",    (0, 0), (-1, 0), colors.white),
                        ("FONTNAME",     (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("FONTSIZE",     (0, 0), (-1, -1), 8),
                        ("GRID",         (0, 0), (-1, -1), 0.25, colors.lightgrey),
                        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0f4ff")]),
                        ("ALIGN",        (1, 0), (-1, -1), "CENTER"),
                    ]))
                    story.append(tbl)
                    story.append(Spacer(1, 4))
            else:
                story.append(Paragraph("Nenhum dado no período.", dim_style))
            story.append(Spacer(1, 10))

        # ── TENDÊNCIAS ────────────────────────────────────────────────────
        if "tendencias" in sections:
            story.append(Paragraph("Tendências (dia a dia no período)", h2_style))
            rows = c.execute(
                """SELECT substr(ts,1,10) AS day,
                          SUM(CASE WHEN bucket='window' THEN duration_sec ELSE 0 END) AS active,
                          SUM(CASE WHEN bucket='afk' AND data_json->>'status'='afk'
                                   THEN duration_sec ELSE 0 END) AS afk,
                          COUNT(DISTINCT user_id) AS users
                   FROM events
                   WHERE tenant_id=%s
                     AND ts::timestamptz >= %s::timestamptz
                     AND ts::timestamptz <= %s::timestamptz
                   GROUP BY day ORDER BY day""",
                (tenant_id, since, until),
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
# (AWS SES removido) — todo envio de e-mail usa o SMTP do tenant (_smtp_send).
# Relatórios: _run_report → _smtp_send. Links de senha: _send_password_token.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Função central de execução de um relatório
# ---------------------------------------------------------------------------
def _run_report(cfg: dict, tenant_id: str, period_label: str | None = None) -> None:
    """Gera o PDF e envia o e-mail para um config de relatório."""
    config_id  = cfg["id"]
    recipients = json.loads(cfg["recipients"]) if isinstance(cfg["recipients"], str) else cfg["recipients"]
    rname      = cfg["name"]
    # Janela de dados conforme a frequência (diário/semanal/mensal).
    since, until, period_label = _report_period_range(cfg.get("frequency") or "daily")

    status, err_msg = "ok", None
    try:
        pdf_bytes = _build_report_pdf(tenant_id, cfg, period_label, since, until)
        filename  = f"workpulse_{rname.lower().replace(' ', '_')}_{dt.date.today().isoformat()}.pdf"
        subject   = f"WorkPulse — {rname} ({period_label})"
        body_html = f"""
        <p>Olá,</p>
        <p>Segue em anexo o relatório automático <b>{rname}</b> referente ao período <b>{period_label}</b>.</p>
        <p>Acesse o dashboard para mais detalhes: <a href="https://workpulse.com.br/dashboard/workforce.html">WorkPulse Dashboard</a></p>
        <br><p style="color:#888;font-size:12px">WorkPulse — Monitoramento de Força de Trabalho</p>
        """
        ecfg = _email_cfg_for_send(tenant_id)
        if not ecfg:
            raise RuntimeError("conta de e-mail do tenant não configurada/habilitada — relatório não enviado")
        _smtp_send(ecfg, recipients, subject, body_html, attachments=[(filename, pdf_bytes)])
        print(f"[reports] sent '{rname}' → {recipients} (smtp {ecfg['host']})", flush=True)
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


@app.get("/v1/reports/{cfg_id}/preview", dependencies=[Depends(require_tenant_scope)])
def preview_report(cfg_id: int, tenant: str, download: int = 0):
    """Gera o MESMO PDF que iria por e-mail e devolve direto (sem enviar)."""
    from fastapi import Response as _FResponse
    with db() as c:
        row = c.execute(
            "SELECT * FROM report_configs WHERE id=%s AND tenant_id=%s",
            (cfg_id, tenant),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Report config not found")
    cfg = dict(row)
    since, until, period_label = _report_period_range(cfg.get("frequency") or "daily")
    pdf_bytes = _build_report_pdf(tenant, cfg, period_label, since, until)
    safe = (cfg.get("name") or "relatorio").lower().replace(" ", "_")
    filename = f"workpulse_{safe}_{dt.date.today().isoformat()}.pdf"
    disp = "attachment" if download else "inline"
    return _FResponse(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disp}; filename="{filename}"'},
    )


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

CLIENT_ROLES   = ("admin_master", "admin", "supervisor", "lider", "suporte")
ALL_ROLES      = ("admin_master", "admin", "supervisor", "lider", "suporte", "wp_admin")

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

def _send_password_token(user_row: dict, purpose: str) -> str:
    """Gera token de uso único, tenta enviar o link pela conta de e-mail (SMTP) do
    PRÓPRIO tenant e devolve o link. Se o tenant não tiver SMTP configurado, o link
    é retornado mesmo assim — o Admin Master copia/envia manualmente pela UI
    (o link já é exibido na tela de Usuários)."""
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
    ecfg = _email_cfg_for_send(user_row.get("tenant_id"))
    if ecfg:
        try:
            _smtp_send(ecfg, [user_row["email"]], "WorkPulse — acesso ao painel", html)
        except Exception as exc:
            print(f"[auth] AVISO: falha ao enviar e-mail de senha (SMTP do tenant): {exc}", flush=True)
    else:
        print("[auth] tenant sem SMTP configurado — link de senha não enviado por e-mail "
              "(Admin Master usa o link manual na UI).", flush=True)
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


# ===========================================================================
# Suporte Técnico (perfil "suporte" + Admin Master)
# (definido aqui, após require_role, pois o decorator o resolve no import)
# ===========================================================================

@app.get("/v1/support/people",
         dependencies=[Depends(require_role("admin_master", "admin", "suporte"))])
def support_people(tenant: str, request: Request):
    """Lista enxuta para a tela de Suporte Técnico: colaborador, última atividade,
    versão do agente e a máquina mais recente. Sem agregação pesada."""
    with db() as c:
        rows = c.execute(
            """SELECT u.user_id, u.display_name, u.department, u.last_seen,
                      d.agent_version, d.hostname
               FROM users u
               LEFT JOIN LATERAL (
                   SELECT agent_version, hostname FROM devices dd
                   WHERE dd.tenant_id = u.tenant_id AND dd.user_id = u.user_id
                   ORDER BY dd.last_seen DESC LIMIT 1
               ) d ON TRUE
               WHERE u.tenant_id = %s
               ORDER BY u.last_seen DESC NULLS LAST""",
            (tenant,),
        ).fetchall()
    return {"tenant": tenant, "users": [dict(r) for r in rows]}


@app.get("/v1/support/person",
         dependencies=[Depends(require_role("admin_master", "admin", "suporte"))])
def support_person(tenant: str, user_id: str, request: Request):
    """Detalhe de um colaborador para o Suporte: máquina, versão, status e a
    última coleta de cada tipo (AFK / app / web) + início da jornada de hoje."""
    nbsql, nbargs = _no_browser_where()    # app = window NÃO navegador
    bsql,  bargs  = _only_browser_where()  # web = window navegador
    floor   = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat()
    floor30 = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat()

    with db() as c:
        u = c.execute(
            "SELECT display_name, department, last_seen FROM users WHERE tenant_id=%s AND user_id=%s",
            (tenant, user_id),
        ).fetchone()
        if not u:
            raise HTTPException(404, "colaborador não encontrado")
        dev = c.execute(
            """SELECT hostname, agent_version, os, os_version, last_seen
               FROM devices WHERE tenant_id=%s AND user_id=%s
               ORDER BY last_seen DESC LIMIT 1""",
            (tenant, user_id),
        ).fetchone()

        # Última coleta por tipo: varredura do índice (tenant,user,ts) de trás p/
        # frente, parando no 1º match. Piso de 30 dias limita o pior caso (sem
        # match → para no piso em vez de varrer o histórico inteiro). Antes era
        # MAX(ts) sem piso → varria tudo e levava ~1min.
        r = c.execute(
            """SELECT ts FROM events
               WHERE tenant_id=%s AND user_id=%s AND ts>=%s AND bucket='afk'
               ORDER BY ts DESC LIMIT 1""",
            (tenant, user_id, floor30),
        ).fetchone()
        last_afk = r["ts"] if r else None
        r = c.execute(
            f"""SELECT ts FROM events
                WHERE tenant_id=%s AND user_id=%s AND ts>=%s AND bucket='window'{nbsql}
                ORDER BY ts DESC LIMIT 1""",
            tuple([tenant, user_id, floor30, *nbargs]),
        ).fetchone()
        last_app = r["ts"] if r else None
        r = c.execute(
            f"""SELECT ts FROM events
                WHERE tenant_id=%s AND user_id=%s AND ts>=%s AND bucket='window'{bsql}
                ORDER BY ts DESC LIMIT 1""",
            tuple([tenant, user_id, floor30, *bargs]),
        ).fetchone()
        last_web = r["ts"] if r else None
        # Início da jornada = primeira coleta de janela do DIA LOCAL de hoje.
        jstart = c.execute(
            """SELECT MIN(ts) AS m FROM events
               WHERE tenant_id=%s AND user_id=%s AND bucket='window' AND ts>=%s
                 AND (ts::timestamptz AT TIME ZONE %s)::date
                   = (now() AT TIME ZONE %s)::date""",
            (tenant, user_id, floor, REPORT_TZ, REPORT_TZ),
        ).fetchone()["m"]

    return {
        "tenant": tenant, "user_id": user_id,
        "display_name":  u["display_name"],
        "department":    u["department"],
        "last_seen":     u["last_seen"],
        "hostname":      dev["hostname"]      if dev else None,
        "agent_version": dev["agent_version"] if dev else None,
        "os":            dev["os"]            if dev else None,
        "os_version":    dev["os_version"]    if dev else None,
        "last_afk":      last_afk,
        "last_app":      last_app,
        "last_web":      last_web,
        "jornada_start": jstart,
    }


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
    # Solicitação fica registrada em dash_license_requests + auditoria; a plataforma
    # acompanha pelo Platform Admin (sem e-mail — SES removido).
    print(f"[license] nova solicitação: tenant={tid} por={ctx['email']} "
          f"plano={body.plan or '-'} seats={body.seats or '-'}", flush=True)
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


# ===========================================================================
# Conta de e-mail por tenant (SMTP) — envio de relatórios e alertas pela
# conta do próprio cliente. Senha guardada criptografada (Fernet).
# ===========================================================================
_SCHEMA_STMTS.extend([
    """
    CREATE TABLE IF NOT EXISTS app_secrets (
        name  TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tenant_email_config (
        tenant_id      TEXT PRIMARY KEY,
        provider       TEXT,                       -- gmail | outlook | custom
        host           TEXT,
        port           INTEGER,
        security       TEXT,                       -- tls | ssl | none
        username       TEXT,
        password_enc   TEXT,                       -- Fernet
        from_email     TEXT,
        from_name      TEXT,
        enabled        BOOLEAN NOT NULL DEFAULT FALSE,
        last_test_at   TEXT,
        last_test_ok   BOOLEAN,
        last_test_error TEXT,
        updated_at     TEXT,
        updated_by     TEXT
    )
    """,
])


def _get_fernet():
    """Chave Fernet: env WORKPULSE_FERNET_KEY se houver; senão persiste uma em
    app_secrets (gerada na primeira vez)."""
    from cryptography.fernet import Fernet
    key = os.getenv("WORKPULSE_FERNET_KEY")
    if not key:
        with db() as c:
            row = c.execute("SELECT value FROM app_secrets WHERE name='fernet_key'").fetchone()
            if not row:
                newk = Fernet.generate_key().decode()
                c.execute(
                    "INSERT INTO app_secrets (name, value) VALUES ('fernet_key', %s) "
                    "ON CONFLICT (name) DO NOTHING",
                    (newk,),
                )
                row = c.execute("SELECT value FROM app_secrets WHERE name='fernet_key'").fetchone()
            key = row["value"]
    return Fernet(key.encode() if isinstance(key, str) else key)


def _enc(plain: str) -> str:
    return _get_fernet().encrypt(plain.encode()).decode()


def _dec(token: str) -> str:
    return _get_fernet().decrypt(token.encode()).decode()


def _email_cfg_row(tenant: str):
    with db() as c:
        return c.execute(
            "SELECT * FROM tenant_email_config WHERE tenant_id=%s", (tenant,)
        ).fetchone()


def _email_cfg_for_send(tenant: str):
    """Devolve dict pronto para envio (com senha decifrada) se o tenant tiver
    conta configurada E habilitada; caso contrário None."""
    row = _email_cfg_row(tenant)
    if not row or not row["enabled"]:
        return None
    if not (row["host"] and row["username"] and row["password_enc"]):
        return None
    try:
        pwd = _dec(row["password_enc"])
    except Exception:
        return None
    return {
        "host": row["host"], "port": int(row["port"] or 587),
        "security": (row["security"] or "tls"),
        "username": row["username"], "password": pwd,
        "from_email": row["from_email"] or row["username"],
        "from_name": row["from_name"] or "",
    }


def _smtp_send(cfg: dict, recipients: list[str], subject: str, html: str,
               attachments: list | None = None) -> None:
    """Envia um e-mail via SMTP do cliente. attachments: lista de (filename, bytes)."""
    import smtplib, ssl as _ssl
    if attachments:
        msg = email.mime.multipart.MIMEMultipart("mixed")
        msg.attach(email.mime.text.MIMEText(html, "html", "utf-8"))
        for fn, data in attachments:
            part = email.mime.application.MIMEApplication(data, Name=fn)
            part["Content-Disposition"] = f'attachment; filename="{fn}"'
            msg.attach(part)
    else:
        msg = email.mime.multipart.MIMEMultipart("alternative")
        msg.attach(email.mime.text.MIMEText(html, "html", "utf-8"))

    from_email = cfg["from_email"]
    from_name  = cfg.get("from_name") or ""
    msg["From"]    = f"{from_name} <{from_email}>" if from_name else from_email
    msg["To"]      = ", ".join(recipients)
    msg["Subject"] = subject

    host = cfg["host"]; port = int(cfg["port"]); sec = (cfg.get("security") or "tls").lower()
    if sec == "ssl":
        server = smtplib.SMTP_SSL(host, port, context=_ssl.create_default_context(), timeout=25)
    else:
        server = smtplib.SMTP(host, port, timeout=25)
        if sec == "tls":
            server.starttls(context=_ssl.create_default_context())
    try:
        server.login(cfg["username"], cfg["password"])
        server.sendmail(from_email, recipients, msg.as_bytes())
    finally:
        try:
            server.quit()
        except Exception:
            pass


# --- Modelos ---
class EmailConfigIn(BaseModel):
    provider:   str | None = None
    host:       str
    port:       int
    security:   str = "tls"
    username:   str
    password:   str | None = None      # vazio => mantém a senha atual
    from_email: str
    from_name:  str | None = None
    enabled:    bool = True


class EmailTestIn(BaseModel):
    to: str | None = None


def _email_cfg_public(row) -> dict:
    if not row:
        return {"configured": False, "enabled": False, "security": "tls", "port": 587, "provider": "gmail"}
    return {
        "configured":  True,
        "provider":    row["provider"] or "custom",
        "host":        row["host"] or "",
        "port":        row["port"] or 587,
        "security":    row["security"] or "tls",
        "username":    row["username"] or "",
        "from_email":  row["from_email"] or "",
        "from_name":   row["from_name"] or "",
        "enabled":     bool(row["enabled"]),
        "has_password": bool(row["password_enc"]),
        "last_test_at":    row["last_test_at"],
        "last_test_ok":    row["last_test_ok"],
        "last_test_error": row["last_test_error"],
    }


@app.get("/v1/tenant-email-config")
def tenant_email_config_get(tenant: str, ctx: Annotated[dict, Depends(require_role("admin_master", "admin"))]):
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    return _email_cfg_public(_email_cfg_row(tid))


@app.put("/v1/tenant-email-config")
def tenant_email_config_set(body: EmailConfigIn, tenant: str, request: Request,
                            ctx: Annotated[dict, Depends(require_role("admin_master", "admin"))]):
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    sec = (body.security or "tls").lower()
    if sec not in ("tls", "ssl", "none"):
        sec = "tls"
    existing = _email_cfg_row(tid)
    # senha: se vier vazia, mantém a anterior (não força redigitar a cada save)
    if body.password:
        pwd_enc = _enc(body.password)
    elif existing and existing["password_enc"]:
        pwd_enc = existing["password_enc"]
    else:
        pwd_enc = None
    with db() as c:
        c.execute(
            """INSERT INTO tenant_email_config
                 (tenant_id, provider, host, port, security, username, password_enc,
                  from_email, from_name, enabled, updated_at, updated_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (tenant_id) DO UPDATE SET
                 provider=EXCLUDED.provider, host=EXCLUDED.host, port=EXCLUDED.port,
                 security=EXCLUDED.security, username=EXCLUDED.username,
                 password_enc=EXCLUDED.password_enc, from_email=EXCLUDED.from_email,
                 from_name=EXCLUDED.from_name, enabled=EXCLUDED.enabled,
                 updated_at=EXCLUDED.updated_at, updated_by=EXCLUDED.updated_by""",
            (tid, (body.provider or "custom"), body.host, int(body.port), sec,
             body.username, pwd_enc, (body.from_email or body.username),
             (body.from_name or None), bool(body.enabled),
             _now_utc().isoformat(), ctx.get("email")),
        )
    audit(ctx.get("email"), "email_config.set", target=tid,
          detail=f"host={body.host};enabled={body.enabled}", ip=_client_ip(request))
    return {"ok": True, "has_password": bool(pwd_enc)}


@app.post("/v1/tenant-email-config/test")
def tenant_email_config_test(body: EmailTestIn, tenant: str,
                             ctx: Annotated[dict, Depends(require_role("admin_master", "admin"))]):
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    row = _email_cfg_row(tid)
    if not row or not (row["host"] and row["username"] and row["password_enc"]):
        raise HTTPException(400, "configure e salve a conta de e-mail antes de testar")
    cfg = {
        "host": row["host"], "port": int(row["port"] or 587),
        "security": row["security"] or "tls",
        "username": row["username"], "password": _dec(row["password_enc"]),
        "from_email": row["from_email"] or row["username"],
        "from_name": row["from_name"] or "",
    }
    to = (body.to or ctx.get("email") or cfg["from_email"]).strip()
    html = (
        "<p>Este é um e-mail de <b>teste</b> do WorkPulse.</p>"
        "<p>Se você recebeu esta mensagem, a conta de e-mail do seu painel está "
        "configurada corretamente para enviar relatórios e alertas.</p>"
        "<p style='color:#888;font-size:12px'>WorkPulse — Monitoramento de Força de Trabalho</p>"
    )
    ok, err = True, None
    try:
        _smtp_send(cfg, [to], "WorkPulse — teste de envio de e-mail", html)
    except Exception as exc:
        ok, err = False, str(exc)
    with db() as c:
        c.execute(
            "UPDATE tenant_email_config SET last_test_at=%s, last_test_ok=%s, last_test_error=%s WHERE tenant_id=%s",
            (_now_utc().isoformat(), ok, (err or None), tid),
        )
    if not ok:
        raise HTTPException(502, f"falha no envio: {err}")
    return {"ok": True, "sent_to": to}


# ===========================================================================
# Alertas operacionais — AFK no dia, ausência, sites perigosos + engajamento.
# Config por tenant (horário comercial, corte, regras por tipo), feriados
# nacionais oficiais, e-mail ao ponto focal (por tipo) com dedupe diário.
# ===========================================================================
_SCHEMA_STMTS.extend([
    """
    CREATE TABLE IF NOT EXISTS alert_settings (
        tenant_id         TEXT PRIMARY KEY,
        business_start    TEXT NOT NULL DEFAULT '08:00',
        business_end      TEXT NOT NULL DEFAULT '18:00',
        absent_cutoff     TEXT NOT NULL DEFAULT '10:00',
        afk_threshold_min INTEGER NOT NULL DEFAULT 120,
        underwork_max_h   REAL NOT NULL DEFAULT 2,
        overwork_min_h    REAL NOT NULL DEFAULT 10,
        engagement_days          INTEGER NOT NULL DEFAULT 7,
        engagement_include_today BOOLEAN NOT NULL DEFAULT FALSE,
        updated_at        TEXT
    )
    """,
    # Migração para tenants já existentes (idempotente).
    "ALTER TABLE alert_settings ADD COLUMN IF NOT EXISTS engagement_days INTEGER NOT NULL DEFAULT 7",
    "ALTER TABLE alert_settings ADD COLUMN IF NOT EXISTS engagement_include_today BOOLEAN NOT NULL DEFAULT FALSE",
    """
    CREATE TABLE IF NOT EXISTS alert_rules (
        tenant_id     TEXT NOT NULL,
        alert_type    TEXT NOT NULL,
        enabled       BOOLEAN NOT NULL DEFAULT TRUE,
        email_enabled BOOLEAN NOT NULL DEFAULT FALSE,
        recipients    TEXT,
        PRIMARY KEY (tenant_id, alert_type)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS risky_sites (
        id         BIGSERIAL PRIMARY KEY,
        tenant_id  TEXT NOT NULL,
        pattern    TEXT NOT NULL,
        label      TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_risky_sites_tenant ON risky_sites(tenant_id)",
    """
    CREATE TABLE IF NOT EXISTS alert_notify_log (
        tenant_id  TEXT NOT NULL,
        alert_type TEXT NOT NULL,
        user_id    TEXT NOT NULL,
        day        TEXT NOT NULL,
        sent_at    TEXT NOT NULL,
        PRIMARY KEY (tenant_id, alert_type, user_id, day)
    )
    """,
])

ALERT_TYPES  = ("afk_today", "absent_today", "risky_site", "underwork", "overwork")
ALERT_LABELS = {
    "afk_today":    "Excesso de AFK no dia",
    "absent_today": "Ausente no dia",
    "risky_site":   "Site potencialmente perigoso",
    "underwork":    "Possível baixo engajamento",
    "overwork":     "Possível sobrecarga",
}


def _easter(year: int) -> "dt.date":
    a = year % 19; b = year // 100; c = year % 100
    d = b // 4; e = b % 4; f = (b + 8) // 25; g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30; i = c // 4; k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return dt.date(year, month, day)


def _br_holidays(year: int) -> set:
    """Feriados NACIONAIS oficiais (fixos + Sexta-feira Santa). Carnaval e
    Corpus Christi são ponto facultativo → não entram. Consciência Negra
    (20/11) é feriado nacional desde 2024 (Lei 14.759/2023)."""
    fixed = [(1, 1), (4, 21), (5, 1), (9, 7), (10, 12), (11, 2), (11, 15), (12, 25)]
    hs = {dt.date(year, mo, da) for (mo, da) in fixed}
    if year >= 2024:
        hs.add(dt.date(year, 11, 20))
    hs.add(_easter(year) - dt.timedelta(days=2))   # Sexta-feira Santa
    return hs


def _is_business_day(d: "dt.date") -> bool:
    return d.weekday() < 5 and d not in _br_holidays(d.year)


def _alert_settings(tenant: str) -> dict:
    with db() as c:
        row = c.execute("SELECT * FROM alert_settings WHERE tenant_id=%s", (tenant,)).fetchone()
    if row:
        return dict(row)
    return {"tenant_id": tenant, "business_start": "08:00", "business_end": "18:00",
            "absent_cutoff": "10:00", "afk_threshold_min": 120,
            "underwork_max_h": 2.0, "overwork_min_h": 10.0,
            "engagement_days": 7, "engagement_include_today": False}


def _alert_rules(tenant: str) -> dict:
    with db() as c:
        rows = c.execute("SELECT * FROM alert_rules WHERE tenant_id=%s", (tenant,)).fetchall()
    by = {r["alert_type"]: dict(r) for r in rows}
    out = {}
    for t in ALERT_TYPES:
        r = by.get(t)
        rec = []
        if r and r.get("recipients"):
            try:
                rec = json.loads(r["recipients"])
            except Exception:
                rec = []
        out[t] = {
            "enabled":       bool(r["enabled"]) if r else True,
            "email_enabled": bool(r["email_enabled"]) if r else False,
            "recipients":    rec,
        }
    return out


def _risky_sites(tenant: str) -> list:
    with db() as c:
        rows = c.execute(
            "SELECT id, pattern, label FROM risky_sites WHERE tenant_id=%s ORDER BY pattern",
            (tenant,),
        ).fetchall()
    return [dict(r) for r in rows]


def _local_tz():
    return ZoneInfo(REPORT_TZ) if ZoneInfo else dt.timezone.utc


def _hhmm_local(base_local, hhmm: str):
    try:
        h, m = (hhmm or "00:00").split(":")
        return base_local.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    except Exception:
        return base_local.replace(hour=0, minute=0, second=0, microsecond=0)


def _business_days_count(since_iso: str, until_iso: str) -> int:
    """Número de dias úteis (seg–sex, sem feriado nacional) no intervalo local."""
    tz = _local_tz()
    try:
        d0 = dt.datetime.fromisoformat(since_iso.replace("Z", "+00:00")).astimezone(tz).date()
        d1 = dt.datetime.fromisoformat(until_iso.replace("Z", "+00:00")).astimezone(tz).date()
    except Exception:
        return 1
    n, d = 0, d0
    while d <= d1:
        if _is_business_day(d):
            n += 1
        d += dt.timedelta(days=1)
    return max(n, 1)


def _compute_day_alerts(tenant: str, settings: dict, risky: list, allowed=None) -> dict:
    """AFK no dia, ausência e sites perigosos — sempre referentes a HOJE (local)."""
    tz = _local_tz()
    now_local = dt.datetime.now(tz)
    today = now_local.date()
    is_biz = _is_business_day(today)

    bstart  = _hhmm_local(now_local, settings.get("business_start", "08:00"))
    bend    = _hhmm_local(now_local, settings.get("business_end", "18:00"))
    cutoff  = _hhmm_local(now_local, settings.get("absent_cutoff", "10:00"))
    bend_eff = min(bend, now_local)

    day_start_utc = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc).isoformat()
    now_utc       = now_local.astimezone(dt.timezone.utc).isoformat()
    bstart_utc    = bstart.astimezone(dt.timezone.utc).isoformat()
    bend_eff_utc  = bend_eff.astimezone(dt.timezone.utc).isoformat()
    prev7_utc     = (now_local - dt.timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc).isoformat()

    sc = " AND user_id = ANY(%s)" if allowed is not None else ""
    sa = [allowed] if allowed is not None else []

    with db() as c:
        users = {r["user_id"]: dict(r) for r in c.execute(
            f"SELECT user_id, display_name, department, last_seen FROM users WHERE tenant_id=%s"
            + (" AND user_id = ANY(%s)" if allowed is not None else ""),
            tuple([tenant, *([allowed] if allowed is not None else [])]),
        ).fetchall()}
        started = {r["user_id"] for r in c.execute(
            f"SELECT DISTINCT user_id FROM events WHERE tenant_id=%s AND bucket='window' AND ts>=%s AND ts<=%s{sc}",
            tuple([tenant, day_start_utc, now_utc, *sa]),
        ).fetchall()}
        afk_rows = c.execute(
            f"""SELECT user_id, SUM(duration_sec) AS afk
                FROM events WHERE tenant_id=%s AND bucket='afk' AND data_json->>'status'='afk'
                  AND ts>=%s AND ts<=%s{sc}
                GROUP BY user_id""",
            tuple([tenant, bstart_utc, bend_eff_utc, *sa]),
        ).fetchall()
        expected = {r["user_id"] for r in c.execute(
            f"SELECT DISTINCT user_id FROM events WHERE tenant_id=%s AND bucket='window' AND ts>=%s AND ts<%s{sc}",
            tuple([tenant, prev7_utc, day_start_utc, *sa]),
        ).fetchall()}
        risky_rows = []
        if risky:
            risky_rows = c.execute(
                f"""SELECT user_id, data_json->>'app' AS app, data_json->>'title' AS title,
                           SUM(duration_sec) AS sec
                    FROM events WHERE tenant_id=%s AND bucket='window' AND ts>=%s AND ts<=%s{sc}
                    GROUP BY user_id, app, title""",
                tuple([tenant, day_start_utc, now_utc, *sa]),
            ).fetchall()

    afk_list, absent_list, risky_list = [], [], []
    thr = int(settings.get("afk_threshold_min", 120) or 120) * 60

    if is_biz:
        for r in afk_rows:
            uid = r["user_id"]; sec = r["afk"] or 0
            if uid in started and sec >= thr:
                u = users.get(uid, {})
                afk_list.append({"user_id": uid, "display_name": u.get("display_name"),
                                 "department": u.get("department"), "afk_minutes": round(sec / 60)})
        if now_local >= cutoff:
            for uid in sorted(expected):
                if uid not in started:
                    u = users.get(uid, {})
                    absent_list.append({"user_id": uid, "display_name": u.get("display_name"),
                                        "department": u.get("department"), "last_seen": u.get("last_seen")})

    if risky and risky_rows:
        agg = {}
        for r in risky_rows:
            app = r["app"] or ""; title = r["title"] or ""
            if not _is_browser(app):
                continue
            hay = (app + " " + title).casefold()
            matched = None
            for p in risky:
                pp = (p.get("pattern") or "").casefold()
                if pp and pp in hay:
                    matched = p.get("pattern"); break
            if matched:
                uid = r["user_id"]; key = (uid, matched)
                e = agg.setdefault(key, {"user_id": uid, "site": matched,
                                         "sample": _clean_title(title), "sec": 0.0})
                e["sec"] += r["sec"] or 0.0
        for (uid, _mat), e in agg.items():
            u = users.get(uid, {})
            risky_list.append({"user_id": uid, "display_name": u.get("display_name"),
                               "site": e["site"], "sample": e["sample"],
                               "minutes": round(e["sec"] / 60)})

    # Ordena do maior para o menor (mais crítico no topo).
    afk_list.sort(key=lambda x: x.get("afk_minutes", 0), reverse=True)
    risky_list.sort(key=lambda x: x.get("minutes", 0), reverse=True)
    absent_list.sort(key=lambda x: (x.get("display_name") or x.get("user_id") or "").lower())

    return {"afk_today": afk_list, "absent_today": absent_list, "risky_site": risky_list,
            "today_is_business": is_biz}


def _last_n_business_days(n: int = 7, include_today: bool = True):
    """Devolve a lista das últimas N datas ÚTEIS (locais), pulando fim de semana e
    feriado nacional. Se include_today=False, começa a contar a partir de ONTEM
    (o dia corrente é parcial e distorce a média)."""
    tz = _local_tz()
    d = dt.datetime.now(tz).date()
    if not include_today:
        d -= dt.timedelta(days=1)
    out = []
    guard = 0
    while len(out) < n and guard < (n * 3 + 30):
        if _is_business_day(d):
            out.append(d)
        d -= dt.timedelta(days=1)
        guard += 1
    return out


def _compute_engagement_alerts(tenant: str, settings: dict, allowed=None) -> dict:
    """Sobrecarga / baixo engajamento — média de horas ativas por **dia trabalhado**,
    olhando os últimos 7 dias úteis (inclui hoje; ignora fim de semana e feriado).
    Importante: divide pela quantidade de dias úteis em que a pessoa **teve coleta**
    (esteve presente), NÃO por 7. Assim quem faltou (possível justificativa) não é
    penalizado; o alerta isola quem **trabalhou pouco nos dias em que esteve presente**.
    Quem não teve coleta nenhuma na janela não aparece."""
    tz = _local_tz()
    n_days    = int(settings.get("engagement_days", 7) or 7)
    inc_today = bool(settings.get("engagement_include_today", False))
    bizdays = _last_n_business_days(n_days, include_today=inc_today)
    bizset = {d.isoformat() for d in bizdays}
    start_d = min(bizdays)
    since = dt.datetime(start_d.year, start_d.month, start_d.day, 0, 0, 0, tzinfo=tz).astimezone(dt.timezone.utc).isoformat()
    until = dt.datetime.now(dt.timezone.utc).isoformat()

    sc = " AND user_id = ANY(%(uids)s)" if allowed is not None else ""
    params = {"tz": REPORT_TZ, "tenant": tenant, "since": since, "until": until}
    if allowed is not None:
        params["uids"] = allowed

    with db() as c:
        rows = c.execute(
            f"""SELECT user_id,
                       to_char((ts::timestamptz AT TIME ZONE %(tz)s)::date, 'YYYY-MM-DD') AS lday,
                       SUM(duration_sec) AS sec
                FROM events
                WHERE tenant_id=%(tenant)s AND bucket='window'
                  AND ts::timestamptz >= %(since)s::timestamptz
                  AND ts::timestamptz <= %(until)s::timestamptz{sc}
                GROUP BY user_id, lday""",
            params,
        ).fetchall()
        names = {r["user_id"]: r["display_name"] for r in c.execute(
            "SELECT user_id, display_name FROM users WHERE tenant_id=%s", (tenant,)
        ).fetchall()}

    # Por usuário: soma de ativo e CONTAGEM de dias úteis com coleta (dias trabalhados).
    # Cada linha (user_id, lday) só existe se houve evento 'window' naquele dia → é um
    # dia em que a pessoa esteve presente.
    sec_by_user  = {}
    days_by_user = {}
    for r in rows:
        if r["lday"] in bizset:
            uid = r["user_id"]
            sec_by_user[uid]  = sec_by_user.get(uid, 0.0) + float(r["sec"] or 0)
            days_by_user[uid] = days_by_user.get(uid, 0) + 1

    umax = float(settings.get("underwork_max_h", 2) or 2)
    omin = float(settings.get("overwork_min_h", 10) or 10)
    over, under = [], []
    for uid, sec in sec_by_user.items():
        worked = days_by_user.get(uid, 0)
        if worked <= 0:
            continue
        avg = (sec / 3600.0) / worked          # média por DIA TRABALHADO (com coleta)
        nm = names.get(uid)
        if avg > omin:
            over.append({"user_id": uid, "display_name": nm, "avg_day_hours": round(avg, 1), "days": worked})
        if 0 < avg < umax:
            under.append({"user_id": uid, "display_name": nm, "avg_day_hours": round(avg, 1), "days": worked})
    over.sort(key=lambda x: x["avg_day_hours"], reverse=True)
    under.sort(key=lambda x: x["avg_day_hours"])
    return {"overwork": over[:50], "underwork": under[:50], "window_business_days": len(bizdays)}


# --- Modelos ---
class AlertRuleIn(BaseModel):
    alert_type:    str
    enabled:       bool = True
    email_enabled: bool = False
    recipients:    list[str] = []


class AlertConfigIn(BaseModel):
    business_start:    str = "08:00"
    business_end:      str = "18:00"
    absent_cutoff:     str = "10:00"
    afk_threshold_min: int = 120
    underwork_max_h:   float = 2
    overwork_min_h:    float = 10
    engagement_days:          int = 7
    engagement_include_today: bool = False
    rules:             list[AlertRuleIn] = []


class RiskySiteIn(BaseModel):
    pattern: str
    label:   str | None = None


# --- Endpoints ---
@app.get("/v1/alerts/today", dependencies=[Depends(require_tenant_scope)])
def alerts_today(tenant: str, request: Request, days: int = 7,
                 since: str | None = None, until: str | None = None):
    if since is None:
        since = _since_for_days(days)
    if until is None:
        until = dt.datetime.now(dt.timezone.utc).isoformat()
    allowed = _lider_allowed_ids(request, tenant)
    settings = _alert_settings(tenant)
    rules    = _alert_rules(tenant)
    risky    = _risky_sites(tenant)
    day = _compute_day_alerts(tenant, settings, risky, allowed=allowed)
    eng = _compute_engagement_alerts(tenant, settings, allowed=allowed)   # janela fixa: últimos 7 dias úteis
    return {
        "tenant": tenant,
        "settings": {k: settings[k] for k in
                     ("business_start", "business_end", "absent_cutoff",
                      "afk_threshold_min", "underwork_max_h", "overwork_min_h",
                      "engagement_days", "engagement_include_today")},
        "rules": rules,
        "today_is_business": day["today_is_business"],
        "alerts": {
            "afk_today":    day["afk_today"],
            "absent_today": day["absent_today"],
            "risky_site":   day["risky_site"],
            "underwork":    eng["underwork"],
            "overwork":     eng["overwork"],
        },
    }


@app.get("/v1/alerts/config")
def alerts_config_get(tenant: str, ctx: Annotated[dict, Depends(require_role("admin_master", "admin"))]):
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    s = _alert_settings(tid)
    return {
        "settings": {k: s[k] for k in
                     ("business_start", "business_end", "absent_cutoff",
                      "afk_threshold_min", "underwork_max_h", "overwork_min_h",
                      "engagement_days", "engagement_include_today")},
        "rules": _alert_rules(tid),
        "risky_sites": _risky_sites(tid),
        "alert_labels": ALERT_LABELS,
    }


@app.put("/v1/alerts/config")
def alerts_config_set(body: AlertConfigIn, tenant: str, request: Request,
                      ctx: Annotated[dict, Depends(require_role("admin_master", "admin"))]):
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    with db() as c:
        c.execute(
            """INSERT INTO alert_settings
                 (tenant_id, business_start, business_end, absent_cutoff,
                  afk_threshold_min, underwork_max_h, overwork_min_h,
                  engagement_days, engagement_include_today, updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (tenant_id) DO UPDATE SET
                 business_start=EXCLUDED.business_start, business_end=EXCLUDED.business_end,
                 absent_cutoff=EXCLUDED.absent_cutoff, afk_threshold_min=EXCLUDED.afk_threshold_min,
                 underwork_max_h=EXCLUDED.underwork_max_h, overwork_min_h=EXCLUDED.overwork_min_h,
                 engagement_days=EXCLUDED.engagement_days,
                 engagement_include_today=EXCLUDED.engagement_include_today,
                 updated_at=EXCLUDED.updated_at""",
            (tid, body.business_start, body.business_end, body.absent_cutoff,
             int(body.afk_threshold_min), float(body.underwork_max_h),
             float(body.overwork_min_h),
             max(1, int(body.engagement_days)), bool(body.engagement_include_today),
             _now_utc().isoformat()),
        )
        for r in body.rules:
            if r.alert_type not in ALERT_TYPES:
                continue
            recips = [e.strip() for e in (r.recipients or []) if e and "@" in e]
            c.execute(
                """INSERT INTO alert_rules (tenant_id, alert_type, enabled, email_enabled, recipients)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id, alert_type) DO UPDATE SET
                     enabled=EXCLUDED.enabled, email_enabled=EXCLUDED.email_enabled,
                     recipients=EXCLUDED.recipients""",
                (tid, r.alert_type, bool(r.enabled), bool(r.email_enabled), json.dumps(recips)),
            )
    audit(ctx.get("email"), "alerts.config_set", target=tid, ip=_client_ip(request))
    return {"ok": True}


@app.post("/v1/alerts/risky-sites")
def alerts_risky_add(body: RiskySiteIn, tenant: str,
                     ctx: Annotated[dict, Depends(require_role("admin_master", "admin"))]):
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    pat = (body.pattern or "").strip()
    if not pat:
        raise HTTPException(400, "informe o padrão do site (ex.: facebook.com)")
    with db() as c:
        c.execute(
            "INSERT INTO risky_sites (tenant_id, pattern, label, created_at) VALUES (%s,%s,%s,%s)",
            (tid, pat, (body.label or None), _now_utc().isoformat()),
        )
    return {"ok": True}


@app.delete("/v1/alerts/risky-sites/{rid}")
def alerts_risky_del(rid: int, tenant: str,
                     ctx: Annotated[dict, Depends(require_role("admin_master", "admin"))]):
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    with db() as c:
        c.execute("DELETE FROM risky_sites WHERE id=%s AND tenant_id=%s", (rid, tid))
    return {"ok": True}


class RiskyBulkIn(BaseModel):
    items: list[RiskySiteIn] = []


@app.post("/v1/alerts/risky-sites/bulk")
def alerts_risky_bulk(body: RiskyBulkIn, tenant: str,
                      ctx: Annotated[dict, Depends(require_role("admin_master", "admin"))]):
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    added, skipped = 0, 0
    with db() as c:
        existing = {(r["pattern"] or "").casefold() for r in c.execute(
            "SELECT pattern FROM risky_sites WHERE tenant_id=%s", (tid,)).fetchall()}
        for it in (body.items or []):
            pat = (it.pattern or "").strip()
            if not pat:
                continue
            if pat.casefold() in existing:
                skipped += 1
                continue
            c.execute(
                "INSERT INTO risky_sites (tenant_id, pattern, label, created_at) VALUES (%s,%s,%s,%s)",
                (tid, pat, (it.label or None), _now_utc().isoformat()),
            )
            existing.add(pat.casefold())
            added += 1
    return {"ok": True, "added": added, "skipped": skipped}


# --- Worker de e-mail (envia quando o alerta acontece, dedupe por dia) ---
def _alert_email_html(alert_type: str, items: list) -> str:
    label = ALERT_LABELS.get(alert_type, alert_type)
    li = []
    for it in items:
        name = it.get("display_name") or it.get("user_id")
        if alert_type == "afk_today":
            li.append(f"<li><b>{name}</b> — {it.get('afk_minutes', 0)} min de AFK no horário comercial</li>")
        elif alert_type == "absent_today":
            li.append(f"<li><b>{name}</b> — ainda não iniciou a jornada</li>")
        elif alert_type == "risky_site":
            li.append(f"<li><b>{name}</b> — acessou <code>{it.get('site')}</code> ({it.get('minutes', 0)} min)</li>")
        else:
            li.append(f"<li><b>{name}</b> — média {it.get('avg_day_hours', 0)}h/dia útil</li>")
    return (f"<p>Alerta WorkPulse — <b>{label}</b>:</p><ul>{''.join(li)}</ul>"
            f"<p style='color:#888;font-size:12px'>WorkPulse — use como ponto de partida para conversas, não como veredicto.</p>")


def _do_alert_emails() -> None:
    tz = _local_tz()
    today_iso = dt.datetime.now(tz).date().isoformat()
    with db() as c:
        tenants = [r["tenant_id"] for r in c.execute(
            "SELECT DISTINCT tenant_id FROM alert_rules WHERE email_enabled=TRUE"
        ).fetchall()]
    for tid in tenants:
        ecfg = _email_cfg_for_send(tid)
        if not ecfg:
            continue  # sem SMTP configurado → não há como enviar
        settings = _alert_settings(tid)
        rules    = _alert_rules(tid)
        risky    = _risky_sites(tid)
        day = _compute_day_alerts(tid, settings, risky)
        eng = _compute_engagement_alerts(tid, settings)   # janela fixa: últimos 7 dias úteis
        bag = {"afk_today": day["afk_today"], "absent_today": day["absent_today"],
               "risky_site": day["risky_site"], "underwork": eng["underwork"],
               "overwork": eng["overwork"]}
        for atype, items in bag.items():
            rule = rules.get(atype, {})
            if not (rule.get("enabled") and rule.get("email_enabled") and rule.get("recipients")):
                continue
            fresh = []
            with db() as c:
                for it in items:
                    uid = it.get("user_id")
                    if not uid:
                        continue
                    # Site perigoso: dedupe por pessoa+site (cada domínio novo alerta).
                    # Demais alertas: dedupe por pessoa (1x no dia).
                    log_key = uid
                    if atype == "risky_site":
                        log_key = f"{uid}|{(it.get('site') or '').casefold()}"
                    try:
                        c.execute(
                            "INSERT INTO alert_notify_log (tenant_id, alert_type, user_id, day, sent_at) "
                            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                            (tid, atype, log_key, today_iso, _now_utc().isoformat()),
                        )
                        if c.rowcount and c.rowcount > 0:
                            fresh.append(it)
                    except Exception:
                        pass
            if not fresh:
                continue
            try:
                _smtp_send(ecfg, rule["recipients"],
                           f"WorkPulse — {ALERT_LABELS.get(atype, atype)}",
                           _alert_email_html(atype, fresh))
                print(f"[alerts] emailed {atype} → {rule['recipients']} ({len(fresh)} pessoa(s))", flush=True)
            except Exception as exc:
                print(f"[alerts] ERRO email {atype} tenant={tid}: {exc}", flush=True)


async def _alerts_worker() -> None:
    """Verifica periodicamente e envia e-mails de alerta (dedupe por dia)."""
    await asyncio.sleep(45)
    while True:
        try:
            _do_alert_emails()
        except Exception as exc:
            print(f"[alerts] worker erro: {exc}", flush=True)
        await asyncio.sleep(15 * 60)


# ===========================================================================
# Exclusão de coletas / colaboradores + lista de bloqueio de agentes.
# Tudo restrito ao Admin Master (require_role("admin_master")).
# ===========================================================================
_SCHEMA_STMTS.extend([
    """
    CREATE TABLE IF NOT EXISTS blocked_devices (
        tenant_id  TEXT NOT NULL,
        user_id    TEXT NOT NULL,
        reason     TEXT,
        blocked_by TEXT,
        blocked_at TEXT NOT NULL,
        PRIMARY KEY (tenant_id, user_id)
    )
    """,
])


def _purge_user_events(tenant: str, user_id: str) -> None:
    """Apaga só as COLETAS de um colaborador (eventos + agregados + IPs), mantendo o cadastro."""
    with db() as c:
        c.execute("DELETE FROM events          WHERE tenant_id=%s AND user_id=%s", (tenant, user_id))
        c.execute("DELETE FROM daily_summaries WHERE tenant_id=%s AND user_id=%s", (tenant, user_id))
        c.execute("DELETE FROM client_ips      WHERE tenant_id=%s AND user_id=%s", (tenant, user_id))
    _cache_bust(tenant)


@app.post("/v1/users/{user_id}/purge-events")
def purge_user_events_ep(user_id: str, tenant: str, request: Request,
                         ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    """Limpar coletas: zera os dados coletados, mantém o colaborador na lista."""
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    _purge_user_events(tid, user_id)
    audit(ctx.get("email"), "user.purge_events", target=user_id, detail=f"tenant={tid}", ip=_client_ip(request))
    return {"ok": True}


@app.delete("/v1/users/{user_id}")
def delete_user_full(user_id: str, tenant: str, request: Request,
                     ctx: Annotated[dict, Depends(require_role("admin_master"))],
                     block: bool = True):
    """Excluir colaborador: remove cadastro + todas as coletas. Por padrão também
    BLOQUEIA o agente (block=True) para não voltar a alimentar dados se ainda instalado."""
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    _purge_user_events(tid, user_id)
    with db() as c:
        c.execute("DELETE FROM devices        WHERE tenant_id=%s AND user_id=%s", (tid, user_id))
        c.execute("DELETE FROM dash_seat_alloc WHERE tenant_id=%s AND user_id=%s", (tid, user_id))
        c.execute("DELETE FROM lider_scope    WHERE tenant_id=%s AND scope_type='user' AND scope_value=%s", (tid, user_id))
        c.execute("DELETE FROM users          WHERE tenant_id=%s AND user_id=%s", (tid, user_id))
        if block:
            c.execute(
                "INSERT INTO blocked_devices (tenant_id, user_id, reason, blocked_by, blocked_at) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (tenant_id, user_id) DO NOTHING",
                (tid, user_id, "excluído pelo Admin Master", ctx.get("email"), _now_utc().isoformat()),
            )
    _cache_bust(tid)
    audit(ctx.get("email"), "user.delete", target=user_id, detail=f"tenant={tid};block={block}", ip=_client_ip(request))
    return {"ok": True, "blocked": bool(block)}


@app.get("/v1/blocked-devices")
def blocked_devices_list(tenant: str, ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    with db() as c:
        rows = c.execute(
            "SELECT user_id, reason, blocked_by, blocked_at FROM blocked_devices "
            "WHERE tenant_id=%s ORDER BY blocked_at DESC", (tid,),
        ).fetchall()
    return {"blocked": [dict(r) for r in rows]}


@app.delete("/v1/blocked-devices/{user_id}")
def blocked_devices_unblock(user_id: str, tenant: str, request: Request,
                            ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    """Desbloquear: o agente volta a poder enviar coletas (recria o usuário no próximo envio)."""
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    with db() as c:
        c.execute("DELETE FROM blocked_devices WHERE tenant_id=%s AND user_id=%s", (tid, user_id))
    audit(ctx.get("email"), "device.unblock", target=user_id, detail=f"tenant={tid}", ip=_client_ip(request))
    return {"ok": True}


# ===========================================================================
# Configuração CENTRAL do agente — global (Platform Admin) + override por tenant
# (Admin Master). O agente busca em /v1/agent/config (autenticado pela licença) e
# aplica sem precisar recompilar/reinstalar para mudanças de lista.
# ===========================================================================
_SCHEMA_STMTS.extend([
    """
    CREATE TABLE IF NOT EXISTS agent_config_global (
        id                     INTEGER PRIMARY KEY,
        afk_timeout_sec        INTEGER,
        poll_interval_ms       INTEGER,
        heartbeat_interval_sec INTEGER,
        capture_titles         BOOLEAN,
        meeting_patterns       TEXT,
        updated_at             TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_config_tenant (
        tenant_id              TEXT PRIMARY KEY,
        afk_timeout_sec        INTEGER,
        poll_interval_ms       INTEGER,
        heartbeat_interval_sec INTEGER,
        capture_titles         BOOLEAN,
        meeting_patterns       TEXT,
        updated_at             TEXT
    )
    """,
])

# ---------------------------------------------------------------------------
# Tabelas de ROLLUP (agregados pré-computados p/ os painéis).
# O dashboard lê destas tabelas (centenas de linhas) em vez de varrer events
# (centenas de milhares). Um worker as mantém atualizadas incrementalmente.
# ---------------------------------------------------------------------------
_SCHEMA_STMTS.extend([
    # Ativo/AFK por (usuário, hora-UTC). 'hour' = 'YYYY-MM-DDTHH'.
    """
    CREATE TABLE IF NOT EXISTS agg_user_hour (
        tenant_id  TEXT NOT NULL,
        user_id    TEXT NOT NULL,
        hour       TEXT NOT NULL,
        active_sec DOUBLE PRECISION NOT NULL DEFAULT 0,
        afk_sec    DOUBLE PRECISION NOT NULL DEFAULT 0,
        UNIQUE (tenant_id, user_id, hour)
    )
    """,
    # Jornada por (usuário, dia-LOCAL). 'day' = 'YYYY-MM-DD' no fuso REPORT_TZ.
    """
    CREATE TABLE IF NOT EXISTS agg_user_day (
        tenant_id     TEXT NOT NULL,
        user_id       TEXT NOT NULL,
        day           TEXT NOT NULL,
        focus_sec     DOUBLE PRECISION NOT NULL DEFAULT 0,
        afk_in_sec    DOUBLE PRECISION NOT NULL DEFAULT 0,
        afk_after_sec DOUBLE PRECISION NOT NULL DEFAULT 0,
        start_sec     DOUBLE PRECISION NOT NULL DEFAULT 0,
        end_sec       DOUBLE PRECISION NOT NULL DEFAULT 0,
        UNIQUE (tenant_id, user_id, day)
    )
    """,
    # Tempo ativo por (dia-LOCAL, app) — alimenta o "Top Aplicações".
    # 'app' = representante (MIN) do grupo case-insensitive, igual à query ao vivo.
    """
    CREATE TABLE IF NOT EXISTS agg_app_day (
        tenant_id  TEXT NOT NULL,
        day        TEXT NOT NULL,
        app        TEXT NOT NULL,
        active_sec DOUBLE PRECISION NOT NULL DEFAULT 0,
        UNIQUE (tenant_id, day, app)
    )
    """,
    # Cursor de backfill por tenant. oldest_day = dia local mais antigo já agregado;
    # [oldest_day .. hoje] é contíguo. done=TRUE quando alcançou a retenção.
    """
    CREATE TABLE IF NOT EXISTS rollup_state (
        tenant_id  TEXT PRIMARY KEY,
        oldest_day TEXT,
        done       BOOLEAN NOT NULL DEFAULT FALSE,
        ver        INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT
    )
    """,
    # Migração p/ instalações que já tinham rollup_state sem a coluna 'ver'.
    "ALTER TABLE rollup_state ADD COLUMN IF NOT EXISTS ver INTEGER NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS idx_agg_uh_tenant_hour ON agg_user_hour(tenant_id, hour)",
    "CREATE INDEX IF NOT EXISTS idx_agg_ud_tenant_day  ON agg_user_day(tenant_id, day)",
    "CREATE INDEX IF NOT EXISTS idx_agg_ad_tenant_day  ON agg_app_day(tenant_id, day)",
])

# ---------------------------------------------------------------------------
# Releases do agente — histórico de instaladores (.msi/.deb) p/ download.
# Arquivos ficam na bucket privada (WORKPULSE_RELEASES_BUCKET); aqui só o manifesto.
# ---------------------------------------------------------------------------
_SCHEMA_STMTS.extend([
    """
    CREATE TABLE IF NOT EXISTS agent_releases (
        id          BIGSERIAL PRIMARY KEY,
        channel     TEXT NOT NULL DEFAULT 'generic',   -- 'generic' ou 'tenant:<id>'
        tenant_id   TEXT,
        os          TEXT NOT NULL,                      -- 'windows' | 'linux'
        arch        TEXT NOT NULL DEFAULT 'amd64',
        version     TEXT NOT NULL,
        filename    TEXT NOT NULL,
        s3_key      TEXT NOT NULL,
        sha256      TEXT NOT NULL,
        size_bytes  BIGINT NOT NULL DEFAULT 0,
        notes       TEXT,
        is_latest   BOOLEAN NOT NULL DEFAULT FALSE,
        uploaded_by TEXT,
        created_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_releases_chan ON agent_releases(channel, os, created_at DESC)",
])

# ---------------------------------------------------------------------------
# Enrollment — onboarding do agente genérico (código → licença do tenant).
# ---------------------------------------------------------------------------
_SCHEMA_STMTS.extend([
    """
    CREATE TABLE IF NOT EXISTS enrollment_codes (
        code        TEXT PRIMARY KEY,
        tenant_id   TEXT NOT NULL,
        label       TEXT,
        active      BOOLEAN NOT NULL DEFAULT TRUE,
        expires_at  TEXT,                 -- null = permanente
        created_by  TEXT,
        created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS enrolled_devices (
        id           BIGSERIAL PRIMARY KEY,
        tenant_id    TEXT NOT NULL,
        device_id    TEXT NOT NULL,
        code         TEXT,
        status       TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
        hostname     TEXT,
        os           TEXT,
        requested_at TEXT NOT NULL,
        decided_at   TEXT,
        decided_by   TEXT,
        UNIQUE (tenant_id, device_id)
    )
    """,
    # Flag por tenant: exigir aprovação manual de novos agentes (default: sim).
    "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS enroll_approval BOOLEAN NOT NULL DEFAULT TRUE",
    "CREATE INDEX IF NOT EXISTS idx_enroll_codes_tenant   ON enrollment_codes(tenant_id)",
    "CREATE INDEX IF NOT EXISTS idx_enrolled_dev_tenant   ON enrolled_devices(tenant_id, status)",
])

# Fallback embutido (se não houver linha global ainda). Espelha os defaults do agente.
_AGENT_CFG_DEFAULTS = {
    "afk_timeout_sec": 180,
    "poll_interval_ms": 1000,
    "heartbeat_interval_sec": 5,
    "capture_titles": True,
    "meeting_patterns": [
        "meet.google.com", "google meet", "meet -", "meet:",
        "teams.microsoft.com", "microsoft teams",
        "zoom meeting", "zoom.us",
        "webex", "whereby.com", "gotomeeting", "goto meeting", "bluejeans",
    ],
}
_AGENT_CFG_FIELDS = ("afk_timeout_sec", "poll_interval_ms", "heartbeat_interval_sec",
                     "capture_titles", "meeting_patterns")


def _agent_cfg_decode(row) -> dict:
    """Linha (global ou tenant) → dict só com os campos NÃO nulos."""
    out = {}
    if not row:
        return out
    for f in _AGENT_CFG_FIELDS:
        v = row[f]
        if v is None:
            continue
        if f == "meeting_patterns":
            try:
                out[f] = json.loads(v) if isinstance(v, str) else v
            except Exception:
                continue
        elif f == "capture_titles":
            out[f] = bool(v)
        else:
            out[f] = int(v)
    return out


def _agent_cfg_global() -> dict:
    with db() as c:
        row = c.execute("SELECT * FROM agent_config_global WHERE id=1").fetchone()
    eff = dict(_AGENT_CFG_DEFAULTS)
    eff.update(_agent_cfg_decode(row))
    return eff


def _agent_cfg_effective(tenant: str) -> dict:
    eff = _agent_cfg_global()
    with db() as c:
        row = c.execute("SELECT * FROM agent_config_tenant WHERE tenant_id=%s", (tenant,)).fetchone()
    eff.update(_agent_cfg_decode(row))   # override por tenant vence o global
    return eff


def _agent_cfg_upsert(table: str, key_col: str, key_val, body: "AgentCfgIn", allow_null: bool):
    """Grava global (allow_null=False: usa default quando vier vazio) ou override
    por tenant (allow_null=True: vazio = herda global)."""
    def col(field, default):
        v = getattr(body, field)
        if v is None:
            return None if allow_null else default
        return v
    mp = getattr(body, "meeting_patterns")
    mp_val = (None if (mp is None and allow_null)
              else json.dumps(mp if mp is not None else _AGENT_CFG_DEFAULTS["meeting_patterns"]))
    with db() as c:
        c.execute(
            f"""INSERT INTO {table}
                  ({key_col}, afk_timeout_sec, poll_interval_ms, heartbeat_interval_sec,
                   capture_titles, meeting_patterns, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT ({key_col}) DO UPDATE SET
                  afk_timeout_sec=EXCLUDED.afk_timeout_sec,
                  poll_interval_ms=EXCLUDED.poll_interval_ms,
                  heartbeat_interval_sec=EXCLUDED.heartbeat_interval_sec,
                  capture_titles=EXCLUDED.capture_titles,
                  meeting_patterns=EXCLUDED.meeting_patterns,
                  updated_at=EXCLUDED.updated_at""",
            (key_val,
             col("afk_timeout_sec", _AGENT_CFG_DEFAULTS["afk_timeout_sec"]),
             col("poll_interval_ms", _AGENT_CFG_DEFAULTS["poll_interval_ms"]),
             col("heartbeat_interval_sec", _AGENT_CFG_DEFAULTS["heartbeat_interval_sec"]),
             col("capture_titles", _AGENT_CFG_DEFAULTS["capture_titles"]),
             mp_val, _now_utc().isoformat()),
        )


class AgentCfgIn(BaseModel):
    afk_timeout_sec:        int | None = None
    poll_interval_ms:       int | None = None
    heartbeat_interval_sec: int | None = None
    capture_titles:         bool | None = None
    meeting_patterns:       list[str] | None = None


# --- Agente: busca a config efetiva (autenticado pela licença) ---
@app.get("/v1/agent/config")
def agent_config_fetch(authorization: Annotated[str | None, Header()] = None,
                       x_tenant: Annotated[str | None, Header()] = None):
    if not x_tenant:
        raise HTTPException(status_code=400, detail="X-Tenant header required")
    token  = parse_bearer(authorization)
    claims = validate_agent_token(token, x_tenant)
    return _agent_cfg_effective(claims["tenant"])


# --- Platform Admin: config GLOBAL (vale para todos os tenants) ---
@app.get("/v1/admin/agent-config", dependencies=[Depends(require_admin)])
def admin_agent_config_get():
    return {"global": _agent_cfg_global(), "defaults": _AGENT_CFG_DEFAULTS}


@app.put("/v1/admin/agent-config", dependencies=[Depends(require_admin)])
def admin_agent_config_set(body: AgentCfgIn):
    _agent_cfg_upsert("agent_config_global", "id", 1, body, allow_null=False)
    return {"ok": True, "global": _agent_cfg_global()}


# --- Tenant (Admin Master): override do tenant (vazio = herda global) ---
@app.get("/v1/agent-config")
def tenant_agent_config_get(tenant: str, ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    with db() as c:
        row = c.execute("SELECT * FROM agent_config_tenant WHERE tenant_id=%s", (tid,)).fetchone()
    return {
        "global":    _agent_cfg_global(),
        "override":  _agent_cfg_decode(row),    # só os campos sobrescritos
        "effective": _agent_cfg_effective(tid),
    }


@app.put("/v1/agent-config")
def tenant_agent_config_set(body: AgentCfgIn, tenant: str, request: Request,
                            ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    tid = ctx["tenant"] if ctx["role"] != "wp_admin" else tenant
    _agent_cfg_upsert("agent_config_tenant", "tenant_id", tid, body, allow_null=True)
    audit(ctx.get("email"), "agent_config.set", target=tid, ip=_client_ip(request))
    return {"ok": True, "effective": _agent_cfg_effective(tid)}


# ===========================================================================
# Releases do agente (Platform Admin) — upload / histórico / download
# ===========================================================================
RELEASES_BUCKET = os.getenv("WORKPULSE_RELEASES_BUCKET", "")
_RELEASES_MAX_MB = int(os.getenv("WORKPULSE_RELEASES_MAX_MB", "200"))


def _s3():
    import boto3
    return boto3.client("s3", region_name=os.getenv("AWS_REGION", "us-east-1"))


def _release_row(r: dict) -> dict:
    d = dict(r)
    d.pop("s3_key", None)   # caminho interno não vai pro cliente
    return d


@app.post("/v1/admin/releases", dependencies=[Depends(require_admin)])
def releases_upload(
    request: Request,
    body:      Annotated[bytes, Body(media_type="application/octet-stream")],
    os_:       Annotated[str, Query(alias="os", pattern="^(windows|linux)$")],
    version:   str,
    filename:  str,
    arch:      str = "amd64",
    channel:   str = "generic",
    tenant_id: str | None = None,
    notes:     str | None = None,
):
    if not RELEASES_BUCKET:
        raise HTTPException(500, "bucket de releases não configurado")
    if not body:
        raise HTTPException(400, "arquivo vazio")
    if len(body) > _RELEASES_MAX_MB * 1024 * 1024:
        raise HTTPException(413, f"arquivo acima de {_RELEASES_MAX_MB} MB")
    if not _re.match(r"^\d+\.\d+\.\d+$", version):
        raise HTTPException(400, "versão inválida (use MAJOR.MINOR.PATCH)")

    safe_name = os.path.basename(filename).replace("\\", "").strip() or f"agent-{version}"
    chan = channel if channel == "generic" else f"tenant:{tenant_id or ''}"
    s3_key = f"{chan}/{os_}/{version}/{safe_name}"
    sha = _hashlib.sha256(body).hexdigest()

    _s3().put_object(Bucket=RELEASES_BUCKET, Key=s3_key, Body=body,
                     ContentType="application/octet-stream")

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    with db() as c:
        row = c.execute(
            """INSERT INTO agent_releases
                   (channel, tenant_id, os, arch, version, filename, s3_key,
                    sha256, size_bytes, notes, is_latest, uploaded_by, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE,%s,%s)
               RETURNING *""",
            (chan, tenant_id, os_, arch, version, safe_name, s3_key,
             sha, len(body), notes, "platform-admin", now),
        ).fetchone()
    audit("platform-admin", "release.upload", target=f"{chan}/{os_}/{version}",
          detail=f"sha256={sha[:12]} size={len(body)}", ip=_client_ip(request))
    return {"ok": True, "release": _release_row(row)}


@app.get("/v1/admin/releases", dependencies=[Depends(require_admin)])
def releases_list(channel: str | None = None, os_: Annotated[str | None, Query(alias="os")] = None):
    where, args = [], []
    if channel:
        where.append("channel=%s"); args.append(channel)
    if os_:
        where.append("os=%s"); args.append(os_)
    sql = "SELECT * FROM agent_releases"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC"
    with db() as c:
        rows = c.execute(sql, tuple(args)).fetchall()
    return {"releases": [_release_row(r) for r in rows]}


@app.put("/v1/admin/releases/{rid}/promote", dependencies=[Depends(require_admin)])
def releases_promote(rid: int, request: Request):
    with db() as c:
        r = c.execute("SELECT channel, os, version FROM agent_releases WHERE id=%s", (rid,)).fetchone()
        if not r:
            raise HTTPException(404, "release não encontrada")
        # Só uma "última" por (canal, SO).
        c.execute("UPDATE agent_releases SET is_latest=FALSE WHERE channel=%s AND os=%s",
                  (r["channel"], r["os"]))
        c.execute("UPDATE agent_releases SET is_latest=TRUE WHERE id=%s", (rid,))
    audit("platform-admin", "release.promote",
          target=f"{r['channel']}/{r['os']}/{r['version']}", ip=_client_ip(request))
    return {"ok": True}


@app.get("/v1/admin/releases/{rid}/download", dependencies=[Depends(require_admin)])
def releases_download(rid: int, expires: int = 300):
    if not RELEASES_BUCKET:
        raise HTTPException(500, "bucket de releases não configurado")
    # 1 min … 7 dias (limite do SigV4). 300s = download imediato; 24h = link p/ compartilhar.
    expires = max(60, min(int(expires), 604800))
    with db() as c:
        r = c.execute("SELECT s3_key, filename FROM agent_releases WHERE id=%s", (rid,)).fetchone()
    if not r:
        raise HTTPException(404, "release não encontrada")
    url = _s3().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": RELEASES_BUCKET, "Key": r["s3_key"],
            "ResponseContentDisposition": f'attachment; filename="{r["filename"]}"',
        },
        ExpiresIn=expires,
    )
    return {"url": url, "expires_in": expires}


@app.delete("/v1/admin/releases/{rid}", dependencies=[Depends(require_admin)])
def releases_delete(rid: int, request: Request):
    with db() as c:
        r = c.execute("SELECT s3_key, channel, os, version FROM agent_releases WHERE id=%s", (rid,)).fetchone()
        if not r:
            raise HTTPException(404, "release não encontrada")
        c.execute("DELETE FROM agent_releases WHERE id=%s", (rid,))
    if RELEASES_BUCKET:
        try:
            _s3().delete_object(Bucket=RELEASES_BUCKET, Key=r["s3_key"])
        except Exception as exc:
            print(f"[releases] falha ao remover do S3: {exc}", flush=True)
    audit("platform-admin", "release.delete",
          target=f"{r['channel']}/{r['os']}/{r['version']}", ip=_client_ip(request))
    return {"ok": True}


# ===========================================================================
# Enrollment — onboarding do agente genérico
#   Público:      POST /v1/enroll        (o agente troca o código pela licença)
#   Admin Master: gerar/listar/revogar códigos, ajustar aprovação, fila pendente
# ===========================================================================
class EnrollIn(BaseModel):
    code:     str
    device:   str
    os:       str | None = None
    hostname: str | None = None


def _gen_enroll_code() -> str:
    return "-".join(secrets.token_hex(2).upper() for _ in range(3))  # AAAA-BBBB-CCCC


def _tenant_license_token(c, tenant: str) -> str | None:
    r = c.execute(
        "SELECT token FROM licenses WHERE tenant_id=%s AND revoked=0 ORDER BY issued DESC LIMIT 1",
        (tenant,),
    ).fetchone()
    return r["token"] if r else None


@app.post("/v1/enroll")
def enroll(req: EnrollIn, request: Request):
    """Troca um código de enrollment pela licença do tenant. Sem auth — o código
    É a credencial. Se o tenant exige aprovação, o device entra como 'pendente'."""
    code = (req.code or "").strip().upper()
    if not code:
        raise HTTPException(400, "código ausente")
    if not (req.device or "").strip():
        raise HTTPException(400, "device ausente")
    now = dt.datetime.now(dt.timezone.utc)
    with db() as c:
        cr = c.execute(
            "SELECT tenant_id, active, expires_at FROM enrollment_codes WHERE code=%s", (code,)
        ).fetchone()
        if not cr or not cr["active"]:
            raise HTTPException(403, "código inválido")
        if cr["expires_at"] and cr["expires_at"] < now.isoformat():
            raise HTTPException(403, "código expirado")
        tenant = cr["tenant_id"]

        ap = c.execute("SELECT COALESCE(enroll_approval, TRUE) AS a FROM tenants WHERE id=%s",
                       (tenant,)).fetchone()
        need_approval = bool(ap["a"]) if ap else True

        dev = c.execute(
            "SELECT status FROM enrolled_devices WHERE tenant_id=%s AND device_id=%s",
            (tenant, req.device),
        ).fetchone()
        if dev:
            status = dev["status"]
        else:
            status = "pending" if need_approval else "approved"
            c.execute(
                """INSERT INTO enrolled_devices
                       (tenant_id, device_id, code, status, hostname, os,
                        requested_at, decided_at, decided_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (tenant, req.device, code, status, req.hostname, req.os, now.isoformat(),
                 now.isoformat() if status == "approved" else None,
                 "auto" if status == "approved" else None),
            )

        if status == "approved":
            token = _tenant_license_token(c, tenant)
            if not token:
                raise HTTPException(409, "tenant sem licença ativa")
            return {"status": "approved", "tenant": tenant, "license_key": token}
        if status == "rejected":
            return {"status": "rejected"}
        return {"status": "pending"}


# ----------------------- Admin Master: gestão -----------------------
def _enroll_tid(ctx, tenant):
    return ctx["tenant"] if ctx["role"] != "wp_admin" else tenant


@app.get("/v1/enroll-settings")
def enroll_settings_get(tenant: str, ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    tid = _enroll_tid(ctx, tenant)
    with db() as c:
        r = c.execute("SELECT COALESCE(enroll_approval, TRUE) AS a FROM tenants WHERE id=%s", (tid,)).fetchone()
    return {"approval_required": bool(r["a"]) if r else True}


class EnrollSettingsIn(BaseModel):
    approval_required: bool


@app.put("/v1/enroll-settings")
def enroll_settings_set(body: EnrollSettingsIn, tenant: str, request: Request,
                        ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    tid = _enroll_tid(ctx, tenant)
    with db() as c:
        c.execute("UPDATE tenants SET enroll_approval=%s WHERE id=%s", (body.approval_required, tid))
    audit(ctx.get("email"), "enroll.settings", target=tid,
          detail=f"approval={body.approval_required}", ip=_client_ip(request))
    return {"ok": True, "approval_required": body.approval_required}


@app.get("/v1/enroll-codes")
def enroll_codes_list(tenant: str, ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    tid = _enroll_tid(ctx, tenant)
    with db() as c:
        rows = c.execute(
            "SELECT code, label, active, expires_at, created_at FROM enrollment_codes "
            "WHERE tenant_id=%s ORDER BY created_at DESC", (tid,),
        ).fetchall()
    return {"codes": [dict(r) for r in rows]}


class EnrollCodeIn(BaseModel):
    label:      str | None = None
    days_valid: int | None = None   # null = permanente


@app.post("/v1/enroll-codes")
def enroll_codes_create(body: EnrollCodeIn, tenant: str, request: Request,
                        ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    tid = _enroll_tid(ctx, tenant)
    now = dt.datetime.now(dt.timezone.utc)
    expires = None
    if body.days_valid and body.days_valid > 0:
        expires = (now + dt.timedelta(days=int(body.days_valid))).isoformat()
    code = _gen_enroll_code()
    with db() as c:
        c.execute(
            "INSERT INTO enrollment_codes (code, tenant_id, label, active, expires_at, created_by, created_at) "
            "VALUES (%s,%s,%s,TRUE,%s,%s,%s)",
            (code, tid, (body.label or None), expires, ctx.get("email"), now.isoformat()),
        )
    audit(ctx.get("email"), "enroll.code.create", target=tid, detail=code, ip=_client_ip(request))
    return {"ok": True, "code": code, "expires_at": expires}


@app.delete("/v1/enroll-codes/{code}")
def enroll_codes_revoke(code: str, tenant: str, request: Request,
                        ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    tid = _enroll_tid(ctx, tenant)
    with db() as c:
        c.execute("UPDATE enrollment_codes SET active=FALSE WHERE code=%s AND tenant_id=%s",
                  (code.upper(), tid))
    audit(ctx.get("email"), "enroll.code.revoke", target=tid, detail=code, ip=_client_ip(request))
    return {"ok": True}


@app.get("/v1/enroll-devices")
def enroll_devices_list(tenant: str, ctx: Annotated[dict, Depends(require_role("admin_master"))],
                        status: str | None = None):
    tid = _enroll_tid(ctx, tenant)
    where, args = ["tenant_id=%s"], [tid]
    if status:
        where.append("status=%s"); args.append(status)
    with db() as c:
        rows = c.execute(
            f"SELECT id, device_id, code, status, hostname, os, requested_at, decided_at, decided_by "
            f"FROM enrolled_devices WHERE {' AND '.join(where)} ORDER BY requested_at DESC LIMIT 500",
            tuple(args),
        ).fetchall()
    return {"devices": [dict(r) for r in rows]}


class EnrollDecisionIn(BaseModel):
    action: str   # 'approve' | 'reject'


@app.post("/v1/enroll-devices/{did}/decide")
def enroll_device_decide(did: int, body: EnrollDecisionIn, tenant: str, request: Request,
                         ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    tid = _enroll_tid(ctx, tenant)
    if body.action not in ("approve", "reject"):
        raise HTTPException(400, "ação inválida")
    new_status = "approved" if body.action == "approve" else "rejected"
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    with db() as c:
        r = c.execute("SELECT device_id FROM enrolled_devices WHERE id=%s AND tenant_id=%s",
                      (did, tid)).fetchone()
        if not r:
            raise HTTPException(404, "device não encontrado")
        c.execute(
            "UPDATE enrolled_devices SET status=%s, decided_at=%s, decided_by=%s WHERE id=%s",
            (new_status, now, ctx.get("email"), did),
        )
    audit(ctx.get("email"), f"enroll.device.{body.action}", target=tid,
          detail=r["device_id"], ip=_client_ip(request))
    return {"ok": True, "status": new_status}


@app.delete("/v1/enroll-devices/{did}")
def enroll_device_delete(did: int, tenant: str, request: Request,
                         ctx: Annotated[dict, Depends(require_role("admin_master"))]):
    """Remove o registro de enrollment do device. Se ele re-enrollar depois,
    passa pela fila de novo. Não revoga licença já entregue nem para coleta em
    andamento (para isso, use 'Agentes bloqueados')."""
    tid = _enroll_tid(ctx, tenant)
    with db() as c:
        r = c.execute("SELECT device_id FROM enrolled_devices WHERE id=%s AND tenant_id=%s",
                      (did, tid)).fetchone()
        if not r:
            raise HTTPException(404, "device não encontrado")
        c.execute("DELETE FROM enrolled_devices WHERE id=%s AND tenant_id=%s", (did, tid))
    audit(ctx.get("email"), "enroll.device.delete", target=tid,
          detail=r["device_id"], ip=_client_ip(request))
    return {"ok": True}


# ===========================================================================
# Diagnóstico — dump dos eventos crus de um colaborador (Platform Admin)
# Útil p/ investigar dados estranhos (ex.: AFK > tempo decorrido).
# ===========================================================================
@app.get("/v1/admin/events-dump", dependencies=[Depends(require_admin)])
def admin_events_dump(tenant: str, match: str,
                      since: str | None = None, until: str | None = None,
                      bucket: str | None = None, limit: int = 200000):
    now = dt.datetime.now(dt.timezone.utc)
    if since is None:
        since = (now - dt.timedelta(days=3)).isoformat()
    if until is None:
        until = now.isoformat()
    m = f"%{(match or '').strip()}%"
    with db() as c:
        urows = c.execute(
            """SELECT DISTINCT u.user_id, u.display_name
               FROM users u
               LEFT JOIN devices d ON d.tenant_id=u.tenant_id AND d.user_id=u.user_id
               WHERE u.tenant_id=%s
                 AND (u.user_id ILIKE %s OR u.display_name ILIKE %s OR d.hostname ILIKE %s)""",
            (tenant, m, m, m),
        ).fetchall()
        uids = [r["user_id"] for r in urows]
        events = []
        if uids:
            where = ["tenant_id=%s", "user_id = ANY(%s)", "ts>=%s", "ts<=%s"]
            args: list = [tenant, uids, since, until]
            if bucket:
                where.append("bucket=%s"); args.append(bucket)
            events = c.execute(
                f"SELECT user_id, device_id, bucket, ts, duration_sec, data_json "
                f"FROM events WHERE {' AND '.join(where)} ORDER BY ts LIMIT %s",
                tuple(args + [int(limit)]),
            ).fetchall()
    return {
        "tenant": tenant, "match": match, "since": since, "until": until,
        "users":  [dict(r) for r in urows],
        "count":  len(events),
        "events": [dict(r) for r in events],
    }


@app.get("/v1/admin/dup-report", dependencies=[Depends(require_admin)])
def admin_dup_report(tenant: str, since: str | None = None, until: str | None = None):
    """Relatório por usuário (uma linha cada): total de eventos, estimativa de
    DUPLICATAS (total - distintos por ts|bucket|duração) e o maior AFK. Serve p/
    ver se a duplicação/AFK gigante é da frota ou de uma máquina só. Período
    padrão = hoje (UTC) — passe since/until p/ ampliar a janela."""
    now = dt.datetime.now(dt.timezone.utc)
    if since is None:
        since = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    if until is None:
        until = now.isoformat()
    with db() as c:
        rows = c.execute(
            """SELECT u.user_id,
                      COALESCE(u.display_name, u.user_id) AS name,
                      COUNT(*)                                              AS total,
                      COUNT(*) FILTER (WHERE e.bucket='afk')               AS afk_events,
                      COUNT(*) - COUNT(DISTINCT
                          (e.ts || '|' || e.bucket || '|' || e.duration_sec::text)) AS dups,
                      COALESCE(MAX(e.duration_sec) FILTER (WHERE e.bucket='afk'), 0) AS max_afk_sec,
                      COUNT(*) FILTER (WHERE e.bucket='afk' AND e.duration_sec > 3600) AS afk_gt_1h
               FROM users u
               JOIN events e ON e.tenant_id=u.tenant_id AND e.user_id=u.user_id
                    AND e.ts>=%s AND e.ts<=%s
               WHERE u.tenant_id=%s
               GROUP BY u.user_id, u.display_name
               ORDER BY dups DESC""",
            (since, until, tenant),
        ).fetchall()
    out = []
    for r in rows:
        total = r["total"] or 0
        dups  = r["dups"] or 0
        out.append({
            "user_id":   r["user_id"],
            "name":      r["name"],
            "total":     total,
            "afk_events": r["afk_events"],
            "dups":      dups,
            "dup_pct":   round(dups / total * 100, 1) if total else 0,
            "max_afk_h": round((r["max_afk_sec"] or 0) / 3600, 2),
            "afk_gt_1h": r["afk_gt_1h"],
        })
    return {"tenant": tenant, "since": since, "until": until, "users": out}


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
