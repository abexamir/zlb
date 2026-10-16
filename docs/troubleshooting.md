# Troubleshooting

Start with the Service, controller logs and load balancer node logs. Then follow the packet path.
Do not change application policy or global load balancer node firewall rules to mask an unverified cause.

## Service stays pending

Check that:

- It is LoadBalancer and has no explicit foreign class.
- Its selected pool exists and permits its exact namespace/name.
- The requested IP belongs to that pool and is not reserved wholesale to another Service.
- Its ports are TCP, UDP or SCTP with allocated NodePorts, and resolved public ports are not blocked.
- Pool JSON is valid and the controller can read Services, Nodes and EndpointSlices.
- The API URL, token, network path and HTTPS trust configuration are correct.
- Every pool IP is independently allowed by natd.

The controller publishes status only after load balancer node acknowledgement. Warning Events describe
per-Service failures, including collisions and anything the daemon reports it would refuse.
Global configuration and transport failures appear in the controller log, including the
daemon's own rejection reason.
An error for an unsupported field such as `shared_ports` means the configuration schema
is wrong, not that restarting Kubernetes will help.

## Assigned IP, but connection times out

1. Check the port. Ordinary Services publish their **NodePort**, not `spec.ports[].port`.
2. Inspect the load balancer node's owned nftables table for that exact public IP, protocol and port. Check counters during one test request from a separate client.
3. From the load balancer node, test the NodePort on each selected node. Use timeouts and the actual
   workload protocol. If every direct NodePort fails, investigate the cluster path rather
   than changing load balancer node DNAT rules.
4. Check EndpointSlice readiness, the application listener, CNI service maps, NetworkPolicies,
   host firewalls, and reply routes. For Local policy, only nodes with local eligible
   endpoints should be selected.
5. Capture narrowly filtered packet headers or CNI drop verdicts when necessary. Avoid
   capturing application bodies or credentials.

A LoadBalancer address does not override a NetworkPolicy. For example, a metrics policy
allowing `namespaceSelector: {}` admits Pods across namespaces; it does not automatically
admit internet clients, a load balancer node address, or a node performing Cluster-policy SNAT. First
confirm a policy-denied verdict, then have the application owner explicitly choose the
allowed sources and target port. zlb deliberately does not create these exceptions.

## Empty backends

The external listener can remain assigned while no eligible nodes exist. NAT mode blocks
new connections instead of allowing them to reach a load balancer node-local socket. Check Node Ready
conditions, exclusion labels, address CIDR, EndpointSlice conditions, and Local policy's
`nodeName` mapping. A null endpoints list is treated as empty.

## Wrong client address, or one-way connections

Confirm return routing traverses the load balancer node. Inspect `externalTrafficPolicy`: Cluster can
perform node-side SNAT, while Local is the intended choice for original-address visibility.
Use a client and Kubernetes service datapath compatible with the selected protocol.

Pods and nodes calling a public IP fail unless `--hairpin-snat-from` covers their source
address; see [in-cluster clients](installation.md#in-cluster-clients-hairpin). Locally generated
load balancer node traffic does not hit PREROUTING, so test from another host.

## Rule apply fails

`nft --check` failures reject the candidate before interface or firewall changes. Later
failures attempt to restore the previous owned table and address set. Read the daemon log
for apply errors and explicit rollback failures. Never restore a whole firewall dump.

If an existing table lacks zlb's schema marker, choose a different `--table` or investigate
its owner. Do not delete an unrelated table. For address failures, inspect `ip -4 address`,
interface names and the persistent ownership journal. Addresses on another interface are
rejected; preexisting addresses on the configured interface are preserved.

A Service whose public listener is already used by another gets a warning Event naming the
holder; the reservation owner or the older Service wins. Check for reserved literal ports
inside the Kubernetes NodePort range, duplicate administrator reservations, or competing
writers. Kubernetes' uniqueness guarantee applies to NodePorts, not independently reserved
external port numbers.

## Service deletion hangs

Inspect `zlb.zarrino.tech/cleanup` and controller/load balancer node errors. The finalizer cannot be
released while the load balancer node cannot acknowledge listener removal. Restore that path first;
manual removal requires independently verifying that the external listener is gone.

## A connection survives deletion or a narrower ACL

Established flows retain their conntrack mappings and are allowed to finish. Validate
removal using a fresh connection using the same protocol. An emergency requirement to terminate existing flows
needs a separate, carefully scoped conntrack operation; zlb does not do that automatically.

## UDP or SCTP fails while TCP works

Verify the Service protocol, allocated NodePort, matching EndpointSlice port name/protocol,
node datapath support and protocol-specific NetworkPolicies. Check the NodePort directly
from the load balancer node. SCTP needs kernel conntrack/NAT support on the load balancer node and a compatible
Kubernetes datapath; see [protocol compatibility](installation.md#protocol-compatibility).
UDP conntrack can keep a backend mapping for repeated datagrams from the same source port;
use a new source port when testing selection of a new backend.
