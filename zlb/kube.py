"""Small in-cluster Kubernetes REST client. No shelling out or static bearer tokens."""
import json
import os
from pathlib import Path
import ssl
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen


class Kubernetes:
    def __init__(self):
        directory = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        self.token_path = directory / "token"
        self.context = ssl.create_default_context(cafile=str(directory / "ca.crt"))
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        if ":" in host:
            host = f"[{host}]"
        self.url = f"https://{host}:{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}"

    def request(self, method, path, body=None, content_type="application/json"):
        headers = {"Authorization": "Bearer " + self.token_path.read_text().strip(),
                   "Content-Type": content_type}
        request = Request(self.url + path, data=None if body is None else json.dumps(body).encode(),
                          headers=headers, method=method)
        with urlopen(request, context=self.context, timeout=15) as response:
            return json.load(response)

    def list_all(self, path, **params):
        items, continuation = [], None
        while True:
            query = {"limit": 500, **params}
            if continuation:
                query["continue"] = continuation
            result = self.request("GET", path + "?" + urlencode(query))
            items.extend(result["items"])
            continuation = result.get("metadata", {}).get("continue")
            if not continuation:
                return items

    def patch(self, svc, field, value, status=False):
        meta = svc["metadata"]
        path = (f"/api/v1/namespaces/{quote(meta['namespace'], safe='')}/services/"
                f"{quote(meta['name'], safe='')}" + ("/status" if status else ""))
        operations = [{"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
                      {"op": "add", "path": field, "value": value}]
        return self.request("PATCH", path, operations, "application/json-patch+json")

    def warning(self, svc, message):
        from datetime import datetime, timezone
        meta = svc["metadata"]
        now = datetime.now(timezone.utc).isoformat()
        self.request("POST", f"/api/v1/namespaces/{meta['namespace']}/events", {
            "apiVersion": "v1", "kind": "Event",
            "metadata": {"generateName": "zlb-", "namespace": meta["namespace"]},
            "involvedObject": {"apiVersion": "v1", "kind": "Service", "name": meta["name"],
                               "namespace": meta["namespace"], "uid": meta["uid"]},
            "reason": "LoadBalancerSyncFailed", "message": message,
            "type": "Warning", "source": {"component": "zlb-controller"},
            "firstTimestamp": now, "lastTimestamp": now, "count": 1})
