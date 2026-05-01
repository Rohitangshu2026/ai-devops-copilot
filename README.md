# AI DevOps Copilot

An autonomous CI/CD debugging and self-healing system. The platform ingests
application logs into Elasticsearch via the ELK stack, processes them through
a local analysis pipeline, and calls an LLM to produce structured root-cause
analysis with fix suggestions.

---

## Architecture

```
sample-app ──logs──► Filebeat ──► Logstash ──► Elasticsearch ──► agent-backend ──► Gemini / Claude
                                                    │
                                                 Kibana
```

```
GitLab CI ──► Docker build ──► Docker Hub ──► (Ansible deploy) ──► (Kubernetes)
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| CI/CD | GitLab CI |
| Containerization | Docker, Docker Compose |
| Log shipping | Filebeat 8.12 |
| Log processing | Logstash 8.12 |
| Storage & search | Elasticsearch 8.12 |
| Visualization | Kibana 8.12 |
| Agent API | FastAPI (Python 3.11) |
| LLM | Gemini (google-generativeai) or Claude (anthropic) — switchable via .env |
| Config management | Ansible (Phase 6) |
| Orchestration | Kubernetes (Phase 5) |

---

## Project Status

| Phase | Description | Status |
|---|---|---|
| 1 | ELK log pipeline + sample-app + CI/CD | Done |
| 2 | agent-backend — LLM-powered analysis API | Done |
| 3 | Log intelligence + confidence scoring engine | Upcoming |
| 4 | Agentic LLM with MCP tool use | Upcoming |
| 5 | Safety architecture (decision engine, rollback, audit) | Upcoming |
| 6 | Memory-aware confidence + evaluation metrics | Upcoming |
| 7 | Kubernetes deployment | Upcoming |
| 8 | Ansible + CI/CD automation | Upcoming |

---

## Phase 1 — ELK Log Pipeline

### What was built

- **sample-app** — a FastAPI app (port 8000) that emits structured JSON logs
  to both stdout and `/app/logs/app.log`. Every request is logged with
  `event`, `endpoint`, `status`, `timestamp`, and `level` fields.
- **Filebeat** — tails `app.log` from a shared Docker volume and ships
  entries to Logstash over port 5044.
- **Logstash** — receives Beats input, parses the JSON payload, normalises
  the timestamp into `@timestamp`, and indexes documents into Elasticsearch
  under the pattern `devops-logs-YYYY.MM.dd`.
- **Elasticsearch** — stores and indexes all log documents. Query via
  `localhost:9200/devops-logs-*/_search`.
- **Kibana** — explore logs at `http://localhost:5601`. Create a data view
  with pattern `devops-logs-*`.
- **GitLab CI pipeline** — four stages: test → build → push → deploy.
  - `test`: runs pytest against sample-app (python:3.11)
  - `build`: builds Docker image, saves as `image.tar` artifact
  - `push`: loads artifact, tags with `$CI_COMMIT_SHORT_SHA` + `latest`,
    pushes to Docker Hub (main branch only, uses protected CI variables)
  - `deploy`: manual gate — prints the Ansible command to run

### Key fixes made

- sample-app logger was emitting Python dict repr instead of valid JSON.
  Fixed `_JsonFormatter` to call `json.dumps()`.
- sample-app healthcheck used `curl` which is absent in `python:3.11-slim`.
  Replaced with `python -c "import urllib.request; ..."`.
- GitLab CI: `needs: [build]` and `only: main` are incompatible.
  Replaced `only` with `rules: [{if: $CI_COMMIT_BRANCH == "main"}]`.
- GitLab CI: top-level `IMAGE_NAME: $DOCKER_USERNAME/sample-app` was
  evaluated before protected variables are injected, producing an empty tag.
  Inlined `$DOCKER_USERNAME/sample-app` directly in the script steps.

### Log flow verification

```bash
# generate traffic (hits /, /health x5, /error)
bash scripts/simulate_failure.sh

# confirm documents reached Elasticsearch
curl 'localhost:9200/devops-logs-*/_search?pretty&size=5'
```

---

## Phase 2 — agent-backend

### What was built

A FastAPI service (port 8001) that queries Elasticsearch, runs logs through
a local processing pipeline, and calls an LLM to return a structured
root-cause analysis.

#### Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness check |
| POST | `/api/v1/analyze` | Analyze logs for a service |

