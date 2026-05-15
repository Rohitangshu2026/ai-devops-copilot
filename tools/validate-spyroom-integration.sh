#!/usr/bin/env bash
# validate-spyroom-integration.sh
# ──────────────────────────────────────────────────────────────────────────────
# Runs the six-step Phase 2 local validation described in REFACTOR.md §10.
# Does NOT push to GitLab.  Does NOT modify the cluster (read-only checks +
# one optional pod restart guarded by --restart).  Designed for the demo:
# one command tells you whether the cross-repo integration is healthy.
#
# Usage:
#   tools/validate-spyroom-integration.sh                  # checks only
#   tools/validate-spyroom-integration.sh --restart        # also restart agent
#   tools/validate-spyroom-integration.sh --service auth-service
#
# Exit codes:
#   0   all checks passed
#   1   one or more critical checks failed
# ──────────────────────────────────────────────────────────────────────────────
set -uo pipefail   # NB: no -e — we want to count failures and keep going

SERVICE="room-service"
DO_RESTART=0
COPILOT_NS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service) SERVICE="$2"; shift 2 ;;
    --restart) DO_RESTART=1; shift ;;
    -h|--help)
      grep '^# ' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Resolve copilot namespace (default → devops-copilot fallback)
for NS in default devops-copilot; do
  if kubectl get deploy agent-backend -n "$NS" >/dev/null 2>&1; then
    COPILOT_NS="$NS"
    break
  fi
done
if [[ -z "$COPILOT_NS" ]]; then
  echo "✗ agent-backend deployment not found in default or devops-copilot"
  exit 1
fi

PASS=0
FAIL=0
ok()   { echo "  ✓ $*";       PASS=$((PASS+1)); }
fail() { echo "  ✗ $*";       FAIL=$((FAIL+1)); }
hdr()  { printf "\n── %s ──\n" "$*"; }

# ── Step 1: ES has spyroom-logs-* with service field ────────────────────────
hdr "1/6  ES has spyroom-logs-* index with top-level service field"
ES_NS=$(kubectl get pods -A -l app=elasticsearch -o jsonpath='{.items[0].metadata.namespace}' 2>/dev/null || echo "")
ES_POD=$(kubectl get pods -A -l app=elasticsearch -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
if [[ -z "$ES_POD" ]]; then
  fail "no elasticsearch pod found (looked for label app=elasticsearch)"
else
  RES=$(kubectl exec -n "$ES_NS" "$ES_POD" -- curl -s \
    'http://localhost:9200/spyroom-logs-*/_search?size=1' 2>/dev/null \
    | grep -oE '"service":"[^"]+"' | head -1)
  if [[ -n "$RES" ]]; then
    ok "ES query returned a doc with $RES"
  else
    fail "no docs in spyroom-logs-* (is Filebeat scraping ns=spyroom?)"
  fi
fi

# ── Step 2: PlatformRegistry yaml uses spyroom-logs-* ───────────────────────
hdr "2/6  configs/platforms/spyroom.yaml index pattern matches producer"
PATTERN=$(grep -E '^log_index_pattern:' configs/platforms/spyroom.yaml 2>/dev/null | awk '{print $2}' | tr -d '"')
case "$PATTERN" in
  spyroom-logs-*) ok "log_index_pattern=$PATTERN  (matches ES)" ;;
  "")             fail "log_index_pattern missing from spyroom.yaml" ;;
  *)              fail "log_index_pattern=$PATTERN  (should be spyroom-logs-*)" ;;
esac

