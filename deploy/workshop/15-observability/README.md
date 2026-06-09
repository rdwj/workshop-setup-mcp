# Module 15: Observability

Add centralized logging and metrics dashboards to the MCP ecosystem using
the Cluster Observability Operator (COO) for Perses dashboards and the
Loki stack for log aggregation. After this module, you can view pod
metrics, resource usage, and application logs for the MCP Gateway, MCP
servers, and Keycloak from the OpenShift console.

**Time:** 30--45 minutes

**Prerequisites:**

- Modules 0, 6, 8--10 complete (MCP Gateway, MCP server, Keycloak)
- MinIO deployed (Module 13 Step 2, or `13-playground/minio.yaml` applied separately)

> **Working directory:** `cd deploy/workshop/15-observability`

## Variables

```bash
CTX="<your-kube-context>"
```

---

## Step 1: Install the Cluster Observability Operator

COO provides Perses dashboard support in the OpenShift console.

```bash
oc apply -f cluster-observability-operator.yaml --context="$CTX"
```

Wait for the CSV to succeed:

```bash
oc get csv -n openshift-operators --context="$CTX" \
  -l operators.coreos.com/cluster-observability-operator.openshift-operators -w
```

You should see `cluster-observability-operator.v*` reach `Succeeded`.

> **Note:** If the InstallPlan requires manual approval, approve it:
>
> ```bash
> PLAN=$(oc get installplan -n openshift-operators --context="$CTX" \
>   -o jsonpath='{.items[?(@.spec.approved==false)].metadata.name}')
> oc patch installplan "$PLAN" -n openshift-operators --context="$CTX" \
>   --type=merge -p '{"spec":{"approved":true}}'
> ```

## Step 2: Install the Loki and Cluster Logging Operators

This creates the `openshift-logging` namespace, an OwnNamespace
OperatorGroup, and subscriptions for the Loki Operator (stable-6.2) and
Cluster Logging operator (stable-6.2):

```bash
oc apply -f loki-operator.yaml --context="$CTX"
```

Wait for both CSVs:

```bash
oc get csv -n openshift-logging --context="$CTX" -w
```

You should see both `loki-operator.v*` and `cluster-logging.v*` reach
`Succeeded`. This may take 2--3 minutes while CRDs are registered.

> **Note:** As with Step 1, check for pending InstallPlans if CSVs don't
> appear:
>
> ```bash
> PLAN=$(oc get installplan -n openshift-logging --context="$CTX" \
>   -o jsonpath='{.items[?(@.spec.approved==false)].metadata.name}')
> oc patch installplan "$PLAN" -n openshift-logging --context="$CTX" \
>   --type=merge -p '{"spec":{"approved":true}}'
> ```

## Step 3: Ensure MinIO is Running

LokiStack requires S3-compatible object storage. This workshop uses the
MinIO instance from Module 13. If you haven't deployed it yet:

```bash
oc apply -f ../13-playground/minio.yaml --context="$CTX"
```

Verify MinIO is ready:

```bash
oc wait deployment/minio -n minio --context="$CTX" \
  --for=condition=Available --timeout=120s
```

!!! important "MinIO Must Be Running Before LokiStack"

    The LokiStack will not reach Ready status if it cannot connect to the
    S3 endpoint. Confirm MinIO is available before proceeding.

## Step 4: Deploy the LokiStack

Create the MinIO credentials Secret and the LokiStack CR. The LokiStack
uses the `1x.demo` size, which is a minimal single-instance deployment
suitable for workshops:

```bash
oc apply -f lokistack.yaml --context="$CTX"
```

Wait for LokiStack to become Ready:

```bash
oc wait lokistack/logging-loki -n openshift-logging --context="$CTX" \
  --for=condition=Ready --timeout=300s
```

This may take several minutes as Loki starts its compactor, distributor,
ingester, querier, and gateway components.

If the LokiStack does not become Ready, check pod status:

```bash
oc get pods -n openshift-logging --context="$CTX" -l app.kubernetes.io/instance=logging-loki
```

## Step 5: Configure Log Forwarding

Deploy the collector ServiceAccount, RBAC bindings, CA ConfigMap, and
ClusterLogForwarder:

```bash
oc apply -f clusterlogforwarder.yaml --context="$CTX"
```

This creates:

- A `collector` ServiceAccount in `openshift-logging`
- Three ClusterRoleBindings granting the collector permission to read
  application logs, infrastructure logs, and write to Loki
- A CA ConfigMap with the `service.beta.openshift.io/inject-cabundle`
  annotation for TLS trust
- A ClusterLogForwarder that sends application and infrastructure logs
  to the LokiStack

