# Control Hub Agent

Local-first inventory agent + dashboard to manage active work in one place.

## What It Inventories

- Local git repositories under `~/projects` by default.
- Repo health: branch, dirty status, ahead/behind, last commit age, remote URL.
- Optional Linear assigned issues (when `LINEAR_API_KEY` is set).
- Auto-generated recommendations (focus and cleanup actions).
- Live active window focus with agenda title, in-window summary, last step, and next step.
- Per-context agenda memory (browser tab/window contexts tracked over time).
- Interaction helper agent that detects unfinished interactive flows and recommends completion actions.
- Mode efficiency agent that detects obvious/simple tasks and recommends lower reasoning mode automatically.

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
python3 ./ops/control_hub_agent.py scan-serve --no-window-tracking
python3 ./ops/control_hub_agent.py scan-serve --window-poll-seconds 1.0
python3 ./ops/control_hub_agent.py scan-serve --no-interaction-helper
python3 ./ops/control_hub_agent.py scan-serve --no-window-ocr
python3 ./ops/control_hub_agent.py scan-serve --ocr-max-chars 1800
python3 ./ops/control_hub_agent.py scan-serve --port 8766
python3 ./ops/control_hub_agent.py scan-serve --no-mode-efficiency-agent
python3 ./ops/control_hub_agent.py scan-serve --auto-apply-reasoning-mode
python3 ./ops/control_hub_agent.py scan-serve --codex-config ~/.codex/config.toml
python3 ./ops/control_hub_agent.py scan-serve --auto-apply-reasoning-mode --mode-stability-threshold 2
```

Window tracking notes:

- Wayland backends: `swaymsg` (Sway) or `hyprctl` (Hyprland).
- X11 backends: `xdotool` (preferred) or `xprop` fallback.
- Browser windows are tracked at active-tab scope; only the currently focused tab is summarized.
- OCR mode (optional) uses `tesseract` + ImageMagick `import` on X11 when available.
- Mode efficiency recommendations appear in Live Focus as task complexity + suggested `/reasoning` mode.
- Live Focus includes an `Apply Suggested Mode` button for one-click config updates.
- Optional auto-apply mode (`--auto-apply-reasoning-mode`) updates Codex reasoning mode whenever complexity recommendations change.
- Auto-apply uses a stability threshold (`--mode-stability-threshold`, default `2`) to avoid mode thrashing on rapid context switches.
- If unavailable, the dashboard shows the reason in `Window tracking` status.

Startup diagnostics and prerequisites:

- `scan-serve` prints startup diagnostics before and after scan (`host`, `port`, DB path, projects root, scan summary).
- `sqlite3` CLI is optional for dashboard runtime but required for manual DB inspection commands such as:
  `sqlite3 ~/.local/share/fleet-control-hub/control_hub.db '.tables'`
- If the HTTP bind fails (for example, port already in use), startup logs include the failing bind target and a `--port` recovery hint.

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
- Live Focus panel with agenda title, app, location, in-window summary, last step, and next step.
- Recent Window Activity feed with per-focus agenda + step updates.
- Agenda Memory table showing rolling context history and next actions.
- Interaction Opportunities table listing unfinished/not-started interactions and recommended solutions.
- "Rescan Now" button to refresh inventory in-place.

## Data Storage

- SQLite DB path:
  `~/.local/share/fleet-control-hub/control_hub.db`
- Local-only and file-based for portability and backups.
