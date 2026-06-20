# CLAUDE.md — WorkPulse

Guidance for AI agents (Claude Code / Cowork) working on WorkPulse.
**Read this fully before changing anything.** Last updated: 2026-06-18.

> **Para o estado atual (auth e-mail/senha, papéis, Localização, Administrativo,
> Financeiro, SMTP por tenant, motor de Alertas, etc.), leia primeiro a seção
> "## Estado atual (2026-06-14)" no FINAL deste arquivo.** As seções estruturais
> abaixo (Auth, Schema, API, Workers) foram atualizadas, mas o changelog detalhado
> de cada época fica no rodapé.

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
- `site/dashboard/workforce.html` (2026-05-27) — Edit/Write truncated mid-word
  while adding the Jornada tab; recovery had to restore from the v20 zip
- `site/dashboard/js/wp-nav.js` (2026-05-27) — Write truncated to original byte
  count; rewrote via `cat > … <<'EOF'` heredoc
- `../workpulse-agent/src/{tracker,config,main,sync}.rs`,
  `../workpulse-agent/windows/installer.wxs`,
  `../workpulse-agent/Cargo.toml`,
  `../workpulse-agent/linux/build-deb.sh`,
  `../workpulse-agent/LEIA-PRIMEIRO.md`,
  `CLAUDE.md` itself (2026-05-27) — eight files in the agent kit + this very
  CLAUDE.md found truncated mid-content during the AFK fix. The compiled
  `dist/WorkPulseAgent-ASAPTECH.msi` (2026-05-26) predates the corruption, so
  the deployed binary is intact, but `cargo build` would fail on the current
  source tree. All recovered via `cat > … <<'EOF'` heredoc using cached Read
  content as the source of truth; backed up beforehand to
  `../workpulse-agent-backup-20260527-145705.zip`

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

## ⚠️ CRITICAL #3 — PostgreSQL GROUP BY + correlated subqueries

When adding a correlated subquery to a `SELECT` that already has `GROUP BY`,
**every outer-table column referenced inside the subquery must also appear in
the `GROUP BY` clause** — even if it is a constant due to a `WHERE` filter.

PostgreSQL raises at runtime:
```
ERROR: column "u.tenant_id" must appear in the GROUP BY clause
       or be used in an aggregate function
```

The browser sees a CORS error (no `Access-Control-Allow-Origin` header) instead
of the real 500, making diagnosis harder.

**Real incident (2026-05-27):** adding `agent_version` to `/v1/team/summary`
via a correlated subquery that referenced `u.tenant_id` — which was in `WHERE`
but not in `GROUP BY` — took the endpoint down in production immediately after
deploy. Fix: add the missing column to `GROUP BY`:

```sql
-- ❌ QUEBRA: subquery usa u.tenant_id mas ele não está no GROUP BY
GROUP BY u.user_id, u.display_name, u.email, u.department, u.last_seen

-- ✅ CORRETO: adicionar u.tenant_id ao GROUP BY
GROUP BY u.user_id, u.display_name, u.email, u.department, u.last_seen, u.tenant_id
```

**Rule:** before shipping any SQL change that adds a subquery to a GROUP BY
query, list every outer column the subquery references and confirm all appear
in the GROUP BY list.

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
| `GET /v1/team/summary` | items: `user_id,display_name,email,department,last_seen,active,afk,agent_version` |
| `GET /v1/users/{id}/summary` | must include `by_day:[{day,active,afk}]` |
| `GET /v1/workforce/overview` | `totals:{total_users,online_now,active_seconds,afk_seconds,today_active_seconds}, daily:[{day,active,afk}], hourly:[{hour,seconds}], by_department:[{dept,users,active}], alerts:{overwork,underwork}` |

There is a server-logic simulation harness pattern used to validate these — see
"Validate / Test" below.

---

## ⚠️ CRITICAL #4 — `<base href>` e Clean URLs

`workforce.html` tem `<base href="/dashboard/">` no `<head>`. Isso é necessário porque
a clean URL `/dashboard` (sem barra) faz o browser resolver paths relativos como `/css/...`
em vez de `/dashboard/css/...`.

**Regra:** todo path relativo em `workforce.html` (CSS, JS, imagens) deve ser relativo
à pasta `dashboard/` — ex.: `css/workforce.css`, `js/wp-core.js`, `../assets/logo.jpeg`.
Se um `<script src="...">` for adicionado sem fechar a tag `</script>`, **o browser
interpreta o resto da página como conteúdo do script e nenhuma função JS é definida**
(real incident 2026-05-29). Sempre fechar: `<script src="..." defer></script>`.

**Clean URLs** são gerenciadas pela CloudFront Function em `scripts/clean-urls.js`:
- `/dashboard` → `/dashboard/workforce.html`
- `/adm`       → `/adm/admin.html`
- `/docs`      → `/docs/developers.html`
- Paths sem extensão → tenta `.html` (fallback genérico)
- Assets com extensão `.js`, `.css` etc. passam sem modificação

Para atualizar a função: `bash scripts/4-setup-clean-urls.sh`.

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
- Workforce dashboard: `https://workpulse.com.br/dashboard` ← **clean URL ativo** (CloudFront Function)
  (`/dashboard/workforce.html` ainda funciona para retrocompatibilidade)
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

Single-file FastAPI app, **~4.500 lines** (cresceu muito desde a 1ª versão), no
routers — endpoints defined inline. `APP_VERSION = "2.0.0"`. **A auth abaixo (token)
foi substituída por e-mail/senha + sessão + papéis — ver "Estado atual" no rodapé.**

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

`_CACHE` dict, `_CACHE_TTL` (env `WORKPULSE_CACHE_TTL`, default **300s**), keys
namespaced `"{tenant}:{endpoint}:{params}"`. **Any endpoint that writes data must
call `_cache_bust(tenant)`** — ingest, PATCH user, create/delete rule, delete dept, purge.

### Rollup / agregados (perf dos painéis) — 2026-06-16

Os painéis NÃO varrem mais `events` cru a cada carga. Tabelas pré-agregadas:
- **`agg_user_hour`** (tenant, user, hora-UTC, active_sec, afk_sec) → alimenta
  `overview` e o ativo/AFK por pessoa do `team_summary`.
- **`agg_user_day`** (tenant, user, dia-LOCAL, focus/afk_in/afk_after/start/end_sec)
  → alimenta o `afk_in` (AFK na jornada); reaproveitável p/ Jornada/engajamento.
- **`agg_app_day`** (tenant, dia-LOCAL, app, active_sec) → alimenta o "Top Aplicações"
  (`_top_apps`). Sem user_id: quando há escopo de líder, cai no fallback de events.
- **`rollup_state`** (tenant, oldest_day, done, **ver**) → cursor do backfill. `ver`
  vs `ROLLUP_SCHEMA_VER`: ao subir um agregado novo, o worker reabre o backfill UMA
  vez por tenant p/ repreencher os dias antigos (bump `ROLLUP_SCHEMA_VER`).