!!! warning "CA Bundle Injection"

    The `openshift-service-ca.crt` ConfigMap uses the annotation
    `service.beta.openshift.io/inject-cabundle: "true"`. This tells
    OpenShift's service-ca-operator to inject the cluster CA certificate.
    Without it, the Vector log collectors cannot verify the LokiStack
    gateway's TLS certificate and log forwarding will fail silently.

!!! warning "Loki Write Access"

    The `logging-collector-logs-writer` ClusterRoleBinding is required
    for the collector to write logs to Loki. Without it, the collectors
    receive 403 errors and no logs are ingested.

Wait for the collector pods to start:

```bash
oc get pods -n openshift-logging --context="$CTX" -l app.kubernetes.io/component=collector -w
```

You should see collector pods running on each node (one per node as a
DaemonSet).

## Step 6: Enable the Perses UI Plugin

Activate Perses dashboarding in the OpenShift console:

```bash
oc apply -f perses-uiplugin.yaml --context="$CTX"
```

After applying, refresh the OpenShift console. The **Observe > Dashboards**
section will now use Perses as the dashboard backend.

## Step 7: Create the Perses Datasource and Dashboard

Connect Perses to the Loki log store and deploy the MCP overview
dashboard:

```bash
oc apply -f loki-datasource.yaml --context="$CTX"
oc apply -f dashboard-mcp-overview.yaml --context="$CTX"
```

The datasource connects to the LokiStack gateway's application log
tenant. The dashboard provides four panel groups:

- **Pod Status** -- Running pod count and container restart totals across
  `mcp-system`, `mcp-ecosystem`, and `keycloak`
- **Gateway Resources** -- CPU and memory usage for pods in `mcp-system`
- **Server Resources** -- CPU and memory for pods in `mcp-ecosystem`
- **Keycloak Resources** -- CPU and memory for pods in `keycloak`

## Step 8: Enable Authorino Evaluator Metrics

The AuthPolicy needs `metrics: true` on each evaluator for per-evaluator
Prometheus metrics. This was already added to the AuthPolicy manifest in
Module 10. If you applied the AuthPolicy before this change, re-apply it:

```bash
KEYCLOAK_ISSUER="https://keycloak-keycloak.${CLUSTER_DOMAIN}/realms/mcp-gateway"
sed "s|KEYCLOAK_ISSUER|${KEYCLOAK_ISSUER}|g" ../10-identity-auth/authpolicy.yaml \
  | oc apply -f - --context="$CTX"
```

If evaluator metrics don't appear in Prometheus, also enable deep metrics
on the Authorino CR:

```bash
oc patch authorino authorino -n kuadrant-system --context="$CTX" \
  --type=merge -p '{"spec":{"metrics":{"deep":true}}}'
```

## Step 9: Create Prometheus Scrape Targets

Create ServiceMonitors and a PodMonitor so Prometheus scrapes the MCP
ecosystem components:

```bash
oc apply -f servicemonitor-authorino.yaml --context="$CTX"
oc apply -f servicemonitor-limitador.yaml --context="$CTX"
oc apply -f podmonitor-envoy-gateway.yaml --context="$CTX"
```

This enables scraping for:

- **Authorino** -- auth evaluator call counts, duration, response status
- **Limitador** -- authorized vs rate-limited call counts
- **Envoy gateway** -- istio_requests_total with response codes, latency
  histograms, kuadrant allow/deny counters

Verify the targets appear in Prometheus (may take up to 60 seconds):

```bash
oc exec -n openshift-user-workload-monitoring prometheus-user-workload-0 \
  -c prometheus --context="$CTX" -- \
  curl -s 'localhost:9090/api/v1/targets?state=active' \
  | python3 -c "
import sys, json
for t in json.load(sys.stdin)['data']['activeTargets']:
    j = t['labels'].get('job','')
    if 'authorino' in j or 'limitador' in j or 'mcp-gateway' in j:
        print(f'{j}: {t[\"health\"]}')"
```

You should see all targets reporting `up`.

## Step 10: Enable Access Logging (Optional)

For per-user request tracking via Loki, enable structured JSON access
logging on the Istio gateway. This patches the `openshift-gateway` Istio
CR to add a custom access log provider that captures the authenticated
username from Authorino dynamic metadata.

```bash
./patch-istio-access-log.sh "$CTX"
oc apply -f mcp-gateway-access-logging.yaml --context="$CTX"
oc rollout restart deployment/mcp-gateway-data-science-gateway-class \
  -n mcp-system --context="$CTX"
```

> **Note:** The gateway pod is managed by the `openshift-gateway` Istio
> revision (v1.26.2) in `openshift-ingress`, not the `default` Istio in
> `istio-system`. The patch script targets the correct Istio CR.

