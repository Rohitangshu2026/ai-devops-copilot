# AI DevOps Copilot — Multi-Platform Refactor

**Goal.** Turn the existing single-service AI DevOps Copilot (which only knows
about `sample-app`) into a reusable observability + incident-response platform
that can onboard any external repository or microservice fleet — starting with
**SpyRoom** (a Next.js + Spring Boot microservice platform in a separate
GitLab repo).

**Non-goals.**
- Not a rewrite. The existing ELK pipeline, safety stack, eval framework, and
  Ansible/k8s/CI plumbing stay exactly as they are.
- Not hyperscale. Target is laptop Minikube, single-node k8s, developer-scale
  load. Design for **clean extensibility**, not Netflix-scale infra.
- No service meshes, CRDs/operators, event buses, workflow engines, or other
  heavy abstractions. Pragmatic over fancy.

**Repository boundary.**
- SpyRoom stays in its own repo: `gitlab.com/spe-group2/spyroom-platform`.
- This repo (`ai-devops-copilot`) owns observability, monitoring, incident
  analysis, and infrastructure intelligence — not application business logic.
- The two repos are joined at runtime by:
  1. SpyRoom logs flowing into the shared ELK stack (Filebeat scrapes its k8s
     namespace).
  2. SpyRoom's GitLab pipeline POSTing to the Copilot's
     `/api/v1/webhook/pipeline-failure` endpoint on failed builds/deploys.

---

## What's hardcoded today (audit)

| File | Hardcoded value | Replace with |
|---|---|---|
| `agent-backend/app/core/causality.py` | `DEPENDENCY_MAP = {"sample-app": ["elasticsearch"], ...}` | Platform-config-driven map + k8s annotations |
| `agent-backend/app/utils/config.py` | `es_index = "devops-logs-*"` | Per-platform index pattern via `PlatformRegistry` |
| `agent-backend/app/services/elk_service.py` | Single index from `settings.es_index` | Resolve index from platform context |
| `agent-backend/app/models/schemas.py` | `Environment = {dev, staging, production}` | Keep enum; add free-form `platform` + optional `namespace` |
| `agent-backend/app/llm/tools.py` | `namespace=tool_input.get("namespace", "default")` | Default to caller's platform namespace |
| `agent-backend/app/api/v1/webhooks.py` | Falls back to `Environment.dev`; uses repo name verbatim | Map (repo → platform → service) via `PlatformRegistry` |
| `agent-backend/app/core/agent.py` | `dry_run=(req.environment.value == "dev")` | `dry_run = platform.dry_run_envs ⊇ {env}` |
| `k8s/elk/filebeat.yaml` | Hardcoded `_default_` / `_devops-test_` path filter | Generated from registered platform namespaces |
| `evals/incidents/*` | Generic archetypes (no SpyRoom failures) | Add SpyRoom-specific fixtures |
| Everything `sample-app/*` | Demo failure modes only | Keep as a reference platform; SpyRoom becomes the first real one |

---