#### Request

```json
{
  "service": "sample-app",
  "environment": "dev",
  "lookback_minutes": 30
}
```

`environment` is an enum: `dev` | `staging` | `production`.

#### Response

```json
{
  "service": "sample-app",
  "environment": "dev",
  "root_cause": "Simulated failure intentionally triggered at /error endpoint.",
  "suggestion": "No fix required — this is a test event. In production, add a circuit breaker.",
  "confidence_hint": "high",
  "parsed_log": {
    "error_type": "runtime_crash",
    "severity": "high",
    "key_events": ["..."],
    "summary": "..."
  },
  "raw_evidence": ["..."]
}
```

#### Internal pipeline

```
Elasticsearch query
      │
      ▼
extractor   — filters to ERROR/CRITICAL/WARNING, caps at 50 entries;
              falls back to all entries if no high-signal logs exist
      │
      ▼
parser      — regex patterns detect error_type:
              dependency_error | build_failure | test_failure |
              runtime_crash | unknown
              extracts up to 10 key_events
      │
      ▼
classifier  — assigns severity: critical | high | medium | low
              based on keywords and error_type
      │
      ▼
LLM client  — routes to Gemini (gemini-*) or Claude (claude-*) based on
              LLM_MODEL env var; strips markdown fences from response;
              Anthropic path uses ephemeral prompt caching
      │
      ▼
AnalysisResult (Pydantic model returned as JSON)
```

#### Switching LLM providers

Only `.env` needs to change — no code modifications required:

```env
# Use Gemini (free tier / Pro subscription)
LLM_API_KEY=your-google-ai-studio-key
LLM_MODEL=gemini-2.5-flash

# Use Claude
LLM_API_KEY=your-anthropic-key
LLM_MODEL=claude-sonnet-4-6
```

---

## Running locally

### Prerequisites

- Docker Desktop
- An LLM API key (Google AI Studio or Anthropic)

### Setup

```bash
# 1. Clone
git clone https://gitlab.com/spe-group2/ai-devops-copilot.git
cd ai-devops-copilot

# 2. Configure agent-backend
cp agent-backend/.env.example agent-backend/.env
# edit agent-backend/.env — set LLM_API_KEY and LLM_MODEL

# 3. Start full stack
docker-compose up --build -d

# 4. Wait ~30s for all services to become healthy, then generate log traffic
bash scripts/simulate_failure.sh

# 5. Query the analysis API
curl -s -X POST localhost:8001/api/v1/analyze \
  -H 'Content-Type: application/json' \
  -d '{"service":"sample-app","environment":"dev","lookback_minutes":10}' | jq .
```

### Service URLs

| Service | URL |
|---|---|
| sample-app | http://localhost:8000 |
| agent-backend | http://localhost:8001 |
| Elasticsearch | http://localhost:9200 |
| Kibana | http://localhost:5601 |

### Stopping

```bash
docker-compose down
```

---

## Project Structure

```
.
├── .gitlab-ci.yml          # CI pipeline: test → build → push → deploy
├── docker-compose.yml      # Full local stack
├── scripts/
│   └── simulate_failure.sh # Generates test log traffic
├── sample-app/             # FastAPI app — the monitored service
│   ├── app.py
│   ├── logger.py           # JSON structured logger (stdout + file)
│   ├── Dockerfile
│   └── requirements.txt
├── agent-backend/          # AI analysis service
│   ├── app/
│   │   ├── main.py
│   │   ├── api/routes.py
│   │   ├── core/agent.py       # Orchestration pipeline
│   │   ├── services/elk_service.py
│   │   ├── log_processor/      # extractor, parser, classifier
│   │   ├── llm/                # client (Gemini/Claude), prompt
│   │   ├── models/schemas.py
│   │   └── utils/              # config, logger
│   ├── Dockerfile
│   ├── requirements.txt
│   └── .env.example
├── elk/
│   ├── filebeat.yml        # Filestream input → Logstash output
│   └── logstash.conf       # JSON parse + @timestamp normalisation
├── ansible/                # Deployment automation (Phase 6)
├── k8s/                    # Kubernetes manifests (Phase 5)
└── README.md
```

---

## CI/CD Pipeline

