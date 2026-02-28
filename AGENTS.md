# AGENTS.md

Project-specific operating instructions for Codex in this repository.

## Mission

Keep `fleet` as a low-friction control plane for:
- workstation bootstrap
- work inventory
- execution tracking

## Execution Rules

1. Prefer direct implementation over long planning.
2. Keep changes incremental and production-usable in one pass.
3. Run validation after edits:
   - `bash -n bootstrap.sh healthcheck.sh install_nix.sh fleetctl`
   - `shellcheck bootstrap.sh healthcheck.sh install_nix.sh fleetctl ops/seed_linear_issues.sh`
   - `python3 -m py_compile ops/control_hub_agent.py`
4. If changing Control Hub behavior, run:
   - `./fleetctl hub-scan`
5. Keep docs aligned with behavior (`README.md` + relevant file in `docs/`).

## Style Constraints

- Default to ASCII.
- Keep scripts POSIX/bash-compatible unless there is a strong reason not to.
- Avoid introducing new dependencies unless required.
- Prefer local-first architecture and optional cloud integrations.

## Security and Secrets

- Never hardcode tokens or credentials.
- Use env vars for integrations (`LINEAR_API_KEY`, `GITHUB_TOKEN`, `NOTION_API_KEY`).
- Avoid broad privileged command policies; keep allow-rules narrow.

## Commit Quality

- Commit message should describe user-visible outcome.
- Include only related files per commit.
- Leave repo in a clean state (`git status` empty).


## Inherit Global Jarrett Prime Protocol
- This project must inherit and apply the global protocol defined at /home/jarrettdustinqq/AGENTS.md.
- If local instructions and global protocol differ, follow higher-priority system/developer policy first, then apply the global protocol.