`_rollup_worker()` (task no `lifespan`, a cada `WORKPULSE_ROLLUP_SEC`=300s): refaz
ontem+hoje sempre (upsert via `_rollup_local_day` = `_rollup_hours`+`_rollup_days`+
`_rollup_apps`) e faz backfill p/ trás `WORKPULSE_ROLLUP_BACKFILL_DAYS`=7 dias/ciclo
até a retenção. `_rollup_hours`/`_rollup_apps` são seguros p/ qualquer faixa;
`_rollup_days` exige dias locais inteiros. Helpers de leitura `_hour_rows` /
`_afk_in_by_user` / `_top_apps` leem do agregado se `_agg_ready` (range coberto pelo
backfill) **senão caem no fallback de varrer `events`** — então nada quebra enquanto
o backfill roda no 1º deploy.

**Resultado medido (tenant ASAPTECH, ~91 colaboradores, período Hoje):**

| Métrica            | Antes  | Depois  |
|--------------------|--------|---------|
| `overview`         | 36,9 s | ~0,2 s  |
| `team_summary`     | 29,0 s | ~0,63 s (cache-miss) |
| Página (Load)      | ~44 s  | ~0,6 s  |

Caminho até aqui (todos no commit desta sprint): (1) render progressivo + cache TTL
90→300s + logs `_Perf`; (2) consolidação do `overview` (6 varreduras → 1 scan + users,
agrega em Python) e `afk_in`/Jornada sem o cast `ts::timestamptz` (texto puro,
indexável); (3) rollup completo (este bloco).

**Perf via CloudWatch:** Logs Insights no grupo `/aws/ecs/workpulse-prod-server`,
filtro `@message like /\[perf\]/`. As linhas `[perf]` logam o tempo de cada query
(`WORKPULSE_PERF_MS`=200 limiar). Cache-hit retorna antes do `_Perf` (não loga).

### Pendência opcional: `ts` TEXT → `timestamptz`

`events.ts` é TEXT (ISO-8601 UTC). Comparações de faixa usam texto (`ts >= %s`,
lexicográfico = cronológico). Migrar p/ `timestamptz` removeria os casts restantes
e deixaria índices 100%. **Virou opcional** — o rollup já contornou o gargalo e o
dashboard ficou rápido sem isso. Faxina futura, sem pressa.

### Gestão de frota do agente — distribuição + enrollment — 2026-06-17

Plano completo em `../PLANO_GESTAO_FROTA_AGENTE.md` (3 fases). **Fase 1 entregue**;
F2 (watchdog) e F3 (auto-update) pendentes.

**Distribuição de builds (F1a):** bucket privada `workpulse-releases-<conta>` (terraform,
versionada). Tabela `agent_releases` (channel `generic`|`tenant:<id>`, os, version, s3_key,
sha256, is_latest…). Endpoints `require_admin`: `POST /v1/admin/releases` (upload corpo cru
octet-stream + query; calcula sha256, grava no S3), `GET .../releases`, `PUT .../{id}/promote`,
`GET .../{id}/download` (URL pré-assinada 5 min), `DELETE`. Página: Platform Admin → Sistema →
"Builds do agente". Env `WORKPULSE_RELEASES_BUCKET`.

**Enrollment (F1c):** agente genérico troca um **código** pela licença do tenant.
- Tabelas: `enrollment_codes` (code, tenant_id, expires_at null=permanente, active),
  `enrolled_devices` (status pending|approved|rejected), `tenants.enroll_approval` (default TRUE).
- `POST /v1/enroll` (público; código = credencial): valida, aplica fila de aprovação, e quando
  aprovado devolve a **licença atual do tenant** (`_tenant_license_token`, mesmo modelo de hoje).
- Admin Master: códigos CRUD, settings (aprovação), fila `GET /v1/enroll-devices`, decide/delete.
  Card em Administrativo → Colaboradores → "Onboarding de agentes" (`loadEnrollment` em wp-settings.js).
- Agente (F1c-2): `src/enroll.rs` + `cfg.enroll_code`; main.rs resolve licença embutida →
  `license.dat` salvo → enrollment (loop 60s se pending; exit 4 se rejected).

### License signing (Ed25519)

`load_or_create_keypair()` loads the private key, priority:
1. Secrets Manager (`workpulse-prod/license-key-b64`) — source of truth
2. env var `WORKPULSE_LICENSE_PRIVKEY_B64`
3. generates a new key on first boot and stores it in Secrets Manager

Public key is served at `GET /v1/license/pubkey`; agents verify licenses locally.

### Background worker

`_retention_worker()` (asyncio task, started in `lifespan`) archives events older
than `tenants.retention_days` (default 90) into `daily_summaries`, then deletes them.

`_rollup_worker()` (asyncio task, started in `lifespan`) mantém `agg_user_hour` /
`agg_user_day` (ver "Rollup / agregados" acima).

`_reports_worker()` (asyncio task, started in `lifespan`) sleeps until 10:00 UTC
(07:00 BRT) daily, then calls `_do_reports()` which queries `report_configs` and
sends PDF reports via AWS SES for each enabled config whose `frequency` matches
the current day (daily=every day, weekly=Monday, monthly=1st). PDF is generated
with **reportlab**. Env vars: `WORKPULSE_FROM_EMAIL`, `WORKPULSE_SES_REGION`.

---

## Database Schema

| Table | Purpose |
|---|---|
| `tenants` | One row per client company. `dash_token`, `retention_days` |
| `report_configs` | Configurações de relatórios automáticos por tenant: `name`, `recipients` (JSON), `frequency`, `sections` (JSON), `user_filter` (JSON), `enabled` |
| `report_history` | Log de envios: `config_id`, `sent_at`, `status` (ok/error), `error_msg` |
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
**Tenant-scoped (Reports):** `GET /reports`, `POST /reports`,
`PUT /reports/{id}`, `DELETE /reports/{id}`, `POST /reports/{id}/send`,
`GET /reports/{id}/history`
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
    wp-core.js            State, utils, api(), apiFetch(), chartTheme(), CAT_*, ptDateTime(), escHtml()
    wp-datepicker.js      Date picker reutilizável (wpDP namespace — init, getRange, getDays)
    wp-overview.js        Aba Visão Geral (loadOverview, drawDailyChart, etc.)
    wp-people.js          Aba Pessoas (loadPeople, renderPeopleTable, drillUser)
    wp-departments.js     Aba Departamentos (loadDepartments)
    wp-analytics.js       Abas Alertas + Tendências (loadAlerts, loadTrends)
    wp-events.js          Aba Aplicativos (loadEvents, drawEvDailyChart, renderEventRows, _renderEvPagination)
    wp-internet.js        Aba Internet (loadInternet, renderSitesTable, _intCatBadge, _renderIntPagination)
    wp-jornada.js         Aba Jornada de Trabalho (loadJornada, computeJornadas)
    wp-settings.js        Aba Configurações (regras, apps, depts, colaboradores)
    wp-reports.js         Relatórios + conta de e-mail SMTP do tenant (CRUD, preview, histórico)
    wp-users.js           Usuários do painel (CRUD, papéis, escopo do Líder), Licença, Financeiro
    wp-locations.js       Coletas → Localização (IPs públicos + geo + mapa Leaflet)
    wp-analytics.js       Aba Alertas (5 sinais via /v1/alerts/today) + Tendências + config de Alertas
    wp-nav.js             Navegação por tabs + dropdowns (switchTab, TABS, TAB_GROUP, loaders)
    wp-auth.js            Login e-mail/senha (sessão), papéis, _applyRoleUI, tema, bootstrap IIFE
```

#### Navegação (topbar)

```
[Visão Geral]  [Coletas ▾]            [Análises ▾]     [⚙ ▾]
               Pessoas                 Alertas          Configurações
               Departamentos           Tendências       Sair
               Eventos
               Internet
               Jornada de Trabalho
