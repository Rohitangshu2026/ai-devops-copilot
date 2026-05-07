# AI DevOps Copilot

An AIOps platform that monitors application logs, reasons about failures using
a multi-provider LLM agentic loop, and proposes or executes remediation actions
through a deterministic safety pipeline.

## What it does

```
Logs → ELK Stack → Log Summariser → Confidence Scorer ← Memory Boost (history)
                                          │
                                    LLM (tool use)
                                    ├── search_logs
                                    ├── get_error_frequency
                                    └── get_k8s_events
                                          │
                                  Cross-Model Voting       ← Phase 9d
                                  (destructive actions)
                                          │
                                    Causality Checker
                                          │
                                    Decision Engine (policy.yaml)
                                          │
                                  Safety Controller (9 gates)
                                  ├── 0: Anomaly gate (z-score baseline) ← Phase 9e
                                  ├── 1: Causality gate
                                  ├── 2: Decision policy
                                  ├── 3: Loop detector
                                  ├── 4: Idempotency lock
                                  ├── 5: Namespace isolation
                                  ├── 6: Severity gate
                                  ├── 7: Rate limit
                                  └── 8: Action budget
                                          │
                                    Action Executor (kubectl, async)
                                          │
                                    Impact Verifier (ES-backed sweeper)
                                          │
                                    Audit Log (devops-incidents-*)
                                          │
                                  Memory Store ← feeds confidence boost + baselines
```

---

## Quick start (docker-compose)

```bash
git clone https://gitlab.com/spe-group2/ai-devops-copilot.git
cd ai-devops-copilot

docker compose up --build -d
# Wait ~60s for Elasticsearch to become healthy

# Full end-to-end demo: failure → logs → AI → approval → remediation → verification
bash scripts/demo.sh

# Or run pieces manually:
bash scripts/simulate_failure.sh
sleep 15
curl -s -X POST http://localhost:8001/api/v1/analyze \
  -H 'Content-Type: application/json' \
  -d '{"service":"sample-app","environment":"dev","lookback_minutes":10}' \
  | jq '{root_cause, confidence_hint, proposed_action, safety_decision, incident_id}'
```

📖 **Detailed walkthrough:** [`docs/demo.md`](docs/demo.md) — narrates the full
incident lifecycle with sample outputs, dashboard screenshots, and grading
criteria mapping.

**Services after `docker compose up`:**

| Service | URL | Purpose |
|---|---|---|
| Sample app | http://localhost:8000 | Target application with failure-mode endpoints |
| Agent backend | http://localhost:8001 | AI analysis pipeline + safety controller |
| Agent dashboard | http://localhost:8001/dashboard | Server-rendered incident audit |
| Kibana | http://localhost:5601 | Application log visualizations |
| Elasticsearch | http://localhost:9200 | Log + incident store |
| Prometheus | http://localhost:9090 | Scrapes agent-backend `/metrics` |
| Grafana | http://localhost:3000 | Agent metrics dashboard (anonymous viewer) |

Kibana dashboards (Error Rate, Log Level Distribution, Endpoint Heatmap) are
automatically imported by the `kibana-setup` container on first start.
Grafana auto-loads the **AI DevOps Copilot — Agent Backend** dashboard from
`monitoring/grafana-dashboard.json`.

---

## Stack

| Layer | Tools |
|---|---|
| Version control | Git + GitLab |
| CI/CD | GitLab CI (test → evals → build → push → deploy) |
| Containerisation | Docker + Docker Compose |
| Configuration management | Ansible (roles: `vault_secrets`, `app_deploy`) |
| Orchestration | Kubernetes + HPA (1–3 replicas, CPU 70% / Memory 80%) |
| Log pipeline | Filebeat → Logstash → Elasticsearch → Kibana |
| Secret management | HashiCorp Vault (KV v2, dev mode in k8s) |
| LLM providers | Google Gemini / Anthropic Claude / OpenAI (configurable fallback chain) |

---

## CI/CD pipeline

