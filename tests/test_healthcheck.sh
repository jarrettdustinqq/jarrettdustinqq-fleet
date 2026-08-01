#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HEALTHCHECK="$REPO_DIR/healthcheck.sh"
TMP_ROOT="$(mktemp -d /tmp/fleet-health-test.XXXXXX)"

cleanup() {
  case "$TMP_ROOT" in
    /tmp/fleet-health-test.*) rm -rf -- "$TMP_ROOT" ;;
    *) printf 'Refusing unsafe cleanup path: %s\n' "$TMP_ROOT" >&2 ;;
  esac
}
trap cleanup EXIT

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

assert_contains() {
  local output="$1"
  local expected="$2"
  local label="$3"
  [[ "$output" == *"$expected"* ]] || fail "$label: missing output: $expected"
}

assert_not_contains() {
  local output="$1"
  local unexpected="$2"
  local label="$3"
  [[ "$output" != *"$unexpected"* ]] || fail "$label: leaked output: $unexpected"
}

prepare_case() {
  local name="$1"
  local case_dir="$TMP_ROOT/$name"

  mkdir -p "$case_dir/bin" "$case_dir/home" "$case_dir/projects"
  cat > "$case_dir/bin/nix" <<'EOF'
#!/usr/bin/env bash
printf 'nix (Fleet health test)\n'
EOF
  chmod +x "$case_dir/bin/nix"
  printf '%s\n' "$case_dir"
}

write_gh_success() {
  local bin_dir="$1"

  cat > "$bin_dir/gh" <<'EOF'
#!/usr/bin/env bash
[ "${GH_PROMPT_DISABLED:-}" = "1" ] || exit 90
[ "${GIT_TERMINAL_PROMPT:-}" = "0" ] || exit 91
case "${1:-}" in
  config)
    printf 'https\n'
    ;;
  auth)
    printf 'AUTH_SECRET_MUST_NOT_LEAK\n'
    printf 'AUTH_SECRET_MUST_NOT_LEAK\n' >&2
    ;;
  *)
    exit 92
    ;;
esac
EOF
  chmod +x "$bin_dir/gh"
}

write_gh_failure() {
  local bin_dir="$1"

  cat > "$bin_dir/gh" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in
  config)
    printf 'https\n'
    ;;
  auth)
    printf 'AUTH_SECRET_MUST_NOT_LEAK\n' >&2
    exit 1
    ;;
  *)
    exit 1
    ;;
esac
EOF
  chmod +x "$bin_dir/gh"
}

write_gh_must_not_run() {
  local bin_dir="$1"

  cat > "$bin_dir/gh" <<'EOF'
#!/usr/bin/env bash
printf 'GH_MUST_NOT_RUN\n' >&2
exit 99
EOF
  chmod +x "$bin_dir/gh"
}

run_health() {
  local case_dir="$1"

  env \
    HOME="$case_dir/home" \
    PROJECTS_DIR="$case_dir/projects" \
    SSH_KEY_PATH="$case_dir/home/.ssh/id_ed25519" \
    PATH="$case_dir/bin:/usr/bin:/bin" \
    bash "$HEALTHCHECK" 2>&1
}

ssh_case="$(prepare_case ssh)"
mkdir -p "$ssh_case/home/.ssh"
: > "$ssh_case/home/.ssh/id_ed25519"
: > "$ssh_case/home/.ssh/id_ed25519.pub"
write_gh_must_not_run "$ssh_case/bin"
ssh_output="$(run_health "$ssh_case")"
assert_contains "$ssh_output" "GitHub auth available: SSH keypair" "SSH auth"
assert_not_contains "$ssh_output" "GH_MUST_NOT_RUN" "SSH auth"

https_case="$(prepare_case https)"
write_gh_success "$https_case/bin"
https_output="$(run_health "$https_case")"
assert_contains "$https_output" "GitHub auth available: authenticated gh CLI over HTTPS" "HTTPS auth"
assert_not_contains "$https_output" "AUTH_SECRET_MUST_NOT_LEAK" "HTTPS auth"

failure_case="$(prepare_case failure)"
write_gh_failure "$failure_case/bin"
if failure_output="$(run_health "$failure_case")"; then
  fail "missing auth unexpectedly passed"
else
  failure_rc=$?
fi
[ "$failure_rc" -eq 1 ] || fail "missing auth returned $failure_rc instead of 1"
assert_contains "$failure_output" "GitHub auth unavailable" "missing auth"
assert_not_contains "$failure_output" "AUTH_SECRET_MUST_NOT_LEAK" "missing auth"

printf 'HEALTHCHECK_TESTS=PASS\n'
