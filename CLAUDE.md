# CLAUDE.md — WorkPulse

Guidance for AI agents (Claude Code / Cowork) working on WorkPulse.
**Read this fully before changing anything.** Last updated: 2026-07-24.

> **Para o estado atual (auth e-mail/senha, papéis, Localização, Administrativo,
> Financeiro, SMTP por tenant, motor de Alertas, etc.), leia primeiro a seção
> "## Estado atual (2026-06-21)" no FINAL deste arquivo.** As seções estruturais
> abaixo (Auth, Schema, API, Workers) foram atualizadas, mas o changelog detalhado
> de cada época fica no rodapé.

---

## 🗂 Índice rápido (Quick Reference)

**Módulos & caminhos** (raiz do repo = `workpulse-deploy/`):

| Módulo | Caminho | Stack |
|---|---|---|
| Backend / API | `server/main.py` | FastAPI single-file (~7,1k linhas), Postgres 15 (RDS), ECS Fargate |
| Agente | `../workpulse-agent` | Rust — **versão atual 2.3.6** |
| Service / updater (watchdog + auto-update) | `../workpulse-service` | Rust (lê o MESMO `agent.conf`) |
| Dashboard cliente | `site/dashboard/` (`workforce.html` + `js/wp-*.js`) | HTML + Chart.js + Leaflet, sem build |
| Console admin (plataforma) | `site/adm/` (`admin.html` + `js/admin.js`) | idem |
| Site institucional | `site/` (`index.html`) | estático |
| Infra | `infra/` (`main.tf`, `variables.tf`, `terraform.tfvars`) | Terraform (Route53/ACM/CloudFront/ALB/ECS/RDS) |

**Deploy** (o usuário roda os `.ps1` no PowerShell — NUNCA os `.sh`):

| O quê | Comando |
|---|---|
| Backend (container) | `scripts/redeploy.ps1` |
| Site (S3+CloudFront) | `scripts/publicar-site.ps1` |
| Backend + site | `scripts/redeploy-publicar.ps1` |
| Infra | `scripts/apply-infra.ps1` (terraform) |
| Agente (MSI/.deb) | `workpulse-agent/windows/build-msi.ps1 -Version X.Y.Z {-LicenseKey|-EnrollCode}` · `linux/build-deb.sh` |

ECS tem **deployment circuit breaker**: deploy que sobe quebrado faz **rollback** sozinho (se "subi e não pegou", checar se a task nova ficou RUNNING).