Every push runs tests for both services. Merges to `main` build and push both
Docker images to DockerHub, then offer a manual deploy gate that runs Ansible
on a self-hosted runner.

```
git push
    │
    ├── test-sample-app   ─┐
    ├── test-agent-backend ┘  GitLab SaaS runners (remote)
    │
    ├── build-sample-app   ─┐
    ├── build-agent-backend ┘  GitLab SaaS runners
    │
    ├── push-sample-app   ─┐   GitLab SaaS runners → DockerHub
    ├── push-agent-backend ┘   (main branch only)
    │
    └── deploy  [manual]       self-hosted runner on your Mac
                    │
                    └── ansible-playbook → kubectl apply → Minikube
```

### Why the deploy job uses a self-hosted runner

GitLab SaaS runners run in remote containers that cannot reach a Minikube
cluster because Minikube binds its API server to `127.0.0.1` on your Mac and
stores TLS certificates under `~/.minikube/` — neither is reachable from a
remote machine. A shell-executor runner registered on the same Mac runs
`kubectl` natively using the existing `~/.kube/config` with no extra config.

**One-time runner setup:** see [`docs/local-runner-setup.md`](docs/local-runner-setup.md)

### Required CI variables (Settings → CI/CD → Variables)

| Variable | Description |
|---|---|
| `DOCKER_USERNAME` | DockerHub username |
| `DOCKER_PASSWORD` | DockerHub password or access token *(mark as Masked)* |
| `LLM_API_KEY` | Primary LLM API key — injected into Vault by `vault_secrets` role |
| `GOOGLE_API_KEYS` | Google Gemini API keys (comma-separated) |
| `ANTHROPIC_API_KEYS` | Anthropic Claude API keys |
| `OPENAI_API_KEYS` | OpenAI API keys (optional) |
| `VAULT_ROOT_TOKEN` | HashiCorp Vault root token *(Masked)* — defaults to `devops-copilot-root-token` |
| `SLACK_WEBHOOK_URL` | (optional) Slack incoming webhook for approval alerts |
| `APPROVAL_SECRET_KEY` | HMAC-SHA256 key for approval tokens *(Masked)* |

`KUBECONFIG_CONTENT` is **not needed** — the shell executor on your Mac uses
`~/.kube/config` directly.

---

## LLM configuration (`agent-backend/.env`)

```bash
# Primary model
LLM_MODEL=gemini-2.0-flash

# Fallback chain — tried in order when keys for the primary are exhausted
LLM_MODEL_FALLBACK=claude-haiku-4-5,gpt-4o-mini

# Per-provider key pools (comma-separated for rotation on rate-limit)
GOOGLE_API_KEYS=key1,key2
ANTHROPIC_API_KEYS=key1
OPENAI_API_KEYS=key1,key2

# Admin API key — protects /admin/* and /services/*/unfreeze endpoints
# Leave blank to disable auth in local dev mode
ADMIN_API_KEY=

# Phase 11d — Human approval workflow
APPROVAL_SECRET_KEY=change-me-in-production   # HMAC-SHA256 key for approval tokens
APPROVAL_EXPIRY_SECONDS=300                    # Token expiry (default: 5 min)
SLACK_WEBHOOK_URL=                             # Slack Block Kit notifications (optional)

# Phase 10 — HashiCorp Vault (auto-configured in k8s; not needed in docker-compose)
VAULT_ADDR=http://vault:8200
VAULT_TOKEN=devops-copilot-root-token
```

---

## HashiCorp Vault (Phase 10)

In Kubernetes, LLM API keys are stored in Vault instead of plain environment
variables.  The Ansible `vault_secrets` role provisions Vault on first deploy:

1. Deploys `k8s/vault.yaml` (Vault pod in dev mode — for dev/demo clusters)
2. Enables KV v2 secrets engine at `secret/`
3. Writes `LLM_API_KEY`, `GOOGLE_API_KEYS`, `ANTHROPIC_API_KEYS`, `OPENAI_API_KEYS`,
   `LLM_MODEL` to `secret/data/llm-credentials`