## Target architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│ ai-devops-copilot repo                                              │
│                                                                     │
│  configs/platforms/                                                 │
│    ├─ sample-app.yaml      ← demo platform (existing behaviour)     │
│    ├─ spyroom.yaml         ← first real platform                    │
│    └─ default.yaml         ← fallback for unknown services          │
│                                                                     │
│  agent-backend/app/platforms/                                       │
│    ├─ registry.py          ← PlatformRegistry, PlatformConfig       │
│    ├─ service_registry.py  ← ServiceRegistry (per-platform)         │
│    └─ discovery.py         ← KubernetesDiscoveryService (annotations)│
│                                                                     │
│  agent-backend/app/integrations/                                    │
│    ├─ slack.py             ← existing                                │
│    └─ gitlab.py            ← NEW: read-only client, MR comments,    │
│                              signed webhook verification             │
│                                                                     │
│  agent-backend/app/services/elk_service.py                          │
│    ← reads index pattern from PlatformRegistry                       │
│                                                                     │
│  agent-backend/app/core/causality.py                                │
│    ← DEPENDENCY_MAP merged from platform configs at startup          │
│    ← (k8s annotations still take precedence via blast_radius.py)     │
│                                                                     │
│  k8s/platforms/spyroom/                                             │
│    ├─ namespace.yaml                                                │
│    ├─ observability-annotations.md  (instructions for SpyRoom repo) │
│    └─ filebeat-patch.yaml           (adds spyroom ns to filter)     │
│                                                                     │
│  ci-templates/                                                      │
│    └─ .gitlab-ci.spyroom.example.yml  ← drop-in for SpyRoom repo    │
│                                                                     │
│  evals/incidents/spyroom_*           ← 4 new SpyRoom fixtures       │
│                                                                     │
│  docs/                                                              │
│    ├─ PLATFORMS.md         ← how to onboard a new platform          │
│    └─ GITLAB_INTEGRATION.md ← cross-repo CI/CD wiring               │
└─────────────────────────────────────────────────────────────────────┘
                            │
                            │ (runtime data flow only — no source)
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│ spyroom-platform repo (separate GitLab repo)                        │
│   ├─ services/{api-gateway,auth-service,room-service}/              │
│   ├─ frontend/web-app/                                              │
│   ├─ docker-compose.yml                                             │
│   └─ .gitlab-ci.yml  ← built from our template; webhooks Copilot    │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Core abstractions (the only new types)

```python
# agent-backend/app/platforms/registry.py

@dataclass
class ServiceSpec:
    name: str                      # "auth-service"
    language: str = ""             # "java" | "python" | "node" | ""
    log_format: str = "json"       # "json" | "logfmt" | "plain"
    depends_on: list[str] = []     # ["postgres", "redis"]
    criticality: str = "medium"    # low | medium | high | critical
    deployment_kind: str = "Deployment"   # for kubectl get/set
    container_name: str = ""              # override if != service name

@dataclass
class PlatformConfig:
    name: str                              # "spyroom"
    namespace: str                         # "spyroom"
    environments: list[str]                # ["dev", "staging", "production"]
    dry_run_envs: list[str]                # ["dev"]
    log_index_pattern: str                 # "devops-logs-*" (shared) or custom
    log_service_field: str = "service"     # ES field that holds service name
    services: list[ServiceSpec]
    gitlab_project: str | None = None      # "spe-group2/spyroom-platform"
    owners: list[str] = []                 # ["@alice", "@bob"] for Slack tags
    metadata: dict = {}                    # free-form (repo URL, dashboard URLs)

class PlatformRegistry:
    def get(self, name: str) -> PlatformConfig | None
    def for_service(self, service: str) -> PlatformConfig | None
    def all() -> list[PlatformConfig]
    def reload() -> None     # SIGHUP-driven, mirrors policy.py
```

`ServiceRegistry` is a thin convenience wrapper that flattens services across
platforms, used by `causality.py` and `blast_radius.py` for fallback lookups.

`KubernetesDiscoveryService` reads `devops-copilot/*` annotations live (cached
60s). It already exists in spirit inside `blast_radius.py` — we extract and
generalize it.

---

## Migration plan (5 incremental commits, each ships green)

Each commit leaves the system fully working. No big-bang rewrite.

### Commit 1 — Foundation (no behaviour change)

**Files added:**
- `configs/platforms/sample-app.yaml` — captures today's hardcoded behaviour
- `configs/platforms/default.yaml` — fallback when no match
- `agent-backend/app/platforms/__init__.py`
- `agent-backend/app/platforms/registry.py` — `PlatformRegistry`, `PlatformConfig`, `ServiceSpec`
- `agent-backend/app/platforms/service_registry.py` — `ServiceRegistry`

**Files modified:**
- `agent-backend/app/main.py` — load registry at startup; register SIGHUP for reload
- `agent-backend/app/utils/config.py` — add `platforms_dir: str = "configs/platforms"` setting

**Backward compat:** if `configs/platforms/` is empty or missing, the registry
seeds itself with one entry equivalent to today's hardcoded values, so existing
callers continue to work.

### Commit 2 — Wire the registry into the query path

**Files modified:**
- `agent-backend/app/models/schemas.py`
  - `AnalysisRequest`: add `platform: Optional[str] = None`, `namespace: Optional[str] = None`
  - Backward-compatible (both optional)
