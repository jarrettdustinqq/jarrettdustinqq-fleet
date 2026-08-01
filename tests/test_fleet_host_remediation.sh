#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_DIR/ops/fleet_host_remediation.sh"
TMP_ROOT="$(mktemp -d /tmp/fleet-host-remediation-test.XXXXXX)"

cleanup() {
  rm -rf -- "$TMP_ROOT"
}
trap cleanup EXIT

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

assert_file() {
  [ -f "$1" ] || fail "missing file: $1"
}

assert_contains() {
  local file="$1"
  local text="$2"
  grep -Fq -- "$text" "$file" || fail "missing '$text' in $file"
}

HOME_DIR="$TMP_ROOT/home"
OUT_ROOT="$TMP_ROOT/output"
WORKSPACE="$HOME_DIR/agent_workspace"
FLEET="$WORKSPACE/jarrettdustinqq-fleet"
mkdir -p "$FLEET/ops" "$FLEET/tests" "$OUT_ROOT"

git -C "$WORKSPACE" init -q
git -C "$WORKSPACE" config user.email test@example.invalid
git -C "$WORKSPACE" config user.name test
printf 'tracked\n' > "$WORKSPACE/tracked.txt"
git -C "$WORKSPACE" add tracked.txt
git -C "$WORKSPACE" commit -qm init

cat > "$FLEET/fleetctl" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf 'ok\n'
SH
chmod +x "$FLEET/fleetctl"

cat > "$FLEET/ops/control_hub_agent.py" <<'PY'
print("ok")
PY

cat > "$FLEET/tests/test_smoke.py" <<'PY'
import unittest


class SmokeTest(unittest.TestCase):
    def test_ok(self):
        self.assertTrue(True)
PY

git -C "$FLEET" init -q
git -C "$FLEET" config user.email test@example.invalid
git -C "$FLEET" config user.name test
git -C "$FLEET" add .
git -C "$FLEET" commit -qm init

FLEET_ALLOW_OTHER_USER=1 \
FLEET_USER_HOME="$HOME_DIR" \
FLEET_EXPECTED_USER="$(id -un)" \
  "$SCRIPT" \
    --quick \
    --no-fetch \
    --output-root "$OUT_ROOT" \
    --outer-workspace "$WORKSPACE" \
    --fleet-dir "$FLEET"

LATEST_POINTER="$HOME_DIR/.local/state/fleet-system-remediation/latest-report"
assert_file "$LATEST_POINTER"
REPORT="$(cat "$LATEST_POINTER")"
assert_file "$REPORT"
SUMMARY="$(dirname "$REPORT")/summary.json"
assert_file "$SUMMARY"
assert_contains "$REPORT" "REQUIRED_FAILURE_COUNT=0"
assert_contains "$REPORT" "CANONICAL_REPOSITORY_COUNT=2"
assert_contains "$REPORT" "NO_GIT_METADATA_MOVED=YES"
assert_contains "$REPORT" "NO_DATABASE_DELETED=YES"
assert_contains "$REPORT" "NO_SYSTEMD_UNIT_CHANGED=YES"
assert_contains "$REPORT" "PYTHON_ENTRYPOINT_ABSENT=$FLEET/ops/control_hub_safe_entry.py"

ARCHIVE="$(find "$(dirname "$REPORT")/backups" -type f -name '*.tar.gz' -print -quit)"
[ -n "$ARCHIVE" ] || fail "archive not created"
sha256sum -c "$ARCHIVE.sha256" >/dev/null

python3 - "$SUMMARY" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text())
assert payload["required_failures"] == 0
assert payload["git_metadata_moved"] is False
assert payload["database_deleted"] is False
assert payload["systemd_unit_changed"] is False
PY

printf 'FLEET_HOST_REMEDIATION_TESTS=PASS\n'