The `agent-backend` reads secrets at startup from Vault via `app/utils/vault_client.py`.
If Vault is unreachable (docker-compose mode), it falls back to environment variables.

```bash
# Verify secrets after deploy
kubectl exec -it vault-0 -n devops-copilot -- vault kv get secret/llm-credentials
```

---

## Blast-radius estimation (Phase 10)

Every analysis computes how many services would be affected if the proposed
action is executed:

```json
"blast_radius": {
  "score": "high",
  "affected_count": 4,
  "affected_services": ["api-gateway", "auth-service", "payment-api", "sample-app"],
  "direct_dependents": ["api-gateway"],
  "source": "k8s_annotations"
}
```

Blast radius is computed by BFS on the **reverse** dependency graph built from
`devops-copilot/depends-on` annotations on Deployments (cached 60s; falls back
to the static map in `causality.py`).

Scores: `0 affected → low` · `1–2 → medium` · `3–5 → high` · `>5 → critical`

The safety gate (gate 6b) tightens requirements based on blast radius:

| Blast radius | Required confidence | Required severity |
|---|---|---|
| `critical` | `high` | `critical` |
| `high` | `high` | `high` |
| `medium` | `medium` | `high` |
| `low` | (no change) | (no change) |

---

## Human approval workflow (Phase 11)

High-impact actions are gated behind human approval before execution.

### When approval is required

Any of:
- Service has `devops-copilot/criticality: critical` annotation
- Action type is `rollback`
- Blast-radius score is `high` or `critical`
- Destructive action with confidence < `high`

### Approval flow

```
analyze() called
    │
    ├── safety gates pass → requires_approval() check
    │       │
    │       ├── approval required → create ApprovalRequest → send Slack notification
    │       │       │
    │       │       └── action_state: "awaiting_approval"
    │       │
    │       └── no approval required → execute immediately
    │
    └── Operator clicks Approve (or Reject) in Slack / calls API
            │
            └── POST /api/v1/approvals/{id}/approve?token=<hmac>
                    │
                    └── token verified → action_state: "approved" → execute
```

Approval tokens are HMAC-SHA256 signed (`APPROVAL_SECRET_KEY`).  Forged or
expired tokens return 403.  The token is embedded in the Slack button URLs.

### Slack notification

When `SLACK_WEBHOOK_URL` is set, approval-required incidents send a Block Kit
message:

```
⚠️  Approval Required — restart_pod sample-app
Service: sample-app    Action: restart_pod
Blast Radius: high     Confidence: 8/10
Reason: destructive action on blast-radius=high service
Confidence breakdown:
  • +2 error_type=runtime_crash (known type)
  • +2 severity=high
  ...
[✅ Approve]   [❌ Reject]
```

Unset `SLACK_WEBHOOK_URL` → feature is a no-op and actions execute directly
(`dev`-mode behaviour unchanged).

---

## Pipeline failure webhook (Phase 11)

Register `POST /api/v1/webhook/pipeline-failure` as a webhook in GitLab
(Settings → Webhooks → Pipeline events) or GitHub Actions (repository dispatch):

```bash
# GitLab CI — set in Settings → Webhooks
URL: http://<agent-backend-host>:8001/api/v1/webhook/pipeline-failure
Trigger: Pipeline events

# Test manually
curl -s -X POST http://localhost:8001/api/v1/webhook/pipeline-failure \
  -H 'Content-Type: application/json' \
  -d '{
    "object_kind": "pipeline",
    "object_attributes": {"status": "failed"},
    "project": {"name": "sample-app"},
    "commit": {"id": "abc12345"},
    "builds": []
  }' | jq
```

On receipt, the webhook extracts the failing service name from the payload
and fires an analysis in the background (non-blocking — the webhook responds
in <1s).  Results appear in the audit dashboard and `GET /api/v1/metrics`.

---

