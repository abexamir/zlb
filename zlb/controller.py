"""Single-writer polling controller. Load balancer node apply always precedes status/cleanup."""
import argparse
import ipaddress
import json
import logging
from pathlib import Path
import random
import ssl
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .cache import Cache
from .http_api import read_token
from .compiler import active, compile_state
from .kube import Kubernetes
from .model import FINALIZER, PUBLISHED, canonical, digest
from .pools import PoolConfig

LOG = logging.getLogger(__name__)


class LoadBalancerNode:
    def __init__(self, url, token_file, ca_file=None, client_cert=None, client_key=None):
        self.base, self.token_file = url.rstrip("/"), token_file
        self.url = self.base + "/v1/state"
        self.context = ssl.create_default_context(cafile=ca_file)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        if client_cert:
            # Presented when the daemon runs with --tls-client-ca (mutual TLS).
            self.context.load_cert_chain(client_cert, client_key)

    def call(self, method, path, body=None, timeout=90):
        request = Request(self.base + path, data=body, method=method, headers={
            "Authorization": "Bearer " + read_token(self.token_file), "Content-Type": "application/json"})
        try:
            with urlopen(request, context=self.context, timeout=timeout) as response:
                return json.load(response)
        except HTTPError as error:
            # Surface the daemon's reason (e.g. which listener it refused), not just the code.
            try:
                reason = json.load(error).get("error")
            except Exception:
                reason = None
            raise RuntimeError(f"load balancer node answered HTTP {error.code} to {method} {path}: "
                               f"{reason or error.reason}") from None

    def info(self):
        """The daemon's acceptance constraints, or None for daemons that predate /v1/info."""
        try:
            return self.call("GET", "/v1/info", timeout=15)
        except RuntimeError as error:
            if "HTTP 404" in str(error):
                return None
            raise

    def apply(self, state):
        result = self.call("POST", "/v1/state", canonical(state))
        if result.get("digest") != digest(state):
            raise RuntimeError("load balancer node did not acknowledge the expected state digest")


def same_ingress(desired, current):
    """Whether the published ingress already matches. API servers before Kubernetes 1.30 (or
    with LoadBalancerIPMode disabled) drop ipMode, so a missing ipMode is not a difference;
    otherwise every tick would rewrite the status of every Service."""
    return (len(desired) == len(current)
            and all(d["ip"] == c.get("ip") and c.get("ipMode", d["ipMode"]) == d["ipMode"]
                    for d, c in zip(desired, current)))


