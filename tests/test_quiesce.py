import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from zlb.quiesce import legacy, main, quiesce


def deployment(args):
    return {"metadata": {"name": "zlb-controller"},
            "spec": {"selector": {"matchLabels": {"app": "zlb-controller"}},
                     "template": {"spec": {"containers": [{"name": "controller", "args": args}]}}}}


class FakeKube:
    """Pods disappear a few polls after their owner is scaled down or deleted."""

    def __init__(self, controller=None, cronjob=True, jobs=(), grace=2):
        self.controller, self.cronjob, self.jobs = controller, cronjob, list(jobs)
        self.calls, self.grace, self.stopped = [], grace, {}

    def request(self, method, path, body=None, content_type="application/json"):
        self.calls.append((method, path, body))
        if path.endswith("/cronjobs/zlb-controller-bootstrap"):
            if not self.cronjob:
                raise HTTPError(path, 404, "not found", {}, io.BytesIO())
            return {}
        if path.endswith("/deployments/zlb-controller"):
            if not self.controller:
                raise HTTPError(path, 404, "not found", {}, io.BytesIO())
            return self.controller
        if path.endswith("/deployments/zlb-controller/scale"):
            self.stopped["app=zlb-controller"] = self.grace
        elif method == "DELETE":
            self.stopped["job-name=" + path.rsplit("/", 1)[1]] = self.grace
        return {}

    def list_all(self, path, **params):
        self.calls.append(("LIST", path, params))
        if path.endswith("/jobs"):
            return self.jobs
        selector = params["labelSelector"]
        if self.stopped.get(selector, 1) <= 0:
            return [{"metadata": {"name": "done"}, "status": {"phase": "Succeeded"}}]
        self.stopped[selector] = self.stopped.get(selector, 1) - 1
        return [{"metadata": {"name": selector.split("=")[1] + "-pod"}, "status": {"phase": "Running"}}]


def owned_job(name, owner="zlb-controller-bootstrap"):
    return {"metadata": {"name": name, "ownerReferences": [{"kind": "CronJob", "name": owner}]}}


class QuiesceTests(unittest.TestCase):
    def run_quiesce(self, kube, timeout=5):
        quiesce(kube, "zlb-system", "zlb-controller", "zlb-controller-bootstrap", timeout=timeout, poll=0)
        return [(method, path.rsplit("/", 2)[-2:], body) for method, path, body in kube.calls if method != "LIST"]

    def test_legacy_controller_and_cronjob_writers_are_stopped_before_returning(self):
        kube = FakeKube(deployment(["--lb-node=http://10.0.0.3:9808", "--bootstrap-key-file=/k"]),
                        jobs=[owned_job("zlb-controller-bootstrap-1"), owned_job("unrelated", owner="other")])
        calls = self.run_quiesce(kube)
        self.assertIn(("PATCH", ["cronjobs", "zlb-controller-bootstrap"], {"spec": {"suspend": True}}), calls)
        self.assertIn(("DELETE", ["jobs", "zlb-controller-bootstrap-1"], {"propagationPolicy": "Background"}), calls)
        self.assertNotIn("unrelated", str(calls))
        self.assertIn(("PATCH", ["zlb-controller", "scale"], {"spec": {"replicas": 0}}), calls)
        # Returned only after both writers' pods were observed gone.
        self.assertEqual(kube.stopped, {"app=zlb-controller": 0, "job-name=zlb-controller-bootstrap-1": 0})

    def test_current_controller_keeps_running(self):
        kube = FakeKube(deployment(["--lb-node=http://10.0.0.3:9808", "--token-file=/t"]), cronjob=False)
        calls = self.run_quiesce(kube)
        self.assertNotIn("scale", str(calls))
        self.assertEqual(kube.stopped, {})

    def test_fresh_namespace_has_nothing_to_stop(self):
        self.assertEqual([c[0] for c in self.run_quiesce(FakeKube(cronjob=False))], ["PATCH", "GET"])

    def test_times_out_while_a_writer_is_still_running(self):
        kube = FakeKube(deployment(["--bootstrap-ip=198.51.100.10"]), grace=10**6)
        with self.assertRaisesRegex(RuntimeError, "still running: zlb-controller-pod"):
            quiesce(kube, "zlb-system", "zlb-controller", "zlb-controller-bootstrap", timeout=0.05, poll=0)

    def test_own_running_pod_never_blocks_quiesce(self):
        # Review finding: the hook pod once carried the controller selector label and waited
        # for itself until timeout. Even if a selector matches it, its own pod is skipped.
        class SelfMatching(FakeKube):
            def list_all(self, path, **params):
                pods = super().list_all(path, **params)
                if not path.endswith("/jobs"):
                    pods = pods + [{"metadata": {"name": "zlb-controller-quiesce-x"}, "status": {"phase": "Running"}}]
                return pods
        kube = SelfMatching(deployment(["--bootstrap-key-file=/k"]), cronjob=False)
        quiesce(kube, "zlb-system", "zlb-controller", "zlb-controller-bootstrap", timeout=5, poll=0,
                own_pod="zlb-controller-quiesce-x")
        with self.assertRaisesRegex(RuntimeError, "zlb-controller-quiesce-x"):
            quiesce(SelfMatching(deployment(["--bootstrap-key-file=/k"]), cronjob=False), "zlb-system",
                    "zlb-controller", "zlb-controller-bootstrap", timeout=0.05, poll=0)

    def test_orphaned_bootstrap_pod_is_awaited_without_its_job(self):
        # A retried hook: the Job is already deleted but its pod is still terminating.
        kube = FakeKube(cronjob=False)
        selector = "app.kubernetes.io/component=bootstrap,app.kubernetes.io/instance=zlb"
        kube.stopped[selector] = 3
        quiesce(kube, "zlb-system", "zlb-controller", "zlb-controller-bootstrap", "zlb", timeout=5, poll=0)
        self.assertEqual(kube.stopped[selector], 0)

    def test_legacy_detection(self):
        self.assertTrue(legacy(deployment(["--bootstrap-ssh-host=10.0.0.3"])))
        self.assertFalse(legacy(deployment(["--pool-config=/etc/zlb/pools.json"])))

    def test_command_reports_failure(self):
        with patch("zlb.quiesce.logging.basicConfig"), self.assertLogs("zlb.quiesce", level="ERROR"):
            self.assertEqual(main(["--namespace", "zlb-system", "--deployment", "d", "--cronjob", "c"]), 1)


if __name__ == "__main__":
    unittest.main()
