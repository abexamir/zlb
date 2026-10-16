import threading
import time
import unittest
from unittest.mock import Mock

from zlb.cache import Reflector, endpoint_slice, node, service
from zlb.controller import Controller, same_ingress
from zlb.model import FINALIZER


def obj(uid, version, **extra):
    return {"metadata": {"uid": uid, "name": uid, "namespace": "apps", "resourceVersion": str(version)}, **extra}


class FakeWatchKube:
    """Serves scripted list results and watch streams, one per call."""

    def __init__(self, lists, watches):
        self.lists, self.watches = list(lists), list(watches)
        self.watched_from = []

    def list_versioned(self, path):
        result = self.lists.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def watch(self, path, version, timeout):
        self.watched_from.append(version)
        events = self.watches.pop(0) if self.watches else []
        if isinstance(events, Exception):
            raise events
        yield from events


def ident(item):
    return item


class ReflectorTests(unittest.TestCase):
    def follow(self, kube, rounds):
        reflector = Reflector(kube, "/x", ident)
        version = reflector.relist()
        for _ in range(rounds):
            version = reflector.follow(version, 1)
        return reflector, version

    def test_events_update_cache_and_track_resource_version(self):
        kube = FakeWatchKube([([obj("a", 1), obj("b", 2)], "10")], [[
            {"type": "ADDED", "object": obj("c", 11)},
            {"type": "MODIFIED", "object": obj("a", 12, spec={"changed": True})},
            {"type": "DELETED", "object": obj("b", 13)},
            {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "20"}}}]])
        reflector, version = self.follow(kube, 1)
        self.assertEqual(version, "20")
        self.assertEqual(kube.watched_from, ["10"])
        items = {x["metadata"]["uid"]: x for x in reflector.snapshot()}
        self.assertEqual(sorted(items), ["a", "c"])
        self.assertTrue(items["a"]["spec"]["changed"])

    def test_stale_event_never_overwrites_a_newer_write_through(self):
        kube = FakeWatchKube([([obj("a", 1)], "1")], [[{"type": "MODIFIED", "object": obj("a", 5)}]])
        reflector = Reflector(kube, "/x", ident)
        reflector.relist()
        reflector.observe(obj("a", 7, spec={"ours": True}))   # the object our own patch returned
        reflector.follow("1", 1)
        self.assertTrue(reflector.snapshot()[0]["spec"]["ours"])

    def test_snapshot_refused_until_synced_and_after_failure(self):
        kube = FakeWatchKube([RuntimeError("API down"), ([obj("a", 1)], "1")], [RuntimeError("stream broke")])
        reflector = Reflector(kube, "/x", ident, resync=60)
        with self.assertRaisesRegex(RuntimeError, "not synced"):
            reflector.snapshot()
        thread = threading.Thread(target=reflector.run, daemon=True)
        with self.assertLogs("zlb.cache", level="WARNING"):
            thread.start()
            deadline = time.monotonic() + 5
            while not kube.watched_from and time.monotonic() < deadline:
                time.sleep(0.01)
            # The broken stream clears the synced flag before the next relist.
            while reflector.synced.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertFalse(reflector.synced.is_set())
        with self.assertRaisesRegex(RuntimeError, "not synced"):
            reflector.snapshot()
        reflector.stop()

    def test_expired_watch_relists(self):
        kube = FakeWatchKube([([obj("a", 1)], "1"), ([obj("b", 9)], "9")],
                             [[{"type": "ERROR", "object": {"code": 410, "message": "too old"}}], []])
        reflector = Reflector(kube, "/x", ident, resync=0.2)
        thread = threading.Thread(target=reflector.run, daemon=True)
        with self.assertLogs("zlb.cache", level="INFO"):
            thread.start()
            deadline = time.monotonic() + 5
            while kube.lists and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.05)
        reflector.stop()
        self.assertEqual([x["metadata"]["uid"] for x in reflector.snapshot()], ["b"])

    def test_transforms_keep_only_what_the_compiler_reads(self):
        full = {"metadata": {"uid": "u", "name": "web", "namespace": "apps", "resourceVersion": "3", "finalizers": [FINALIZER],
                             "annotations": {"zlb.zarrino.tech/ip": "198.51.100.10", "kubectl.kubernetes.io/last-applied-configuration": "x" * 9000},
                             "labels": {"team": "a"}, "managedFields": [{"manager": "kubectl"}]},
                "spec": {"type": "LoadBalancer", "ports": [{"port": 80, "nodePort": 30080}], "selector": {"a": "b"},
                         "externalTrafficPolicy": "Local", "clusterIP": "10.96.0.10"},
                "status": {"loadBalancer": {"ingress": [{"ip": "198.51.100.10"}]}, "conditions": []}}
        trimmed = service(full)
        self.assertEqual(trimmed["metadata"]["annotations"], {"zlb.zarrino.tech/ip": "198.51.100.10"})
        self.assertNotIn("managedFields", trimmed["metadata"])
        self.assertEqual(set(trimmed["spec"]), {"type", "ports", "externalTrafficPolicy"})
        self.assertEqual(trimmed["status"], full["status"])
        n = node({"metadata": {"uid": "n", "name": "n1", "labels": {"node.kubernetes.io/exclude-from-external-load-balancers": "",
                                                                     "zone": "a"}},
                  "status": {"conditions": [{"type": "Ready", "status": "True"}, {"type": "MemoryPressure", "status": "False"}],
                             "addresses": [{"type": "InternalIP", "address": "10.0.0.1"}, {"type": "Hostname", "address": "n1"}],
                             "images": [{"names": ["x"] * 50}]}})
        self.assertEqual(list(n["metadata"]["labels"]), ["node.kubernetes.io/exclude-from-external-load-balancers"])
        self.assertEqual([c["type"] for c in n["status"]["conditions"]], ["Ready"])
        self.assertNotIn("images", n["status"])
        s = endpoint_slice({"metadata": {"uid": "s", "name": "web-1", "namespace": "apps",
                                         "labels": {"kubernetes.io/service-name": "web", "other": "x"}},
                            "addressType": "IPv4", "endpoints": None, "ports": [{"name": "http", "port": 8080, "protocol": "TCP", "appProtocol": "h2"}]})
        self.assertEqual(s["metadata"]["labels"], {"kubernetes.io/service-name": "web"})
        self.assertEqual(s["endpoints"], [])
        self.assertEqual(s["ports"], [{"name": "http", "protocol": "TCP", "port": 8080}])