class Controller:
    def __init__(self, kube, lb_node, pool, node_cidr, pool_config_path=None, cache=None):
        self.kube, self.lb_node = kube, lb_node
        self.pool, self.node_cidr = pool, node_cidr
        self.pool_config_path = pool_config_path
        self.cache = cache
        self.warnings = {}

    def snapshot(self):
        if self.cache:
            # Raises until every cache reflects a complete list and an unbroken watch.
            return self.cache.snapshot()
        return (self.kube.list_all("/api/v1/services"), self.kube.list_all("/api/v1/nodes"),
                self.kube.list_all("/apis/discovery.k8s.io/v1/endpointslices"))

    def written(self, result):
        if self.cache:
            # Write through so the next tick does not act on the pre-patch object.
            self.cache.observe_service(result)
        return result

    def patch(self, svc, field, value, status=False):
        return self.written(self.kube.patch(svc, field, value, status=status))

    def annotate(self, svc, key, value):
        return self.written(self.kube.annotate(svc, key, value))

    def constraints(self):
        """The daemon's acceptance rules, checked against this controller's configuration."""
        info = self.lb_node.info()
        if info is not None and not ipaddress.ip_network(self.node_cidr).subnet_of(ipaddress.ip_network(info["node_cidr"])):
            raise RuntimeError(f"controller --node-cidr {self.node_cidr} is not inside the load balancer "
                               f"node's --node-cidr {info['node_cidr']}; backends would be refused")
        return info

    def reconcile(self):
        # Any incomplete/failed API snapshot aborts the whole tick; never push an accidental empty state.
        pool = PoolConfig.from_file(self.pool_config_path) if self.pool_config_path else self.pool
        services, nodes, slices = self.snapshot()
        constraints = self.constraints()
        # One Service's API failure must not hold back the others: record it, skip that
        # Service where needed, finish the tick, then report the tick as failed.
        failures = []
        def attempt(svc, action, call):
            try:
                return call()
            except Exception as exc:
                identity = f"{svc['metadata']['namespace']}/{svc['metadata']['name']}"
                LOG.warning("service=%s %s failed: %s", identity, action, exc)
                failures.append(f"{identity}: {action}: {exc}")
                return None
        ready = []
        for svc in services:
            finalizers = svc["metadata"].get("finalizers", [])
            if active(svc) and FINALIZER not in finalizers:
                # Never publish a Service whose cleanup finalizer is not in place yet.
                svc = attempt(svc, "adding the cleanup finalizer",
                              lambda: self.patch(svc, "/metadata/finalizers", finalizers + [FINALIZER]))
                if svc is None:
                    continue
            ready.append(svc)
        plan = compile_state(ready, nodes, slices, pool, self.node_cidr, constraints)
        # Re-send every tick, including empty state, so load balancer node restarts recover without controller state.
        self.lb_node.apply(plan.state)
        for svc in ready:
            uid = svc["metadata"]["uid"]
            finalizers = svc["metadata"].get("finalizers", [])
            if FINALIZER not in finalizers:
                continue
            error = plan.errors.get(uid)
            if error:
                previous = self.warnings.get(uid)
                if previous is None or previous[0] != error or time.monotonic() - previous[1] > 300:
                    try:
                        self.kube.warning(svc, error)
                    except Exception as exc:  # Events are best effort, e.g. in a terminating namespace.
                        LOG.warning("could not record a warning Event: %s", exc)
                    self.warnings[uid] = (error, time.monotonic())
                LOG.warning("service=%s/%s error=%s", svc["metadata"]["namespace"], svc["metadata"]["name"], error)
            else:
                self.warnings.pop(uid, None)
            ingress = [{"ip": plan.assignments[uid], "ipMode": "Proxy"}] if uid in plan.assignments else []
            current = svc.get("status", {}).get("loadBalancer", {}).get("ingress", [])
            if not same_ingress(ingress, current):
                status = dict(svc.get("status", {}))
                status["loadBalancer"] = {"ingress": ingress}
                svc = attempt(svc, "publishing status", lambda: self.patch(svc, "/status", status, status=True))
                if svc is None:
                    continue
            published = plan.published.get(uid)
            meta = svc["metadata"]
            if (meta.get("annotations", {}).get(PUBLISHED) != published
                    and not (published is None and meta.get("deletionTimestamp"))):
                svc = attempt(svc, "recording published ports", lambda: self.annotate(svc, PUBLISHED, published))
                if svc is None:
                    continue
            if not active(svc):
                attempt(svc, "releasing the cleanup finalizer",
                        lambda: self.patch(svc, "/metadata/finalizers", [f for f in finalizers if f != FINALIZER]))
        live_uids = {s["metadata"]["uid"] for s in services}
        self.warnings = {uid: warning for uid, warning in self.warnings.items() if uid in live_uids}
        LOG.info("reconciled services=%d listeners=%d errors=%d", len(plan.assignments),
                 len(plan.state["listeners"]), len(plan.errors))
        if failures:
            raise RuntimeError(f"{len(failures)} Service update(s) failed; first: {failures[0]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lb-node", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--ca-file", help="CA bundle that signed the daemon's HTTPS certificate")
    parser.add_argument("--client-cert", help="Client certificate for a daemon that requires mTLS")
    parser.add_argument("--client-key", help="Private key for --client-cert")
    allocation = parser.add_mutually_exclusive_group(required=True)
    allocation.add_argument("--ip", action="append", help="Simple unrestricted pool (repeat for multiple IPs)")
    allocation.add_argument("--pool-config", help="JSON file with named public pools and reservations")
    parser.add_argument("--node-cidr", required=True)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--allow-private-http", action="store_true",
                        help="Allow plaintext bearer token on an explicitly trusted private network")
    parser.add_argument("--ready-file")
    parser.add_argument("--resync", type=float, default=600,
                        help="Seconds between full relists of the watched resources")
    args = parser.parse_args()
    if not args.lb_node.startswith("https://") and not (args.allow_private_http and args.lb_node.startswith("http://")):
        parser.error("use HTTPS or explicitly allow private-network HTTP")
    if bool(args.client_cert) != bool(args.client_key):
        parser.error("--client-cert and --client-key must be used together")
    if args.client_cert and not args.lb_node.startswith("https://"):
        parser.error("client certificates require an https:// load balancer node URL")
    if args.resync < 30:
        parser.error("resync must be at least 30 seconds")
    if args.interval < 1:
        parser.error("interval must be at least one second")
    logging.basicConfig(level=logging.INFO)
    kube = Kubernetes()
    cache = Cache(kube, args.resync).start()
    if not cache.wait(120):
        LOG.warning("watch caches are not synced yet; reconciliation waits for them")
    controller = Controller(kube, LoadBalancerNode(args.lb_node, args.token_file, args.ca_file, args.client_cert,
                                                   args.client_key), args.ip, args.node_cidr, args.pool_config, cache)
    ready = Path(args.ready_file) if args.ready_file else None
    while True:
        try:
            controller.reconcile()
            if ready:
                ready.write_text(str(time.time()))
        except Exception:
            if ready:
                ready.unlink(missing_ok=True)
            LOG.exception("reconcile failed; preserving last acknowledged load balancer node state")
        time.sleep(args.interval * random.uniform(0.9, 1.1))


if __name__ == "__main__":
    main()
