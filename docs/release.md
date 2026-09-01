# Release and contribution workflow

## Branches and pull requests

`main` is the protected release branch. All changes should use a short-lived
branch such as `fix/telemetry-warning`, `ci/package-release`, or
`docs/installation`, then enter `main` through a pull request. Direct pushes,
force-pushes, branch deletion, and merge commits are disabled on `main`.

A pull request must have a passing `test` and `package` check, be up to date
with `main`, and receive one approving review. Use squash merging so `main`
records one coherent commit per change. Dependabot/automation changes follow
the same checks and review rule.

## Releases

Push an annotated semantic-version tag such as `v0.1.1` after merging a
release-ready change. The release workflow reruns the test and syntax gates,
builds the deterministic package, verifies its checksum, and publishes these
assets to the GitHub release:

- `libvirt-balloon-keeper-0.1.1.tar.gz`
- `libvirt-balloon-keeper-0.1.1.tar.gz.sha256`
- `libvirt-balloon-keeper.tar.gz` (stable installer target)
- `libvirt-balloon-keeper.tar.gz.sha256`

The Community Applications plugin wrapper downloads the stable assets from the
latest GitHub release and then delegates installation to the repository's
managed Unraid lifecycle. It starts in the existing dry-run-safe configuration;
operators must explicitly review and change local configuration before live
mode.

## Community Applications submission files

The submission metadata lives in CA-standard locations:

- `plugins/libvirt-balloon-keeper.xml` — plugin catalog entry;
- `ca_profile.xml` — maintainer profile at repository root;
- `unraid/libvirt-balloon-keeper.plg` — checksum-verifying installer wrapper.

The XML entry is intended for submission to the Community Applications
repository after a public support thread or equivalent support destination has
been established. Metadata tests run in CI; installation behavior is exercised
by the existing isolated lifecycle harness and package-content checks.

## Local release gate

```bash
python3 -m unittest discover -s tests -v
python3 -m coverage run --branch -m unittest discover -s tests
python3 -m coverage report --fail-under=90
python3 -m compileall -q balloon_keeper.py web_server.py libvirt_balloon_keeper tests
bash -n unraid/*.sh
git diff --check
VERSION=0.1.0 bash unraid/build-package.sh /tmp/lbk-build-a
VERSION=0.1.0 bash unraid/build-package.sh /tmp/lbk-build-b
cmp /tmp/lbk-build-a/libvirt-balloon-keeper-0.1.0.tar.gz \
    /tmp/lbk-build-b/libvirt-balloon-keeper-0.1.0.tar.gz
```