class EquivalenceTests(unittest.TestCase):
    def test_compiler_reaches_the_same_state_from_trimmed_objects(self):
        import copy
        from zlb.compiler import compile_state
        def es(i, endpoints):
            return {"metadata": {"uid": f"es{i}", "name": f"s{i}-x", "namespace": "apps",
                                 "labels": {"kubernetes.io/service-name": f"s{i}", "other": "x"}},
                    "addressType": "IPv4", "ports": [{"name": "http", "protocol": "TCP", "port": 8080}],
                    "endpoints": [{"nodeName": name, "conditions": dict(c, serving=True), "addresses": ["10.244.0.1"]}
                                  for name, c in endpoints]}
        slices = [es(0, [("n1", {"ready": True}), ("n2", {"ready": False})]),
                  es(1, [("n2", {"ready": True, "terminating": True}), ("n3", {})]),
                  es(2, [("n1", {"ready": True}), ("n1", {"ready": True})]),
                  dict(es(3, [("n1", {"ready": True})]), addressType="IPv6")]
        nodes = [{"metadata": {"uid": n, "name": n, "labels": {"zone": "a"}},
                  "status": {"conditions": [{"type": "Ready", "status": "True"}], "images": [],
                             "addresses": [{"type": "InternalIP", "address": f"10.0.0.{i}"}]}} for i, n in enumerate(("n1", "n2", "n3"), 1)]
        services = [{"metadata": {"uid": f"u{i}", "name": f"s{i}", "namespace": "apps", "resourceVersion": "1",
                                  "annotations": {"note": "x"}},
                     "spec": {"type": "LoadBalancer", "externalTrafficPolicy": "Local", "selector": {"a": "b"},
                              "ports": [{"name": "http", "port": 80, "nodePort": 30000 + i}]}} for i in range(4)]
        full = compile_state(services, nodes, slices, ["198.51.100.10"], "10.0.0.0/24")
        trimmed = compile_state([service(copy.deepcopy(x)) for x in services], [node(copy.deepcopy(x)) for x in nodes],
                                [endpoint_slice(copy.deepcopy(x)) for x in slices], ["198.51.100.10"], "10.0.0.0/24")
        self.assertEqual(full, trimmed)
        self.assertEqual(full.state["backend_sets"], [[], ["10.0.0.1"], ["10.0.0.3"]])


class StatusTests(unittest.TestCase):
    def test_missing_ip_mode_is_not_a_difference(self):
        desired = [{"ip": "198.51.100.10", "ipMode": "Proxy"}]
        self.assertTrue(same_ingress(desired, [{"ip": "198.51.100.10"}]))          # pre-1.30 API server
        self.assertTrue(same_ingress(desired, desired))
        self.assertFalse(same_ingress(desired, [{"ip": "198.51.100.10", "ipMode": "VIP"}]))
        self.assertFalse(same_ingress(desired, [{"ip": "198.51.100.11"}]))
        self.assertFalse(same_ingress([], [{"ip": "198.51.100.10"}]))
        self.assertTrue(same_ingress([], []))

    def test_patches_write_through_to_the_cache(self):
        cache, kube = Mock(), Mock()
        svc = {"metadata": {"uid": "a", "name": "a", "namespace": "apps", "resourceVersion": "1"},
               "spec": {"type": "LoadBalancer", "ports": [{"port": 80, "nodePort": 30080}]}}
        cache.snapshot.return_value = ([svc], [], [])
        kube.patch.side_effect = lambda svc, field, value, status=False: dict(svc, metadata=dict(svc["metadata"], finalizers=[FINALIZER]))
        lb_node = Mock()
        Controller(kube, lb_node, ["198.51.100.10"], "10.0.0.0/24", cache=cache).reconcile()
        kube.list_all.assert_not_called()
        self.assertTrue(cache.observe_service.called)


if __name__ == "__main__":
    unittest.main()
