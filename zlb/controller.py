"""Single-writer polling controller. Load balancer node apply always precedes status/cleanup."""
import argparse
import json
import logging
from pathlib import Path
from urllib.parse import urlparse
import random
import ssl
import time
from urllib.request import Request, urlopen

from .http_api import read_token
from .compiler import active, compile_state
from .kube import Kubernetes
from .model import FINALIZER, NODE_PORTS, canonical, digest, port_range
from .pools import PoolConfig

LOG = logging.getLogger(__name__)


class LoadBalancerNode:
    def __init__(self, url, token_file, ca_file=None):
        self.url, self.token_file = url.rstrip("/") + "/v1/state", token_file
        self.context = ssl.create_default_context(cafile=ca_file)

    def apply(self, state):
        request = Request(self.url, data=canonical(state), method="POST", headers={
            "Authorization": "Bearer " + read_token(self.token_file), "Content-Type": "application/json"})
        with urlopen(request, context=self.context, timeout=90) as response:
            result = json.load(response)
        if result.get("digest") != digest(state):
            raise RuntimeError("load balancer node did not acknowledge the expected state digest")


class Controller:
    def __init__(self, kube, lb_node, pool, node_cidr, pool_config_path=None):
        self.kube, self.lb_node = kube, lb_node
        self.pool, self.node_cidr = pool, node_cidr
        self.pool_config_path = pool_config_path
        self.warnings = {}

    def reconcile(self):
        # Any incomplete/failed API snapshot aborts the whole tick; never push an accidental empty state.
        pool = PoolConfig.from_file(self.pool_config_path) if self.pool_config_path else self.pool
        services = self.kube.list_all("/api/v1/services")
        nodes = self.kube.list_all("/api/v1/nodes")
        slices = self.kube.list_all("/apis/discovery.k8s.io/v1/endpointslices")
        for index, svc in enumerate(services):
            finalizers = svc["metadata"].get("finalizers", [])
            if active(svc) and FINALIZER not in finalizers:
                services[index] = self.kube.patch(svc, "/metadata/finalizers", finalizers + [FINALIZER])
        plan = compile_state(services, nodes, slices, pool, self.node_cidr)
        # Re-send every tick, including empty state, so load balancer node restarts recover without controller state.
        self.lb_node.apply(plan.state)
        for svc in services:
            uid = svc["metadata"]["uid"]
            finalizers = svc["metadata"].get("finalizers", [])
            if FINALIZER not in finalizers:
                continue
            error = plan.errors.get(uid)
            if error:
                previous = self.warnings.get(uid)
                if previous is None or previous[0] != error or time.monotonic() - previous[1] > 300:
                    self.kube.warning(svc, error)
                    self.warnings[uid] = (error, time.monotonic())
                LOG.warning("service=%s/%s error=%s", svc["metadata"]["namespace"], svc["metadata"]["name"], error)
            else:
                self.warnings.pop(uid, None)
            ingress = [{"ip": plan.assignments[uid], "ipMode": "Proxy"}] if uid in plan.assignments else []
            current = svc.get("status", {}).get("loadBalancer", {}).get("ingress", [])
            if ingress != current:
                status = dict(svc.get("status", {}))
                status["loadBalancer"] = {"ingress": ingress}
                svc = self.kube.patch(svc, "/status", status, status=True)
            if not active(svc):
                self.kube.patch(svc, "/metadata/finalizers", [f for f in finalizers if f != FINALIZER])
        live_uids = {s["metadata"]["uid"] for s in services}
        self.warnings = {uid: warning for uid, warning in self.warnings.items() if uid in live_uids}
        LOG.info("reconciled services=%d listeners=%d errors=%d", len(plan.assignments),
                 len(plan.state["listeners"]), len(plan.errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lb-node", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--ca-file")
    allocation = parser.add_mutually_exclusive_group(required=True)
    allocation.add_argument("--ip", action="append", help="Simple unrestricted pool (repeat for multiple IPs)")
    allocation.add_argument("--pool-config", help="JSON file with named public pools and reservations")
    parser.add_argument("--node-cidr", required=True)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--allow-private-http", action="store_true",
                        help="Allow plaintext bearer token on an explicitly trusted private network")
    parser.add_argument("--bootstrap-key-file")
    parser.add_argument("--bootstrap-known-hosts-file")
    parser.add_argument("--bootstrap-ssh-host")
    parser.add_argument("--bootstrap-ssh-user", default="root")
    parser.add_argument("--bootstrap-ssh-port", type=int, default=22)
    parser.add_argument("--bootstrap-interface")
    parser.add_argument("--bootstrap-ip", action="append")
    parser.add_argument("--bootstrap-backend-port-range", type=port_range, default=NODE_PORTS,
                        help="NodePort range the bootstrapped daemon accepts as backends")
    parser.add_argument("--ready-file")
    args = parser.parse_args()
    if not args.lb_node.startswith("https://") and not (args.allow_private_http and args.lb_node.startswith("http://")):
        parser.error("use HTTPS or explicitly allow private-network HTTP")
    if args.interval < 1:
        parser.error("interval must be at least one second")
    logging.basicConfig(level=logging.INFO)
    controller = Controller(Kubernetes(), LoadBalancerNode(args.lb_node, args.token_file, args.ca_file), args.ip, args.node_cidr, args.pool_config)
    bootstrap = None
    if args.bootstrap_key_file:
        if not (args.bootstrap_known_hosts_file and args.bootstrap_interface and args.bootstrap_ip):
            parser.error("SSH bootstrap requires known hosts, public interface and independent public IP allowlist")
        from .bootstrap import SSHBootstrap
        endpoint = urlparse(args.lb_node)
        if endpoint.scheme != "http":
            parser.error("SSH bootstrap configures the native HTTP daemon; use a trusted private HTTP endpoint")
        bootstrap = SSHBootstrap(args.bootstrap_ssh_host or endpoint.hostname, args.bootstrap_ssh_user,
            args.bootstrap_key_file, args.bootstrap_known_hosts_file, args.token_file,
            endpoint.hostname, endpoint.port or 9808, args.bootstrap_ip, args.bootstrap_interface,
            args.node_cidr, args.bootstrap_ssh_port, backend_ports=args.bootstrap_backend_port_range)
    ready = Path(args.ready_file) if args.ready_file else None
    while True:
        try:
            if bootstrap:
                bootstrap.ensure()
            controller.reconcile()
            if ready:
                ready.write_text(str(time.time()))
        except Exception:
            if ready:
                ready.unlink(missing_ok=True)
            if bootstrap:
                bootstrap.invalidate()
            LOG.exception("reconcile failed; preserving last acknowledged load balancer node state")
        time.sleep(args.interval * random.uniform(0.9, 1.1))


if __name__ == "__main__":
    main()
