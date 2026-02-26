# Fleet Bootstrap Toolkit

Small bootstrap toolkit for setting up a controller/dev node quickly.

## Commands

- `./fleetctl install-nix`: Install Nix in single-user mode.
- `./fleetctl bootstrap [repo_url ...]`: Create SSH key, clone/update repos.
- `./fleetctl health`: Validate local tooling and project workspace state.
- `./fleetctl remote-agent [options]`: Print/create SSH key, verify GitHub auth, optionally test a VPS target.
- `./fleetctl vps-discover [options]`: Find likely VPS targets from local SSH/git history.
- `./fleetctl chat-agent [options]`: Analyze open/saved Codex chats and recommend what to finish next.
- `./fleetctl chat-agent-timer [options]`: Install/manage hourly chat-agent snapshots.
- `./fleetctl venture-agent [options]`: Analyze Linux code repos and generate an autonomy optimization queue.
- `./fleetctl venture-agent-timer [options]`: Install/manage daily venture-agent runs.
- `./fleetctl hub-scan`: Build/update local Control Hub inventory DB.
- `./fleetctl hub-serve`: Scan + run local interactive Control Hub dashboard with startup diagnostics (scan progress, sqlite3 CLI availability, bind target failures), live tracking (Wayland: `swaymsg`/`hyprctl`, X11: `xdotool`/`xprop`), agenda last/next-step guidance, an interaction helper agent, and a mode-efficiency agent that recommends lower reasoning mode for obvious/simple tasks (manual apply button + optional auto-apply with stability threshold).
- `./fleetctl shell`: Enter the flake dev shell (requires Nix).

## Repository List

If `bootstrap` is run without arguments, repo URLs are loaded from `repos.txt`
(one URL per line, comments allowed with `#`).

## Local Validation

```bash
bash -n bootstrap.sh healthcheck.sh install_nix.sh fleetctl
shellcheck bootstrap.sh healthcheck.sh install_nix.sh fleetctl
```

Remote access agent examples:

```bash
./fleetctl remote-agent
./fleetctl remote-agent --auto
./fleetctl remote-agent --save-vps user@your-vps
./fleetctl remote-agent --auto --discover
./fleetctl remote-agent --auto --discover --default-user ubuntu
./fleetctl remote-agent --gh-add
./fleetctl remote-agent --vps user@your-vps
./fleetctl remote-agent --vps user@your-vps --copy-key
./fleetctl vps-discover
./fleetctl vps-discover --best
./fleetctl chat-agent
./fleetctl chat-agent --top 8
./fleetctl chat-agent --profile security-first
./fleetctl chat-agent --md-out ~/chat-work-brief.md --json-out ~/chat-work-brief.json
./fleetctl chat-agent --codex-prompt-out ~/chat-work-codex-prompt.txt
./fleetctl chat-agent --ack-topic general
./fleetctl chat-agent --ack-thread 019c99c9-6aa3-7940-bd4d-4ad1516cd176
./fleetctl chat-agent --unack-topic general
./fleetctl chat-agent --archive-suggest-max 8
./fleetctl chat-agent --apply-archive-suggestions
./fleetctl chat-agent-timer
./fleetctl chat-agent-timer --status
./fleetctl chat-agent-timer --uninstall
./fleetctl venture-agent
./fleetctl venture-agent --run-checks --top 15
./fleetctl venture-agent --root ~/projects --root ~/control_station
./fleetctl venture-agent-timer
./fleetctl venture-agent-timer --status
./fleetctl venture-agent-timer --uninstall
```

Tip: in interactive terminals, `--auto --discover` now shows top VPS candidates (ranked by local evidence) and lets you select by number, then runs a lightweight SSH reachability probe before proceeding.

## Operational Artifacts

- `docs/control-plane-runbook.md`: bootstrap, operations, and incident handling.
- `docs/control-hub-agent.md`: local inventory + dashboard usage.
- `docs/control-hub-architecture.md`: approach comparison and chosen structure.
- `docs/venture-agent.md`: repo autonomy scoring and optimization queue generation.
- `ops/linear-seed-backlog.csv`: ready-to-use objective backlog template.
- `ops/linear-seed-backlog.json`: machine-readable backlog for automation.
- `ops/seed_linear_issues.sh`: seed Linear issues from JSON via API key.

## Seed Linear Backlog

```bash
export LINEAR_API_KEY=lin_api_xxx
./ops/seed_linear_issues.sh --team-id <TEAM_ID> --dry-run
./ops/seed_linear_issues.sh --team-id <TEAM_ID>
```
