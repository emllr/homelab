# Observability and alerting

## Trust
Telemetry comes from hosts that might be compromised, so the backend is isolated like any other exposed VM. Each
origin has its own collector port and bearer token. The port, not the sender, sets `telemetry.origin`, so a host
can't pass itself off as another.

## Alerts
Rules are provisioned from [`grafana-alerting.yaml.j2`](../roles/observability/templates/grafana-alerting.yaml.j2).

- **Return a value only when something is wrong.** Expressions filter to the bad case, and "no data" counts as fine
  unless silence is itself the fault; those rules test for silence explicitly.
- **Hold times** of 3 to 30 minutes, so redeploys don't page.
- **Baselines from the metric itself.** An annotation says what normal looks like, measured with the same metric
  and window as the rule. Figures from another source didn't fit, because the alert's metric also counts retries.
- **The first check goes in the alert**, naming the usual wrong conclusion, e.g. connection timeouts across many job
  types point to a blocked event loop rather than the database.
- **Two priorities:** critical makes a sound, everything else doesn't.
