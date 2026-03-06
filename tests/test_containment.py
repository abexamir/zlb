from http.client import HTTPConnection
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from zlb.compiler import compile_state
from zlb.controller import Controller, LoadBalancerNode
from zlb.http_api import APIServer, make_handler
from zlb.model import expand
from zlb.pools import PoolConfig

IP, CIDR = "198.51.100.10", "10.20.0.0/24"


def svc(name, port, node_port, created="2026-01-01T00:00:00Z", namespace="apps"):
    return {"metadata": {"uid": name, "name": name, "namespace": namespace, "creationTimestamp": created},
            "spec": {"type": "LoadBalancer", "ports": [{"port": port, "nodePort": node_port}]}}


NODES = [{"metadata": {"name": "n1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}],
                                                  "addresses": [{"type": "InternalIP", "address": "10.20.0.11"}]}}]


def ports(plan):
    return sorted(x["port"] for x in expand(plan.state))


class CollisionTests(unittest.TestCase):
    def test_reservation_owner_wins_and_only_the_loser_fails(self):
        pool = PoolConfig.parse({"default_pool": "p", "pools": [{"name": "p", "ips": [IP],
                                 "reservations": [{"ip": IP, "ports": [30443], "service": "apps/owner"}]}]})
        services = [svc("bystander", 80, 30443, created="2025-01-01T00:00:00Z"),  # older, but not the owner
                    svc("owner", 30443, 31000), svc("innocent", 5432, 31543)]
        plan = compile_state(services, NODES, [], pool, CIDR)
        self.assertEqual(sorted(plan.assignments), ["innocent", "owner"])
        self.assertIn(f"{IP}:30443/TCP is claimed by apps/owner", plan.errors["bystander"])
        self.assertEqual(ports(plan), [30443, 31543])

    def test_a_service_colliding_with_itself_fails_alone(self):
        # Its reserved literal 31000 equals its other port's NodePort 31000.
        pool = PoolConfig.parse({"default_pool": "p", "pools": [{"name": "p", "ips": [IP],
                                 "reservations": [{"ip": IP, "ports": [31000], "service": "apps/self"}]}]})
        broken = svc("self", 31000, 30001)
        broken["spec"]["ports"].append({"port": 80, "nodePort": 31000})
        plan = compile_state([broken, svc("innocent", 5432, 31543)], NODES, [], pool, CIDR)
        self.assertEqual(list(plan.assignments), ["innocent"])
        self.assertIn(f"two ports resolve to public listener {IP}:31000/TCP", plan.errors["self"])

    def test_precedence_is_decided_per_listener_not_per_service(self):
        # Older "a" owns a reservation on 31000, but its NodePort 32000 collides with the port
        # reserved to "b". Owning some reservation must not let "a" take b's.
        pool = PoolConfig.parse({"default_pool": "p", "pools": [{"name": "p", "ips": [IP], "reservations": [
            {"ip": IP, "ports": [31000], "service": "apps/a"}, {"ip": IP, "ports": [32000], "service": "apps/b"}]}]})
        a = svc("a", 31000, 30001, created="2026-01-01T00:00:00Z")
        a["spec"]["ports"].append({"port": 80, "nodePort": 32000})
        b = svc("b", 32000, 30002, created="2026-02-01T00:00:00Z")
        for order in ([a, b], [b, a]):
            plan = compile_state(order, NODES, [], pool, CIDR)
            self.assertEqual(list(plan.assignments), ["b"])
            self.assertIn(f"{IP}:32000/TCP is claimed by apps/b", plan.errors["a"])
            self.assertEqual(ports(plan), [32000])

    def test_without_reservations_the_older_service_wins(self):
        newer, older = svc("newer", 80, 30080, "2026-02-01T00:00:00Z"), svc("older", 81, 30080, "2026-01-01T00:00:00Z")
        for order in ([newer, older], [older, newer]):
            plan = compile_state(order, NODES, [], [IP], CIDR)
            self.assertEqual(list(plan.assignments), ["older"])
            self.assertIn("apps/older", plan.errors["newer"])


class ConstraintTests(unittest.TestCase):
    INFO = {"ips": [IP], "node_cidr": CIDR, "forbidden_ports": [9808, 31999], "backend_ports": [30000, 32000]}

    def test_daemon_constraints_fail_single_services(self):
        services = [svc("ok", 80, 30080), svc("reserved", 81, 31999), svc("range", 82, 32500)]
        plan = compile_state(services, NODES, [], [IP, "198.51.100.11"], CIDR, self.INFO)
        self.assertEqual(list(plan.assignments), ["ok"])
        self.assertIn("reserved on the load balancer node", plan.errors["reserved"])
        self.assertIn("outside the load balancer node's backend range 30000-32000", plan.errors["range"])

    def test_pool_ip_unknown_to_the_daemon_is_skipped_or_reported(self):
        pool = [IP, "198.51.100.11"]
        moved = svc("moved", 80, 30080)
        moved["status"] = {"loadBalancer": {"ingress": [{"ip": "198.51.100.11"}]}}
        plan = compile_state([moved], NODES, [], pool, CIDR, self.INFO)
        self.assertEqual(plan.assignments, {"moved": IP})          # only daemon-allowed IPs are chosen
        pinned = svc("pinned", 80, 30081)
        pinned["metadata"]["annotations"] = {"zlb.zarrino.tech/ip": "198.51.100.11"}
        self.assertIn("does not allow", compile_state([pinned], NODES, [], pool, CIDR, self.INFO).errors["pinned"])

    def test_node_cidr_mismatch_is_a_clear_global_error(self):
        lb_node = Mock()
        lb_node.info.return_value = dict(self.INFO, node_cidr="10.20.0.0/25")
        kube = Mock()
        kube.list_all.return_value = []
        with self.assertRaisesRegex(RuntimeError, "not inside the load balancer node's --node-cidr 10.20.0.0/25"):
            Controller(kube, lb_node, [IP], CIDR).reconcile()
        lb_node.apply.assert_not_called()


class DaemonAPITests(unittest.TestCase):
    def setUp(self):
        self.applier = Mock()
        self.applier.info.return_value = ConstraintTests.INFO
        self.applier.apply.side_effect = ValueError("listener outside load balancer node allowlist")
        self.server = APIServer(("127.0.0.1", 0), make_handler(self.applier, "a" * 32))
        worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), worker.join()))
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        (Path(self.directory.name) / "token").write_text("a" * 32)
        self.node = LoadBalancerNode("http://%s:%d" % self.server.server_address, Path(self.directory.name) / "token")

    def test_info_requires_the_token(self):
        connection = HTTPConnection(*self.server.server_address, timeout=3)
        connection.request("GET", "/v1/info")
        self.assertEqual(connection.getresponse().status, 401)
        self.assertEqual(self.node.info(), ConstraintTests.INFO)

    def test_rejection_reason_reaches_both_logs(self):
        with self.assertLogs("zlb.http_api", level="WARNING") as daemon:
            with self.assertRaisesRegex(RuntimeError, "HTTP 400 to POST /v1/state: listener outside load balancer node allowlist"):
                self.node.apply({"version": 3, "backend_sets": [], "listeners": []})
        self.assertIn("rejected desired state: listener outside", daemon.output[0])

    def test_older_daemon_without_info_is_tolerated(self):
        self.node.call = Mock(side_effect=RuntimeError("load balancer node answered HTTP 404 to GET /v1/info: not found"))
        self.assertIsNone(self.node.info())


if __name__ == "__main__":
    unittest.main()
