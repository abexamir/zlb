# Operating zlb

Set `ZLB_KUBECONFIG`, `ZLB_CONTEXT` and `ZLB_LB_NODE_SSH` explicitly to your target cluster
and VM before using these administrative examples. Never rely on an unrelated default context.

## Inspect the control plane and load balancer node

```sh
kubectl --kubeconfig "$ZLB_KUBECONFIG" --context "$ZLB_CONTEXT" get services -A
kubectl --kubeconfig "$ZLB_KUBECONFIG" --context "$ZLB_CONTEXT" -n zlb-system logs deployment/zlb-controller --tail=50
ssh "$ZLB_LB_NODE_SSH" 'systemctl status zlb-natd.service --no-pager; journalctl -u zlb-natd.service --no-pager -n 50'
```

Successful controller logs report assigned Services, listeners and errors. Load balancer node logs
include the applied revision and listener count. A receipt is evidence of installed rules,
not of application readiness or a successful external connection.

On the load balancer node, inspect owned rules and counters without modifying them:

```sh
sudo nft -a list table ip zlb
sudo nft list counters table ip zlb   # c_<id>: new connections per listener, kept across applies
sudo ip -4 address show dev eth0
sudo cat /var/lib/zlb/zlb-addresses.json
curl --noproxy '*' --fail http://10.0.0.1:9808/healthz
```

Replace the API address, table name and interface. `/healthz` reports process liveness; it does not
verify current rules, controller connectivity or workload health. Do not flush the load balancer node firewall or persist derived zlb rules through a firewall manager.

## Verify a published Service

Inspect its assigned IP, nodePorts, policy, and Events:

```sh
kubectl --kubeconfig "$ZLB_KUBECONFIG" --context "$ZLB_CONTEXT" -n apps get service database -o yaml
kubectl --kubeconfig "$ZLB_KUBECONFIG" --context "$ZLB_CONTEXT" -n apps describe service database
kubectl --kubeconfig "$ZLB_KUBECONFIG" --context "$ZLB_CONTEXT" -n apps get endpointslices -l kubernetes.io/service-name=database -o yaml
```

For an unreserved Service, use the NodePort as the external port. For a reserved port, use
the literal Service port. Test from outside the load balancer node using your actual assigned address:

```sh
curl --noproxy '*' --connect-timeout 3 --max-time 10 http://198.51.100.10:31543/
```

That HTTP command is illustrative; use a client matching the workload protocol. Check the
application's observed client address, then verify cleanup by deleting the test Service and
observing removal of its finalizer and new-connection DNAT rule. Established conntrack flows
may outlive listener removal; that is separate from the rule continuing to accept new flows.

## Changing pools or addresses

Arrange provider routing for new addresses first. Update the load balancer node's independent `--ip`
configuration and cleanup command, then restart it and update controller pools. zlb assigns
all configured addresses on its public interface. Existing administrator addresses retain
their prefix and are never removed by zlb.

To shrink a pool, remove the address from controller pools first and wait for an acknowledged
snapshot without its listeners, then remove the load balancer node `--ip` entry and restart. Only
journal-owned addresses are retired. Inspect the journal before changing prefixes, interface,
state directory or table; cleanup with the original settings is required for ownership changes.
A managed restart has a short reconciliation gap. Changing reservations can change published
ports; verify the resulting traffic from an external client.

The controller reloads valid mounted pool JSON each tick. An invalid file preserves the
last acknowledged load balancer node state. Use a ConfigMap directory mount rather than a subPath mount
when relying on propagation of file changes.

## Tokens and transport

Natd reads its token at startup. The controller reads its mounted token file for each push.
Coordinate load balancer node token replacement/restart with Kubernetes Secret updates, allowing for
Secret-volume propagation. There is no overlapping-token grace period or hot token reload
in natd. Prefer your existing secret-management process; do not print tokens in logs or
store them in manifests.

Serve the API over HTTPS with `--tls-cert`/`--tls-key`, optionally requiring client
certificates with `--tls-client-ca`, and restrict sources with `--allow-from`. See
[securing the daemon API](installation.md#securing-the-daemon-api). Applications remain responsible for
their own TLS and authentication; zlb is an L4 forwarding component.

## Stop and uninstall

The supplied unit's ExecStopPost removes only the owned table and owned interface addresses:

```sh
sudo systemctl stop zlb-natd.service
```

For a manually started daemon, stop its process and run cleanup with the same table, state directory, public interface,
node CIDR and public IP allowlist. For the example topology:

```sh
cd /opt/zlb
sudo python3 -m zlb.natd --cleanup --listen 10.0.0.1 --public-interface eth0 \
  --ip 198.51.100.10 --node-cidr 10.0.0.0/24
```

Stopping an arbitrary daemon process alone does not remove kernel rules. Cleanup does not
flush conntrack. Never delete the address journal before cleanup; it records which
addresses zlb is allowed to remove.

Before uninstalling the controller, delete or convert its Services while both controller and
load balancer node are still available, and wait for cleanup finalizers to disappear. Then remove the
controller resources and stop/disable natd. Do not delete the controller first and leave
unresolved finalizers. If the load balancer node is permanently unavailable, inspect and remove stale
external forwarding through an operator-controlled recovery before manually removing any
Service finalizers. Never remove unrelated finalizers.
