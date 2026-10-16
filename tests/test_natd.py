import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from zlb.addresses import Addresses
from zlb.model import NODE_PORTS, VERSION, digest, expand, port_range, validate_state
from zlb.natd import Applier, cleanup_reason, generation, host_guards, layout, listener_id, render, stable
from test_zlb import service, node, endpoint_slice, plan

IP = "198.51.100.10"
CIDR = "10.0.0.0/24"
STATE = {"version": 2, "listeners": [{"ip": IP, "port": 80, "protocol": "TCP",
         "source_ranges": [], "backends": [{"ip": "10.0.0.10", "port": 30080}]}]}


class ProtocolTests(unittest.TestCase):
    def test_generation_falls_back_without_netlink_support(self):
        with patch("zlb.natd.socket.AF_NETLINK", None, create=True), patch("zlb.natd.socket.socket") as constructor:
            self.assertIsNone(generation())
            constructor.assert_not_called()

    def test_protocols_share_same_public_port(self):
        state = copy.deepcopy(STATE)
        state["listeners"] = [dict(state["listeners"][0], protocol=p) for p in ("TCP", "UDP", "SCTP")]
        validated = validate_state(state, [IP], CIDR)
        self.assertEqual(len(validated["listeners"]), 3)
        text = render(validated, CIDR)
        for proto in ("tcp", "udp", "sctp"):
            # Listener identity, dispatch and filtering all include the protocol.
            self.assertIn(f"{IP} . {proto} . 80 : jump l_", text)
            self.assertIn(f"{IP} . {proto} . 80 : jump open", text)
            self.assertIn(f"meta l4proto {proto} dnat ip to numgen inc mod 1 map", text)
        self.assertEqual(len({listener_id(x) for x in validated["listeners"]}), 3)
        with self.assertRaises(ValueError):
            validate_state({"version": 2, "listeners": [state["listeners"][0]] * 2}, [IP], CIDR)

    def test_mixed_protocol_service_and_local_backend_selection(self):
        svc = service()
        svc["spec"]["externalTrafficPolicy"] = "Local"
        svc["spec"]["ports"] = [{"name": p.lower(), "port": 53, "nodePort": 30053, "protocol": p}
                                for p in ("TCP", "UDP", "SCTP")]
        slices = []
        for proto, nodename in [("TCP", "n1"), ("UDP", "n2")]:
            es = endpoint_slice([{"nodeName": nodename, "conditions": {"ready": True}}])
            es["ports"] = [{"name": proto.lower(), "protocol": proto, "port": 5353}]
            slices.append(es)
        result = plan([svc], [node(), node("n2", "10.20.0.11")], slices)
        self.assertEqual(result.errors, {})
        backends = {x["protocol"]: x["backends"] for x in expand(result.state)}
        self.assertEqual(backends["TCP"][0]["ip"], "10.20.0.10")
        self.assertEqual(backends["UDP"][0]["ip"], "10.20.0.11")
        self.assertEqual(backends["SCTP"], [])

    def test_validation_rejects_unknown_fields_and_protocol(self):
        for change in ({"protocol": "ICMP"}, {"port": "80; flush ruleset"}, {"unknown": True}):
            state = copy.deepcopy(STATE)
            state["listeners"][0].update(change)
            with self.assertRaises((ValueError, TypeError)):
                validate_state(state, [IP], CIDR)

    def test_backend_ports_limited_to_node_port_range(self):
        # A stolen token must not be able to publish the kubelet, etcd or SSH of a node.
        for number in (22, 2379, 10250, 29999, 32768):
            state = copy.deepcopy(STATE)
            state["listeners"][0]["backends"][0]["port"] = number
            with self.subTest(port=number), self.assertRaisesRegex(ValueError, "NodePort range"):
                validate_state(state, [IP], CIDR, backend_ports=NODE_PORTS)
        for number in NODE_PORTS:
            state = copy.deepcopy(STATE)
            state["listeners"][0]["backends"][0]["port"] = number
            validate_state(state, [IP], CIDR, backend_ports=NODE_PORTS)

    def test_port_range_parsing(self):
        self.assertEqual(port_range("30000-32767"), (30000, 32767))
        self.assertEqual(port_range("8080-8080"), (8080, 8080))
        for value in ("32767-30000", "0-10", "1-65536", "30000", "a-b", "1-2-3"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                port_range(value)

    def test_source_ranges_and_empty_backend_filter_guard(self):
        state = copy.deepcopy(STATE)
        state["listeners"][0].update(source_ranges=["192.0.2.0/24"])
        text = render(state, CIDR)
        lid = listener_id(state["listeners"][0])
        # The source range gates DNAT and the per-listener filter chain.
        self.assertIn(f'ip saddr {{ 192.0.2.0/24 }} counter name "c_{lid}"', text)
        self.assertIn(f"{IP} . tcp . 80 : jump a_{lid}", text)
        self.assertIn(f"ip daddr {CIDR} ct status dnat ip saddr {{ 192.0.2.0/24 }} counter accept", text)
        state["listeners"][0]["backends"] = []
        text = render(state, CIDR)
        self.assertIn(f"{IP} . tcp . 80 : drop", text)
        self.assertNotIn("dnat ip to", text)
        self.assertEqual(layout(state, [IP])[1], {"dispatch": 0, "guard": 1})

    def test_layout_is_constant_per_packet_and_one_chain_per_listener(self):
        state = {"version": 2, "listeners": [
            {"ip": IP, "port": 30000 + i, "protocol": "TCP", "source_ranges": ["192.0.2.0/24"] if i % 2 else [],
             "backends": [{"ip": f"10.0.0.{b}", "port": 30000 + i} for b in range(1, 51)]} for i in range(20)]}
        chains, maps, counters = layout(state, [IP], "wan0")
        self.assertEqual(len(chains), 4 + 20 + 10)  # hooks + open, l_* per listener, a_* per ACL
        self.assertEqual(chains["prerouting"], 1)
        self.assertEqual(chains["input"], 1)
        self.assertEqual(maps, {"dispatch": 20, "guard": 20})
        self.assertEqual(len(counters), 20)
        text = render(state, CIDR, allowed_ips=[IP], public_interface="wan0")
        self.assertEqual(text.count("chain "), len(chains))

    def test_forward_guard_and_optional_public_isolation(self):
        text = render(STATE, CIDR, public_interface="wan0")
        forward = text.split("chain forward")[1].split("chain input")[0]
        self.assertIn('iifname "wan0" ct state new ct status & dnat == 0 counter drop', forward)
        self.assertIn('iifname "wan0" ct state invalid,untracked counter drop', forward)
        # Replies of established DNAT flows are accepted before the guard sees them.
        self.assertLess(forward.index("ct state established,related"), forward.index('iifname "wan0"'))
        self.assertNotIn("iifname", render(STATE, CIDR))
        isolated = render(STATE, CIDR, allowed_ips=[IP, "198.51.100.11"], isolate=True).split("chain input")[1]
        self.assertIn(f"ip daddr {{ {IP}, 198.51.100.11 }} counter drop", isolated)
        self.assertLess(isolated.index("established,related accept"), isolated.index("counter drop;\n}"))
        self.assertEqual(list(map(len, host_guards("wan0", [IP], True))), [2, 3])
        self.assertEqual(list(map(len, host_guards(None, [IP]))), [0, 0])

    def test_optional_hairpin_masquerade_matches_only_zlb_flows(self):
        self.assertNotIn("postrouting", render(STATE, CIDR, allowed_ips=[IP]))
        self.assertNotIn("postrouting", layout(STATE, [IP])[0])
        text = render(STATE, CIDR, allowed_ips=["198.51.100.11", IP], hairpin=["10.0.0.0/24", "10.244.0.0/16"])
        postrouting = text.split("chain postrouting")[1]
        self.assertIn("type nat hook postrouting priority 100; policy accept;", postrouting)
        self.assertIn(f"ip saddr {{ 10.0.0.0/24, 10.244.0.0/16 }} ip daddr {CIDR} ct status dnat "
                      f"ct original ip daddr {{ {IP}, 198.51.100.11 }} counter masquerade", postrouting)
        self.assertEqual(layout(STATE, [IP], hairpin=["10.0.0.0/24"])[0]["postrouting"], 1)

    def test_ips_render_in_numeric_order(self):
        text = render(STATE, CIDR, allowed_ips=["198.51.100.10", "198.51.100.9"], isolate=True)
        self.assertIn("ip daddr { 198.51.100.9, 198.51.100.10 }", text)

    def test_only_owned_table_and_numeric_generators(self):
        text = render(STATE, CIDR)
        self.assertIn("table ip zlb", text)
        self.assertIn("numgen inc mod 1 map", text)
        self.assertNotIn("flush ruleset", text)
        for name in ("bad;", "x\nflush ruleset", "X", "a" * 33):
            with self.assertRaises(ValueError):
                render(STATE, CIDR, name)

    def test_normalization_ignores_only_kernel_metadata_and_counters(self):
        self.assertEqual(stable({"handle": 1, "counter": {"packets": 99, "bytes": 200}, "expr": "x"}),
                         {"counter": {}, "expr": "x"})


class FakeIP:
    def __init__(self):
        self.values = [("wan0", "192.0.2.1/24")]
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if "-j" in args:
            names = {name for name, _ in self.values} | {"wan0"}
            if "-4" in args:
                names = {name for name, _ in self.values}
            return json.dumps([{"ifname": name, "addr_info": [dict(family="inet", local=cidr.split('/')[0],
                prefixlen=int(cidr.split('/')[1])) for n, cidr in self.values if n == name]} for name in names])
        if args[1:3] == ("address", "add"):
            self.values.append((args[5], args[3]))
        elif args[1:3] == ("address", "del"):
            self.values.remove((args[5], args[3]))
        else:
            raise AssertionError(args)
        return ""


class AddressTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.journal = Path(self.directory.name) / "addresses.json"
        self.ip = FakeIP()

    def manager(self, addresses=None):
        return Addresses(self.ip, "wan0", addresses or [IP], self.journal)

    def test_assign_restart_and_cleanup_only_owned_addresses(self):
        a = self.manager()
        a.add()
        self.assertIn(("wan0", IP + "/32"), self.ip.values)
        self.assertEqual(self.manager().owned, {IP + "/32"})
        self.manager().cleanup()
        self.assertEqual(self.ip.values, [("wan0", "192.0.2.1/24")])

    def test_existing_administrator_address_is_never_removed(self):
        self.ip.values.append(("wan0", IP + "/29"))
        a = self.manager()
        a.add()
        a.cleanup()
        self.assertIn(("wan0", IP + "/29"), self.ip.values)
        self.assertEqual(a.owned, set())

    def test_initially_unaddressed_interface_can_claim_public_ip(self):
        self.ip.values = []
        a = self.manager()
        a.add()
        self.assertEqual(self.ip.values, [("wan0", IP + "/32")])
        a.cleanup()
        self.assertEqual(self.ip.values, [])

    def test_other_interface_conflict_rejected(self):
        self.ip.values.append(("other0", IP + "/32"))
        with self.assertRaises(ValueError):
            self.manager().add()

    def test_interface_change_rejected(self):
        self.manager().add()
        with self.assertRaises(ValueError):
            Addresses(self.ip, "other0", [IP], self.journal)

    def test_journal_write_failure_rolls_back_added_ip(self):
        a = self.manager()
        with patch.object(a, "save", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError): a.add()
        self.assertEqual(a.owned, set())
        self.assertNotIn(("wan0", IP + "/32"), self.ip.values)

    def test_allowlist_shrink_retires_only_owned_ip(self):
        self.manager().add()
        a = self.manager(["198.51.100.11/32"])
        a.add()
        a.retire()
        self.assertNotIn(("wan0", IP + "/32"), self.ip.values)
        self.assertIn(("wan0", "198.51.100.11/32"), self.ip.values)

    def test_restore_reinstates_removed_and_removes_added_owned_ips(self):
        a = self.manager()
        a.add()
        before = a.owned.copy()
        a.desired = {"198.51.100.11": "198.51.100.11/32"}
        a.add(); a.retire(); a.restore(before)
        self.assertEqual(a.owned, before)
        self.assertIn(("wan0", IP + "/32"), self.ip.values)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.a = Applier([IP], CIDR, "wan0", self.directory.name)
        self.a.addresses = Mock(owned=set())
        self.a.install = Mock()
        self.a.verify = Mock()
        self.actual = {"fingerprint": "new", "comment": "revision"}
        self.a.snapshot = Mock(side_effect=[None, self.actual])
        self.a.text = Mock(return_value="old table")
        self.a.counters = Mock(return_value={})

    def test_validate_check_addresses_commit_verify_and_ack(self):
        self.assertEqual(self.a.apply(STATE), digest(STATE))
        self.assertTrue(self.a.install.call_args_list[0].kwargs['check'])
        self.assertEqual(self.a.install.call_count, 2)
        self.a.addresses.add.assert_called_once()
        self.a.addresses.retire.assert_called_once()

    def test_daemon_enforces_node_port_range_by_default(self):
        state = copy.deepcopy(STATE)
        state["listeners"][0]["backends"][0]["port"] = 10250
        with self.assertRaisesRegex(ValueError, "NodePort range"):
            self.a.apply(state)
        self.a.install.assert_not_called()
        custom = Applier([IP], CIDR, "wan0", self.directory.name, backend_ports=(10000, 10999))
        custom.addresses, custom.install, custom.verify = Mock(owned=set()), Mock(), Mock()
        custom.counters = Mock(return_value={})
        custom.snapshot = Mock(side_effect=[None, self.actual])
        state["listeners"][0]["backends"][0]["port"] = 10250
        self.assertEqual(custom.apply(state), digest(state))

    def test_dry_run_failure_never_changes_addresses(self):
        self.a.install.side_effect = RuntimeError("check failure")
        with self.assertRaises(RuntimeError): self.a.apply(STATE)
        self.a.addresses.add.assert_not_called()

    def test_post_commit_failure_restores_previous_table_and_addresses(self):
        old = {"fingerprint": "old"}
        self.a.snapshot.side_effect = [old, self.actual, self.actual, old]
        self.a.verify.side_effect = RuntimeError("readback failure")
        with self.assertRaises(RuntimeError): self.a.apply(STATE)
        self.assertEqual(self.a.install.call_args.args[0], "old table")
        self.a.addresses.restore.assert_called_once_with(set())

    def test_address_rollback_failure_does_not_skip_firewall_rollback(self):
        old = {"fingerprint": "old"}
        self.a.snapshot.side_effect = [old, self.actual, self.actual, old]
        self.a.verify.side_effect = RuntimeError("readback failure")
        self.a.addresses.restore.side_effect = OSError("address failure")
        with self.assertLogs("zlb.natd", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "readback failure"):
                self.a.apply(STATE)
        self.assertEqual(self.a.install.call_args.args[0], "old table")

    def test_identical_state_skips_render_check_and_commit(self):
        self.a.current_digest, self.a.last_fingerprint = digest(STATE), "new"
        self.a.snapshot.side_effect = [self.actual]
        self.assertEqual(self.a.apply(STATE), digest(STATE))
        self.a.install.assert_not_called()
        self.a.text.assert_not_called()
        self.a.counters.assert_not_called()
        self.a.addresses.add.assert_called_once()

    def test_drift_is_reapplied_and_counters_carried_over(self):
        self.a.current_digest, self.a.last_fingerprint = digest(STATE), "new"
        drifted = {"fingerprint": "tampered", "comment": "revision"}
        self.a.snapshot.side_effect = [drifted, self.actual]
        lid = listener_id(STATE["listeners"][0])
        self.a.counters.return_value = {"c_" + lid: (42, 4200)}
        self.a.apply(STATE)
        self.assertEqual(self.a.install.call_count, 2)
        self.assertIn(f"counter c_{lid} {{ packets 42 bytes 4200 }}", self.a.install.call_args.args[0])

    def test_cleanup_requires_ownership_snapshot(self):
        self.a.snapshot.side_effect = RuntimeError("foreign table")
        self.a.command = Mock()
        with self.assertRaises(RuntimeError): self.a.cleanup()
        self.a.command.assert_not_called()
        self.a.addresses.cleanup.assert_not_called()


class CleanupDecisionTests(unittest.TestCase):
    def test_only_a_clean_stop_without_keep_marker_cleans_up(self):
        with tempfile.TemporaryDirectory() as runtime:
            self.assertIsNone(cleanup_reason("zlb", runtime, {}))                       # operator run by hand
            self.assertIsNone(cleanup_reason("zlb", runtime, {"SERVICE_RESULT": "success"}))
            for result in ("exit-code", "signal", "core-dump", "watchdog", "oom-kill"):
                self.assertIn("abnormally", cleanup_reason("zlb", runtime, {"SERVICE_RESULT": result}))
            Path(runtime, "zlb.keep").touch()
            self.assertIn("managed restart", cleanup_reason("zlb", runtime, {"SERVICE_RESULT": "success"}))
            self.assertIsNone(cleanup_reason("other", runtime, {"SERVICE_RESULT": "success"}))
