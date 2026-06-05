#!/usr/bin/env bash
# Generate ECDSA P-256 key pair and create Kubernetes secrets for wristband signing.
#
# Creates:
#   - wristband-signing-key in kuadrant-system (private key, for Authorino)
#   - wristband-public-key in mcp-system (public key, for MCP Gateway broker)
#
# Then patches the MCPGatewayExtension to reference the public key secret.
#
# Usage:
#   export CTX="default/api-cluster-.../kube:admin"
#   bash generate-wristband-keys.sh
set -euo pipefail

: "${CTX:?Set CTX to your kube context}"

TMPDIR=$(mktemp -d)
trap "rm -rf ${TMPDIR}" EXIT

echo "--- Generating ECDSA P-256 key pair ---"
openssl ecparam -name prime256v1 -genkey -noout -out "${TMPDIR}/private.pem" 2>/dev/null
openssl ec -in "${TMPDIR}/private.pem" -pubout -out "${TMPDIR}/public.pem" 2>/dev/null
echo "  Keys generated"

echo "--- Creating wristband-signing-key in kuadrant-system ---"
oc create secret generic wristband-signing-key \
  -n kuadrant-system --context="$CTX" \
  --from-file=key.pem="${TMPDIR}/private.pem" \
  --dry-run=client -o yaml | oc apply --context="$CTX" -f -

echo "--- Creating wristband-public-key in mcp-system ---"
oc create secret generic wristband-public-key \
  -n mcp-system --context="$CTX" \
  --from-file=key="${TMPDIR}/public.pem" \
  --dry-run=client -o yaml | oc apply --context="$CTX" -f -

echo "--- Patching MCPGatewayExtension ---"
oc patch mcpgatewayextension mcp-gateway -n mcp-system --context="$CTX" --type=merge \
  -p '{"spec":{"trustedHeadersKey":{"generate":"Disabled","secretName":"wristband-public-key"}}}'

echo "--- Verifying ---"
oc get mcpgatewayextension mcp-gateway -n mcp-system --context="$CTX" \
  -o jsonpath='{.spec.trustedHeadersKey}' && echo
echo "Done"
