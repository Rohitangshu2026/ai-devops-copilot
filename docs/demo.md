# Demo Walkthrough — End-to-End Incident Lifecycle

This document walks through the full incident lifecycle as it unfolds during a live
demo: **failure → logs → AI analysis → approval → remediation → verification**.

It assumes the stack is running (either via `docker compose up -d` or via
Kubernetes with the manifests in `k8s/`). For the simplest experience, run the
automated walkthrough:

```bash
bash scripts/demo.sh
```

The script pauses at each step so you can narrate; pass `--no-pause` to run
unattended (CI uses this).

---

## Stack URLs (docker-compose)

| Service | URL |
|---|---|
| Sample app | http://localhost:8000 |
| Agent backend | http://localhost:8001 |
| Kibana | http://localhost:5601 |
| Elasticsearch | http://localhost:9200 |
| Grafana | http://localhost:3000 *(admin/admin or anonymous viewer)* |
| Prometheus | http://localhost:9090 |

In Kubernetes, port-forward each service:

```bash
kubectl port-forward svc/sample-app 8000:8000 &
kubectl port-forward svc/agent-backend 8001:8001 &
kubectl port-forward svc/kibana 5601:5601 &
kubectl port-forward svc/grafana 3000:3000 &
```

---

## Step 1 — Inject a failure

The sample-app exposes injectable failure modes for live demos:

| Endpoint | Behaviour |
|---|---|
| `GET /error` | Always returns `status: error` (drives error rate) |
| `GET /crash` | Raises `RuntimeError` (drives severity = high) |
| `GET /slow` | Sleeps `SLOW_MS` ms; 504 above threshold |
| `GET /dep-error` | Returns 503 when downstream unreachable |
| `GET /oom` | Allocates `MEM_MB` MB until OOMKilled |

Trigger a realistic failure pattern:

```bash
for i in {1..12}; do curl -s http://localhost:8000/error > /dev/null; done
for i in {1..5};  do curl -s http://localhost:8000/crash > /dev/null; done
```

The app emits structured JSON logs that Filebeat forwards to Logstash → Elasticsearch.

**Expected result:** error rate jumps from ~0% to ~80% over the last minute. The
log summarizer in the analysis pipeline detects this as a **change-point**.

---

## Step 2 — See the logs in Kibana

Open http://localhost:5601 → **Discover** → index pattern `devops-logs-*`.

Three pre-built visualisations are available under **Dashboards → DevOps Copilot — Log Monitor**:

1. **Error Rate Over Time** — line chart of `level: ERROR` and `level: WARNING`
   counts bucketed by minute. The change-point is visible as a sudden vertical line.
2. **Log Level Distribution** — pie chart showing the INFO / WARNING / ERROR mix.
3. **Endpoint Activity Heatmap** — heat tiles of endpoint × status code.

> **Why this matters for grading:** the CSE 816 mandatory criterion is
> *"Application logs must feed into the ELK Stack, and the Kibana dashboard
> should visualize these logs."* Both are demonstrably wired.

Screenshot expected at `docs/screenshots/kibana-dashboard.png`.

---

## Step 3 — Run the AI analysis pipeline

```bash
curl -s -X POST http://localhost:8001/api/v1/analyze \
  -H 'Content-Type: application/json' \
  -d '{"service":"sample-app","environment":"dev","lookback_minutes":15}' \
  | jq
```

Behind the scenes, the pipeline runs:

```
fetch logs → summarize → score confidence
                ↓
          LLM (tool use) — search_logs, get_error_frequency, get_k8s_events
                ↓
          cross-model voting (destructive actions only)
                ↓
          causality validator (root-cause must be evidenced in logs)
                ↓
          safety controller — 9 gates:
            0  anomaly z-score baseline
            1  causality verification
            2  decision policy (action_type allowed for severity/confidence?)
            3  loop detector (≥5 unresolved → freeze service)
            4  idempotency lock (atomic ES op_type=create)
            5  namespace isolation
            6  severity gate
            6b blast-radius gate (transitive dependents from k8s annotations)
            7  rate limit
            8  action budget
                ↓
          requires_approval()? — criticality=critical, rollback, blast≥high,
                                 or destructive+confidence<high
                ↓
          execute (kubectl) ← only when approved
                ↓
          verify impact 2 min later (background sweeper)
```