```

#### Features adicionadas em 2026-05-26

- **Dropdowns de nav**: Coletas (Pessoas/Depts/Eventos/Internet/Jornada) e Análises (Alertas/Tendências).
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


#### Date Picker (`wp-datepicker.js`)

Componente reusável `wpDP` (IIFE, sem dependências). Instanciado em `wp-nav.js`
ao inicializar a página.

```javascript
wpDP.init('myId', { defaultDays: 7, onChange: myCallback });
const { since, until } = wpDP.getRange('myId');  // ISO strings
const days = wpDP.getDays('myId');               // número de dias
```

HTML: apenas `<div id="myId"></div>`. O componente injeta o trigger e o painel.

**Presets:** Hoje / Últimos 7 dias / Últimos 30 dias / Últimos 90 dias / Personalizado.
**Modo Personalizado:** clique inicial = início do período; clique seguinte = fim.
Se já houver início+fim selecionados, qualquer novo clique reinicia a seleção.

Pickers por aba:
| ID | Aba | Default |
|---|---|---|
| `rangeDays` | Visão Geral + Alertas | 7 dias |
| `peopleRange` | Pessoas | 7 dias |
| `trendDays` | Tendências | 30 dias |
| `evDays` | Aplicativos | Hoje |
| `intDays` | Internet | Hoje |
| `jorDays` | Jornada de Trabalho | 7 dias |

**Cache busting:** `?v=N` é **por arquivo** em `workforce.html` (não há um número
único global). Incrementar só o(s) arquivo(s) alterado(s) a cada deploy. Estado
atual (2026-06-17): `wp-core.js?v=20`, `wp-settings.js?v=25`, `wp-nav.js?v=30`,
`wp-overview.js?v=21`, `wp-internet.js?v=20`, `wp-events.js?v=19`, `wp-users.js?v=26`,
`wp-analytics.js?v=24`, `workforce.css?v=26`; painel adm `admin.js?v=17`.

#### Aba Jornada de Trabalho (adicionada 2026-05-27)

Submenu dentro de **Coletas → Jornada de Trabalho**. Mostra para um colaborador
escolhido, dia a dia: início da jornada, fim, total de pausas, bruta e líquida.

**Lógica de cálculo** (`computeJornadas` em `wp-jornada.js`):
- `Início da Jornada` = primeiro `bucket=window` event do dia (timestamp local).
- `Final da Jornada`  = `ts + duration_sec` do último `bucket=window` event do dia.
- `Total de Pausas`   = soma dos `duration_sec` de `bucket=afk` que caem dentro
  da janela `[start, end]` do mesmo dia local (almoço, café, AFK longos).
- `Bruta`   = `end - start`. `Líquida` = `Bruta - Pausas`.
- **Dia atual**: Final em branco (em andamento), Pausas/Líquida marcadas como
  parciais. Detectado por `_jorLocalDayKey(new Date()) === j.day`.

**Fetch chunked por dia** (`_jorFetchByDay`): como `/v1/events` limita a 1000
eventos/chamada, faz `N` chamadas paralelas (cap de 5) com `since/until` por dia.
Para 30 dias × 2 buckets = 60 chamadas em ~10 batches. Sem necessidade de novo
endpoint de backend.

**IMPORTANTE — `order=asc`:** `/v1/events` suporta `?order=asc|desc` (default `desc`).
Jornada usa `order=asc` para garantir que os primeiros 1000 eventos de cada dia
incluam o início da jornada (início matinal). Com `order=desc`, ao final do dia
os 1000 resultados são todos do período da tarde, cortando os eventos da manhã
e exibindo horário de entrada errado. Redeploy do backend é necessário para que
o parâmetro `order` seja respeitado.

**Visual**: 5 KPI cards no topo (dias, jornada líquida média, início médio,
encerramento médio, pausas médias), 2 charts (barra empilhada trabalho+pausas
por dia, linha de início × fim) e a tabela final.

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

Separate crate. Built per-client with an embedded license **ou** genérico (sem licença,
com enrollment).
- **Resolução de licença no boot (main.rs, antes do `license::validate`):**
  embutida (`cfg.license_key`) → `license.dat` salvo (enroll anterior) → **enrollment**
  (se `cfg.enroll_code` setado): `enroll.rs` chama `POST /v1/enroll`, recebe a licença e
  salva em `license.dat` (data dir, instalador não toca). Pending → tenta a cada 60s;
  rejeitado → `exit(4)`. Adicionado 2026-06-17.
- **REGRA: todo rebuild de agente é SEMPRE Windows E Linux** (os dois, nunca só um).
- **Nomes de saída (fixos):**
  - Windows: `workpulse-agent-windows-<empresa>-<versao>.msi`
  - Linux:   `workpulse-agent-ubuntu_22.04-<empresa>-<versao>.deb`
- **Build por-cliente (licença embutida):**
  - Windows: `windows/build-msi.ps1 -LicenseKey "<token>" -ClientName "<empresa>" -Version "2.2.2"`
  - Linux:   `VERSION="2.2.2" CLIENT_NAME="<empresa>" LICENSE_KEY="<token>" ./linux/build-deb.sh`
- **Build genérico (enrollment):** licença vazia + código de enrollment (gerado no painel):
  - Windows: `build-msi.ps1 -LicenseKey "" -EnrollCode "<código>" -ClientName "<empresa>" -Version "2.2.2"`
  - Linux:   `VERSION="2.2.2" CLIENT_NAME="<empresa>" LICENSE_KEY="" ENROLL_CODE="<código>" ./linux/build-deb.sh`
- **Trava de build (F1c-3):** os scripts **recusam** gerar pacote sem licença E sem código
  (agente zumbi). Bypass: `-AllowEmpty` (Win) / `ALLOW_EMPTY=1` (Linux).
- `CLIENT_NAME`/`-ClientName` default `generico`; ambos rodam `cargo build --release`.
  - **Deps de build (Ubuntu/WSL):** `build-essential dpkg-dev pkg-config libx11-dev
    libxi-dev libxtst-dev`. O `x11rb` (janela ativa) é Rust puro, mas o `device_query`
    (AFK) puxa o crate C `x11` → exige `pkg-config` + `libx11/xi/xtst-dev`.
  - **glibc/versão:** o binário fica preso à glibc da distro onde compila. **Compile
    na versão de Ubuntu mais ANTIGA da frota** (ex.: 22.04 = glibc 2.35 roda em 22.04 e
    24.04; um build em 24.04/glibc 2.39 NÃO roda em 22.04 — erro `GLIBC_2.39 not found`).
  - **`DeviceState` é `!Send` no Linux:** no `tracker.rs`, leia teclado/mouse com um
    `DeviceState::new()` dentro de um bloco `{ }` efêmero (criar→ler→descartar) a cada
    ciclo; manter o `DeviceState` vivo através de um `.await` quebra o `tokio::spawn`
    com "future cannot be sent between threads safely".
  - **Distribuição unattended (.deb):** não há flag de silent; use
    `DEBIAN_FRONTEND=noninteractive apt-get install -y -o Dpkg::Options::="--force-confold" ./pkg.deb`
    (o `--force-confold` evita prompt de conffile no upgrade). Roda como root; o agente
    inicia no próximo login gráfico (X11) via autostart XDG. 1 `.deb` por tenant.
- **macOS — PARCIAL (código sim, distribuição não).** `Cargo.toml` tem deps macOS
  (`core-graphics`/`core-foundation`/`objc`) e `src/platform.rs` tem `macos_impl`
  (janela ativa); `device_query` cobre AFK. Ou seja, **compila e roda** num Mac. Mas
  **NÃO existe** pasta `macos/`, instalador (`.pkg`/`.dmg`), `launchd` (autostart), nem
  assinatura/notarização. Para entregar de fato faltam: build num Mac (binário universal
  Intel+ARM via `lipo`), `.pkg` + LaunchAgent, **codesign + notarize** (Apple Developer
  ID), e **permissões TCC** (Acessibilidade/Gravação de Tela) — em deploy gerenciado,
  via **perfil PPPC pelo MDM** (Endpoint Central). Ver `PLANO_AREA_DOWNLOAD_AGENTE.md`
  e (quando criado) o plano do agente Mac.
- **Config:** `agent.conf` (TOML). Only `license_key` is required; `server_url`
  defaults to `https://api.workpulse.com.br`; tenant comes from inside the license.
  Installed at `%ProgramData%\WorkPulse\agent.conf` (Windows) /
  `/etc/workpulse/agent.conf` (Linux).
