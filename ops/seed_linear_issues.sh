#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  LINEAR_API_KEY=... ./ops/seed_linear_issues.sh --team-id <TEAM_ID> [--assignee-id <USER_ID>] [--data-file <path>] [--dry-run]

Description:
  Seeds Linear issues from a JSON backlog file (default: ./ops/linear-seed-backlog.json).
  Requires an API key from https://linear.app/settings/api.

Examples:
  LINEAR_API_KEY=lin_api_xxx ./ops/seed_linear_issues.sh --team-id efb1dcb0-9509-42e1-a9c3-a926b51fcff6
  LINEAR_API_KEY=lin_api_xxx ./ops/seed_linear_issues.sh --team-id TEAM --assignee-id USER --dry-run
EOF
}

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_FILE="$ROOT_DIR/ops/linear-seed-backlog.json"
TEAM_ID=""
ASSIGNEE_ID=""
DRY_RUN=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --team-id)
      TEAM_ID="${2:-}"
      shift 2
      ;;
    --assignee-id)
      ASSIGNEE_ID="${2:-}"
      shift 2
      ;;
    --data-file)
      DATA_FILE="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [ -z "$TEAM_ID" ]; then
  echo "Missing required --team-id" >&2
  usage
  exit 1
fi

if [ ! -f "$DATA_FILE" ]; then
  echo "Data file not found: $DATA_FILE" >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "Missing required command: jq" >&2
  exit 1
fi

if [ "$DRY_RUN" -ne 1 ] && [ -z "${LINEAR_API_KEY:-}" ]; then
  echo "Missing LINEAR_API_KEY environment variable" >&2
  exit 1
fi

if [ "$DRY_RUN" -ne 1 ] && [ "${LINEAR_API_KEY:-}" = "lin_api_xxx" ]; then
  echo "LINEAR_API_KEY is still set to the placeholder value 'lin_api_xxx'." >&2
  echo "Create a real key at https://linear.app/settings/api and export it before rerunning." >&2
  exit 1
fi

linear_graphql() {
  local payload="$1"
  local tmpfile
  local http_code

  tmpfile="$(mktemp)"
  http_code="$(curl -sS -o "$tmpfile" -w "%{http_code}" https://api.linear.app/graphql \
    -H "Authorization: $LINEAR_API_KEY" \
    -H "Content-Type: application/json" \
    --data "$payload")"

  if [ "$http_code" -ge 400 ] 2>/dev/null; then
    echo "Linear API request failed with HTTP $http_code." >&2
    if [ -s "$tmpfile" ]; then
      jq -r '.errors // .' "$tmpfile" 2>/dev/null >&2 || cat "$tmpfile" >&2
    fi
    rm -f "$tmpfile"
    return 1
  fi

  cat "$tmpfile"
  rm -f "$tmpfile"
}

STATE_RESPONSE=""
DEFAULT_STATE_ID=""
if [ "$DRY_RUN" -ne 1 ]; then
  STATE_QUERY_PAYLOAD="$(jq -n \
    --arg team_id "$TEAM_ID" \
    '{query: "query($teamId: String!) { team(id: $teamId) { states { nodes { id name } } } }", variables: {teamId: $team_id}}')"

  STATE_RESPONSE="$(linear_graphql "$STATE_QUERY_PAYLOAD")"
  DEFAULT_STATE_ID="$(jq -r '.data.team.states.nodes[] | select(.name == "Todo") | .id' <<<"$STATE_RESPONSE" | head -n1)"

  if [ -z "$DEFAULT_STATE_ID" ]; then
    DEFAULT_STATE_ID="$(jq -r '.data.team.states.nodes[0].id // empty' <<<"$STATE_RESPONSE")"
  fi

  if [ -z "$DEFAULT_STATE_ID" ]; then
    echo "Could not resolve a usable state for team $TEAM_ID" >&2
    exit 1
  fi
fi

count=0
while IFS= read -r item; do
  title="$(jq -r '.title' <<<"$item")"
  priority="$(jq -r '.priority // 3' <<<"$item")"
  state_name="$(jq -r '.state // "Todo"' <<<"$item")"
  description="$(jq -r '.description // ""' <<<"$item")"
  acceptance="$(jq -r '.acceptance_criteria // ""' <<<"$item")"
  repo="$(jq -r '.repo // ""' <<<"$item")"

  state_id=""
  if [ "$DRY_RUN" -ne 1 ]; then
    state_id="$(jq -r --arg name "$state_name" '.data.team.states.nodes[] | select(.name == $name) | .id' <<<"$STATE_RESPONSE" | head -n1)"
    if [ -z "$state_id" ]; then
      state_id="$DEFAULT_STATE_ID"
    fi
  fi

  body="$(cat <<EOF
$description

Acceptance Criteria:
- $acceptance

Context:
- Repo: $repo
EOF
)"

  input_json="$(jq -n \
    --arg team_id "$TEAM_ID" \
    --arg title "$title" \
    --arg description "$body" \
    --arg state_id "$state_id" \
    --argjson priority "$priority" \
    --arg assignee_id "$ASSIGNEE_ID" \
    '{
      teamId: $team_id,
      title: $title,
      description: $description,
      stateId: $state_id,
      priority: $priority
    } + (if $assignee_id != "" then {assigneeId: $assignee_id} else {} end)')"

  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] would create: $title"
    count=$((count + 1))
    continue
  fi

  payload="$(jq -n \
    --arg query 'mutation($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { id identifier title } } }' \
    --argjson input "$input_json" \
    '{query: $query, variables: {input: $input}}')"

  response="$(linear_graphql "$payload")"
  ok="$(jq -r '.data.issueCreate.success // false' <<<"$response")"
  if [ "$ok" != "true" ]; then
    echo "Failed creating issue: $title" >&2
    jq -r '.errors // .data' <<<"$response" >&2
    exit 1
  fi

  identifier="$(jq -r '.data.issueCreate.issue.identifier' <<<"$response")"
  echo "created: $identifier - $title"
  count=$((count + 1))
done < <(jq -c '.[]' "$DATA_FILE")

echo "Seeded $count issue(s)."
