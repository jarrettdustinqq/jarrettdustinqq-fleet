# Control Hub Non-Root Runtime Contract

## Scope

This document defines the repository-supported Control Hub service identity,
authoritative state boundary, migration path, verified backup format, and
systemd user invocation.

It does not claim that the intended Chromebook/Crostini host currently uses this
contract. Host ownership, permissions, service state, and process identity must
be observed on that host before the architecture tracker can close.

## One Service/Operator Identity

Control Hub is operated by exactly one non-root Unix identity.

`fleetctl hub-runtime configure` records the current effective UID, GID, username,
and home directory in a private runtime config. Every runtime-management command
re-reads that config and refuses:

- effective UID 0;
- a different UID, GID, username, or home directory;
- a config, state directory, database, sidecar, backup, or manifest owned by
  another UID;
- symlinked runtime files/directories;
- group/world-readable private files or a group/world-accessible state directory.

The generated systemd unit is a user unit. It intentionally has no `User=` or
`Group=` override: the user manager launches it as the same identity bound in the
runtime config, and the Python launcher verifies that identity again.

## Authoritative Paths and Permissions

Default paths:

| Artifact | Default | Required mode |
|---|---|---|
| Runtime config | `${XDG_CONFIG_HOME:-$HOME/.config}/fleet-control-hub/runtime.json` | `0600` |
| State directory | `${FLEET_CONTROL_HUB_STATE_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/fleet-control-hub}` | `0700` |
| SQLite database | `<state-dir>/control_hub.db` | `0600` |
| Backup directory | `<state-dir>/backups` | `0700` |
| SQLite backups/manifests | `<state-dir>/backups/*` | `0600` |
| Chat inventory input | `<state-dir>/chat_work_brief.json` | private under the state boundary |
| Venture inventory input | `<state-dir>/venture_autonomy_report.json` | private under the state boundary |

The database at `<state-dir>/control_hub.db` is the one authoritative local
Control Hub database for that identity. It is an operational projection;
`jarrettdustinqq/continuity` remains canonical for cross-project identity and
durable workstream truth.

The service uses `UMask=0077`, a loopback address, and an unprivileged port. No
command in this contract changes ownership or escalates privileges.

## First-Time Configuration

Run as the intended non-root operator from the Fleet checkout:

```bash
id

./fleetctl hub-runtime configure \
  --projects-root "$HOME/projects" \
  --additional-projects-root "$HOME/control_station" \
  --repo-registry "$HOME/projects/continuity/repo-registry.json"

./fleetctl hub-runtime check
./fleetctl hub-runtime scan
```

`configure` requires at least one observable, reviewed root. It reuses the same
multi-root overlap and all-failed guards as normal Control Hub scans.

Use `--state-dir <absolute-path>` or `FLEET_CONTROL_HUB_STATE_DIR` only when a
different authoritative boundary is intentional. Use `--replace-config` only
after reviewing the complete replacement root/state contract; it never changes
the bound UID.

Window tracking is disabled for the service by default because a background user
manager may not have a desktop capture session. Add
`--enable-window-tracking` at configuration time only for a reviewed interactive
user-session deployment.

## User Service and Backup Timer

After configuration and a successful manual scan:

```bash
./fleetctl hub-runtime install-user-service
systemctl --user status fleet-control-hub.service --no-pager
systemctl --user status fleet-control-hub-backup.timer --no-pager
systemctl --user list-timers fleet-control-hub-backup.timer --no-pager
```

Installed units:

- `fleet-control-hub.service`: runs guarded `scan-serve` through the
  identity-bound runtime config;
- `fleet-control-hub-backup.service`: creates one online SQLite backup and
  manifest;
- `fleet-control-hub-backup.timer`: invokes the backup service daily with
  persistent catch-up and bounded randomized delay.

The timer never deletes backups. Retention or purge remains a separate reviewed
operator policy.

`--no-start` renders the units without calling `systemctl`; this is used by CI
and can be used for review:

```bash
./fleetctl hub-runtime install-user-service --no-start
```

