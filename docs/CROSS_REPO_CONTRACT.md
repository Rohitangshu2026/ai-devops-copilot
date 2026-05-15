# Cross-Repo Contract: `spyroom-platform` ↔ `ai-devops-copilot`

Two repos. One Minikube cluster. Three protocol contracts. No source coupling.

This document is the canonical reference for **what** the two repos exchange,
**how** they exchange it, and **what each side promises to keep stable**. The
goal is that either side can refactor independently as long as the three
contracts hold.

```
   spyroom-platform                          ai-devops-copilot
   (owner of the data plane)                 (operational intelligence)
   ───────────────────────────                ───────────────────────────
   Spring Boot + Next.js apps                FastAPI agent-backend
   ELK stack                                 PlatformRegistry
   Prometheus + Grafana                      LLM agentic loop
   Vault                                     Safety stack + eval suite
   Four Ansible roles                        ─────────────
                                             reads:  ES, K8s API
                                             writes: k8s (scoped RBAC)
                                                     Slack
                                                     GitLab MR comments
                                                     (optional)
```

## The three contracts

### Contract A — Elasticsearch read

| Item | Value |
|---|---|
| Endpoint | `http://elasticsearch.logging.svc.cluster.local:9200` (in-cluster DNS) |
| Index pattern | `spyroom-logs-YYYY.MM.dd` (queried as `spyroom-logs-*`) |
| Required top-level fields | `@timestamp`, `service`, `message`, `kubernetes.namespace`, `kubernetes.container.name` |
| Optional fields | `level`, `log.level`, `endpoint`, `status_code`, `error`, `kubernetes.labels.app` |
| Schema rule | `service` exists at the top level, supplied by the Logstash `mutate` from `kubernetes.container.name`. Dynamic mapping gives it a `.keyword` subfield. |

**Producer (spyroom-platform).** Logstash filter pipeline guarantees the
`service` field is set on every doc. If the filter is ever removed, the
copilot's `elk_service.py` falls back to matching `kubernetes.container.name`.

**Consumer (ai-devops-copilot).**
`configs/platforms/spyroom.yaml`:
```yaml
log_index_pattern: spyroom-logs-*
log_service_field: service
```
`elk_service.py::fetch_logs` uses `bool/should[term on service.keyword, match on service]` — robust to either dynamic-map state.

**Stability promise.** Index name pattern and the presence of a top-level
`service` field are part of the contract. Anything else (Filebeat config,
Logstash filters, ES heap size) is an implementation detail of the producer.

### Contract B — Kubernetes API read

| Item | Value |
|---|---|
| Caller | ServiceAccount `default:agent-backend` |
| Granted permission | ClusterRole `agent-backend-role` (ClusterRoleBinding scope) |
| Verbs | `get`, `list`, `watch` on `pods`, `events`, `deployments`, `services` |
| Write verbs (action executor) | `update`, `patch` on `deployments` (scoped by namespace deny-list in `policy.yaml`) |

**No spyroom-side changes required.** The ClusterRole is cluster-wide and
already covers `ns:spyroom`. Verify with:

```bash
kubectl auth can-i list events --as=system:serviceaccount:default:agent-backend -n spyroom
kubectl auth can-i list pods   --as=system:serviceaccount:default:agent-backend -n spyroom
# both must print: yes
```

The copilot's `app/llm/tools.py::_get_k8s_events` consumes this. The default
namespace for k8s queries comes from `PlatformRegistry.namespace_for(service)`
— so a query about `auth-service` resolves to `ns:spyroom` automatically.

**Stability promise.** The copilot's RBAC scope is namespaced-by-default
through `policy.yaml` even though the underlying ClusterRole is cluster-wide.
Adding new platforms does not require RBAC churn.

### Contract C — Failure webhook (optional but powerful)

| Item | Value |
|---|---|
| Endpoint | `POST http(s)://<copilot-host>/api/v1/webhook/pipeline-failure` |
| Auth | HMAC compare on header `X-Gitlab-Token` against `GITLAB_WEBHOOK_TOKEN` (env / Vault) |
| Required body fields | `object_kind="pipeline"`, `object_attributes.status="failed"`, `project.path_with_namespace`, `builds[].name`, `builds[].status="failed"` |
| Routing | `PlatformRegistry.get_by_gitlab_project(project.path_with_namespace)` → `PlatformConfig` |
| Service extraction | first failing `builds[].name`, prefix-stripped (`deploy-`/`test-`/`build-`/...) → matched against `platform.service_names()` |
| Response time | < 100 ms (analysis is dispatched async) |

**Producer.** SpyRoom's `.gitlab-ci.yml` has a `notify-copilot` stage that
runs on `on_failure`. For the demo we substitute `tools/simulate-pipeline-failure.sh`
which produces an identical payload via curl. The copilot cannot distinguish
the two — same code path, same outcome.

