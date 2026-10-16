# Architecture and failure behavior

For the full illustrated walkthrough and command trace, see [installation and packet lifecycle](lifecycle.md).

## Components

The controller runs inside Kubernetes using a ServiceAccount. It keeps watch-backed caches of
Services, Nodes and EndpointSlices that store only the fields it needs. Every tick reads
those caches (no API reads), compiles the desired listeners, and posts the entire state to
the load balancer node. A tick does nothing unless every cache reflects a complete list and
an unbroken watch. It needs no cloud API or external IP-rental service.

The Linux load balancer node runs `zlb-natd`. The daemon authenticates the request, validates a separate
address/backend allowlist, and assigns configured interface addresses and renders native nftables DNAT/filter rules. It does not listen
on public application ports as a userspace proxy or open a second application TCP connection.

The Kubernetes NodePort datapath performs the node-to-workload portion. zlb is not tied to
Cilium: another implementation can be used if it provides the required NodePort, routing and
traffic-policy behavior. Check interoperability in your own topology; reference-deployment
verification is not a claim of exhaustive CNI compatibility.

## Reconciliation and ownership

The compiler is a pure function of the API snapshot and administrator pool configuration.
The three resource kinds are not an atomic cross-kind snapshot; later ticks converge after
concurrent Kubernetes changes. A failed or incomplete read aborts the whole tick.

Before exposing a Service, the controller adds `zlb.zarrino.tech/cleanup`. It sends the full
state and verifies the load balancer node's digest acknowledgement before publishing ingress IP status
with `ipMode: Proxy`. Service patches test resourceVersion rather than overwriting a newer
object. A failed status patch after a successful apply can leave a working listener whose
status is temporarily stale.

Deletion or a change away from LoadBalancer removes the listener, then clears ingress status
and removes only zlb's finalizer. The load balancer node must acknowledge cleanup first; otherwise
Service deletion remains pending. This is intentional protection against orphaned listeners.

One controller is expected to own the complete load balancer node state. There is no leader election,
state merge between clusters, or fencing of independent controller installations. Recreate
strategy prevents normal rollout overlap; it does not protect against additional manual
Deployments or several controller processes using the same API.

## Optional host provisioning

With explicit SSH bootstrap configuration, a one-shot Helm hook Job (and optionally a
CronJob) verifies the VM host key, installs required packages and the daemon, and maintains
its declared revision. The bootstrap pod holds the root-equivalent SSH key but no Kubernetes
API token; the controller holds API access but no SSH key. API tokens and SSH private keys
travel through mounted Secrets and encrypted SSH stdin, never command arguments. Only a
verified apply makes the controller Ready. Before the bootstrap hook runs on upgrade or
rollback, a quiesce hook stops the drift-repair CronJob and any legacy controller that still
bootstraps by itself, so an older payload cannot overwrite the new daemon.

RBAC cannot restrict `patch services` to particular fields. On Kubernetes 1.30+ the chart
installs a ValidatingAdmissionPolicy that lets the controller's ServiceAccount add or remove
only its own `zlb.zarrino.tech/cleanup` finalizer. Status writes go through the
`services/status` subresource, which cannot change spec or metadata. VM creation, routing and firewall policy
remain infrastructure responsibilities. Bootstrap is optional for preprovisioned load balancer nodes.

## Rule application

By default the daemon owns only `table ip zlb`, marked with a schema version and state
digest. Its PREROUTING NAT chain runs at priority -110; INPUT and FORWARD filter chains at
-10. Unmatched traffic continues through the load balancer node firewall. An accept in this table does
not override a drop in another base chain. Other tables and host routes are untouched.
The FORWARD chain also drops new or invalid connections arriving on the public interface
that were not DNATed, so enabling IPv4 forwarding does not turn the VM into an open router
into the node CIDR. Optional input isolation hides VM-local services on the public IPs.

