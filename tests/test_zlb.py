import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from zlb.compiler import compile_state
from zlb.controller import Controller, LoadBalancerNode
from zlb.kube import Kubernetes
from zlb.model import CLASS, FINALIZER, PREFIX, digest, validate_state
from zlb.pools import PoolConfig

POOL = ["198.51.100.10"]
CIDR = "10.20.0.0/24"


def service(name="web", public_port=80, previous=None, node_port=30080):
    result = {"metadata": {"name": name, "namespace": "apps", "uid": name,
                           "resourceVersion": "1", "creationTimestamp": "2026-10-15T00:00:00Z"},
              "spec": {"type": "LoadBalancer", "loadBalancerClass": CLASS,
                       "ports": [{"port": public_port, "nodePort": node_port}]}}
    if previous:
        result["status"] = {"loadBalancer": {"ingress": [{"ip": previous}]}}
    return result


def node(name="n1", ip="10.20.0.10"):
    return {"metadata": {"name": name}, "status": {
        "conditions": [{"type": "Ready", "status": "True"}],
        "addresses": [{"type": "InternalIP", "address": ip}]}}


def endpoint_slice(endpoints):
    return {"ports": [{"port": 8080, "protocol": "TCP"}], "addressType": "IPv4", "metadata": {"namespace": "apps", "labels": {
        "kubernetes.io/service-name": "web"}}, "endpoints": endpoints}


def plan(services, nodes=None, slices=None, pool=None):
    return compile_state(services, [node()] if nodes is None else nodes, slices or [], pool or POOL, CIDR)


