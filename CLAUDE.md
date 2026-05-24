# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Project Overview

WorkPulse is a SaaS employee-activity monitoring platform. A Rust agent runs on each monitored Windows/Linux workstation, batches window-focus/AFK events, and POSTs them to a FastAPI backend. Dashboards (plain HTML + Chart.js, no build step) read from the backend API.

**Live URLs:**
- API: `https://api.workpulse.com.br`
- Workforce dashboard: `https://workpulse.com.br/dashboard/workforce.html`
- Platform admin: `https://workpulse.com.br/adm/admin.html`

---

## ⚠️ CRITICAL: File Edit Safety

`workforce.html` (~2030 lines, 85 KB) and `main.py` (~1600 lines, 61 KB) are large enough to **trigger silent mid-file truncation** when using the Edit tool with large replacement blocks. The file appears saved but is cut off partway through.

**Always follow this workflow for any edit touching >30 lines of replacement text:**

```bash
# 1. Work on a temp copy
cp "/sessions/.../mnt/WORK PULSE/workpulse-deploy/server/main.py" /tmp/main_safe.py

# 2. Make changes in /tmp (Edit tool, or head+append for large blocks)
head -n <last_good_line> /tmp/main_safe.py > /tmp/main_final.py
cat >> /tmp/main_final.py << 'EOF'
...new content...
EOF

# 3. Validate before touching the real file
python3 -m py_compile /tmp/main_final.py && echo OK

# 4. Only then copy back
cp /tmp/main_final.py "/sessions/.../mnt/WORK PULSE/workpulse-deploy/server/main.py"
```

For HTML files use Python string replacement instead of Edit when changing multiple sections:
```python
with open('/tmp/wf_work.html', 'r') as f: src = f.read()
src = src.replace('OLD', 'NEW')
with open('/tmp/wf_work.html', 'w') as f: f.write(src)
```

**After every Edit, verify the file did not truncate:**
```bash
tail -5 <file>         # must end with }, </html>, serve(), etc.
wc -l <file>           # line count should not drop unexpectedly
python3 -m py_compile  # for .py files
```

---

## Validate / Run Locally

```bash
# Syntax check backend (no live server needed for validation)
python3 -m py_compile workpulse-deploy/server/main.py

# Run backend locally (requires env vars)
cd workpulse-deploy/server
WORKPULSE_ADMIN_TOKEN=$(openssl rand -hex 32) \
WORKPULSE_DB=/tmp/wp_test.db \
uvicorn main:app --host 0.0.0.0 --port 8080 --reload

# Health check
curl http://localhost:8080/healthz

# HTML files have no build step — open directly in browser or serve with:
python3 -m http.server 8081 --directory workpulse-deploy/site
```

There are no automated tests. Validation = `py_compile` + manual curl/browser check.

---

## Deploy to Production (AWS)

All commands run in **AWS CloudShell** (`us-east-1`), inside the `workpulse-deploy/` folder.

```bash
# After changing server/main.py — rebuilds Docker image via CodeBuild, rolls ECS
bash scripts/redeploy.sh          # ~5 min, brief downtime during swap

# After changing any site HTML
bash scripts/3-publicar-site.sh   # uploads to S3, invalidates CloudFront

# Full infrastructure redeploy (rarely needed)
bash scripts/2-deploy.sh
```

**Infrastructure:** Single ECS Fargate task (1 instance) → SQLite on EFS. Never run multiple replicas — SQLite does not support concurrent writers. The EFS volume at `/data/workpulse.db` is the only stateful resource.

---

## Backend Architecture (`server/main.py`)

Single-file FastAPI app. No routers — all endpoints defined inline.

### Authentication (two levels)

| Decorator | Who can call | How |
|---|---|---|
| `Depends(require_admin)` | Platform admin only | `X-Admin-Token: <WORKPULSE_ADMIN_TOKEN>` header |
| `Depends(require_tenant_scope)` | Admin OR the tenant's own dash_token | `X-Admin-Token: <either token>` + `?tenant=<id>` |
| Bearer token (inline) | Rust agents | `Authorization: Bearer <license_token>` |

`require_tenant_scope` accepts either the master admin token **or** the per-tenant `dash_token` (stored in `tenants.dash_token`). This lets each client company access only its own data.

