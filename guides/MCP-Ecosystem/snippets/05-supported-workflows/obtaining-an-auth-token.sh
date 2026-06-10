TOKEN=$(curl -s -X POST \
  https://<keycloak-route>/realms/mcp-gateway/protocol/openid-connect/token \
  -d "grant_type=password&client_id=mcp-playground&username=<user>&password=<pass>&scope=openid groups" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
