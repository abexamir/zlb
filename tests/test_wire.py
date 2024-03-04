import copy
import json
import unittest

from zlb.compiler import compile_state
from zlb.model import canonical, compact, digest, expand, validate_state
from zlb.natd import Applier, render

IP, CIDR = "198.51.100.10", "10.20.0.0/16"


def v3(sets, listeners):
    return {"version": 3, "backend_sets": sets, "listeners": listeners}


def listener(port, backend_set, backend_port=30000, protocol="TCP", ranges=()):
    return {"ip": IP, "port": port, "protocol": protocol, "source_ranges": list(ranges),
            "backend_port": backend_port, "backend_set": backend_set}


class WireTests(unittest.TestCase):
    def test_canonical_form_dedupes_reorders_and_drops_unused_sets(self):
        messy = v3([["10.20.0.2", "10.20.0.1", "10.20.0.2"], ["10.20.0.9"], ["10.20.0.1", "10.20.0.2"], []],
                   [listener(31000, 2), listener(30500, 0), listener(30600, 3, 30001)])
        tidy = validate_state(messy, [IP], CIDR)
        self.assertEqual(tidy["backend_sets"], [[], ["10.20.0.1", "10.20.0.2"]])
        self.assertEqual([(x["port"], x["backend_set"]) for x in tidy["listeners"]], [(30500, 1), (30600, 0), (31000, 1)])
        # Normalizing is idempotent, so controller and daemon agree on the digest.
        self.assertEqual(validate_state(tidy, [IP], CIDR), tidy)
        shuffled = v3([["10.20.0.1", "10.20.0.2"], []], [listener(31000, 0), listener(30600, 1, 30001), listener(30500, 0)])
        self.assertEqual(digest(validate_state(shuffled, [IP], CIDR)), digest(tidy))

    def test_v3_rejects_malformed_or_unsafe_input(self):
        for state in (v3([["10.20.0.1"]], [listener(80, 1)]),                       # dangling index
                      v3([["10.20.0.1"]], [listener(80, True)]),                    # bool is not an index
                      v3([["192.0.2.1"]], [listener(80, 0)]),                       # outside node CIDR
                      v3([["10.20.0.1"]], [dict(listener(80, 0), backends=[])]),    # mixed v2 field
                      {"version": 3, "listeners": []},                              # missing sets
                      v3([["10.20.0.1"]], [listener(80, 0), listener(80, 0)])):     # duplicate listener
            with self.subTest(state=state), self.assertRaises(ValueError):
                validate_state(state, [IP], CIDR)
        with self.assertRaisesRegex(ValueError, "NodePort range"):
            validate_state(v3([["10.20.0.1"]], [listener(80, 0, 10250)]), [IP], CIDR, backend_ports=(30000, 32767))

    def test_v2_and_v3_forward_identically(self):
        v2 = {"version": 2, "listeners": [{"ip": IP, "port": 80, "protocol": "UDP", "source_ranges": ["192.0.2.0/24"],
              "backends": [{"ip": "10.20.0.2", "port": 30080}, {"ip": "10.20.0.1", "port": 30080}]}]}
        new = v3([["10.20.0.1", "10.20.0.2"]], [listener(80, 0, 30080, "UDP", ["192.0.2.0/24"])])
        a, b = validate_state(v2, [IP], CIDR), validate_state(new, [IP], CIDR)
        self.assertEqual(expand(a), expand(b))
        strip = lambda text: text.split("\n", 2)[2]  # everything after the revision marker
        self.assertEqual(strip(render(a, CIDR)), strip(render(b, CIDR)))
        self.assertIn('comment "zlb:v2:', render(a, CIDR))
        self.assertIn('comment "zlb:v3:', render(b, CIDR))

    def test_existing_v2_table_is_still_owned(self):
        applier = Applier.__new__(Applier)
        applier.table = "zlb"
        def table(comment):
            data = {"nftables": [{"table": {"family": "ip", "name": "zlb", "comment": comment}}]}
            applier.command = lambda *args, input=None: json.dumps(
                {"nftables": [{"table": {"family": "ip", "name": "zlb"}}]} if "tables" in args else data)
            return applier.snapshot()
        for comment in ("zlb:v2:abc", "zlb:v3:abc"):
            self.assertEqual(table(comment)["comment"], comment)
        for comment in ("zlb:v9:abc", "someone else's table"):
            with self.subTest(comment=comment), self.assertRaises(RuntimeError):
                table(comment)

    def test_compiled_state_stays_small_at_scale(self):
        nodes = [{"metadata": {"name": f"n{i}"}, "status": {"conditions": [{"type": "Ready", "status": "True"}],
                  "addresses": [{"type": "InternalIP", "address": f"10.20.{i // 250}.{i % 250 + 1}"}]}} for i in range(500)]
        services = [{"metadata": {"name": f"s{i}", "namespace": "apps", "uid": f"s{i}"},
                     "spec": {"type": "LoadBalancer", "ports": [{"port": 80, "nodePort": 30000 + i}]}} for i in range(2000)]
        state = compile_state(services, nodes, [], [IP], CIDR).state
        size = len(canonical(state))
        legacy = len(canonical({"version": 2, "listeners": expand(state)}))
        self.assertEqual(len(state["backend_sets"]), 1)
        self.assertLess(size, 300 * 1024)                     # was far over the 2 MiB request cap
        self.assertGreater(legacy, 2 * 1024 * 1024)
        self.assertLess(size * 100, legacy)


if __name__ == "__main__":
    unittest.main()
