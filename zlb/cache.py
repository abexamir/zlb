"""Watch-backed caches of the few fields the controller needs.

Polling listed every Service, Node and EndpointSlice in the cluster on every tick: tens of
megabytes per tick in large clusters and full objects held in memory. Each Reflector lists
its resource once, then follows a watch, storing only trimmed objects. It relists after any
error, an expired resourceVersion, or `resync` seconds. Reconciliation only reads a cache
while it reflects a complete list plus an unbroken watch, so a partial view is never pushed.
"""
import logging
import threading
import time

from .model import PREFIX

LOG = logging.getLogger(__name__)
EXCLUDE = "node.kubernetes.io/exclude-from-external-load-balancers"
SERVICE_NAME = "kubernetes.io/service-name"


def pick(source, keys):
    return {key: source[key] for key in keys if key in source}


def service(obj):
    metadata = obj["metadata"]
    trimmed = {"metadata": pick(metadata, ("name", "namespace", "uid", "resourceVersion", "finalizers",
                                           "deletionTimestamp", "creationTimestamp")),
               "spec": pick(obj.get("spec", {}), ("type", "loadBalancerClass", "ports", "loadBalancerIP",
                                                  "loadBalancerSourceRanges", "externalTrafficPolicy")),
               # Status is written back whole, so keep all of it.
               "status": obj.get("status", {})}
    annotations = {k: v for k, v in metadata.get("annotations", {}).items() if k.startswith(PREFIX)}
    if annotations:
        trimmed["metadata"]["annotations"] = annotations
    return trimmed


def node(obj):
    metadata, status = obj["metadata"], obj.get("status", {})
    trimmed = {"metadata": pick(metadata, ("name", "uid", "resourceVersion", "deletionTimestamp")),
               "status": {"conditions": [c for c in status.get("conditions", []) if c.get("type") == "Ready"],
                          "addresses": [a for a in status.get("addresses", []) if a.get("type") == "InternalIP"]}}
    labels = pick(metadata.get("labels", {}), (EXCLUDE,))
    if labels:
        trimmed["metadata"]["labels"] = labels
    return trimmed


# Identical small read-only dicts (one per node name, per port definition, per label set)
# are shared between slices: per-object overhead, not field count, dominates memory.
SHARED = {}


def shared(value):
    if len(SHARED) > 100000:
        SHARED.clear()
    return SHARED.setdefault(tuple(sorted(value.items())), value)


def endpoint_slice(obj):
    """Only what the compiler reads, with its endpoint filter already applied: the nodes with
    an endpoint that is not explicitly unready or terminating, each listed once."""
    metadata = obj["metadata"]
    trimmed = {"metadata": dict(pick(metadata, ("namespace", "uid", "resourceVersion")),
                                labels=shared(pick(metadata.get("labels", {}), (SERVICE_NAME,)))),
               "addressType": obj.get("addressType"), "endpoints": [], "ports": []}
    if trimmed["addressType"] != "IPv4":
        return trimmed  # the compiler only uses IPv4 slices
    nodes = {e.get("nodeName") for e in obj.get("endpoints") or []
             if e.get("conditions", {}).get("ready") is not False and e.get("conditions", {}).get("terminating") is not True}
    trimmed["endpoints"] = [shared({"nodeName": name}) for name in sorted(n for n in nodes if n)]
    trimmed["ports"] = [shared(pick(p, ("name", "protocol", "port"))) for p in obj.get("ports") or []]
    return trimmed


class Expired(Exception):
    """The watch resourceVersion is too old; a relist is required."""


def newer(candidate, current):
    # resourceVersions are opaque, but an etcd-backed server uses increasing integers. Only
    # refuse an update when both are numeric and the stored one is strictly newer.
    try:
        return int(candidate["metadata"]["resourceVersion"]) >= int(current["metadata"]["resourceVersion"])
    except (KeyError, TypeError, ValueError):
        return True


class Reflector:
    def __init__(self, kube, path, transform, resync=600, watch_timeout=240):
        self.kube, self.path, self.transform = kube, path, transform
        self.resync, self.watch_timeout = resync, watch_timeout
        self.items, self.lock = {}, threading.Lock()
        self.synced = threading.Event()
        self.stopped = threading.Event()

    def start(self):
        threading.Thread(target=self.run, name="watch " + self.path, daemon=True).start()
        return self

    def stop(self):
        self.stopped.set()

    def run(self):
        delay = 1
        while not self.stopped.is_set():
            try:
                version = self.relist()
                delay = 1
                deadline = time.monotonic() + self.resync
                while time.monotonic() < deadline and not self.stopped.is_set():
                    version = self.follow(version, min(self.watch_timeout, max(1, deadline - time.monotonic())))
            except Expired:
                LOG.info("watch of %s expired; relisting", self.path)
            except Exception:
                self.synced.clear()
                LOG.warning("watch of %s failed; relisting in %ds", self.path, delay, exc_info=True)
                self.stopped.wait(delay)
                delay = min(delay * 2, 30)

    def relist(self):
        items, version = self.kube.list_versioned(self.path)
        with self.lock:
            self.items = {item["metadata"]["uid"]: self.transform(item) for item in items}
        self.synced.set()
        return version

    def follow(self, version, timeout):
        for event in self.kube.watch(self.path, version, timeout):
            kind, obj = event.get("type"), event.get("object", {})
            if kind == "ERROR":
                if obj.get("code") == 410:
                    raise Expired()
                raise RuntimeError(f"watch error: {obj.get('message')}")
            version = obj["metadata"]["resourceVersion"]
            if kind == "BOOKMARK":
                continue
            if kind == "DELETED":
                with self.lock:
                    self.items.pop(obj["metadata"]["uid"], None)
            elif kind in ("ADDED", "MODIFIED"):
                self.observe(obj)
            if self.stopped.is_set():
                break
        return version

    def observe(self, obj):
        """Store an object seen in a watch event or returned by our own write."""
        trimmed = self.transform(obj)
        with self.lock:
            current = self.items.get(trimmed["metadata"]["uid"])
            if current is None or newer(trimmed, current):
                self.items[trimmed["metadata"]["uid"]] = trimmed

    def snapshot(self):
        if not self.synced.is_set():
            raise RuntimeError(f"cache of {self.path} is not synced")
        with self.lock:
            return list(self.items.values())


class Cache:
    """The three resources the controller compiles from, each behind its own Reflector."""
    PATHS = {"services": ("/api/v1/services", service),
             "nodes": ("/api/v1/nodes", node),
             "endpointslices": ("/apis/discovery.k8s.io/v1/endpointslices", endpoint_slice)}

    def __init__(self, kube, resync=600):
        self.reflectors = {name: Reflector(kube, path, transform, resync)
                           for name, (path, transform) in self.PATHS.items()}

    def start(self):
        for reflector in self.reflectors.values():
            reflector.start()
        return self

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        return all(r.synced.wait(max(0, deadline - time.monotonic())) for r in self.reflectors.values())

    def snapshot(self):
        # Copy services: reconcile replaces entries with the objects its patches return.
        return tuple(self.reflectors[name].snapshot() for name in ("services", "nodes", "endpointslices"))

    def observe_service(self, obj):
        self.reflectors["services"].observe(obj)
