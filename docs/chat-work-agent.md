# Chat Work Agent

`chat-agent` synthesizes Codex terminal chat state into an actionable finish queue.

## Purpose

- Inventory open and saved chats.
- Group related threads into workstreams.
- Highlight blockers and completion signals.
- Recommend what to finish next and why.
- Track trends via delta snapshots so repeated blockers are surfaced.
- Allow ack/suppress controls so finished threads stop resurfacing.

## Data Sources

- `~/.codex/state_5.sqlite` (`threads` table)
- `~/.codex/history.jsonl` (latest user signals per session)
- Live process list (`ps`) for active Codex terminal sessions

## Usage

```bash
./fleetctl chat-agent
./fleetctl chat-agent --top 8
./fleetctl chat-agent --profile security-first
./fleetctl chat-agent --include-archived
./fleetctl chat-agent --md-out ~/chat-work-brief.md --json-out ~/chat-work-brief.json
./fleetctl chat-agent --codex-prompt-out ~/chat-work-codex-prompt.txt
./fleetctl chat-agent --ack-topic general
./fleetctl chat-agent --ack-thread 019c99c9-6aa3-7940-bd4d-4ad1516cd176
./fleetctl chat-agent --unack-topic general
./fleetctl chat-agent --clear-acks
./fleetctl chat-agent --archive-suggest-max 8
./fleetctl chat-agent --apply-archive-suggestions
```

## Outputs

- Markdown brief (default):
  `~/.local/share/fleet-control-hub/chat_work_brief.md`
- JSON report (default):
  `~/.local/share/fleet-control-hub/chat_work_brief.json`
- Codex handoff prompt (default):
  `~/.local/share/fleet-control-hub/chat_work_codex_prompt.txt`
- Trend delta log (default):
  `~/.local/share/fleet-control-hub/chat_work_deltas.jsonl`
- Ack/suppression state (default):
  `~/.local/share/fleet-control-hub/chat_work_ack.json`

The brief includes:

- Priority workstreams (grouped by topic)
- Top threads with urgency scoring
- "Finish next" recommendations with rationale
- Archive suggestions for stale or completed threads
- Active Codex terminal process snapshot

## Scoring Model (Heuristic)

Priority score combines:

- stream risk class (security/integrity/infrastructure)
- recency of activity
- blocker/error signals in recent user text
- completion signals (to avoid over-prioritizing closed loops)
- primary vs subagent role
- persistent blocker trends from recent delta snapshots
- check/build failure hints parsed from recent thread signals

Profiles:

- `balanced` (default): mixed risk + momentum weighting
- `security-first`: heavier weighting for incident/integrity risk streams
- `ship-fast`: higher recency/momentum weighting
- `cleanup-first`: lower momentum, tuned for stale-context reduction

Archive suggestion controls:

- `--archive-suggest-max N`: cap suggested ack/archive candidates
- `--apply-archive-suggestions`: auto-ack suggested thread IDs and rebuild output

## Repo Context

Each workstream attempts to link to local git repositories via thread `cwd`.
Reported repo context includes:

- branch
- dirty/clean status
- ahead/behind counts vs upstream
- last commit age (hours)

## Scheduling

Install hourly snapshots:

```bash
./fleetctl chat-agent-timer
./fleetctl chat-agent-timer --status
```

Remove timer:

```bash
./fleetctl chat-agent-timer --uninstall
```

## Notes

- This is local-first and dependency-light (stdlib Python only).
- Thread grouping is heuristic and may be refined over time.
