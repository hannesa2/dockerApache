# GitHub Copilot Instructions

## Project overview

This repository contains Docker Compose deployments, host-side automation, and
configuration for a self-hosted service environment. The main stacks are:

| Path | System |
|---|---|
| `apache/` | Apache + PHP 8.3 + MySQL 8.4; primary web/reverse-proxy stack |
| `nextcloud/` | Nextcloud + PostgreSQL 16 + Redis; includes optional split-DNS and DNS-over-TLS services |
| `gitlab/` | Version-pinned GitLab CE standby, daily backup receiver, and restore tooling |
| `owncloud/` | Isolated legacy ownCloud standby and synchronization tooling |
| `mxserver/` | MXServer standby preparation and synchronization tooling |
| `monitoring/` | Hourly host and application availability checks |
| `github-artifacts/` | Private GitHub artifacts mirror and synchronization tooling |

Other maintained integrations include Matomo/Piwik served by the Apache stack
and Home Assistant behind the host Apache reverse proxy. See `README.md` and
the system-specific documentation before changing their deployment or
operations.

The production host is Linux (`latitude`); the standby/runtime host is macOS
(`mac2016`) using OrbStack. The Apache and Nextcloud Compose stacks are
independent. Persistent application data is stored in host bind mounts, not
container-managed volumes. Some configurations build from sibling checkouts
such as `MXServer`; check the relevant Compose file and README before assuming
this repository is self-contained.

## Current system boundaries

- **Standbys are not all the same.** GitLab, ownCloud, MXServer, and the
  GitHub artifacts mirror have distinct sync and promotion procedures. Check
  their documentation before changing schedules, restore logic, or failover.
- GitLab is a daily backup-based standby, not live replication. Keep source
  and standby versions compatible for restores; do not overwrite existing
  standby data with a restore unless explicitly initializing or recovering it.
- The GitHub artifacts mirror sync is one-way and propagates deletions.
  Treat its external parameter/configuration file as secret runtime data.
- Matomo's mirror database is an independent copy after its initial import;
  do not assume ongoing database replication.
- ownCloud's legacy runtime is intentionally isolated from the main PHP 8.3
  Apache service. Preserve its compatibility boundary when modifying it.
- MXServer is built from a sibling checkout, runs with its documented legacy
  runtime, and uses a separate standby database/data sync. Preserve those
  boundaries and its mail-capture behavior.
- The hourly monitor runs on the production host; it cannot report outages
  while that host itself is unavailable.
- Home Assistant is an existing host service proxied by native Apache. Preserve
  WebSocket forwarding, forwarded-header handling, and trusted-proxy settings.
- The deployment uses OrbStack on macOS. Check the active Docker context before
  making or documenting host-side Docker operations.

## Configuration and persistence

- Read the relevant `docker-compose.yml`, `.env.example`, and system
  documentation before changing services, ports, bind mounts, or environment
  variables. Do not read, print, or commit ignored `.env` files or credentials.
- Keep secrets, private keys, certificates, application secrets, and private
  runtime configuration outside the repository. Example files must use
  placeholders. Never copy sensitive production data into tests or temporary
  repository files.
- Prefer bind mounts and existing data-directory variables; do not introduce
  named volumes as the primary persistence mechanism.
- Do not add production host paths or passwords as general defaults. Follow
  existing environment-variable patterns and `.env.example` conventions.
- Resolve Compose ports from the current configuration; do not rely on port
  values remembered from older documentation.
- For Nextcloud `occ`, run as `www-data`:

  ```bash
  docker compose exec --user www-data nextcloud php occ <command>
  ```

  Trusted-domain environment values apply during initial setup; after
  installation, use `occ` to change them.

## Backups, synchronization, and recovery

- Read the relevant script and system documentation before changing backup,
  restore, or sync behavior. `rsync --delete` and standby restores can remove or
  replace data; preserve existing safety checks and refusal conditions.
- Keep backup and sync jobs scoped to their intended source and destination.
  Do not combine separate restricted SSH keys or broaden forced commands.
- Treat GitLab's `gitlab-secrets.json`, private application configuration, and
  TLS private keys as equivalent to credentials. Keep restrictive file
  permissions and never log their contents.
- Certbot runs on the Linux source host and deploys selected certificates to
  the macOS host through restricted hooks. Do not change the active renewal
  mechanism, enable a duplicate timer, force certificate reissuance, or weaken
  the receiver's SSH restrictions without explicit operational justification.
- For private configuration, the documented approach is SOPS with age for
  encrypted versioning and Restic for encrypted backups to a separate target.
  Only ciphertext belongs in Git; keep recovery keys and backup passwords
  independent of the repository and backup destination.

## Development practices

- Keep changes small and scoped to the owning system directory. Update the
  corresponding README or system documentation when behavior or operations
  change.
- Shell scripts should follow their local conventions; inspect neighboring
  scripts before changing error handling, logging, locale, or privilege use.
- Preserve service health checks, dependency ordering, restart behavior,
  network isolation, and externally mounted data.
- Verify configuration and scripts with the narrowest relevant checks available.
  Do not start production services or perform a restore/sync as a validation
  shortcut.
- CI workflows live under `.github/workflows/`; inspect the current workflow
  files rather than assuming workflow names, triggers, or image-publishing
  behavior.

## Useful entry points

- `README.md` — current systems, architecture, and operations
- `apache/docker-compose.yml` and `apache/.env.example`
- `nextcloud/docker-compose.yml` and `nextcloud/.env.example`
- `gitlab/docker-compose.yml` and `gitlab/` backup/restore scripts
- `owncloud/`, `mxserver/`, `monitoring/`, and `github-artifacts/` system docs
- `apache/HOMEASSISTANT.md` — Home Assistant reverse-proxy setup