**Consumer.** `app/api/v1/webhooks.py::handle_pipeline_failure`:
1. Verify HMAC token (constant-time compare; permissive when secret unset).
2. Look up platform by GitLab project.
3. Extract failing service name.
4. `asyncio.create_task(run_analysis(...))` — return 202 immediately.

**Token permissiveness.** When `GITLAB_WEBHOOK_TOKEN` is empty the verifier
returns True for any header value (dev mode). Set the token in production to
enforce.

## Ownership: who changes what

| Concern | Owner | Why |
|---|---|---|
| Spring Boot service code | spyroom-platform | Domain |
| Postgres schemas, JWT secrets, JWT signing | spyroom-platform | App-internal |
| ELK stack (ES, Logstash, Kibana, Filebeat) | spyroom-platform | Producer owns the data plane |
| Prometheus + Grafana | spyroom-platform | Same reasoning |
| Vault | spyroom-platform | Application's own secret store |
| Ansible roles for cluster bring-up | spyroom-platform | Operates the cluster |
| `configs/platforms/spyroom.yaml` | ai-devops-copilot | Describes *how to read* spyroom |
| `agent-backend` Deployment + RBAC | ai-devops-copilot | Operates the AI side |
| `policy.yaml` decision rules | ai-devops-copilot | Safety stack tuning |
| Eval fixtures under `evals/incidents/spyroom_*` | ai-devops-copilot | Validates the consumer side |
| `ci-templates/.gitlab-ci.spyroom.example.yml` | ai-devops-copilot | Template; SpyRoom copies it into its repo |
| `tools/simulate-pipeline-failure.sh` | ai-devops-copilot | Demo affordance for Contract C |
| `tools/validate-spyroom-integration.sh` | ai-devops-copilot | End-to-end health check |

## Deployment correlation (read-only signal)

Every `/api/v1/analyze` response now includes a `deployment_timeline`
(populated from the k8s API in your namespace) and — when the heuristic
fires AND the namespace is non-production AND confidence is high — a
`rollback_candidate`.  See README's *Deployment-aware incident
intelligence* section for the exact shape.

What this means for the SpyRoom side:

* **No code changes required.**  The copilot reads
  `apps/v1.list_namespaced_deployment` via Contract B (K8s API read).
* The `app` label SpyRoom already sets on its Deployments is sufficient
  for service matching.  No new annotations needed.
* `rollback_candidate` is **a recommendation only** — the copilot's
  `auto_executable: false` field is hard-coded.  Production rollback
  still requires a human to call `POST /api/v1/approvals/{id}/approve`
  or `kubectl rollout undo` directly.

## When either side changes

| Change in spyroom-platform | Does it break ai-devops-copilot? | What to do |
|---|---|---|
| Add a new Spring Boot service | No — until the copilot needs to monitor it | Add an entry under `services:` in `configs/platforms/spyroom.yaml` |
| Rename a service (container name) | Yes — `service` field in ES changes | Update both the yaml `services[].name` and `services[].container_name` |
| Move logs to a different ES index | Yes — copilot queries a non-existent index | Update `log_index_pattern` in the yaml |
| Drop the Logstash `service` mutate | Yes — query yields zero hits | Add an `kubernetes.container.name` fallback in `elk_service.py`, OR restore the mutate |
| Add a new namespace | Possibly — Filebeat path filter | Add the namespace to the Filebeat `drop_event` filter (or onboard it as a new platform) |
| Change Postgres credentials in Vault | No | Copilot doesn't read app secrets |

| Change in ai-devops-copilot | Does it break spyroom-platform? | What to do |
|---|---|---|
| Add a new platform yaml | No | Nothing on spyroom's side |
| Tighten safety policy | No — only affects copilot's action proposals | Edit `policy.yaml`; SIGHUP to reload |
| Add a new LLM provider | No | Nothing on spyroom's side |
| Bump agent-backend image | No | The K8s API + ES contracts are stable |
| Move agent-backend to a different namespace | No, but verify RBAC binding still resolves the SA | Update `rbac.yaml` if needed |

## Verifying the contract end-to-end

```bash
# One command. Six checks. Exit 0 = demo-ready.
./tools/validate-spyroom-integration.sh

# Add --restart to roll the agent-backend pod first (picks up new yaml).
./tools/validate-spyroom-integration.sh --restart

# Different service:
./tools/validate-spyroom-integration.sh --service auth-service
```

A `confidence_source: heuristic_fallback` in step 4's output means **the
analysis worked but the LLM was unavailable**. The deterministic safety
stack still gated a response — the demo is functional even without LLM
credentials. Fix the LLM by setting one of `GOOGLE_API_KEY`,
`GOOGLE_API_KEYS`, `ANTHROPIC_API_KEYS`, or `OPENAI_API_KEYS` in the
agent-backend env (or via Vault) and confirming `LLM_MODEL` resolves to a
real model name (default: `gemini-1.5-flash`).
