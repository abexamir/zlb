# Pools, Services and command-line configuration

## Pool configuration

The controller reads `--pool-config` before every reconciliation. With a ConfigMap directory
mount, changes become visible after Kubernetes propagates the volume update. Invalid JSON or
policy aborts the tick before load balancer node changes; the last acknowledged rules remain in place.

```json
{
  "default_pool": "public",
  "blocked_ports": [9808],
  "pools": [
    {
      "name": "public",
      "ips": ["198.51.100.10"],
      "reservations": [
        {"ip": "198.51.100.10", "ports": [80, 443], "service": "ingress/ingress"}
      ]
    },
    {
      "name": "dedicated",
      "ips": ["198.51.100.11"],
      "services": ["apps/mail"],
      "reservations": [
        {"ip": "198.51.100.11", "service": "apps/mail"}
      ]
    }
  ]
}
```

Replace documentation addresses with your own assigned/routed IPs. Fields have these meanings:

- `default_pool` names the pool used when a Service does not select one.
- `pools` is a non-empty list. Each pool has a unique lowercase DNS-label `name` and non-empty
  explicit `ips` list. IPs cannot overlap within or between pools. CIDR expansion is not supported.
- `services`, when present, is an exact `namespace/name` allowlist. Omission allows any eligible
  Service; an empty list allows none. There are no label-selector or namespace-wildcard rules.
- `reservations` binds an IP and optional literal ports to an exact Service identity. `ports`
  accepts numbers and inclusive `[first, last]` pairs, such as `[80, 443, [8000, 8010]]`.
  Omit `ports` to reserve the entire IP for that Service. Empty or overlapping reservations
  are invalid, as are owners outside the pool's Service allowlist.
- `blocked_ports` applies to the final published port, whether literal or a NodePort. It
  defaults to `[9808]` and takes precedence over reservations. Explicit reservations cannot
  include a blocked port; whole-IP reservations implicitly exclude blocked ports.

A reservation holds its literal port while its owner is absent. It does not create a
listener until the owning Service requests that port. Other Services can still use their
own NodePorts on that IP. A whole-IP reservation excludes every other Service entirely.

An explicitly requested IP wins if eligible. Otherwise, an existing eligible status IP is
kept; absent one, an IP with a reservation for that Service is preferred, then another
available pool IP. Selection does not spill into another named pool.

Keep reserved literal ports outside your Kubernetes NodePort range. Kubernetes makes ordinary
NodePorts unique, but cannot prevent an administrator reserving the same numeric port for
another Service. Duplicate resolved `(IP, protocol, port)` listeners reject the full snapshot rather
than choosing a winner. Do not use port reservations to bypass this constraint.

`shared_ports` is not a supported field. Unreserved ports always use NodePort numbers; there
is no separate automatic high-port allocator.

## Service behavior

No annotation is necessary for a plain `type: LoadBalancer` Service. For example:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: database
  namespace: apps
spec:
  type: LoadBalancer
  externalTrafficPolicy: Local
  selector:
    app: database
  ports:
    - name: tcp
      port: 5432
      targetPort: 5432