- **Reunião → não-AFK (keepalive), 2026-06-15.** No `tracker.rs`, o `afk` agora é
  `idle && !in_meeting`: enquanto a janela ativa casar com um padrão de reunião, o
  agente NÃO entra em AFK (o heartbeat de janela segue → a call conta ativa inteira,
  mesmo sem teclado/mouse). Helper `is_meeting_window` (substring no app OU no título,
  case-insensitive). Lista configurável `meeting_patterns` em `config.rs`/`agent.conf`
  (defaults: Meet, Teams, Zoom, Webex, Whereby, GoTo, BlueJeans; lista vazia desliga).
  `afk_timeout_sec` (os "3 min") já era configurável. **Só agente** — sem mudança no
  servidor; o stream de eventos é o mesmo (durante a call o agente emite `window`
  heartbeats e não emite `afk`). **Forward/backward-compatible:** agentes antigos
  (sem o fix) e novos convivem; só o resultado difere por máquina até o rollout
  terminar. Detecção é por título/app — exige `capture_titles=true` p/ Meet no browser;
  "zoom meeting" é preciso (só em call), "microsoft teams" casa com o Teams em foco.
- **License flow (`src/license.rs`):** the agent verifies the Ed25519 signature
  locally. It fetches the server public key from `/v1/license/pubkey` and caches
  it at `%APPDATA%\WorkPulse\Agent\data\server_pubkey.txt`. As of 2026-05-25 the
  agent **self-heals**: if verification fails with the cached key, it drops the
  cache and refetches the current key.
- **Per-client versioning (2026-05-27).** The agent reads its version from
  `env!("WORKPULSE_AGENT_VERSION")` at compile time. `build.rs` resolves the
  value from the `WORKPULSE_AGENT_VERSION` env var (set by the build scripts),
  falling back to `CARGO_PKG_VERSION` from `Cargo.toml` (currently `1.0.0`).
  `Cargo.toml` declares `build = "build.rs"`. The MSI script accepts
  `-Version "X.Y.Z"` (validated regex, WiX gets `X.Y.Z.0`); the deb script
  accepts `VERSION="X.Y.Z"`. The value is propagated to the binary (used in
  the `agent_version` field of every ingest payload), the MSI Package
  `Version` attribute via `<?define AgentVersion?>` + `$(var.AgentVersion)`,
  and the `.deb` `Version` field. Without these flags every client appears as
  `1.0.0` in the workforce dashboard — that was the symptom that prompted
  the change.
- **AFK with real duration (2026-05-27, v2.0+ of the agent).** Pre-fix the
  tracker only emitted transition markers (`status="afk"`/`"not-afk"`) with
  `duration_sec=0.0`, so every server query of the form
  `SUM(duration_sec WHERE bucket='afk' AND status='afk')` returned 0. This
  silently broke `users/{id}/summary`, `team/summary`, `workforce/overview`,
  and the Jornada tab. The fix in `src/tracker.rs` slices each AFK period
  into events with real durations: (1) an initial slice with
  `ts = now - afk_timeout_sec`, `duration = afk_timeout_sec` on the
  active→AFK transition; (2) periodic heartbeats every `heartbeat_interval_sec`
  while AFK with `data.heartbeat = true`; (3) a final partial slice on
  AFK→active, followed by the legacy `status="not-afk"` marker with
  `duration=0` (kept for backward compatibility with consumers that detect
  end-of-period by marker). Slices are non-overlapping, so a plain
  `SUM(duration_sec)` reflects the real idle time. **No server or dashboard
  changes were needed** — the SQL contracts in CRITICAL #2 already expected
  this shape, the agent just wasn't honoring it.

---

## Estado atual (2026-06-18) — Suporte Técnico, bugs de coleta (Fases A/B/C), Consolidado

Sprint: nova tela **Suporte Técnico** (+ perfil), correção dos **bugs de coleta** do
`../BUGS_COLETA.md` (AFK gigante, duplicação, sono), refinos na **Consolidado** e perf da
Categorização.

### Perfil `suporte` + tela Suporte Técnico
- Papel **`suporte`** em `CLIENT_ROLES`/`ALL_ROLES`. Tenant-scoped, sem restrição de líder.
  Selecionável na tela Usuários.
- Página **Administrativo → Suporte Técnico** (`tab-suportetec`, `wp-support.js`), visível só
  para **Admin Master, Admin e Suporte** (`data-role="suporte_admin"`; o perfil `suporte` só vê
  essa tela — `_applyRoleUI` esconde o resto e cai nela no login).
- 2 cards (grid 1fr 1fr): esquerda = lista paginada (Colaborador/Status/Última atividade/Versão
  do agente); direita = detalhe ao clicar (Departamento/Máquina/Sistema/última coleta AFK·App·Web/
  início da jornada). Endpoints `require_role("admin_master","admin","suporte")` (definidos APÓS
  `require_role` no arquivo): `GET /v1/support/people`, `GET /v1/support/person` (últimas coletas
  via `ORDER BY ts DESC LIMIT 1` + piso 30d, NÃO `MAX(ts)` sem piso que levava ~1min/clique).

### Online/offline — tolerância a skew de relógio
`_isOnline` (wp-people.js) considera online dentro de **±**`WP_ONLINE_MINUTES`. Agente que
reportou "agora" pode ter `last_seen` segundos no futuro (relógio do servidor adiantado); antes
caía em Offline com "última atividade: agora".

### Bugs de coleta — Fases A/B/C (detalhe em `../BUGS_COLETA.md`)
- **Fase A (no ar):** trava de AFK gigante (sono) nas LEITURAS. `AFK_MAX_SEC`
  (`WORKPULSE_AFK_MAX_SEC`, default 10800s/3h) — AFK acima do teto é **descartado** em TODA soma
  de AFK (rollup, `_hour_rows`, `_afk_in_by_user`, jornada, drill, overview/tendências, PDF,
  retenção). `ROLLUP_SCHEMA_VER`=**3** re-rola o histórico suave.
