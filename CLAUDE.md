# CLAUDE.md — WorkPulse

Guidance for AI agents (Claude Code / Cowork) working on WorkPulse.
**Read this fully before changing anything.** Last updated: 2026-05-26.

---

## ⚠️ CRITICAL #1 — Silent file-edit truncation

Large files in this project get **truncated silently** by the Edit/Write tools:
the file looks saved but is cut off partway through. Real incidents in this repo:

- `server/main.py` (~1700 lines) — `serve()` cut mid-word
- `infra/main.tf`, `infra/variables.tf`, `infra/outputs.tf` — cut mid-file
- `scripts/2-deploy.sh` — lost the last 60 lines (FASE B/C)
- `server/requirements.txt` — lost `psycopg2-binary` + `boto3` (server then crash-looped)
- `../workpulse-agent/src/license.rs` — cut mid-function, lost `spawn_heartbeat`
- `../workpulse-agent/windows/installer.wxs` — stray trailing NUL bytes

**Rule:** for any file over ~150 lines, do NOT use the Edit tool with multi-line
replacements. Edit in the shell instead:

```bash
head -n <last_good_line> file > /tmp/work
cat >> /tmp/work <<'EOF'
...new content...
EOF
cp /tmp/work file
```

For surgical changes on big files, prefer a Python string-replace in the shell
with an assertion that the old block was found exactly once.

**After EVERY edit, verify the file is intact:**
- `.py`  → `python3 -m py_compile file`
- `.rs`  → a real delimiter-balance check (lexer that skips strings/char-literals/
  comments — a naive regex gives false positives on `{e}` and `'.'`)
- `.tf`  → braces/brackets balance, last non-blank line is `}`
- `.xml/.wxs` → parse with an XML parser; assert 0 NUL bytes
- always: `tail -c 80 file` must end the way the file should end

---

## ⚠️ CRITICAL #2 — Frontend ↔ Backend response contracts

The dashboards are plain HTML+JS that read **exact JSON shapes** from the API.
The PostgreSQL rewrite changed several response shapes and silently broke the
panels (e.g. `admin.html` does `stats.counts.tenants`; the backend was returning
a flat `stats.tenants` → "Cannot read properties of undefined").

**Rule:** when you change ANY endpoint's response shape, grep the consuming HTML
for every field it reads BEFORE you ship:
`site/adm/admin.html` and `site/dashboard/workforce.html`.

Contracts that MUST hold (verified working as of 2026-05-25):

| Endpoint | Shape the frontend requires |
|---|---|
| `GET /v1/admin/stats` | `{version, server_time, db_size_bytes, counts:{tenants,users,devices,total_events,active_licenses,revoked_licenses}, events_last_24h, events_last_7d, ingest_per_hour:[{hour,events}], os_distribution:[{os,n}], plan_distribution:[{plan,n}]}` |
| `GET /v1/admin/tenants` | items: `id,name,plan,status,users,devices,active_licenses,created_at,last_activity,retention_days` |
| `GET /v1/admin/licenses` | items: `lic_id,tenant_id,tenant_name,plan,seats,issued,expires,revoked,days_remaining,expired` |
| `GET /v1/users/{id}/summary` | must include `by_day:[{day,active,afk}]` |
| `GET /v1/workforce/overview` | `totals:{total_users,online_now,active_seconds,afk_seconds,today_active_seconds}, daily:[{day,active,afk}], hourly:[{hour,seconds}], by_department:[{dept,users,active}], alerts:{overwork,underwork}` |

There is a server-logic simulation harness pattern used to validate these — see
"Validate / Test" below.

---

## Project Overview

WorkPulse is a SaaS employee-activity monitoring platform.

- A **Rust agent** runs on each monitored Windows/Linux workstation, batches
  window-focus/AFK events, and POSTs them to the backend.
- A **FastAPI backend** (single file, `server/main.py`) ingests events, validates
  Ed25519 licenses, and serves the reporting API.
- **Dashboards** (plain HTML + Chart.js, no build step) read from the API.
- **PostgreSQL 15 on RDS** is the database (v2.0.0 replaced SQLite-on-EFS for
  concurrency + performance).

**Live URLs:**
- API: `https://api.workpulse.com.br`
- Site: `https://workpulse.com.br`
- Workforce dashboard: `https://workpulse.com.br/dashboard/workforce.html` (modular, atual)
- Platform admin: `https://workpulse.com.br/adm/admin.html`

---

## Current Production Deployment

WorkPulse v2.0.0 is **LIVE** on AWS.