```

If Kubernetes assigns NodePort 31543, this is published at `external-IP:31543`, even though
its Service port is 5432. TargetPort resolution and Pod forwarding belong to Kubernetes.
zlb always sends to `node-IP:nodePort`.

The optional annotations are:

- `zlb.zarrino.tech/pool`: select a configured pool, subject to its allowlist.
- `zlb.zarrino.tech/ip`: request one IP in that pool, subject to whole-IP reservations.

An unset class or `spec.loadBalancerClass: zlb.zarrino.tech/gateway` is handled by zlb.
A different explicit class is ignored. `spec.loadBalancerIP` is rejected; use the annotation.
Keep NodePort allocation enabled. All ports must use TCP, UDP or SCTP and have allocated NodePorts.
The same number can be used by different protocols. Reservations and blocked-port rules
apply to numeric ports across all protocols.

`externalTrafficPolicy: Cluster` uses eligible Ready nodes, allowing the Kubernetes datapath
to route to remote endpoints. `Local` uses only nodes with ready, non-terminating IPv4
EndpointSlice endpoints. Ready nodes marked for deletion, outside the configured node CIDR,
or labeled `node.kubernetes.io/exclude-from-external-load-balancers` are excluded.

`loadBalancerSourceRanges` is enforced on the external listener. Empty ranges allow all
sources at zlb's layer; application NetworkPolicies and other firewalls still apply.
Existing conntrack flows survive ACL tightening. Rejected Services produce warning Events.

## Controller options

Run `python3 -m zlb.controller --help` for the CLI.

- Required: `--lb-node`, `--token-file`, `--node-cidr`, and either `--pool-config` or repeated `--ip`.
- `--ip` creates a simple pool named `default`, without reservations. It is mutually
  exclusive with `--pool-config`.
- `--node-cidr` must describe node InternalIPs, not Pod or Service CIDRs.
- `--interval` defaults to 5 seconds with jitter and must be at least 1 second.
- `--ca-file` configures HTTPS trust; `--client-cert`/`--client-key` present a client
  certificate for mutual TLS; `--allow-private-http` explicitly permits HTTP.

## Load balancer node options

Run `python3 -m zlb.natd --help` for the CLI.

- Required: `--listen`, `--node-cidr`, `--public-interface`, and repeated `--ip`.
  Serving also requires `--token-file`; the API port defaults to 9808.
- `--ip` is an independent allowlist and interface-address configuration. A bare IPv4 address
  defaults to `/32`; append a prefix when required by your network. Each configured address
  is assigned on a successful reconciliation, even when no Service currently uses it.
- Existing addresses on the selected interface are adopted without ownership or prefix
  changes. An address found on another interface is rejected. Owned prefix/interface changes
  require cleanup with the original settings first.
- `--state-dir` defaults to `/var/lib/zlb`. Its per-table address journal must persist across
  process and machine restarts. Only recorded, exact owned addresses are removed; an
  administrator's changed prefix is preserved. Primary and preexisting addresses are never
  recorded as owned.
- `--reserved-port` may be repeated; the API port is also forbidden. Keep controller
  `blocked_ports` consistent with these restrictions.
- `--backend-port-range` (default `30000-32767`) limits backend ports to the cluster's
  NodePort range. The controller only targets NodePorts, so this stops a stolen API token
  from publishing other node services such as the kubelet, etcd or SSH. Set it to your
  `--service-node-port-range` when that differs; with SSH bootstrap use
  `bootstrap.backendPortRange`.
- `--tls-cert`/`--tls-key` serve HTTPS; `--tls-client-ca` requires client certificates
  from that CA. `--allow-from` (repeatable) limits API sources and `--max-connections`
  (default 16) caps concurrent connections. See
  [securing the daemon API](installation.md#securing-the-daemon-api).
- `--allow-foreign-forwarding` disables the default guard that drops new forwarded
  connections from the public interface that were not DNATed. `--isolate-public-ips` drops
  traffic to the public IPs other than listeners, replies and ping. See
  [host protection](installation.md#load-balancer-node-host-protection).
- `--table` defaults to `zlb`, with 1–32 lowercase letters/digits/underscores starting with
  a letter. The daemon refuses to overwrite a table without its schema ownership marker.
- `--cleanup` removes the owned table and journal-owned addresses without a token. Stop
  the daemon first; a per-table lock rejects concurrent local writers. Use the same public
  interface, state directory, table and network settings as the serving process.

The wire envelope is version 2. Each listener contains exactly `ip`, `port`, `protocol`,
`backends` and `source_ranges`. Load balancer node and controller must run compatible versions.
Multiple disjoint node CIDRs and multiple load balancer nodes are not implemented.
