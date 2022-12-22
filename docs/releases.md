# Releases and OCI artifacts

## Public artifacts

- Repository: https://github.com/abexamir/zlb
- Controller: `ghcr.io/abexamir/zlb` (Linux amd64 and arm64).
- Chart: `oci://ghcr.io/abexamir/charts/zlb`. Public pulls need no registry login.

Every push to `main` runs the reusable CI workflow, then publishes images tagged `main`
and `sha-<full-commit>`. It also packages a development chart with a unique semantic version
`CHART_VERSION-main.RUN_NUMBER.sha.SHORT_COMMIT`. Its defaults pin the exact image digest.
The workflow summary records the published chart reference and image digest.

A version tag `vAPP_VERSION` publishes image `APP_VERSION`, `latest`, and the immutable
commit tag, plus a stable chart using `charts/zlb/Chart.yaml`'s version. Image and chart
versions are independent. A stable chart must receive a new version for each new release.

## Install or inspect

```sh
helm show values oci://ghcr.io/abexamir/charts/zlb --version 0.2.0
helm pull oci://ghcr.io/abexamir/charts/zlb --version 0.2.0
helm upgrade --install zlb oci://ghcr.io/abexamir/charts/zlb --version 0.2.0 \
  --namespace zlb-system --create-namespace \
  --kubeconfig /absolute/path/to/target-kubeconfig -f your-values.yaml --wait --timeout 15m
docker pull ghcr.io/abexamir/zlb:0.4.0
```

For development snapshots, choose the exact chart version shown in a successful Publish
workflow. Do not assume an OCI chart has a `latest` tag. The packaged chart overrides
source defaults with the build's image digest, so installing a development chart cannot
accidentally select an unrelated stable image.

## Maintainer release steps

1. Update `pyproject.toml` version, chart `appVersion`, and source image tag together.
2. Increment the independent chart `version`; update pinned installation commands/examples.
3. Commit the unit of work and push `main`. Wait for CI and development publication.
4. Create and push the matching application tag, e.g. `git tag v0.4.0` followed by
   `git push origin v0.4.0`. Do not move an existing release tag or reuse a stable chart version.
5. Confirm the Publish run succeeded, inspect both architectures, pull the chart anonymously,
   and test `helm template` with operator values. Publish release notes on GitHub.

The workflow uses `GITHUB_TOKEN` with `contents: read` and `packages: write`; no registry
password or cluster credentials are stored. All actions are pinned to commits and updated
through Dependabot. Pull requests run checks but do not publish.

GitHub initially creates GHCR packages privately. Maintainers must set both the controller
package and `charts/zlb` package to public in package settings after the first publication.
Repository visibility does not automatically make packages public. Verify this with an
empty Docker/Helm registry configuration before announcing the installation commands.

For local packaging without publication:

```sh
python3 scripts/package_chart.py --output dist --version 0.2.0-dev.1 \
  --image-repository ghcr.io/abexamir/zlb --image-tag sha-YOUR_COMMIT
```

CI compiles/tests the source, checks docs and Helm examples, scans Git history with Gitleaks,
and runs the privileged NAT suite inside a disposable network-isolated Linux container.
No production infrastructure is contacted.
