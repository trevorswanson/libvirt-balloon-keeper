# Release process

This document covers release publication and Community Applications metadata.
Development branches, pull-request requirements, and Git Flow are documented
in the root-level [CONTRIBUTING.md](../CONTRIBUTING.md).

## Release prerequisites

A release should be promoted only after the work has landed on `dev`, the
integration branch is stable, and the release pull request from `dev` to
`main` has passed the required checks.

Before tagging:

1. Confirm `main` contains the intended release commit.
2. Run the complete local validation gate from
   [CONTRIBUTING.md](../CONTRIBUTING.md).
3. Confirm the package builds deterministically and its checksum verifies.
4. Review the release contents for real host data, credentials, VM names, and
   development-only files.

## Date-version tag and GitHub release

Releases use an Unraid-style annotated date tag in `YYYY.MM.DD` format, for
example `2026.09.01`. Create it from the release commit on `main`, then push
the tag:

```bash
git fetch origin
git switch main
git pull --ff-only origin main
git tag -a 2026.09.01 -m "Release 2026.09.01"
git push origin 2026.09.01
```

The release workflow validates the tag, reruns the release validation gates,
builds the deterministic package, verifies its checksum, and publishes these
assets:

- `libvirt-balloon-keeper-2026.09.01.tar.gz`
- `libvirt-balloon-keeper-2026.09.01.tar.gz.sha256`
- `libvirt-balloon-keeper.tar.gz` (stable installer target)
- `libvirt-balloon-keeper.tar.gz.sha256`

After the workflow completes, verify the tag, release, and all four assets with
GitHub before installing anything. The stable `latest/download` URLs must point
to the newly published release.

## Local package check

Use the date version being prepared rather than the obsolete semantic-version
example:

```bash
VERSION=2026.09.01 bash unraid/build-package.sh /tmp/lbk-build-a
VERSION=2026.09.01 bash unraid/build-package.sh /tmp/lbk-build-b
cmp /tmp/lbk-build-a/libvirt-balloon-keeper-2026.09.01.tar.gz \
    /tmp/lbk-build-b/libvirt-balloon-keeper-2026.09.01.tar.gz
sha256sum -c /tmp/lbk-build-a/libvirt-balloon-keeper-2026.09.01.tar.gz.sha256
sha256sum -c /tmp/lbk-build-b/libvirt-balloon-keeper-2026.09.01.tar.gz.sha256
```

## Community Applications files

The repository-controlled submission metadata lives in these CA-standard
locations:

- `plugins/libvirt-balloon-keeper.xml` — plugin catalog entry and overview;
- `ca_profile.xml` — maintainer profile;
- `unraid/libvirt-balloon-keeper.plg` — checksum-verifying installer wrapper.

Submit the repository through the Community Applications submission portal
only after the public support destination and release assets are ready. CA
approval, catalog indexing, and local plugin installation are separate steps;
verify each one independently.

The plugin wrapper downloads the stable assets from the latest GitHub release
and delegates installation to the managed Unraid lifecycle. Installation starts
with the existing dry-run-safe configuration. Operators must explicitly review
local configuration before enabling live mode.

## Installing a release on Unraid

Use the canonical plugin manifest from the repository's `main` branch or the
Community Applications catalog. Before changing a live host:

1. Back up the Unraid boot device.
2. Confirm the release manifest reports the intended date version.
3. Keep `dry_run = true` and run the lifecycle checks.
4. Verify the API remains loopback-only and the managed cron block remains
   unique.
5. Preserve configuration, state, and audit logs across installation or
   upgrade.

The detailed Unraid lifecycle, rollback, and operational procedures are in
[`docs/unraid.md`](unraid.md). Do not reboot a production host or enable live
memory changes as an implicit part of release installation.
