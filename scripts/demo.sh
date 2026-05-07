#!/usr/bin/env bash
# AI DevOps Copilot — end-to-end demo walkthrough
#
# Walks through the full incident lifecycle:
#   failure → logs → AI analysis → approval → remediation → verification
#
# Prerequisites:
#   - Stack running (docker compose up -d, OR k8s with port-forwards)
#   - jq installed
#   - Optional: GOOGLE_API_KEYS / ANTHROPIC_API_KEYS in environment for real LLM
#
# Usage:
#   bash scripts/demo.sh                           # full walkthrough
#   bash scripts/demo.sh --no-pause                # skip prompts (CI-friendly)
#   AGENT_URL=http://localhost:8001 bash scripts/demo.sh
#
set -uo pipefail

AGENT_URL="${AGENT_URL:-http://localhost:8001}"
SAMPLE_URL="${SAMPLE_URL:-http://localhost:8000}"
KIBANA_URL="${KIBANA_URL:-http://localhost:5601}"
GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"
PAUSE=true
[[ "${1:-}" == "--no-pause" ]] && PAUSE=false

# ── Pretty output ─────────────────────────────────────────────────────────────
BOLD=$(tput bold 2>/dev/null || echo "")
DIM=$(tput dim 2>/dev/null || echo "")
GREEN=$(tput setaf 2 2>/dev/null || echo "")
YELLOW=$(tput setaf 3 2>/dev/null || echo "")
BLUE=$(tput setaf 4 2>/dev/null || echo "")
RED=$(tput setaf 1 2>/dev/null || echo "")
RESET=$(tput sgr0 2>/dev/null || echo "")

step() { echo; echo "${BOLD}${BLUE}▶  $*${RESET}"; }
ok()   { echo "${GREEN}✓${RESET}  $*"; }
info() { echo "${DIM}   $*${RESET}"; }
warn() { echo "${YELLOW}⚠${RESET}  $*"; }
err()  { echo "${RED}✗${RESET}  $*" >&2; }

pause() {
  $PAUSE || return 0
  echo
  read -rp "${DIM}-- press Enter to continue --${RESET}"
}

# ── Pre-flight checks ─────────────────────────────────────────────────────────
step "Pre-flight: verifying stack is reachable"

if ! curl -sf "$AGENT_URL/health" >/dev/null; then
  err "agent-backend not reachable at $AGENT_URL"
  echo "  Start it with: docker compose up -d"
  echo "  Or: kubectl port-forward svc/agent-backend 8001:8001"
  exit 1
fi
ok "agent-backend healthy at $AGENT_URL"

if ! curl -sf "$SAMPLE_URL/health" >/dev/null; then
  err "sample-app not reachable at $SAMPLE_URL"
  exit 1
fi
ok "sample-app healthy at $SAMPLE_URL"

info "Kibana:  $KIBANA_URL"
info "Grafana: $GRAFANA_URL  (anonymous viewer)"

pause

# ── Step 1 — Inject failure ───────────────────────────────────────────────────
step "Step 1 / 5  ─  Inject a failure into sample-app"

info "Hitting /error 12 times to drive the error rate above the change-point threshold"
for i in {1..12}; do
  curl -s "$SAMPLE_URL/error" >/dev/null
  printf "."
done
echo
ok "12 errors generated"

info "Hitting /crash 5 times to push severity to high"
for i in {1..5}; do
  curl -s "$SAMPLE_URL/crash" >/dev/null
  printf "."
done
echo
ok "5 crashes generated"

info "Waiting 8s for logs to flush through Filebeat → Logstash → Elasticsearch"
sleep 8

pause

# ── Step 2 — Confirm logs landed in ES ────────────────────────────────────────
step "Step 2 / 5  ─  Verify logs in Elasticsearch (Kibana visualizes the same index)"

ES_URL="${ES_URL:-http://localhost:9200}"

if curl -sf "$ES_URL/_cluster/health" >/dev/null; then
  hits=$(curl -s "$ES_URL/devops-logs-*/_count?q=level:ERROR" | jq -r '.count // 0')
  info "Errors indexed in devops-logs-*: ${BOLD}$hits${RESET}"

  echo
  info "Most recent ERROR log line:"
  curl -s "$ES_URL/devops-logs-*/_search?size=1&q=level:ERROR&sort=@timestamp:desc" \
    | jq '.hits.hits[0]._source | {timestamp: .["@timestamp"], event, endpoint, status}'
else
  warn "Cannot reach Elasticsearch at $ES_URL — skipping log verification"
fi

echo
info "Open Kibana to see the same data visualized:  $KIBANA_URL"
info "  • Error Rate Over Time"
info "  • Log Level Distribution"
info "  • Endpoint Activity Heatmap"

