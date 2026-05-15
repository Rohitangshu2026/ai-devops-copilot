#!/usr/bin/env bash
# simulate-deployment-failure.sh
# ──────────────────────────────────────────────────────────────────────────────
# Demo path for deployment-aware incident intelligence.
#
# Flow:
#   1. Trigger a fresh rollout on a SpyRoom service (default: room-service)
#      by setting a no-op annotation — this forces a new ReplicaSet WITHOUT
#      actually changing the image, so existing pods don't crash.
#   2. Wait for rollout to settle.
#   3. Hit the gateway with traffic that produces 5xx logs (so the
#      summarizer's change-point detector fires AFTER the rollout).
#   4. Run analyze and inspect:
#        - deployment_timeline  → contains the just-completed rollout
#        - deployment_suspected → true
#        - rollback_candidate   → populated (non-prod namespace)
#
# Usage:
#   tools/simulate-deployment-failure.sh
#   tools/simulate-deployment-failure.sh --service auth-service
#   tools/simulate-deployment-failure.sh --service room-service --window 20
#
# Pre-reqs: a working SpyRoom deployment in ns=spyroom, agent-backend running.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SERVICE="room-service"
SPYROOM_NS="spyroom"
COPILOT_NS=""
WINDOW=15

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service) SERVICE="$2"; shift 2 ;;
    --namespace) SPYROOM_NS="$2"; shift 2 ;;
    --window) WINDOW="$2"; shift 2 ;;
    -h|--help) grep '^# ' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Locate the copilot's namespace
for NS in default devops-copilot; do
  if kubectl get deploy agent-backend -n "$NS" >/dev/null 2>&1; then
    COPILOT_NS="$NS"; break
  fi
done
COPILOT_NS="${COPILOT_NS:-default}"

hdr() { printf "\n── %s ──\n" "$*"; }

# ── Step 1: trigger rollout on the target service ───────────────────────────
hdr "1/4  Triggering rollout of $SERVICE in ns=$SPYROOM_NS"

if ! kubectl get deploy "$SERVICE" -n "$SPYROOM_NS" >/dev/null 2>&1; then
  echo "  ✗ Deployment $SERVICE not found in $SPYROOM_NS"
  echo "    available deployments:"
  kubectl get deploy -n "$SPYROOM_NS" -o name | sed 's/^/      /'
  exit 1
fi

# Annotate to force a new ReplicaSet (no image change → no pod crash risk).
ROLLOUT_TS=$(date -u +%FT%TZ)
kubectl annotate deployment/$SERVICE -n "$SPYROOM_NS" \
  "deployment.kubernetes.io/restartedAt=$ROLLOUT_TS" \
  --overwrite >/dev/null
kubectl rollout restart deployment/"$SERVICE" -n "$SPYROOM_NS" >/dev/null
echo "  ✓ Rollout triggered at $ROLLOUT_TS"
kubectl rollout status deployment/"$SERVICE" -n "$SPYROOM_NS" --timeout=120s >/dev/null
echo "  ✓ Rollout settled"

# ── Step 2: produce error traffic so the change-point lands AFTER rollout ──
hdr "2/4  Producing error traffic via api-gateway"

GW_PF=$(mktemp)
kubectl port-forward -n "$SPYROOM_NS" svc/api-gateway 18080:8080 >"$GW_PF" 2>&1 &
GW_PID=$!
trap "kill $GW_PID 2>/dev/null || true; rm -f $GW_PF" EXIT INT TERM
for _ in $(seq 1 10); do
  curl -sf http://localhost:18080/health >/dev/null 2>&1 && break
  sleep 1
done

# Generate a burst of 404/5xx requests — the gateway logs all of them.
for i in $(seq 1 20); do
  curl -s -o /dev/null -w "" http://localhost:18080/api/rooms/9999/messages   || true
  curl -s -o /dev/null -w "" http://localhost:18080/api/auth/this-does-not-exist || true
  curl -s -o /dev/null -w "" http://localhost:18080/rooms/$RANDOM             || true
done
echo "  ✓ 60 requests sent"

# ── Step 3: wait for Filebeat/Logstash to ship logs ─────────────────────────
hdr "3/4  Waiting 15s for log shipping"
sleep 15

# ── Step 4: analyze and surface deployment_timeline + rollback_candidate ───
hdr "4/4  Analyzing $SERVICE (window=${WINDOW}m)"

AB_PF=$(mktemp)
kubectl port-forward -n "$COPILOT_NS" svc/agent-backend 18001:8001 >"$AB_PF" 2>&1 &
AB_PID=$!
trap "kill $GW_PID 2>/dev/null || true; kill $AB_PID 2>/dev/null || true; rm -f $GW_PF $AB_PF" EXIT INT TERM
for _ in $(seq 1 15); do
  curl -sf http://localhost:18001/health >/dev/null 2>&1 && break
  sleep 1
done

RESP=$(curl -s -m 60 -X POST http://localhost:18001/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d "{\"platform\":\"spyroom\",\"service\":\"$SERVICE\",\"namespace\":\"$SPYROOM_NS\",\"environment\":\"dev\",\"lookback_minutes\":$WINDOW}")

if [[ -z "$RESP" ]]; then
  echo "  ✗ empty response from /analyze — check agent-backend logs:"
  echo "    kubectl logs -n $COPILOT_NS deploy/agent-backend --tail=50"
  exit 1
fi

echo
echo "$RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
tl = d.get('deployment_timeline') or []
rc = d.get('rollback_candidate')
print('confidence_source :', d.get('confidence_source'))
print('confidence_score  :', d.get('confidence_score'), '(' + str(d.get('confidence_hint', '')) + ')')
print('error_type        :', (d.get('parsed_log') or {}).get('error_type'))
print('change_point      :', (d.get('log_summary') or {}).get('change_point_description'))
print()
print('deployment_timeline (' + str(len(tl)) + ' entries):')
for e in tl:
    print('  - {name} @ {age:.1f}m ago  ({img})  Δincident={d}'.format(
        name=e['deployment_name'],
        age=e['deployment_age_minutes'],
        img=e['image_tag'][:60],
        d=('%.1fm' % e['minutes_before_incident']) if e['minutes_before_incident'] is not None else 'n/a',
    ))
print()
print('deployment_suspected:', d.get('deployment_suspected'))
if rc:
    print('rollback_candidate:')
    print('  deployment        :', rc['deployment'])
    print('  current_image     :', rc['current_image'])
    print('  recommended_action:', rc['recommended_action'])
    print('  reason            :', rc['reason'])
    print('  blast_radius_score:', rc['blast_radius_score'])
    print('  auto_executable   :', rc['auto_executable'])
else:
    print('rollback_candidate: (none — gated on confidence + non-prod + change-point)')
"

echo
echo "──────────────────────────────────────────────────────────"
echo "Demo complete.  Note: rollback_candidate is RECOMMENDATION ONLY."
echo "Never auto-executed.  Approve via /api/v1/approvals/{id}/approve."
echo "──────────────────────────────────────────────────────────"