> **Note:** The `mcp-system` namespace must have the label
> `istio-discovery=enabled` for the Telemetry resource to take effect:
>
> ```bash
> oc label namespace mcp-system istio-discovery=enabled --context="$CTX"
> ```

## Verify

```bash
# COO operator installed
oc get csv -n openshift-operators --context="$CTX" \
  -l operators.coreos.com/cluster-observability-operator.openshift-operators \
  -o jsonpath='{.items[0].status.phase}'
# Expected: Succeeded

# Loki operator installed
oc get csv -n openshift-logging --context="$CTX" \
  -l operators.coreos.com/loki-operator.openshift-logging \
  -o jsonpath='{.items[0].status.phase}'
# Expected: Succeeded

# LokiStack is Ready
oc get lokistack logging-loki -n openshift-logging --context="$CTX" \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'
# Expected: True

# ClusterLogForwarder is running
oc get clusterlogforwarder collector -n openshift-logging --context="$CTX"
# Expected: exists

# Collector pods are running
oc get pods -n openshift-logging --context="$CTX" \
  -l app.kubernetes.io/component=collector \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.phase}{"\n"}{end}'
# Expected: Running on each node

# Perses UIPlugin exists
oc get uiplugin monitoring --context="$CTX"
# Expected: exists

# Dashboard deployed
oc get persesdashboard mcp-ecosystem-overview -n openshift-operators --context="$CTX"
# Expected: exists

# Prometheus scrape targets are up
oc exec -n openshift-user-workload-monitoring prometheus-user-workload-0 \
  -c prometheus --context="$CTX" -- \
  curl -s 'localhost:9090/api/v1/targets?state=active' \
  | python3 -c "
import sys, json
for t in json.load(sys.stdin)['data']['activeTargets']:
    j = t['labels'].get('job','')
    if 'authorino' in j or 'limitador' in j or 'mcp-gateway' in j:
        print(f'{j}: {t[\"health\"]}')"
# Expected: all targets showing 'up'

# Authorino evaluator metrics are flowing
oc exec -n openshift-user-workload-monitoring prometheus-user-workload-0 \
  -c prometheus --context="$CTX" -- \
  curl -s 'localhost:9090/api/v1/query?query=auth_server_evaluator_total' \
  | python3 -c "
import sys, json
for r in json.load(sys.stdin)['data']['result']:
    print(f'{r[\"metric\"][\"evaluator_name\"]}: {r[\"value\"][1]}')"
# Expected: keycloak-jwt, tool-roles, x-authorized-tools with non-zero counts
```

## What You Deployed

| Resource | Namespace | Purpose |
|----------|-----------|---------|
| COO Subscription | `openshift-operators` | Cluster Observability Operator for Perses dashboard support |
| Loki Operator Subscription | `openshift-logging` | Manages LokiStack instances for log storage |
| Cluster Logging Subscription | `openshift-logging` | Provides the ClusterLogForwarder CRD and Vector collectors |
| Secret `loki-minio-credentials` | `openshift-logging` | S3 credentials for LokiStack to connect to MinIO |
| LokiStack `logging-loki` | `openshift-logging` | Log storage backend (1x.demo size, S3 via MinIO) |
| ServiceAccount `collector` | `openshift-logging` | Identity for log collector pods |
| ClusterRoleBindings (x3) | (cluster-wide) | Grant collector read access to app/infra logs and write access to Loki |
| ConfigMap `openshift-service-ca.crt` | `openshift-logging` | CA bundle for TLS between collectors and LokiStack |
| ClusterLogForwarder `collector` | `openshift-logging` | Routes application and infrastructure logs to LokiStack |
| UIPlugin `monitoring` | (cluster-wide) | Enables Perses dashboards in the OpenShift console |
| PersesDatasource `loki-mcp-logs` | `openshift-logging` | Connects Perses to the LokiStack application log API |
| PersesDashboard `mcp-ecosystem-overview` | `openshift-operators` | Metrics dashboard for MCP Gateway, MCP servers, and Keycloak |
| ServiceMonitor `authorino-metrics` | `kuadrant-system` | Scrapes Authorino auth evaluator metrics |
| ServiceMonitor `limitador-metrics` | `kuadrant-system` | Scrapes Limitador rate-limit metrics |
| PodMonitor `mcp-gateway-envoy` | `mcp-system` | Scrapes Envoy gateway Istio/Kuadrant metrics |
| Telemetry `mcp-gateway-access-logging` | `mcp-system` | Enables structured access logging on the gateway |

---

**Next**: [Workshop Overview](../README.md)
