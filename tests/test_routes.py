import copy
import json
from pathlib import Path
import tempfile
import unittest

from zlb.routes import Routes, route


class Kernel:
    def __init__(self):
        self.rows = [{"dst": "default", "gateway": "10.0.0.1", "dev": "mgmt0", "protocol": "static", "metric": 100}]
        self.calls = []
        self.fail = False

    def __call__(self, *args):
        self.calls.append(args)
        if args[1] == "-j":
            return json.dumps(self.rows)
        destination = "default" if args[4] == "0.0.0.0/0" else args[4]
        if self.fail and args[3] == "replace" and "wan0" in args:
            self.fail = False
            raise RuntimeError("injected route failure")
        metric = int(args[args.index("metric") + 1])
        self.rows = [r for r in self.rows if not (r["dst"] == destination and r.get("metric", 0) == metric)]
        if args[3] == "replace":
            row = {"dst": destination, "dev": args[args.index("dev") + 1], "metric": metric}
            for key, flag in (("gateway", "via"), ("protocol", "proto"), ("scope", "scope"), ("prefsrc", "src")):
                if flag in args:
                    row[key] = args[args.index(flag) + 1]
            if "onlink" in args:
                row["flags"] = ["onlink"]
            self.rows.append(row)
        return ""


PUBLIC = {"destination": "0.0.0.0/0", "gateway": "198.51.100.1", "interface": "wan0"}
PRIVATE = {"destination": "10.0.0.0/8", "gateway": "10.0.0.1", "interface": "mgmt0"}


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Path(self.tmp.name) / "routes.json"
        self.kernel = Kernel()

    def test_management_routes_first_default_last_and_restore_on_cleanup(self):
        original = copy.deepcopy(self.kernel.rows)
        routes = Routes(self.kernel, [PUBLIC, PRIVATE], self.journal)
        routes.reconcile()
        writes = [c for c in self.kernel.calls if c[1] == "-4"]
        self.assertEqual([c[4] for c in writes], ["10.0.0.0/8", "0.0.0.0/0"])
        self.assertEqual(next(r for r in self.kernel.rows if r["dst"] == "default")["metric"], 100)
        self.kernel.calls.clear()
        routes.reconcile()
        self.assertFalse(any(c[1] == "-4" for c in self.kernel.calls))
        Routes(self.kernel, [PUBLIC, PRIVATE], self.journal).cleanup()
        restored = self.kernel.rows[0]
        self.assertEqual(restored["gateway"], original[0]["gateway"])
        self.assertEqual(len(self.kernel.rows), 1)

    def test_failed_change_restores_original_routes_and_ownership(self):
        self.kernel.fail = True
        routes = Routes(self.kernel, [PUBLIC, PRIVATE], self.journal)
        with self.assertRaisesRegex(RuntimeError, "injected"):
            routes.reconcile()
        self.assertEqual(len(self.kernel.rows), 1)
        self.assertEqual(self.kernel.rows[0]["gateway"], "10.0.0.1")
        self.assertEqual(routes.owned, {})

    def test_missing_owned_route_keeps_metric_and_cleanup_restores_original(self):
        routes = Routes(self.kernel, [PUBLIC], self.journal)
        routes.reconcile()
        self.kernel.rows = []
        Routes(self.kernel, [PUBLIC], self.journal).reconcile()
        self.assertEqual(self.kernel.rows[0]['metric'], 100)
        Routes(self.kernel, [PUBLIC], self.journal).cleanup()
        self.assertEqual(len(self.kernel.rows), 1)
        self.assertEqual(self.kernel.rows[0]['gateway'], '10.0.0.1')

    def test_preexisting_routes_are_never_removed(self):
        wanted = {"destination": "0.0.0.0/0", "gateway": "10.0.0.1", "interface": "mgmt0"}
        routes = Routes(self.kernel, [wanted], self.journal)
        routes.reconcile()
        self.assertEqual(routes.owned, {})
        routes.cleanup()
        self.assertEqual(len(self.kernel.rows), 1)

    def test_removed_configuration_restores_old_routes(self):
        Routes(self.kernel, [PUBLIC, PRIVATE], self.journal).reconcile()
        Routes(self.kernel, [], self.journal).reconcile()
        self.assertEqual(len(self.kernel.rows), 1)
        self.assertEqual(self.kernel.rows[0]["gateway"], "10.0.0.1")

    def test_drift_is_repaired_and_unrelated_routes_survive(self):
        self.kernel.rows.append({"dst": "203.0.113.0/24", "dev": "mgmt0", "protocol": "static"})
        routes = Routes(self.kernel, [PUBLIC], self.journal)
        routes.reconcile()
        next(r for r in self.kernel.rows if r["dst"] == "default")["gateway"] = "198.51.100.254"
        routes.reconcile()
        self.assertEqual(next(r for r in self.kernel.rows if r["dst"] == "default")["gateway"], PUBLIC["gateway"])
        routes.cleanup()
        self.assertTrue(any(r["dst"] == "203.0.113.0/24" for r in self.kernel.rows))

    def test_ambiguous_or_connected_routes_are_refused(self):
        self.kernel.rows.append({"dst": "default", "dev": "wan0", "metric": 200})
        with self.assertRaisesRegex(ValueError, "one simple"):
            Routes(self.kernel, [PUBLIC], self.journal).reconcile()
        self.kernel.rows = [{"dst": "10.0.0.0/8", "dev": "mgmt0", "protocol": "kernel"}]
        with self.assertRaisesRegex(ValueError, "kernel"):
            Routes(self.kernel, [dict(PRIVATE, gateway="10.0.0.254")], self.journal).reconcile()

    def test_invalid_inputs_and_opt_out(self):
        for value in ({}, dict(PUBLIC, interface="eth0;reboot"), dict(PUBLIC, gateway="::1"), dict(PUBLIC, extra=True)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                route(value)
        with self.assertRaisesRegex(ValueError, "unique"):
            Routes(self.kernel, [PUBLIC, PUBLIC], self.journal)
        Routes(self.kernel, [], self.journal).reconcile()
        self.assertEqual(self.kernel.calls, [])
