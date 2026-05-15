#!/usr/bin/env bash
# simulate-pipeline-failure.sh
# ──────────────────────────────────────────────────────────────────────────────
# Demo-only stand-in for the SpyRoom CI's `notify-copilot` stage.
#
# Sends a GitLab-shaped pipeline-failure webhook to the running agent-backend
# without burning GitLab CI minutes.  The copilot's webhook handler cannot
# distinguish this from a real GitLab POST — same code path, same auth, same
# downstream `run_analysis` dispatch.
#
# Defaults:
#   service to fail        = room-service   (override with --service)
#   stage that failed      = deploy         (override with --stage)
#   target webhook         = http://localhost:8001/api/v1/webhook/pipeline-failure
#                                              (override with COPILOT_WEBHOOK_URL)
#   X-Gitlab-Token         = $COPILOT_WEBHOOK_TOKEN (omit header when unset)
#
# Auto port-forwards `svc/agent-backend` on demand when COPILOT_WEBHOOK_URL
# is not provided — convenient for the demo: one command, full chain.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SERVICE="room-service"
STAGE="deploy"
SHA="${CI_COMMIT_SHA:-$(printf 'demo%016x' $RANDOM)}"
PROJECT_PATH="spe-group2/spyroom-platform"
PIPELINE_ID="${CI_PIPELINE_ID:-99999}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service)  SERVICE="$2";  shift 2 ;;
    --stage)    STAGE="$2";    shift 2 ;;
    --sha)      SHA="$2";      shift 2 ;;
    --project)  PROJECT_PATH="$2"; shift 2 ;;
    -h|--help)
      grep '^# ' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2 ;;
  esac
done

JOB_NAME="${STAGE}-${SERVICE}"

# ── Port-forward bootstrap (only if URL not preset) ─────────────────────────
PF_PID=""
COPILOT_WEBHOOK_URL="${COPILOT_WEBHOOK_URL:-}"
LOCAL_PORT="${LOCAL_PORT:-8001}"
if [[ -z "$COPILOT_WEBHOOK_URL" ]]; then
  # Resolve which namespace agent-backend lives in (default → devops-copilot).
  for NS in default devops-copilot; do
    if kubectl get svc agent-backend -n "$NS" >/dev/null 2>&1; then
      COPILOT_NS="$NS"
      break
    fi
  done
  COPILOT_NS="${COPILOT_NS:-default}"

  # If something is already serving /health on LOCAL_PORT, reuse it — avoids
  # collision with an already-running validate-spyroom-integration.sh or with
  # a developer's manual port-forward.
  if curl -sf "http://localhost:${LOCAL_PORT}/health" >/dev/null 2>&1; then
    echo "→ reusing existing local listener on :${LOCAL_PORT}"
  else
    echo "→ port-forwarding svc/agent-backend in ns=$COPILOT_NS on :${LOCAL_PORT} …"
    kubectl port-forward -n "$COPILOT_NS" svc/agent-backend ${LOCAL_PORT}:8001 >/dev/null 2>&1 &
    PF_PID=$!
    trap '[[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null || true' EXIT INT TERM
    # Wait for the local port to start serving
    for _ in $(seq 1 15); do
      if curl -sf "http://localhost:${LOCAL_PORT}/health" >/dev/null 2>&1; then break; fi
      sleep 1
    done
  fi
  COPILOT_WEBHOOK_URL="http://localhost:${LOCAL_PORT}/api/v1/webhook/pipeline-failure"
fi

PAYLOAD=$(cat <<EOF
{
  "object_kind": "pipeline",
  "object_attributes": {"status": "failed", "id": ${PIPELINE_ID}},
  "project": {"name": "spyroom-platform", "path_with_namespace": "${PROJECT_PATH}"},
  "commit": {"id": "${SHA}"},
  "builds": [{"status": "failed", "stage": "${STAGE}", "name": "${JOB_NAME}"}]
}
EOF
)

echo "→ POST $COPILOT_WEBHOOK_URL"
echo "  service=$SERVICE  stage=$STAGE  sha=${SHA:0:8}"
echo

# Add token header only if defined — keeps dev-mode unauthenticated POST working.
HEADERS=(-H "Content-Type: application/json")
if [[ -n "${COPILOT_WEBHOOK_TOKEN:-}" ]]; then
  HEADERS+=(-H "X-Gitlab-Token: ${COPILOT_WEBHOOK_TOKEN}")
fi

curl --fail-with-body --show-error --silent \
     -X POST "$COPILOT_WEBHOOK_URL" \
     "${HEADERS[@]}" \
     --data "$PAYLOAD" | sed 's/^/  /'
echo
echo
echo "→ tailing agent-backend pod for analysis dispatch (Ctrl-C to exit)…"
kubectl logs -n "${COPILOT_NS:-default}" deploy/agent-backend --tail 30 \
  | grep -E "webhook|platform|service|analyze|llm_unavailable|heuristic" || true