- **Fase B1 (no ar):** idempotência. Coluna `events.event_id` + índice único **parcial**
  `idx_events_eid` criado **CONCURRENTLY** em `init_db` (advisory-lock + `_event_id_dedup_watcher`
  + flag `_EVENT_ID_DEDUP`). Ingest: `INSERT ... ON CONFLICT (event_id) WHERE event_id IS NOT NULL
  DO NOTHING` quando o índice é válido. Retrocompatível (agente antigo → NULL → grava normal).
  Cuidado: 2 tasks subindo juntas davam deadlock no build → daí o advisory-lock + skip-se-válido.
- **Fase B2 (agente):** `sync.rs` envia `event_id` = **UUID v5 do conteúdo**
  (`host|bucket|ts|duration|data`) — estável entre reenvios, distinto entre eventos. `uuid` com
  feature `v5`. (Chave natural, toca só sync.rs — sem persistir no buffer.)
- **Fase C (agente):** `tracker.rs` detecta resume de suspensão (gap > 120s entre ciclos via
  `last_loop_utc`) e **descarta** o buraco de sono. **Validado** (hibernação de 1h25 não virou AFK).
- **Bug 3 — dupla contagem da carência (agente):** a fatia inicial de AFK **não retroage mais**
  `afk_timeout`. Antes `ts=now-afk_timeout, duration=afk_timeout` contava a carência (3min) como
  ATIVO (heartbeats) **E** AFK. Agora `ts=now, duration=0` (marcador) e o AFK acumula pelas fatias
  periódicas. **Substitui** o ponto (1) do bloco "AFK with real duration (2026-05-27)" acima.
- B2+C+Bug3 no **MESMO rebuild** (nova versão, ex. 2.3.0). Pendente: rollout da frota → depois
  **Fase D** (limpeza do histórico: dedup + AFK gigante), opcional, em lotes/throttle.

### Coletas → Consolidado (nova tela)
`tab-consolidado` (`wp-consolidado.js`) abaixo de Internet. Endpoints
`/v1/productivity/consolidado` (agregado do período inteiro, inclui browsers, `bucket=''|'afk'|
'all'`) e `/v1/productivity/timeline` (blocos cronológicos colapsando consecutivos iguais).
Defaults Tipo=Todos / Agrupamento=Sequencial; mais novo→mais antigo; paginação (`_renderCfgPagination`).
Gráfico por categoria/dia com **unidade adaptativa** (min/h) + `maxBarThickness`.

### Categorização — perf + lista de trabalho
`/v1/productivity/apps` lê do rollup `agg_app_day` quando pronto + cache; `/v1/productivity/internet`
ganhou `detail=0` (pula o scan do gráfico), filtro de navegador no SQL (`_only_browser_where`) e
cache. Toggle **"só não categorizados"** (default on, `uncat=1`) nos cards Apps/Sites. Era ~50s → sub-segundo.

### Cache busting (atual)
`wp-consolidado.js?v=6`, `wp-support.js?v=3`, `wp-people.js?v=22`, `wp-settings.js?v=26`,
`wp-users.js?v=27`, `wp-nav.js?v=32`, `wp-auth.js?v=22`.

---

## Estado atual (2026-06-17)

Snapshot consolidado do que existe hoje. O changelog detalhado por data segue abaixo
(histórico). `server/main.py` agora tem **~6.200 linhas**, ainda single-file; blocos
novos são **adicionados antes da seção `# Entry point`**, cada um com seu
`_SCHEMA_STMTS.extend([...])`.

**Trabalho recente (jun/2026), por seção acima:** perf dos painéis (consolidação do
overview + rollup `agg_*` + cache 300s); SMTP por tenant; alertas (engajamento/AFK/ausente/
sites de risco); papéis Admin/Admin Master + blocklist; config central do agente; keepalive
de reunião; e a **Fase 1 da gestão de frota** (distribuição de builds + enrollment) — ver
"Gestão de frota do agente" acima e `../PLANO_GESTAO_FROTA_AGENTE.md`. **Pausado** em F2/F3.

### Autenticação — agora é e-mail/senha com sessão + papéis
Migrou do login Tenant+Token para **e-mail/senha** (cookie de sessão `wp_sess`).
- Tabelas: `dash_users` (email único global, `role`, `status`, hash argon2),
  `dash_sessions`, `dash_user_tokens` (link de definir/redefinir senha).
- Papéis: `admin_master`, `admin`, `supervisor`, `lider`, `suporte`, `wp_admin` (plataforma).
  `CLIENT_ROLES=("admin_master","admin","supervisor","lider","suporte")`, `ALL_ROLES` inclui `wp_admin`.
  `suporte` só vê a tela Suporte Técnico (ver "Estado atual 2026-06-18").
- Deps no backend: `require_session`, `require_role("...")`, `require_tenant_scope`
  (aceita cookie de sessão **ou** token break-glass). `_load_session_user`.
- **Líder** = perfil mais restrito: vê só colaboradores no escopo dele. Tabela
  `lider_scope` (department | user). Helpers `_lider_allowed_ids(request, tenant)`
  (devolve lista de user_ids permitidos ou `None` = sem restrição) e `_scope_for()`
  (devolve `(allowed, cache_tag)` p/ o cache não vazar entre escopos). Enforcement
  injetado em ~12 endpoints (filtro Python em listas; `AND user_id = ANY(%s)` nas SQL).
- Frontend: `wp-auth.js` faz login/sessão; `_applyRoleUI()` esconde elementos via
  `data-role="admin_master"` (wp_admin conta como admin). Menu da conta mostra o Perfil.

### Tabelas novas (além das do schema acima), por subsistema
- **Localização/IP:** `client_ips`, `ip_geo` (geo via FreeIPAPI; mapa Leaflet + CartoDB).
- **Financeiro:** `plan_prices` (preço/seat por plano, em centavos), `tenant_billing`
  (override de preço + desconto% por tenant), `payments` (baixa manual mês a mês).
- **E-mail por tenant:** `tenant_email_config` (SMTP do cliente; senha **criptografada
  Fernet**), `app_secrets` (guarda a chave Fernet se não houver env `WORKPULSE_FERNET_KEY`).
- **Alertas:** `alert_settings` (horário comercial, corte, limiares), `alert_rules`
  (por tipo: enabled/email_enabled/recipients), `risky_sites` (padrões de sites),
  `alert_notify_log` (dedupe de e-mail por tenant/tipo/usuário/dia).

### Endpoints novos (todos sob `/v1`)
- **Auth/sessão:** `POST /auth/login`, `/auth/logout`, `/auth/set-password`,
  `/auth/me`; gestão: `/tenant-users` (CRUD), `/tenant-users/{id}/scope` (Líder).
- **Licença do cliente:** `/tenant-license`, `/tenant-license/assign|request`.
- **Localização:** `/locations` (lista + drill por usuário); admin `/admin/geo-test`.
- **Custos AWS (admin):** `/admin/aws-costs`, `/admin/tenant-costs`, `/admin/cost-simulate`
  (Cost Explorer, boto3, RECORD_TYPE=Usage p/ furar o Free Tier).
- **Financeiro:** admin `/admin/plan-prices` (GET/PUT), `/admin/tenant-billing` (GET/PUT),
  `/admin/payments` (GET/POST/DELETE); cliente `/tenant-finance`.
