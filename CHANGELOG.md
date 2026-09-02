# Changelog

All notable changes to this project are documented here.

## Unreleased

- Add an atomic, idempotent migration from the legacy single-domain Unraid
  configuration into the versioned multi-VM plugin format while preserving
  pool-backed state and audit paths.
- Replace the loopback TCP API port with a mode-restricted Unix socket for the
  managed WebGUI bridge.

### Added

- Versioned multi-VM configuration with legacy single-domain migration.
- Layered policy core, injectable libvirt adapter with target read-back, and
  one-shot runtime with atomic state/audit persistence.
- Unraid lifecycle script, idempotent cron scheduling, Unix-socket status/config
  API, native API-backed settings page, health classification, and actionable
  notification boundary.
- Lifecycle-managed Unix-socket API daemon with explicit PID ownership and
  restrictive socket permissions.
- Hermetic tests for configuration, policy, adapters, locks, persistence,
  scheduling isolation, lifecycle helpers, health, and WebGUI behavior.
- Unraid-specific persistent installation and plugin migration guide.