**Sample response:**

```json
{
  "root_cause": "Sample-app pod crashed multiple times due to RuntimeError",
  "confidence_hint": "high",
  "confidence_score": 8,
  "confidence_breakdown": [
    "+2 error_type=runtime_crash (known type)",
    "+2 severity=high",
    "+2 error_ratio=0.74 (>10%)",
    "+1 event_count=23 (≥10)",
    "+1 historical_match (2 of 3 similar incidents resolved)"
  ],
  "proposed_action": {
    "type": "restart_pod",
    "target": "sample-app",
    "reason": "RuntimeError suggests transient state — restart should clear"
  },
  "safety_decision": "allowed",
  "blast_radius": {
    "score": "medium",
    "affected_count": 1,
    "affected_services": ["api-gateway"],
    "direct_dependents": ["api-gateway"],
    "source": "k8s_annotations"
  },
  "anomaly_score": 4.2,
  "action_state": "awaiting_approval",
  "approval_id": "8c1f3a4e-...",
  "incident_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

---

## Step 4 — Human approval

When `action_state == "awaiting_approval"`, the agent has created a signed
HMAC-SHA256 token and (if `SLACK_WEBHOOK_URL` is set) posted a Slack message
with **Approve** / **Reject** buttons.

**Slack notification:**

> ⚠️  Approval Required — restart_pod sample-app
>
> Service: `sample-app`    Action: `restart_pod`
> Blast Radius: `medium`   Confidence Score: `8/10`
>
> Reason: destructive action with confidence=high requires approval
>
> Confidence breakdown:
>   • +2 error_type=runtime_crash (known type)
>   • +2 severity=high
>   • +2 error_ratio=0.74 (>10%)
>   ...
>
>   [✅ Approve]   [❌ Reject]

Each button URL embeds the signed token:

```
http://agent-backend:8001/api/v1/approvals/{id}/approve?token={hmac}
```

Forged or expired tokens return 403. Tokens auto-expire in 5 minutes
(`APPROVAL_EXPIRY_SECONDS`).

**Manual approval via curl** (when Slack is not set up):

```bash
APPROVAL_ID=8c1f3a4e-...
TOKEN=$(kubectl logs deployment/agent-backend | grep approval_request_created | jq -r '.signed_token')

curl -X POST "http://localhost:8001/api/v1/approvals/$APPROVAL_ID/approve?token=$TOKEN"
```

Screenshot expected at `docs/screenshots/slack-approval.png`.

---

## Step 5 — Remediation and verification

After approval (or directly, when no approval is required), the action executor:

1. **Snapshots** the current Deployment spec into the rollback registry
   (so the next rollback restores this exact state)
2. Calls `kubectl rollout restart deployment/sample-app` (or scales / rolls back)
3. Polls until all replicas are `Ready` (90s timeout)
4. Updates `incident.execution_result` with `status: success | partial | failed`

**Watch the rolling update:**

```bash
kubectl get pods -l app=sample-app --watch
```

Because `replicas=2` and the strategy is `RollingUpdate(maxUnavailable=0, maxSurge=1)`,
**zero requests fail** during the restart. This is the "live patching" requirement
from the course brief.

**Two minutes later**, the persistent verifier sweeper runs:

```python
# evaluate impact: did the error rate drop?
new_ratio = compute_error_ratio(service, lookback=2min)
if new_ratio < baseline * 0.3:
    outcome = "resolved"
elif new_ratio < baseline:
    outcome = "partial"
else:
    outcome = "unresolved"  # increments loop_detector
