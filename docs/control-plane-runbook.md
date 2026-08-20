# Controller Runbook

## Scope

This runbook defines how to bootstrap and operate the Chromebook-based controller
node using Crostini + Debian + Nix, with project execution coordinated through
GitHub and Linear.

## Architecture Snapshot

- Controller node: Chromebook Crostini container (local operator surface).
- Local toolkit: `fleet/` scripts + Nix dev shell for reproducible commands.
- Canonical cross-project state: `jarrettdustinqq/continuity`.
- Managed repository root: an explicit reviewed `PROJECTS_DIR` boundary.
- Execution tracking: Linear project/issue workflow.
- Optional knowledge layer: Notion page mirrored from this runbook.

`PROJECTS_DIR` may override the default `$HOME/projects`, but it must point to an
intentional managed-repository boundary. Do not select a broad workspace merely
because it contains repositories; an outer Git repository can mask or distort
nested inventory.

## Bootstrap Procedure

1. Update host packages.
2. Install Nix with `fleetctl install-nix` when the reproducible shell is needed.
3. Select and record the managed `PROJECTS_DIR`.
4. Run `fleetctl bootstrap` to create or update the configured repositories.
5. Run `fleetctl health` to verify readiness.

### Commands

From the Fleet checkout:

```bash
sudo apt-get update && sudo apt-get upgrade -y && sudo apt-get autoremove -y
./fleetctl install-nix
PROJECTS_DIR="$HOME/projects" ./fleetctl bootstrap
PROJECTS_DIR="$HOME/projects" ./fleetctl health
```

## Daily Operations

1. Read `jarrettdustinqq/continuity:repo-registry.json` and `state.json` before repository changes.
2. Enter the reproducible shell when needed: `fleetctl shell`.
3. Sync repos before coding: `fleetctl bootstrap`.
4. Run health before and after significant changes: `fleetctl health`.
5. Use guarded Control Hub commands with the same managed root:

```bash
./fleetctl hub-scan --projects-root "$HOME/projects"
./fleetctl hub-serve --projects-root "$HOME/projects"

# Optional second independent root; repeat the option for more.
./fleetctl hub-scan \
  --projects-root "$HOME/projects" \
  --additional-projects-root "$HOME/control_station"
```

Control Hub resolves the canonical registry at
`$HOME/projects/continuity/repo-registry.json` for this root. Use
`--repo-registry <path>` or `CONTINUITY_REPO_REGISTRY` only when continuity is
intentionally checked out elsewhere. Treat `repo_registry_status=unavailable`
or `invalid` as degraded canonical identity: local observations may refresh, but
the last valid canonical projection and operator fields are preserved.

If every configured root is missing, inaccessible, non-directory, or unsafe,
the scan is refused before the Control Hub database is opened. With mixed root
health, complete roots refresh only their own observations and failed roots are
preserved. Keep an unavailable root in the command to record a temporary
failure. Omitting a previously configured root explicitly marks that root and
its observations removed without deleting them; re-adding it is reversible.


### Identity-Bound Control Hub Service

Use one intended non-root operator identity. Do not install the Control Hub as a
system/root service.

```bash
id
./fleetctl hub-runtime configure \
  --projects-root "$HOME/projects" \
  --repo-registry "$HOME/projects/continuity/repo-registry.json"
./fleetctl hub-runtime check
./fleetctl hub-runtime scan
./fleetctl hub-runtime backup
./fleetctl hub-runtime install-user-service
systemctl --user status fleet-control-hub.service --no-pager
systemctl --user status fleet-control-hub-backup.timer --no-pager
```

The runtime config binds the current UID/GID/user to one private authoritative
state directory. The generated service is a systemd user unit with no `User=`
override. The backup timer never purges state. Follow
`docs/control-hub-runtime.md` for migration, manifest verification, restore, and
the host-evidence checklist.

## Weekly Reliability Review

1. Confirm patch baseline (`apt list --upgradable`).
2. Confirm repo sync status (`fleetctl bootstrap` result).
3. Confirm Shell CI and Python tests pass.
4. Reconcile `repos.txt` against the continuity repository registry.
5. Review open risks and create or update tracked issues.

## Incident Checklist

### Nix unavailable

Nix is required for `fleetctl shell`, not for every Fleet inventory operation.

1. Source profile script: `. "$HOME/.nix-profile/etc/profile.d/nix.sh"`.
2. Verify version: `nix --version`.
3. If still failing and the dev shell is required, rerun `fleetctl install-nix`.

### GitHub auth failure

Fleet health accepts either HTTPS authentication through GitHub CLI or a
complete SSH keypair.

For HTTPS:

1. Authenticate: `gh auth login --hostname github.com --git-protocol https --web`.
2. Verify: `gh auth status --hostname github.com`.
3. Confirm the protocol: `gh config get git_protocol --host github.com`.
4. Confirm noninteractive Git access to a required private repository before declaring the controller fully ready.

For SSH:

1. Print the public key: `cat ~/.ssh/id_ed25519.pub`.
2. Ensure the key exists in GitHub SSH settings.
3. Test: `ssh -T git@github.com`.

### Repo sync failure

1. Check network and DNS.
2. Verify the target directory's `origin` matches the expected registry URL.
3. Run `fleetctl bootstrap` again.
4. If one repo fails, run a manual fetch in that directory and inspect its state.

### Healthcheck failure

1. Run `fleetctl health` and note the failing capability.
2. Distinguish required controller capabilities from optional Nix/dev-shell capability.
3. Resolve the missing dependency or authentication path.
4. Re-run health and record the result in the relevant continuity workstream.

### Control Hub scan refusal

1. Confirm `--projects-root` exists and is a directory.
2. Confirm the intended user can read and traverse it.
3. Do not substitute `/`, `$HOME`, or an unreviewed broad workspace.
4. Correct the configured root and rerun the same guarded command.
5. Existing database state should remain untouched after refusal.


### Control Hub runtime refusal

1. Run `id` and confirm the effective UID is nonzero and matches the configured operator.
2. Run `./fleetctl hub-runtime check` and preserve the exact refusal.
3. Inspect owner/mode evidence for the config, state directory, DB, and sidecars.
4. Do not use root, `sudo`, automatic `chown`, or a second database to bypass an identity mismatch.
5. Stop the user service before migration or restore; never overwrite an existing authoritative DB or orphaned `-wal`/`-shm` sidecar.
6. Verify a backup manifest before recovery, then rerun check, scan, and the loopback dashboard probe.

## Recovery From Clean Machine

1. Clone or restore the Fleet repository.
2. Read the continuity registry and state.
3. Select a reviewed managed repository root.
4. Run the bootstrap procedure in order.
5. Configure GitHub HTTPS authentication or add the SSH key to GitHub.
6. Validate required repositories against the continuity registry.
7. Configure the identity-bound Control Hub runtime under the intended non-root user.
8. Run runtime check, guarded scan, verified backup, and loopback serve proof.
9. Install the systemd user service/timer only after those checks pass.
