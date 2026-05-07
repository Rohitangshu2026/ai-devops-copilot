#!/usr/bin/env bash
# e2e_setup.sh — Create a kind cluster, load local Docker images, apply test
# manifests, and wait for all deployments to become ready.
#
# Usage:
#   LLM_API_KEY="your-key" bash scripts/e2e_setup.sh
#
# Environment:
#   E2E_CLUSTER_NAME  — kind cluster name (default: devops-copilot-e2e)
#   LLM_API_KEY       — API key for the LLM (Gemini or Anthropic)
#   GOOGLE_API_KEYS   — comma-separated Google API keys (optional)
#   ANTHROPIC_API_KEYS — comma-separated Anthropic API keys (optional)

set -euo pipefail

CLUSTER_NAME="${E2E_CLUSTER_NAME:-devops-copilot-e2e}"
NAMESPACE="devops-test"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "==> Checking prerequisites"
for cmd in kind kubectl docker; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "ERROR: '$cmd' is not installed or not in PATH." >&2
    exit 1
  fi
done

echo "==> Creating kind cluster: $CLUSTER_NAME"
if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
  echo "    Cluster '$CLUSTER_NAME' already exists — reusing."
else
  kind create cluster --name "$CLUSTER_NAME" --wait 90s
fi

echo "==> Switching kubectl context to kind cluster"
kubectl config use-context "kind-${CLUSTER_NAME}"

echo "==> Building Docker images"
docker build -t logicule/sample-app:latest "${REPO_ROOT}/sample-app"
docker build -t logicule/agent-backend:latest "${REPO_ROOT}/agent-backend"

echo "==> Loading images into kind cluster (avoids DockerHub pull limits)"
kind load docker-image logicule/sample-app:latest --name "$CLUSTER_NAME"
kind load docker-image logicule/agent-backend:latest --name "$CLUSTER_NAME"

echo "==> Creating namespace: $NAMESPACE"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

echo "==> Creating LLM credentials secret"
kubectl create secret generic llm-credentials \
  --namespace="$NAMESPACE" \
  --from-literal=api_key="${LLM_API_KEY:-placeholder}" \
  --from-literal=google_api_keys="${GOOGLE_API_KEYS:-placeholder}" \
  --from-literal=anthropic_api_keys="${ANTHROPIC_API_KEYS:-placeholder}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "==> Applying test manifests"
kubectl apply -f "${REPO_ROOT}/k8s/test/elasticsearch.yaml"
kubectl apply -f "${REPO_ROOT}/k8s/test/mock-downstream.yaml"
kubectl apply -f "${REPO_ROOT}/k8s/test/sample-app.yaml"
kubectl apply -f "${REPO_ROOT}/k8s/test/agent-backend.yaml"

echo "==> Waiting for Elasticsearch (up to 5 minutes)"
kubectl rollout status deployment/elasticsearch -n "$NAMESPACE" --timeout=300s

echo "==> Waiting for remaining deployments"
kubectl rollout status deployment/sample-app     -n "$NAMESPACE" --timeout=120s
kubectl rollout status deployment/mock-downstream -n "$NAMESPACE" --timeout=120s
kubectl rollout status deployment/agent-backend  -n "$NAMESPACE" --timeout=180s

echo ""
echo "============================================================"
echo " E2E cluster ready: $CLUSTER_NAME / namespace: $NAMESPACE"
echo "============================================================"
echo ""
echo "Port-forward commands (run in separate terminals):"
echo "  kubectl port-forward svc/agent-backend  8001:8001 -n $NAMESPACE"
echo "  kubectl port-forward svc/sample-app     8000:8000 -n $NAMESPACE"
echo ""
echo "Run e2e tests:"
echo "  cd agent-backend && pytest tests/e2e/ -v -s"
echo ""
echo "Teardown:"
echo "  bash scripts/e2e_teardown.sh"
