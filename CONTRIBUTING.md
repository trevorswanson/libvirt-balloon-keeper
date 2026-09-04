# Contributing to Libvirt Balloon Keeper

This project is maintained as a conservative, reviewable Unraid/libvirt
controller. Contributions should preserve the fail-closed safety model,
loopback-only WebGUI boundary, dry-run-first defaults, and recoverable plugin
lifecycle.

## Git Flow

The repository uses a two-stage Git Flow:

```text
feature branch -> dev -> main -> date-version release tag
```

- `main` is the protected release branch and contains released or release-ready
  history.
- `dev` is the protected integration branch for the next release.
- Work starts from the current `origin/dev` and uses a short-lived branch.
- Feature, bug-fix, documentation, test, and CI branches merge into `dev`
  through pull requests.
- When `dev` is release-ready, open a pull request from `dev` to `main`.
- After that release PR is merged, create the annotated `YYYY.MM.DD` tag from
  the resulting `main` commit. The release workflow validates the tag, reruns
  the release gates, builds the package, verifies checksums, and publishes the
  GitHub release assets.

### Branch names

Use a descriptive prefix and a short kebab-case name:

- `feat/<description>` — product functionality
- `fix/<description>` — bug fixes and hardening
- `docs/<description>` — documentation
- `test/<description>` — test-only changes
- `ci/<description>` — CI or repository automation
- `refactor/<description>` — behavior-preserving restructuring

Do not develop directly on `main` or `dev`. Do not reuse a long-lived branch
for unrelated work.

## Pull request requirements

Every change must enter `dev` or `main` through a pull request. Direct pushes,
force-pushes, branch deletion, and merge commits are disabled on the protected
branches.

A pull request should:

1. Explain the problem, the change, and any safety or operational impact.
2. Reference the relevant issue when one exists.
3. Target the correct branch: normal work targets `dev`; release promotion
   targets `main` from `dev`.
4. Be up to date with its target branch before merging.
5. Pass the required `test` and `package` GitHub Actions checks.
6. Resolve review conversations and incorporate requested changes.
7. Use a squash merge so the target branch records one coherent commit per
   change.

This is currently a single-maintainer repository, so branch protection requires
the pull-request gate and passing checks but does not require an approving
review. That is deliberate; it is not permission to skip review-quality
explanations or tests.

Keep PRs focused. Split unrelated fixes, features, and documentation changes
into separate PRs unless combining them is necessary for correctness.

## Development and safety requirements

Before opening a PR:

- Keep real VM names, host paths, addresses, credentials, telemetry, and
  deployed configuration out of Git.
- Preserve `dry_run = true` as the example and safe default.
- Never broaden the API beyond loopback or bypass the same-origin WebGUI bridge.
- Keep configuration validation server-side and writes atomic.
- Do not add browser-side shell execution, direct TOML writes, or `/update.php`.
- Preserve lifecycle backup, rollback, and configuration/state retention.
- Do not perform live VM memory changes, reboot an Unraid host, or alter a
  deployed installation as part of ordinary local testing.

For behavior changes, add or update focused tests. Use temporary files and
isolated fixtures; never overwrite real data to simulate a test case.

## Local validation

Run the same gates expected by CI:

```bash
python3 -m unittest discover -s tests -v
python3 -m coverage run --branch -m unittest discover -s tests
python3 -m coverage report --fail-under=90
python3 -m compileall -q balloon_keeper.py web_server.py libvirt_balloon_keeper tests
bash -n unraid/*.sh
git diff --check
```

For package changes, build twice and compare the artifacts and checksums:

```bash
VERSION=2026.09.01 bash unraid/build-package.sh /tmp/lbk-build-a
VERSION=2026.09.01 bash unraid/build-package.sh /tmp/lbk-build-b
cmp /tmp/lbk-build-a/libvirt-balloon-keeper.tar.gz \
    /tmp/lbk-build-b/libvirt-balloon-keeper.tar.gz
sha256sum -c /tmp/lbk-build-a/libvirt-balloon-keeper.tar.gz.sha256
sha256sum -c /tmp/lbk-build-b/libvirt-balloon-keeper.tar.gz.sha256
```

## Release process

Release tags use the Unraid-style `YYYY.MM.DD` format. The tag must point to
the release commit on `main`; do not tag `dev` or a feature branch.

The complete release and Community Applications process is documented in
[`docs/release.md`](docs/release.md). The implementation and operational
background for Unraid are in [`docs/unraid.md`](docs/unraid.md).

## Commit messages

Use Conventional Commit-style messages:

```text
type: concise imperative description
```

Examples include `feat:`, `fix:`, `docs:`, `test:`, `ci:`, `refactor:`, and
`chore:`. Keep commits understandable when viewed in the target branch history;
the eventual squash commit should describe the complete change.
