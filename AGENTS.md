# AGENTS.md

Use this file as the repository-wide maintainer guidance. Keep future agent instructions
here; user-facing documentation belongs in README.md and docs/.

zlb is a reusable Kubernetes LoadBalancer controller plus Linux load balancer node DNAT daemon.
The primary user entry point is README.md; docs/guide.md is the operator guide and docs/internals.md
the design reference. Keep docs short, with each fact in one place.
Use charts/zlb for Kubernetes installation and examples/ for portable operator inputs.
Installation-specific infrastructure and operating records belong outside this repository.
Helm deploys the controller. Optional SSH bootstrap manages packages and the daemon on one
explicit VM; infrastructure owns VM creation, trusted host keys, routing and firewall policy.
## Project boundaries

- Keep zlb independent of hosting provider, CNI, ingress controller, registry and secret manager.
- External IPs, load balancer node address, node CIDR, image registry and credentials are operator inputs.
  Use documentation-only external IPs in public examples. Never copy reference-deployment
  addresses, registry credentials, kubeconfigs or application policy exceptions into defaults.
- Annotations/class under zlb.zarrino.tech are compatibility identifiers, not an
  infrastructure dependency. Do not rename the wire API as an incidental documentation change.
- Public repository: https://github.com/abexamir/zlb. Images: ghcr.io/abexamir/zlb.
  OCI charts: ghcr.io/abexamir/charts/zlb. License: Apache-2.0.
- Keep deployment credentials, real infrastructure addresses and local overrides out of Git.
  Keep published Git history free of deployment state and extracted third-party research.

## Current implementation

Python 3.11+ with the standard library. The controller keeps list+watch caches of trimmed
Services, Nodes and discovery/v1 EndpointSlices and reconciles from them every five seconds
with jitter. Only push a full snapshot while every cache reflects a complete, successful
list and an unbroken watch, and treat null endpoint lists as empty.

The load balancer node uses native nftables for IPv4 TCP, UDP and SCTP in one owned `table ip zlb`.
One controller posts full desired state (wire schema v3; the daemon also accepts v2) to one load balancer node. Ordinary ports publish
NodePorts; administrator reservations publish literal Service ports or dedicate an IP.
Unclassified LoadBalancer Services are adopted. Listener identity includes protocol.
The load balancer node assigns configured IPs to an explicit public interface. Its persistent address
journal owns only addresses it added; preexisting and primary addresses must survive cleanup.
It does not SNAT external clients (in-cluster hairpin SNAT is opt-in) or manage the whole firewall. Return routing, CNI protocol support
and application NetworkPolicies remain operator responsibilities.

Keep the compiler pure, validate the wire format before rule generation, preserve independent
allowlists and unrelated firewall rules, and acknowledge only verified applies. Preserve the
cleanup finalizer, resourceVersion-guarded patches and rollback semantics. Load balancer node acceptance
must precede publishing Service status or releasing cleanup finalizers. Never persist derived
zlb rules or restore a whole firewall dump over unrelated rules. Validate with nft --check
before mutation, commit the owned table atomically, and verify readback before acknowledgement.
Keep IPv4 and single-controller limitations explicit; do not advertise load balancer node HA.

## Development

See CONTRIBUTING.md for commands. Unit tests run with
`python3 -m unittest discover -s tests -v`; Linux netns tests use tests/Dockerfile.nat.
Documentation-only changes need link/example validation rather than live deployment.
Validate the Helm chart with the portable example values; never embed an installation
registry, private subnet, credential or Service identity into chart defaults.

Reference deployment changes must always use that cluster's explicit kubeconfig/API target.
The operator's main kubeconfig is a different cluster and must not be modified or used for
reference-deployment mutations. Never store tokens or secret-bearing outputs in Git.
