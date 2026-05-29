# CLAUDE.md — WorkPulse

Guidance for AI agents (Claude Code / Cowork) working on WorkPulse.
**Read this fully before changing anything.** Last updated: 2026-05-29.

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
    wp-reports.js         Relatórios automáticos (CRUD, envio SES, histórico)
    wp-nav.js             Navegação por tabs + dropdowns (switchTab, TABS)
    wp-auth.js            Login com validação API, logout, tema, bootstrap IIFE
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

**Cache busting:** versão atual do site é `?v=15` (em todos os `<script>` e `<link>`
de `workforce.html`). Incrementar a cada deploy que muda JS/CSS.

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

Separate crate. Built per-client with an embedded license.
- **Build (Windows):** `windows/build-msi.ps1 -LicenseKey "<token>" -ClientName "X" -Version "2.1.0"`
  → runs `cargo build --release` then WiX. Output in `dist/`.
- **Build (Linux):** `VERSION="2.1.0" LICENSE_KEY="<token>" ./linux/build-deb.sh`
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
