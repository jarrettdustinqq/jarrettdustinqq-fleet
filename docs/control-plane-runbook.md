# Controller Runbook

## Scope

This runbook defines how to bootstrap and operate the Chromebook-based controller
node using Crostini + Debian + Nix, with project execution coordinated through
GitHub and Linear.

## Architecture Snapshot

- Controller node: Chromebook Crostini container (local operator surface).
- Local toolkit: `fleet/` scripts + Nix dev shell for reproducible commands.
- Source of truth: GitHub repositories in `~/projects`.
- Execution tracking: Linear project/issue workflow.
- Optional knowledge layer: Notion page mirrored from this runbook.

## Bootstrap Procedure

1. Update host packages.
2. Install Nix with `fleetctl install-nix`.
3. Run `fleetctl bootstrap` to create SSH key and sync repos.
4. Run `fleetctl health` to verify readiness.

### Commands

```bash
sudo apt-get update && sudo apt-get upgrade -y && sudo apt-get autoremove -y
/home/jarrettdustinqq/fleet/fleetctl install-nix
/home/jarrettdustinqq/fleet/fleetctl bootstrap
/home/jarrettdustinqq/fleet/fleetctl health
```

## Daily Operations

1. Enter reproducible shell when doing active work:
   `fleetctl shell`
2. Sync repos before coding:
   `fleetctl bootstrap`
3. Run health check before and after significant changes:
   `fleetctl health`

## Weekly Reliability Review

1. Confirm patch baseline (`apt list --upgradable`).
2. Confirm repo sync status (`fleetctl bootstrap` result).
3. Confirm shell lint CI still passes (`Shell CI` workflow).
4. Review open risks and create/update Linear issues.

## Incident Checklist

### Nix unavailable

1. Source profile script:
   `. "$HOME/.nix-profile/etc/profile.d/nix.sh"`
2. Verify version:
   `nix --version`
3. If still failing, rerun:
   `fleetctl install-nix`

### GitHub auth failure

1. Print public key:
   `cat ~/.ssh/id_ed25519.pub`
2. Ensure key exists in GitHub SSH settings.
3. Test:
   `ssh -T git@github.com`

### Repo sync failure

1. Check network/DNS.
2. Run `fleetctl bootstrap` again.
3. If one repo fails, run manual pull in that directory and inspect merge state.

### Healthcheck failure

1. Run `fleetctl health` and note failing section.
2. Resolve missing dependency/tool.
3. Re-run `fleetctl health` and document result.

## Recovery From Clean Machine

1. Copy `fleet/` directory to the machine.
2. Run bootstrap procedure in order.
3. Add SSH key to GitHub.
4. Validate all repos cloned under `~/projects`.