# ── Step 3: agent-backend running and healthy ───────────────────────────────
hdr "3/6  agent-backend pod is Ready in ns=$COPILOT_NS"
READY=$(kubectl get deploy agent-backend -n "$COPILOT_NS" \
        -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
if [[ "${READY:-0}" -ge 1 ]]; then
  ok "agent-backend has $READY ready replica(s)"
else
  fail "agent-backend has 0 ready replicas — check pod logs"
fi

# Optional roll restart
if [[ "$DO_RESTART" == "1" ]]; then
  echo "  ⟳ restarting agent-backend to re-read configs/platforms/*.yaml…"
  kubectl rollout restart deployment/agent-backend -n "$COPILOT_NS" >/dev/null
  kubectl rollout status  deployment/agent-backend -n "$COPILOT_NS" --timeout=120s
fi

# ── Step 4: end-to-end /analyze returns non-empty result ────────────────────
hdr "4/6  /api/v1/analyze returns a valid response for $SERVICE"
PF_LOG=$(mktemp)
kubectl port-forward -n "$COPILOT_NS" svc/agent-backend 18001:8001 >"$PF_LOG" 2>&1 &
PF_PID=$!
trap "kill $PF_PID 2>/dev/null || true; rm -f $PF_LOG" EXIT INT TERM
# Wait until /health returns 200
PF_OK=0
for _ in $(seq 1 20); do
  if curl -sf http://localhost:18001/health >/dev/null 2>&1; then
    PF_OK=1
    break
  fi
  sleep 1
done
if [[ "$PF_OK" -eq 0 ]]; then
  echo "  ⚠  port-forward never became ready.  kubectl output:"
  sed 's/^/    /' "$PF_LOG" | head -10
fi

RESP=$(curl -s -m 30 -X POST http://localhost:18001/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d "{\"platform\":\"spyroom\",\"service\":\"$SERVICE\",\"environment\":\"dev\",\"lookback_minutes\":15}" 2>/dev/null)
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -m 30 -X POST \
  http://localhost:18001/api/v1/analyze \
  -H "Content-Type: application/json" \
  -d "{\"platform\":\"spyroom\",\"service\":\"$SERVICE\",\"environment\":\"dev\",\"lookback_minutes\":15}" 2>/dev/null)

case "$HTTP_CODE" in
  200)
    PLATFORM=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('platform',''))" 2>/dev/null || echo "")
    SOURCE=$(echo "$RESP"   | python3 -c "import sys,json; print(json.load(sys.stdin).get('confidence_source',''))" 2>/dev/null || echo "")
    ok "HTTP 200  platform=$PLATFORM  confidence_source=$SOURCE"
    if [[ "$SOURCE" == "heuristic_fallback" ]]; then
      echo "    ⚠  LLM was unavailable — heuristic stack handled the analysis."
      echo "      Check LLM_MODEL value and GOOGLE_API_KEY{,S} in the llm-credentials Secret."
    fi
    # Deployment-aware incident intelligence — surface signal when present
    DEP_SUSPECTED=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('deployment_suspected'))" 2>/dev/null || echo "")
    DEP_TL_LEN=$(echo "$RESP"   | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('deployment_timeline') or []))" 2>/dev/null || echo "0")
    HAS_RC=$(echo "$RESP"        | python3 -c "import sys,json; print('yes' if json.load(sys.stdin).get('rollback_candidate') else 'no')" 2>/dev/null || echo "no")
    echo "    deployment_timeline=$DEP_TL_LEN entries  suspected=$DEP_SUSPECTED  rollback_candidate=$HAS_RC"
    ;;
  500)
    fail "HTTP 500 — analyze raised.  Body: $(echo "$RESP" | head -c 200)"
    ;;
  "")
    fail "no response from analyze (port-forward never connected?)"
    ;;
  *)
    fail "HTTP $HTTP_CODE  body=$(echo "$RESP" | head -c 200)"
    ;;
esac

# ── Step 5: cross-namespace RBAC works ─────────────────────────────────────
hdr "5/6  SA agent-backend can read pods+events in ns=spyroom"
SA="system:serviceaccount:${COPILOT_NS}:agent-backend"
for verb_resource in "list events" "list pods" "get pods"; do
  if kubectl auth can-i $verb_resource --as="$SA" -n spyroom 2>/dev/null | grep -q "^yes"; then
    ok "$SA can $verb_resource in ns=spyroom"
  else
    fail "$SA CANNOT $verb_resource in ns=spyroom  (add Role+RoleBinding if needed)"
  fi
done

# ── Step 6: webhook receives + routes correctly ─────────────────────────────
hdr "6/6  /api/v1/webhook/pipeline-failure routes spyroom-platform → platform=spyroom"
PAYLOAD=$(cat <<EOF
{"object_kind":"pipeline","object_attributes":{"status":"failed","id":9999},
 "project":{"name":"spyroom-platform","path_with_namespace":"spe-group2/spyroom-platform"},
 "commit":{"id":"deadbeefcafebabe"},
 "builds":[{"status":"failed","stage":"deploy","name":"deploy-$SERVICE"}]}
EOF
)
HEADERS=(-H "Content-Type: application/json")
[[ -n "${COPILOT_WEBHOOK_TOKEN:-}" ]] && HEADERS+=(-H "X-Gitlab-Token: $COPILOT_WEBHOOK_TOKEN")
WH=$(curl -s -m 10 -X POST http://localhost:18001/api/v1/webhook/pipeline-failure \
     "${HEADERS[@]}" --data "$PAYLOAD" 2>/dev/null)
ACCEPTED=$(echo "$WH" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('accepted'))" 2>/dev/null || echo "")
PLAT=$(echo "$WH"     | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('platform'))" 2>/dev/null || echo "")
SVC=$(echo "$WH"      | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('service'))" 2>/dev/null || echo "")
if [[ "$ACCEPTED" == "True" && "$PLAT" == "spyroom" && "$SVC" == "$SERVICE" ]]; then
  ok "webhook routed correctly  platform=$PLAT  service=$SVC"
else
  fail "webhook routing failed.  raw: $(echo "$WH" | head -c 250)"
fi

# ── Summary ─────────────────────────────────────────────────────────────────
echo
echo "════════════════════════════════════════════════════════════"
echo " summary:  $PASS passed   $FAIL failed"
echo "════════════════════════════════════════════════════════════"
[[ "$FAIL" -eq 0 ]]