```

Poll the final state:

```bash
curl -s http://localhost:8001/api/v1/incidents/$INCIDENT_ID | jq
```

```json
{
  "incident_id": "550e8400-...",
  "action_state": "completed",
  "outcome": "resolved",
  "execution_result": {
    "status": "success",
    "intended": 2,
    "achieved": 2
  }
}
```

---

## Step 6 — Observability dashboards

### Kibana (logs)

http://localhost:5601 → Dashboards → DevOps Copilot — Log Monitor

Visualises raw application logs. Useful for the SRE during the demo to show
the human-visible side of the same data the AI analysed.

### Grafana (agent metrics)

http://localhost:3000 → Dashboards → AI DevOps Copilot — Agent Backend

Six panels powered by Prometheus scraping `/metrics` on agent-backend:

| Panel | What it shows |
|---|---|
| Analysis rate | Incidents analysed per second, broken down by outcome |
| Analysis latency | p50/p95 of full pipeline duration |
| LLM calls — provider × outcome | Calls per provider/model with `ok / rate_limit / error` |
| Safety denials by gate | Which of the 9 gates is blocking actions most often |
| Actions executed | restart_pod / rollback / scale_up totals with status |
| LLM call latency p95 | Per-provider latency distribution |

Screenshot expected at `docs/screenshots/grafana-agent.png`.

### Internal incident audit (HTML)

http://localhost:8001/dashboard

Server-rendered HTML table of the last 50 incidents with filters by service,
safety_decision, and outcome. Click any row to see the full incident JSON
plus the inline event timeline.

Screenshot expected at `docs/screenshots/incident-dashboard.png`.

### Eval suite results

```bash
bash scripts/run_evals.sh
# Expected: ≥70% root-cause accuracy, ≥80% action correctness, ≤20% false-positive rate
```

The CI pipeline blocks the build on any accuracy regression — see the `evals`
stage in `.gitlab-ci.yml`.

---

## Mapping to CSE 816 evaluation criteria

| Criterion | Marks | Where demonstrated |
|---|---|---|
| **Working Project** | 20 | All 6 steps above complete end-to-end |
| Git push triggers CI/CD | – | `.gitlab-ci.yml`: test → evals → build → push to DockerHub → deploy via Ansible |
| App refresh shows changes seamlessly | – | Step 5 above: 2 replicas + RollingUpdate(maxUnavailable=0) = zero downtime |
| Logs feed into ELK + Kibana visualises | – | Steps 1–2 above; `k8s/elk/` deploys ELK in K8s; auto-imported dashboard |
| **Advanced Features** | 3 | |
| HashiCorp Vault | – | `k8s/vault.yaml` + `app/utils/vault_client.py` + `ansible/roles/vault_secrets` |
| Ansible roles (multiple) | – | `vault_secrets` + `app_deploy` roles |
| Kubernetes HPA | – | `k8s/hpa.yaml` (sample-app 1–5) + `k8s/hpa-agent-backend.yaml` (1–3) |
| **Innovation** | 2 | |
| AIOps domain | – | LLM agentic loop with 3 tools, 9-gate safety stack, cross-model voting, statistical anomaly baseline, blast-radius graph, human approval workflow with HMAC-signed tokens, eval suite |

---

## Troubleshooting

**Q: the `analyze` call returns 500 `No logs found for service='sample-app'`**

The log pipeline takes 5–10 seconds to flush. Increase the wait after Step 1
to 15 seconds, or check Kibana to confirm logs are arriving.

**Q: Slack notification doesn't appear**

`SLACK_WEBHOOK_URL` must be set in `agent-backend/.env` (docker-compose) or
in the `llm-credentials` Secret (k8s). When unset, approvals still work via
the API — just no Slack message.

**Q: Grafana shows "no data"**

Prometheus needs ~30 seconds to scrape the first metrics. Refresh the panel.
Confirm Prometheus is scraping with: http://localhost:9090/targets.

**Q: how do I reset the demo state?**

```bash
docker compose down -v  # drops the esdata volume
docker compose up -d
# OR for k8s:
kubectl delete -k k8s/   # if using kustomize
```