- AWS account: **881027528914**, region **us-east-1**
- Console: `https://881027528914.signin.aws.amazon.com/console`
- Domain `workpulse.com.br` — Route 53 hosted zone in this account
- Full infra inventory + costs: see `INFRA_AWS_V3.docx` in the WORK PULSE folder
- **Canonical deploy package: `workpulse-deploy-v16-VALIDADO.zip`** (WORK PULSE
  folder). Every older zip (`workpulse-deploy_v1..v13`, `-corrigido`, v14, v15)
  is obsolete or broken — ignore them.

---

## Repository Layout (inside the "WORK PULSE" folder)

```
workpulse-deploy/        <- THIS kit: backend + infra + site + scripts
  server/main.py         FastAPI backend (single file)
  server/Dockerfile      multi-stage image build
  server/requirements.txt
  infra/*.tf             Terraform (all AWS resources)
  scripts/*.sh           bootstrap / deploy / redeploy / destroy
  site/                  static dashboards + marketing pages
workpulse-agent/         <- the Rust agent (separate crate)
  src/*.rs               agent source
  windows/               MSI installer (WiX) + build-msi.ps1
  linux/                 .deb packaging
RUNBOOK_DEPLOY_v14.md    step-by-step CloudShell deploy guide
INFRA_AWS_V3.docx        full AWS inventory + cost estimate
```

---

## Validate / Test

There is **no live test suite** and the sandbox has no AWS/Docker/Postgres/pip.
Validation that has proven reliable:

```bash
# Backend syntax
python3 -m py_compile server/main.py server/migration.py
```

**Server-logic simulation.** The backend can be exercised without FastAPI/psycopg2
by importing `main.py` with lightweight stubs:
- stub `fastapi`/`pydantic`/`uvicorn`/`boto3` (no-op decorators, simple BaseModel)
- stub `psycopg2` as a package whose cursor translates PostgreSQL SQL to SQLite
  (`%s`→`?`, `data_json->>'k'`→`json_extract(...)`, `BIGSERIAL`→`INTEGER ... AUTOINCREMENT`,
  `JSONB`→`TEXT`, `DOUBLE PRECISION`→`REAL`, `pg_database_size(...)`→`0`)
- `cryptography` is real, so Ed25519 sign/verify is tested for real
Then call the endpoint functions directly and assert response shapes against the
frontend contracts above. This caught every real bug in the last recovery.

**Terraform** — `terraform validate` needs the binary (not in sandbox). Static
checks that work: delimiter balance per `.tf` file, every `var.*`/`local.*`/
`data.*`/resource reference resolves, `terraform.tfvars` keys all declared,
every `-target=` in `2-deploy.sh` exists.

Always add a final verification step before declaring work done.

---

## Deploy to Production (AWS CloudShell, us-east-1)

All commands run in **AWS CloudShell**, inside `workpulse-deploy/`.

```bash
bash scripts/1-bootstrap.sh        # S3 state bucket, DynamoDB lock, checks Route53
bash scripts/2-deploy.sh --novo    # fresh install (no SQLite data to migrate)
bash scripts/3-publicar-site.sh    # uploads site/ to S3, invalidates CloudFront

bash scripts/redeploy.sh           # after changing server/main.py: rebuilds image,
                                   # rolling zero-downtime deploy (~5 min)
bash scripts/destruir.sh           # tears everything down (asks: type DESTRUIR)
```

`2-deploy.sh` is 3-phase: FASE A creates RDS + builds the image; FASE B migrates
SQLite→PG only if an EFS is detected; FASE C does the full `terraform apply`.
`--novo` skips FASE B.

**Terraform backend init** (needed to run `terraform` directly in `infra/`):
```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
terraform init -reconfigure \
  -backend-config="bucket=workpulse-tfstate-${ACCOUNT_ID}" \
  -backend-config="key=prod/terraform.tfstate" \
  -backend-config="region=us-east-1" \
  -backend-config="dynamodb_table=workpulse-tf-locks" \
  -backend-config="encrypt=true"
```

After `server/main.py` changes: `redeploy.sh`. After `site/` changes:
`3-publicar-site.sh`. Site is unaffected by server changes and vice-versa.

---

## Backend Architecture (`server/main.py`)

Single-file FastAPI app, ~1700 lines, no routers — endpoints defined inline.
`APP_VERSION = "2.0.0"`.

### Authentication (two levels)

