#!/usr/bin/env bash
# Generates a mix of normal and error log events against the running sample-app.
# Usage: ./scripts/simulate_failure.sh [base_url]

set -euo pipefail

BASE_URL=${1:-http://localhost:8000}

echo "Hitting $BASE_URL — generating log events..."

for i in $(seq 1 5); do
  curl -sf "$BASE_URL/" > /dev/null && echo "  GET /        → ok"
  curl -sf "$BASE_URL/health" > /dev/null && echo "  GET /health  → ok"
done

# Trigger the intentional error endpoint
curl -sf "$BASE_URL/error" > /dev/null && echo "  GET /error   → ok (simulated failure logged)"

echo ""
echo "Done. Verify logs reached Elasticsearch:"
echo "  curl 'localhost:9200/devops-logs-*/_search?pretty&size=5'"
echo ""
echo "Or open Kibana at http://localhost:5601"
echo "  Stack Management → Data Views → create pattern: devops-logs-*"
