# Fleet Bootstrap Toolkit

Small bootstrap toolkit for setting up a controller/dev node quickly.

## Commands

- `./fleetctl install-nix`: Install Nix in single-user mode.
- `./fleetctl bootstrap [repo_url ...]`: Create SSH key, clone/update repos.
- `./fleetctl health`: Validate local tooling and project workspace state.
- `./fleetctl shell`: Enter the flake dev shell (requires Nix).

## Repository List

If `bootstrap` is run without arguments, repo URLs are loaded from `repos.txt`
(one URL per line, comments allowed with `#`).

## Local Validation

```bash
bash -n bootstrap.sh healthcheck.sh install_nix.sh fleetctl
shellcheck bootstrap.sh healthcheck.sh install_nix.sh fleetctl
```

## Operational Artifacts

- `docs/control-plane-runbook.md`: bootstrap, operations, and incident handling.
- `ops/linear-seed-backlog.csv`: ready-to-use objective backlog template.
