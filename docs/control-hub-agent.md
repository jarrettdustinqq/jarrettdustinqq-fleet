# Control Hub Agent

Local-first inventory agent + dashboard to manage active work in one place.

## What It Inventories

- Local git repositories under `~/projects` by default.
- Repo health: branch, dirty status, ahead/behind, last commit age, remote URL.
- Optional Linear assigned issues (when `LINEAR_API_KEY` is set).
- Auto-generated recommendations (focus and cleanup actions).

## Commands

```bash
# scan and print summary JSON
./fleetctl hub-scan

# scan then start local dashboard at http://127.0.0.1:8765
./fleetctl hub-serve
```

Direct script usage:

```bash
python3 ./ops/control_hub_agent.py scan
python3 ./ops/control_hub_agent.py scan-serve --port 8765
```

## Optional Integrations

### Linear

```bash
export LINEAR_API_KEY=lin_api_xxx
export LINEAR_TEAM_ID=<optional-team-id>
./fleetctl hub-scan
```

When enabled, only non-completed and non-canceled assigned issues are imported.

## Dashboard Features

- Repository table with editable `focus_level` (0-3) and `next_action`.
- Task table with editable `done` and `notes`.
- Recommendation table with open/done tracking.
- "Rescan Now" button to refresh inventory in-place.

## Data Storage

- SQLite DB path:
  `~/.local/share/fleet-control-hub/control_hub.db`
- Local-only and file-based for portability and backups.