- **Relatórios:** `/reports/{id}/preview` (devolve o PDF inline, sem enviar).
- **E-mail SMTP:** `/tenant-email-config` (GET/PUT), `/tenant-email-config/test`.
- **Alertas:** `/alerts/today` (5 sinais; respeita escopo do Líder),
  `/alerts/config` (GET/PUT), `/alerts/risky-sites` (POST/DELETE/`bulk`).

### Workers (asyncio, no `lifespan`)
`_retention_worker`, `_reports_worker` e **`_alerts_worker`** (a cada ~15 min;
`_do_alert_emails` envia por tipo ao ponto focal, dedupe diário via `alert_notify_log`;
o site perigoso deduplica por **pessoa+site**, os demais por **pessoa**).

### Relatórios — mudou bastante
- O envio agora usa o **SMTP do tenant** (`_smtp_send`), **não** mais SES; se não houver
  conta configurada/habilitada, o relatório **não é enviado** (fica logado no histórico).
- Dados alinhados ao **período** (diário=o dia, semanal=a semana, mensal=o mês) via
  `_report_period_range(freq)`, em vez de janelas móveis fixas. Datas em fuso local.
- Seção **Jornada** do PDF é calculada a partir dos `events` (mesma lógica do `/v1/jornada`),
  agrupada por colaborador, com as colunas da tela (Dia/Início/Final/AFK na Jornada/
  AFK Pós Jornada/Bruta/Líquida). A criação de relatório escolhe **1 tipo por vez**
  (Jornada **ou** Alertas; "Coletas"/"Tendências" foram removidas da UI de criação).

### Alertas operacionais (tela Análises → Alertas + página Administrativo → Alertas)
5 sinais: `afk_today` (AFK ≥ limiar no horário comercial, só se já começou),
`absent_today` (não iniciou até a hora de corte), `risky_site` (site da lista),
`underwork`/`overwork` (média por **dia útil** — ignora FDS e **feriados nacionais**
calculados em `_br_holidays`/`_easter`). Config por tenant em página própria no
Administrativo (horário comercial, corte, regras por tipo, lista de sites + importação
em massa CSV `padrão;rótulo`).

### Menu Administrativo (Admin Master) — `workforce.html`
Novo grupo no topo com páginas: **Financeiro**, **Usuários**, **Categorização**,
**Colaboradores**, **Relatórios**, **Alertas**, **Licenciamento**. Configurações ficou
só com **Aparência** + **Minha conta**; os demais cards migraram para essas páginas.
`wp-nav.js`: `TABS`, `TAB_GROUP` (grupo `administrativo`/`coletas`) e `loaders`.

### Platform Admin (`site/adm/`)
Card **Preços dos Planos** (aba Sistema) + botão **financeiro** por tenant (modal de
override/desconto + registrar/listar pagamentos). Card "Banco" agora diz **PostgreSQL**.

### Deploy — o usuário roda os **PowerShell** (.ps1), não os .sh
- `scripts\redeploy.ps1` (backend: zip→S3→CodeBuild→ECS rolling) e
  `scripts\publicar-site.ps1` (site→S3 + invalida CloudFront).
- **`scripts\redeploy-publicar.ps1`** roda os dois em sequência (aborta se o 1º falhar).
- Regra: mudou `server/main.py` → redeploy; mudou `site/` → publicar; mudou os dois →
  o combinado. Cache-busting é **por arquivo** (`?v=N` em cada `<script>/<link>`),
  incrementar o(s) que mudou.

### Infra (Terraform)
A **CloudFront Function de clean-URLs** foi portada para `infra/main.tf`
(`aws_cloudfront_function "clean_urls"` + `function_association` lendo
`scripts/clean-urls.js`) — um `terraform apply` já entrega as URLs limpas (o passo
manual `4-setup-clean-urls.sh` deixou de ser obrigatório em ambiente novo). A política
de Cost Explorer (`aws_iam_role_policy "ecs_task_costs"`) também está no Terraform.

### Ambiente de edição — ATENÇÃO (relacionado ao CRITICAL #1)
A pasta do projeto é **OneDrive (cloud-on-demand)**. O mount Linux do sandbox às vezes
entrega cópias **truncadas/defasadas** dos arquivos (o `py_compile`/`node --check` do
arquivo inteiro falha por isso, não por erro real). Os arquivos reais (caminho Windows,
que o deploy usa) ficam íntegros. Para validar sob esse cenário: compile/`node --check`
os **blocos novos isoladamente** com stubs, confirme o fim do arquivo via Read, e confie
no deploy (lê o caminho Windows). Exclusão de arquivos na pasta exige o tool
`allow_cowork_file_delete` antes do `rm`.

---

**Last updated:** 2026-05-29 (added per-client versioning, fixed AFK duration
calculation in the Rust agent, rebuilt eight silently-truncated agent files;
added `agent_version` column to aba Pessoas — `GET /v1/team/summary` now returns
`agent_version` via correlated subquery on `devices` table; `workforce.html`
and `wp-people.js` updated accordingly; fixed GROUP BY omission that caused
production 500 → added `u.tenant_id` to GROUP BY in `team_summary` SQL;
added CRITICAL #3 documenting the GROUP BY + correlated subquery pitfall;
**added Relatórios Automáticos** — tables `report_configs`/`report_history`,
6 new tenant-scoped endpoints under `/v1/reports`, PDF generation via reportlab,
SES email via boto3, `_reports_worker` cron task (daily at 10:00 UTC = 07:00 BRT),
frontend in `wp-reports.js`, UI card in aba Configurações of `workforce.html`).

**2026-05-29 updates:**
- **Calendar date picker** (`wp-datepicker.js`): novo componente reusável `wpDP`.
  Todos os selects de período substituídos por date pickers com presets + calendário.
  Período agora passa `since`/`until` (ISO) para a API em vez de `days` (int).
- **Backend `since`/`until`**: 6 endpoints atualizados para aceitar `since`/`until`
  como strings ISO além de `days` (fallback). Endpoints: `user_summary`, `team_summary`,
  `productivity_events_summary`, `workforce_overview`, `workforce_timeseries`,
  `productivity_internet`. Endpoint `/v1/events` agora aceita `order=asc|desc`.
- **Clean URLs**: CloudFront Function em `scripts/clean-urls.js` + `scripts/4-setup-clean-urls.sh`.
  `workforce.html` tem `<base href="/dashboard/">` para resolver paths corretamente.
- **Funções recuperadas** (truncação anterior): `drawEvDailyChart`, `renderEventRows`,
  `_renderEvPagination` (wp-events.js); `_intCatBadge`, `_renderIntPagination`
  (wp-internet.js); `ptDateTime`, `escHtml` (wp-core.js); `prefillRule` (wp-settings.js).
- **Bug fixes**: posição do calendar (requestAnimationFrame); reset de range ao
  re-clicar com início+fim já selecionados; Pessoas: picker movido para esquerda;
  `renderSitesTable`: duplicate `const` removido; script tag `wp-auth.js` sem
  `</script>` (quebrava todo o JS da página).
- **Jornada horário de entrada**: `_jorFetchByDay` usa `order=asc` — garante que
  os primeiros 1000 eventos incluam o início da manhã.