class CompilerTests(unittest.TestCase):
    def test_unreserved_services_never_conflict_on_requested_port(self):
        # user request 2026-10-15: "if it's not a reserved service, just port forward its
        # ports to a high port ... that high port can be the same as the nodePort" -- the
        # *requested* .port no longer has anything to collide over; each publishes its own
        # nodePort instead, which Kubernetes already guarantees is unique cluster-wide. Two
        # Services requesting the exact same literal port (80 here) is a complete non-event.
        a = service("a", 80, node_port=31010)
        c = service("c", 80, node_port=31011)
        result = plan([c, a])
        self.assertEqual(result.assignments, {"a": POOL[0], "c": POOL[0]})
        self.assertEqual(result.errors, {})
        self.assertEqual(sorted(x["port"] for x in result.state["listeners"]), [31010, 31011])
        self.assertEqual(result, plan([a, c]))

    def test_service_allocation_is_atomic_on_blocked_port(self):
        # Atomicity still exists, just has nothing left to do with port *conflicts* between
        # Services (impossible now) -- a single Service with one good port and one port whose
        # resolved number lands on the globally-blocked management port still fails as a whole.
        svc = service("first")
        svc["spec"]["ports"].append({"port": 22, "nodePort": 9808})
        result = plan([svc])
        self.assertNotIn("first", result.assignments)
        self.assertEqual(result.state["listeners"], [])

    def test_second_ip_and_explicit_request(self):
        pool = POOL + ["198.51.100.11"]
        a, b = service("a"), service("b")
        b["metadata"]["annotations"] = {PREFIX + "ip": pool[1]}
        self.assertEqual(plan([a, b], pool=pool).assignments, {"a": pool[0], "b": pool[1]})
        b["metadata"]["annotations"][PREFIX + "ip"] = "198.51.100.12"
        self.assertIn("b", plan([b], pool=pool).errors)

    def test_claims_unclassified_ignores_foreign_and_deleting(self):
        # No annotation or loadBalancerClass needed to get claimed (user request 2026-10-15):
        # "a" has no loadBalancerClass at all and still gets a public IP. A Service naming a
        # *different* class ("b") and one being deleted ("c") are still left alone.
        a, b, c = service("a"), service("b"), service("c")
        del a["spec"]["loadBalancerClass"]
        b["spec"]["loadBalancerClass"] = "other/controller"
        c["metadata"]["deletionTimestamp"] = "now"
        result = plan([a, b, c])
        self.assertEqual(result.assignments, {"a": POOL[0]})
        # "a" is unreserved (the simple pool has no reservations), so it publishes its
        # own nodePort (30080), not the literal 80 it requested -- see pools.py/compiler.py.
        self.assertEqual([x["port"] for x in result.state["listeners"]], [30080])

    def test_null_endpoint_slice_does_not_block_reconcile(self):
        empty = endpoint_slice([])
        empty["endpoints"] = None
        self.assertEqual(plan([service()], slices=[empty]).errors, {})
        svc = service()
        svc["spec"]["externalTrafficPolicy"] = "Local"
        self.assertEqual(plan([svc], slices=[empty]).state["listeners"][0]["backends"], [])

    def test_cluster_filters_nodes(self):
        good = node()
        unready, excluded, outside, deleting = [node(str(i), f"10.20.0.{i}") for i in range(20, 24)]
        unready["status"]["conditions"][0]["status"] = "False"
        excluded["metadata"]["labels"] = {"node.kubernetes.io/exclude-from-external-load-balancers": ""}
        outside["status"]["addresses"][0]["address"] = "10.0.0.1"
        deleting["metadata"]["deletionTimestamp"] = "now"
        backends = plan([service()], [unready, excluded, outside, good, deleting]).state["listeners"][0]["backends"]
        self.assertEqual(backends, [{"ip": "10.20.0.10", "port": 30080}])

    def test_local_only_ready_nonterminating_endpoint_nodes(self):
        svc = service()
        svc["spec"]["externalTrafficPolicy"] = "Local"
        slices = [endpoint_slice([
            {"nodeName": "n1", "conditions": {"ready": True}},
            {"nodeName": "n2", "conditions": {"ready": False}},
            {"nodeName": "n3", "conditions": {"ready": True, "terminating": True}}])]
        result = plan([svc], [node(), node("n2", "10.20.0.11"), node("n3", "10.20.0.12")], slices)
        self.assertEqual(result.state["listeners"][0]["backends"], [{"ip": "10.20.0.10", "port": 30080}])
        self.assertEqual(plan([svc]).state["listeners"][0]["backends"], [])

    def test_invalid_service_does_not_block_valid(self):
        mutations = [lambda s: s["spec"]["ports"][0].update(protocol="ICMP"),
                     lambda s: s["spec"]["ports"][0].pop("nodePort"),
                     lambda s: s["spec"].update(loadBalancerSourceRanges=["0/0\nfrontend injected"])]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                bad = service("bad", 443)
                mutate(bad)
                result = plan([bad, service("good")])
                self.assertIn("bad", result.errors)
                self.assertIn("good", result.assignments)



class FakeKube:
    def __init__(self, services):
        self.services = copy.deepcopy(services)
        self.actions = []
        self.fail_nodes = False

    def list_all(self, path):
        if path == "/api/v1/services":
            return copy.deepcopy(self.services)
        if path == "/api/v1/nodes":
            if self.fail_nodes:
                raise RuntimeError("node API unavailable")
            return [node()]
        return []

    def patch(self, svc, field, value, status=False):
        self.actions.append((field, value))
        result = copy.deepcopy(svc)
        if field == "/metadata/finalizers":
            result["metadata"]["finalizers"] = value
        elif field == "/status":
            result["status"] = value
        return result

    def warning(self, svc, message):
        self.actions.append(("warning", message))


