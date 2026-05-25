"""
WorkPulse — Migração SQLite → PostgreSQL
=========================================

Lê todos os dados do banco SQLite (em /data/workpulse.db por padrão)
e os importa para o PostgreSQL. Roda uma vez e é idempotente para a
maioria das tabelas (usa ON CONFLICT DO NOTHING).

Uso dentro de um container ECS (via migrar-dados.sh):
    python migration.py

Variáveis de ambiente necessárias:
    SQLITE_PATH  Caminho do arquivo SQLite (default: /data/workpulse.db)
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD  — conexão PostgreSQL
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

import psycopg2
import psycopg2.extras

SQLITE_PATH = os.getenv("SQLITE_PATH", "/data/workpulse.db")
PG_HOST     = os.getenv("DB_HOST", "localhost")
PG_PORT     = int(os.getenv("DB_PORT", "5432"))
PG_NAME     = os.getenv("DB_NAME", "workpulse")
PG_USER     = os.getenv("DB_USER", "workpulse")
PG_PASSWORD = os.getenv("DB_PASSWORD", "")

BATCH_SIZE = 500


def log(msg: str) -> None:
    print(f"[migration] {msg}", flush=True)


def migrate() -> None:
    if not os.path.exists(SQLITE_PATH):
        log(f"ERRO: arquivo SQLite não encontrado em {SQLITE_PATH}")
        sys.exit(1)

    log(f"Abrindo SQLite: {SQLITE_PATH}")
    sq = sqlite3.connect(SQLITE_PATH)
    sq.row_factory = sqlite3.Row

    log(f"Conectando ao PostgreSQL: {PG_HOST}:{PG_PORT}/{PG_NAME}")
    pg = psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_NAME,
        user=PG_USER, password=PG_PASSWORD,
        connect_timeout=15,
    )
    pg.autocommit = True
    cur = pg.cursor()

    # ------------------------------------------------------------------
    # tenants
    # ------------------------------------------------------------------
    log("Migrando tenants...")
    rows = sq.execute("SELECT * FROM tenants").fetchall()
    count = 0
    for r in rows:
        cur.execute(
            """INSERT INTO tenants (id, name, plan, status, created_at, dash_token, retention_days)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO NOTHING""",
            (r["id"], r["name"], r["plan"], r["status"], r["created_at"],
             r["dash_token"] if "dash_token" in r.keys() else None,
             r["retention_days"] if "retention_days" in r.keys() else None),
        )
        count += 1
    log(f"  → {count} tenants migrados")

    # ------------------------------------------------------------------
    # licenses
    # ------------------------------------------------------------------
    log("Migrando licenses...")
    rows = sq.execute("SELECT * FROM licenses").fetchall()
    count = 0
    for r in rows:
        cur.execute(
            """INSERT INTO licenses (lic_id, tenant_id, plan, seats, issued, expires, revoked, revoked_at, token)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (lic_id) DO NOTHING""",
            (r["lic_id"], r["tenant_id"], r["plan"], r["seats"],
             r["issued"], r["expires"], r["revoked"], r["revoked_at"], r["token"]),
        )
        count += 1
    log(f"  → {count} licenses migradas")

    # ------------------------------------------------------------------
    # users
    # ------------------------------------------------------------------
    log("Migrando users...")
    rows = sq.execute("SELECT * FROM users").fetchall()
    keys = [d[0] for d in sq.execute("PRAGMA table_info(users)").fetchall()]
    count = 0
    for r in rows:
        login_user = r["login_user"] if "login_user" in keys else None
        cur.execute(
            """INSERT INTO users
                   (user_id, tenant_id, display_name, email, department, role, login_user, first_seen, last_seen)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (tenant_id, user_id) DO NOTHING""",
            (r["user_id"], r["tenant_id"], r["display_name"], r["email"],
             r["department"], r["role"], login_user, r["first_seen"], r["last_seen"]),
        )
        count += 1
    log(f"  → {count} users migrados")

    # ------------------------------------------------------------------
    # devices
    # ------------------------------------------------------------------
    log("Migrando devices...")
    rows = sq.execute("SELECT * FROM devices").fetchall()
    count = 0
    for r in rows:
        cur.execute(
            """INSERT INTO devices
                   (device_id, user_id, tenant_id, hostname, os, os_version, agent_version, last_seen)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (device_id) DO NOTHING""",
            (r["device_id"], r["user_id"], r["tenant_id"], r["hostname"],
             r["os"], r["os_version"], r["agent_version"], r["last_seen"]),
        )
        count += 1
    log(f"  → {count} devices migrados")

    # ------------------------------------------------------------------
    # events — em lotes (potencialmente a maior tabela)
    # ------------------------------------------------------------------
    log("Migrando events (em lotes de 500)...")
    total_events = sq.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    log(f"  Total de eventos: {total_events}")

    migrated = 0
    offset   = 0
    while True:
        batch = sq.execute(
            "SELECT * FROM events ORDER BY id LIMIT ? OFFSET ?",
            (BATCH_SIZE, offset),
        ).fetchall()
        if not batch:
            break

        params = []
        for r in batch:
            # data_json pode ser string (SQLite) → converter para dict para o JSONB do PG
            dj = r["data_json"]
            if isinstance(dj, str):
                try:
                    dj = json.loads(dj)
                except json.JSONDecodeError:
                    dj = {}
            params.append((
                r["tenant_id"], r["user_id"], r["device_id"],
                r["bucket"], r["ts"], r["duration_sec"],
                psycopg2.extras.Json(dj),
            ))

        psycopg2.extras.execute_batch(
            cur,
            """INSERT INTO events
                   (tenant_id, user_id, device_id, bucket, ts, duration_sec, data_json)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            params,
            page_size=BATCH_SIZE,
        )
        migrated += len(batch)
        offset   += BATCH_SIZE
        log(f"  → {migrated}/{total_events} eventos migrados...")

    log(f"  → {migrated} eventos migrados no total")

    # ------------------------------------------------------------------
    # daily_summaries
    # ------------------------------------------------------------------
    log("Migrando daily_summaries...")
    rows = sq.execute("SELECT * FROM daily_summaries").fetchall()
    count = 0
    for r in rows:
        cur.execute(
            """INSERT INTO daily_summaries (tenant_id, user_id, day, active_sec, afk_sec)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (tenant_id, user_id, day) DO NOTHING""",
            (r["tenant_id"], r["user_id"], r["day"], r["active_sec"], r["afk_sec"]),
        )
        count += 1
    log(f"  → {count} daily_summaries migrados")

    # ------------------------------------------------------------------
    # productivity_rules
    # ------------------------------------------------------------------
    log("Migrando productivity_rules...")
    try:
        rows = sq.execute("SELECT * FROM productivity_rules").fetchall()
        count = 0
        for r in rows:
            cur.execute(
                """INSERT INTO productivity_rules
                       (tenant_id, label, pattern, match_field, match_type, category, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (r["tenant_id"], r["label"], r["pattern"],
                 r["match_field"], r["match_type"], r["category"], r["created_at"]),
            )
            count += 1
        log(f"  → {count} productivity_rules migradas")
    except sqlite3.OperationalError:
        log("  → tabela productivity_rules não encontrada no SQLite, pulando")

    # ------------------------------------------------------------------
    # tenant_departments
    # ------------------------------------------------------------------
    log("Migrando tenant_departments...")
    try:
        rows = sq.execute("SELECT * FROM tenant_departments").fetchall()
        count = 0
        for r in rows:
            cur.execute(
                """INSERT INTO tenant_departments (tenant_id, name, created_at)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (tenant_id, name) DO NOTHING""",
                (r["tenant_id"], r["name"], r["created_at"]),
            )
            count += 1
        log(f"  → {count} tenant_departments migrados")
    except sqlite3.OperationalError:
        log("  → tabela tenant_departments não encontrada no SQLite, pulando")

    # ------------------------------------------------------------------
    # audit_log
    # ------------------------------------------------------------------
    log("Migrando audit_log...")
    rows = sq.execute("SELECT * FROM audit_log").fetchall()
    count = 0
    for r in rows:
        cur.execute(
            """INSERT INTO audit_log (ts, actor, action, target, detail, ip)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (r["ts"], r["actor"], r["action"], r["target"], r["detail"], r["ip"]),
        )
        count += 1
    log(f"  → {count} entradas de audit_log migradas")

    sq.close()
    cur.close()
    pg.close()

    log("Migração concluída com sucesso!")


if __name__ == "__main__":
    migrate()
