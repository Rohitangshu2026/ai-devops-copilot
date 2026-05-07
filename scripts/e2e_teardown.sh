#!/usr/bin/env bash
# e2e_teardown.sh — Delete the kind cluster created by e2e_setup.sh.
#
# Usage:
#   bash scripts/e2e_teardown.sh
#
# Environment:
#   E2E_CLUSTER_NAME  — kind cluster name (default: devops-copilot-e2e)

set -euo pipefail

CLUSTER_NAME="${E2E_CLUSTER_NAME:-devops-copilot-e2e}"

echo "==> Deleting kind cluster: $CLUSTER_NAME"
if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
  kind delete cluster --name "$CLUSTER_NAME"
  echo "==> Cluster '$CLUSTER_NAME' deleted."
else
  echo "    Cluster '$CLUSTER_NAME' does not exist — nothing to do."
fi
