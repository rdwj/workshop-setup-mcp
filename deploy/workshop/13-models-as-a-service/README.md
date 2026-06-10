# Module 13: Models as a Service (Optional Model Track)

Enable RHOAI's Models as a Service (MaaS) infrastructure so you can deploy
models from the Model Catalog and create API subscriptions. This module
deploys a PostgreSQL database, creates the MaaS Gateway with TLS,
configures Authorino for authentication, and patches the DataScienceCluster
to activate MaaS.

**Time:** 30--45 minutes

!!! note "Skippable -- only needed for on-cluster model serving"
    If you skipped Module 12 (no GPU), skip this module and
    [Module 14](../14-deploy-model/README.md) as well. Continue to
    [Module 15 -- Model Endpoint](../15-model-endpoint/README.md).

**Prerequisites:**

- Module 0 complete (RHOAI operators installed)
- Module 1 complete (RHCL operator and `data-science-gateway-class` GatewayClass)
- Module 12 complete (GPU node available)

> **Working directory:** `cd deploy/workshop/13-models-as-a-service`

## Variables

```bash
export CTX="<your-kube-context>"
export CLUSTER_DOMAIN=$(oc get ingresses.config.openshift.io cluster --context="$CTX" \
  -o jsonpath='{.spec.domain}')
echo "CLUSTER_DOMAIN=$CLUSTER_DOMAIN"
```

## Step 1: Deploy PostgreSQL for MaaS

MaaS uses PostgreSQL to store model registrations, subscriptions, and API
keys:

```bash
oc apply -f maas-postgresql.yaml --context="$CTX"
```

Wait for PostgreSQL to be ready:

```bash
oc wait deployment/maas-postgresql -n maas-postgresql --context="$CTX" \
  --for=condition=Available --timeout=120s
```

## Step 2: Enable User Workload Monitoring

MaaS requires Prometheus user workload monitoring for inference metrics:

```bash
oc apply -f user-workload-monitoring.yaml --context="$CTX"
```

!!! note "Already enabled?"
    If user workload monitoring is already enabled on your cluster,
    this command will update the existing ConfigMap without effect.

## Step 3: Create the MaaS Gateway

MaaS requires a dedicated Gateway named `maas-default-gateway` in the
`openshift-ingress` namespace. The RHOAI operator does not create this
automatically.

```bash
sed "s/<CLUSTER_DOMAIN>/${CLUSTER_DOMAIN}/g" maas-gateway.yaml \
  | oc apply --context="$CTX" -f -
```

Create a TLS certificate with the correct Subject Alternative Name for
the Gateway's wildcard hostname:

```bash
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout /tmp/maas-gateway.key \
  -out /tmp/maas-gateway.crt \
  -subj "/CN=*.maas.${CLUSTER_DOMAIN}" \
  -addext "subjectAltName=DNS:*.maas.${CLUSTER_DOMAIN}"

oc create secret tls maas-default-gateway-cert \
  -n openshift-ingress --context="$CTX" \
  --cert=/tmp/maas-gateway.crt \
  --key=/tmp/maas-gateway.key

rm -f /tmp/maas-gateway.key /tmp/maas-gateway.crt
```

!!! warning "Do not use the service serving cert"
    OpenShift's `service.beta.openshift.io/serving-cert-secret-name`
    annotation generates a cert with SANs for the service's internal
    DNS name, not the external `*.maas.<domain>` hostname. The Gateway
    needs a cert whose SAN matches its listener hostname, otherwise
    Envoy returns `filter_chain_not_found`.

Wait for the Gateway to be accepted:

```bash
oc get gateway maas-default-gateway -n openshift-ingress --context="$CTX" \
  -o jsonpath='{.status.conditions[?(@.type=="Accepted")].status}'
```

Expected: `True`

## Step 4: Create MaaS Routes

Create OpenShift Routes for external access to the MaaS Gateway:

```bash
sed "s/<CLUSTER_DOMAIN>/${CLUSTER_DOMAIN}/g" maas-routes.yaml \
  | oc apply --context="$CTX" -f -
```

This creates two passthrough TLS routes:

- `inference.maas.<CLUSTER_DOMAIN>` -- model inference endpoint
- `maas.<CLUSTER_DOMAIN>` -- MaaS API endpoint

## Step 5: Configure Authorino TLS

