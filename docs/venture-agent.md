# Venture Agent

`venture-agent` scans your Linux venture repositories and builds a prioritized
optimization queue for autonomous execution.

## What It Analyzes

- Git repositories discovered under `~` (your Linux home) by default.
- Repo signals: CI workflows, tests, docs, AGENTS guidance, and manifests.
- Safe local verification commands (for example: `make test`, `pytest`, `bash -n`).
- Optional check execution to detect failing automation gates.

## Commands

```bash
# Generate autonomy reports without executing checks
./fleetctl venture-agent

# Execute safe checks while scanning and include failures in the queue
./fleetctl venture-agent --run-checks --top 15

# Restrict scan roots
./fleetctl venture-agent --root ~/projects --root ~/control_station
```

## Outputs

- Markdown brief:
  `~/.local/share/fleet-control-hub/venture_autonomy_brief.md`
- JSON report:
  `~/.local/share/fleet-control-hub/venture_autonomy_report.json`
- Codex execution prompt:
  `~/.local/share/fleet-control-hub/venture_autonomy_codex_prompt.txt`

## Timer Automation

```bash
# Install/start daily timer
./fleetctl venture-agent-timer

# View timer + service state
./fleetctl venture-agent-timer --status

# Remove timer + service
./fleetctl venture-agent-timer --uninstall
```

The timer runs daily with randomized delay and executes:
`./fleetctl venture-agent --top 20 --run-checks`