Listener lookups use nftables verdict maps keyed by `(IP, protocol, port)`, so per-packet
cost does not grow with the number of listeners and the table holds one chain per listener.
Source ACLs gate DNAT. Filter rules look up the original conntrack IP, protocol and port, so
source-denied or empty-backend traffic cannot fall through to a load balancer node-local socket.
Established DNAT flows with reply sources in the node CIDR remain accepted after backend
changes, listener removal, or ACL tightening. New connections use a per-listener round-robin
`numgen inc` DNAT map; conntrack pins each flow to its selected backend. A named counter
per listener (`c_<id>`) counts new connections and keeps its value across applies.
No SNAT or conntrack flush is introduced.

An apply snapshots only the owned table, validates the complete candidate with `nft --check`,
adds missing configured interface addresses, atomically replaces the whole table, and reads
back the ownership marker, chain hooks, chains, rules, map elements and counters. Only then does it retire removed
owned addresses and acknowledge the digest. Failure attempts to restore and verify the prior
table and owned addresses. A rollback failure is logged and never acknowledged as success.
Interface changes and a firewall transaction cannot be one atomic kernel operation.

An unchanged acknowledged snapshot is not re-rendered, re-checked or committed. The daemon
reads the kernel's nftables ruleset generation over netlink, which advances on every committed
ruleset change; while it is unchanged, nothing is listed at all. After any ruleset change,
including in another table, it lists the owned table once and compares its fingerprint.
Owned-rule drift triggers a reapply. A per-table file lock prevents concurrent local writers.
An unrelated table with the same name is rejected, including during cleanup.

Interface addresses added by zlb are recorded in a persistent, atomically replaced journal.
Preexisting addresses are never recorded as owned. Cleanup and allowlist shrink remove only
exact recorded addresses; manually changed prefixes are preserved. A machine failure between
an address add and journal persistence can leave an unowned address for operator review;
zlb favors preserving administrator addresses over deleting an uncertain address.

## Outages and restarts

- Controller or Kubernetes API outage: the last acknowledged load balancer node rules continue serving.
  There is no expiry lease or automatic shutdown of stale listeners.
- Load balancer node API outage: the controller retries. It cannot publish new assignments or release
  cleanup finalizers until the load balancer node confirms the state.
- Daemon crash: systemd restarts it, and the unit's cleanup sees `$SERVICE_RESULT` is not
  `success` and keeps the table and addresses, so forwarding continues. The restarted
  daemon keeps the table until the next push.
- Managed restart (a bootstrap upgrade, or `touch /run/zlb/zlb.keep` before
  `systemctl restart`): the keep marker makes cleanup leave forwarding in place; systemd
  removes the marker with the runtime directory. A token rotation needs no restart at all,
  because the daemon re-reads its token file for every request.
- `systemctl stop` without the marker: cleanup removes the owned table and addresses.
- Load balancer node reboot: the daemon reassigns configured addresses from its persistent journal,
  installs an empty table and waits for the controller. Dynamic firewall rules are not persisted.
- Load balancer node failure: traffic through that load balancer node fails. Controller replicas do not make the
  external load balancer node highly available.

There are no active backend health probes. Node and EndpointSlice readiness plus
the Kubernetes datapath determine backend eligibility. Local policy ignores terminating
endpoints even if they are still serving.

## Source addresses and routing

DNAT preserves the client's source address between load balancer node and node. `Cluster` traffic policy
can still lead to node-side SNAT. Use Local policy and inspect the workload's observed address
when source preservation is required.

Replies must traverse the same load balancer node for reverse translation. A node's ability to reach
the load balancer node does not establish this property for every client network. Clients inside
the node network get direct replies that bypass it unless their connections are masqueraded
with the opt-in `--hairpin-snat-from`; see [in-cluster clients](installation.md#in-cluster-clients-hairpin).
Load balancer node-originated public-IP requests do not traverse PREROUTING because there is no OUTPUT
DNAT hook. Use a separate client for external reachability tests.

See [Kubernetes Services](https://kubernetes.io/docs/concepts/services-networking/service/)
for NodePort and traffic-policy semantics.
