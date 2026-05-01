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
| 3 | Confidence scoring engine (rule-based) | Upcoming |
| 4 | Self-healing actions | Upcoming |
| 5 | Kubernetes deployment | Upcoming |
| 6 | Ansible provisioning | Upcoming |

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
deployment to a server is wired in Phase 6.
