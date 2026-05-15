#!/usr/bin/env bash
# simulate-k8s-event-failure.sh
# ──────────────────────────────────────────────────────────────────────────────
# Demo path for Phase 1 of the K8s event-driven incident roadmap.
#
# Applies a throw-away Deployment in ns=spyroom with a bogus image so kubelet
# emits `ImagePullBackOff` / `ErrImagePull` / `Failed` events.  The
# agent-backend's K8s event watcher should detect those, dispatch the existing
# analyze pipeline, and log:
#
#     k8s_event_detected             — watcher saw it
#     k8s_event_analysis_triggered   — handing to run_analysis
#     k8s_event_analysis_completed   — analyze returned
#
# Usage:
#   tools/simulate-k8s-event-failure.sh
#   tools/simulate-k8s-event-failure.sh --keep       # don't delete on exit
#   tools/simulate-k8s-event-failure.sh --service auth-service-broken
#
# Pre-reqs:
#   * agent-backend running in ns=default (or devops-copilot)
#   * K8S_WATCHER_ENABLED=true on that Deployment
#   * ns=spyroom exists
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SERVICE="auth-service-broken"
NS="spyroom"
KEEP=0
COPILOT_NS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service)   SERVICE="$2"; shift 2 ;;
    --namespace) NS="$2"; shift 2 ;;
    --keep)      KEEP=1; shift ;;
    -h|--help)
      grep '^# ' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Resolve copilot namespace
for n in default devops-copilot; do
  if kubectl get deploy agent-backend -n "$n" >/dev/null 2>&1; then
    COPILOT_NS="$n"; break
  fi
done
COPILOT_NS="${COPILOT_NS:-default}"

hdr() { printf "\n── %s ──\n" "$*"; }

# ── Verify the watcher is actually enabled ──────────────────────────────────
hdr "0/4  Verify K8S_WATCHER_ENABLED is set on agent-backend"
ENV_FLAG=$(kubectl get deploy agent-backend -n "$COPILOT_NS" \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="K8S_WATCHER_ENABLED")].value}{"\n"}' 2>/dev/null \
  || true)
# Also check llm-credentials Secret (envFrom)
if [[ -z "$ENV_FLAG" ]]; then
  ENV_FLAG=$(kubectl get secret llm-credentials -n "$COPILOT_NS" \
    -o jsonpath='{.data.K8S_WATCHER_ENABLED}' 2>/dev/null \
    | base64 -d 2>/dev/null || true)
fi
case "$ENV_FLAG" in
  true|TRUE|1|on) echo "  ✓ K8S_WATCHER_ENABLED=$ENV_FLAG (watcher should be running)" ;;
  *)
    echo "  ⚠  K8S_WATCHER_ENABLED is not 'true' (got: '$ENV_FLAG')"
    echo "     The watcher won't process events.  Enable it with one of:"
    echo "       kubectl set env deploy/agent-backend -n $COPILOT_NS K8S_WATCHER_ENABLED=true"
    echo "       kubectl patch secret llm-credentials -n $COPILOT_NS --type=merge \\"
    echo "         -p '{\"stringData\":{\"K8S_WATCHER_ENABLED\":\"true\"}}'"
    echo "     Then: kubectl rollout restart deploy/agent-backend -n $COPILOT_NS"
    echo "     Continuing anyway — events will be visible in k8s 'describe events'."
    ;;
esac

# ── Step 1: apply broken Deployment in spyroom ──────────────────────────────
hdr "1/4  Apply broken Deployment ${NS}/${SERVICE}"

cat <<EOF | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${SERVICE}
  namespace: ${NS}
  labels:
    app: ${SERVICE}
    devops-copilot/platform: spyroom
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ${SERVICE}
  template:
    metadata:
      labels:
        app: ${SERVICE}
    spec:
      containers:
        - name: ${SERVICE}
          image: spyroom/does-not-exist:0
          imagePullPolicy: IfNotPresent
EOF

# ── Step 2: wait for ImagePullBackOff events to fire ────────────────────────
hdr "2/4  Wait for ImagePullBackOff event"

for i in $(seq 1 30); do
  REASON=$(kubectl get events -n "$NS" \
    --field-selector involvedObject.kind=Pod \
    --sort-by='.lastTimestamp' \
    2>/dev/null | grep -E "ErrImagePull|ImagePullBackOff" | tail -1 | awk '{print $2}' || true)
  if [[ -n "$REASON" ]]; then
    echo "  ✓ Saw '$REASON' event after ${i}s"
    break
  fi
  sleep 1
done

if [[ -z "${REASON:-}" ]]; then
  echo "  ⚠  No ImagePullBackOff event detected after 30s — image-pull may be cached"
fi

# ── Step 3: confirm watcher dispatched analyze ──────────────────────────────
hdr "3/4  Tail agent-backend logs for k8s_event_* lines (max 30s)"

# Pull from the last 90s; the rollout above + propagation usually fits.
for i in $(seq 1 30); do
  HITS=$(kubectl logs -n "$COPILOT_NS" deploy/agent-backend --since=120s 2>/dev/null \
    | grep -E "k8s_event_detected|k8s_event_analysis_triggered|k8s_event_analysis_completed" \
    | grep -E "$SERVICE|${SERVICE//-broken/}|broken" || true)
  if [[ -n "$HITS" ]]; then
    echo
    echo "$HITS" | tail -20 | sed 's/^/  /'
    echo
    break
  fi
  sleep 1
done

if [[ -z "${HITS:-}" ]]; then
  echo "  ⚠  No watcher dispatch logs seen for $SERVICE in the last 120s"
  echo "     Investigation hints:"
  echo "     - kubectl logs -n $COPILOT_NS deploy/agent-backend --since=2m | grep k8s_event"
  echo "     - kubectl get events -n $NS --field-selector involvedObject.kind=Pod"
fi

# ── Step 4: cleanup ─────────────────────────────────────────────────────────
hdr "4/4  Cleanup"

if [[ "$KEEP" == "1" ]]; then
  echo "  --keep specified.  Deployment ${NS}/${SERVICE} left in place."
  echo "  Remove with: kubectl delete deployment ${SERVICE} -n ${NS}"
else
  kubectl delete deployment "${SERVICE}" -n "${NS}" --ignore-not-found=true
  echo "  ✓ Deployment removed"
fi

echo
echo "Done.  See README → 'K8s event-driven incident triggering' for details."
