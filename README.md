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

bash scripts/simulate_failure.sh
sleep 15

curl -s -X POST http://localhost:8001/api/v1/analyze \
  -H 'Content-Type: application/json' \
  -d '{"service":"sample-app","environment":"dev","lookback_minutes":10}' \
  | jq '{root_cause, confidence_hint, proposed_action, safety_decision, incident_id}'
```

**Services after `docker compose up`:**

| Service | URL |
|---|---|
| Sample app | http://localhost:8000 |
| Agent backend | http://localhost:8001 |
| Kibana | http://localhost:5601 |
| Elasticsearch | http://localhost:9200 |

Kibana dashboards (Error Rate, Log Level Distribution, Endpoint Heatmap) are
automatically imported by the `kibana-setup` container on first start.

---

## Stack

| Layer | Tools |
|---|---|
| Version control | Git + GitLab |
| CI/CD | GitLab CI (test → build → push → deploy) |
| Containerisation | Docker + Docker Compose |
| Configuration management | Ansible (role: `app_deploy`) |
| Orchestration | Kubernetes + HPA (1–5 replicas, CPU 70%) |
| Log pipeline | Filebeat → Logstash → Elasticsearch → Kibana |
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
```

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

### Production

```bash
kubectl apply -f k8s/deployment.yaml    # sample-app Deployment
kubectl apply -f k8s/service.yaml       # sample-app NodePort (30007)
kubectl apply -f k8s/agent-backend.yaml # agent-backend Deployment + ClusterIP
kubectl apply -f k8s/hpa.yaml           # HPA: 1–5 replicas, CPU 70%

kubectl get hpa                         # verify autoscaler
kubectl get pods -o wide
```

### Ansible (CI or local)

```bash
cd ansible
ansible-playbook -i inventory.ini deploy.yml

# Override image tags (CI passes these automatically)
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
# 449 tests pass

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
- **Incidents** written to daily indices `devops-incidents-YYYY.MM.DD` with 90-day ILM delete policy.
- **Pending verifications** queued in ES — the background sweeper recovers them after pod restarts.
- **Action locks and leases** in ES — safe under multi-replica (HPA) deployments.

---

## Project structure

```
.
├── agent-backend/
│   ├── app/
│   │   ├── api/            routes, auth (API-key guard)
│   │   ├── core/           agent, safety (9 gates), decision, causality,
│   │   │                   confidence, loop_detector, rollback, anomaly,
│   │   │                   action_executor, impact, policy, audit, metrics_builder
│   │   ├── llm/            client (multi-provider + cross-model voting),
│   │   │                   tools (3 tools), prompt, sanitize (injection defence)
│   │   ├── log_processor/  summarizer, parser, classifier, extractor
│   │   ├── api/v1/         dashboard (HTML), routes, auth
│   │   └── services/       elk_service, memory_store (leases, ILM)
│   ├── tests/              449 tests; e2e/ auto-skips without cluster
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
│   ├── deployment.yaml     sample-app Deployment (RollingUpdate)
│   ├── agent-backend.yaml  agent-backend Deployment + Service
│   ├── service.yaml        sample-app NodePort
│   ├── hpa.yaml            HPA 1–5 replicas CPU/memory
│   └── test/               ephemeral kind cluster manifests
├── ansible/
│   ├── deploy.yml
│   └── roles/app_deploy/   tasks, handlers, defaults
├── scripts/
│   ├── simulate_failure.sh
│   ├── e2e_setup.sh
│   ├── e2e_teardown.sh
│   ├── gen_eval_fixtures.py   parameterised variant generator
│   └── run_evals.sh           eval suite runner
└── docker-compose.yml
```