- `agent-backend/app/services/elk_service.py`
  - `fetch_logs` accepts an optional `PlatformConfig` and uses its index pattern + service field name
  - Existing single-arg signature preserved via a default-resolver helper
- `agent-backend/app/core/agent.py`
  - Resolve platform: explicit `req.platform` → `registry.for_service(req.service)` → `default`
  - Use platform for index, namespace, dry_run gating, owners
- `agent-backend/app/llm/tools.py`
  - `_get_k8s_events` defaults namespace to `platform.namespace` instead of `"default"`
- `agent-backend/app/core/causality.py`
  - Build `DEPENDENCY_MAP` from registered platforms' `ServiceSpec.depends_on` at startup
  - Static fallback dict preserved

**Test coverage:** existing 544 tests pass unchanged. Add ~10 new tests covering:
- registry load/reload
- `for_service` fallback chain (explicit → service-lookup → default)
- `fetch_logs` honors platform index pattern when supplied
- `causality` merges deps from registered platforms

### Commit 3 — Onboard SpyRoom

**Files added:**
- `configs/platforms/spyroom.yaml` — namespace, services, dependencies, owners
- `k8s/platforms/spyroom/namespace.yaml`
- `k8s/platforms/spyroom/filebeat-namespace-patch.yaml` — extends Filebeat's
  path filter to include `_spyroom_`
- `k8s/platforms/spyroom/observability-annotations.md` — copy-paste snippets
  for SpyRoom maintainers to add to their Deployments (criticality, depends-on,
  scrape annotations)
- `evals/incidents/spyroom_auth_crash/` (logs.json + expected.json)
- `evals/incidents/spyroom_db_outage/`
- `evals/incidents/spyroom_gateway_cascade/`
- `evals/incidents/spyroom_websocket_outage/`

**Files modified:**
- `k8s/elk/filebeat.yaml` — drop_event filter regenerated from registered
  platform namespaces (or trivially patched to include `_spyroom_`)
- `ansible/roles/elk_stack/...` — render Filebeat ConfigMap from platforms list

### Commit 4 — GitLab integration

**Files added:**
- `agent-backend/app/integrations/gitlab.py` — read-only API client + webhook
  signature verification (`X-Gitlab-Token` HMAC compare)
- `ci-templates/.gitlab-ci.spyroom.example.yml` — production-style template
  for SpyRoom's GitLab repo (test → build → push → deploy → notify-copilot)
- `docs/GITLAB_INTEGRATION.md` — cross-repo wiring doc

**Files modified:**
- `agent-backend/app/api/v1/webhooks.py`
  - Verify `X-Gitlab-Token` against `GITLAB_WEBHOOK_TOKEN` env var
  - Map repo name → `PlatformRegistry.get_by_gitlab_project()` to determine
    target service + namespace (no more `Environment.dev` hardcoded)
  - Accept deployment-failure payloads in addition to pipeline-failure
- `agent-backend/app/utils/config.py` — add `gitlab_webhook_token`, `gitlab_api_url`, `gitlab_api_token`

### Commit 5 — Docs + CI guard

**Files added:**
- `docs/PLATFORMS.md` — onboarding walkthrough (add a yaml, add a namespace,
  annotate deployments, verify in dashboard)
- `docs/ARCHITECTURE.md` — diagrams updated to show registry layer
- One test `tests/test_platforms.py` covering registry, service lookup,
  multi-platform causality merge

**Files modified:**
- `README.md` — new "Onboarding a platform" section + cross-repo CI section
- `.gitlab-ci.yml` — keep current pipeline; add a `validate-platforms` job that
  parses every `configs/platforms/*.yaml` through the pydantic schema and fails
  CI on a bad config (same pattern as `policy.py` startup validation)

---

## DevOps / CI-CD layer (CSE 816 course coverage)

The course requires automation across Git → CI → Test → Build → Push →
Deploy → Logs visible in Kibana. We use **GitLab CI** instead of Jenkins.
Coverage matrix:

| Requirement | Where it lives | Status |
|---|---|---|
| Git/VCS | Both repos on GitLab | ✅ |
| CI/CD automation | `.gitlab-ci.yml` (copilot) + `ci-templates/.gitlab-ci.spyroom.example.yml` (template) | ✅ refactor adds template + cross-repo webhook |
| Hook trigger on push | GitLab pipeline `rules: if: $CI_COMMIT_BRANCH == "main"` (equivalent to GitHub Hook Trigger / GITScm Polling) | ✅ |
| Docker / Compose | `docker-compose.yml` (copilot), `spyroom-platform/docker-compose.yml` (SpyRoom) | ✅ |
| Push to Docker Hub | `push-*` stages in `.gitlab-ci.yml` | ✅ |
| Ansible config mgmt with roles | `ansible/roles/{vault_secrets,elk_stack,monitoring,app_deploy}/` | ✅ |
| Kubernetes orchestration | `k8s/*` + `k8s/platforms/spyroom/*` (new) | ✅ refactor adds platforms/ |
| ELK logging | `k8s/elk/{elasticsearch,logstash,filebeat,kibana}.yaml` | ✅ refactor adds spyroom ns to filter |
| Kibana dashboard | `monitoring/grafana/*` + Kibana index pattern | ✅ |
| Vault for secrets | `ansible/roles/vault_secrets/`, `app/utils/vault_client.py`, `k8s/vault.yaml` | ✅ |
| HPA scaling | `k8s/hpa.yaml`, `k8s/hpa-agent-backend.yaml` | ✅ |
| Live patching / no-downtime updates | `kubectl set image` + `rollout status` in `ansible/roles/app_deploy/tasks/main.yml`; `RollingUpdate maxUnavailable: 0` | ✅ |
| Domain-specific (AIOps) | The entire project IS AIOps — LLM-driven incident analysis | ✅ |

### Cross-repo CI flow (after refactor)

```
┌─────────────────────────┐
│ Developer pushes to     │
│ spyroom-platform/main   │
└────────────┬────────────┘
             │
             ▼
┌──────────────────────────────────────────────┐
│ SpyRoom .gitlab-ci.yml (from our template)   │
│                                              │
│  stages: test → build → push → deploy        │
│                       → notify-copilot       │
│                                              │
│  test:     mvn test (for each java service)  │
│  build:    docker build per service          │
│  push:     docker push to Docker Hub         │
│  deploy:   ansible-playbook (self-hosted)    │
│            applies k8s/platforms/spyroom/    │
│  notify:   on_failure → POST                 │
│            /api/v1/webhook/pipeline-failure  │
│            with X-Gitlab-Token HMAC          │
└─────────────────┬────────────────────────────┘
                  │ (failure case)
                  ▼
┌─────────────────────────────────────────────┐
│ agent-backend (this repo)                   │
│  1. Verify HMAC                             │
│  2. Map project=spyroom-platform → platform │
│     "spyroom"; pick failing service from    │
│     payload (api-gateway / auth-service /…) │
│  3. run_analysis(platform=spyroom,          │
│                  service=auth-service,…)    │
│  4. Post MR comment via GitLab API          │
│  5. Slack notify (already wired)            │
└─────────────────────────────────────────────┘
```

### What runs *where*

| Stage | Runner | Why |
|---|---|---|
| test, build, push (both repos) | GitLab SaaS runners | stateless, public images |
| deploy (both repos) | Self-hosted runner tagged `local` on the Mac | Minikube binds to 127.0.0.1 + ~/.minikube TLS — only a local runner can reach it. Already documented in `docs/local-runner-setup.md` |
| notify-copilot | GitLab SaaS runner | just an HTTP POST |

This is the same self-hosted-runner pattern already used by the copilot's own
`deploy` job (see `.gitlab-ci.yml` line 142). The SpyRoom pipeline reuses the
same runner via the `local` tag.

### Where credentials live (Vault flow, course requirement)

```
                    GitLab CI variables
                    (DOCKER_PASSWORD, GOOGLE_API_KEYS,
                     VAULT_ROOT_TOKEN, APPROVAL_SECRET_KEY,
                     GITLAB_WEBHOOK_TOKEN [new])
                            │
                            ▼
          ansible-playbook (vault_secrets role)
                            │
                            ▼
         HashiCorp Vault (in-cluster, k8s.yaml)
                            │
                            ▼
       app/utils/vault_client.py reads on startup
       (env vars are a fallback when Vault is down)
```

