# Portable examples

Replace documentation IPs, example identities and image placeholders with operator inputs.

- `helm-values.yaml`: controller image, load balancer node endpoint, existing token Secret and IP pools.
- `systemd/zlb-natd.service`: standalone Linux daemon configuration when SSH bootstrap is disabled.
- `pools.json`: standalone controller pool configuration example.
- `kubernetes/services.yaml`: ordinary NodePort publication and a reserved ingress Service.
- `kubernetes/mixed-protocol-service.yaml`: TCP/UDP/SCTP port declarations for a compatible workload.
- `kubernetes/canary.yaml`: temporary HTTP workload and LoadBalancer Service for traffic tests.

The chart does not discover network topology. The load balancer node's public allowlist, routing and
interface must match your installation. See [installation](../docs/installation.md).