## API reference

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/api/v1/analyze` | — | Run full analysis pipeline |
| `GET` | `/api/v1/incidents/{id}` | — | Poll async action state |
| `GET` | `/api/v1/incidents/{id}/timeline` | — | Chronological event timeline for an incident |
| `GET` | `/api/v1/metrics` | — | JSON operational metrics (fix rate, MTTR, …) |
| `GET` | `/metrics` | — | Prometheus scrape endpoint |
| `GET` | `/dashboard` | — | HTML incident dashboard (last 50 incidents) |
| `POST` | `/api/v1/services/{service}/unfreeze` | Admin key | Clear a frozen service |
| `POST` | `/api/v1/admin/reload-policy` | Admin key | Hot-reload `policy.yaml` |
| `POST` | `/api/v1/webhook/pipeline-failure` | — | GitLab CI / GitHub Actions failure webhook |
| `GET` | `/api/v1/approvals/{id}` | — | Get pending approval request status |
| `POST` | `/api/v1/approvals/{id}/approve` | Signed token | Approve a high-impact action |
| `POST` | `/api/v1/approvals/{id}/reject` | Signed token | Reject a high-impact action |
| `GET` | `/health` | — | Liveness check |

Admin endpoints require the `X-Admin-Key: <value>` header when `ADMIN_API_KEY`
is set in `.env`.

### Analyze — example response

```json
{
  "root_cause": "Connection refused to downstream elasticsearch:9200",
  "confidence_hint": "high",
  "confidence_score": 8,
  "confidence_breakdown": [
    "+2 error_type=dependency_error",
    "+2 severity=high",
    "+2 error_ratio=0.80 (>10%)",
    "+1 event_count=12 (>=10)",
    "+1 historical_match (2 of 3 similar incidents resolved)"
  ],
  "proposed_action": {"type": "notify", "target": "sample-app", "reason": "..."},
  "safety_decision": "allowed",
  "causality_verified": true,
  "incident_id": "550e8400-e29b-41d4-a716-446655440000",
  "execution_result": {"status": "executing"},
  "anomaly_score": 3.14,
  "cross_validation": {"agreed": true, "primary_model": "gemma-4-31b-it", "secondary_model": "claude-haiku-3-5"},
  "cascade_depth": 1,
  "upstream_incident_id": "db-incident-uuid",
  "incident_chain_id": "chain-uuid"
}
```

`/analyze` returns immediately. Poll `GET /api/v1/incidents/{id}` for the
final `action_state` when an action is executing asynchronously.

---

## Sample-app failure endpoints

| Endpoint | Behaviour | Env var |
|---|---|---|
| `GET /error` | Always returns `status: error` | — |
| `GET /slow` | Sleeps `SLOW_MS` ms; 504 if > `SLOW_THRESHOLD_MS` | `SLOW_MS`, `SLOW_THRESHOLD_MS` |
| `GET /crash` | Raises `RuntimeError(CRASH_MESSAGE)`; always 500 | `CRASH_MESSAGE` |
| `GET /dep-error` | Calls `DOWNSTREAM_URL`; 503 on failure | `DOWNSTREAM_URL` |
| `GET /oom` | Allocates `MEM_MB` MB in memory | `MEM_MB` |

---

## Safety pipeline (9 gates)

Every proposed action passes all gates sequentially before touching the cluster:

| # | Gate | Blocks when |
|---|---|---|
| 0 | **Anomaly gate** | z-score < 2.0 vs 7-day baseline — error rate is statistically routine |
| 1 | Causality | Root cause not evidenced in logs |
| 2 | Decision policy | Action type not allowed for `(error_type, severity, confidence)` |
| 3 | Loop detector | ≥5 consecutive unresolved actions → service frozen |
| 4 | Idempotency lock | Identical action already in flight (atomic ES `op_type=create`) |
| 5 | Namespace isolation | Target is `kube-system` or `monitoring` |
| 6 | Severity gate | Destructive action on low severity or low confidence |
| 7 | Rate limit | >3 restarts for this service in 10 minutes |
| 8 | Action budget | >5 automated actions across all services in 1 hour |

Gate 0 only fires once the service has ≥5 baseline samples (7-day rolling window
updated nightly). A new service bypasses the anomaly gate until enough history
accumulates. The `anomaly_score` z-score is included in every `AnalysisResult`.

Failures are downgraded to `notify` and recorded in the incident audit log.

All thresholds are editable in `policy.yaml` — reload live with `SIGHUP` or
`POST /api/v1/admin/reload-policy`.

---

## Kubernetes deployment

The full production stack (sample-app + agent-backend + ELK + Prometheus + Grafana
+ Vault + RBAC + HPAs) is layered into four directories:

```
k8s/
├── *.yaml             ← application layer (sample-app, agent-backend, vault, RBAC, HPAs)
├── elk/               ← Elasticsearch StatefulSet, Logstash, Kibana, Filebeat DaemonSet
├── monitoring/        ← Prometheus + Grafana
└── test/              ← ephemeral kind cluster manifests for e2e tests
```

### One-command deploy via Ansible (recommended)

```bash
cd ansible
ansible-playbook -i inventory.ini deploy.yml
```

The playbook runs four roles in order: `vault_secrets` → `elk_stack` →
`monitoring` → `app_deploy`. Each role is independently re-runnable and
selectable via tags:

```bash
ansible-playbook -i inventory.ini deploy.yml --tags elk        # only ELK
ansible-playbook -i inventory.ini deploy.yml --skip-tags monitoring
```

### Manual kubectl apply (production)

```bash
kubectl apply -f k8s/vault.yaml             # secret store
kubectl apply -f k8s/elk/                   # ES + Logstash + Kibana + Filebeat
kubectl apply -f k8s/monitoring/            # Prometheus + Grafana
kubectl apply -f k8s/rbac.yaml              # agent-backend ServiceAccount + ClusterRole
kubectl apply -f k8s/networkpolicy.yaml     # in-cluster-only access
kubectl apply -f k8s/deployment.yaml        # sample-app (RollingUpdate, 2 replicas)
kubectl apply -f k8s/service.yaml           # sample-app NodePort (30007)
kubectl apply -f k8s/agent-backend.yaml     # agent-backend Deployment + ClusterIP
kubectl apply -f k8s/hpa.yaml               # sample-app HPA (1–5)
kubectl apply -f k8s/hpa-agent-backend.yaml # agent-backend HPA (1–3)