The agent-backend never stores raw API keys in env. Vault is the source of
truth. Same flow extends to SpyRoom's JWT signing key and DB password
(optional follow-up: SpyRoom auth-service can fetch its JWT secret from Vault
via the same role).

---

## Out-of-scope (deliberately)

| Wanted feature | Why deferred |
|---|---|
| Full MCP migration | Course doesn't require it; the existing tool interface in `app/llm/tools.py` is already shaped like MCP. The refactor only adds an `execute_tool()` boundary so future MCP wrapping is trivial. |
| Dynamic ServiceRegistry from k8s alone (no yaml) | k8s annotations already work for `depends-on` and `criticality` via `blast_radius.py`. The yaml exists for things k8s annotations can't easily express: GitLab project name, owners, environment list, dry-run policy. Hybrid is the right answer. |
| Workflow engine (Argo, Temporal) | Two-step "run analysis → post MR comment" doesn't need one. |
| Per-platform LLM model selection | Single LLM chain handles all platforms today. Easy to add later as `PlatformConfig.llm_model: Optional[str]`. |
| Pulling SpyRoom source into this repo | Explicit non-goal — see "Repository boundary" at top. |

---

## Verification plan (after refactor lands)

```bash
# 1. Existing tests still green (no regressions)
cd agent-backend && pytest tests/ --ignore=tests/e2e -q
# expect 544+ pass

# 2. New tests green
pytest tests/test_platforms.py -v
# expect: registry load, service lookup, multi-platform causality merge

# 3. Eval suite still passes accuracy gate
python evals/run_evals.py --suite incidents
# expect: ≥70% root_cause accuracy

# 4. SpyRoom platform discoverable via API
curl -s localhost:8001/api/v1/platforms | jq
# expect: ["sample-app", "spyroom", "default"]

# 5. End-to-end: SpyRoom log → ELK → analyze
kubectl apply -f k8s/platforms/spyroom/namespace.yaml
# (deploy SpyRoom into its namespace per its own repo's CI)
kubectl logs -n spyroom deploy/auth-service --tail 20   # see app logs
curl -s 'http://localhost:9200/devops-logs-*/_search?q=service:auth-service' | jq '.hits.total'
# expect: >0

curl -s -X POST localhost:8001/api/v1/analyze \
  -H 'Content-Type: application/json' \
  -d '{"platform":"spyroom","service":"auth-service","environment":"dev","lookback_minutes":10}' \
  | jq '.root_cause, .blast_radius, .proposed_action'

# 6. GitLab webhook signature verification
curl -X POST localhost:8001/api/v1/webhook/pipeline-failure \
  -H 'X-Gitlab-Token: wrong' \
  -d '{"object_kind":"pipeline","project":{"name":"spyroom-platform"}}'
# expect: 403

curl -X POST localhost:8001/api/v1/webhook/pipeline-failure \
  -H "X-Gitlab-Token: $GITLAB_WEBHOOK_TOKEN" \
  -d '{"object_kind":"pipeline","object_attributes":{"status":"failed"},
       "project":{"name":"spyroom-platform"},"commit":{"id":"abc1234"},
       "builds":[{"status":"failed","name":"deploy-auth-service","stage":"deploy"}]}'
# expect: {accepted:true, service:"auth-service", platform:"spyroom"}

# 7. CI eval gate still blocks on accuracy regression
# (run-evals job in .gitlab-ci.yml — already wired)
```

---

## Implementation order in this session

1. **Foundation (Commit 1):** registry, platform configs, pydantic schemas — no behaviour change
2. **Wiring (Commit 2):** thread platform context through agent.py → elk_service → tools → causality
3. **SpyRoom onboarding (Commit 3):** yaml + k8s namespace + filebeat patch + 4 eval fixtures
4. **GitLab integration (Commit 4):** gitlab.py module + webhook hardening + CI template
5. **Docs (Commit 5):** PLATFORMS.md + GITLAB_INTEGRATION.md + README section

Each commit is independently revertible and ships green.