A user service normally follows the user's login session. Boot-without-login
requires an explicit supervised host decision such as enabling user lingering;
the installer does not make that privileged policy change.

To remove the service while preserving all state, stop/disable the exact units,
remove only their three files from the systemd user-unit directory, and reload
the user manager. Do not delete the runtime config, database, or backups as part
of service removal.

## Verified Backups

Create and verify a backup:

```bash
backup_json="$(./fleetctl hub-runtime backup)"
manifest="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["manifest"])' <<<"$backup_json")"
./fleetctl hub-runtime verify-backup --manifest "$manifest"
```

The backup command uses SQLite's online backup API. Each backup has a bounded
JSON manifest containing:

- schema version and UTC creation time;
- bound user/UID/GID;
- authoritative source database path;
- backup filename and size;
- SHA-256 digest;
- successful SQLite integrity result.

Verification rechecks identity, ownership, modes, filename containment, size,
SHA-256, and SQLite integrity. Backups and manifests are local state and must not
be committed.

## Migration

### Adopt the existing default database

If the supported database already lives at
`$HOME/.local/share/fleet-control-hub/control_hub.db` and the default state path
is unchanged, configure the runtime around that path. Configuration accepts only
same-owner regular files and tightens their private modes. Then run:

```bash
./fleetctl hub-runtime check
./fleetctl hub-runtime backup
./fleetctl hub-runtime scan
```

### Move a database into a new authoritative state directory

1. Stop the user service.
2. Back up and verify the source database using its current supported tooling.
3. Ensure the source database is a same-owner regular file with mode `0600`.
4. Configure the new state directory while its authoritative DB path is absent.
5. Run:

```bash
./fleetctl hub-runtime migrate --from-db /absolute/path/to/legacy-control_hub.db
./fleetctl hub-runtime check
./fleetctl hub-runtime backup
./fleetctl hub-runtime scan
```

Migration uses SQLite's backup API and refuses to overwrite an existing
authoritative database. It does not delete or rename the source.

A root-owned or mixed-owner database is a host remediation blocker. This tool
will not `chown`, copy through root, or normalize split-brain state. Capture a
backup and ownership evidence, then perform any ownership correction only as a
separately supervised host action.

## Restore

1. Stop `fleet-control-hub.service`.
2. Verify the selected manifest.
3. Move the current database and any `-wal`/`-shm` sidecars together to an
   explicitly named quarantine/incident path.
4. Restore only when the authoritative database and sidecars are absent:

```bash
./fleetctl hub-runtime verify-backup --manifest /absolute/path/to/manifest.json
./fleetctl hub-runtime restore --manifest /absolute/path/to/manifest.json
./fleetctl hub-runtime check
./fleetctl hub-runtime scan
systemctl --user start fleet-control-hub.service
```

Restore re-verifies the manifest and backup, copies through SQLite into a private
temporary file, verifies integrity again, and atomically places the database.
It refuses to overwrite current state.

## Repository and Host Proof

CI runs an explicit non-root smoke test. It fails if the runner UID is 0, then
proves in an isolated boundary that the same non-root identity can:

- configure private state;
- scan a real Git repository and canonical registry;
- preserve operator-owned fields through backup and restore;
- validate permissions and SQLite integrity;
- render the exact user service/timer;
- start the configured dashboard on loopback and receive HTTP 200.

This is repository evidence for the runtime contract, not evidence about the
intended host. Host closure still requires current output for:

```bash
id
stat -c '%U:%G %a %n' \
  "$HOME/.config/fleet-control-hub/runtime.json" \
  "$HOME/.local/share/fleet-control-hub" \
  "$HOME/.local/share/fleet-control-hub/control_hub.db"
./fleetctl hub-runtime check
systemctl --user status fleet-control-hub.service --no-pager
systemctl --user status fleet-control-hub-backup.timer --no-pager
```

Also record a successful host backup/verify/restore rehearsal, guarded scan, and
loopback dashboard response before claiming normal host operation.