The GitLab pipeline runs on every push. Push and deploy jobs are restricted
to the `main` branch and require protected CI/CD variables (`DOCKER_USERNAME`,
`DOCKER_PASSWORD`) to be set in Settings → CI/CD → Variables.

```
test ──► build ──► push (main only) ──► deploy (main only, manual)
```

The deploy job is a manual gate that prints the Ansible command. Automated
deployment to a server is wired in Phase 8.

---

## Upcoming Phases

### Design Principle

This is a **policy-driven infrastructure platform**, not raw AI automation.
The LLM reasons and proposes. Deterministic layers decide, validate, and act.
Every automated action must pass through a safety pipeline before reaching
Kubernetes. The full control flow:

```
Logs → Summarizer → Confidence → LLM (tool use) → Causality Check
                                       │
                                  Decision Engine
                                       │
                                 Safety Controller
                                  (rate / budget /
                                   idempotency /
                                   dry-run / loops)
                                       │
                              Action Executor (kubectl)
                                       │
                              Partial Failure Handler
                                       │
                            Impact Verifier (did it work?)
                                       │
                              Memory Store + Audit Log
                                       │
                           Memory-Aware Confidence Boost
                               (next incident)
```

---

### Phase 3 — Log Intelligence + Confidence Scoring

**Problem**: Raw K8s and CI/CD logs are verbose, redundant, and low-signal.
Feeding them raw to an LLM exhausts the token budget, increases cost, and
degrades reasoning accuracy.

#### Log Summarizer (`log_processor/summarizer.py`)

Before any log reaches the LLM:

1. **Mask dynamic values** — replace UUIDs, IPs, user IDs, temp paths with
   stable tokens (`192.168.1.5` → `<IP>`, `user_abc123` → `<ID>`) so that
   deduplication works correctly on high-cardinality log data
2. **Deduplicate** — group identical `(event, endpoint, status)` tuples,
   keep first occurrence + frequency count
3. **Time-bucket** — collapse runs of the same event within 60 s:
   `"health_check /health 200 — 47× over 8 min"`
4. **Strip noise** — drop INFO health checks unless they are the only events
5. **Surface signals** — keep all ERROR/WARNING + first and last INFO per endpoint

Output is a `LogSummary` dataclass with `total_events`, `error_count`,
`error_ratio`, `deduplicated_events` (max 20), `time_span_minutes`.

#### Confidence Scoring Engine (`core/confidence.py`)

Replaces the LLM's self-reported `confidence_hint` with a deterministic score:

| Signal | Points |
|---|---|
| error_type is not "unknown" | +2 |
| severity is "high" or "critical" | +2 |
| error_ratio > 10% | +2 |
| 3+ distinct error events | +1 |
| 10+ total events (enough data) | +1 |
| error_type is runtime_crash or build_failure | +1 |

Score ≥ 7 → `high` · ≥ 4 → `medium` · else → `low`

#### Improved LLM Prompt

The summarized `LogSummary` replaces raw log blobs. System prompt uses
chain-of-thought framing and requires the LLM to propose a structured action:

```
Step 1: Identify the most specific error signal.
Step 2: Determine what triggered it.
Step 3: Suggest one concrete, actionable fix.
Step 4: Propose one action: [restart_pod | scale_up | rollback |
                              trigger_retry | notify | no_action]
```

---

### Phase 4 — Agentic LLM with MCP Tool Use

**Problem**: The LLM currently gets a static snapshot of logs. It cannot
ask for more context if the initial summary is ambiguous.

#### MCP Tool Definitions (`llm/tools.py`)

Two tools exposed to the LLM via Claude's `tool_use` / Gemini function calling:

| Tool | Purpose |
|---|---|
| `search_logs` | Search ES for a keyword with optional level filter |
| `get_error_frequency` | Get error counts grouped by endpoint |

#### Agentic Loop

The LLM client runs an iterative loop:
1. Send `LogSummary` + tool definitions
2. If the LLM calls a tool → execute it against Elasticsearch, return result
3. LLM continues reasoning with the new context
4. Hard cap: **3 tool rounds maximum** — prevents runaway LLM calls

This means the LLM can start with a high-level summary, notice an ambiguous
pattern, search for specific evidence, and produce a grounded answer — all
within a single `POST /api/v1/analyze` call.

#### Response Validator

