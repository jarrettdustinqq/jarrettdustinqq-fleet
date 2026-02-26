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

1. Discover: scan local repos + optional external tasks.
2. Normalize: persist into SQLite with stable item identities.
3. Recommend: generate next-best actions from drift and risk signals.
4. Manage: update focus, notes, and done state in the dashboard.
5. Review: rescan regularly and resolve/reopen recommendations.

## Data Model

- `repos`: local source-of-truth for engineering execution context.
- `tasks`: external and local work items with management fields.
- `recommendations`: generated guidance tracked as open/done/resolved.
- `meta`: scan timestamps and integration status.

## Next Extensions

1. GitHub API enrichment: open PR counts, stale branches, review backlog.
2. Notion enrichment: tagged project docs and decision logs.
3. Timeboxing: weekly focus plans generated from priorities and drift.
4. Cross-machine sync: optional remote SQLite replication or export snapshots.

