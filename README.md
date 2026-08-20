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
- `./fleetctl mission-control [options]`: Run chat-agent + venture-agent, then launch the unified Control Hub workflow.
- `./fleetctl hub-scan`: Build/update local Control Hub inventory DB.
- `./fleetctl hub-serve`: Scan + run local interactive Control Hub dashboard with startup diagnostics (scan progress, sqlite3 CLI availability, bind target failures), live tracking (Wayland: `swaymsg`/`hyprctl`, X11: `xdotool`/`xprop`), agenda last/next-step guidance, an interaction helper agent, and a mode-efficiency agent that recommends lower reasoning mode for obvious/simple tasks (manual apply button + optional auto-apply with stability threshold).
- `./fleetctl shell`: Enter the flake dev shell (requires Nix).

## Repository List

If `bootstrap` is run without arguments, repo URLs are loaded from `repos.txt`
(one URL per line, comments allowed with `#`).

## Health Configuration

`./fleetctl health` accepts these environment overrides:

- `PROJECTS_DIR` (default: `$HOME/projects`): the intentionally managed repository root.
- `SSH_KEY_PATH` (default: `$HOME/.ssh/id_ed25519`): SSH private-key path; the matching `.pub` file must also exist.

GitHub health is decided by a noninteractive `git ls-remote` preflight for every
active entry in `repos.txt`, including private support repositories. An SSH
keypair is configuration evidence, not proof that GitHub accepts the key. An
authenticated GitHub CLI HTTPS session is supported, and auth diagnostics are
suppressed.

Set `PROJECTS_DIR` only to a reviewed repository boundary. Do not point it at a
broad workspace merely because that directory contains repositories; an outer
Git repository can mask or distort nested inventory.

Control Hub repository scans expose `complete`, `partial`, or `failed`
observation status per configured root. Add independent roots with repeated
`--additional-projects-root <path>` options. If every configured root fails, the
scan refuses before opening the database. If one root fails while another is
observable, successful roots refresh only their own evidence and the failed
root's prior rows remain untouched. Partial roots never reconcile missing rows
or resolve missing recommendations. A verified complete root stale-marks only
its own unseen rows with a 30-day review grace instead of deleting them,
preserving `focus_level`, `next_action`, and prior observations.

Omitting a previously configured additional root is an explicit configuration
removal, not a failed observation. Its rows become `root-removed` and remain
recoverable; re-adding the root restores observation. Duplicate roots are
de-duplicated, overlapping parent/child roots are refused as ambiguous, and the
total configured root count is bounded.

Control Hub also imports canonical repository identity from
`<projects-root>/continuity/repo-registry.json` by default. Override the path with
`--repo-registry` or `CONTINUITY_REPO_REGISTRY`. Registry input is validated and
applied atomically: missing or malformed input preserves prior canonical rows and
operator management fields. Registered repositories remain visible without a
local checkout, while each checkout or linked worktree remains a separate local
observation mapped to the same `owner/repo` identity. Remote URLs are stored
without embedded credentials or query data.

Example using a dedicated managed root:

```bash
PROJECTS_DIR="$HOME/projects" ./fleetctl health
./fleetctl hub-scan \
  --projects-root "$HOME/projects" \
  --additional-projects-root "$HOME/control_station"
```

## Local Validation

```bash
bash -n bootstrap.sh healthcheck.sh install_nix.sh fleetctl ops/seed_linear_issues.sh tests/test_healthcheck.sh
shellcheck bootstrap.sh healthcheck.sh install_nix.sh fleetctl ops/seed_linear_issues.sh tests/test_healthcheck.sh
bash tests/test_healthcheck.sh
python3 -m py_compile ops/control_hub_agent.py ops/control_hub_safe_entry.py ops/mission_control_agent.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
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
./fleetctl mission-control
./fleetctl mission-control --venture-run-checks -- --port 8766
./fleetctl mission-control --scan-only
```

Tip: in interactive terminals, `--auto --discover` now shows top VPS candidates (ranked by local evidence) and lets you select by number, then runs a lightweight SSH reachability probe before proceeding.

## Operational Artifacts

- `docs/control-plane-runbook.md`: bootstrap, operations, and incident handling.
- `docs/control-hub-agent.md`: local inventory + dashboard usage.
- `docs/mission-control-agent.md`: one-command orchestration for chat/venture/hub inventory.
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