Before any response reaches the Safety Controller:
- `root_cause` must be non-empty and > 10 words
- `suggestion` must contain an actionable verb
- `proposed_action.type` must be a known action name
- Malformed response → one retry with stricter prompt → still malformed → `no_action`

---

### Phase 5 — Safety Architecture

**Core principle**: the LLM proposes; deterministic policy decides.

Every proposed action passes through nine sequential checks:

```
[1] Causality Validation   — is the root cause evidence-backed?
[2] Decision Engine        — is this action allowed for this error/severity/confidence?
[3] Loop Detector          — same failure N times without resolution?
[4] Idempotency Check      — is this action already executing?
[5] Dry-Run Validator      — kubectl --dry-run=server
[6] Safety Controller      — rate limit / budget / namespace / severity gate
[7] Action Executor        — kubectl apply / scale / rollout restart
[8] Partial Failure Check  — did all intended replicas come up?
[9] Impact Verifier        — did the error actually go away?
```

#### Causality Validation (`core/causality.py`)

The LLM's root cause is verified against actual log evidence before any action:

| LLM hypothesis | Required evidence |
|---|---|
| DB connection failure | `connection refused` or `timeout` in logs |
| OOM / memory issue | `OOMKilled` or memory spike in events |
| Build failure | `build fail` or `compilation error` in logs |
| Test failure | `FAILED` or `assertion` in logs |

If the hypothesis is not supported by evidence → action blocked, flagged as
`causality_unverified` in audit log. Prevents acting on plausible-but-wrong explanations.

#### Decision Engine (`core/decision.py`)

Policy table mapping `(error_type, severity, confidence)` to allowed actions:

| Incident | Allowed actions |
|---|---|
| runtime_crash + critical + high | restart_pod, rollback |
| runtime_crash + high + high | restart_pod |
| runtime_crash + high + medium | notify |
| build_failure + high + high | trigger_retry |
| dependency_error + any | notify, no_action |
| unknown + any | no_action |

LLM proposals outside the allowed set are substituted with `no_action` and
the override is recorded in the audit log.

#### Loop Detector (`core/loop_detector.py`)

Tracks how many times the same `(service, error_type)` pair has been actioned
without `outcome: resolved` in the last 60 minutes:
- **≥ 3 failures** → escalate to `notify` only (no automated action)
- **≥ 5 failures** → freeze all automation for the service for 60 min, flag `LOOP_DETECTED`

Prevents the system from repeatedly restarting a service with a non-restartable bug.

#### Idempotency Check

Uses a rolling 120-second query against the Memory Store rather than
time-bucket math (which has a boundary flaw at bucket edges). Any
`(service, action_type)` pair with a recent `executing` or `completed` record
within 120 s is denied.

#### Dry-Run Validator

`kubectl apply --dry-run=server` / `kubectl scale --dry-run=server` runs
before every action. The diff is stored in the audit record. Simulation
failure → action denied regardless of policy.

#### Safety Controller (`core/safety.py`)

Final deterministic gate:
- **Namespace isolation** — deny actions on `kube-system`, `monitoring`
- **Rate limit** — max 3 restarts per service per 10 minutes
- **Action budget** — max 5 automated actions per hour across all services
- **Severity gate** — `restart_pod` / `rollback` only when severity ≥ `high` AND confidence ≥ `medium`
- **Rollback guard** — rollback only when a snapshot exists in the Rollback Registry

#### Action Executor + Partial Failure Handler (`core/action_executor.py`)

Every action returns an `ExecutionResult` with `intended` and `achieved` replica
counts. If `achieved / intended < 0.5` → automatic rollback triggered.
For restarts: if the pod enters `CrashLoopBackOff` within 30 s → status `failed`,
rollback triggered.

#### Impact Verifier (`core/impact.py`)

2 minutes after a successful action, re-queries Elasticsearch and checks
whether the error rate actually dropped. Outcome written back to the incident:
- `resolved` — error rate dropped significantly
- `unresolved` — same error still present (increments Loop Detector count)
- `partial` — error rate reduced but not eliminated

**Terminal rollback failure**: if a rollback itself fails to reach `Running`
state within 90 s (e.g. a DB schema migration already broke the old image),
the system:
1. Sets `action_state: CRITICAL_INTERVENTION_REQUIRED`
2. Freezes all automation for the service indefinitely
3. Fires a `notify` action to human operators
4. Freeze clears only via `POST /api/v1/services/{service}/unfreeze`

