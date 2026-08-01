# Fleet Host Remediation Audit

`fleetctl host-audit` creates a reversible evidence bundle for a controller host.
It is intended for repository-topology, service, ownership, state-store, and
recovery-readiness investigations.

## Safety contract

The audit may read host state, update Git remote-tracking references, run tests,
and create a compressed backup of the outer workspace `.git` directory. It does
not:

- move, delete, quarantine, or reinitialize Git metadata;
- delete or migrate databases;
- modify systemd units;
- change file ownership;
- copy SSH keys, token stores, cookies, `.env` contents, or authentication data
  into the report.

Run it as the normal controller user, not as root.

```bash
./fleetctl host-audit
```

Useful options:

```bash
./fleetctl host-audit --no-fetch
./fleetctl host-audit --quick --no-fetch
./fleetctl host-audit --output-root "$HOME/audit-output"
./fleetctl host-audit --outer-workspace "$HOME/agent_workspace"
./fleetctl host-audit --fleet-dir "$HOME/agent_workspace/jarrettdustinqq-fleet"
```

## Output

Each run creates:

```text
~/fleet-system-remediation-<UTC timestamp>/
├── report.txt
├── summary.json
└── backups/
    ├── agent_workspace-dotgit-<UTC timestamp>.tar.gz
    └── agent_workspace-dotgit-<UTC timestamp>.tar.gz.sha256
```

Stable pointer files are also written to:

```text
~/.local/state/fleet-system-remediation/latest-report
~/.local/state/fleet-system-remediation/latest-output-dir
```

These pointers avoid fragile recursive glob verification.

## User systemd access

Crostini and root-mediated sessions can lack the default user D-Bus environment.
The audit tries three read-only routes in order:

1. direct `systemctl --user`;
2. the user's `/run/user/<uid>/bus` with explicit environment variables;
3. `systemctl --user --machine=<user>@.host`.

Failure to reach a user bus is recorded as a diagnostic warning, not confused
with a failed remediation run.

## Result semantics

`REQUIRED_FAILURE_COUNT=0` means the backup, checksum, Fleet validation, and
report creation completed. Diagnostic probes are counted separately as
`DIAGNOSTIC_WARNING_COUNT`.

A nonzero required-failure count causes the command to exit with status `1`.