| Decorator | Who | How |
|---|---|---|
| `Depends(require_admin)` | Platform admin only | `X-Admin-Token: <WORKPULSE_ADMIN_TOKEN>` |
| `Depends(require_tenant_scope)` | Admin OR the tenant's own dash_token | `X-Admin-Token: <either>` + `?tenant=<id>` |
| Bearer token (inline) | Rust agents | `Authorization: Bearer <license_token>` |

`require_tenant_scope` accepts the master admin token **or** the per-tenant
`dash_token` (in `tenants.dash_token`). A 403 "token invalido para este tenant"
just means stale credentials — after a fresh install the DB is empty, so old
tenant IDs/tokens no longer exist; issue a new license and use the new ones.

### Database layer (PostgreSQL / psycopg2)

`db()` context manager yields a `_PGConn` wrapper over a `ThreadedConnectionPool`
(minconn=2, maxconn=10). The wrapper mimics the sqlite3 cursor API:
`with db() as c: rows = c.execute("SELECT ...", (param,)).fetchall()`.

SQL is PostgreSQL: `%s` placeholders, `data_json->>'app'` JSONB operator,
`INSERT ... ON CONFLICT ... DO NOTHING/UPDATE`, `RETURNING id`, `BIGSERIAL`.
Schema lives in `_SCHEMA_STMTS` (run by `init_db()`); JSONB expression indexes
`idx_events_app`/`idx_events_title` are critical for performance.

### In-memory TTL cache

`_CACHE` dict, `_CACHE_TTL = 90s`, keys namespaced `"{tenant}:{endpoint}:{params}"`.
**Any endpoint that writes data must call `_cache_bust(tenant)`** — this includes
ingest, PATCH user, create/delete productivity rule, delete department, purge.

### License signing (Ed25519)

`load_or_create_keypair()` loads the private key, priority:
1. Secrets Manager (`workpulse-prod/license-key-b64`) — source of truth
2. env var `WORKPULSE_LICENSE_PRIVKEY_B64`
3. generates a new key on first boot and stores it in Secrets Manager

Public key is served at `GET /v1/license/pubkey`; agents verify licenses locally.

### Background worker

`_retention_worker()` (asyncio task, started in `lifespan`) archives events older
than `tenants.retention_days` (default 90) into `daily_summaries`, then deletes them.

---

## Database Schema

| Table | Purpose |
|---|---|
| `tenants` | One row per client company. `dash_token`, `retention_days` |
| `licenses` | Ed25519-signed tokens. `revoked` flag |
| `users` | `(tenant_id, user_id)` — `user_id` = hostname. `display_name`, `login_user`, `department`, `role` |
| `devices` | One row per device (hostname + OS metadata) |
| `events` | Raw activity. `bucket` ∈ {window, afk}. `data_json JSONB` = `{app,title}` or `{status}` |
| `productivity_rules` | Per-tenant pattern rules; evaluated in `created_at` order, first match wins |
| `tenant_departments` | Department names, UNIQUE(tenant_id, name) |
| `daily_summaries` | Archived aggregates past the retention cutoff |
| `audit_log` | Admin action trail |

Key indexes: `idx_events_tenant_bucket_ts`, JSONB expression indexes
`idx_events_app`/`idx_events_title`.

---

## API Reference (all paths under `/v1`)

**Agent (Bearer auth):** `POST /ingest`, `POST /license/heartbeat`
**Tenant-scoped (`?tenant=`):** `GET /users`, `GET /users/{id}/summary`,
`PATCH /users/{id}`, `GET /team/summary`, `GET /events`,
`GET /productivity/events_summary`, `GET /workforce/overview`,
`GET /workforce/timeseries`, `GET|POST /productivity/rules`,
`DELETE /productivity/rules/{id}`, `GET /productivity/internet`,
`GET /productivity/apps`, `GET|POST /departments`, `DELETE /departments/{name}`
**Admin-only:** `POST /license/issue`, `POST /license/revoke`,
`POST /admin/tenant-token`, `GET /admin/tenants`, `GET /admin/licenses`,
`GET /admin/stats`, `GET /admin/audit`, `GET /admin/data-summary`,
`POST /admin/purge-data`, `PUT /admin/tenants/{id}/retention`
**Public:** `GET /healthz`, `GET /license/pubkey`

When adding an endpoint: add the Pydantic model, the route with the right
`Depends()`, use `%s` + `data_json->>'field'`, call `_cache_bust` on writes,
call `audit(...)` on sensitive ops, place before the `# Entry point` section,
then `py_compile`.

---

## Frontend Architecture

### Client Dashboard (`site/dashboard/`)

**`workforce.html`** — versão modular atual. HTML lean carrega módulos via
`<script defer>`. Acesse em `https://workpulse.com.br/dashboard/workforce.html`.