### In-memory TTL cache

```python
_CACHE: dict[str, tuple[float, Any]] = {}   # key → (monotonic_ts, value)
_CACHE_TTL = 90  # seconds
```

Cache keys are namespaced `"{tenant}:{endpoint}:{params}"`. Call `_cache_bust(tenant)` after any write (ingest, PATCH, DELETE) to invalidate. Heavy endpoints (`workforce_overview`, `team_summary`) check/set cache. Do not cache admin-only endpoints.

### Database patterns

Always use the `db()` context manager — it opens/closes connection and sets WAL mode:
```python
with db() as c:
    rows = c.execute("SELECT ...", (param,)).fetchall()
    return [dict(r) for r in rows]
```

**Schema migrations** go in `init_db()` with `PRAGMA table_info` guards:
```python
cols = [r["name"] for r in c.execute("PRAGMA table_info(table_name)").fetchall()]
if "new_col" not in cols:
    c.execute("ALTER TABLE table_name ADD COLUMN new_col TYPE DEFAULT val")
```

New tables also go into the `SCHEMA` constant (for fresh installs) **and** as `CREATE TABLE IF NOT EXISTS` in `init_db()` (for existing databases).

### Adding a new endpoint — checklist

1. Add Pydantic model if needed (after `DepartmentCreate`)
2. Add route function with correct `Depends()`
3. Call `_cache_bust(tenant)` if the endpoint writes data
4. Call `audit(actor, action, target, detail, ip)` for any destructive or sensitive operation
5. Place before `# Entry point` section
6. Validate with `python3 -m py_compile`

### Background worker

`_retention_worker()` runs as an asyncio task started in `lifespan`. It sleeps 24 h between runs. Archives old events to `daily_summaries` then deletes them, per-tenant retention policy (`tenants.retention_days`, default 90).

---

## Database Schema (all tables)

| Table | Purpose |
|---|---|
| `tenants` | One row per client company. Has `dash_token`, `retention_days` |
| `licenses` | Ed25519-signed tokens issued to agents. `revoked` flag |
| `users` | One row per `(tenant_id, user_id)`. `user_id` = hostname. Has `display_name`, `login_user`, `department`, `role` |
| `devices` | One row per device (hostname + OS metadata) |
| `events` | Raw activity events. `bucket` ∈ `{window, afk}`. `data_json` holds `{app, title}` or `{status}` |
| `productivity_rules` | Pattern-matching rules per tenant. Evaluated in `created_at` order, first match wins |
| `tenant_departments` | Department names per tenant. UNIQUE(tenant_id, name) |
| `daily_summaries` | Archived aggregates for events older than retention cutoff |
| `audit_log` | Admin action audit trail |

**Key indexes:** `(tenant_id, bucket, ts)` is the hot path for most dashboard queries.

**user_id convention:** Since Session 7 — stored as bare hostname (e.g., `DESKTOP-ABC`). Legacy records may have `TENANTID:HOSTNAME` format; `cleanUserId()` in the frontend strips the prefix client-side.

---

## Complete API Reference

### Agent endpoints (Bearer token auth)
| Method | Path | Description |
|---|---|---|
| POST | `/v1/ingest` | Push event batch. Body: `IngestPayload`. Headers: `Authorization: Bearer <lic>`, `X-Tenant`, `X-Device` |
| POST | `/v1/license/heartbeat` | Keep-alive ping |

### Tenant-scoped endpoints (`require_tenant_scope`, `?tenant=<id>`)
| Method | Path | Description |
|---|---|---|
| GET | `/v1/users` | List users with device count |
| GET | `/v1/users/{user_id}/summary` | Per-user breakdown. `?days=` |
| PATCH | `/v1/users/{user_id}` | Update display_name, email, department, role. Body: `UserUpdate` |
| GET | `/v1/team/summary` | Team stats for Overview tab. `?days=` |
| GET | `/v1/events` | Paginated raw events. `?days=&bucket=&user_id=&limit=` |
| GET | `/v1/productivity/events_summary` | KPIs + daily breakdown with categories |
| GET | `/v1/workforce/overview` | Main dashboard aggregate (cached 90 s) |
| GET | `/v1/workforce/timeseries` | Day-by-day series. `?days=&bucket=` |
| GET | `/v1/productivity/rules` | List rules |
| POST | `/v1/productivity/rules` | Create rule. Body: `ProductivityRuleCreate` |
| DELETE | `/v1/productivity/rules/{id}` | Delete rule |
| GET | `/v1/productivity/internet` | Browser usage aggregated. `?days=&user_id=` |
| GET | `/v1/productivity/apps` | All apps with category. `?days=` |
| GET | `/v1/departments` | List departments with user lists |
| POST | `/v1/departments` | Create department. Body: `{name}` |
| DELETE | `/v1/departments/{dept_name}` | Delete dept + clear users' department field |

