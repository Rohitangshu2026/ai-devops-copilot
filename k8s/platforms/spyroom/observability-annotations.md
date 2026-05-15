# SpyRoom — observability annotations cheat sheet

The DevOps Copilot infers a lot from annotations on your Deployment manifests.
None of these are required — they're hints that improve incident triage. Add
them in **your repo's** k8s manifests (not here).

## Per-Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: auth-service
  namespace: spyroom            # must match configs/platforms/spyroom.yaml
  labels:
    app: auth-service
    devops-copilot/platform: spyroom
    devops-copilot/service: auth-service
  annotations:
    # Operator-set risk class — gates which actions the Copilot will propose.
    # critical → always requires human approval, never auto-restarted.
    # high     → auto-restart only when confidence=high AND severity>=high.
    # medium   → today's default rules apply.
    # low      → may auto-scale_up at confidence=medium.
    devops-copilot/criticality: "critical"

    # Comma-separated list of services this depends on.  Used by the
    # blast-radius analyzer to compute transitive impact.  Match the names
    # under `services:` in configs/platforms/spyroom.yaml.
    devops-copilot/depends-on: "postgres"

    # Optional: Prometheus scrape annotations (already supported by your
    # spring-boot-starter-actuator; remove if your service doesn't expose them).
    prometheus.io/scrape: "true"
    prometheus.io/path: "/actuator/prometheus"
    prometheus.io/port: "8080"
spec:
  template:
    metadata:
      labels:
        app: auth-service
        devops-copilot/platform: spyroom
        devops-copilot/service: auth-service
    spec:
      containers:
        - name: auth-service              # MUST equal the service name in
                                          # configs/platforms/spyroom.yaml
          image: your-registry/spyroom-auth-service:abc123
          # ... ports, env, probes, resources, etc.
```

## What each annotation buys you

| Annotation | Used by | Effect |
|---|---|---|
| `devops-copilot/criticality` | `app/core/blast_radius.py:service_criticality_from_k8s` | Tightens the safety gate — `critical` forces human approval |
| `devops-copilot/depends-on` | `app/core/blast_radius.py:_fetch_dep_map_from_k8s` | Live dependency graph for transitive impact calc |
| `devops-copilot/platform` | dashboards + Slack routing | Owner/topology in incident summary |
| `devops-copilot/service` | log enrichment, dashboards | Disambiguates when container name ≠ service name |

## Suggested annotations per SpyRoom service

| Service | criticality | depends-on |
|---|---|---|
| `api-gateway` | high | `auth-service,room-service` |
| `auth-service` | critical | `postgres` |
| `room-service` | high | `postgres,redis` |
| `spyroom-frontend` | medium | `api-gateway` |

(These mirror `configs/platforms/spyroom.yaml`. The yaml is the *config* source;
annotations are the *runtime* source. When both exist, annotations win for
graph traversal — see `app/core/blast_radius.py`.)

## Required cluster-side prerequisite

The Copilot's Filebeat DaemonSet must scrape the `spyroom` namespace. That's
configured in `k8s/elk/filebeat.yaml` (already updated). Apply it once:

```bash
kubectl apply -f k8s/elk/filebeat.yaml
kubectl rollout restart daemonset/filebeat
```

After that, any pod logs in `spyroom` flow into ELK and become visible at:

```bash
curl -s 'http://localhost:9200/devops-logs-*/_search?q=kubernetes.namespace:spyroom&size=1' | jq
```