**Gotchas recorrentes** (já custaram tempo — leia CRITICAL #1 abaixo):
- **Mirror do bash trunca arquivos grandes**: `py_compile`/`node --check` do arquivo INTEIRO falha no FIM (linha ~7xxx) = **falso alarme**. Validar por SLICE (extrair a função e compilar) ou `node --check` (arquivos pequenos ok). Fonte da verdade = ferramentas Read/Edit, não o bash.
- **OneDrive**: a pasta é a BASE local do notebook; não baixa do OneDrive, só ENVIA p/ bkp. Não discutir cache de OneDrive. `rm` via bash falha "Operation not permitted" → usar `allow_cowork_file_delete`.
- **Sem toolchain no sandbox**: sem `cargo`/`pydantic`/`psql`. Rust/Python compilam na máquina do usuário. Validar por slice/lógica isolada; confirmar APIs de crate na doc (docs.rs) antes de usar.
- **Cache-busting do dashboard**: cada `js/wp-*.js` tem `?v=N` em `workforce.html` (fonte da verdade das versões). Bump ao alterar um JS.

**Índice de features/incidentes** (detalhe no changelog ao final; mais recente primeiro):
- 2026-07-25 — **Organograma: animação ao expandir/recolher liderados.**
  Antes, `_ocToggle()` só marcava/desmarcava o id em `_oc.expanded` e chamava `_ocRender()`,
  que reconstrói o `innerHTML` da árvore INTEIRA do zero — por isso os cards apareciam/
  sumiam secos, de uma vez, sem transição possível (nó destruído e recriado não anima).
  Fix: `_ocToggle()` agora faz a operação **cirúrgica**, só no `<li>` daquele card:
  ao expandir, insere apenas o `<ul>` de filhos daquele nó (via `_ocChildrenBlock`, já
  existia) e anima cada `<li>` filho com a Web Animations API (`fade + translateY + scale`,
  com stagger de 45ms entre um card e outro -- efeito de "cascata"); ao recolher, anima o
  `<ul>` inteiro encolhendo/desaparecendo e só remove do DOM no `onfinish`. O resto da árvore
  não é tocado (preserva scroll/pan e evita repaginar nós não relacionados). Ícone
  ▸/▾ virou só `▸` que **rotaciona via CSS** (`.oc-toggle.expanded .oc-toggle-arrow`) em vez
  de trocar de caractere -- mais suave. Respeita `prefers-reduced-motion` (pula a animação,
  insere/remove direto). Ações em massa (Expandir/Recolher tudo, filtro de níveis, busca)
  continuam usando `_ocRender()` completo -- não valia a pena animar dezenas de nós de uma
  vez. **Só front, precisa `publicar-site.ps1`.**
- 2026-07-24 — **Organograma: causa real da lentidão encontrada e corrigida (25,7 MB!).**
  O Caio mandou o print da aba Rede do DevTools: `/v1/org-chart` sozinho vinha com **25,7 MB**
  e levava **32,4s** -- muito maior que qualquer outra chamada da página. Causa: fotos
  importadas com `store_copy=true` (`POST /v1/tenant-directory/photos`) ficam guardadas como
  **data URI** (base64) em `users.photo_url`/`directory_people.photo_url`, cada uma
  150-260KB; com ~90 pessoas com foto, o organograma embutia TODAS no mesmo JSON. Fix:
  (1) novo endpoint **`GET /v1/tenant-directory/photo?tenant=&cid=`** que decodifica o data
  URI e serve a imagem como um arquivo de verdade (`Content-Type` certo, `Cache-Control:
  public, max-age=86400` -- o navegador baixa 1x só e reusa). (2) `org_chart()` agora manda
  **essa URL de referência** no lugar do data URI bruto sempre que o valor guardado começa
  com `data:` (link externo -- Convenia/S3 -- continua passando direto, já é leve). (3)
  Frontend: 3 linhas em `loadOrgChart()` prefixam o path relativo (`/v1/tenant-directory/
  photo?...`) com `state.url` (a API e o dashboard são domínios diferentes) -- resto do
  código de foto (`_ocPhotoHtml` etc.) não mudou, já tratava qualquer URL igual. Resultado
  esperado: JSON do organograma cai de MB pra poucos KB; as fotos viram ~90 requests
  paralelos/cacheáveis, carregados sob demanda (`loading="lazy"` já existia no `<img>`).
  **Precisa `redeploy.ps1` (endpoint novo) + `publicar-site.ps1`.**
- 2026-07-24 — **Organograma v6: revertida a quebra em fileiras — fileira única sempre.**
  Testando a v5 (fileiras de até 4) com os 10 liderados reais do CEO, o Caio reportou: (1)
  3 fileiras (4+4+2) ficou ruim visualmente — pediu algo como 5+5 pra esse caso; (2) a linha
  de conexão ENTRE fileiras aparecia quebrada; (3) ao expandir um card no MEIO de uma
  fileira (ex.: "Fabio Shinji Hara", 2º de 4), a fileira de baixo não repaginava e ficava
  com cards "ligados no nada". Isso é a SEGUNDA abordagem de quebra em múltiplas linhas que
  quebra na prática (a primeira, em v2, era um modo de grade CSS que comprimia os cards).
  Diagnosticar/corrigir esse tipo de bug de reflow/posicionamento com confiança exige testar
  em navegador de verdade (não dá pra garantir 100% só por leitura de código+screenshot) —
  o Caio topou "melhor funcionar que ficar todo estragado" e voltar pra fileira única sempre
  caso não desse pra deixar 100%. **Decisão: fileira única sempre**, removido todo o código
  de `.oc-rows`/chunking (`_ocChildrenBlock` voltou a ser uma função de ~5 linhas). Times
  grandes (ex.: 10 liderados) ficam numa fileira larga só — navega com o pan (arrastar),
  zoom (−/+) e a rolagem horizontal, todos já existentes. Se quiser tentar de novo o layout
  em múltiplas linhas no futuro, fazer com acesso a um navegador real pra testar o reflow
  ao expandir/recolher em cada posição da fileira antes de considerar pronto. **Sem mudança
  de backend; só `publicar-site.ps1`.**
- 2026-07-24 — **Organograma v5: bug do botão expandir/recolher, fileiras de até 4, diagnóstico
  de lentidão.** Três ajustes depois de testar com 113 colaboradores reais:
  (1) **BUG real corrigido** -- o pan (v4) chamava `setPointerCapture` em TODO `pointerdown`,
  inclusive um clique comum, e isso quebrava o `click` delegado nos cards (o botão "N
  liderados" e o clique pra abrir a ficha pararam de funcionar). Fix: só captura o ponteiro
  DEPOIS de confirmar que é de fato um arraste (`moved===true`, dentro do `pointermove`) --
  um clique normal (pointerdown+pointerup sem passar o limiar de 5px) nunca toca a captura,
  então o clique chega normal no elemento. (2) **Fileiras de até 4**: nó com mais de 4
  liderados agora quebra em várias fileiras de até 4 (`OC_ROW_MAX`) -- 6→4+2, 7→4+3, 8→4+4,
  etc. -- em vez de uma fileira única e comprida. Cada fileira reaproveita a MESMA `<ul
  class="oc-tree">` que já funciona bem sozinha (mesmos conectores, sem grade/compressão
  nova); elas só empilham verticalmente (`.oc-rows{display:flex;flex-direction:column;
  align-items:center}`) e o traço de entrada de cada fileira (regra já existente
  `.oc-tree ul::before`) dá a impressão de tronco contínuo. (3) **Diagnóstico de lentidão
  (~30s pra abrir)**: confirmado no código que `GET /v1/org-chart` NÃO chama a Convenia --
  só lê `directory_people`/`users` local (cache de 300s). Adicionada instrumentação `_Perf`
  no endpoint (mesmo padrão do resto do backend) pra medir escopo/query/build separadamente;
  os logs `[perf] org_chart ...` aparecem no CloudWatch (`/aws/ecs/workpulse-prod-server`)
  depois do redeploy. Hipótese mais provável (não confirmada): contenção do pool de conexões
  (`ThreadedConnectionPool` max 10) sob carga de ingestão dos agentes -- o mesmo gargalo já
  documentado na seção "Cache em escala" acima, só que afetando esta tela também. **Precisa
  `redeploy.ps1` (perf log no backend) + `publicar-site.ps1` (JS/CSS).**
- 2026-07-24 — **Organograma v4: clicar-e-arrastar pra navegar (pan).** `#ocScroll` (novo id no
  container `.oc-scroll`) ganhou Pointer Events (`_ocWirePan` em `wp-orgchart.js`): mousedown/
  touch inicia arraste, `setPointerCapture` mantém o gesto mesmo se o ponteiro sair da área,
  `scrollLeft/scrollTop` seguem o movimento 1:1. Distingue clique de arraste por um limiar de
  5px (`THRESH`) — abaixo disso ainda conta como clique normal no card/chevron; acima disso,
  suprime o click seguinte (`_ocSuppressClick`) pra não abrir a ficha nem expandir sem querer
  ao soltar. Cursor `grab`/`grabbing` (classe `.oc-panning`). Barra de rolagem nativa continua
  funcionando em paralelo (scroll do mouse/trackpad, arrastar a barra) — o drag é um jeito A
  MAIS de navegar, não substitui. **Sem mudança de backend; só `publicar-site.ps1`.**
- 2026-07-24 — **Organograma v3: corrige sobreposição de cards + raiz órfã do CEO.** Testado
  com dados reais (113 colaboradores) e o Caio reportou dois problemas da v2:
  (1) **Cards sobrepostos** -- o modo "grade" (cluster) pra times com >4 liderados comprimia
  os cards uns nos outros. Removido; voltou pra árvore clássica em fileira única SEMPRE
  (`flex-shrink:0` nos cards garante que nunca comprimem) -- ao expandir, os cards empurram
  os irmãos pro lado e o canvas cresce, com rolagem horizontal (`.oc-scroll{overflow:auto}`).
  (2) **Nó "perdido" do lado do CEO** -- gente sem gestor cadastrado na Convenia (erro do RH,
  não é o topo real da empresa) virava uma raiz separada flutuando ao lado do CEO de verdade.
  Agora existe uma **raiz "primária"** (nível 0): escolhida pelo cargo (regex pega CEO/
  Presidente/Fundador/Founder/Owner/Diretor(a) Presidente/Diretor(a) Geral) ou, na falta de
  título reconhecível, por quem tem a maior estrutura abaixo (mais gente, direta+indireta).
  Ela ganha o selo "Nível 0" no card. Checkbox **"Ocultar sem gestor"** no toolbar (ligado por
  padrão) esconde as OUTRAS raízes (prováveis falhas de cadastro); desligando, elas aparecem
  com o selo "⚠ Sem gestor" pra facilitar auditoria. Buscar por alguém que só existe numa
  raiz oculta desliga o filtro automaticamente e revela o caminho até ele. Nota no topo da
  página conta quantos estão ocultos pelo filtro. **Sem mudança de backend nesta rodada** --
  só `js/wp-orgchart.js` e `css/workforce.css`; precisa só `publicar-site.ps1`.
- 2026-07-24 — **Organograma v2: roster completo da Convenia (115) + ícone de agente + layout
  em grade.** Ajustes pedidos pelo Caio depois da v1:
  (1) **Nova tabela `directory_people`** -- `_directory_upsert` agora grava o roster INTEIRO
  vindo da Convenia (upsert por `convenia_id`), não só quem já é `users` por e-mail. Sync
  continua 1x/dia (worker existente) + botão "Sincronizar agora". (2) `GET /v1/org-chart`
  reescrito p/ ler `directory_people` LEFT JOIN `users` (por `convenia_id`) -- devolve todo
  mundo (hoje 115), com `has_agent` (true se casou com um `users`). (3) `POST
  /v1/tenant-directory/photos` agora também casa/grava foto em `directory_people` (antes só
  em `users`), cobrindo quem não tem agente. (4) **Frontend:** ícone (bolinha verde/vermelha
  no canto da foto) indicando se a pessoa tem agente instalado; clique em quem NÃO tem agente
  abre um resumo local (sem fetch, já que não há Ficha completa); clique em quem TEM agente
  continua abrindo a Ficha normal. (5) **Bug de CSS corrigido:** a regra que devia desenhar a
  linha vertical entre o card do gestor e a linha de filhos estava sendo anulada por outra
  regra (`.oc-tree > li > ul::before{display:none}`) -- removida; a árvore ficava "flutuando"
  sem conexão visual, isso que deixava o layout "pobre". (6) **Layout em grade para times
  grandes:** nó com mais de 4 liderados (`OC_CLUSTER_THRESHOLD`) vira uma "caixa de time" com
  grade de 3/4/5 colunas (ex.: 6 liderados = 2 linhas de 3) em vez de uma fileira única que
  estourava a largura; cards dentro da grade ficam menores (`.oc-card.compact`) e o
  espaçamento horizontal da árvore em geral foi reduzido. (7) **Navegação retrátil por
  padrão:** "Níveis exibidos" agora abre com 2 (só raiz + 1º nível) em vez de 3, pra não
  abrir tudo de uma vez; expandir further é sempre por clique no chevron de cada nó. (8)
  Polimento visual: fundo em pontilhado sutil no canvas, hover com elevação no card, pill de
  departamento com cor translúcida (`color-mix`), legenda com bolinhas de "com agente"/"sem
  agente". **Precisa `redeploy.ps1` (nova tabela + endpoints) + `publicar-site.ps1`.**
- 2026-07-24 — **Aba Análises → "Organograma"** (`tab-organograma`, `js/wp-orgchart.js`) +
  endpoint `GET /v1/org-chart`. Árvore hierárquica interativa (foto/iniciais, nome, cargo,
  departamento com cor por depto, legenda) montada casando `manager` (texto vindo da
  Convenia) com `display_name` de outro colaborador do mesmo tenant. Interação: clique na
  foto/nome abre a Ficha do Colaborador (reusa `openProfileModal`); clique no chevron
  expande/recolhe os liderados daquele nó (estado independente por nó, não é acordeão);
  busca por nome/cargo/depto expande o caminho até os resultados e centraliza o primeiro
  match; controle "Níveis exibidos" (2/3/4/Todos) recalcula a profundidade padrão; zoom
  +/−/reset via `transform:scale` no canvas; "Expandir tudo"/"Recolher tudo". Layout usa o
  padrão CSS clássico de organograma via `<ul>/<li>` (conectores por `::before/::after`,
  robusto a qualquer número de irmãos/largura de card) — sem lib externa. **Regra de raiz**
  (decidida com o Caio 2026-07-24): a Convenia é fonte de verdade do campo Gestor; quem tem
  gestor vinculado vira filho dele; quem NÃO tem gestor (vazio ou sem correspondência) só
  aparece como raiz se tiver ≥1 liderado — sem nenhuma relação (nem gestor nem liderado),
  fica de fora do organograma (nota "`N` não exibidos" no topo da página explica isso).
  Escopo de visibilidade igual às demais abas de Análises (`_scope_for` — Líder vê só o
  próprio ramo). **Sem migração de schema** (reusa `users.manager/photo_url/cargo/team/
  department` já existentes). **Precisa `redeploy.ps1` (backend, novo endpoint) +
  `publicar-site.ps1` (frontend).**
- 2026-07-24 — **Ficha do Colaborador (modal) + Fonte de dados de RH (Convenia)**. (1) Modal
  reutilizável `openProfileModal(userId,name)` — `js/wp-profile-modal.js` + CSS `.wp-modal-*`;
  mostra Foto, Nome, E-mail, Máquina, Gestor, Departamento; loading assíncrono, cache de sessão,
  fallback client-side; ligado no menu `openPersonMenu` ("👤 Ver ficha"). (2) Endpoint
  `GET /v1/collaborator/profile`; colunas `users.manager` e `users.photo_url` (ALTER idempotente).
  (3) Integração RH por tenant: tabela `directory_config` (provider convenia|google|none,
  convenia_token, auto_sync, last_sync/status); endpoints `GET/PUT /v1/tenant-directory/config` +
  `POST /v1/tenant-directory/sync` (todos `require_role("admin_master")`); adapter Convenia
  **`GET /api/v3/employees`** (header `token`, stdlib urllib, base `WORKPULSE_CONVENIA_BASE`),
  upsert por e-mail; **worker diário** `_directory_sync_worker` (carga noturna, `auto_sync`).
  Google fica como "em breve". Guia: `../DOCS/FICHA_COLABORADOR_SYNC.md`. **Precisa `redeploy.ps1`
  (backend) + `publicar-site.ps1`.** Campos Convenia confirmados no histórico da API v3: gestor=
  `supervisor{name,last_name}`, depto=`department{name}`, e-mail=`email`; envelope Laravel
  (`next_page_url`). **Convenia NÃO expõe foto via API**, MAS a foto tem link público no S3
  (`storage-api.rh.live.convenia.com.br/<UUID-empresa>/Colaboradores/<id-API>/Foto de perfil/photo_*.png`).
  **Confirmado (JSON real, 115 colab.):** `id` da API É o UUID da pasta da foto; **sem campo de
  foto**; envelope Laravel, todos numa página. Campos: `id`, `name`, `last_name`, `email`,
  `supervisor{name,last_name}`, `department{name}`. **Vínculo = e-mail** (`users.email` = email prof).
  **Fotos:** S3 nega list anônimo (`AccessDenied`) → não dá p/ montar o link pela API. Extração é pelo
  **painel web** (`app.convenia.com.br/colaboradores`, rolar até o fim, coletar `<img>` do bucket) —
  ver **`../DOCS/CONVENIA_EXTRAIR_FOTOS.md`** (script de console pronto). Sync grava `users.convenia_id`;
  `POST /v1/tenant-directory/photos` extrai o UUID de `Colaboradores/<uuid>/` de cada link e casa por
  `convenia_id` → **cola a lista de links** (arquivo `../convenia_fotos_links.txt`, 115 gerados em 2026-07-24).
  `store_copy` baixa data-URI. Diag: `GET /v1/tenant-directory/convenia-sample`.

  ⚠️ **403 "Forbidden" no sync (funciona no Postman, falha no servidor):** o urllib manda UA
  `Python-urllib/…` que o WAF da Convenia bloqueia. `_convenia_get` agora envia `User-Agent`
  explícito e, em HTTPError, inclui o corpo da resposta na msg (diag de IP/permissão). Campo "UUID da
  empresa" foi **removido** da tela (era p/ o S3-list, que não funciona); `_convenia_resolve_photo`
  ficou órfã (mantida, mas não chamada — sync não faz mais 115 requests S3 inúteis).
  **Estado atual (2026-07-24):** aguardando `redeploy.ps1` p/ o fix do UA; depois sync → importar fotos.
- 2026-07-24 — **Campos Cargo + Time na ficha.** Convenia `job{name}`=Cargo, `team{name}`=Time (Time =
  subdivisão do departamento; confirmado no JSON de exemplo da doc). Colunas `users.cargo`/`users.team`;
  mapeados no fetch + upsert; retornados em `/collaborator/profile`. Modal: **Cargo aparece abaixo da
  foto** (no lugar do departamento); corpo lista Gestor, Departamento, Time. ⚠️ Precisa **adicionar
  Cargo e Time aos campos do token** na Convenia (senão a API não devolve `job`/`team`) e re-sincronizar.
- 2026-07-24 — **Renome**: aba Administrativo "Licenciamento" (`tab-admlicenca`) → **"Configurações"**
  (id interno mantido); menu da conta "Configurações" → **"Ajustes"** (`tab-config`, título tb).
  A nova página Configurações recebeu o card **Fonte de dados (RH)** (Admin Master).
- 2026-07-24 — **Aba Análises → "Tempo Ativo"** (`tab-tempoativo`, `js/wp-tempoativo.js`): todos os
  colaboradores no período, busca, ordenação (maior/menor ativo e AFK), **multi-select de
  departamento**, KPIs (ativo total/médio, AFK média, aderência média) e gráfico horizontal
  clicável (abre a ficha). Entrada pelo card "Tempo ativo por colaborador" da Visão Geral (título
  clicável + botão "Veja mais"). Rótulo 🏖 Férias no gráfico. Tooltip em hh:mm (`fmtHM`).
- 2026-07-24 — **Perf Categorização**: apps (30d) e sites (14d) detectados agora têm **cache**
  (`_appsLoaded`/`_sitesLoaded` em wp-settings.js) — não refazem a agregação pesada a cada
  entrada na aba / `switchTab`; refetch só no botão ↻ ou ao trocar "só não categorizados".
- 2026-07-24 — **Gestão de Departamentos (UI)**: membros viraram lista vertical compacta de
  **altura fixa** (rolável) com filtro sempre visível; cards L/R do mesmo tamanho
  (`align-items:stretch` + `margin-top:auto` no bloco de adicionar). Visão Geral: card
  "Distribuição por departamento" agora mostra **média h/pessoa** por linha.
- 2026-07-16 — Apelido (alias) de IP na Localização (Admin Master); tabela `ip_aliases`, `/v1/ip-aliases`.
- 2026-07-15 — Incidente 1 usuário: `/v1/ingest` 500 infinito. Diagnóstico: buffer LIMPO; era agente antigo/backlog desde 01/06 → resolvido reinstalando ZERADO (apagar `%APPDATA%\WorkPulse\Agent\`). `_strip_nul` (NUL em JSONB) fica como hardening.
- 2026-07-10 — Agente 2.3.6: `log_level` no `agent.conf` (toggle debug s/ recompilar; RUST_LOG>arquivo>info); timestamp do log em horário LOCAL limpo; **instância única** (lock `agent.lock` via `fs2`); `Conectado a <host> — sincronizados N` em INFO.
- 2026-07-03 — Agente 2.3.x: **log local em arquivo** (rotação diária, retenção ~7d, `tracing-appender`, writer não-bloqueante) + panic/crash log. Caderno de testes atualizado (`DOCS/Caderno_Testes_Agente_WorkPulse.docx`).
- 2026-07-03 — Desconto de almoço no AFK (config nos Alertas, por dia local, só desconta se AFK do dia ≥ almoço); Consolidado no menu de Pessoas; paginação 3/pág no card de Departamentos (Visão Geral).
- 2026-06-2x — Gestão de Departamentos (tela + atribuição em lote); Férias (rótulo de ausência); download do agente no Suporte; rollup `ingest_hour`; perf Alertas/Consolidada; `sync_interval_sec` configurável.
- **Troca de domínio (on-premise/white-label)**: guia completo em `DOCS/WorkPulse_Guia_Troca_de_Dominio.md` (TL;DR: `infra/terraform.tfvars` → `public_domain`; agente/service via `server_url` no `agent.conf`; web via `wp-core.js`/`config.js`; nada recompila).

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
- `../workpulse-agent/{Cargo.toml,src/config.rs,src/main.rs}` (2026-07-03) — editados p/ o log
  local com rotação diária (agente 2.3.4). Edições via Edit tool casaram exato; balance de
  delimitadores em `main.rs` OK (308 linhas). Sem toolchain Rust no ambiente → validar com
  `cargo build --release` na máquina antes de empacotar.

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

## Deploy to Production

### Método do dono (Caio) — Windows PowerShell + AWS CLI v2 (PRINCIPAL)

Caio faz o deploy da máquina dele (Windows). **Não usa CloudShell e não tem python3** —
então validação local com `py_compile` é OPCIONAL/impossível p/ ele. Pré-req: AWS CLI v2 com
`aws configure` já feito (conta 881027528914, us-east-1). Sem Docker local (build roda no AWS
CodeBuild). Os 3 scripts em `scripts/` que ele usa:

- **`redeploy.ps1`** — BACKEND. Empacota `server/` (main.py, migration.py, requirements.txt,
  Dockerfile) → S3 → dispara CodeBuild → força rolling deploy no ECS (zero downtime). Rode
  após mexer em `server/`.
- **`publicar-site.ps1`** — FRONTEND. `s3 sync site/ --delete` + invalida CloudFront. Rode
  após mexer em `site/`. ⚠️ `--delete` publica TUDO em `site/` — mantenha `.bak`/temp FORA de
  `site/` (há um `site/adm/admin.html.bak-*` que sobe junto; limpar).
- **`redeploy-publicar.ps1`** — atalho que roda os dois em sequência, **backend e DEPOIS site**
  (ordem certa), abortando se um falhar. Use quando mexer em backend E frontend.

É seguro deployar sem validar localmente: o ECS tem `deployment_circuit_breaker` (rollback
automático sem downtime se a imagem nova não passar no health check). Após mudar `site/`, lembre
do cache-bust por arquivo (`?v=N`) e do Ctrl+Shift+R no navegador.

### Alternativa — AWS CloudShell (us-east-1)

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

`_directory_sync_worker()` (asyncio task, `lifespan`) — carga **diária** (1x/dia) da
fonte de RH. `_do_directory_sync_all()` itera `directory_config` com `provider<>'none'
AND auto_sync` e chama `_run_directory_sync(tid)` por tenant (erros isolados vão p/
`last_status`). Convenia: `_convenia_fetch_employees` (`GET /api/v3/employees`, header
`token`, urllib) → `_directory_upsert` (casa por e-mail, COALESCE em `manager`/`photo_url`/
`department`). Sync manual: `POST /v1/tenant-directory/sync`. Env: `WORKPULSE_CONVENIA_BASE`.

---

## Database Schema

| Table | Purpose |
|---|---|
| `tenants` | One row per client company. `dash_token`, `retention_days` |
| `report_configs` | Configurações de relatórios automáticos por tenant: `name`, `recipients` (JSON), `frequency`, `sections` (JSON), `user_filter` (JSON), `enabled` |
| `report_history` | Log de envios: `config_id`, `sent_at`, `status` (ok/error), `error_msg` |
| `licenses` | Ed25519-signed tokens. `revoked` flag |
| `users` | `(tenant_id, user_id)` — `user_id` = hostname. `display_name`, `login_user`, `department`, `role`, **`manager`, `photo_url`** (preenchidos pela fonte de RH) |
| `directory_config` | Fonte de RH por tenant (Admin Master): `provider` (convenia\|google\|none), `convenia_token`, `auto_sync`, `last_sync`, `last_status` |
| `directory_people` | Roster COMPLETO da fonte de RH (2026-07-24) -- TODOS os colaboradores da Convenia, inclusive quem NÃO tem agente instalado (nunca vira `users`). PK `(tenant_id, convenia_id)`. Gravado pelo mesmo sync que atualiza `users` (`_directory_upsert`). Alimenta `GET /v1/org-chart`. |
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
`GET /productivity/apps`, `GET|POST /departments`, `DELETE /departments/{name}`,
`GET /org-chart` (Análises → Organograma: roster COMPLETO de `directory_people`
LEFT JOIN `users` por `convenia_id` -- devolve `id`/`user_id`/`has_agent`/nome/e-mail/
depto/manager-texto/cargo/team/photo_url/on_vacation p/ TODOS os colaboradores da
Convenia, não só quem tem agente; escopo de Líder via `_scope_for` + `_lider_dept_scope`)
**Admin-only:** `POST /license/issue`, `POST /license/revoke`,
`POST /admin/tenant-token`, `GET /admin/tenants`, `GET /admin/licenses`,
`GET /admin/stats`, `GET /admin/audit`, `GET /admin/data-summary`,
`POST /admin/purge-data`, `PUT /admin/tenants/{id}/retention`
**Tenant-scoped (Reports):** `GET /reports`, `POST /reports`,
`PUT /reports/{id}`, `DELETE /reports/{id}`, `POST /reports/{id}/send`,
`GET /reports/{id}/history`
**Ficha / RH:** `GET /collaborator/profile?user_id=` (tenant-scoped) — nome, e-mail,
departamento, hostname, manager, photo_url. **Admin Master:** `GET|PUT /tenant-directory/config`
(inclui `convenia_company_id` p/ fotos), `POST /tenant-directory/sync`, `POST /tenant-directory/photos`
(DE-PARA manual de fotos: `{items:[{key,url}], store_copy}`), `GET /tenant-directory/convenia-sample`
(diagnóstico: estrutura do 1º colaborador da Convenia — keys, `id`, campos foto). Fonte de RH: Convenia hoje, Google em breve.
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
    wp-users.js           Usuários do painel (CRUD, papéis, escopo do Líder), Licença, Financeiro, Fonte de dados RH (loadDirectoryConfig/save/sync)
    wp-tempoativo.js      Aba Análises → Tempo Ativo (todos os colaboradores; busca/ordenação/multi-depto; gráfico clicável → ficha)
    wp-orgchart.js        Aba Análises → Organograma (árvore casando `manager`↔`display_name`; expand/collapse por nó, busca, zoom, legenda por depto; clique abre a ficha)
    wp-profile-modal.js   Modal reutilizável Ficha do Colaborador — openProfileModal(userId,name); drop-in em qualquer página
    wp-locations.js       Coletas → Localização (IPs públicos + geo + mapa Leaflet)
    wp-analytics.js       Aba Alertas (5 sinais via /v1/alerts/today) + Tendências + config de Alertas
    wp-nav.js             Navegação por tabs + dropdowns (switchTab, TABS, TAB_GROUP, loaders)
    wp-auth.js            Login e-mail/senha (sessão), papéis, _applyRoleUI, tema, bootstrap IIFE
```

#### Navegação (topbar)

```
[Visão Geral]  [Coletas ▾]            [Análises ▾]     [Administrativo ▾]   [⚙ ▾]
               Pessoas                 Tempo Ativo      … Configurações       Ajustes
               Departamentos           Organograma        (ex-Licenciamento)  Sair
               Eventos                 Alertas
               Internet                Tendências
               Jornada de Trabalho

Nota: "Configurações" é a página do Admin Master (Administrativo, id interno `tab-admlicenca`);
"Ajustes" (⚙ menu de conta, `tab-config`) são as preferências pessoais/tema.
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

## Estado atual (2026-06-21) — sync_interval_sec, perf do /admin/stats, cache em escala, UI

Sprint de performance/escalabilidade (ingestão maior) + ajustes de UI. **Deploy: ver "Método
do dono (Caio)" na seção Deploy — `redeploy.ps1` / `publicar-site.ps1` / `redeploy-publicar.ps1`.**

### Agente — intervalo de envio (sync) configurável pelo painel + AO VIVO
- Novo campo **`sync_interval_sec`** na config central (Platform Admin → Sistema → "Config do
  agente (global)", input `acSync`). Schema: coluna em `agent_config_global` e
  `agent_config_tenant` (+ `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`), default 30; incluído em
  `_AGENT_CFG_DEFAULTS` / `_AGENT_CFG_FIELDS` / `_agent_cfg_upsert` / `AgentCfgIn`.
- Agente (Rust): campo em `RemoteConfig` + `apply()` (remote_config.rs). O loop de **sync**
  agora lê a config COMPARTILHADA a cada ciclo: `sync::run` recebe `Arc<RwLock<Config>>` e
  main.rs passa `shared_cfg` (antes recebia snapshot fixo → só valia ao reiniciar). Agora vale
  no refresh remoto (~10 min), igual ao tracker. **Exige rebuild+rollout do agente** (FEITO,
  em distribuição).
- `heartbeat_interval_sec` já era central e vale nos agentes existentes; só o `sync` precisou
  do rebuild.

### Perf do /v1/admin/stats (gráficos zerados / página lenta)
- CAUSA: contagens 24h/7d e "ingestão por hora" filtram `events` por `ts` SEM tenant_id, e
  TODOS os índices de events são prefixados por tenant_id → varredura da tabela inteira →
  estoura o `statement_timeout` de 6s → o except zera os números (gráfico vazio). Some quando a
  tabela cresce.
- FIX parcial: **`_ensure_ts_index()`** cria `idx_events_ts ON events(ts)` CONCURRENTLY (mesmo
  padrão de advisory lock do `idx_events_eid`), chamado em `init_db`. Ajudou o COUNT de 24h, mas
  o COUNT de 7d + o GROUP BY por hora AINDA estouravam o timeout em escala. (O índice segue útil
  p/ o backfill e outras queries de faixa por ts.)
- **FIX definitivo (2026-06-21b) — tabela consolidada `ingest_hour`**: dado do passado não muda,
  então o `/admin/stats` NÃO varre mais `events`. Nova tabela `ingest_hour(tenant_id, hour, n)`
  (PK), incrementada NO `/v1/ingest` (conta os eventos do lote por hora-UTC do evento → 1-2
  upserts por PK; barato e O(1) por lote). O `/admin/stats` lê 24h/7d/por-hora dessa tabela
  (poucas dezenas de linhas, instantâneo, sem `statement_timeout`). Hora passada fica congelada.
  Backfill único das últimas 48h a partir de events na subida (`_ingest_hour_backfill`, task no
  lifespan, em background, guardado por `MIN(hour)` p/ não varrer events a cada redeploy). Pequena
  super-contagem possível em reenvio de lote já aceito — irrelevante p/ gráfico de volume.

### Cache em escala (gargalo nº1 sob carga)
- **`_cache_bust_throttled(tenant)`**: o `/v1/ingest` antes chamava `_cache_bust` a cada lote,
  zerando o cache do tenant várias vezes/s (pior quanto MAIOR a frota → painéis nunca achavam
  cache quente). Agora no máx. 1 invalidação a cada `WORKPULSE_INGEST_BUST_SEC` (default 60s)
  por tenant. **Escritas do painel (PATCH user, regras, depts, purge) seguem com `_cache_bust`
  imediato** — só o ingest usa o throttle.
- Cache dedicado do `/v1/admin/stats`: `_ADMIN_STATS_CACHE` + `_admin_stats_cached()`, TTL
  `WORKPULSE_ADMIN_STATS_TTL` (default 45s). A página recarrega a cada 30s; evita re-agregar a
  events a cada refresh. Global (não leva tenant), independente do `_CACHE`.

### Perf da aba Alertas (Análises → "Sinais que merecem atenção")
- `/v1/alerts/today` calculava tudo ao vivo varrendo `events`. Dois ajustes SEM mudar resultado:
  1. **`_compute_engagement_alerts`**: o `WHERE` usava `ts::timestamptz >= ...::timestamptz`, e o
     cast em `ts` (TEXT) ANULAVA o índice (varria a janela inteira de 7 dias úteis convertendo
     linha a linha). Trocado por comparação de TEXTO (`ts >= %(since)s AND ts <= %(until)s`) —
     mesmo idioma que o `_compute_day_alerts` já usa em prod (ISO-8601 UTC, lexicográfico =
     cronológico), índice volta a valer. O cast `to_char(... AT TIME ZONE)` fica só no SELECT
     (rótulo do dia local), rodando apenas nas linhas já filtradas. Resultado idêntico, bem mais rápido.
  2. **Cache POR ESCOPO** no endpoint: chave `"{tenant}:alerts_today:{scope}:{days}:{since}:{until}"`
     onde scope = `all` (admin) ou `lid:<uids ordenados>` (Líder — não pode vazar entre escopos).
     Chaveia pelos params CRUS do request (não pelo `now` derivado). Reusa `_cache_get/_cache_set`
     (TTL 300s, invalidado nas escritas + throttle do ingest) → recargas e o auto-refresh de 30s
     ficam instantâneos. Defasagem aceitável (a tela é "ponto de partida, não veredito").
- **(2026-06-21c) Engajamento migrado p/ rollup** — o de-cast + cache NÃO bastaram: cold load do
  `today?tenant=` ainda dava ~22s (medido no Network), pois agregar 7 dias de `events` crus é caro
  por natureza, índice ou não. Agora `_compute_engagement_alerts` lê de **`agg_user_day`** (campo
  `focus_sec` == `SUM(duration_sec)` de window por dia local → resultado IDÊNTICO ao cru) usando o
  padrão **`_agg_ready` + fallback**: lê do rollup quando o backfill cobre a janela; senão cai na
  consulta crua (de-castada). Filtro de escopo do líder preservado (`user_id = ANY(allowed)`).
  Resultado: 22s → ~10s.
- **(2026-06-21d) "Esperados" migrado p/ rollup** — em `_compute_day_alerts`, o roster de quem
  esteve ativo nos últimos 7 dias (p/ detectar ausência) era um `SELECT DISTINCT user_id` varrendo
  7 dias de events. Passou a ler `DISTINCT user_id FROM agg_user_day` no range de 7 dias locais
  (mesmo padrão `_agg_ready` + fallback; respeita escopo). Restam AO VIVO só consultas de HOJE
  (started/afk/risky em `_compute_day_alerts` — ~1 dia, pequenas). "Sites perigosos" fica cru
  (depende de título, sem rollup). Resultado esperado: ~10s → ~1-2s.
- Render progressivo (carregar card a card) ainda NÃO foi feito: a ideia some o spinner mas não
  acelera o dado. Como os scans de 7 dias saíram, o endpoint deve ficar rápido o bastante sem isso;
  se ainda pesar (provável culpado: `risky_rows`, GROUP BY user/app/título de hoje), avaliar.

### "Picotado" na Consolidada — RESOLVIDO: era CHURN DE TÍTULO, não AFK
- INVESTIGAÇÃO (com `RUST_LOG=debug` no Windows): **AFK está correto** — digitando continuamente
  NÃO logou "AFK iniciado" (descartado o cenário de "AFK durante atividade"/falha de input no Win;
  **ainda vale testar no Linux/Wayland**, onde a detecção via X11/`device_query` é mais frágil).
- CAUSA REAL: páginas que mudam o TÍTULO a cada segundo (cronômetro, notificações do Slack, YouTube)
  geram um evento `window` de 1s por segundo (cada título = "troca de janela"). E durante o churn o
  heartbeat sumia (a condição era "janela não mudou"). Resultado: timeline picotada + volume alto.
- FIX (B) AGENTE (`tracker.rs`) — **debounce de título** (exige rebuild + rollout; TESTAR antes):
  - troca de APP → registra na hora; mesmo app, título diferente → só registra se persistir
    `WINDOW_TITLE_DWELL_SEC`=3s (título piscando nunca "assenta" → sem evento). Estado novo:
    `pending_win`/`pending_since`; `commit` substitui `window_changed`.
  - heartbeat agora dispara por **mesmo APP** (`same_app_as_emitted`), não pela janela inteira —
    assim o churn de título não suprime mais o heartbeat nem perde tempo ativo.
  - Efeito: ~6 ev/min em vez de ~60, mesmo tempo ativo, timeline limpa. Tradeoff: visitas ao MESMO
    app < 3s caem p/ resolução do heartbeat (~10s). (A supressão de window em AFK foi descartada —
    o debounce já limpa o churn sem o risco de esconder tempo produtivo.)
- FIX (A) BACKEND (`/v1/productivity/timeline`) — pós-passe que funde RAJADAS de micro-runs (<=3s)
  consecutivos do mesmo (tipo, categoria, app) num bloco "(vários títulos)". Limpa a timeline p/
  dados ANTIGOS e agentes ainda não atualizados, sem afetar visitas reais (runs >=3s/heartbeats de
  10s) nem os totais. Testado em Python (churn 6→2 preservando segundos; navegação normal e AFK não
  fundem). Deploy do A é só backend (`redeploy.ps1`); o B vai no próximo rebuild do agente.

### Frontend
- **Platform Admin** (`admin.js?v=20`, `admin.html`): gráfico "Ingestão por hora" rotula o eixo
  no fuso de **Brasília** (`drawIngestChart` converte o `hour` UTC via `toLocaleString` com
  `timeZone: America/Sao_Paulo`). Card "Config do agente" ganhou **Helper** (`<details>`)
  explicando cada campo + o input `acSync` (Envio/sync).
- **Painel do cliente — Coleta consolidada** (`wp-consolidado.js?v=8`, workforce.html): a barra
  do gráfico "Por categoria — por dia" voltou a ficar larga com 1 dia e afinar com mais dias —
  removido o `maxBarThickness` fixo (entrou junto com o ajuste do eixo Y) p/ igualar o gráfico
  da Visão Geral (`drawDailyChart`, que não tem cap).
- **Colaboradores** (`wp-settings.js?v=27`, workforce.html): **campo de busca** (nome / device /
  e-mail / depto / função) + **ordenação clicável** por Device e Nome. Funções `_collabsView`,
  `_collabsOnSearch`, `_collabsSortBy`; tudo client-side (`state.collabs`). ⚠️ o handler é
  **`_collabsSortBy`** — NÃO `_collabsSort` (essa é a variável de estado `{key,dir}`; nome igual
  ao de uma função = SyntaxError que derruba o arquivo inteiro).
- **Suporte Técnico** (`wp-support.js?v=4`, workforce.html): **ordenação clicável** nas 4 colunas
  (Colaborador/Status/Última atividade/Versão do agente — `_supSortBy`/`_supSort`; versão com
  compare semântico `_supVerCmp`, ex.: 2.10 > 2.9). Novo **card consolidado** (2 cards 6-col):
  Devices por SO e Devices por versão do agente — calculado NO CLIENTE a partir de `_supUsers`
  (mesma lista já carregada), **sem query extra** (`_supRenderDist`/`_supDistHtml`). Backend:
  `/v1/support/people` passou a devolver `d.os` (uma coluna a mais no LATERAL existente, custo
  desprezível) — exige redeploy do backend p/ o card de SO popular.

### Suporte: download do agente + rótulo de Férias (2026-06-25)
- **Download do agente na tela Suporte do cliente.** Novo `GET /v1/support/agent-download`
  (`require_role` admin_master/admin/suporte): pega o release `is_latest` por SO, **preferindo o
  canal `tenant:<id>`; senão `generic`**, e gera URL pré-assigned na hora (1h). NÃO existe link de
  longa duração (o mito dos "30 dias" não cabe — S3 SigV4 limita a 7 dias; aqui é fresco a cada
  carga). Front: card "Baixar agente (versão atual)" em `tab-suportetec` + `_supLoadAgentDownload`
  (wp-support.js?v=5). Os endpoints `/v1/admin/releases*` (upload/promote/download) seguem só admin.
- **Rótulo de Férias** (liga/desliga manual por colaborador = ausência justificada):
  - Schema: `users.on_vacation BOOLEAN DEFAULT FALSE` (+ ALTER). PATCH `/v1/users/{id}` aceita
    `on_vacation` (no front é enviado SEMPRE, após o filtro de falsy, senão não dava p/ DESLIGAR).
  - **Visão Geral (`/v1/workforce/overview`) EXCLUI quem está de férias** de TODAS as somas
    (online, ativo, por-depto, totais) via `vac_set` — pra não diluir as médias do time.
  - Alerta **"Ausente no dia"** (`_compute_day_alerts`) **suprime** quem está de férias.
  - Front centralizado em wp-core.js (v=21): `state.vacation` (Set) + `markVacation(users)` +
    `vacBadge(uid)`. O dropdown de pessoa (`_populateUserDropdown`, compartilhado) mostra
    "🏖 (Férias)" → cobre Consolidada/Jornada/Aplicativos/Internet de uma vez. Badge 🏖 também em
    Pessoas (wp-people v=23), Colaboradores + toggle no form (wp-settings v=28) e Suporte (status
    "Férias"). Endpoints que passaram a devolver `on_vacation`: `/v1/users`, `/v1/team/summary`,
    `/v1/support/people`. Deploy: backend (`redeploy.ps1`) + site (`publicar-site.ps1`).

### Gestão de Departamentos — tela faltava no HTML + atribuição em LOTE (2026-06-26)
- DESCOBERTA: o JS de gestão de depto (`renderDeptsSection`/`createDept`/`deleteDept`/`assignUser*`
  em wp-settings.js) já existia e era chamado no load da aba Colaboradores, MAS o **HTML host
  (`#deptsList`, `#newDeptName`) não existia** em workforce.html → `renderDeptsSection` saía no
  `if(!el)return` e a tela nunca aparecia. Por isso o usuário atribuía depto editando 1 a 1.
- FIX: adicionado o card "🏢 Departamentos" no topo da aba Colaboradores (workforce.html) com
  input de novo depto + `#deptsList`. CSS (`dept-mgmt-card` etc.) já existia.
- NOVO: atribuição **em lote**. Backend `POST /v1/departments/{name}/members` (require_tenant_scope,
  body `{user_ids:[...]}`, `UPDATE users SET department=name WHERE user_id = ANY(%s)`). Front: o
  `<select>` de 1 colaborador virou **multi-select** (candidatos = quem NÃO está no depto, rótulo
  mostra o depto atual p/ permitir mover) + `assignUsersToDept` chama o endpoint em lote. Removida a
  `assignUserToDept` (singular). wp-settings.js?v=29. Deploy: backend + site.
- LACUNA do depto "texto livre": atribuir depto no Editar gravava só `users.department` (texto),
  NUNCA em `tenant_departments` → a tela de gestão (que lista `tenant_departments`) saía vazia
  mesmo com gente com depto. FIX (sem perder nada): (1) backfill idempotente em `_SCHEMA_STMTS`
  (`INSERT ... SELECT DISTINCT department FROM users ... ON CONFLICT DO NOTHING`) registra todos os
  nomes já atribuídos; (2) `update_user` auto-registra o depto no `tenant_departments` ao definí-lo.
  Deletar o depto continua zerando `users.department` (não ressuscita no próximo boot).

### Menu "Consolidado" no popup de cada colaborador (Pessoas) (2026-06-29)
- Em `wp-people.js` (`openPersonMenu`) adicionado o item "📋 Consolidado" que chama
  `goToUserPage('consolidado', uid, name)` — abre a aba Consolidada já filtrada na pessoa.
  wp-people.js?v=24.

### Desconto de almoço no AFK — configurável, por DIA LOCAL (2026-06-29)
- DEMANDA: descontar o almoço (default **60 min**, configurável) do AFK **na jornada**, somando o
  AFK do dia e abatendo o almoço **sem exigir bloco contínuo** (por dia local, não corrido).
  Refletir em **todas** as telas que mostram AFK da pessoa (números e gráficos).
- CONFIG: vive no card **"Horário comercial e limites"** (Análises → Alertas), campo
  "Almoço descontado (min)". `0` desliga o desconto.
- BACKEND (`main.py`): coluna `alert_settings.lunch_minutes INTEGER NOT NULL DEFAULT 60` (+ ALTER
  idempotente). Helper `_lunch_sec(settings)`. `AlertConfigIn.lunch_minutes`, lido/gravado em
  `alerts_config_get`/`alerts_config_set` (com `_cache_bust`). Exposto em `/v1/license/info`.
  Desconto aplicado **por (uid, dia_local)**: `afk_exibido = max(0, afk_do_dia − almoço)` em
  `team_summary`, `workforce_overview`, `user_summary`, `productivity_timeline` e
  `productivity_consolidado`. **`afk_in` permanece cru** (não descontado) p/ não misturar bases.
- FRONTEND: `state.lunchMin` (wp-core, default 60) populado por `/v1/license/info` (wp-auth
  `_loadLicenseInfo`) e pelo config de Alertas (wp-analytics). Campo "Almoço descontado (min)"
  (`#alLunch`) no workforce.html + load/save em wp-analytics. **Jornada** desconta localmente por
  dia em `_jorComputeFromEvents` (wp-jornada) pois calcula do cru. Demais telas recebem o AFK já
  abatido do backend → refletem sozinhas.
- CACHE-BUST: wp-core v22, wp-analytics v25, wp-jornada v22, wp-auth v23. Deploy: backend + site.
- AJUSTE (2026-07-03): a tela **Jornada** usa o endpoint `/v1/jornada` (caminho preferido,
  `_jorBuildJornada`) — o desconto só estava no fallback `_jorComputeFromEvents` (404), então NÃO
  aplicava. Movido p/ `_jorBuildJornada` (+ fallback). NOVA REGRA (pedido do usuário): **só desconta
  se o AFK do dia for >= o almoço**; abaixo disso mantém o cru (senão zera dias de pouca pausa e
  parece erro de coleta). Backend ganhou helper `_apply_lunch(afk_day, lunch)` com a mesma regra,
  usado nos 5 pontos (user_summary, team_summary, os 2 productivity_*, workforce_overview).
  wp-jornada v23. Deploy: backend + site.

### Distribuição por departamento (Visão Geral) — paginação 3/página (2026-07-03)
Card "DISTRIBUIÇÃO POR DEPARTAMENTO" estourava o layout com muitos deptos. `drawDepartments`
(wp-overview) virou paginado: `_deptsOvData`/`_deptsOvPage`/`_DEPTS_OV_PER=3` + `_renderDeptsOvPage`
+ `_deptsOvGo(dir)` com pager ‹ Anterior / Próximo ›. Percentual sempre relativo ao total de TODOS
os deptos (não muda entre páginas). wp-overview v22. Deploy: só site.

### Agente 2.3.4 — log local com rotação diária p/ diagnóstico de TI (2026-07-03)
DEMANDA: agentes que "sobem e caem" ou param de coletar hoje são resolvidos reinstalando; queríamos
um log local que ajude TI a antecipar e implantar resiliência, SEM onerar CPU nem disco.
- `../workpulse-agent` (Rust). Antes o `tracing` ia só p/ console → perdido em serviço/background.
- Cargo.toml: dep `tracing-appender = "0.2"`; versão 2.3.4 (o número real vem do env
  `WORKPULSE_AGENT_VERSION` no empacotamento; Cargo.toml é fallback).
- `config.rs`: `pub fn log_dir()` = `dirs().data_dir().join("logs")` (resolve sem instância de
  Config, p/ inicializar o log cedo).
- `main.rs`: `init_logging()` monta subscriber em CAMADAS (console + arquivo). Arquivo via
  `RollingFileAppender::builder()` DAILY, prefix `workpulse-agent`, suffix `log`,
  `max_log_files(8)` (~7 dias de retenção → limita disco), writer NÃO-BLOQUEANTE
  (`tracing_appender::non_blocking`, thread dedicada → quase zero CPU). Nível **INFO** por padrão
  (`RUST_LOG` sobrepõe): o loop de 1s e o heartbeat (~10s) seguem em DEBUG (fora do arquivo em
  nível padrão). Em INFO o arquivo recebe início/fim, licença, config e as falhas (`warn`) de
  coleta/gravação/sync. AJUSTE (2026-07-03): promovido a INFO só o sinal de envio bem-sucedido —
  `sync.rs` agora loga `Conectado a <host> — sincronizados N eventos` (era `debug!("Sincronizados
  N eventos")`), provando conectividade + throughput numa linha nossa (o `connected to <IP>` que
  aparecia no console é da lib hyper, DEBUG, NÃO é nosso — por isso logamos o host por conta
  própria). Heartbeat foi deixado em DEBUG de propósito (evita encher o INFO a cada 10s).
  Guarda o `WorkerGuard` vivo em `main` (flush no fim). Se a pasta não for gravável → cai p/
  só-console e segue.
- `install_panic_hook(log_dir)`: como o release usa `panic = "abort"`, grava a causa do panic de
  forma SÍNCRONA num `workpulse-agent-crash.log` (além do log estruturado) — garante captura antes
  do abort. É o arquivo que explica as quedas.
- Startup loga "Logs locais em: <caminho>" p/ TI achar rápido. Windows: `%APPDATA%\WorkPulse\Agent\
  data\logs\`. API validada na doc oficial (tracing-appender 0.2.5). SEM toolchain Rust no ambiente
  de edição → `cargo build`/reempacotar MSI/.deb é na máquina do usuário.

### Agente 2.3.6 — modo debug persistente via `log_level` no agent.conf (2026-07-10)
DEMANDA: ligar/desligar debug pra deixar o dia todo e analisar quando precisar, sem `RUST_LOG`
(chato em serviço/background) e sem recompilar.
- `config.rs`: novo campo `log_level: String` (default `"info"`) no `Config` (serde(default) →
  retrocompatível, agent.conf antigo assume info). Nova `pub fn read_log_level()` — lê SÓ o nível do
  agent.conf de forma tolerante (error|warn|info|debug|trace; inválido/ausente → "info"), p/ inicializar
  o log antes do load completo.
- `main.rs` `init_logging()`: base do filtro passou a ser `EnvFilter::try_from_default_env()
  .unwrap_or_else(|| EnvFilter::new(config::read_log_level()))`. **Prioridade: RUST_LOG > log_level
  (agent.conf) > info.** Boot loga "Nivel de log: <x> ..." p/ confirmar. Uso: `log_level = "debug"` +
  reiniciar o agente → debug o dia todo no arquivo; voltar a "info" desliga.
- Versão: Cargo 2.3.4 → **2.3.6** (a build do usuário já estava em 2.3.5 — que tinha o log em arquivo
  mas NÃO o `log_level`; por isso "não pegou" ao editar o agent.conf: serde ignora chave desconhecida).
  Precisa recompilar/reempacotar (`-Version 2.3.6`) p/ o campo valer.
- (2026-07-10) O `agent.conf` gerado agora JÁ traz o campo com comentário das opções, nos 3 geradores:
  template do `config.rs` (`load_or_create`), `windows/build-msi.ps1` e `linux/build-deb.sh`:
  `# log_level: error | warn | info | debug | trace ...` + `log_level = "info"`.
- COMPORTAMENTO: o nível é lido UMA vez no boot (`init_logging`) → mudar exige **reiniciar** o agente
  (não é hot-reload). Reler o arquivo seria barato (arquivo minúsculo), mas manteríamos restart por
  simplicidade/robustez; live-reload via `tracing_subscriber::reload` é possível se pedirem.
- DOC atualizado: `DOCS/Caderno_Testes_Agente_WorkPulse.docx` (ref. 2.3.6): Seção 4 com caminhos dos
  logs, Seção 9 reescrita (log em arquivo automático + modo debug), tabela de mensagens-chave com
  `Logs locais em`/`Nivel de log`/`Conectado a ...`/`PANIC`, e removido o bloco obsoleto "BUTA A
  MAQUINA" (workaround manual de autostart, superado pelo log built-in). Salvo como
  `Caderno_Testes_Agente_WorkPulse_2.3.6.docx` (arquivo original estava travado/aberto).

### Agente — formato/timezone do timestamp do log (2026-07-10)
O log saía em UTC com `T` e fração de segundo (`2026-07-10T14:26:12.001407Z`). Trocado para horário
LOCAL da máquina, limpo: `2026-07-10 14:26:12` (espaço, sem fração, sem `Z`).
- Cargo.toml: `tracing-subscriber` ganhou a feature `chrono` (já dependíamos de `chrono`).
- `main.rs` `init_logging()`: `let timer = fmt::time::ChronoLocal::new("%Y-%m-%d %H:%M:%S".into())`
  aplicado com `.with_timer(timer.clone())` nas DUAS camadas (console e arquivo). ChronoLocal usa
  `chrono::Local` → fuso da máquina (em Brasília, -3). Vai junto na build 2.3.6.

### Agente — instância única (uma por usuário) (2026-07-10)
DEMANDA: às vezes a máquina rodava com 2 agentes ao mesmo tempo (autostart duplo etc.) → SQLite
duplicado / eventos em dobro. Além do WatchDog, tratar no próprio agente.
- Cargo.toml: dep `fs2 = "0.4"` (lock de arquivo advisory multiplataforma: `flock` Unix / `LockFile`
  Windows; liberado pelo SO no fim do processo, inclusive em `panic=abort` → nunca fica preso).
- `main.rs`: helper `acquire_single_instance_lock(data_dir)` → trava exclusiva em
  `<data_dir>/agent.lock`. Chamado logo após carregar a config (antes de abrir o SQLite). Retorna
  Ok(Some(File)) [mantido vivo em `_instance_lock` no main], Ok(None) [outra instância detém →
  `warn` + `return Ok(())`], ou Err [falha de I/O ao criar/abrir → segue SEM proteção, não suprime
  coleta]. Contenção detectada por `ErrorKind::WouldBlock` ou raw_os_error 33/11/35/13.
- É POR USUÁRIO de propósito (a pasta de dados é por usuário): dois usuários distintos, cada um com
  seu agente, continua correto — cada sessão coleta a sua.
- Validação: `cargo build` é na máquina do usuário (sem toolchain aqui). O verificador Python de
  balanceamento dá falso positivo por causa das strings multi-linha `\`-continuadas do bloco de
  heartbeat (pré-existentes); provado balance-neutro (stripped==full) e a 2.3.5 compila. Blocos
  novos conferidos à mão. Vai junto na build 2.3.6.

### BUG /v1/ingest — 500 infinito por NUL ( ) no título (2026-07-15)
SINTOMA: 1 usuário (de 100+) parou de sincronizar; agente saudável (conecta, licença/config OK),
mas TODO POST `/v1/ingest` → `500 Internal Server Error`, com backoff crescente (2→4→…→256s). Log do
agente (v2.3.6, já com log em arquivo) foi o que permitiu ver isso.
- CAUSA: um evento com caractere **NUL (` `)** no título/app (alguns apps do Windows geram
  títulos com byte nulo). O PostgreSQL **rejeita ` ` em JSONB e TEXT** → o INSERT do lote
  estoura (exceção não tratada → 500). Como o agente só apaga o lote **após sucesso**, ele reenviava
  o MESMO lote pra sempre → coleta travada só daquele usuário. Os outros 100 tinham lotes "limpos".
- FIX (backend, `main.py`): helper `_strip_nul(v)` remove ` ` de strings recursivamente
  (str/dict/list). Aplicado em `ingest` a: `user_id`, `device_id`, `login_user`, campos do device
  (hostname/os_version/agent_version), e principalmente `Json(_strip_nul(e.data))` + `e.bucket`/`event_id`.
- DEPLOY: só backend (`redeploy.ps1`).
- ATUALIZAÇÃO (2026-07-15b): o fix de NUL **NÃO resolveu** este caso. Análise do buffer real da máquina
  (`buffer_solange.db`, 127k eventos / 20MB, desde 01/06) mostrou o dado **100% limpo**: sem NUL, sem
  surrogate solto, JSON válido, sem event_id duplicado no lote, colunas todas TEXT, ts nano igual ao de
  todos. Ou seja, NÃO era dado envenenado. Único diferencial: eventos ANTIGOS de um agente que foi
  atualizado ao longo do tempo — provável formato/comportamento antigo de request rejeitado pelo servidor
  atual, travado desde 01/06 (buffer nunca drenou, cresceu). RESOLUÇÃO adotada pelo usuário: **remover o
  agente + APAGAR a pasta de dados** `%APPDATA%\WorkPulse\Agent\` (a desinstalação do MSI NÃO apaga o
  buffer!) + reinstalar 2.3.6 zerado. O `_strip_nul` fica como hardening defensivo (NUL em JSONB é 500
  real para outros dados), mas não era a causa aqui. Se recorrer em agente NOVO/zerado → pegar o
  traceback no CloudWatch (causa-raiz não foi isolada porque o dado estava limpo).

### Apelido (alias) de IP na tela de Localização — Admin Master (2026-07-16)
DEMANDA: em "Coletas → Localização", trocar o rótulo de geo de IPs conhecidos por um nome amigável
(ex.: `200.236.217.18` → "Escritorio Asaptech" no lugar de "São Paulo, ..."). Modal só p/ Admin Master.
- BACKEND (`main.py`): tabela `ip_aliases (tenant_id, ip, label, created_at, updated_at)` PK (tenant,ip)
  — POR TENANT. Helper `_ip_aliases(tenant)` → {ip: label}. Endpoints: `GET /v1/ip-aliases`
  (require_tenant_scope) e `PUT /v1/ip-aliases` (require_role admin_master; label vazio = remove;
  `_strip_nul` no label; audit + `_cache_bust`). `/v1/locations` agora inclui `last_alias` (resumo) e
  `alias` (detalhe) — a geo real continua em `last_geo`/`geo` (mapa/filtro seguem por geo).
- FRONTEND (`wp-locations.js` v5): `_isLocAdmin()` (role admin_master|wp_admin), `_locLabel(alias,geo)`
  (badge 🏢 se houver alias, senão geo), `_locAliasBtn(ip,label)` (lápis ✎ só p/ admin). Modal
  `#ipAliasModal` no `workforce.html` com `openIpAliasModal/saveIpAlias/removeIpAlias/closeIpAliasModal`.
  Aplica na lista (coluna Localização) e nos detalhes (por IP). Deploy: backend + site.

### Novos env knobs
`WORKPULSE_INGEST_BUST_SEC` (60), `WORKPULSE_ADMIN_STATS_TTL` (45). Já existentes:
`WORKPULSE_CACHE_TTL` (300), `WORKPULSE_AFK_MAX_SEC` (10800), `WORKPULSE_ROLLUP_SEC` (300),
`WORKPULSE_PERF_MS` (200).

### Próximos passos de escala (recomendado, NÃO feito)
Cache compartilhado (Redis/ElastiCache) → habilita `desired_count ≥ 2` + autoscaling por CPU;
read replica do RDS p/ as leituras dos dashboards; manter heartbeat 10–15s + sync 60–120s p/
frear o crescimento da `events`; migração opcional `events.ts` TEXT → timestamptz. Análise
completa em **`WorkPulse_Analise_Performance.docx`** (pasta WORK PULSE).

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
