# GitLab CI/CD Integration

This doc explains how the AI DevOps Copilot integrates with GitLab CI for
both **its own pipeline** (this repo) and **monitored applications' pipelines**
(SpyRoom, future apps). It also lists what the integration covers against the
**CSE 816 Software Production Engineering** course requirements.

## TL;DR

| Concern | Where |
|---|---|
| Copilot's own pipeline | `.gitlab-ci.yml` (this repo) |
| Monitored repo's pipeline template | `ci-templates/.gitlab-ci.spyroom.example.yml` |
| Cross-repo failure trigger | `notify-copilot` stage POSTs to `/api/v1/webhook/pipeline-failure` with `X-Gitlab-Token` |
| Webhook authentication | `app/integrations/gitlab.py:verify_webhook_token` (HMAC compare) |
| Read-only API client | `app/integrations/gitlab.py` (MR comments, pipeline fetch) |
| Webhook routing logic | `app/api/v1/webhooks.py` → `PlatformRegistry.get_by_gitlab_project()` |

## Architecture: how the two repos talk to each other

```
┌─────────────────────────────────────────────┐
│ spyroom-platform/main commit                │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│ SpyRoom .gitlab-ci.yml                      │
│                                             │
│   stages: test → build → push → deploy →    │
│           notify-copilot                    │
│                                             │
│   test (parallel):                          │
│     - test-auth-service   (mvn test)        │
│     - test-room-service   (mvn test)        │
│     - test-api-gateway    (mvn test)        │
│     - test-frontend       (npm build)       │
│                                             │
│   build/push:                               │
│     - docker build & push to Docker Hub     │
│                                             │
│   deploy (tags: [local]):                   │
│     - kubectl apply k8s/*                   │
│     - kubectl set image <new sha>           │
│     - kubectl rollout status                │
│                                             │
│   notify-copilot (when: on_failure):        │
│     - POST $COPILOT_WEBHOOK_URL             │
│       Header: X-Gitlab-Token (HMAC)         │
│       Body:   GitLab pipeline payload       │
└──────────────────┬──────────────────────────┘
                   │ (failure path)
                   ▼
┌─────────────────────────────────────────────┐
│ agent-backend (this repo)                   │
│                                             │
│ 1. POST /api/v1/webhook/pipeline-failure    │
│ 2. verify_webhook_token(X-Gitlab-Token)     │
│    → 403 on mismatch                        │
│ 3. registry.get_by_gitlab_project(          │
│       "spe-group2/spyroom-platform")        │
│    → PlatformConfig{name=spyroom, ns=...}   │
│ 4. extract_failing_service(payload,         │
│       known_services=[api-gateway,...])     │
│    → "auth-service"                         │
│ 5. run_analysis(AnalysisRequest(            │
│       platform="spyroom",                   │
│       service="auth-service",               │
│       namespace="spyroom",                  │
│       environment="dev"))                   │
│ 6. (optional) post_mr_comment(...)          │
│ 7. (optional) Slack notify_approval_*       │
└─────────────────────────────────────────────┘
```

## Course (CSE 816) coverage matrix

We are using GitLab CI instead of Jenkins.  The requirements map 1:1 — every
mandatory functionality is implemented; the marks-table items are explicit.

| CSE 816 requirement | Where it lives | Status |
|---|---|---|
| Version Control: Git + remote host | Both repos on GitLab | ✅ |
| CI/CD automation | `.gitlab-ci.yml` (copilot) + `ci-templates/.gitlab-ci.spyroom.example.yml` (template for SpyRoom) | ✅ |
| **Hook trigger on push** | GitLab pipelines fire on push; `rules: if: $CI_COMMIT_BRANCH == "main"` mirrors the Jenkins "GitSCM Polling" behavior | ✅ |
| **Automated tests run** | `test-*` stages (`pytest`, `mvn test`, `npm run lint && npm run build`) | ✅ |
| **Push Docker images** | `push-*` stages publish to Docker Hub on `main` | ✅ |
| **Deploy to target system** | `deploy` stage applies k8s manifests via Ansible on a self-hosted runner (`tags: [local]`) | ✅ |
| **Live patching** | `kubectl set image` + `RollingUpdate maxUnavailable: 0, maxSurge: 1` for zero-downtime rollouts; no full re-deploy needed | ✅ |
| **Logs into ELK + Kibana dashboard** | Filebeat DaemonSet → Logstash → Elasticsearch → Kibana index pattern `devops-logs-*`; multi-namespace filter in `k8s/elk/filebeat.yaml`; Grafana dashboards in `monitoring/grafana/` | ✅ |
| **Vault for secrets** | `ansible/roles/vault_secrets/` deploys HashiCorp Vault; `app/utils/vault_client.py` reads at startup; LLM keys + JWT secrets live in Vault | ✅ |
| **Ansible roles** | `ansible/roles/{vault_secrets,elk_stack,monitoring,app_deploy}/` — fully modular | ✅ |
| **Kubernetes HPA** | `k8s/hpa.yaml` + `k8s/hpa-agent-backend.yaml` — CPU 70%, 1–3 replicas | ✅ |
| **Domain-specific (AIOps)** | The entire Copilot IS AIOps — LLM-driven incident analysis with safety stack, blast radius, eval gates | ✅ |
| **Innovation** | (a) Cross-repo failure trigger via signed webhook routing through a platform registry; (b) Eval framework with 100 labelled scenarios + accuracy gate in CI; (c) Human approval workflow with Slack Block Kit | ✅ |

## What the `notify-copilot` job sends

The job constructs a GitLab-shaped pipeline payload from the CI variables
already available in any GitLab job:

```json
{
  "object_kind": "pipeline",
  "object_attributes": {
    "status": "failed",
    "id": 12345
  },
  "project": {
    "name": "spyroom-platform",
    "path_with_namespace": "spe-group2/spyroom-platform"
  },
  "commit": {
    "id": "abc1234567890abcdef..."
  },
  "builds": [
    {
      "status": "failed",
      "stage": "deploy",
      "name": "deploy-auth-service"
    }
  ]
}
```

The Copilot uses `path_with_namespace` (preferred) or `name` to look up the
platform, then strips the standard prefix (`deploy-`, `test-`, etc.) from the
build name and matches against the platform's declared services.

## How the Copilot authenticates the webhook

`agent-backend/app/integrations/gitlab.py:verify_webhook_token` does a
constant-time HMAC compare on the `X-Gitlab-Token` header against
`settings.gitlab_webhook_token`.  Two modes:

| `GITLAB_WEBHOOK_TOKEN` (env) | Behaviour |
|---|---|
| Unset / empty | Permissive (dev mode) — accepts any token, logs a debug line |
| Set | Strict — mismatch returns HTTP 403 |

Generate a shared secret:

```bash
openssl rand -hex 32
```

Put it in:
* **Copilot side** — `GITLAB_WEBHOOK_TOKEN` env var (preferably via Vault).
  The Vault key is `secret/data/llm-credentials.GITLAB_WEBHOOK_TOKEN`
  (extend `app/utils/vault_client.py` if you want it loaded from there).
* **SpyRoom side** — `COPILOT_WEBHOOK_TOKEN` in GitLab CI/CD Variables
  (Settings → CI/CD → Variables → mark as **Masked**).

## Read-only API client (optional)

When `GITLAB_API_TOKEN` is set on the Copilot side, the webhook handler can
post the analysis summary back as a merge-request comment via
`app/integrations/gitlab.py:post_mr_comment`.  No-op when the token is unset
— development stays frictionless.

```bash
# 1. Create a Personal Access Token in GitLab with `api` scope.
# 2. Set it on the Copilot:
kubectl create secret generic agent-backend-secrets \
  --from-literal=GITLAB_API_TOKEN=glpat-xxxx \
  --dry-run=client -o yaml | kubectl apply -f -
```

## Self-hosted runner for Minikube deploys

Both repos' `deploy` stages target `tags: [local]` so they route to a
self-hosted runner registered on the developer laptop.  Why:

* Minikube's API server binds to `127.0.0.1:<random-port>` and stores TLS
  certs under `~/.minikube/`.
* GitLab SaaS runners run in containers on remote machines and can't reach
  your loopback address or read your home directory.
* A self-hosted shell-executor on the same Mac runs `kubectl` natively,
  shares the existing `~/.kube/config`, and talks to Minikube directly.

One-time setup is documented in `docs/local-runner-setup.md`.  The same
runner serves both repos — register it once, tag both pipelines with `local`.

## Required GitLab CI/CD variables

### This repo (`ai-devops-copilot`)

| Variable | Notes |
|---|---|
| `DOCKER_USERNAME` / `DOCKER_PASSWORD` | DockerHub credentials |
| `LLM_API_KEY`, `GOOGLE_API_KEYS`, `ANTHROPIC_API_KEYS`, `OPENAI_API_KEYS` | At least one provider |
| `VAULT_ROOT_TOKEN` | Used by `vault_secrets` Ansible role |
| `APPROVAL_SECRET_KEY` | HMAC for approval tokens (mark Masked) |
| `GITLAB_WEBHOOK_TOKEN` | NEW — incoming webhook auth (mark Masked) |
| `GITLAB_API_TOKEN` | Optional — for MR comment posting (mark Masked) |
| `SLACK_WEBHOOK_URL` | Optional |

### SpyRoom repo

| Variable | Notes |
|---|---|
| `DOCKER_USERNAME` / `DOCKER_PASSWORD` | DockerHub credentials (mark Masked) |
| `COPILOT_WEBHOOK_URL` | e.g. `http://localhost:8001/api/v1/webhook/pipeline-failure` |
| `COPILOT_WEBHOOK_TOKEN` | Must equal `GITLAB_WEBHOOK_TOKEN` on the Copilot side (Masked) |

## Smoke test the full flow

```bash
# 1. Make sure the Copilot is running and aware of SpyRoom
curl -s localhost:8001/api/v1/platforms | jq '.platforms[] | select(.name == "spyroom")'

# 2. Simulate a SpyRoom pipeline failure
curl -X POST localhost:8001/api/v1/webhook/pipeline-failure \
  -H "Content-Type: application/json" \
  -H "X-Gitlab-Token: $GITLAB_WEBHOOK_TOKEN" \
  -d '{
    "object_kind": "pipeline",
    "object_attributes": {"status": "failed", "id": 999},
    "project": {"name": "spyroom-platform", "path_with_namespace": "spe-group2/spyroom-platform"},
    "commit": {"id": "deadbeef000111222"},
    "builds": [{"status": "failed", "stage": "deploy", "name": "deploy-auth-service"}]
  }'

# expected:
# {
#   "accepted": true,
#   "service": "auth-service",
#   "platform": "spyroom",
#   "namespace": "spyroom",
#   "commit_sha": "deadbeef",
#   "message": "Analysis triggered for auth-service. ..."
# }

# 3. Without the token → 403
curl -i -X POST localhost:8001/api/v1/webhook/pipeline-failure \
  -H "X-Gitlab-Token: wrong-token" \
  -d '{"object_kind":"pipeline","object_attributes":{"status":"failed"},"project":{}}'
# → HTTP/1.1 403 Forbidden
```