#### Rollback Registry (`core/rollback.py`)

Before any action executes, a snapshot of the current deployment state is
stored: previous image tag, replica count, and spec hash. The Safety
Controller refuses any action that has no rollback path.

#### Memory Store + Audit Log (`services/memory_store.py`, `core/audit.py`)

Every incident is stored in Elasticsearch index `devops-incidents-*` with
the full decision trail: log summary, causality evidence, proposed action,
safety decision, dry-run diff, execution result, and final outcome. Nothing
is discarded — the audit log is the source of truth for every automated
decision the system has ever made.

---

### Phase 6 — Memory-Aware Confidence + Evaluation Metrics

#### Memory-Aware Confidence Boost

After computing the base confidence score, the system queries the Memory Store
for similar past incidents. If the same `(service, error_type)` pair was
previously resolved successfully, confidence score gets a +2 boost. The system
becomes more decisive over time on failure patterns it has handled before.
The `confidence_source` field (`signal` vs `memory_boost`) is always recorded.

#### Evaluation Metrics (`GET /api/v1/metrics`)

Rolling 24-hour window computed from the Memory Store:

| Metric | Description |
|---|---|
| `correct_fix_rate` | resolved / total actioned |
| `false_positive_rate` | no_action_needed / total actioned |
| `rollback_frequency` | rolled_back / total actioned |
| `human_override_rate` | safety_denied / total proposed |
| `causality_reject_rate` | causality_unverified / total analyzed |
| `loop_detection_rate` | LOOP_DETECTED / total services |
| `mttr_p50_seconds` | median time to resolution |
| `mttr_p95_seconds` | 95th percentile time to resolution |
| `action_budget_used` | automated actions in last hour |

---

### Phase 7 — Kubernetes Deployment

Deploy the full stack to Kubernetes using Minikube (already running locally).

New manifests:
- `k8s/namespace.yaml` — `devops-copilot` namespace (enforces the namespace
  isolation rule in the Safety Controller)
- `k8s/agent-backend-deployment.yaml` — Deployment + ConfigMap + Secret
  (for `LLM_API_KEY`) + ClusterIP Service
- `k8s/elasticsearch-statefulset.yaml` — StatefulSet + PersistentVolumeClaim
  + ClusterIP Service (StatefulSet needed for stable network identity)
- `k8s/hpa.yaml` — HorizontalPodAutoscaler for agent-backend:
  CPU target 70%, min 1 replica, max 3 replicas

Updated manifests:
- `k8s/deployment.yaml` — fix image tag, add resource requests/limits,
  move to `devops-copilot` namespace
- `k8s/service.yaml` — update selector and namespace

---

### Phase 8 — Ansible + CI/CD Automation

#### Fix Ansible (`ansible/roles/app_deploy/tasks/main.yml`)

Current Ansible tasks use `shell: cmd.exe /c kubectl apply ...` which only
works on Windows. Replace with the cross-platform `kubernetes.core.k8s` module:

```yaml
- name: Apply Kubernetes manifests
  kubernetes.core.k8s:
    state: present
    src: "{{ playbook_dir }}/../k8s/{{ item }}"
  loop:
    - namespace.yaml
    - deployment.yaml
    - service.yaml
    - agent-backend-deployment.yaml
    - hpa.yaml

- name: Wait for rollouts
  command: kubectl rollout status deployment/{{ item }} -n devops-copilot --timeout=120s
  loop: [sample-app, agent-backend]
```

#### Wire CI/CD Deploy Job

Replace the placeholder `echo` commands with a real Ansible run. The runner
needs the `kubernetes` Python package installed (required by `kubernetes.core.k8s`):

```yaml
deploy:
  image: alpine/ansible:latest
  before_script:
    - pip install kubernetes
    - ansible-galaxy collection install kubernetes.core
    - echo "$KUBECONFIG_CONTENT" | base64 -d > /tmp/kubeconfig
    - export KUBECONFIG=/tmp/kubeconfig
  script:
    - cd ansible && ansible-playbook -i inventory.ini deploy.yml
```

Requires a new protected CI/CD variable: `KUBECONFIG_CONTENT` (base64-encoded kubeconfig).

Also adds a `build-agent` CI job to build and push the agent-backend Docker
image alongside sample-app on every merge to `main`.