### Admin-only endpoints (`require_admin`)
| Method | Path | Description |
|---|---|---|
| POST | `/v1/license/issue` | Issue license + create tenant |
| POST | `/v1/license/revoke` | Revoke license |
| POST | `/v1/admin/tenant-token` | Generate/rotate a tenant's dash_token |
| GET | `/v1/admin/tenants` | All tenants with user/license counts + `retention_days` |
| GET | `/v1/admin/licenses` | All licenses |
| GET | `/v1/admin/stats` | System-wide stats + DB size |
| GET | `/v1/admin/audit` | Audit log |
| PUT | `/v1/admin/tenants/{tenant_id}/retention` | Set retention policy. `?days=` |

### Public
| Method | Path | Description |
|---|---|---|
| GET | `/healthz` | Liveness probe |
| GET | `/v1/license/pubkey` | Ed25519 public key (agents verify locally) |

---

## Frontend Architecture

### Files
| File | Purpose |
|---|---|
| `site/dashboard/workforce.html` | Main client dashboard (~2030 lines). Self-contained: HTML + CSS + JS in one file |
| `site/adm/admin.html` | Platform admin panel (~1118 lines). Same pattern |
| `site/index.html` | Marketing landing page |
| `site/dashboard/index.html` | Redirect / alternate entry |

No bundler, no npm, no framework. Each HTML file is fully self-contained. External deps loaded from CDN:
- `chart.js@4.4.4` (jsdelivr) — all charts

### workforce.html — Global state

```javascript
const state = {
  url:    localStorage.getItem('wp_url')    || 'https://api.workpulse.com.br',
  tenant: localStorage.getItem('wp_tenant') || '',
  token:  localStorage.getItem('wp_token')  || '',
  charts: {},        // Chart.js instances keyed by canvas ID
  people: [],        // cached from loadPeople()
  lastOverview: null,// cached from loadOverview() — reused by loadDepartments/loadAlerts
  collabs: [],       // cached from loadCollabs()
};
```

### workforce.html — Tab system

Tabs: `overview | people | departments | alerts | trends | events | internet | config`

`switchTab(name)` → calls `_cancelPending()` (aborts in-flight requests) → hides all sections → shows `#tab-{name}` → calls the corresponding loader.

Each tab section is `<section id="tab-{name}" class="hide">`. Active tab button gets `.active` class.

### workforce.html — API call pattern

```javascript
// Use api() for GET requests — handles 30s timeout + AbortController for tab switches
const data = await api(`/v1/endpoint?tenant=${encodeURIComponent(state.tenant)}&days=${days}`, signal);

// Use apiFetch() for POST/PATCH/DELETE with JSON body
await apiFetch('PATCH', `/v1/users/${uid}?tenant=${encodeURIComponent(state.tenant)}`, { display_name: 'Name' });
```

Never call `fetch()` directly — always go through `api()` or `apiFetch()`.

### workforce.html — Chart.js pattern

Always destroy before recreating to avoid canvas reuse errors:
```javascript
if (state.charts.myChart) state.charts.myChart.destroy();
state.charts.myChart = new Chart(document.getElementById('myCanvas').getContext('2d'), { ... });
```

### workforce.html — User display

- `cleanUserId(uid)` — strips legacy `TENANTID:HOSTNAME` prefix
- `displayName(u)` — returns `u.display_name || cleanUserId(u.user_id) || '—'`
- `avatar(name, large)` — renders initials chip

### Productivity categories

Four categories: `productive | unproductive | neutral | uncategorized`

