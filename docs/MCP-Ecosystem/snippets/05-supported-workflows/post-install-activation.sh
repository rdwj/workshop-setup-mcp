        oc patch odhdashboardconfigs odh-dashboard-config \
    -n redhat-ods-applications \
    --type=merge \
    -p '{"spec":{"dashboardConfig":{"mcpCatalog":true,"genAiStudio":true}}}'
