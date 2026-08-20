# Control Hub Architecture

## Goal

Maintain a single interactive place to manage active work across:

- local code repositories
- task systems (Linear)
- knowledge systems (Notion, optional)
- system-level execution hygiene

## Approaches Considered

## 1) SaaS-first (all work in one cloud PM tool)

Pros:

- fast setup
- polished UX
- strong collaboration defaults

Cons:

- weak inventory for local machine state (dirty repos, branch drift, local-only notes)
- difficult to automate deep host telemetry without additional tooling

## 2) Cloud integration hub (always-on server + APIs + cloud DB)

Pros:

- central automation
- easy cross-device dashboards

Cons:

- more infrastructure and credentials to manage
- higher operational overhead before first value

## 3) Local-first agent + optional cloud sync (chosen)

Pros:

- immediate value with zero external infra
- resilient when offline
- can still pull from cloud tools when tokens are present
- aligns with workstation-centric dev execution

Cons:

- initially single-user oriented
- requires explicit token setup for external systems

## Chosen Structure

The Control Hub follows a five-stage loop:

1. Discover: import canonical registry identity, scan local checkouts/worktrees,
   and collect optional external tasks.
2. Normalize: persist canonical repositories separately from path-keyed local
   observations and external items.
3. Recommend: generate next-best actions from drift and risk signals.
4. Manage: update focus, notes, and done state in the dashboard.
5. Review: rescan regularly and resolve/reopen recommendations.

## Data Model

- `registered_repos`: local projection of canonical continuity registry identity,
  classification, lifecycle state, and canonical operator management fields.
- `repos`: path-keyed local checkout/worktree observations plus preserved legacy
  management fields and an owning observation root; not canonical cross-project
  truth.
- `repo_observation_roots`: current/removed root configuration plus each root's
  latest independent outcome and bounded errors.
- `repo_scan_runs`: aggregate scan outcome and root-set snapshot.
- `repo_root_scan_runs`: per-root `complete`, `partial`, `failed`, or explicit
  configuration-`removed` history under an aggregate scan.
- `tasks`: external and local work items with management fields.
- `recommendations`: generated guidance tracked as open/done/resolved.
- `meta`: scan timestamps and integration status.

Repository reconciliation is fail-closed and scoped by root. If every root
fails, the database is not opened. When roots have mixed outcomes, complete
roots may reconcile only observations they own, while partial/failed roots
preserve their prior evidence. Omitting a previously configured root is recorded
as an explicit reversible removal rather than confused with temporary
unavailability. Complete roots stale-mark unseen rows instead of deleting them.
The default 30-day stale grace is a review threshold; deletion remains an
explicit future operation. Canonical repository/workstream identity remains in
`jarrettdustinqq/continuity`; Control Hub imports a validated projection without
becoming a competing source of truth. Registry input is also fail-closed:
unavailable or invalid input preserves the last valid projection and operator
state. A canonical repository can exist without a checkout, and any number of
local checkout/worktree observations can map to the same `owner/repo` identity.


## Runtime and State Boundary

The supported background process is one systemd user service bound to the
non-root UID/GID/user recorded in a private runtime config. Its single
authoritative SQLite path is inside a mode-`0700` state directory; config,
database, sidecars, backups, and manifests are owner-bound and private. The
launcher refuses UID 0, identity drift, unsafe ownership/modes, symlinks, and
non-loopback/privileged binds.

The service invokes the guarded multi-root `scan-serve` path. A separate user
timer creates online SQLite backups with identity, size, SHA-256, and integrity
evidence. Migration and restore refuse overwrite and preserve the source/backup;
there is no automatic retention deletion. See `docs/control-hub-runtime.md`.

CI exercises configure, scan, backup, verify, restore, unit rendering, and an
HTTP dashboard response while explicitly asserting a non-root runner. This
proves the repository contract but cannot substitute for current host ownership,
service, or process evidence.

## Next Extensions

1. GitHub API enrichment: open PR counts, stale branches, review backlog.
2. Notion enrichment: tagged project docs and decision logs.
3. Timeboxing: weekly focus plans generated from priorities and drift.
4. Cross-machine sync: optional remote SQLite replication or export snapshots.

## Internet-Backed Structure Notes

Additional structure guidance was reviewed against upstream docs:

1. Local-first SQLite with WAL stays the default baseline.
   Why: SQLite WAL supports concurrent readers with a writer and is robust for local app state snapshots.
   Source: https://www.sqlite.org/wal.html
2. Keep dashboard server scoped to localhost unless explicitly proxied.
   Why: Python `http.server` is convenient for local tooling but not intended as a hardened internet-facing server.
   Source: https://docs.python.org/3/library/http.server.html
3. Prefer systemd timers for recurring inventory refreshes over ad-hoc cron wrappers.
   Why: timer units support persistence and randomized delays, improving resilience after downtime and reducing burst contention.
   Source: https://man7.org/linux/man-pages/man5/systemd.timer.5.html

Practical result in this repo:

- Keep Control Hub local-first and dependency-light.
- Use timer-driven collectors (`chat-agent-timer`, `venture-agent-timer`) for periodic refresh.
- Add an orchestrator (`mission-control`) to run collectors + dashboard as one operator entrypoint.