class ControllerTests(unittest.TestCase):
    def controller(self, services, pool=None):
        kube, lb_node = FakeKube(services), Mock()
        return Controller(kube, lb_node, pool if pool is not None else POOL, CIDR), kube, lb_node

    def test_finalizer_before_push_status_after_ack(self):
        controller, kube, lb_node = self.controller([service()])
        def apply(state):
            self.assertEqual(kube.actions, [("/metadata/finalizers", [FINALIZER])])
        lb_node.apply.side_effect = apply
        controller.reconcile()
        self.assertEqual(kube.actions[-1][0], "/status")
        self.assertEqual(kube.actions[-1][1]["loadBalancer"]["ingress"],
                         [{"ip": POOL[0], "ipMode": "Proxy"}])

    def test_failed_push_never_publishes_or_cleans_up(self):
        svc = service(previous=POOL[0])
        svc["metadata"].update(finalizers=[FINALIZER], deletionTimestamp="now")
        controller, kube, lb_node = self.controller([svc])
        lb_node.apply.side_effect = RuntimeError("load balancer node unavailable")
        with self.assertRaises(RuntimeError):
            controller.reconcile()
        self.assertEqual(kube.actions, [])

    def test_deletion_removes_listener_then_status_then_only_our_finalizer(self):
        svc = service(previous=POOL[0])
        svc["metadata"].update(finalizers=["other/cleanup", FINALIZER], deletionTimestamp="now")
        controller, kube, lb_node = self.controller([svc])
        controller.reconcile()
        self.assertEqual(lb_node.apply.call_args.args[0]["listeners"], [])
        self.assertEqual(kube.actions[0], ("/status", {"loadBalancer": {"ingress": []}}))
        self.assertEqual(kube.actions[1], ("/metadata/finalizers", ["other/cleanup"]))

    def test_api_read_failure_does_not_push(self):
        controller, kube, lb_node = self.controller([service()])
        kube.fail_nodes = True
        with self.assertRaises(RuntimeError):
            controller.reconcile()
        lb_node.apply.assert_not_called()
        self.assertEqual(kube.actions, [])

    def test_conflict_event_and_stale_status_cleared(self):
        # A Service holding a valid assignment loses eligibility entirely (a whole-IP
        # reservation for a *different* identity now covers its only IP) -- not a conflict
        # between two Services any more (unreserved ports never collide, see compiler.py),
        # but the same Warning-Event-plus-stale-status-cleanup behavior still has to hold for
        # whatever single-Service error does occur.
        svc = service("a", previous=POOL[0])
        svc["metadata"]["finalizers"] = [FINALIZER]
        pool = PoolConfig.parse({"default_pool": "public", "pools": [
            {"name": "public", "ips": POOL, "reservations": [{"ip": POOL[0], "service": "apps/other"}]}]})
        controller, kube, lb_node = self.controller([svc], pool=pool)
        controller.reconcile()
        self.assertEqual(kube.actions[0][0], "warning")
        self.assertEqual(kube.actions[1], ("/status", {"loadBalancer": {"ingress": []}}))

    def test_resource_version_conflict_propagates_without_push(self):
        controller, kube, lb_node = self.controller([service()])
        kube.patch = Mock(side_effect=RuntimeError("conflict"))
        with self.assertRaises(RuntimeError):
            controller.reconcile()
        lb_node.apply.assert_not_called()


class TransportTests(unittest.TestCase):
    def test_kube_pagination(self):
        kube = Kubernetes.__new__(Kubernetes)
        kube.request = Mock(side_effect=[
            {"items": [{"name": "first"}], "metadata": {"continue": "token/with+symbols"}},
            {"items": [{"name": "last"}], "metadata": {}}])
        self.assertEqual(kube.list_all("/api/v1/services"), [{"name": "first"}, {"name": "last"}])
        self.assertIn("continue=token%2Fwith%2Bsymbols", kube.request.call_args.args[1])

    def test_status_patch_is_guarded_by_resource_version(self):
        kube = Kubernetes.__new__(Kubernetes)
        kube.request = Mock()
        kube.patch(service(), "/status", {"loadBalancer": {"ingress": []}}, status=True)
        args = kube.request.call_args.args
        self.assertEqual(args[1], "/api/v1/namespaces/apps/services/web/status")
        self.assertEqual(args[2][0], {"op": "test", "path": "/metadata/resourceVersion", "value": "1"})
        self.assertEqual(args[3], "application/json-patch+json")

    def test_lb_node_rejects_wrong_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("x" * 32)
            lb_node = LoadBalancerNode("http://127.0.0.1:9808", token)
            response = Mock()
            response.read.return_value = b'{"digest": "wrong"}'
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            with patch("zlb.controller.urlopen", return_value=response):
                with self.assertRaisesRegex(RuntimeError, "expected state digest"):
                    lb_node.apply({"version": 2, "listeners": []})


if __name__ == "__main__":
    unittest.main()