kubectl get pods,hpa -o wide
```

### Image tag overrides (CI deployments)

```bash
ansible-playbook -i inventory.ini deploy.yml \
  -e "sample_app_image=logicule/sample-app:abc1234" \
  -e "agent_image=logicule/agent-backend:abc1234"
```

### Local kind cluster (testing)

```bash
bash scripts/e2e_setup.sh
kubectl port-forward svc/agent-backend 8001:8001 -n devops-test &
kubectl port-forward svc/sample-app    8000:8000 -n devops-test &
bash scripts/e2e_teardown.sh   # cleanup
```

---

## Testing

```bash
# Unit + integration tests (no cluster, no real LLM)
cd agent-backend
pytest tests/ --ignore=tests/e2e --ignore=tests/test_integration_llm.py -q
# 544 tests pass

# Sample-app tests
cd sample-app && pytest test_app.py -q
# 12 tests pass

# Real LLM integration (API key required)
GOOGLE_API_KEYS="AIza..." pytest agent-backend/tests/test_integration_llm.py -v -s

# End-to-end against kind cluster
bash scripts/e2e_setup.sh
pytest agent-backend/tests/e2e/ -v -s

# Offline eval suite (20 hand-authored scenarios)
bash scripts/run_evals.sh
# Expected: ≥70% root-cause accuracy, ≥80% action correctness, ≤20% false-positive rate

