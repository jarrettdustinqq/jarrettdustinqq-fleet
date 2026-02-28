# Mission Control Agent

`mission-control` is the one-command orchestrator for centralized execution inventory.

## What It Runs

1. `chat-agent` to synthesize active Codex chat workstreams.
2. `venture-agent` to score repo autonomy and execution readiness.
3. `control_hub_agent` (`scan` or `scan-serve`) for interactive management.

## Commands

```bash
# Refresh chat + venture reports, then launch dashboard
./fleetctl mission-control

# Include safe check execution in venture-agent before launching hub
./fleetctl mission-control --venture-run-checks

# Inventory-only run (no web server)
./fleetctl mission-control --scan-only

# Pass through Control Hub args after `--`
./fleetctl mission-control -- --port 8766 --no-window-tracking
```

## Notes

- Unknown args are forwarded to `ops/control_hub_agent.py`.
- If chat or venture collectors fail, Mission Control still launches Control Hub with latest available data and logs the collector failure count.