MaaS uses Authorino for API authentication. Two configuration steps
are needed for TLS to work end-to-end. See the
[MaaS TLS documentation](https://opendatahub-io.github.io/models-as-a-service/latest/configuration-and-management/tls-configuration/)
for details.

**5a.** Annotate the Authorino authorization service for certificate
generation:

```bash
oc annotate service authorino-authorino-authorization \
  -n kuadrant-system --context="$CTX" \
  service.beta.openshift.io/serving-cert-secret-name=authorino-server-cert \
  --overwrite
```

**5b.** Configure SSL environment variables for Authorino's outbound
HTTPS calls to `maas-api`:

```bash
oc -n kuadrant-system --context="$CTX" set env deployment/authorino \
  SSL_CERT_FILE=/etc/ssl/certs/openshift-service-ca/service-ca-bundle.crt \
  REQUESTS_CA_BUNDLE=/etc/ssl/certs/openshift-service-ca/service-ca-bundle.crt
```

## Step 6: Create the Database Configuration Secret

The MaaS API needs a connection string to reach PostgreSQL. This secret
must exist before the DataScienceCluster is patched in the next step,
otherwise the MaaS component cannot become ready:

```bash
oc apply -f maas-db-config-secret.yaml --context="$CTX"
```

## Step 7: Enable MaaS in the DataScienceCluster

Patch the DataScienceCluster to enable Models as a Service. The operator
requires the `maas-default-gateway` Gateway and `maas-db-config` Secret
to exist before this step succeeds:

```bash
oc patch datasciencecluster default-dsc --context="$CTX" \
  --type merge \
  -p '{"spec":{"components":{"kserve":{"modelsAsService":{"managementState":"Managed"}}}}}'
```

Wait for the DataScienceCluster to reconcile:

```bash
oc wait datasciencecluster default-dsc --context="$CTX" \
  --for=condition=Ready --timeout=300s
```

!!! warning "ModelsAsServiceReady: GatewayNotReady"
    If the DSC shows `ModelsAsServiceReady: False` with message
    `gateway openshift-ingress/maas-default-gateway not found`, the
    Gateway from Step 3 was not created or is in a different namespace.
    Verify with: `oc get gateway maas-default-gateway -n openshift-ingress --context="$CTX"`

## Step 8: Wait for the MaaS API

The RHOAI operator deploys the MaaS API and controller after the DSC
patch. Wait for both:

```bash
oc wait deployment/maas-api -n redhat-ods-applications --context="$CTX" \
  --for=condition=Available --timeout=300s

oc wait deployment/maas-controller -n redhat-ods-applications --context="$CTX" \
  --for=condition=Available --timeout=300s
```

## Step 9: Verify MaaS CRDs

MaaS creates several Custom Resource Definitions. Confirm they exist:

```bash
oc get crd --context="$CTX" | grep maas
```

You should see CRDs including `externalmodels`, `maasauthpolicies`,
`maasmodelrefs`, `maassubscriptions`, and `tenants` in the
`maas.opendatahub.io` group.

## Verify

```bash
# MaaS API is running
oc get deployment maas-api -n redhat-ods-applications --context="$CTX" \
  -o jsonpath='{.status.availableReplicas}'
# Expected: 1

# MaaS controller is running
oc get deployment maas-controller -n redhat-ods-applications --context="$CTX" \
  -o jsonpath='{.status.availableReplicas}'
# Expected: 1

# PostgreSQL is running
oc get deployment maas-postgresql -n maas-postgresql --context="$CTX" \
  -o jsonpath='{.status.availableReplicas}'
# Expected: 1

# MaaS CRDs exist
oc get crd --context="$CTX" -o name | grep -c maas
# Expected: 5

# Default tenant created
oc get tenants -n models-as-a-service --context="$CTX"
# Expected: default-tenant with READY=True

# Gateway is programmed
oc get gateway maas-default-gateway -n openshift-ingress --context="$CTX" \
  -o jsonpath='{.status.conditions[?(@.type=="Programmed")].status}'
# Expected: True

# DSC shows MaaS as Managed
oc get datasciencecluster default-dsc --context="$CTX" \
  -o jsonpath='{.spec.components.kserve.modelsAsService.managementState}'
# Expected: Managed
```

!!! note "Check the RHOAI Dashboard"
    Open the RHOAI Dashboard in your browser. You should now see
    **Model Catalog** and **Deployed Models** sections in the
    navigation. If these don't appear, the OdhDashboardConfig may
    need a page refresh or the MaaS API may still be starting.

## What You Deployed

| Resource | Namespace | Purpose |
|----------|-----------|---------|
| Namespace `maas-postgresql` | -- | Isolation for PostgreSQL database |
| PVC `maas-postgresql-data` | `maas-postgresql` | 5 GiB persistent storage for MaaS database |
| Deployment `maas-postgresql` | `maas-postgresql` | PostgreSQL 16 database for MaaS state |
| Service `maas-postgresql` | `maas-postgresql` | Internal access to PostgreSQL on port 5432 |
| ConfigMap `cluster-monitoring-config` | `openshift-monitoring` | Enables user workload monitoring for inference metrics |
| Gateway `maas-default-gateway` | `openshift-ingress` | MaaS inference gateway (HTTPS, TLS termination) |
| Secret `maas-default-gateway-cert` | `openshift-ingress` | TLS certificate for `*.maas.<domain>` |
| Route `maas-gateway` | `openshift-ingress` | External access to MaaS API |
| Route `maas-inference-gateway` | `openshift-ingress` | External access to model inference |
| Authorino TLS | `kuadrant-system` | TLS cert annotation and CA bundle for auth |
| Secret `maas-db-config` | `redhat-ods-applications` | PostgreSQL connection URL for MaaS API |
| DSC patch | (cluster-wide) | Enables `modelsAsService: Managed` in the DataScienceCluster |
| Deployment `maas-api` | `redhat-ods-applications` | MaaS API server (created by RHOAI operator) |
| Deployment `maas-controller` | `redhat-ods-applications` | MaaS controller (created by RHOAI operator) |

---

**Next**: [Module 14 -- Deploy a Model](../14-deploy-model/README.md)
