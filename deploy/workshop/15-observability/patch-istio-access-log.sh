#!/usr/bin/env bash
# Patch the Istio CR to add a custom JSON access log provider that captures
# per-user identity from Authorino dynamic metadata.
#
# This adds a new extension provider named "mcp-access-log" that writes
# structured JSON to stdout. The "username" field comes from Authorino's
# dynamicMetadata response (set in the AuthPolicy).
#
# The gateway pod is managed by the openshift-gateway Istio revision
# in the openshift-ingress namespace (NOT the default Istio in istio-system).
#
# After patching, restart the gateway pod and apply the Telemetry resource:
#   oc rollout restart deployment/mcp-gateway-data-science-gateway-class -n mcp-system
#   oc apply -f mcp-gateway-access-logging.yaml
#
# Usage: ./patch-istio-access-log.sh [CONTEXT]

set -euo pipefail

CTX="${1:-$(oc config current-context)}"

oc patch istio openshift-gateway -n openshift-ingress --context="$CTX" --type=merge -p '
{
  "spec": {
    "values": {
      "meshConfig": {
        "extensionProviders": [
          {
            "name": "mcp-access-log",
            "envoyFileAccessLog": {
              "path": "/dev/stdout",
              "logFormat": {
                "labels": {
                  "timestamp": "%START_TIME%",
                  "method": "%REQ(:METHOD)%",
                  "path": "%REQ(X-ENVOY-ORIGINAL-PATH?:PATH)%",
                  "protocol": "%PROTOCOL%",
                  "response_code": "%RESPONSE_CODE%",
                  "response_flags": "%RESPONSE_FLAGS%",
                  "bytes_received": "%BYTES_RECEIVED%",
                  "bytes_sent": "%BYTES_SENT%",
                  "duration_ms": "%DURATION%",
                  "authority": "%REQ(:AUTHORITY)%",
                  "upstream_host": "%UPSTREAM_HOST%",
                  "upstream_cluster": "%UPSTREAM_CLUSTER%",
                  "request_id": "%REQ(X-REQUEST-ID)%",
                  "username": "%DYNAMIC_METADATA(envoy.filters.http.ext_authz:user-identity:username)%"
                }
              }
            }
          }
        ]
      }
    }
  }
}'

echo "Istio CR patched."
echo "Restart the gateway pod:"
echo "  oc rollout restart deployment/mcp-gateway-data-science-gateway-class -n mcp-system --context=$CTX"
echo ""
echo "Verify with:"
echo "  oc get istio openshift-gateway -n openshift-ingress --context=$CTX -o jsonpath='{.spec.values.meshConfig.extensionProviders}' | python3 -m json.tool"