**2026-05-30 updates:**
- **Aba Internet — coluna "Site / Página" colapsada**: títulos/URLs longos (ex.: URLs
  do Figma) estouravam a linha da tabela `#intSitesTable`. Adicionada regra CSS
  `#intSitesTable .site-name` em `workforce.css` (`max-width: clamp(160px,32vw,560px)`
  + `overflow:hidden; text-overflow:ellipsis; white-space:nowrap`) que trunca em uma
  linha com "…", preservando o começo do texto e garantindo que a linha sempre caiba.
  O texto completo fica no atributo `title` (tooltip) — adicionado à `<td class="site-name">`
  em `wp-internet.js`. Cache-busting bump `?v=15` → `?v=16`.
- **⚠️ Reincidência da truncação silenciosa (CRITICAL #1)**: o Edit tool truncou
  `wp-internet.js` no final (cortou em "Próx" na última linha de `_renderIntPagination`).
  Recuperado via heredoc `cat >>` conforme o procedimento da seção CRITICAL #1.
  Lembrete: para qualquer arquivo grande, validar com `node --check` (JS) e checar
  balanço de chaves/parênteses/backticks após editar.
- **Normalização case-insensitive na agregação de apps/sites**: `Slack.exe` vs
  `slack.exe` (e `Explorer.exe` vs `explorer.exe`) eram contados como apps distintos
  na sumarização. Correções:
  - `server/main.py` — `team_summary` (donut "Top Aplicações" da Visão Geral) e
    `user_summary` (drilldown): `GROUP BY app` → `GROUP BY LOWER(data_json->>'app')`
    com `MIN(data_json->>'app')` como nome de exibição (determinístico).
  - `server/main.py` — `productivity_internet`: chave do `site_agg` passou de
    `site_name` para `site_name.casefold()` (mescla títulos idênticos com casing
    diferente; nome de exibição = primeiro visto). `events_summary` NÃO precisou
    mudar — só soma por categoria e as regras já fazem `lower()`.
  - `site/dashboard/js/wp-events.js` (aba Aplicativos): `groupByApp` passou a usar
    `app.toLowerCase()` como chave do Map (exibição = primeiro visto), e o modo
    sequencial (`collapseEvents`) compara `app` em lowercase.
  - Validado com simulação SQLite (GROUP BY LOWER + MIN) e Node (groupByApp).
- **✅ CORRIGIDO — `server/main.py` estava truncado em `list_reports` (CRITICAL #1)**:
  o `main.py` no disco (e dentro de `workpulse-deploy.zip`) estava truncado na
  assinatura `def list_reports(t` (linha ~2355) e não compilava. Era anterior às
  mudanças acima (confirmado: `.bak` e o zip byte-idênticos antes da edição). A
  seção de Relatórios (`list_reports`/`create_report`/`update_report`/
  `delete_report`/`send_report_now`/`report_history`) e o bloco `# Entry point` +
  `serve()` foram **restaurados a partir de `workpulse-deploy-v39.zip`** (cujos
  `ReportConfigIn`/`ReportConfigOut`, `_do_reports` e `_reports_worker` são
  byte-idênticos aos do arquivo atual — feature de Relatórios não mudou entre as
  versões; só endpoints novos como `/v1/jornada` divergiam no corpo). Confirmado que
  todos os símbolos referenciados pelos handlers (`_run_report`, `threading`, `json`,
  `os`, `LISTEN_ADDR`, `db`, `audit`, `_cache_bust`) já existiam no corpo atual.
  Resultado: `main.py` agora tem 2472 linhas e **compila** (`py_compile` OK). Splice
  feito em `/tmp` e copiado ao disco com verificação de `md5sum`.

**2026-05-31 updates:**
- **Login — campo "Endpoint da API" ocultado**: o `#loginUrl` em `workforce.html`
  virou `<input type="hidden">` com valor fixo `https://api.workpulse.com.br`. O
  `wp-auth.js` não mudou (ainda lê/escreve `#loginUrl.value`). Remove o vetor de
  apontar o painel a um host malicioso. (Auth e-mail/senha completo será tratado à
  parte — ver `PLANO_AUTH_EMAIL_SENHA.md` na pasta WORK PULSE: design plano-primeiro
  com papéis admin/leitor, JWT, tabela `dash_users`, Admin Master via tenant+token.)
- **Aba Configurações — Relatórios religada + correções**: o card "Relatórios
  Automáticos" (com `#reportsListContainer`, botão `+ Novo Relatório` →
  `openCreateReportModal()`) e o `<script src="js/wp-reports.js">` haviam sumido do
  `workforce.html` (o `wp-settings.js` já chamava `loadReports()`, que ficava
  indefinido). Restaurados a partir de `workpulse-deploy-v25.zip` (cujo `wp-reports.js`
  é byte-idêntico ao atual). Ajuste de tema: `var(--bg-card,#1e293b)` → `var(--bg-2)`
  (a var `--bg-card` não existe; cairia em fundo escuro no tema claro). Também
  **removido o bloco "Regras Ativas" duplicado** (dois `id="rulesTable"` → um) e
  **removidos 64 bytes NUL** no final do `workforce.html` (corrupção de cauda, tipo
  CRITICAL #1, inofensiva ao render mas limpa agora). Validado: tags balanceadas
  (section/div/table/style/script), `id="rulesTable"`=1, container=1. Lista de scripts
  de `workforce.html` agora inclui `wp-reports.js?v=16`.
- **Site institucional**: adicionado link "Acesso cliente" → `/dashboard` na navbar de
  `site/index.html` (ao lado de "Começar piloto"; classes `.nav-actions`/`.nav-login`).
- **Removidos 6 arquivos `.bak`** de `site/` (seriam publicados pelo `s3 sync --delete`).

**2026-05-31 — fuso nos gráficos diários (Visão Geral):**
- **Sintoma**: "Horas trabalhadas por dia" mostrava o dia anterior / dividia um dia
  local em duas barras. Causa dupla: (1) backend agrupava por **data UTC**
  (`substr(ts,1,10)`) enquanto o date picker manda janelas em **meia-noite local
  (BRT)** convertida p/ UTC → um dia local cruza duas datas UTC; (2) frontend rotulava
  com `ptDate(d.day)`, e `new Date("YYYY-MM-DD")` é meia-noite **UTC**, exibida em BRT
  como o dia anterior.
- **Correção backend** (`workforce_overview`): agrupa por **hora UTC** (`substr(ts,1,13)`)
  e re-bucketiza em Python para **data local** via `DASH_TZ_OFFSET_HOURS=-3` (Brasil,
  sem DST). Cada dia local = uma barra. Mantém o shape de `daily` (`day/active/afk/users`).
  SQL portável (`substr`, `data_json->>`, `DISTINCT`). Validado com simulação SQLite.
- **Correção frontend** (`wp-overview.js`): rótulo agora `ptDate(d.day + 'T12:00:00')`
  (interpreta como data local, sem deslocar −3h).
- **Pendente (mesma classe de bug, fora do escopo pedido)**: as abas **Aplicativos**
  (`wp-events.js`) e **Internet** (`wp-internet.js`) usam `ptDate(d.day + 'T00:00:00Z')`
  e bucketing UTC nos respectivos gráficos diários — têm o mesmo deslocamento de rótulo.
  Não alterados nesta rodada; aplicar a mesma lógica se/quando solicitado.

**2026-05-31 — fuso em Aplicativos/Internet + bugs da aba Internet:**
- **Fuso (gráficos diários)**: aplicada a mesma correção da Visão Geral às abas
  **Aplicativos** (`events_summary`) e **Internet** (`productivity_internet`): SQL
  diário agrupa por **hora UTC** (`substr(ts,1,13) AS h`) e o loop Python converte
  para data local via `_utc_hour_to_local_day()` (agora função de módulo, reusada
  pelos 3 endpoints). Rótulos no front (`wp-events.js`, `wp-internet.js`) passaram de
  `ptDate(d.day + 'T00:00:00Z')` para `ptDate(d.day + 'T12:00:00')`.
  - Ainda em UTC (fora do escopo): `workforce_timeseries` (Tendências) e
    `user_summary` daily (drill-down de Pessoas) — mesmos ajustes pendentes.
- **Bug 1 — cabeçalho trocado**: o `<thead>` de `#intSitesTable` estava com
  Categoria/Tempo total invertidos vs. o `<tbody>`, com um `<th>` vazio sobrando e
  "% do browser" desalinhado. `<thead>` reescrito para 6 colunas batendo com o corpo:
  `Quando | Site / Página | Tempo total | Categoria | Nº usuários | Usuários`
  (`colspan` dos empty-states 7→6).
- **Bug 2 — dados vazando no botão Categorizar**: `onclick="prefillRule(${JSON.stringify(s.site_name)})"`
  quebrava o atributo HTML quando o título tinha `"`/`<`/`>`. Agora
  `prefillRule(${escHtml(JSON.stringify(s.site_name))})` — escapa para o contexto de
  atributo; o browser decodifica ao executar o onclick.
- **Bug 3 — coluna Usuários inútil**: `state.lastTeam` nunca era setado (só lido) →
  caía sempre em `u.slice(0,8)` ("asaptech" para todos). Agora resolve via
  `state.people` (carregado no `loadInternet` se vazio) → `displayName` (nome amigável)
  e, no fallback, `cleanUserId(u)` (nome da máquina completo, não truncado).
- **Bug paginação Internet (regressão própria)**: a linha de abertura do botão
  "Próxima" (`<button class="ghost ev-page-nav" ${hasNext ? '' : 'disabled'}`) tinha
  se perdido na restauração via heredoc do início desta sessão — o `</div>` ia direto
  ao `onclick=...`, vazando o botão como texto. Linha restaurada em `_renderIntPagination`.
  Lição: ao restaurar trechos via heredoc/`head -n`, conferir o bloco inteiro, não só a
  última linha.
- **Coluna Usuários → ícone com tooltip**: para não estourar o layout com listas longas
  de nomes, a célula agora mostra um ícone (SVG de usuários) e os nomes aparecem no
  `title` (hover). A contagem fica na coluna "Nº usuários".
- **Heatmap "Quando a equipe trabalha" vazio (Visão Geral)**: `drawHeatmap` somava
  `h.active` no fallback, mas o backend manda `hourly` como `{hour, seconds}` → todas as
  células viravam `NaN` → sem cor. Corrigido para `h.seconds` em `wp-overview.js`. (O
  laço sobre `daily[].by_hour` é código morto — esse campo nunca vem na resposta; mantido
  inofensivo.)
- **Heatmap em horário local**: o `hourly` (consumido só pelo heatmap) passou a ser
  re-bucketizado para **hora local BRT** no backend (Python, usando `DASH_TZ_OFFSET_HOURS`
  — `(hora_utc + offset) % 24`; evita o operador `%` no SQL, que é armadilha do psycopg2).
  Subtítulo do card em `workforce.html` atualizado de "(UTC)" para "(horário local)".

**2026-05-31 — sessão expira por inatividade (30 min):**
- Antes, o painel restaurava `wp_url/wp_tenant/wp_token` do `localStorage` no
  `bootstrap()` sem nunca expirar → abria sempre logado. Adicionado timeout de
  **inatividade de 30 min** em `wp-auth.js` (`WP_SESSION_IDLE_MS`):
  - `_wpTouchSession()` grava `wp_last_active` (throttle 20s; `force` no login/retomada).
  - `_wpSessionExpired()` compara `now - wp_last_active` com o limite.
  - `_wpStartIdleWatch()` registra atividade (`mousedown/keydown/scroll/touchstart/
    visibilitychange`) e checa a cada 30s; `_wpExpireSession()` faz auto-logout
    (limpa token, reabre o login com aviso "Sessão expirada por inatividade").
  - `bootstrap()` só restaura a sessão se `!_wpSessionExpired()`; senão limpa o token
    vencido e exige novo login (mantém o prefill do tenant por conveniência).
  - `logout()` também limpa `wp_last_active` e o timer.
  - Tudo frontend, baseado no token atual (dash_token). O login e-mail/senha completo
    segue no `PLANO_AUTH_EMAIL_SENHA.md` para depois.

**2026-05-31 — loader global mais visível (overlay):**
- A barrinha fina no topo (`.global-loader`, 3px) era pouco perceptível. Trocada por um
  **overlay de tela cheia** esmaecido + desfocado, com card central (spinner + "Carregando…"),
  temático (claro/escuro). Reaproveita os mesmos gatilhos `_showLoader`/`_hideLoader`
  (contador) chamados em `api()`/`apiFetch()` — nenhuma chamada precisou ser religada.
  - `wp-core.js`: `_showLoader` agora usa **delay de 300ms** (`setTimeout`) antes de
    revelar o overlay; `_hideLoader` cancela o timer. Assim chamadas rápidas (<300ms)
    não piscam o modal; só cargas mais longas mostram o overlay.
  - `workforce.html`: `#globalLoader` agora contém `.gl-box > .gl-spinner + .gl-text`
    (com `role="status"`/`aria-live`). `.hide` (com `!important`) continua escondendo.
  - `workforce.css`: `.global-loader` virou flex full-screen com backdrop temático
    (`body.theme-light` tem variante clara) + `.gl-spinner` (anel girando) + `.gl-box`.

**2026-05-31 — Configurações: paginação padrão 10 + seletor "por página":**
- As listas da aba Configurações (Regras Ativas, Aplicativos Detectados, Sites
  Detectados, Colaboradores) ficavam grandes. Padrão de página baixado para **10**
  (`_RULES/_APPS/_SITES/_COLLABS_PAGE_SIZE`, agora `let` e = 10).
- `_renderCfgPagination` (em `wp-settings.js`) ganhou um `<select>` "10/25/50/100 por
  página" — sempre visível quando há > 10 itens (mesmo em página única, para voltar a
  um tamanho menor). O setter é derivado do `goToFn` (`_xGoTo` → `_xSetSize`), então as
  6 chamadas existentes não mudaram. Setters novos: `_rulesSetSize/_appsSetSize/
  _sitesSetSize/_collabsSetSize`.
- **Colaboradores agora pagina** (antes mostrava todos): `loadCollabs`/`renderCollabsTable`
  passam por `_renderCollabsPage()` + `_collabsGoTo()`; `workforce.html` ganhou
  `#collabsCountLabel` + `#collabsPagination` no cabeçalho e `id="collabsSection"` no
  card (que o `editUser` já usava para rolar). Validado com `node --check`.
- **Estilo do dropdown**: o `<select class="cfg-page-size">` ficava com a aparência
  nativa do browser (destoava). Adicionada regra `.cfg-page-size` em `workforce.css`
  (fundo transparente, `border`/`border-radius` temáticos, seta SVG custom via
  `appearance:none`) para casar com os botões `.ghost`/`.ev-page-nav`. Removido o
  `style=""` inline do select no `wp-settings.js`.
