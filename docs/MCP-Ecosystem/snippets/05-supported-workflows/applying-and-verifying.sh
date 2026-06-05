oc apply -f mcp-catalog-sources.yaml
oc rollout restart deployment model-catalog -n odh-model-registries
oc rollout status deployment model-catalog -n odh-model-registries --timeout=60s
