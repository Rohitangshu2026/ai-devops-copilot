# Onboarding a Platform

The AI DevOps Copilot can monitor any external application repository through
a thin **platform config**.  A platform is a logical bundle of services that
share an ownership boundary (typically one repo).  The Copilot was originally
hardcoded around a single demo service (`sample-app`); the multi-platform
refactor moves all per-app knowledge into `configs/platforms/*.yaml` so new
apps can be onboarded **without code changes**.

This guide walks through onboarding a new platform end-to-end using
**SpyRoom** as the worked example.  Pre-built artifacts for SpyRoom already
ship in this repo — replace `spyroom` with your platform name to reuse the
pattern.

## TL;DR (4 steps)

1. **Add a yaml** at `configs/platforms/<your-platform>.yaml` describing
   services, dependencies, and the GitLab project path.
2. **Create a k8s namespace manifest** at `k8s/platforms/<your-platform>/namespace.yaml`.
3. **Add the namespace** to the Filebeat `drop_event` filter in
   `k8s/elk/filebeat.yaml` so logs flow into the shared ELK stack.
4. **Tell your repo's CI** to POST to the Copilot webhook on failure (use
   `ci-templates/.gitlab-ci.spyroom.example.yml` as a starting point).

That's it.  No code changes in the Copilot.

## Step 1 — Write the platform yaml

`configs/platforms/<your-platform>.yaml`:

```yaml
name: spyroom               # used by /api/v1/analyze {"platform": "spyroom", ...}
namespace: spyroom          # Kubernetes namespace that holds your pods
environments: [dev, staging, production]
dry_run_envs: [dev]         # destructive actions are simulated in these envs

log_index_pattern: devops-logs-*   # shared index; override for per-platform isolation
log_service_field: service          # ES field that holds the service name

services:
  - name: auth-service
    language: java
    depends_on: [postgres]
    criticality: critical          # gates which actions Copilot will propose
  - name: room-service
    depends_on: [postgres, redis]
    criticality: high
  - name: api-gateway
    depends_on: [auth-service, room-service]
    criticality: high
  # Data plane — listed so they show up in the blast-radius graph.
  - name: postgres
    criticality: critical
    deployment_kind: StatefulSet
  - name: redis
    criticality: high

# Cross-repo wiring
gitlab_project: "spe-group2/spyroom-platform"
repo_url: "https://gitlab.com/spe-group2/spyroom-platform"

owners: ["@spyroom-oncall"]
metadata:
  description: "SpyRoom chat platform"
```

### What each field controls

| Field | Read by | Effect |
|---|---|---|
| `name` | analyze API, dashboard | Routes `POST /api/v1/analyze {"platform": "..."}` |
| `namespace` | `agent.py`, `tools.py` (k8s events) | Default k8s namespace for log + event queries |
| `environments` | schema validation | Whitelist of accepted environment values |
| `dry_run_envs` | `agent.py` | Action executor runs in dry-run mode for these envs |
| `log_index_pattern` | `elk_service.py` | ES index to query for this platform |
| `log_service_field` | `elk_service.py` | ES field holding the service name (default `service`) |
| `services[].name` | webhook routing, causality | Must match the container name / ES `service` field |
| `services[].depends_on` | `causality.py`, `blast_radius.py` | Static dependency graph fallback (live annotations win) |
| `services[].criticality` | `safety.py`, `blast_radius.py` | Tightens approval/severity gates |
| `gitlab_project` | webhook handler | Maps incoming pipeline-failure webhooks to this platform |
| `owners` | Slack integration | Tag operators on approval-required incidents |

### Validation

Platform yamls are validated against the pydantic schema in
`agent-backend/app/platforms/registry.py` at startup.  A malformed file is
**logged and skipped** — the registry stays usable with the platforms that
did parse, but the bad file does not load.

To validate without starting the server:

```bash
cd agent-backend
python -c "from app.platforms.registry import PlatformRegistry; \
           PlatformRegistry('configs/platforms')"
```

CI runs the same check in the `validate-platforms` job (see `.gitlab-ci.yml`).

## Step 2 — Create the Kubernetes namespace

`k8s/platforms/<your-platform>/namespace.yaml`:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: spyroom
  labels:
    app.kubernetes.io/managed-by: ai-devops-copilot
    devops-copilot/platform: spyroom
    devops-copilot/scrape: "true"
  annotations:
    devops-copilot/gitlab-project: "spe-group2/spyroom-platform"