```javascript
const CAT_PILLS = {
  productive:    '<span class="pill prod">✅ Produtivo</span>',
  unproductive:  '<span class="pill unprod">❌ Não-produtivo</span>',
  neutral:       '<span class="pill neutral">⚪ Neutro</span>',
  uncategorized: '<span class="pill uncat">🟡 Sem categoria</span>',
};
const CAT_COLORS = {
  productive: '#4ade80', unproductive: '#f87171',
  neutral: '#94a3b8',    uncategorized: '#fbbf24',
};
```

### CSS design system

Dark theme only. All colors via CSS variables:

| Variable | Role |
|---|---|
| `--bg`, `--bg-2` | Page background, slightly lighter |
| `--panel`, `--panel-soft` | Card backgrounds |
| `--border`, `--border-soft` | Borders |
| `--text`, `--text-dim`, `--text-muted` | Three text intensities |
| `--primary` | `#6c8cff` — main blue/indigo accent |
| `--accent` | `#34d399` — green (online/OK) |
| `--warn` | `#f59e0b` — amber |
| `--danger` | `#ef4444` — red |

Button classes: `.primary` (filled blue), `.ghost` (outlined), `.cat-btn` (small teal), `.del-btn` (small red outline).

Grid: 12-column `.grid` with `.span-{3,4,5,6,7,8,9,12}`.

### admin.html — Structure

Same single-file pattern as workforce.html. Has its own `state = { token, url, tenants, licenses, stats, audit }`. Tabs: `overview | tenants | licenses | audit`. API calls use `api(path, opts)` — note different signature from workforce: `api(path, { method, body })`.

---

## Config Tab — Sections in workforce.html

The Config tab (`#tab-config`) contains, in order:
1. **Nova Regra** — productivity rule form (label, field, pattern, type, category)
2. **Regras Ativas** — table of existing rules (`#rulesTable`)
3. **Aplicativos e Sites Detectados** — chip grid for quick-add (`#appsList`)
4. **Departamentos** — create/delete depts, assign users per dept (`#deptsSection`, `#deptsList`)
5. **Colaboradores** — per-device name/email/dept/role editor (`#collabsSection`, `#collabsTable`)

When adding new Config sub-sections, insert in this order and update `loadConfig()`:
```javascript
async function loadConfig() {
  await Promise.all([loadRules(), loadAppsForConfig(), loadCollabs(), loadDepts()]);
}
```

---

## Productivity Rule Engine (backend)

`_apply_rules(site_name, app, rules)` — evaluates rules in `created_at` ASC order, returns first match or `"uncategorized"`.

`_is_browser(app)` — matches against `_BROWSER_APPS` set + `_BROWSER_KEYWORDS` tuple.

`_clean_title(title)` — strips browser name suffix from window title using `_BROWSER_SUFFIX_RE`.

For browser events: `match_field="title"` matches against the cleaned site name extracted from the window title. For non-browser: `match_field="app"` matches against the executable name.

---

## Rust Agent Integration

The agent (not in this repo) POSTs to `/v1/ingest` with:
```json
{
  "device": "HOSTNAME",
  "os": "windows",
  "os_version": "11",
  "agent_version": "1.x.x",
  "login_user": "joão.silva",
  "events": [
    { "bucket": "window", "timestamp": "...", "duration": 30.0,
      "data": { "app": "chrome.exe", "title": "GitHub - Chrome" } },
    { "bucket": "afk",    "timestamp": "...", "duration": 120.0,
      "data": { "status": "afk" } }
  ]
}
```

`login_user` comes from `std::env::var("USERNAME").or_else(|_| std::env::var("USER"))`. The backend auto-sets `display_name` from `login_user` if `display_name` is currently NULL.

Headers: `Authorization: Bearer <license_token>`, `X-Tenant: <tenant_id>`, `X-Device: <hostname>`.

---

## Infra Quick Reference

- **ECS Cluster:** `workpulse-prod` / Service: `workpulse-prod-server`
- **SQLite path in container:** `/data/workpulse.db` (EFS mount)
- **Logs:** CloudWatch → `/aws/ecs/workpulse-prod-server`
- **Image build:** CodeBuild project `workpulse-prod-image-build`
- **Admin token:** `cd infra && terraform output -raw admin_token`
- **Single instance only** — SQLite cannot handle concurrent writes; never scale above 1 task

To migrate to PostgreSQL later, the schema is compatible. The `db()` context manager is the only place to change.
