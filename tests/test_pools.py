import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from zlb.controller import Controller
from zlb.model import PREFIX, PUBLISHED
from zlb.pools import PoolConfig
from test_zlb import CIDR, POOL, FakeKube, plan, service


def configuration():
    return {"default_pool": "public", "blocked_ports": [9808], "pools": [{
        "name": "public", "ips": POOL,
        "reservations": [{"ip": POOL[0], "ports": [80, 443], "service": "apps/ingress"}]}]}


class PoolTests(unittest.TestCase):
    def test_reserved_ports_stay_literal_everything_else_uses_nodeport(self):
        # "apps/ingress" owns 80+443 on POOL[0] -- its own listeners stay literal. Neither
        # other Service is reserved anything, so each publishes its own nodePort instead,
        # whatever its requested .port happened to be (user request 2026-10-15: "if it's not
        # a reserved service, just port forward its ports to a high port").
        ingress = service("ingress", 443, node_port=30443)
        database = service("database", 30432, node_port=31000)
        extra = service("extra", 32000, node_port=31001)
        result = plan([extra, database, ingress], pool=PoolConfig.parse(configuration()))
        self.assertEqual(result.assignments, {s: POOL[0] for s in ["ingress", "database", "extra"]})
        self.assertEqual(sorted(x["port"] for x in result.state["listeners"]), [443, 31000, 31001])

    def test_non_owner_falls_back_to_nodeport_instead_of_rejected(self):
        # "squatter" asks for port 443, which belongs to "apps/ingress" -- it is simply not
        # the owner, so it gets its own nodePort instead of being rejected outright.
        result = plan([service("squatter", 443, node_port=31002)], pool=PoolConfig.parse(configuration()))
        self.assertEqual(result.assignments, {"squatter": POOL[0]})
        self.assertEqual(result.state["listeners"][0]["port"], 31002)

    def test_reservation_is_scoped_to_exact_namespace_and_name(self):
        wrong_namespace = service("ingress", 443, node_port=31003)
        wrong_namespace["metadata"]["namespace"] = "other"
        result = plan([wrong_namespace], pool=PoolConfig.parse(configuration()))
        self.assertEqual(result.assignments, {"ingress": POOL[0]})
        self.assertEqual(result.state["listeners"][0]["port"], 31003)  # not the literal 443

    def test_low_and_management_ports_as_requests_never_rejected_unreserved(self):
        # Nothing about an unreserved Service's *requested* .port matters any more -- 22 and
        # 808 are never published, so there is nothing for a port policy to reject.
        for number in (22, 808):
            with self.subTest(port=number):
                result = plan([service("app", number, node_port=31004)], pool=PoolConfig.parse(configuration()))
                self.assertEqual(result.assignments, {"app": POOL[0]})
                self.assertEqual(result.state["listeners"][0]["port"], 31004)

    def test_blocked_port_rejected_even_via_nodeport(self):
        # The one thing that still has to be checked at runtime: the *resolved* public port
        # (here, an unreserved Service's own nodePort) must not land on the globally blocked
        # load balancer node management port. Contrived (default node-port ranges never produce 9808), but
        # the guard itself has to work.
        result = plan([service("app", 1234, node_port=9808)], pool=PoolConfig.parse(configuration()))
        self.assertIn("app", result.errors)
        # The simple CLI pool also protects the default load balancer node management port.
        self.assertIn("app", plan([service("app", 1234, node_port=9808)]).errors)

    def test_explicit_ip_still_uses_nodeport_when_unreserved(self):
        svc = service("squatter", 443, node_port=31006)
        svc["metadata"]["annotations"] = {PREFIX + "pool": "public", PREFIX + "ip": POOL[0]}
        result = plan([svc], pool=PoolConfig.parse(configuration()))
        self.assertEqual(result.assignments, {"squatter": POOL[0]})
        self.assertEqual(result.state["listeners"][0]["port"], 31006)

    def test_reservation_applies_per_port_not_atomically(self):
        # "ingress" owns 443. It also has an unreserved port (22) -- that one gets its own
        # nodePort, same as any unreserved Service's would; nothing about owning *one*
        # reservation on this IP makes every one of its ports literal.
        svc = service("ingress", 443)
        svc["spec"]["ports"].append({"port": 22, "nodePort": 30022})
        result = plan([svc], pool=PoolConfig.parse(configuration()))
        self.assertEqual(result.assignments, {"ingress": POOL[0]})
        self.assertEqual(sorted(x["port"] for x in result.state["listeners"]), [443, 30022])

    def test_named_pool_and_pool_service_allowlist(self):
        raw = configuration()
        raw["pools"].append({"name": "dedicated", "ips": ["198.51.100.11"], "services": ["apps/private"]})
        config = PoolConfig.parse(raw)
        for name, allowed in [("private", True), ("other", False)]:
            svc = service(name, 443)
            svc["metadata"]["annotations"] = {PREFIX + "pool": "dedicated"}
            result = plan([svc], pool=config)
            self.assertEqual(name in result.assignments, allowed)
            if allowed:
                self.assertEqual(result.assignments[name], "198.51.100.11")
        # No `pool` annotation at all uses default_pool, never the named "dedicated" one --
        # "other" is unreserved there too, so it succeeds via its own nodePort on POOL[0].
        self.assertEqual(plan([service("other", 443)], pool=config).assignments, {"other": POOL[0]})

    def test_unknown_pool_and_ip_in_other_pool_rejected(self):
        raw = configuration()
        raw["pools"].append({"name": "second", "ips": ["198.51.100.11"]})
        for annotations in [{PREFIX + "pool": "missing"}, {PREFIX + "ip": "198.51.100.11"}]:
            svc = service("app", 20000)
            svc["metadata"]["annotations"] = annotations
            self.assertIn("app", plan([svc], pool=PoolConfig.parse(raw)).errors)

    def test_entire_ip_reserved_but_other_ip_remains_shared(self):
        raw = configuration()
        raw["pools"][0]["ips"] = POOL + ["198.51.100.11"]
        raw["pools"][0]["reservations"].append({"ip": "198.51.100.11", "service": "apps/special"})
        config = PoolConfig.parse(raw)
        svc = service("other", 20000)
        svc["metadata"]["annotations"] = {PREFIX + "ip": "198.51.100.11"}
        self.assertIn("other", plan([svc], pool=config).errors)
        special = service("special", 443)
        self.assertEqual(plan([special], pool=config).assignments, {"special": "198.51.100.11"})
        self.assertEqual(plan([service("shared", 20000)], pool=config).assignments, {"shared": POOL[0]})

    def test_existing_ip_kept_only_while_policy_allows(self):
        raw = {"default_pool": "public", "pools": [{"name": "public", "ips": POOL + ["198.51.100.11"]}]}
        svc = service("app", 20000, previous="198.51.100.11")
        self.assertEqual(plan([svc], pool=PoolConfig.parse(raw)).assignments["app"], "198.51.100.11")
        raw["pools"][0]["reservations"] = [{"ip": "198.51.100.11", "service": "apps/special"}]
        self.assertEqual(plan([svc], pool=PoolConfig.parse(raw)).assignments["app"], POOL[0])

    def test_malformed_config_fails_before_any_lb_node_push(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            path.write_text('{"pools":')
            kube, lb_node = FakeKube([service()]), Mock()
            lb_node.info.return_value = None
            controller = Controller(kube, lb_node, None, CIDR, path)
            with self.assertRaises(ValueError):
                controller.reconcile()
            lb_node.apply.assert_not_called()
            self.assertEqual(kube.actions, [])

    def test_config_reloaded_and_reservation_enforced_next_tick(self):
        # Before the reload "app" is unreserved and publishes its own nodePort; the config is
        # re-read every tick, so once it becomes the named owner of 443 the very next
        # reconcile switches it to publishing that literal port instead.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pools.json"
            raw = {"default_pool": "public", "pools": [{"name": "public", "ips": POOL}]}
            path.write_text(json.dumps(raw))
            kube, lb_node = FakeKube([service("app", 443, node_port=31007)]), Mock()
            lb_node.info.return_value = None
            controller = Controller(kube, lb_node, None, CIDR, path)
            controller.reconcile()
            self.assertEqual(lb_node.apply.call_args.args[0]["listeners"][0]["port"], 31007)
            raw["pools"][0]["reservations"] = [{"ip": POOL[0], "ports": [443], "service": "apps/app"}]
            path.write_text(json.dumps(raw))
            controller.reconcile()
            self.assertEqual(lb_node.apply.call_args.args[0]["listeners"][0]["port"], 443)
            self.assertEqual(kube.actions[-1], ("annotate", {PUBLISHED: "443:443/TCP"}))

    def test_ips_are_offered_in_numeric_order(self):
        config = PoolConfig.parse({"default_pool": "public", "pools": [
            {"name": "public", "ips": ["198.51.100.10", "198.51.100.9", "198.51.100.100"]}]})
        self.assertEqual(config.ips, ["198.51.100.9", "198.51.100.10", "198.51.100.100"])
        self.assertEqual(plan([service()], pool=config).assignments["web"], "198.51.100.9")

    def test_invalid_configuration_rejected(self):
        mutations = [
            lambda r: r.update(default_pool="missing"),
            lambda r: r.update(unknown=True),
            lambda r: r["pools"].append(copy.deepcopy(r["pools"][0])),
            lambda r: r["pools"].append({"name": "second", "ips": POOL}),
            lambda r: r["pools"][0].update(shared_ports=[[1024, 65535]]),  # no longer a field at all
            lambda r: r["pools"][0]["reservations"][0].update(whole_ip=True),  # internal-only, not JSON
            lambda r: r["pools"][0]["reservations"][0].update(service="ingress"),
            lambda r: r["pools"][0]["reservations"][0].update(ip="198.51.100.11"),
            lambda r: r["pools"][0]["reservations"].append({"ip": POOL[0], "ports": [443], "service": "apps/other"}),
            lambda r: r["pools"][0]["reservations"][0].update(ports=[9808]),
            lambda r: r["pools"][0].update(services=["apps/other"]),
            lambda r: r["pools"][0].update(ips=[]),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                raw = configuration()
                mutate(raw)
                with self.assertRaises(ValueError):
                    PoolConfig.parse(raw)

    def test_portable_pool_example_is_valid(self):
        config = PoolConfig.from_file("examples/pools.json")
        self.assertEqual(config.ips, POOL)
        svc = service("ingress", 443)
        _, available = config.candidates(svc, "ingress/ingress")
        self.assertEqual(available, POOL)


if __name__ == "__main__":
    unittest.main()