```

Apply with the existing Ansible deploy (or directly):

```bash
kubectl apply -f k8s/platforms/spyroom/namespace.yaml
```

## Step 3 — Extend the Filebeat path filter

Filebeat's container-log scraper drops events whose namespace is not in an
allow-list.  The allow-list lives in `k8s/elk/filebeat.yaml`:

```yaml
- drop_event:
    when:
      not:
        or:
          - contains: { log.file.path: "_default_" }
          - contains: { log.file.path: "_devops-test_" }
          - contains: { log.file.path: "_spyroom_" }   # ← add your namespace here
```

The namespace is embedded in the container log filename
(`<pod>_<namespace>_<container>-<id>.log`) so this filter requires **no k8s
API call** — it's resilient to RBAC races and expired tokens.

Apply and restart Filebeat:

```bash
kubectl apply -f k8s/elk/filebeat.yaml
kubectl rollout restart daemonset/filebeat
```

After that, any pod logs in your namespace show up in ES:

```bash
curl -s 'http://localhost:9200/devops-logs-*/_search?q=kubernetes.namespace:spyroom&size=1' | jq
```

## Step 4 — Wire your repo's CI to the Copilot webhook

A complete production-style template is in
`ci-templates/.gitlab-ci.spyroom.example.yml`.  The relevant fragment is the
`notify-copilot` stage:

```yaml
notify-copilot:
  stage: notify
  image: alpine:3.19
  when: on_failure
  rules:
    - if: $CI_COMMIT_BRANCH == "main"
  before_script:
    - apk add --no-cache curl jq
  script:
    - |
      PAYLOAD=$(jq -nc \
        --arg name "$CI_PROJECT_NAME" --arg path "$CI_PROJECT_PATH" \
        --arg sha "$CI_COMMIT_SHA" --arg pipe "$CI_PIPELINE_ID" \
        --arg job "$CI_JOB_NAME" --arg stage "$CI_JOB_STAGE" \
        '{
          object_kind: "pipeline",
          object_attributes: {status: "failed", id: ($pipe|tonumber)},
          project: {name: $name, path_with_namespace: $path},
          commit:  {id: $sha},
          builds:  [{status: "failed", stage: $stage, name: $job}]
        }')
    - curl --fail -X POST "$COPILOT_WEBHOOK_URL" \
           -H "Content-Type: application/json" \
           -H "X-Gitlab-Token: $COPILOT_WEBHOOK_TOKEN" \
           --data "$PAYLOAD"
```

Required GitLab CI variables in your repo:

| Variable | Value |
|---|---|
| `COPILOT_WEBHOOK_URL` | `https://<copilot-host>/api/v1/webhook/pipeline-failure` |
| `COPILOT_WEBHOOK_TOKEN` | Shared HMAC secret; must equal `GITLAB_WEBHOOK_TOKEN` on the Copilot side |

The Copilot side sets `GITLAB_WEBHOOK_TOKEN` via `settings.gitlab_webhook_token`
(env var or Vault).  When unset, the verifier is permissive (dev mode).

## Verifying the onboarding worked

```bash
# 1. The Copilot knows about your platform
curl -s localhost:8001/api/v1/platforms | jq '.platforms[].name'
# expect: "default", "sample-app", "spyroom"

# 2. ES is receiving your logs
curl -s 'http://localhost:9200/devops-logs-*/_search?q=kubernetes.namespace:spyroom&size=1' | jq

# 3. Analyze a service in your platform
curl -s -X POST localhost:8001/api/v1/analyze \
  -H 'Content-Type: application/json' \
  -d '{"platform":"spyroom","service":"auth-service","environment":"dev","lookback_minutes":10}' \
  | jq '.platform, .root_cause, .proposed_action, .blast_radius'

# 4. Reload the registry without restarting the pod (after editing yaml)
curl -X POST localhost:8001/api/v1/admin/reload-platforms \
  -H "X-Admin-API-Key: $ADMIN_API_KEY"
# Or send SIGHUP to the pod:
kubectl exec -n devops-copilot deploy/agent-backend -- kill -HUP 1
```

## Operational notes

* **Annotations beat yaml when both exist.**  The blast-radius analyzer
  prefers `devops-copilot/depends-on` annotations on live Deployments over
  the static yaml.  The yaml is the source of truth for things annotations
  can't easily express (GitLab project, owners, environments).
* **The yaml is reloadable.**  `SIGHUP` to the agent-backend pod or
  `POST /api/v1/admin/reload-platforms` re-reads every file without
  restarting.
* **Unregistered services still work.**  When no platform claims a service,
  the registry's `default` fallback kicks in — same behaviour as before the
  refactor.  This is what keeps the legacy `sample-app` path green.
* **One yaml per platform.**  Splitting the SpyRoom services across two
  yamls is supported but counter-productive — keep them together for
  legibility.