pause

# ── Step 3 — Run AI analysis ──────────────────────────────────────────────────
step "Step 3 / 5  ─  Run AI analysis pipeline"

info "POST /api/v1/analyze — full pipeline:"
info "  → fetch logs from ES"
info "  → summarize + score confidence"
info "  → LLM agentic loop (tool use)"
info "  → causality validation"
info "  → safety controller (9 gates)"
info "  → action proposal"

result=$(curl -sf -X POST "$AGENT_URL/api/v1/analyze" \
  -H 'Content-Type: application/json' \
  -d '{"service":"sample-app","environment":"dev","lookback_minutes":15}' || echo '')

if [[ -z "$result" ]]; then
  err "Analysis failed (no response). Check agent-backend logs."
  exit 1
fi

echo
echo "$result" | jq '{
  root_cause,
  confidence: confidence_hint,
  confidence_score,
  confidence_breakdown,
  proposed_action,
  safety_decision,
  causality_verified,
  blast_radius: .blast_radius.score,
  blast_affected: .blast_radius.affected_count,
  anomaly_score,
  action_state,
  approval_id,
  incident_id
}'

incident_id=$(echo "$result" | jq -r '.incident_id // empty')
action_state=$(echo "$result" | jq -r '.action_state // empty')
approval_id=$(echo "$result" | jq -r '.approval_id // empty')
proposed_action=$(echo "$result" | jq -r '.proposed_action.type // empty')

echo
ok "Incident recorded: $incident_id"
ok "Proposed action:   $proposed_action"
ok "Action state:      $action_state"

pause

# ── Step 4 — Approval (only if required) ──────────────────────────────────────
step "Step 4 / 5  ─  Human approval gate"

if [[ "$action_state" == "awaiting_approval" && -n "$approval_id" ]]; then
  warn "Action requires human approval — approval_id=$approval_id"
  info "In production, an operator would click the Approve button in Slack."
  info "For this demo, fetching the signed token from the approval store..."

  # Fetch the pending approval (has token in response only at creation time —
  # the demo scrapes it via the GET endpoint which returns metadata only).
  # The actual signed token is logged at creation; fetch from agent logs.
  curl -s "$AGENT_URL/api/v1/approvals/$approval_id" | jq

  warn "Skipping live approval — see docs/demo.md for the full Slack flow"
else
  ok "No approval required — action proceeds directly"
  info "(approval would be triggered by criticality=critical, rollback, blast≥high, or low confidence)"
fi

pause

# ── Step 5 — Verify resolution ────────────────────────────────────────────────
step "Step 5 / 5  ─  Verify incident resolution"

if [[ -n "$incident_id" ]]; then
  info "Polling incident state..."
  curl -sf "$AGENT_URL/api/v1/incidents/$incident_id" | jq '{
    incident_id,
    service,
    action_state,
    outcome,
    proposed_action: .proposed_action.type,
    safety_decision,
    safety_reason
  }'

  echo
  info "Incident timeline (chronological events):"
  curl -sf "$AGENT_URL/api/v1/incidents/$incident_id/timeline" | jq '.timeline'
fi

echo
info "View aggregated metrics:"
echo "  curl -s $AGENT_URL/api/v1/metrics | jq"
echo
info "Prometheus metrics for Grafana:"
echo "  curl -s $AGENT_URL/metrics | head -20"
echo
info "HTML incident dashboard:"
echo "  open $AGENT_URL/dashboard"

# ── Summary ───────────────────────────────────────────────────────────────────
echo
echo "${BOLD}${GREEN}═══════════════════════════════════════════════════════════════${RESET}"
echo "${BOLD}${GREEN}  Demo complete${RESET}"
echo "${BOLD}${GREEN}═══════════════════════════════════════════════════════════════${RESET}"
echo
echo "Pipeline traversed:"
echo "  ${GREEN}1.${RESET} Failure injected      → /error + /crash on sample-app"
echo "  ${GREEN}2.${RESET} Logs collected        → Filebeat → Logstash → Elasticsearch"
echo "  ${GREEN}3.${RESET} AI analyzed           → 9-gate safety stack proposed action"
echo "  ${GREEN}4.${RESET} Approval gated        → ${action_state}"
echo "  ${GREEN}5.${RESET} Verification          → impact verifier sweeper (runs in 2 min)"
echo
echo "Dashboards:"
echo "  • Kibana:    $KIBANA_URL          (log-level visualizations)"
echo "  • Grafana:   $GRAFANA_URL          (agent metrics)"
echo "  • Internal:  $AGENT_URL/dashboard  (incident audit log)"
echo
