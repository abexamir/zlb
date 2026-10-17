# Developing zlb

zlb is Python 3.11+ with only the standard library at runtime. Open pull requests against
`main`; contributions are licensed under Apache-2.0. Never commit credentials, kubeconfigs,
real infrastructure addresses or deployment state.

## Layout

| Path | Contents |
| --- | --- |
| `zlb/controller.py`, `cache.py`, `kube.py` | reconcile loop, watch caches, Kubernetes API |
| `zlb/compiler.py`, `pools.py` | pure Service/Node/EndpointSlice → listener compiler, pool policy |
| `zlb/model.py` | wire format, validation, digests |
| `zlb/natd.py`, `addresses.py`, `routes.py`, `http_api.py` | daemon: nftables, address journal, HTTP API |
| `zlb/bootstrap.py`, `quiesce.py` | SSH bootstrap and its upgrade quiesce hook |
| `charts/zlb/` | Helm chart |
| `scripts/` | `check_docs.py`, `check_chart.py`, `package_chart.py` |
| `tests/test_*.py` | unit tests |
| `tests/verify_*.py`, `tests/Dockerfile.nat` | privileged NAT and bootstrap integration suites |
| `docs/` | [operator guide](docs/guide.md) and [internals](docs/internals.md) |

Keep the compiler pure, validate everything before rendering rules, and never touch firewall
state zlb does not own. Changes to deletion, rollback, source ranges, port resolution or the
wire format need a regression test. Keep the daemon's allowlists independent of the
controller's pools, and keep zlb free of provider, CNI, ingress, registry or secret-manager
assumptions. The `zlb.zarrino.tech/*` identifiers are a stable API.

## Checks

```sh
python3 -m pip install -r requirements-dev.txt
ruff check zlb tests scripts
coverage run -m unittest discover -s tests && coverage report   # fails below 80 %
python3 scripts/check_docs.py      # links, anchors, examples, versions, rendered nft example
python3 scripts/check_chart.py     # needs helm; hook order and credential separation
helm lint charts/zlb -f examples/helm-values.yaml
```

The integration suites need a Linux container runtime and a kernel with SCTP (`sudo modprobe sctp`):

```sh
docker build -f tests/Dockerfile.nat -t zlb-nat-test:local .
docker run --rm --privileged --network none zlb-nat-test:local
```

`verify_nat.py` builds disposable namespaces, veths and a bridge and checks real packets:
optional main-table route ownership, drift repair and restoration; TCP/UDP/SCTP forwarding with client source preserved, ACLs, empty-backend guards, the
forward guard, isolation, hairpin SNAT, counters, drift repair, rollback, crash-safe cleanup
and untouched unrelated tables. `verify_bootstrap.py` runs the real bootstrap remote script
against a real daemon with a minimal `systemctl` stand-in: first install, token rotation,
upgrade, and rollback of rejected candidates without losing forwarding.

The end-to-end test installs the chart into a [kind](https://kind.sigs.k8s.io/) cluster and
drives real traffic through a daemon in a privileged container (needs docker, kind, kubectl
and helm):

```sh
docker build -t zlb:e2e . && docker build -f tests/Dockerfile.nat -t zlb-nat-test:e2e .
kind create cluster --name zlb --config tests/kind.yaml
kind load docker-image zlb:e2e --name zlb
python3 tests/e2e_kind.py zlb zlb:e2e zlb-nat-test:e2e
```

Never use a production VM or cluster in their place.

CI runs all of the above on every pull request: ruff, actionlint (with shellcheck) and
zizmor on the code and workflows; unit tests on Python 3.11 to 3.14; coverage; docs and
chart checks with kubeconform schema validation; a controller image build and smoke test;
the privileged NAT and bootstrap suites; the kind end-to-end test on two Kubernetes
versions; and a Gitleaks scan of the history. `ci-ok` is the single status to require.

## Docs and examples

[README.md](README.md) is the entry point, [docs/guide.md](docs/guide.md) the operator guide
and [docs/internals.md](docs/internals.md) the design reference. Keep them short, keep each
fact in one place and link to it, and match commands to the CLI's `--help`. Use
documentation-only addresses and example names.

## Releases

Every push to `main` runs CI, then publishes `ghcr.io/abexamir/zlb:main` and
`:sha-<commit>` (amd64 and arm64) and a development chart
`oci://ghcr.io/abexamir/charts/zlb` versioned `CHART_VERSION-main.RUN.sha.SHORT` that pins
the image digest. A tag `vX.Y.Z` publishes image `X.Y.Z` and `latest` and the stable chart
from `charts/zlb/Chart.yaml`, and creates a GitHub release. Images carry an SBOM and build
provenance; images and charts are signed keylessly with cosign by the Publish workflow:

```sh
cosign verify ghcr.io/abexamir/zlb@sha256:DIGEST \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github.com/abexamir/zlb/\.github/workflows/publish\.yml@'
```

Pull requests run checks only. Publishing uses `GITHUB_TOKEN` (`packages: write`) and an
OIDC token for signing; actions are pinned by commit and updated by Dependabot.

To release:

1. Set the same version in `pyproject.toml`, chart `appVersion`, and `image.tag` in
   `values.yaml` and `examples/helm-values.yaml`; bump the chart `version`; update the pinned
   commands in the docs and the chart README's upgrade notes (they ship in the chart).
2. Merge to `main` and wait for CI and the development publish.
3. `git tag vX.Y.Z && git push origin vX.Y.Z`. Never move a tag or reuse a chart version.
4. Check the Publish run, both architectures and an anonymous `helm pull`, then write
   the release notes.

New GHCR packages start private: make the image and `charts/zlb` packages public once.
To package a chart locally:

```sh
python3 scripts/package_chart.py --output dist --version 0.3.0-dev.1 \
  --image-repository ghcr.io/abexamir/zlb --image-tag sha-YOUR_COMMIT
```