**`workforce-v1.html`** — versão original monolítica (~2150 linhas, tudo inline).
Mantida como contingência — não modificar.

#### Estrutura de arquivos

```
site/dashboard/
  workforce.html          HTML principal (lean, sem CSS/JS inline)
  workforce-v1.html       Versão legada (contingência — não modificar)
  css/
    workforce.css         Todos os estilos + tema claro/escuro
  js/
    wp-core.js            State, utils, api(), apiFetch(), chartTheme(), CAT_*
    wp-overview.js        Aba Visão Geral (loadOverview, drawDailyChart, etc.)
    wp-people.js          Aba Pessoas (loadPeople, renderPeopleTable, drillUser)
    wp-departments.js     Aba Departamentos (loadDepartments)
    wp-analytics.js       Abas Alertas + Tendências (loadAlerts, loadTrends)
    wp-events.js          Aba Eventos (loadEvents, drawEvDailyChart)
    wp-internet.js        Aba Internet (loadInternet, renderSitesTable)
    wp-settings.js        Aba Configurações (regras, apps, depts, colaboradores)
    wp-nav.js             Navegação por tabs + dropdowns (switchTab, TABS)
    wp-auth.js            Login com validação API, logout, tema, bootstrap IIFE
```

#### Navegação (topbar)

```
[Visão Geral]  [Coletas ▾]  [Análises ▾]         [⚙ ▾]
               Pessoas       Alertas          Configurações
               Departamentos Tendências       Sair
               Eventos
               Internet
```

#### Features adicionadas em 2026-05-26

- **Dropdowns de nav**: Coletas (Pessoas/Depts/Eventos/Internet) e Análises (Alertas/Tendências).
- **Actions como dropdown**: botão ⚙ ▾ abre menu com Configurações e Sair.
- **Tema claro/escuro**: toggle em Configurações → Aparência. Salvo em `localStorage('wp_theme')`. Default: `light` (claro).
  `body.theme-light` substitui todas as CSS vars. Charts destruídos e recriados ao trocar.
  `chartTheme()` em `wp-core.js` retorna cores corretas para o tema ativo.
- **Validação de login**: `doLogin()` faz `fetch` real para `/v1/workforce/overview?days=1`.
  HTTP 401/403 → mostra `.login-error` dentro do modal, não entra na página.
  Timeout de 12s (AbortController). Não valida na restauração de sessão do bootstrap.
- **Modularidade**: cada aba é um arquivo JS independente (~50–150 linhas).
  Estado global em `state` (definido em `wp-core.js`, escopo global sem `window.WP`).
  Scripts carregados com `defer` — DOM pronto, ordem garantida.

#### Contratos de resposta relevantes (herdados)

Os mesmos do `workforce-v1.html` (legado) — ver tabela na seção CRITICAL #2 acima.

**`admin.html`** — platform admin (~1120 linhas, self-contained).
- tabs: `overview|tenants|licenses|system|audit`

Both store `{url, token, tenant}` in `localStorage`.
All API calls go through `api()` / `apiFetch()` — never raw `fetch()`.
Flat paths (`/workforce.html`, `/admin.html`) are redirect stubs to the
`/dashboard/` and `/adm/` versions.

After editing site files run `3-publicar-site.sh`. **`s3 sync --delete` publishes
everything in `site/` — keep `.bak`/temp files out of that folder.**

---

## The Rust Agent (`../workpulse-agent`)

Separate crate. Built per-client with an embedded license.

- **Build (Windows):** `windows/build-msi.ps1 -LicenseKey "<token>" -ClientName "X"`
  → runs `cargo build --release` then WiX. Output in `dist/`.
- **Build (Linux):** `LICENSE_KEY="<token>" ./linux/build-deb.sh`
  → runs `cargo build --release` then `dpkg-deb`. Output in `dist/`.
- **Config:** `agent.conf` (TOML). Only `license_key` is required; `server_url`
  defaults to `https://api.workpulse.com.br`; tenant comes from inside the license.
  Installed at `%ProgramData%\WorkPulse\agent.conf` (Windows) /
  `/etc/workpulse/agent.conf` (Linux).
- **License flow (`src/license.rs`):** the agent verifies the Ed25519 signature
  locally. It fetches the server public key from `/v1/license/pubkey` and caches
  it at `%APPDATA%\WorkPulse\Agent\data\server_pubkey.txt`. As of 2026-05-25 the
  agent **self-heals**: if verification fails with the cached key, it drops the
  cache and refetches the current key.
-