# Generate 80 parameterized variants and run all 100
bash scripts/run_evals.sh --generated
```

---

## Observability

| Endpoint | What it returns |
|---|---|
| `GET /dashboard` | HTML table of the last 50 incidents with service/outcome filters |
| `GET /api/v1/incidents/{id}/timeline` | Chronological events: error spike → analysis → tool calls → safety → action → impact |
| `GET /api/v1/metrics` | JSON: fix rate, false-positive rate, MTTR p50/p95, safety denials, frozen services |
| `GET /metrics` | Prometheus text format — scrape with any standard collector |

Prometheus metrics exported:
- `agent_analysis_total{service,outcome}` — incidents processed
- `agent_analysis_duration_seconds` — end-to-end latency histogram
- `agent_llm_call_total{provider,model,result}` — LLM call outcomes
- `agent_llm_call_duration_seconds{provider}` — LLM latency
- `agent_safety_denials_total{reason}` — which gate is blocking most often
- `agent_actions_executed_total{action_type,status}` — remediation outcomes

---

## Trust & empirical validation

The system's decision path has two independent layers to prevent hallucinated actions:

**Cross-model voting (Phase 9d):** For destructive actions (`restart_pod`, `rollback`,
`scale_up`) on `high`/`critical` severity incidents, a second LLM is consulted.
If the two models propose different actions, the result is downgraded to `notify`
and the disagreement is recorded in `cross_validation` in the response.

**Statistical anomaly baseline (Phase 9e):** Each service maintains a rolling 7-day
baseline of error ratio in Elasticsearch (`devops-baselines` index). The anomaly
gate (gate #0) computes a z-score on each analysis. If z < 2.0 the error rate is
not statistically anomalous — destructive actions are blocked regardless of what
the LLM proposes. Baselines are refreshed nightly by a background sweeper. A
service with fewer than 5 baseline samples bypasses the gate.

**Offline eval suite:** 20 hand-authored scenarios covering all major failure
archetypes (OOM, CrashLoop, dependency errors, build failures, and healthy traffic
that should not trigger actions). Run with `bash scripts/run_evals.sh`.

---

## Persistence

- **Elasticsearch data** survives `docker compose down/up` via the `esdata` named volume.
- In K8s, ES uses a StatefulSet with a 5 GiB PVC (`k8s/elk/elasticsearch.yaml`).
- **Incidents** written to daily indices `devops-incidents-YYYY.MM.DD` with 90-day ILM delete policy.
- **Pending verifications** queued in ES — the background sweeper recovers them after pod restarts.
- **Action locks and leases** in ES — safe under multi-replica (HPA) deployments.

---

## CSE 816 evaluation criteria mapping

| Criterion | Marks | Where it lives |
|---|---|---|
| **Working Project (20)** | 20 | All flows wired end-to-end ([demo.md](docs/demo.md)) |
| Git push triggers fetch → build → test | – | `.gitlab-ci.yml` test + evals + build stages |
| Push to DockerHub | – | `.gitlab-ci.yml` push-{sample-app,agent-backend} on main |
| Deploy via Ansible | – | `.gitlab-ci.yml` deploy stage; `ansible/deploy.yml` |
| Refresh shows changes seamlessly | – | `k8s/deployment.yaml` (2 replicas + maxUnavailable=0) |
| Logs feed into ELK | – | `k8s/elk/filebeat.yaml` DaemonSet → Logstash → ES |
| Kibana dashboards | – | `elk/kibana-dashboard.ndjson` auto-imported |
| **Advanced Features (3)** | 3 | |
| Vault for secure credentials | – | `k8s/vault.yaml`, `app/utils/vault_client.py`, `ansible/roles/vault_secrets` |
| Roles in Ansible (modular) | – | 4 roles: `vault_secrets`, `elk_stack`, `monitoring`, `app_deploy` |
| Kubernetes HPA | – | `k8s/hpa.yaml` (sample-app), `k8s/hpa-agent-backend.yaml` (agent) |
| **Innovation (2)** | 2 | |
| AIOps domain | – | LLM agentic loop + 9-gate safety stack + cross-model voting + statistical anomaly baseline + blast-radius graph + HMAC-signed approval workflow + offline eval suite |

---

## Project structure

```
.
├── agent-backend/
│   ├── app/
│   │   ├── api/            routes, auth (API-key guard)
│   │   ├── core/           agent, safety (9 gates), decision, causality,
│   │   │                   confidence, loop_detector, rollback, anomaly,
│   │   │                   blast_radius, approval, action_executor,
│   │   │                   impact, policy, audit, metrics_builder
│   │   ├── llm/            client (multi-provider + cross-model voting),
│   │   │                   tools (3 tools), prompt, sanitize (injection defence)
│   │   ├── log_processor/  summarizer, parser, classifier, extractor
│   │   ├── api/v1/         dashboard (HTML), routes, webhooks, auth
│   │   ├── integrations/   slack (Block Kit approval notifications)
│   │   └── services/       elk_service, memory_store (leases, ILM)
│   ├── tests/              544 tests; e2e/ auto-skips without cluster
│   └── policy.yaml         live-editable safety policy
├── sample-app/             FastAPI app with injectable failure endpoints
├── evals/
│   ├── incidents/          20 hand-authored eval fixture archetypes
│   ├── generated/          80 parameterised variants (git-ignored)
│   ├── results/            timestamped JSON eval reports
│   └── run_evals.py        offline eval harness
├── elk/
│   ├── logstash.conf       JSON parse + ES output
│   ├── filebeat.yml        reads /app/logs/app.log
│   └── kibana-dashboard.ndjson   auto-imported dashboard
├── k8s/
│   ├── deployment.yaml         sample-app Deployment (2 replicas, RollingUpdate)
│   ├── agent-backend.yaml      agent-backend Deployment + Service + criticality annotation
│   ├── service.yaml            sample-app NodePort
│   ├── hpa.yaml                HPA 1–5 replicas CPU/memory (sample-app)
│   ├── hpa-agent-backend.yaml  HPA 1–3 replicas CPU 70% / Memory 80%
│   ├── vault.yaml              HashiCorp Vault (dev mode) Deployment + Service
│   ├── rbac.yaml               ServiceAccount + ClusterRole for agent-backend
│   ├── networkpolicy.yaml      In-cluster-only access for agent-backend
│   ├── namespace.yaml          devops-copilot Namespace
│   ├── elk/
│   │   ├── elasticsearch.yaml  StatefulSet + PVC + ILM bootstrap Job
│   │   ├── logstash.yaml       Deployment + ConfigMap (Beats input → ES output)
│   │   ├── kibana.yaml         Deployment + dashboard auto-import Job
│   │   └── filebeat.yaml       DaemonSet + RBAC (ships container logs)
│   ├── monitoring/
│   │   ├── prometheus.yaml     Deployment + RBAC (scrapes agent-backend /metrics)
│   │   └── grafana.yaml        Deployment + datasource + dashboard provisioning
│   └── test/                   ephemeral kind cluster manifests
├── monitoring/
│   └── grafana-dashboard.json  6-panel agent metrics dashboard
├── ansible/
│   ├── deploy.yml              vault_secrets → elk_stack → monitoring → app_deploy
│   └── roles/
│       ├── vault_secrets/      Vault provisioning + secret injection
│       ├── elk_stack/          ELK deployment + dashboard ConfigMap
│       ├── monitoring/         Prometheus + Grafana + dashboard provisioning
│       └── app_deploy/         tasks, handlers, defaults
├── docs/
│   ├── demo.md                 End-to-end walkthrough with grading mapping
│   ├── local-runner-setup.md   Self-hosted GitLab runner setup
│   └── screenshots/            Kibana / Grafana / dashboard screenshots
├── scripts/
│   ├── demo.sh                 Interactive end-to-end walkthrough
│   ├── simulate_failure.sh     Inject errors into sample-app
│   ├── e2e_setup.sh            Bootstrap kind cluster
│   ├── e2e_teardown.sh         Tear down kind cluster
│   ├── gen_eval_fixtures.py    Parameterised variant generator
│   └── run_evals.sh            Eval suite runner
└── docker-compose.yml
```
