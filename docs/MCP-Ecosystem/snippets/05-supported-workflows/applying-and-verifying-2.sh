TOKEN=$(oc create token model-catalog -n rhoai-model-registries)
curl -sk -H "Authorization: Bearer $TOKEN" \
  "https://<model-catalog-route>/api/mcp_catalog/v1alpha1/mcp_servers"
